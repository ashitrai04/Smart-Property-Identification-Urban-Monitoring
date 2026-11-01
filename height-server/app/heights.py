"""Measure the height of footprints that were detected somewhere else.

This is the part that differs from DSM_DTM_Building_Height_Suite on purpose.
That suite finds buildings *in* the DSM, by thresholding nDSM and running a
watershed. Here the footprints arrive already detected - from HiSup, at
F1 0.81 on RGB - so re-finding them from elevation would replace a good
detector with a worse one. The elevation data is used only for what RGB
cannot give: how tall each building is.

Two further differences, both accuracy rather than plumbing:

* **The real DTM is used when a tile covers the building.** The suite derives
  ground by morphological opening with a 15-22 m radius, which assumes bare
  ground exists within that radius. In dense low-rise - which is most of
  Ongole - it does not, so the derived "ground" sits part-way up the roofs and
  every height comes out short. A surveyed DTM has no such failure mode.

* **The footprint is shrunk before sampling.** A polygon traced from imagery
  includes the wall face and a little shadow, and those pixels are at ground
  level. Averaging them in pulls a two-storey roof down by a metre or more.
"""
import logging
import math

import numpy as np
import rasterio
import rasterio.features
import rasterio.windows
import shapely
import shapely.ops
from pyproj import Transformer
from rasterio.warp import Resampling, reproject, transform_bounds

from . import config
from .tiles import INDEX

log = logging.getLogger("height.measure")

_TF_CACHE = {}


def _to_crs(geom, dst_crs, src_crs="EPSG:4326"):
    key = (str(src_crs), str(dst_crs))
    tf = _TF_CACHE.get(key)
    if tf is None:
        tf = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
        _TF_CACHE[key] = tf
    return shapely.ops.transform(lambda x, y, z=None: tf.transform(x, y), geom)


def _clean(arr, nodata):
    """Elevation rasters carry several flavours of 'no data'. Unify to NaN."""
    a = arr.astype("float32", copy=True)
    bad = ~np.isfinite(a)
    if nodata is not None and np.isfinite(nodata):
        bad |= np.isclose(a, float(nodata))
    bad |= a < -1000.0        # the -32767 / -9999 family
    bad |= a > 1e6
    a[bad] = np.nan
    return a


def whole_pixels(win, ds, pad_px=0):
    """Snap a float window out to whole pixels and clip it to the raster.

    Window.round_offsets(op=...) was removed in rasterio 1.4, and
    Window.intersection raises rather than returning empty when there is no
    overlap, so both are done by hand here - it keeps the package working
    across the rasterio versions these two laptops are likely to have.
    """
    c0 = int(math.floor(win.col_off)) - pad_px
    r0 = int(math.floor(win.row_off)) - pad_px
    c1 = int(math.ceil(win.col_off + win.width)) + pad_px
    r1 = int(math.ceil(win.row_off + win.height)) + pad_px
    c0, r0 = max(0, c0), max(0, r0)
    c1, r1 = min(ds.width, c1), min(ds.height, r1)
    if c1 <= c0 or r1 <= r0:
        return None
    return rasterio.windows.Window(c0, r0, c1 - c0, r1 - r0)


def _read_window(ds, bounds, pad_px=0):
    """Read the smallest window of ds covering these bounds, in its own CRS."""
    try:
        win = whole_pixels(ds.window(*bounds), ds, pad_px)
    except Exception:
        return None, None
    if win is None:
        return None, None
    arr = ds.read(1, window=win)
    return _clean(arr, ds.nodata), rasterio.windows.transform(win, ds.transform)


def _read_aligned(ds, dst_shape, dst_transform, dst_crs, dst_bounds):
    """Read ds and resample it onto an existing grid (used to put DTM on DSM)."""
    src_bounds = dst_bounds
    if str(ds.crs) != str(dst_crs):
        src_bounds = transform_bounds(dst_crs, ds.crs, *dst_bounds, densify_pts=21)
    arr, src_transform = _read_window(ds, src_bounds, pad_px=2)
    if arr is None or not np.any(np.isfinite(arr)):
        return None
    out = np.full(dst_shape, np.nan, dtype="float32")
    try:
        reproject(
            source=arr, destination=out,
            src_transform=src_transform, src_crs=ds.crs,
            dst_transform=dst_transform, dst_crs=dst_crs,
            src_nodata=np.nan, dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )
    except Exception as e:
        log.warning("DTM resample failed: %s", e)
        return None
    return out if np.any(np.isfinite(out)) else None


def _floors(eff_h):
    """Storey count, using the suite's own thresholds so numbers stay comparable."""
    fh, off = config.FLOOR_HEIGHT, config.FLOOR_OFFSET
    for n in (1, 2, 3, 4):
        if eff_h < n * fh + off:
            return n, f"{n} Floor (G)" if n == 1 else f"{n} Floors (G+{n - 1})"
    n = max(1, int(round(eff_h / fh)))
    return n, f"{n} Floors (G+{n - 1})"


def _rasterize(geom, shape, transform):
    if geom.is_empty:
        return np.zeros(shape, dtype=bool)
    return rasterio.features.rasterize(
        [(geom, 1)], out_shape=shape, transform=transform,
        fill=0, all_touched=False, dtype="uint8").astype(bool)


# How many overlapping clips to try before giving up on a footprint. Survey
# clips overlap, and a clip's margin is often nodata where its neighbour has
# real elevation, so the first clip that covers a building on paper is not
# always the one holding its pixels. Four is well past the point of
# diminishing returns and bounds the cost for a footprint genuinely in a gap.
MAX_TILE_TRIES = 4


def _blank():
    return {
        "height_m": None, "mean_height_m": None, "max_height_m": None,
        "min_height_m": None, "floors": None, "floor_label": None,
        "ground_elev_m": None, "roof_elev_m": None, "height_source": None,
        "dtm_minus_ring_m": None, "n_pixels": 0, "gsd_m": None,
        "roughness_m": None, "dsm_tile": None, "dtm_tile": None,
        "quality": "no_dsm",
    }


def measure_one(geom_wgs, edge_buffer=None, want_ring=True):
    """Height metrics for a single EPSG:4326 polygon.

    Returns a dict that always has `quality`; everything numeric is None when
    the measurement could not be made, so a caller can attach the result to a
    feature without having to branch.

    Where clips overlap, each is tried in turn until one actually yields roof
    pixels. Stopping at the first clip whose *bounds* contain the building
    silently loses every building sitting on a nodata margin - a third of them
    in the survey this was built for.
    """
    if geom_wgs is None or geom_wgs.is_empty:
        out = _blank()
        out["quality"] = "empty_geometry"
        return out

    dsm_tiles = INDEX.covering(geom_wgs, "DSM")
    if not dsm_tiles:
        return _blank()

    best = None
    for tile in dsm_tiles[:MAX_TILE_TRIES]:
        try:
            r = _measure_on(tile, geom_wgs, edge_buffer, want_ring)
        except Exception as e:
            log.warning("clip %s failed for one footprint: %s", tile["name"], e)
            continue
        if r["quality"] == "good":
            return r
        # Not good on this clip: keep whichever attempt saw the most roof, so
        # a genuine "below_min_height" is reported rather than an empty margin.
        if best is None or r["n_pixels"] > best["n_pixels"]:
            best = r
    return best if best is not None else _blank()


def _measure_on(tile, geom_wgs, edge_buffer, want_ring):
    """One attempt, against one DSM clip."""
    blank = _blank()
    ds = INDEX.open(tile["path"])
    geom = _to_crs(geom_wgs, ds.crs)
    gsd = (abs(ds.res[0]) + abs(ds.res[1])) / 2.0

    # Read a margin wide enough to hold the outside ground ring too.
    margin = config.RING_OUTER_M + 2 * gsd
    b = geom.buffer(margin).bounds
    dsm, transform = _read_window(ds, b)
    if dsm is None:
        return blank
    shape = dsm.shape
    blank["gsd_m"] = round(gsd, 4)
    blank["dsm_tile"] = tile["name"]

    # Roof pixels: the footprint pulled in off its own wall line.
    eb = config.EDGE_BUFFER_M if edge_buffer is None else float(edge_buffer)
    inner = geom.buffer(-eb) if eb > 0 else geom
    if inner.is_empty or inner.area < 0.2 * geom.area:
        inner = geom          # too small to shrink; a shed is still a building
    roof = _rasterize(inner, shape, transform) & np.isfinite(dsm)
    if roof.sum() < config.MIN_PIXELS:
        roof = _rasterize(geom, shape, transform) & np.isfinite(dsm)
    n = int(roof.sum())
    blank["n_pixels"] = n
    if n < max(4, config.MIN_PIXELS // 4):
        blank["quality"] = "low_pixels"
        return blank

    # Ground, route 1: the surveyed DTM, read onto the DSM grid. The DTM clips
    # overlap exactly as the DSM ones do, so the same walk applies - the first
    # clip covering this ground may hold nodata where the next one holds terrain.
    dtm_ground = None
    for dtile in INDEX.covering(geom_wgs, "DTM")[:MAX_TILE_TRIES]:
        try:
            dtm = _read_aligned(INDEX.open(dtile["path"]), shape, transform, ds.crs, b)
        except Exception as e:
            log.warning("DTM clip %s failed: %s", dtile["name"], e)
            continue
        if dtm is None:
            continue
        vals = dtm[roof & np.isfinite(dtm)]
        if vals.size >= 4:
            dtm_ground = float(np.nanmedian(vals))
            blank["dtm_tile"] = dtile["name"]
            break

    # Ground, route 2: a ring of DSM just outside the footprint. This is the
    # fallback where no DTM tile reaches, and a cross-check where one does -
    # a large disagreement means the DTM is stale or wrongly referenced.
    ring_ground = None
    if want_ring:
        outer = _rasterize(geom.buffer(config.RING_OUTER_M), shape, transform)
        skirt = _rasterize(geom.buffer(config.RING_INNER_M), shape, transform)
        ring = outer & ~skirt & np.isfinite(dsm)
        if ring.sum() >= 20:
            # A low percentile, not the mean: the ring crosses neighbouring
            # roofs and trees as well as the street.
            ring_ground = float(np.nanpercentile(dsm[ring], 10))

    if dtm_ground is not None:
        ground, source = dtm_ground, "dtm"
    elif ring_ground is not None:
        ground, source = ring_ground, "ring"
    else:
        blank["quality"] = "no_ground"
        return blank

    if dtm_ground is not None and ring_ground is not None:
        blank["dtm_minus_ring_m"] = round(dtm_ground - ring_ground, 2)

    roof_vals = dsm[roof]
    ndsm = roof_vals - ground
    ndsm = np.clip(ndsm, 0.0, config.MAX_HEIGHT)

    mean_h = float(np.nanmean(ndsm))
    max_h = float(np.nanpercentile(ndsm, 95))
    min_h = float(np.nanpercentile(ndsm, 10))
    rough = float(np.nanstd(ndsm))

    floors, label = _floors(max_h)
    quality = "good"
    if max_h < config.MIN_HEIGHT:
        quality = "below_min_height"
    elif n < config.MIN_PIXELS:
        quality = "low_pixels"
    elif rough > config.ROUGH_M:
        # Advisory only - see HS_ROUGH_M. The number itself is in the
        # response, so a caller can apply its own cut instead.
        quality = "rough"

    blank.update({
        "height_m": round(max_h, 2),          # the headline number
        "mean_height_m": round(mean_h, 2),
        "max_height_m": round(max_h, 2),
        "min_height_m": round(min_h, 2),
        "floors": floors,
        "floor_label": label,
        "ground_elev_m": round(ground, 2),
        "roof_elev_m": round(float(np.nanpercentile(roof_vals, 95)), 2),
        "height_source": source,
        "roughness_m": round(rough, 2),
        "quality": quality,
    })
    return blank


def measure_many(geoms_wgs, edge_buffer=None, want_ring=True):
    """Measure a batch, grouped so each raster is opened once."""
    out = []
    for g in geoms_wgs:
        try:
            out.append(measure_one(g, edge_buffer=edge_buffer, want_ring=want_ring))
        except Exception as e:
            log.exception("measure failed: %s", e)
            r = {"quality": "error", "error": str(e), "height_m": None,
                 "floors": None, "n_pixels": 0}
            out.append(r)
    return out


def summarise(rows):
    """The aggregate block the HiSup side shows next to a finished job."""
    ok = [r for r in rows if r.get("height_m") is not None and r.get("quality") == "good"]
    hs = [r["height_m"] for r in ok]
    by_floor = {}
    for r in ok:
        by_floor[r["floor_label"]] = by_floor.get(r["floor_label"], 0) + 1
    deltas = [abs(r["dtm_minus_ring_m"]) for r in rows
              if r.get("dtm_minus_ring_m") is not None]
    qual = {}
    for r in rows:
        qual[r.get("quality", "?")] = qual.get(r.get("quality", "?"), 0) + 1
    return {
        "n_input": len(rows),
        "n_measured": len(ok),
        "mean_height_m": round(sum(hs) / len(hs), 2) if hs else None,
        "max_height_m": round(max(hs), 2) if hs else None,
        "median_height_m": round(float(np.median(hs)), 2) if hs else None,
        "floors": dict(sorted(by_floor.items())),
        "quality": qual,
        "dtm_vs_ring_median_m": round(float(np.median(deltas)), 2) if deltas else None,
    }
