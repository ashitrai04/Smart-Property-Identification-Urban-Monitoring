"""QGIS entry point.

QGIS calls classFactory(iface) when the plugin is enabled; everything else is
deferred to plugin.py so that a syntax error in the algorithms cannot stop
QGIS from starting.
"""


def classFactory(iface):
    from .plugin import BuildingDelineatorPlugin
    return BuildingDelineatorPlugin(iface)
