"""Run a "refine" (seamless layers + water check + SAM 2 outlines for every class) for one
image, outside the API — in the SAM 2 environment (.venv-sam2), with no server restart.

Waits until the image's segmentation is finished (and nothing is still queued for it),
then writes an ordinary "refine" job that the webapp shows like any other.

    .venv-sam2/bin/python refine_now.py --image img_xxx [--segment-job job_xxx]
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
os.environ.setdefault("HF_HOME", str(ROOT / "weights" / "hf"))
os.environ.setdefault("MPLBACKEND", "Agg")
sys.path.insert(0, str(ROOT / "app"))

import db  # noqa: E402
import jobs  # noqa: E402
import refine  # noqa: E402
from config import RESULTS  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--image", required=True)
ap.add_argument("--segment-job")
ap.add_argument("--no-sam", action="store_true")
a = ap.parse_args()


def seg_done():
    js = db.list_jobs(a.image)
    if any(j["task"] in ("segment", "fusion") and j["status"] in ("queued", "running") for j in js):
        return None
    if a.segment_job:
        j = db.get_job(a.segment_job)
        return j if j and j["status"] == "done" else None
    done = [j for j in js if j["task"] in ("segment", "fusion") and j["status"] == "done"]
    return max(done, key=lambda j: j.get("finished") or 0) if done else None


while (sj := seg_done()) is None:
    print(f"[refine_now] {time.strftime('%H:%M')} waiting for the segmentation of {a.image}...", flush=True)
    time.sleep(120)

img = db.get_imagery(a.image)
jid = db.new_id("job")
out_dir = RESULTS / jid
out_dir.mkdir(parents=True, exist_ok=True)
params = {"segment_job": sj["id"], "sam": not a.no_sam, "runner": "refine_now"}
db.conn().execute("INSERT INTO jobs (id, imagery_id, task, params, status, created, started, result_dir) "
                  "VALUES (?,?,?,?, 'running', ?, ?, ?)",
                  (jid, a.image, "refine", json.dumps(params), time.time(), time.time(), str(out_dir)))
db.conn().commit()
print(f"[refine_now] {jid}: refining {img['name']} from {sj['id']} (SAM model {refine.SAM_ID})", flush=True)
job = db.get_job(jid)
try:
    res = refine.run(job, img, out_dir, on_gpu=lambda fn: jobs._on_gpu(jid, fn),
                     progress=lambda p: jobs._progress(jid, p), cancelled=lambda: jobs._cancelled(jid))
    if res is None:
        db.update_job(jid, message="cancelled", finished=time.time())
    else:
        db.update_job(jid, status="done", progress=1.0, layers=res["layers"], stats=res["stats"], finished=time.time())
        print(f"[refine_now] done: {res['layers']}", flush=True)
except Exception as e:
    import traceback
    traceback.print_exc()
    db.update_job(jid, status="failed", message=str(e)[:500], finished=time.time())
    sys.exit(1)
