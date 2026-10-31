Written for: you, installing and publishing this yourself.

# Building Delineator — install, test, publish

## What was built

A QGIS Processing provider with two algorithms. The plugin carries **no
model**: it calls the Urbanly server, which is why it is 19 KB instead of
2.5 GB and installs in seconds.

```
qgis-plugin/
  building_delineator/        the plugin itself
    metadata.txt              what QGIS reads before loading anything
    __init__.py               classFactory()
    plugin.py                 registers the provider
    provider.py               the "Urbanly" entry in the Toolbox
    client.py                 all HTTP; no qgis import, so it is testable alone
    algorithms/
      common.py               server parameter, GeoJSON -> QgsFeature
      detect_raster.py        a raster layer on your machine
      detect_area.py          an area of imagery already on the server
    icon.svg  LICENSE  README.md
  package.py                  builds + validates the zip
  test_without_qgis.py        runs the logic with QGIS stubbed
  building_delineator.zip     what you install
```

## Install it

1. QGIS ▸ **Plugins ▸ Manage and Install Plugins ▸ Install from ZIP**
2. Pick `qgis-plugin/building_delineator.zip`
3. Open the **Processing Toolbox** (Ctrl+Alt+T). Under **Urbanly ▸ Buildings**
   there are two algorithms.

Set **Model server URL** on first run:

| where QGIS is | URL |
|---|---|
| same machine as the server | `http://127.0.0.1:8777` |
| anywhere else | the public address the webapp uses |

Get the public one with:

```
curl https://asashit-smart-property-backend.hf.space/api/gpu-server
```

The server must be running — `start_everything.bat`. The plugin checks on
start and says so plainly rather than failing at the first upload.

## The two algorithms

**Building footprints from a raster layer** — for imagery on your own disk.
Clips the extent you choose with GDAL, resamples to 12.5 cm, sends it, writes
the footprints back as a polygon layer in EPSG:4326. The raster must be
georeferenced: the server reads the ground resolution from the file. Capped at
200 MB per clip, which is the server's limit.

**Building footprints over an area** — for imagery already ingested on the
server. Nothing is uploaded; the server reads only the windows it needs from
the orthomosaic, so the area can be far larger than anything you could send.
Give it a boundary layer *or* an extent — a boundary wins, because squaring a
real shape off to its bounding box would process ground you excluded on
purpose. The progress bar is the server's real progress, and cancelling in
QGIS cancels the job on the server.

Both are Processing algorithms rather than a dialog, so they also work in the
graphical modeller, run in **batch** over many layers, and can be driven
headless:

```
qgis_process run urbanly:detectinarea \
  --EXTENT="80.08,80.09,15.48,15.49 [EPSG:4326]" \
  --SERVER="http://127.0.0.1:8777" --TASK=0 --OUTPUT=out.gpkg
```

## Change it

```
cd qgis-plugin
python test_without_qgis.py            # logic, with QGIS stubbed
python package.py                      # rebuild + validate the zip
```

`test_without_qgis.py` exists because QGIS is awkward to drive headless and
"it parses" is not "it runs". It stubs `qgis.*` and `osgeo`, imports the
algorithms, and checks the parts that actually break — GeoJSON to features,
extent handling, a dead server URL — plus that the model list the plugin
offers still matches what the server will run. Run it before every package.

After reinstalling a changed zip, QGIS keeps the old module in memory. Use
the **Plugin Reloader** plugin, or restart QGIS.

## Publishing

### The constraint, first

A QGIS plugin imports `qgis.core`, so it is a derivative work of QGIS, which
is **GPL v2 or later**. Two consequences, neither avoidable:

- **plugins.qgis.org requires a GPL-compatible licence.** A closed-source
  plugin cannot be listed. This is checked at review, not just asserted.
- **GPL does not stop you charging.** You may sell GPL software at any price.
  What you cannot do is stop the buyer redistributing it with source.

This plugin is GPL-2.0-or-later and ships its LICENSE. That costs you nothing,
because the value is not in this 19 KB of glue — **it is in the weights, and
they never leave your server.** Model weights are not a derivative work of
QGIS, so the GPL does not reach them. That is the whole reason the thin-client
shape was chosen over bundling an ONNX export.

### Steps

1. **Register** an OSGeo ID at https://www.osgeo.org/community/getting-started/
   then sign in at https://plugins.qgis.org
2. **Check the metadata.** `package.py` already enforces the required fields
   and that the licence is GPL, so if it builds, the metadata will pass.
3. **Upload** `building_delineator.zip` at *Plugins ▸ Upload a plugin*. It
   lands in the **unapproved** queue.
4. **Review** by the plugin approval team: licence compatibility, no
   obfuscated code, no bundled binaries without source, sane metadata. Days,
   sometimes weeks.
5. **Approved** — it appears in every QGIS user's Plugin Manager.

`experimental=True` is set in metadata.txt. Leave it for the first release:
experimental plugins are hidden unless the user opts in, so you can publish
early without a bad first impression. Clear it when you are happy.

**Payment never goes through QGIS.** There is no store and no billing. You
sell licence keys or API credits on your own site and the plugin checks them —
which, in this shape, means the *server* checks them, which is the right place.

### If you would rather not list it publicly

Host your own plugin repository: a `plugins.xml` on any web server, which
users add under *Plugins ▸ Settings ▸ Add repository*. Still GPL, but you
choose who gets the URL. This is normal for agency and government work.

## Where the money actually is

Be realistic: the QGIS plugin market is small and price-sensitive, and most
revenue in this space comes from **processing someone's survey for them**, not
from licence sales. Treat the plugin as a sales channel and a credibility
signal for that work.

What is genuinely scarce is a model that reads dense Indian low-rise rooftops,
where off-the-shelf Western-trained models get F1 0.53 and yours gets 0.81.
That difference is the asset, and this plugin is a way to demonstrate it
inside the tool your customers already use.

## Still open before you sell

- **Imagery and labels: settled.** Both are yours, drawn in-house.
- **AICrowd weights: the one dependency.** The current model is initialised
  from HiSup's CrowdAI checkpoint. HiSup's *code* is MIT, but those weights
  were trained on the AICrowd Mapping Challenge dataset, whose terms have not
  been verified. `hisup/backbones/build.py` already initialises from ImageNet
  HRNet weights when the file is present, so retraining from there needs no
  code change — only the file and a run to confirm accuracy holds. With 50,937
  hand-drawn buildings the CrowdAI head start may now be worth very little.
- **Quote F1 0.81**, the held-out number, never one measured inside the
  training footprint. A customer who discovers the difference will not return.
