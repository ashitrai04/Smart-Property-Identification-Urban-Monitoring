"""
Instance-matched bi-temporal change detection.
Buildings are matched as WHOLE footprints (a footprint counts as new/demolished
only if it barely overlaps the other epoch) -> far fewer false positives than
pixel differencing. Adds roads, vegetation gain/loss, and water change.
Legend ids: 1 New Construction | 2 Demolished | 3 New/Widened Road
            4 Vegetation Loss   | 5 Vegetation Gain | 6 Water Change
"""
import numpy as np
import cv2
from skimage.measure import label as sklabel, regionprops
import inference as seg

CD = {
    1: ("New Construction",   (0, 220, 0)),
    2: ("Demolished",         (235, 40, 40)),
    3: ("New / Widened Road", (245, 200, 20)),
    4: ("Vegetation Loss",    (255, 140, 0)),
    5: ("Vegetation Gain",    (90, 200, 130)),
    6: ("Water Change",       (40, 120, 255)),
}
CD_DEFS = {0: "No Change", **{k: v[0] for k, v in CD.items()}}
CD_COLORS = {k: v[1] for k, v in CD.items()}
# which category ids each analysis mode reports
MODE_IDS = {"building": (1, 2), "vegetation": (4, 5), "water": (6,), "all": (1, 2, 3, 4, 5, 6)}
_K = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
_O = np.ones((3, 3), np.uint8)


def _veg(rgb):
    f = rgb.astype(np.float32)
    for c in range(3):
        lo, hi = np.percentile(f[:, :, c], [2, 98])
        f[:, :, c] = np.clip((f[:, :, c] - lo) / max(hi - lo, 1e-6), 0, 1) * 255
    s = f.sum(2) + 1e-6
    r, g, b = f[:, :, 0] / s, f[:, :, 1] / s, f[:, :, 2] / s
    m = ((2 * g - r - b) > 0.06) & (f[:, :, 1] > f[:, :, 0]) & (f[:, :, 1] > f[:, :, 2])
    return cv2.morphologyEx(m.astype(np.uint8), cv2.MORPH_OPEN, _O).astype(bool)


def _clean(m, min_area):
    m = cv2.morphologyEx(m.astype(np.uint8), cv2.MORPH_OPEN, _O)
    lab = sklabel(m, connectivity=2)
    out = np.zeros_like(m)
    for rp in regionprops(lab):
        if rp.area >= min_area:
            out[lab == rp.label] = 1
    return out.astype(bool)


def change(rgb1, rgb2, model, mode="building"):
    keep = MODE_IDS.get(mode, MODE_IDS["building"])
    pred1 = seg.segment(model, rgb1)
    pred2 = seg.segment(model, rgb2)
    if pred2.shape != pred1.shape:
        pred2 = cv2.resize(pred2.astype(np.uint8), (pred1.shape[1], pred1.shape[0]),
                           interpolation=cv2.INTER_NEAREST).astype(np.int32)
        rgb2 = cv2.resize(rgb2, (pred1.shape[1], pred1.shape[0]))
    H, W = pred1.shape
    area = H * W
    MIN = max(40, int(0.00025 * area))
    B, RD, WA = seg.CLASS_BUILDING, seg.CLASS_ROAD, seg.CLASS_WATER
    ids = np.zeros((H, W), np.uint8)

    # ── buildings: instance-level footprint matching (dilation tolerates small offset) ──
    B1 = (pred1 == B); B2 = (pred2 == B)
    B1d = cv2.dilate(B1.astype(np.uint8), _K).astype(bool)
    B2d = cv2.dilate(B2.astype(np.uint8), _K).astype(bool)
    L1 = sklabel(cv2.morphologyEx(B1.astype(np.uint8), cv2.MORPH_OPEN, _O), connectivity=2)
    L2 = sklabel(cv2.morphologyEx(B2.astype(np.uint8), cv2.MORPH_OPEN, _O), connectivity=2)
    n_new = n_dem = 0
    for rp in regionprops(L2):
        if rp.area < MIN:
            continue
        m = (L2 == rp.label)
        if (m & B1d).sum() / m.sum() < 0.25:
            ids[m] = 1; n_new += 1
    for rp in regionprops(L1):
        if rp.area < MIN:
            continue
        m = (L1 == rp.label)
        if (m & B2d).sum() / m.sum() < 0.25:
            ids[m & (ids == 0)] = 2; n_dem += 1

    # ── roads ──
    R1d = cv2.dilate((pred1 == RD).astype(np.uint8), _K).astype(bool)
    newroad = _clean((pred2 == RD) & ~R1d, MIN)
    ids[newroad & (ids == 0)] = 3

    # ── vegetation gain / loss ──
    v1, v2 = _veg(rgb1), _veg(rgb2)
    v1d = cv2.dilate(v1.astype(np.uint8), _K).astype(bool)
    v2d = cv2.dilate(v2.astype(np.uint8), _K).astype(bool)
    ids[_clean(v1 & ~v2d, MIN) & (ids == 0)] = 4
    ids[_clean(v2 & ~v1d, MIN) & (ids == 0)] = 5

    # ── water change (shrinkage/expansion/conversion) ──
    ids[_clean((pred1 == WA) ^ (pred2 == WA), max(MIN, 30)) & (ids == 0)] = 6

    # ── keep only the categories for the selected mode ──
    ids = np.where(np.isin(ids, keep), ids, 0).astype(np.uint8)

    # ── colored overlay on the PRESENT image + white outlines on building changes ──
    over = rgb2.astype(np.float32)
    for cid in keep:
        mm = ids == cid
        if mm.any():
            over[mm] = 0.30 * over[mm] + 0.70 * np.array(CD_COLORS[cid], np.float32)
    over = over.astype(np.uint8)
    for cid in (1, 2):
        if cid in keep:
            cs, _ = cv2.findContours((ids == cid).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(over, cs, -1, (255, 255, 255), 1)

    px = lambda i: int((ids == i).sum())
    stats = {CD[i][0]: {"pixels": px(i), "percent": round(px(i) / area * 100, 3)} for i in keep}
    if 1 in keep or 2 in keep:
        stats["counts"] = {"new_construction": int(n_new), "demolished": int(n_dem)}
    return {
        "mode": mode,
        "change_map_base64": seg.png_b64(over),
        "past_mask_base64": seg.png_b64(seg.colorize(pred1)),
        "present_mask_base64": seg.png_b64(seg.colorize(pred2)),
        "change_class_base64": seg.png_b64_gray(ids),
        "change_defs": {0: "No Change", **{i: CD[i][0] for i in keep}},
        "colors": {str(i): list(CD_COLORS[i]) for i in keep},
        "stats": stats,
    }
