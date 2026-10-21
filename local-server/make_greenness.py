"""Bake a small excess-green raster per clip, so encroachment.py can run on a
machine that does not have the 42 GB ortho.

exg = 2*G - R - B at 0.5 m is all the encroachment step uses the imagery for -
telling tree canopy over a road from a structure standing in it. One clip comes
out around 3 MB instead of carrying the whole ortho.

  python make_greenness.py clip_05 clip_08 clip_09
  python make_greenness.py all
"""
import os
import sys
import time

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import from_bounds

ORTHO = "D:/GEO_SERVE/Ongole_ORI_V3_jpeg92.tif"
CLIPS = "C:/Users/asash/Downloads/clip_index.gpkg"
OUT_T = "D:/GEO_SERVE/_kb/greenness_{clip}.tif"
RES = 0.50
M = 32644
BLOCK = 2048


def one(od, clip, poly):
    b = poly.bounds
    W = max(1, int((b[2] - b[0]) / RES))
    H = max(1, int((b[3] - b[1]) / RES))
    tr = rasterio.Affine(RES, 0, b[0], 0, -RES, b[3])
    outp = OUT_T.format(clip=clip)
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    prof = dict(driver="GTiff", width=W, height=H, count=1, dtype="int16",
                crs=rasterio.crs.CRS.from_epsg(M), transform=tr, nodata=-32768,
                compress="deflate", predictor=2, tiled=True,
                blockxsize=256, blockysize=256)
    with rasterio.open(outp, "w", **prof) as dst:
        for y0 in range(0, H, BLOCK):
            for x0 in range(0, W, BLOCK):
                hh = min(BLOCK, H - y0)
                ww = min(BLOCK, W - x0)
                gb = (b[0] + x0 * RES, b[3] - (y0 + hh) * RES,
                      b[0] + (x0 + ww) * RES, b[3] - y0 * RES)
                a = od.read([1, 2, 3], window=from_bounds(*gb, od.transform),
                            out_shape=(3, hh, ww), resampling=Resampling.average,
                            boundless=True, fill_value=0).astype(np.float32)
                exg = 2 * a[1] - a[0] - a[2]
                exg[a.sum(0) == 0] = -32768            # outside the ortho
                dst.write(np.clip(exg, -32768, 32767).astype(np.int16), 1,
                          window=rasterio.windows.Window(x0, y0, ww, hh))
    mb = os.path.getsize(outp) / 1e6
    return outp, W, H, mb


def main(clips):
    t0 = time.time()
    ci = gpd.read_file(CLIPS, layer="clip_index").to_crs(M)
    if clips == ["all"]:
        clips = sorted(ci["clip"].tolist())
    od = rasterio.open(ORTHO)
    for c in clips:
        sel = ci[ci["clip"] == c]
        if not len(sel):
            print(f"{c}: not in the clip index - skipped", flush=True)
            continue
        t = time.time()
        outp, W, H, mb = one(od, c, sel.geometry.iloc[0])
        print(f"{c}: {W}x{H} @{RES} m -> {mb:.1f} MB  ({time.time()-t:.0f}s)", flush=True)
    print(f"done ({(time.time()-t0)/60:.1f} min)", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:] or ["all"])
