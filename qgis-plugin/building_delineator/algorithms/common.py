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
DEFAULT_SERVER = "auto"


def server_param():
    return QgsProcessingParameterString(
        SERVER,
        "Model server URL (\"auto\" finds it)",
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


def line_fields():
    f = QgsFields()
    f.append(QgsField("length_m", QVariant.Double))
    f.append(QgsField("layer", QVariant.String))
    return f


def lines_from_geojson(fc, fields):
    """GeoJSON LineStrings -> QgsFeature, skipping anything that is not a line.

    The road model returns carriageway polygons and their centrelines in one
    collection. features_from_geojson keeps the polygons and drops everything
    else; this is its mirror, so the centrelines reach their own sink instead
    of being silently discarded.
    """
    out = []
    for feat in fc.get("features") or []:
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        coords = geom.get("coordinates")
        if gtype == "LineString":
            parts = [coords]
        elif gtype == "MultiLineString":
            parts = coords
        else:
            continue
        pts = [[QgsPointXY(float(x), float(y)) for x, y in p]
               for p in parts if p and len(p) >= 2]
        if not pts:
            continue
        g = (QgsGeometry.fromPolylineXY(pts[0]) if len(pts) == 1
             else QgsGeometry.fromMultiPolylineXY(pts))
        if g.isEmpty():
            continue
        props = feat.get("properties") or {}
        f = QgsFeature(fields)
        f.setGeometry(g)
        f["length_m"] = float(props["length_m"]) if props.get("length_m") is not None else None
        f["layer"] = str(props.get("layer") or "road_centrelines")
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
    return url
