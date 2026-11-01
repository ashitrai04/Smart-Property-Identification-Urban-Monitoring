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
import json
import os
import random

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsFeature, QgsField, QgsFillSymbol, QgsGeometry, QgsPointXY,
                       QgsProject, QgsVectorLayer)
from qgis.core import QgsWkbTypes
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

# The presets that matter, measured rather than guessed - see PIPELINE.md.
# "Best" is four resolutions with a 2-vote agreement filter, worth about five
# points of F1 over a single pass for roughly four times the time.
QUALITY = [
    ("Best — four resolutions, agreement filter", "0.08,0.10,0.125,0.15", 2),
    ("Fast — single pass", "0.125", 1),
]


class DelineatorPanel(QWidget):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.aoi_geom = None          # QgsGeometry, canvas CRS
        self.resolved = None          # the address "auto" resolved to
        self.task = None
        self.layer = None
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
        v.addWidget(opt)

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
        gpu = (h.get("gpu") or {}).get("name")
        where = f"\n{url}" if asked.lower() in ("", "auto") else ""
        self.server_note.setText(f"✓ {h.get('device', '?')}"
                                 + (f" · {gpu}" if gpu else "") + where)
        self.server_note.setStyleSheet("color: #15803d;")

    # ── area ────────────────────────────────────────────────────
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
            QMessageBox.information(self, "Pick a polygon layer",
                                    "Select a polygon layer in the Layers panel first.")
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
        scales, votes = QUALITY[self.quality.currentIndex()][1:]
        params = {
            "scales": [float(x) for x in scales.split(",")],
            "min_votes": votes,
            "core": self.core.value(),
            "work_gsd_m": self.gsd.value(),
            "regularize": self.regularize.isChecked(),
        }

        self._new_layer()
        self.log.clear()
        self.bar.setValue(0)
        self.bar.setVisible(True)
        self.btn_run.setVisible(False)
        self.btn_stop.setVisible(True)

        self.task = AreaJobTask(url, self.model.currentData(), self._aoi_geojson(), params)
        self.task.found.connect(self._add_buildings)
        self.task.note.connect(self._log)
        self.task.failed.connect(self._failed)
        self.task.progressChanged.connect(lambda p: self.bar.setValue(int(p)))
        self.task.taskCompleted.connect(self._done)
        self.task.taskTerminated.connect(self._done)
        self.settle_timer.start()
        QgsApplication.taskManager().addTask(self.task)

    def stop(self):
        if self.task:
            self.task.cancel()
            self._log("Stopping…")

    def _done(self):
        self.bar.setVisible(False)
        self.btn_run.setVisible(True)
        self.btn_stop.setVisible(False)
        self.settle_timer.stop()
        self._settle(force=True)
        n = self.layer.featureCount() if self.layer else 0
        self.btn_export.setEnabled(n > 0)
        self._log(f"Finished — {n} buildings.")
        self.task = None

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
        self.count.setText("0 buildings")

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
        self.count.setText(f"{self.layer.featureCount()} buildings")

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
        if self.layer is not None:
            self.layer.setOpacity(value / 100.0)
            self.layer.triggerRepaint()

    def export(self):
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

    def unload(self):
        if self.task:
            self.task.cancel()
        self.aoi_band.reset(QgsWkbTypes.PolygonGeometry)
        if self.tool and self.canvas.mapTool() is self.tool:
            self.canvas.unsetMapTool(self.tool)


def _ring_geom(ring):
    return QgsGeometry.fromPolygonXY([[QgsPointXY(float(x), float(y)) for x, y in ring]])


def _expr(field):
    from qgis.core import QgsProperty
    return QgsProperty.fromExpression(f'"{field}"')
