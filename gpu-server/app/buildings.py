"""Building footprint delineation — HiSup, fine-tuned on Ongole drone imagery.

Where SegFormer answers "which pixels are building", this answers "how many
buildings are there and where does each one end". They are different jobs and
this one is harder in dense Indian low-rise fabric, where houses share walls
and a semantic mask merges a whole terrace into one blob.

The pipeline is four stages, each chosen by measurement rather than by
reasoning (see PIPELINE.md in the training repo for the numbers):

  1. SCALE.  The network is trained at 512 px, so buildings land around 55 px
     across - the range HiSup's attraction field can represent. Feeding the
     native 3 cm imagery puts them at 400 px and the output collapses. The
     imagery is resampled to a set of working resolutions instead.

  2. SEPARATE.  HiSup takes one polygon per connected component of its mask,
     which merges terraces. Seeding a watershed from the network's own
     confidence and flooding -probability puts the boundary on the party wall,
     where the model is genuinely least certain. Measured: F1 0.235 -> 0.506.

  3. VOTE.  Each resolution is run independently and a building is kept only
     if two or more passes agree on it. Without the vote, merging makes things
     worse, because single-pass noise accumulates. Measured: 0.743 -> 0.791.

  4. REGULARIZE.  Rectilinear footprints in a block-consistent frame, so
     neighbours share walls instead of nearly-sharing them.

Held out on 450 hand-drawn buildings the model never trained on:
P 0.819 · R 0.764 · F1 0.791, against 0.531 for a Western-trained baseline.

Named buildings.py rather than hisup.py on purpose: a module called
hisup would shadow the vendored HiSup package and every internal
`from hisup.utils import ...` would resolve back to this file.

Swapping in a newer checkpoint is a file copy plus POST /hisup/reload - the
weights path is read from HISUP_WEIGHTS and the model is loaded lazily, so
nothing needs restarting.
"""
import io
import json
import os
import sys
import threading
import time

import cv2
import numpy as np
import torch
from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

_VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

router = APIRouter(prefix="/hisup", tags=["hisup"])

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEIGHTS = os.environ.get("HISUP_WEIGHTS", os.path.join(HERE, "weights", "hisup_ongole.pth"))
DEVICE = os.environ.get("SEG_DEVICE") or ("cuda" if torch.cuda.is_available() else "cpu")

# The tuned operating point. These were swept per-checkpoint; a new checkpoint
# shifts the probability calibration, so they are overridable per request.
SCALES = [float(x) for x in os.environ.get("HISUP_SCALES", "0.08,0.10,0.125,0.15").split(",")]
MIN_VOTES = int(os.environ.get("HISUP_MIN_VOTES", "2"))
CORE = float(os.environ.get("HISUP_CORE", "0.90"))
LO = float(os.environ.get("HISUP_LO", "0.50"))
# 18.6 m2, not the 31 that was used first. The largest single category of missed
# buildings was not merged terraces but small houses discarded outright - 56 of
# 106 misses had no prediction at all, median 38 m2 against 71 m2 overall - and a
# 31 m2 core floor was throwing them away, because a building's confident core is
# smaller than its footprint. Confirmed on a second validation window that no
# threshold was tuned on: F1 0.726 -> 0.759 there, 0.790 -> 0.811 on the first.
CORE_M2 = float(os.environ.get("HISUP_CORE_M2", "18.6"))  # smallest confident core, m2
TILE = 512
OVERLAP = 64
SCORE = 0.50

# ImageNet statistics: the checkpoint was fine-tuned with these, not with the
# 128/50 its shipped config advertises. Swapping them measurably lowers F1.
MEAN = torch.tensor([0.485, 0.456, 0.406]).view(3, 1, 1) * 255.0
STD = torch.tensor([0.229, 0.224, 0.225]).view(3, 1, 1) * 255.0

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
            raise HTTPException(503, f"HiSup weights not found at {WEIGHTS}")
        print(f"[hisup] loading {WEIGHTS} on {DEVICE}…", flush=True)
        t0 = time.time()
        from hisup.config import cfg
        from hisup.detector import BuildingDetector
        cfg.merge_from_file(os.path.join(HERE, "app", "hisup_config.yaml"))
        cfg.MODEL.DEVICE = DEVICE
        m = BuildingDetector(cfg, test=True)
        sd = torch.load(WEIGHTS, map_location="cpu", weights_only=False)
        sd = sd.get("model", sd.get("state_dict", sd))
        sd = {k.replace("module.", "", 1): v for k, v in sd.items()}
        miss, _ = m.load_state_dict(sd, strict=False)
        tot = len(m.state_dict())
        _model = m.to(DEVICE).eval()
        LOAD_INFO.update(loaded=True, matched=f"{tot - len(miss)}/{tot}",
                         seconds=round(time.time() - t0, 1),
                         mtime=os.path.getmtime(WEIGHTS))
        print(f"[hisup] ready — matched {tot - len(miss)}/{tot} in "
              f"{LOAD_INFO['seconds']}s", flush=True)
        return _model


def _split(mask, lo, core, min_px):
    """One label per building, not one per connected blob.

    A party wall is 3 px of shared masonry; the silhouette barely notches
    there, so a distance transform cannot find it. The mask probability does
    dip, because the network is genuinely unsure which house those pixels
    belong to - so the confident cores become watershed seeds and the flood
    settles on the dip.
    """
    from scipy import ndimage as ndi
    from skimage.measure import regionprops
    from skimage.segmentation import watershed

    fg = mask > lo
    if not fg.any():
        return np.zeros(fg.shape, np.int32)
    fg = ndi.binary_fill_holes(fg)
    seeds = ndi.binary_opening(mask > core, np.ones((3, 3)))
    lbl, k = ndi.label(seeds)
    if k:
        sz = ndi.sum(np.ones(lbl.shape, np.uint8), lbl, range(1, k + 1))
        drop = {i + 1 for i, v in enumerate(sz) if v < min_px}
        if drop:
            lbl, k = ndi.label(seeds & ~np.isin(lbl, list(drop)))
    if k == 0:
        lbl, k = ndi.label(fg)
    lab = watershed(-mask, lbl, mask=fg)
    out = np.zeros(lab.shape, np.int32)
    j = 0
    for r in regionprops(lab):
        if r.area >= min_px:
            j += 1
            out[lab == r.label] = j
    return out


@torch.no_grad()
def _detect_one(img_rgb, gsd, core, lo, core_m2):
    """Run one resolution over a whole image, returning pixel-space polygons."""
    from hisup.utils.polygon import generate_polygon
    from skimage.measure import regionprops

    model = _load()
    H, W = img_rgb.shape[:2]
    step = TILE - OVERLAP
    min_px = max(16, int(core_m2 / (gsd * gsd)))
    polys = []
    for r in range(0, H, step):
        for c in range(0, W, step):
            h, w = min(TILE, H - r), min(TILE, W - c)
            if h < 64 or w < 64:
                continue
            tile = img_rgb[r:r + h, c:c + w]
            if (tile.max(axis=2) > 6).mean() < 0.05:
                continue
            if (h, w) != (TILE, TILE):
                tile = cv2.copyMakeBorder(tile, 0, TILE - h, 0, TILE - w,
                                          cv2.BORDER_CONSTANT, value=0)
            t = torch.from_numpy(np.ascontiguousarray(tile.transpose(2, 0, 1))).float()
            t = ((t - MEAN) / STD).unsqueeze(0).to(DEVICE)
            try:
                out, _ = model(t, [{"height": TILE, "width": TILE, "filename": "x.png"}])
            except Exception:
                continue
            mask = out["mask_pred"][0]
            juncs = out["juncs_pred"][0]
            lab = _split(mask, lo, core, min_px)
            if lab.max() == 0:
                continue
            for prop in regionprops(lab):
                try:
                    poly, js, _, sc, _ = generate_polygon(prop, mask, juncs, 0, False)
                except Exception:
                    continue
                if js.shape[0] == 0 or float(sc) < SCORE:
                    continue
                p = np.asarray(poly, np.float64).reshape(-1, 2)
                if len(p) < 3:
                    continue
                polys.append((p + [c, r], float(sc)))
    return polys


def _to_shapely(polys, scale):
    """Pixel polygons at one working resolution -> shapes in a common frame."""
    from shapely.geometry import Polygon
    out = []
    for p, s in polys:
        g = Polygon(p * scale)
        if not g.is_valid:
            g = g.buffer(0)
        if g.geom_type == "Polygon" and not g.is_empty and g.area > 4:
            out.append((g, s))
    return out


def _dedup(items, thr=0.4):
    from shapely.strtree import STRtree
    items = sorted(items, key=lambda x: -x[1])
    geoms = [g for g, _ in items]
    if not geoms:
        return []
    tree = STRtree(geoms)
    dead, keep = set(), []
    for i, g in enumerate(geoms):
        if i in dead:
            continue
        keep.append(g)
        for j in tree.query(g):
            j = int(j)
            if j <= i or j in dead:
                continue
            m = min(g.area, geoms[j].area)
            if m > 0 and g.intersection(geoms[j]).area / m > thr:
                dead.add(j)
    return keep


def _vote(passes, iou_thr, min_votes):
    """Keep only what two or more resolutions agree on.

    A building seen at one scale alone is usually a shadow, a tarpaulin or a
    water tank. One seen at two is almost always real. Without this filter
    multi-scale merging is worse than a single pass.
    """
    from shapely.strtree import STRtree
    order = sorted(range(len(passes)), key=lambda i: -len(passes[i]))
    kept, votes = [], []
    for pi in order:
        for g in passes[pi]:
            hit = -1
            if kept:
                tree = STRtree(kept)
                for j in tree.query(g):
                    j = int(j)
                    u = kept[j].union(g).area
                    if u > 0 and kept[j].intersection(g).area / u >= iou_thr:
                        hit = j
                        break
            if hit >= 0:
                votes[hit] += 1
                if len(g.exterior.coords) > len(kept[hit].exterior.coords):
                    kept[hit] = g
            else:
                kept.append(g)
                votes.append(1)
    survived = [g for g, v in zip(kept, votes) if v >= min_votes]
    return _drop_contained(survived), votes


def _drop_contained(geoms, thr=0.7):
    """Remove a footprint that sits inside another one.

    Voting matches by IoU, which punishes a size difference hard: a small
    polygon wholly inside a larger one scores small/large, so at 16 m2 inside
    32 m2 the IoU is 0.5 even though containment is 98%. Such a pair is not
    two buildings, it is one building found at two scales - one of them having
    caught only part of the roof - and the vote lets both through.

    Measured on a real run: 4 scales returned 18 footprints to a single pass's
    16, and one of the extra two was exactly this, a 16.3 m2 polygon 98.4%
    inside a 31.9 m2 one. Judging containment by the SMALLER area, the way
    _dedup already does within a pass, is what catches it.

    The larger polygon is kept: the partial detection is the broken one.
    """
    if len(geoms) < 2:
        return list(geoms)
    from shapely.strtree import STRtree
    order = sorted(range(len(geoms)), key=lambda i: -geoms[i].area)
    tree = STRtree(geoms)
    dead = set()
    for i in order:
        if i in dead:
            continue
        g = geoms[i]
        for j in tree.query(g):
            j = int(j)
            if j == i or j in dead or geoms[j].area > g.area:
                continue
            m = geoms[j].area
            if m > 0 and g.intersection(geoms[j]).area / m > thr:
                dead.add(j)
    return [g for i, g in enumerate(geoms) if i not in dead]


# ── endpoints ────────────────────────────────────────────────────
@router.get("/health")
def health():
    return {
        "status": "ok",
        "model": "HiSup HRNetV2-W48, fine-tuned on Ongole drone imagery",
        "task": "building footprint delineation (instances, not semantics)",
        "weights": os.path.basename(WEIGHTS),
        "weights_present": os.path.exists(WEIGHTS),
        "device": DEVICE,
        "load": LOAD_INFO,
        "defaults": {"scales": SCALES, "min_votes": MIN_VOTES,
                     "core": CORE, "lo": LO, "core_m2": CORE_M2},
        "held_out": {"precision": 0.792, "recall": 0.831, "f1": 0.811,
                     "note": "450 buildings outside the training footprint; "
                             "0.759 on a second window of 653"},
    }


@router.post("/reload")
def reload_weights():
    """Pick up a new checkpoint without restarting the server.

    Training produces a better model every few days. Copying the file over
    weights/hisup_ongole.pth and calling this is the whole update procedure.
    """
    global _model
    with _lock:
        _model = None
        LOAD_INFO.update(loaded=False)
    present = os.path.exists(WEIGHTS)
    return {"reloaded": True, "weights": WEIGHTS, "present": present,
            "mtime": os.path.getmtime(WEIGHTS) if present else None,
            "note": "the next request loads it"}


# The resize factor search. The network represents buildings 30-100 px across
# and an Ongole house is ~63 px at the resolution that scored best, so the
# factor that puts the median detection near 63 px is the one to run at.
AUTO_TARGET_PX = 63.0
AUTO_FACTORS = (0.15, 0.22, 0.33, 0.5, 0.75, 1.0, 1.5, 2.2)
AUTO_PROBE = 1024          # probe a centre crop: the search costs the same for any image


def _decode(raw, filename=""):
    """Decode an upload to RGB, and read its real GSD when the file carries one.

    A GeoTIFF states its own ground resolution in its transform, which is
    better than anything we could infer - so for TIFFs go through rasterio
    first and only fall back to the generic decoder. This is also what lets a
    full-size orthophoto tile load at all: cv2 has no notion of a BigTIFF.
    """
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in (".tif", ".tiff"):
        import tempfile
        import rasterio
        from rasterio.enums import Resampling
        fd, tmp = tempfile.mkstemp(suffix=ext)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
            with rasterio.open(tmp) as src:
                gsd, geo = None, None
                if src.crs:
                    a = abs(src.transform.a)
                    if src.crs.is_geographic:          # degrees -> metres at this latitude
                        lat = (src.bounds.top + src.bounds.bottom) / 2
                        a = a * 111320 * float(np.cos(np.radians(lat)))
                    gsd = float(a)
                    # Kept so the result can be put on the map at the place it
                    # came from, instead of only in the upload's own pixels.
                    geo = {"transform": list(src.transform)[:6], "crs": src.crs.to_string()}
                n = min(3, src.count)
                arr = src.read(list(range(1, n + 1)), out_dtype="uint8",
                               resampling=Resampling.average)
                if n == 1:
                    arr = np.repeat(arr, 3, axis=0)
                return np.ascontiguousarray(np.transpose(arr[:3], (1, 2, 0))), gsd, geo
        except Exception as e:
            print(f"[hisup] rasterio could not read {filename}: {e}", flush=True)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    arr = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        raise HTTPException(400, f"could not decode {filename or 'the image'}")
    return cv2.cvtColor(arr, cv2.COLOR_BGR2RGB), None, None


def _auto_factor(img, core, lo):
    """Work out how much to resize an image whose ground resolution is unknown.

    Detect on a small centre crop at a ladder of factors and keep the one whose
    buildings come out closest to the size the network was trained to see. A
    wrong guess shows up clearly: too coarse and nothing is found, too fine and
    the few detections are enormous. Scoring on detection count alone would
    always pick the finest factor, so count is weighted by how far the median
    building is from the trained size, on a log scale because being 2x too big
    and 2x too small are equally wrong.
    """
    H, W = img.shape[:2]
    y0 = max(0, (H - AUTO_PROBE) // 2)
    x0 = max(0, (W - AUTO_PROBE) // 2)
    crop = img[y0:y0 + AUTO_PROBE, x0:x0 + AUTO_PROBE]

    best, best_score, tried = 1.0, -1.0, []
    for f in AUTO_FACTORS:
        h, w = int(crop.shape[0] * f), int(crop.shape[1] * f)
        if min(h, w) < TILE // 2:
            continue
        im = cv2.resize(crop, (w, h), interpolation=(cv2.INTER_AREA if f < 1 else cv2.INTER_CUBIC))
        try:
            px = _detect_one(im, 0.125, core, lo, CORE_M2)
        except Exception as e:
            print(f"[hisup] auto probe failed at {f}: {e}", flush=True)
            continue
        polys = _dedup(_to_shapely(px, 1.0))
        if len(polys) < 2:
            tried.append({"factor": f, "n": len(polys), "score": 0.0})
            continue
        med_px = float(np.median([np.sqrt(g.area) for g in polys]))
        fit = np.exp(-((np.log(max(med_px, 1.0) / AUTO_TARGET_PX)) ** 2) / 0.5)
        score = len(polys) * fit
        tried.append({"factor": f, "n": len(polys), "median_px": round(med_px, 1),
                      "score": round(float(score), 2)})
        if score > best_score:
            best, best_score = f, score
    return best, tried


def _world_geojson(polys, geo):
    """The same footprints in EPSG:4326, for dropping straight on the map.

    Only possible when the upload was georeferenced; a plain PNG has no place
    in the world and gets pixel coordinates alone.
    """
    if not polys or not geo:
        return None
    try:
        import geopandas as gpd
        from affine import Affine as _A
        import shapely
        T = _A(*geo["transform"])
        out = []
        for g in polys:
            xy = list(g.exterior.coords)
            out.append(shapely.Polygon([T * (x, y) for x, y in xy]))
        s = gpd.GeoSeries(out, crs=geo["crs"]).to_crs(4326)
        return json.loads(gpd.GeoDataFrame(
            {"area_m2": [round(p.area, 1) for p in out]}, geometry=list(s), crs=4326).to_json())
    except Exception as e:
        print(f"[hisup] could not georeference the result: {e}", flush=True)
        return None


@router.post("/predict")
async def predict(
    file: UploadFile = File(...),
    gsd: float = Form(0.0),
    scales: str = Form(""),
    min_votes: int = Form(0),
    core: float = Form(0.0),
    lo: float = Form(0.0),
    regularize_out: bool = Form(True),
):
    """Delineate buildings in an uploaded image.

    `gsd` is the ground sample distance of the upload in metres per pixel.
    It no longer has to be supplied: a GeoTIFF states its own, and for
    everything else the resolution is found by probing (`_auto_factor`).
    Passing it explicitly still wins, since a known value beats a measured one.
    """
    raw = await file.read()
    img, geo_gsd, geo = _decode(raw, file.filename or "")

    want = [float(x) for x in scales.split(",") if x.strip()] or SCALES
    mv = min_votes or MIN_VOTES
    co = core or CORE
    l0 = lo or LO

    probe = None
    if gsd > 0:
        src_gsd, gsd_source = gsd, "declared"
    elif geo_gsd and geo_gsd > 0:
        src_gsd, gsd_source = geo_gsd, "georeferenced"
    else:
        f, probe = _auto_factor(img, co, l0)
        src_gsd, gsd_source = f * 0.125, "auto"

    t0 = time.time()
    passes = []
    for target in want:
        f = src_gsd / target            # >1 upsamples, <1 downsamples
        if abs(f - 1.0) < 0.02:
            im = img
        else:
            w = max(64, int(round(img.shape[1] * f)))
            h = max(64, int(round(img.shape[0] * f)))
            im = cv2.resize(img, (w, h), interpolation=(
                cv2.INTER_AREA if f < 1 else cv2.INTER_CUBIC))
        px = _detect_one(im, target, co, l0, CORE_M2)
        # back to the source pixel grid so the passes are comparable
        passes.append(_dedup(_to_shapely(px, 1.0 / f if f else 1.0)))

    merged, votes = _vote(passes, 0.5, mv) if len(passes) > 1 else (passes[0], [])

    # Kept so the client can show what the detector drew before the
    # regularizer squared it off - the two stages the pipeline actually has.
    raw_polys = list(merged)

    if regularize_out and merged:
        # The server already carries a block-consistent rectilinear
        # regularizer; reuse it rather than shipping a second one. Its
        # distances are metric, so they are converted from metres to the
        # pixels these polygons are expressed in.
        try:
            from regularize import regularize_all
            px = max(src_gsd, 1e-6)
            reg, _methods = regularize_all(merged,
                                           neighbour_m=25.0 / px,
                                           simplify=0.30 / px,
                                           min_edge=0.80 / px,
                                           min_iou=0.65)
            # regularize() returns None where it refuses to change a shape
            merged = [r if r is not None and not r.is_empty else g
                      for r, g in zip(reg, merged)]
        except Exception as e:
            print(f"[hisup] regularization skipped: {e}", flush=True)

    raw_feats = [{"type": "Feature", "properties": {"building_id": i},
                  "geometry": {"type": "Polygon",
                               "coordinates": [[[round(x, 2), round(y, 2)]
                                                for x, y in g.exterior.coords]]}}
                 for i, g in enumerate(raw_polys, 1)]

    feats = []
    for i, g in enumerate(merged, 1):
        feats.append({
            "type": "Feature",
            "properties": {"building_id": i,
                           "area_px2": round(g.area, 1),
                           "area_m2": round(g.area * src_gsd * src_gsd, 1),
                           "n_vertices": len(g.exterior.coords) - 1},
            "geometry": {"type": "Polygon",
                         "coordinates": [[[round(x, 2), round(y, 2)]
                                          for x, y in g.exterior.coords]]},
        })

    return JSONResponse({
        "ok": True,
        "model": "hisup",
        "count": len(feats),
        "seconds": round(time.time() - t0, 1),
        "image": {"width": img.shape[1], "height": img.shape[0], "gsd": src_gsd,
                  "gsd_source": gsd_source},
        "settings": {"scales": want, "min_votes": mv, "core": co, "lo": l0},
        "auto": probe,
        "geojson": {"type": "FeatureCollection", "features": feats},
        "geojson_raw": {"type": "FeatureCollection", "features": raw_feats},
        "geo": _world_geojson(merged, geo) if geo else None,
    })


# ── running over an area of a registered orthomosaic ─────────────
#
# These sit on the /hisup router rather than the generic /jobs API on purpose.
# /jobs requires SP_API_KEY, which a browser cannot hold without publishing it,
# and /hisup/* is already the unauthenticated model surface that the data
# server proxies - so an area job reaches the same place a single upload does.

def _pick_imagery(aoi_geojson, imagery_id=None):
    """Which registered raster covers the drawn area."""
    import db
    if imagery_id:
        img = db.get_imagery(imagery_id)
        if not img:
            raise HTTPException(404, f"no imagery {imagery_id}")
        return img

    import shapely
    g = shapely.geometry.shape(aoi_geojson.get("geometry", aoi_geojson))
    best, best_gsd = None, None
    for row in db.list_imagery():
        r = dict(row)
        if r.get("status") != "ready" or not r.get("bounds"):
            continue
        b = r["bounds"] if isinstance(r["bounds"], (list, tuple)) else json.loads(r["bounds"])
        if not shapely.box(*b).intersects(g):
            continue
        # Finest ground resolution wins: it is the one with the detail the
        # model was tuned on, and the job downsamples to 12.5 cm anyway.
        if best is None or (r.get("gsd_m") or 9e9) < best_gsd:
            best, best_gsd = r, (r.get("gsd_m") or 9e9)
    if not best:
        raise HTTPException(404, "no ingested imagery covers that area")
    return best


# Models that can run over a drawn area, and what the map should call them.
AREA_TASKS = {
    "hisup":   {"label": "Building delineation", "kind": "vector",
                "note": "individual footprints - our Ongole model"},
    "segment": {"label": "Land-use segmentation", "kind": "vector",
                "note": "buildings / roads / water / open ground"},
    "fusion":  {"label": "Segmentation + SAM", "kind": "vector",
                "note": "crisper per-building outlines, slower"},
}

area_router = APIRouter(prefix="/area", tags=["area"])


@area_router.get("/tasks")
def area_tasks():
    """What the map may offer, so the list cannot drift from what runs."""
    return {"tasks": [dict(id=k, **v) for k, v in AREA_TASKS.items()]}


@area_router.post("")
@router.post("/area")
async def area_start(payload: dict = Body(...)):
    """Queue a model run over a drawn polygon (EPSG:4326)."""
    import db
    import jobs

    aoi = payload.get("aoi")
    if not aoi:
        raise HTTPException(400, "send an 'aoi' polygon as GeoJSON (EPSG:4326)")
    img = _pick_imagery(aoi, payload.get("imagery_id"))

    params = {"aoi": aoi, "regularize": bool(payload.get("regularize", True))}
    for k in ("scales", "min_votes", "core", "lo", "work_gsd_m", "window"):
        if payload.get(k) not in (None, "", []):
            params[k] = payload[k]

    task = payload.get("task") or "hisup"
    if task not in AREA_TASKS:
        raise HTTPException(400, f"task must be one of {', '.join(AREA_TASKS)}")

    jid = db.add_job(img["id"], task, params)
    jobs.ensure_worker()
    return {"id": jid, "task": task, "status": "queued",
            "imagery": {"id": img["id"], "name": img["name"],
                        "gsd_m": img["gsd_m"]}}


@area_router.get("/{jid}")
@router.get("/area/{jid}")
def area_status(jid: str):
    """Progress for the map's progress bar, with an estimate of what is left.

    The estimate is elapsed/progress, which is honest here because every
    window costs about the same - same pixel count, same four passes.
    """
    import db
    row = db.get_job(jid)
    if not row:
        raise HTTPException(404, "no such job")
    j = dict(row)
    p = float(j.get("progress") or 0.0)
    started = j.get("started") or 0
    elapsed = (time.time() - started) if started else 0
    eta = None
    if j.get("status") == "running" and 0.02 < p < 1:
        eta = max(0, round(elapsed / p - elapsed))
    stats = j.get("stats")
    if isinstance(stats, str):
        try:
            stats = json.loads(stats)
        except ValueError:
            stats = None
    return {"id": jid, "status": j.get("status"), "progress": round(p, 4),
            "percent": round(p * 100, 1), "elapsed_s": round(elapsed),
            "eta_s": eta, "message": j.get("message"),
            "buildings": ((stats or {}).get("features") or {}).get("buildings"),
            "windows": (stats or {}).get("windows"), "stats": stats}


@area_router.post("/{jid}/cancel")
@router.post("/area/{jid}/cancel")
def area_cancel(jid: str):
    import db
    row = db.get_job(jid)
    if not row:
        raise HTTPException(404, "no such job")
    # The runner checks this between windows; there is no way to
    # interrupt one mid-window and no reason to.
    if dict(row).get("status") in ("queued", "running"):
        db.update_job(jid, status="cancelled")
    return {"id": jid, "status": "cancelled"}


@area_router.get("/{jid}/live")
@router.get("/area/{jid}/live")
def area_live(jid: str, since: int = 0):
    """Buildings found so far, from `since` onwards.

    The map calls this while the job runs and draws what comes back, so a
    minutes-long job shows its work instead of a bar and a wait. Only whole
    lines are returned: the writer appends one complete JSON object per
    building, so a partial last line simply waits for the next call.
    """
    import db
    from config import RESULTS
    row = db.get_job(jid)
    if not row:
        raise HTTPException(404, "no such job")
    j = dict(row)
    path = os.path.join(str(RESULTS), jid, "live.jsonl")
    items, n = [], 0
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if not line.endswith("\n"):
                    break                      # still being written
                n += 1
                if n > since:
                    try:
                        items.append(json.loads(line))
                    except ValueError:
                        break
    p = float(j.get("progress") or 0.0)
    return {"id": jid, "status": j.get("status"), "percent": round(p * 100, 1),
            "next": n, "count": n, "items": items}


@area_router.get("/{jid}/result.geojson")
@router.get("/area/{jid}/result.geojson")
def area_result(jid: str):
    """The finished footprints in EPSG:4326, ready to drop on the map."""
    import db
    from config import RESULTS
    row = db.get_job(jid)
    if not row:
        raise HTTPException(404, "no such job")
    j = dict(row)
    if j.get("status") != "done":
        raise HTTPException(409, f"job is {j.get('status')}, not done")
    path = os.path.join(str(RESULTS), jid, "results.gpkg")
    if not os.path.exists(path):
        return JSONResponse({"type": "FeatureCollection", "features": []})

    # Segmentation writes one layer per class, HiSup only "buildings". Return
    # them together, each feature tagged, so the map can colour by layer
    # without having to know which model produced the file.
    import geopandas as gpd
    import pyogrio
    feats = []
    for layer in pyogrio.list_layers(path)[:, 0]:
        g = gpd.read_file(path, layer=layer).to_crs(4326)
        for f in json.loads(g.to_json())["features"]:
            f["properties"] = {**(f.get("properties") or {}), "layer": str(layer)}
            feats.append(f)
    return JSONResponse({"type": "FeatureCollection", "features": feats})
