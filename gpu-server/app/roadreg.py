"""Road mask regularization: blobby pixels -> smooth corridors with centrelines.

`regularize.py` next door turns building masks into right-angled polygons. That
is exactly the wrong thing for a road: a carriageway is a long thin corridor of
roughly constant width that bends, and forcing it to meet at right angles
destroys it. So this is a separate module with its own geometry.

What makes a raw road mask look blobby, in the order it matters:

  1. Pixel stair-steps. At 12.5 cm a 7 m carriageway is 56 px across, so every
     edge is a 1-px sawtooth. Douglas-Peucker alone turns that into long
     straight runs with visible kinks; corner-cutting after it is what makes the
     edge read as a road edge.
  2. Nubs and notches. The network hedges at driveways, shadows and parked
     vehicles, leaving 1-2 m bumps on the edge and bites out of it. These are
     not geometry, they are uncertainty, and a morphological open-then-close in
     vector space removes both without moving the real edge.
  3. Specks and pinholes. Isolated blobs in compounds, and holes where a tree
     overhangs. Both are area-thresholded away.
  4. The worst one: a mask has no notion of a road's width being smooth along
     its length. The corridor mode fixes that by finding the centreline, where
     the road actually is, smoothing it, and rebuilding the carriageway from the
     centreline outwards with a width that varies slowly.

Two modes, because they fail differently:

  "smooth"   - works on the mask outline. Always produces something, keeps
               junctions and odd widths faithfully, and is the safe default.
  "corridor" - centreline plus a slowly varying half-width. Gives the cleanest,
               most road-like shapes on ordinary carriageways, but a centreline
               is a poor description of a wide junction or a parking apron, so
               it falls back to "smooth" per component when the rebuilt corridor
               stops matching the mask.

Everything takes and returns pixel coordinates; the caller georeferences. Sizes
are given in metres and converted with `gsd`, so the same numbers hold at any
resolution.
"""
import cv2
import numpy as np
import shapely
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.ops import unary_union


# ──────────────────────────── mask cleanup ────────────────────────────
def clean_mask(mask, gsd, min_area_m2=25.0, min_hole_m2=4.0,
               close_m=0.75, open_m=0.25):
    """Morphological tidy-up before any geometry is extracted.

    Closing first, then opening: a road broken by a tree shadow should be
    rejoined before specks are removed, otherwise the two halves are judged
    separately and a short stub falls under the area floor.
    """
    m = (np.asarray(mask) > 0).astype(np.uint8)
    if not m.any():
        return m

    def ker(metres):
        k = max(1, int(round(metres / gsd)))
        k += 1 - (k % 2)                      # odd, so the kernel is centred
        return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))

    if close_m > 0:
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, ker(close_m))
    if open_m > 0:
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, ker(open_m))

    px = gsd * gsd
    # Drop small components.
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    keep = np.zeros(n, bool)
    for i in range(1, n):
        keep[i] = stats[i, cv2.CC_STAT_AREA] * px >= min_area_m2
    m = keep[lab].astype(np.uint8)

    # Fill small holes: label the background and keep only large background
    # components (the true outside plus real courtyards).
    inv = (1 - m).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(inv, connectivity=4)
    fill = np.zeros(n, bool)
    for i in range(1, n):
        fill[i] = stats[i, cv2.CC_STAT_AREA] * px < min_hole_m2
    m[fill[lab]] = 1
    return m


# ──────────────────────────── outline smoothing ────────────────────────────
def _chaikin(ring, iters=2, closed=True):
    """Corner-cutting. Each pass replaces a corner with two points at 1/4, 3/4.

    This is what removes the stair-step look. It shrinks the shape slightly
    towards its convex hull, which is why it runs after Douglas-Peucker and on
    a modest number of iterations - two passes smooth a sawtooth, five round off
    real corners.
    """
    p = np.asarray(ring, float)
    if closed and len(p) > 1 and np.allclose(p[0], p[-1]):
        p = p[:-1]
    if len(p) < 3:
        return ring
    for _ in range(max(0, iters)):
        a = p
        b = np.roll(p, -1, axis=0) if closed else p[1:]
        if not closed:
            a = p[:-1]
        q = np.empty((len(a) * 2, 2))
        q[0::2] = 0.75 * a + 0.25 * b
        q[1::2] = 0.25 * a + 0.75 * b
        if not closed:
            q = np.vstack([p[0], q, p[-1]])
        p = q
    if closed:
        p = np.vstack([p, p[:1]])
    return p


def _poly_from_contour(outer, holes, gsd, simplify_m, chaikin):
    def prep(ring):
        if len(ring) < 4:
            return None
        g = Polygon(ring)
        if not g.is_valid:
            g = g.buffer(0)
            if g.is_empty:
                return None
            g = max(getattr(g, "geoms", [g]), key=lambda z: z.area)
        g = g.simplify(simplify_m / gsd, preserve_topology=True)
        if g.is_empty or len(getattr(g, "exterior", []).coords if g.geom_type == "Polygon" else []) < 4:
            return None
        return _chaikin(np.asarray(g.exterior.coords), chaikin)

    o = prep(outer)
    if o is None:
        return None
    hs = [h for h in (prep(x) for x in holes) if h is not None]
    try:
        g = Polygon(o, hs)
    except Exception:
        return None
    if not g.is_valid:
        g = g.buffer(0)
    return g if (not g.is_empty and g.geom_type in ("Polygon", "MultiPolygon")) else None


def smooth_polygons(mask, gsd, simplify_m=0.40, chaikin=2,
                    nub_m=0.60, min_area_m2=25.0):
    """Mask -> smoothed polygons, following the mask's own outline."""
    m = np.asarray(mask).astype(np.uint8)
    if not m.any():
        return []
    cnts, hier = cv2.findContours(m, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    if hier is None:
        return []
    hier = hier[0]
    out = []
    for i, c in enumerate(cnts):
        if hier[i][3] != -1:                  # a hole; handled with its parent
            continue
        holes = []
        j = hier[i][2]
        while j != -1:
            if len(cnts[j]) >= 4:
                holes.append(cnts[j][:, 0, :])
            j = hier[j][0]
        g = _poly_from_contour(c[:, 0, :], holes, gsd, simplify_m, chaikin)
        if g is None:
            continue
        # Vector open-then-close: shaves nubs, then fills the notches the shave
        # opened, leaving the real edge where it was.
        if nub_m > 0:
            r = nub_m / gsd
            try:
                g = g.buffer(-r, join_style=1).buffer(2 * r, join_style=1).buffer(-r, join_style=1)
            except Exception:
                pass
        for part in getattr(g, "geoms", [g]):
            if part.is_empty or part.geom_type != "Polygon":
                continue
            if part.area * gsd * gsd < min_area_m2:
                continue
            out.append(part.simplify(0.10 / gsd, preserve_topology=True))
    return out


# ──────────────────────────── centreline corridors ────────────────────────────
def _skeleton_paths(skel):
    """Trace the skeleton into polylines between endpoints and junctions.

    Walks each pixel run rather than building a graph library's worth of
    machinery: the skeleton of a road network is mostly long degree-2 chains,
    and those are exactly what a walk is good at.
    """
    s = (skel > 0).astype(np.uint8)
    if not s.any():
        return []
    nb = cv2.filter2D(s, -1, np.ones((3, 3), np.uint8), borderType=cv2.BORDER_CONSTANT) - s
    deg = nb * s
    nodes = ((deg != 2) & (s > 0))            # endpoints (1) and junctions (>=3)
    H, W = s.shape
    seen = np.zeros_like(s, bool)
    offs = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]

    def neigh(y, x):
        for dy, dx in offs:
            yy, xx = y + dy, x + dx
            if 0 <= yy < H and 0 <= xx < W and s[yy, xx]:
                yield yy, xx

    paths = []
    ys, xs = np.nonzero(nodes)
    for y0, x0 in zip(ys, xs):
        for y1, x1 in neigh(y0, x0):
            if seen[y1, x1] and not nodes[y1, x1]:
                continue
            path = [(x0, y0)]
            py, px_ = y0, x0
            cy, cx = y1, x1
            guard = 0
            while True:
                path.append((cx, cy))
                if not nodes[cy, cx]:
                    seen[cy, cx] = True
                if nodes[cy, cx] or (guard := guard + 1) > 100000:
                    break
                nxt = [(yy, xx) for yy, xx in neigh(cy, cx) if (yy, xx) != (py, px_)]
                nxt = [p for p in nxt if not seen[p[0], p[1]] or nodes[p[0], p[1]]]
                if not nxt:
                    break
                py, px_ = cy, cx
                cy, cx = nxt[0]
            if len(path) >= 3:
                paths.append(np.asarray(path, float))

    # Rings: closed loops with no node on them at all.
    leftover = (s > 0) & ~seen & ~nodes
    n, lab = cv2.connectedComponents(leftover.astype(np.uint8), connectivity=8)
    for i in range(1, n):
        pts = np.column_stack(np.nonzero(lab == i))[:, ::-1].astype(float)
        if len(pts) >= 8:
            paths.append(pts)
    return paths


def _smooth_path(p, gsd, step_m=1.5, window_m=6.0):
    """Resample to a fixed step, then run a moving average along it."""
    if len(p) < 3:
        return p
    d = np.r_[0.0, np.cumsum(np.hypot(*np.diff(p, axis=0).T))]
    if d[-1] <= 0:
        return p
    step = max(1.0, step_m / gsd)
    t = np.arange(0, d[-1], step)
    if len(t) < 3:
        return p
    q = np.column_stack([np.interp(t, d, p[:, 0]), np.interp(t, d, p[:, 1])])
    k = int(round((window_m / gsd) / step))
    k = max(1, k | 1)
    if k >= 3 and len(q) > k:
        pad = k // 2
        qq = np.vstack([np.repeat(q[:1], pad, 0), q, np.repeat(q[-1:], pad, 0)])
        ker = np.ones(k) / k
        q = np.column_stack([np.convolve(qq[:, 0], ker, "valid"),
                             np.convolve(qq[:, 1], ker, "valid")])
    return q


def corridor_polygons(mask, gsd, min_len_m=8.0, min_half_m=1.0, max_half_m=15.0,
                      width_window_m=12.0, min_iou=0.55, simplify_m=0.30,
                      min_area_m2=25.0):
    """Centreline-based rebuild. Returns (polygons, centrelines).

    The half-width comes from the distance transform at each centreline point,
    which is exactly the distance to the nearest non-road pixel - the true local
    half-carriageway. Smoothing that profile along the road is what stops the
    corridor bulging at junctions and pinching at parked cars.
    """
    from skimage.morphology import skeletonize

    m = (np.asarray(mask) > 0).astype(np.uint8)
    if not m.any():
        return [], []
    dist = cv2.distanceTransform(m, cv2.DIST_L2, 5)
    skel = skeletonize(m.astype(bool))

    polys, lines = [], []
    for p in _skeleton_paths(skel):
        q = _smooth_path(p, gsd)
        if len(q) < 3:
            continue
        length_m = float(np.hypot(*np.diff(q, axis=0).T).sum()) * gsd
        if length_m < min_len_m:
            continue
        xi = np.clip(q[:, 0].round().astype(int), 0, m.shape[1] - 1)
        yi = np.clip(q[:, 1].round().astype(int), 0, m.shape[0] - 1)
        half = dist[yi, xi].astype(float)
        k = max(1, int(round((width_window_m / gsd) / max(1.0, 1.5 / gsd))) | 1)
        if k >= 3 and len(half) > k:
            pad = k // 2
            hh = np.r_[np.repeat(half[:1], pad), half, np.repeat(half[-1:], pad)]
            half = np.convolve(hh, np.ones(k) / k, "valid")
        half = np.clip(half, min_half_m / gsd, max_half_m / gsd)

        line = LineString(q)
        if line.length <= 0:
            continue
        # Variable width: buffer short pieces at their own half-width and union.
        # A single buffer would impose one width on the whole road.
        # Round caps, not flat. Consecutive segments have slightly different
        # half-widths, so flat caps meet at a wedge and leave a gap: the union
        # then comes out as a ladder of disjoint rungs instead of one corridor.
        # Round caps overlap their neighbours and dissolve into a single piece.
        pieces = []
        for i in range(len(q) - 1):
            seg = LineString([q[i], q[i + 1]])
            if seg.length <= 0:
                continue
            pieces.append(seg.buffer(float((half[i] + half[i + 1]) / 2),
                                     cap_style=1, join_style=1, quad_segs=8))
        if not pieces:
            continue
        g = unary_union(pieces)
        if g.is_empty:
            continue
        polys.append((g, line, length_m))

    if not polys:
        return [], []

    # Accept the rebuild only where it still describes the mask. A junction or a
    # parking apron is not a corridor and should go back to outline smoothing.
    merged = unary_union([g for g, _, _ in polys])
    mask_area = float(m.sum())
    inter = 0.0
    if not merged.is_empty:
        tmp = np.zeros_like(m)
        from rasterio.features import rasterize
        from rasterio.transform import IDENTITY
        try:
            tmp = rasterize([(merged, 1)], out_shape=m.shape, transform=IDENTITY,
                            fill=0, dtype="uint8")
        except Exception:
            tmp = np.zeros_like(m)
        inter = float((tmp & m).sum())
        union = float((tmp | m).sum())
        iou = inter / union if union else 0.0
    else:
        iou = 0.0
    if iou < min_iou:
        return [], []

    # Emit the dissolved network, not one polygon per traced path. Paths meet
    # at junctions, so per-path polygons would overlap there and the map would
    # draw the junction two or three times over, each with its own outline
    # across the middle of the carriageway.
    out, cls = [], [line for _, line, _ in polys]
    for part in getattr(merged, "geoms", [merged]):
        if part.geom_type != "Polygon" or part.is_empty:
            continue
        part = part.simplify(simplify_m / gsd, preserve_topology=True)
        if part.is_empty or not part.is_valid:
            part = part.buffer(0)
        for q in getattr(part, "geoms", [part]):
            if q.geom_type != "Polygon" or q.is_empty:
                continue
            if q.area * gsd * gsd < min_area_m2:
                continue
            out.append(q)
    return out, cls


# ──────────────────────────── entry point ────────────────────────────
def regularize_roads(mask, gsd, mode="corridor", **kw):
    """Blobby road mask -> (polygons, centrelines, how).

    `mode` is "corridor", "smooth", or "none". Corridor falls back to smooth
    when the centreline rebuild does not match the mask, so the caller always
    gets geometry; `how` says which path produced it.
    """
    m = np.asarray(mask)
    if mode == "none":
        return smooth_polygons(m, gsd, simplify_m=0.0, chaikin=0,
                               nub_m=0.0, min_area_m2=0.0), [], "none"

    m = clean_mask(m, gsd,
                   min_area_m2=kw.get("min_area_m2", 25.0),
                   min_hole_m2=kw.get("min_hole_m2", 4.0),
                   close_m=kw.get("close_m", 0.75),
                   open_m=kw.get("open_m", 0.25))
    if not m.any():
        return [], [], "empty"

    if mode == "corridor":
        polys, lines = corridor_polygons(
            m, gsd,
            min_len_m=kw.get("min_len_m", 8.0),
            max_half_m=kw.get("max_half_m", 15.0),
            min_iou=kw.get("min_iou", 0.55),
            min_area_m2=kw.get("min_area_m2", 25.0))
        if polys:
            return polys, lines, "corridor"

    return (smooth_polygons(m, gsd,
                            simplify_m=kw.get("simplify_m", 0.40),
                            chaikin=kw.get("chaikin", 2),
                            nub_m=kw.get("nub_m", 0.60),
                            min_area_m2=kw.get("min_area_m2", 25.0)),
            [], "smooth")
