// Utilities to add ArcGIS sources/layers to Mapbox GL
import mapboxgl from 'mapbox-gl';

// ── In-memory cache for ArcGIS GeoJSON responses ──
const _arcgisGeojsonCache = new Map();
const MAX_CACHE = 30;

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

// ── ArcGIS POST query helper ──
async function _postQuery(url, params) {
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

// ── Fetch a single page of features ──
async function _fetchPage(url, params) {
    let resp = await _postQuery(url, params);
    if (!resp.ok && resp.status >= 500) {
        const { outSR, ...noSr } = params;
        resp = await _postQuery(url, noSr);
    }
    let json = resp.ok ? await resp.json() : null;
    if (!json || json.type !== 'FeatureCollection') {
        const qs = new URLSearchParams(params).toString();
        const getResp = await fetch(`${url}/query?${qs}`);
        if (!getResp.ok) throw new Error(`ArcGIS query failed: ${getResp.status}`);
        json = await getResp.json();
        if (!json || json.type !== 'FeatureCollection') {
            throw new Error('ArcGIS did not return valid GeoJSON FeatureCollection');
        }
    }
    return json;
}

// ── Get feature count for a layer ──
export async function getFeatureCount(featureServerUrl) {
    const url = typeof featureServerUrl === 'object' ? featureServerUrl.url : featureServerUrl;
    try {
        const resp = await fetch(`${url}/query?where=1%3D1&returnCountOnly=true&f=json`);
        if (!resp.ok) return 0;
        const data = await resp.json();
        return data.count || 0;
    } catch { return 0; }
}

// Raster tile layer
export function addArcGISTileLayer(map, { id, tiles, tileSize = 256, attribution = "" }) {
    if (!map.getSource(id)) {
        map.addSource(id, { type: "raster", tiles, tileSize, attribution });
    }
    if (!map.getLayer(id)) {
        map.addLayer({ id, type: "raster", source: id });
    }
}

// ── Helper: add visual map layers for a GeoJSON source ──
function _addLayersForGeojson(map, id, geojson, paintOverrides = {}, labelField = null) {
    const types = new Set((geojson.features || []).map(f => f.geometry && f.geometry.type));

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
    if ((types.has("LineString") || types.has("MultiLineString")) && !map.getLayer(`${id}-line`)) {
        map.addLayer({
            id: `${id}-line`, type: "line", source: id,
            filter: ["match", ["geometry-type"], ["LineString", "MultiLineString"], true, false],
            paint: { "line-color": "#0B1E3E", "line-width": 2, ...(paintOverrides.line || {}) },
        });
    }
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
}

// ══════════════════════════════════════════════════════════════════
// Load ArcGIS FeatureServer as GeoJSON — with PARALLEL pagination
// for large datasets (buildings etc.).
// onProgress(loaded, total) is called during batch fetching.
// ══════════════════════════════════════════════════════════════════
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
        onProgress = null, // (loadedCount, totalCount) => void
    }
) {
    const url = typeof featureServerUrl === 'object' ? featureServerUrl.url : featureServerUrl;
    const PAGE_SIZE = 2000;
    const CONCURRENCY = 6; // parallel requests

    const baseParams = {
        where,
        outFields: outFields || (labelField ? labelField : "*"),
        outSR: 4326,
        returnGeometry: true,
        geometryPrecision: 5,
        f: "geojson",
        resultRecordCount: PAGE_SIZE,
    };

    const cacheKey = `${url}|${where}`;
    let geojson = _cacheGet(cacheKey);

    if (!geojson) {
        // 1) Get total count first
        let totalCount = 0;
        try {
            const countResp = await fetch(`${url}/query?where=${encodeURIComponent(where)}&returnCountOnly=true&f=json`);
            if (countResp.ok) {
                const countData = await countResp.json();
                totalCount = countData.count || 0;
            }
        } catch { /* fallback to sequential */ }

        if (onProgress) onProgress(0, totalCount);

        if (totalCount > 0 && totalCount > PAGE_SIZE) {
            // ── Parallel batch pagination ──
            const totalPages = Math.ceil(totalCount / PAGE_SIZE);
            let allFeatures = [];
            let layersAdded = false;

            // Process in batches of CONCURRENCY
            for (let batch = 0; batch < totalPages; batch += CONCURRENCY) {
                const promises = [];
                for (let i = 0; i < CONCURRENCY && (batch + i) < totalPages; i++) {
                    const offset = (batch + i) * PAGE_SIZE;
                    const pageParams = { ...baseParams, resultOffset: offset };
                    promises.push(
                        _fetchPage(url, pageParams)
                            .then(json => Array.isArray(json.features) ? json.features : [])
                            .catch(() => [])
                    );
                }
                const results = await Promise.all(promises);
                for (const feats of results) {
                    allFeatures.push(...feats);
                }
                if (onProgress) onProgress(allFeatures.length, totalCount);

                // Progressive render: update map source every batch
                const partialGeojson = { type: 'FeatureCollection', features: allFeatures };
                if (!map.getSource(id)) {
                    map.addSource(id, { type: "geojson", data: partialGeojson });
                } else {
                    map.getSource(id).setData(partialGeojson);
                }

                // Add visual layers on FIRST batch so features render immediately
                if (!layersAdded) {
                    _addLayersForGeojson(map, id, partialGeojson, paintOverrides, labelField);
                    layersAdded = true;
                }
            }
            geojson = { type: 'FeatureCollection', features: allFeatures };
        } else {
            // ── Small dataset or count failed — sequential fallback ──
            let pageParams = { ...baseParams, resultOffset: 0 };
            let first = await _fetchPage(url, pageParams);
            let features = Array.isArray(first.features) ? [...first.features] : [];
            let exceeded = !!first.exceededTransferLimit;
            while (exceeded) {
                pageParams = { ...baseParams, resultOffset: features.length };
                const next = await _fetchPage(url, pageParams);
                const feats = Array.isArray(next.features) ? next.features : [];
                if (feats.length === 0) break;
                features.push(...feats);
                exceeded = !!next.exceededTransferLimit;
                if (onProgress) onProgress(features.length, totalCount || features.length);
            }
            geojson = { type: 'FeatureCollection', features };
        }
        _cacheSet(cacheKey, geojson);
    }

    // Ensure source exists
    if (!map.getSource(id)) {
        map.addSource(id, { type: "geojson", data: geojson });
    } else {
        map.getSource(id).setData(geojson);
    }

    // Add visual layers (skip if already added by progressive path)
    _addLayersForGeojson(map, id, geojson, paintOverrides, labelField);

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
