"""Road signal for the whole Ongole drone ortho, in one raster.

Built from the 3 cm imagery (grey + smooth + ridge-shaped) and fused with the
SegFormer probability. Per-clip this took 16 minutes because it was computed
over every square metre of the clip, farmland included. Here it is tiled and a
tile is skipped unless an OSM line passes within CORRIDOR metres of it, which
is the only place the signal is ever read.

Writes a single uint8 raster at 30 cm: D:/GEO_SERVE/_kb/ongole_road_signal.tif
"""
import os
import time

import cv2
import geopandas as gpd
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import Window, from_bounds
from shapely.geometry import box
from shapely.strtree import STRtree
from skimage.filters import sato

DRONE = "D:/GEO_SERVE/Ongole_ORI_V3_jpeg92.tif"
SEG = "D:/GEO_SERVE/_kb/ongole_drone_road_prob.tif"
OSM = "C:/Users/asash/Downloads/Roads.gpkg"
DST = "D:/GEO_SERVE/_kb/ongole_road_signal.tif"
RES = 0.30
TILE = 1024          # output px per tile (307 m)
MARGIN = 96          # context so the ridge filter is not cut at tile edges
CORRIDOR = 30.0      # only compute where a road might be


def nz(x, lo, hi):
    return np.clip((x - lo) / max(hi - lo, 1e-9), 0, 1)


def signal(rgb, seg):
    f = rgb.astype(np.float32)
    R, G, B = f[..., 0], f[..., 1], f[..., 2]
    sat = f.max(2) - f.min(2)
    exg = 2 * G - R - B
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    tex = cv2.GaussianBlur(
        cv2.absdiff(gray, cv2.bilateralFilter(gray, 7, 40, 7)).astype(np.float32), (0, 0), 3)
    valid = rgb.max(2) > 8
    if valid.sum() < 500:
        return None
    surf = (1 - nz(sat, 10, 60)) * (1 - nz(exg, -5, 40)) * (1 - nz(tex, 1.5, 9))
    surf = cv2.GaussianBlur(surf, (0, 0), 2)
    surf = nz(surf, np.percentile(surf[valid], 55), np.percentile(surf[valid], 99))
    ridge = sato(surf, sigmas=[w / 2 / RES for w in (2.5, 5, 9)], black_ridges=False)
    hi = np.percentile(ridge[valid], 99.5)
    ridge = nz(ridge, np.percentile(ridge[valid], 70), hi if hi > 0 else 1)
    img = nz(surf * ridge, 0.02, 0.5)
    out = np.maximum(seg, img * 0.95)
    out[~valid] = 0
    return out


def main():
    t0 = time.time()
    src = rasterio.open(DRONE)
    sg = rasterio.open(SEG)
    b = src.bounds
    W = int((b.right - b.left) / RES)
    H = int((b.top - b.bottom) / RES)
    tr = rasterio.Affine(RES, 0, b.left, 0, -RES, b.top)
    print(f"city {W}x{H} @{RES} m", flush=True)

    roads = gpd.read_file(OSM).to_crs(src.crs)
    tree = STRtree(list(roads.geometry.values))

    prof = dict(driver="GTiff", width=W, height=H, count=1, dtype="uint8",
                crs=src.crs, transform=tr, compress="deflate", predictor=2,
                tiled=True, blockxsize=512, blockysize=512, BIGTIFF="YES")
    os.makedirs(os.path.dirname(DST), exist_ok=True)
    dst = rasterio.open(DST, "w", **prof)

    tiles = [(y, x) for y in range(0, H, TILE) for x in range(0, W, TILE)]
    done = skipped = 0
    for n, (ty, tx) in enumerate(tiles, 1):
        th, tw = min(TILE, H - ty), min(TILE, W - tx)
        wx0, wy1 = b.left + tx * RES, b.top - ty * RES
        wx1, wy0 = wx0 + tw * RES, wy1 - th * RES
        if not tree.query(box(wx0, wy0, wx1, wy1).buffer(CORRIDOR)).size:
            skipped += 1
            continue
        y0, x0 = max(0, ty - MARGIN), max(0, tx - MARGIN)
        y1, x1 = min(H, ty + th + MARGIN), min(W, tx + tw + MARGIN)
        gw = (b.left + x0 * RES, b.top - y1 * RES, b.left + x1 * RES, b.top - y0 * RES)
        a = src.read([1, 2, 3], window=from_bounds(*gw, src.transform),
                     out_shape=(3, y1 - y0, x1 - x0), resampling=Resampling.average,
                     boundless=True, fill_value=0)
        rgb = np.ascontiguousarray(np.transpose(a, (1, 2, 0)))
        sgv = sg.read(1, window=from_bounds(*gw, sg.transform),
                      out_shape=(y1 - y0, x1 - x0), resampling=Resampling.bilinear,
                      boundless=True, fill_value=0) / 255.0
        s = signal(rgb, sgv)
        if s is None:
            skipped += 1
            continue
        core = s[ty - y0:ty - y0 + th, tx - x0:tx - x0 + tw]
        dst.write((np.clip(core, 0, 1) * 255).astype(np.uint8), 1,
                  window=Window(tx, ty, tw, th))
        done += 1
        if n % 50 == 0:
            el = time.time() - t0
            print(f"  {n}/{len(tiles)}  built {done} skipped {skipped}  "
                  f"{el/60:.1f} min  eta {el/n*(len(tiles)-n)/60:.0f} min", flush=True)
    dst.close()
    print(f"wrote {DST}: {done} tiles built, {skipped} skipped "
          f"({(time.time()-t0)/60:.1f} min)", flush=True)


if __name__ == "__main__":
    main()
