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
        
        objects = s3.list_objects_v2(Bucket=bucket_name)
        if 'Contents' in objects:
            for obj in objects['Contents']:
                file_key = obj['Key']
                local_path = os.path.join(DATA_DIR, file_key)
                if not os.path.exists(local_path):
                    print(f"  Downloading {file_key} ({obj['Size'] / 1e6:.1f} MB)...")
                    s3.download_file(bucket_name, file_key, local_path)
            print("✅ All datasets synced successfully!")
        else:
            print("⚠️ R2 bucket is empty.")
    except Exception as e:
        print(f"❌ Failed to sync datasets: {e}")

sync_datasets_from_r2()

# Class ID → layer name mapping (matches your cleaned GPKG)
CLASS_LAYER_MAP = {
    1: "buildings",
    4: "roads",
    5: "waterbodies",
    6: "openareas",
}
LAYER_CLASS_MAP = {v: k for k, v in CLASS_LAYER_MAP.items()}

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


def _gdf_to_geojson(gdf):
    """Convert GeoDataFrame to GeoJSON dict, handling numpy types."""
    if gdf is None or len(gdf) == 0:
        return {"type": "FeatureCollection", "features": []}

    # Convert to dict and handle numpy types
    geojson = json.loads(gdf.to_json())
    return geojson


def _filter_features(gdf, class_id=None, bbox_str=None, zoom=None, limit=None):
    """Filter GeoDataFrame by class, bbox, and zoom-based limits."""
    if gdf is None or len(gdf) == 0:
        return gdf

    # Filter by class
    if class_id is not None and "class_id" in gdf.columns:
        gdf = gdf[gdf["class_id"] == class_id]

    # Filter by bounding box
    if bbox_str:
        try:
            parts = [float(x) for x in bbox_str.split(",")]
            if len(parts) == 4:
                xmin, ymin, xmax, ymax = parts
                bbox_geom = box(xmin, ymin, xmax, ymax)
                gdf = gdf[gdf.geometry.intersects(bbox_geom)]
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


# ═══════════════════════════════════════════════════════════
#  ENDPOINTS
# ═══════════════════════════════════════════════════════════

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
async def get_district(name: str):
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
async def get_boundary(name: str):
    """Get district boundary as GeoJSON."""
    name = name.lower()
    data = _load_district(name)
    bdf = data.get("boundary", gpd.GeoDataFrame())
    return JSONResponse(_gdf_to_geojson(bdf))


@app.get("/api/districts/{name}/{layer}")
async def get_layer(
    name: str,
    layer: str,
    bbox: str = Query(None, description="xmin,ymin,xmax,ymax in EPSG:4326"),
    zoom: int = Query(None, description="Current map zoom level"),
    limit: int = Query(None, description="Max features to return"),
):
    """
    Get a feature layer as GeoJSON.

    Supports:
    - layer: buildings, roads, waterbodies, openareas
    - bbox: spatial filter (xmin,ymin,xmax,ymax)
    - zoom: zoom-level-based feature limit for performance
    - limit: hard cap on features
    """
    name = name.lower()
    layer = layer.lower()

    if layer == "boundary":
        return await get_boundary(name)

    class_id = LAYER_CLASS_MAP.get(layer)
    if class_id is None:
        raise HTTPException(400, f"Unknown layer '{layer}'. Use: boundary, buildings, roads, waterbodies, openareas")

    data = _load_district(name)
    gdf = data.get("features", gpd.GeoDataFrame())

    t0 = time.time()
    filtered = _filter_features(gdf, class_id=class_id, bbox_str=bbox, zoom=zoom, limit=limit)
    elapsed = time.time() - t0

    geojson = _gdf_to_geojson(filtered)

    # Add metadata header
    geojson["metadata"] = {
        "district": name,
        "layer": layer,
        "count": len(geojson.get("features", [])),
        "total_in_district": int((gdf["class_id"] == class_id).sum()) if "class_id" in gdf.columns else 0,
        "query_time_ms": round(elapsed * 1000, 1),
        "bbox_filter": bbox is not None,
        "zoom_filter": zoom,
    }

    return JSONResponse(geojson)


@app.get("/api/districts/{name}/stats")
async def get_stats(name: str):
    """Get feature statistics for a district."""
    name = name.lower()
    data = _load_district(name)
    gdf = data.get("features", gpd.GeoDataFrame())

    stats = {}
    if "class_id" in gdf.columns:
        for cls_id, lname in CLASS_LAYER_MAP.items():
            subset = gdf[gdf["class_id"] == cls_id]
            layer_stats = {"count": len(subset)}

            # Area stats for buildings
            if "area_m2" in subset.columns and cls_id == 1:
                areas = subset["area_m2"].dropna()
                if len(areas) > 0:
                    layer_stats["total_area_m2"] = float(areas.sum())
                    layer_stats["avg_area_m2"] = float(areas.mean())
                    layer_stats["min_area_m2"] = float(areas.min())
                    layer_stats["max_area_m2"] = float(areas.max())

            # Road types
            if "fclass" in subset.columns and cls_id == 4:
                layer_stats["road_types"] = subset["fclass"].value_counts().to_dict()

            stats[lname] = layer_stats

    return {"district": name, "stats": stats}


# ── Tile cache: stores rendered PNG bytes keyed by (district, z, x, y) ──
_tile_cache = {}
_TILE_CACHE_MAX = 500  # ~50MB of tiles

EMPTY_PNG = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n\xb4\x00\x00\x00\x00IEND\xaeB`\x82'

# Legend-matching colormap
RASTER_COLORMAP = {
    1: (220, 38, 38, 200),    # Dark Red — Buildings High
    2: (249, 115, 22, 200),   # Orange — Buildings Med
    3: (251, 191, 36, 200),   # Amber — Buildings Low
    4: (234, 179, 8, 220),    # Yellow — Roads
    5: (59, 130, 246, 200),   # Blue — Waterbodies
    6: (156, 163, 175, 180),  # Gray — Open Areas
}


@app.get("/api/districts/{name}/raster/tiles/{z}/{x}/{y}.png")
async def get_raster_tile(name: str, z: int, x: int, y: int):
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
            
            for val, color in RASTER_COLORMAP.items():
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
