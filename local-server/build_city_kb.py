"""Knowledge base for everything that is not road damage.

The ask bot only knew potholes. The served folder also holds Ongole's buildings,
water bodies and the whole road network, which answer far more of what anyone
would actually ask a city survey.

One honesty rule is baked in here. The 2017 buildings are HAND LABELS over a
6 x 3 km patch; the 2026 buildings are MODEL OUTPUT over the whole 15 x 14 km
city. Reporting "4,772 grew to 11,112" would be nonsense twice over — different
areas and different methods. Any comparison is clipped to the 2017 extent and
carries the caveat with it.

Output
  D:\\GEO_SERVE\\_kb\\city.json        stats + top features, for the ask API
  D:\\GEO_SERVE\\city_kb.gpkg          the top features, for pinning on the map
"""
import json
import os
import time

import geopandas as gpd
import numpy as np
from shapely.geometry import box

OUT_DIR = r"D:\GEO_SERVE\_kb"
OUT_GPKG = r"D:\GEO_SERVE\city_kb.gpkg"
SEG = r"D:\GEO_SERVE\ongole_2026_segformer.gpkg"
B17 = r"D:\GEO_SERVE\ongole_2017_buildings.gpkg"
M = 32644
TOP = 25


def stats_area(g, name, unit_ha=True):
    a = g.geometry.area
    q = a.quantile([0.25, 0.5, 0.75, 0.9]).round(1)
    return {
        "layer": name, "count": int(len(g)),
        "total_m2": round(float(a.sum()), 1),
        "total_ha": round(float(a.sum()) / 1e4, 2),
        "median_m2": round(float(a.median()), 1),
        "mean_m2": round(float(a.mean()), 1),
        "max_m2": round(float(a.max()), 1),
        "min_m2": round(float(a.min()), 1),
        "p25_m2": float(q.loc[0.25]), "p75_m2": float(q.loc[0.75]), "p90_m2": float(q.loc[0.9]),
    }


def top_features(g, name, by, k=TOP):
    g = g.copy()
    g["_v"] = by
    g = g.sort_values("_v", ascending=False).head(k)
    g4 = g.to_crs(4326)
    rows = []
    for i, ((_, r), (_, r4)) in enumerate(zip(g.iterrows(), g4.iterrows()), 1):
        c = r4.geometry.centroid
        b = r4.geometry.bounds
        rows.append({"rank": i, "layer": name, "value": round(float(r["_v"]), 1),
                     "lon": round(float(c.x), 6), "lat": round(float(c.y), 6),
                     "bbox": [round(v, 6) for v in b]})
    return rows, g4


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    out = {"city": "Ongole", "built": time.strftime("%Y-%m-%d %H:%M"), "layers": {}, "top": {}}
    keep = []

    # ── buildings 2026 ───────────────────────────────────────────
    b = gpd.read_file(SEG, layer="buildings").to_crs(M)
    s = stats_area(b, "buildings")
    s["year"] = 2026
    s["source"] = "SegFormer over the 2026 satellite, whole city"
    big = (b.geometry.area > 1000).sum()
    s["over_1000_m2"] = int(big)
    out["layers"]["buildings"] = s
    rows, g4 = top_features(b, "buildings", b.geometry.area)
    out["top"]["buildings"] = rows
    g4 = g4.assign(layer="buildings", value=g4.geometry.to_crs(M).area.round(1))
    keep.append(g4[["layer", "value", "geometry"]])

    # ── water ────────────────────────────────────────────────────
    w = gpd.read_file(SEG, layer="water").to_crs(M)
    s = stats_area(w, "water")
    s["year"] = 2026
    s["source"] = "SegFormer over the 2026 satellite"
    out["layers"]["water"] = s
    rows, g4 = top_features(w, "water", w.geometry.area)
    out["top"]["water"] = rows
    g4 = g4.assign(layer="water", value=g4.geometry.to_crs(M).area.round(1))
    keep.append(g4[["layer", "value", "geometry"]])

    # ── road network ─────────────────────────────────────────────
    rc = gpd.read_file(SEG, layer="road_centerlines").to_crs(M)
    L = rc.geometry.length
    wid = rc["width_m"].astype(float)
    rp = gpd.read_file(SEG, layer="road_polygons").to_crs(M)
    out["layers"]["roads"] = {
        "layer": "roads", "count": int(len(rc)), "year": 2026,
        "source": "SegFormer over the 2026 satellite",
        "total_km": round(float(L.sum()) / 1000, 2),
        "median_m": round(float(L.median()), 1),
        "max_m": round(float(L.max()), 1),
        "surface_ha": round(float(rp.geometry.area.sum()) / 1e4, 2),
        "width_median_m": round(float(wid.median()), 2),
        "width_max_m": round(float(wid.max()), 2),
        "width_p90_m": round(float(wid.quantile(0.9)), 2),
    }
    rows, g4 = top_features(rc, "roads", L)
    out["top"]["roads"] = rows
    g4 = g4.assign(layer="roads", value=rc.geometry.length.sort_values(ascending=False).head(TOP).round(1).values)
    keep.append(g4[["layer", "value", "geometry"]])
    rows_w, _ = top_features(rc, "roads_wide", wid)
    out["top"]["roads_wide"] = rows_w

    # ── 2017 buildings, and the only comparison that is defensible ──
    b17 = gpd.read_file(B17, layer="buildings").to_crs(M)
    s17 = stats_area(b17, "buildings_2017")
    s17["year"] = 2017
    s17["source"] = "hand labels, part of the city only"
    out["layers"]["buildings_2017"] = s17

    ext = box(*b17.total_bounds)
    in26 = b[b.geometry.centroid.within(ext)]
    b4 = b17.to_crs(4326).total_bounds
    out["compare"] = {
        "extent_bbox": [round(v, 6) for v in b4],
        "extent_km2": round(ext.area / 1e6, 2),
        "y2017": {"count": int(len(b17)), "ha": round(float(b17.geometry.area.sum()) / 1e4, 2),
                  "median_m2": round(float(b17.geometry.area.median()), 1)},
        "y2026": {"count": int(len(in26)), "ha": round(float(in26.geometry.area.sum()) / 1e4, 2),
                  "median_m2": round(float(in26.geometry.area.median()), 1)},
        "caveat": ("the 2017 layer is hand-labelled and the 2026 layer is model output, "
                   "so the difference mixes real change with a change of method and is "
                   "not a growth figure"),
    }

    gpd.GeoDataFrame(np.nan, index=[], columns=["layer", "value", "geometry"]) if False else None
    all_top = gpd.GeoDataFrame(
        __import__("pandas").concat(keep, ignore_index=True), crs=4326)
    all_top.to_file(OUT_GPKG, layer="top_features", driver="GPKG")

    with open(os.path.join(OUT_DIR, "city.json"), "w", encoding="utf-8") as f:
        json.dump(out, f)

    print(f"wrote {OUT_DIR}\\city.json and {OUT_GPKG}")
    for k, v in out["layers"].items():
        if "total_km" in v:
            print(f"  {k:<16} {v['count']:>6} features  {v['total_km']:>8.1f} km  "
                  f"surface {v['surface_ha']:.0f} ha  median width {v['width_median_m']:.1f} m")
        else:
            print(f"  {k:<16} {v['count']:>6} features  {v['total_ha']:>8.1f} ha  "
                  f"median {v['median_m2']:.0f} m2  max {v['max_m2']:,.0f} m2")
    c = out["compare"]
    print(f"\n  comparable extent {c['extent_km2']} km2: "
          f"2017 {c['y2017']['count']} buildings / {c['y2017']['ha']} ha  vs  "
          f"2026 {c['y2026']['count']} / {c['y2026']['ha']} ha")


if __name__ == "__main__":
    main()
