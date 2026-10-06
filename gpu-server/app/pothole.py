"""
Smart Property — road-damage (pothole) detection API.

Serves the YOLOv8s road-damage model (final_best.pt, trained at 1024 px) on CPU.
Mounted as a router (/pothole/*) both in the SegFormer HF Space (CPU fallback)
and on the GPU server. The model loads lazily on the first pothole request.

Classes: D00 longitudinal crack · D10 transverse crack · D20 alligator crack ·
         D40 pothole · Repair · potholes
"""
import base64
import io
import os
import threading
import time

os.environ.setdefault("YOLO_CONFIG_DIR", "/tmp/Ultralytics")   # HF runs as a non-root user

import cv2
import numpy as np
import torch
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from PIL import Image
from torchvision.ops import batched_nms
from ultralytics import YOLO

WEIGHTS = os.environ.get("WEIGHTS", os.path.join(os.path.dirname(__file__), "final_best.pt"))
IMGSZ = 1024
TILE = 1024
OVERLAP = 0.2
TILE_ABOVE = 1600            # images with a longer side than this are tiled
MAX_SIDE = 8192              # refuse anything bigger (CPU Space, keep it responsive)
MAX_BYTES = 40 * 1024 * 1024
MAX_DET = 300

LABELS = {
    "D00": "Longitudinal crack", "D10": "Transverse crack", "D20": "Alligator crack",
    "D40": "Pothole", "Repair": "Repaired patch", "potholes": "Pothole",
}
POTHOLE = {"D40", "potholes"}
COLORS = {  # BGR — matches the webapp legend
    "D00": (191, 212, 45), "D10": (248, 189, 56), "D20": (36, 165, 245),
    "D40": (68, 68, 239), "Repair": (94, 197, 34), "potholes": (68, 68, 239),
}

WEIGHTS = os.environ.get("POTHOLE_WEIGHTS", WEIGHTS)
DEVICE = os.environ.get("SEG_DEVICE") or ("cuda:0" if torch.cuda.is_available() else "cpu")
HALF = DEVICE.startswith("cuda")
_model = None
NAMES = {0: "D00", 1: "D10", 2: "D20", 3: "D40", 4: "Repair", 5: "potholes"}
_lock = threading.Lock()     # one inference at a time on a small CPU box

router = APIRouter(prefix="/pothole", tags=["pothole"])


def _m():
    global _model, NAMES
    if _model is None:
        print("[pothole] first request — loading YOLO weights…", flush=True)
        _model = YOLO(WEIGHTS)
        NAMES = _model.names
        print("[pothole] ready", flush=True)
    return _model


# ─────────────────────────────────────────────────────────────
def _predict(imgs, conf):
    res = _m().predict(imgs, imgsz=IMGSZ, conf=conf, iou=0.5, device=DEVICE, half=HALF, max_det=MAX_DET, verbose=False)
    return [(r.boxes.xyxy.cpu().numpy(), r.boxes.conf.cpu().numpy(), r.boxes.cls.cpu().numpy().astype(int)) for r in res]


def _grid(n, tile, step):
    xs = list(range(0, max(n - tile, 0) + 1, step))
    if xs[-1] + tile < n:
        xs.append(n - tile)
    return xs


def _detect(img, conf):
    """Single pass for normal photos; overlapping tiles + seam NMS for large orthophotos."""
    h, w = img.shape[:2]
    if max(h, w) <= TILE_ABOVE:
        return _predict([img], conf)[0]
    step = int(TILE * (1 - OVERLAP))
    boxes, confs, clss = [], [], []
    tiles = [(x, y) for y in _grid(h, TILE, step) for x in _grid(w, TILE, step)]
    for i in range(0, len(tiles), 4):
        batch = tiles[i:i + 4]
        for (x, y), (b, c, k) in zip(batch, _predict([img[y:y + TILE, x:x + TILE] for x, y in batch], conf)):
            if len(b):
                boxes.append(b + [x, y, x, y]); confs.append(c); clss.append(k)
    if not boxes:
        return np.zeros((0, 4)), np.zeros(0), np.zeros(0, int)
    b, c, k = np.concatenate(boxes), np.concatenate(confs), np.concatenate(clss)
    keep = batched_nms(torch.as_tensor(b, dtype=torch.float32), torch.as_tensor(c, dtype=torch.float32),
                       torch.as_tensor(k), 0.4).numpy()
    return b[keep], c[keep], k[keep]


def _auto(img):
    """The auto-confidence rule from test_final_model.py: start strict (0.50) and
    relax in 0.05 steps until damage is found; if a step is too noisy (>25 boxes),
    step back up once and stop. Floor 0.15."""
    conf, last = 0.50, None
    while conf >= 0.15 - 1e-9:
        b, c, k = _detect(img, conf)
        if len(b) == 0:
            last = (b, c, k)
            conf = round(conf - 0.05, 2)
            continue
        if len(b) > 25:
            conf = round(conf + 0.05, 2)
            return (*_detect(img, conf), conf)
        return b, c, k, conf
    return (*last, 0.15) if last else (*_detect(img, 0.15), 0.15)


def _draw(img, boxes, confs, clss):
    out = img.copy()
    t = max(2, round(max(img.shape[:2]) / 600))
    fs = max(0.45, max(img.shape[:2]) / 1600)
    for (x1, y1, x2, y2), c, k in zip(boxes.astype(int), confs, clss):
        name = NAMES[int(k)]
        col = COLORS.get(name, (255, 255, 255))
        cv2.rectangle(out, (x1, y1), (x2, y2), col, t)
        txt = f"{name} {c:.2f}"
        (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, fs, 1)
        y0 = max(y1, th + 6)
        cv2.rectangle(out, (x1, y0 - th - 6), (x1 + tw + 6, y0), col, -1)
        cv2.putText(out, txt, (x1 + 3, y0 - 4), cv2.FONT_HERSHEY_SIMPLEX, fs, (11, 18, 32), 1, cv2.LINE_AA)
    return out


def _read_image(data):
    """Bytes -> BGR uint8. PIL first; OpenCV for what PIL can't open (e.g. 16-bit
    multi-band TIFF), stretched 2-98 % to 8-bit."""
    try:
        pil = Image.open(io.BytesIO(data))
        pil.draft("RGB", (MAX_SIDE, MAX_SIDE))
        return cv2.cvtColor(np.array(pil.convert("RGB")), cv2.COLOR_RGB2BGR)
    except Exception:
        pass
    arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if arr is None:
        return None
    if arr.ndim == 2:
        arr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    arr = arr[:, :, :3]
    if arr.dtype != np.uint8:
        a = arr.astype(np.float32)
        lo, hi = np.percentile(a[a > 0], (2, 98)) if (a > 0).any() else (0.0, 1.0)
        arr = np.clip((a - lo) * 255.0 / max(hi - lo, 1e-6), 0, 255).astype(np.uint8)
    return np.ascontiguousarray(arr)


# ─────────────────────────────────────────────────────────────
@router.get("/health")
def health():
    return {"status": "ok", "model": os.path.basename(WEIGHTS), "loaded": _model is not None,
            "classes": NAMES, "imgsz": IMGSZ, "device": DEVICE}


@router.post("/detect")
def detect(file: UploadFile = File(...), mode: str = Form("auto"), conf: float = Form(0.25)):
    data = file.file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, f"Image larger than {MAX_BYTES // 2**20} MB")
    img = _read_image(data)
    if img is None:
        raise HTTPException(400, "Could not read image (JPG, PNG, WEBP, BMP or TIFF expected)")
    h, w = img.shape[:2]
    if max(h, w) > MAX_SIDE:
        raise HTTPException(413, f"Image is {w}x{h}; the limit is {MAX_SIDE} px per side")

    t0 = time.time()
    with _lock:
        if mode.lower().startswith("auto"):
            boxes, confs, clss, used = _auto(img)
        else:
            used = float(min(max(conf, 0.05), 0.95))
            boxes, confs, clss = _detect(img, used)
    ms = int((time.time() - t0) * 1000)

    annotated = _draw(img, boxes, confs, clss)
    scale = min(1.0, 2048 / max(h, w))     # keep the returned preview light
    if scale < 1:
        annotated = cv2.resize(annotated, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, jpg = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 88])

    dets = [{
        "class_id": int(k), "class_name": NAMES[int(k)], "label": LABELS.get(NAMES[int(k)], NAMES[int(k)]),
        "is_pothole": NAMES[int(k)] in POTHOLE, "confidence": round(float(c), 4),
        "bbox_xyxy": [round(float(v), 1) for v in b],
    } for b, c, k in zip(boxes, confs, clss)]
    counts = {}
    for d in dets:
        counts[d["label"]] = counts.get(d["label"], 0) + 1

    return {
        "count": len(dets),
        "potholes": sum(d["is_pothole"] for d in dets),
        "counts": counts,
        "confidence_used": used,
        "mode": "auto" if mode.lower().startswith("auto") else "manual",
        "tiled": max(h, w) > TILE_ABOVE,
        "width": w, "height": h,
        "ms": ms,
        "detections": dets,
        "image": "data:image/jpeg;base64," + base64.b64encode(jpg.tobytes()).decode(),
    }
