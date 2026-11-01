Written for: you, setting this up on the DSM laptop.

# Building Height Server

Runs on the laptop that holds the DSM and DTM folders. The HiSup laptop
detects building footprints from imagery and asks this server how tall each
one is. Neither machine holds the other's data, and either can be restarted
without the other caring.

---

## Run it

Copy this folder to the DSM laptop, open a terminal in it, and run:

```bat
start.bat
```

That is the whole thing. On the first run it creates a `.venv` and installs
dependencies (a few minutes, once), checks your elevation data, then serves.
Leave the window open; Ctrl+C stops it.

It defaults to the folders you gave:

```
C:\Users\Asus\Downloads\DSM Data
C:\Users\Asus\Downloads\DTM Data
```

If they are somewhere else, pass them — **with the quotes**, because the paths
contain a space:

```bat
start.bat "D:\survey\DSM Data" "D:\survey\DTM Data"
```

Clip-wise folders are expected. Subfolders are fine: it searches recursively
for `.tif`, `.tiff`, `.vrt`, `.img` and `.jp2`.

On startup it prints the address to use:

```
  Point the HiSup machine at:  http://192.168.1.42:8900
```

### Then, on the HiSup laptop, once

```bat
set HEIGHT_SERVER=http://192.168.1.42:8900
```

and restart the GPU server. Check they can see each other:

```
GET http://<hisup-laptop>:8777/height/health
```

Use the IP it printed, not `localhost` — `localhost` on the HiSup laptop means
the HiSup laptop.

---

## Reach it from anywhere

`start.bat` is LAN only: the `192.168.x.x` address works for machines on the
same wifi and nowhere else. For global access, run this instead:

```bat
start_global.bat
```

It serves the same API and additionally opens a Cloudflare tunnel, giving a
public HTTPS address. cloudflared makes only an **outbound** connection, so
no ports are opened on this laptop, no router settings change, no admin
rights are needed, and it works from behind a college or hotel firewall that
blocks inbound traffic entirely. It prints:

```
  PUBLIC URL : https://some-random-words.trycloudflare.com
  API KEY    : xK9...
```

### An API key is required in this mode, and that is deliberate

On a LAN, leaving the API open is fine. On the open internet it is not. A
`trycloudflare.com` hostname is **not a secret** — it appears in TLS SNI and
in public certificate-transparency logs, and those get scanned continuously.
An open endpoint there would let a stranger read your elevation data and hammer
the disk the rasters sit on.

So global mode generates a key on the first run, saves it to
`cache/api_key.txt`, and reuses it every restart after — you are not
reconfiguring the HiSup laptop daily. Set `HS_API_KEY` to pin your own.

With a key set, `/height`, `/extract`, `/tiles` and `/reindex` all require it.
`/health` stays open so anything can check liveness, but without the key it
answers only `{"status":"ok","has_dsm":true}` — no folder paths, no survey
bounds.

### The address changes on every restart. Handle it once, not daily.

The free tunnel gets a new random hostname each time it starts. Rather than
pasting a URL that goes stale, this laptop **publishes** its current address.
Copy the four `R2_*` values from the HiSup laptop's `.env` into
`height-server/config.env` (see `config.env.example`), then on the HiSup
laptop, once and for good:

```bat
set HEIGHT_SERVER=auto
```

It reads the current address *and the key* from R2 before each run, cached for
two minutes. Restart either laptop as often as you like; nothing is
reconfigured. On shutdown this server marks itself offline, so the HiSup side
stops trying instead of timing out.

Without R2 configured it still works — you just set the URL and key by hand
each time the tunnel restarts:

```bat
set HEIGHT_SERVER=https://some-random-words.trycloudflare.com
set HEIGHT_API_KEY=xK9...
```

### A fixed hostname instead

A random hostname per restart is the price of the free tunnel. For a stable
address, create a **named tunnel** in the Cloudflare dashboard (Zero Trust ▸
Networks ▸ Tunnels), put its token in `CF_TUNNEL_TOKEN` and the hostname you
mapped it to in `HS_PUBLIC_URL`, both in `config.env`. Needs a domain on
Cloudflare; everything else is identical.

### If you would rather not expose it publicly at all

Install [Tailscale](https://tailscale.com) on both laptops and keep using
`start.bat`. Both machines then get stable private addresses that work from
any network, with nothing reachable from the public internet. It is the safer
option for survey data and costs nothing for personal use — the tunnel is the
better choice only when the webapp or someone else's machine must reach it too.

---

## Using it

### The normal route: measure a finished detection job

Detect over an area on the HiSup machine as usual, then:

```
POST http://<hisup-laptop>:8777/height/area/<job-id>
```

Every footprint comes back with `height_m`, `floors`, `floor_label` and
ground elevation, and they are written into that job's GeoPackage, so later
downloads and QGIS exports carry heights without needing this laptop again.

### Footprints you already have

```
POST http://<hisup-laptop>:8777/height/enrich
```

with a GeoJSON FeatureCollection. Your own properties are preserved and the
height fields are added alongside.

### Footprints already on *this* laptop

Skip the network. Same code, same numbers, files in and out:

```bat
.venv\Scripts\python measure_file.py labels.gpkg out_heights.gpkg --csv out.csv
```

This is the right way to put heights on the 27k drone labels, or on any
exported layer. Add `--limit 500` for a trial run first.

### No footprints at all

```
POST http://localhost:8900/extract   {"bounds": [west, south, east, north]}
```

Finds buildings in the DSM itself. **This is the weaker path** — it is the
original suite's nDSM-threshold method, and it will merge adjoining houses
that HiSup separates correctly. Use it only for ground with no imagery.

---

## What it returns per building

| field | meaning |
|---|---|
| `height_m` | the headline number: 95th percentile of roof height above ground |
| `mean_height_m`, `min_height_m`, `max_height_m` | the spread, for a flat-roof check |
| `floors`, `floor_label` | storeys, at 3.0 m each (`2 Floors (G+1)`) |
| `ground_elev_m`, `roof_elev_m` | absolute elevations, for drainage and flood work |
| `height_source` | `dtm` if a surveyed DTM tile covered it, `ring` if ground was estimated |
| `dtm_minus_ring_m` | the two ground estimates disagreeing. Near 0 is healthy |
| `n_pixels`, `gsd_m` | how much evidence the number rests on |
| `roughness_m` | spread inside one footprint. High means tree overhang or merged buildings |
| `quality` | `good`, or why not: `no_dsm`, `low_pixels`, `below_min_height`, `rough`, `no_ground` |

**Check `quality` before trusting `height_m`.** A footprint outside the DSM
coverage returns `no_dsm` and nulls rather than an error, because one building
off the edge of a clip must not fail a job of 5,000.

---

## How it differs from DSM_DTM_Building_Height_Suite

Two deliberate changes, both accuracy rather than plumbing.

**It uses your real DTM.** The suite derives ground from the DSM by
morphological opening with a 15–22 m radius, which assumes bare ground exists
within that radius of every building. In dense low-rise — most of Ongole — it
does not, so the derived "ground" sits part-way up the surrounding roofs and
every height comes out short. A surveyed DTM has no such failure mode. Where
no DTM tile reaches, ground falls back to the 10th percentile of DSM in a ring
1–8 m outside the footprint, which is still far better than opening in dense
fabric. `height_source` tells you which was used, per building.

**It does not re-detect buildings.** The suite segments footprints out of the
DSM. HiSup already does that from RGB at F1 0.81, and elevation-based
segmentation merges adjoining houses — the exact failure HiSup was tuned to
avoid. So footprints come in and only the height is computed. `/extract`
keeps the old method for ground with no imagery, and even there the geometry
comes from the engine while the heights come from the DTM path.

The storey thresholds are unchanged (3.0 m per floor, 1.2 m offset), so
numbers stay comparable with the 1,841-building run you already have.

Separately, footprints are shrunk 0.5 m before sampling. A polygon traced from
imagery includes the wall face and some shadow, which sit at ground level;
averaging them in pulls a two-storey roof down by a metre or more.

---

## Verified

`selfcheck.py` runs on every start and refuses to serve on a real problem. It
catches, among others, the two mistakes most likely to happen: **swapped DSM
and DTM folders** (a surface model cannot sit below a terrain model, so the
low tail of DSM−DTM gives it away) and **unprojected rasters** in degrees,
where heights and areas would be nonsense.

The measurement itself was tested against synthetic DSM/DTM with buildings of
known height, on sloping ground, with the DTM at 5× coarser resolution on an
offset grid — the awkward case real survey products present:

```
true 3.0 m -> 3.07    true 12.0 m -> 12.07
true 6.0 m -> 6.08    true 15.0 m -> 15.11
true 9.0 m -> 9.10    true 21.0 m -> 21.14
```

Worst error 0.14 m, all storey counts correct, bare ground reads 0.08 m. With
the DTM removed the ring fallback was within 0.40 m. A noisy tree canopy read
roughness 1.57 against 0.05 for a flat roof.

For global mode: the API key is confirmed to gate `/height`, `/extract`,
`/tiles` and `/reindex` (401 with no key and with a wrong key, 200 with the
right one), `/health` confirmed to hide folder paths and bounds without it,
the key confirmed stable across restarts, and the publish→discover round trip
confirmed field-for-field so `HEIGHT_SERVER=auto` resolves the right URL and
key, stops resolving after shutdown, and loses to an explicit `HEIGHT_SERVER`.
Client failure states are all confirmed: with this server off, footprints and
their own properties come back untouched with no invented heights.

Two things were **not** exercised in testing, so treat the first run as the
test: an actual `trycloudflare.com` tunnel being opened, and the publish
actually writing to your R2 bucket. The tunnel code is the same shape as the
GPU server's own `tunnel.sh`, and discovery was proven against a stand-in
bucket, but the live path is yours to confirm. `start_global.bat` prints the
URL and key it got, and `GET /height/health` on the HiSup side reports what it
discovered — check both on the first run.

One caveat, stated plainly: **the `rough` flag is advisory and its default
threshold (2.5 m) is loose.** It would not have flagged that test canopy. Real
roofs carry parapets, stairwells and water tanks, so a tight threshold flags
more honest buildings than it catches problems. The `roughness_m` number is in
every response — look at its distribution on your data, then set `HS_ROUGH_M`.

---

## Settings

All optional; override in the environment before `start.bat`.

| variable | default | what it does |
|---|---|---|
| `HS_DSM_DIR` | `C:\Users\Asus\Downloads\DSM Data` | DSM folder |
| `HS_DTM_DIR` | `C:\Users\Asus\Downloads\DTM Data` | DTM folder |
| `HS_PORT` | `8900` | port to serve on |
| `HS_API_KEY` | *(empty)* | require `X-API-Key`; set `HEIGHT_API_KEY` to match on the HiSup side |
| `HS_FLOOR_HEIGHT` | `3.0` | metres per storey |
| `HS_MIN_HEIGHT` | `2.2` | below this it is not a building |
| `HS_EDGE_BUFFER_M` | `0.5` | shrink before sampling |
| `HS_ROUGH_M` | `2.5` | roughness above which `quality` becomes `rough` |
| `HS_RING_INNER_M` / `HS_RING_OUTER_M` | `1.0` / `8.0` | the outside ground ring |

On the HiSup laptop: `HEIGHT_SERVER` (a URL, or `auto` to discover it),
`HEIGHT_API_KEY`, `HEIGHT_CHUNK` (default 2000 footprints per request),
`HEIGHT_TIMEOUT` (default 180 s), `HEIGHT_DISCOVER_TTL` (default 120 s).

For global mode, in `config.env` on this laptop: the four `R2_*` values,
plus `CF_TUNNEL_TOKEN` and `HS_PUBLIC_URL` for a named tunnel.

## Other endpoints

| | |
|---|---|
| `GET /` | a page saying what it has indexed |
| `GET /health` | status, folders, clip counts, coverage, thresholds |
| `GET /tiles` | every indexed clip with bounds, CRS and resolution |
| `POST /reindex` | pick up files added since startup |
| `GET /docs` | interactive API, to try a request by hand |

The clip index is cached in `cache/tile_index.json` and rebuilt automatically
when a file is added, replaced or removed, so restarts are instant.

## If something is wrong

**"no rasters found"** — the path is wrong, or the quotes were left off a path
containing a space. `GET /health` echoes the folders it is actually using.

**Every building returns `no_dsm`** — the footprints are outside the clips you
have. `GET /health` reports `bounds_wgs84`; compare it with your area.

**Heights all look short** — check `height_source`. If it says `ring`
everywhere, no DTM tile is covering those buildings.

**`dtm_minus_ring_m` is large and consistent** — the DSM and DTM are
referenced to different vertical datums, or one is from a different survey.
Heights from the `ring` source stay usable; the `dtm` ones are offset by that
amount.
