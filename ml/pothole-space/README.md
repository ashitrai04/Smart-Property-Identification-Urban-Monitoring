# Pothole / road-damage detection API

YOLOv8s road-damage detector (`final_best.pt`, trained at 1024 px) served on CPU.

**Where it runs:** inside the `asashit/smart-property-segformer` Space, mounted by its
`server.py` under `/pothole/*` (new free-tier Docker Spaces need HF PRO; the existing
Space doesn't). The model loads lazily on the first pothole request.

- `pothole.py` — the router that is deployed (`python deploy_pothole_space.py` from the repo root)
- `app.py` — the same API as a standalone app, for local runs:
  `uvicorn app:app --port 7861`

## Endpoints

- `GET /pothole/health` — model info and whether it is loaded
- `POST /pothole/detect` — multipart `file` (image), `mode` = `auto` | `manual`,
  `conf` (0.05–0.95, manual only). Returns detections, per-class counts and an
  annotated JPEG as a data URL.

`auto` follows the rule from `test_final_model.py`: start at 0.50 and relax by 0.05
until damage is found (floor 0.15); if a step returns more than 25 boxes, step back up once.
Images with a longer side over 1600 px are tiled at 1024 px (20 % overlap) and merged with NMS.

Classes: D00 longitudinal crack · D10 transverse crack · D20 alligator crack · D40 pothole · Repair · potholes
