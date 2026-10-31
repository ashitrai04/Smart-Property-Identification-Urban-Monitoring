"""Draw the area of interest on the map canvas.

Click to place vertices, right-click to close. Matches the webapp's Draw AOI
rather than QGIS's extent widget, because an extent is a rectangle and a ward
boundary is not: squaring a real shape off to its bounding box would process
ground the operator deliberately left out.
"""
from qgis.core import QgsGeometry, QgsPointXY, QgsWkbTypes
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor


class DrawAOITool(QgsMapTool):
    finished = pyqtSignal(object)     # QgsGeometry in the canvas CRS, or None

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.points = []
        self.band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.band.setColor(QColor(34, 197, 94, 60))
        self.band.setStrokeColor(QColor(34, 197, 94))
        self.band.setWidth(2)
        # A second band follows the cursor so the edge being drawn is visible
        # before it is committed; without it the shape only appears on click.
        self.ghost = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
        self.ghost.setColor(QColor(34, 197, 94, 140))
        self.ghost.setWidth(1)
        self.setCursor(Qt.CrossCursor)

    # ── canvas events ───────────────────────────────────────────
    def canvasPressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.points.append(self.toMapCoordinates(e.pos()))
            self._redraw()
        elif e.button() == Qt.RightButton:
            self._commit()

    def canvasMoveEvent(self, e):
        if not self.points:
            return
        self.ghost.reset(QgsWkbTypes.LineGeometry)
        self.ghost.addPoint(self.points[-1], False)
        self.ghost.addPoint(self.toMapCoordinates(e.pos()), True)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.reset()
            self.finished.emit(None)
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._commit()
        elif e.key() == Qt.Key_Backspace and self.points:
            self.points.pop()
            self._redraw()

    def deactivate(self):
        self.ghost.reset(QgsWkbTypes.LineGeometry)
        super().deactivate()

    # ── state ───────────────────────────────────────────────────
    def reset(self):
        self.points = []
        self.band.reset(QgsWkbTypes.PolygonGeometry)
        self.ghost.reset(QgsWkbTypes.LineGeometry)

    def _redraw(self):
        self.band.reset(QgsWkbTypes.PolygonGeometry)
        for i, p in enumerate(self.points):
            self.band.addPoint(QgsPointXY(p), i == len(self.points) - 1)

    def _commit(self):
        if len(self.points) < 3:
            # Two points are a line, not an area; keep drawing rather than
            # emitting something the server would reject.
            return
        geom = QgsGeometry.fromPolygonXY([[QgsPointXY(p) for p in self.points]
                                          + [QgsPointXY(self.points[0])]])
        self.ghost.reset(QgsWkbTypes.LineGeometry)
        self.finished.emit(geom)
