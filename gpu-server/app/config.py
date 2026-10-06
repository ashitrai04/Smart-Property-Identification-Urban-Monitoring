"""Paths and settings. Everything is overridable from config.env (loaded by run.sh)."""
import os
from pathlib import Path

ROOT = Path(os.environ.get("SP_ROOT", Path(__file__).resolve().parent.parent))
DATA = Path(os.environ.get("SP_DATA", ROOT / "data"))
RAW = DATA / "raw"            # drop original drone / satellite files here (scp / rsync)
COGS = DATA / "cogs"          # compressed Cloud-Optimised GeoTIFFs served as map tiles
RESULTS = DATA / "results"    # per-job outputs (GeoPackage vectors, class rasters)
DB_PATH = DATA / "catalog.sqlite"
WEIGHTS = ROOT / "weights"

for d in (RAW, COGS, RESULTS):
    d.mkdir(parents=True, exist_ok=True)

API_KEY = os.environ.get("SP_API_KEY", "")          # required for ingest / jobs / deletes
CORS_ORIGINS = [o.strip() for o in os.environ.get(
    "SP_CORS_ORIGINS",
    "https://smart-property-identification-urban.vercel.app,http://localhost:5173,http://localhost:5179",
).split(",") if o.strip()]

# Compression for ingested imagery: JPEG (YCbCr) is ~10x smaller than raw RGB
# and visually lossless at q85; WEBP is smaller still where GDAL supports it.
COG_PROFILE = os.environ.get("SP_COG_PROFILE", "jpeg")      # jpeg | webp | deflate
COG_QUALITY = int(os.environ.get("SP_COG_QUALITY", "85"))

# Batch jobs over large orthomosaics
JOB_WINDOW = int(os.environ.get("SP_JOB_WINDOW", "1536"))   # px per window fed to SegFormer
POTHOLE_WINDOW = int(os.environ.get("SP_POTHOLE_WINDOW", "4096"))
