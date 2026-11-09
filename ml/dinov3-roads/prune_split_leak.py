"""Remove train/val/test spatial overlap from an existing chip set.

chip_dataset.py now discards chips that straddle a split-block boundary, so a
freshly built set has no leak. This repairs a set built before that fix
without re-reading the orthomosaic - seconds instead of an hour and a half.

Validation and test chips that come within one chip-width of any training
chip are dropped; the training set is left intact, because it is the larger
and more valuable half and because dropping from val costs only a little
evaluation precision.

    python prune_split_leak.py --data C:/ongole_roads            # report only
    python prune_split_leak.py --data C:/ongole_roads --apply    # delete them
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np


def centres(chips, split):
    out = [(c["name"], ((c["bounds_utm"][0] + c["bounds_utm"][2]) / 2,
                        (c["bounds_utm"][1] + c["bounds_utm"][3]) / 2))
           for c in chips if c["split"] == split]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--apply", action="store_true", help="actually delete the files")
    ap.add_argument("--margin", type=float, default=1.0,
                    help="separation required, in chip widths (default 1.0)")
    a = ap.parse_args()

    root = Path(a.data)
    meta_p = root / "dataset.json"
    if not meta_p.exists():
        sys.exit(f"no dataset.json in {root}")
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    chip_m = float(meta["chip_m"])
    need = chip_m * a.margin

    tr = centres(meta["chips"], "train")
    if not tr:
        sys.exit("no training chips")
    T = np.asarray([c for _, c in tr])
    print(f"chip {chip_m:.0f} m; requiring {need:.0f} m between a train chip "
          f"and any val/test chip")

    drop = []
    for split in ("val", "test"):
        rows = centres(meta["chips"], split)
        if not rows:
            continue
        P = np.asarray([c for _, c in rows])
        # Chunked so a large set does not build an N x M matrix at once.
        d = np.empty(len(P))
        for i in range(0, len(P), 512):
            blk = P[i:i + 512]
            d[i:i + 512] = np.sqrt(((blk[:, None, :] - T[None, :, :]) ** 2).sum(-1)).min(1)
        bad = [rows[i][0] for i in np.nonzero(d < need)[0]]
        print(f"  {split}: {len(rows):,} chips, nearest train chip "
              f"min {d.min():.0f} m / median {np.median(d):.0f} m -> "
              f"{len(bad):,} too close ({100*len(bad)/len(rows):.0f}%)")
        drop += [(split, n) for n in bad]

    if not drop:
        print("\nnothing to prune; the splits are already separated.")
        return

    print(f"\n{len(drop):,} chips would be removed")
    if not a.apply:
        print("dry run - pass --apply to delete them")
        return

    removed = 0
    for split, name in drop:
        for p in (root / "images" / split / f"{name}.tif",
                  root / "images" / split / f"{name}.png",
                  root / "masks" / split / f"{name}.png",
                  root / "vectors" / split / f"{name}.geojson",
                  root / "vectors" / split / f"{name}_px.geojson"):
            if p.exists():
                p.unlink()
                removed += 1
    gone = {n for _, n in drop}
    meta["chips"] = [c for c in meta["chips"] if c["name"] not in gone]
    counts = {s: sum(1 for c in meta["chips"] if c["split"] == s)
              for s in ("train", "val", "test")}
    meta["counts"] = counts
    meta["pruned_for_split_leak"] = len(gone)
    meta_p.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(f"deleted {removed:,} files for {len(gone):,} chips")
    print(f"now: train {counts['train']:,}  val {counts['val']:,}  test {counts['test']:,}")
    print("re-run verify_dataset.py to confirm.")


if __name__ == "__main__":
    main()
