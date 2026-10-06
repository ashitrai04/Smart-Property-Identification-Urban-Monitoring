# Smart Property — GPU server

Runs every AI model of the webapp on the A100 server, plus a pipeline for heavy
drone / satellite imagery. The webapp uses this server first and falls back to
the Hugging Face Space automatically when it is unreachable or out of GPU memory.

```
gpu-server/
├── setup.sh            one-time install (no root, isolated .venv via uv)
├── start.sh / stop.sh  run the API + public HTTPS tunnel in the background
├── run.sh              API in the foreground (picks the A100 with most free memory)
├── tunnel.sh           Cloudflare tunnel → https URL for the webapp
├── spctl.py            command line: ingest imagery, run jobs, check progress
├── config.env.example  settings (copy to config.env)
├── requirements.txt
├── weights/
│   ├── best_model.pth  SegFormer-B5 (5 classes) — same file as the HF Space
│   ├── final_best.pt   YOLOv8s road damage / potholes
│   ├── roofs.pt        YOLO fine-tune with roof hard negatives (optional)
│   └── hf/             SAM weights cache (filled by setup.sh)
├── app/                FastAPI service (main.py) + model code
└── data/               created at runtime
    ├── raw/            ← put original drone / satellite files here
    ├── cogs/           compressed Cloud-Optimised GeoTIFFs (served as map tiles)
    ├── results/<job>/  results.gpkg (vectors) + classes.tif (class map)
    ├── catalog.sqlite  database: imagery, jobs, results
    └── logs/
```

## 1. Upload and install

Upload the folder (or `gpu-server.zip`) to your home directory with the Jupyter
file browser, then open **File → New → Terminal** in JupyterLab:

```bash
cd ~ && unzip -q gpu-server.zip && cd gpu-server     # if you uploaded the zip
bash setup.sh                                          # ~10 min: uv, PyTorch CUDA, deps, SAM weights
nano config.env                                        # set SP_API_KEY and the R2_* values (same as the webapp .env)
```

## 2. Start

```bash
bash start.sh        # API on :8800 + tunnel; prints  Public URL: https://xxxx.trycloudflare.com
```

Put that URL in the webapp as `VITE_GPU_API` (local `.env`, and Vercel → Settings →
Environment Variables, then redeploy). The top bar then shows **AI · GPU**; if the
server goes away it shows **AI · HF fallback** and everything keeps working on Hugging Face.

The quick-tunnel URL changes whenever the tunnel restarts. For a fixed address, create a
named tunnel in Cloudflare (Zero Trust → Networks → Tunnels), put its token in
`CF_TUNNEL_TOKEN`, and route a hostname to `http://localhost:8800`.

```bash
bash stop.sh                              # stop API + tunnel
tail -f data/logs/api.log                 # live log
.venv/bin/python spctl.py status          # health, GPU, loaded models
```

`start.sh` uses `nohup`, so the server keeps running after you close the notebook.
After a server reboot, run `bash start.sh` again.

## 3. GPU behaviour (shared A100s)

Other users hold most of both GPUs' memory. The server is built for that:

* `run.sh` picks the A100 with the most free memory at start; below `MIN_FREE_MB`
  on both, it starts on CPU (256 cores) instead of failing.
* Models load on first use: SegFormer-B5, SAM (`SAM_MODEL_ID`), YOLO.
* SegFormer runs batched in fp16; on out-of-memory the batch halves automatically.
* If a request still cannot fit, the API answers 503 and the webapp re-sends it to
  Hugging Face — users never see the failure.
* All models share one GPU queue; batch jobs take it one window at a time, so a
  webapp request waits seconds, not hours, behind a big job.

Segmentation results match the HF Space (same weights, same transformers 4.44.2). The
GPU defaults are a little richer — `SEG_MAX_DIM=2048` and flip TTA — set
`SEG_MAX_DIM=1536 SEG_TTA=0` in config.env for byte-identical output.

> The deployed "SAM2" fusion uses SAM ViT-B (`facebook/sam-vit-base`) through
> transformers. With GPU memory to spare, `SAM_MODEL_ID=facebook/sam-vit-large` or
> `-huge` gives crisper building outlines.

## 4. Heavy imagery (drone / satellite, tens of GB)

```bash
# copy originals into data/raw (scp/rsync from your machine, or download there)
rsync -avP vijayawada_drone_2026.tif user@tbiaiserver:~/gpu-server/data/raw/

.venv/bin/python spctl.py raw                       # what is waiting
.venv/bin/python spctl.py ingest data/raw/vijayawada_drone_2026.tif \
    --name "Vijayawada drone Sep-2026" --kind drone --district vijayawada --wait
.venv/bin/python spctl.py imagery                   # catalog with original vs compressed size
```

Ingest converts the file to a **Cloud-Optimised GeoTIFF**: 512 px internal tiles,
overviews, JPEG (YCbCr, q85) compression — typically 8–12× smaller than the original
with no visible loss (`SP_COG_PROFILE=webp` is smaller still, `deflate` is lossless).
The map reads only the tiles in view, so a 50 GB ortho opens instantly in the webapp
(Mapping → left rail → **Server imagery**). `--delete-source` removes the original
after a successful conversion.

Requirements: a georeferenced raster (GeoTIFF/JP2/VRT with a CRS). 8-bit RGB gets
JPEG/WEBP; 16-bit or multispectral imagery is stored losslessly.

## 5. Run the models over a whole image

```bash
.venv/bin/python spctl.py job img_xxxx pothole --conf 0.25 --wait   # YOLO road damage
.venv/bin/python spctl.py job img_xxxx segment --wait               # SegFormer land use
.venv/bin/python spctl.py job img_xxxx fusion  --wait               # SegFormer + SAM buildings
.venv/bin/python spctl.py job img_xxxx segment --gsd 0.3            # analyse on a coarser 30 cm grid (faster)
.venv/bin/python spctl.py jobs                                      # progress of all jobs
.venv/bin/python spctl.py cancel job_xxxx
```

Jobs walk the raster in overlapping windows read straight from the COG, so memory
stays flat however big the file is. Each window's output is written immediately:

* `results.gpkg` — vectors in EPSG:4326 with a spatial index: `road_damage` (class,
  label, confidence, is_pothole) or `buildings` / `roads` / `waterbodies` / `openareas`
  (with `area_m2`). Opens directly in QGIS / ArcGIS.
* `classes.tif` — the class map, served as coloured map tiles.

Results appear in the webapp under each image in **Server imagery**; vectors load for
the visible area only. Jobs survive restarts (a running job is re-queued).

## 6. API

Same endpoints as the HF Space — `/predict`, `/predict-fusion`, `/change-detection`,
`/predict-url`, `/predict-fusion-url`, `/change-detection-url`, `/r2/presign`,
`/r2/delete`, `/pothole/detect` — plus:

| | |
|---|---|
| `GET /api/health` | status, device, free GPU memory |
| `GET /imagery` · `GET /imagery/{id}` | catalog |
| `POST /imagery/ingest` 🔑 | `{"path","name","kind","district","captured","delete_source"}` |
| `GET /imagery/{id}/tiles/{z}/{x}/{y}.webp` | map tiles |
| `POST /jobs` 🔑 | `{"imagery_id","task":"segment\|fusion\|pothole","params":{"conf","target_gsd_m"}}` |
| `GET /jobs` · `GET /jobs/{id}` · `POST /jobs/{id}/cancel` 🔑 | |
| `GET /results/{job}/{layer}.geojson?bbox=w,s,e,n&zoom=` | vectors for the view |
| `GET /results/{job}/classes/{z}/{x}/{y}.png` | class-map tiles |
| `GET /results/{job}/download` | the GeoPackage |

🔑 = header `X-API-Key: <SP_API_KEY>`. Model and read endpoints are open, like the HF Space.

## 7. Updating

Replace files in `app/` or `weights/`, then `bash start.sh` (it restarts). Retrained
SegFormer weights → `weights/best_model.pth`; pothole → `weights/final_best.pt`.
