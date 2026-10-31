from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon
import os

from .algorithms.detect_raster import DetectInRasterAlgorithm
from .algorithms.detect_area import DetectInAreaAlgorithm


class BuildingDelineatorProvider(QgsProcessingProvider):
    def loadAlgorithms(self):
        self.addAlgorithm(DetectInRasterAlgorithm())
        self.addAlgorithm(DetectInAreaAlgorithm())

    def id(self):
        return "urbanly"

    def name(self):
        return "Urbanly"

    def longName(self):
        return "Urbanly building delineation"

    def icon(self):
        p = os.path.join(os.path.dirname(__file__), "icon.svg")
        return QIcon(p) if os.path.exists(p) else QgsProcessingProvider.icon(self)
