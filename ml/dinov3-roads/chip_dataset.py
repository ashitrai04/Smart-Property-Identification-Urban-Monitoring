"""Cut the Ongole orthomosaic and the road labels into a training set.

Writes, for every chip:
    images/<split>/<name>.tif   RGB, georeferenced, uint8
    images/<split>/<name>.png   the same pixels, for anything that wants a PNG
    masks/<split>/<name>.png    binary road mask, 0/255, same grid
    vectors/<split>/<name>.geojson   the label clipped to the chip, in EPSG:4326
    vectors/<split>/<name>_px.geojson  the same geometry in chip pixel coords
plus dataset.json describing the lot.

Two decisions here matter more than they look.

**The split is spatial, not random.** Road chips drawn at random overlap each
other and sit on the same continuous street network, so a random split leaks
the validation set into training and every score comes out flattering. The
area is divided into a coarse grid of blocks and whole blocks are assigned to
train / val / test, so a street in validation is never partly in training.

**Vectors are kept, not just masks.** A raster mask is what the loss needs,
but the vector is what tells you whether the shapes are right, lets you
re-rasterize at another resolution without recutting imagery, and is what any
polygon-native model (frame fields, vertex heads) would train against later.

Usage
-----
    python chip_dataset.py --out D:/ongole_roads --gsd 0.20 --chip 512 --overlap 0.5

On the cluster, point --image and --labels at wherever the data landed.
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


def chip_grid(bounds, chip_m, stride_m):
    minx, miny, maxx, maxy = bounds
    xs = np.arange(minx, maxx - chip_m * 0.5, stride_m)
    ys = np.arange(miny, maxy - chip_m * 0.5, stride_m)
    return [(float(x), float(y)) for y in ys for x in xs]


def _block_hash(bx, by, seed=0):
    """Stable across runs, unlike hash(), which Python salts per process."""
    h = (bx * 73856093) ^ (by * 19349663) ^ (seed * 83492791)
    return (h & 0x7FFFFFFF) % 1000 / 1000.0


def assign_split(x, y, chip_m, origin, block_m, ratios, seed=0):
    """Which split a chip belongs to, or None if it straddles a block edge.

    Blocks alone are not enough. At 50% overlap a chip is 102 m across on a
    51 m stride, so a chip sitting near a block boundary overlaps chips in the
    neighbouring block - and if that block went to a different split, the same
    few metres of street are in both train and val. verify_dataset.py measured
    exactly that on the first build: nearest train chip to a val chip, 51 m,
    against a 102 m chip.

    So a chip is only kept when its whole footprint lies inside one block. At
    800 m blocks and 102 m chips that discards a one-chip-wide band at each
    block edge, roughly a fifth of candidates, and buys a guarantee that no
    validation pixel was ever seen in training.
    """
    half = chip_m / 2.0
    bx0 = int((x - half - origin[0]) // block_m)
    by0 = int((y - half - origin[1]) // block_m)
    bx1 = int((x + half - origin[0]) // block_m)
    by1 = int((y + half - origin[1]) // block_m)
    if bx0 != bx1 or by0 != by1:
        return None                       # straddles a boundary: discard
    h = _block_hash(bx0, by0, seed)
    tr, va = ratios
    if h < tr:
        return "train"
    if h < tr + va:
        return "val"
    return "test"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", default=r"D:\GEO_SERVE\Ongole_ORI_V3_jpeg92.tif")
    ap.add_argument("--labels", required=True, help="road polygons (.shp/.gpkg/.geojson)")
    ap.add_argument("--layer", default=None, help="layer name, for multi-layer files")
    ap.add_argument("--out", required=True)
    ap.add_argument("--chip", type=int, default=512, help="chip size in pixels")
    ap.add_argument("--gsd", type=float, default=0.20, help="metres per pixel to resample to")
    ap.add_argument("--overlap", type=float, default=0.5, help="0 = tiled, 0.5 = half-chip stride")
    ap.add_argument("--block-m", type=float, default=800.0, help="spatial split block edge, metres")
    ap.add_argument("--min-road-frac", type=float, default=0.01,
                    help="skip chips with less road than this (1%% default)")
    ap.add_argument("--keep-empty-frac", type=float, default=0.10,
                    help="fraction of road-free chips to keep as negatives")
    ap.add_argument("--min-valid-frac", type=float, default=0.95,
                    help="skip chips that are not this fully covered by real imagery")
    ap.add_argument("--no-png", action="store_true", help="write only GeoTIFF, skip PNG")
    ap.add_argument("--limit", type=int, default=0, help="stop after N chips (for a smoke test)")
    a = ap.parse_args()

    out = Path(a.out)
    src = rasterio.open(a.image)
    if not src.crs or not src.crs.is_projected:
        sys.exit("this needs a projected (metre) CRS")

    lab = gpd.read_file(a.labels, layer=a.layer) if a.layer else gpd.read_file(a.labels)
    labp = lab.to_crs(src.crs)
    road = shapely.unary_union(labp.geometry.values)
    print(f"labels: {len(labp)} feature(s), {road.area/1e4:.1f} ha, crs {src.crs}")

    chip_m = a.chip * a.gsd
    stride_m = chip_m * (1.0 - a.overlap)
    lb = labp.total_bounds
    ib = src.bounds
    bounds = (max(lb[0], ib.left), max(lb[1], ib.bottom),
              min(lb[2], ib.right), min(lb[3], ib.top))
    origin = (bounds[0], bounds[1])
    cells = chip_grid(bounds, chip_m, stride_m)
    print(f"chip {a.chip}px @ {a.gsd} m = {chip_m:.0f} m, stride {stride_m:.0f} m "
          f"-> {len(cells):,} candidate positions")

    for split in ("train", "val", "test"):
        for sub in ("images", "masks", "vectors"):
            (out / sub / split).mkdir(parents=True, exist_ok=True)

    native = src.res[0]
    span = int(round(chip_m / native))          # chip edge in source pixels
    rng = np.random.default_rng(0)
    rows, counts = [], {"train": 0, "val": 0, "test": 0}
    skipped = {"off_image": 0, "nodata": 0, "no_road": 0, "edge_of_block": 0}

    for n, (x, y) in enumerate(cells):
        if a.limit and sum(counts.values()) >= a.limit:
            break
        col, row = ~src.transform * (x, y + chip_m)      # upper-left corner
        c0, r0 = int(round(col)), int(round(row))
        if c0 < 0 or r0 < 0 or c0 + span > src.width or r0 + span > src.height:
            skipped["off_image"] += 1
            continue
        w = Window(c0, r0, span, span)

        valid = src.dataset_mask(window=w, out_shape=(a.chip, a.chip),
                                 resampling=Resampling.nearest)
        if (valid > 0).mean() < a.min_valid_frac:
            skipped["nodata"] += 1
            continue

        t = src.window_transform(w) * rasterio.Affine.scale(span / a.chip, span / a.chip)
        box = shapely.box(*rasterio.windows.bounds(w, src.transform))
        local = road.intersection(box)
        frac = (local.area / box.area) if not local.is_empty else 0.0

        if frac < a.min_road_frac:
            # Some road-free chips are worth keeping: without them the model
            # never learns what is not a road and paints car parks and rooftops.
            if local.is_empty and rng.random() > a.keep_empty_frac:
                skipped["no_road"] += 1
                continue
            if not local.is_empty:
                skipped["no_road"] += 1
                continue

        # Chip centre, not corner: the grid is keyed on the lower-left.
        split = assign_split(x + chip_m / 2, y + chip_m / 2, chip_m,
                             origin, a.block_m, (0.70, 0.15))
        if split is None:
            skipped["edge_of_block"] += 1
            continue
        name = f"ongole_{int(a.gsd*100):03d}_{c0}_{r0}"

        arr = src.read([1, 2, 3], window=w, out_shape=(3, a.chip, a.chip),
                       resampling=Resampling.average)
        prof = {"driver": "GTiff", "width": a.chip, "height": a.chip, "count": 3,
                "dtype": "uint8", "crs": src.crs, "transform": t, "compress": "deflate"}
        with rasterio.open(out / "images" / split / f"{name}.tif", "w", **prof) as dst:
            dst.write(arr)
        rgb = np.ascontiguousarray(arr.transpose(1, 2, 0))
        if not a.no_png:
            Image.fromarray(rgb).save(out / "images" / split / f"{name}.png")

        mask = (rasterize([(local, 1)], out_shape=(a.chip, a.chip), transform=t,
                          fill=0, dtype="uint8") if not local.is_empty
                else np.zeros((a.chip, a.chip), np.uint8))
        Image.fromarray((mask * 255).astype(np.uint8)).save(
            out / "masks" / split / f"{name}.png")

        # Vector labels: world coordinates for GIS, and pixel coordinates so a
        # polygon-native model can train on them without carrying a transform.
        vdir = out / "vectors" / split
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

        counts[split] += 1
        rows.append({"name": name, "split": split, "road_frac": round(frac, 5),
                     "bounds_utm": [round(v, 2) for v in box.bounds]})
        if sum(counts.values()) % 200 == 0:
            print(f"  {sum(counts.values()):,} chips written", flush=True)

    meta = {"image": a.image, "labels": a.labels, "crs": str(src.crs),
            "chip_px": a.chip, "gsd_m": a.gsd, "chip_m": chip_m,
            "stride_m": stride_m, "block_m": a.block_m,
            "counts": counts, "skipped": skipped, "chips": rows}
    (out / "dataset.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")

    print(f"\ntrain {counts['train']:,}   val {counts['val']:,}   test {counts['test']:,}")
    print(f"skipped: {skipped}")
    print(f"-> {out}")


if __name__ == "__main__":
    main()
