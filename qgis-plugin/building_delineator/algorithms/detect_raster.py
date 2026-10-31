"""Delineate buildings in a raster layer the user already has open."""
import os
import tempfile

from qgis.core import (QgsProcessing, QgsProcessingAlgorithm, QgsProcessingException,
                       QgsProcessingParameterExtent,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterNumber,
                       QgsProcessingParameterRasterLayer, QgsWkbTypes)

from .. import client
from .common import (SERVER, WGS84, check_server, features_from_geojson, out_fields,
                     server_param)

# The model reads buildings at a size in pixels, and 12.5 cm is where it scores
# best on Indian low-rise fabric. Sending native 3 cm would put a house at
# 264 px, four times outside the range the network represents - and upload a
# needlessly huge file. Anything finer than this is resampled down server-side
# anyway, so do it here and send less over the wire.
WORK_GSD = 0.125


class DetectInRasterAlgorithm(QgsProcessingAlgorithm):
    INPUT = "INPUT"
    EXTENT = "EXTENT"
    GSD = "GSD"
    OUTPUT = "OUTPUT"

    def name(self):
        return "detectinraster"

    def displayName(self):
        return "Building footprints from a raster layer"

    def group(self):
        return "Buildings"

    def groupId(self):
        return "buildings"

    def shortHelpString(self):
        return (
            "<p>Delineates <b>individual building footprints</b> in a raster layer and "
            "returns one polygon per building, already regularized to a rectilinear "
            "outline.</p>"
            "<p>This is not a semantic mask. In dense terraced housing a mask merges a "
            "whole block into one shape; this separates the houses.</p>"
            "<p>The raster must be <b>georeferenced</b> — the server reads the ground "
            "resolution from it. Leave the resolution on 0 to resample to the "
            "12.5 cm the model scores best at; that is almost always what you want.</p>"
            "<p>Inference runs on the model server, so QGIS needs no GPU. Point the "
            "<i>Model server URL</i> at your own server.</p>")

    def createInstance(self):
        return DetectInRasterAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(self.INPUT, "Imagery"))
        self.addParameter(QgsProcessingParameterExtent(
            self.EXTENT, "Area to process (defaults to the whole layer)", optional=True))
        self.addParameter(server_param())
        p = QgsProcessingParameterNumber(
            self.GSD, "Resample to this ground resolution, m/px (0 = 0.125, recommended)",
            QgsProcessingParameterNumber.Double, defaultValue=0.0, minValue=0.0, maxValue=5.0)
        p.setFlags(p.flags() | p.FlagAdvanced)
        self.addParameter(p)
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, "Building footprints", type=QgsProcessing.TypeVectorPolygon))

    def processAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        if layer is None:
            raise QgsProcessingException("Choose a raster layer.")
        if not layer.crs().isValid():
            raise QgsProcessingException(
                "That layer has no CRS. The server needs georeferencing to place the "
                "footprints on the ground.")
        url = check_server(client, self.parameterAsString(parameters, SERVER, context),
                           feedback)

        gsd = self.parameterAsDouble(parameters, self.GSD, context) or WORK_GSD
        extent = self.parameterAsExtent(parameters, self.EXTENT, context, layer.crs())
        if extent.isEmpty():
            extent = layer.extent()

        tmp = os.path.join(tempfile.mkdtemp(prefix="urbanly_"), "clip.tif")
        feedback.pushInfo(f"Preparing a {gsd * 100:.1f} cm clip…")
        self._clip(layer, extent, gsd, tmp, feedback)
        if feedback.isCanceled():
            return {}

        size_mb = os.path.getsize(tmp) / 1048576
        feedback.pushInfo(f"Sending {size_mb:.1f} MB to {url}…")
        if size_mb > 200:
            raise QgsProcessingException(
                f"That clip is {size_mb:.0f} MB; the server accepts 200 MB. Process a "
                f"smaller extent, or use 'Building footprints over an area' which "
                f"streams from imagery already on the server.")
        feedback.setProgress(20)

        try:
            res = client.predict_file(tmp, server=url)
        except client.ServerError as e:
            raise QgsProcessingException(str(e)) from None
        feedback.setProgress(90)

        fc = res.get("geo")
        if not fc:
            raise QgsProcessingException(
                "The server could not georeference the result — check the layer has a "
                "projected CRS.")
        fields = out_fields()
        feats = features_from_geojson(fc, fields)
        feedback.pushInfo(f"{len(feats)} buildings in {res.get('seconds', '?')}s "
                          f"at {res['image']['gsd'] * 100:.1f} cm "
                          f"({res['image'].get('gsd_source', '?')})")

        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields,
                                          QgsWkbTypes.Polygon, WGS84)
        for f in feats:
            sink.addFeature(f)
        feedback.setProgress(100)
        return {self.OUTPUT: dest}

    @staticmethod
    def _clip(layer, extent, gsd, out_path, feedback):
        """Write the requested extent as a GeoTIFF at the working resolution.

        gdal rather than a Processing call: this is one translate, and shelling
        out to gdal:translate would make the algorithm depend on another
        provider being present.
        """
        from osgeo import gdal
        src = gdal.Open(layer.source())
        if src is None:
            raise QgsProcessingException(
                f"GDAL cannot open {layer.source()!r}. A layer from a tile or WMS "
                f"service has no file to send — export it first.")
        opts = gdal.TranslateOptions(
            projWin=[extent.xMinimum(), extent.yMaximum(), extent.xMaximum(), extent.yMinimum()],
            xRes=gsd, yRes=gsd, resampleAlg="average",
            bandList=[1, 2, 3][:src.RasterCount] or None,
            creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"],
            callback=lambda pct, *_: not feedback.isCanceled() and feedback.setProgress(pct * 15))
        gdal.Translate(out_path, src, options=opts)
        src = None
        if not os.path.exists(out_path):
            raise QgsProcessingException("Could not clip that extent from the layer.")
