# Building Delineator — QGIS plugin

One polygon per building, not a semantic mask.

The plugin holds no model. It sends imagery, or the area you drew, to an
Urbanly model server and brings back footprints already regularized to
rectilinear outlines. QGIS needs no PyTorch, no CUDA and no GPU — which is
also why it installs in seconds rather than asking you to pip install into
the QGIS Python.

## Two algorithms, in Processing ▸ Urbanly ▸ Buildings

**Building footprints from a raster layer** — for imagery on your own
machine. Clips the extent you choose, resamples to 12.5 cm (where the model
scores best), sends it, and writes the footprints as a polygon layer.
Georeferenced input only: the server reads the ground resolution from the
file.

**Building footprints over an area** — for imagery already ingested on the
server. Nothing is uploaded; the server reads only the windows it needs from
the orthomosaic, so the area can be far larger than anything you could send.
The progress bar is the server's real progress, and cancelling in QGIS
cancels the job on the server.

They are Processing algorithms rather than a dialog, so they work in the
graphical modeller, run in batch over many layers, and can be driven headless
from `qgis_process`.

## Install

Plugins ▸ Manage and Install Plugins ▸ Install from ZIP, and pick
`building_delineator.zip`. Then set **Model server URL** to your server —
`http://127.0.0.1:8777` when it is the same machine, or the public address
the webapp uses.

## Licence

GPL-2.0-or-later. A QGIS plugin imports `qgis.core` and is therefore a
derivative work of QGIS, which is GPL; there is no way around that and no
reason to want one. The model weights are a separate work and are not
covered by it — they stay on the server.
