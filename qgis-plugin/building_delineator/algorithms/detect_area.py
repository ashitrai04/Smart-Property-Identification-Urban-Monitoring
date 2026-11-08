"""Delineate buildings over an area, using imagery already on the server.

Nothing is uploaded: the server holds the orthomosaic and reads the windows it
needs straight out of the COG. That is what makes a square kilometre practical
from a laptop on a hotel wifi.
"""
from qgis.core import (QgsCoordinateTransform, QgsProcessing, QgsProcessingAlgorithm,
                       QgsProcessingException, QgsProcessingParameterEnum,
                       QgsProcessingParameterExtent, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProject, QgsWkbTypes)

from .. import client
from .common import (SERVER, WGS84, check_server, features_from_geojson, line_fields,
                     lines_from_geojson, out_fields, server_param)


class DetectInAreaAlgorithm(QgsProcessingAlgorithm):
    BOUNDARY = "BOUNDARY"
    EXTENT = "EXTENT"
    TASK = "TASK"
    OUTPUT = "OUTPUT"
    CENTRELINES = "CENTRELINES"

    # Filled from the server when the dialog opens, so the list cannot drift
    # from what the server will actually run.
    def __init__(self):
        super().__init__()
        self._tasks = [("hisup", "Building delineation"),
                       ("roadnet", "Road extraction (DINOv3)"),
                       ("segment", "Land-use segmentation"),
                       ("fusion", "Segmentation + SAM")]

    def name(self):
        return "detectinarea"

    def displayName(self):
        return "Building footprints over an area"

    def group(self):
        return "Buildings"

    def groupId(self):
        return "buildings"

    def shortHelpString(self):
        return (
            "<p>Runs a model over an area of imagery <b>already ingested on the model "
            "server</b> and returns the features it finds.</p>"
            "<p>Nothing is uploaded. Give it a boundary polygon — or just an extent — "
            "and the server reads only the windows it needs from the orthomosaic, so "
            "the area can be far larger than anything you could send.</p>"
            "<p>Progress in the Processing dialog is the server's real progress: it "
            "reports windows completed, and cancelling here cancels the job there.</p>"
            "<p>Use <i>Building footprints from a raster layer</i> instead when the "
            "imagery is a file on your own machine.</p>")

    def createInstance(self):
        return DetectInAreaAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.BOUNDARY, "Boundary polygons (optional)",
            [QgsProcessing.TypeVectorPolygon], optional=True))
        self.addParameter(QgsProcessingParameterExtent(
            self.EXTENT, "…or an extent", optional=True))
        self.addParameter(server_param())
        self.addParameter(QgsProcessingParameterEnum(
            self.TASK, "Model", options=[label for _, label in self._tasks],
            defaultValue=0))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, "Detected features", type=QgsProcessing.TypeVectorPolygon))
        # Road extraction returns centrelines beside the carriageway polygons.
        # They cannot share a sink with the polygons, so they get their own;
        # for every other model it simply comes back empty.
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.CENTRELINES, "Road centrelines", type=QgsProcessing.TypeVectorLine,
            optional=True, createByDefault=False))

    def processAlgorithm(self, parameters, context, feedback):
        url = check_server(client, self.parameterAsString(parameters, SERVER, context),
                           feedback)
        task = self._tasks[self.parameterAsEnum(parameters, self.TASK, context)][0]

        aoi = self._aoi(parameters, context, feedback)
        if aoi is None:
            raise QgsProcessingException(
                "Give the area to process: either a boundary layer or an extent.")

        feedback.pushInfo(f"Running {task} on the server…")
        try:
            fc, st = client.run_area(
                aoi, task=task, server=url,
                on_progress=lambda pct, msg: (feedback.setProgress(pct),
                                              feedback.pushInfo(msg)),
                is_cancelled=feedback.isCanceled)
        except client.ServerError as e:
            if "cancelled" in str(e):
                feedback.pushInfo("Cancelled; the server stopped the job too.")
                return {}
            raise QgsProcessingException(str(e)) from None

        fields = out_fields()
        feats = features_from_geojson(fc, fields)
        feedback.pushInfo(f"{len(feats)} features over {st.get('windows', '?')} windows")

        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields,
                                          QgsWkbTypes.Polygon, WGS84)
        for f in feats:
            sink.addFeature(f)

        out = {self.OUTPUT: dest}
        lines = lines_from_geojson(fc, line_fields())
        if lines:
            lsink, ldest = self.parameterAsSink(parameters, self.CENTRELINES, context,
                                                line_fields(), QgsWkbTypes.LineString, WGS84)
            if lsink is not None:
                for f in lines:
                    lsink.addFeature(f)
                out[self.CENTRELINES] = ldest
                feedback.pushInfo(f"{len(lines)} centrelines")
        return out

    def _aoi(self, parameters, context, feedback):
        """The area to process, as one EPSG:4326 GeoJSON geometry.

        A boundary layer wins over an extent: if the user supplied an actual
        shape, squaring it off to its bounding box would process ground they
        deliberately excluded.
        """
        src = self.parameterAsSource(parameters, self.BOUNDARY, context)
        if src is not None and src.featureCount() > 0:
            tr = QgsCoordinateTransform(src.sourceCrs(), WGS84, QgsProject.instance())
            polys = []
            for feat in src.getFeatures():
                g = feat.geometry()
                if g.isEmpty():
                    continue
                g.transform(tr)
                gj = g.asJson()
                import json as _json
                geom = _json.loads(gj)
                if geom["type"] == "Polygon":
                    polys.append(geom["coordinates"])
                elif geom["type"] == "MultiPolygon":
                    polys.extend(geom["coordinates"])
            if polys:
                geom = ({"type": "Polygon", "coordinates": polys[0]} if len(polys) == 1
                        else {"type": "MultiPolygon", "coordinates": polys})
                return {"type": "Feature", "properties": {}, "geometry": geom}
            feedback.pushWarning("The boundary layer had no usable polygons; "
                                 "falling back to the extent.")

        ext = self.parameterAsExtent(parameters, self.EXTENT, context, WGS84)
        if ext.isEmpty():
            return None
        ring = [[ext.xMinimum(), ext.yMinimum()], [ext.xMaximum(), ext.yMinimum()],
                [ext.xMaximum(), ext.yMaximum()], [ext.xMinimum(), ext.yMaximum()],
                [ext.xMinimum(), ext.yMinimum()]]
        return {"type": "Feature", "properties": {},
                "geometry": {"type": "Polygon", "coordinates": [ring]}}
