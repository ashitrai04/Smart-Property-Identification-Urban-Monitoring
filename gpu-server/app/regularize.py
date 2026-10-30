"""Building footprint regularization: blobby masks -> clean, right-angled polygons.

For each footprint (in a metric CRS):
  1. simplify (Douglas-Peucker) to drop pixel stair-steps;
  2. find its dominant orientation from the length-weighted edge angles (mod 90°),
     optionally pulled towards the neighbours' orientation so a street of houses lines up;
  3. rotate to that frame, classify every edge as horizontal or vertical, merge runs of
     the same direction, drop jogs shorter than `min_edge`, and rebuild the outline from
     the intersections of consecutive H / V lines — a rectilinear polygon;
  4. rotate back, and keep the result only if it still matches the original shape
     (IoU >= min_iou); otherwise fall back to the minimum rotated rectangle when the
     shape is nearly rectangular, else to the simplified original.
"""
import math

import numpy as np
import shapely
from shapely import affinity
from shapely.geometry import Polygon


def _angles(ring):
    a = np.asarray(ring)
    d = np.diff(a, axis=0)
    ln = np.hypot(d[:, 0], d[:, 1])
    th = np.arctan2(d[:, 1], d[:, 0])
    return th, ln


def dominant_angle(poly):
    """Length-weighted dominant direction (radians, in [0, pi/2))."""
    th, ln = _angles(poly.exterior.coords)
    if not ln.sum():
        return 0.0
    # angles are 90°-periodic for rectilinear shapes: average on the 4θ circle
    c, s = (ln * np.cos(4 * th)).sum(), (ln * np.sin(4 * th)).sum()
    return (math.atan2(s, c) / 4) % (math.pi / 2)


def _rectilinear(ring, min_edge):
    """Rotated-frame ring -> rectilinear ring (list of (x, y)) or None."""
    pts = np.asarray(ring)[:-1]
    n = len(pts)
    if n < 3:
        return None
    segs = []                                     # (kind, coord, length)
    for i in range(n):
        p, q = pts[i], pts[(i + 1) % n]
        dx, dy = q[0] - p[0], q[1] - p[1]
        L = math.hypot(dx, dy)
        if L == 0:
            continue
        if abs(dx) >= abs(dy):
            segs.append(["H", (p[1] + q[1]) / 2, L])
        else:
            segs.append(["V", (p[0] + q[0]) / 2, L])
    # merge consecutive same-direction runs (length-weighted position)
    changed = True
    while changed and len(segs) > 4:
        changed = False
        out = []
        for s in segs:
            if out and out[-1][0] == s[0]:
                k, c, L = out[-1]
                out[-1] = [k, (c * L + s[1] * s[2]) / (L + s[2]), L + s[2]]
                changed = True
            else:
                out.append(s)
        if len(out) > 1 and out[0][0] == out[-1][0]:
            k, c, L = out[-1]
            out[0] = [k, (c * L + out[0][1] * out[0][2]) / (L + out[0][2]), L + out[0][2]]
            out.pop()
            changed = True
        # drop short jogs: a short segment between two segments of the other direction
        segs2 = []
        for i, s in enumerate(out):
            if s[2] < min_edge and len(out) > 4:
                changed = True
                continue
            segs2.append(s)
        segs = segs2
    if len(segs) < 4 or len(segs) % 2:
        return None
    # rebuild vertices at the intersections of consecutive H / V lines
    verts = []
    for i in range(len(segs)):
        a, b = segs[i], segs[(i + 1) % len(segs)]
        if a[0] == b[0]:
            return None
        x = a[1] if a[0] == "V" else b[1]
        y = a[1] if a[0] == "H" else b[1]
        verts.append((x, y))
    verts.append(verts[0])
    return verts


def _valid_poly(g):
    """Largest valid polygon of g (or None)."""
    if g is None or g.is_empty:
        return None
    if not g.is_valid:
        g = shapely.make_valid(g)
    parts = [p for p in getattr(g, "geoms", [g]) if p.geom_type == "Polygon" and p.area > 0]
    return max(parts, key=lambda p: p.area) if parts else None


def regularize(poly, angle=None, simplify=0.6, min_edge=1.5, min_iou=0.72, rect_ratio=0.8, open_m=0.6):
    """One footprint -> (regularized polygon, method)."""
    if poly is None or poly.is_empty or poly.area < 4:
        return poly, "skip"
    poly = _valid_poly(poly)                     # model output can self-intersect
    if poly is None:
        return None, "skip"
    # morphological opening (in metres): trims tree / shadow bumps before squaring up
    base = poly.buffer(0)
    if open_m:
        o = base.buffer(-open_m, join_style=2).buffer(open_m, join_style=2)
        if o.geom_type == "MultiPolygon":
            o = max(o.geoms, key=lambda g: g.area)
        if not o.is_empty and o.area > 0.6 * base.area:
            base = o
    base = base.simplify(simplify, preserve_topology=True)
    if base.geom_type != "Polygon" or base.is_empty:
        return poly, "skip"
    th = dominant_angle(base) if angle is None else angle
    c = base.centroid
    rot = affinity.rotate(base, -th, origin=c, use_radians=True)
    ring = _rectilinear(rot.exterior.coords, min_edge)
    if ring:
        cand = Polygon(ring)
        if not cand.is_valid:
            cand = cand.buffer(0)
        if cand.geom_type == "MultiPolygon":
            cand = max(cand.geoms, key=lambda g: g.area)
        cand = affinity.rotate(cand, th, origin=c, use_radians=True)
        if not cand.is_empty and cand.area > 0:
            iou = cand.intersection(poly).area / cand.union(poly).area
            if iou >= min_iou:
                return cand, "rectilinear"
    mrr = poly.minimum_rotated_rectangle
    if mrr.area and poly.area / mrr.area >= rect_ratio:
        return mrr, "rectangle"
    return base, "simplified"


def regularize_all(geoms, neighbour_m=25.0, snap_deg=12.0, **kw):
    """Regularize many footprints; each one's angle is snapped to the median angle of its
    neighbours when they agree within `snap_deg` (so houses along a street line up)."""
    geoms = list(geoms)
    geoms = [_valid_poly(g) for g in geoms]
    angles = np.array([dominant_angle(g.simplify(0.6)) if g is not None else 0.0 for g in geoms])
    tree = shapely.STRtree([g if g is not None else shapely.Point() for g in geoms])
    out, methods = [], []
    for i, g in enumerate(geoms):
        th = angles[i]
        if g is not None and not g.is_empty:
            nb = [j for j in tree.query(g.buffer(neighbour_m)) if j != i]
            if nb:
                # circular median on the 90°-periodic circle
                d = ((angles[nb] - th + math.pi / 4) % (math.pi / 2)) - math.pi / 4
                med = float(np.median(d))
                if abs(med) <= math.radians(snap_deg):
                    th = (th + med) % (math.pi / 2)
        r, m = regularize(g, angle=th, **kw)
        out.append(r)
        methods.append(m)
    return out, methods
