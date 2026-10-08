/*
 * Where drone / satellite imagery goes in the layer stack: directly on top of the
 * Mapbox basemap imagery, so EVERYTHING else (district AI layers, boundary, detections,
 * grid, basemap roads & labels) draws above it.
 *
 *   satellite basemaps: right after the basemap's last raster layer (the satellite photo)
 *   other basemaps:     before the first label or app overlay
 */
export function imageryBeforeId(map) {
    const style = map.getStyle?.();
    const layers = style?.layers || [];
    const sources = style?.sources || {};
    const isBasemap = (l) => {
        if (!l.source) return true;                                  // background
        const s = sources[l.source];
        return !!s && typeof s.url === "string" && s.url.startsWith("mapbox://");
    };
    let lastBaseRaster = -1;
    layers.forEach((l, i) => { if (l.type === "raster" && isBasemap(l)) lastBaseRaster = i; });
    if (lastBaseRaster >= 0) return layers[lastBaseRaster + 1]?.id;
    return layers.find((l) => l.type === "symbol" || !isBasemap(l))?.id;
}
