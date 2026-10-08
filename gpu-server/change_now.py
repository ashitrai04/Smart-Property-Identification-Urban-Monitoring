"""Change detection between two dates of the same area, from their segmentations.

Runs on its own next to the live API (no restart, no GPU): waits until both images
have a finished segment job, compares their class maps on the newer image's grid and
writes the changes as an ordinary job of task "change" (on the newer image), which the
webapp shows like any other layer:

  new_buildings      building now, not before          removed_buildings  the reverse
  new_roads          road now, not before
  water_gained       water now, not before             water_lost         the reverse

The two dates are rarely registered to the pixel, so each comparison allows a small
shift (a building counts as new only if no building was within SHIFT_M before) and
tiny or sliver changes are dropped.

    .venv/bin/python change_now.py --before img_xxx --after img_yyy --wait
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for line in (ROOT / "config.env").read_text().splitlines() if (ROOT / "config.env").exists() else []:
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.split("#")[0].strip())
sys.path.insert(0, str(ROOT / "app"))

import cv2                                   # noqa: E402
import numpy as np                           # noqa: E402
import rasterio                              # noqa: E402
from rasterio.enums import Resampling        # noqa: E402
from rasterio.vrt import WarpedVRT           # noqa: E402
from rasterio.windows import Window          # noqa: E402

import db                                    # noqa: E402
import refine                                # noqa: E402
from config import RESULTS                   # noqa: E402

BUILDING, ROAD, WATER = 1, 2, 3
SHIFT_M = {BUILDING: 2.5, ROAD: 4.0, WATER: 5.0}   # allowed mis-registration between dates
EDGE_M = 15                                         # ignore this close to either image's edge
CODES = {   # code: (layer, close px, fill holes < m², drop < m², simplify m)
    1: ("new_buildings", 1, 20, 25, 0.4),
    2: ("removed_buildings", 1, 20, 25, 0.4),
    3: ("new_roads", 1, 30, 150, 0.6),
    4: ("water_gained", 1, 50, 300, 0.8),
    5: ("water_lost", 1, 50, 300, 0.8),
}
BLOCK, PAD = 4096, 64

ap = argparse.ArgumentParser()
ap.add_argument("--before", required=True, help="imagery id of the older image")
ap.add_argument("--after", required=True, help="imagery id of the newer image")
ap.add_argument("--wait", action="store_true", help="wait for both segmentations to finish")
a = ap.parse_args()


def seg_job(img_id):
    js = [j for j in db.list_jobs(img_id) if j["task"] in ("segment", "fusion") and j["status"] == "done"]
    return max(js, key=lambda j: j.get("finished") or 0) if js else None


def pending(img_id):
    return any(j["task"] in ("segment", "fusion") and j["status"] in ("queued", "running") for j in db.list_jobs(img_id))


while True:
    sb, sa = seg_job(a.before), seg_job(a.after)
    if sb and sa and not pending(a.before) and not pending(a.after):
        break
    if not a.wait:
        sys.exit("Both images need a finished segment job (use --wait to wait for them).")
    print(f"[change] {time.strftime('%H:%M')} waiting for segmentation of both dates...", flush=True)
    time.sleep(120)

img_b, img_a = db.get_imagery(a.before), db.get_imagery(a.after)
jid = db.new_id("job")
out_dir = RESULTS / jid
out_dir.mkdir(parents=True, exist_ok=True)
params = {"before_imagery": a.before, "before_job": sb["id"], "after_job": sa["id"]}
# inserted as "running" directly, so the API's job worker never picks it up
db.conn().execute("INSERT INTO jobs (id, imagery_id, task, params, status, created, started, result_dir) "
                  "VALUES (?,?,?,?, 'running', ?, ?, ?)",
                  (jid, a.after, "change", json.dumps(params), time.time(), time.time(), str(out_dir)))
db.conn().commit()
def progress(p):
    db.update_job(jid, progress=round(min(p, 1.0), 4))


def padded(read, ds, win):
    """read(window=...) for a window that may stick out of the raster: zeros outside."""
    x0, y0 = int(win.col_off), int(win.row_off)
    w, h = int(win.width), int(win.height)
    cx0, cy0 = max(0, x0), max(0, y0)
    cx1, cy1 = min(ds.width, x0 + w), min(ds.height, y0 + h)
    out = np.zeros((h, w), np.uint8)
    if cx1 > cx0 and cy1 > cy0:
        out[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0] = read(window=Window(cx0, cy0, cx1 - cx0, cy1 - cy0))
    return out


def disk(r_px):
    r = max(1, int(round(r_px)))
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))


try:
    print(f"[change] job {jid}: {img_b['name']} -> {img_a['name']}", flush=True)
    t0 = time.time()
    with rasterio.open(os.path.join(sa["result_dir"], "classes.tif")) as A, \
            rasterio.open(os.path.join(sb["result_dir"], "classes.tif")) as B0, \
            rasterio.open(img_a["cog_path"]) as IA0, rasterio.open(img_b["cog_path"]) as IB0:
        grid = dict(crs=A.crs, transform=A.transform, width=A.width, height=A.height, resampling=Resampling.nearest)
        px = abs(A.transform.a)
        with WarpedVRT(B0, **grid) as B, WarpedVRT(IA0, **grid) as IA, WarpedVRT(IB0, **grid) as IB:
            prof = dict(driver="GTiff", width=A.width, height=A.height, count=1, dtype="uint8", crs=A.crs,
                        transform=A.transform, tiled=True, blockxsize=512, blockysize=512, compress="deflate",
                        BIGTIFF="IF_SAFER")
            blocks = [(x, y) for y in range(0, A.height, BLOCK) for x in range(0, A.width, BLOCK)]
            edge = disk(EDGE_M / px)
            with rasterio.open(out_dir / "change.tif", "w", **prof) as dst:
                for i, (x, y) in enumerate(blocks, 1):
                    w, h = min(BLOCK, A.width - x), min(BLOCK, A.height - y)
                    win = Window(x - PAD, y - PAD, w + 2 * PAD, h + 2 * PAD)
                    ca = padded(lambda window: A.read(1, window=window), A, win)
                    cb = padded(lambda window: B.read(1, window=window), B, win)
                    valid = (padded(IA.dataset_mask, IA, win) > 0) & (padded(IB.dataset_mask, IB, win) > 0)
                    code = np.zeros(ca.shape, np.uint8)
                    if valid.any() and (ca.any() or cb.any()):
                        valid = cv2.erode(valid.astype(np.uint8), edge).astype(bool)
                        ca, cb = refine._majority(ca), refine._majority(cb)
                        for cls, (gain, loss) in {WATER: (4, 5), ROAD: (3, None), BUILDING: (1, 2)}.items():
                            ma, mb = ca == cls, cb == cls
                            k = disk(SHIFT_M[cls] / px)
                            near_b = cv2.dilate(mb.astype(np.uint8), k).astype(bool)
                            near_a = cv2.dilate(ma.astype(np.uint8), k).astype(bool)
                            code[ma & ~near_b & valid] = gain
                            if loss:
                                code[mb & ~near_a & valid] = loss
                    dst.write(code[PAD:PAD + h, PAD:PAD + w], 1, window=Window(x, y, w, h))
                    progress(0.6 * i / len(blocks))

    layers = refine._seamless_layers(out_dir / "change.tif", lambda p: progress(0.6 + 0.35 * p),
                                     rules=CODES, order=(1, 2, 3, 4, 5), ncls=6)
    from jobs import _Sink
    crs = next(iter(layers.values())).crs
    sink = _Sink(out_dir / "results.gpkg", crs)
    for name, gdf in layers.items():
        if len(gdf):
            sink.add(name, list(gdf.geometry), gdf.drop(columns="geometry").to_dict("records"))
    stats = {"before": img_b["name"], "after": img_a["name"],
             "area_m2": {k: round(float(v.area.sum()), 1) for k, v in layers.items()},
             "count": {k: len(v) for k, v in layers.items()}, "minutes": round((time.time() - t0) / 60, 1)}
    db.update_job(jid, status="done", progress=1.0, layers=sink.counts, stats=stats, finished=time.time())
    print(f"[change] done: {stats['count']}", flush=True)
except Exception as e:
    import traceback
    traceback.print_exc()
    db.update_job(jid, status="failed", message=str(e)[:500], finished=time.time())
    sys.exit(1)
