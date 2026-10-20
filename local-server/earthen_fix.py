"""Add earthen (kutcha) roads to the road signal.

Measured gap: the signal scores "grey, smooth, ridge-shaped", so it sees paved
arterials at 81.9% and residential at 70.3%, but earthen tracks at only 28.7%.
Kutcha roads are reddish-brown, not grey, so the saturation term that finds
tarmac actively rejects them - about 43 km of Ongole's network.

This adds a second surface term (warm, smooth, ridge-shaped) and merges it into
the existing raster with a max. It only visits tiles near a road that is likely
unpaved, so it is minutes rather than the 6 hours a full rebuild costs.
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
SIGNAL = "D:/GEO_SERVE/_kb/ongole_road_signal.tif"
OSM = "D:/GEO_SERVE/ongole_osm_roads_aligned_v2.gpkg"
LAYER = "osm_roads_aligned"
# classes that are commonly unpaved here
UNPAVED = {"track", "path", "unclassified"}
TILE, MARGIN, CORRIDOR = 1024, 96, 18.0


def nz(x, lo, hi):
    return np.clip((x - lo) / max(hi - lo, 1e-9), 0, 1)


def earthen(rgb, res):
    """Warm, smooth, ribbon-shaped surface = a dirt road."""
    f = rgb.astype(np.float32)
    R, G, B = f[..., 0], f[..., 1], f[..., 2]
    valid = rgb.max(2) > 8
    if valid.sum() < 500:
        return None
    warm = nz(R - B, 8, 70)                      # brown/red earth
    notveg = 1 - nz(2 * G - R - B, 0, 45)        # but not vegetation
    bright = nz((R + G + B) / 3, 60, 110)        # not deep shadow
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    tex = cv2.GaussianBlur(
        cv2.absdiff(gray, cv2.bilateralFilter(gray, 7, 40, 7)).astype(np.float32), (0, 0), 3)
    smooth = 1 - nz(tex, 2.0, 11)
    surf = cv2.GaussianBlur(warm * notveg * smooth * bright, (0, 0), 2)
    hi = np.percentile(surf[valid], 99)
    surf = nz(surf, np.percentile(surf[valid], 60), hi if hi > 0 else 1)
    ridge = sato(surf, sigmas=[w / 2 / res for w in (2.5, 5, 9)], black_ridges=False)
    h2 = np.percentile(ridge[valid], 99.5)
    ridge = nz(ridge, np.percentile(ridge[valid], 75), h2 if h2 > 0 else 1)
    out = nz(surf * ridge, 0.02, 0.5)
    out[~valid] = 0
    return out


def main():
    t0 = time.time()
    g = gpd.read_file(OSM, layer=LAYER)
    ds = rasterio.open(SIGNAL, "r+")
    g = g.to_crs(ds.crs)
    sel = g[g["fclass"].fillna("").str.lower().isin(UNPAVED)]
    print(f"{len(sel)} candidate unpaved roads, {sel.length.sum()/1000:.1f} km", flush=True)
    tree = STRtree(list(sel.geometry.values))
    src = rasterio.open(DRONE)
    res = abs(ds.transform.a)
    W, H = ds.width, ds.height
    b = ds.bounds

    tiles = [(y, x) for y in range(0, H, TILE) for x in range(0, W, TILE)]
    done = skipped = 0
    raised = []
    for n, (ty, tx) in enumerate(tiles, 1):
        th, tw = min(TILE, H - ty), min(TILE, W - tx)
        wx0, wy1 = b.left + tx * res, b.top - ty * res
        wx1, wy0 = wx0 + tw * res, wy1 - th * res
        if not tree.query(box(wx0, wy0, wx1, wy1).buffer(CORRIDOR)).size:
            skipped += 1
            continue
        y0, x0 = max(0, ty - MARGIN), max(0, tx - MARGIN)
        y1, x1 = min(H, ty + th + MARGIN), min(W, tx + tw + MARGIN)
        gw = (b.left + x0 * res, b.top - y1 * res, b.left + x1 * res, b.top - y0 * res)
        a = src.read([1, 2, 3], window=from_bounds(*gw, src.transform),
                     out_shape=(3, y1 - y0, x1 - x0), resampling=Resampling.average,
                     boundless=True, fill_value=0)
        rgb = np.ascontiguousarray(np.transpose(a, (1, 2, 0)))
        e = earthen(rgb, res)
        if e is None:
            skipped += 1
            continue
        core = e[ty - y0:ty - y0 + th, tx - x0:tx - x0 + tw]
        win = Window(tx, ty, tw, th)
        cur = ds.read(1, window=win).astype(np.float32) / 255.0
        new = np.maximum(cur, core * 0.92)
        raised.append(float((new - cur).mean()))
        ds.write((np.clip(new, 0, 1) * 255).astype(np.uint8), 1, window=win)
        done += 1
        if n % 100 == 0:
            el = time.time() - t0
            print(f"  {n}/{len(tiles)}  updated {done} skipped {skipped}  "
                  f"{el/60:.1f} min  eta {el/n*(len(tiles)-n)/60:.0f} min", flush=True)
    ds.close()
    print(f"updated {done} tiles, skipped {skipped}, mean signal raised "
          f"{np.mean(raised) if raised else 0:.3f}  ({(time.time()-t0)/60:.1f} min)", flush=True)


if __name__ == "__main__":
    main()
