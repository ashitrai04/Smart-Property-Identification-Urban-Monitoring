"""Ask the other laptop how tall each detected building is.

The two machines stay independent on purpose. This one owns the imagery and
the HiSup weights; the other owns the DSM and DTM folders, which are far too
large to copy and would be stale the moment the survey is reflown. So neither
holds the other's data, and the only coupling is one HTTP call.

That means every path here has to survive the other laptop being off, which is
the normal state most of the time. A failed height lookup must never fail a
detection job: footprints without heights are still the product, heights are
an enrichment. So every entry point returns the footprints either way and says
plainly in `height_status` what happened.

Point this at the other machine with one variable:

    set HEIGHT_SERVER=http://192.168.1.42:8900
"""
import json
import logging
import os
import time

from fastapi import APIRouter, Body, HTTPException, Query

log = logging.getLogger("height-client")

router = APIRouter(prefix="/height", tags=["height"])

_SET = os.environ.get("HEIGHT_SERVER", "").strip()

# "auto" means: look the address up instead of being told it. The DSM laptop's
# free Cloudflare tunnel gets a new hostname every restart, so a pasted URL
# goes stale within a day; the height server publishes its current address to
# the same R2 bucket the GPU server uses for its own, and this reads it back.
AUTO = _SET.lower() == "auto"
SERVER = "" if AUTO else _SET.rstrip("/")
API_KEY = os.environ.get("HEIGHT_API_KEY", "")

R2_KEY = "runtime/height_server.json"
DISCOVER_TTL = float(os.environ.get("HEIGHT_DISCOVER_TTL", "120"))
_found = {"url": None, "key": None, "at": 0.0, "why": "not looked up yet"}

# The remote measures a few hundred footprints a second, but it reads windows
# off a spinning disk to do it. Chunking keeps any one request short enough to
# survive a flaky wifi link, and gives progress for a long run.
CHUNK = int(os.environ.get("HEIGHT_CHUNK", "2000"))
TIMEOUT = float(os.environ.get("HEIGHT_TIMEOUT", "180"))

# Properties the remote adds. Listed so a merge can be undone, and so the
# webapp and QGIS plugin have one place to read the schema from.
FIELDS = ("height_m", "mean_height_m", "max_height_m", "min_height_m",
          "floors", "floor_label", "ground_elev_m", "roof_elev_m",
          "height_source", "dtm_minus_ring_m", "n_pixels", "gsd_m",
          "roughness_m", "dsm_tile", "dtm_tile", "quality")


def configured():
    return bool(SERVER or AUTO)


def _headers(key):
    return {"X-API-Key": key} if key else {}


def _r2_env():
    """Environment plus the repo's env files, where the R2 credentials live."""
    out = dict(os.environ)
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    for p in (os.path.join(os.path.dirname(here), "config.env"),
              os.path.join(root, ".env.local"), os.path.join(root, ".env")):
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if "=" in line and not line.lstrip().startswith("#"):
                        k, v = line.split("=", 1)
                        out.setdefault(k.strip(), v.split(" #", 1)[0].strip())
        except Exception:
            pass
    return out


def discover(force=False):
    """The DSM laptop's published address and key, or (None, None).

    Cached for DISCOVER_TTL so a job of many chunks does not hit R2 per chunk.
    Never raises: the other laptop being off is the normal case.
    """
    now = time.time()
    if not force and _found["url"] and now - _found["at"] < DISCOVER_TTL:
        return _found["url"], _found["key"]

    e = _r2_env()
    acc = (e.get("R2_ACCOUNT_ID", "").replace("https://", "")
           .replace(".r2.cloudflarestorage.com", "").replace("/", "").strip())
    if not (acc and e.get("R2_ACCESS_KEY_ID") and e.get("R2_SECRET_ACCESS_KEY")
            and e.get("R2_BUCKET_NAME")):
        _found.update(url=None, key=None, at=now,
                      why="R2 is not configured here, so there is nowhere to "
                          "look up the address; set HEIGHT_SERVER to the URL "
                          "instead of 'auto'")
        return None, None
    try:
        import boto3
        s3 = boto3.client("s3", endpoint_url=f"https://{acc}.r2.cloudflarestorage.com",
                          aws_access_key_id=e["R2_ACCESS_KEY_ID"],
                          aws_secret_access_key=e["R2_SECRET_ACCESS_KEY"],
                          region_name="auto")
        body = s3.get_object(Bucket=e["R2_BUCKET_NAME"], Key=R2_KEY)["Body"].read()
        j = json.loads(body)
    except Exception as ex:
        _found.update(url=None, key=None, at=now,
                      why=f"could not read {R2_KEY}: {str(ex)[:120]}")
        return None, None

    if not j.get("online") or not j.get("url"):
        _found.update(url=None, key=None, at=now,
                      why="the DSM laptop published itself as offline")
        return None, None
    age = int(now - (j.get("updated") or 0))
    _found.update(url=str(j["url"]).rstrip("/"), key=j.get("api_key") or "",
                  at=now, why=f"discovered from R2, published {age}s ago")
    return _found["url"], _found["key"]


def endpoint(force=False):
    """-> (url, key). An explicit HEIGHT_SERVER always wins over discovery."""
    if SERVER:
        return SERVER, API_KEY
    if AUTO:
        url, key = discover(force=force)
        return url, (API_KEY or key)
    return None, None


def probe(timeout=4.0):
    """Is the other laptop reachable, and what does it have? Never raises."""
    if not configured():
        return {"ok": False, "reason": "HEIGHT_SERVER is not set",
                "hint": "set HEIGHT_SERVER=auto for a tunnelled DSM laptop, or "
                        "HEIGHT_SERVER=http://<dsm-laptop-ip>:8900 on a LAN"}
    url, key = endpoint(force=True)
    if not url:
        return {"ok": False, "mode": "auto", "reason": _found["why"],
                "hint": "run serve_global.py on the DSM laptop; it publishes "
                        "its address on startup"}
    try:
        import requests
        r = requests.get(f"{url}/health", headers=_headers(key), timeout=timeout)
        r.raise_for_status()
        j = r.json()
        return {"ok": j.get("status") == "ok", "server": url,
                "mode": "auto" if AUTO else "explicit",
                "discovery": _found["why"] if AUTO else None, **j}
    except Exception as e:
        return {"ok": False, "server": url, "reason": str(e),
                "mode": "auto" if AUTO else "explicit",
                "hint": "is start.bat or serve_global.py running on the DSM "
                        "laptop? If the URL above is a stale tunnel address, "
                        "restart it there so it republishes."}


def enrich(features, edge_buffer_m=None, timeout=None):
    """Attach height properties to GeoJSON features, in place of nothing.

    Returns (features, status). The features come back in the same order with
    the same geometry whatever happens, so a caller can use the result without
    checking status first; status is for reporting, not for branching.
    """
    if not features:
        return features, {"state": "empty", "n": 0}
    if not configured():
        return features, {"state": "not_configured",
                          "detail": "HEIGHT_SERVER is not set on this machine"}
    url, key = endpoint()
    if not url:
        return features, {"state": "not_configured", "mode": "auto",
                          "detail": _found["why"]}

    try:
        import requests
    except ImportError:
        return features, {"state": "error", "detail": "requests is not installed"}

    params = {}
    if edge_buffer_m is not None:
        params["edge_buffer_m"] = edge_buffer_m

    done = 0
    failed = 0
    summaries = []
    for start in range(0, len(features), CHUNK):
        chunk = features[start:start + CHUNK]
        body = {"type": "FeatureCollection",
                "features": [{"type": "Feature", "geometry": f.get("geometry"),
                              "properties": {}} for f in chunk]}
        try:
            r = requests.post(f"{url}/height", json=body, params=params,
                              headers=_headers(key),
                              timeout=timeout or TIMEOUT)
            r.raise_for_status()
            got = r.json()
        except Exception as e:
            log.warning("height chunk %d-%d failed: %s", start, start + len(chunk), e)
            failed += len(chunk)
            continue

        out = got.get("features") or []
        if len(out) != len(chunk):
            # Order is the only join key, so a length mismatch makes the whole
            # chunk unusable. Dropping it is correct; guessing an alignment is not.
            log.warning("height chunk returned %d for %d sent - skipped",
                        len(out), len(chunk))
            failed += len(chunk)
            continue
        for src, res in zip(chunk, out):
            p = res.get("properties") or {}
            src.setdefault("properties", {})
            for k in FIELDS:
                if k in p:
                    src["properties"][k] = p[k]
            done += 1
        if got.get("summary"):
            summaries.append(got["summary"])

    status = {"state": "ok" if done and not failed else
                       ("partial" if done else "failed"),
              "server": url, "measured": done, "not_measured": failed,
              "n": len(features)}
    if summaries:
        status["summary"] = _merge_summaries(summaries)
    return features, status


def _merge_summaries(parts):
    """Combine the per-chunk aggregates into one, weighting means by count."""
    n_in = sum(p.get("n_input") or 0 for p in parts)
    n_ok = sum(p.get("n_measured") or 0 for p in parts)
    floors, qual = {}, {}
    for p in parts:
        for k, v in (p.get("floors") or {}).items():
            floors[k] = floors.get(k, 0) + v
        for k, v in (p.get("quality") or {}).items():
            qual[k] = qual.get(k, 0) + v
    weighted = [(p["mean_height_m"], p["n_measured"]) for p in parts
                if p.get("mean_height_m") is not None and p.get("n_measured")]
    mean = (round(sum(h * n for h, n in weighted) / sum(n for _, n in weighted), 2)
            if weighted else None)
    maxes = [p["max_height_m"] for p in parts if p.get("max_height_m") is not None]
    return {"n_input": n_in, "n_measured": n_ok, "mean_height_m": mean,
            "max_height_m": max(maxes) if maxes else None,
            "floors": dict(sorted(floors.items())), "quality": qual}


# ── routes ──────────────────────────────────────────────────────────
@router.get("/health")
def health():
    """Whether the DSM laptop is reachable from here, and what it holds."""
    return probe()


@router.post("/enrich")
def enrich_route(
    payload: dict = Body(..., description="GeoJSON FeatureCollection of footprints"),
    edge_buffer_m: float = Query(None),
):
    """Add heights to footprints the caller already has.

    Used by the QGIS plugin and the webapp on a layer that is already on screen,
    so nothing is re-detected.
    """
    feats = (payload or {}).get("features")
    if not isinstance(feats, list):
        raise HTTPException(400, "send a GeoJSON FeatureCollection")
    feats, status = enrich(feats, edge_buffer_m=edge_buffer_m)
    return {"type": "FeatureCollection", "features": feats, "height_status": status}


@router.post("/area/{jid}")
def enrich_area(jid: str, edge_buffer_m: float = Query(None),
                save: bool = Query(True, description="also write heights into "
                                                     "the job's GeoPackage")):
    """Add heights to a finished HiSup area job.

    This is the normal route: detect over an area on this machine, then measure
    what was found on the other. With save=true the heights are written back
    into the job's results.gpkg, so a later download or QGIS export carries
    them without needing the DSM laptop again.
    """
    import db
    from config import RESULTS

    row = db.get_job(jid)
    if not row:
        raise HTTPException(404, "no such job")
    j = dict(row)
    if j.get("status") != "done":
        raise HTTPException(409, f"job is {j.get('status')}, not done")
    path = os.path.join(str(RESULTS), jid, "results.gpkg")
    if not os.path.exists(path):
        raise HTTPException(404, "that job has no result file")

    import geopandas as gpd
    import pyogrio

    layers = [str(x) for x in pyogrio.list_layers(path)[:, 0]]
    target = "buildings" if "buildings" in layers else layers[0]
    # Kept in its stored CRS: the heights are written back onto this frame, and
    # reprojecting the saved layer to 4326 as a side effect of measuring it
    # would quietly change what every earlier download returned.
    native = gpd.read_file(path, layer=target)
    if native.empty:
        return {"type": "FeatureCollection", "features": [],
                "height_status": {"state": "empty", "n": 0}}

    feats = json.loads(native.to_crs(4326).to_json())["features"]
    feats, status = enrich(feats, edge_buffer_m=edge_buffer_m)

    if save and status.get("measured"):
        try:
            for k in FIELDS:
                native[k] = [f["properties"].get(k) for f in feats]
            native.to_file(path, layer=target, driver="GPKG")
            status["saved_to"] = path
        except Exception as e:
            # The heights are already in the response; failing to persist them
            # is a nuisance, not a reason to lose the measurement.
            log.warning("could not write heights into %s: %s", path, e)
            status["save_error"] = str(e)

    return {"type": "FeatureCollection", "features": feats,
            "height_status": status, "layer": target}
