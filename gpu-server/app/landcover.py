"""Land-cover classification — frozen DINOv3 ViT-L/16 (sat493m) + UPerNet, 9 classes.

Sits beside roadnet.py and buildings.py and follows the same shape, so anything
that already knows how to drive those knows how to drive this: lazy load, a
/health that says whether the weights are really in memory, a /reload that is a
file copy plus one POST, and an entry in AREA_TASKS so the map can run it over a
drawn polygon.

The architecture is in landcover_arch.py, reconstructed from the checkpoint and
loaded with all 403 tensors accounted for. See that file for the tap and
normalization choices, both of which were measured.

Unlike roadnet (one channel, sigmoid) this is a 9-way softmax, so the output is
an argmax class map. A whole-AOI run writes it as `classes.tif` — a georeferenced
uint8 raster carrying its own colour table — which the existing
/results/{job}/classes/{z}/{x}/{y}.png endpoint already serves as map tiles.

PALETTE is the OpenEarthMap reference palette, byte for byte — the colours the
dataset itself ships and that every published figure from it uses. That is a
deliberate choice over a prettier one: this is a standard land-cover product,
and a reader who knows OpenEarthMap should recognise the map without consulting
a key. White roads, grey developed ground, maroon bareland and three different
greens are not accidents of taste, they are the convention.

It is worth knowing what that costs, because it is not zero. Run these nine
through the OKLab colour-difference checks and several pairs fail: the three
greens (rangeland, agriculture, tree) sit close together, and white road
against grey developed is a lightness difference rather than a hue one. Under
red-green colour blindness the greens are hard to separate. The legend
therefore has to carry its weight - it is served from here, with names, so
identity never rests on colour alone. Do not treat this palette as validated;
treat it as standard, which is a different and in this case better reason.
"""
import base64
import io
import os
import sys
import threading
import time

import cv2
import numpy as np
import torch
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import landcover_arch as arch  # noqa: E402

router = APIRouter(prefix="/landcover", tags=["landcover"])

CLASSES = arch.CLASSES

# class -> (hex, alpha) for the map overlay. Alpha 0 on "unknown" so the model
# declining to answer leaves the imagery visible instead of painting over it.
#
# Alpha is 255 on every class that is an answer. It used to be ~170, which
# multiplied with the map's own opacity slider: at the slider's default the
# colours reached the eye at about 37 % strength and the whole overlay read as
# muddy pastel no matter how saturated the hex values were. Transparency is
# the map's job and it already has a control for it, so the tiles ship at full
# strength and the slider means what it says. "unknown" stays at 0 — the model
# declining to answer should leave the imagery alone, not tint it.
PALETTE_HEX = {
    "unknown":     ("#000000", 0),
    "bareland":    ("#800000", 255),
    "rangeland":   ("#00FF24", 255),
    "developed":   ("#949494", 255),
    "road":        ("#FFFFFF", 255),
    "tree":        ("#226126", 255),
    "water":       ("#0045FF", 255),
    "agriculture": ("#4BB549", 255),
    "building":    ("#DE1F07", 255),
}
# Human-readable notes for the legend, so the webapp does not have to carry its
# own copy of what these OpenEarthMap classes actually mean.
NOTES = {
    "unknown":     "model declined — drawn transparent",
    "bareland":    "bare soil, sand, quarry",
    "rangeland":   "grass, scrub, open vegetation",
    "developed":   "built-up ground that is not a roof or a carriageway",
    "road":        "carriageway and paved track",
    "tree":        "tree canopy and plantation",
    "water":       "river, tank, pond",
    "agriculture": "cropland and paddy",
    "building":    "roofs",
}


def _hex2rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


# RGB for drawing an overlay, RGBA for the map tile colour table.
PALETTE = np.array([_hex2rgb(PALETTE_HEX[c][0]) for c in CLASSES], np.uint8)
COLORMAP = {i: (*_hex2rgb(PALETTE_HEX[c][0]), PALETTE_HEX[c][1])
            for i, c in enumerate(CLASSES)}


def _find_weights():
    """Locate the land-cover checkpoint, wherever it was dropped.

    Same rule as roadnet._find_param_weights: take the LARGEST .pth under the
    folder, because a half-finished copy can sit beside the real one looking
    exactly like it, and a truncated file is never the largest.
    """
    root = os.path.join(HERE, "weights", "landcover")
    best, size = os.path.join(root, "best.pth"), -1
    if os.path.isdir(root):
        for dirpath, _, files in os.walk(root):
            for f in files:
                if f.endswith((".pth", ".pt")):
                    q = os.path.join(dirpath, f)
                    try:
                        n = os.path.getsize(q)
                    except OSError:
                        continue
                    if n > size:
                        best, size = q, n
    return best


WEIGHTS = os.environ.get("LANDCOVER_WEIGHTS") or _find_weights()
DEVICE = os.environ.get("SEG_DEVICE") or ("cuda" if torch.cuda.is_available() else "cpu")
FP16 = os.environ.get("LANDCOVER_FP16", "1") == "1" and DEVICE.startswith("cuda")

MEAN = np.array([float(x) for x in os.environ.get(
    "LANDCOVER_MEAN", ",".join(str(v) for v in arch.MEAN)).split(",")], np.float32)
STD = np.array([float(x) for x in os.environ.get(
    "LANDCOVER_STD", ",".join(str(v) for v in arch.STD)).split(",")], np.float32)

# Working ground resolution. 0.20 m is the one configuration with a measured
# number behind it: building IoU 0.768 against 2,146 hand-drawn polygons over
# 1 km^2 of Ongole. The same weights on 0.279 m satellite of the same ground
# returned essentially the same class balance (building 41.5 % vs 40.2 %), so
# the model is not fragile across that range — but 0.20 m is what was scored,
# so it is the default.
WORK_GSD = float(os.environ.get("LANDCOVER_GSD", "0.20"))
# 512 with a 256 stride: every interior pixel is covered by four windows whose
# softmax outputs are averaged, which is what keeps class boundaries from
# snapping to the tile grid.
WIN = int(os.environ.get("LANDCOVER_WINDOW", "512"))
STRIDE = int(os.environ.get("LANDCOVER_STRIDE", "256"))
MAX_SIDE = int(os.environ.get("LANDCOVER_MAX_SIDE", "8192"))
# ViT-L/16 at fp32 is ~1.2 GB. Well under that and the file is truncated
# rather than merely a different build.
MIN_BYTES = 900_000_000

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
            raise HTTPException(503, f"land-cover weights not found at {WEIGHTS}")
        size = os.path.getsize(WEIGHTS)
        if size < MIN_BYTES:
            raise HTTPException(503,
                f"land-cover weights at {WEIGHTS} are {size/1e6:.0f} MB, below the "
                f"{MIN_BYTES/1e6:.0f} MB floor for this checkpoint, so this copy is "
                f"incomplete. Delete it and copy it again.")
        print(f"[landcover] loading {WEIGHTS} on {DEVICE}…", flush=True)
        t0 = time.time()
        try:
            ck = torch.load(WEIGHTS, map_location="cpu", weights_only=True)
        except Exception as e:
            raise HTTPException(503,
                f"land-cover weights at {WEIGHTS} could not be read "
                f"({size/1e6:.0f} MB): {type(e).__name__}: {str(e)[:200]}. If this "
                f"says the archive is corrupt the file arrived damaged — copy it "
                f"again.") from e

        sd = ck.get("model", ck.get("state_dict", ck)) if isinstance(ck, dict) else ck
        meta = {k: ck[k] for k in ("epoch", "val_miou", "val_iou", "timm_id")
                if isinstance(ck, dict) and k in ck and not hasattr(ck[k], "shape")}
        try:
            m, miss, unexp = arch.build(sd)
        except ModuleNotFoundError as e:
            raise HTTPException(503,
                f"the land-cover model needs the '{e.name}' package, which is not "
                f"installed in the interpreter this server runs under "
                f"({sys.executable}). Run SETUP SERVER.bat once on this machine, "
                f"or: pip install {e.name}") from e
        # Same stance as HiSup and roadnet: a partial load predicts confident
        # nonsense, so it is an error rather than a warning. This architecture
        # was reconstructed from the weights, which makes this check the thing
        # that would catch a checkpoint from a different model generation.
        if miss or unexp:
            raise HTTPException(503,
                f"land-cover checkpoint does not match this architecture: "
                f"{len(miss)} tensors missing, {len(unexp)} unexpected "
                f"(e.g. missing={miss[:3]}, unexpected={unexp[:3]}). Do not trust "
                f"predictions from this pair.")
        m = m.to(DEVICE).eval()
        if FP16:
            m = m.half()
        _model = m
        LOAD_INFO.update(loaded=True, weights=WEIGHTS, device=DEVICE, fp16=FP16,
                         seconds=round(time.time() - t0, 1), tensors=len(sd),
                         taps=list(arch.TAPS), norm_taps=arch.NORM_TAPS,
                         mtime=os.path.getmtime(WEIGHTS), **meta)
        print(f"[landcover] ready — {len(sd)} tensors in {LOAD_INFO['seconds']}s"
              f"{' (fp16)' if FP16 else ''}", flush=True)
        return _model


def _norm(rgb):
    x = rgb.astype(np.float32) / 255.0
    x = (x - MEAN) / STD
    return np.ascontiguousarray(x.transpose(2, 0, 1))


def probabilities(rgb):
    """Per-class softmax for an RGB array, averaged over overlapping windows.

    Returns float32 [9, H, W]. Accumulated in float32 rather than float16: a
    1 km^2 AOI is ~5000x5000, and summing four fp16 softmaxes per pixel over
    that many pixels loses enough precision to flip marginal pixels between
    classes.

    Tiles are blended with a cosine taper, not averaged with equal weight.
    Equal weighting looks like it should be fine - every interior pixel is
    covered by the same four windows - but the SET of four changes every time
    you cross a stride boundary, and this head's PPM branch global-average-
    pools each window, so the four opinions being averaged shift slightly at
    that line. Measured on a 3328 px AOI, equal weighting left class-change
    spikes of 3.3x to 5.6x the local median at exactly the multiples of the
    stride; those are the faint ruled lines visible across the overlay. The
    taper sends a tile's weight to nearly zero at its own edge, so the
    handover is gradual and there is no line to see.
    """
    m = _load()
    h, w = rgb.shape[:2]
    if max(h, w) > MAX_SIDE:
        raise HTTPException(413, f"image is {w}x{h}; the limit is {MAX_SIDE} px a side "
                                 f"for a single request — use /area for anything larger")
    # The ViT needs a multiple of its 16 px patch; the head pools the deepest
    # tap by 2 and the FPN rescales by 4, so round up to 64.
    pad_h, pad_w = (-h) % 64, (-w) % 64
    if pad_h or pad_w:
        rgb = cv2.copyMakeBorder(rgb, 0, pad_h, 0, pad_w, cv2.BORDER_REFLECT_101)
    H, W = rgb.shape[:2]
    x = _norm(rgb)

    n = len(CLASSES)
    acc = np.zeros((n, H, W), np.float32)
    cnt = np.zeros((1, H, W), np.float32)
    win = min(WIN, max(64, (min(H, W) // 64) * 64 or 64))
    step = max(64, min(STRIDE, win))

    ys = list(range(0, max(H - win, 0) + 1, step)) or [0]
    xs = list(range(0, max(W - win, 0) + 1, step)) or [0]
    if ys[-1] + win < H:
        ys.append(H - win)
    if xs[-1] + win < W:
        xs.append(W - win)

    # Separable Hann taper: 1 in the middle of a tile, ~0 at its edge. Floored
    # just above zero so a pixel covered by exactly one tile (the corners of
    # the padded frame) still gets that tile's answer instead of a divide by
    # nothing.
    def taper(k):
        if k <= 1:
            return np.ones(k, np.float32)
        return np.maximum(np.hanning(k + 2)[1:-1].astype(np.float32), 1e-3)

    wgt = np.outer(taper(win), taper(win))[None]

    dt = torch.float16 if FP16 else torch.float32
    with torch.no_grad():
        for yy in ys:
            for xx in xs:
                t = torch.from_numpy(x[:, yy:yy + win, xx:xx + win])[None].to(DEVICE, dtype=dt)
                o = m(t).float().softmax(1)[0].cpu().numpy()
                acc[:, yy:yy + win, xx:xx + win] += o * wgt
                cnt[:, yy:yy + win, xx:xx + win] += wgt
    acc /= np.maximum(cnt, 1e-6)
    return acc[:, :h, :w]


def classify(rgb):
    """Argmax class map, uint8 [H, W], values indexing CLASSES."""
    return probabilities(rgb).argmax(0).astype(np.uint8)


def overlay(rgb, lab, alpha=0.45):
    """Blend the class map over the image, leaving 'unknown' untouched.

    `unknown` is index 0 and its palette entry is black; blending it would
    darken exactly the pixels the model was least sure about, which reads as
    a shadow and is the opposite of informative. So those pixels keep the
    original imagery.
    """
    out = rgb.astype(np.float32).copy()
    col = PALETTE[lab].astype(np.float32)
    m = (lab != 0)[..., None]
    blended = (1.0 - alpha) * out + alpha * col
    return np.where(m, blended, out).astype(np.uint8)


def legend(counts=None, total=None, px_area_m2=None):
    """The legend, optionally with per-class share. One source of truth.

    The webapp builds its key from this rather than carrying its own copy of
    the colours — a legend that disagrees with the pixels is worse than none.

    Shares are of the CLASSIFIED pixels, classes 1-8. Index 0 is "unknown",
    which on a whole-image run is the model declining and on an area run is
    also everything outside the drawn polygon: including it in the denominator
    would make every class's share depend on how square the drawn shape
    happened to be. Unknown still reports its pixel count, but no percent,
    because a share of the thing excluded from the denominator is meaningless.
    """
    out = []
    denom = total
    if denom is None and counts is not None:
        denom = int(np.sum(np.asarray(counts)[1:]))
    for i, c in enumerate(CLASSES):
        hexv, a = PALETTE_HEX[c]
        row = {"id": i, "name": c, "label": c.replace("_", " ").title(),
               "color": hexv, "alpha": a, "note": NOTES[c]}
        if counts is not None:
            n = int(counts[i]) if i < len(counts) else 0
            row["pixels"] = n
            row["percent"] = None if i == 0 else round(100.0 * n / max(1, denom), 2)
            if px_area_m2:
                row["area_m2"] = round(n * px_area_m2, 1)
                row["area_ha"] = round(n * px_area_m2 / 10000.0, 3)
        out.append(row)
    return out


# ───────────────────────────── endpoints ─────────────────────────────
@router.get("/health")
def health():
    present = os.path.exists(WEIGHTS)
    return {
        "status": "ok",
        "model": "DINOv3 ViT-L/16 (sat493m, frozen) + UPerNet, 9-class land cover",
        "task": "land-cover / land-use classification",
        "classes": CLASSES,
        "weights": os.path.basename(WEIGHTS),
        "weights_present": present,
        "size_mb": round(os.path.getsize(WEIGHTS) / 1e6) if present else 0,
        "device": DEVICE,
        "load": LOAD_INFO,
        "defaults": {"work_gsd_m": WORK_GSD, "window": WIN, "stride": STRIDE,
                     "fp16": FP16, "taps": list(arch.TAPS),
                     "norm_taps": arch.NORM_TAPS,
                     "mean": MEAN.tolist(), "std": STD.tolist()},
        # Just the colours here. /health is printed to a console at startup, so
        # the full legend with notes and areas lives on /legend instead of
        # burying the status line it is there to show.
        "palette": {c: PALETTE_HEX[c][0] for c in CLASSES},
    }


@router.get("/legend")
def legend_only():
    """Colours and class meanings, for the map key."""
    return {"classes": legend(), "palette": {c: PALETTE_HEX[c][0] for c in CLASSES}}


@router.post("/reload")
def reload_weights():
    """Drop the loaded model so the next request picks up a new file."""
    global _model
    with _lock:
        _model = None
        LOAD_INFO.update(loaded=False)
    return {"reloaded": True, "weights": WEIGHTS, "present": os.path.exists(WEIGHTS)}


@router.post("/predict")
async def predict(
    file: UploadFile = File(...),
    gsd: float = Form(0.0),
    want_overlay: bool = Form(True),
    alpha: float = Form(0.45),
    preview_max: int = Form(2048),
):
    """Classify an uploaded image.

    `gsd` is metres per pixel. A GeoTIFF states its own; for anything else pass
    it, because the image is resampled to the model's working resolution and
    guessing that wrong changes what the classes mean.

    Returns per-class share, the legend, and — unless you ask it not to — a
    PNG overlay and the raw class map, both base64. The class map is a plain
    uint8 PNG whose values index `classes`, so it can be re-coloured or
    georeferenced downstream without re-running the model.
    """
    import buildings as B        # reuse its decoder: GeoTIFF-aware, same rules

    raw = await file.read()
    img, geo_gsd, geo = B._decode(raw, file.filename or "")
    src_gsd = gsd if gsd > 0 else (geo_gsd or 0.0)
    if src_gsd <= 0:
        raise HTTPException(400, "this model needs a ground sample distance: send "
                                 "gsd=<metres per pixel>, or upload a GeoTIFF that "
                                 "carries one")

    # Only ever downsample, which is what the whole-AOI job does too. Upsampling
    # a coarser source to the work grid invents no detail, costs quadratically
    # more windows for it, and would make a 0.28 m satellite scene run as a
    # 5001 px square instead of 3583. Coarser-than-work imagery runs native.
    f = src_gsd / WORK_GSD
    t0 = time.time()
    if f >= 0.98:
        work, eff_gsd = img, src_gsd
    else:
        work = cv2.resize(img, (max(64, int(round(img.shape[1] * f))),
                                max(64, int(round(img.shape[0] * f)))),
                          interpolation=cv2.INTER_AREA)
        eff_gsd = WORK_GSD
    lab = classify(work)
    counts = np.bincount(lab.ravel(), minlength=len(CLASSES))
    # Areas come from the grid the model actually ran on, not from WORK_GSD:
    # quoting work-grid areas for a native-resolution run would be wrong by the
    # square of the ratio, silently.
    rows = legend(counts, total=int(lab.size), px_area_m2=eff_gsd * eff_gsd)

    out = {
        "ok": True,
        "model": "landcover",
        "seconds": round(time.time() - t0, 1),
        "image": {"width": int(img.shape[1]), "height": int(img.shape[0]),
                  "gsd": src_gsd, "work_gsd": eff_gsd, "preferred_gsd": WORK_GSD,
                  "resampled": bool(f < 0.98),
                  "work_width": int(work.shape[1]), "work_height": int(work.shape[0])},
        "classes": rows,
        "dominant": CLASSES[int(np.argmax(counts[1:]) + 1)] if counts[1:].sum() else "unknown",
    }
    from PIL import Image

    def _png64(arr, mode):
        buf = io.BytesIO()
        Image.fromarray(arr, mode=mode).save(buf, format="PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    # The class map is DATA and is always returned, never capped: a caller
    # georeferences it with the upload's own transform, and resampling class
    # ids with any interpolation would invent classes that were never
    # predicted. It used to be gated behind want_overlay, which meant asking
    # for no picture also threw away the answer - the QGIS plugin, which wants
    # the raster and emphatically not 30 MB of base64 overlay, got nothing.
    out["classes_png"] = _png64(lab, "L")
    out["classes_size"] = [int(lab.shape[1]), int(lab.shape[0])]

    if want_overlay:
        ov = overlay(work, lab, alpha=float(np.clip(alpha, 0.0, 1.0)))
        # The overlay IS only a picture, so it is capped: a 5000 px square one
        # is ~30 MB, and base64 of that in a browser tab is 40 MB of string
        # for detail no screen shows.
        cap = max(256, int(preview_max)) if preview_max else 0
        if cap and max(ov.shape[:2]) > cap:
            f2 = cap / float(max(ov.shape[:2]))
            ov = cv2.resize(ov, (max(1, int(ov.shape[1] * f2)), max(1, int(ov.shape[0] * f2))),
                            interpolation=cv2.INTER_AREA)
        out["overlay_png"] = _png64(ov, "RGB")
        out["overlay_size"] = [int(ov.shape[1]), int(ov.shape[0])]
    return out
