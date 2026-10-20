"""Turn road QA from "check 3,708 roads" into "check the 200 that are wrong".

Three jobs that are painful by hand in QGIS, done from the road signal instead:

  width        already measured per road by city_roads.py - nobody types widths.
               This only flags the ones the measurement could not get.

  wrong class  a road tagged residential that measures 11 m is not residential.
               Comparing measured width against the fclass table finds those and
               suggests the class whose table width actually fits.

  missing      road surface in the imagery with no OSM line anywhere near it.
               Emitted as polygons, not lines: the surveyor still draws the
               centreline the way they are used to, but they are shown exactly
               where to draw instead of hunting for gaps.

Everything lands in one GeoPackage with a `needs_review` flag and a reason, so
QGIS can filter to just the work.
"""
import json
import os
import sys
import time

import cv2
import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import rasterize, shapes
from shapely.geometry import shape
from shapely.ops import unary_union

SIGNAL = "D:/GEO_SERVE/_kb/ongole_road_signal.tif"
ROADS = "D:/GEO_SERVE/ongole_roads_city.gpkg"
LAYER = "road_lines"
OUT = "D:/GEO_SERVE/ongole_roads_review.gpkg"
M = 32644

# vehicle classes only: a 6 m measurement should never suggest "footway"
CLASS_W = {"service": 2.5, "residential": 3, "tertiary": 5,
           "secondary": 7, "primary": 9, "trunk": 14, "motorway": 18}
FOOT = {"pedestrian", "footway", "path", "steps", "cycleway", "bridleway"}
MISMATCH = 1.8          # measured/table ratio that counts as the wrong class
THR = 0.5               # signal that counts as road surface
NEAR_M = 4.0            # a road already mapped covers this much either side
MIN_AREA = 150.0        # ignore scraps
MIN_LEN = 25.0          # and stubs


def suggest(width):
    """Closest class by table width."""
    return min(CLASS_W.items(), key=lambda kv: abs(kv[1] - width))[0]


def main(clip=None):
    t0 = time.time()
    g = gpd.read_file(ROADS, layer=LAYER).to_crs(M)
    if clip:
        ci = gpd.read_file("C:/Users/asash/Downloads/clip_index.gpkg",
                           layer="clip_index").to_crs(M)
        poly = ci[ci["clip"] == clip].geometry.iloc[0]
        g = g[g.geometry.intersects(poly)].reset_index(drop=True)
    print(f"{len(g)} roads, {g.length.sum()/1000:.1f} km", flush=True)

    # ── 1. flag roads for review ─────────────────────────────────
    reasons, suggests, review = [], [], []
    for _, r in g.iterrows():
        fc = str(r.get("fclass") or "").lower()
        w, wm, src = r["width_m"], r["width_measured"], str(r["width_src"])
        why, sug = [], ""
        if src == "fclass":
            why.append("width not measurable (tree cover or no road visible)")
        if wm is not None and fc not in FOOT and fc in CLASS_W:
            ratio = wm / max(CLASS_W[fc], 0.1)
            if ratio >= MISMATCH or ratio <= 1 / MISMATCH:
                sug = suggest(wm)
                if sug != fc:
                    why.append(f"measures {wm:.1f} m, closer to {sug}")
        if wm is not None and fc in FOOT and wm > 3.5:
            sug = suggest(wm)
            why.append(f"tagged {fc} but measures {wm:.1f} m")
        if not fc:
            why.append("no fclass")
        reasons.append("; ".join(why))
        suggests.append(sug)
        review.append(bool(why))
    g["fclass_suggest"] = suggests
    g["review_reason"] = reasons
    g["needs_review"] = review
    n_rev = int(sum(review))
    print(f"  flagged {n_rev} of {len(g)} roads ({100*n_rev/len(g):.0f}%)", flush=True)
    byr = {}
    for rr in reasons:
        for part in (rr.split("; ") if rr else []):
            k = part.split(",")[0].split(" (")[0]
            byr[k] = byr.get(k, 0) + 1
    for k, v in sorted(byr.items(), key=lambda kv: -kv[1]):
        print(f"    {v:>5}  {k}", flush=True)

    # ── 2. road surface with no OSM line near it ─────────────────
    ds = rasterio.open(SIGNAL)
    b = g.total_bounds
    win = rasterio.windows.from_bounds(*b, ds.transform)
    arr = ds.read(1, window=win)
    tr = ds.window_transform(win)
    road = (arr >= THR * 255).astype(np.uint8)
    covered = rasterize(
        [(ln.buffer(max(w / 2, 1.0) + NEAR_M), 1)
         for ln, w in zip(g.geometry.values, g["width_m"])],
        out_shape=road.shape, transform=tr, fill=0, dtype="uint8")
    gap = (road & (1 - covered)).astype(np.uint8)
    # a real missing road is a connected ribbon, not speckle
    gap = cv2.morphologyEx(gap, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    gap = cv2.morphologyEx(gap, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    res = abs(tr.a)
    feats = []
    for geom, v in shapes(gap, mask=gap.astype(bool), transform=tr):
        if v != 1:
            continue
        p = shape(geom)
        if p.area < MIN_AREA:
            continue
        # elongated? a square blob is a yard, a ribbon is a road
        mrr = p.minimum_rotated_rectangle
        xs, ys = mrr.exterior.coords.xy
        e = [np.hypot(xs[i + 1] - xs[i], ys[i + 1] - ys[i]) for i in range(4)]
        L, Wd = max(e[0], e[1]), min(e[0], e[1])
        if L < MIN_LEN or Wd < 1.5 or L / max(Wd, 0.1) < 2.2:
            continue
        feats.append({"geometry": p, "area_m2": round(p.area, 1),
                      "length_m": round(L, 1), "width_m": round(p.area / max(L, 1), 1),
                      "suggest": suggest(p.area / max(L, 1))})
    miss = gpd.GeoDataFrame(feats, crs=M) if feats else gpd.GeoDataFrame(
        {"area_m2": [], "length_m": [], "width_m": [], "suggest": []},
        geometry=[], crs=M)
    print(f"  missing road candidates: {len(miss)}  "
          f"({miss['length_m'].sum()/1000 if len(miss) else 0:.1f} km of edge, "
          f"{miss['area_m2'].sum()/1e4 if len(miss) else 0:.1f} ha)", flush=True)

    if os.path.exists(OUT):
        os.remove(OUT)
    g.to_crs(4326).to_file(OUT, layer="road_lines", driver="GPKG")
    g[g["needs_review"]].to_crs(4326).to_file(OUT, layer="to_review", driver="GPKG")
    if len(miss):
        miss.to_crs(4326).to_file(OUT, layer="missing_roads", driver="GPKG")
    print(f"\nwrote {OUT}", flush=True)
    print("  road_lines     every road + fclass_suggest + needs_review", flush=True)
    print("  to_review      only the flagged ones - start here", flush=True)
    print("  missing_roads  surface with no line; draw the centreline inside", flush=True)
    print(f"({time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
