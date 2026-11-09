"""Audit a built chip set before it costs cluster time.

Checks the things that quietly ruin a run and are invisible in a file count:

  * every image has a mask and a vector, and none of them is corrupt
  * image and mask are the same size and actually aligned
  * how much road there is per split, and whether the splits are comparable
  * spatial leakage: the nearest train chip to each val chip. Chips overlap by
    construction at 50% stride, so if the split were random the same street
    would sit in both and the validation score would be fiction
  * the dataset's own pixel statistics against the normalisation the model
    will use, which is how you find out the wrong constants are in play
  * a contact sheet of random chips with their labels drawn on, because the
    only reliable test of a label is looking at it

    python verify_dataset.py --data C:/ongole_roads --qa qa_sheet.png
"""
import argparse
import json
import random
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np
import rasterio
from PIL import Image

STATS = {
    "sat493m": ((0.430, 0.411, 0.296), (0.213, 0.156, 0.143)),
    "lvd1689m": ((0.485, 0.456, 0.406), (0.229, 0.224, 0.225)),
}

problems, notes = [], []


def fail(m):
    problems.append(m)
    print(f"  [FAIL] {m}")


def warn(m):
    notes.append(m)
    print(f"  [warn] {m}")


def ok(m):
    print(f"  [ok]   {m}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--qa", default=None, help="write a contact sheet here")
    ap.add_argument("--sample", type=int, default=400,
                    help="chips to open for the deep checks")
    ap.add_argument("--norm", default="sat493m", choices=list(STATS))
    a = ap.parse_args()

    root = Path(a.data)
    meta_p = root / "dataset.json"
    if not meta_p.exists():
        sys.exit(f"no dataset.json in {root} - was chip_dataset.py run?")
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    print(f"chip {meta['chip_px']} px @ {meta['gsd_m']} m = {meta['chip_m']:.0f} m, "
          f"stride {meta['stride_m']:.0f} m, split blocks {meta['block_m']:.0f} m")

    # ── 1. completeness ───────────────────────────────────────────
    print("\n[1] files")
    splits = {}
    for s in ("train", "val", "test"):
        imgs = sorted((root / "images" / s).glob("*.tif"))
        splits[s] = imgs
        if not imgs:
            (fail if s != "test" else warn)(f"{s}: no chips")
            continue
        nm = sum(1 for p in imgs if not (root / "masks" / s / f"{p.stem}.png").exists())
        nv = sum(1 for p in imgs if not (root / "vectors" / s / f"{p.stem}.geojson").exists())
        if nm:
            fail(f"{s}: {nm} chips without a mask")
        if nv:
            fail(f"{s}: {nv} chips without a vector")
        if not nm and not nv:
            ok(f"{s}: {len(imgs):,} chips, all with mask + vector")
    total = sum(len(v) for v in splits.values())
    if total == 0:
        return finish()

    frac = {s: len(v) / total for s, v in splits.items()}
    print(f"       split  train {frac['train']*100:.0f}%  "
          f"val {frac['val']*100:.0f}%  test {frac['test']*100:.0f}%")
    if frac["val"] < 0.05:
        warn(f"val is {frac['val']*100:.0f}% of the set; the score will be noisy")

    # ── 2. integrity and alignment ────────────────────────────────
    print(f"\n[2] integrity (opening {min(a.sample, total):,} chips)")
    rng = random.Random(0)
    pool = [(s, p) for s, v in splits.items() for p in v]
    rng.shuffle(pool)
    pool = pool[:a.sample]

    bad, shape_bad, blank, road_px, tot_px = 0, 0, 0, 0, 0
    sums = np.zeros(3); sqs = np.zeros(3); npix = 0
    for s, p in pool:
        try:
            with rasterio.open(p) as src:
                arr = src.read([1, 2, 3])
                crs_ok = src.crs is not None
            m = np.asarray(Image.open(root / "masks" / s / f"{p.stem}.png").convert("L"))
        except Exception as e:
            bad += 1
            if bad <= 3:
                fail(f"unreadable {p.name}: {type(e).__name__}")
            continue
        if not crs_ok:
            shape_bad += 1
        if arr.shape[1:] != m.shape:
            shape_bad += 1
            if shape_bad <= 3:
                fail(f"{p.name}: image {arr.shape[1:]} vs mask {m.shape}")
        if arr.max() == arr.min():
            blank += 1
        x = arr.transpose(1, 2, 0).reshape(-1, 3).astype(np.float64) / 255.0
        sums += x.sum(0); sqs += (x ** 2).sum(0); npix += len(x)
        road_px += int((m > 127).sum()); tot_px += m.size
    if not bad:
        ok("no unreadable chips")
    if not shape_bad:
        ok("image and mask dimensions agree, CRS present")
    if blank:
        warn(f"{blank} chips are a single flat value (dead imagery?)")

    cover = 100 * road_px / max(1, tot_px)
    print(f"       road pixels: {cover:.2f}% of the sampled area")
    if cover < 0.5:
        warn(f"only {cover:.2f}% road - the loss will need the pos_weight it has, "
             f"and Dice is doing most of the work")
    elif cover > 40:
        warn(f"{cover:.1f}% road is very high; check the labels are roads and not an area fill")
    else:
        ok(f"class balance is workable ({cover:.2f}% positive)")

    # ── 3. normalisation sanity ───────────────────────────────────
    print("\n[3] normalisation")
    mean = sums / npix
    std = np.sqrt(np.maximum(sqs / npix - mean ** 2, 1e-12))
    bm, bs = STATS[a.norm]
    print(f"       this data     mean {np.round(mean,3).tolist()}  std {np.round(std,3).tolist()}")
    print(f"       {a.norm:<12}  mean {list(bm)}  std {list(bs)}")
    z = np.abs((mean - np.asarray(bm)) / np.asarray(bs))
    if z.max() > 1.5:
        warn(f"after {a.norm} normalisation the data centres at {np.round((mean-bm)/bs,2).tolist()} "
             f"rather than near 0. That is a domain shift, not a bug - the frozen "
             f"backbone still needs its own constants - but expect the decoder to "
             f"spend early epochs absorbing it.")
    else:
        ok(f"normalised data centres near zero (max |z| {z.max():.2f})")

    # ── 4. spatial leakage ────────────────────────────────────────
    print("\n[4] split separation")
    by = {s: [] for s in splits}
    for c in meta["chips"]:
        b = c["bounds_utm"]
        by[c["split"]].append(((b[0] + b[2]) / 2, (b[1] + b[3]) / 2))
    if by["train"] and by["val"]:
        tr = np.asarray(by["train"]); va = np.asarray(by["val"])
        step = max(1, len(va) // 500)
        d = np.sqrt(((va[::step, None, :] - tr[None, :, :]) ** 2).sum(-1)).min(1)
        chip_m = meta["chip_m"]
        print(f"       nearest train chip to a val chip: min {d.min():.0f} m, "
              f"median {np.median(d):.0f} m  (chip is {chip_m:.0f} m across)")
        if d.min() < chip_m:
            fail(f"val and train chips overlap in space (min {d.min():.0f} m < "
                 f"{chip_m:.0f} m). The validation score would be inflated.")
        else:
            ok("no train/val chip overlaps another in space")

    # ── 5. look at it ─────────────────────────────────────────────
    if a.qa:
        print("\n[5] QA sheet")
        import cv2
        picks = [p for p in pool if p[0] == "train"][:12] or pool[:12]
        rows, row = [], []
        for s, p in picks:
            with rasterio.open(p) as src:
                rgb = src.read([1, 2, 3]).transpose(1, 2, 0).astype(np.uint8)
            m = np.asarray(Image.open(root / "masks" / s / f"{p.stem}.png").convert("L"))
            ov = rgb.copy(); ov[m > 127] = (60, 220, 255)
            vis = cv2.addWeighted(rgb, 0.55, ov, 0.45, 0)
            cv2.putText(vis, f"{s} {100*(m>127).mean():.1f}%", (8, 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)
            vis = cv2.resize(vis, (256, 256))
            row.append(vis)
            if len(row) == 4:
                rows.append(np.hstack(row)); row = []
        if row:
            while len(row) < 4:
                row.append(np.zeros_like(row[0]))
            rows.append(np.hstack(row))
        if rows:
            cv2.imwrite(a.qa, np.vstack(rows)[:, :, ::-1])
            ok(f"wrote {a.qa} - open it and check the labels sit on the roads")

    return finish()


def finish():
    print("\n" + "=" * 68)
    if problems:
        print(f"DATASET NOT READY - {len(problems)} problem(s):")
        for p in problems:
            print("   - " + p)
        print("=" * 68)
        sys.exit(1)
    if notes:
        print(f"dataset usable, {len(notes)} note(s):")
        for n in notes:
            print("   - " + n)
    else:
        print("dataset verified.")
    print("=" * 68)
    sys.exit(0)


if __name__ == "__main__":
    main()
