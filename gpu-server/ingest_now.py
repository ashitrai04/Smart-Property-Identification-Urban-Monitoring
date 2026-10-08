"""Import an uploaded raster and queue its analysis, without restarting the server.

Runs on its own (next to the live API, sharing its catalogue): converts the file in
data/raw/ to a map-ready COG, registers it, then queues segmentation followed by
road-gated pothole detection (using that segmentation's roads). The running server
picks the jobs up in order and serves the imagery as soon as it is registered.

    .venv/bin/python ingest_now.py GUNTUR_DRONE.tif --name "Guntur drone" --district guntur
"""
import argparse
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

import db                      # noqa: E402
import imagery                 # noqa: E402
from config import RAW         # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("file", help="file name in data/raw (as uploaded)")
ap.add_argument("--name")
ap.add_argument("--district", required=True)
ap.add_argument("--kind", default="drone")
ap.add_argument("--gsd", type=float, default=0.25, help="segmentation resolution, m/px")
ap.add_argument("--no-analyse", action="store_true")
a = ap.parse_args()

src = RAW / os.path.basename(a.file)
for r in db.list_imagery():
    if r.get("source_path") == str(src) and r.get("status") in ("ingesting", "ready"):
        sys.exit(f"Already imported as {r['id']} ({r['status']})")
if not src.exists():
    sys.exit(f"Not found: {src} (is the upload complete?)")

img_id = db.add_imagery(name=a.name or src.stem, kind=a.kind, district=a.district, source_path=str(src),
                        status="ingesting", source_bytes=src.stat().st_size)
print(f"[{time.strftime('%H:%M')}] importing {src.name} as {img_id} ...", flush=True)
t0 = time.time()
imagery.ingest(img_id, str(src))
img = db.get_imagery(img_id)
print(f"[{time.strftime('%H:%M')}] {img['status']}: {img.get('error')} ({(time.time() - t0) / 60:.0f} min)", flush=True)
if img["status"] != "ready":
    sys.exit(1)

if not a.no_analyse:
    seg = db.add_job(img_id, "segment", {"target_gsd_m": a.gsd})
    pot = db.add_job(img_id, "pothole", {"conf": 0.25, "roads_job": seg, "seg_roads": False, "min_overlap": 0.3})
    print(f"queued segmentation {seg} and potholes {pot} (they run after the jobs already queued)", flush=True)
print("DONE", flush=True)
