"""Merge the hand-drawn label clips into one training set.

Two things this has to get right.

AI output is not a label. Several files carry det_id/class/confidence - they are
the model's own predictions, and training on them teaches the model what it
already believes. They are excluded and listed in the report.

Touching buildings must be separated WITHOUT moving them. The labels were cut
neighbour-by-neighbour with no gap, so adjacent buildings share an edge exactly;
rasterised, a terrace becomes one blob and the model learns to merge. The fix
is surgical: the shared edge itself is buffered by GAP/2 and subtracted, so a
thin gap opens along the party wall and every free edge stays exactly where it
was drawn. Shrinking each polygon instead would move every edge and break the
alignment with the imagery.
"""
import os
import time

import fiona
import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
from shapely import make_valid
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

ROOT = r"D:/DSM/Labeled Dataset"
OUT = "D:/GEO_SERVE/_kb/training_labels.gpkg"
REPORT = "D:/GEO_SERVE/_kb/training_labels_report.csv"
M = 32644

GAP = 0.30            # m of separation opened between touching buildings
MIN_AREA = 4.0        # m2 - smaller than this is not a building anyone drew
MAX_AREA = 20000.0    # m2 - larger is a block, not a building
DUP_IOU = 0.80        # two polygons this similar are the same building twice
SLIVER_MIN = 1.0      # m2 - drop fragments left by the cut
AI_FIELDS = {"det_id", "confidence", "class"}     # marks model output
BUILD_PAT = ("build", "bulding", "pic1")          # pic1 = Drone_6 buildings; "bulding" is a typo in the data
OPEN_PAT = ("openplot", "openplat", "open_plot")


def classify(path, layer, fields):
    f = {str(x).lower() for x in fields}
    is_ai = len(AI_FIELDS & f) >= 2
    # strip punctuation and spaces first: the folder has "Open _Plot.shp" and
    # "Bulding_layerclip12.shp", and a plain substring test misses both
    import re as _re
    hay = _re.sub(r"[^a-z0-9]+", "", (os.path.basename(path) + " " + layer).lower())
    if any(k in hay for k in OPEN_PAT):
        return "open_plot_ai" if is_ai else "open_plot"
    if any(k in hay for k in BUILD_PAT) or is_ai:
        # the model output in this folder is verified and filtered, so it is
        # kept as labels - tagged, so the mix stays auditable later
        return "building_ai" if is_ai else "building"
    return "unknown"


def load_all():
    rows, manifest = [], []
    for dp, _, fs in os.walk(ROOT):
        for f in sorted(fs):
            if not f.lower().endswith((".gpkg", ".shp")):
                continue
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, ROOT)
            try:
                layers = fiona.listlayers(p)
            except Exception as e:
                manifest.append({"file": rel, "layer": "", "kind": "unreadable",
                                 "n": 0, "note": str(e)[:60]})
                continue
            for L in layers:
                try:
                    info = pyogrio.read_info(p, layer=L)
                except Exception:
                    continue
                if not info.get("geometry_type"):
                    continue
                flds = info.get("fields")
                kind = classify(p, L, [] if flds is None else list(flds))
                n = int(info.get("features") or 0)
                manifest.append({"file": rel, "layer": L, "kind": kind, "n": n,
                                 "crs": str(info.get("crs"))})
                if kind not in ("building", "open_plot", "building_ai", "open_plot_ai") or not n:
                    continue
                g = gpd.read_file(p, layer=L)
                if g.crs is None:
                    g.set_crs(4326, inplace=True)
                g = g.to_crs(M)
                g = g[["geometry"]].copy()
                g["kind"] = kind.replace("_ai", "")
                g["label_source"] = "ai_verified" if kind.endswith("_ai") else "hand"
                g["src"] = f"{rel}:{L}"
                rows.append(g)
    return rows, pd.DataFrame(manifest)


def clean(g):
    """valid, single-part, sane size"""
    g = g.copy()
    g["geometry"] = [make_valid(x) if x is not None else None for x in g.geometry]
    g = g[g.geometry.notna()]
    g = g.explode(index_parts=False)
    g = g[g.geometry.geom_type == "Polygon"]
    g = g[g.geometry.is_valid & ~g.geometry.is_empty]
    a = g.geometry.area
    g = g[(a >= MIN_AREA) & (a <= MAX_AREA)]
    return g.reset_index(drop=True)


def dedupe(g):
    """the clips overlap, so the same building is drawn twice at the seams"""
    geoms = list(g.geometry.values)
    tree = STRtree(geoms)
    drop = set()
    for i, gi in enumerate(geoms):
        if i in drop:
            continue
        for j in tree.query(gi):
            j = int(j)
            if j <= i or j in drop:
                continue
            gj = geoms[j]
            inter = gi.intersection(gj).area
            if inter <= 0:
                continue
            iou = inter / (gi.area + gj.area - inter)
            if iou >= DUP_IOU:
                drop.add(j if gj.area <= gi.area else i)
                if i in drop:
                    break
    return g.drop(index=list(drop)).reset_index(drop=True), len(drop)


def separate(g, gap=GAP):
    """Open a gap along shared edges only; free edges are untouched."""
    geoms = list(g.geometry.values)
    tree = STRtree(geoms)
    cuts, touching, overlapping = [], 0, 0
    for i, gi in enumerate(geoms):
        for j in tree.query(gi):
            j = int(j)
            if j <= i:
                continue
            gj = geoms[j]
            if not gi.intersects(gj):
                continue
            inter = gi.intersection(gj)
            if inter.is_empty:
                continue
            if getattr(inter, "area", 0) > 1e-9:
                overlapping += 1
                cuts.append(inter)                       # real overlap
            else:
                touching += 1
                cuts.append(inter)                       # shared edge (a line)
    if not cuts:
        return g, 0, 0, 0.0
    cut = unary_union(cuts).buffer(gap / 2.0, cap_style=2, join_style=2)
    before = g.geometry.area.sum()
    out = []
    for geom in g.geometry.values:
        d = geom.difference(cut)
        if d.is_empty:
            out.append(None)
            continue
        if d.geom_type == "MultiPolygon":              # keep the main body
            parts = [p for p in d.geoms if p.area >= SLIVER_MIN]
            if not parts:
                out.append(None)
                continue
            d = max(parts, key=lambda p: p.area) if len(parts) == 1 else MultiPolygon(parts)
            if d.geom_type == "MultiPolygon":
                d = max(d.geoms, key=lambda p: p.area)
        out.append(d if d.area >= MIN_AREA else None)
    g = g.assign(geometry=out)
    lost = int(g.geometry.isna().sum())
    g = g[g.geometry.notna()].reset_index(drop=True)
    after = g.geometry.area.sum()
    return g, touching, overlapping, 100 * (before - after) / max(before, 1e-9)


def main():
    t0 = time.time()
    rows, man = load_all()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    man.to_csv(REPORT, index=False)
    print(man.groupby("kind")["n"].agg(["count", "sum"]).to_string(), flush=True)
    ai = man[man["kind"] == "ai_output"]
    if len(ai):
        print(f"\nexcluded as model output (det_id/confidence present): "
              f"{int(ai['n'].sum())} features in {len(ai)} layers", flush=True)
        for _, r in ai.iterrows():
            print(f"    {r['file']}:{r['layer']}  {r['n']}", flush=True)

    allg = gpd.GeoDataFrame(pd.concat(rows, ignore_index=True), crs=M)
    print(f"\nloaded {len(allg):,} hand-drawn polygons", flush=True)

    out_layers = {}
    for kind in ("building", "open_plot"):
        g = allg[allg["kind"] == kind]
        if not len(g):
            continue
        n0 = len(g)
        g = clean(g)
        n1 = len(g)
        g, ndup = dedupe(g)
        n2 = len(g)
        if kind == "building":
            g, touch, over, lost_pct = separate(g)
            print(f"\n{kind}: {n0:,} -> clean {n1:,} -> dedupe {n2:,} "
                  f"(-{ndup} duplicates) -> {len(g):,} after separation", flush=True)
            print(f"  shared edges cut: {touch:,} pairs | overlapping pairs: {over:,}", flush=True)
            print(f"  area lost to the {GAP*100:.0f} cm gap: {lost_pct:.2f}%", flush=True)
        else:
            print(f"\n{kind}: {n0:,} -> clean {n1:,} -> dedupe {n2:,} (-{ndup})", flush=True)
        g["area_m2"] = g.geometry.area.round(2)
        g["inst_id"] = np.arange(1, len(g) + 1)
        if "label_source" in g.columns:
            print(f"  source: {g['label_source'].value_counts().to_dict()}", flush=True)
        a = g["area_m2"]
        print(f"  area m2: min {a.min():.0f}  p25 {a.quantile(.25):.0f}  "
              f"median {a.median():.0f}  p75 {a.quantile(.75):.0f}  max {a.max():.0f}", flush=True)
        # did any still touch after the cut?
        tree = STRtree(list(g.geometry.values))
        still = sum(1 for i, gi in enumerate(g.geometry.values)
                    for j in tree.query(gi) if int(j) > i and gi.intersects(g.geometry.values[int(j)]))
        print(f"  still touching after separation: {still}", flush=True)
        out_layers[kind] = g

    if os.path.exists(OUT):
        os.remove(OUT)
    for k, g in out_layers.items():
        g.to_file(OUT, layer=k, driver="GPKG")
    print(f"\nwrote {OUT}")
    for k, g in out_layers.items():
        print(f"  {k}: {len(g):,}")
    print(f"      {REPORT}  ({time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
