"""Unattended: once both dates of a satellite pair are on the server, analyse them.

Waits until imagery of --district / kind "satellite" captured --before and --after is
registered, then: segmentation of each date with the in-house SegFormer-B5 (queued in
the API, at the image's own resolution), change detection between the dates
(change_now.py), and refinement of each date with SAM 2 for buildings, roads, open
plots and water (refine_now.py, in .venv-sam2). No pothole detection on satellite.
Run it with nohup; it needs no restart.

    nohup .venv/bin/python queue_satellite.py --district ongole --before 2017 --after 2026 > data/logs/satellite.log 2>&1 &
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for line in (ROOT / "config.env").read_text().splitlines() if (ROOT / "config.env").exists() else []:
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.split("#")[0].strip())
sys.path.insert(0, str(ROOT / "app"))

import db  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--district", default="ongole")
ap.add_argument("--before", default="2017")
ap.add_argument("--after", default="2026")
a = ap.parse_args()


def find(year):
    for i in db.list_imagery():
        if (i.get("kind") == "satellite" and (i.get("district") or "").lower() == a.district
                and str(i.get("captured") or "") == year and i.get("status") == "ready"):
            return i
    return None


while not (find(a.before) and find(a.after)):
    print(f"[satellite] {time.strftime('%H:%M')} waiting for {a.district} {a.before} and {a.after} to be uploaded...",
          flush=True)
    time.sleep(120)

for year in (a.before, a.after):
    img = find(year)
    done = {j["task"] for j in db.list_jobs(img["id"]) if j["status"] in ("queued", "running", "done")}
    if "segment" not in done:
        gsd = max(0.3, float(img.get("gsd_m") or 0.3))       # segment at the image's own resolution
        s = db.add_job(img["id"], "segment", {"target_gsd_m": gsd})     # in-house SegFormer-B5, in the API
        print(f"[satellite] {img['name']}: segment {s}", flush=True)
    else:
        print(f"[satellite] {img['name']}: already analysed / queued", flush=True)

print("[satellite] change detection, then SAM 2 refinement, once both segmentations are done", flush=True)
ids = [find(a.before)["id"], find(a.after)["id"]]
rc = subprocess.call([sys.executable, str(ROOT / "change_now.py"), "--before", ids[0], "--after", ids[1], "--wait"])
# SAM 2 lives in its own environment (setup_sam2.sh); without it, refine falls back to SAM 1
sam_py = ROOT / ".venv-sam2" / "bin" / "python"
py = str(sam_py) if sam_py.exists() else sys.executable
for i in ids:
    rc |= subprocess.call([py, str(ROOT / "refine_now.py"), "--image", i])
sys.exit(rc)
