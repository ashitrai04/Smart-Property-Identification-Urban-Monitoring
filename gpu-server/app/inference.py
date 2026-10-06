"""
SegFormer-B5 inference core (5-class: Background / Building / Road / Water / Open).
Adapted from the user's tested Guntur pipeline, generalised to accept ANY image
(jpg/png/webp/bmp/tif), CPU-friendly, with optional topological healing.
"""
import os
import io
import numpy as np
import cv2
import torch
import torch.nn as nn
import torch.nn.functional as F
from transformers import SegformerForSemanticSegmentation, SegformerConfig
from scipy import ndimage
from scipy.spatial import cKDTree
from skimage.measure import label, regionprops
from skimage.morphology import disk, skeletonize
import networkx as nx
from PIL import Image

# ───────── classes ─────────
CLASS_BACKGROUND, CLASS_BUILDING, CLASS_ROAD, CLASS_WATER, CLASS_OPEN = 0, 1, 2, 3, 4
NUM_CLASSES = 5
CLASS_NAMES = {0: "Background", 1: "Buildings", 2: "Roads", 3: "Water Bodies", 4: "Open / Barren"}

# RGB colors matching the webapp mask legend
CLASS_COLORS_RGB = {
    CLASS_BACKGROUND: (0, 0, 0),
    CLASS_BUILDING:   (239, 68, 68),    # red
    CLASS_ROAD:       (234, 179, 8),    # yellow
    CLASS_WATER:      (59, 130, 246),   # blue
    CLASS_OPEN:       (156, 163, 175),  # gray
}

CONF_THRESHOLD = {0: 0.30, 1: 0.55, 2: 0.45, 3: 0.45, 4: 0.40}

# ───────── settings (CPU-tuned; override via env) ─────────
# GPU server: run.sh pins CUDA_VISIBLE_DEVICES to the A100 with the most free
# memory; SEG_DEVICE=cpu forces CPU (e.g. when both GPUs are full).
DEVICE     = os.environ.get("SEG_DEVICE") or ("cuda" if torch.cuda.is_available() else "cpu")
SEG_BATCH  = int(os.environ.get("SEG_BATCH", "8" if DEVICE == "cuda" else "1"))
SEG_FP16   = os.environ.get("SEG_FP16", "1") == "1" and DEVICE == "cuda"
CHIP_SIZE  = 512
OVERLAP    = 128
MAX_DIM    = int(os.environ.get("SEG_MAX_DIM", "1536"))   # downscale longest side for CPU speed
USE_TTA    = os.environ.get("SEG_TTA", "0") == "1" and DEVICE == "cuda"
DO_HEAL    = os.environ.get("SEG_HEAL", "1") == "1"

_MODEL = None
LOAD_INFO = {"loaded": False}


def _build_config():
    # Explicit mit-b5 params → no network download needed at startup
    return SegformerConfig(
        num_labels=NUM_CLASSES,
        num_encoder_blocks=4,
        depths=[3, 6, 40, 3],
        sr_ratios=[8, 4, 2, 1],
        hidden_sizes=[64, 128, 320, 512],
        patch_sizes=[7, 3, 3, 3],
        strides=[4, 2, 2, 2],
        num_attention_heads=[1, 2, 5, 8],
        mlp_ratios=[4, 4, 4, 4],
        decoder_hidden_size=768,
    )


def load_model(weights_path):
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    model = SegformerForSemanticSegmentation(_build_config())
    ckpt = torch.load(weights_path, map_location=DEVICE, weights_only=False)
    sd = ckpt.get("model_state_dict", ckpt) if isinstance(ckpt, dict) else ckpt
    sd = {k.replace("module.", "").replace("backbone.", "").replace("model.", ""): v for k, v in sd.items()}
    miss, unexp = model.load_state_dict(sd, strict=False)
    LOAD_INFO.update({
        "loaded": len(miss) < 50,  # a clean load has ~0 missing; large => version mismatch
        "missing": len(miss),
        "unexpected": len(unexp),
        "ckpt_tensors": len(sd),
        "transformers_ok_hint": "pin transformers 4.x if missing is large",
    })
    print(f"[model] weights — missing={len(miss)} unexpected={len(unexp)} ckpt_tensors={len(sd)}")
    model.to(DEVICE).eval()
    _MODEL = model
    return model


# ───────── image IO ─────────
def read_image_any(data: bytes, filename: str = "") -> np.ndarray:
    """Return an RGB uint8 array (H, W, 3) from arbitrary image bytes."""
    name = (filename or "").lower()
    if name.endswith((".tif", ".tiff")):
        try:
            import tifffile
            arr = tifffile.imread(io.BytesIO(data))
            arr = np.asarray(arr)
            if arr.ndim == 2:
                arr = np.stack([arr] * 3, axis=-1)
            elif arr.ndim == 3:
                # could be (H,W,bands) or (bands,H,W)
                if arr.shape[0] <= 4 and arr.shape[0] < arr.shape[-1]:
                    arr = np.transpose(arr, (1, 2, 0))
                arr = arr[:, :, :3] if arr.shape[-1] >= 3 else np.repeat(arr[:, :, :1], 3, axis=-1)
            # scale to 0-255 if needed
            if arr.dtype != np.uint8:
                a = arr.astype(np.float32)
                lo, hi = np.percentile(a, 2), np.percentile(a, 98)
                a = np.clip((a - lo) / max(hi - lo, 1e-6), 0, 1) * 255.0
                arr = a.astype(np.uint8)
            return np.ascontiguousarray(arr[:, :, :3])
        except Exception:
            pass
    img = Image.open(io.BytesIO(data)).convert("RGB")
    return np.array(img)


# ───────── normalisation (per-chip CLAHE + percentile stretch) ─────────
def _normalise_chip(chip_rgb_u8: np.ndarray):
    out = chip_rgb_u8.astype(np.float32)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    for b in range(3):
        ch = out[:, :, b]
        p2, p98 = np.percentile(ch, 2), np.percentile(ch, 98)
        ch = np.clip((ch - p2) / max(p98 - p2, 1e-6), 0.0, 1.0)
        u8 = (ch * 255).astype(np.uint8)
        out[:, :, b] = clahe.apply(u8).astype(np.float32) / 255.0
    return out  # HWC float 0..1


_IMAGENET_MEAN = torch.tensor([0.485, 0.456, 0.406]).view(3, 1, 1)
_IMAGENET_STD = torch.tensor([0.229, 0.224, 0.225]).view(3, 1, 1)


def _to_tensor(normed_hwc: np.ndarray) -> torch.Tensor:
    t = torch.from_numpy(np.transpose(normed_hwc, (2, 0, 1)).copy()).float()
    return (t - _IMAGENET_MEAN) / _IMAGENET_STD  # ImageNet normalize (no torchvision)


@torch.no_grad()
def _infer(model, batch):
    with torch.autocast("cuda", dtype=torch.float16, enabled=SEG_FP16):
        return _infer_inner(model, batch)


def _infer_inner(model, batch):
    logits = model(pixel_values=batch).logits
    logits = F.interpolate(logits, size=batch.shape[-2:], mode="bilinear", align_corners=False)
    p0 = F.softmax(logits.float(), dim=1)
    if not USE_TTA:
        return p0.cpu().numpy()
    ph = torch.flip(F.softmax(F.interpolate(model(pixel_values=torch.flip(batch, [3])).logits, size=batch.shape[-2:], mode="bilinear", align_corners=False).float(), dim=1), [3])
    return ((p0 + ph) / 2.0).cpu().numpy()


# ───────── healing (from user's tested code, trimmed) ─────────
class _UF:
    def __init__(self, n): self.p = list(range(n)); self.r = [0]*n
    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]; x = self.p[x]
        return x
    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb: return False
        if self.r[ra] < self.r[rb]: ra, rb = rb, ra
        self.p[rb] = ra
        if self.r[ra] == self.r[rb]: self.r[ra] += 1
        return True
    def same(self, a, b): return self.find(a) == self.find(b)


def _remove_small_cc(binary, min_size):
    lbl = label(binary, connectivity=2)
    out = np.zeros_like(binary)
    for p in regionprops(lbl):
        if p.area >= min_size:
            out[lbl == p.label] = 1
    return out


def _skel_neighbors(skel, r, c, visited):
    H, W = skel.shape
    out = []
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0: continue
            nr, nc = r+dr, c+dc
            if 0 <= nr < H and 0 <= nc < W and skel[nr, nc] and (nr, nc) not in visited:
                out.append((nr, nc))
    return out


def _angle_diff(a, b):
    d = abs(a-b) % (2*np.pi)
    if d > np.pi: d = 2*np.pi - d
    if d > np.pi/2: d = np.pi - d
    return float(d)


def heal_roads(road_bin, close_px=2, width_px=2, max_gap=120, angle_tol_deg=45.0, walk=12, min_area=120):
    road = _remove_small_cc(road_bin, min_area)
    road = ndimage.binary_closing(road, structure=disk(close_px)).astype(np.uint8)
    skel = skeletonize(road.astype(bool))
    su8 = skel.astype(np.uint8); k = np.ones((3, 3), np.uint8); k[1, 1] = 0
    nc = cv2.filter2D(su8, -1, k)
    rows, cols = np.where((su8 == 1) & (nc == 1))
    if len(rows) < 2:
        return ndimage.binary_dilation(road.astype(bool), structure=disk(width_px)).astype(np.uint8)
    eps, angs = [], []
    for r0, c0 in zip(rows, cols):
        visited = {(r0, c0)}; r, c = r0, c0
        for _ in range(walk):
            nbrs = _skel_neighbors(skel, r, c, visited)
            if not nbrs: break
            r, c = nbrs[0]; visited.add((r, c))
        eps.append((r0, c0)); angs.append(float(np.arctan2(r-r0, c-c0)))
    pts = np.array(eps, np.float32); angs = np.array(angs, np.float32)
    pairs = cKDTree(pts).query_pairs(r=float(max_gap))
    comp = label(skel, connectivity=2)
    ep_comp = np.array([comp[r, c] for r, c in eps], np.int32)
    dsu = _UF(int(comp.max())+1); G = nx.Graph(); G.add_nodes_from(range(len(pts)))
    tol = np.deg2rad(angle_tol_deg)
    for i, j in pairs:
        dist = float(np.linalg.norm(pts[i]-pts[j]))
        if dist < 1.0 or dsu.same(int(ep_comp[i]), int(ep_comp[j])): continue
        conn = float(np.arctan2(pts[j][0]-pts[i][0], pts[j][1]-pts[i][1]))
        if _angle_diff(angs[i], conn) < tol and _angle_diff(angs[j], conn+np.pi) < tol:
            G.add_edge(i, j, weight=dist)
    if G.number_of_edges() > 0:
        for i, j in nx.minimum_spanning_tree(G, weight="weight").edges():
            ci, cj = int(ep_comp[i]), int(ep_comp[j])
            if ci == 0 or cj == 0 or dsu.same(ci, cj): continue
            cv2.line(road, (int(pts[i][1]), int(pts[i][0])), (int(pts[j][1]), int(pts[j][0])), 1, thickness=width_px)
            dsu.union(ci, cj)
    return ndimage.binary_dilation(road.astype(bool), structure=disk(width_px)).astype(np.uint8)


def _heal(pred, avg_prob):
    # buildings: drop tiny blobs
    b = _remove_small_cc((pred == CLASS_BUILDING).astype(np.uint8), 55)
    pred[(pred == CLASS_BUILDING) & (b == 0)] = CLASS_BACKGROUND
    # roads: skeleton bridge
    road_healed = heal_roads((pred == CLASS_ROAD).astype(np.uint8))
    high_conf_bldg = (avg_prob[CLASS_BUILDING] > 0.40) & (pred == CLASS_BUILDING)
    pred[(road_healed == 1) & ~high_conf_bldg] = CLASS_ROAD
    return pred


# ───────── main entry ─────────
def segment(model, rgb_u8: np.ndarray) -> np.ndarray:
    """Return a (H, W) int class map for an RGB uint8 image."""
    H0, W0 = rgb_u8.shape[:2]
    scale = 1.0
    if max(H0, W0) > MAX_DIM:
        scale = MAX_DIM / max(H0, W0)
        rgb = cv2.resize(rgb_u8, (int(W0*scale), int(H0*scale)), interpolation=cv2.INTER_AREA)
    else:
        rgb = rgb_u8
    H, W = rgb.shape[:2]

    stride = CHIP_SIZE - OVERLAP
    prob_sum = np.zeros((NUM_CLASSES, H, W), np.float32)
    cnt = np.zeros((H, W), np.float32)

    ys = list(range(0, max(1, H), stride))
    xs = list(range(0, max(1, W), stride))
    jobs = []
    for y in ys:
        for x in xs:
            ch = min(CHIP_SIZE, H - y); cw = min(CHIP_SIZE, W - x)
            if ch <= 0 or cw <= 0: continue
            jobs.append((y, x, ch, cw))

    # Chips go through the model SEG_BATCH at a time. On a shared GPU the free
    # memory moves under us, so an OOM halves the batch and retries the chunk.
    batch = max(1, SEG_BATCH)
    i = 0
    while i < len(jobs):
        chunk = jobs[i:i + batch]
        tens = []
        for (y, x, ch, cw) in chunk:
            chip = np.zeros((CHIP_SIZE, CHIP_SIZE, 3), np.uint8)
            chip[:ch, :cw] = rgb[y:y+ch, x:x+cw]
            tens.append(_to_tensor(_normalise_chip(chip)))
        try:
            probs = _infer(model, torch.stack(tens).to(DEVICE))
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            if batch == 1:
                raise
            batch = max(1, batch // 2)
            print(f"[segment] GPU memory short — batch -> {batch}", flush=True)
            continue
        for k, (y, x, ch, cw) in enumerate(chunk):
            prob_sum[:, y:y+ch, x:x+cw] += probs[k, :, :ch, :cw]
            cnt[y:y+ch, x:x+cw] += 1.0
        i += len(chunk)

    avg = prob_sum / np.maximum(cnt[None], 1e-6)
    maxp = avg.max(0)
    pred = avg.argmax(0).astype(np.int32)
    for cls, th in CONF_THRESHOLD.items():
        pred[(pred == cls) & (maxp < th)] = CLASS_BACKGROUND
    if DO_HEAL:
        try:
            pred = _heal(pred, avg)
        except Exception as e:
            print("[heal] skipped:", repr(e)[:120])

    if scale != 1.0:
        pred = cv2.resize(pred.astype(np.uint8), (W0, H0), interpolation=cv2.INTER_NEAREST).astype(np.int32)
    return pred


def colorize(pred: np.ndarray) -> np.ndarray:
    """class map → RGB uint8."""
    H, W = pred.shape
    out = np.zeros((H, W, 3), np.uint8)
    for c, col in CLASS_COLORS_RGB.items():
        out[pred == c] = col
    return out


def overlay(rgb_u8: np.ndarray, pred: np.ndarray, alpha=0.5) -> np.ndarray:
    color = colorize(pred)
    mask = (pred > 0)[:, :, None]
    blended = (rgb_u8.astype(np.float32) * (1 - alpha) + color.astype(np.float32) * alpha).astype(np.uint8)
    return np.where(mask, blended, rgb_u8)


def class_stats(pred: np.ndarray) -> dict:
    total = int(pred.size)
    out = {}
    for c, name in CLASS_NAMES.items():
        n = int((pred == c).sum())
        out[name] = {"pixels": n, "percent": round(n / max(total, 1) * 100, 2)}
    return out


def png_b64(rgb_u8: np.ndarray) -> str:
    import base64
    ok, buf = cv2.imencode(".png", cv2.cvtColor(rgb_u8, cv2.COLOR_RGB2BGR))
    return base64.b64encode(buf.tobytes()).decode("ascii")


def png_b64_gray(ids: np.ndarray) -> str:
    """Single-channel PNG of raw class ids (lossless, tiny) — used for client-side
    vector/GeoTIFF exports."""
    import base64
    ok, buf = cv2.imencode(".png", ids.astype(np.uint8))
    return base64.b64encode(buf.tobytes()).decode("ascii")
