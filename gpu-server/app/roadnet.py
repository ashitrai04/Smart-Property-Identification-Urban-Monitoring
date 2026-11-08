"""Road extraction — DINOv3 ViT-L/16 (satellite-pretrained) with a UPerNet head.

Sits beside buildings.py and follows the same shape, so everything that knows
how to drive HiSup already knows how to drive this: lazy load, a /health that
says whether the weights are really in memory, a /reload that is a file copy
plus one POST, and an entry in AREA_TASKS so the map can run it over a drawn
polygon.

The architecture is in roadnet_arch.py, reconstructed from the checkpoint and
loaded with every one of its 439 tensors accounted for. See that file.

Two things about this model differ from HiSup and matter:

  * Normalization is NOT ImageNet. The backbone is the `sat493m` DINOv3
    variant, pretrained on satellite imagery, and timm ships it with
    mean (0.430, 0.411, 0.296) / std (0.213, 0.156, 0.143). Using ImageNet
    statistics here measurably lowers the score, in the same way it does for
    HiSup in the other direction.
  * The output is one channel, so it is road / not-road through a sigmoid -
    there is no softmax and no class axis.

Output goes through roadreg.py rather than regularize.py: a carriageway is a
corridor, and the right-angle regularizer that suits buildings would wreck it.
"""
import io
import os
import sys
import threading
import time

import cv2
import numpy as np
import torch
from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

router = APIRouter(prefix="/roadnet", tags=["roadnet"])

WEIGHTS = os.environ.get("ROADNET_WEIGHTS",
                         os.path.join(HERE, "weights", "roadnet_dinov3.pth"))
DEVICE = os.environ.get("SEG_DEVICE") or ("cuda" if torch.cuda.is_available() else "cpu")
FP16 = os.environ.get("ROADNET_FP16", "1") == "1" and DEVICE.startswith("cuda")

# The satellite-pretrained DINOv3 statistics, not ImageNet. Overridable because
# a future checkpoint may be fine-tuned under different ones.
MEAN = np.array([float(x) for x in os.environ.get(
    "ROADNET_MEAN", "0.430,0.411,0.296").split(",")], np.float32)
STD = np.array([float(x) for x in os.environ.get(
    "ROADNET_STD", "0.213,0.156,0.143").split(",")], np.float32)

# Working ground resolution. Swept against the project's own road-surface
# layer on the Ongole orthomosaic, 14 windows per point:
#
#     10 cm  F1 0.604   (precision 0.79, recall 0.49 - misses narrow lanes)
#     20 cm  F1 0.653   <- here
#     30 cm  F1 0.630
#     50 cm  F1 0.632
#    100 cm  F1 0.496
#
# The source is 3 cm, so this downsamples, which is also what makes a
# city-sized area affordable. Raise it for speed on a large AOI; 10 cm is the
# one to pick when precision matters more than finding every lane.
WORK_GSD = float(os.environ.get("ROADNET_GSD", "0.20"))
# 0.45, not 0.50. Swept against centre_part_Rd_v1: the curve is flat from
# 0.35 to 0.55 and peaks at 0.45 (F1 0.790 against 0.789 at 0.50), buying a
# little recall for a little precision. Raise it when false road on open
# ground matters more than finding every lane.
THRESH = float(os.environ.get("ROADNET_THRESH", "0.45"))
# Multiple of 64: the ViT needs a multiple of its 16 px patch, and the head
# pools the deepest tap by 4, so the token grid must divide by 4 as well.
TILE = int(os.environ.get("ROADNET_TILE", "512"))
OVERLAP = int(os.environ.get("ROADNET_OVERLAP", "64"))
MAX_SIDE = int(os.environ.get("ROADNET_MAX_SIDE", "8192"))

# The checkpoint is stored fp16: 316.2 M parameters = 632 MB. Well under that
# and the file is incomplete rather than merely a different build.
MIN_BYTES = 550_000_000

_model = None
_lock = threading.Lock()
LOAD_INFO = {"loaded": False, "weights": WEIGHTS, "device": DEVICE}


def _load():
    global _model
    if _model is not None:
        return _model
    with _lock:
        if _model is not None:
            return _model
        if not os.path.exists(WEIGHTS):
            raise HTTPException(503, f"road model weights not found at {WEIGHTS}")
        size = os.path.getsize(WEIGHTS)
        if size < MIN_BYTES:
            raise HTTPException(503,
                f"road model weights at {WEIGHTS} are {size/1e6:.0f} MB; the "
                f"checkpoint is about 632 MB, so this copy is incomplete. "
                f"Delete it and let the sync pull it again.")
        print(f"[roadnet] loading {WEIGHTS} on {DEVICE}…", flush=True)
        t0 = time.time()
        try:
            ck = torch.load(WEIGHTS, map_location="cpu", weights_only=True)
        except Exception as e:
            raise HTTPException(503,
                f"road model weights at {WEIGHTS} could not be read "
                f"({size/1e6:.0f} MB): {type(e).__name__}: {str(e)[:200]}. If "
                f"this says the archive is corrupt the file arrived damaged - "
                f"delete it and let the sync pull it again.") from e

        sd = ck.get("model", ck.get("state_dict", ck)) if isinstance(ck, dict) else ck
        meta = {k: ck[k] for k in ("epoch", "val_f1", "timm_id")
                if isinstance(ck, dict) and k in ck}

        # Same treatment as HiSup's yacs: timm supplies the DINOv3 encoder and
        # is imported inside the architecture module, so without it the failure
        # arrives as a bare ModuleNotFoundError with no hint of the remedy.
        try:
            import roadnet_arch
            m, miss, unexp = roadnet_arch.verify(sd)
        except ModuleNotFoundError as e:
            raise HTTPException(503,
                f"the road model needs the '{e.name}' package, which is not "
                f"installed in the environment this server is running under "
                f"({sys.executable}). Run SETUP SERVER.bat once on this machine, "
                f"or: pip install {e.name}") from e
        # Same stance as HiSup: a partial load predicts confident nonsense, so
        # it is an error, not a warning. The architecture here is reconstructed
        # from the weights, which makes this check the thing that would catch a
        # checkpoint from a different model generation.
        if miss or unexp:
            _model = None
            raise HTTPException(503,
                f"road checkpoint does not match this architecture: "
                f"{len(miss)} tensors missing, {len(unexp)} unexpected "
                f"(e.g. missing={miss[:3]}, unexpected={unexp[:3]}). Do not "
                f"trust predictions from this pair.")
        m = m.to(DEVICE).eval()
        if FP16:
            m = m.half()
        _model = m
        LOAD_INFO.update(loaded=True, seconds=round(time.time() - t0, 1),
                         tensors=len(sd), fp16=FP16,
                         mtime=os.path.getmtime(WEIGHTS), **meta)
        print(f"[roadnet] ready — {len(sd)} tensors in {LOAD_INFO['seconds']}s"
              f"{' (fp16)' if FP16 else ''}", flush=True)
        return _model


def _norm(rgb):
    x = rgb.astype(np.float32) / 255.0
    x = (x - MEAN) / STD
    return np.ascontiguousarray(x.transpose(2, 0, 1))


def probability(rgb):
    """Road probability for an RGB array, tiled with a feathered blend.

    Tiles are blended with a cosine window rather than averaged on a hard
    seam: a hard seam leaves a faint grid in the probability field, and after
    thresholding that grid becomes real notches in the road edge - exactly the
    blobbiness this model is meant to avoid.
    """
    m = _load()
    h, w = rgb.shape[:2]
    if max(h, w) > MAX_SIDE:
        raise HTTPException(413, f"image is {w}x{h}; the limit is {MAX_SIDE} px "
                                 f"a side for a single request - use /area for "
                                 f"anything larger")
    pad_h = (-h) % 64 or 0
    pad_w = (-w) % 64 or 0
    if pad_h or pad_w:
        rgb = cv2.copyMakeBorder(rgb, 0, pad_h, 0, pad_w, cv2.BORDER_REFLECT_101)
    H, W = rgb.shape[:2]

    acc = np.zeros((H, W), np.float32)
    wsum = np.zeros((H, W), np.float32)
    tile = min(TILE, max(64, (min(H, W) // 64) * 64 or 64))
    step = max(64, tile - OVERLAP)

    # Separable cosine taper, 1 in the middle and 0 at the edge of a tile.
    def taper(n):
        if n <= 1:
            return np.ones(n, np.float32)
        t = np.hanning(n + 2)[1:-1].astype(np.float32)
        return np.maximum(t, 1e-3)

    win = np.outer(taper(tile), taper(tile))

    ys = list(range(0, max(1, H - tile + 1), step))
    xs = list(range(0, max(1, W - tile + 1), step))
    if ys[-1] + tile < H:
        ys.append(H - tile)
    if xs[-1] + tile < W:
        xs.append(W - tile)

    dt = torch.float16 if FP16 else torch.float32
    with torch.no_grad():
        for y in ys:
            for x in xs:
                patch = rgb[y:y + tile, x:x + tile]
                t = torch.from_numpy(_norm(patch))[None].to(DEVICE, dtype=dt)
                p = torch.sigmoid(m(t))[0, 0].float().cpu().numpy()
                acc[y:y + tile, x:x + tile] += p * win
                wsum[y:y + tile, x:x + tile] += win
    prob = acc / np.maximum(wsum, 1e-6)
    return prob[:h, :w]


# ───────────────────────────── endpoints ─────────────────────────────
@router.get("/health")
def health():
    return {
        "status": "ok",
        "model": "DINOv3 ViT-L/16 (sat493m) + UPerNet, binary road segmentation",
        "task": "road surface extraction",
        "weights": os.path.basename(WEIGHTS),
        "weights_present": os.path.exists(WEIGHTS),
        "device": DEVICE,
        "load": LOAD_INFO,
        "defaults": {"work_gsd_m": WORK_GSD, "threshold": THRESH, "tile": TILE,
                     "overlap": OVERLAP, "fp16": FP16,
                     "mean": MEAN.tolist(), "std": STD.tolist()},
        "regularize": {"modes": ["smooth", "corridor", "none"], "default": "smooth",
                       "note": "smooth follows the model's own outline and scores "
                               "F1 0.786 against the test labels; corridor rebuilds "
                               "the carriageway from a smoothed centreline, which "
                               "reads cleaner but measures 0.783. Centrelines come "
                               "back either way."},
    }


@router.post("/reload")
def reload_weights():
    """Drop the loaded model so the next request picks up a new file."""
    global _model
    with _lock:
        _model = None
        LOAD_INFO.update(loaded=False)
    present = os.path.exists(WEIGHTS)
    return {"reloaded": True, "weights": WEIGHTS, "present": present,
            "note": "the next request loads it"}


@router.post("/predict")
async def predict(
    file: UploadFile = File(...),
    gsd: float = Form(0.0),
    threshold: float = Form(0.0),
    regularize: str = Form("smooth"),
    want_centrelines: bool = Form(True),
):
    """Extract roads from an uploaded image.

    `gsd` is metres per pixel. A GeoTIFF states its own; for anything else pass
    it, because this model is resolution-sensitive and guessing wrong is the
    difference between a road and a smear. The image is resampled to the
    model's working resolution and the result is returned in the pixel
    coordinates of the upload.
    """
    import buildings as B        # reuse its decoder: GeoTIFF-aware, same rules

    raw = await file.read()
    img, geo_gsd, geo = B._decode(raw, file.filename or "")
    src_gsd = gsd if gsd > 0 else (geo_gsd or 0.0)
    if src_gsd <= 0:
        raise HTTPException(400, "this model needs a ground sample distance: "
                                 "send gsd=<metres per pixel>, or upload a "
                                 "GeoTIFF that carries one")
    thr = threshold if threshold > 0 else THRESH

    f = src_gsd / WORK_GSD                     # <1 downsamples to the work grid
    t0 = time.time()
    if abs(f - 1.0) < 0.02:
        work = img
    else:
        work = cv2.resize(img, (max(64, int(round(img.shape[1] * f))),
                                max(64, int(round(img.shape[0] * f)))),
                          interpolation=(cv2.INTER_AREA if f < 1 else cv2.INTER_CUBIC))
    prob = probability(work)
    mask = prob >= thr

    import roadreg
    polys, lines, how = ([], [], "none")
    if regularize != "none":
        polys, lines, how = roadreg.regularize_roads(mask, WORK_GSD, mode=regularize)
    else:
        polys = roadreg.smooth_polygons(mask.astype(np.uint8), WORK_GSD,
                                        simplify_m=0.0, chaikin=0, nub_m=0.0,
                                        min_area_m2=0.0)
        how = "none"

    # Back to the upload's pixel grid.
    inv = 1.0 / f if f else 1.0

    def scale_coords(seq):
        a = np.asarray(seq, float) * inv
        return [[round(float(u), 2), round(float(v), 2)] for u, v in a]

    feats = []
    for g in polys:
        if g.is_empty or g.geom_type != "Polygon":
            continue
        rings = [scale_coords(g.exterior.coords)]
        rings += [scale_coords(r.coords) for r in g.interiors]
        feats.append({
            "type": "Feature",
            "properties": {"area_m2": round(g.area * WORK_GSD * WORK_GSD, 2)},
            "geometry": {"type": "Polygon", "coordinates": rings},
        })
    cl = []
    if want_centrelines:
        for ln in lines:
            cl.append({
                "type": "Feature",
                "properties": {"length_m": round(ln.length * WORK_GSD, 2)},
                "geometry": {"type": "LineString", "coordinates": scale_coords(ln.coords)},
            })

    area_m2 = sum(f_["properties"]["area_m2"] for f_ in feats)
    return {
        "ok": True,
        "model": "roadnet",
        "count": len(feats),
        "seconds": round(time.time() - t0, 1),
        "image": {"width": int(img.shape[1]), "height": int(img.shape[0]),
                  "gsd": src_gsd, "work_gsd": WORK_GSD},
        "settings": {"threshold": thr, "regularize": regularize, "applied": how},
        "road_area_m2": round(area_m2, 1),
        "road_cover_pct": round(100.0 * float(mask.mean()), 2),
        "geojson": {"type": "FeatureCollection", "features": feats},
        "centrelines": {"type": "FeatureCollection", "features": cl},
    }
