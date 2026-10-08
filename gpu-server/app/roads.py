"""Where the roads are — so road-damage detections can be kept to roads.

Two independent sources, combined (a road missed by one is caught by the other):
  * OpenStreetMap roads for the image's area (Overpass API, cached on disk), buffered
    to a realistic carriageway width per road type.
  * SegFormer's own road class on the same imagery (run at ~12 cm/px, the scale the
    model is happier with than 3 cm, then scaled back up).
"""
import json
import os
import time
import urllib.parse
import urllib.request

import cv2
import geopandas as gpd
import numpy as np
from rasterio.features import rasterize
from shapely.geometry import box, mapping, shape

from config import DATA

OSM_DIR = DATA / "osm"
OSM_DIR.mkdir(parents=True, exist_ok=True)
# Public Overpass mirrors, tried in order (the main one rejects some networks with 406).
OVERPASS = [u for u in [os.environ.get("OVERPASS_URL"),
                        "https://overpass-api.de/api/interpreter",
                        "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
                        "https://overpass.kumi.systems/api/interpreter"] if u]

# Half-width (m) of the carriageway per OSM highway type, before the safety margin.
HALF_WIDTH = {
    "motorway": 9, "trunk": 8, "primary": 6.5, "secondary": 5.5, "tertiary": 4.5,
    "motorway_link": 4, "trunk_link": 4, "primary_link": 4, "secondary_link": 3.5, "tertiary_link": 3.5,
    "unclassified": 3.5, "residential": 3.5, "living_street": 3, "service": 2.5, "road": 3.5,
    "track": 2, "pedestrian": 2.5,
}
EDGE_MARGIN_M = 1.5          # potholes at the road edge must survive the gate


def osm_roads(bounds4326, cache_key):
    """Road centre-lines (EPSG:4326) for a bbox, fetched once and cached as GeoJSON."""
    path = OSM_DIR / f"{cache_key}_roads.geojson"
    if path.exists():
        return gpd.read_file(path)
    w, s, e, n = bounds4326
    hw = "|".join(HALF_WIDTH)
    q = f'[out:json][timeout:180];way["highway"~"^({hw})$"]({s},{w},{n},{e});out geom;'
    data = urllib.parse.urlencode({"data": q}).encode()
    js = None
    for attempt in range(2):
        for url in OVERPASS:
            try:
                req = urllib.request.Request(url, data=data, headers={
                    "User-Agent": "smart-property-gpu-server/1.0", "Accept": "*/*",
                    "Content-Type": "application/x-www-form-urlencoded"})
                with urllib.request.urlopen(req, timeout=240) as r:
                    js = json.load(r)
                break
            except Exception as ex:   # rate limits / mirror down: try the next one
                print(f"[roads] {url.split('/')[2]} failed: {ex}", flush=True)
        if js is not None:
            break
        time.sleep(20)
    if js is None:
        print("[roads] OSM unavailable — road gate will use SegFormer only", flush=True)
        return gpd.GeoDataFrame({"highway": []}, geometry=[], crs=4326)
    feats = []
    for el in js.get("elements", []):
        g = el.get("geometry")
        if el.get("type") == "way" and g and len(g) >= 2:
            feats.append({"type": "Feature", "properties": {"highway": el.get("tags", {}).get("highway", "road")},
                          "geometry": {"type": "LineString", "coordinates": [[p["lon"], p["lat"]] for p in g]}})
    gdf = gpd.GeoDataFrame.from_features(feats, crs=4326) if feats else gpd.GeoDataFrame({"highway": []}, geometry=[], crs=4326)
    gdf.to_file(path, driver="GeoJSON")
    print(f"[roads] {len(gdf)} OSM road segments cached for {cache_key}", flush=True)
    return gdf


def buffered_roads(roads4326, crs):
    """Road polygons (carriageway + margin) in the raster's projected CRS."""
    if roads4326 is None or not len(roads4326):
        return gpd.GeoDataFrame(geometry=[], crs=crs)
    r = roads4326.to_crs(crs)
    widths = r["highway"].map(HALF_WIDTH).fillna(3.0) + EDGE_MARGIN_M
    return gpd.GeoDataFrame(geometry=r.geometry.buffer(widths.values), crs=crs)


def job_roads(job_id, crs, margin_m=EDGE_MARGIN_M):
    """Road polygons produced by a finished segment/fusion job, buffered by the edge margin."""
    import db
    j = db.get_job(job_id)
    if not j or j.get("status") != "done" or not j.get("result_dir"):
        print(f"[roads] roads_job {job_id} not finished — ignored", flush=True)
        return None
    p = os.path.join(j["result_dir"], "results.gpkg")
    try:
        g = gpd.read_file(p, layer="roads", engine="pyogrio")
    except Exception:
        return None
    if not len(g):
        return None
    g = g.to_crs(crs)
    return gpd.GeoDataFrame(geometry=g.geometry.buffer(margin_m), crs=crs)


def osm_mask(road_polys, win_transform, shape_hw):
    """Rasterise road polygons into a window grid (bool)."""
    if road_polys is None or not len(road_polys):
        return np.zeros(shape_hw, bool)
    h, w = shape_hw
    x0, y0 = win_transform * (0, 0)
    x1, y1 = win_transform * (w, h)
    sub = road_polys[road_polys.intersects(box(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))]
    if not len(sub):
        return np.zeros(shape_hw, bool)
    return rasterize(((mapping(g), 1) for g in sub.geometry), out_shape=shape_hw, transform=win_transform,
                     fill=0, dtype="uint8").astype(bool)


def seg_road_mask(rgb, gsd_m, model, seg, target_gsd=0.12, margin_m=EDGE_MARGIN_M):
    """SegFormer road class for an RGB window, computed at ~target_gsd and scaled back."""
    h, w = rgb.shape[:2]
    f = min(1.0, (gsd_m or target_gsd) / target_gsd)          # e.g. 0.03/0.12 = 0.25
    small = cv2.resize(rgb, (max(32, int(w * f)), max(32, int(h * f))), interpolation=cv2.INTER_AREA) if f < 1 else rgb
    pred = seg.segment(model, small)
    road = (pred == seg.CLASS_ROAD).astype(np.uint8)
    rpx = max(1, int(round(margin_m / max(gsd_m or target_gsd, 1e-6) * f)))
    road = cv2.dilate(road, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * rpx + 1, 2 * rpx + 1)))
    if f < 1:
        road = cv2.resize(road, (w, h), interpolation=cv2.INTER_NEAREST)
    return road.astype(bool)


def box_overlap(mask, x1, y1, x2, y2):
    """Fraction of a pixel box covered by the mask."""
    h, w = mask.shape
    xa, ya = max(0, int(x1)), max(0, int(y1))
    xb, yb = min(w, int(np.ceil(x2))), min(h, int(np.ceil(y2)))
    if xb <= xa or yb <= ya:
        return 0.0
    return float(mask[ya:yb, xa:xb].mean())
