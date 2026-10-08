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
from rasterio.enums import MaskFlags
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


def _to_uint8(src_path):
    """16-bit / float RGB -> 8-bit RGB with an internal validity mask, as one consistent
    conversion for the whole image (per-tile stretching makes a patchwork, and the
    models expect plain 8-bit colour). Exports that store 0-255 in uint16 (nodata 256)
    are copied losslessly; real 16-bit data gets one global 2-98 % stretch.
    Returns the path of the 8-bit copy, or None when the source is already 8-bit."""
    from rasterio.windows import Window

    with rasterio.open(src_path) as src:
        if src.dtypes[0] == "uint8" or src.count < 3:
            return None
        nd = src.nodata
        rng = np.random.default_rng(0)
        samples = []
        for _ in range(48):          # random 512 px windows: cheap, whole-image statistics
            x = int(rng.integers(0, max(1, src.width - 512)))
            y = int(rng.integers(0, max(1, src.height - 512)))
            a = src.read([1, 2, 3], window=Window(x, y, 512, 512)).astype(np.float32)
            ok = src.dataset_mask(window=Window(x, y, 512, 512)) > 0
            if nd is not None:
                ok &= ~(a == nd).any(0)
            if ok.any():
                samples.append(a[:, ok])
        v = np.concatenate(samples, axis=1) if samples else np.zeros((3, 1), np.float32)
        if v.max() <= 255 and v.min() >= 0:
            lo, hi = 0.0, 255.0
            note = "8-bit values stored as 16-bit: copied losslessly"
        else:
            lo, hi = float(np.percentile(v, 2)), float(np.percentile(v, 98))
            note = f"16-bit stretched {lo:.0f}-{hi:.0f}"
        print(f"[ingest] {Path(src_path).name}: {note}", flush=True)

        dst = RAW / f"{Path(src_path).stem}_8bit.tif"
        prof = dict(driver="GTiff", width=src.width, height=src.height, count=3, dtype="uint8", crs=src.crs,
                    transform=src.transform, tiled=True, blockxsize=512, blockysize=512, compress="deflate",
                    photometric="RGB", BIGTIFF="YES")
        B = 4096
        with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True, GDAL_NUM_THREADS="ALL_CPUS"), rasterio.open(dst, "w", **prof) as out:
            for y in range(0, src.height, B):
                for x in range(0, src.width, B):
                    w = Window(x, y, min(B, src.width - x), min(B, src.height - y))
                    a = src.read([1, 2, 3], window=w).astype(np.float32)
                    ok = src.dataset_mask(window=w) > 0
                    if nd is not None:
                        ok &= ~(a == nd).any(0)
                    out.write(np.clip((a - lo) * 255.0 / max(hi - lo, 1e-6), 0, 255).astype(np.uint8), window=w)
                    out.write_mask(np.where(ok, 255, 0).astype(np.uint8), window=w)
    return str(dst)


def _cog_translate(src_path, dst_path):
    from rio_cogeo.cogeo import cog_translate, cog_validate
    from rio_cogeo.profiles import cog_profiles

    tmp8 = _to_uint8(src_path)
    if tmp8:
        try:
            return _cog_translate(tmp8, dst_path)
        finally:
            os.remove(tmp8)

    with rasterio.open(src_path) as src:
        rgb8 = src.count >= 3 and src.dtypes[0] == "uint8"
        has_alpha = src.count == 4 or any(ci.name == "alpha" for ci in src.colorinterp)
        has_alpha = has_alpha or MaskFlags.per_dataset in src.mask_flag_enums[0]
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


def _tile_ready(path):
    """Already tiled with overviews (e.g. exported as COG / JPEG-tiled from QGIS)?
    Then it can be served as-is: re-encoding would only lose quality and time."""
    with rasterio.open(path) as src:
        bx, by = src.block_shapes[0]
        return (src.profile.get("tiled") and bx == by and bx in (256, 512, 1024)
                and len(src.overviews(1)) >= 3 and src.dtypes[0] == "uint8")


def ingest(img_id, src_path):
    """Register (if already tile-ready) or convert to COG, and record the result. Runs in a thread."""
    try:
        meta = describe(src_path)
        db.update_imagery(img_id, **{k: meta[k] for k in ("bounds", "crs", "gsd_m", "width", "height", "bands")})
        dst = COGS / f"{img_id}.tif"
        t0 = time.time()
        if _tile_ready(src_path):
            os.replace(src_path, dst)                    # same disk: instant move, no re-encode
            note = "already tiled with overviews — registered as-is"
        else:
            _cog_translate(src_path, str(dst))
            note = f"compressed in {time.time() - t0:.0f}s"
            if Path(src_path).parent == RAW:      # the served COG replaces the raw upload: free the disk
                os.remove(src_path)
        db.update_imagery(img_id, cog_path=str(dst), cog_bytes=dst.stat().st_size, status="ready", error=note)
    except Exception as e:
        traceback.print_exc()
        db.update_imagery(img_id, status="failed", error=str(e)[:500])


def start_import_r2(r2_client, bucket, key, name, kind, district=None, captured=None, delete_after=True):
    """Pull a large raster from R2 into data/raw, then ingest it. For files too big to
    upload through the Jupyter browser: send them to R2 from your machine, import here."""
    fname = os.path.basename(key) or "upload.tif"
    dst = RAW / fname
    head = r2_client.head_object(Bucket=bucket, Key=key)
    size = head["ContentLength"]
    img_id = db.add_imagery(name=name or Path(fname).stem, kind=kind, district=district, captured=captured,
                            source_path=str(dst), status="ingesting", source_bytes=size, error="downloading from R2 0%")

    def run():
        try:
            got = [0]
            last = [0.0]

            def cb(n):
                got[0] += n
                if time.time() - last[0] > 5:
                    last[0] = time.time()
                    db.update_imagery(img_id, error=f"downloading from R2 {100 * got[0] / size:.0f}%")

            from boto3.s3.transfer import TransferConfig
            r2_client.download_file(bucket, key, str(dst) + ".part", Callback=cb,
                                    Config=TransferConfig(multipart_chunksize=64 * 2**20, max_concurrency=8))
            os.replace(str(dst) + ".part", dst)
            db.update_imagery(img_id, error="downloaded — registering")
            ingest(img_id, str(dst))
            if delete_after and db.get_imagery(img_id)["status"] == "ready":
                r2_client.delete_object(Bucket=bucket, Key=key)   # keep R2 small: the server has the copy now
        except Exception as e:
            traceback.print_exc()
            db.update_imagery(img_id, status="failed", error=f"R2 import: {e}"[:500])

    threading.Thread(target=run, name=f"import-{img_id}", daemon=True).start()
    return img_id


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
# One open dataset per worker thread: a GDAL/rasterio handle must not be shared
# between threads — the map requests 10-20 tiles at once, and a shared reader made
# some of them fail with 500s.
_tls = threading.local()


def _reader(path):
    cache = getattr(_tls, "readers", None)
    if cache is None:
        cache = _tls.readers = {}
    r = cache.get(path)
    if r is None:
        r = cache[path] = Reader(path)
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
