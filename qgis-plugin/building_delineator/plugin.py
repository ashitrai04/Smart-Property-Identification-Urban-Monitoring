"""Register the Processing provider, and nothing else.

There is no toolbar button and no dialog on purpose. A Processing algorithm
appears in the Toolbox, works inside the graphical modeller, runs in batch
over many layers, and can be called headless from qgis_process on a server. A
bare dialog does none of that, and this is a batch tool by nature.
"""
from qgis.core import QgsApplication

from .provider import BuildingDelineatorProvider


class BuildingDelineatorPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.provider = None

    def initGui(self):
        self.provider = BuildingDelineatorProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def unload(self):
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
