"""Per-clip road alignment + width, driven by the drone imagery itself.

Why this exists. The first pass aligned OSM against the SegFormer road
probability computed at 30 cm. In the dense centre that worked. On the fringes
it did not: the probability there is faint and smeared, so the block search had
nothing to lock onto, the lines stayed off the road, and width measurement
failed and fell back to the fclass table.

The drone ortho is 3 cm and the roads are plainly visible, so the road signal is
built from the image instead:

  grey + smooth    tarmac has low saturation, little greenness, little texture
  ridge filter     a road is a long thin ribbon; a concrete roof is a blob, and
                   this is what separates the two, since both are grey and smooth
  + SegFormer      kept as a second opinion, so places the model is confident
                   about still count

Then, with that signal:
  1. block shift   150 m cells, +/-15 m, as before
  2. line nudge    one rigid sideways offset per line - never per piece, so the
                   OSM geometry stays exactly as drawn
  3. width         perpendicular transects every 10 m, median of the unbroken
                   run through the centre, clamped against the fclass table
"""
import json
import os
import sys
import time

import geopandas as gpd
import numpy as np
import rasterio
import cv2
from rasterio.windows import from_bounds
from rasterio.enums import Resampling
from scipy.ndimage import generic_filter, map_coordinates, uniform_filter
from shapely.geometry import LineString, MultiLineString
from shapely.ops import unary_union
from skimage.filters import sato

DRONE = "D:/GEO_SERVE/Ongole_ORI_V3_jpeg92.tif"
SEG = "D:/GEO_SERVE/_kb/ongole_drone_road_prob.tif"
OSM = "C:/Users/asash/Downloads/Roads.gpkg"
CLIPS = "C:/Users/asash/Downloads/clip_index.gpkg"
OUTDIR = "D:/GEO_SERVE"
M = 32644
RES = 0.25                 # working resolution for the road signal
PAD = 60.0                 # metres of context around the clip

FCLASS_W = {"motorway": 18, "trunk": 14, "primary": 9, "secondary": 7,
            "tertiary": 5, "residential": 3, "service": 2.5,
            "unclassified": 2, "track": 1.5,
            "motorway_link": 9, "trunk_link": 7, "primary_link": 5,
            "secondary_link": 4, "tertiary_link": 3,
            "living_street": 3, "residential_link": 3,
            "pedestrian": 4, "footway": 1.5, "path": 1.2, "steps": 1.2,
            "cycleway": 1.5, "bridleway": 1.2}
FOOT = {"pedestrian", "footway", "path", "steps", "cycleway", "bridleway"}
DEFAULT_W = 3.0

CELL, MAXSHIFT, STEP = 150.0, 15.0, 0.6
MIN_LEN, MIN_SCORE, MIN_MARGIN = 60.0, 0.20, 0.02
MAXPERP, PERP_GAIN = 6.0, 0.03
STEP_ALONG, HALF, THR = 10.0, 14.0, 0.35
MIN_VALID, LO_MULT, HI_MULT = 0.35, 0.5, 2.5
ABS_MIN, ABS_MAX = 1.5, 30.0
SAMPLE = 2.0


def nz(x, lo, hi):
    return np.clip((x - lo) / max(hi - lo, 1e-9), 0, 1)


def road_signal(bounds):
    """Fused road-likeness over the clip, as an array + its transform."""
    w, s, e, n = bounds
    W = int((e - w) / RES)
    H = int((n - s) / RES)
    dr = rasterio.open(DRONE)
    a = dr.read([1, 2, 3], window=from_bounds(w, s, e, n, dr.transform),
                out_shape=(3, H, W), resampling=Resampling.average,
                boundless=True, fill_value=0)
    rgb = np.ascontiguousarray(np.transpose(a, (1, 2, 0)))
    f = rgb.astype(np.float32)
    R, G, B = f[..., 0], f[..., 1], f[..., 2]
    sat = f.max(2) - f.min(2)
    exg = 2 * G - R - B
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    tex = cv2.GaussianBlur(cv2.absdiff(gray, cv2.bilateralFilter(gray, 9, 40, 9))
                           .astype(np.float32), (0, 0), 3)
    surf = (1 - nz(sat, 10, 60)) * (1 - nz(exg, -5, 40)) * (1 - nz(tex, 1.5, 9))
    surf = cv2.GaussianBlur(surf, (0, 0), 2)
    valid = rgb.max(2) > 8
    if valid.sum() < 1000:
        return None, None, rgb
    surf = nz(surf, np.percentile(surf[valid], 55), np.percentile(surf[valid], 99))
    ridge = sato(surf, sigmas=[x / 2 / RES for x in (2.5, 5, 9)], black_ridges=False)
    ridge = nz(ridge, np.percentile(ridge[valid], 70), np.percentile(ridge[valid], 99.5))
    img_road = nz(surf * ridge, 0.02, 0.5)
    sg = rasterio.open(SEG)
    p = sg.read(1, window=from_bounds(w, s, e, n, sg.transform), out_shape=(H, W),
                resampling=Resampling.bilinear, boundless=True, fill_value=0) / 255.0
    fused = np.clip(np.maximum(p, img_road * 0.95), 0, 1).astype(np.float32)
    fused[~valid] = 0
    tr = rasterio.Affine(RES, 0, w, 0, -RES, n)
    return fused, tr, rgb


class Score:
    def __init__(self, arr, tr):
        self.a = arr
        self.inv = ~tr

    def at(self, xy):
        c, r = self.inv * (xy[:, 0], xy[:, 1])
        return map_coordinates(self.a, [r, c], order=1, mode="constant", cval=0)


def dense(ls, every=SAMPLE):
    n = max(2, int(ls.length / every) + 1)
    return np.array([[p.x, p.y] for p in
                     (ls.interpolate(i / (n - 1), normalized=True) for i in range(n))])


def width_of(line, S, foot):
    n = max(2, int(line.length / STEP_ALONG) + 1)
    offs = np.arange(-HALF, HALF + 1e-9, RES)
    mid = len(offs) // 2
    out = []
    for i in range(n):
        t = i / (n - 1)
        p = line.interpolate(t, normalized=True)
        d = 1.0 / max(line.length, 1e-6)
        a = line.interpolate(max(0.0, t - d * 5), normalized=True)
        b = line.interpolate(min(1.0, t + d * 5), normalized=True)
        vx, vy = b.x - a.x, b.y - a.y
        L = np.hypot(vx, vy)
        if L < 1e-9:
            continue
        nx, ny = -vy / L, vx / L
        v = S.at(np.c_[p.x + nx * offs, p.y + ny * offs]) > THR
        if not v[mid]:
            continue
        l = mid
        while l > 0 and v[l - 1]:
            l -= 1
        r = mid
        while r < len(v) - 1 and v[r + 1]:
            r += 1
        w = (r - l) * RES
        if w > 0:
            out.append(w)
    if len(out) < max(2, MIN_VALID * n):
        return None, len(out), n
    return float(np.median(out)), len(out), n


def main(clip="clip_04"):
    t0 = time.time()
    ci = gpd.read_file(CLIPS, layer="clip_index").to_crs(M)
    poly = ci[ci["clip"] == clip].geometry.iloc[0]
    b = poly.bounds
    bb = (b[0] - PAD, b[1] - PAD, b[2] + PAD, b[3] + PAD)
    g = gpd.read_file(OSM).to_crs(M)
    g = g[g.geometry.intersects(poly)].reset_index(drop=True)
    print(f"{clip}: {poly.area/1e6:.2f} km2, {len(g)} roads, {g.length.sum()/1000:.1f} km",
          flush=True)

    arr, tr, _ = road_signal(bb)
    if arr is None:
        print("  no imagery over this clip", flush=True)
        return
    print(f"  road signal {arr.shape[1]}x{arr.shape[0]} @{RES} m  "
          f">0.35 covers {100*(arr>THR).mean():.1f}%  ({time.time()-t0:.0f}s)", flush=True)
    S = Score(arr, tr)

    parts = []
    for i, geom in enumerate(g.geometry.values):
        for ls in ([geom] if geom.geom_type == "LineString" else list(geom.geoms)):
            if ls.length > 0.5:
                parts.append((i, ls))
    verts = [np.asarray(ls.coords, float)[:, :2] for _, ls in parts]
    scor = [dense(ls) for _, ls in parts]
    P = np.vstack(scor)

    x0, y0 = bb[0], bb[1]
    nx = int((bb[2] - x0) / CELL) + 1
    ny = int((bb[3] - y0) / CELL) + 1
    cid = (((P[:, 1] - y0) / CELL).astype(int) * nx + ((P[:, 0] - x0) / CELL).astype(int))
    offs = np.arange(-MAXSHIFT, MAXSHIFT + 1e-9, STEP)
    TRL = np.array([(dx, dy) for dy in offs for dx in offs], np.float32)
    order = np.argsort(cid)
    cs, Ps = cid[order], P[order]
    uniq, st = np.unique(cs, return_index=True)
    en = np.r_[st[1:], len(cs)]
    shift = np.full((ny, nx, 2), np.nan, np.float32)
    kept = 0
    for cv_, lo, hi in zip(uniq, st, en):
        if (hi - lo) * SAMPLE < MIN_LEN:
            continue
        pp = Ps[lo:hi]
        q = (pp[None, :, :] + TRL[:, None, :]).reshape(-1, 2)
        sc = S.at(q).reshape(len(TRL), -1).mean(axis=1)
        bi = int(np.argmax(sc))
        if sc[bi] < MIN_SCORE or sc[bi] - float(np.median(sc)) < MIN_MARGIN:
            continue
        r, c = divmod(int(cv_), nx)
        shift[r, c] = TRL[bi]
        kept += 1
    print(f"  cells trusted {kept}/{len(uniq)}", flush=True)

    def sm(a):
        f = a.copy()
        for _ in range(4):
            fill = generic_filter(np.nan_to_num(f), np.nanmedian, size=3, mode="nearest")
            cnt = generic_filter((~np.isnan(f)).astype(float), np.sum, size=3, mode="nearest")
            f = np.where(np.isnan(f) & (cnt >= 2), fill, f)
        return uniform_filter(np.nan_to_num(f), size=3, mode="nearest")

    sx, sy = sm(shift[..., 0]), sm(shift[..., 1])

    def fld(xy, F):
        return map_coordinates(F, [(xy[:, 1] - y0) / CELL - 0.5,
                                   (xy[:, 0] - x0) / CELL - 0.5], order=1, mode="nearest")

    def mv(xy):
        return xy + np.c_[fld(xy, sx), fld(xy, sy)]

    before = S.at(P)
    out_lines, after = [], []
    for k, (_, ls) in enumerate(parts):
        v, sp = mv(verts[k]), mv(scor[k])
        d = v[-1] - v[0]
        L = np.hypot(*d)
        if L < 1e-6:
            out_lines.append(v); after.append(sp); continue
        nrm = np.array([-d[1], d[0]]) / L
        cand = np.arange(-MAXPERP, MAXPERP + 1e-9, 0.25)
        q = (sp[None, :, :] + nrm[None, None, :] * cand[:, None, None]).reshape(-1, 2)
        sc = S.at(q).reshape(len(cand), -1).mean(axis=1)
        j = int(np.argmax(sc))
        off = float(cand[j]) if sc[j] - sc[len(cand) // 2] > PERP_GAIN else 0.0
        out_lines.append(v + nrm * off)
        after.append(sp + nrm * off)
    after = S.at(np.vstack(after))
    print(f"  on road >{THR}: {100*(before>THR).mean():.1f}% -> {100*(after>THR).mean():.1f}%",
          flush=True)

    byp = {}
    for (parent, _), line in zip(parts, out_lines):
        byp.setdefault(parent, []).append(LineString(line))
    geoms = [byp[i][0] if len(byp.get(i, [])) == 1 else
             (MultiLineString(byp[i]) if i in byp else g.geometry.iloc[i])
             for i in range(len(g))]
    al = g.copy()
    al["geometry"] = geoms
    al = al.set_geometry("geometry")

    wc, wm, wu, srcs = [], [], [], []
    for geom, fc in zip(al.geometry.values, al["fclass"].fillna("")):
        key = str(fc).lower()
        base = FCLASS_W.get(key, DEFAULT_W)
        hi = 1.3 if key in FOOT else HI_MULT
        meas = []
        for ls in ([geom] if geom.geom_type == "LineString" else list(geom.geoms)):
            if ls.length < 2:
                continue
            w, _, _ = width_of(ls, S, key in FOOT)
            if w is not None:
                meas.append(w)
        wc.append(base)
        if meas:
            m = float(np.median(meas))
            c = float(np.clip(m, max(ABS_MIN, base * LO_MULT), min(ABS_MAX, base * hi)))
            wm.append(round(m, 2)); wu.append(round(c, 2))
            srcs.append("measured" if abs(c - m) < 0.01 else "measured-clamped")
        else:
            wm.append(None); wu.append(base); srcs.append("fclass")
    al["width_fclass"], al["width_measured"] = wc, wm
    al["width_m"], al["width_src"] = wu, srcs
    al["area_m2"] = (al["width_m"] * al.length).round(1)

    out = f"{OUTDIR}/ongole_roads_{clip}.gpkg"
    if os.path.exists(out):
        os.remove(out)
    al.to_crs(4326).to_file(out, layer="road_lines", driver="GPKG")
    pg = al.copy()
    pg["geometry"] = [ln.buffer(w / 2, cap_style=2, join_style=2)
                      for ln, w in zip(al.geometry.values, al["width_m"])]
    pg.to_crs(4326).to_file(out, layer="road_polygons", driver="GPKG")
    surf = gpd.GeoDataFrame({"clip": [clip], "roads": [len(al)],
                             "length_km": [round(float(al.length.sum() / 1000), 2)]},
                            geometry=[unary_union(pg.geometry.values)], crs=M)
    surf["surface_ha"] = (surf.geometry.area / 1e4).round(2)
    surf.to_crs(4326).to_file(out, layer="road_surface", driver="GPKG")

    nmeas = sum(1 for s in srcs if s.startswith("measured"))
    print(f"  width: measured {nmeas}, table fallback {len(srcs)-nmeas}", flush=True)
    print(f"  {'fclass':<14}{'n':>4}{'table':>7}{'measured':>10}{'used':>7}", flush=True)
    for fc in sorted(set(al["fclass"].fillna(""))):
        sel = al["fclass"].fillna("") == fc
        mm = al.loc[sel, "width_measured"].dropna()
        print(f"    {(fc or '(blank)')[:12]:<12}{int(sel.sum()):>4}"
              f"{FCLASS_W.get(str(fc).lower(), DEFAULT_W):>7.1f}"
              f"{(f'{mm.median():.1f}' if len(mm) else '-'):>10}"
              f"{al.loc[sel,'width_m'].median():>7.1f}", flush=True)
    print(f"  surface {float(surf['surface_ha'].iloc[0]):.2f} ha | wrote {out} "
          f"({time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "clip_04")
