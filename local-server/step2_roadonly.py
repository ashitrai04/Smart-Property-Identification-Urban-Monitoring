"""STEP 2 - keep only what is on the road.

Clips the step-1 detections to the road polygons, drops anything that falls on a
building, and writes a road-only raster so the road surface can be looked at on
its own in QGIS.
"""
import os
import time

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import rasterize
from rasterio.windows import Window
from shapely.ops import unary_union

# ── settings ─────────────────────────────────────────────────────
DET = "D:/GEO_SERVE/ongole_road_damage_step1.gpkg"
DET_LAYER = "damage_all"
ROADS = "D:/GEO_SERVE/ongole_roads_district.gpkg"
ROADS_LAYER = "road_polygons"
BUILDINGS = "D:/GEO_SERVE/ongole_2026_segformer.gpkg"
BUILDINGS_LAYER = "buildings"
ORTHO = "D:/GEO_SERVE/Ongole_ORI_V3_jpeg92.tif"
OUT = "D:/GEO_SERVE/ongole_road_damage.gpkg"
RASTER = "D:/GEO_SERVE/ongole_road_only.tif"
CSV = "D:/GEO_SERVE/_kb/step2_summary.csv"

ON_ROAD_MIN = 0.50        # at least half the detection must sit on road
BUILDING_MAX = 0.30       # drop it if this much overlaps a building
RASTER_RES = 0.30         # road-only raster resolution, m
MAKE_RASTER = True


def main():
    t0 = time.time()
    det = gpd.read_file(DET, layer=DET_LAYER)
    rd = gpd.read_file(ROADS, layer=ROADS_LAYER).to_crs(32644)
    det = det.to_crs(32644)
    print(f"step-1 detections {len(det):,} | road polygons {len(rd)}", flush=True)

    road = unary_union(rd.geometry.values)
    try:
        bld = gpd.read_file(BUILDINGS, layer=BUILDINGS_LAYER).to_crs(32644)
        bld_u = unary_union(bld.geometry.values)
        print(f"buildings {len(bld):,}", flush=True)
    except Exception as e:
        print(f"buildings unavailable ({str(e)[:60]}) - skipping that filter", flush=True)
        bld_u = None

    a = det.geometry.area.values
    on = np.array([g.intersection(road).area for g in det.geometry.values]) / np.maximum(a, 1e-9)
    det["on_road"] = np.round(on, 3)
    if bld_u is not None:
        ov = np.array([g.intersection(bld_u).area for g in det.geometry.values]) / np.maximum(a, 1e-9)
    else:
        ov = np.zeros(len(det))
    det["on_building"] = np.round(ov, 3)
    keep = (det["on_road"] >= ON_ROAD_MIN) & (det["on_building"] <= BUILDING_MAX)
    kept = det[keep].copy()
    drop = det[~keep].copy()
    print(f"kept {len(kept):,}  dropped {len(drop):,} "
          f"({int((det['on_road'] < ON_ROAD_MIN).sum()):,} off road, "
          f"{int((det['on_building'] > BUILDING_MAX).sum()):,} on buildings)", flush=True)

    if os.path.exists(OUT):
        os.remove(OUT)
    kept.to_crs(4326).to_file(OUT, layer="road_damage", driver="GPKG")
    for lab, sub in kept.groupby("label"):
        sub.to_crs(4326).to_file(OUT, layer=f"damage_{lab}", driver="GPKG")
    if len(drop):
        drop.to_crs(4326).to_file(OUT, layer="rejected", driver="GPKG")

    s = (kept.groupby("label")
         .agg(n=("label", "size"), low_conf=("low_confidence", "sum"),
              conf_median=("confidence", "median"), size_median_m=("size_m", "median"),
              area_total_m2=("area_m2", "sum")).reset_index())
    os.makedirs(os.path.dirname(CSV), exist_ok=True)
    s.to_csv(CSV, index=False)
    print(f"\n{'class':<22}{'kept':>7}{'low conf':>10}{'conf':>7}{'size':>8}", flush=True)
    for _, r in s.iterrows():
        print(f"  {r['label']:<20}{int(r['n']):>7}{int(r['low_conf']):>10}"
              f"{r['conf_median']:>7.2f}{r['size_median_m']:>7.1f}m", flush=True)

    if MAKE_RASTER:
        src = rasterio.open(ORTHO)
        b = rd.total_bounds
        W = int((b[2] - b[0]) / RASTER_RES)
        H = int((b[3] - b[1]) / RASTER_RES)
        tr = rasterio.Affine(RASTER_RES, 0, b[0], 0, -RASTER_RES, b[3])
        prof = dict(driver="GTiff", width=W, height=H, count=4, dtype="uint8",
                    crs=src.crs, transform=tr, compress="deflate", predictor=2,
                    tiled=True, blockxsize=512, blockysize=512, BIGTIFF="YES")
        print(f"\nroad-only raster {W}x{H} @{RASTER_RES} m", flush=True)
        with rasterio.open(RASTER, "w", **prof) as dst:
            TS = 2048
            for y in range(0, H, TS):
                for x in range(0, W, TS):
                    th, tw = min(TS, H - y), min(TS, W - x)
                    gw = (b[0] + x * RASTER_RES, b[3] - (y + th) * RASTER_RES,
                          b[0] + (x + tw) * RASTER_RES, b[3] - y * RASTER_RES)
                    win = rasterio.windows.from_bounds(*gw, src.transform)
                    img = src.read([1, 2, 3], window=win, out_shape=(3, th, tw),
                                   resampling=rasterio.enums.Resampling.average,
                                   boundless=True, fill_value=0)
                    ttr = rasterio.Affine(RASTER_RES, 0, gw[0], 0, -RASTER_RES, gw[3])
                    m = rasterize([(g, 1) for g in rd.geometry.values], out_shape=(th, tw),
                                  transform=ttr, fill=0, dtype="uint8")
                    out = np.zeros((4, th, tw), np.uint8)
                    out[:3] = img * m
                    out[3] = m * 255
                    dst.write(out, window=Window(x, y, tw, th))
                if (y // TS) % 5 == 0:
                    print(f"  row {y}/{H}", flush=True)
        print(f"wrote {RASTER}", flush=True)

    print(f"\nwrote {OUT}\n      {CSV}  ({(time.time()-t0)/60:.1f} min)", flush=True)


if __name__ == "__main__":
    main()
