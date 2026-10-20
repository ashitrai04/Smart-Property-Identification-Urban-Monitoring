"""Whole-city OSM roads: aligned to the drone ortho, widths measured, polygons out.

Reads the fused road signal built by city_signal.py and does, over all 806 km:

  1. block shift  - 150 m cells, +/-15 m at 60 cm steps, median-filtered and
                    interpolated between cell centres
  2. line nudge   - ONE rigid sideways offset per line, so OSM geometry keeps
                    its original vertices and stays straight
  3. width        - perpendicular transects every 10 m, median of the unbroken
                    run of road through the centre, clamped against the fclass
                    table and falling back to it where the road is not visible
  4. polygons     - buffer each line by its own width, flat caps

Outputs D:/GEO_SERVE/ongole_roads_city.gpkg with road_lines, road_polygons and
a merged road_surface, plus a per-clip breakdown.
"""
import json
import os
import time

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.enums import Resampling
from scipy.ndimage import generic_filter, map_coordinates, uniform_filter
from shapely.geometry import LineString, MultiLineString
from shapely.ops import unary_union

SIGNAL = "D:/GEO_SERVE/_kb/ongole_road_signal.tif"
OSM = "C:/Users/asash/Downloads/Roads.gpkg"
CLIPS = "C:/Users/asash/Downloads/clip_index.gpkg"
OUT = "D:/GEO_SERVE/ongole_roads_city.gpkg"
REPORT = "D:/GEO_SERVE/_kb/city_roads_report.json"
M = 32644

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
ABS_MIN, ABS_MAX, SAMPLE = 1.5, 30.0, 2.0
FINE = 0.30          # width transect step


class Score:
    """Signal at two scales: coarse for the +/-15 m search, fine for widths."""

    def __init__(self, path):
        ds = rasterio.open(path)
        t = ds.transform
        self.fine = ds.read(1).astype(np.float32) / 255.0
        self.finv = ~t
        k = 2
        self.coarse = ds.read(1, out_shape=(ds.height // k, ds.width // k),
                              resampling=Resampling.average).astype(np.float32) / 255.0
        self.cinv = ~rasterio.Affine(t.a * k, t.b, t.c, t.d, t.e * k, t.f)
        print(f"  signal {ds.width}x{ds.height} @{abs(t.a):.2f} m "
              f"({self.fine.nbytes/2**20:.0f} MB) | >0.35 covers "
              f"{100*(self.fine>THR).mean():.1f}%", flush=True)

    def at(self, xy, fine=False):
        a, inv = (self.fine, self.finv) if fine else (self.coarse, self.cinv)
        c, r = inv * (xy[:, 0], xy[:, 1])
        return map_coordinates(a, [r, c], order=1, mode="constant", cval=0)


def dense(ls, every=SAMPLE):
    n = max(2, int(ls.length / every) + 1)
    return np.array([[p.x, p.y] for p in
                     (ls.interpolate(i / (n - 1), normalized=True) for i in range(n))])


def width_of(line, S):
    n = max(2, int(line.length / STEP_ALONG) + 1)
    offs = np.arange(-HALF, HALF + 1e-9, FINE)
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
        v = S.at(np.c_[p.x + nx * offs, p.y + ny * offs], fine=True) > THR
        if not v[mid]:
            continue
        l = mid
        while l > 0 and v[l - 1]:
            l -= 1
        r = mid
        while r < len(v) - 1 and v[r + 1]:
            r += 1
        w = (r - l) * FINE
        if w > 0:
            out.append(w)
    if len(out) < max(2, MIN_VALID * n):
        return None
    return float(np.median(out))


def main():
    t0 = time.time()
    g = gpd.read_file(OSM)
    src_crs = g.crs
    g = g.to_crs(M).reset_index(drop=True)
    print(f"OSM {len(g)} roads, {g.length.sum()/1000:.1f} km", flush=True)
    S = Score(SIGNAL)

    parts = []
    for i, geom in enumerate(g.geometry.values):
        for ls in ([geom] if geom.geom_type == "LineString" else list(geom.geoms)):
            if ls.length > 0.5:
                parts.append((i, ls))
    verts = [np.asarray(ls.coords, float)[:, :2] for _, ls in parts]
    scor = [dense(ls) for _, ls in parts]
    P = np.vstack(scor)
    print(f"  {len(parts)} lines, {sum(len(v) for v in verts):,} vertices", flush=True)

    x0, y0, x1, y1 = g.total_bounds
    nx = int((x1 - x0) / CELL) + 1
    ny = int((y1 - y0) / CELL) + 1
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
    print(f"  cells trusted {kept}/{len(uniq)}  ({time.time()-t0:.0f}s)", flush=True)

    def sm(a):
        f = a.copy()
        for _ in range(4):
            fill = generic_filter(np.nan_to_num(f), np.nanmedian, size=3, mode="nearest")
            cnt = generic_filter((~np.isnan(f)).astype(float), np.sum, size=3, mode="nearest")
            f = np.where(np.isnan(f) & (cnt >= 2), fill, f)
        return uniform_filter(np.nan_to_num(f), size=3, mode="nearest")

    sx, sy = sm(shift[..., 0]), sm(shift[..., 1])

    def mv(xy):
        fc = (xy[:, 0] - x0) / CELL - 0.5
        fr = (xy[:, 1] - y0) / CELL - 0.5
        return xy + np.c_[map_coordinates(sx, [fr, fc], order=1, mode="nearest"),
                          map_coordinates(sy, [fr, fc], order=1, mode="nearest")]

    before = S.at(P, fine=True)
    out_lines, after = [], []
    cand = np.arange(-MAXPERP, MAXPERP + 1e-9, 0.25)
    for k, (_, ls) in enumerate(parts):
        v, sp = mv(verts[k]), mv(scor[k])
        d = v[-1] - v[0]
        L = np.hypot(*d)
        if L < 1e-6:
            out_lines.append(v); after.append(sp); continue
        nrm = np.array([-d[1], d[0]]) / L
        q = (sp[None, :, :] + nrm[None, None, :] * cand[:, None, None]).reshape(-1, 2)
        sc = S.at(q, fine=True).reshape(len(cand), -1).mean(axis=1)
        j = int(np.argmax(sc))
        off = float(cand[j]) if sc[j] - sc[len(cand) // 2] > PERP_GAIN else 0.0
        out_lines.append(v + nrm * off)
        after.append(sp + nrm * off)
    after = S.at(np.vstack(after), fine=True)
    print(f"  on road >{THR}: {100*(before>THR).mean():.1f}% -> "
          f"{100*(after>THR).mean():.1f}%  ({time.time()-t0:.0f}s)", flush=True)

    byp = {}
    for (parent, _), line in zip(parts, out_lines):
        byp.setdefault(parent, []).append(LineString(line))
    geoms = [(byp[i][0] if len(byp[i]) == 1 else MultiLineString(byp[i]))
             if i in byp else g.geometry.iloc[i] for i in range(len(g))]
    al = g.copy()
    al["geometry"] = geoms
    al = al.set_geometry("geometry")

    wc, wm, wu, srcs = [], [], [], []
    for n, (geom, fc) in enumerate(zip(al.geometry.values, al["fclass"].fillna("")), 1):
        key = str(fc).lower()
        base = FCLASS_W.get(key, DEFAULT_W)
        hi = 1.3 if key in FOOT else HI_MULT
        meas = [w for ls in ([geom] if geom.geom_type == "LineString" else list(geom.geoms))
                if ls.length >= 2 for w in [width_of(ls, S)] if w is not None]
        wc.append(base)
        if meas:
            m = float(np.median(meas))
            c = float(np.clip(m, max(ABS_MIN, base * LO_MULT), min(ABS_MAX, base * hi)))
            wm.append(round(m, 2)); wu.append(round(c, 2))
            srcs.append("measured" if abs(c - m) < 0.01 else "measured-clamped")
        else:
            wm.append(None); wu.append(base); srcs.append("fclass")
        if n % 500 == 0:
            print(f"    width {n}/{len(al)}  ({time.time()-t0:.0f}s)", flush=True)
    al["width_fclass"], al["width_measured"] = wc, wm
    al["width_m"], al["width_src"] = wu, srcs
    al["area_m2"] = (al["width_m"] * al.length).round(1)

    pg = al.copy()
    pg["geometry"] = [ln.buffer(w / 2, cap_style=2, join_style=2)
                      for ln, w in zip(al.geometry.values, al["width_m"])]
    if os.path.exists(OUT):
        os.remove(OUT)
    al.to_crs(4326).to_file(OUT, layer="road_lines", driver="GPKG")
    pg.to_crs(4326).to_file(OUT, layer="road_polygons", driver="GPKG")
    merged = unary_union(pg.geometry.values)
    gpd.GeoDataFrame({"city": ["Ongole"], "roads": [len(al)],
                      "length_km": [round(float(al.length.sum()/1000), 2)],
                      "surface_ha": [round(merged.area/1e4, 2)]},
                     geometry=[merged], crs=M).to_crs(4326).to_file(
        OUT, layer="road_surface", driver="GPKG")

    # per-clip summary, so each clip can be checked on its own
    ci = gpd.read_file(CLIPS, layer="clip_index").to_crs(M)
    rows = []
    cent = al.geometry.centroid
    for _, c in ci.iterrows():
        sel = cent.within(c.geometry)
        if not sel.any():
            continue
        sub = al[sel]
        rows.append({"clip": c["clip"], "roads": int(sel.sum()),
                     "length_km": round(float(sub.length.sum()/1000), 2),
                     "surface_ha": round(float(sub["area_m2"].sum()/1e4), 2),
                     "measured_pct": round(100*float(sub["width_src"].str.startswith("measured").mean()), 1)})
    nmeas = sum(1 for s in srcs if s.startswith("measured"))
    rep = {"built": time.strftime("%Y-%m-%d %H:%M"), "roads": int(len(al)),
           "length_km": round(float(al.length.sum()/1000), 2),
           "surface_ha": round(merged.area/1e4, 2),
           "vertices_in": int(sum(len(np.asarray(ls.coords)) for _, ls in parts)),
           "cells_trusted": int(kept), "cells_total": int(len(uniq)),
           "on_road_pct": {"before": round(100*float((before > THR).mean()), 1),
                           "after": round(100*float((after > THR).mean()), 1)},
           "width_measured": nmeas, "width_fallback": len(srcs) - nmeas,
           "per_clip": rows}
    json.dump(rep, open(REPORT, "w"), indent=1)

    print(f"\n{'fclass':<14}{'n':>5}{'table':>7}{'measured':>10}{'used':>7}", flush=True)
    for fc in sorted(set(al["fclass"].fillna(""))):
        sel = al["fclass"].fillna("") == fc
        mmm = al.loc[sel, "width_measured"].dropna()
        print(f"  {(fc or '(blank)')[:12]:<12}{int(sel.sum()):>5}"
              f"{FCLASS_W.get(str(fc).lower(), DEFAULT_W):>7.1f}"
              f"{(f'{mmm.median():.1f}' if len(mmm) else '-'):>10}"
              f"{al.loc[sel,'width_m'].median():>7.1f}", flush=True)
    print(f"\nroads {len(al)} | {al.length.sum()/1000:.1f} km | surface "
          f"{merged.area/1e4:.1f} ha | measured {nmeas}, table {len(srcs)-nmeas}", flush=True)
    print(f"wrote {OUT}  ({(time.time()-t0)/60:.1f} min)", flush=True)


if __name__ == "__main__":
    main()
