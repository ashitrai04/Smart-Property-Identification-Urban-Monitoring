# Local data server

Serves drone and satellite imagery, plus segmentation results, to the webapp —
from **one folder on this laptop**, with no GPU.

It speaks the same API as `gpu-server/`, so the webapp uses it unchanged.

## Turn it on

Double-click **`start.bat`** (or `python run.py`). That's it.

```
  data folder : D:\GEO_SERVE
  imagery     : 3
  result layers: 4
  local       : http://localhost:8777
  catalog     : http://localhost:8777/catalog
  public      : https://xxxx-xxxx.trycloudflare.com
  published: https://xxxx-xxxx.trycloudflare.com
```

The public address is written to R2 (`runtime/gpu_server.json`), which the backend
serves at `/api/gpu-server`. The deployed webapp polls that every minute, so the
layers appear **without a Vercel redeploy or any .env change**. Ctrl+C marks it
offline again.

Options: `--no-tunnel` (localhost only), `--port 8777`, `--data D:\other\folder`.

## Put data in

Drop files into `D:\GEO_SERVE`. They are picked up within ~5 seconds — no restart.

| File | Becomes |
|---|---|
| `*.tif` `*.tiff` `*.vrt` | imagery |
| `*.gpkg` `*.geojson` `*.shp` | result layers on the imagery they overlap |

Large rasters don't need copying — hardlink them instead (instant, no extra disk):

```bat
mklink /H D:\GEO_SERVE\ongole_2026_sat.tif D:\SAT\ongole_2026_sat.tif
```

### How a file is read

Everything is inferred from the **file name**, so name files sensibly:

- **district** — `ongole`, `guntur`, … anywhere in the name or parent folder
  (otherwise `GEO_DEFAULT_DISTRICT`, default `ongole`)
- **kind** — `satellite` if the name contains `sat` / `satellite` / `sentinel` /
  `worldview`, otherwise `drone`
- **year** — any `2017`, `2026`, … in the name
- **resolution, size, bounds** — read from the raster itself

A vector layer is named from its file name first, then its layer name inside the
GeoPackage:

| name contains | layer |
|---|---|
| `build`, `footprint` | buildings |
| `road`, `street`, `centerline` | roads |
| `water`, `lake`, `tank`, `pond` | waterbodies |
| `open`, `plot`, `vacant` | openareas |
| `damage`, `pothole`, `crack` | road_damage |
| `new_buildings`, `removed_buildings`, `new_roads`, `water_gained`, `water_lost` | change layers |

Results attach to the imagery they match — same year wins, then shared words in
the name, then how much they overlap. So `ongole_2017_buildings.gpkg` lands on
`ongole_2017_sat.tif` on its own.

If two layers map to the same name (`road_centerlines` and `road_polygons` both
→ `roads`), the polygon layer wins, since the map draws a fill.

### Check what it did

**`http://localhost:8777/catalog`** lists every file found, how it was classified,
and an `unmapped` list explaining anything it could not place — the first thing to
look at when a layer doesn't show up.


## Turn the whole stack on

```bat
D:\React folder\Smart-Property-Identification-Urban-Monitoring\local-server\start_all.bat
```

That checks Ollama, loads the Qwen model into memory so the first question is not
slow, then starts the data server and the tunnel. Ctrl+C stops it.

### Or each piece by hand

```bat
ollama serve                     REM only if it is not already running
ollama run qwen2.5vl:3b          REM loads the model; Ctrl+D to leave it resident
python run.py --port 8777        REM data server + tunnel
```

Ollama installs as a Windows startup app, so it is usually already up — check with:

```bat
curl http://127.0.0.1:11434/api/tags
ollama ps                        REM which model is loaded right now
```

### Is the chatbot working?

```bat
curl http://localhost:8777/ask/health
curl -G --data-urlencode "q=give me the most damaged road" http://localhost:8777/ask
```

`ask/health` reports `"ollama": true` when the model is reachable. If it is false
the box still answers — a rule parser takes over — but it understands fewer
phrasings.

The first question after a cold start takes about 9 seconds while the model
loads; after that it is well under a second. Ollama unloads the model after a
few minutes idle, so an occasional slow question is normal. To keep it resident:

```bat
set OLLAMA_KEEP_ALIVE=-1
```

### Rebuilding what the bot knows

```bat
python build_kb.py         REM Guntur roads + pothole damage
python build_city_kb.py    REM Ongole buildings / water / road network
```

## Notes

- Rasters want internal overviews, or low zoom levels are slow. All current files
  have them. Build them with `gdaladdo -r average file.tif 2 4 8 16 32 64 128 256`.
- GeoPackages in UTM are fine; the bbox query and the output are reprojected for you.
- Nothing is written to your data files — the folder is read-only to this server.
- Tiles are cached by the browser for a day; results are read per viewport.

## Endpoints

| | |
|---|---|
| `GET /api/health` | status, counts, unmapped files |
| `GET /catalog` | what was found and how it was mapped |
| `GET /imagery` | imagery list (id, kind, district, bounds, gsd) |
| `GET /imagery/{id}/tiles/{z}/{x}/{y}.webp` | map tiles |
| `GET /jobs` | result sets, one per imagery + task |
| `GET /results/{job}/{layer}.geojson?bbox=w,s,e,n&zoom=` | vectors for the viewport |
