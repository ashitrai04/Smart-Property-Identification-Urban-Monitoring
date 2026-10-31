"""Register the Processing provider, and put it within reach.

The algorithms are Processing algorithms, not a bespoke dialog: that is what
makes them work in the graphical modeller, run in batch over many layers, and
be callable headless from qgis_process. A hand-rolled dialog gets none of
that.

But a Processing algorithm lives in the Toolbox, which is easy to miss if you
are not already a Processing user - so the plugin also adds a toolbar button
and a menu entry that open the algorithm dialog directly. Same algorithm,
shorter path to it.
"""
import os

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QDockWidget, QMessageBox

from .provider import BuildingDelineatorProvider

MENU = "&Building Delineator"
ICON = os.path.join(os.path.dirname(__file__), "icon.svg")


class BuildingDelineatorPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.actions = []
        self.toolbar = None
        self.dock = None
        self.panel = None

    # ── lifecycle ───────────────────────────────────────────────
    def initGui(self):
        self.provider = BuildingDelineatorProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

        self.toolbar = self.iface.addToolBar("Building Delineator")
        self.toolbar.setObjectName("BuildingDelineatorToolbar")

        # The panel first: it is the one most people want, and it is the one
        # that looks like the webapp they already know.
        self._add_panel_action()
        self._add("Building footprints over an area",
                  "urbanly:detectinarea",
                  "Run a model over a boundary or extent, using imagery on the server")
        self._add("Building footprints from a raster layer",
                  "urbanly:detectinraster",
                  "Send a raster layer from this machine and get footprints back")

    def unload(self):
        if self.panel is not None:
            self.panel.unload()
            self.panel = None
        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
        for a in self.actions:
            self.iface.removePluginMenu(MENU, a)
            self.iface.removeToolBarIcon(a)
        self.actions = []
        if self.toolbar is not None:
            # Deleting it removes the (now empty) toolbar from the View menu too.
            del self.toolbar
            self.toolbar = None

    # ── wiring ──────────────────────────────────────────────────
    def _add_panel_action(self):
        action = QAction(QIcon(ICON), "Building Delineator panel", self.iface.mainWindow())
        action.setStatusTip("Draw an area, pick a model, and watch buildings appear")
        action.triggered.connect(self._show_panel)
        self.toolbar.addAction(action)
        self.iface.addPluginToMenu(MENU, action)
        self.actions.append(action)

    def _show_panel(self):
        """Build the dock the first time it is asked for, then just show it.

        Deferred rather than built in initGui so that enabling the plugin
        costs nothing until it is used, and so a panel the user closed comes
        back with its settings intact.
        """
        if self.dock is None:
            from .gui.panel import DelineatorPanel
            self.panel = DelineatorPanel(self.iface)
            self.dock = QDockWidget("Building Delineator", self.iface.mainWindow())
            self.dock.setObjectName("BuildingDelineatorDock")
            self.dock.setWidget(self.panel)
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock)
        self.dock.show()
        self.dock.raise_()

    def _add(self, text, alg_id, tip):
        action = QAction(QIcon(ICON), text, self.iface.mainWindow())
        action.setStatusTip(tip)
        action.triggered.connect(lambda _checked=False, a=alg_id: self._run(a))
        self.toolbar.addAction(action)
        self.iface.addPluginToMenu(MENU, action)
        self.actions.append(action)

    def _run(self, alg_id):
        """Open the algorithm's own dialog.

        `processing` is part of the core Processing plugin, which the user can
        disable - and if they have, the Toolbox is gone and this button is the
        only way in, so say what to do rather than raising ImportError into the
        QGIS log where nobody will look.
        """
        try:
            import processing
        except ImportError:
            QMessageBox.warning(
                self.iface.mainWindow(), "Processing is turned off",
                "This plugin runs inside QGIS's Processing framework, which is "
                "currently disabled.\n\nTurn it on in "
                "Plugins ▸ Manage and Install Plugins ▸ Installed, by "
                "ticking “Processing”, then try again.")
            return
        processing.execAlgorithmDialog(alg_id, {})

    @staticmethod
    def tr(message):
        return QCoreApplication.translate("BuildingDelineator", message)
