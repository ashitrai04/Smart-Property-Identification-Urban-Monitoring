"""Batch jobs over whole orthomosaics: segment | fusion | pothole | refine (see refine.py).

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
import os
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
# ── shared GPU: wait out other users instead of failing ─────────
GPU_WAIT_S = int(os.environ.get("SP_GPU_WAIT_S", str(6 * 3600)))   # give up after this much waiting


def _on_gpu(jid, fn):
    """Run fn() under the GPU lock. Other people's programs on this shared A100 can take
    all its memory for a while; then wait (job message says so) and try again, instead
    of failing a job that is hours in."""
    waited, pause = 0, 20
    while True:
        try:
            with gpu.GPU_LOCK:
                out = fn()
            if waited:
                db.update_job(jid, message=None)
            return out
        except Exception as e:
            if not gpu.is_oom(e) or waited >= GPU_WAIT_S:
                raise
        gpu.release()
        info = gpu.device_info()
        db.update_job(jid, message=f"waiting for GPU memory — other users have it ({info.get('free_gb', '?')} GB free); "
                                   f"retrying, waited {waited // 60} min")
        print(f"[job {jid}] GPU full — waiting {pause}s", flush=True)
        if _cancelled(jid):
            raise RuntimeError("cancelled")
        time.sleep(pause)
        waited += pause
        pause = min(pause * 2, 120)


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
                    def _seg():
                        p = seg.segment(MODEL_(), rgb).astype(np.uint8)
                        if fusion:
                            import fusion as fu
                            return p, fu.fuse(rgb, p)
                        return p, None
                    pred, fz = _on_gpu(job["id"], _seg)
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
    """YOLO road damage over a whole orthomosaic, kept to roads.

    * Only windows that touch a (buffered) OSM road are read at all — fields and plots
      are skipped outright, which is most of a municipal area.
    * Every detection is scored against a road mask = OSM carriageway ∪ SegFormer road
      class; `on_road` = at least `min_overlap` of the box on road.
    * Ground-size check from the GSD: potholes ~0.15-4 m, cracks up to ~25 m.
    Off-road / implausible detections are stored too (on_road = 0) so they can be
    inspected, but the map shows on_road = 1 only.
    """
    import pothole as ph
    import inference as seg
    import roads
    from main import MODEL_

    params = job.get("params") or {}
    conf = float(params.get("conf", 0.25))
    road_filter = params.get("road_filter", True)
    min_overlap = float(params.get("min_overlap", 0.3))
    use_seg = params.get("seg_roads", True)
    win, ov = POTHOLE_WINDOW, 256
    step = win - 2 * ov
    totals, kept_n, off_n = {}, 0, 0

    with rasterio.open(img["cog_path"]) as src:
        sink = _Sink(out_dir / "results.gpkg", src.crs)
        gsd = abs(src.transform.a) if src.crs.is_projected else (img.get("gsd_m") or 0.05)
        W, H = src.width, src.height

        road_polys = None
        if road_filter:
            road_polys = roads.buffered_roads(roads.osm_roads(img["bounds"], img["id"]), src.crs)
            print(f"[pothole] {len(road_polys)} OSM road polygons for {img['id']}", flush=True)
            # Roads segmented from THIS imagery (a finished segment/fusion job) line up with the
            # pixels exactly — OSM can be 5-10 m off. Use both.
            rj = params.get("roads_job")
            if rj:
                own = roads.job_roads(rj, src.crs)
                if own is not None and len(own):
                    road_polys = gpd.GeoDataFrame(geometry=list(road_polys.geometry) + list(own.geometry), crs=src.crs)
                    print(f"[pothole] + {len(own)} road polygons from segmentation job {rj}", flush=True)

        windows = [(x, y) for y in _grid(H, win, step) for x in _grid(W, win, step)]
        if road_filter and road_polys is not None and len(road_polys):
            # only windows that touch a road (plus 20 m, for unmapped spurs SegFormer may find)
            from shapely.geometry import box as _box
            union_idx = road_polys.sindex
            keep = []
            for (x, y) in windows:
                w_, h_ = min(win, W - x), min(win, H - y)
                x0, y0 = src.transform * (x, y)
                x1, y1 = src.transform * (x + w_, y + h_)
                b = _box(min(x0, x1) - 20, min(y0, y1) - 20, max(x0, x1) + 20, max(y0, y1) + 20)
                if len(union_idx.query(b, predicate="intersects")):
                    keep.append((x, y))
            print(f"[pothole] {len(keep)}/{len(windows)} windows touch a road — the rest are skipped", flush=True)
            windows = keep

        n = len(windows)
        for done, (x, y) in enumerate(windows, 1):
            if _cancelled(job["id"]):
                return None
            w_, h_ = min(win, W - x), min(win, H - y)
            rgb, valid = _read_rgb(src, Window(x, y, w_, h_), w_, h_)
            if valid.mean() < 0.02:
                _progress(job["id"], done / n)
                continue
            bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            wt = src.transform * Affine.translation(x, y)
            boxes, confs, clss = _on_gpu(job["id"], lambda: ph._detect(bgr, conf))
            road = None
            if road_filter and len(boxes):
                road = roads.osm_mask(road_polys, wt, (h_, w_))
                if use_seg:
                    try:
                        road |= _on_gpu(job["id"], lambda: roads.seg_road_mask(rgb, gsd, MODEL_(), seg))
                    except Exception as e:   # SegFormer is a helper here, never a blocker
                        print("[pothole] seg road mask skipped:", e, flush=True)
            cx0, cy0 = (0 if x == 0 else ov), (0 if y == 0 else ov)
            cx1, cy1 = (w_ if x + w_ >= W else w_ - ov), (h_ if y + h_ >= H else h_ - ov)
            geoms, props = [], []
            for (x1, y1, x2, y2), c, k in zip(boxes, confs, clss):
                mx, my = (x1 + x2) / 2, (y1 + y2) / 2
                if not (cx0 <= mx < cx1 and cy0 <= my < cy1):
                    continue
                name = ph.NAMES[int(k)]
                is_pothole = name in ph.POTHOLE
                size_m = max(x2 - x1, y2 - y1) * gsd
                size_ok = (0.15 <= size_m <= 4.0) if is_pothole else (0.2 <= size_m <= 25.0)
                overlap = roads.box_overlap(road, x1, y1, x2, y2) if road is not None else 1.0
                on_road = int(overlap >= min_overlap and size_ok)
                ring = _pix_to_map([(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)], wt)
                geoms.append(shapely.Polygon(ring))
                props.append({"class_name": name, "label": ph.LABELS.get(name, name), "is_pothole": int(is_pothole),
                              "confidence": round(float(c), 4), "on_road": on_road,
                              "road_overlap": round(overlap, 3), "size_m": round(size_m, 2), "size_ok": int(size_ok)})
                if on_road:
                    kept_n += 1
                    lbl = ph.LABELS.get(name, name)
                    totals[lbl] = totals.get(lbl, 0) + 1
                else:
                    off_n += 1
            sink.add("road_damage", geoms, props)
            _progress(job["id"], done / n)

    return {"layers": sink.counts,
            "stats": {"counts": totals, "on_road": kept_n, "filtered_off_road": off_n, "conf": conf,
                      "road_filter": bool(road_filter), "min_overlap": min_overlap}}


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
        elif job["task"] == "refine":
            import refine
            res = refine.run(job, img, out_dir, on_gpu=lambda fn: _on_gpu(jid, fn),
                             progress=lambda p: _progress(jid, p), cancelled=lambda: _cancelled(jid))
        else:
            raise ValueError(f"unknown task {job['task']}")
        if res is None:
            db.update_job(jid, message="cancelled", finished=time.time())
            return
        db.update_job(jid, status="done", progress=1.0, layers=res["layers"], stats=res["stats"],
                      finished=time.time())
    except Exception as e:
        if _cancelled(jid):
            db.update_job(jid, message="cancelled", finished=time.time())
            return
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
