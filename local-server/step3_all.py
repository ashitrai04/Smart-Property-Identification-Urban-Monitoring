"""STEP 3 - waterlogging, cracks/patches, encroachment, depth & volume.

Run per clip:  python step3_all.py clip_06

DSM and DTM arrive on different grids (0.0627 m vs 0.0639 m for the same
extent), so everything is resampled onto one working grid before any arithmetic.
Rasters are read windowed at a working resolution, never whole: the native
pairs are 1.4-1.8 GB each.
"""
import json
import os
import sys
import time

import cv2
import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.enums import Resampling
from rasterio.features import rasterize, shapes
from rasterio.windows import from_bounds
from scipy.ndimage import distance_transform_edt, uniform_filter
from shapely.geometry import LineString, shape
from skimage.morphology import reconstruction

# ── settings ─────────────────────────────────────────────────────
DSM_T = "D:/DSM/DSM_{clip}.tif"
DTM_T = "D:/DSM/DTM_{clip}.tif"
ORTHO = "D:/GEO_SERVE/Ongole_ORI_V3_jpeg92.tif"
ROADS = "D:/GEO_SERVE/ongole_roads_district.gpkg"
ROAD_LINES = "road_lines"
ROAD_POLYS = "road_polygons"
DAMAGE = "D:/GEO_SERVE/Ongole_ORI_V3_road_damage.gpkg"
CLIPS = "C:/Users/asash/Downloads/clip_index.gpkg"
OUT_T = "D:/GEO_SERVE/ongole_step3_{clip}.gpkg"
CSV_T = "D:/GEO_SERVE/_kb/step3_{clip}.csv"
M = 32644

WORK_RES = 0.50            # terrain analysis grid, m
FLOW_RES = 1.00            # flow accumulation grid (coarser: it is a routing question)
FINE_RES = 0.0639          # native-ish, for pothole depth

# part 1 - waterlogging
POND_MIN_DEPTH = 0.03      # m, shallower than this is noise
POND_MIN_AREA = 1.0        # m2
POND_MAX_DEPTH = 0.60      # m - deeper than this on a road is an artefact:
                           # an underpass, a culvert, or a NaN hole in the DTM
                           # that depression filling turns into a basin
SECTION_M = 30.0           # road is scored in sections this long
FLOW_HIGH_Q = 0.98         # flow accumulation above this quantile = a channel
RISK_HIGH = (0.08, 0.015)  # (pond depth m, cross slope m/m) -> high if depth over and slope under
RISK_MED = (0.04, 0.025)

# part 3 - encroachment
ENCROACH_MIN_H = 0.35      # m above the road surface
ENCROACH_MIN_AREA = 1.0    # m2
ENCROACH_SHRINK = 0.5      # m inside the road edge, so kerbs are not flagged
FLAT_MAX = 0.25            # m of nDSM: above this the ground is not road surface.
                           # The road polygons are buffered centrelines, so they
                           # cover rooftops and tree canopy - 33% of their cells
                           # sit more than 0.35 m above the terrain. Without this
                           # the buildings became "ponds", "encroachments" and
                           # 3 m deep "potholes".
DEPTH_MAX = 0.40           # m - deeper than this is not a pothole

# part 4 - depth, volume, material
RING_IN, RING_OUT = 0.25, 1.0   # m: reference ring around the pothole
DEPTH_MIN = 0.015               # m, below this is not a hole
COMPACTION = 1.25               # loose volume needed per compacted volume
DENSITY_T_M3 = 2.30             # t/m3 of bituminous mix
RATE_PER_T = 9500.0             # INR per tonne, change to your rate


def device_note():
    import torch
    d = "cuda" if torch.cuda.is_available() else (
        "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available() else "cpu")
    print(f"device: {d} (terrain work is numpy/CPU regardless)", flush=True)


def read_on(path, bounds, res, resampling=Resampling.bilinear, block=1024):
    """Read a raster onto a fixed grid, in blocks.

    The DSM/DTM have no overviews, so asking GDAL for a downsampled read of the
    whole extent makes it decode all 1.4 gigapixels at once and the read simply
    fails. Reading block by block keeps it to ~16 megapixels at a time.
    """
    ds = rasterio.open(path)
    W = max(1, int((bounds[2] - bounds[0]) / res))
    H = max(1, int((bounds[3] - bounds[1]) / res))
    out = np.full((H, W), np.nan, np.float32)
    nd = ds.nodata
    for y0 in range(0, H, block):
        for x0 in range(0, W, block):
            h = min(block, H - y0)
            w = min(block, W - x0)
            gb = (bounds[0] + x0 * res, bounds[3] - (y0 + h) * res,
                  bounds[0] + (x0 + w) * res, bounds[3] - y0 * res)
            try:
                a = ds.read(1, window=from_bounds(*gb, ds.transform), out_shape=(h, w),
                            resampling=resampling, boundless=True,
                            fill_value=nd if nd is not None else 0).astype(np.float32)
            except Exception:
                continue
            if nd is not None:
                a[a == nd] = np.nan
            out[y0:y0 + h, x0:x0 + w] = a
    tr = rasterio.Affine(res, 0, bounds[0], 0, -res, bounds[3])
    return out, tr


def fill_sinks(z):
    """Depression filling by morphological reconstruction."""
    m = np.isnan(z)
    zz = z.copy()
    if m.any():
        zz[m] = np.nanmax(z)
    seed = zz.copy()
    seed[1:-1, 1:-1] = zz.max()
    filled = reconstruction(seed, zz, method="erosion")
    filled[m] = np.nan
    return filled


def flow_accum(z):
    """D8 accumulation.

    The first version looped over every cell AND its 8 neighbours in Python -
    92 million cells, which does not finish. Here the steepest-descent receiver
    is computed for the whole grid with 8 array shifts, and the only loop left
    is one array lookup per cell, walking from high to low.
    """
    h, w = z.shape
    zz = np.where(np.isnan(z), np.inf, z).astype(np.float32)
    best = np.zeros((h, w), np.float32)
    recv = np.full(h * w, -1, np.int64)
    idx = np.arange(h * w).reshape(h, w)
    for dr, dc in ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)):
        nz = np.full((h, w), np.inf, np.float32)
        ni = np.full((h, w), -1, np.int64)
        r0, r1 = max(0, -dr), h - max(0, dr)
        c0, c1 = max(0, -dc), w - max(0, dc)
        nz[r0:r1, c0:c1] = zz[r0 + dr:r1 + dr, c0 + dc:c1 + dc]
        ni[r0:r1, c0:c1] = idx[r0 + dr:r1 + dr, c0 + dc:c1 + dc]
        drop = (zz - nz) / np.hypot(dr, dc)
        take = np.isfinite(drop) & (drop > best)
        best[take] = drop[take]
        recv.reshape(h, w)[take] = ni[take]
    acc = np.ones(h * w, np.float32)
    zf = zz.ravel()
    order = np.argsort(zf)[::-1]
    order = order[np.isfinite(zf[order])]
    for i in order:                      # one lookup per cell, high to low
        j = recv[i]
        if j >= 0:
            acc[j] += acc[i]
    return acc.reshape(h, w)


def main(clip="clip_06"):
    t0 = time.time()
    device_note()
    dsm_p, dtm_p = DSM_T.format(clip=clip), DTM_T.format(clip=clip)
    ci = gpd.read_file(CLIPS, layer="clip_index").to_crs(M)
    poly = ci[ci["clip"] == clip].geometry.iloc[0]
    b = poly.bounds
    with rasterio.open(dsm_p) as a, rasterio.open(dtm_p) as c:
        print(f"{clip}: DSM {a.width}x{a.height} @{abs(a.transform.a):.4f} m | "
              f"DTM {c.width}x{c.height} @{abs(c.transform.a):.4f} m", flush=True)
        if a.crs != c.crs:
            print("  ! CRS mismatch between DSM and DTM", flush=True)
        print(f"  grids differ -> both resampled onto a {WORK_RES} m grid", flush=True)
    bounds = (max(b[0], rasterio.open(dsm_p).bounds.left),
              max(b[1], rasterio.open(dsm_p).bounds.bottom),
              min(b[2], rasterio.open(dsm_p).bounds.right),
              min(b[3], rasterio.open(dsm_p).bounds.top))

    dtm, tr = read_on(dtm_p, bounds, WORK_RES)
    dsm, _ = read_on(dsm_p, bounds, WORK_RES)
    print(f"  working grid {dtm.shape[1]}x{dtm.shape[0]} @{WORK_RES} m "
          f"({time.time()-t0:.0f}s)", flush=True)

    rl = gpd.read_file(ROADS, layer=ROAD_LINES).to_crs(M)
    rp = gpd.read_file(ROADS, layer=ROAD_POLYS).to_crs(M)
    rl = rl[rl.geometry.intersects(poly)].reset_index(drop=True)
    rp = rp[rp.geometry.intersects(poly)].reset_index(drop=True)
    roadmask = rasterize([(g, 1) for g in rp.geometry.values], out_shape=dtm.shape,
                         transform=tr, fill=0, dtype="uint8").astype(bool)
    ndsm_all = dsm - dtm
    flat = roadmask & (np.nan_to_num(ndsm_all, nan=9e9) < FLAT_MAX)
    print(f"  roads {len(rl)} lines, {roadmask.mean()*100:.1f}% of the grid; "
          f"of that {100*flat.sum()/max(roadmask.sum(),1):.0f}% is flat road surface "
          f"(the rest is roof or canopy inside the buffered polygon)", flush=True)
    roadmask = flat

    out, summary = {}, {}

    # ── PART 1: waterlogging ─────────────────────────────────────
    filled = fill_sinks(dtm)
    pond = np.nan_to_num(filled - dtm)
    pond[~roadmask] = 0
    pond[pond < POND_MIN_DEPTH] = 0
    artefact = pond > POND_MAX_DEPTH
    n_art = int(artefact.sum())
    pond[artefact] = 0          # not a puddle: an underpass or a DTM void
    gy, gx = np.gradient(np.nan_to_num(dtm), WORK_RES)
    slope = np.hypot(gx, gy)
    # routing is done on a coarser grid - where water crosses a road does not
    # need 50 cm cells, and it keeps the one remaining loop short
    dtm_f, tr_f = read_on(dtm_p, bounds, FLOW_RES)
    acc_f = flow_accum(dtm_f)
    acc = cv2.resize(acc_f, (dtm.shape[1], dtm.shape[0]), interpolation=cv2.INTER_NEAREST)
    acc_thr = np.nanquantile(acc[roadmask], FLOW_HIGH_Q) if roadmask.any() else np.inf
    print(f"  part1: {n_art:,} cells above {POND_MAX_DEPTH} m discarded as artefacts", flush=True)
    print(f"  part1: ponding on road max {pond.max():.2f} m, "
          f"flow threshold {acc_thr:.0f} cells  ({time.time()-t0:.0f}s)", flush=True)

    secs = []
    for _, r in rl.iterrows():
        g = r.geometry
        for ls in ([g] if g.geom_type == "LineString" else list(g.geoms)):
            n = max(1, int(ls.length / SECTION_M))
            for i in range(n):
                s = ls.interpolate(i / n, normalized=True)
                e = ls.interpolate((i + 1) / n, normalized=True)
                seg = LineString([s, e])
                mask = rasterize([(seg.buffer(max(r["width_m"], 2) / 2), 1)],
                                 out_shape=dtm.shape, transform=tr, fill=0, dtype="uint8").astype(bool)
                mask &= roadmask
                if mask.sum() < 4:
                    continue
                pd_ = float(pond[mask].max())
                sl = float(np.nanmedian(slope[mask]))
                fl = float(np.nanmax(acc[mask]))
                if pd_ >= RISK_HIGH[0] and sl <= RISK_HIGH[1]:
                    risk = "high"
                elif pd_ >= RISK_MED[0] and sl <= RISK_MED[1]:
                    risk = "medium"
                elif fl >= acc_thr and sl <= RISK_MED[1]:
                    risk = "medium"
                else:
                    risk = "low"
                secs.append({"geometry": seg, "osm_id": r.get("osm_id"),
                             "name": r.get("name"), "fclass": r.get("fclass"),
                             "length_m": round(seg.length, 1),
                             "pond_depth_m": round(pd_, 3),
                             "slope": round(sl, 4),
                             "flow_acc": int(fl),
                             "crosses_channel": int(fl >= acc_thr),
                             "risk": risk})
    sec = gpd.GeoDataFrame(secs, crs=M)
    out["waterlog_sections"] = sec
    summary["waterlog"] = sec["risk"].value_counts().to_dict()

    pm = (pond >= POND_MIN_DEPTH).astype(np.uint8)
    ponds = [{"geometry": shape(g), "max_depth_m": 0.0}
             for g, v in shapes(pm, mask=pm.astype(bool), transform=tr) if v == 1]
    ponds = [p for p in ponds if p["geometry"].area >= POND_MIN_AREA]
    for p in ponds:
        mk = rasterize([(p["geometry"], 1)], out_shape=dtm.shape, transform=tr,
                       fill=0, dtype="uint8").astype(bool)
        p["max_depth_m"] = round(float(pond[mk].max()), 3)
        p["area_m2"] = round(p["geometry"].area, 2)
        p["volume_m3"] = round(float(pond[mk].sum()) * WORK_RES ** 2, 2)
    if ponds:
        out["waterlog_ponds"] = gpd.GeoDataFrame(ponds, crs=M)
    summary["ponds"] = len(ponds)

    # ── PART 2: cracks, potholes, patches (from the model) ───────
    dmg = gpd.read_file(DAMAGE, layer="road_damage").to_crs(M)
    dmg = dmg[dmg.geometry.centroid.within(poly)].reset_index(drop=True)
    for lab, sub in dmg.groupby("label"):
        out[f"damage_{lab}"] = sub
    summary["damage"] = dmg["label"].value_counts().to_dict()
    print(f"  part2: {len(dmg)} detections in this clip "
          f"({dmg['label'].value_counts().to_dict()})", flush=True)

    # ── PART 3: encroachment ─────────────────────────────────────
    ndsm = ndsm_all
    inner = rasterize([(g.buffer(-ENCROACH_SHRINK), 1) for g in rp.geometry.values
                       if g.buffer(-ENCROACH_SHRINK).area > 0],
                      out_shape=dtm.shape, transform=tr, fill=0, dtype="uint8").astype(bool)
    enc = ((ndsm >= ENCROACH_MIN_H) & inner).astype(np.uint8)
    enc = cv2.morphologyEx(enc, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    rows = []
    for g, v in shapes(enc, mask=enc.astype(bool), transform=tr):
        if v != 1:
            continue
        p = shape(g)
        if p.area < ENCROACH_MIN_AREA:
            continue
        mk = rasterize([(p, 1)], out_shape=dtm.shape, transform=tr, fill=0,
                       dtype="uint8").astype(bool)
        hh = ndsm[mk]
        rows.append({"geometry": p, "area_m2": round(p.area, 2),
                     "height_max_m": round(float(np.nanmax(hh)), 2),
                     "height_mean_m": round(float(np.nanmean(hh)), 2),
                     "volume_m3": round(float(np.nansum(hh)) * WORK_RES ** 2, 2)})
    if rows:
        out["encroachment"] = gpd.GeoDataFrame(rows, crs=M)
    summary["encroachment"] = {"n": len(rows),
                               "area_m2": round(sum(r["area_m2"] for r in rows), 1)}
    print(f"  part3: {len(rows)} encroachments on the carriageway "
          f"({time.time()-t0:.0f}s)", flush=True)

    # ── PART 4: pothole depth, volume, material ──────────────────
    ph = dmg[dmg["label"] == "pothole"].reset_index(drop=True)
    dsm_ds = rasterio.open(dsm_p)
    res_f = abs(dsm_ds.transform.a)
    rows = []
    for i, r in ph.iterrows():
        c = r.geometry.centroid
        rad = max(r["size_m"], 0.6) / 2 + RING_OUT + 0.3
        bb = (c.x - rad, c.y - rad, c.x + rad, c.y + rad)
        try:
            z, ztr = read_on(dsm_p, bb, FINE_RES)
        except Exception:
            continue
        if z.size < 25 or np.all(np.isnan(z)):
            continue
        H, W = z.shape
        yy, xx = np.mgrid[0:H, 0:W]
        wx = ztr.c + (xx + 0.5) * FINE_RES
        wy = ztr.f - (yy + 0.5) * FINE_RES
        d = np.hypot(wx - c.x, wy - c.y)
        half = max(r["size_m"], 0.6) / 2
        inside = d <= half
        ring = (d > half + RING_IN) & (d <= half + RING_OUT) & np.isfinite(z)
        if ring.sum() < 20 or inside.sum() < 9:
            continue
        # the reference ring has to be road, not a kerb, roof or bush
        if float(np.nanstd(z[ring])) > 0.12:
            continue
        # plane through the good road around the hole
        A = np.c_[wx[ring] - c.x, wy[ring] - c.y, np.ones(ring.sum())]
        coef, *_ = np.linalg.lstsq(A, z[ring], rcond=None)
        ref = coef[0] * (wx - c.x) + coef[1] * (wy - c.y) + coef[2]
        dep = ref - z
        dep[~inside] = np.nan
        dep[dep < 0] = 0
        valid = np.isfinite(dep) & inside
        if not valid.any() or np.nanmax(dep[valid]) < DEPTH_MIN:
            continue
        if float(np.nanmax(dep[valid])) > DEPTH_MAX:
            continue
        cell = FINE_RES ** 2
        area = float((dep[valid] > DEPTH_MIN).sum()) * cell
        vol = float(np.nansum(dep[valid])) * cell
        loose = vol * COMPACTION
        tonnes = loose * DENSITY_T_M3
        rows.append({"geometry": r.geometry,
                     "confidence": r["confidence"], "low_confidence": r["low_confidence"],
                     "size_m": r["size_m"],
                     "area_m2": round(area, 3),
                     "depth_max_m": round(float(np.nanmax(dep[valid])), 3),
                     "depth_mean_m": round(float(np.nanmean(dep[valid][dep[valid] > 0])), 3),
                     "volume_m3": round(vol, 4),
                     "loose_m3": round(loose, 4),
                     "tonnes": round(tonnes, 4),
                     "cost_inr": round(tonnes * RATE_PER_T, 1)})
        if (i + 1) % 400 == 0:
            print(f"    depth {i+1}/{len(ph)}  ({time.time()-t0:.0f}s)", flush=True)
    if rows:
        pv = gpd.GeoDataFrame(rows, crs=M)
        out["pothole_volume"] = pv
        summary["volume"] = {
            "potholes_measured": len(pv), "of_detected": int(len(ph)),
            "depth_median_m": round(float(pv["depth_max_m"].median()), 3),
            "depth_p90_m": round(float(pv["depth_max_m"].quantile(.9)), 3),
            "volume_total_m3": round(float(pv["volume_m3"].sum()), 2),
            "tonnes_total": round(float(pv["tonnes"].sum()), 2),
            "cost_total_inr": round(float(pv["cost_inr"].sum()), 0)}
        print(f"  part4: {len(pv)} of {len(ph)} potholes measured, "
              f"{pv['volume_m3'].sum():.2f} m3, {pv['tonnes'].sum():.1f} t", flush=True)

    outp = OUT_T.format(clip=clip)
    if os.path.exists(outp):
        os.remove(outp)
    for name, g in out.items():
        g.to_crs(4326).to_file(outp, layer=name, driver="GPKG")
    os.makedirs(os.path.dirname(CSV_T.format(clip=clip)), exist_ok=True)
    pd.DataFrame([{"clip": clip, **{f"{k}_{kk}": vv for k, v in summary.items()
                                    if isinstance(v, dict) for kk, vv in v.items()},
                   **{k: v for k, v in summary.items() if not isinstance(v, dict)}}]
                 ).to_csv(CSV_T.format(clip=clip), index=False)
    json.dump(summary, open(CSV_T.format(clip=clip).replace(".csv", ".json"), "w"), indent=1)
    print(f"\nlayers: {', '.join(out)}")
    print(json.dumps(summary, indent=1))
    print(f"wrote {outp}  ({(time.time()-t0)/60:.1f} min)", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "clip_06")
