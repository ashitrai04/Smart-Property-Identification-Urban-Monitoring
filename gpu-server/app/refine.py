"""Refine a finished segmentation into clean, seamless, precise map layers.

Runs as job task "refine" on an image that already has a segment/fusion job (and,
optionally, a pothole job). Three passes:

  A. Seamless clean-up of the class raster (classes.tif). The segmentation was done
     window by window, so its polygons were cut at every window edge. Here the raster
     is cleaned (majority filter, gap closing, hole filling, speck removal) in large
     padded blocks, polygonised, and every shape that touches a block edge is merged
     with its neighbour across that edge — no straight cut lines.
  B. Outlines snapped to the imagery with SAM 2 (SAM 1 where SAM 2 is not installed):
     a box prompt per building / water body / open plot, points along each road. A SAM
     outline replaces the SegFormer shape only when the two agree (IoU) and SAM is
     confident; near-rectangular building footprints are squared up.
  W. Water bodies checked against the imagery (water.py): fake water removed, real
     lakes grown to their full outline.
  C. Road damage re-checked against the refined roads (+ OSM) and refined buildings:
     a box counts as "on road" only if enough of it is on a road and it is not on a roof.

Output: results/<job>/results.gpkg with buildings, roads, waterbodies, openareas and
(when a pothole job exists) road_damage — the webapp shows the newest finished layer.
"""
import os
import time

import cv2
import geopandas as gpd
import numpy as np
import rasterio
import shapely
import torch
from rasterio.features import rasterize, shapes
from rasterio.transform import Affine, from_bounds
from rasterio.windows import Window, from_bounds as win_from_bounds
from shapely.geometry import box, mapping

import db

BLOCK, PAD = 4096, 64
# class: (layer, close radius px, fill holes < m², drop shapes < m², simplify m)
RULES = {
    1: ("buildings", 1, 30, 6, 0.25),
    3: ("waterbodies", 1, 50, 40, 0.5),
    2: ("roads", 2, 25, 25, 0.5),
    4: ("openareas", 1, 60, 100, 0.6),
}
ORDER = (1, 3, 2, 4)                 # earlier classes win where cleaned masks touch

SAM_ID = os.environ.get("SAM_REFINE_MODEL", "facebook/sam2.1-hiera-large")
SAM_PX = 1024                        # SAM input crop (px)
SAM_GSD = 0.12                       # m/px of that crop -> ~123 m
SAM_MARGIN_M = 16                    # context around the core of each crop
_sam = {}


def _latest_job(img_id, tasks, layer):
    js = [j for j in db.list_jobs(img_id) if j["task"] in tasks and j["status"] == "done"
          and (j.get("layers") or {}).get(layer)]
    return max(js, key=lambda j: j.get("finished") or 0) if js else None


# ── A. seamless clean-up ─────────────────────────────────────────
def _majority(cls, k=5, ncls=5):
    out = np.zeros_like(cls)
    best = None
    for c in range(ncls):
        f = cv2.blur((cls == c).astype(np.float32), (k, k))
        if best is None:
            best = f
        else:
            m = f > best
            out[m] = c
            best = np.maximum(best, f)
    return out


def _clean(mask, close_px, hole_px, min_px):
    m = mask.astype(np.uint8)
    if close_px:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * close_px + 1, 2 * close_px + 1))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    n, lab, st, _ = cv2.connectedComponentsWithStats((1 - m).astype(np.uint8), connectivity=4)
    fill = st[:, cv2.CC_STAT_AREA] < hole_px
    fill[0] = False                  # label 0 = the mask itself
    m[fill[lab]] = 1
    n, lab, st, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    keep = st[:, cv2.CC_STAT_AREA] >= min_px
    keep[0] = False
    return keep[lab]


def _seamless_layers(classes_path, progress, rules=None, order=None, ncls=5):
    """{layer: GeoDataFrame (raster CRS)} from a class raster, cleaned and merged across blocks.
    rules/order default to the land-use classes; change detection passes its own codes."""
    rules, order = rules or RULES, order or ORDER
    final = {rules[c][0]: [] for c in rules}
    edge = {rules[c][0]: [] for c in rules}
    with rasterio.open(classes_path) as src:
        T, crs, W, H = src.transform, src.crs, src.width, src.height
        px = abs(T.a)
        px_area = abs(T.a * T.e)
        blocks = [(x, y) for y in range(0, H, BLOCK) for x in range(0, W, BLOCK)]
        for i, (x, y) in enumerate(blocks, 1):
            w, h = min(BLOCK, W - x), min(BLOCK, H - y)
            cls = src.read(1, window=Window(x - PAD, y - PAD, w + 2 * PAD, h + 2 * PAD), boundless=True, fill_value=0)
            if not cls.any():
                progress(i / len(blocks))
                continue
            cls = _majority(cls, ncls=ncls)
            core_T = T * Affine.translation(x, y)
            cx0, cy0 = core_T * (0, 0)
            cx1, cy1 = core_T * (w, h)
            ring = box(min(cx0, cx1), min(cy0, cy1), max(cx0, cx1), max(cy0, cy1)).exterior.buffer(px * 0.75)
            taken = np.zeros(cls.shape, bool)
            for c in order:
                name, close_px, hole_m2, min_m2, _ = rules[c]
                m = _clean(cls == c, close_px, hole_m2 / px_area, min_m2 / px_area) & ~taken
                taken |= m
                core = m[PAD:PAD + h, PAD:PAD + w]
                if not core.any():
                    continue
                for g, v in shapes(core.astype(np.uint8), mask=core, transform=core_T):
                    p = shapely.geometry.shape(g)
                    (edge if p.intersects(ring) else final)[name].append(p)
            progress(i / len(blocks))

    out = {}
    for c in rules:
        name, _, _, min_m2, simp = rules[c]
        polys = final[name]
        if edge[name]:
            merged = shapely.union_all(edge[name])
            polys = polys + list(getattr(merged, "geoms", [merged]))
        g = gpd.GeoSeries(polys, crs=crs)
        g = g[g.area >= min_m2].simplify(simp, preserve_topology=True)
        g = g[g.is_valid & ~g.is_empty]
        out[name] = gpd.GeoDataFrame({"area_m2": g.area.round(2).values}, geometry=g.values, crs=crs)
    return out


# ── B. SAM / SAM 2 outlines for every class ─────────────────────
# SAM 2 (transformers >= 4.56) runs in its own environment (.venv-sam2, see
# refine_now.py); inside the API's older environment this falls back to SAM 1.
SAM_FALLBACK = "facebook/sam-vit-large"
# per class: SAM confidence, agreement (IoU) with SegFormer, max area growth, square up
SAM_RULES = {
    "buildings": (0.80, 0.50, 1.6, True),
    "waterbodies": (0.75, 0.50, 1.5, False),
    "openareas": (0.70, 0.40, 1.5, False),
}


def _load_sam(dev):
    if "m" not in _sam:
        if "sam2" in SAM_ID:
            try:
                from transformers import Sam2Model, Sam2Processor
                _sam.update(p=Sam2Processor.from_pretrained(SAM_ID), m=Sam2Model.from_pretrained(SAM_ID).to(dev).eval(),
                            v=2, id=SAM_ID)
            except Exception as e:  # noqa: BLE001 — older transformers: use SAM 1
                print(f"[refine] SAM 2 unavailable here ({repr(e)[:120]}) — using {SAM_FALLBACK}", flush=True)
        if "m" not in _sam:
            from transformers import SamModel, SamProcessor
            mid = SAM_ID if "sam2" not in SAM_ID else SAM_FALLBACK
            _sam.update(p=SamProcessor.from_pretrained(mid), m=SamModel.from_pretrained(mid).to(dev).eval(), v=1, id=mid)
        print(f"[refine] SAM loaded: {_sam['id']} on {dev}", flush=True)
    return _sam


class _Sam:
    """One crop: the image is encoded once, then any number of box / point prompts."""

    def __init__(self, rgb, dev):
        from PIL import Image
        s = _load_sam(dev)
        self.m, self.p, self.v, self.dev = s["m"], s["p"], s["v"], dev
        self.img = Image.fromarray(rgb)
        self.emb, self.orig = None, None
        if self.v == 1:
            with torch.no_grad():
                self.emb = self.m.get_image_embeddings(self.p(self.img, return_tensors="pt")["pixel_values"].to(dev))

    @torch.no_grad()
    def _run(self, **prompts):
        if self.v == 1:
            inp = self.p(self.img, return_tensors="pt", **prompts)
            res = self.m(image_embeddings=self.emb, multimask_output=False,
                         **{k: inp[k].to(self.dev) for k in ("input_boxes", "input_points", "input_labels") if k in inp})
            masks = self.p.image_processor.post_process_masks(res.pred_masks.cpu(), inp["original_sizes"].cpu(),
                                                              inp["reshaped_input_sizes"].cpu())[0]
        else:
            if self.emb is None:
                inp = self.p(images=self.img, return_tensors="pt", **prompts).to(self.dev)
                res = self.m(**inp, multimask_output=False)
                self.emb, self.orig = res.image_embeddings, inp["original_sizes"]
            else:
                inp = self.p(original_sizes=self.orig, return_tensors="pt", **prompts).to(self.dev)
                res = self.m(**inp, image_embeddings=self.emb, multimask_output=False)
            masks = self.p.post_process_masks(res.pred_masks.cpu(), self.orig.cpu())[0]
        masks = masks.numpy().astype(bool)
        masks = masks[:, 0] if masks.ndim == 4 else masks
        return masks, res.iou_scores.float().cpu().numpy().reshape(-1)

    def boxes(self, boxes, chunk=32):
        """Per box: mask cropped to the (integer) box, and SAM's score."""
        out, scores = [], []
        for i in range(0, len(boxes), chunk):
            part = [list(map(float, b)) for b in boxes[i:i + chunk]]
            m, s = self._run(input_boxes=[part])
            for k, (x0, y0, x1, y1) in enumerate(boxes[i:i + chunk]):
                out.append(m[k, int(y0):int(y1) + 1, int(x0):int(x1) + 1])
            scores.append(s[:len(part)])
        return out, np.concatenate(scores) if scores else np.zeros(0)

    def points(self, pts, labels):
        """One object from several positive / negative points: full-crop mask, score."""
        pts = [list(map(float, p)) for p in pts]
        m, s = self._run(input_points=[[pts]], input_labels=[[labels]])
        return m[0], float(s[0])


def _square_up(p):
    """Near-rectangular footprints -> their minimum rotated rectangle."""
    r = p.minimum_rotated_rectangle
    return (r, "rect") if r.area > 0 and p.area / r.area >= 0.85 else (p, "poly")


def _mask_polygon(mask, T):
    m = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    if n < 2:
        return None
    m = (lab == 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))).astype(np.uint8)
    cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        return None
    c = cv2.approxPolyDP(max(cs, key=cv2.contourArea), 1.5, True)[:, 0, :].astype(float)
    if len(c) < 3:
        return None
    xs, ys = T * (c[:, 0], c[:, 1])
    p = shapely.Polygon(np.column_stack([xs, ys]))
    return p if p.is_valid else p.buffer(0)


def _tiles(bounds, gsd):
    crop_m = SAM_PX * gsd
    core_m = crop_m - 2 * SAM_MARGIN_M
    x0, y0, x1, y1 = bounds
    return [(x, y, core_m) for y in np.arange(y0, y1, core_m) for x in np.arange(x0, x1, core_m)]


def _sam_objects(gdf, kind, img_path, on_gpu, progress, dev, gsd=None):
    """Replace SegFormer shapes of one class with SAM outlines where the two agree."""
    from jobs import _read_rgb
    min_score, min_iou, max_grow, square = SAM_RULES[kind]
    sam_gsd = max(SAM_GSD, gsd or 0)          # satellite (~0.3 m): no point upsampling
    geoms = list(gdf.geometry)
    out = [None] * len(geoms)
    tree = shapely.STRtree(geoms)
    cents = shapely.centroid(np.array(geoms, dtype=object))
    tiles = _tiles(gdf.total_bounds, sam_gsd)
    used = 0
    with rasterio.open(img_path) as src:
        for t, (tx, ty, core_m) in enumerate(tiles, 1):
            core = box(tx, ty, tx + core_m, ty + core_m)
            idx = [i for i in tree.query(core) if core.covers(cents[i]) and out[i] is None]
            crop = (tx - SAM_MARGIN_M, ty - SAM_MARGIN_M, tx + core_m + SAM_MARGIN_M, ty + core_m + SAM_MARGIN_M)
            fit = [i for i in idx if box(*crop).contains(geoms[i]) and geoms[i].area >= 4]
            if fit:
                CT = from_bounds(*crop, SAM_PX, SAM_PX)
                rgb, _ = _read_rgb(src, win_from_bounds(*crop, transform=src.transform), SAM_PX, SAM_PX)
                inv = ~CT
                boxes = []
                for i in fit:
                    a, b, c, d = geoms[i].bounds
                    (px0, py1), (px1, py0) = inv * (a, b), inv * (c, d)
                    boxes.append([max(0, int(px0) - 4), max(0, int(py0) - 4),
                                  min(SAM_PX - 1, int(px1) + 4), min(SAM_PX - 1, int(py1) + 4)])
                masks, scores = on_gpu(lambda: _Sam(rgb, dev).boxes(boxes))
                for k, i in enumerate(fit):
                    bx0, by0, bx1, by1 = boxes[k]
                    sub_T = CT * Affine.translation(bx0, by0)
                    segm = rasterize([(mapping(geoms[i]), 1)], out_shape=(by1 - by0 + 1, bx1 - bx0 + 1),
                                     transform=sub_T, dtype="uint8").astype(bool)
                    samm = masks[k]
                    union = (segm | samm).sum()
                    iou = (segm & samm).sum() / union if union else 0
                    if scores[k] >= min_score and iou >= min_iou and samm.sum() <= max_grow * max(segm.sum(), 1):
                        full = np.zeros((SAM_PX, SAM_PX), bool)
                        full[by0:by1 + 1, bx0:bx1 + 1] = samm
                        p = _mask_polygon(full, CT)
                        if p is not None and not p.is_empty and p.area >= 4:
                            out[i] = (p, round(float(scores[k]), 3), round(float(iou), 3))
                            used += 1
            progress(t / len(tiles))

    keep_cols = [c for c in gdf.columns if c not in ("geometry", "area_m2")]
    rows, polys = [], []
    for i, g in enumerate(geoms):
        base = {c: gdf.iloc[i][c] for c in keep_cols}
        if out[i]:
            p, score, iou = out[i]
            tag = "sam2" if _sam.get("v") == 2 else "sam"
        else:
            p, score, iou, tag = g, None, None, "segformer"
        form = None
        if square:
            p, form = _square_up(p)
        polys.append(p)
        rows.append({**base, "area_m2": round(p.area, 2), "outline": tag, "sam_score": score, "agreement": iou,
                     **({"shape": form} if square else {})})
    print(f"[refine] {kind}: SAM outlines accepted for {used}/{len(geoms)}", flush=True)
    return gpd.GeoDataFrame(rows, geometry=polys, crs=gdf.crs), used


def _sam_roads(roads_gdf, bld_gdf, img_path, on_gpu, progress, dev, gsd=None):
    """Roads are long, so they are prompted with points along each road piece's centre line
    (plus negatives on nearby roofs). SAM may only sharpen edges near the SegFormer road:
    road = (SegFormer road near SAM's road) + (SAM road near the SegFormer road)."""
    from jobs import _read_rgb
    from skimage.morphology import skeletonize
    sam_gsd = max(SAM_GSD, gsd or 0)
    roads = list(roads_gdf.geometry)
    rtree = shapely.STRtree(roads)
    bl = list(bld_gdf.geometry) if bld_gdf is not None and len(bld_gdf) else []
    btree = shapely.STRtree(bl) if bl else None
    k_in, k_out = max(1, int(1.0 / sam_gsd)), max(1, int(2.0 / sam_gsd))
    near_in = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k_in + 1, 2 * k_in + 1))
    near_out = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k_out + 1, 2 * k_out + 1))
    tiles = _tiles(roads_gdf.total_bounds, sam_gsd)
    pieces, counts = [], {"used": 0, "tried": 0}
    with rasterio.open(img_path) as src:
        for t, (tx, ty, core_m) in enumerate(tiles, 1):
            core = box(tx, ty, tx + core_m, ty + core_m)
            hits = rtree.query(core)
            if not len(hits):
                progress(t / len(tiles))
                continue
            crop = (tx - SAM_MARGIN_M, ty - SAM_MARGIN_M, tx + core_m + SAM_MARGIN_M, ty + core_m + SAM_MARGIN_M)
            CT = from_bounds(*crop, SAM_PX, SAM_PX)
            hits = rtree.query(box(*crop))
            R = rasterize([(mapping(roads[i]), 1) for i in hits], out_shape=(SAM_PX, SAM_PX), transform=CT,
                          dtype="uint8").astype(bool)
            if R.sum() < 50:
                progress(t / len(tiles))
                continue
            rgb, _ = _read_rgb(src, win_from_bounds(*crop, transform=src.transform), SAM_PX, SAM_PX)
            inv = ~CT
            neg = []
            if btree is not None:
                for j in btree.query(box(*crop))[:12]:
                    c = bl[j].centroid
                    px, py = inv * (c.x, c.y)
                    if 0 <= px < SAM_PX and 0 <= py < SAM_PX and not R[int(py), int(px)]:
                        neg.append([px, py])
            n, lab, st, _ = cv2.connectedComponentsWithStats(R.astype(np.uint8), connectivity=8)
            order = np.argsort(-st[1:, cv2.CC_STAT_AREA])[:6] + 1
            out = R.copy()

            def refine_tile():
                sess = _Sam(rgb, dev)
                for c in order:
                    comp = lab == c
                    if comp.sum() < 50:
                        continue
                    ys, xs = np.nonzero(skeletonize(comp))
                    if len(xs) < 2:
                        continue
                    sel = np.linspace(0, len(xs) - 1, min(8, len(xs))).astype(int)
                    pts = [[float(xs[s]), float(ys[s])] for s in sel] + neg[:6]
                    labels = [1] * len(sel) + [0] * len(neg[:6])
                    counts["tried"] += 1
                    m, score = sess.points(pts, labels)
                    union = (m | comp).sum()
                    iou = (m & comp).sum() / union if union else 0
                    if score >= 0.6 and iou >= 0.35 and m.sum() <= 2.5 * comp.sum():
                        near_sam = cv2.dilate(m.astype(np.uint8), near_in).astype(bool)
                        near_seg = cv2.dilate(comp.astype(np.uint8), near_out).astype(bool)
                        out[comp] = False
                        out[(comp & near_sam) | (m & near_seg)] = True
                        counts["used"] += 1

            on_gpu(refine_tile)
            # keep the core of this crop; neighbouring cores meet exactly at their edges
            mg = int(round(SAM_MARGIN_M / sam_gsd))
            core_mask = out[mg:SAM_PX - mg, mg:SAM_PX - mg]
            core_T = CT * Affine.translation(mg, mg)
            for g, v in shapes(core_mask.astype(np.uint8), mask=core_mask, transform=core_T):
                pieces.append(shapely.geometry.shape(g))
            progress(t / len(tiles))
    merged = shapely.union_all(pieces) if pieces else None
    polys = [p.simplify(0.5) for p in (getattr(merged, "geoms", [merged]) if merged is not None else [])]
    polys = [p for p in polys if p.area >= 25]
    print(f"[refine] roads: SAM sharpened {counts['used']}/{counts['tried']} road pieces", flush=True)
    return gpd.GeoDataFrame({"area_m2": [round(p.area, 2) for p in polys], "outline": "sam+segformer"},
                            geometry=polys, crs=roads_gdf.crs), counts["used"]


# ── C. potholes re-checked ───────────────────────────────────────
def _regate(pot_job, img, crs, roads_gdf, bld_gdf, min_overlap=0.3):
    import roads as rd
    g = gpd.read_file(os.path.join(pot_job["result_dir"], "results.gpkg"), layer="road_damage", engine="pyogrio").to_crs(crs)
    if not len(g):
        return g, {}
    road = list(roads_gdf.geometry.buffer(1.0))
    try:
        road += list(rd.buffered_roads(rd.osm_roads(img["bounds"], img["id"]), crs).geometry)
    except Exception as e:
        print("[refine] OSM roads skipped:", e, flush=True)
    rtree, btree = shapely.STRtree(road), shapely.STRtree(list(bld_gdf.geometry))
    bgeoms = list(bld_gdf.geometry)

    def cover(tree, geoms_, b):
        hits = tree.query(b)
        if not len(hits):
            return 0.0
        return shapely.union_all([geoms_[h] for h in hits]).intersection(b).area / b.area if b.area else 0.0

    ro, bo, on = [], [], []
    for b, ok in zip(g.geometry, g["size_ok"] if "size_ok" in g else [1] * len(g)):
        r_ = cover(rtree, road, b)
        b_ = cover(btree, bgeoms, b)
        ro.append(round(r_, 3)); bo.append(round(b_, 3))
        on.append(int(r_ >= min_overlap and b_ < 0.5 and bool(ok)))
    g["road_overlap"], g["roof_overlap"], g["on_road"] = ro, bo, on
    counts = {}
    for lbl, o in zip(g["label"], on):
        if o:
            counts[lbl] = counts.get(lbl, 0) + 1
    return g, {"counts": counts, "on_road": int(sum(on)), "filtered_off_road": int(len(on) - sum(on))}


# ── the job ──────────────────────────────────────────────────────
def run(job, img, out_dir, on_gpu, progress, cancelled):
    from jobs import _Sink
    import inference as seg

    params = job.get("params") or {}
    seg_job = db.get_job(params["segment_job"]) if params.get("segment_job") else None
    if not seg_job or seg_job.get("status") != "done":      # asked-for one failed: use the newest good one
        seg_job = _latest_job(img["id"], ("segment", "fusion"), "buildings")
    if not seg_job:
        raise RuntimeError("no finished segmentation for this image — run a segment job first")
    classes = os.path.join(seg_job["result_dir"], "classes.tif")
    t0 = time.time()

    layers = _seamless_layers(classes, lambda p: progress(0.3 * p))
    print(f"[refine] seamless layers in {time.time() - t0:.0f}s: " +
          ", ".join(f"{k} {len(v)}" for k, v in layers.items()), flush=True)
    if cancelled():
        return None

    # water: keep only what looks like water in the imagery, grown to its real outline
    wstats = {}
    if len(layers["waterbodies"]) and params.get("water_check", True):
        import water
        try:
            layers["waterbodies"], layers["waterbodies_removed"], wstats = water.check(
                layers["waterbodies"], img, lambda p: progress(0.3 + 0.1 * p), buildings=layers["buildings"])
        except Exception as e:
            print("[refine] water check failed — keeping cleaned SegFormer water:", repr(e)[:200], flush=True)
    if cancelled():
        return None

    # SAM / SAM 2: sharpen every class (each in its own try: a failure keeps the SegFormer shapes)
    sam_used = {}
    if params.get("sam", True):
        kinds = [k for k in params.get("sam_classes", ["buildings", "waterbodies", "openareas", "roads"])
                 if k in layers and len(layers[k])]
        for n, kind in enumerate(kinds):
            s0, ds = 0.4 + 0.5 * n / len(kinds), 0.5 / len(kinds)
            prog = (lambda s0, ds: (lambda p: progress(s0 + ds * p)))(s0, ds)
            try:
                if kind == "roads":
                    layers[kind], sam_used[kind] = _sam_roads(layers["roads"], layers.get("buildings"), img["cog_path"],
                                                              on_gpu, prog, seg.DEVICE, gsd=img.get("gsd_m"))
                else:
                    layers[kind], sam_used[kind] = _sam_objects(layers[kind], kind, img["cog_path"], on_gpu, prog,
                                                                seg.DEVICE, gsd=img.get("gsd_m"))
            except Exception as e:
                if cancelled():
                    return None
                print(f"[refine] SAM pass for {kind} failed - keeping cleaned SegFormer shapes:", repr(e)[:200],
                      flush=True)
            if cancelled():
                return None

    crs = layers["buildings"].crs
    sink = _Sink(out_dir / "results.gpkg", crs)
    for name, gdf in layers.items():
        if len(gdf):
            sink.add(name, list(gdf.geometry), gdf.drop(columns="geometry").to_dict("records"))

    stats = {"segment_job": seg_job["id"], "sam_model": _sam.get("id") if sam_used else None, "sam_outlines": sam_used,
             "features": {k: len(v) for k, v in layers.items()}, **wstats}
    pot = db.get_job(params["pothole_job"]) if params.get("pothole_job") else \
        _latest_job(img["id"], ("pothole",), "road_damage")
    if pot:
        g, s = _regate(pot, img, crs, layers["roads"], layers["buildings"], float(params.get("min_overlap", 0.3)))
        if len(g):
            sink.add("road_damage", list(g.geometry), g.drop(columns="geometry").to_dict("records"))
        stats.update(s, pothole_job=pot["id"])
    progress(1.0)
    return {"layers": sink.counts, "stats": stats}
