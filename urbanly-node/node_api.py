"""The peer service: what one laptop tells another about itself.

Every machine running Urbanly starts one of these. It answers two questions
and nothing else:

    GET /node/status           what I am, what I have, how fresh it is
    GET /node/file?...         give me that one file

That is deliberately the whole surface. Sync is then a comparison of two
manifests and a series of ordinary downloads, which is something you can
follow in a log and interrupt without leaving anything half-written.

Rasters are listed but never offered for transfer. The Ongole orthomosaic is
42.7 GB; copying it between laptops over a tunnel is slower than re-flying the
survey, and both machines already hold it. What actually changes day to day is
the model checkpoint and the vector layers, and those are small.
"""
import hashlib
import os
import platform
import socket
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse

import config

app = FastAPI(title="Urbanly node", version="1.0")

RASTER = {".tif", ".tiff", ".vrt", ".img", ".jp2"}
VECTOR = {".gpkg", ".geojson", ".json", ".shp", ".csv"}
WEIGHT = {".pth", ".pt", ".onnx", ".safetensors"}


def require_key(x_api_key: str = Header(default="")):
    if config.API_KEY and x_api_key != config.API_KEY:
        raise HTTPException(401, "bad or missing X-API-Key")
    return True


def quick_hash(p: Path, cap: int = 4 << 20) -> str:
    """Size plus the head and tail of the file.

    A full hash of 1.5 GB of weights costs seconds on every status call, and
    this is for deciding whether two files differ, not for proving one is
    untampered. Size plus both ends catches every realistic case: a re-trained
    checkpoint, a re-exported layer, a truncated copy.
    """
    h = hashlib.sha256()
    size = p.stat().st_size
    h.update(str(size).encode())
    with open(p, "rb") as fh:
        h.update(fh.read(cap))
        if size > 2 * cap:
            fh.seek(-cap, os.SEEK_END)
            h.update(fh.read(cap))
    return h.hexdigest()[:16]


def describe(p: Path, root: Path, kind: str) -> dict:
    st = p.stat()
    return {
        "name": str(p.relative_to(root)).replace(os.sep, "/"),
        "kind": kind,
        "bytes": st.st_size,
        "mtime": int(st.st_mtime),
        "fingerprint": quick_hash(p),
    }


def manifest() -> dict:
    """Everything this node holds that is worth comparing with a peer."""
    out = {"layers": [], "weights": [], "rasters": []}

    data = config.DATA_DIR
    if data.exists():
        for p in sorted(data.rglob("*")):
            if not p.is_file() or any(part.startswith("_") for part in
                                      p.relative_to(data).parts[:-1]):
                continue
            ext = p.suffix.lower()
            if ext in VECTOR and p.stat().st_size <= config.MAX_SYNC_BYTES:
                out["layers"].append(describe(p, data, "layer"))
            elif ext in RASTER:
                # Listed so a peer can see coverage, never offered for transfer.
                st = p.stat()
                out["rasters"].append({"name": p.name, "bytes": st.st_size,
                                       "mtime": int(st.st_mtime)})

    w = config.WEIGHTS_DIR
    if w.exists():
        for p in sorted(w.iterdir()):
            if p.is_file() and p.suffix.lower() in WEIGHT:
                out["weights"].append(describe(p, w, "weight"))
    return out


def gpu_info() -> dict:
    try:
        import torch
        if not torch.cuda.is_available():
            return {"available": False, "device": "cpu"}
        i = torch.cuda.current_device()
        free, total = torch.cuda.mem_get_info(i)
        return {"available": True, "device": f"cuda:{i}",
                "name": torch.cuda.get_device_name(i),
                "free_gb": round(free / 1e9, 2), "total_gb": round(total / 1e9, 2)}
    except Exception as e:
        return {"available": False, "device": "unknown", "error": str(e)[:120]}


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@app.get("/node/status")
def status(full: bool = Query(True), x_api_key: str = Header(default="")):
    """What this machine is and what it holds.

    Open without a key, but reduced: a peer deciding whether to sync needs the
    manifest, while anything that merely wants to know the node is alive does
    not need a list of its files.
    """
    trusted = (not config.API_KEY) or x_api_key == config.API_KEY
    g = gpu_info()
    out = {
        "node": config.NODE_NAME,
        "host": platform.node(),
        "lan_url": f"http://{lan_ip()}:{config.NODE_PORT}",
        "gpu": g,
        "time": int(time.time()),
        "authenticated": trusted,
    }
    if not trusted:
        return out
    m = manifest()
    out["data_dir"] = str(config.DATA_DIR)
    out["weights_dir"] = str(config.WEIGHTS_DIR)
    out["counts"] = {k: len(v) for k, v in m.items()}
    out["bytes"] = {k: sum(x["bytes"] for x in v) for k, v in m.items()}
    if full:
        out["manifest"] = m
    return out


@app.get("/node/file")
def get_file(kind: str, name: str, x_api_key: str = Header(default="")):
    """Serve one layer or one weight file.

    `name` is matched against the manifest rather than joined onto a path, so a
    request cannot walk out of the data directory however it is spelled.
    """
    require_key(x_api_key)
    if kind not in ("layer", "weight"):
        raise HTTPException(400, "kind must be 'layer' or 'weight'")
    m = manifest()
    entries = m["layers"] if kind == "layer" else m["weights"]
    hit = next((e for e in entries if e["name"] == name), None)
    if hit is None:
        raise HTTPException(404, f"no {kind} called {name!r} on this node")
    root = config.DATA_DIR if kind == "layer" else config.WEIGHTS_DIR
    path = root / hit["name"]
    if not path.is_file():
        raise HTTPException(404, "file vanished between listing and request")
    return FileResponse(path, filename=path.name,
                        media_type="application/octet-stream")
