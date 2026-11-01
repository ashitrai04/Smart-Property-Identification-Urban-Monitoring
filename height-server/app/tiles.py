"""An index of the clip-wise DSM and DTM rasters, so a query reads one window.

The folders hold many clips covering different ground. Opening all of them per
request is impossible, and building a full mosaic would mean copying terabytes,
so the index records only each file's footprint - cheap to build, cheap to
cache - and a query then opens the one or two files it actually needs.

Bounds are stored in EPSG:4326 as well as native, so DSM and DTM clips can be
matched to each other and to HiSup's output even when the clips sit in
different projections.
"""
import json
import logging
import threading
import time
from pathlib import Path

import rasterio
import shapely
from rasterio.warp import transform_bounds
from shapely.geometry import box

from . import config

log = logging.getLogger("height.tiles")


def _describe(path: Path):
    """Read one raster's georeferencing without reading any pixels."""
    try:
        with rasterio.open(path) as src:
            if src.crs is None:
                log.warning("%s has no CRS - skipped", path.name)
                return None
            b = src.bounds
            wgs = transform_bounds(src.crs, "EPSG:4326", *b, densify_pts=21)
            return {
                "path": str(path),
                "name": path.name,
                "crs": src.crs.to_string(),
                "bounds": [b.left, b.bottom, b.right, b.top],
                "bounds_wgs84": list(wgs),
                "width": src.width,
                "height": src.height,
                "res": [abs(src.res[0]), abs(src.res[1])],
                "nodata": src.nodata,
                "dtype": str(src.dtypes[0]),
                "mtime": path.stat().st_mtime,
            }
    except Exception as e:
        log.warning("could not read %s: %s", path.name, e)
        return None


class TileIndex:
    """DSM and DTM footprints, with the overlap query the measurement needs."""

    def __init__(self):
        self.dsm = []
        self.dtm = []
        self.built_at = None
        self.errors = []
        self._lock = threading.Lock()
        self._open = {}

    # -- build / cache ------------------------------------------------
    def build(self, force=False):
        """Scan both folders. Uses the cache unless a file changed or force=True."""
        with self._lock:
            if not force and self._load_cache():
                log.info("tile index loaded from cache: %d DSM, %d DTM",
                         len(self.dsm), len(self.dtm))
                return self.summary()

            self.errors = []
            self.dsm = self._scan(config.DSM_DIR, "DSM")
            self.dtm = self._scan(config.DTM_DIR, "DTM")
            self.built_at = time.time()
            self._save_cache()
            log.info("tile index built: %d DSM, %d DTM", len(self.dsm), len(self.dtm))
            return self.summary()

    def _scan(self, folder, label):
        if not folder.exists():
            msg = label + " folder does not exist: " + str(folder)
            log.error(msg)
            self.errors.append(msg)
            return []
        # rglob, so a folder of per-clip subfolders works as well as a flat one.
        paths = sorted(p for p in folder.rglob("*")
                       if p.is_file() and p.suffix.lower() in config.RASTER_EXT)
        if not paths:
            msg = "no rasters found under " + str(folder)
            log.error(msg)
            self.errors.append(msg)
            return []
        out = []
        for p in paths:
            d = _describe(p)
            if d:
                d["kind"] = label
                out.append(d)
            else:
                self.errors.append(label + ": unreadable " + p.name)
        return out

    def _load_cache(self):
        if not config.INDEX_PATH.exists():
            return False
        try:
            raw = json.loads(config.INDEX_PATH.read_text(encoding="utf-8"))
        except Exception:
            return False
        # A cache built against other folders describes other ground.
        if raw.get("dsm_dir") != str(config.DSM_DIR):
            return False
        if raw.get("dtm_dir") != str(config.DTM_DIR):
            return False
        for e in raw.get("dsm", []) + raw.get("dtm", []):
            p = Path(e["path"])
            if not p.exists() or p.stat().st_mtime != e.get("mtime"):
                return False   # something was added, replaced or removed
        self.dsm = raw.get("dsm", [])
        self.dtm = raw.get("dtm", [])
        self.built_at = raw.get("built_at")
        self.errors = raw.get("errors", [])
        return bool(self.dsm)

    def _save_cache(self):
        try:
            config.INDEX_PATH.write_text(json.dumps({
                "dsm_dir": str(config.DSM_DIR),
                "dtm_dir": str(config.DTM_DIR),
                "built_at": self.built_at,
                "errors": self.errors,
                "dsm": self.dsm,
                "dtm": self.dtm,
            }, indent=1), encoding="utf-8")
        except Exception as e:
            log.warning("could not write index cache: %s", e)

    # -- query --------------------------------------------------------
    def covering(self, geom, kind="DSM"):
        """Tiles whose footprint intersects this EPSG:4326 geometry, best first.

        Finest resolution wins first: where clips overlap, the sharper one
        carries the better roof edge, which is what the height is measured
        from.

        Among clips of equal resolution the one the geometry sits deepest
        inside wins. Survey clips overlap heavily - 34 clips covering 2.5x
        their own union, in the survey this was built for - and a clip's outer
        margin is often nodata where a neighbouring clip has real elevation.
        Picking by bounding-box intersection alone lands on that empty margin
        for a third of buildings near a seam, which reads as "too few pixels
        to measure" when the data was there all along in the next clip.

        The ordering only makes the first attempt usually right; the caller
        still walks the list until a clip actually yields pixels.
        """
        tiles = self.dsm if kind == "DSM" else self.dtm
        hits = []
        for t in tiles:
            b = box(*t["bounds_wgs84"])
            if not b.intersects(geom):
                continue
            # Distance from the geometry out to the clip's edge: large means
            # safely inside, 0 means it straddles the edge.
            inset = geom.distance(b.exterior) if b.contains(geom) else 0.0
            hits.append((t["res"][0], -inset, t))
        hits.sort(key=lambda x: (x[0], x[1]))
        return [t for _, _, t in hits]

    def open(self, path):
        """Cached dataset handles - reopening per polygon dominates the runtime."""
        with self._lock:
            ds = self._open.get(path)
            if ds is None or ds.closed:
                ds = rasterio.open(path)
                self._open[path] = ds
            return ds

    def close_all(self):
        with self._lock:
            for ds in self._open.values():
                try:
                    ds.close()
                except Exception:
                    pass
            self._open.clear()

    # -- reporting ----------------------------------------------------
    def coverage(self, kind="DSM"):
        """Union of all footprints for this kind, as an EPSG:4326 geometry."""
        tiles = self.dsm if kind == "DSM" else self.dtm
        if not tiles:
            return None
        return shapely.union_all([box(*t["bounds_wgs84"]) for t in tiles])

    def summary(self):
        def part(tiles):
            if not tiles:
                return {"count": 0}
            res = sorted(t["res"][0] for t in tiles)
            px = sum(t["width"] * t["height"] for t in tiles)
            return {
                "count": len(tiles),
                "gsd_m": {"min": round(res[0], 4),
                          "median": round(res[len(res) // 2], 4),
                          "max": round(res[-1], 4)},
                "megapixels": round(px / 1e6, 1),
                "crs": sorted({t["crs"] for t in tiles}),
            }

        cov_dsm = self.coverage("DSM")
        cov_dtm = self.coverage("DTM")
        overlap = None
        if cov_dsm is not None and cov_dtm is not None and cov_dsm.area:
            overlap = round(cov_dsm.intersection(cov_dtm).area / cov_dsm.area * 100, 1)
        return {
            "dsm_dir": str(config.DSM_DIR),
            "dtm_dir": str(config.DTM_DIR),
            "dsm": part(self.dsm),
            "dtm": part(self.dtm),
            "dtm_covers_pct_of_dsm": overlap,
            "bounds_wgs84": list(cov_dsm.bounds) if cov_dsm is not None else None,
            "built_at": self.built_at,
            "errors": self.errors,
        }


INDEX = TileIndex()
