"""Turn the chip set into the layout ACPV-Net trains from.

ACPV-Net does not read a folder of images and masks the way train_roads.py
does. Its training dataset wants three directories per split:

    <data_dir>/<split>/images                       - the chips
    <data_dir>/<split>/masks                        - the label rasters
    <data_dir>/<split>/heatmap_augmented_latent_kl-4 - precomputed latents

The third is produced offline by three scripts in the repo, and the first of
those reads **COCO polygon annotations**, which chip_dataset.py does not emit:
it writes GeoJSON, because that is what a GIS wants. This bridges the two,
then prints the exact three commands to finish the job.

    python acpv_prepare.py --chips C:/ongole_roads --repo /scratch/$USER/ACPV-Net

Classes: Deventer-512 is five-class. Ongole labels are roads only, so the
config written here is two-class (background + road). That means the Deventer
checkpoint's final classifier cannot transfer - everything up to it can, and
does, which is the point of fine-tuning from it.
"""
import argparse
import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np
from PIL import Image

CATEGORIES = [{"id": 1, "name": "road", "supercategory": "road"}]


def rings_from_geojson(path):
    """Exterior rings of a chip's pixel-space GeoJSON, as flat COCO lists."""
    try:
        gj = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for feat in gj.get("features", []):
        geom = feat.get("geometry") or {}
        t, c = geom.get("type"), geom.get("coordinates")
        polys = [c] if t == "Polygon" else (c if t == "MultiPolygon" else [])
        for poly in polys:
            for ring in poly:
                if ring and len(ring) >= 4:
                    out.append([float(v) for xy in ring for v in xy[:2]])
    return out


def bbox_area(flat):
    a = np.asarray(flat, float).reshape(-1, 2)
    x0, y0 = a.min(0)
    x1, y1 = a.max(0)
    # Shoelace: COCO wants the polygon's area, not the box's.
    x, y = a[:, 0], a[:, 1]
    area = 0.5 * abs(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1)))
    return [float(x0), float(y0), float(x1 - x0), float(y1 - y0)], float(area)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chips", required=True, help="output of chip_dataset.py")
    ap.add_argument("--out", default=None,
                    help="where to build the ACPV layout (default <repo>/data/ongole_512)")
    ap.add_argument("--repo", default="/scratch/$USER/ACPV-Net",
                    help="only used to print the follow-up commands")
    ap.add_argument("--link", action="store_true",
                    help="hardlink images/masks instead of copying (same volume only)")
    a = ap.parse_args()

    chips = Path(a.chips)
    meta_p = chips / "dataset.json"
    if not meta_p.exists():
        sys.exit(f"no dataset.json in {chips}")
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    out = Path(a.out) if a.out else Path("data/ongole_512")
    out.mkdir(parents=True, exist_ok=True)

    import os
    import shutil
    totals = {}
    for split in ("train", "val", "test"):
        src_img = chips / "images" / split
        imgs = sorted(src_img.glob("*.png")) or sorted(src_img.glob("*.tif"))
        if not imgs:
            print(f"{split}: no chips, skipped")
            continue
        d_img = out / split / "images"
        d_msk = out / split / "masks"
        d_img.mkdir(parents=True, exist_ok=True)
        d_msk.mkdir(parents=True, exist_ok=True)

        coco = {"images": [], "annotations": [], "categories": CATEGORIES}
        aid = 1
        for i, p in enumerate(imgs, 1):
            stem = p.stem
            # ACPV-Net's loader does io.imread on whatever is in images/, and
            # its config declares 512x512, so the PNG is the safe choice.
            dst_i = d_img / f"{stem}.png"
            src_png = src_img / f"{stem}.png"
            if not dst_i.exists():
                if a.link and src_png.exists():
                    try:
                        os.link(src_png, dst_i)
                    except OSError:
                        shutil.copy2(src_png, dst_i)
                elif src_png.exists():
                    shutil.copy2(src_png, dst_i)
                else:
                    import rasterio
                    with rasterio.open(p) as s:
                        Image.fromarray(s.read([1, 2, 3]).transpose(1, 2, 0)).save(dst_i)

            src_m = chips / "masks" / split / f"{stem}.png"
            dst_m = d_msk / f"{stem}.png"
            if src_m.exists() and not dst_m.exists():
                if a.link:
                    try:
                        os.link(src_m, dst_m)
                    except OSError:
                        shutil.copy2(src_m, dst_m)
                else:
                    shutil.copy2(src_m, dst_m)

            w, h = Image.open(dst_i).size
            coco["images"].append({"id": i, "file_name": f"{stem}.png",
                                   "height": int(h), "width": int(w)})
            for flat in rings_from_geojson(chips / "vectors" / split / f"{stem}_px.geojson"):
                # Clip to the chip. A polygon clipped against the chip box can
                # land a hair outside through float rounding; the repo's own
                # generator clips to width-1 regardless, so emitting clean
                # coordinates just means the annotation file says what it means.
                pts = np.asarray(flat, float).reshape(-1, 2)
                pts[:, 0] = np.clip(pts[:, 0], 0.0, float(w))
                pts[:, 1] = np.clip(pts[:, 1], 0.0, float(h))
                flat = [float(v) for xy in pts for v in xy]
                bb, ar = bbox_area(flat)
                coco["annotations"].append({
                    "id": aid, "image_id": i, "category_id": 1,
                    "segmentation": [flat], "bbox": bb, "area": ar, "iscrowd": 0})
                aid += 1
            if i % 500 == 0:
                print(f"  {split}: {i:,}/{len(imgs):,}", flush=True)

        ann_dir = out / "annotations"
        ann_dir.mkdir(exist_ok=True)
        (ann_dir / f"{split}.json").write_text(json.dumps(coco), encoding="utf-8")
        totals[split] = (len(coco["images"]), len(coco["annotations"]))
        print(f"{split}: {len(coco['images']):,} images, "
              f"{len(coco['annotations']):,} polygons -> {ann_dir/f'{split}.json'}")

    repo = a.repo
    print(f"""
built: {out}

Still to do, on PARAM, to make the latent heatmaps the training dataset
expects. These are the repo's own scripts; they need the acpvnet env and the
kl-f4 autoencoder already in place.

  cd {repo}
  conda activate acpvnet
  export PYTHONPATH={repo}

  # 1. vertex heatmaps from the COCO polygons
  python data/generate_coco_vertex_heatmaps.py \\
      --annotation-path data/ongole_512/annotations/train.json \\
      --output-dir      data/ongole_512/train/heatmap --sigma 3.0

  # 2. D4 augmentation
  python data/augment_heatmap_d4.py \\
      --input_dir  data/ongole_512/train/heatmap \\
      --output_dir data/ongole_512/train/heatmap_augmented

  # 3. encode to kl-4 latents (this is the directory the loader reads)
  python latent_encoder_accelerated.py \\
      --config models/first_stage_models/kl-f4/config.yaml \\
      --input      data/ongole_512/train/heatmap_augmented \\
      --output_dir data/ongole_512/train/heatmap_augmented_latent_kl-4 \\
      --type heatmap --batch-size 8

Then register the dataset in topomapper/config/paths_catalog.py:

    'ongole_512_train_with_latent_vertex_heatmap': {{
        'data_dir': 'data/ongole_512',
        'factory': 'LatentVertexHeatmapTrainDataset'
    }},
    'ongole_512_test_with_latent_vertex_heatmap': {{
        'data_dir': 'data/ongole_512',
        'factory': 'LatentVertexHeatmapTestDataset'
    }},

and submit with  sbatch --export=ALL,REPO={repo} slurm_acpv.sh
""")


if __name__ == "__main__":
    main()
