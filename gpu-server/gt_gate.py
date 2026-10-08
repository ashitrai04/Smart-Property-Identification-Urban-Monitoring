"""Keep road damage to the GROUND-TRUTH roads of a district.

Ground truth, either:
  * --gt-vector  a GeoPackage with a "roads" polygon layer (and optionally "buildings"),
                 e.g. cut from the district road shapefile — exact road outlines (preferred);
  * --gt         the district class raster in the R2 bucket (e.g. GUNTUR-RASTER.tif, 1 m),
                 road / building class values given by --road / --buildings.

A pothole / crack detection is kept ("on_road") only if at least --min-road of its box
lies on a ground-truth road, less than --max-roof on a ground-truth building, and its
ground size is plausible. Everything else is kept too, as on_road = 0, so it can be
inspected on the map ("Filtered out").

Runs beside the API (no restart) and writes an ordinary job (task "gtgate") that the
webapp shows as the Road damage layer; the kept detections are also saved as
potholes_on_road.geojson / .csv in the job folder.

    .venv/bin/python gt_gate.py --image img_xxx --gt-vector data/gt/guntur_gt_vector.gpkg
    .venv/bin/python gt_gate.py --image img_xxx --gt GUNTUR-RASTER.tif
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for line in (ROOT / "config.env").read_text().splitlines() if (ROOT / "config.env").exists() else []:
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.split("#")[0].strip())
sys.path.insert(0, str(ROOT / "app"))

import cv2  # noqa: E402
import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import rasterio  # noqa: E402
import shapely  # noqa: E402
from rasterio.features import rasterize  # noqa: E402
from rasterio.windows import from_bounds  # noqa: E402
from shapely.geometry import mapping  # noqa: E402

import db  # noqa: E402
from config import DATA, RESULTS  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--image", required=True, help="imagery id (e.g. the Guntur drone image)")
ap.add_argument("--gt-vector", help="GeoPackage with 'roads' (+ optional 'buildings') polygon layers")
ap.add_argument("--gt", default="GUNTUR-RASTER.tif", help="ground-truth raster key in the R2 bucket")
ap.add_argument("--pothole-job", help="default: the newest finished pothole job of the image")
ap.add_argument("--road", default="4", help="raster class value(s) for road, comma separated")
ap.add_argument("--buildings", default="1,2,3", help="raster class values for buildings")
ap.add_argument("--min-road", type=float, default=0.3)
ap.add_argument("--max-roof", type=float, default=0.3)
a = ap.parse_args()


def score_vector(det):
    """Exact box fractions on ground-truth road / roof polygons."""
    import pyogrio
    layers = [n for n, _ in pyogrio.list_layers(a.gt_vector)]
    roads = gpd.read_file(a.gt_vector, layer="roads")
    crs = roads.crs if roads.crs is not None and roads.crs.is_projected else "EPSG:32644"
    d = det.to_crs(crs)
    rparts = [p for g in roads.to_crs(crs).geometry for p in getattr(g, "geoms", [g])]
    bparts = list(gpd.read_file(a.gt_vector, layer="buildings").to_crs(crs).geometry) if "buildings" in layers else []
    rtree = shapely.STRtree(rparts)
    btree = shapely.STRtree(bparts) if bparts else None

    def frac(tree, parts, geom):
        if tree is None or geom.area <= 0:
            return 0.0
        hit = tree.query(geom, predicate="intersects")
        if not len(hit):
            return 0.0
        return float(shapely.union_all([parts[i] for i in hit]).intersection(geom).area / geom.area)

    return ([round(frac(rtree, rparts, g), 3) for g in d.geometry],
            [round(frac(btree, bparts, g), 3) for g in d.geometry])


def score_raster(det):
    """Box fractions on ground-truth road / roof from the class raster (scored at 25 cm)."""
    road_v = [int(v) for v in a.road.split(",")]
    bldg_v = [int(v) for v in a.buildings.split(",")]
    gt_path = DATA / "gt" / os.path.basename(a.gt)
    gt_path.parent.mkdir(parents=True, exist_ok=True)
    if not gt_path.exists():
        import boto3
        acc = os.environ["R2_ACCOUNT_ID"].replace("https://", "").replace(".r2.cloudflarestorage.com", "").strip("/")
        s3 = boto3.client("s3", endpoint_url=f"https://{acc}.r2.cloudflarestorage.com", region_name="auto",
                          aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                          aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])
        print(f"[gt] downloading {a.gt} from R2...", flush=True)
        s3.download_file(os.environ.get("R2_BUCKET_NAME", "property-data"), a.gt, str(gt_path) + ".part")
        os.replace(str(gt_path) + ".part", gt_path)
    with rasterio.open(gt_path) as g:
        d = det.to_crs(g.crs)
        x0, y0, x1, y1 = d.total_bounds
        win = from_bounds(x0 - 5, y0 - 5, x1 + 5, y1 + 5, transform=g.transform).round_offsets().round_lengths()
        gt = g.read(1, window=win, boundless=True, fill_value=0)
        wt = g.window_transform(win)
    up = 4
    T = wt * wt.scale(1 / up, 1 / up)
    road = cv2.resize(np.isin(gt, road_v).astype(np.uint8), None, fx=up, fy=up, interpolation=cv2.INTER_NEAREST)
    roof = cv2.resize(np.isin(gt, bldg_v).astype(np.uint8), None, fx=up, fy=up, interpolation=cv2.INTER_NEAREST)
    inv = ~T
    ro, bo = [], []
    for geom in d.geometry:
        bx0, by0, bx1, by1 = geom.bounds
        (c0, r1), (c1, r0) = inv * (bx0, by0), inv * (bx1, by1)
        c0, r0 = max(0, int(np.floor(c0))), max(0, int(np.floor(r0)))
        c1, r1 = min(road.shape[1], int(np.ceil(c1)) + 1), min(road.shape[0], int(np.ceil(r1)) + 1)
        if c1 <= c0 or r1 <= r0:
            ro.append(0.0); bo.append(0.0)
            continue
        m = rasterize([(mapping(geom), 1)], out_shape=(r1 - r0, c1 - c0),
                      transform=T * T.translation(c0, r0), dtype="uint8").astype(bool)
        n = max(int(m.sum()), 1)
        ro.append(round(float(road[r0:r1, c0:c1][m].sum() / n), 3))
        bo.append(round(float(roof[r0:r1, c0:c1][m].sum() / n), 3))
    return ro, bo


# the detections
if a.pothole_job:
    pj = db.get_job(a.pothole_job)
else:
    js = [j for j in db.list_jobs(a.image) if j["task"] == "pothole" and j["status"] == "done"]
    pj = max(js, key=lambda j: j.get("finished") or 0) if js else None
if not pj:
    sys.exit("No finished pothole job for this image.")
det = gpd.read_file(os.path.join(pj["result_dir"], "results.gpkg"), layer="road_damage", engine="pyogrio")
source = a.gt_vector or a.gt
print(f"[gt] {len(det)} detections from {pj['id']}, ground truth: {source}", flush=True)

jid = db.new_id("job")
out_dir = RESULTS / jid
out_dir.mkdir(parents=True, exist_ok=True)
db.conn().execute("INSERT INTO jobs (id, imagery_id, task, params, status, created, started, result_dir) "
                  "VALUES (?,?,?,?, 'running', ?, ?, ?)",
                  (jid, a.image, "gtgate", json.dumps({"pothole_job": pj["id"], "ground_truth": os.path.basename(source)}),
                   time.time(), time.time(), str(out_dir)))
db.conn().commit()

try:
    ro, bo = score_vector(det) if a.gt_vector else score_raster(det)
    size_ok = det["size_ok"].tolist() if "size_ok" in det else [1] * len(det)
    det["gt_road_overlap"], det["gt_roof_overlap"] = ro, bo
    det["on_road"] = [int(r >= a.min_road and b < a.max_roof and bool(ok)) for r, b, ok in zip(ro, bo, size_ok)]

    from jobs import _Sink
    sink = _Sink(out_dir / "results.gpkg", det.crs)
    sink.add("road_damage", list(det.geometry), det.drop(columns="geometry").to_dict("records"))
    kept = det[det["on_road"] == 1].to_crs(4326)
    kept.to_file(out_dir / "potholes_on_road.geojson", driver="GeoJSON")
    c = kept.to_crs(32644).geometry.centroid.to_crs(4326)
    kept.assign(lon=c.x.round(7), lat=c.y.round(7)).drop(columns="geometry").to_csv(out_dir / "potholes_on_road.csv",
                                                                                   index=False)
    counts = kept["label"].value_counts().to_dict() if "label" in kept else {}
    stats = {"on_road": int(len(kept)), "filtered_off_road": int(len(det) - len(kept)), "counts": counts,
             "ground_truth": os.path.basename(source), "pothole_job": pj["id"], "min_road": a.min_road,
             "max_roof": a.max_roof, "on_gt_roof": int(sum(1 for b in bo if b >= a.max_roof))}
    db.update_job(jid, status="done", progress=1.0, layers=sink.counts, stats=stats, finished=time.time())
    print(f"[gt] kept {len(kept)} on ground-truth roads, filtered {len(det) - len(kept)} "
          f"({stats['on_gt_roof']} on ground-truth roofs) -> job {jid}", flush=True)
    print(f"[gt] files: {out_dir}/potholes_on_road.geojson and .csv", flush=True)
except Exception as e:
    import traceback
    traceback.print_exc()
    db.update_job(jid, status="failed", message=str(e)[:500], finished=time.time())
    sys.exit(1)
