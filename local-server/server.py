"""Local data server: one folder in, map layers out.

Speaks the SAME API as gpu-server/ (/api/health, /imagery, /jobs,
/imagery/{id}/tiles/..., /results/{job}/{layer}.geojson), so the existing webapp
works against it unchanged — no frontend edits, no redeploy.

Drop files into the data folder and they are mapped automatically:
  *.tif *.tiff *.vrt            -> imagery (drone or satellite)
  *.gpkg *.geojson *.json *.shp -> result layers, attached to the imagery they
                                   overlap (buildings / roads / waterbodies /
                                   openareas / road_damage / change layers)
District, kind and capture year are read from the file name; resolution, size and
bounds are read from the raster itself. The folder is re-scanned automatically,
so new files appear without a restart.

No GPU and no database: the catalog is rebuilt in memory from the folder.
"""
import json
import os
import re
import threading
import time
from pathlib import Path

import numpy as np
import pyogrio
import rasterio
import shapely
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from rasterio.warp import transform_bounds
from rio_tiler.io import Reader
from rio_tiler.models import ImageData

DATA_DIR = Path(os.environ.get("GEO_DATA_DIR", r"D:\GEO_SERVE"))
RASTER_EXT = {".tif", ".tiff", ".vrt"}
VECTOR_EXT = {".gpkg", ".geojson", ".json", ".shp"}
RESCAN_SEC = 5.0
SETTLE = 20.0          # ignore files written less than this long ago
CACHE_DIR = Path(os.environ.get("GEO_CACHE_DIR", str(DATA_DIR) + "_cache"))
# 88, not 72: at 3-4 cm a tile is mostly fine detail, which is exactly what low
# WEBP quality destroys — kerbs, markings and potholes smear. The extra bytes are
# worth it, and cached tiles are served straight from disk anyway.
TILE_Q = int(os.environ.get("GEO_TILE_QUALITY", 88))
# Districts that get their own section in the webapp's layer rail.
DISTRICTS = ["ongole", "guntur", "vijayawada", "nellore", "kurnool", "tirupati",
             "kakinada", "rajahmundry", "anantapur", "kadapa", "eluru", "chittoor",
             "visakhapatnam", "narasaraopet", "tenali", "bapatla", "chirala"]
DEFAULT_DISTRICT = os.environ.get("GEO_DEFAULT_DISTRICT", "ongole").lower()

# Layer name -> pattern, most specific first (a "new buildings" file must not
# become plain "buildings", and "road damage" must not become "roads").
LAYER_RULES = [
    ("pothole_depth",     r"potholedepth|pothole_depth|depth"),
    ("waterlog",          r"waterlog|ponding|flooding"),
    ("encroachment",      r"encroach"),
    ("road_damage",       r"damage|pothole|crack"),
    ("new_buildings",     r"(new|added)[_\- ]*(building|bldg|footprint)|building[_\- ]*new"),
    ("removed_buildings", r"(removed|demolish|lost)[_\- ]*(building|bldg)|building[_\- ]*(removed|lost)"),
    ("new_roads",         r"(new|added)[_\- ]*road|road[_\- ]*new"),
    ("water_gained",      r"water[_\- ]*(gain|gained|increase)"),
    ("water_lost",        r"water[_\- ]*(lost|loss|decrease)"),
    ("buildings",         r"build|bldg|footprint|structure"),
    ("roads",             r"road|street|centre?line|carriageway|lane"),
    ("waterbodies",       r"water|lake|tank|pond|canal|river"),
    ("openareas",         r"open|vacant|plot|barren|empty"),
]
CHANGE_LAYERS = {"new_buildings", "removed_buildings", "new_roads", "water_gained", "water_lost"}
# Task each layer is published under, so the webapp's task filters match.
TASK_FOR = {"road_damage": "pothole", "pothole_depth": "pothole",
            "waterlog": "segment", "encroachment": "segment",
            **{k: "change" for k in CHANGE_LAYERS}}
GENERIC_TOKENS = {"ongole", "guntur", "final", "clip", "clipped", "output", "result",
                  "results", "data", "layer", "merged", "copy", "new", "old", "v1", "v2", "v3"}

_lock = threading.Lock()
_cat = {"imagery": [], "jobs": [], "unmapped": [], "scanned": 0, "sig": None}


# ── metadata from file names ─────────────────────────────────────
def _tokens(name):
    return {t for t in re.split(r"[^A-Za-z0-9]+", name.lower()) if len(t) > 1}


def _year(name):
    m = re.search(r"(?<!\d)(19[89]\d|20[0-4]\d)(?!\d)", name)
    return m.group(1) if m else None


def _district(path):
    hay = " ".join([path.stem, *[p.name for p in path.parents if p != DATA_DIR.parent]]).lower()
    for d in DISTRICTS:
        if d in hay:
            return d
    return DEFAULT_DISTRICT


def _kind(name):
    # Token match, not \b: "_" is a word character, so r"\bsat\b" never fires on
    # "ongole_2017_sat".
    if _tokens(name) & {"sat", "satellite", "sentinel", "worldview", "planet",
                        "pleiades", "wv2", "wv3", "spot", "esri", "bing"}:
        return "satellite"
    if re.search(r"satellite|sentinel|worldview|pleiades", name.lower()):
        return "satellite"
    return "drone"


def _layer_of(name):
    n = name.lower()
    for layer, pat in LAYER_RULES:
        if re.search(pat, n):
            return layer
    return None


# ── rasters ──────────────────────────────────────────────────────
def _gsd_m(src):
    gsd = abs(src.transform.a)
    if src.crs and src.crs.to_epsg() == 4326:        # degrees -> metres
        lat = (src.bounds.top + src.bounds.bottom) / 2
        gsd = gsd * 111320 * max(0.1, abs(np.cos(np.deg2rad(lat))))
    return gsd


def _describe_raster(p):
    with rasterio.open(p) as src:
        if not src.crs:
            raise ValueError("raster has no CRS")
        b = transform_bounds(src.crs, "EPSG:4326", *src.bounds, densify_pts=21)
        ov = src.overviews(1)
        return {
            "bounds": [round(v, 7) for v in b], "crs": src.crs.to_string(),
            "gsd_m": round(float(_gsd_m(src)), 4), "width": src.width, "height": src.height,
            "bands": src.count, "dtype": src.dtypes[0], "overviews": list(ov),
        }


# ── vectors ──────────────────────────────────────────────────────
def _vector_layers(p):
    """One dict per readable layer: name, normalised layer, 4326 bounds, CRS, fields."""
    out = []
    try:
        names = pyogrio.list_layers(p)
    except Exception:
        return out
    for row in np.atleast_2d(names):
        ln = str(row[0])
        try:
            info = pyogrio.read_info(p, layer=ln)
        except Exception:
            continue
        tb = info.get("total_bounds")
        if tb is None or any(v is None or not np.isfinite(v) for v in tb):
            continue
        crs = info.get("crs") or "EPSG:4326"
        try:
            b = transform_bounds(crs, "EPSG:4326", *tb, densify_pts=21)
        except Exception:
            continue
        if not all(np.isfinite(b)):
            continue
        # name the layer from the file name first, falling back to the layer name
        norm = _layer_of(Path(p).stem) or _layer_of(ln)
        out.append({"src_layer": ln, "layer": norm, "bounds": [float(v) for v in b],
                    "features": int(info.get("features") or 0),
                    "gtype": str(info.get("geometry_type") or ""),
                    "crs": str(crs),
                    # fields comes back as a numpy array: test for None, not truthiness
                    "fields": [str(f) for f in (info.get("fields")
                                                if info.get("fields") is not None else [])]})
    return out


def _overlap(vb, ib):
    """fraction of the vector's bbox that falls inside the imagery bbox"""
    w = max(0.0, min(vb[2], ib[2]) - max(vb[0], ib[0]))
    h = max(0.0, min(vb[3], ib[3]) - max(vb[1], ib[1]))
    va = max((vb[2] - vb[0]) * (vb[3] - vb[1]), 1e-12)
    return (w * h) / va


def _best_imagery(imgs, vpath, vbounds, vlayer):
    """Score every image and return the best host for this vector layer."""
    vt = _tokens(Path(vpath).stem) - GENERIC_TOKENS
    vy = _year(Path(vpath).stem)
    best, bestscore = None, 0.0
    for im in imgs:
        frac = _overlap(vbounds, im["bounds"])
        if frac < 0.05:
            continue
        s = 3.0 * frac
        iy = im.get("captured")
        if vy and iy:
            s += 5.0 if vy == iy else -6.0
        s += 2.0 * len(vt & (_tokens(im["name"]) - GENERIC_TOKENS))
        if im.get("district") and im["district"] in Path(vpath).stem.lower():
            s += 1.0
        # a change layer belongs on the newer image of a pair
        if vlayer in CHANGE_LAYERS and iy:
            s += 0.5
        if s > bestscore:
            best, bestscore = im, s
    return best, bestscore


# ── folder scan ──────────────────────────────────────────────────
def _usable(p):
    # Anything under a folder starting with "_" is working data, not a layer to
    # publish: _kb holds the road signal, raw model output and knowledge bases,
    # and they were being served as imagery and as a damage layer.
    if any(part.startswith("_") for part in p.relative_to(DATA_DIR).parts[:-1]):
        return False
    """A data file that is not still being written.

    GDAL writes .tmp/partial files while converting; picking one up mid-write
    publishes a broken 12 MB "image" to the map, so anything touched in the last
    SETTLE seconds is ignored until it stops changing.
    """
    if not p.is_file() or p.name.startswith("."):
        return False
    if p.suffix.lower() not in (RASTER_EXT | VECTOR_EXT):
        return False
    if any(p.name.lower().endswith(x) for x in (".tmp", ".aux.xml", ".part", ".download")):
        return False
    try:
        return time.time() - p.stat().st_mtime > SETTLE
    except OSError:
        return False


def _signature():
    sig = []
    if not DATA_DIR.is_dir():
        return None
    for p in sorted(DATA_DIR.rglob("*")):
        if _usable(p):
            try:
                st = p.stat()
            except OSError:
                continue
            sig.append((str(p), int(st.st_mtime), st.st_size))
    return tuple(sig)


def _scan():
    """Rebuild the catalog from the data folder."""
    imgs, unmapped = [], []
    if not DATA_DIR.is_dir():
        return {"imagery": [], "jobs": [], "unmapped": [f"data folder not found: {DATA_DIR}"],
                "scanned": time.time(), "sig": None}

    files = [p for p in sorted(DATA_DIR.rglob("*")) if _usable(p)]
    # optional overrides: meta.json = {"<file name or stem>": {district, kind, captured, name}}
    over = {}
    mp = DATA_DIR / "meta.json"
    if mp.is_file():
        try:
            over = {k.lower(): v for k, v in json.loads(mp.read_text(encoding="utf-8")).items()}
        except Exception as e:
            unmapped.append(f"meta.json unreadable: {e}")

    for p in [f for f in files if f.suffix.lower() in RASTER_EXT]:
        try:
            meta = _describe_raster(p)
        except Exception as e:
            unmapped.append(f"{p.name}: unreadable raster ({str(e)[:70]})")
            continue
        o = over.get(p.name.lower()) or over.get(p.stem.lower()) or {}
        st = p.stat()
        img = {
            "id": re.sub(r"[^a-z0-9]+", "-", p.stem.lower()).strip("-") or "img",
            "name": o.get("name") or p.stem,
            "kind": (o.get("kind") or _kind(p.stem)).lower(),
            "district": (o.get("district") or _district(p)).lower(),
            "captured": o.get("captured") or _year(p.stem),
            "status": "ready", "error": None,
            "source_bytes": st.st_size, "cog_bytes": st.st_size,
            "created": st.st_mtime, "_path": str(p),
            **meta,
        }
        if not meta["overviews"] and max(meta["width"], meta["height"]) > 8000:
            img["error"] = "no internal overviews — low zoom levels will be slow"
        imgs.append(img)

    # unique ids even if two folders hold the same stem
    seen = {}
    for im in imgs:
        n = seen.get(im["id"], 0)
        seen[im["id"]] = n + 1
        if n:
            im["id"] = f"{im['id']}-{n + 1}"

    # vectors -> synthetic jobs on their host imagery
    byjob = {}
    for p in [f for f in files if f.suffix.lower() in VECTOR_EXT]:
        lays = _vector_layers(str(p))
        if not lays:
            unmapped.append(f"{p.name}: no readable vector layers")
            continue
        for L in lays:
            ln, norm, vb, nfeat, gtype = (L["src_layer"], L["layer"], L["bounds"],
                                          L["features"], L["gtype"])
            if not norm:
                unmapped.append(f"{p.name}:{ln}: cannot tell which layer this is "
                                f"(name it *buildings*, *roads*, *water*, *open* or *damage*)")
                continue
            host, sc = _best_imagery(imgs, str(p), vb, norm)
            if not host:
                unmapped.append(f"{p.name}:{ln} ({norm}): no imagery overlaps it")
                continue
            task = TASK_FOR.get(norm, "segment")
            jid = f"{host['id']}-{task}"
            j = byjob.setdefault(jid, {
                "id": jid, "imagery_id": host["id"], "task": task, "status": "done",
                "progress": 1.0, "message": "from local folder", "device": "local",
                "layers": {}, "stats": {}, "params": {},
                "created": time.time(), "started": time.time(), "finished": time.time(),
                "_sources": {},
            })
            # Two layers can normalise to the same name (road_centerlines and
            # road_polygons both -> "roads"). The webapp draws a fill, so the
            # polygon layer wins; otherwise the richer one does.
            if norm in j["_sources"]:
                new_poly = "polygon" in gtype.lower()
                old_poly = "polygon" in j["_sources"][norm]["gtype"].lower()
                if new_poly != old_poly:
                    keep_new = new_poly
                else:
                    keep_new = nfeat > j["layers"][norm]
                if not keep_new:
                    continue
            j["layers"][norm] = nfeat
            j["_sources"][norm] = {"path": str(p), "layer": ln, "gtype": gtype,
                                   "crs": L["crs"], "fields": L["fields"]}

    return {"imagery": imgs, "jobs": list(byjob.values()), "unmapped": unmapped,
            "scanned": time.time(), "sig": _signature()}


def refresh(force=False):
    with _lock:
        if not force and time.time() - _cat["scanned"] < RESCAN_SEC:
            return _cat
        sig = _signature()
        if not force and sig == _cat["sig"]:
            _cat["scanned"] = time.time()
            return _cat
        new = _scan()
        _cat.update(new)
        print(f"[scan] {len(new['imagery'])} imagery, "
              f"{sum(len(j['layers']) for j in new['jobs'])} result layers"
              + (f", {len(new['unmapped'])} unmapped" if new["unmapped"] else ""), flush=True)
        return _cat


def _watcher():
    while True:
        time.sleep(RESCAN_SEC)
        try:
            refresh()
        except Exception as e:
            print("[scan] failed:", e, flush=True)


# ── tiles ────────────────────────────────────────────────────────
# One open dataset PER THREAD. A GDAL dataset is not thread-safe: sharing a
# single Reader across the server's worker threads crashes the process outright
# (no traceback — it dies mid-request), and the map asks for 10-20 tiles at once,
# so it was only a matter of time. Thread-local readers keep the speed of a warm
# handle without the sharing.
_tls = threading.local()


def _reader(path):
    cache = getattr(_tls, "readers", None)
    if cache is None:
        cache = _tls.readers = {}
    r = cache.get(path)
    if r is None:
        r = cache[path] = Reader(path)
    return r


def _tile(path, z, x, y, fmt="webp", size=256):
    img = _reader(path).tile(x, y, z, tilesize=size)
    arr = img.data
    if arr.shape[0] >= 4:                      # RGBA drone imagery: band 4 is alpha
        img = ImageData(arr[:3], mask=arr[3], crs=img.crs, bounds=img.bounds)
    elif arr.dtype != np.uint8:                # DSM / index rasters: stretch to 8-bit
        v = arr[0][img.mask > 0] if img.mask is not None and (img.mask > 0).any() else arr[0].ravel()
        if v.size:
            lo, hi = np.percentile(v, [2, 98])
            img = img.rescale(in_range=((float(lo), float(hi) if hi > lo else float(lo) + 1),))
    if fmt == "png":
        return img.render(img_format="PNG")
    return img.render(img_format="WEBP", quality=TILE_Q)


def _cached_tile(img_id, path, z, x, y, fmt, size):
    """Render once, then serve from disk. Panning back over an area is instant."""
    # quality is part of the key: changing it must not serve tiles rendered at
    # the old setting, and old ones simply fall out of use rather than needing
    # the cache to be wiped.
    key = CACHE_DIR / f"{img_id}@q{TILE_Q}" / str(size) / str(z) / str(x) / f"{y}.{fmt}"
    try:
        if key.is_file() and key.stat().st_mtime >= os.path.getmtime(path):
            return key.read_bytes()
    except OSError:
        pass
    data = _tile(path, z, x, y, fmt=fmt, size=size)
    if data:
        try:
            key.parent.mkdir(parents=True, exist_ok=True)
            tmp = key.with_suffix(key.suffix + ".part")
            tmp.write_bytes(data)
            os.replace(tmp, key)
        except OSError:
            pass
    return data


# ── app ──────────────────────────────────────────────────────────
app = FastAPI(title="local geo server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in os.environ.get("CORS_ORIGINS", "").split(",") if o] or ["*"],
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
)


@app.middleware("http")
async def private_network(request, call_next):
    """Chrome needs this to let an https:// page call a server on the local network."""
    resp = await call_next(request)
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    return resp


def _public(d):
    return {k: v for k, v in d.items() if not k.startswith("_")}


@app.get("/")
@app.get("/health")
@app.get("/api/health")
def health():
    c = refresh()
    return {
        "status": "ok", "server": "local", "device": "cpu",
        "data_dir": str(DATA_DIR),
        "imagery": len(c["imagery"]),
        "layers": sum(len(j["layers"]) for j in c["jobs"]),
        "unmapped": c["unmapped"],
        "capabilities": ["imagery", "jobs"],
    }


@app.get("/catalog")
def catalog():
    """What was found in the folder and how it was mapped — the thing to check first."""
    c = refresh()
    return {
        "data_dir": str(DATA_DIR),
        "imagery": [{
            "id": i["id"], "name": i["name"], "kind": i["kind"], "district": i["district"],
            "captured": i["captured"], "gsd_cm": round(i["gsd_m"] * 100, 1),
            "size": f"{i['width']}x{i['height']}", "bands": i["bands"],
            "gb": round(i["source_bytes"] / 2 ** 30, 2), "overviews": i["overviews"],
            "note": i["error"], "file": Path(i["_path"]).name,
        } for i in c["imagery"]],
        "results": [{
            "job": j["id"], "imagery_id": j["imagery_id"], "task": j["task"],
            "layers": {k: {"features": v, "file": Path(j["_sources"][k]["path"]).name,
                           "src_layer": j["_sources"][k]["layer"]} for k, v in j["layers"].items()},
        } for j in c["jobs"]],
        "unmapped": c["unmapped"],
    }


@app.get("/imagery")
def imagery_list():
    return [_public(i) for i in refresh()["imagery"]]


@app.get("/imagery/{img_id}")
def imagery_get(img_id: str):
    for i in refresh()["imagery"]:
        if i["id"] == img_id:
            return _public(i)
    raise HTTPException(404, "imagery not found")


@app.get("/imagery/{img_id}/tiles/{z}/{x}/{y}.{fmt}")
def imagery_tile(img_id: str, z: int, x: int, y: int, fmt: str, size: int = Query(256)):
    img = next((i for i in refresh()["imagery"] if i["id"] == img_id), None)
    if not img:
        raise HTTPException(404, "imagery not found")
    try:
        data = _cached_tile(img_id, img["_path"], z, x, y, fmt, 512 if size >= 512 else 256)
    except Exception:
        # outside coverage, or a bad read: an empty tile, never a CORS-less 500
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    if data is None:
        return Response(status_code=204)
    return Response(data, media_type="image/webp" if fmt == "webp" else "image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/jobs")
def jobs_list():
    return [_public(j) for j in refresh()["jobs"]]


@app.get("/jobs/{jid}")
def job_get(jid: str):
    for j in refresh()["jobs"]:
        if j["id"] == jid:
            return _public(j)
    raise HTTPException(404, "job not found")


EMPTY = {"type": "FeatureCollection", "features": []}


@app.get("/results/{jid}/{layer}.geojson")
def result_layer(jid: str, layer: str, bbox: str = Query(None), limit: int = Query(20000),
                 zoom: float = Query(None), on_road: int = Query(None)):
    job = next((j for j in refresh()["jobs"] if j["id"] == jid), None)
    if not job or layer not in job["_sources"]:
        return JSONResponse(EMPTY)
    src = job["_sources"][layer]
    bb = tuple(float(v) for v in bbox.split(",")) if bbox else None
    # The map sends WGS84, but pyogrio reads bbox in the LAYER's CRS — and these
    # files are usually UTM. Without this the query silently returns nothing.
    if bb and src["crs"] and "4326" not in src["crs"]:
        try:
            bb = tuple(transform_bounds("EPSG:4326", src["crs"], *bb, densify_pts=21))
        except Exception:
            bb = None
    try:
        where = None
        if on_road is not None and layer == "road_damage" and "on_road" in src["fields"]:
            where = f"on_road = {1 if on_road else 0}"
        df = pyogrio.read_dataframe(src["path"], layer=src["layer"], bbox=bb, where=where,
                                    max_features=max(1, min(limit, 200000)))
    except Exception:
        return JSONResponse(EMPTY)
    if not len(df):
        return JSONResponse(EMPTY)
    if df.crs and df.crs.to_epsg() != 4326:
        df = df.to_crs(4326)
    geoms = df.geometry.values
    if zoom is not None and float(zoom) < 16:
        geoms = shapely.simplify(geoms, 360.0 / (512 * 2 ** float(zoom)) / 2, preserve_topology=False)
    geoms = shapely.transform(geoms, lambda c: np.round(c, 7))
    gj = shapely.to_geojson(geoms)
    if len(df.columns) > 1:
        props = df.drop(columns=df.geometry.name).to_json(orient="records", lines=True).splitlines()
    else:
        props = ["{}"] * len(df)
    feats = ",".join('{"type":"Feature","properties":%s,"geometry":%s}' % (pr, g)
                     for pr, g in zip(props, gj) if g)
    return Response('{"type":"FeatureCollection","features":[' + feats + ']}',
                    media_type="application/json")


@app.get("/results/{jid}/download")
def result_download(jid: str):
    job = next((j for j in refresh()["jobs"] if j["id"] == jid), None)
    if not job or not job["_sources"]:
        raise HTTPException(404, "no results")
    p = next(iter(job["_sources"].values()))["path"]
    return FileResponse(p, filename=Path(p).name)


# ── ask ──────────────────────────────────────────────────────────
@app.get("/ask")
def ask_get(q: str = Query(..., min_length=2, max_length=400)):
    """Plain-English question -> answer computed from the road knowledge base."""
    import ask as _ask
    try:
        return _ask.run(q, str(DATA_DIR))
    except Exception as e:
        print("[ask]", repr(e)[:160], flush=True)
        return JSONResponse({"ok": False, "answer": "The question could not be answered.",
                             "error": str(e)[:200], "roads": []}, status_code=200)


@app.get("/ask/health")
def ask_health():
    import ask as _ask
    data = _ask.kb(str(DATA_DIR))
    return {"ok": bool(data), "model": _ask.MODEL, "ollama": _ask.ollama_up(),
            "roads": (data or {}).get("totals", {})}


@app.on_event("startup")
def _startup():
    print(f"[local-server] data folder: {DATA_DIR}", flush=True)
    refresh(force=True)
    for u in _cat["unmapped"]:
        print("  ! " + u, flush=True)
    threading.Thread(target=_watcher, daemon=True).start()
