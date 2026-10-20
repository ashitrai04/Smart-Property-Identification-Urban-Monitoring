"""Centre-aligned OSM lines + measured width + buffered polygons, all 24 clips.

Same output as ongole_roads_clip_04.gpkg, for the whole district:
  road_lines      OSM geometry, original vertices, shifted onto the road
  road_polygons   each line buffered by ITS OWN measured width
  road_surface    merged, for area totals

Per clip rather than city-wide because the signal is 2.6 gigapixels - about
10 GB as float32 - and a clip is ~70 megapixels. The signal itself is read from
the prebuilt raster instead of recomputed, which is what made the first per-clip
version take 16 minutes each.
"""
import os
import time

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import from_bounds
from scipy.ndimage import generic_filter, map_coordinates, uniform_filter
from shapely.geometry import LineString, MultiLineString
from shapely.ops import unary_union

SIGNAL = "D:/GEO_SERVE/_kb/ongole_road_signal.tif"
OSM = "C:/Users/asash/Downloads/Roads.gpkg"
CLIPS = "C:/Users/asash/Downloads/clip_index.gpkg"
OUT = "D:/GEO_SERVE/ongole_roads_district.gpkg"
M = 32644
PAD = 200.0

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
ABS_MIN, ABS_MAX, SAMPLE, FINE = 1.5, 30.0, 2.0, 0.30


class Score:
    def __init__(self, path, bounds):
        ds = rasterio.open(path)
        win = from_bounds(*bounds, ds.transform)
        self.fine = (ds.read(1, window=win, boundless=True, fill_value=0)
                     .astype(np.float32) / 255.0)
        t = ds.window_transform(win)
        self.finv = ~t
        h, w = self.fine.shape
        self.coarse = (ds.read(1, window=win, out_shape=(h // 2, w // 2),
                               resampling=Resampling.average, boundless=True,
                               fill_value=0).astype(np.float32) / 255.0)
        self.cinv = ~rasterio.Affine(t.a * 2, t.b, t.c, t.d, t.e * 2, t.f)

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
        if (r - l) * FINE > 0:
            out.append((r - l) * FINE)
    if len(out) < max(2, MIN_VALID * n):
        return None
    return float(np.median(out))


def do_clip(g, S, bounds):
    parts = []
    for i, geom in enumerate(g.geometry.values):
        for ls in ([geom] if geom.geom_type == "LineString" else list(geom.geoms)):
            if ls.length > 0.5:
                parts.append((i, ls))
    if not parts:
        return None, 0, 0, 0.0, 0.0
    verts = [np.asarray(ls.coords, float)[:, :2] for _, ls in parts]
    scor = [dense(ls) for _, ls in parts]
    P = np.vstack(scor)

    x0, y0 = bounds[0], bounds[1]
    nx = int((bounds[2] - x0) / CELL) + 1
    ny = int((bounds[3] - y0) / CELL) + 1
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

    byp = {}
    for (parent, _), line in zip(parts, out_lines):
        byp.setdefault(parent, []).append(LineString(line))
    geoms = [(byp[i][0] if len(byp[i]) == 1 else MultiLineString(byp[i]))
             if i in byp else g.geometry.iloc[i] for i in range(len(g))]
    al = g.copy()
    al["geometry"] = geoms
    al = al.set_geometry("geometry")

    wc, wm, wu, srcs = [], [], [], []
    for geom, fc in zip(al.geometry.values, al["fclass"].fillna("")):
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
    al["width_fclass"], al["width_measured"] = wc, wm
    al["width_m"], al["width_src"] = wu, srcs
    al["area_m2"] = (al["width_m"] * al.length).round(1)
    return al, kept, len(uniq), 100 * float((before > THR).mean()), 100 * float((after > THR).mean())


def main():
    t0 = time.time()
    osm = gpd.read_file(OSM).to_crs(M)
    ci = gpd.read_file(CLIPS, layer="clip_index").to_crs(M)
    frames, rows, seen = [], [], set()
    for i, c in enumerate(sorted(ci["clip"]), 1):
        poly = ci[ci["clip"] == c].geometry.iloc[0]
        # centroid-in-clip so a road is handled once, geometry clipped to no clip
        sub = osm[osm.geometry.centroid.within(poly) & ~osm.index.isin(seen)]
        if not len(sub):
            print(f"[{i}/24] {c}: none", flush=True)
            continue
        seen |= set(sub.index)
        b = poly.bounds
        bb = (b[0] - PAD, b[1] - PAD, b[2] + PAD, b[3] + PAD)
        S = Score(SIGNAL, bb)
        al, kept, tot, bef, aft = do_clip(sub.reset_index(drop=True), S, bb)
        if al is None:
            continue
        al["clip"] = c
        frames.append(al)
        nm = int(al["width_src"].str.startswith("measured").sum())
        rows.append({"clip": c, "roads": len(al), "km": round(float(al.length.sum()/1000), 2),
                     "measured": nm, "fallback": len(al) - nm,
                     "cells": f"{kept}/{tot}", "on_road_before": round(bef, 1),
                     "on_road_after": round(aft, 1)})
        print(f"[{i}/24] {c}: {len(al)} roads {al.length.sum()/1000:.1f} km | "
              f"cells {kept}/{tot} | on-road {bef:.1f}%->{aft:.1f}% | "
              f"measured {nm}/{len(al)}  ({time.time()-t0:.0f}s)", flush=True)

    al = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=M)
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
    pd.DataFrame(rows).to_csv("D:/GEO_SERVE/_kb/district_roads_by_clip.csv", index=False)

    nm = int(al["width_src"].str.startswith("measured").sum())
    print(f"\n{'fclass':<16}{'n':>6}{'table':>8}{'measured':>10}{'used':>8}", flush=True)
    for fc in sorted(set(al["fclass"].fillna(""))):
        sel = al["fclass"].fillna("") == fc
        mmm = al.loc[sel, "width_measured"].dropna()
        if int(sel.sum()) < 3:
            continue
        print(f"  {(fc or '(blank)')[:14]:<14}{int(sel.sum()):>6}"
              f"{FCLASS_W.get(str(fc).lower(), DEFAULT_W):>8.1f}"
              f"{(f'{mmm.median():.1f}' if len(mmm) else '-'):>10}"
              f"{al.loc[sel,'width_m'].median():>8.1f}", flush=True)
    print(f"\ndistrict: {len(al)} roads | {al.length.sum()/1000:.1f} km | "
          f"surface {merged.area/1e4:.1f} ha | measured {nm}, table {len(al)-nm}", flush=True)
    print(f"wrote {OUT}  ({(time.time()-t0)/60:.1f} min)", flush=True)


if __name__ == "__main__":
    main()
