"""Run a trained checkpoint over an area of the orthomosaic and write GIS output.

Tiled inference with a feathered blend, then the same road regularization the
production server uses. Writes everything twice - raster and vector, PNG and
GeoTIFF - so the result can be checked in QGIS and handed to the webapp
without another step.

    python infer_area.py \
        --image  $SCRATCH/Ongole_ORI_V3_jpeg92.tif \
        --ckpt   $SCRATCH/runs/.../best.pth \
        --out    $SCRATCH/infer/ongole_1km \
        --area-km2 1.0 \
        --labels $SCRATCH/centre_part_Rd.shp      # optional, for a score

Outputs
    probability.tif   float32, georeferenced
    mask.tif          uint8 0/1, georeferenced
    mask.png          plain PNG
    overlay.png       mask drawn over the imagery
    roads.gpkg        smoothed polygons + centrelines, EPSG:4326
    report.json       settings, timings, and the score if labels were given
"""
import argparse
import json
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np
import rasterio
import torch
from rasterio.enums import Resampling
from rasterio.windows import Window

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

STATS = {
    "sat493m": ((0.430, 0.411, 0.296), (0.213, 0.156, 0.143)),
    "lvd1689m": ((0.485, 0.456, 0.406), (0.229, 0.224, 0.225)),
}


def load_model(ckpt_path, device):
    import importlib.util
    spec = importlib.util.spec_from_file_location("tr", HERE / "train_roads.py")
    tr = importlib.util.module_from_spec(spec)
    argv, sys.argv = sys.argv, [sys.argv[0]]
    spec.loader.exec_module(tr)
    sys.argv = argv

    ck = torch.load(ckpt_path, map_location="cpu")
    backbone = ck["backbone"]
    model = tr.RoadNet(backbone, taps=ck.get("taps"), freeze=True)
    missing, unexpected = model.load_state_dict(ck["model"], strict=False)
    # A frozen-backbone checkpoint deliberately omits enc.*, which comes from
    # the public weights instead. Anything else missing is a real mismatch and
    # must not be waved through - a half-loaded decoder predicts confident
    # nonsense rather than failing.
    dec_missing = [k for k in missing if not k.startswith("enc.")]
    if dec_missing or unexpected:
        raise SystemExit(
            f"checkpoint does not match this architecture: "
            f"{len(dec_missing)} decoder tensors missing, {len(unexpected)} unexpected "
            f"(e.g. {dec_missing[:3]}, {list(unexpected)[:3]})")
    print(f"loaded {backbone}; val_f1 at save time {ck.get('val_f1', '?')}, "
          f"epoch {ck.get('epoch', '?')}")
    return model.to(device).eval(), backbone


@torch.no_grad()
def run(model, src, win_src, out_hw, gsd, mean, std, tile, overlap, device, amp):
    """Probability over one source window, resampled to the working grid."""
    H, W = out_hw
    rgb = src.read([1, 2, 3], window=win_src, out_shape=(3, H, W),
                   resampling=Resampling.average).transpose(1, 2, 0)
    valid = src.dataset_mask(window=win_src, out_shape=(H, W),
                             resampling=Resampling.nearest) > 0

    pad_h, pad_w = (-H) % 64, (-W) % 64
    if pad_h or pad_w:
        rgb = np.pad(rgb, ((0, pad_h), (0, pad_w), (0, 0)), mode="reflect")
    Hp, Wp = rgb.shape[:2]

    acc = np.zeros((Hp, Wp), np.float32)
    wsum = np.zeros((Hp, Wp), np.float32)
    step = max(64, tile - overlap)
    taper = np.hanning(tile + 2)[1:-1].astype(np.float32)
    win2d = np.maximum(np.outer(taper, taper), 1e-3)

    ys = list(range(0, max(1, Hp - tile + 1), step))
    xs = list(range(0, max(1, Wp - tile + 1), step))
    if ys[-1] + tile < Hp:
        ys.append(Hp - tile)
    if xs[-1] + tile < Wp:
        xs.append(Wp - tile)

    n = len(ys) * len(xs)
    done = 0
    for y in ys:
        for x in xs:
            patch = rgb[y:y + tile, x:x + tile].astype(np.float32) / 255.0
            t = torch.from_numpy(np.ascontiguousarray(
                ((patch - mean) / std).transpose(2, 0, 1)))[None].to(device)
            with torch.autocast("cuda", dtype=amp, enabled=device.type == "cuda"):
                p = torch.sigmoid(model(t))[0, 0].float().cpu().numpy()
            acc[y:y + tile, x:x + tile] += p * win2d
            wsum[y:y + tile, x:x + tile] += win2d
            done += 1
            if done % 25 == 0 or done == n:
                print(f"    {done}/{n} tiles", flush=True)
    prob = (acc / np.maximum(wsum, 1e-6))[:H, :W]
    prob[~valid] = 0.0
    return prob, valid


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", required=True)
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--area-km2", type=float, default=1.0)
    ap.add_argument("--centre", nargs=2, type=float, default=None,
                    metavar=("X", "Y"), help="centre in the image CRS; default = mosaic centre")
    ap.add_argument("--gsd", type=float, default=0.20)
    ap.add_argument("--tile", type=int, default=512)
    ap.add_argument("--overlap", type=int, default=128)
    ap.add_argument("--threshold", type=float, default=0.45)
    ap.add_argument("--regularize", default="smooth", choices=["smooth", "corridor", "none"])
    ap.add_argument("--labels", default=None, help="optional, to score the result")
    ap.add_argument("--amp", default="bf16", choices=["bf16", "fp16", "off"])
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    amp = {"bf16": torch.bfloat16, "fp16": torch.float16}.get(a.amp, torch.float32)

    model, backbone = load_model(a.ckpt, device)
    tag = "sat493m" if "sat493m" in backbone else "lvd1689m"
    mean, std = (np.asarray(v, np.float32) for v in STATS[tag])
    print(f"normalisation: {tag}")

    src = rasterio.open(a.image)
    if not src.crs or not src.crs.is_projected:
        sys.exit("needs a projected (metre) CRS")
    native = src.res[0]
    side_m = (a.area_km2 ** 0.5) * 1000.0
    span = int(round(side_m / native))          # source pixels
    H = W = int(round(side_m / a.gsd))          # working pixels

    if a.centre:
        cx, cy = a.centre
        col, row = ~src.transform * (cx, cy)
        c0, r0 = int(col - span / 2), int(row - span / 2)
    else:
        c0, r0 = src.width // 2 - span // 2, src.height // 2 - span // 2
    c0 = max(0, min(c0, src.width - span))
    r0 = max(0, min(r0, src.height - span))
    win = Window(c0, r0, span, span)
    t = src.window_transform(win) * rasterio.Affine.scale(span / W, span / H)

    print(f"area {a.area_km2} km2 = {side_m:.0f} m a side")
    print(f"  source window {span}x{span} px @ {native:.3f} m")
    print(f"  working grid  {W}x{H} px @ {a.gsd} m, tiles of {a.tile} with {a.overlap} overlap")

    t0 = time.time()
    prob, valid = run(model, src, win, (H, W), a.gsd, mean, std,
                      a.tile, a.overlap, device, amp)
    secs = time.time() - t0
    print(f"inference {secs:.0f}s")

    prof = {"driver": "GTiff", "width": W, "height": H, "count": 1,
            "crs": src.crs, "transform": t, "compress": "deflate"}
    with rasterio.open(out / "probability.tif", "w", dtype="float32", **prof) as d:
        d.write(prob.astype(np.float32), 1)

    mask = (prob >= a.threshold) & valid
    with rasterio.open(out / "mask.tif", "w", dtype="uint8", **prof) as d:
        d.write(mask.astype(np.uint8), 1)

    import cv2
    cv2.imwrite(str(out / "mask.png"), (mask * 255).astype(np.uint8))
    rgb = src.read([1, 2, 3], window=win, out_shape=(3, H, W),
                   resampling=Resampling.average).transpose(1, 2, 0).astype(np.uint8)
    ov = rgb.copy()
    ov[mask] = (60, 220, 255)
    cv2.imwrite(str(out / "overlay.png"),
                cv2.addWeighted(rgb, 0.55, ov, 0.45, 0)[:, :, ::-1])

    # Vector, through the same regularizer the live server uses.
    import geopandas as gpd
    import shapely
    from shapely.affinity import affine_transform
    stats = {"polygons": 0, "centrelines": 0, "road_ha": 0.0, "applied": a.regularize}
    try:
        sys.path.insert(0, str(HERE.parent.parent / "gpu-server" / "app"))
        import roadreg
        polys, lines, how = roadreg.regularize_roads(mask, a.gsd, mode=a.regularize)
        m = [t.a, t.b, t.d, t.e, t.xoff, t.yoff]
        geoms = [affine_transform(p, m) for p in polys]
        cls = [affine_transform(l, m) for l in lines]
        if geoms:
            gpd.GeoDataFrame({"area_m2": [round(g.area, 2) for g in geoms]},
                             geometry=geoms, crs=src.crs).to_crs(4326) \
               .to_file(out / "roads.gpkg", layer="roads", driver="GPKG")
        if cls:
            gpd.GeoDataFrame({"length_m": [round(g.length, 2) for g in cls]},
                             geometry=cls, crs=src.crs).to_crs(4326) \
               .to_file(out / "roads.gpkg", layer="road_centrelines", driver="GPKG")
        stats.update(polygons=len(geoms), centrelines=len(cls), applied=how,
                     road_ha=round(sum(g.area for g in geoms) / 1e4, 2))
        print(f"vector: {len(geoms)} polygons ({stats['road_ha']} ha), "
              f"{len(cls)} centrelines, mode '{how}'")
    except Exception as e:
        print(f"regularization skipped: {type(e).__name__}: {str(e)[:160]}")

    score = None
    if a.labels:
        from rasterio.features import rasterize
        lab = gpd.read_file(a.labels).to_crs(src.crs)
        g = shapely.unary_union(lab.geometry.values)
        truth = rasterize([(g, 1)], out_shape=(H, W), transform=t,
                          fill=0, dtype="uint8").astype(bool)
        tp = int((mask & truth).sum()); fp = int((mask & ~truth).sum())
        fn = int((~mask & truth).sum())
        pr = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        score = {"f1": round(2 * pr * rc / (pr + rc), 4) if pr + rc else 0.0,
                 "precision": round(pr, 4), "recall": round(rc, 4),
                 "iou": round(tp / (tp + fp + fn), 4) if tp + fp + fn else 0.0,
                 "label_cover_pct": round(100 * float(truth.mean()), 2)}
        print(f"vs labels: F1 {score['f1']}  P {score['precision']}  "
              f"R {score['recall']}  IoU {score['iou']}")

    (out / "report.json").write_text(json.dumps({
        "image": a.image, "ckpt": a.ckpt, "backbone": backbone,
        "area_km2": a.area_km2, "gsd_m": a.gsd, "tile": a.tile,
        "overlap": a.overlap, "threshold": a.threshold,
        "crs": str(src.crs), "window_px": [c0, r0, span, span],
        "seconds": round(secs, 1), "vector": stats, "score": score,
    }, indent=1), encoding="utf-8")
    print(f"-> {out}")


if __name__ == "__main__":
    main()
