"""Turn aligned OSM road lines into road polygons for the drone imagery.

The satellite version buffered each line by a width taken from fclass. That is
the right fallback, but it is a guess: every residential street becomes 3 m
whether it is 2 m or 6 m. The drone ortho is 3 cm, and there is already a road
probability raster over it, so the actual width can be MEASURED.

Per line:
  - walk along it every 10 m and cast a perpendicular transect
  - on each transect, measure the unbroken run of road probability through the
    centre; that run is the carriageway at that point
  - take the median over the transects, so a parked lorry or a tree shadow on
    one transect does not set the width
  - fall back to the fclass table where too few transects are usable (under
    canopy, or where the model did not see road)

Measured widths are clamped to between half and 2.5x the fclass value, because
a transect that runs into an open plot or a junction would otherwise report a
20 m "street".
"""
import json
import os
import sys
import time

import geopandas as gpd
import numpy as np
import rasterio
from scipy.ndimage import map_coordinates
from shapely.geometry import MultiLineString
from shapely.ops import unary_union

ALIGNED = "D:/GEO_SERVE/ongole_osm_roads_aligned_v2.gpkg"
LAYER = "osm_roads_aligned"
PROB = "D:/GEO_SERVE/_kb/ongole_drone_road_prob.tif"
CLIPS = "C:/Users/asash/Downloads/clip_index.gpkg"
M = 32644

# the width table used for the satellite polygons, kept as the fallback
FCLASS_W = {
    # your satellite table
    "motorway": 18, "trunk": 14, "primary": 9, "secondary": 7,
    "tertiary": 5, "residential": 3, "service": 2.5,
    "unclassified": 2, "track": 1.5,
    # link roads inherit a narrower version of their parent, otherwise a
    # primary_link falls to the 3 m default and gets clamped at 7.5 m
    "motorway_link": 9, "trunk_link": 7, "primary_link": 5,
    "secondary_link": 4, "tertiary_link": 3,
    "living_street": 3, "residential_link": 3,
    # not carriageways: these must not absorb the street beside them
    "pedestrian": 4, "footway": 1.5, "path": 1.2, "steps": 1.2,
    "cycleway": 1.5, "bridleway": 1.2,
}
# a footway transect runs straight into the adjacent road, so measuring is
# allowed to widen it far less than a street
FOOT = {"pedestrian", "footway", "path", "steps", "cycleway", "bridleway"}
DEFAULT_W = 3.0

STEP_ALONG = 10.0     # transect every 10 m
HALF = 14.0           # look this far either side
DS = 0.3              # transect sampling step (= drone prob raster pixel)
THR = 0.35            # road probability that counts as carriageway
MIN_VALID = 0.4       # need this share of transects to trust the measurement
LO_MULT, HI_MULT = 0.5, 2.5
ABS_MIN, ABS_MAX = 1.5, 30.0


class Prob:
    def __init__(self, path):
        self.ds = rasterio.open(path)
        self.a = self.ds.read(1)
        self.tr = ~self.ds.transform

    def at(self, xy):
        c, r = self.tr * (xy[:, 0], xy[:, 1])
        return map_coordinates(self.a, [r, c], order=1, mode="constant", cval=0) / 255.0


def width_of(line, P, fallback):
    """Median carriageway width along the line, or None if not measurable."""
    n = max(2, int(line.length / STEP_ALONG) + 1)
    offs = np.arange(-HALF, HALF + 1e-9, DS)
    mid = len(offs) // 2
    widths = []
    for i in range(n):
        t = i / (n - 1)
        p = line.interpolate(t, normalized=True)
        # local direction, so the transect is square to the road
        d = 1.0 / max(line.length, 1e-6)
        a = line.interpolate(max(0.0, t - d * 5), normalized=True)
        b = line.interpolate(min(1.0, t + d * 5), normalized=True)
        vx, vy = b.x - a.x, b.y - a.y
        L = np.hypot(vx, vy)
        if L < 1e-9:
            continue
        nx, ny = -vy / L, vx / L
        pts = np.c_[p.x + nx * offs, p.y + ny * offs]
        v = P.at(pts) > THR
        if not v[mid]:
            continue                      # centre is not on road: skip, do not widen
        l = mid
        while l > 0 and v[l - 1]:
            l -= 1
        r = mid
        while r < len(v) - 1 and v[r + 1]:
            r += 1
        w = (r - l) * DS
        if w > 0:
            widths.append(w)
    if len(widths) < max(2, MIN_VALID * n):
        return None, len(widths), n
    return float(np.median(widths)), len(widths), n


def main(clip="clip_13"):
    t0 = time.time()
    g = gpd.read_file(ALIGNED, layer=LAYER).to_crs(M)
    ci = gpd.read_file(CLIPS, layer="clip_index").to_crs(M)
    poly = ci[ci["clip"] == clip].geometry.iloc[0]
    g = g[g.geometry.intersects(poly)].reset_index(drop=True)
    print(f"{clip}: {len(g)} roads, {g.length.sum()/1000:.1f} km", flush=True)
    P = Prob(PROB)

    wc, wm, wu, src, nv = [], [], [], [], []
    for geom, fc in zip(g.geometry.values, g["fclass"].fillna("")):
        key = str(fc).lower()
        base = FCLASS_W.get(key, DEFAULT_W)
        hi_mult = 1.3 if key in FOOT else HI_MULT
        lines = [geom] if geom.geom_type == "LineString" else list(geom.geoms)
        meas, ok, tot = [], 0, 0
        for ls in lines:
            if ls.length < 2:
                continue
            w, k, n = width_of(ls, P, base)
            ok += k; tot += n
            if w is not None:
                meas.append(w)
        wc.append(base)
        if meas:
            m = float(np.median(meas))
            clamped = float(np.clip(m, max(ABS_MIN, base * LO_MULT),
                                    min(ABS_MAX, base * hi_mult)))
            wm.append(round(m, 2)); wu.append(round(clamped, 2))
            src.append("measured" if abs(clamped - m) < 0.01 else "measured-clamped")
        else:
            wm.append(None); wu.append(base); src.append("fclass")
        nv.append(f"{ok}/{tot}")

    g["width_fclass"] = wc
    g["width_measured"] = wm
    g["width_m"] = wu
    g["width_src"] = src
    g["transects"] = nv
    g["area_m2"] = (g["width_m"] * g.length).round(1)

    # polygons: flat caps so a road does not bulge past its end at junctions
    g_poly = g.copy()
    g_poly["geometry"] = [ln.buffer(w / 2.0, cap_style=2, join_style=2)
                          for ln, w in zip(g.geometry.values, g["width_m"])]
    out = f"D:/GEO_SERVE/ongole_road_polygons_{clip}.gpkg"
    if os.path.exists(out):
        os.remove(out)
    g_poly.to_crs(4326).to_file(out, layer="road_polygons", driver="GPKG")
    # one merged surface, for area totals
    merged = gpd.GeoDataFrame(
        {"clip": [clip], "roads": [len(g)],
         "length_km": [round(float(g.length.sum() / 1000), 2)]},
        geometry=[unary_union(g_poly.geometry.values)], crs=M)
    merged["surface_ha"] = (merged.geometry.area / 1e4).round(2)
    merged.to_crs(4326).to_file(out, layer="road_surface", driver="GPKG")

    n_meas = sum(1 for s in src if s.startswith("measured"))
    print(f"\nwidth source: measured {n_meas}, fclass fallback {len(src) - n_meas}", flush=True)
    print(f"{'fclass':<14}{'n':>5}{'table':>8}{'measured median':>17}", flush=True)
    for fc in sorted(set(g["fclass"].fillna(""))):
        sel = g["fclass"].fillna("") == fc
        mm = g.loc[sel, "width_measured"].dropna()
        print(f"  {str(fc)[:12]:<12}{int(sel.sum()):>5}"
              f"{FCLASS_W.get(str(fc).lower(), DEFAULT_W):>8.1f}"
              f"{(f'{mm.median():.1f} m' if len(mm) else '-'):>17}", flush=True)
    print(f"\nsurface {float(merged['surface_ha'].iloc[0]):.2f} ha over "
          f"{g.length.sum()/1000:.1f} km", flush=True)
    print(f"wrote {out}  ({time.time() - t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "clip_13")
