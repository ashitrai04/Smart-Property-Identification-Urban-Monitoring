import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

"""
Smart Property Identification — Local Backend Server
=====================================================
FastAPI server that serves GeoJSON from cleaned GPKG files.
Mirrors the DRONACHARYA pattern: /api/districts/{name}/{layer}

Features:
  - Spatial filtering via ?bbox=xmin,ymin,xmax,ymax
  - Zoom-level aware feature limits (fewer features at low zoom)
  - In-memory caching of GPKG reads
  - CORS enabled for Vite dev server
"""

import os
import json
import time
import glob
import hashlib
from pathlib import Path
from functools import lru_cache
import boto3
from dotenv import load_dotenv

load_dotenv()

import geopandas as gpd
import numpy as np
from shapely.geometry import box, mapping
import shapely
from fastapi.middleware.gzip import GZipMiddleware
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from rio_tiler.io import Reader
from rio_tiler.profiles import img_profiles
from PIL import Image
from io import BytesIO
import uvicorn

# ═══════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════

# On Hugging Face (or docker), we'll store data locally in the app dir
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "datasets", "cleaned_features"))
PORT = 8000

# ═══════════════════════════════════════════════════════════
#  CLOUD STORAGE SYNC (Hugging Face Startup)
# ═══════════════════════════════════════════════════════════
DATA_EXTENSIONS = ('.gpkg', '.tif', '.tiff')

def sync_datasets_from_r2():
    account_id = os.environ.get('R2_ACCOUNT_ID')
    access_key = os.environ.get('R2_ACCESS_KEY_ID')
    secret_key = os.environ.get('R2_SECRET_ACCESS_KEY')
    bucket_name = os.environ.get('R2_BUCKET_NAME')

    if not all([account_id, access_key, secret_key, bucket_name]):
        print("⚠️ No R2 credentials found. Skipping dataset sync.")
        return

    account_id = account_id.replace("https://", "").replace(".r2.cloudflarestorage.com", "").replace("/", "").strip()
    
    print(f"📥 Syncing datasets from R2 bucket '{bucket_name}' to {DATA_DIR}...")
    os.makedirs(DATA_DIR, exist_ok=True)
    
    try:
        s3 = boto3.client(
            service_name='s3',
            endpoint_url=f'https://{account_id}.r2.cloudflarestorage.com',
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name='auto',
        )
        objects = []
        for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket_name):
            objects.extend(page.get('Contents', []))
    except Exception as e:
        print(f"❌ Could not list R2 bucket: {e}")
        return

    # The bucket is shared: other projects keep folders in it (e.g. "Gujarat-hackathon/")
    # and the upload flow writes "temp/…" scratch files. Only top-level district data
    # belongs here — a folder key used to make download_file throw and abort the whole
    # sync, leaving the server with zero districts.
    wanted = [o for o in objects
              if '/' not in o['Key'] and o['Key'].lower().endswith(DATA_EXTENSIONS)]
    if not wanted:
        print("⚠️ R2 bucket has no district data files.")
        return

    ok = failed = 0
    for obj in wanted:
        key, size = obj['Key'], obj['Size']
        local_path = os.path.join(DATA_DIR, key)
        # Skip only complete copies; a size mismatch means an interrupted download.
        if os.path.exists(local_path) and os.path.getsize(local_path) == size:
            ok += 1
            continue
        tmp = local_path + '.part'
        try:
            print(f"  Downloading {key} ({size / 1e6:.1f} MB)...")
            s3.download_file(bucket_name, key, tmp)
            os.replace(tmp, local_path)
            ok += 1
        except Exception as e:
            failed += 1
            print(f"  ❌ {key}: {e}")
            try:
                os.remove(tmp)
            except OSError:
                pass
    print(f"✅ Dataset sync: {ok} file(s) ready, {failed} failed.")

sync_datasets_from_r2()

# Class ID → layer name mapping (matches your cleaned GPKG)
CLASS_LAYER_MAP = {
    1: "buildings",
    4: "roads",
    5: "waterbodies",
    6: "openareas",
}
LAYER_CLASS_MAP = {v: k for k, v in CLASS_LAYER_MAP.items()}

# The source GPKG mis-files water (and a few roads) under class_id 1 (buildings)
# but tags the real type in `fclass`. We classify by fclass so the waterbodies/
# roads layers return the right features and buildings doesn't double-count them.
WATER_FCLASS = ["water", "riverbank", "reservoir", "wetland", "pond", "lake",
                "basin", "dock", "canal", "stream", "river", "lagoon",
                "glacier", "wastewater"]
ROAD_FCLASS = ["motorway", "trunk", "primary", "secondary", "tertiary",
               "unclassified", "residential", "service", "road", "living_street",
               "track", "path", "footway", "cycleway", "pedestrian", "steps",
               "bridleway", "motorway_link", "trunk_link", "primary_link",
               "secondary_link", "tertiary_link"]

# District metadata (centers & zoom for the UI)
DISTRICT_META = {
    "visakhapatnam": {"center": [83.25, 17.93], "zoom": 11},
    "vijayawada":    {"center": [80.62, 16.51], "zoom": 11},
    "guntur":        {"center": [80.45, 16.30], "zoom": 11},
    "anantapur":     {"center": [77.60, 14.68], "zoom": 10},
    "nellore":       {"center": [79.99, 14.44], "zoom": 10},
}

# Zoom-level feature limits — prevent browser crash at low zoom
ZOOM_FEATURE_LIMITS = {
    # zoom: max_features
    0: 500, 1: 500, 2: 500, 3: 500, 4: 500,
    5: 1000, 6: 1000, 7: 2000, 8: 3000,
    9: 5000, 10: 8000, 11: 15000,
    12: 30000, 13: 50000, 14: 80000,
    15: 150000, 16: 300000, 17: 500000,
    18: 1000000, 19: 1000000, 20: 1000000,
}

# ═══════════════════════════════════════════════════════════
#  APP
# ═══════════════════════════════════════════════════════════

app = FastAPI(
    title="Smart Property Backend",
    version="1.0.0",
    description="Serves building/road/water GeoJSON from GPKG files",
)

app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ═══════════════════════════════════════════════════════════
#  DATA LOADING & CACHING
# ═══════════════════════════════════════════════════════════

# Cache: { "district_name": { "features": GeoDataFrame, "boundary": GeoDataFrame } }
_cache = {}

def _discover_districts():
    """Find all _cleaned.gpkg files and register them."""
    districts = {}
    pattern = os.path.join(DATA_DIR, "*_cleaned.gpkg")
    for fp in sorted(glob.glob(pattern)):
        name = os.path.basename(fp).replace("_cleaned.gpkg", "").lower()
        districts[name] = fp
    return districts

AVAILABLE_DISTRICTS = _discover_districts()

# Also map districts to their corresponding raster .tif file
AVAILABLE_RASTERS = {
    "anantapur": os.path.join(DATA_DIR, "ANANTAPUR-RASTER.tif"),
    "guntur": os.path.join(DATA_DIR, "GUNTUR-RASTER.tif"),
    "nellore": os.path.join(DATA_DIR, "NELLORE--RASTER.tif"),
    "vijayawada": os.path.join(DATA_DIR, "VIJAYAVDA-RASTER.tif"),
    "visakhapatnam": os.path.join(DATA_DIR, "visakhapatnam_mask.tif"),
}

print(f"\n{'='*60}")
print(f"📂 Discovered {len(AVAILABLE_DISTRICTS)} districts:")
for name, path in AVAILABLE_DISTRICTS.items():
    sz = os.path.getsize(path) / 1e6
    print(f"   {name:20s} → {sz:>8.1f} MB")
print(f"{'='*60}\n")


def _load_district(name: str):
    """Load GPKG into memory (cached)."""
    if name in _cache:
        return _cache[name]

    if name not in AVAILABLE_DISTRICTS:
        raise HTTPException(404, f"District '{name}' not found")

    path = AVAILABLE_DISTRICTS[name]
    t0 = time.time()
    print(f"⏳ Loading {name}...")

    result = {}

    # Load features layer
    try:
        gdf = gpd.read_file(path, layer="features")
        # Ensure EPSG:4326
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
        result["features"] = gdf
        # Vertex count per row. The road network is stored as a few giant
        # dissolved polygons (millions of vertices); a bbox filter cannot trim
        # those, so they are clipped to the viewport per request instead.
        result["ncoords"] = shapely.get_num_coordinates(gdf.geometry.values)
        print(f"   Features: {len(gdf):,} rows, columns: {list(gdf.columns)}")
    except Exception as e:
        print(f"   ⚠️ No 'features' layer: {e}")
        result["features"] = gpd.GeoDataFrame()

    # Load boundary layer
    try:
        bdf = gpd.read_file(path, layer="boundary")
        if bdf.crs and bdf.crs.to_epsg() != 4326:
            bdf = bdf.to_crs(epsg=4326)
        result["boundary"] = bdf
        print(f"   Boundary: {len(bdf)} rows")
    except Exception as e:
        print(f"   ⚠️ No 'boundary' layer: {e}")
        result["boundary"] = gpd.GeoDataFrame()

    elapsed = time.time() - t0
    print(f"   ✅ Loaded in {elapsed:.1f}s")

    _cache[name] = result
    return result



def _gdf_to_geojson_string(gdf):
    """FeatureCollection JSON. Built from vectorised shapely.to_geojson + pandas
    record JSON — ~20x faster than GeoDataFrame.to_json on 50k features."""
    if gdf is None or len(gdf) == 0:
        return '{"type": "FeatureCollection", "features": []}'
    geoms = shapely.to_geojson(gdf.geometry.values)
    attrs = gdf.drop(columns=gdf.geometry.name)
    props = (attrs.to_json(orient="records", lines=True, default_handler=str).splitlines()
             if len(attrs.columns) else [])
    if len(props) != len(geoms):          # no attribute columns (e.g. boundary)
        props = ["{}"] * len(geoms)
    feats = ",".join(
        '{"type":"Feature","properties":%s,"geometry":%s}' % (p, g if g is not None else "null")
        for p, g in zip(props, geoms))
    return '{"type":"FeatureCollection","features":[' + feats + ']}'


_layer_idx = {}   # (district, layer) -> (row positions, STRtree over those rows)


def _layer_index(name, layer, gdf):
    """Row positions of a layer plus an STRtree over them, built once per district.

    A viewport request then touches only the rows it needs. Filtering the full
    563k-row frame per request (fclass string ops + boolean copies of the whole
    table) was cheap on a laptop but took 15-50 s on the Space's shared CPU.
    """
    key = (name, layer)
    hit = _layer_idx.get(key)
    if hit is None:
        sub = _filter_features(gdf, layer=layer, class_id=LAYER_CLASS_MAP.get(layer))
        pos = gdf.index.get_indexer(sub.index) if sub is not None and len(sub) else np.array([], dtype=np.int64)
        hit = (pos, shapely.STRtree(gdf.geometry.values[pos]))
        _layer_idx[key] = hit
    return hit


BIG_GEOM_VERTICES = 5000   # above this a shape is simplified once per zoom (cached) and clipped
_big_cache = {}            # (district, row index, zoom) -> simplified geometry


def _simplify_rings(geom, tol):
    """Douglas-Peucker each ring as a plain line and rebuild the polygons unvalidated.

    shapely.simplify() on these dissolved road polygons (700k+ vertices, 10k rings)
    spends ~50 s in GEOS topology repair; this takes ~1 s and the browser renders
    the result identically.
    """
    if geom is None or geom.is_empty:
        return geom
    if geom.geom_type not in ("Polygon", "MultiPolygon"):
        return shapely.simplify(geom, tol, preserve_topology=False)
    polys = []
    for part in shapely.get_parts(geom):
        rings = []
        for ring in shapely.get_rings(part):
            line = shapely.simplify(shapely.linestrings(shapely.get_coordinates(ring)), tol, preserve_topology=False)
            c = shapely.get_coordinates(line)
            rings.append(c if len(c) >= 4 else None)
        if rings and rings[0] is not None:
            polys.append(shapely.Polygon(rings[0], [r for r in rings[1:] if r is not None]))
    return shapely.MultiPolygon(polys) if polys else shapely.Polygon()


def _zoom_tol(zoom):
    """Half a screen pixel at this zoom (512 px tiles), in degrees."""
    return 360.0 / (512 * 2 ** float(zoom)) / 2


def _shape_for_view(name, gdf, ncoords, bbox, zoom):
    """Display geometry for the layer endpoint: simplify to the zoom, clip giant
    shapes to the viewport, round to ~0.1 m. Returns a copy — the cached data,
    and therefore stats / AOI counts, keep the original geometry."""
    if gdf is None or len(gdf) == 0:
        return gdf
    geoms = gdf.geometry.values.copy()
    z = None if zoom is None else max(0, min(16, int(round(float(zoom)))))
    big = ncoords > BIG_GEOM_VERTICES if ncoords is not None else np.zeros(len(geoms), bool)

    small = ~big
    if z is not None and z < 16 and small.any():
        geoms[small] = shapely.simplify(geoms[small], _zoom_tol(z), preserve_topology=False)

    if big.any():
        for i in np.flatnonzero(big):
            key = (name, gdf.index[i], z)
            g = _big_cache.get(key)
            if g is None:
                g = _simplify_rings(geoms[i], _zoom_tol(z if z is not None else 16))
                if len(_big_cache) > 200:
                    _big_cache.pop(next(iter(_big_cache)))
                _big_cache[key] = g
            geoms[i] = g
        if bbox is not None:
            pad = 0.02 * max(bbox[2] - bbox[0], bbox[3] - bbox[1])
            geoms[big] = shapely.clip_by_rect(geoms[big], bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)

    geoms = shapely.transform(geoms, lambda c: np.round(c, 6))
    out = gdf.set_geometry(gpd.GeoSeries(geoms, index=gdf.index, crs=gdf.crs))
    return out[~shapely.is_empty(geoms)]


def _filter_features(gdf, layer=None, class_id=None, bbox_str=None, zoom=None, limit=None):
    """Filter GeoDataFrame by layer (fclass-aware), bbox, and zoom-based limits."""
    if gdf is None or len(gdf) == 0:
        return gdf

    has_class = "class_id" in gdf.columns
    fc = gdf["fclass"].astype(str).str.lower() if "fclass" in gdf.columns else None

    # fclass-aware layer filtering (falls back to plain class_id when needed)
    if layer and has_class:
        if layer == "waterbodies":
            mask = (gdf["class_id"] == 5)
            if fc is not None:
                mask = mask | fc.isin(WATER_FCLASS)
            gdf = gdf[mask]
        elif layer == "roads":
            mask = (gdf["class_id"] == 4)
            if fc is not None:
                mask = mask | fc.isin(ROAD_FCLASS)
            gdf = gdf[mask]
        elif layer == "buildings":
            mask = (gdf["class_id"] == 1)
            if fc is not None:
                mask = mask & ~fc.isin(WATER_FCLASS) & ~fc.isin(ROAD_FCLASS)
            gdf = gdf[mask]
        elif layer == "openareas":
            gdf = gdf[gdf["class_id"] == 6]
        elif class_id is not None:
            gdf = gdf[gdf["class_id"] == class_id]
    elif class_id is not None and has_class:
        gdf = gdf[gdf["class_id"] == class_id]

    # Filter by bounding box using spatial index (.cx)
    if bbox_str:
        try:
            parts = [float(x) for x in bbox_str.split(",")]
            if len(parts) == 4:
                xmin, ymin, xmax, ymax = parts
                # .cx is 100x faster than full intersection
                gdf = gdf.cx[xmin:xmax, ymin:ymax]
        except (ValueError, TypeError):
            pass

    # Zoom-based limit
    if zoom is not None:
        max_features = ZOOM_FEATURE_LIMITS.get(int(zoom), 100000)
        if limit:
            max_features = min(max_features, limit)
        if len(gdf) > max_features:
            gdf = gdf.head(max_features)
    elif limit and len(gdf) > limit:
        gdf = gdf.head(limit)

    return gdf


print("\n[STARTUP] Pre-loading datasets and building spatial indices to prevent 504 Timeouts...")
for d_name in AVAILABLE_DISTRICTS:
    _load_district(d_name)
    if "features" in _cache[d_name] and not _cache[d_name]["features"].empty:
        for _lyr in ("buildings", "roads", "waterbodies", "openareas"):
            _layer_index(d_name, _lyr, _cache[d_name]["features"])
print("[STARTUP] All spatial indices built!\n")


# ═══════════════════════════════════════════════════════════
#  ENDPOINTS
# ═══════════════════════════════════════════════════════════

_gpu_url_cache = {"t": 0.0, "v": None}


@app.get("/api/gpu-server")
def gpu_server():
    """Current public URL of the GPU inference server.

    Its free Cloudflare tunnel gets a new address on every restart; the GPU
    server writes it to R2 (runtime/gpu_server.json) and the webapp asks here,
    so nothing has to be redeployed when it changes. Cached for 20 s.
    """
    now = time.time()
    if now - _gpu_url_cache["t"] < 20 and _gpu_url_cache["v"] is not None:
        return _gpu_url_cache["v"]
    out = {"url": None, "online": False, "updated": None}
    try:
        acc = os.environ.get("R2_ACCOUNT_ID", "").replace("https://", "").replace(".r2.cloudflarestorage.com", "").replace("/", "").strip()
        s3 = boto3.client("s3", endpoint_url=f"https://{acc}.r2.cloudflarestorage.com",
                          aws_access_key_id=os.environ.get("R2_ACCESS_KEY_ID"),
                          aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY"), region_name="auto")
        body = s3.get_object(Bucket=os.environ.get("R2_BUCKET_NAME"), Key="runtime/gpu_server.json")["Body"].read()
        out.update(json.loads(body))
    except Exception:
        pass   # no GPU server published yet → webapp uses Hugging Face
    _gpu_url_cache.update(t=now, v=out)
    return out


@app.get("/api/health")
async def health():
    return {"status": "ok", "districts": len(AVAILABLE_DISTRICTS)}


@app.get("/api/districts")
async def list_districts():
    """List all available districts with metadata."""
    result = []
    for name in AVAILABLE_DISTRICTS:
        meta = DISTRICT_META.get(name, {"center": [80, 16], "zoom": 10})
        layers = ["boundary", "buildings", "roads", "waterbodies", "openareas"]
        result.append({
            "name": name.title(),
            "key": name,
            "center": meta["center"],
            "zoom": meta["zoom"],
            "layers": layers,
        })
    return result


@app.get("/api/districts/{name}")
def get_district(name: str):
    """Get district metadata."""
    name = name.lower()
    if name not in AVAILABLE_DISTRICTS:
        raise HTTPException(404, f"District '{name}' not found")
    meta = DISTRICT_META.get(name, {"center": [80, 16], "zoom": 10})

    # Load to count features
    data = _load_district(name)
    gdf = data.get("features", gpd.GeoDataFrame())

    layer_counts = {}
    if "class_id" in gdf.columns:
        for cls_id, lname in CLASS_LAYER_MAP.items():
            layer_counts[lname] = int((gdf["class_id"] == cls_id).sum())
    layer_counts["boundary"] = len(data.get("boundary", []))

    return {
        "name": name.title(),
        "key": name,
        "center": meta["center"],
        "zoom": meta["zoom"],
        "layer_counts": layer_counts,
        "total_features": len(gdf),
    }


@app.get("/api/districts/{name}/boundary")
def get_boundary(name: str):
    """Get district boundary as GeoJSON."""
    name = name.lower()
    data = _load_district(name)
    bdf = data.get("boundary", gpd.GeoDataFrame())
    # Raw response for boundary too
    return Response(content=_gdf_to_geojson_string(bdf), media_type="application/json")


# NOTE: this route MUST be defined before /{layer}, otherwise "stats" is
# swallowed by the layer catch-all and this endpoint is unreachable.
@app.get("/api/districts/{name}/stats")
def get_stats(name: str):
    """Real per-layer statistics for a district (fclass-aware, matches the layer endpoints)."""
    name = name.lower()
    data = _load_district(name)
    gdf = data.get("features", gpd.GeoDataFrame())

    stats = {}
    total_area = 0.0
    if len(gdf) > 0 and "class_id" in gdf.columns:
        for lname in ["buildings", "roads", "waterbodies", "openareas"]:
            subset = _filter_features(gdf, layer=lname)
            layer_stats = {"count": int(len(subset))}
            if len(subset) > 0 and "area_m2" in subset.columns:
                areas = subset["area_m2"].dropna()
                if len(areas) > 0:
                    layer_stats["total_area_m2"] = float(areas.sum())
                    layer_stats["avg_area_m2"] = float(areas.mean())
                    total_area += float(areas.sum())
            if lname == "roads" and len(subset) > 0 and "fclass" in subset.columns:
                layer_stats["road_types"] = {str(k): int(v) for k, v in
                                             subset["fclass"].value_counts().head(10).items()}
            stats[lname] = layer_stats
    stats["boundary"] = {"count": int(len(data.get("boundary", [])))}

    return {"district": name, "total_features": int(len(gdf)),
            "total_classified_area_m2": total_area, "stats": stats}


@app.get("/api/districts/{name}/{layer}")
def get_district_layer(name: str, layer: str, bbox: str = Query(None), zoom: float = Query(None), limit: int = Query(None)):
    """Get GeoJSON for a specific layer, optionally filtered by bbox and zoom."""
    name = name.lower()
    layer = layer.lower()

    if layer == "boundary":
        return get_boundary(name)

    class_id = LAYER_CLASS_MAP.get(layer)
    if class_id is None:
        raise HTTPException(400, f"Unknown layer '{layer}'. Use: boundary, buildings, roads, waterbodies, openareas")

    data = _load_district(name)
    gdf = data.get("features", gpd.GeoDataFrame())

    bb = None
    if bbox:
        try:
            parts = [float(x) for x in bbox.split(",")]
            bb = parts if len(parts) == 4 else None
        except ValueError:
            bb = None

    nc_all = data.get("ncoords")
    if bb is not None and len(gdf):
        pos, tree = _layer_index(name, layer, gdf)
        sel = np.sort(pos[tree.query(box(*bb))])
        cap = ZOOM_FEATURE_LIMITS.get(int(zoom), 100000) if zoom is not None else None
        if limit:
            cap = min(cap, limit) if cap else limit
        if cap and len(sel) > cap:
            sel = sel[:cap]
        filtered = gdf.iloc[sel]
        nc = nc_all[sel] if nc_all is not None else None
    else:
        filtered = _filter_features(gdf, layer=layer, class_id=class_id, bbox_str=bbox, zoom=zoom, limit=limit)
        nc = nc_all[gdf.index.get_indexer(filtered.index)] if (nc_all is not None and filtered is not None and len(filtered)) else None
    # The road network is stored twice (class 4, and a class-1 row tagged
    # fclass=trunk); drawing both doubles the payload for no visible change.
    if filtered is not None and nc is not None and (nc > BIG_GEOM_VERTICES).sum() > 1:
        keep = np.ones(len(filtered), bool)
        seen = set()
        bounds = filtered.geometry.bounds.values
        for i in np.flatnonzero(nc > BIG_GEOM_VERTICES):   # only the few giant rows
            sig = (int(nc[i]), tuple(np.round(bounds[i], 5)))
            keep[i] = sig not in seen
            seen.add(sig)
        filtered, nc = filtered[keep], nc[keep]
    shaped = _shape_for_view(name, filtered, nc, bb, zoom)

    return Response(content=_gdf_to_geojson_string(shaped), media_type="application/json")


# ── Tile cache: stores rendered PNG bytes keyed by (district, z, x, y) ──
_tile_cache = {}
_TILE_CACHE_MAX = 500  # ~50MB of tiles

EMPTY_PNG = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n\xb4\x00\x00\x00\x00IEND\xaeB`\x82'

# Legend-matching colormap (default: 1-3 = building confidence tiers, 4 road, 5 water, 6 open)
RASTER_COLORMAP = {
    1: (220, 38, 38, 200),    # Dark Red — Buildings High
    2: (249, 115, 22, 200),   # Orange — Buildings Med
    3: (251, 191, 36, 200),   # Amber — Buildings Low
    4: (234, 179, 8, 220),    # Yellow — Roads
    5: (59, 130, 246, 200),   # Blue — Waterbodies
    6: (156, 163, 175, 180),  # Gray — Open Areas
}

# Visakhapatnam raster uses a different class scheme: 1 = building, 2 = road, 3 = water.
VIZAG_COLORMAP = {
    1: (220, 38, 38, 200),    # Red    — Buildings
    2: (234, 179, 8, 220),    # Yellow — Roads
    3: (59, 130, 246, 200),   # Blue   — Waterbodies
}

# Per-district colormap overrides
DISTRICT_COLORMAPS = {
    "visakhapatnam": VIZAG_COLORMAP,
}


@app.get("/api/districts/{name}/raster/tiles/{z}/{x}/{y}.png")
def get_raster_tile(name: str, z: int, x: int, y: int):
    """Serve XYZ raster tiles from the district .tif file with proper colormap."""
    name = name.lower()
    if name not in AVAILABLE_RASTERS:
        raise HTTPException(status_code=404, detail=f"No raster found for district '{name}'.")

    tif_path = AVAILABLE_RASTERS[name]
    if not os.path.exists(tif_path):
        raise HTTPException(status_code=404, detail="Raster file not found on disk.")

    # Check cache first
    cache_key = (name, z, x, y)
    if cache_key in _tile_cache:
        return Response(content=_tile_cache[cache_key], media_type="image/png", headers={
            "Cache-Control": "public, max-age=86400",
            "X-Cache": "HIT",
        })

    try:
        with Reader(tif_path) as src:
            img = src.tile(x, y, z, tilesize=256)
            band = img.data[0]  # shape: (256, 256) — uint8 class values
            
            h, w = band.shape
            rgba = np.zeros((h, w, 4), dtype=np.uint8)

            colormap = DISTRICT_COLORMAPS.get(name, RASTER_COLORMAP)
            for val, color in colormap.items():
                mask = band == val
                rgba[mask] = color
            
            # Encode as PNG
            pil_img = Image.fromarray(rgba, 'RGBA')
            buf = BytesIO()
            pil_img.save(buf, format='PNG', optimize=False)
            content = buf.getvalue()
        
        # Store in cache (evict oldest if full)
        if len(_tile_cache) >= _TILE_CACHE_MAX:
            oldest = next(iter(_tile_cache))
            del _tile_cache[oldest]
        _tile_cache[cache_key] = content
            
        return Response(content=content, media_type="image/png", headers={
            "Cache-Control": "public, max-age=86400",
            "X-Cache": "MISS",
        })
    except Exception as e:
        return Response(content=EMPTY_PNG, media_type="image/png", headers={
            "Cache-Control": "public, max-age=86400",
        })

# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"\n🚀 Starting Smart Property Backend on port {PORT}...")
    print(f"   Data dir: {os.path.abspath(DATA_DIR)}")
    print(f"   Endpoints:")
    print(f"     GET /api/districts")
    print(f"     GET /api/districts/{{name}}")
    print(f"     GET /api/districts/{{name}}/boundary")
    print(f"     GET /api/districts/{{name}}/buildings?bbox=...&zoom=...")
    print(f"     GET /api/districts/{{name}}/roads?bbox=...&zoom=...")
    print(f"     GET /api/districts/{{name}}/waterbodies?bbox=...&zoom=...")
    print(f"     GET /api/districts/{{name}}/openareas?bbox=...&zoom=...")
    print(f"     GET /api/districts/{{name}}/stats")
    print()

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
