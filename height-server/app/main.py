"""Building Height Server - an independent elevation service on the DSM machine.

It owns the DSM and DTM folders and nothing else. The HiSup machine detects
footprints from imagery and asks this server how tall each one is; neither
machine needs the other's data, and either can be restarted without the other
noticing beyond a failed request.

    POST /height     footprints in  ->  height, floors, ground elevation out
    POST /extract    no footprints to hand: find them in the DSM instead
    GET  /health     is it up, and what does it have
    GET  /tiles      every indexed clip, with bounds and resolution
    POST /reindex    pick up files added to the folders since startup
"""
import logging
import socket
import time

import shapely
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from shapely.geometry import box, shape

from . import config, heights
from .tiles import INDEX

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("height")

app = FastAPI(title="Building Height Server", version="1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=config.CORS_ORIGINS or ["*"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)


def require_key(x_api_key: str = Header(default="")):
    """No-op when HS_API_KEY is unset, which is the normal case on a LAN."""
    if config.API_KEY and x_api_key != config.API_KEY:
        raise HTTPException(401, "bad or missing X-API-Key")
    return True


def trusted(x_api_key: str = Header(default="")):
    """Whether to answer in full, for endpoints that stay open either way.

    /health has to work without a key so anything can check liveness. But it
    reports local folder paths and the bounds of the survey, and once this is
    behind a public tunnel there is no reason to hand that to whoever finds
    the hostname. With no key configured - a LAN run - everything is trusted
    and nothing changes.
    """
    return (not config.API_KEY) or x_api_key == config.API_KEY


def lan_ip():
    """The address the other laptop should be pointed at."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))     # no packet is sent; this just picks a route
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@app.on_event("startup")
def startup():
    t0 = time.time()
    s = INDEX.build()
    ip = lan_ip()
    log.info("DSM clips: %s   DTM clips: %s   (%.1fs)",
             s["dsm"].get("count", 0), s["dtm"].get("count", 0), time.time() - t0)
    if s["errors"]:
        for e in s["errors"]:
            log.error("INDEX: %s", e)
    log.info("=" * 62)
    log.info("  Point the HiSup machine at:  http://%s:%d", ip, config.PORT)
    log.info("  On that machine:   set HEIGHT_SERVER=http://%s:%d", ip, config.PORT)
    log.info("=" * 62)


@app.on_event("shutdown")
def shutdown():
    INDEX.close_all()


@app.get("/", response_class=HTMLResponse)
def home(full: bool = Depends(trusted)):
    if not full:
        return ("<!doctype html><meta charset=utf-8>"
                "<title>Building Height Server</title>"
                "<style>body{font:15px system-ui;margin:3rem auto;max-width:34rem;"
                "color:#0f172a}</style><h1>Building Height Server</h1>"
                "<p>Running. This endpoint needs an API key.</p>")
    s = INDEX.summary()
    rows = "".join(
        f"<tr><td>{k}</td><td>{v}</td></tr>"
        for k, v in [("DSM folder", s["dsm_dir"]), ("DTM folder", s["dtm_dir"]),
                     ("DSM clips", s["dsm"].get("count", 0)),
                     ("DTM clips", s["dtm"].get("count", 0)),
                     ("DTM covers", f"{s['dtm_covers_pct_of_dsm']}% of DSM")])
    err = "".join(f"<li>{e}</li>" for e in s["errors"])
    return (
        "<!doctype html><meta charset=utf-8>"
        "<title>Building Height Server</title>"
        "<style>body{font:15px system-ui;margin:3rem auto;max-width:44rem;"
        "color:#0f172a}td{padding:.3rem .8rem .3rem 0}code{background:#eef2ff;"
        "padding:.1rem .3rem;border-radius:3px}</style>"
        "<h1>Building Height Server</h1><p>Running. It serves height and storey "
        "counts for footprints posted to <code>/height</code>.</p>"
        f"<table>{rows}</table>"
        + (f"<h3>Problems</h3><ul>{err}</ul>" if err else "")
        + "<p><a href='/health'>/health</a> &middot; <a href='/tiles'>/tiles</a> "
          "&middot; <a href='/docs'>/docs</a></p>"
    )


@app.get("/health")
def health(full: bool = Depends(trusted)):
    s = INDEX.summary()
    out = {"status": "ok" if s["dsm"].get("count") else "no_dsm_data",
           "service": "building-height"}
    if not full:
        # Enough to see it is alive and has data, with no paths and no bounds.
        out["authenticated"] = False
        out["has_dsm"] = bool(s["dsm"].get("count"))
        return out
    out.update({
        "lan_url": f"http://{lan_ip()}:{config.PORT}",
        "index": s,
        "thresholds": {
            "floor_height_m": config.FLOOR_HEIGHT,
            "min_height_m": config.MIN_HEIGHT,
            "edge_buffer_m": config.EDGE_BUFFER_M,
            "rough_m": config.ROUGH_M,
            "ring_m": [config.RING_INNER_M, config.RING_OUTER_M],
        },
    })
    return out


@app.get("/tiles", dependencies=[Depends(require_key)])
def tiles():
    keep = ("name", "kind", "crs", "bounds_wgs84", "res", "width", "height", "dtype")
    return {
        "dsm": [{k: t[k] for k in keep if k in t} for t in INDEX.dsm],
        "dtm": [{k: t[k] for k in keep if k in t} for t in INDEX.dtm],
    }


@app.post("/reindex")
def reindex(_=Depends(require_key)):
    INDEX.close_all()
    return INDEX.build(force=True)


@app.post("/height")
def height(
    payload: dict = Body(..., description="GeoJSON FeatureCollection, Feature, "
                                         "geometry, or {'geometries': [...]}"),
    edge_buffer_m: float = Query(None, description="shrink before sampling; "
                                                   "default from HS_EDGE_BUFFER_M"),
    ring: bool = Query(True, description="also estimate ground outside the "
                                         "footprint, as fallback and cross-check"),
    _=Depends(require_key),
):
    """Attach height, storeys and ground elevation to footprints.

    Input geometry is EPSG:4326, which is what HiSup emits. The response keeps
    the input order and echoes the original properties, so the caller can merge
    by index without carrying an id scheme across machines.
    """
    feats = _features(payload)
    if not feats:
        raise HTTPException(400, "no usable geometry in the request")
    if len(feats) > 20000:
        raise HTTPException(413, f"{len(feats)} features in one request; send "
                                 "batches of about 2000")

    geoms, props = [], []
    for f in feats:
        geoms.append(f[0])
        props.append(f[1])

    t0 = time.time()
    rows = heights.measure_many(geoms, edge_buffer=edge_buffer_m, want_ring=ring)
    dt = time.time() - t0

    out = []
    for g, p, r in zip(geoms, props, rows):
        merged = dict(p)
        merged.update(r)
        out.append({"type": "Feature", "geometry": shapely.geometry.mapping(g),
                    "properties": merged})
    return {
        "type": "FeatureCollection",
        "features": out,
        "summary": heights.summarise(rows),
        "seconds": round(dt, 2),
        "rate_per_s": round(len(rows) / dt, 1) if dt > 0 else None,
    }


@app.post("/extract")
def extract(
    bounds: list = Body(..., embed=True,
                        description="[west, south, east, north] in EPSG:4326"),
    workers: int = Query(4),
    _=Depends(require_key),
):
    """Find buildings in the DSM itself, for ground with no imagery detection.

    This is the original suite's method - nDSM threshold plus roughness filter.
    It is the weaker of the two paths and exists for coverage, not accuracy:
    prefer posting HiSup footprints to /height.
    """
    from .extract import extract_bbox
    if len(bounds) != 4:
        raise HTTPException(400, "bounds must be [west, south, east, north]")
    g = box(*bounds)
    if not INDEX.covering(g, "DSM"):
        raise HTTPException(404, "no DSM clip covers that area")
    t0 = time.time()
    fc = extract_bbox(g, workers=workers)
    fc["seconds"] = round(time.time() - t0, 2)
    return fc


def _features(payload):
    """Accept any of the shapes a caller might reasonably send."""
    if not isinstance(payload, dict):
        return []
    raw = []
    t = payload.get("type")
    if t == "FeatureCollection":
        raw = payload.get("features") or []
    elif t == "Feature":
        raw = [payload]
    elif t in ("Polygon", "MultiPolygon"):
        raw = [{"type": "Feature", "geometry": payload, "properties": {}}]
    elif "geometries" in payload:
        raw = [{"type": "Feature", "geometry": g, "properties": {}}
               for g in payload["geometries"]]
    elif "features" in payload:
        raw = payload["features"] or []

    out = []
    for f in raw:
        geom = f.get("geometry") if isinstance(f, dict) else None
        if not geom:
            continue
        try:
            g = shape(geom)
        except Exception:
            continue
        if g.is_empty:
            continue
        if not g.is_valid:
            g = g.buffer(0)
        if g.is_empty:
            continue
        out.append((g, f.get("properties") or {}))
    return out
