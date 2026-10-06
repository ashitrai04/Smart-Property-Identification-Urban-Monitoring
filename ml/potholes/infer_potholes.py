#!/usr/bin/env python3
"""
Road-damage / pothole inference for Smart Property — Urban Monitoring.

Model : YOLOv8s (Ultralytics), trained at imgsz=1024 on drone road-damage data
Classes: 0 D00 longitudinal crack | 1 D10 transverse crack | 2 D20 alligator crack
         3 D40 pothole | 4 Repair (patched road) | 5 potholes

Accepted sources (file, folder, or glob; mixed is fine):
  * GeoTIFF orthomosaics (.tif/.tiff with a CRS)  -> sliding-window tiling, georeferenced output
  * Plain images (.jpg/.png/.tif without CRS)     -> tiled automatically when much larger than imgsz
  * Videos (.mp4/.avi/.mov/.mkv)                  -> frame-stride inference, annotated video out

Outputs (in --out):
  detections.geojson   EPSG:4326 FeatureCollection (polygons for GeoTIFFs, points for EXIF-GPS photos)
                       -> drop into the webapp as the "potholes" layer
  detections.json      every detection with pixel boxes, per source
  summary.csv          per-source counts by class
  annotated/           images / ortho previews / videos with boxes drawn

Examples (GPU server):
  python infer_potholes.py --source /data/drone/ortho.tif --out runs/ortho1
  python infer_potholes.py --source /data/drone/photos/ --conf 0.3 --out runs/photos
  python infer_potholes.py --source flight.mp4 --vid-stride 5 --out runs/vid
  python infer_potholes.py --source img.jpg --weights roofs.pt     # roof-robust fine-tune
"""

import argparse
import csv
import glob
import json
import math
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import torch
from ultralytics import YOLO

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_WEIGHTS = SCRIPT_DIR / "weights" / "final_best.pt"

IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
VID_EXT = {".mp4", ".avi", ".mov", ".mkv", ".m4v"}

LABELS = {
    "D00": "Longitudinal crack",
    "D10": "Transverse crack",
    "D20": "Alligator crack",
    "D40": "Pothole",
    "Repair": "Repaired patch",
    "potholes": "Pothole",
}
POTHOLE_CLASSES = {"D40", "potholes"}
# BGR colours for drawing
COLORS = {
    "D00": (255, 200, 0), "D10": (255, 120, 0), "D20": (0, 200, 255),
    "D40": (0, 0, 255), "Repair": (0, 200, 0), "potholes": (0, 0, 255),
}


# ─────────────────────────────────────────────────────────────
#  Setup
# ─────────────────────────────────────────────────────────────
def pick_device(requested):
    """'auto' -> the CUDA GPU with the most free memory (the A100s are shared)."""
    if requested != "auto":
        return requested
    if not torch.cuda.is_available():
        return "cpu"
    best, best_free = 0, -1
    for i in range(torch.cuda.device_count()):
        free, total = torch.cuda.mem_get_info(i)
        print(f"   cuda:{i} {torch.cuda.get_device_name(i)}  free {free / 2**30:.1f} / {total / 2**30:.1f} GiB")
        if free > best_free:
            best, best_free = i, free
    return f"cuda:{best}"


def collect_sources(items):
    out = []
    for s in items:
        p = Path(s)
        if p.is_dir():
            out += sorted(f for f in p.rglob("*") if f.suffix.lower() in IMG_EXT | VID_EXT)
        elif any(c in s for c in "*?["):
            out += sorted(Path(f) for f in glob.glob(s, recursive=True))
        elif p.exists():
            out.append(p)
        else:
            print(f"⚠️  not found: {s}")
    return out


def open_geotiff(path):
    """Return an open rasterio dataset if the file is a georeferenced raster, else None."""
    if path.suffix.lower() not in {".tif", ".tiff"}:
        return None
    try:
        import rasterio
    except ImportError:
        print("⚠️  rasterio not installed — treating TIFF as a plain image")
        return None
    ds = rasterio.open(path)
    if ds.crs is None:
        ds.close()
        return None
    return ds


# ─────────────────────────────────────────────────────────────
#  Core: tiled prediction
# ─────────────────────────────────────────────────────────────
def tile_grid(width, height, tile, overlap):
    step = max(1, int(tile * (1 - overlap)))
    xs = list(range(0, max(width - tile, 0) + 1, step))
    ys = list(range(0, max(height - tile, 0) + 1, step))
    if xs[-1] + tile < width:
        xs.append(width - tile)
    if ys[-1] + tile < height:
        ys.append(height - tile)
    return [(max(x, 0), max(y, 0)) for y in ys for x in xs]


def _precision_kwargs(half):
    """Newer Ultralytics replaced `half` with `quantize`; support both."""
    try:
        from ultralytics.cfg import DEFAULT_CFG_DICT
        if "quantize" in DEFAULT_CFG_DICT:
            return {"quantize": 16 if half else None}
    except ImportError:
        pass
    return {"half": half}


def predict_batch(model, imgs, args):
    """imgs: list of HxWx3 BGR uint8. Returns list of (xyxy[N,4], conf[N], cls[N]) numpy arrays."""
    res = model.predict(imgs, imgsz=args.imgsz, conf=args.conf, iou=args.iou, device=args.device,
                        **_precision_kwargs(args.half), augment=args.tta, max_det=args.max_det, classes=args.class_ids,
                        verbose=False)
    out = []
    for r in res:
        b = r.boxes
        out.append((b.xyxy.cpu().numpy(), b.conf.cpu().numpy(), b.cls.cpu().numpy().astype(int)))
    return out


def merge_nms(xyxy, conf, cls, iou):
    """Class-aware NMS across tile seams."""
    if len(xyxy) == 0:
        return xyxy, conf, cls
    from torchvision.ops import batched_nms
    keep = batched_nms(torch.as_tensor(xyxy, dtype=torch.float32), torch.as_tensor(conf, dtype=torch.float32),
                       torch.as_tensor(cls), iou).numpy()
    return xyxy[keep], conf[keep], cls[keep]


def run_tiled(model, read_tile, width, height, args):
    """Generic sliding window. read_tile(x, y, w, h) -> BGR uint8 tile or None (skip empty)."""
    tile = args.tile
    grid = tile_grid(width, height, tile, args.overlap)
    all_xyxy, all_conf, all_cls = [], [], []
    batch_imgs, batch_off = [], []
    skipped = 0
    t0 = time.time()

    def flush():
        for (ox, oy), (xyxy, conf, cls) in zip(batch_off, predict_batch(model, batch_imgs, args)):
            if len(xyxy):
                all_xyxy.append(xyxy + [ox, oy, ox, oy])
                all_conf.append(conf)
                all_cls.append(cls)
        batch_imgs.clear()
        batch_off.clear()

    for i, (x, y) in enumerate(grid, 1):
        img = read_tile(x, y, min(tile, width - x), min(tile, height - y))
        if img is None:
            skipped += 1
        else:
            batch_imgs.append(img)
            batch_off.append((x, y))
            if len(batch_imgs) >= args.batch:
                flush()
        if i % 200 == 0 or i == len(grid):
            rate = i / max(time.time() - t0, 1e-6)
            print(f"\r   tiles {i}/{len(grid)}  (skipped empty {skipped})  {rate:.1f} tiles/s", end="", flush=True)
    if batch_imgs:
        flush()
    print()

    if not all_xyxy:
        return np.zeros((0, 4)), np.zeros(0), np.zeros(0, dtype=int)
    return merge_nms(np.concatenate(all_xyxy), np.concatenate(all_conf), np.concatenate(all_cls), args.merge_iou)


# ─────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────
def to_uint8_bgr(arr, nodata_mask=None):
    """rasterio (bands, H, W) -> H x W x 3 BGR uint8; stretches 16-bit / float data."""
    if arr.shape[0] >= 3:
        rgb = arr[:3]
    else:
        rgb = np.repeat(arr[:1], 3, axis=0)
    if rgb.dtype != np.uint8:
        valid = rgb[:, ~nodata_mask] if nodata_mask is not None and (~nodata_mask).any() else rgb.reshape(3, -1)
        lo, hi = np.percentile(valid, 2), np.percentile(valid, 98)
        rgb = np.clip((rgb.astype(np.float32) - lo) / max(hi - lo, 1e-6) * 255, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(rgb.transpose(1, 2, 0)[:, :, ::-1])


def draw(img, xyxy, conf, cls, names, thickness=2):
    for (x1, y1, x2, y2), c, k in zip(xyxy.astype(int), conf, cls):
        n = names[k]
        col = COLORS.get(n, (255, 255, 255))
        cv2.rectangle(img, (x1, y1), (x2, y2), col, thickness)
        txt = f"{n} {c:.2f}"
        (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(img, (x1, max(y1 - th - 6, 0)), (x1 + tw + 4, max(y1, th + 6)), col, -1)
        cv2.putText(img, txt, (x1 + 2, max(y1 - 4, th + 2)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)
    return img


def severity(name, area_m2, length_m):
    """Rough heuristic for triage on the map — tune with field data."""
    if area_m2 is None:
        return None
    if name in POTHOLE_CLASSES:
        return "high" if area_m2 >= 1.0 else "medium" if area_m2 >= 0.25 else "low"
    if name == "Repair":
        return "info"
    if name == "D20":  # alligator cracking = structural failure
        return "high" if area_m2 >= 4.0 else "medium"
    return "high" if length_m >= 3.0 else "medium" if length_m >= 1.0 else "low"


def exif_gps(path):
    """(lat, lon) from JPEG EXIF if present."""
    try:
        from PIL import Image
        gps = Image.open(path).getexif().get_ifd(0x8825)
        if not gps or 2 not in gps or 4 not in gps:
            return None

        def dms(v):
            return float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600

        lat, lon = dms(gps[2]), dms(gps[4])
        if gps.get(1) == "S":
            lat = -lat
        if gps.get(3) == "W":
            lon = -lon
        return lat, lon
    except Exception:
        return None


def det_records(xyxy, conf, cls, names):
    return [{"class_id": int(k), "class_name": names[k], "label": LABELS.get(names[k], names[k]),
             "is_pothole": names[k] in POTHOLE_CLASSES, "confidence": round(float(c), 4),
             "bbox_xyxy": [round(float(v), 1) for v in b]}
            for b, c, k in zip(xyxy, conf, cls)]


# ─────────────────────────────────────────────────────────────
#  Per-source handlers
# ─────────────────────────────────────────────────────────────
def process_geotiff(model, ds, path, args, names, geo_features):
    from rasterio.windows import Window
    from rasterio.warp import transform as warp_transform
    from pyproj import Geod

    W, H = ds.width, ds.height
    gsd = abs(ds.transform.a)
    unit = "m" if ds.crs.is_projected else "deg"
    print(f"   GeoTIFF {W}x{H}, {ds.count} bands, {ds.crs}, GSD {gsd:.4f} {unit}")
    if ds.count < 3:
        print("   ⚠️  fewer than 3 bands — this looks like a mask, not an RGB orthomosaic")
    if ds.crs.is_projected and gsd > 0.2:
        print(f"   ⚠️  GSD {gsd:.2f} m is coarse; the model was trained on ~1–5 cm/px drone imagery")

    has_alpha = ds.count >= 4
    nodata = ds.nodata

    def read_tile(x, y, w, h):
        win = Window(x, y, w, h)
        arr = ds.read(window=win)
        mask = None
        if has_alpha:
            mask = arr[3] == 0
        elif nodata is not None:
            mask = (arr[:3] == nodata).all(axis=0)
        if mask is None:
            mask = (arr[:3] == 0).all(axis=0)  # black ortho collar
        if mask.mean() > args.skip_empty:
            return None
        return to_uint8_bgr(arr, mask)

    xyxy, conf, cls = run_tiled(model, read_tile, W, H, args)
    print(f"   → {len(xyxy)} detections after seam NMS")

    geod = Geod(ellps="WGS84")
    recs = det_records(xyxy, conf, cls, names)
    for rec, (x1, y1, x2, y2) in zip(recs, xyxy):
        corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]
        mx, my = zip(*[ds.transform * c for c in corners])
        lon, lat = warp_transform(ds.crs, "EPSG:4326", list(mx), list(my))
        ring = [[round(a, 8), round(b, 8)] for a, b in zip(lon, lat)]
        area, _ = geod.polygon_area_perimeter(lon[:-1], lat[:-1])
        area = abs(area)
        _, _, w_m = geod.inv(lon[0], lat[0], lon[1], lat[1])
        _, _, h_m = geod.inv(lon[1], lat[1], lon[2], lat[2])
        rec.update(area_m2=round(area, 3), length_m=round(max(w_m, h_m), 2),
                   severity=severity(rec["class_name"], area, max(w_m, h_m)),
                   center=[round(sum(lon[:-1]) / 4, 8), round(sum(lat[:-1]) / 4, 8)])
        geo_features.append({"type": "Feature",
                             "geometry": {"type": "Polygon", "coordinates": [ring]},
                             "properties": {**{k: v for k, v in rec.items() if k not in ("bbox_xyxy", "center")},
                                            "source": path.name}})

    if args.save_vis:
        scale = min(1.0, args.preview_max / max(W, H))
        pw, ph = max(1, int(W * scale)), max(1, int(H * scale))
        from rasterio.enums import Resampling
        prev = ds.read(out_shape=(ds.count, ph, pw), resampling=Resampling.average)
        prev = to_uint8_bgr(prev)
        draw(prev, xyxy * scale, conf, cls, names, thickness=max(1, int(3 * scale + 1)))
        out = args.out / "annotated" / f"{path.stem}_preview.jpg"
        cv2.imwrite(str(out), prev, [cv2.IMWRITE_JPEG_QUALITY, 90])
        # full-res crops around every pothole for review
        crop_dir = args.out / "annotated" / f"{path.stem}_crops"
        crop_dir.mkdir(parents=True, exist_ok=True)
        for i, ((x1, y1, x2, y2), c, k) in enumerate(zip(xyxy.astype(int), conf, cls)):
            if i >= args.max_crops:
                break
            pad = 64
            cx1, cy1 = max(x1 - pad, 0), max(y1 - pad, 0)
            cx2, cy2 = min(x2 + pad, W), min(y2 + pad, H)
            crop = to_uint8_bgr(ds.read(window=Window(cx1, cy1, cx2 - cx1, cy2 - cy1)))
            draw(crop, np.array([[x1 - cx1, y1 - cy1, x2 - cx1, y2 - cy1]]), [c], [k], names)
            cv2.imwrite(str(crop_dir / f"{i:05d}_{names[k]}_{c:.2f}.jpg"), crop)
    return recs


def process_image(model, path, args, names, geo_features):
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        print("   ⚠️  could not read image")
        return []
    H, W = img.shape[:2]
    if max(W, H) > args.tile * 1.5 and not args.no_tile:
        print(f"   large image {W}x{H} → tiling")
        xyxy, conf, cls = run_tiled(model, lambda x, y, w, h: img[y:y + h, x:x + w], W, H, args)
    else:
        xyxy, conf, cls = predict_batch(model, [img], args)[0]
    recs = det_records(xyxy, conf, cls, names)
    print(f"   → {len(recs)} detections")

    gps = exif_gps(path)
    if gps:
        for rec in recs:
            geo_features.append({"type": "Feature",
                                 "geometry": {"type": "Point", "coordinates": [round(gps[1], 8), round(gps[0], 8)]},
                                 "properties": {**{k: v for k, v in rec.items() if k != "bbox_xyxy"},
                                                "source": path.name, "geo_precision": "image_center_exif"}})
    if args.save_vis:
        cv2.imwrite(str(args.out / "annotated" / f"{path.stem}.jpg"), draw(img.copy(), xyxy, conf, cls, names))
    return recs


def process_video(model, path, args, names):
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    writer = None
    if args.save_vis:
        writer = cv2.VideoWriter(str(args.out / "annotated" / f"{path.stem}.mp4"),
                                 cv2.VideoWriter_fourcc(*"mp4v"), fps / args.vid_stride, (W, H))
    recs, frames, idxs = [], [], []

    def flush():
        for f, fi, (xyxy, conf, cls) in zip(frames, idxs, predict_batch(model, frames, args)):
            for r in det_records(xyxy, conf, cls, names):
                r.update(frame=fi, time_s=round(fi / fps, 3))
                recs.append(r)
            if writer:
                writer.write(draw(f, xyxy, conf, cls, names))
        frames.clear()
        idxs.clear()

    fi = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if fi % args.vid_stride == 0:
            frames.append(frame)
            idxs.append(fi)
            if len(frames) >= args.batch:
                flush()
        fi += 1
        if fi % 300 == 0:
            print(f"\r   frame {fi}/{total}", end="", flush=True)
    if frames:
        flush()
    cap.release()
    if writer:
        writer.release()
    print(f"\n   → {len(recs)} detections over {fi} frames")
    return recs


# ─────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", nargs="+", required=True, help="file(s), folder(s) or glob(s)")
    ap.add_argument("--weights", default=str(DEFAULT_WEIGHTS))
    ap.add_argument("--out", default="runs/potholes")
    ap.add_argument("--device", default="auto", help="auto | cpu | 0 | cuda:1 ...")
    ap.add_argument("--imgsz", type=int, default=1024, help="model input size (trained at 1024)")
    ap.add_argument("--tile", type=int, default=1024, help="tile size in source pixels for large rasters")
    ap.add_argument("--overlap", type=float, default=0.2, help="tile overlap fraction")
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--iou", type=float, default=0.5, help="NMS IoU inside a tile")
    ap.add_argument("--merge-iou", type=float, default=0.4, help="NMS IoU across tile seams")
    ap.add_argument("--batch", type=int, default=16, help="tiles/frames per GPU batch")
    ap.add_argument("--max-det", type=int, default=300)
    ap.add_argument("--classes", nargs="*", default=None,
                    help="keep only these class names, e.g. --classes D40 potholes")
    ap.add_argument("--skip-empty", type=float, default=0.9, help="skip ortho tiles with > this nodata fraction")
    ap.add_argument("--vid-stride", type=int, default=1)
    ap.add_argument("--tta", action="store_true", help="test-time augmentation (slower, sometimes better)")
    ap.add_argument("--no-half", dest="half", action="store_false", help="disable FP16 on GPU")
    ap.add_argument("--no-tile", action="store_true", help="never tile plain images")
    ap.add_argument("--no-vis", dest="save_vis", action="store_false", help="skip annotated outputs")
    ap.add_argument("--preview-max", type=int, default=8000, help="max side of ortho preview jpg")
    ap.add_argument("--max-crops", type=int, default=500, help="max review crops per ortho")
    args = ap.parse_args()

    args.out = Path(args.out)
    (args.out / "annotated").mkdir(parents=True, exist_ok=True)

    print("🔧 Selecting device...")
    args.device = pick_device(args.device)
    if args.device == "cpu":
        args.half = False
    print(f"   using {args.device}  fp16={args.half}")

    if not Path(args.weights).exists():
        sys.exit(f"❌ weights not found: {args.weights}")
    model = YOLO(args.weights)
    names = model.names
    print(f"📦 {Path(args.weights).name}: {names}")

    args.class_ids = None
    if args.classes:
        inv = {v: k for k, v in names.items()}
        bad = [c for c in args.classes if c not in inv]
        if bad:
            sys.exit(f"❌ unknown classes {bad}; choose from {list(names.values())}")
        args.class_ids = [inv[c] for c in args.classes]

    sources = collect_sources(args.source)
    if not sources:
        sys.exit("❌ no input files")

    # warm-up so the first timing isn't skewed by CUDA init
    predict_batch(model, [np.zeros((args.imgsz, args.imgsz, 3), np.uint8)], args)

    all_results, geo_features, summary = [], [], []
    t_all = time.time()
    for n, path in enumerate(sources, 1):
        print(f"\n[{n}/{len(sources)}] {path}")
        t0 = time.time()
        ext = path.suffix.lower()
        try:
            if ext in VID_EXT:
                kind, recs = "video", process_video(model, path, args, names)
            else:
                ds = open_geotiff(path)
                if ds is not None:
                    with ds:
                        kind, recs = "geotiff", process_geotiff(model, ds, path, args, names, geo_features)
                else:
                    kind, recs = "image", process_image(model, path, args, names, geo_features)
        except Exception as e:
            print(f"   ❌ failed: {e}")
            summary.append({"source": str(path), "type": "error", "error": str(e)})
            continue
        dt = time.time() - t0
        counts = {v: 0 for v in names.values()}
        for r in recs:
            counts[r["class_name"]] += 1
        all_results.append({"source": str(path), "type": kind, "seconds": round(dt, 2), "detections": recs})
        summary.append({"source": str(path), "type": kind, "seconds": round(dt, 2), "total": len(recs),
                        "pothole_total": sum(counts[c] for c in POTHOLE_CLASSES if c in counts), **counts})

    with open(args.out / "detections.json", "w") as f:
        json.dump({"weights": str(args.weights), "names": names, "conf": args.conf, "imgsz": args.imgsz,
                   "results": all_results}, f, indent=1)
    with open(args.out / "detections.geojson", "w") as f:
        json.dump({"type": "FeatureCollection", "name": "road_damage",
                   "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
                   "features": geo_features}, f)
    cols = ["source", "type", "seconds", "total", "pothole_total", *names.values(), "error"]
    with open(args.out / "summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(summary)

    total = sum(s.get("total", 0) for s in summary)
    print(f"\n✅ {len(sources)} source(s), {total} detections, {time.time() - t_all:.1f}s")
    print(f"   {args.out / 'detections.geojson'}  ({len(geo_features)} georeferenced features)")
    print(f"   {args.out / 'detections.json'}")
    print(f"   {args.out / 'summary.csv'}")
    if args.save_vis:
        print(f"   {args.out / 'annotated'}/")


if __name__ == "__main__":
    main()
