"""Road polygons traced from the imagery, not buffered from a line.

The buffer approach was wrong for this. A road's width changes along its
length, junctions flare, and a line buffered by one number matches none of
that - which is why the polygons looked too wide in one place and too narrow in
the next, and why fixing it by hand meant editing an attribute per road forever.

Here the polygon comes from the road signal itself, so the edges are wherever
the road actually ends. The OSM line is still used, but only for the two jobs it
is good at:

    deciding which blobs are roads   a patch of grey surface that no line
                                     touches is a yard, a car park or a roof
    carrying name and class          transferred from the line that runs
                                     through the blob

So nobody measures a width or types one in. Width becomes an output
(area / centreline length), not an input.

Run after city_signal.py. Per clip:  python road_surface.py clip_13
"""
import os
import sys
import time

import cv2
import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import rasterize, shapes
from scipy.ndimage import distance_transform_edt
from shapely.geometry import shape
from shapely.ops import unary_union

SIGNAL = "D:/GEO_SERVE/_kb/ongole_road_signal.tif"
LINES = "D:/GEO_SERVE/ongole_osm_roads_aligned_v2.gpkg"
LINES_LAYER = "osm_roads_aligned"
CLIPS = "C:/Users/asash/Downloads/clip_index.gpkg"
M = 32644

HI = 0.55                # far from any mapped road: strong evidence required
LO_NEAR = 0.30           # within NEAR_LANE of a line: a shaded lane counts
LO_MID = 0.38            # between the two
NEAR_LANE = 4.0          # m from the aligned centreline
FAR_LANE = 9.0
CAP_MULT = 2.6           # a road is never wider than this x its class width
CAP_MIN = 9.0            # ...but always allow at least this much
CLASS_W = {"motorway": 18, "trunk": 14, "primary": 9, "secondary": 7,
           "tertiary": 5, "residential": 3, "service": 2.5, "unclassified": 2,
           "track": 1.5, "footway": 1.5, "path": 1.2, "steps": 1.2,
           "pedestrian": 4, "cycleway": 1.5, "trunk_link": 7,
           "primary_link": 5, "secondary_link": 4, "tertiary_link": 3,
           "living_street": 3}
CLOSE_M = 1.2            # bridge shadows and parked vehicles across the road
OPEN_M = 0.6             # drop speckle
MIN_AREA = 60.0          # m2
CORRIDOR = 12.0          # a blob must touch a line within this to count as road
SIMPLIFY = 0.25          # m, keeps edges honest but drops pixel staircase


def main(clip=None):
    t0 = time.time()
    lines = gpd.read_file(LINES, layer=LINES_LAYER).to_crs(M)
    ds = rasterio.open(SIGNAL)
    if clip:
        ci = gpd.read_file(CLIPS, layer="clip_index").to_crs(M)
        poly = ci[ci["clip"] == clip].geometry.iloc[0]
        lines = lines[lines.geometry.intersects(poly)].reset_index(drop=True)
        b = poly.bounds
    else:
        b = tuple(lines.total_bounds)
    win = rasterio.windows.from_bounds(*b, ds.transform)
    arr = ds.read(1, window=win).astype(np.float32) / 255.0
    tr = ds.window_transform(win)
    res = abs(tr.a)
    print(f"{clip or 'city'}: {len(lines)} lines, {lines.length.sum()/1000:.1f} km | "
          f"signal {arr.shape[1]}x{arr.shape[0]} @{res:.2f} m", flush=True)

    # Thresholding conditioned on where a road is already known to be.
    # A flat 0.55 core dropped most residential lanes: they are 3 m wide and in
    # the shade of buildings and trees, so their signal never gets that high,
    # and the first attempt traced the arterials beautifully while losing the
    # network. Near an aligned OSM line the evidence needed is relaxed, far from
    # one it is raised, so shaded lanes come back without roofs and yards
    # leaking in.
    dist = distance_transform_edt(
        rasterize([(g, 1) for g in lines.geometry.values], out_shape=arr.shape,
                  transform=tr, fill=1, dtype="uint8") == 0) * res
    # A hard geometric cap, because thresholds alone cannot be trusted here:
    # too strict and the shaded lanes vanish, too loose and the whole block
    # floods. The road can never be wider than a generous multiple of its class
    # width, so the mask is clipped to that corridor and the threshold inside it
    # can then be relaxed safely.
    cap = []
    for ln, fc in zip(lines.geometry.values, lines["fclass"].fillna("")):
        w = CLASS_W.get(str(fc).lower(), 3.0)
        cap.append(ln.buffer(max(w * CAP_MULT, CAP_MIN) / 2.0))
    corridor = rasterize([(c, 1) for c in cap], out_shape=arr.shape,
                         transform=tr, fill=0, dtype="uint8")
    thr = np.where(dist <= NEAR_LANE, LO_NEAR,
                   np.where(dist <= FAR_LANE, LO_MID, HI)).astype(np.float32)
    mask = ((arr >= thr) & (corridor > 0)).astype(np.uint8)

    k = lambda m: np.ones((max(1, int(round(m / res)) * 2 + 1),) * 2, np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k(CLOSE_M))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k(OPEN_M))
    print(f"  surface covers {100*mask.mean():.1f}% of the area  "
          f"({time.time()-t0:.0f}s)", flush=True)

    n, lab = cv2.connectedComponents(mask, 8)
    onroad = rasterize([(g.buffer(CORRIDOR), 1) for g in lines.geometry.values],
                       out_shape=mask.shape, transform=tr, fill=0, dtype="uint8")
    touched = np.unique(lab[(onroad > 0) & (mask > 0)])
    keep = np.zeros(n, bool)
    keep[touched] = True
    keep[0] = False
    mask = keep[lab].astype(np.uint8)
    print(f"  after keeping only blobs on a road line: {100*mask.mean():.1f}%", flush=True)

    polys = []
    for geom, v in shapes(mask, mask=mask.astype(bool), transform=tr):
        if v != 1:
            continue
        p = shape(geom).simplify(SIMPLIFY)
        if p.is_valid and p.area >= MIN_AREA:
            polys.append(p)
    print(f"  {len(polys)} surface polygons", flush=True)

    # attributes from the line that runs through each polygon
    sidx = lines.sindex
    rows = []
    for p in polys:
        hit = list(sidx.intersection(p.bounds))
        best, blen = None, 0.0
        for i in hit:
            ln = lines.geometry.iloc[i]
            try:
                inside = ln.intersection(p).length
            except Exception:
                inside = 0.0
            if inside > blen:
                best, blen = i, inside
        r = {"geometry": p, "area_m2": round(p.area, 1),
             "centreline_m": round(blen, 1),
             "width_m": round(p.area / blen, 2) if blen > 2 else None}
        if best is not None:
            src = lines.iloc[best]
            for f in ("osm_id", "fclass", "name", "ref", "oneway"):
                if f in lines.columns:
                    r[f] = src[f]
        rows.append(r)
    out = gpd.GeoDataFrame(rows, crs=M)

    name = f"D:/GEO_SERVE/ongole_road_surface_{clip or 'city'}.gpkg"
    if os.path.exists(name):
        os.remove(name)
    out.to_crs(4326).to_file(name, layer="road_surface", driver="GPKG")
    merged = unary_union(out.geometry.values)
    gpd.GeoDataFrame({"clip": [clip or "city"], "polygons": [len(out)],
                      "surface_ha": [round(merged.area / 1e4, 2)]},
                     geometry=[merged], crs=M).to_crs(4326).to_file(
        name, layer="merged", driver="GPKG")

    w = out["width_m"].dropna()
    print(f"\n  surface {merged.area/1e4:.2f} ha", flush=True)
    if len(w):
        print(f"  width from area/length: median {w.median():.1f} m  "
              f"p10 {w.quantile(.1):.1f}  p90 {w.quantile(.9):.1f}  "
              f"(an OUTPUT, nothing was typed in)", flush=True)
    if "fclass" in out.columns:
        for fc, grp in out.groupby(out["fclass"].fillna("(none)")):
            ww = grp["width_m"].dropna()
            if len(ww) >= 3:
                print(f"    {str(fc)[:12]:<12} n={len(grp):>4}  width median {ww.median():>5.1f} m",
                      flush=True)
    print(f"wrote {name}  ({time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
