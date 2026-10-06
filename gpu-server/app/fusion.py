"""
SegFormer + SAM instance fusion (single image, CPU).
Uses SegFormer's building/road semantic mask to prompt SAM (transformers SamModel,
no extra deps) so every building gets a crisp, separated footprint — and a
recovery step guarantees coverage never drops below SegFormer.
Returns per-building + road polygons (pixel coords of the working image).
"""
import os
import numpy as np
import cv2
import torch
from scipy import ndimage as ndi
from skimage.feature import peak_local_max
from skimage.segmentation import watershed
from skimage.measure import label as sklabel, regionprops
from PIL import Image

import inference as seg

SAM_MODEL_ID = os.environ.get("SAM_MODEL_ID", "facebook/sam-vit-base")
FUSE_MAX_DIM = int(os.environ.get("FUSE_MAX_DIM", "1536"))
_SAM = None
_SP = None


def _load_sam():
    global _SAM, _SP
    if _SAM is None:
        from transformers import SamModel, SamProcessor
        _SP = SamProcessor.from_pretrained(SAM_MODEL_ID)
        _SAM = SamModel.from_pretrained(SAM_MODEL_ID).to(seg.DEVICE).eval()
        print(f"[fusion] SAM loaded: {SAM_MODEL_ID} on {seg.DEVICE}")
    return _SAM, _SP


@torch.no_grad()
def _sam_masks_for_boxes(rgb_u8, boxes, chunk=48):
    """Return list of bool masks (H,W) for pixel-space boxes [x1,y1,x2,y2].
    Image embedding is computed once and reused across box batches."""
    model, proc = _load_sam()
    img = Image.fromarray(rgb_u8)
    emb_in = proc(img, return_tensors="pt")
    dev = seg.DEVICE
    image_embeddings = model.get_image_embeddings(emb_in["pixel_values"].to(dev))
    out_masks = []
    for i in range(0, len(boxes), chunk):
        ch = [[float(v) for v in b] for b in boxes[i:i + chunk]]
        inp = proc(img, input_boxes=[ch], return_tensors="pt")
        res = model(image_embeddings=image_embeddings,
                    input_boxes=inp["input_boxes"].to(dev), multimask_output=False)
        masks = proc.image_processor.post_process_masks(
            res.pred_masks.cpu(), inp["original_sizes"].cpu(), inp["reshaped_input_sizes"].cpu())[0]
        m = masks[:, 0].numpy().astype(bool)          # (n,H,W)
        for k in range(m.shape[0]):
            out_masks.append(m[k])
    return out_masks


def _contours_to_polys(binary, transform_xy, min_area, simplify=1.2):
    """cv2 contours -> list of rings [[x,y],...] in the coord space given by transform_xy(px,py)."""
    cnts, _ = cv2.findContours(binary.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    polys = []
    for c in cnts:
        if cv2.contourArea(c) < min_area:
            continue
        ap = cv2.approxPolyDP(c, simplify, True)
        if len(ap) < 3:
            continue
        ring = [transform_xy(float(p[0][0]), float(p[0][1])) for p in ap]
        ring.append(ring[0])
        polys.append(ring)
    return polys


def fuse(rgb_u8, pred):
    """rgb_u8 (H0,W0,3), pred class-map (H0,W0). Returns fusion dict."""
    H0, W0 = pred.shape
    scale = 1.0
    rgb, pr = rgb_u8, pred
    if max(H0, W0) > FUSE_MAX_DIM:
        scale = FUSE_MAX_DIM / max(H0, W0)
        rgb = cv2.resize(rgb_u8, (int(W0 * scale), int(H0 * scale)), interpolation=cv2.INTER_AREA)
        pr = cv2.resize(pred.astype(np.uint8), (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_NEAREST).astype(np.int32)
    H, W = pr.shape
    min_area = max(20, int(0.00002 * H * W))
    B = (pr == seg.CLASS_BUILDING)
    R = (pr == seg.CLASS_ROAD)

    inst = np.zeros((H, W), np.int32)
    nid = 0
    sam_n = 0
    if B.any():
        dist = ndi.distance_transform_edt(B)
        md = max(6, int(round(min(H, W) * 0.006)))
        coords = peak_local_max(dist, min_distance=md, labels=B, exclude_border=False)
        mk = np.zeros(B.shape, np.int32)
        for i, (r, c) in enumerate(coords, 1):
            mk[r, c] = i
        ws = watershed(-dist, mk, mask=B)
        boxes = []
        for rp in regionprops(ws):
            if rp.area < min_area:
                continue
            mr, mc, Mr, Mc = rp.bbox
            boxes.append([mc, mr, Mc, Mr])
        if boxes:
            try:
                masks = _sam_masks_for_boxes(rgb, boxes)
                for mm in masks:
                    mm = mm[:H, :W] & B
                    if mm.sum() < min_area:
                        continue
                    lb = sklabel(mm)
                    if lb.max() > 1:
                        sz = np.bincount(lb.ravel()); sz[0] = 0; mm = (lb == sz.argmax())
                    if ((inst > 0) & mm).sum() > 0.5 * mm.sum():
                        continue
                    nid += 1; sam_n += 1
                    inst[mm & (inst == 0)] = nid
            except Exception as e:
                print("[fusion] SAM step failed, falling back to semantic split:", repr(e)[:160])
        # recovery: any building pixel not covered -> keep as instance (coverage >= SegFormer)
        leftover = B & (inst == 0)
        lab = sklabel(leftover, connectivity=2)
        for rp in regionprops(lab):
            if rp.area < max(min_area // 2, 12):
                continue
            nid += 1
            inst[lab == rp.label] = nid

    # ---- overlay (instances colored + roads) ----
    rng = np.random.default_rng(7)
    lut = np.zeros((nid + 1, 3), np.uint8)
    if nid:
        lut[1:] = rng.integers(60, 256, size=(nid, 3))
    over = rgb.copy().astype(np.float32)
    colal = lut[inst].astype(np.float32)
    mk_b = inst > 0
    over[mk_b] = 0.45 * over[mk_b] + 0.55 * colal[mk_b]
    over[(inst == 0) & R] = np.array(seg.CLASS_COLORS_RGB[seg.CLASS_ROAD], np.float32)
    over = over.astype(np.uint8)
    for i in range(1, nid + 1):
        cs, _ = cv2.findContours((inst == i).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(over, cs, -1, (255, 255, 255), 1)

    # ---- polygons in WORKING-image pixel coords (frontend georeferences if bounds) ----
    ident = lambda x, y: [round(x, 2), round(y, 2)]
    b_feats = []
    for i in range(1, nid + 1):
        for ring in _contours_to_polys((inst == i), ident, min_area):
            area_px = int((inst == i).sum())
            b_feats.append({"type": "Feature",
                            "properties": {"id": i, "area_px": area_px},
                            "geometry": {"type": "Polygon", "coordinates": [ring]}})
            break
    r_close = cv2.morphologyEx(R.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    r_feats = [{"type": "Feature", "properties": {"class": "Roads"},
                "geometry": {"type": "Polygon", "coordinates": [ring]}}
               for ring in _contours_to_polys(r_close, ident, min_area * 3)]

    total = H * W
    stats = seg.class_stats(pr)
    stats["building_count"] = nid
    return {
        "overlay_rgb": over,
        "working_size": {"width": W, "height": H},
        "buildings_geojson": {"type": "FeatureCollection", "features": b_feats},
        "roads_geojson": {"type": "FeatureCollection", "features": r_feats},
        "building_count": nid,
        "sam_refined": sam_n,
        "stats": stats,
        "semantic_ids": pr.astype(np.uint8),
    }
