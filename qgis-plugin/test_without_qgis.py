"""Exercise the plugin's logic with QGIS stubbed out.

QGIS cannot be launched from this environment, and "it parses" is not the same
as "it runs". This stubs the qgis and osgeo imports with just enough behaviour
to import the algorithms, build them, and run the pure-Python parts - GeoJSON
to features, and the area/extent handling - then checks the rest against a
real server. It catches the mistakes that actually happen: a name imported
from the wrong module, a typo in a method, a geometry branch never taken, a
field written that was never declared.

Run:  python test_without_qgis.py [server-url]
"""
import sys
import types

# ── stub qgis ────────────────────────────────────────────────────
class _AnyMeta(type):
    """Lets class-level lookups resolve too, e.g. QTimer.singleShot."""
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Any()


class _Any(metaclass=_AnyMeta):
    """Stands in for any QGIS class: constructs, allows anything, returns self."""
    def __init__(self, *a, **k):
        self._a, self._k = a, k

    def __getattr__(self, n):
        if n.startswith("__"):
            raise AttributeError(n)
        return _Any()

    def __call__(self, *a, **k):
        return _Any()

    def __or__(self, other):
        return self


class _Point:
    def __init__(self, x, y):
        self.x, self.y = float(x), float(y)


class _Geometry:
    def __init__(self, rings=None, kind="Polygon"):
        self.rings, self.kind = rings or [], kind

    @staticmethod
    def fromPolygonXY(rings):
        return _Geometry(rings, "Polygon")

    @staticmethod
    def fromMultiPolygonXY(polys):
        return _Geometry([r for p in polys for r in p], "MultiPolygon")

    def isEmpty(self):
        return not self.rings


class _Fields:
    def __init__(self):
        self.names = []

    def append(self, f):
        self.names.append(f.name)

    def __len__(self):
        return len(self.names)


class _Field:
    def __init__(self, name, typ=None):
        self.name = name


class _Feature:
    def __init__(self, fields=None):
        self.fields, self.attrs, self.geom = fields, {}, None

    def setGeometry(self, g):
        self.geom = g

    def __setitem__(self, k, v):
        # The real QgsFeature silently ignores an unknown field; that would
        # hide a typo until someone opened the attribute table.
        if self.fields is not None and k not in self.fields.names:
            raise KeyError(f"field {k!r} was never declared in out_fields()")
        self.attrs[k] = v

    def __getitem__(self, k):
        return self.attrs[k]


class _Rect:
    def __init__(self, empty=False, b=(0, 0, 1, 1)):
        self._empty, self._b = empty, b

    def isEmpty(self):
        return self._empty

    def xMinimum(self):
        return self._b[0]

    def yMinimum(self):
        return self._b[1]

    def xMaximum(self):
        return self._b[2]

    def yMaximum(self):
        return self._b[3]


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)

    def emit(self, *a):
        for fn in self.slots:
            fn(*a)


class _Action:
    def __init__(self, icon=None, text="", parent=None):
        self.text, self.tip = text, ""
        self.triggered = _Signal()

    def setStatusTip(self, t):
        self.tip = t


class _ToolBar:
    def __init__(self):
        self.actions = []

    def setObjectName(self, n):
        self.name = n

    def addAction(self, a):
        self.actions.append(a)


class _Iface:
    """Just enough QgisInterface to see whether initGui/unload are balanced."""
    def __init__(self):
        self.toolbars, self.menu, self.icons, self.docks = [], [], [], []

    def addToolBar(self, name):
        tb = _ToolBar()
        self.toolbars.append(tb)
        return tb

    def mainWindow(self):
        return None

    def mapCanvas(self):
        return _Any()

    def activeLayer(self):
        return None

    def addDockWidget(self, area, dock):
        self.docks.append(dock)

    def removeDockWidget(self, dock):
        self.docks.remove(dock)

    def addPluginToMenu(self, menu, action):
        self.menu.append((menu, action))

    def removePluginMenu(self, menu, action):
        self.menu.remove((menu, action))

    def removeToolBarIcon(self, action):
        self.icons.append(action)


def _install_stubs():
    """Install stub qgis / osgeo modules.

    Any attribute not defined explicitly resolves to _Any, so the stubs do not
    have to be extended every time the plugin imports another Qt widget. The
    few classes that are spelled out are the ones whose behaviour the tests
    actually depend on.
    """
    def catch_all(mod):
        mod.__getattr__ = lambda name: _Any
        return mod

    qgis = types.ModuleType("qgis")
    core = catch_all(types.ModuleType("qgis.core"))
    core.QgsProcessingException = type("QgsProcessingException", (RuntimeError,), {})
    core.QgsPointXY = _Point
    core.QgsGeometry = _Geometry
    core.QgsFields = _Fields
    core.QgsField = _Field
    core.QgsFeature = _Feature
    qgis.core = core
    gui = catch_all(types.ModuleType("qgis.gui"))
    qgis.gui = gui

    pyqt = types.ModuleType("qgis.PyQt")
    qtcore = catch_all(types.ModuleType("qgis.PyQt.QtCore"))
    qtcore.QVariant = _Any()
    qtcore.Qt = _Any()
    qtcore.pyqtSignal = lambda *a, **k: _Signal()
    qtgui = catch_all(types.ModuleType("qgis.PyQt.QtGui"))
    qtwidgets = catch_all(types.ModuleType("qgis.PyQt.QtWidgets"))
    qtwidgets.QAction = _Action
    pyqt.QtCore, pyqt.QtGui, pyqt.QtWidgets = qtcore, qtgui, qtwidgets
    qgis.PyQt = pyqt

    for name, mod in (("qgis", qgis), ("qgis.core", core), ("qgis.gui", gui),
                      ("qgis.PyQt", pyqt), ("qgis.PyQt.QtCore", qtcore),
                      ("qgis.PyQt.QtGui", qtgui), ("qgis.PyQt.QtWidgets", qtwidgets)):
        sys.modules[name] = mod

    sys.modules["osgeo"] = catch_all(types.ModuleType("osgeo"))


_install_stubs()

# ── harness ──────────────────────────────────────────────────────
FAILS = []


def check(label, fn):
    try:
        fn()
        print(f"  PASS  {label}")
    except Exception as e:
        FAILS.append(label)
        print(f"  FAIL  {label}: {type(e).__name__}: {e}")


def main():
    server = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8777"
    sys.path.insert(0, ".")

    print("importing the plugin with QGIS stubbed")
    from building_delineator import client
    from building_delineator.algorithms import common
    from building_delineator.algorithms.detect_area import DetectInAreaAlgorithm
    from building_delineator.algorithms.detect_raster import DetectInRasterAlgorithm
    from building_delineator.provider import BuildingDelineatorProvider
    print("  imports OK\n")

    print("algorithm metadata")
    for cls in (DetectInRasterAlgorithm, DetectInAreaAlgorithm):
        a = cls()

        def meta(a=a):
            assert a.name() and a.name().islower(), "name must be a lowercase id"
            assert a.displayName() and a.group() and a.groupId()
            assert "<p>" in a.shortHelpString(), "help should be rich text"
            assert isinstance(a.createInstance(), cls)
        check(f"{cls.__name__} metadata", meta)
    check("provider ids", lambda: (lambda p: (
        p.id() and p.name() and p.longName()))(BuildingDelineatorProvider()))

    print("\nplugin GUI wiring")
    import building_delineator.plugin as plugin_mod
    registry = []
    plugin_mod.QgsApplication = type("A", (), {
        "processingRegistry": staticmethod(lambda: type("R", (), {
            "addProvider": staticmethod(lambda p: registry.append(p)),
            "removeProvider": staticmethod(lambda p: registry.remove(p)),
        })())})

    iface = _Iface()
    plug = plugin_mod.BuildingDelineatorPlugin(iface)
    plug.initGui()
    check("initGui registers the provider", lambda: _eq(len(registry), 1))
    check("initGui adds three toolbar buttons (panel + two algorithms)",
          lambda: _eq(len(iface.toolbars[0].actions), 3))
    check("initGui adds three menu entries", lambda: _eq(len(iface.menu), 3))

    # The panel is the first button and is built lazily, so this is also the
    # only check here that the panel module imports and constructs at all.
    iface.toolbars[0].actions[0].triggered.emit()
    check("the panel button builds and docks the panel",
          lambda: (_eq(len(iface.docks), 1), _eq(plug.panel is not None, True)))
    check("asking twice reuses the same dock",
          lambda: (iface.toolbars[0].actions[0].triggered.emit(),
                   _eq(len(iface.docks), 1)))

    shown = []
    plugin_mod.QMessageBox = type("MB", (), {
        "warning": staticmethod(lambda *a, **k: shown.append(a[1]))})

    def button_without_processing():
        # Processing disabled is exactly the state this plugin was first
        # opened in, so the button must explain it rather than raise an
        # ImportError into the QGIS log where nobody will look.
        sys.modules["processing"] = None
        try:
            iface.toolbars[0].actions[1].triggered.emit()
        finally:
            del sys.modules["processing"]
        assert shown, "no message shown when Processing is off"

    check("a button with Processing off explains what to turn on",
          button_without_processing)

    ran = []
    fake = types.ModuleType("processing")
    fake.execAlgorithmDialog = lambda alg, params: ran.append(alg)
    sys.modules["processing"] = fake
    iface.toolbars[0].actions[1].triggered.emit()
    iface.toolbars[0].actions[2].triggered.emit()
    del sys.modules["processing"]
    check("the two buttons open the two algorithms",
          lambda: _eq(ran, ["urbanly:detectinarea", "urbanly:detectinraster"]))

    plug.unload()
    check("unload removes the provider, menu entries and the dock",
          lambda: (_eq(len(registry), 0), _eq(len(iface.menu), 0),
                   _eq(len(iface.docks), 0)))

    print("\nserver discovery")
    check("an explicit URL passes straight through",
          lambda: _eq(client.resolve("http://example.test:1234/"),
                      "http://example.test:1234"))
    check("auto falls back to localhost when discovery finds nothing",
          lambda: _with_no_discovery(
              client, lambda: _eq(client.resolve("auto"), client.LOCAL_SERVER)))
    check("discover returns None rather than raising when offline",
          lambda: _with_no_discovery(
              client, lambda: _eq(client.discover(timeout=1), None)))
    check("an empty server box behaves like auto",
          lambda: _with_no_discovery(
              client, lambda: _eq(client.resolve(""), client.LOCAL_SERVER)))

    # The field failure this guards: the free tunnel takes a new hostname on
    # every restart and the old one stops resolving, so a published address is
    # not evidence that it works. Returning one unchecked killed a run with
    # "getaddrinfo failed" while the server was up on localhost.
    def _dead_discovery(body):
        real_d, real_a = client.discover, client.alive
        client.discover = lambda timeout=8: "https://gone.invalid"
        client.alive = lambda base, timeout=2.5: False
        try:
            return body()
        finally:
            client.discover, client.alive = real_d, real_a

    check("a published address that does not answer is not returned",
          lambda: _dead_discovery(
              lambda: _eq(client.resolve("auto"), client.LOCAL_SERVER)))

    print("\nGeoJSON to features")
    fc = {"features": [
        {"geometry": {"type": "Polygon",
                      "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
         "properties": {"building_id": 7, "area_m2": 42.5, "n_vertices": 4,
                        "layer": "buildings"}},
        {"geometry": {"type": "Polygon",
                      "coordinates": [[[0, 0], [1, 0], [0, 1], [0, 0]],
                                      [[0.2, 0.2], [0.4, 0.2], [0.3, 0.4], [0.2, 0.2]]]},
         "properties": {}},
        {"geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
         "properties": {}},
        {"geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 1]]]},
         "properties": {}},
    ]}
    feats = common.features_from_geojson(fc, common.out_fields())
    check("keeps 2 real polygons, drops the line and the 2-point ring",
          lambda: _eq(len(feats), 2))
    check("properties carried across",
          lambda: (_eq(feats[0]["building_id"], 7), _eq(feats[0]["area_m2"], 42.5)))
    check("missing properties default instead of crashing",
          lambda: (_eq(feats[1]["building_id"], 2), _eq(feats[1]["area_m2"], None)))
    check("interior ring kept (a courtyard is not building)",
          lambda: _eq(len(feats[1].geom.rings), 2))
    check("empty collection is not an error",
          lambda: _eq(len(common.features_from_geojson({"features": []},
                                                       common.out_fields())), 0))

    print("\narea-of-interest handling")
    alg = DetectInAreaAlgorithm()
    alg.parameterAsSource = lambda *a, **k: None
    alg.parameterAsExtent = lambda *a, **k: _Rect(b=(80.0, 15.0, 80.1, 15.1))
    aoi = alg._aoi({}, None, _Any())

    def ring_ok():
        ring = aoi["geometry"]["coordinates"][0]
        _eq(aoi["geometry"]["type"], "Polygon")
        _eq(len(ring), 5)
        assert ring[0] == ring[-1], "ring must close"
    check("an extent becomes a closed 5-point ring", ring_ok)

    alg.parameterAsExtent = lambda *a, **k: _Rect(empty=True)
    check("no boundary and no extent gives None, so the caller can explain",
          lambda: _eq(alg._aoi({}, None, _Any()), None))

    print(f"\nagainst the real server at {server}")
    try:
        h = client.health(server, timeout=8)
    except client.ServerError as e:
        print(f"  SKIP  server not reachable ({e})")
    else:
        check("health reports a building model",
              lambda: _in("hisup", h.get("capabilities") or []))
        check("check_server accepts it",
              lambda: common.check_server(client, server, _Any()))

        def dead_url():
            try:
                common.check_server(client, "http://127.0.0.1:9", _Any())
            except Exception as e:
                assert "cannot reach" in str(e) or "start_everything" in str(e).lower(), str(e)
                return
            raise AssertionError("a dead server should have been reported")
        check("a dead URL is reported in plain words", dead_url)

        live = [t["id"] for t in client.tasks(server)]
        check("the plugin's model list matches the server's",
              lambda: _eq(live, [t[0] for t in alg._tasks]))

    print()
    print(("FAILED: " + ", ".join(FAILS)) if FAILS else "all checks passed")
    return 1 if FAILS else 0


def _with_no_discovery(client, fn):
    """Run fn with the discovery endpoint pointed somewhere dead.

    A laptop with no route to the backend is the normal case this has to
    survive, so it is worth testing rather than assuming.
    """
    old = client.DISCOVERY
    client.DISCOVERY = "http://127.0.0.1:9/nothing"
    try:
        return fn()
    finally:
        client.DISCOVERY = old


def _eq(got, want):
    assert got == want, f"expected {want!r}, got {got!r}"


def _in(needle, hay):
    assert needle in hay, f"{needle!r} not in {hay!r}"


if __name__ == "__main__":
    sys.exit(main())
