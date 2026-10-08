"""Water bodies checked against the imagery itself.

SegFormer's water class fires on things that are not water — dry beige fields,
solar panels, factory roofs, scrub — and often catches only a sliver of a real lake.
Measured on the Ongole drone mosaic, real water (ponds, tanks, lakes) is greenish-grey
and smooth: in CIE-Lab its a* (green<->red) sits clearly below the scene's typical
value, while dry fields are warm (a* above it) and roofs / panels / scrub are
strongly textured. So, on the imagery at ~0.5 m:

  water-like pixel = a* at least A_DROP below the scene median, local texture low,
                     and not vivid (green crops are far more saturated than water)

  * a candidate (SegFormer polygon, or an OpenStreetMap water body) is accepted when
    at least MIN_FRACTION of it is water-like;
  * accepted water is then grown into the connected water-like pixels around it
    (within GROW_M), so a lake caught as a sliver gets its real outline;
  * rejected candidates are kept in a separate layer for checking.
"""
import json
import urllib.parse
import urllib.request

import cv2
import geopandas as gpd
import numpy as np
import rasterio
import shapely
import shapely.ops
from rasterio.features import rasterize, shapes
from rasterio.transform import from_bounds
from rasterio.windows import from_bounds as win_from_bounds
from shapely.geometry import box, mapping

from config import DATA

GSD = 0.5            # m/px for the check
A_DROP = 3.0         # a* below the scene median
TEXTURE_MAX = 6.0    # grey-level std over a TEXTURE_WIN_M window (0-255): open water is very even
TEXTURE_WIN_M = 4.5
SOLID_M = 2.5        # water must be at least ~5 m across here
SAT_MAX = 85         # HSV saturation (0-255); crops above this
MIN_FRACTION = 0.5
GROW_M = 120
MIN_AREA_M2 = 80
OSM_DIR = DATA / "osm"


def osm_water(bounds4326, cache_key):
    """OSM water polygons (EPSG:4326), cached. Empty frame when Overpass is unreachable."""
    import roads
    path = OSM_DIR / f"{cache_key}_water.geojson"
    if path.exists():
        return gpd.read_file(path)
    w, s, e, n = bounds4326
    q = (f'[out:json][timeout:180];(way["natural"="water"]({s},{w},{n},{e});'
         f'way["landuse"~"^(reservoir|basin)$"]({s},{w},{n},{e});'
         f'relation["natural"="water"]({s},{w},{n},{e}););out geom;')
    js = None
    for url in roads.OVERPASS:
        try:
            req = urllib.request.Request(url, data=urllib.parse.urlencode({"data": q}).encode(), headers={
                "User-Agent": "smart-property-gpu-server/1.0", "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=240) as r:
                js = json.load(r)
            break
        except Exception as ex:
            print(f"[water] {url.split('/')[2]} failed: {ex}", flush=True)
    polys = []
    for el in (js or {}).get("elements", []):
        if el.get("type") == "way" and len(el.get("geometry") or []) >= 4:
            ring = [(p["lon"], p["lat"]) for p in el["geometry"]]
            if ring[0] == ring[-1]:
                polys.append(shapely.Polygon(ring))
        elif el.get("type") == "relation":
            lines = [shapely.LineString([(p["lon"], p["lat"]) for p in m["geometry"]])
                     for m in el.get("members", []) if m.get("role") == "outer" and len(m.get("geometry") or []) >= 2]
            polys += list(shapely.ops.polygonize(lines).geoms) if lines else []
    polys = [p if p.is_valid else p.buffer(0) for p in polys]
    g = gpd.GeoDataFrame(geometry=polys, crs=4326)
    if js is not None:
        g.to_file(path, driver="GeoJSON")
        print(f"[water] {len(g)} OSM water bodies cached for {cache_key}", flush=True)
    return g


def _scene_median_a(src):
    """Median Lab a* of the whole image, from a small overview read."""
    from jobs import _read_rgb
    s = 1024 / max(src.width, src.height)
    rgb, valid = _read_rgb(src, rasterio.windows.Window(0, 0, src.width, src.height),
                           max(8, int(src.width * s)), max(8, int(src.height * s)))
    a = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)[:, :, 1].astype(np.float32) - 128
    return float(np.median(a[valid])) if valid.any() else 0.0


def _waterlike(rgb, valid, med_a, gsd=GSD, exclude=None):
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    a = lab[:, :, 1] - 128
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    k = max(3, int(round(TEXTURE_WIN_M / gsd)) | 1)
    mu = cv2.blur(g, (k, k))
    std = np.sqrt(np.maximum(cv2.blur(g * g, (k, k)) - mu * mu, 0))
    sat = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)[:, :, 1]
    m = (a <= med_a - A_DROP) & (std <= TEXTURE_MAX) & (sat <= SAT_MAX) & valid
    if exclude is not None:
        m &= ~exclude
    m = cv2.morphologyEx(m.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    # Open water is a solid body; scrub, roofs and fields only make scattered, thin
    # patches. An opening with a SOLID_M radius keeps the former and drops the latter.
    r = max(1, int(round(SOLID_M / gsd)))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)))
    m = m.astype(bool)
    # the texture window blurs across the shoreline: give back that band where the colour is still water
    colour = (a <= med_a - A_DROP) & (sat <= SAT_MAX) & valid
    if exclude is not None:
        colour &= ~exclude
    k2 = k | 1
    return m | (cv2.dilate(m.astype(np.uint8), np.ones((k2, k2), np.uint8)).astype(bool) & colour)


def check(cands, img, progress=lambda p: None, buildings=None):
    """cands: GeoDataFrame of candidate water polygons (raster CRS); buildings (optional)
    are never water — a smooth grey-green roof must not pass.
    Returns (accepted water GeoDataFrame, rejected GeoDataFrame, stats)."""
    from jobs import _read_rgb
    crs = cands.crs
    try:
        osm = osm_water(img["bounds"], img["id"]).to_crs(crs)
        osm = osm[osm.area >= MIN_AREA_M2]
    except Exception as e:
        print("[water] OSM water skipped:", e, flush=True)
        osm = gpd.GeoDataFrame(geometry=[], crs=crs)
    src_tag = ["segformer"] * len(cands) + ["osm"] * len(osm)
    geoms = list(cands.geometry) + list(osm.geometry)
    if not geoms:
        return cands, cands.iloc[0:0], {"water_kept": 0, "water_removed": 0}

    regions = shapely.union_all([g.buffer(GROW_M) for g in geoms])
    regions = list(getattr(regions, "geoms", [regions]))
    tree = shapely.STRtree(geoms)
    bgeoms = list(buildings.geometry) if buildings is not None and len(buildings) else []
    btree = shapely.STRtree(bgeoms) if bgeoms else None
    accepted = [False] * len(geoms)
    frac = [0.0] * len(geoms)
    out = []
    with rasterio.open(img["cog_path"]) as src:
        med_a = _scene_median_a(src)
        print(f"[water] scene median a* = {med_a:.1f}", flush=True)
        for r_i, reg in enumerate(regions, 1):
            x0, y0, x1, y1 = reg.bounds
            w, h = max(8, int((x1 - x0) / GSD)), max(8, int((y1 - y0) / GSD))
            if w * h > 12000 * 12000:          # a giant region: read it coarser, keep memory bounded
                f = (w * h / 12000 ** 2) ** 0.5
                w, h = int(w / f), int(h / f)
            T = from_bounds(x0, y0, x1, y1, w, h)
            rgb, valid = _read_rgb(src, win_from_bounds(x0, y0, x1, y1, transform=src.transform), w, h)
            roofs = None
            if btree is not None:
                hit = btree.query(reg)
                if len(hit):
                    roofs = rasterize([(mapping(bgeoms[j]), 1) for j in hit], out_shape=(h, w), transform=T,
                                      dtype="uint8").astype(bool)
            wl = _waterlike(rgb, valid, med_a, gsd=(x1 - x0) / w, exclude=roofs)
            seeds = np.zeros((h, w), bool)
            for i in tree.query(reg):
                m = rasterize([(mapping(geoms[i]), 1)], out_shape=(h, w), transform=T, dtype="uint8").astype(bool)
                n = m.sum()
                if not n:
                    continue
                frac[i] = float((m & wl).sum() / n)
                if frac[i] >= MIN_FRACTION:
                    accepted[i] = True
                    seeds |= m & wl
            if seeds.any():
                # grow: connected water-like pixels that touch an accepted seed
                n_lab, lab = cv2.connectedComponents(wl.astype(np.uint8), connectivity=8)
                keep = np.zeros(n_lab, bool)
                keep[np.unique(lab[seeds])] = True
                keep[0] = False
                water = keep[lab] | seeds
                inv = (~water).astype(np.uint8)       # fill small islands (reflections, boats)
                n2, lab2, st2, _ = cv2.connectedComponentsWithStats(inv, connectivity=4)
                small = st2[:, cv2.CC_STAT_AREA] < (200 / GSD ** 2)
                small[0] = False
                water[small[lab2]] = True
                for g, v in shapes(water.astype(np.uint8), mask=water, transform=T):
                    p = shapely.geometry.shape(g).buffer(GSD).buffer(-GSD).simplify(GSD)
                    if p.area >= MIN_AREA_M2:
                        out.append(p)
            progress(r_i / len(regions))

    merged = shapely.union_all(out) if out else None
    polys = list(getattr(merged, "geoms", [merged])) if merged is not None and not merged.is_empty else []
    kept = gpd.GeoDataFrame({"area_m2": [round(p.area, 2) for p in polys], "check": "imagery"}, geometry=polys, crs=crs)
    rej_i = [i for i in range(len(cands)) if not accepted[i]]
    removed = gpd.GeoDataFrame({"area_m2": [round(geoms[i].area, 2) for i in rej_i],
                                "water_like": [round(frac[i], 2) for i in rej_i]},
                               geometry=[geoms[i] for i in rej_i], crs=crs)
    stats = {"water_kept": len(kept), "water_removed": len(removed),
             "water_from_osm": int(sum(1 for i, t in enumerate(src_tag) if t == "osm" and accepted[i])),
             "scene_median_a": round(med_a, 2)}
    print(f"[water] kept {len(kept)} water bodies, removed {len(removed)} false ones", flush=True)
    return kept, removed, stats
