"""Measure a file of footprints on this machine, without the network.

The server exists so the HiSup laptop can ask for heights over HTTP. But when
the footprints are already on this machine - an exported GeoPackage, a label
set, the 27k drone labels - going through HTTP to talk to yourself is pointless
and slower. Same measurement code, same numbers, read and written as files.

    python measure_file.py labels.gpkg out_heights.gpkg
    python measure_file.py labels.gpkg out.gpkg --layer building --csv out.csv
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inp", help="footprints: .gpkg, .geojson, .shp")
    ap.add_argument("out", help="where to write them with heights attached")
    ap.add_argument("--layer", default=None, help="layer name, for a multi-layer file")
    ap.add_argument("--csv", default=None, help="also write a table with no geometry")
    ap.add_argument("--edge-buffer", type=float, default=None,
                    help="metres to shrink each footprint before sampling")
    ap.add_argument("--limit", type=int, default=None, help="stop after N (for a trial run)")
    args = ap.parse_args()

    import geopandas as gpd

    from app.heights import measure_many, summarise
    from app.tiles import INDEX

    s = INDEX.build()
    if not s["dsm"].get("count"):
        sys.exit("no DSM clips indexed - run selfcheck.py to see why")
    print(f"[*] {s['dsm']['count']} DSM clips, {s['dtm'].get('count', 0)} DTM clips")

    g = gpd.read_file(args.inp, layer=args.layer) if args.layer else gpd.read_file(args.inp)
    if args.limit:
        g = g.iloc[:args.limit].copy()
    print(f"[*] {len(g)} footprints from {Path(args.inp).name}")
    if g.empty:
        sys.exit("nothing to measure")

    wgs = g.to_crs(4326)
    t0 = time.time()
    rows = []
    step = max(1, len(wgs) // 20)
    for i in range(0, len(wgs), step):
        part = list(wgs.geometry.iloc[i:i + step])
        rows.extend(measure_many(part, edge_buffer=args.edge_buffer))
        done = min(len(wgs), i + step)
        el = time.time() - t0
        if el > 0:
            rate = done / el
            eta = (len(wgs) - done) / rate / 60 if rate else 0
            print(f"    {done}/{len(wgs)}  {rate:.0f}/s  eta {eta:.1f} min")
    dt = time.time() - t0

    # Union of keys, not rows[0]: a footprint that failed to measure returns a
    # shorter dict, and keying off the first row would drop whole columns.
    keys = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    for k in keys:
        g[k] = [r.get(k) for r in rows]

    out = Path(args.out)
    if out.suffix.lower() == ".gpkg":
        g.to_file(out, driver="GPKG", layer=out.stem)
    else:
        g.to_file(out)
    print(f"\n[+] wrote {out}  ({len(g)} features, {dt / 60:.1f} min)")

    if args.csv:
        g.drop(columns=["geometry"]).to_csv(args.csv, index=False)
        print(f"[+] wrote {args.csv}")

    summ = summarise(rows)
    print("\n" + "=" * 52)
    print(f"  measured            : {summ['n_measured']} of {summ['n_input']}")
    print(f"  mean / median height: {summ['mean_height_m']} / {summ['median_height_m']} m")
    print(f"  tallest             : {summ['max_height_m']} m")
    if summ.get("dtm_vs_ring_median_m") is not None:
        print(f"  DTM vs ring ground  : {summ['dtm_vs_ring_median_m']} m median "
              f"disagreement (under ~0.5 m is healthy)")
    print("  storeys:")
    for k, v in summ["floors"].items():
        print(f"    {k:<22} {v:>6}")
    if len(summ["quality"]) > 1:
        print("  not measured:")
        for k, v in sorted(summ["quality"].items()):
            if k != "good":
                print(f"    {k:<22} {v:>6}")
    print("=" * 52)


if __name__ == "__main__":
    main()
