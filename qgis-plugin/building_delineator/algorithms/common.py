"""Shared pieces for the algorithms: the server parameter, and turning the
server's GeoJSON into QGIS features.

The server always answers in EPSG:4326, so that is the output CRS. QGIS
reprojects on the fly, and writing it as anything else here would mean doing a
transform twice.
"""
from qgis.core import (QgsCoordinateReferenceSystem, QgsFeature, QgsField, QgsFields,
                       QgsGeometry, QgsPointXY, QgsProcessingException,
                       QgsProcessingParameterString)
from qgis.PyQt.QtCore import QVariant

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
SERVER = "SERVER"
DEFAULT_SERVER = "http://127.0.0.1:8777"


def server_param():
    return QgsProcessingParameterString(
        SERVER,
        "Model server URL",
        defaultValue=DEFAULT_SERVER,
        optional=False,
    )


def out_fields():
    f = QgsFields()
    f.append(QgsField("building_id", QVariant.Int))
    f.append(QgsField("area_m2", QVariant.Double))
    f.append(QgsField("n_vertices", QVariant.Int))
    f.append(QgsField("layer", QVariant.String))
    return f


def features_from_geojson(fc, fields):
    """GeoJSON polygons -> QgsFeature, skipping anything that is not a ring.

    Holes are kept: a courtyard inside a building is not building.
    """
    out = []
    for i, feat in enumerate(fc.get("features") or [], 1):
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        coords = geom.get("coordinates")
        if gtype == "Polygon":
            polys = [coords]
        elif gtype == "MultiPolygon":
            polys = coords
        else:
            continue
        rings = []
        for poly in polys:
            for ring in poly:
                if ring and len(ring) >= 4:
                    rings.append([QgsPointXY(float(x), float(y)) for x, y in ring])
        if not rings:
            continue
        g = QgsGeometry.fromPolygonXY(rings) if gtype == "Polygon" else \
            QgsGeometry.fromMultiPolygonXY([[r] for r in rings])
        if g.isEmpty():
            continue
        props = feat.get("properties") or {}
        f = QgsFeature(fields)
        f.setGeometry(g)
        f["building_id"] = int(props.get("building_id") or i)
        f["area_m2"] = float(props["area_m2"]) if props.get("area_m2") is not None else None
        f["n_vertices"] = int(props["n_vertices"]) if props.get("n_vertices") is not None else None
        f["layer"] = str(props.get("layer") or "buildings")
        out.append(f)
    return out


def check_server(client, url, feedback):
    """Fail early and in plain words, rather than at the first upload."""
    try:
        h = client.health(url)
    except client.ServerError as e:
        raise QgsProcessingException(
            f"{e}\n\nStart the server with start_everything.bat, or set the URL to "
            f"the public address the webapp uses.") from None
    caps = h.get("capabilities") or []
    if "hisup" not in caps:
        raise QgsProcessingException(
            f"{url} is reachable but has no building model installed "
            f"(capabilities: {', '.join(caps) or 'none'}).")
    feedback.pushInfo(f"Server {url} — {h.get('device', '?')}"
                      + (f", {h['gpu']['name']}" if h.get("gpu") else ""))
    return h
