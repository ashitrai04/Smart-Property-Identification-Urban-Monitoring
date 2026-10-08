"""
Smart Property — GPU inference & imagery server.

Drop-in for the Hugging Face SegFormer Space (same endpoints, same responses), so
the webapp can use this box first and fall back to Hugging Face when it is down:

  GET  /api/health                    status, GPU, models, capabilities
  POST /predict                       SegFormer-B5 segmentation           (form: file)
  POST /predict-fusion                SegFormer + SAM building instances  (form: file)
  POST /change-detection              two-date change                     (form: past, present, mode)
  GET  /r2/presign  · POST /predict-url · /predict-fusion-url · /change-detection-url · /r2/delete
  GET  /pothole/health · POST /pothole/detect      YOLO road damage      (form: file, mode, conf)

Server-only (heavy imagery pipeline):
  GET  /imagery                        catalog (drone / satellite COGs)
  POST /imagery/ingest                 compress a file under data/raw into a COG       [API key]
  GET  /imagery/{id}/tiles/{z}/{x}/{y}.webp   map tiles
  POST /jobs                           run segment | fusion | pothole | refine over a whole image [API key]
  GET  /jobs · /jobs/{id} · POST /jobs/{id}/cancel                                     [cancel: API key]
  GET  /results/{job}/{layer}.geojson?bbox=w,s,e,n     result vectors (bbox-filtered)
  GET  /results/{job}/classes/{z}/{x}/{y}.png          result class map tiles
  GET  /results/{job}/download                         the job's GeoPackage
"""
import os
import sys
import uuid

sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
import shapely
from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

import config
import db
import gpu
import imagery
import inference as seg
import jobs

WEIGHTS = os.environ.get("WEIGHTS_PATH", str(config.WEIGHTS / "best_model.pth"))
os.environ.setdefault("POTHOLE_WEIGHTS", str(config.WEIGHTS / "final_best.pt"))

import pothole  # noqa: E402  (reads POTHOLE_WEIGHTS at import)

pothole._lock = gpu.GPU_LOCK      # one GPU queue for every model


def _road_mask_for_pothole(rgb):
    import roads
    return roads.seg_road_mask(rgb, None, MODEL_(), seg, target_gsd=0.12)


pothole.ROAD_MASK_FN = _road_mask_for_pothole

app = FastAPI(title="Smart Property — GPU server", version="2.0.0")
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
)
app.include_router(pothole.router)

print(f"[startup] device={seg.DEVICE} batch={seg.SEG_BATCH} fp16={seg.SEG_FP16} max_dim={seg.MAX_DIM} "
      f"gpu={gpu.device_info()}", flush=True)

_MODEL = None


def MODEL_():
    global _MODEL
    if _MODEL is None:
        print("[model] loading SegFormer-B5…", flush=True)
        _MODEL = seg.load_model(WEIGHTS)
        print("[model] ready", seg.LOAD_INFO, flush=True)
    if not seg.LOAD_INFO.get("loaded"):
        # A newer transformers renames SegFormer parameters, the weights silently
        # don't load and every pixel comes out as one class. Refuse rather than
        # return wrong maps; the 500 makes the webapp fall back to Hugging Face.
        raise HTTPException(500, f"SegFormer weights did not load ({seg.LOAD_INFO.get('missing')} tensors missing) — "
                                 "install the pinned transformers==4.44.2 (bash setup.sh)")
    return _MODEL


def require_key(x_api_key: str = Header(default="")):
    if not config.API_KEY:
        raise HTTPException(503, "SP_API_KEY is not set on the server — write endpoints are disabled")
    if x_api_key != config.API_KEY:
        raise HTTPException(401, "Missing or wrong X-API-Key")


def _gpu_call(fn, *a, **k):
    """Run a model call on the shared GPU. If the box is out of GPU memory even after
    the automatic batch back-off, answer 503 so the webapp falls back to Hugging Face."""
    with gpu.GPU_LOCK:
        try:
            return fn(*a, **k)
        except Exception as e:
            if gpu.is_oom(e):
                gpu.release()
                raise HTTPException(503, "GPU is out of memory right now (shared server) — try the fallback")
            raise


# ── R2 (same bucket and key scheme as the HF Space, so either backend can read an upload) ──
R2_ACCOUNT = os.environ.get("R2_ACCOUNT_ID", "").replace("https://", "").replace(".r2.cloudflarestorage.com", "").replace("/", "").strip()
R2_KEY = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_BUCKET = os.environ.get("R2_BUCKET_NAME")
_r2 = None


def r2():
    global _r2
    if _r2 is None:
        if not (R2_ACCOUNT and R2_KEY and R2_SECRET and R2_BUCKET):
            raise HTTPException(500, "R2 storage is not configured on the server")
        import boto3
        _r2 = boto3.client("s3", endpoint_url=f"https://{R2_ACCOUNT}.r2.cloudflarestorage.com",
                           aws_access_key_id=R2_KEY, aws_secret_access_key=R2_SECRET, region_name="auto")
    return _r2


# ── lifecycle ────────────────────────────────────────────────────
@app.on_event("startup")
def _startup():
    db.init()
    jobs.start_worker()


# ── health ───────────────────────────────────────────────────────
@app.get("/")
@app.get("/api/health")
def health():
    return {
        "status": "ok", "server": "gpu", "device": seg.DEVICE, "gpu": gpu.device_info(),
        "classes": seg.CLASS_NAMES, "weights": seg.LOAD_INFO, "r2": bool(R2_ACCOUNT and R2_KEY),
        "max_dim": seg.MAX_DIM,
        "capabilities": ["predict", "predict-fusion", "change-detection", "pothole", "imagery", "jobs"],
    }


DEMO_DIR = os.path.join(os.path.dirname(__file__), "demo")


@app.get("/demo/{name}")
def demo_asset(name: str):
    p = os.path.join(DEMO_DIR, os.path.basename(name))
    if not os.path.isfile(p):
        raise HTTPException(404, "demo asset not found")
    if p.endswith(".json"):
        import json as _json
        return JSONResponse(_json.load(open(p, encoding="utf-8")))
    return FileResponse(p)


# ── SegFormer / fusion / change — identical responses to the HF Space ──
def _predict_rgb(rgb):
    pred = _gpu_call(seg.segment, MODEL_(), rgb)
    return {
        "master_map_base64": seg.png_b64(seg.overlay(rgb, pred)),
        "raw_mask_base64": seg.png_b64(seg.colorize(pred)),
        "class_map_base64": seg.png_b64_gray(pred),
        "class_defs": seg.CLASS_NAMES,
        "stats": seg.class_stats(pred),
        "size": {"width": int(rgb.shape[1]), "height": int(rgb.shape[0])},
        "served_by": "gpu",
    }


def _fuse_rgb(rgb):
    import fusion

    def run():
        pred = seg.segment(MODEL_(), rgb)
        return fusion.fuse(rgb, pred)

    f = _gpu_call(run)
    return {
        "mode": "fusion",
        "master_map_base64": seg.png_b64(f["overlay_rgb"]),
        "class_map_base64": seg.png_b64_gray(f["semantic_ids"]),
        "class_defs": seg.CLASS_NAMES,
        "working_size": f["working_size"],
        "building_count": f["building_count"],
        "sam_refined": f["sam_refined"],
        "buildings_geojson": f["buildings_geojson"],
        "roads_geojson": f["roads_geojson"],
        "stats": f["stats"],
        "size": {"width": int(rgb.shape[1]), "height": int(rgb.shape[0])},
        "served_by": "gpu",
    }


def _change(rgb1, rgb2, mode="building"):
    import change as cd
    out = _gpu_call(cd.change, rgb1, rgb2, MODEL_(), mode=mode)
    out["served_by"] = "gpu"
    return out


async def _read(upload: UploadFile):
    data = await upload.read()
    if not data:
        raise HTTPException(400, "Empty file")
    try:
        return seg.read_image_any(data, upload.filename or "")
    except Exception as e:
        raise HTTPException(400, f"Could not read image: {e}")


def _read_r2(key, filename=""):
    if not key or not key.startswith("temp/"):
        raise HTTPException(400, "Invalid R2 key")
    try:
        data = r2().get_object(Bucket=R2_BUCKET, Key=key)["Body"].read()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(404, f"Could not read object from R2: {e}")
    return seg.read_image_any(data, filename or key)


@app.post("/predict")
async def predict(file: UploadFile = File(...)):
    return _predict_rgb(await _read(file))


@app.post("/predict-fusion")
async def predict_fusion(file: UploadFile = File(...)):
    return _fuse_rgb(await _read(file))


@app.post("/change-detection")
async def change_detection(past: UploadFile = File(...), present: UploadFile = File(...), mode: str = Form("building")):
    return _change(await _read(past), await _read(present), mode)


@app.get("/r2/presign")
def r2_presign(filename: str, content_type: str = "application/octet-stream"):
    ext = os.path.splitext(filename)[1] or ".bin"
    key = f"temp/{uuid.uuid4().hex}{ext}"
    put_url = r2().generate_presigned_url("put_object", Params={"Bucket": R2_BUCKET, "Key": key, "ContentType": content_type}, ExpiresIn=3600)
    return {"key": key, "put_url": put_url, "content_type": content_type}


@app.post("/predict-url")
def predict_url(payload: dict = Body(...)):
    return _predict_rgb(_read_r2(payload.get("key"), payload.get("filename", "")))


@app.post("/predict-fusion-url")
def predict_fusion_url(payload: dict = Body(...)):
    return _fuse_rgb(_read_r2(payload.get("key"), payload.get("filename", "")))


@app.post("/change-detection-url")
def change_url(payload: dict = Body(...)):
    rgb1 = _read_r2(payload.get("past_key"), payload.get("past_filename", ""))
    rgb2 = _read_r2(payload.get("present_key"), payload.get("present_filename", ""))
    return _change(rgb1, rgb2, payload.get("mode", "building"))


@app.post("/r2/delete")
def r2_delete(payload: dict = Body(...)):
    keys = [k for k in (payload.get("keys") or []) if k and str(k).startswith("temp/")]
    for k in keys:
        try:
            r2().delete_object(Bucket=R2_BUCKET, Key=k)
        except Exception:
            pass
    return {"deleted": keys}


# ── imagery ──────────────────────────────────────────────────────
def _public_img(r):
    r = dict(r)
    r.pop("source_path", None)
    r.pop("cog_path", None)
    return r


@app.get("/imagery")
def imagery_list():
    return [_public_img(r) for r in db.list_imagery()]


@app.get("/imagery/{img_id}")
def imagery_get(img_id: str):
    r = db.get_imagery(img_id)
    if not r:
        raise HTTPException(404, "imagery not found")
    return _public_img(r)


@app.get("/imagery/raw/files", dependencies=[Depends(require_key)])
def raw_files():
    """Files sitting in data/raw, ready to ingest."""
    return [{"path": str(p), "name": p.name, "gb": round(p.stat().st_size / 2**30, 3)}
            for p in sorted(config.RAW.rglob("*")) if p.is_file() and p.suffix.lower() in imagery.RASTER_EXT]


@app.post("/imagery/ingest", dependencies=[Depends(require_key)])
def imagery_ingest(payload: dict = Body(...)):
    try:
        img_id = imagery.start_ingest(payload.get("path", ""), payload.get("name"), payload.get("kind", "drone"),
                                      payload.get("district"), payload.get("captured"), bool(payload.get("delete_source")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"id": img_id, "status": "ingesting"}


# ── Direct, resumable upload of very large files (tens of GB) straight to this server ──
# The sender (send_to_gpu.py) PUTs ≤90 MB pieces — the free Cloudflare tunnel caps a
# request at 100 MB — each tagged with its byte offset. Nothing is stored in R2.
def _upload_path(name):
    safe = os.path.basename(name).replace("..", "")
    if not safe or not safe.lower().endswith(imagery.RASTER_EXT):
        raise HTTPException(400, f"not a raster file name: {name}")
    return config.RAW / safe


def _registered(final):
    """Imagery already created from this upload (ingest moves the file into cogs/)."""
    for r in db.list_imagery():
        if r.get("source_path") == str(final) and r.get("status") in ("ingesting", "ready"):
            return r
    return None


@app.get("/uploads/{name}", dependencies=[Depends(require_key)])
def upload_status(name: str):
    final = _upload_path(name)
    part = final.with_name(final.name + ".part")
    reg = _registered(final)
    if reg:   # finished and registered — never resend a 45 GB file because the sender was re-run
        return {"name": final.name, "bytes": reg.get("source_bytes"), "complete": True, "registered": reg["id"]}
    if final.exists():
        return {"name": final.name, "bytes": final.stat().st_size, "complete": True}
    return {"name": final.name, "bytes": part.stat().st_size if part.exists() else 0, "complete": False}


@app.put("/uploads/{name}", dependencies=[Depends(require_key)])
async def upload_piece(name: str, request: Request, offset: int = Query(...), total: int = Query(...)):
    final = _upload_path(name)
    part = final.with_name(final.name + ".part")
    if final.exists():
        return {"bytes": final.stat().st_size, "complete": True}
    have = part.stat().st_size if part.exists() else 0
    if offset != have:                       # sender is out of step (e.g. after a reconnect): tell it where to resume
        return JSONResponse({"bytes": have, "complete": False, "resume_from": have}, status_code=409)
    n = 0
    with open(part, "ab") as fh:
        async for chunk in request.stream():
            fh.write(chunk)
            n += len(chunk)
    have += n
    if have > total:
        part.unlink(missing_ok=True)
        raise HTTPException(400, "received more bytes than the declared total — upload reset, start again")
    if have == total:
        os.replace(part, final)
        return {"bytes": have, "complete": True}
    return {"bytes": have, "complete": False}


@app.post("/uploads/{name}/ingest", dependencies=[Depends(require_key)])
def upload_ingest(name: str, payload: dict = Body(default={})):
    final = _upload_path(name)
    reg = _registered(final)
    if reg:
        return {"id": reg["id"], "status": reg["status"], "note": "already registered"}
    if not final.exists():
        raise HTTPException(409, "upload not complete yet")
    try:
        img_id = imagery.start_ingest(str(final), payload.get("name"), payload.get("kind", "drone"),
                                      payload.get("district"), payload.get("captured"), False)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"id": img_id, "status": "ingesting"}


@app.post("/imagery/import-r2", dependencies=[Depends(require_key)])
def imagery_import_r2(payload: dict = Body(...)):
    """Import a large raster that was uploaded to R2 (e.g. uploads/drone/ongole/clip_01.tif)."""
    key = payload.get("key", "")
    if not key.startswith("uploads/"):
        raise HTTPException(400, "key must be under uploads/")
    try:
        img_id = imagery.start_import_r2(r2(), R2_BUCKET, key, payload.get("name"), payload.get("kind", "drone"),
                                         payload.get("district"), payload.get("captured"),
                                         payload.get("delete_after", True))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(404, f"Could not import {key}: {e}")
    return {"id": img_id, "status": "ingesting"}


@app.delete("/imagery/{img_id}", dependencies=[Depends(require_key)])
def imagery_delete(img_id: str):
    r = db.get_imagery(img_id)
    if not r:
        raise HTTPException(404, "imagery not found")
    if r.get("cog_path"):
        try:
            os.remove(r["cog_path"])
        except OSError:
            pass
    db.delete_imagery(img_id)
    return {"deleted": img_id}


@app.get("/imagery/{img_id}/tiles/{z}/{x}/{y}.{fmt}")
def imagery_tile(img_id: str, z: int, x: int, y: int, fmt: str, size: int = Query(256)):
    r = db.get_imagery(img_id)
    if not r or r["status"] != "ready":
        raise HTTPException(404, "imagery not ready")
    try:
        data = imagery.tile(r["cog_path"], z, x, y, fmt=fmt, size=512 if size >= 512 else 256)
    except Exception as e:   # a failed tile must not become a CORS-less 500 in the browser
        print(f"[tile] {img_id} {z}/{x}/{y}: {e}", flush=True)
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    if data is None:
        return Response(status_code=204)
    return Response(data, media_type="image/webp" if fmt == "webp" else "image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


# ── jobs ─────────────────────────────────────────────────────────
@app.post("/jobs", dependencies=[Depends(require_key)])
def job_create(payload: dict = Body(...)):
    task = payload.get("task")
    if task not in ("segment", "fusion", "pothole", "refine"):
        raise HTTPException(400, "task must be segment | fusion | pothole | refine")
    img = db.get_imagery(payload.get("imagery_id", ""))
    if not img:
        raise HTTPException(404, "imagery not found")
    if img["status"] != "ready":
        raise HTTPException(409, f"imagery is {img['status']}")
    return {"id": db.add_job(img["id"], task, payload.get("params") or {}), "status": "queued"}


@app.get("/jobs")
def job_list(imagery_id: str = Query(None)):
    return db.list_jobs(imagery_id)


@app.get("/jobs/{jid}")
def job_get(jid: str):
    j = db.get_job(jid)
    if not j:
        raise HTTPException(404, "job not found")
    return j


@app.post("/jobs/{jid}/cancel", dependencies=[Depends(require_key)])
def job_cancel(jid: str):
    j = db.get_job(jid)
    if not j:
        raise HTTPException(404, "job not found")
    if j["status"] in ("queued", "running"):
        db.update_job(jid, status="cancelled")
    return db.get_job(jid)


# ── results ──────────────────────────────────────────────────────
def _done_job(jid):
    j = db.get_job(jid)
    if not j or not j.get("result_dir"):
        raise HTTPException(404, "job not found")
    return j


@app.get("/results/{jid}/download")
def result_download(jid: str):
    j = _done_job(jid)
    p = os.path.join(j["result_dir"], "results.gpkg")
    if not os.path.isfile(p):
        raise HTTPException(404, "no vector results yet")
    return FileResponse(p, filename=f"{jid}.gpkg", media_type="application/geopackage+sqlite3")


@app.get("/results/{jid}/{layer}.geojson")
def result_layer(jid: str, layer: str, bbox: str = Query(None), limit: int = Query(20000), zoom: float = Query(None),
                 on_road: int = Query(None)):
    import pyogrio
    j = _done_job(jid)
    p = os.path.join(j["result_dir"], "results.gpkg")
    if not os.path.isfile(p) or layer not in (j.get("layers") or {}) and j["status"] == "done":
        return JSONResponse({"type": "FeatureCollection", "features": []})
    bb = tuple(float(v) for v in bbox.split(",")) if bbox else None
    try:
        where = None
        if on_road is not None and layer == "road_damage":
            where = f"on_road = {1 if on_road else 0}"       # road-gated detections (or the filtered-out ones)
        df = pyogrio.read_dataframe(p, layer=layer, bbox=bb, where=where, max_features=max(1, min(limit, 200000)))
    except Exception:
        return JSONResponse({"type": "FeatureCollection", "features": []})
    if not len(df):
        return JSONResponse({"type": "FeatureCollection", "features": []})
    geoms = df.geometry.values
    if zoom is not None and zoom < 16:
        geoms = shapely.simplify(geoms, 360.0 / (512 * 2 ** float(zoom)) / 2, preserve_topology=False)
    geoms = shapely.transform(geoms, lambda c: np.round(c, 7))
    gj = shapely.to_geojson(geoms)
    props = df.drop(columns=df.geometry.name).to_json(orient="records", lines=True).splitlines() if len(df.columns) > 1 else ["{}"] * len(df)
    feats = ",".join('{"type":"Feature","properties":%s,"geometry":%s}' % (pr, g) for pr, g in zip(props, gj) if g)
    return Response('{"type":"FeatureCollection","features":[' + feats + ']}', media_type="application/json")


CLASS_COLORMAP = {0: (0, 0, 0, 0), 1: (239, 68, 68, 190), 2: (234, 179, 8, 210), 3: (59, 130, 246, 190), 4: (156, 163, 175, 160)}


@app.get("/results/{jid}/classes/{z}/{x}/{y}.png")
def result_class_tile(jid: str, z: int, x: int, y: int):
    j = _done_job(jid)
    p = os.path.join(j["result_dir"], "classes.tif")
    if not os.path.isfile(p):
        raise HTTPException(404, "no class map for this job")
    data = imagery.tile(p, z, x, y, colormap=CLASS_COLORMAP)
    if data is None:
        return Response(status_code=204)
    return Response(data, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})
