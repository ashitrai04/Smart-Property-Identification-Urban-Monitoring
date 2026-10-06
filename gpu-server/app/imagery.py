"""Imagery: ingest heavy drone / satellite rasters as compressed COGs, serve map tiles.

A 50 GB orthomosaic becomes a Cloud-Optimised GeoTIFF with JPEG (YCbCr) or WEBP
internal tiles plus overviews — typically 8-12x smaller — and the map reads only
the 256/512 px blocks it needs at the zoom it is at, so the browser never pulls
the whole file.
"""
import os
import threading
import time
import traceback
from pathlib import Path

import numpy as np
import rasterio
from rasterio.warp import transform_bounds
from rio_tiler.errors import TileOutsideBounds
from rio_tiler.io import Reader

import db
from config import COG_PROFILE, COG_QUALITY, COGS, RAW

RASTER_EXT = (".tif", ".tiff", ".jp2", ".img", ".vrt", ".png", ".jpg", ".jpeg")


def _gsd_m(src):
    """Ground sample distance in metres (approximate for geographic CRS)."""
    a = abs(src.transform.a)
    if src.crs and src.crs.is_geographic:
        lat = (src.bounds.top + src.bounds.bottom) / 2
        return a * 111320 * np.cos(np.radians(lat))
    return a


def describe(path):
    with rasterio.open(path) as src:
        if not src.crs:
            raise ValueError("Raster has no CRS — it needs georeferencing to go on the map")
        b = transform_bounds(src.crs, "EPSG:4326", *src.bounds, densify_pts=21)
        return {
            "bounds": [round(v, 7) for v in b], "crs": src.crs.to_string(),
            "gsd_m": round(float(_gsd_m(src)), 4), "width": src.width, "height": src.height,
            "bands": src.count, "dtype": src.dtypes[0],
        }


def _cog_translate(src_path, dst_path):
    from rio_cogeo.cogeo import cog_translate, cog_validate
    from rio_cogeo.profiles import cog_profiles

    with rasterio.open(src_path) as src:
        rgb8 = src.count >= 3 and src.dtypes[0] == "uint8"
        has_alpha = src.count == 4 or any(ci.name == "alpha" for ci in src.colorinterp)
    profile_name = COG_PROFILE if rgb8 else "deflate"     # JPEG/WEBP need 8-bit RGB
    profile = cog_profiles.get(profile_name)
    profile.update(BIGTIFF="IF_SAFER", blockxsize=512, blockysize=512)
    if profile_name in ("jpeg", "webp"):
        profile["QUALITY"] = COG_QUALITY
    cfg = {"GDAL_NUM_THREADS": "ALL_CPUS", "GDAL_TIFF_INTERNAL_MASK": True, "GDAL_CACHEMAX": 4096}
    cog_translate(
        src_path, dst_path, profile,
        indexes=(1, 2, 3) if rgb8 else None,             # drop alpha into an internal mask
        add_mask=rgb8 and (has_alpha or profile_name == "jpeg"),
        overview_level=None, overview_resampling="average",
        config=cfg, in_memory=False, quiet=True, use_cog_driver=False,
    )
    ok = cog_validate(dst_path)[0]
    if not ok:
        raise RuntimeError("COG validation failed")


def ingest(img_id, src_path):
    """Convert to COG (unless it already is one) and record the result. Runs in a thread."""
    try:
        meta = describe(src_path)
        db.update_imagery(img_id, **{k: meta[k] for k in ("bounds", "crs", "gsd_m", "width", "height", "bands")})
        dst = COGS / f"{img_id}.tif"
        t0 = time.time()
        _cog_translate(src_path, str(dst))
        db.update_imagery(img_id, cog_path=str(dst), cog_bytes=dst.stat().st_size, status="ready",
                          error=f"compressed in {time.time() - t0:.0f}s")
    except Exception as e:
        traceback.print_exc()
        db.update_imagery(img_id, status="failed", error=str(e)[:500])


def start_ingest(path, name, kind, district=None, captured=None, delete_source=False):
    p = Path(path).expanduser().resolve()
    if RAW.resolve() not in p.parents and p.parent != RAW.resolve():
        raise ValueError(f"Put imagery under {RAW} first (scp/rsync), then ingest it by path")
    if not p.exists() or p.suffix.lower() not in RASTER_EXT:
        raise ValueError(f"Not found or not a raster: {p}")
    img_id = db.add_imagery(name=name or p.stem, kind=kind, district=district, captured=captured,
                            source_path=str(p), status="ingesting", source_bytes=p.stat().st_size)

    def run():
        ingest(img_id, str(p))
        if delete_source and db.get_imagery(img_id)["status"] == "ready":
            p.unlink(missing_ok=True)

    threading.Thread(target=run, name=f"ingest-{img_id}", daemon=True).start()
    return img_id


# ── tiles ────────────────────────────────────────────────────────
_readers = {}
_readers_lock = threading.Lock()


def _reader(path):
    with _readers_lock:
        r = _readers.get(path)
        if r is None:
            r = Reader(path)
            _readers[path] = r
        return r


def tile(path, z, x, y, fmt="webp", size=256, colormap=None):
    try:
        img = _reader(path).tile(x, y, z, tilesize=size)
    except TileOutsideBounds:
        return None
    if colormap is not None:
        return img.render(img_format="PNG", colormap=colormap)
    if img.data.dtype != np.uint8:
        img.rescale(in_range=((float(np.percentile(img.data, 2)), float(np.percentile(img.data, 98))),))
    return img.render(img_format="WEBP" if fmt == "webp" else "PNG", **({"quality": 85} if fmt == "webp" else {}))
