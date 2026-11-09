"""Cut the 1 km2 test area out of the orthomosaic, so PARAM needs no 45 GB file.

The only reason the full mosaic would have to go to the cluster is the final
inference pass, and that pass covers 1 km2. Cutting it here turns a 45 GB
transfer into a few hundred megabytes. JPEG compression inside the GeoTIFF is
visually lossless at quality 92 and is what the source mosaic already uses, so
nothing is lost that the model would see.

    python make_infer_clip.py --out C:/param_upload/ongole_1km.tif

Pick the area deliberately with --centre X Y in the image CRS (EPSG:32644
here); the default is the middle of the mosaic, which is dense urban.
"""
import argparse
import warnings

warnings.filterwarnings("ignore")

import rasterio
from rasterio.enums import Resampling
from rasterio.windows import Window


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", default=r"D:\GEO_SERVE\Ongole_ORI_V3_jpeg92.tif")
    ap.add_argument("--out", required=True)
    ap.add_argument("--area-km2", type=float, default=1.0)
    ap.add_argument("--centre", nargs=2, type=float, default=None, metavar=("X", "Y"))
    ap.add_argument("--gsd", type=float, default=0.0,
                    help="0 keeps the source resolution; set e.g. 0.10 to shrink further")
    ap.add_argument("--quality", type=int, default=92)
    a = ap.parse_args()

    src = rasterio.open(a.image)
    native = src.res[0]
    side = (a.area_km2 ** 0.5) * 1000.0
    span = int(round(side / native))

    if a.centre:
        col, row = ~src.transform * tuple(a.centre)
        c0, r0 = int(col - span / 2), int(row - span / 2)
    else:
        c0, r0 = src.width // 2 - span // 2, src.height // 2 - span // 2
    c0 = max(0, min(c0, src.width - span))
    r0 = max(0, min(r0, src.height - span))
    win = Window(c0, r0, span, span)

    out_gsd = a.gsd if a.gsd > 0 else native
    W = H = int(round(side / out_gsd))
    print(f"{a.area_km2} km2 = {side:.0f} m a side")
    print(f"  source window {span}x{span} px @ {native:.3f} m at ({c0}, {r0})")
    print(f"  writing       {W}x{H} px @ {out_gsd:.3f} m")

    arr = src.read([1, 2, 3], window=win, out_shape=(3, H, W),
                   resampling=Resampling.average)
    t = src.window_transform(win) * rasterio.Affine.scale(span / W, span / H)
    prof = {"driver": "GTiff", "width": W, "height": H, "count": 3, "dtype": "uint8",
            "crs": src.crs, "transform": t, "compress": "JPEG",
            "photometric": "YCBCR", "jpeg_quality": a.quality,
            "tiled": True, "blockxsize": 512, "blockysize": 512}
    with rasterio.open(a.out, "w", **prof) as d:
        d.write(arr)

    import os
    print(f"  -> {a.out}  {os.path.getsize(a.out)/1e6:.0f} MB")
    b = rasterio.open(a.out).bounds
    print(f"  bounds {[round(v, 1) for v in b]} in {src.crs}")


if __name__ == "__main__":
    main()
