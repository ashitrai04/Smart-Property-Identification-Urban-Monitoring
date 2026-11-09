"""Regenerate chips that are missing from a chip set.

Written after a tool of mine deleted chips it judged mislabelled, by setting
the production model's opinion against the labels. That was the wrong call -
the labels are hand-drawn, so where the model and the label disagree the
label is the authority - and the tool has been removed. This puts back
whatever is missing, and is generally useful whenever a chip set has been
pruned and you want it whole again.

The chipper is deterministic - the grid comes from the label extent and the
chip geometry, and the split from a stable hash of the block index - so a
missing chip can be rebuilt exactly, from its name, without re-reading the
whole mosaic.

    python restore_chips.py --data C:/ongole_roads \
        --image D:/GEO_SERVE/Ongole_ORI_V3_jpeg92.tif \
        --labels <path>/centre_part_Rd.shp
"""
import argparse
import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import geopandas as gpd
import numpy as np
import rasterio
import shapely
from PIL import Image
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.windows import Window
from shapely.affinity import affine_transform

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chip_dataset import assign_split, chip_grid


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--labels", required=True)
    ap.add_argument("--layer", default=None)
    a = ap.parse_args()

    root = Path(a.data)
    meta = json.loads((root / "dataset.json").read_text(encoding="utf-8"))
    chip_px = int(meta["chip_px"])
    gsd = float(meta["gsd_m"])
    chip_m = float(meta["chip_m"])
    stride_m = float(meta["stride_m"])
    block_m = float(meta["block_m"])

    src = rasterio.open(a.image)
    lab = gpd.read_file(a.labels, layer=a.layer) if a.layer else gpd.read_file(a.labels)
    labp = lab.to_crs(src.crs)
    road = shapely.unary_union(labp.geometry.values)

    lb = labp.total_bounds
    ib = src.bounds
    bounds = (max(lb[0], ib.left), max(lb[1], ib.bottom),
              min(lb[2], ib.right), min(lb[3], ib.top))
    origin = (bounds[0], bounds[1])
    cells = chip_grid(bounds, chip_m, stride_m)
    native = src.res[0]
    span = int(round(chip_m / native))

    have = {c["name"] for c in meta["chips"]}
    print(f"manifest lists {len(have):,} chips; walking {len(cells):,} grid positions")

    restored, rows = 0, []
    for x, y in cells:
        col, row = ~src.transform * (x, y + chip_m)
        c0, r0 = int(round(col)), int(round(row))
        if c0 < 0 or r0 < 0 or c0 + span > src.width or r0 + span > src.height:
            continue
        name = f"ongole_{int(gsd*100):03d}_{c0}_{r0}"
        if name in have:
            continue
        split = assign_split(x + chip_m / 2, y + chip_m / 2, chip_m,
                             origin, block_m, (0.70, 0.15))
        if split is None:
            continue
        w = Window(c0, r0, span, span)
        valid = src.dataset_mask(window=w, out_shape=(chip_px, chip_px),
                                 resampling=Resampling.nearest)
        if (valid > 0).mean() < 0.95:
            continue
        t = src.window_transform(w) * rasterio.Affine.scale(span / chip_px, span / chip_px)
        box = shapely.box(*rasterio.windows.bounds(w, src.transform))
        local = road.intersection(box)
        frac = (local.area / box.area) if not local.is_empty else 0.0

        arr = src.read([1, 2, 3], window=w, out_shape=(3, chip_px, chip_px),
                       resampling=Resampling.average)
        if arr.mean() < 8:
            continue
        for sub in ("images", "masks", "vectors"):
            (root / sub / split).mkdir(parents=True, exist_ok=True)
        prof = {"driver": "GTiff", "width": chip_px, "height": chip_px, "count": 3,
                "dtype": "uint8", "crs": src.crs, "transform": t, "compress": "deflate"}
        with rasterio.open(root / "images" / split / f"{name}.tif", "w", **prof) as d:
            d.write(arr)
        Image.fromarray(np.ascontiguousarray(arr.transpose(1, 2, 0))).save(
            root / "images" / split / f"{name}.png")
        mask = (rasterize([(local, 1)], out_shape=(chip_px, chip_px), transform=t,
                          fill=0, dtype="uint8") if not local.is_empty
                else np.zeros((chip_px, chip_px), np.uint8))
        Image.fromarray((mask * 255).astype(np.uint8)).save(
            root / "masks" / split / f"{name}.png")
        vdir = root / "vectors" / split
        if not local.is_empty:
            gpd.GeoDataFrame({"road": [1]}, geometry=[local], crs=src.crs) \
               .to_crs(4326).to_file(vdir / f"{name}.geojson", driver="GeoJSON")
            inv = ~t
            m = [inv.a, inv.b, inv.d, inv.e, inv.xoff, inv.yoff]
            gpd.GeoDataFrame({"road": [1]}, geometry=[affine_transform(local, m)],
                             crs=None).to_file(vdir / f"{name}_px.geojson", driver="GeoJSON")
        else:
            (vdir / f"{name}.geojson").write_text(
                '{"type":"FeatureCollection","features":[]}', encoding="utf-8")

        rows.append({"name": name, "split": split, "road_frac": round(frac, 5),
                     "bounds_utm": [round(v, 2) for v in box.bounds]})
        restored += 1
        if restored % 100 == 0:
            print(f"  restored {restored:,}", flush=True)

    meta["chips"] += rows
    counts = {s: sum(1 for c in meta["chips"] if c["split"] == s)
              for s in ("train", "val", "test")}
    meta["counts"] = counts
    meta.pop("dropped_false_negatives", None)
    meta["restored_chips"] = restored
    (root / "dataset.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(f"\nrestored {restored:,} chips")
    print(f"now: train {counts['train']:,}  val {counts['val']:,}  test {counts['test']:,}")


if __name__ == "__main__":
    main()
