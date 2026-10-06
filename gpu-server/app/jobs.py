"""Batch jobs over whole orthomosaics: segment | fusion | pothole.

The raster is walked in overlapping windows read straight from the COG (only the
blocks needed, optionally at a coarser target GSD), so a 50 GB file never has to
fit in memory. Each window's output is written immediately:
  * vectors  -> results/<job>/results.gpkg  (EPSG:4326, R-tree indexed)
  * classes  -> results/<job>/classes.tif   (uint8 class map, tiled, served as map tiles)
Only the centre of each window is kept (the overlap is context), so tiles join
without seams.
"""
import json
import math
import threading
import time
import traceback
from pathlib import Path

import cv2
import geopandas as gpd
import numpy as np
import rasterio
import shapely
from rasterio.enums import Resampling
from rasterio.features import shapes
from rasterio.transform import Affine
from rasterio.windows import Window

import db
import gpu
from config import JOB_WINDOW, POTHOLE_WINDOW, RESULTS

OVERLAP = 128
SEG_LAYERS = {1: "buildings", 2: "roads", 3: "waterbodies", 4: "openareas"}
_stop = threading.Event()


# ── helpers ──────────────────────────────────────────────────────
def _grid(n, size, step):
    xs = list(range(0, max(n - size, 0) + 1, step))
    if xs[-1] + size < n:
        xs.append(max(n - size, 0))
    return xs


def _read_rgb(src, win, out_w, out_h):
    """RGB uint8 + validity mask for a source window, resampled to out size."""
    idx = [1, 2, 3] if src.count >= 3 else [1, 1, 1]
    arr = src.read(idx, window=win, out_shape=(3, out_h, out_w), resampling=Resampling.average, boundless=True)
    msk = src.dataset_mask(window=win, out_shape=(out_h, out_w), resampling=Resampling.nearest, boundless=True) > 0
    if arr.dtype != np.uint8:
        a = arr.astype(np.float32)
        valid = a[:, msk] if msk.any() else a.reshape(3, -1)
        lo, hi = np.percentile(valid, 2), np.percentile(valid, 98)
        arr = np.clip((a - lo) * 255.0 / max(hi - lo, 1e-6), 0, 255).astype(np.uint8)
    return np.ascontiguousarray(arr.transpose(1, 2, 0)), msk


class _Sink:
    """Appends GeoDataFrames to one GeoPackage, one layer per kind, in EPSG:4326."""

    def __init__(self, path, src_crs):
        self.path = str(path)
        self.crs = src_crs
        self.counts = {}

    def add(self, layer, geoms, props):
        if not len(geoms):
            return
        gdf = gpd.GeoDataFrame(props, geometry=list(geoms), crs=self.crs).to_crs(4326)
        gdf = gdf[~gdf.geometry.is_empty]
        if not len(gdf):
            return
        mode = "a" if layer in self.counts else "w"
        gdf.to_file(self.path, layer=layer, driver="GPKG", engine="pyogrio", mode=mode)
        self.counts[layer] = self.counts.get(layer, 0) + len(gdf)


def _pix_to_map(ring_xy, transform):
    """Window-pixel coords -> source CRS coords."""
    a = np.asarray(ring_xy, float)
    xs, ys = transform * (a[:, 0], a[:, 1])
    return np.column_stack([xs, ys])


def _keep_centre(geoms, transform, win_px, keep_px):
    """Keep shapes whose centroid falls in the window's non-overlap centre."""
    x0, y0, x1, y1 = keep_px
    inv = ~transform
    keep = []
    for g in geoms:
        c = g.centroid
        px, py = inv * (c.x, c.y)
        keep.append(x0 <= px < x1 and y0 <= py < y1)
    return np.array(keep, bool)


# ── task: segment / fusion ───────────────────────────────────────
def _run_segmentation(job, img, out_dir, fusion=False):
    import inference as seg
    from main import MODEL_   # the same lazily-loaded SegFormer the API uses

    params = job.get("params") or {}
    with rasterio.open(img["cog_path"]) as src:
        native = abs(src.transform.a)
        target = float(params.get("target_gsd_m") or 0) or None
        # GSD in CRS units; for metre CRSs the param is metres.
        scale = (target / native) if (target and src.crs.is_projected and target > native) else 1.0
        W, H = int(src.width / scale), int(src.height / scale)
        T = src.transform * Affine.scale(scale)                 # target-grid transform
        win = min(JOB_WINDOW, seg.MAX_DIM)
        step = win - 2 * OVERLAP

        prof = dict(driver="GTiff", width=W, height=H, count=1, dtype="uint8", crs=src.crs, transform=T,
                    tiled=True, blockxsize=512, blockysize=512, compress="deflate", nodata=0, BIGTIFF="IF_SAFER")
        sink = _Sink(out_dir / "results.gpkg", src.crs)
        totals = {v: 0 for v in SEG_LAYERS.values()}
        px_area = abs(T.a * T.e) if src.crs.is_projected else None
        ys, xs = _grid(H, win, step), _grid(W, win, step)
        n, done = len(ys) * len(xs), 0

        with rasterio.open(out_dir / "classes.tif", "w", **prof) as dst:
            for y in ys:
                for x in xs:
                    if _cancelled(job["id"]):
                        return None
                    w, h = min(win, W - x), min(win, H - y)
                    src_win = Window(x * scale, y * scale, w * scale, h * scale)
                    rgb, valid = _read_rgb(src, src_win, w, h)
                    done += 1
                    if valid.mean() < 0.02:
                        _progress(job["id"], done / n)
                        continue
                    with gpu.GPU_LOCK:
                        pred = seg.segment(MODEL_(), rgb).astype(np.uint8)
                        fz = None
                        if fusion:
                            import fusion as fu
                            fz = fu.fuse(rgb, pred)
                    pred[~valid] = 0
                    # centre region (overlap trimmed, except at raster edges)
                    cx0 = 0 if x == 0 else OVERLAP
                    cy0 = 0 if y == 0 else OVERLAP
                    cx1 = w if x + w >= W else w - OVERLAP
                    cy1 = h if y + h >= H else h - OVERLAP
                    centre = pred[cy0:cy1, cx0:cx1]
                    dst.write(centre, 1, window=Window(x + cx0, y + cy0, cx1 - cx0, cy1 - cy0))
                    wt = T * Affine.translation(x, y)          # window-pixel -> CRS

                    if fusion and fz is not None:
                        # SAM instances: one polygon per building
                        # fusion works on a copy capped at FUSE_MAX_DIM: rescale to window pixels
                        fs = w / float(fz["working_size"]["width"])
                        geoms = []
                        for f in fz["buildings_geojson"]["features"]:
                            ring = np.asarray(f["geometry"]["coordinates"][0], float) * fs
                            if len(ring) >= 4:
                                geoms.append(shapely.Polygon(_pix_to_map(ring, wt)))
                        if geoms:
                            k = _keep_centre(geoms, wt, (w, h), (cx0, cy0, cx1, cy1))
                            geoms = [g for g, kk in zip(geoms, k) if kk]
                            props = [{"area_m2": round(g.area, 2) if px_area else None} for g in geoms]
                            sink.add("buildings", geoms, props)
                            totals["buildings"] += len(geoms)
                        layers = {2: "roads", 3: "waterbodies", 4: "openareas"}
                    else:
                        layers = SEG_LAYERS

                    # polygonise the kept centre, class by class
                    ct = wt * Affine.translation(cx0, cy0)
                    for cls, name in layers.items():
                        m = (centre == cls).astype(np.uint8)
                        if m.sum() < 16:
                            continue
                        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
                        geoms = [shapely.geometry.shape(g) for g, v in shapes(m, mask=m.astype(bool), transform=ct) if v == 1]
                        geoms = [g.simplify(abs(T.a) * 0.75) for g in geoms if g.area > 16 * abs(T.a * T.e)]
                        if geoms:
                            sink.add(name, geoms, [{"area_m2": round(g.area, 2) if px_area else None} for g in geoms])
                            totals[name] += len(geoms)
                    _progress(job["id"], done / n)
    return {"layers": sink.counts, "stats": {"features": totals, "grid": [W, H], "gsd": abs(T.a)}}


# ── task: pothole ────────────────────────────────────────────────
def _run_pothole(job, img, out_dir):
    import pothole as ph

    params = job.get("params") or {}
    conf = float(params.get("conf", 0.25))
    win = POTHOLE_WINDOW
    ov = 256
    step = win - 2 * ov
    sink = None
    totals = {}
    with rasterio.open(img["cog_path"]) as src:
        sink = _Sink(out_dir / "results.gpkg", src.crs)
        W, H = src.width, src.height
        ys, xs = _grid(H, win, step), _grid(W, win, step)
        n, done = len(ys) * len(xs), 0
        for y in ys:
            for x in xs:
                if _cancelled(job["id"]):
                    return None
                w, h = min(win, W - x), min(win, H - y)
                rgb, valid = _read_rgb(src, Window(x, y, w, h), w, h)
                done += 1
                if valid.mean() < 0.02:
                    _progress(job["id"], done / n)
                    continue
                bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                with gpu.GPU_LOCK:
                    boxes, confs, clss = ph._detect(bgr, conf)
                wt = src.transform * Affine.translation(x, y)
                cx0, cy0 = (0 if x == 0 else ov), (0 if y == 0 else ov)
                cx1, cy1 = (w if x + w >= W else w - ov), (h if y + h >= H else h - ov)
                geoms, props = [], []
                for (x1, y1, x2, y2), c, k in zip(boxes, confs, clss):
                    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
                    if not (cx0 <= mx < cx1 and cy0 <= my < cy1):
                        continue
                    ring = _pix_to_map([(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)], wt)
                    name = ph.NAMES[int(k)]
                    geoms.append(shapely.Polygon(ring))
                    props.append({"class_name": name, "label": ph.LABELS.get(name, name),
                                  "is_pothole": name in ph.POTHOLE, "confidence": round(float(c), 4)})
                    totals[ph.LABELS.get(name, name)] = totals.get(ph.LABELS.get(name, name), 0) + 1
                sink.add("road_damage", geoms, props)
                _progress(job["id"], done / n)
    return {"layers": sink.counts, "stats": {"counts": totals, "total": sum(totals.values()), "conf": conf}}


# ── worker ───────────────────────────────────────────────────────
def _cancelled(jid):
    return (db.get_job(jid) or {}).get("status") == "cancelled"


_last_progress = {}


def _progress(jid, p):
    now = time.time()
    if now - _last_progress.get(jid, 0) > 2 or p >= 1:
        db.update_job(jid, progress=round(min(p, 1.0), 4))
        _last_progress[jid] = now


def run_job(job):
    jid = job["id"]
    img = db.get_imagery(job["imagery_id"])
    if not img or img["status"] != "ready":
        db.update_job(jid, status="failed", message="imagery not ready", finished=time.time())
        return
    out_dir = RESULTS / jid
    out_dir.mkdir(parents=True, exist_ok=True)
    db.update_job(jid, status="running", started=time.time(), result_dir=str(out_dir),
                  device=json.dumps(gpu.device_info()), message=None)
    try:
        if job["task"] in ("segment", "fusion"):
            res = _run_segmentation(job, img, out_dir, fusion=job["task"] == "fusion")
        elif job["task"] == "pothole":
            res = _run_pothole(job, img, out_dir)
        else:
            raise ValueError(f"unknown task {job['task']}")
        if res is None:
            db.update_job(jid, message="cancelled", finished=time.time())
            return
        db.update_job(jid, status="done", progress=1.0, layers=res["layers"], stats=res["stats"],
                      finished=time.time())
    except Exception as e:
        traceback.print_exc()
        db.update_job(jid, status="failed", message=str(e)[:500], finished=time.time())
    finally:
        gpu.release()


def worker():
    while not _stop.is_set():
        job = db.next_queued()
        if job:
            run_job(job)
        else:
            _stop.wait(3)


def start_worker():
    t = threading.Thread(target=worker, name="job-worker", daemon=True)
    t.start()
    return t
