// Utilities to add ArcGIS sources/layers to Mapbox GL
import mapboxgl from 'mapbox-gl';

// Simple in-memory cache for ArcGIS GeoJSON responses
const _arcgisGeojsonCache = new Map();
const MAX_CACHE = 24;

function _cacheGet(key) {
    if (_arcgisGeojsonCache.has(key)) {
        const val = _arcgisGeojsonCache.get(key);
        _arcgisGeojsonCache.delete(key);
        _arcgisGeojsonCache.set(key, val);
        return val;
    }
    return null;
}
function _cacheSet(key, val) {
    _arcgisGeojsonCache.set(key, val);
    if (_arcgisGeojsonCache.size > MAX_CACHE) {
        const firstKey = _arcgisGeojsonCache.keys().next().value;
        _arcgisGeojsonCache.delete(firstKey);
    }
}

// Raster tile layer (for ArcGIS MapServer/TileServer XYZ endpoints)
export function addArcGISTileLayer(map, { id, tiles, tileSize = 256, attribution = "" }) {
    if (!map.getSource(id)) {
        map.addSource(id, { type: "raster", tiles, tileSize, attribution });
    }
    if (!map.getLayer(id)) {
        map.addLayer({ id, type: "raster", source: id });
    }
}

// Load ArcGIS FeatureServer as GeoJSON via /query?f=geojson
export async function addArcGISFeatureLayer(
    map,
    {
        id,
        featureServerUrl,
        where = "1=1",
        outFields = "*",
        fit = true,
        labelField = null,
        paintOverrides = {},
    }
) {
    const baseParams = {
        where,
        outFields: outFields || (labelField ? labelField : "*"),
        outSR: 4326,
        returnGeometry: true,
        geometryPrecision: 5,
        f: "geojson",
        resultRecordCount: 2000,
    };

    async function postQuery(params) {
        const url = typeof featureServerUrl === 'object' ? featureServerUrl.url : featureServerUrl;
        const resp = await fetch(`${url}/query`, {
            method: "POST",
            headers: {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json"
            },
            body: new URLSearchParams(params).toString(),
        });
        return resp;
    }

    const cacheKey = `${featureServerUrl}|${JSON.stringify(baseParams)}`;
    let geojson = _cacheGet(cacheKey);
    if (!geojson) {
        async function fetchPage(paramsObj) {
            let resp = await postQuery(paramsObj);
            if (!resp.ok && resp.status >= 500) {
                const { outSR, ...noSr } = paramsObj;
                resp = await postQuery(noSr);
            }
            let json = resp.ok ? await resp.json() : null;
            if (!json || json.type !== 'FeatureCollection') {
                const params = new URLSearchParams(paramsObj).toString();
                const getUrl = `${featureServerUrl}/query?${params}`;
                const getResp = await fetch(getUrl);
                if (!getResp.ok) throw new Error(`ArcGIS query failed: ${getResp.status}`);
                json = await getResp.json();
                if (!json || json.type !== 'FeatureCollection') {
                    throw new Error('ArcGIS did not return valid GeoJSON FeatureCollection');
                }
            }
            return json;
        }

        let pageParams = { ...baseParams, resultOffset: 0 };
        let first = await fetchPage(pageParams);
        let features = Array.isArray(first.features) ? [...first.features] : [];
        let exceeded = !!first.exceededTransferLimit;
        while (exceeded) {
            pageParams = { ...baseParams, resultOffset: features.length };
            const next = await fetchPage(pageParams);
            const feats = Array.isArray(next.features) ? next.features : [];
            if (feats.length === 0) break;
            features.push(...feats);
            exceeded = !!next.exceededTransferLimit;
            if (features.length > 200000) break;
        }
        geojson = { type: 'FeatureCollection', features };
        _cacheSet(cacheKey, geojson);
    }

    if (!map.getSource(id)) {
        map.addSource(id, { type: "geojson", data: geojson });
    } else {
        map.getSource(id).setData(geojson);
    }

    const types = new Set((geojson.features || []).map(f => f.geometry && f.geometry.type));

    // Polygons
    if ((types.has("Polygon") || types.has("MultiPolygon")) && !map.getLayer(`${id}-fill`)) {
        map.addLayer({
            id: `${id}-fill`, type: "fill", source: id,
            filter: ["match", ["geometry-type"], ["Polygon", "MultiPolygon"], true, false],
            paint: { "fill-color": "#0EA5E9", "fill-opacity": 0.2, ...(paintOverrides.fill || {}) },
        });
    }
    if ((types.has("Polygon") || types.has("MultiPolygon")) && !map.getLayer(`${id}-outline`)) {
        map.addLayer({
            id: `${id}-outline`, type: "line", source: id,
            filter: ["match", ["geometry-type"], ["Polygon", "MultiPolygon"], true, false],
            paint: { "line-color": "#0EA5E9", "line-width": 1.5, ...(paintOverrides.outline || {}) },
        });
    }

    // Lines
    if ((types.has("LineString") || types.has("MultiLineString")) && !map.getLayer(`${id}-line`)) {
        map.addLayer({
            id: `${id}-line`, type: "line", source: id,
            filter: ["match", ["geometry-type"], ["LineString", "MultiLineString"], true, false],
            paint: { "line-color": "#0B1E3E", "line-width": 2, ...(paintOverrides.line || {}) },
        });
    }

    // Points
    if ((types.has("Point") || types.has("MultiPoint")) && !map.getLayer(`${id}-circle`)) {
        map.addLayer({
            id: `${id}-circle`, type: "circle", source: id,
            filter: ["match", ["geometry-type"], ["Point", "MultiPoint"], true, false],
            paint: {
                "circle-radius": 5, "circle-color": "#0B1E3E",
                "circle-stroke-color": "#ffffff", "circle-stroke-width": 1,
                ...(paintOverrides.circle || {}),
            },
        });
    }

    // Labels
    if (labelField && !map.getLayer(`${id}-label`)) {
        map.addLayer({
            id: `${id}-label`, type: "symbol", source: id,
            layout: { "text-field": ["get", labelField], "text-size": 11, "text-allow-overlap": false },
            paint: {
                "text-color": "#0B1E3E", "text-halo-color": "#ffffff", "text-halo-width": 1,
                ...(paintOverrides.label || {}),
            },
        });
    }

    // Fit bounds
    if (fit) {
        try {
            const bounds = new mapboxgl.LngLatBounds();
            (geojson.features || []).forEach(f => {
                const g = f.geometry;
                if (!g) return;
                if (g.type === "Point") bounds.extend(g.coordinates);
                else if (g.type === "MultiPoint") g.coordinates.forEach(c => bounds.extend(c));
                else if (g.type === "Polygon") g.coordinates.forEach(ring => ring.forEach(c => bounds.extend(c)));
                else if (g.type === "MultiPolygon") g.coordinates.forEach(poly => poly.forEach(ring => ring.forEach(c => bounds.extend(c))));
            });
            if (!bounds.isEmpty()) map.fitBounds(bounds, { padding: 40, duration: 800 });
        } catch (_) { }
    }

    return geojson;
}

// Remove a layer group
export function removeLayerGroup(map, baseId) {
    const suffixes = ['-fill', '-outline', '-line', '-circle', '-label'];
    suffixes.forEach(s => {
        if (map.getLayer(`${baseId}${s}`)) map.removeLayer(`${baseId}${s}`);
    });
    if (map.getSource(baseId)) map.removeSource(baseId);
}
