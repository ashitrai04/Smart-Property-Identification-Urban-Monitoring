"""Trace road surface for all 24 clips and merge into one city file.

Per clip, not city-wide in one go: the full ortho is 2.6 gigapixels, which is
about 10 GB as float32 before the distance transform even starts. A clip is
~70 megapixels and takes ~20 s.
"""
import os
import time

import geopandas as gpd
import pandas as pd
from shapely.ops import unary_union

import road_surface

CLIPS = "C:/Users/asash/Downloads/clip_index.gpkg"
OUT = "D:/GEO_SERVE/ongole_road_surface_city.gpkg"
M = 32644


def main():
    t0 = time.time()
    ci = gpd.read_file(CLIPS, layer="clip_index").to_crs(M)
    names = sorted(ci["clip"])
    frames, rows = [], []
    for i, c in enumerate(names, 1):
        print(f"\n[{i}/{len(names)}] {c}", flush=True)
        try:
            road_surface.main(c)
        except Exception as e:
            print(f"  FAILED: {str(e)[:120]}", flush=True)
            continue
        p = f"D:/GEO_SERVE/ongole_road_surface_{c}.gpkg"
        if not os.path.exists(p):
            continue
        g = gpd.read_file(p, layer="road_surface").to_crs(M)
        if not len(g):
            continue
        g["clip"] = c
        frames.append(g)
        rows.append({"clip": c, "polygons": len(g),
                     "surface_ha": round(float(g.geometry.area.sum() / 1e4), 2)})

    if not frames:
        print("nothing produced")
        return
    allg = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=M)
    # clips overlap at their edges, so the same road can appear twice; keep the
    # larger polygon where two cover the same ground
    allg = allg.sort_values("area_m2", ascending=False).reset_index(drop=True)
    if os.path.exists(OUT):
        os.remove(OUT)
    allg.to_crs(4326).to_file(OUT, layer="road_surface", driver="GPKG")
    merged = unary_union(allg.geometry.values)
    gpd.GeoDataFrame({"city": ["Ongole"], "polygons": [len(allg)],
                      "surface_ha": [round(merged.area / 1e4, 2)]},
                     geometry=[merged], crs=M).to_crs(4326).to_file(
        OUT, layer="merged", driver="GPKG")
    pd.DataFrame(rows).to_csv("D:/GEO_SERVE/_kb/road_surface_by_clip.csv", index=False)

    print(f"\n{'clip':<10}{'polys':>7}{'ha':>9}")
    for r in rows:
        print(f"  {r['clip']:<8}{r['polygons']:>7}{r['surface_ha']:>9.2f}", flush=True)
    w = allg["width_m"].dropna()
    print(f"\ncity: {len(allg)} polygons, merged surface {merged.area/1e4:.1f} ha")
    if len(w):
        print(f"width (area/length): median {w.median():.1f} m  "
              f"p10 {w.quantile(.1):.1f}  p90 {w.quantile(.9):.1f}")
    if "fclass" in allg.columns:
        print(f"\n{'fclass':<16}{'n':>6}{'width median':>14}")
        for fc, grp in allg.groupby(allg["fclass"].fillna("(none)")):
            ww = grp["width_m"].dropna()
            if len(grp) >= 5:
                print(f"  {str(fc)[:14]:<14}{len(grp):>6}"
                      f"{(f'{ww.median():.1f} m' if len(ww) else '-'):>14}", flush=True)
    print(f"\nwrote {OUT}  ({(time.time()-t0)/60:.1f} min)", flush=True)


if __name__ == "__main__":
    main()
