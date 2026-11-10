"""Measure post-processing choices on a saved probability map, instead of guessing.

Every step in the usual mask-to-polygon chain is defended somewhere in the
literature and none of it is free. Douglas-Peucker is the de facto simplifier
and is also reported to "increase defects and irregularity" when pushed
(ISPRS Annals XI-2-2026). Morphological opening and closing are what the
city-scale road extraction pipelines use before skeletonising (arXiv
1904.09901). Smoothing high-curvature regions weakly is what preserves sharp
features (ISPRS Archives XLVIII-4-W18-2025).

Which of those helps THIS model on THIS ground is an empirical question, and
the answer differs by imagery, by label style, and by what you are optimising
for. So this runs the variants over a saved probability.tif and scores each
against the labels, on two metrics that disagree with each other on purpose:

    F1          pixel agreement. Dominated by road interiors. A blobby road
                with soft edges still scores well, which is exactly why it is
                not sufficient on its own.
    boundary F1 outline agreement within a tolerance. This is the one that
                moves when shapes get sharper, and the one to optimise if the
                complaint is "blobby".

It also reports vertex count, because a polygon that scores the same with a
tenth of the vertices is a better polygon - smaller files, faster GIS, and an
outline a surveyor can actually read.

    python tune_postproc.py --prob C:/param_results/param-vitl-1km/probability.tif \\
        --labels <path>/centre_part_Rd.shp
"""
import argparse
import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np
import rasterio
import shapely
from rasterio.features import rasterize, shapes
from scipy import ndimage


def boundary_f1(pred, truth, tol=2):
    """Agreement between the two outlines, within `tol` pixels.

    Not IoU on a dilated band - that rewards a thick prediction. This is the
    symmetric precision/recall of one outline against the other's tolerance
    band, which is what the segmentation literature means by boundary F1.
    """
    def edges(m):
        return m ^ ndimage.binary_erosion(m, np.ones((3, 3), bool))

    pe, te = edges(pred), edges(truth)
    k = np.ones((2 * tol + 1, 2 * tol + 1), bool)
    pe_d = ndimage.binary_dilation(pe, k)
    te_d = ndimage.binary_dilation(te, k)
    tp_p = int((pe & te_d).sum())
    tp_t = int((te & pe_d).sum())
    np_, nt = int(pe.sum()), int(te.sum())
    prec = tp_p / np_ if np_ else 0.0
    rec = tp_t / nt if nt else 0.0
    return (2 * prec * rec / (prec + rec)) if prec + rec else 0.0


def pixel_scores(pred, truth):
    tp = int((pred & truth).sum())
    fp = int((pred & ~truth).sum())
    fn = int((~pred & truth).sum())
    pr = tp / (tp + fp) if tp + fp else 0.0
    rc = tp / (tp + fn) if tp + fn else 0.0
    return {"f1": (2 * pr * rc / (pr + rc)) if pr + rc else 0.0,
            "precision": pr, "recall": rc,
            "iou": tp / (tp + fp + fn) if tp + fp + fn else 0.0}


def area_open(mask, min_px):
    """Drop connected components below min_px. Specks, not roads."""
    lab, n = ndimage.label(mask)
    if n == 0:
        return mask
    sizes = ndimage.sum(mask, lab, range(1, n + 1))
    keep = np.zeros(n + 1, bool)
    keep[1:] = sizes >= min_px
    return keep[lab]


def fill_holes(mask, max_px):
    """Fill enclosed holes below max_px - a carriageway has no courtyards."""
    filled = ndimage.binary_fill_holes(mask)
    holes = filled & ~mask
    lab, n = ndimage.label(holes)
    if n == 0:
        return mask
    sizes = ndimage.sum(holes, lab, range(1, n + 1))
    small = np.zeros(n + 1, bool)
    small[1:] = sizes <= max_px
    return mask | small[lab]


def polygonize(mask, transform, simplify_m=0.0, chaikin=0):
    """Mask -> shapely polygons, optionally simplified and corner-cut."""
    geoms = [shapely.geometry.shape(g) for g, v in
             shapes(mask.astype(np.uint8), mask=mask, transform=transform) if v == 1]
    if simplify_m > 0:
        # preserve_topology: a road network that self-intersects after
        # simplification is worse than one with a few more vertices.
        geoms = [g.simplify(simplify_m, preserve_topology=True) for g in geoms]
    for _ in range(chaikin):
        geoms = [_chaikin(g) for g in geoms]
    return [g for g in geoms if not g.is_empty and g.is_valid]


def _chaikin(poly):
    """One round of Chaikin corner cutting on every ring.

    Replaces each corner with two points a quarter in from either side. It
    rounds corners, which is right for a carriageway edge following a kerb and
    wrong for a building, and it multiplies vertices by about 2 - which is why
    it belongs after simplification, not before.
    """
    def cut(coords):
        pts = list(coords)[:-1]
        if len(pts) < 3:
            return list(coords)
        out = []
        for i in range(len(pts)):
            p, q = np.asarray(pts[i]), np.asarray(pts[(i + 1) % len(pts)])
            out.append(tuple(0.75 * p + 0.25 * q))
            out.append(tuple(0.25 * p + 0.75 * q))
        out.append(out[0])
        return out

    try:
        ext = cut(poly.exterior.coords)
        ints = [cut(r.coords) for r in poly.interiors]
        g = shapely.geometry.Polygon(ext, ints)
        return g if g.is_valid else poly.buffer(0)
    except Exception:
        return poly


def nvertices(geoms):
    return sum(len(g.exterior.coords) + sum(len(r.coords) for r in g.interiors)
               for g in geoms)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prob", required=True, help="probability.tif from infer_area.py")
    ap.add_argument("--labels", required=True)
    ap.add_argument("--out", default=None, help="write the table as JSON")
    a = ap.parse_args()

    import geopandas as gpd

    src = rasterio.open(a.prob)
    prob = src.read(1).astype(np.float32)
    gsd = abs(src.transform.a)
    px_m2 = gsd * gsd
    lab = gpd.read_file(a.labels).to_crs(src.crs)
    truth = rasterize([(shapely.unary_union(lab.geometry.values), 1)],
                      out_shape=prob.shape, transform=src.transform,
                      fill=0, dtype="uint8").astype(bool)
    print(f"{prob.shape[1]}x{prob.shape[0]} @ {gsd:.2f} m, "
          f"labels cover {100*truth.mean():.2f}%\n")

    rows = []

    def run(name, mask, simplify_m=0.0, chaikin=0):
        s = pixel_scores(mask, truth)
        s["bf1"] = boundary_f1(mask, truth)
        s["name"] = name
        if simplify_m or chaikin:
            geoms = polygonize(mask, src.transform, simplify_m, chaikin)
            if geoms:
                back = rasterize([(g, 1) for g in geoms], out_shape=prob.shape,
                                 transform=src.transform, fill=0,
                                 dtype="uint8").astype(bool)
                s = pixel_scores(back, truth)
                s["bf1"] = boundary_f1(back, truth)
                s["name"] = name
                s["polys"] = len(geoms)
                s["verts"] = nvertices(geoms)
        rows.append(s)
        print(f"  {name:44s} F1 {s['f1']:.4f}  bF1 {s['bf1']:.4f}  "
              f"P {s['precision']:.3f} R {s['recall']:.3f}"
              + (f"  {s.get('verts', 0):>6d} verts" if "verts" in s else ""))
        return s

    print("threshold")
    best_t, best = 0.45, -1
    for t in (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60):
        s = run(f"raw @ {t:.2f}", prob >= t)
        if s["f1"] > best:
            best_t, best = t, s["f1"]
    print(f"  -> best threshold {best_t:.2f}\n")

    m0 = prob >= best_t

    print("cleanup (cumulative, kept only if it helps)")
    cur = m0
    for label_, cand in [
        (f"+ drop components < 20 m2", area_open(cur, int(20 / px_m2))),
        (f"+ drop components < 50 m2", area_open(cur, int(50 / px_m2))),
    ]:
        run(label_, cand)
    cur = area_open(m0, int(20 / px_m2))
    run("cleaned, then fill holes < 10 m2", fill_holes(cur, int(10 / px_m2)))
    cur = fill_holes(cur, int(10 / px_m2))
    for r in (1, 2, 3):
        k = np.ones((2 * r + 1, 2 * r + 1), bool)
        run(f"cleaned + close r={r} ({r*gsd:.1f} m)",
            ndimage.binary_closing(cur, k))
    print()

    print("vectorise: simplify, then corner-cut")
    run("polygonise, no simplify", cur, simplify_m=1e-9)
    for s_m in (0.1, 0.2, 0.3, 0.5, 1.0):
        run(f"simplify {s_m:.1f} m", cur, simplify_m=s_m)
    for s_m in (0.2, 0.3):
        for ch in (1, 2):
            run(f"simplify {s_m:.1f} m + chaikin x{ch}", cur,
                simplify_m=s_m, chaikin=ch)

    print()
    base = rows[[r["name"] for r in rows].index(f"raw @ {best_t:.2f}")]
    bestf = max(rows, key=lambda r: r["f1"])
    bestb = max(rows, key=lambda r: r["bf1"])
    print(f"baseline   {base['name']:40s} F1 {base['f1']:.4f}  bF1 {base['bf1']:.4f}")
    print(f"best F1    {bestf['name']:40s} F1 {bestf['f1']:.4f}  "
          f"({bestf['f1']-base['f1']:+.4f})")
    print(f"best bF1   {bestb['name']:40s} bF1 {bestb['bf1']:.4f}  "
          f"({bestb['bf1']-base['bf1']:+.4f})")
    if a.out:
        Path(a.out).write_text(json.dumps(rows, indent=1), encoding="utf-8")
        print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
