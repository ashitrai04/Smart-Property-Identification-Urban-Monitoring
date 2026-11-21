"""The Building Delineator dock panel.

Deliberately the same shape as the webapp's map panel, because it is the same
job and the operator should not have to learn it twice: pick an area, pick a
model, set the post-processing, run, watch buildings appear, adjust the
overlay, export.

Buildings are drawn as the server finds them. That is not decoration - an
area job is minutes, and a panel that shows nothing until it finishes is
indistinguishable from one that has hung. Each building arrives as the
detector drew it and is replaced by its regularized outline a moment later,
which are the two stages the pipeline really has.
"""
import base64
import json
import os
import random

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsFeature, QgsField, QgsFillSymbol, QgsGeometry, QgsPointXY,
                       QgsLineSymbol, QgsProject, QgsVectorLayer)
from qgis.core import Qgis, QgsMessageLog, QgsRasterLayer, QgsWkbTypes
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
                                 QFormLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox,
                                 QProgressBar, QPushButton, QSlider, QSpinBox, QTextEdit,
                                 QVBoxLayout, QWidget)

from .. import client
from .draw_tool import DrawAOITool
from .job import AreaJobTask

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
ICON = os.path.join(os.path.dirname(os.path.dirname(__file__)), "icon.svg")

# How long a building shows the detector's outline before the regularized one
# replaces it. Long enough to see the shape square off, short enough that a
# busy window does not look stalled.
SETTLE_MS = 700

# Task ids whose result is roads rather than buildings. The server decides what
# a task returns; this list only decides which controls make sense to offer and
# what to call the things that come back. Keep it in step with AREA_TASKS in
# gpu-server/app/buildings.py.
# Only one road model remains - the PARAM-trained DINOv3 + sharp decoder.
# The retired names stay here so a job queued under them is still shown
# with road controls rather than building ones.
ROAD_TASKS = ("roadnet", "roadnet_v2", "roadnet_param")

# Tasks whose result is a raster, not vectors. Land cover classifies every
# pixel into one of nine classes, so there is nothing to stream and nothing to
# count - asking "how many buildings" of it is meaningless. The class GeoTIFF
# is pulled at the end and added as a raster layer; it carries its own colour
# table, so QGIS styles it without help.
RASTER_TASKS = ("landcover", "waterchange")

# Tasks that compare two dates and therefore need TWO images chosen, not one.
# Water change runs the same land-cover model over both and differences the
# water class, so "which two" is the whole question the operator is asking -
# it cannot be inferred from the area the way a single-epoch job's can.
TWO_EPOCH_TASKS = ("waterchange",)

# The road regularizer's modes, measured against the project's own labels.
ROAD_REG = [("Smooth — follow the road edge (F1 0.786)", "smooth"),
            ("Corridor — centreline + width (F1 0.783)", "corridor"),
            ("Raw — no regularization", "none")]

# Colours for the layers a road run returns, so the two read apart at a glance.
LAYER_STYLE = {"roads": ("235,100,40", "polygon"),
               "road_centrelines": ("255,220,60", "line"),
               "centrelines": ("255,220,60", "line")}

# The presets that matter, measured rather than guessed - see PIPELINE.md.
# "Best" is four resolutions with a 2-vote agreement filter, worth about five
# points of F1 over a single pass for roughly four times the time.
QUALITY = [
    ("Best — four resolutions, agreement filter", "0.08,0.10,0.125,0.15", 2),
    ("Fast — single pass", "0.125", 1),
]



def imagery_entries(is_raster, qgis_rasters, server_rows):
    """What the Imagery box should offer. (label, data) pairs, in order.

    Pure so it can be tested without QGIS, which is the whole point: this
    decision has been got wrong twice by eye.

    Land cover runs on an upload, so it offers ONLY rasters open in the
    project - listing the server's catalogue beside them invites picking
    something that is not on screen. Every other model is a server-side job
    streaming from the server's own COGs; it cannot read a file on this
    machine, so those offer the server's catalogue and never a QGIS layer.

    qgis_rasters: (layer_id, name, pixel_size) tuples.
    server_rows:  the /area/imagery dicts.
    """
    out = []
    if is_raster:
        for lid, name, px in qgis_rasters:
            out.append(("%s%s" % (name, (" — %.2f m" % px) if px else ""),
                        ("qgis", lid)))
        if not out:
            out.append(("No raster layer open in QGIS — add one", None))
        return out

    out.append(("Auto — finest the server has over the area", None))
    for im in server_rows or []:
        gsd = im.get("gsd_m")
        out.append(("%s — %s%s" % (im.get("name") or im.get("id"),
                                    im.get("kind") or "?",
                                    (", %.2f m" % gsd) if gsd else ""),
                    im.get("id")))
    return out


def epoch_b_entries(qgis_rasters, exclude_layer_id=None):
    """What the "Compare with" box should offer: the other open rasters.

    Pure, for the same reason imagery_entries is - this is a decision worth
    testing without standing up QGIS.

    The layer already chosen as the earlier epoch is left out. Comparing a
    raster with itself returns "nothing changed" after several minutes of
    GPU time, which looks exactly like a broken model rather than a silly
    question, so it is better not to be offerable at all.
    """
    out = []
    for lid, name, px in qgis_rasters:
        if exclude_layer_id is not None and lid == exclude_layer_id:
            continue
        out.append(("%s%s" % (name, (" - %.2f m" % px) if px else ""),
                    ("qgis", lid)))
    if not out:
        out.append(("Add a second raster to QGIS to compare against", None))
    return out


class DelineatorPanel(QWidget):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.aoi_geom = None          # QgsGeometry, canvas CRS
        self.resolved = None          # the address "auto" resolved to
        self.task = None
        self.layer = None
        self.job_id = None            # set as soon as the server issues one
        self.result_layers = []       # layers built from a non-streamed result
        self.pending = []             # [(fid, reg_ring, due_ms)]
        self.tool = None
        self.prev_tool = None
        # The panel owns the AOI outline, not the map tool. A tool is created
        # fresh on every draw, so a band owned by one could never be cleared
        # by the next - which is why the old outline stayed on the canvas.
        self.aoi_band = QgsRubberBand(self.canvas, QgsWkbTypes.PolygonGeometry)
        self.aoi_band.setFillColor(QColor(0, 0, 0, 0))     # outline only
        self.aoi_band.setStrokeColor(QColor(34, 197, 94))
        self.aoi_band.setWidth(2)
        self._build()
        QTimer.singleShot(0, self.refresh_models)

        self.settle_timer = QTimer(self)
        self.settle_timer.timeout.connect(self._settle)
        self.settle_timer.setInterval(200)

    # ── layout ──────────────────────────────────────────────────
    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)

        # server
        srv = QGroupBox("Model server")
        sl = QVBoxLayout(srv)
        row = QHBoxLayout()
        self.server = QComboBox()
        self.server.setEditable(True)
        # "auto" first and selected: the plugin asks the backend where the
        # server currently is. The free tunnel changes hostname on every
        # restart, so a remembered URL goes stale but "auto" never does.
        self.server.addItems(["auto", "http://127.0.0.1:8777", "http://127.0.0.1:8800"])
        self.server.setToolTip(
            "Leave on \"auto\" to look the server up, which works whether it is "
            "on this machine or another one. Or paste an address.")
        self.btn_check = QPushButton("Check")
        self.btn_check.clicked.connect(self.refresh_models)
        row.addWidget(self.server, 1)
        row.addWidget(self.btn_check)
        sl.addLayout(row)
        self.server_note = QLabel("not checked")
        self.server_note.setWordWrap(True)
        self.server_note.setStyleSheet("color: grey;")
        sl.addWidget(self.server_note)
        v.addWidget(srv)

        # area
        area = QGroupBox("Area of interest")
        al = QVBoxLayout(area)
        row = QHBoxLayout()
        self.btn_draw = QPushButton(QIcon(ICON), "Draw on map")
        self.btn_draw.clicked.connect(self.start_draw)
        self.btn_layer = QPushButton("Use selected layer")
        self.btn_layer.clicked.connect(self.use_layer)
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(self.clear_aoi)
        for b in (self.btn_draw, self.btn_layer, self.btn_clear):
            row.addWidget(b)
        al.addLayout(row)
        self.area_note = QLabel("No area yet — draw one, or select a polygon layer.")
        self.area_note.setWordWrap(True)
        self.area_note.setStyleSheet("color: grey;")
        al.addWidget(self.area_note)
        v.addWidget(area)

        # model + post-processing
        opt = QGroupBox("Model and post-processing")
        f = QFormLayout(opt)
        self.model = QComboBox()
        f.addRow("Model", self.model)

        # Which raster to run on. Without this the server silently takes the
        # finest one that intersects the area, and with 3 cm drone cover that
        # means a satellite epoch can never be chosen however much you want to
        # compare against it.
        self.imagery = QComboBox()
        self._server_rows = []
        self.imagery.setToolTip(
            "Which registered raster to run on. “Auto” takes the finest "
            "one covering the area, which is what every run did before this "
            "box existed.")
        f.addRow("Imagery", self.imagery)

        # The second epoch. Water change needs two images and the question is
        # "which two", so both come from the layers open in QGIS - the same
        # list, so the two boxes cannot be describing different worlds.
        self.imagery_b = QComboBox()
        self.imagery_b.setToolTip(
            "The later date, compared against the one above. Both are read "
            "from the rasters open in this project, clipped to the drawn "
            "area and uploaded - the server never has to hold either of them.")
        f.addRow("Compare with", self.imagery_b)

        self.quality = QComboBox()
        self.quality.addItems([q[0] for q in QUALITY])
        f.addRow("Quality", self.quality)

        self.regularize = QCheckBox("Square off to rectilinear outlines")
        self.regularize.setChecked(True)
        self.regularize.setToolTip(
            "Block-consistent regularization: neighbours end up sharing walls "
            "instead of nearly sharing them. Costs about 0.005 of F1 and makes "
            "80% of footprints clean rectangles.")
        f.addRow("", self.regularize)

        self.core = QDoubleSpinBox()
        self.core.setRange(0.5, 0.99)
        self.core.setSingleStep(0.01)
        self.core.setValue(0.90)
        self.core.setToolTip(
            "Confidence a pixel needs to seed a building. Lower finds more and "
            "merges more terraces; higher is cleaner but loses small houses.")
        f.addRow("Seed confidence", self.core)

        self.gsd = QDoubleSpinBox()
        self.gsd.setRange(0.0, 1.0)
        self.gsd.setSingleStep(0.005)
        self.gsd.setDecimals(3)
        self.gsd.setValue(0.125)
        self.gsd.setToolTip(
            "Ground resolution the model reads at, m/px. 0.125 measured best; "
            "this is the single most sensitive setting.")
        f.addRow("Working resolution", self.gsd)

        # Road controls. Hidden for building tasks and vice versa: the previous
        # version showed "Seed confidence" and "Square off to rectilinear
        # outlines" while Road extraction was selected, which are meaningless
        # there and were quietly sent to a model that ignores them.
        self.road_reg = QComboBox()
        self.road_reg.addItems([lbl for lbl, _ in ROAD_REG])
        self.road_reg.setToolTip(
            "Smooth follows the model's own outline. Corridor rebuilds the "
            "carriageway from a smoothed centreline - cleaner to look at, "
            "marginally worse against the labels. Centrelines come back "
            "either way.")
        self.row_road_reg = f.addRow("Road shape", self.road_reg) or self.road_reg
        self._rows = f
        v.addWidget(opt)
        self.model.currentIndexChanged.connect(self._model_changed)
        # Changing the earlier epoch has to rebuild the later one's list, or
        # the layer just chosen stays offered as its own comparison.
        self.imagery.currentIndexChanged.connect(self._fill_imagery_b)

        # run
        self.btn_run = QPushButton(QIcon(ICON), "Run on this area")
        self.btn_run.clicked.connect(self.run)
        v.addWidget(self.btn_run)
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.clicked.connect(self.stop)
        self.btn_stop.setVisible(False)
        v.addWidget(self.btn_stop)

        self.bar = QProgressBar()
        self.bar.setVisible(False)
        v.addWidget(self.bar)

        # results
        res = QGroupBox("Results")
        rl = QVBoxLayout(res)
        self.count = QLabel("—")
        rl.addWidget(self.count)
        row = QHBoxLayout()
        row.addWidget(QLabel("Opacity"))
        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(0, 100)
        self.opacity.setValue(55)
        self.opacity.valueChanged.connect(self._set_opacity)
        row.addWidget(self.opacity, 1)
        rl.addLayout(row)
        self.btn_height = QPushButton("Measure heights")
        self.btn_height.clicked.connect(self.measure_heights)
        self.btn_height.setEnabled(False)
        self.btn_height.setToolTip(
            "Asks the DSM machine for a height per building, through the model "
            "server - the same route the web app uses, so the two cannot "
            "disagree. Needs that second machine to be on; if it is not, this "
            "says so and the footprints are unaffected.")
        rl.addWidget(self.btn_height)

        self.btn_export = QPushButton("Export…")
        self.btn_export.clicked.connect(self.export)
        self.btn_export.setEnabled(False)
        rl.addWidget(self.btn_export)
        v.addWidget(res)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(90)
        v.addWidget(self.log)
        v.addStretch(1)

    # ── server ──────────────────────────────────────────────────
    # ── which controls belong to which model ───────────────────
    def _is_road(self):
        return (self.model.currentData() or "") in ROAD_TASKS

    def _is_raster(self):
        return (self.model.currentData() or "") in RASTER_TASKS

    def _is_two_epoch(self):
        return (self.model.currentData() or "") in TWO_EPOCH_TASKS

    def _noun(self):
        if self._is_two_epoch():
            return "change map"
        if self._is_raster():
            return "class map"
        return "roads" if self._is_road() else "buildings"

    def _model_changed(self):
        """Show only the parameters the selected model actually reads.

        The server hands each task the parameters it knows and ignores the
        rest, so sending the wrong ones is harmless - but showing them is not.
        A "Seed confidence" spinner beside Road extraction says the number does
        something, and it does not.
        """
        road = self._is_road()
        raster = self._is_raster()
        two = self._is_two_epoch()
        if hasattr(self, "imagery"):
            self._fill_imagery()
        # The second epoch box only makes sense for a two-date comparison.
        # Leaving it visible for the others would be a control that silently
        # does nothing, which is the same mistake as the spinner below.
        if hasattr(self, "imagery_b"):
            self.imagery_b.setVisible(two)
            lbl = self._rows.labelForField(self.imagery_b)
            if lbl is not None:
                lbl.setVisible(two)
            lbl = self._rows.labelForField(self.imagery)
            if lbl is not None:
                lbl.setText("Earlier date" if two else "Imagery")
        # Land cover reads none of the building controls, and the working
        # resolution one is worse than useless there: it is sent as a floor the
        # server may not coarsen, which is what made large AOIs fail. Hiding it
        # and sending nothing lets the job size itself.
        for w in (self.quality, self.regularize, self.core, self.gsd):
            show = not road and not raster
            w.setVisible(show)
            lbl = self._rows.labelForField(w)
            if lbl is not None:
                lbl.setVisible(show)
        self.road_reg.setVisible(road)
        lbl = self._rows.labelForField(self.road_reg)
        if lbl is not None:
            lbl.setVisible(road)
        # Heights come from a DSM of rooftops; a road or a class map has none.
        if hasattr(self, "btn_height"):
            self.btn_height.setVisible(not road and not raster)
        if self.layer is None:
            self.count.setText(f"0 {self._noun()}")

    def refresh_models(self):
        asked = self.server.currentText().strip()
        self.model.clear()
        self.server_note.setText("looking…")
        self.server_note.setStyleSheet("color: grey;")
        QgsApplication.processEvents()
        url = client.resolve(asked)
        self.resolved = url
        try:
            h = client.health(url, timeout=8)
            tasks = client.tasks(url, timeout=10)
        except client.ServerError as e:
            self.server_note.setText(
                f"✗ {e}" + "\n\n"
                + ("No server found. Start it with start_everything.bat on the "
                   "machine that has the GPU — it publishes its own address, "
                   "which this panel looks up."
                   if asked.lower() in ("", "auto") else
                   "Or set this back to \"auto\" to look the address up."))
            self.server_note.setStyleSheet("color: #b91c1c;")
            return
        for t in tasks:
            self.model.addItem(t["label"], t["id"])

        self._refresh_imagery(url)
        self._model_changed()
        gpu = (h.get("gpu") or {}).get("name")
        where = f"\n{url}" if asked.lower() in ("", "auto") else ""
        self.server_note.setText(f"✓ {h.get('device', '?')}"
                                 + (f" · {gpu}" if gpu else "") + where)
        self.server_note.setStyleSheet("color: #15803d;")

    # ── area ────────────────────────────────────────────────────
    # ── running on a raster that is open in QGIS, not on the server ─────
    #
    # Land cover and water change both take this path: the AOI is clipped out
    # of the layer here, posted as an upload, and the raster that comes back
    # is georeferenced with the clip's own transform. The other models answer
    # with vectors from a server-side job and have no equivalent endpoint.
    #
    # The three steps below are shared rather than copied. The first draft of
    # water change duplicated them, and a copy of the colour-table ordering
    # rule is a copy that can be fixed in one place and left wrong in the
    # other.

    def _clip_to_aoi(self, layer_id, what="image"):
        """Clip one open QGIS raster to the drawn AOI.

        Returns a dict with the clip's path and geotransform, or None - having
        already reset the run UI and said why, so the caller just returns.
        """
        import tempfile
        from osgeo import gdal

        lyr = QgsProject.instance().mapLayer(layer_id)
        if lyr is None:
            self._reset_run_ui()
            self._log("That QGIS layer is gone.")
            return None

        src = lyr.source().split("|")[0]
        geom = self._aoi_geojson()
        if not geom:
            self._reset_run_ui()
            self._log("Draw an area first.")
            return None

        # The AOI is in EPSG:4326; the file is in its own CRS.
        ring = geom["geometry"]["coordinates"][0]
        wgs = QgsCoordinateReferenceSystem("EPSG:4326")
        tr = QgsCoordinateTransform(wgs, lyr.crs(), QgsProject.instance())
        xs, ys = [], []
        for x, y in ring:
            pt = tr.transform(QgsPointXY(float(x), float(y)))
            xs.append(pt.x()); ys.append(pt.y())
        ulx, uly, lrx, lry = min(xs), max(ys), max(xs), min(ys)

        px = lyr.rasterUnitsPerPixelX() or 0.0
        wpx = int(abs(lrx - ulx) / px) if px else 0
        hpx = int(abs(uly - lry) / px) if px else 0
        self._log("Clipping %s (%s) — %d x %d px at %.2f m"
                  % (lyr.name(), what, wpx, hpx, px))

        # The predict endpoints refuse anything over 8192 px a side, and a
        # whole-scene clip would be far over. Downsample the clip rather than
        # fail, and say so - the model works at ~0.2-0.3 m anyway.
        opts = {"format": "GTiff", "projWin": [ulx, uly, lrx, lry],
                "projWinSRS": lyr.crs().toWkt(), "bandList": [1, 2, 3],
                "creationOptions": ["COMPRESS=DEFLATE"]}
        cap = 8192
        if wpx > cap or hpx > cap:
            k = cap / float(max(wpx, hpx))
            opts["width"] = max(1, int(wpx * k))
            opts["height"] = max(1, int(hpx * k))
            self._log("   too large for one request — resampled to %d x %d"
                      % (opts["width"], opts["height"]))

        clip = os.path.join(tempfile.gettempdir(),
                            "urbanly_clip_%s_%s.tif" % (what, layer_id[:8]))
        try:
            ds = gdal.Translate(clip, src, **opts)
            if ds is None:
                raise RuntimeError("GDAL could not clip that area out of the layer")
            out = {"path": clip, "gt": ds.GetGeoTransform(),
                   "wkt": ds.GetProjection(), "cw": ds.RasterXSize,
                   "ch": ds.RasterYSize, "px": px, "layer": lyr}
            ds = None
        except Exception as e:
            self._reset_run_ui()
            self._log("Clip failed: %s" % e)
            return None
        self._log("   uploading %.1f MB…" % (os.path.getsize(clip) / 1e6))
        return out

    def _georeference(self, b64, clip, rows, out_name):
        """Give a returned class/change PNG the clip's position, and write it.

        Returns the GeoTIFF's path, or None (having already said why).
        """
        from osgeo import gdal

        png = os.path.splitext(clip["path"])[0] + "_%s.png" % out_name
        with open(png, "wb") as fh:
            fh.write(base64.b64decode(b64))

        # The model may have worked at a different size, so the pixel size is
        # scaled by the ratio rather than copied - copying it is how a result
        # ends up the right shape in the wrong place. Water change makes that
        # the normal case rather than the exception: its shared grid is the
        # COARSER of the two epochs, so the result rarely matches either clip.
        out_tif = os.path.splitext(clip["path"])[0] + "_%s.tif" % out_name
        gt = clip["gt"]
        png_ds = gdal.Open(png)
        if png_ds is None:
            self._log("Could not read the raster the server sent back.")
            return None
        lw, lh = png_ds.RasterXSize, png_ds.RasterYSize
        drv = gdal.GetDriverByName("GTiff")
        dst = drv.Create(out_tif, lw, lh, 1, gdal.GDT_Byte, ["COMPRESS=DEFLATE"])
        dst.SetGeoTransform((gt[0], gt[1] * clip["cw"] / float(lw), gt[2],
                             gt[3], gt[4], gt[5] * clip["ch"] / float(lh)))
        dst.SetProjection(clip["wkt"])
        band = dst.GetRasterBand(1)

        # Colour table BEFORE the pixels, and this order is not a style
        # preference. A palette sets the TIFF PhotometricInterpretation tag,
        # which libtiff will not let you change once data has been written -
        # the other order dies with
        #   TIFFSetField: Cannot modify tag "PhotometricInterpretation"
        #                 while writing
        # and loses the whole result at the last step. Reproduced both ways in
        # QGIS's own GDAL before fixing.
        #
        # The colours come from the legend in the response, not a copy kept
        # here: a second palette is a second palette that can drift. Class 0
        # is transparent in both models - "unknown" for land cover, "no water"
        # for water change - so the imagery stays visible underneath.
        #
        # The per-class alpha below is set but does NOT survive the write: a
        # TIFF ColorMap tag is RGB only, so everything except the nodata class
        # reads back opaque. Measured, not assumed - a round trip through
        # QGIS's own GDAL returns 255 for a class written as 235. It is left
        # in because it costs nothing and is right if the format ever carries
        # it; what actually keeps the result readable as an overlay is class 0
        # being nodata, which is most of the raster.
        ct = gdal.ColorTable()
        for r in (rows or []):
            h = (r.get("color") or "").lstrip("#")
            if len(h) != 6:
                continue
            rgb = tuple(int(h[k:k + 2], 16) for k in (0, 2, 4))
            ct.SetColorEntry(int(r.get("id", 0)),
                             rgb + (0 if r.get("name") in ("unknown", "no water")
                                    else int(r.get("alpha", 255)),))
        band.SetRasterColorTable(ct)
        band.SetNoDataValue(0)
        band.WriteArray(png_ds.GetRasterBand(1).ReadAsArray())
        band = None; dst = None; png_ds = None
        return out_tif

    def _style_class_raster(self, layer, rows):
        """Give a class raster a paletted renderer with labelled classes.

        A GeoTIFF colour table makes QGIS draw the right colours, but its
        legend entry is just the raster name - the Layers panel shows no key,
        so nothing on screen says which colour is "encroached" and which is
        "receded". A paletted renderer carries a LABEL per value, and that is
        what QGIS prints in the legend, in the print layout and in the map
        export.

        Falls back silently to the colour table if this QGIS cannot build the
        renderer: a drawn-but-unlabelled layer beats an exception.
        """
        try:
            from qgis.core import (QgsPalettedRasterRenderer,
                                   QgsRasterShader, QgsColorRampShader)
        except ImportError:
            return False
        classes = []
        for r in (rows or []):
            try:
                v = int(r.get("id", -1))
            except (TypeError, ValueError):
                continue
            # Value 0 is "nothing here" in both models and is drawn
            # transparent; listing it would put a meaningless row at the top
            # of every legend.
            if v <= 0:
                continue
            h = (r.get("color") or "").lstrip("#")
            if len(h) != 6:
                continue
            col = QColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
            name = r.get("label") or r.get("name") or str(v)
            area = r.get("area_ha")
            if isinstance(area, (int, float)) and area > 0:
                name = "%s  (%.2f ha)" % (name, area)
            elif isinstance(r.get("percent"), (int, float)):
                name = "%s  (%.1f%%)" % (name, r["percent"])
            classes.append(QgsPalettedRasterRenderer.Class(v, col, name))
        if not classes:
            return False
        try:
            layer.setRenderer(QgsPalettedRasterRenderer(
                layer.dataProvider(), 1, classes))
            layer.triggerRepaint()
            return True
        except Exception:
            return False

    def _run_on_qgis_layer(self, layer_id, url):
        """Classify the AOI out of a QGIS layer the server does not have."""
        if not self._is_raster():
            self._reset_run_ui()
            QMessageBox.information(
                self, "Not supported for this model",
                "Running on a QGIS layer is implemented for Land cover and "
                "Water change only. For the other models, pick a Server "
                "entry, or ingest the raster so it joins the server's "
                "catalogue.")
            return

        clip = self._clip_to_aoi(layer_id)
        if clip is None:
            return

        try:
            res = client.landcover_predict(clip["path"], server=url,
                                           gsd=(clip["px"] or None))
        except Exception as e:
            self._reset_run_ui()
            self._log("The server refused the upload: %s" % str(e)[:200])
            return

        b64 = res.get("classes_png")
        if not b64:
            # An older server only returned the class map alongside the RGB
            # overlay. Ask again the expensive way rather than fail: the
            # overlay is wasted bytes here, but a result beats an error.
            self._log("   server did not send a class map; retrying with overlay…")
            try:
                res = client.landcover_predict(clip["path"], server=url,
                                               gsd=(clip["px"] or None),
                                               want_overlay=True, preview_max=256)
            except Exception as e:
                self._reset_run_ui()
                self._log("Retry failed: %s" % str(e)[:200])
                return
            b64 = res.get("classes_png")
        if not b64:
            self._reset_run_ui()
            self._log("The server returned no class map.")
            return

        rows_all = res.get("classes") or []
        out_tif = self._georeference(b64, clip, rows_all, "classes")
        if out_tif is None:
            self._reset_run_ui()
            return

        name = clip["layer"].name()
        out = QgsRasterLayer(out_tif, "Land cover — %s" % name)
        if not out.isValid():
            self._reset_run_ui()
            self._log("QGIS could not open %s" % out_tif)
            return
        self._style_class_raster(out, rows_all)
        out.setOpacity(self.opacity.value() / 100.0)
        QgsProject.instance().addMapLayer(out)
        self.result_layers.append(out)
        self.class_raster = out_tif
        rows = [r for r in rows_all
                if r.get("name") != "unknown" and (r.get("percent") or 0) > 0.05]
        rows.sort(key=lambda r: -(r.get("percent") or 0))
        self.class_rows = rows
        for r in rows:
            ha = r.get("area_ha")
            self._log("   %-12s %5.1f%%%s" % (r.get("name"), r.get("percent") or 0,
                                              ("  %.1f ha" % ha) if ha else ""))
        self._reset_run_ui()
        self.btn_export.setEnabled(True)
        self.count.setText("class map")
        self._log("Finished — class raster from %s added to the map." % name)

    def _run_waterchange_on_qgis_layers(self, layer_a_id, layer_b_id, url):
        """Compare two open QGIS rasters over the drawn AOI.

        Both epochs are clipped to the same area and posted together, so the
        server puts them on one grid and classifies both with the same model.
        Sending them as two separate land-cover runs and differencing here
        would be the same arithmetic over two independently chosen working
        resolutions, which measures the resampling as much as the water.
        """
        clip_a = self._clip_to_aoi(layer_a_id, "earlier")
        if clip_a is None:
            return
        clip_b = self._clip_to_aoi(layer_b_id, "later")
        if clip_b is None:
            return

        name_a = clip_a["layer"].name()
        name_b = clip_b["layer"].name()
        self._log("Comparing %s → %s — the model runs over both, so give it "
                  "a minute or two." % (name_a, name_b))

        # gsd is deliberately not sent: it applies to BOTH images, so forcing
        # one layer's pixel size onto the other would be a lie about the
        # coarser one and every area would come out wrong. A clip out of a
        # QGIS layer is a GeoTIFF and carries its own.
        try:
            res = client.waterchange_predict(clip_a["path"], clip_b["path"],
                                             server=url)
        except Exception as e:
            self._reset_run_ui()
            self._log("The server refused the upload: %s" % str(e)[:200])
            return

        b64 = res.get("change_png")
        if not b64:
            self._reset_run_ui()
            self._log("The server returned no change map.")
            return

        rows_all = res.get("classes") or []
        # The change map comes back on the SHARED grid, which may match
        # neither clip. _georeference scales by the ratio, so either clip
        # places it correctly - they were cut from one AOI.
        out_tif = self._georeference(b64, clip_a, rows_all, "waterchange")
        if out_tif is None:
            self._reset_run_ui()
            return

        out = QgsRasterLayer(out_tif, "Water change — %s → %s" % (name_a, name_b))
        if not out.isValid():
            self._reset_run_ui()
            self._log("QGIS could not open %s" % out_tif)
            return
        self._style_class_raster(out, rows_all)
        out.setOpacity(self.opacity.value() / 100.0)
        QgsProject.instance().addMapLayer(out)
        self.result_layers.append(out)
        self.class_raster = out_tif

        st = res.get("stats") or {}
        a_m2 = st.get("water_area_a_m2") or 0
        b_m2 = st.get("water_area_b_m2") or 0
        pct = st.get("net_change_pct")
        self._log("   water %.2f ha → %.2f ha   %+.2f ha%s"
                  % (a_m2 / 1e4, b_m2 / 1e4, (b_m2 - a_m2) / 1e4,
                     ("  (%+.1f%%)" % pct) if pct is not None else ""))
        rows = [r for r in rows_all
                if r.get("name") != "no water" and (r.get("area_m2") or 0) > 0]
        self.class_rows = rows
        for r in rows:
            self._log("   %-13s %9.0f m2  %6.2f ha"
                      % (r.get("label") or r.get("name"),
                         r.get("area_m2") or 0, r.get("area_ha") or 0))
        # Worth saying every run rather than hoping it is remembered: a thin
        # rim of change along a shoreline is the two dates not lining up, and
        # a bund exposed by falling water classifies as built ground, so real
        # recession can read as encroachment.
        self._log("   Check encroachment along a shoreline against the "
                  "imagery before reporting it.")
        self._reset_run_ui()
        self.btn_export.setEnabled(True)
        self.count.setText("change map")
        self._log("Finished — change raster added to the map.")

    def _refresh_imagery(self, url=None):
        """Re-read the server's catalogue, then rebuild the Imagery box."""
        try:
            self._server_rows = client.imagery(url or self.resolved, timeout=10)
        except Exception:
            self._server_rows = getattr(self, "_server_rows", [])
        self._fill_imagery()
        return len(getattr(self, "_server_rows", []))

    def _qgis_rasters(self):
        """File-backed raster layers open in this project, top of the tree first.

        File-backed only: a WMS or an in-memory layer has no file to clip, so
        offering it would be a choice that cannot be acted on.
        """
        out = []
        root = QgsProject.instance().layerTreeRoot()
        for node in root.findLayers():
            lyr = node.layer()
            if not isinstance(lyr, QgsRasterLayer) or not lyr.isValid():
                continue
            src = lyr.source().split("|")[0]
            if not os.path.exists(src):
                continue
            out.append(lyr)
        return out

    def _fill_imagery(self):
        """Render imagery_entries() into the combo, keeping the selection."""
        want = self.imagery.currentData() if self.imagery.count() else None
        rasters = []
        for lyr in self._qgis_rasters():
            try:
                px = lyr.rasterUnitsPerPixelX()
            except Exception:
                px = 0
            rasters.append((lyr.id(), lyr.name(), px))
        entries = imagery_entries(self._is_raster(), rasters,
                                  getattr(self, "_server_rows", []))
        self.imagery.blockSignals(True)
        self.imagery.clear()
        for label, data in entries:
            self.imagery.addItem(label, data)
        if want is not None:
            i = self.imagery.findData(want)
            if i >= 0:
                self.imagery.setCurrentIndex(i)
        self.imagery.blockSignals(False)
        self._fill_imagery_b()

    def _fill_imagery_b(self):
        """Render epoch_b_entries() into the second combo, keeping the choice."""
        if not hasattr(self, "imagery_b"):
            return
        want = self.imagery_b.currentData() if self.imagery_b.count() else None
        chosen = self.imagery.currentData() if self.imagery.count() else None
        exclude = chosen[1] if isinstance(chosen, tuple) and chosen[0] == "qgis" else None
        rasters = []
        for lyr in self._qgis_rasters():
            try:
                px = lyr.rasterUnitsPerPixelX()
            except Exception:
                px = 0
            rasters.append((lyr.id(), lyr.name(), px))
        self.imagery_b.blockSignals(True)
        self.imagery_b.clear()
        for label, data in epoch_b_entries(rasters, exclude):
            self.imagery_b.addItem(label, data)
        if want is not None:
            i = self.imagery_b.findData(want)
            if i >= 0:
                self.imagery_b.setCurrentIndex(i)
        self.imagery_b.blockSignals(False)

    def start_draw(self):
        self.prev_tool = self.canvas.mapTool()
        self.tool = DrawAOITool(self.canvas)
        self.tool.finished.connect(self._drawn)
        self.canvas.setMapTool(self.tool)
        self.area_note.setText("Click to add corners, right-click or Enter to finish, "
                               "Backspace to undo, Esc to cancel.")

    def _drawn(self, geom):
        if geom is not None:
            self.aoi_geom = geom
            self._show_aoi()
            self._describe_aoi()
        if self.prev_tool is not None:
            self.canvas.setMapTool(self.prev_tool)

    def use_layer(self):
        layer = self.iface.activeLayer()
        if not isinstance(layer, QgsVectorLayer) or layer.geometryType() != 2:
            # The active layer is often the result just added - a raster for a
            # land-cover run - so refusing outright and saying nothing else
            # reads as a dead button. Fall back to the project's polygon
            # layers: use the only one if there is only one, otherwise name
            # them so the operator knows what to select.
            polys = [l for l in QgsProject.instance().mapLayers().values()
                     if isinstance(l, QgsVectorLayer) and l.geometryType() == 2]
            if len(polys) == 1:
                layer = polys[0]
                self._log("Using the only polygon layer: %s" % layer.name())
            elif polys:
                QMessageBox.information(
                    self, "Pick a polygon layer",
                    "Select one of these in the Layers panel first:" + chr(10) + chr(10)
                    + chr(10).join("  - " + l.name() for l in polys[:12]))
                return
            else:
                QMessageBox.information(
                    self, "Pick a polygon layer",
                    "This project has no polygon layer to take an area from." + chr(10)
                    + "Use “Draw on map” instead, or load a polygon layer.")
                return
        feats = list(layer.selectedFeatures()) or list(layer.getFeatures())
        geoms = [f.geometry() for f in feats if not f.geometry().isEmpty()]
        if not geoms:
            QMessageBox.information(self, "Nothing to use", "That layer has no polygons.")
            return
        g = QgsGeometry.unaryUnion(geoms)
        tr = QgsCoordinateTransform(layer.crs(), self.canvas.mapSettings().destinationCrs(),
                                    QgsProject.instance())
        g.transform(tr)
        self.aoi_geom = g
        self._show_aoi()
        self._describe_aoi(f"from {layer.name()}")

    def _describe_aoi(self, extra=""):
        # Area in square kilometres, which is the unit the operator thinks in
        # and the one that predicts how long this will take.
        d = QgsCoordinateTransform(self.canvas.mapSettings().destinationCrs(), WGS84,
                                   QgsProject.instance())
        g = QgsGeometry(self.aoi_geom)
        g.transform(d)
        from qgis.core import QgsDistanceArea
        da = QgsDistanceArea()
        da.setSourceCrs(WGS84, QgsProject.instance().transformContext())
        da.setEllipsoid("WGS84")
        km2 = da.measureArea(g) / 1e6
        self.area_note.setText(f"Area ready{' ' + extra if extra else ''} — {km2:.3f} km²")
        self.area_note.setStyleSheet("color: #15803d;")

    def _show_aoi(self):
        self.aoi_band.reset(QgsWkbTypes.PolygonGeometry)
        if self.aoi_geom is not None:
            self.aoi_band.setToGeometry(self.aoi_geom, None)

    def clear_aoi(self):
        self.aoi_geom = None
        if self.tool:
            self.tool.reset()
            # Drop the drawing tool too, or the next click keeps adding
            # vertices to an area the operator just cleared.
            if self.canvas.mapTool() is self.tool:
                self.canvas.unsetMapTool(self.tool)
            self.tool = None
        self.aoi_band.reset(QgsWkbTypes.PolygonGeometry)
        self.area_note.setText("No area yet — draw one, or select a polygon layer.")
        self.area_note.setStyleSheet("color: grey;")

    def _aoi_geojson(self):
        d = QgsCoordinateTransform(self.canvas.mapSettings().destinationCrs(), WGS84,
                                   QgsProject.instance())
        g = QgsGeometry(self.aoi_geom)
        g.transform(d)
        return {"type": "Feature", "properties": {}, "geometry": json.loads(g.asJson())}

    # ── running ─────────────────────────────────────────────────
    def run(self):
        if self.aoi_geom is None:
            QMessageBox.information(self, "No area",
                                    "Draw an area, or select a polygon layer, first.")
            return
        if self.model.count() == 0:
            QMessageBox.information(self, "No models",
                                    "Check the server first — no model list yet.")
            return
        # Re-resolve rather than reusing what Check found. The free tunnel takes
        # a new hostname whenever the server restarts, and the old one stops
        # resolving entirely - so an address that was right when Check ran can
        # be dead by the time Run is pressed, which failed the job with
        # "getaddrinfo failed" while the server itself was perfectly fine.
        # resolve() is cheap and prefers localhost, so this costs nothing when
        # QGIS and the server share a machine.
        url = client.resolve(self.server.currentText())
        self.resolved = url
        if self._is_raster():
            # Nothing from the building controls applies, and work_gsd_m in
            # particular must NOT be sent: the server treats an explicit value
            # as a floor it may not coarsen, so passing this panel's 0.125 m
            # made every AOI over ~0.2 km2 fail with "82 megapixels ... needs
            # 1814 MB". Left unset, the job picks a resolution that fits and
            # says in its message when it had to coarsen.
            params = {}
        elif self._is_road():
            mode = ROAD_REG[self.road_reg.currentIndex()][1]
            params = {"reg_mode": mode, "regularize": mode != "none"}
        else:
            scales, votes = QUALITY[self.quality.currentIndex()][1:]
            params = {
                "scales": [float(x) for x in scales.split(",")],
                "min_votes": votes,
                "core": self.core.value(),
                "work_gsd_m": self.gsd.value(),
                "regularize": self.regularize.isChecked(),
            }

        # Drop the previous run's result layers. Leaving them behind is how a
        # second road run reads as twice as many roads.
        for lyr in self.result_layers:
            try:
                QgsProject.instance().removeMapLayer(lyr.id())
            except Exception:
                pass
        self.result_layers = []
        self._new_layer()
        self.log.clear()
        self.bar.setValue(0)
        self.bar.setVisible(True)
        self.btn_run.setVisible(False)
        self.btn_stop.setVisible(True)

        self.job_id = None

        # Everything from here to addTask runs AFTER the panel has switched
        # itself into the running state, so anything that raises in between
        # leaves a progress bar, a live Stop button and no task behind it -
        # a panel that looks busy forever and a Stop that cannot work, because
        # self.task is still None. That is exactly what a NameError in this
        # block did. The job never started; the panel only looked like it had.
        try:
            # Which raster to run on. Blank means "let the server choose",
            # which is the finest one covering the area - the behaviour
            # before this control existed.
            if (hasattr(self, "imagery") and not self._is_raster()
                    and self.imagery.count() <= 1):
                # The server may have gained /area/imagery since this panel
                # loaded. One cheap GET beats telling the operator to press
                # Check. Only for server-side models - land cover's list comes
                # from the open project and never needs the server.
                self._refresh_imagery(url)
            chosen_img = self.imagery.currentData() if hasattr(self, "imagery") else None

            is_qgis = isinstance(chosen_img, tuple) and chosen_img[0] == "qgis"
            if self._is_raster() and not is_qgis:
                # These models run on an open QGIS raster. Falling through here
                # would quietly queue a server job against whatever raster the
                # server thinks is best - the opposite of choosing.
                self._reset_run_ui()
                QMessageBox.information(
                    self, "No image selected",
                    "%s runs on a raster layer open in QGIS. Add one to the "
                    "project, then pick it in the %s box."
                    % (self.model.currentText(),
                       "Earlier date" if self._is_two_epoch() else "Imagery"))
                return

            if self._is_two_epoch():
                # Two dates, both chosen here. Checked before any clipping so
                # a missing second epoch costs a dialog, not several minutes.
                chosen_b = self.imagery_b.currentData()
                if not (isinstance(chosen_b, tuple) and chosen_b[0] == "qgis"):
                    self._reset_run_ui()
                    QMessageBox.information(
                        self, "No second image",
                        "%s compares two dates. Pick the later one in the "
                        "“Compare with” box - it has to be a raster "
                        "open in this project too."
                        % self.model.currentText())
                    return
                if chosen_b[1] == chosen_img[1]:
                    self._reset_run_ui()
                    QMessageBox.information(
                        self, "Same image twice",
                        "Both boxes name the same layer, so there is nothing "
                        "to compare. Pick a different later date.")
                    return
                self._run_waterchange_on_qgis_layers(chosen_img[1], chosen_b[1], url)
                return

            if is_qgis:
                # A layer the server has never seen. Clip and upload instead
                # of queuing an area job, which can only read the server's
                # own catalogue.
                self._run_on_qgis_layer(chosen_img[1], url)
                return

            if chosen_img:
                params["imagery_id"] = chosen_img
                self._log("Imagery: %s" % self.imagery.currentText())

            self.task = AreaJobTask(url, self.model.currentData(),
                                    self._aoi_geojson(), params)
            self.task.started_job.connect(self._job_started)
            self.task.found.connect(self._add_buildings)
            self.task.note.connect(self._log)
            self.task.failed.connect(self._failed)
            self.task.progressChanged.connect(lambda p: self.bar.setValue(int(p)))
            self.task.taskCompleted.connect(self._done)
            self.task.taskTerminated.connect(self._done)
            self.settle_timer.start()
            QgsApplication.taskManager().addTask(self.task)
        except Exception as e:
            import traceback
            self._reset_run_ui()
            self._log("Could not start the job: %s" % e)
            QgsMessageLog.logMessage(traceback.format_exc(), "Building Delineator",
                                     Qgis.Critical)
            QMessageBox.warning(self, "Could not start", str(e))

    def _reset_run_ui(self):
        """Put the panel back to idle. Safe to call twice."""
        self.settle_timer.stop()
        self.bar.setVisible(False)
        self.btn_run.setVisible(True)
        self.btn_stop.setVisible(False)
        self.task = None

    def stop(self):
        if self.task:
            self.task.cancel()
            self._log("Stopping…")
            return
        # No task: the panel is wedged in the running state rather than
        # actually running something. Give the operator their buttons back
        # instead of a Stop that does nothing.
        self._reset_run_ui()
        self._log("Nothing was running — panel reset.")

    def _job_started(self, jid):
        self.job_id = jid

    def _load_result(self):
        """Fetch a result the job never streamed.

        Building runs publish each window as it finishes, so the map fills in
        while the model works. The road model does not - it writes its layers
        once, at the end - so the panel sat at "0 buildings" through a run that
        had in fact succeeded, and showed nothing when it finished. This is the
        other half: when nothing arrived live, ask for the finished result.
        """
        if not self.job_id:
            return 0
        try:
            fc = client.area_result(self.job_id, server=self.resolved)
        except client.ServerError as e:
            self._log(f"Could not fetch the result: {e}")
            return 0
        feats = fc.get("features") or []
        if not feats:
            return 0

        # Split by the layer each feature names - a road run returns polygons
        # and centrelines together, and a line in a polygon layer is dropped
        # silently by the provider, which is its own kind of invisible failure.
        groups = {}
        for ft in feats:
            name = str((ft.get("properties") or {}).get("layer") or "result").lower()
            groups.setdefault(name, []).append(ft)

        total = 0
        for name, items in sorted(groups.items()):
            colour, kind = LAYER_STYLE.get(name, ("235,100,40", "polygon"))
            geom_kinds = {(f.get("geometry") or {}).get("type", "") for f in items}
            if kind != "line" and geom_kinds & {"LineString", "MultiLineString"}:
                kind = "line"
            wkb = "MultiLineString" if kind == "line" else "MultiPolygon"
            lyr = QgsVectorLayer(f"{wkb}?crs=EPSG:4326",
                                 f"Urbanly {name.replace('_', ' ')}", "memory")
            dp = lyr.dataProvider()
            dp.addAttributes([QgsField("fid", 2), QgsField("area_m2", 6),
                              QgsField("length_m", 6), QgsField("layer", 10)])
            lyr.updateFields()
            made = []
            for i, ft in enumerate(items):
                g = QgsGeometry.fromWkt(_geojson_wkt(ft.get("geometry")))
                if g is None or g.isEmpty():
                    continue
                f = QgsFeature(lyr.fields())
                f.setGeometry(g)
                props = ft.get("properties") or {}
                f["fid"] = i + 1
                f["area_m2"] = float(props.get("area_m2") or 0)
                f["length_m"] = float(props.get("length_m") or 0)
                f["layer"] = name[:10]
                made.append(f)
            if not made:
                continue
            dp.addFeatures(made)
            if kind == "line":
                lyr.renderer().setSymbol(
                    QgsLineSymbol.createSimple({"color": colour, "width": "0.6"}))
            else:
                lyr.renderer().setSymbol(QgsFillSymbol.createSimple(
                    {"color": colour + ",90", "outline_color": colour,
                     "outline_width": "0.3"}))
            lyr.updateExtents()
            QgsProject.instance().addMapLayer(lyr)
            self.result_layers.append(lyr)
            total += len(made)
            self._log(f"{len(made)} {name.replace('_', ' ')}")
        return total

    def _load_classes(self):
        """Pull a finished land-cover job's class raster and add it to the map.

        The GeoTIFF has its own colour table, so QGIS renders the nine classes
        straight away. The legend call is best-effort: if it fails the raster
        is still on the map, which is the part that matters.
        """
        import tempfile
        if not self.job_id:
            return 0
        two = self._is_two_epoch()
        kind = "waterchange" if two else "landcover"
        title = "Water change" if two else "Land cover"
        dest = os.path.join(tempfile.gettempdir(), "%s_%s.tif" % (kind, self.job_id))
        try:
            client.download_classes(self.job_id, dest, server=self.resolved)
        except Exception as e:
            self._log("Could not fetch the class raster: %s" % str(e)[:140])
            return 0
        lyr = QgsRasterLayer(dest, "%s — %s" % (title, self.job_id[:12]))
        if not lyr.isValid():
            self._log("QGIS could not open %s" % dest)
            return 0
        self.class_rows = []
        rows = []
        try:
            rows = (client.result_legend(self.job_id, server=self.resolved) or {}).get("classes", [])
            # Land cover reports a SHARE of the area, water change reports an
            # AREA. Filtering on percent alone dropped every water-change row,
            # so the run finished with a coloured raster and no key at all.
            rows = [r for r in rows
                    if r.get("name") not in ("unknown", "no water")
                    and ((r.get("percent") or 0) > 0.05 or (r.get("area_m2") or 0) > 0)]
            rows.sort(key=lambda r: -(r.get("percent") or r.get("area_m2") or 0))
            self.class_rows = rows
            for r in rows:
                pct, ha = r.get("percent"), r.get("area_ha")
                bits = []
                if isinstance(pct, (int, float)):
                    bits.append("%5.1f%%" % pct)
                if isinstance(ha, (int, float)) and ha:
                    bits.append("%.2f ha" % ha)
                self._log("   %-13s %s" % (r.get("label") or r.get("name"),
                                           "  ".join(bits)))
        except Exception:
            pass
        # Labelled classes, so the Layers panel says what each colour means,
        # and the panel's own opacity slider applies from the start.
        self._style_class_raster(lyr, rows)
        lyr.setOpacity(self.opacity.value() / 100.0)
        QgsProject.instance().addMapLayer(lyr)
        self.result_layers.append(lyr)
        self.class_raster = dest
        return 1

    def _done(self):
        self.bar.setVisible(False)
        self.btn_run.setVisible(True)
        self.btn_stop.setVisible(False)
        self.settle_timer.stop()
        self._settle(force=True)
        if self._is_raster():
            # No features by design; the result is the class raster.
            n = self._load_classes()
            if self.layer is not None and self.layer.featureCount() == 0:
                QgsProject.instance().removeMapLayer(self.layer.id())
                self.layer = None
            self.btn_export.setEnabled(bool(n))
            noun = "change map" if self._is_two_epoch() else "class map"
            self.count.setText(noun if n else "no result")
            self._log(("Finished — %s added to the map." % noun) if n
                      else "Finished, but the raster could not be loaded.")
            if hasattr(self, "btn_height"):
                self.btn_height.setEnabled(False)
            self.task = None
            return
        n = self.layer.featureCount() if self.layer else 0
        if n == 0:
            # Nothing streamed. Either the task publishes only at the end - the
            # road model does - or it genuinely found nothing. Asking costs one
            # request and is the difference between a result and a blank map.
            n = self._load_result()
            if self.layer is not None and self.layer.featureCount() == 0:
                QgsProject.instance().removeMapLayer(self.layer.id())
                self.layer = None
        self.btn_export.setEnabled(n > 0)
        self.count.setText(f"{n} {self._noun()}")
        self._log(f"Finished — {n} {self._noun()}." if n
                  else "Finished — nothing found in this area.")
        if hasattr(self, "btn_height"):
            self.btn_height.setEnabled(bool(self.job_id) and n > 0
                                       and not self._is_road())
        self.task = None

    def measure_heights(self):
        """Join DSM heights onto the footprints of the run just finished.

        POST /height/area/<jid> answers with a GeoJSON FeatureCollection -
        {"type", "features", "height_status", "layer"} - where every feature
        carries the fields height.py's FIELDS names, in the order the job's
        GeoPackage stores them. An earlier version of this method guessed at
        that shape, looked for res["items"], found nothing, and reported "the
        height server returned nothing" for responses that were complete.
        """
        if not (self.job_id and self.layer):
            return
        self.btn_height.setEnabled(False)
        self._log("Asking the height server…")
        try:
            res = client.enrich_area_heights(self.job_id, server=self.resolved)
        except client.ServerError as e:
            self._log(f"No heights: {e}")
            QMessageBox.information(
                self, "Height server",
                f"Could not measure heights:\n\n{e}\n\n"
                "This needs the second machine - the one holding the DSM and "
                "DTM - to be running and reachable. The footprints are "
                "unaffected.")
            self.btn_height.setEnabled(True)
            return

        items = res.get("features") or []
        status = res.get("height_status") or {}
        state = status.get("state", "?")

        # The server distinguishes "no DSM machine configured" from "measured
        # nothing", and the difference is the whole of what to do next.
        if state in ("not_configured", "error", "failed") or not items:
            detail = status.get("detail") or status.get("reason") or ""
            self._log(f"No heights ({state}){': ' + detail if detail else ''}")
            QMessageBox.information(
                self, "Height server",
                {"not_configured":
                     "No height server is configured. The machine holding the "
                     "DSM and DTM has to be running, and this server has to "
                     "know its address - see height-server/README.md.",
                 "failed":
                     "The height server was reached but measured nothing. "
                     "Usually that means the DSM does not cover this area.",
                 "empty":
                     "That job has no footprints to measure."}.get(
                     state, f"No heights came back (state: {state}).")
                + (f"\n\n{detail}" if detail else ""))
            self.btn_height.setEnabled(True)
            return

        # Add the fields once. Only the handful worth carrying into QGIS - the
        # server returns sixteen, and a attribute table of diagnostics is worse
        # than one of answers.
        want = [("height_m", 6), ("floors", 6), ("floor_label", 10),
                ("roof_elev_m", 6), ("ground_elev_m", 6), ("quality", 10)]
        dp = self.layer.dataProvider()
        have = {f.name() for f in self.layer.fields()}
        add = [QgsField(n, t) for n, t in want if n not in have]
        if add:
            dp.addAttributes(add)
            self.layer.updateFields()
        idx = {n: self.layer.fields().indexOf(n) for n, _ in want}

        # Position, not id: the server returns the same features, in the same
        # order, as the GeoPackage this job wrote.
        fids = [f.id() for f in self.layer.getFeatures()]
        changes, n = {}, 0
        for fid, item in zip(fids, items):
            props = item.get("properties") or {}
            if props.get("height_m") is None:
                continue
            row = {}
            for name, _ in want:
                v = props.get(name)
                if v is None or idx[name] < 0:
                    continue
                row[idx[name]] = v if isinstance(v, str) else float(v)
            if row:
                changes[fid] = row
                n += 1
        if changes:
            dp.changeAttributeValues(changes)
            self.layer.triggerRepaint()

        total = len(fids)
        summary = status.get("summary") or {}
        mean = summary.get("mean_height_m")
        self._log(f"Heights: {n} of {total} measured"
                  + (f", mean {mean:.1f} m" if isinstance(mean, (int, float)) else "")
                  + (f" ({state})" if state != "ok" else ""))
        if len(items) != total:
            self._log(f"  note: server returned {len(items)} features for "
                      f"{total} on screen - matched by position, so check one.")
        self.btn_height.setEnabled(True)

    def _failed(self, message):
        self._log(f"Failed: {message}")
        QMessageBox.warning(self, "The job failed", message)

    def _log(self, text):
        self.log.append(text)

    # ── the result layer ────────────────────────────────────────
    def _new_layer(self):
        self.layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "Urbanly buildings", "memory")
        dp = self.layer.dataProvider()
        dp.addAttributes([QgsField("building_id", 2), QgsField("area_m2", 6),
                          QgsField("colour", 10), QgsField("stage", 10)])
        self.layer.updateFields()
        # One colour per building, data-defined: adjacent houses sharing a wall
        # only read as separate buildings when they are drawn differently.
        sym = QgsFillSymbol.createSimple({"color": "0,0,0,0", "outline_width": "0.4"})
        sym.symbolLayer(0).setDataDefinedProperty(
            sym.symbolLayer(0).PropertyFillColor, _expr("colour"))
        sym.symbolLayer(0).setDataDefinedProperty(
            sym.symbolLayer(0).PropertyStrokeColor, _expr("colour"))
        self.layer.renderer().setSymbol(sym)
        self._set_opacity(self.opacity.value())
        QgsProject.instance().addMapLayer(self.layer)
        self.pending = []
        self.count.setText(f"0 {self._noun()}")

    def _add_buildings(self, items):
        if self.layer is None:
            return
        dp = self.layer.dataProvider()
        feats = []
        import time
        due = time.time() * 1000 + SETTLE_MS
        for it in items:
            colour = "%d,%d,%d" % (random.randint(60, 230), random.randint(60, 230),
                                   random.randint(60, 230))
            f = QgsFeature(self.layer.fields())
            f.setGeometry(_ring_geom(it["raw"]))
            f["building_id"] = 0
            f["area_m2"] = float(it.get("area_m2") or 0)
            f["colour"] = colour
            f["stage"] = "raw"
            feats.append((f, it["reg"]))
        ok, added = dp.addFeatures([f for f, _ in feats])
        for (f, reg), made in zip(feats, added):
            self.pending.append((made.id(), reg, due))
        self.layer.updateExtents()
        self.layer.triggerRepaint()
        self.count.setText(f"{self.layer.featureCount()} {self._noun()}")

    def _settle(self, force=False):
        """Replace detector outlines with the regularized ones once they are due."""
        if not self.pending or self.layer is None:
            return
        import time
        now = time.time() * 1000
        still, changes, attrs = [], {}, {}
        for fid, reg, due in self.pending:
            if force or now >= due:
                changes[fid] = _ring_geom(reg)
                idx = self.layer.fields().indexFromName("stage")
                attrs[fid] = {idx: "reg"}
            else:
                still.append((fid, reg, due))
        if changes:
            self.layer.dataProvider().changeGeometryValues(changes)
            self.layer.dataProvider().changeAttributeValues(attrs)
            self.layer.triggerRepaint()
        self.pending = still

    def _set_opacity(self, value):
        """Drive whatever this run produced - vectors OR the class raster.

        It only ever reached self.layer, which is the vector result, so for
        land cover and water change the slider moved and nothing happened:
        the overlay sat at full strength over the imagery with no way to see
        through it.
        """
        o = value / 100.0
        if self.layer is not None:
            self.layer.setOpacity(o)
            self.layer.triggerRepaint()
        for lyr in list(getattr(self, "result_layers", [])):
            try:
                lyr.setOpacity(o)
                lyr.triggerRepaint()
            except (AttributeError, RuntimeError):
                pass          # layer removed from the project under us

    def export(self):
        if self._is_raster() or getattr(self, "class_raster", None):
            return self._export_classes()
        if self.layer is None or self.layer.featureCount() == 0:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export footprints", "buildings.gpkg",
            "GeoPackage (*.gpkg);;GeoJSON (*.geojson);;ESRI Shapefile (*.shp)")
        if not path:
            return
        from qgis.core import QgsVectorFileWriter
        fmt = {".gpkg": "GPKG", ".geojson": "GeoJSON", ".shp": "ESRI Shapefile"}.get(
            os.path.splitext(path)[1].lower(), "GPKG")
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = fmt
        err = QgsVectorFileWriter.writeAsVectorFormatV3(
            self.layer, path, QgsProject.instance().transformContext(), opts)
        if err[0] == QgsVectorFileWriter.NoError:
            self._log(f"Exported to {path}")
        else:
            QMessageBox.warning(self, "Export failed", str(err))

    def _export_classes(self):
        """Save the class raster, and the class table beside it.

        The GeoTIFF is copied rather than re-rendered: it already carries the
        colour table and the CRS, so a copy is lossless and opens anywhere.
        The CSV is written alongside because the per-class areas are the thing
        a land-use survey actually reports, and re-deriving them from the
        raster later means getting the pixel area right all over again.
        """
        import shutil
        src = getattr(self, "class_raster", None)
        two = self._is_two_epoch()
        if not src or not os.path.exists(src):
            QMessageBox.information(
                self, "Nothing to export",
                "Run %s first." % ("the water-change model" if two
                                   else "the land-cover model"))
            return
        # Named for the model that made it. A water-change raster saved as
        # landcover_*.tif is a file that will be misread a month from now.
        default = "%s_%s.tif" % ("waterchange" if two else "landcover",
                                 self.job_id or "result")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export %s" % ("change map" if two else "class map"),
            default, "GeoTIFF (*.tif *.tiff)")
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".tif"
        try:
            shutil.copyfile(src, path)
        except Exception as e:
            QMessageBox.warning(self, "Export failed", str(e))
            return
        self._log("Exported %s to %s"
                  % ("change map" if two else "class map", path))

        rows = getattr(self, "class_rows", None) or []
        if rows:
            csv_path = os.path.splitext(path)[0] + (
                "_change.csv" if two else "_classes.csv")
            try:
                with open(csv_path, "w", encoding="utf-8") as fh:
                    fh.write("class,percent,area_m2,area_ha,colour\n")
                    for r in rows:
                        fh.write("%s,%s,%s,%s,%s\n" % (
                            r.get("name", ""), r.get("percent", ""),
                            r.get("area_m2", ""), r.get("area_ha", ""),
                            r.get("color", "")))
                self._log("Class areas written to %s" % csv_path)
            except Exception as e:
                self._log("Could not write the class table: %s" % str(e)[:120])

    def unload(self):
        if self.task:
            self.task.cancel()
        self.aoi_band.reset(QgsWkbTypes.PolygonGeometry)
        if self.tool and self.canvas.mapTool() is self.tool:
            self.canvas.unsetMapTool(self.tool)


def _geojson_wkt(geom):
    """GeoJSON geometry -> WKT.

    QgsGeometry has no GeoJSON constructor in every QGIS this plugin supports,
    and json->WKT by hand is short enough that pulling in a dependency for it
    would be worse. Handles the four types the server actually emits.
    """
    if not geom:
        return ""
    t = geom.get("type")
    c = geom.get("coordinates") or []

    def pts(seq):
        return ", ".join(f"{p[0]} {p[1]}" for p in seq)

    def ring(r):
        return "(" + pts(r) + ")"

    def poly(p):
        return "(" + ", ".join(ring(r) for r in p) + ")"

    if t == "Polygon":
        return "MULTIPOLYGON((" + ", ".join(ring(r) for r in c) + "))"
    if t == "MultiPolygon":
        return "MULTIPOLYGON(" + ", ".join(poly(p) for p in c) + ")"
    if t == "LineString":
        return "MULTILINESTRING(" + ring(c) + ")"
    if t == "MultiLineString":
        return "MULTILINESTRING(" + ", ".join(ring(r) for r in c) + ")"
    return ""


def _ring_geom(ring):
    return QgsGeometry.fromPolygonXY([[QgsPointXY(float(x), float(y)) for x, y in ring]])


def _expr(field):
    from qgis.core import QgsProperty
    return QgsProperty.fromExpression(f'"{field}"')
