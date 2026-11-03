r"""Where this node's data lives, and who its peers are.

Everything is overridable from config.env so the same folder runs unchanged on
either laptop - which is the point of the package. Only the paths and the peer
list differ between machines.
"""
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent


def _load_env():
    """Read config.env as KEY=VALUE, without overriding the real environment."""
    p = HERE / "config.env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.split(" #", 1)[0].strip())


_load_env()

NODE_NAME = os.environ.get("UN_NODE_NAME", os.environ.get("COMPUTERNAME", "node"))

# The survey data folder. Both laptops hold the same layout; only the drive
# letter or user folder differs.
DATA_DIR = Path(os.environ.get("UN_DATA_DIR", r"D:\GEO_SERVE"))
WEIGHTS_DIR = Path(os.environ.get("UN_WEIGHTS_DIR", REPO / "gpu-server" / "weights"))

GPU_SERVER_DIR = Path(os.environ.get("UN_GPU_SERVER_DIR", REPO / "gpu-server"))
DATA_SERVER_DIR = Path(os.environ.get("UN_DATA_SERVER_DIR", REPO / "local-server"))

NODE_PORT = int(os.environ.get("UN_NODE_PORT", "8790"))
GPU_PORT = int(os.environ.get("UN_GPU_PORT", "8800"))
DATA_PORT = int(os.environ.get("UN_DATA_PORT", "8777"))

# Shared between the laptops. Without it the file endpoints are open to anyone
# who can reach the port, which over a tunnel is anyone at all.
API_KEY = os.environ.get("UN_API_KEY", "")

# Other machines running this same package. Comma-separated, each a base URL
# of that machine's node service. A Tailscale address keeps working when the
# laptops are on different networks.
PEERS = [p.strip().rstrip("/") for p in os.environ.get("UN_PEERS", "").split(",")
         if p.strip()]

# Files larger than this are listed but never transferred. Orthomosaics live
# well above it on purpose - see the note in node_api.
MAX_SYNC_BYTES = int(os.environ.get("UN_MAX_SYNC_BYTES", str(3 * 1024 ** 3)))

SYNC_TIMEOUT = float(os.environ.get("UN_SYNC_TIMEOUT", "900"))
PROBE_TIMEOUT = float(os.environ.get("UN_PROBE_TIMEOUT", "6"))
