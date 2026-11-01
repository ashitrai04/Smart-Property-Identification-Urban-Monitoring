r"""Where the elevation data is, and the thresholds used to read it.

Every value is overridable from the environment so the same zip runs on a
machine whose folders are laid out differently, without editing code:

    set HS_DSM_DIR=D:\survey\dsm
    set HS_DTM_DIR=D:\survey\dtm
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The two folders of clip-wise rasters. These defaults are the laptop this
# package was built for; override with HS_DSM_DIR / HS_DTM_DIR anywhere else.
DSM_DIR = Path(os.environ.get("HS_DSM_DIR", r"C:\Users\Asus\Downloads\DSM Data"))
DTM_DIR = Path(os.environ.get("HS_DTM_DIR", r"C:\Users\Asus\Downloads\DTM Data"))

CACHE = Path(os.environ.get("HS_CACHE", ROOT / "cache"))
CACHE.mkdir(parents=True, exist_ok=True)
INDEX_PATH = CACHE / "tile_index.json"

HOST = os.environ.get("HS_HOST", "0.0.0.0")
PORT = int(os.environ.get("HS_PORT", "8900"))

# Optional. Set the same value here and as HEIGHT_API_KEY on the HiSup machine
# to stop anything else on the LAN querying the elevation data. Empty = open,
# which is fine on a trusted network.
API_KEY = os.environ.get("HS_API_KEY", "")

CORS_ORIGINS = [o.strip() for o in os.environ.get("HS_CORS_ORIGINS", "*").split(",") if o.strip()]

# ── measurement thresholds ──────────────────────────────────────────
# Kept identical to DSM_DTM_Building_Height_Suite so the numbers this server
# returns are comparable with the 1,841-building run already produced.
FLOOR_HEIGHT = float(os.environ.get("HS_FLOOR_HEIGHT", "3.0"))
FLOOR_OFFSET = float(os.environ.get("HS_FLOOR_OFFSET", "1.2"))
MIN_HEIGHT = float(os.environ.get("HS_MIN_HEIGHT", "2.2"))
MAX_HEIGHT = float(os.environ.get("HS_MAX_HEIGHT", "60.0"))

# A footprint traced from RGB includes the wall face and a little shadow, and
# those pixels sit at ground level - averaging them in drags a roof down by a
# metre or more. Shrink the polygon before sampling so only roof is measured.
EDGE_BUFFER_M = float(os.environ.get("HS_EDGE_BUFFER_M", "0.5"))

# Ground estimated from a ring OUTSIDE the footprint, used where no DTM tile
# covers the building, and as a cross-check on the DTM where one does.
RING_INNER_M = float(os.environ.get("HS_RING_INNER_M", "1.0"))
RING_OUTER_M = float(os.environ.get("HS_RING_OUTER_M", "8.0"))

# Below this many roof pixels the percentiles are not meaningful.
MIN_PIXELS = int(os.environ.get("HS_MIN_PIXELS", "25"))

# Spread of nDSM inside one footprint, above which the result is flagged
# "rough". High spread means a tree overhanging the roof, or two buildings
# merged into one polygon. This is ADVISORY and the default is deliberately
# loose: real roofs carry parapets, stairwells and water tanks, and a tight
# threshold flags more honest buildings than it catches problems. Tighten it
# once you have seen the distribution on your own data.
ROUGH_M = float(os.environ.get("HS_ROUGH_M", "2.5"))

RASTER_EXT = {".tif", ".tiff", ".vrt", ".img", ".jp2"}
