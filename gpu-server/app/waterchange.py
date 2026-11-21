"""Water change between two epochs, from the land-cover model's own classes.

No new network and no new weights: the DINOv3 + UPerNet land-cover model
already predicts `water` as one of its nine classes, so change detection is
running it on both epochs over one shared grid and differencing. That also
means water change inherits every improvement to land cover for free.

The idea that makes it worth having, rather than a gain/loss mask:

    classify WHAT REPLACED the water.

Water that became building, road or developed ground is ENCROACHMENT - a
tank built over. Water that became bare soil, scrub or crop is RECESSION -
a level drop, seasonal or drying. Those are the same number of square metres
and completely different findings, and the land-cover model already predicts
the replacing class, so separating them costs nothing.

Two filters, both measured on Ongole 2017 vs 2026 before being chosen:

  MIN_WATER_M2   a "water body" under this is a shadow or a dark roof, not a
                 tank. Without it the dense urban fabric produced a scatter
                 of one- and two-pixel false encroachments: 871 m2 of
                 "encroachment" over the test AOI fell to 359 m2 once bodies
                 under 200 m2 were dropped, and what remained was the bund of
                 the real tank.

  MIN_CHANGE_M2  a change patch too small to send anyone to look at. Keeps
                 the GeoPackage to findings rather than noise.

Registration between epochs is never perfect, so a thin rim of change around
every shoreline is expected and is why the minimum areas exist. Treat a
one-pixel ring as alignment, not as a finding.
"""
import os
import time

import cv2
import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

import landcover as lc

router = APIRouter(prefix="/waterchange", tags=["waterchange"])

# change classes. 0 is "no water either epoch", drawn transparent.
NONE, RETAINED, ENCROACHED, RECEDED, GAINED, LOST_OTHER = 0, 1, 2, 3, 4, 5

CHANGE = {
    NONE:       ("no water",      "#000000",   0, "no water on either date"),
    RETAINED:   ("retained",      "#0045FF", 170, "water on both dates - the water body that stayed"),
    ENCROACHED: ("encroached",    "#E3000F", 235, "water on the earlier date, built over by the later one"),
    RECEDED:    ("receded",       "#FF8A00", 215, "water on the earlier date, now bare ground or plants - a level drop"),
    GAINED:     ("gained",        "#00E5FF", 200, "water on the later date where there was none before"),
    LOST_OTHER: ("lost_unclear",  "#A000A0", 200, "water has gone, but what took its place is not clear"),
}

# What each class is CALLED on screen, as opposed to what it is called in
# code. The two had been one thing, run through .title(), which is how
# "lost_unclear" reached a user's legend as "Lost Unclear" - and how the
# rest read as jargon: "Retained" and "Receded" next to a coloured square
# do not tell anyone what happened to the water.
#
# Each label is a short sentence about the water, so the legend can be read
# without the key beside it. The longer `note` stays as the tooltip.
LABELS = {
    NONE:       "No water either year",
    RETAINED:   "Water in both years",
    ENCROACHED: "Water built over",
    RECEDED:    "Water dried up",
    GAINED:     "Water gained",
    LOST_OTHER: "Water gone, cause unclear",
}

# What counts as having been built over, and what counts as having dried.
BUILT_CLASSES = ("building", "road", "developed")
NATURAL_CLASSES = ("bareland", "rangeland", "tree", "agriculture")

MIN_WATER_M2 = float(os.environ.get("WATERCHANGE_MIN_WATER_M2", "200"))
MIN_CHANGE_M2 = float(os.environ.get("WATERCHANGE_MIN_CHANGE_M2", "50"))


def _hex2rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


PALETTE = np.array([_hex2rgb(CHANGE[i][1]) for i in sorted(CHANGE)], np.uint8)
COLORMAP = {i: (*_hex2rgb(CHANGE[i][1]), CHANGE[i][2]) for i in sorted(CHANGE)}


def clean_water(lab, px_area_m2, min_water_m2=None):
    """The water mask of one epoch, with specks removed.

    Open then close before the size filter: the open kills single-pixel
    noise, the close re-joins a body that thresholding split in two, and
    doing the size filter after both means a real tank is measured whole
    rather than as fragments that each fall under the floor.
    """
    from scipy import ndimage
    floor = MIN_WATER_M2 if min_water_m2 is None else float(min_water_m2)
    m = (lab == lc.CLASSES.index("water")).astype(np.uint8)
    k = np.ones((3, 3), np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, k)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    if floor <= 0 or not m.any():
        return m.astype(bool), 0
    lb, n = ndimage.label(m, structure=np.ones((3, 3)))
    if not n:
        return m.astype(bool), 0
    sizes = np.bincount(lb.ravel())
    sizes[0] = 0
    keep = np.flatnonzero(sizes * px_area_m2 >= floor)
    return np.isin(lb, keep), int(len(keep))


def compare(lab_a, lab_b, px_area_m2, min_water_m2=None, min_change_m2=None):
    """Change map and per-class areas for two aligned land-cover maps.

    Pure: no files, no model, no server. The two labels must already be on
    the same grid - differencing maps that are not aligned measures the
    misalignment, not the change.
    """
    from scipy import ndimage
    if lab_a.shape != lab_b.shape:
        raise ValueError("the two epochs are not on the same grid: %s vs %s"
                         % (lab_a.shape, lab_b.shape))
    floor = MIN_CHANGE_M2 if min_change_m2 is None else float(min_change_m2)

    wa, n_a = clean_water(lab_a, px_area_m2, min_water_m2)
    wb, n_b = clean_water(lab_b, px_area_m2, min_water_m2)

    built = [lc.CLASSES.index(c) for c in BUILT_CLASSES]
    natural = [lc.CLASSES.index(c) for c in NATURAL_CLASSES]

    chg = np.zeros(lab_a.shape, np.uint8)
    chg[wa & wb] = RETAINED
    chg[~wa & wb] = GAINED
    lost = wa & ~wb
    chg[lost & np.isin(lab_b, built)] = ENCROACHED
    chg[lost & np.isin(lab_b, natural)] = RECEDED
    chg[lost & ~np.isin(lab_b, built + natural)] = LOST_OTHER

    # Drop change patches too small to act on. RETAINED is left alone: it is
    # context, not a finding, and shrinking it would misstate the areas.
    if floor > 0:
        for k in (ENCROACHED, RECEDED, GAINED, LOST_OTHER):
            m = chg == k
            if not m.any():
                continue
            lb, n = ndimage.label(m, structure=np.ones((3, 3)))
            if not n:
                continue
            sizes = np.bincount(lb.ravel())
            sizes[0] = 0
            small = np.flatnonzero((sizes > 0) & (sizes * px_area_m2 < floor))
            if len(small):
                chg[np.isin(lb, small)] = NONE

    stats = {"water_bodies_a": n_a, "water_bodies_b": n_b,
             "min_water_m2": MIN_WATER_M2 if min_water_m2 is None else float(min_water_m2),
             "min_change_m2": floor, "px_area_m2": px_area_m2, "classes": {}}
    for k in sorted(CHANGE):
        if k == NONE:
            continue
        n = int((chg == k).sum())
        stats["classes"][CHANGE[k][0]] = {
            "pixels": n, "area_m2": round(n * px_area_m2, 1),
            "area_ha": round(n * px_area_m2 / 1e4, 4)}
    wa_m2 = float(wa.sum()) * px_area_m2
    wb_m2 = float(wb.sum()) * px_area_m2
    stats["water_area_a_m2"] = round(wa_m2, 1)
    stats["water_area_b_m2"] = round(wb_m2, 1)
    stats["net_change_m2"] = round(wb_m2 - wa_m2, 1)
    stats["net_change_pct"] = round(100.0 * (wb_m2 - wa_m2) / wa_m2, 2) if wa_m2 else None
    return chg, stats


def overlay(rgb, chg, alpha=0.65):
    """Blend the change map over the later epoch, leaving unchanged ground alone."""
    out = rgb.astype(np.float32).copy()
    m = (chg > 0)[..., None]
    col = PALETTE[chg].astype(np.float32)
    return np.where(m, (1.0 - alpha) * out + alpha * col, out).astype(np.uint8)


def legend(stats=None):
    """Colours, meanings and - when a run is given - the areas found."""
    rows = []
    for i in sorted(CHANGE):
        name, colour, a, note = CHANGE[i]
        row = {"id": i, "name": name, "label": LABELS.get(i, name),
               "color": colour, "alpha": a, "note": note}
        if stats:
            row.update(stats.get("classes", {}).get(name, {}))
        rows.append(row)
    return rows


@router.get("/health")
def health():
    return {
        "status": "ok",
        "model": "DINOv3 ViT-L/16 (sat493m) + UPerNet land cover, differenced across two epochs",
        "task": "water change, encroachment and recession",
        "weights": os.path.basename(lc.WEIGHTS),
        "weights_present": os.path.exists(lc.WEIGHTS),
        "device": lc.DEVICE,
        "note": "no separate weights: this reads the land-cover model's water class "
                "in both epochs, so it improves whenever land cover does",
        "defaults": {"work_gsd_m": lc.WORK_GSD,
                     "min_water_m2": MIN_WATER_M2,
                     "min_change_m2": MIN_CHANGE_M2,
                     "built_classes": list(BUILT_CLASSES),
                     "natural_classes": list(NATURAL_CLASSES)},
        "palette": {CHANGE[i][0]: CHANGE[i][1] for i in sorted(CHANGE)},
    }


@router.get("/legend")
def legend_only():
    return {"classes": legend()}


@router.post("/predict")
async def predict(
    past: UploadFile = File(...),
    present: UploadFile = File(...),
    gsd: float = Form(0.0),
    min_water_m2: float = Form(0.0),
    min_change_m2: float = Form(0.0),
    want_overlay: bool = Form(True),
    preview_max: int = Form(2048),
):
    """Compare two uploaded images of the same ground.

    They are resampled onto one grid before anything is compared, because
    differencing two maps that are not aligned measures the misalignment.
    The grid is the coarser of the two: upsampling the coarser epoch would
    invent detail and then attribute the invention to change.
    """
    import base64
    import io

    import buildings as B
    from PIL import Image

    raw_a, raw_b = await past.read(), await present.read()
    img_a, gsd_a, _ = B._decode(raw_a, past.filename or "past")
    img_b, gsd_b, _ = B._decode(raw_b, present.filename or "present")
    g_a = gsd or gsd_a or 0.0
    g_b = gsd or gsd_b or 0.0
    if g_a <= 0 or g_b <= 0:
        raise HTTPException(400, "both images need a ground sample distance: send "
                                 "gsd=<metres per pixel>, or upload GeoTIFFs")

    work = max(g_a, g_b, lc.WORK_GSD)
    t0 = time.time()

    def to_grid(img, g):
        f = g / work
        if abs(f - 1.0) < 0.02:
            return img
        return cv2.resize(img, (max(64, int(round(img.shape[1] * f))),
                                max(64, int(round(img.shape[0] * f)))),
                          interpolation=cv2.INTER_AREA)

    a, b = to_grid(img_a, g_a), to_grid(img_b, g_b)
    h = min(a.shape[0], b.shape[0])
    w = min(a.shape[1], b.shape[1])
    if h < 64 or w < 64:
        raise HTTPException(400, "the two images do not overlap enough to compare")
    a, b = a[:h, :w], b[:h, :w]

    lab_a = lc.classify(a)
    lab_b = lc.classify(b)
    chg, stats = compare(lab_a, lab_b, work * work,
                         min_water_m2 or None, min_change_m2 or None)
    stats["seconds"] = round(time.time() - t0, 1)
    stats["work_gsd_m"] = work

    out = {"ok": True, "model": "waterchange", "stats": stats,
           "classes": legend(stats),
           "image": {"width": w, "height": h, "work_gsd": work,
                     "gsd_past": g_a, "gsd_present": g_b}}

    def _png64(arr, mode):
        buf = io.BytesIO()
        Image.fromarray(arr, mode=mode).save(buf, format="PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    # The change map is data and always comes back; the overlay is a picture.
    out["change_png"] = _png64(chg, "L")
    out["change_size"] = [int(w), int(h)]
    if want_overlay:
        ov = overlay(b, chg)
        cap = max(256, int(preview_max)) if preview_max else 0
        if cap and max(ov.shape[:2]) > cap:
            s = cap / float(max(ov.shape[:2]))
            ov = cv2.resize(ov, (max(1, int(ov.shape[1] * s)), max(1, int(ov.shape[0] * s))),
                            interpolation=cv2.INTER_AREA)
        out["overlay_png"] = _png64(ov, "RGB")
    return out
