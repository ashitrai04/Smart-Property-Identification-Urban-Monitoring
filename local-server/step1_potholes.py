"""STEP 1 - road damage detection over the Ongole drone ortho.

Tiled inference with overlap, duplicates merged across tile borders with NMS,
saved as georeferenced polygons with a confidence on each.

Scope note: tiling is restricted to the road corridor. The ortho is 388,740
tiles at 1024 px, which is 7.1 hours, and roads are 1.8% of it - step 2 throws
the rest away. Same end result, 23 minutes.
"""
import os
import time

import cv2
import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import torch
from rasterio.windows import Window
from shapely.geometry import box
from shapely.strtree import STRtree
from ultralytics import YOLO

# ── settings ─────────────────────────────────────────────────────
ORTHO = "D:/GEO_SERVE/Ongole_ORI_V3_jpeg92.tif"
ROADS = "D:/GEO_SERVE/ongole_roads_district.gpkg"
ROADS_LAYER = "road_polygons"
WEIGHTS = "D:/React folder/Smart-Property-Identification-Urban-Monitoring/gpu-server/weights/final_best.pt"
OUT = "D:/GEO_SERVE/ongole_road_damage_step1.gpkg"
CSV = "D:/GEO_SERVE/_kb/step1_summary.csv"

TILE = 1024               # model was trained at this
OVERLAP = 0.20            # so a pothole on a tile edge is seen whole
CONF = 0.25               # keep everything; LOW_CONF flags the weak ones
LOW_CONF = 0.40           # below this -> low_confidence = 1
NMS_IOU = 0.50            # merging duplicates across tile borders
BATCH = 8
CORRIDOR_PAD = 6.0        # m beyond the road polygon, for edge potholes
MIN_SIDE_PX = 6           # ignore specks
MAX_SIDE_M = 25.0         # and absurdly large boxes
CLASS_NAMES = {0: "longitudinal_crack", 1: "transverse_crack", 2: "alligator_crack",
               3: "pothole", 4: "repair_patch", 5: "pothole"}


def device():
    if torch.cuda.is_available():
        print(f"device: cuda ({torch.cuda.get_device_name(0)})", flush=True)
        return 0
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        print("device: mps", flush=True)
        return "mps"
    print("device: cpu", flush=True)
    return "cpu"


def main():
    t0 = time.time()
    dev = device()
    src = rasterio.open(ORTHO)
    res = abs(src.transform.a)
    print(f"ortho {src.width}x{src.height} @{res*100:.0f} cm  crs {src.crs}", flush=True)

    rd = gpd.read_file(ROADS, layer=ROADS_LAYER).to_crs(src.crs)
    corridor = rd.geometry.buffer(CORRIDOR_PAD)
    tree = STRtree(list(corridor.values))
    print(f"roads {len(rd)}  surface {rd.geometry.area.sum()/1e4:.0f} ha  "
          f"corridor +{CORRIDOR_PAD:.0f} m", flush=True)

    step = int(TILE * (1 - OVERLAP))
    jobs = []
    for y in range(0, src.height - 1, step):
        for x in range(0, src.width - 1, step):
            l, t = src.transform * (x, y)
            r, b = src.transform * (x + TILE, y + TILE)
            # predicate="intersects": a bare query returns bounding-box
            # candidates, and a diagonal road's bbox is far bigger than the
            # road, which tripled the tile count (154k instead of 48k)
            if tree.query(box(min(l, r), min(t, b), max(l, r), max(t, b)),
                          predicate="intersects").size:
                jobs.append((x, y))
    print(f"tiles on the road corridor: {len(jobs):,} "
          f"(full image would be {(src.width//step)*(src.height//step):,})", flush=True)

    model = YOLO(WEIGHTS)
    boxes, confs, clss = [], [], []
    buf, pos = [], []

    def flush():
        if not buf:
            return
        res_ = model.predict(buf, imgsz=TILE, conf=CONF, iou=0.7, device=dev,
                             verbose=False, batch=len(buf))
        for r_, (px, py) in zip(res_, pos):
            if r_.boxes is None or not len(r_.boxes):
                continue
            xy = r_.boxes.xyxy.cpu().numpy()
            cf = r_.boxes.conf.cpu().numpy()
            cl = r_.boxes.cls.cpu().numpy().astype(int)
            keep = ((xy[:, 2] - xy[:, 0]) >= MIN_SIDE_PX) & ((xy[:, 3] - xy[:, 1]) >= MIN_SIDE_PX)
            xy, cf, cl = xy[keep], cf[keep], cl[keep]
            if not len(xy):
                continue
            xy[:, [0, 2]] += px
            xy[:, [1, 3]] += py
            boxes.append(xy); confs.append(cf); clss.append(cl)
        buf.clear(); pos.clear()

    for n, (x, y) in enumerate(jobs, 1):
        a = src.read([1, 2, 3], window=Window(x, y, TILE, TILE),
                     boundless=True, fill_value=0)
        img = np.ascontiguousarray(np.transpose(a, (1, 2, 0)))
        if img.max() < 8:
            continue
        buf.append(img); pos.append((x, y))
        if len(buf) == BATCH:
            flush()
        if n % 2000 == 0:
            el = time.time() - t0
            got = sum(len(b) for b in boxes)
            print(f"  {n:,}/{len(jobs):,} tiles  {got:,} raw detections  "
                  f"{el/60:.1f} min  eta {el/n*(len(jobs)-n)/60:.0f} min", flush=True)
    flush()

    if not boxes:
        print("no detections")
        return
    XY = np.concatenate(boxes)
    CF = np.concatenate(confs)
    CL = np.concatenate(clss)
    print(f"\nraw detections {len(XY):,}  ({time.time()-t0:.0f}s)", flush=True)

    # merge duplicates from overlapping tiles
    from torchvision.ops import batched_nms
    keep = batched_nms(torch.as_tensor(XY, dtype=torch.float32),
                       torch.as_tensor(CF, dtype=torch.float32),
                       torch.as_tensor(CL), NMS_IOU).numpy()
    XY, CF, CL = XY[keep], CF[keep], CL[keep]
    print(f"after cross-tile NMS: {len(XY):,}", flush=True)

    rows = []
    for (x0, y0, x1, y1), c, k in zip(XY, CF, CL):
        wx0, wy0 = src.transform * (x0, y0)
        wx1, wy1 = src.transform * (x1, y1)
        w_m, h_m = abs(wx1 - wx0), abs(wy1 - wy0)
        if max(w_m, h_m) > MAX_SIDE_M:
            continue
        rows.append({"geometry": box(min(wx0, wx1), min(wy0, wy1), max(wx0, wx1), max(wy0, wy1)),
                     "cls": int(k), "label": CLASS_NAMES.get(int(k), str(k)),
                     "confidence": round(float(c), 3),
                     "low_confidence": int(c < LOW_CONF),
                     "width_m": round(w_m, 2), "height_m": round(h_m, 2),
                     "size_m": round(max(w_m, h_m), 2),
                     "area_m2": round(w_m * h_m, 2)})
    g = gpd.GeoDataFrame(rows, crs=src.crs)
    if os.path.exists(OUT):
        os.remove(OUT)
    g.to_crs(4326).to_file(OUT, layer="damage_all", driver="GPKG")

    os.makedirs(os.path.dirname(CSV), exist_ok=True)
    s = (g.groupby("label")
           .agg(n=("label", "size"), low_conf=("low_confidence", "sum"),
                conf_median=("confidence", "median"), size_median_m=("size_m", "median"))
           .reset_index())
    s.to_csv(CSV, index=False)
    print(f"\n{'class':<22}{'n':>7}{'low conf':>10}{'conf med':>10}{'size med':>10}", flush=True)
    for _, r in s.iterrows():
        print(f"  {r['label']:<20}{int(r['n']):>7}{int(r['low_conf']):>10}"
              f"{r['conf_median']:>10.2f}{r['size_median_m']:>9.1f}m", flush=True)
    print(f"\ntotal {len(g):,} detections, {int(g['low_confidence'].sum()):,} below {LOW_CONF}")
    print(f"wrote {OUT}\n      {CSV}  ({(time.time()-t0)/60:.1f} min)", flush=True)


if __name__ == "__main__":
    main()
