"""The fallback path: find buildings in the DSM when no detector output exists.

The division of labour is deliberate. The suite's engine is good at finding
*where* a raised, planar, non-vegetated object is, so it supplies the
geometry. It is poor at *how tall*, because it derives ground by morphological
opening. So every polygon it returns is then re-measured through heights.py,
which uses the surveyed DTM. Geometry from one, numbers from the other.
"""
import logging
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import rasterio
import rasterio.windows
import shapely
import shapely.ops
from rasterio.warp import transform_bounds

from . import config, heights
from .dsm_height_engine import DSMHeightEngine
from .heights import whole_pixels
from .tiles import INDEX

log = logging.getLogger("height.extract")

TILE = 2048
OVERLAP = 128
MAX_WINDOWS = 400          # about 1.5 km2 at 6 cm; beyond that, ask per area


def _windows(ds, bounds_native):
    """Tile the requested extent into overlapping windows inside the raster."""
    big = whole_pixels(ds.window(*bounds_native), ds)
    if big is None or big.width < 16 or big.height < 16:
        return []
    step = TILE - OVERLAP
    out = []
    r0, c0 = int(big.row_off), int(big.col_off)
    for r in range(r0, r0 + int(big.height), step):
        for c in range(c0, c0 + int(big.width), step):
            h = min(TILE, r0 + int(big.height) - r)
            w = min(TILE, c0 + int(big.width) - c)
            if h > 16 and w > 16:
                out.append(rasterio.windows.Window(c, r, w, h))
    return out


def _dedupe(polys, overlap_frac=0.35):
    """Drop the duplicates that tile overlap necessarily creates at the seams."""
    if not polys:
        return []
    tree = shapely.STRtree(polys)
    drop = set()
    for i, g in enumerate(polys):
        if i in drop:
            continue
        for j in tree.query(g):
            j = int(j)
            if j <= i or j in drop:
                continue
            h = polys[j]
            if not g.intersects(h):
                continue
            inter = g.intersection(h).area
            smaller = min(g.area, h.area)
            if smaller > 0 and inter / smaller > overlap_frac:
                drop.add(j if g.area >= h.area else i)
                if i in drop:
                    break
    return [g for i, g in enumerate(polys) if i not in drop]


def extract_bbox(geom_wgs, workers=4):
    """Detect footprints in the DSM over an EPSG:4326 area, then measure them."""
    tiles = INDEX.covering(geom_wgs, "DSM")
    if not tiles:
        return {"type": "FeatureCollection", "features": [], "summary": {}}

    tile = tiles[0]
    ds = INDEX.open(tile["path"])
    native = transform_bounds("EPSG:4326", ds.crs, *geom_wgs.bounds, densify_pts=21)
    wins = _windows(ds, native)
    if not wins:
        return {"type": "FeatureCollection", "features": [], "summary": {}}
    truncated = len(wins) > MAX_WINDOWS
    wins = wins[:MAX_WINDOWS]
    log.info("extract: %d windows from %s", len(wins), tile["name"])

    engine = DSMHeightEngine(
        min_building_height=config.MIN_HEIGHT,
        floor_height=config.FLOOR_HEIGHT,
    )
    res_x, res_y = abs(ds.res[0]), abs(ds.res[1])
    nodata = ds.nodata if ds.nodata is not None else -32767.0

    def run(win):
        arr = ds.read(1, window=win)
        tr = rasterio.windows.transform(win, ds.transform)
        try:
            return engine.extract_tile_buildings(
                dsm_tile=arr, tile_transform=tr, crs=ds.crs,
                res_x=res_x, res_y=res_y, nodata=nodata)
        except Exception as e:
            log.warning("window failed: %s", e)
            return []

    found = []
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        for recs in ex.map(run, wins):
            found.extend(r["geometry"] for r in recs)

    polys = _dedupe(found)

    # Back to 4326, clipped to what was actually asked for.
    inv = heights._to_crs(geom_wgs, ds.crs)
    polys = [p for p in polys if p.intersects(inv)]
    wgs = [heights._to_crs(p, "EPSG:4326", src_crs=ds.crs) for p in polys]

    # Re-measure with the real DTM. This is the whole point of the two-stage
    # shape: the engine's own height numbers are discarded.
    rows = heights.measure_many(wgs)
    feats = [{"type": "Feature",
              "geometry": shapely.geometry.mapping(g),
              "properties": dict(r, bldg_id=f"DSM_B{i + 1:05d}",
                                 area_m2=round(p.area, 1),
                                 perimeter_m=round(p.length, 1))}
             for i, (g, p, r) in enumerate(zip(wgs, polys, rows))]

    out = {"type": "FeatureCollection", "features": feats,
           "summary": heights.summarise(rows), "dsm_tile": tile["name"]}
    if truncated:
        out["warning"] = (f"area needed more than {MAX_WINDOWS} windows; only the "
                          "first part was processed - request smaller areas")
    return out
