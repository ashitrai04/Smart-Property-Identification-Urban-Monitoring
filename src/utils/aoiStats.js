/**
 * AOI statistics — counts buildings / waterbodies and road length that fall
 * INSIDE the drawn/uploaded Area of Interest, per individual polygon + totals.
 *
 * Speed/accuracy notes:
 *  - The backend has no AOI-aware endpoint (its /stats route is whole-district and
 *    is shadowed by /{layer}), so we fetch the vector layers for the AOI bbox and
 *    do the spatial test in the browser.
 *  - Results are cached per (district, layer, bbox), requests are abortable (so a
 *    redraw cancels stale work), and each polygon bbox-prefilters candidates before
 *    the (more expensive) point-in-polygon test.
 */
import { API_BASE } from './mapLayers';
import {
    getAOIBounds,
    isPointInAOI,
    lineLengthKmInFeature,
    computeAreaKm2,
} from './aoiUtils';

// OSM `fclass` values that represent water / roads. Used to reclassify features
// because the backend dumps water + road polygons into the "buildings" layer.
const WATER_CLASSES = new Set([
    'water', 'riverbank', 'reservoir', 'wetland', 'pond', 'lake', 'basin',
    'dock', 'canal', 'stream', 'river', 'glacier', 'wastewater', 'lagoon',
]);
const ROAD_CLASSES = new Set([
    'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'unclassified',
    'residential', 'service', 'road', 'living_street', 'track', 'path',
    'footway', 'cycleway', 'pedestrian', 'steps', 'bridleway',
    'motorway_link', 'trunk_link', 'primary_link', 'secondary_link', 'tertiary_link',
]);

// ── Module-level cache of fetched layer GeoJSON (keyed by district|layer|bbox) ──
const layerCache = new Map();
const MAX_CACHE = 24;
function cacheGet(key) {
    if (!layerCache.has(key)) return null;
    const v = layerCache.get(key);
    layerCache.delete(key); layerCache.set(key, v); // LRU bump
    return v;
}
function cacheSet(key, v) {
    layerCache.set(key, v);
    if (layerCache.size > MAX_CACHE) layerCache.delete(layerCache.keys().next().value);
}

// Wake the HF Space early so the first analysis isn't blocked by a cold start.
export function warmBackend() {
    try { fetch(`${API_BASE}/api/health`, { cache: 'no-store' }).catch(() => {}); } catch (_) {}
}

async function fetchLayer(districtKey, layer, bbox, signal, zoom = 18) {
    const key = `${districtKey}|${layer}|${bbox}`;
    const cached = cacheGet(key);
    if (cached) return cached;
    try {
        const url = `${API_BASE}/api/districts/${encodeURIComponent(districtKey)}/${encodeURIComponent(layer)}?bbox=${bbox}&zoom=${zoom}&limit=1000000`;
        const resp = await fetch(url, { signal });
        if (!resp.ok) return { type: 'FeatureCollection', features: [] };
        const json = await resp.json();
        const out = json && Array.isArray(json.features) ? json : { type: 'FeatureCollection', features: [] };
        cacheSet(key, out);
        return out;
    } catch (e) {
        if (e?.name === 'AbortError') throw e;
        return { type: 'FeatureCollection', features: [] };
    }
}

// Representative point of any geometry (for point-in-polygon membership tests)
function geomCentroid(geom) {
    if (!geom) return null;
    const t = geom.type;
    if (t === 'Point') return geom.coordinates;
    if (t === 'MultiPoint') return geom.coordinates[0];
    if (t === 'LineString') return geom.coordinates[Math.floor(geom.coordinates.length / 2)];
    if (t === 'MultiLineString') return geom.coordinates[0]?.[Math.floor((geom.coordinates[0]?.length || 1) / 2)];
    const ring = t === 'Polygon' ? geom.coordinates[0]
        : t === 'MultiPolygon' ? geom.coordinates[0]?.[0]
            : null;
    if (!ring) return null;
    let x = 0, y = 0, n = 0;
    for (let i = 0; i < ring.length - 1; i++) { x += ring[i][0]; y += ring[i][1]; n++; }
    return n ? [x / n, y / n] : ring[0];
}

// Flatten a geometry into an array of LineString coordinate arrays
function geomToLines(geom) {
    if (!geom) return [];
    if (geom.type === 'LineString') return [geom.coordinates];
    if (geom.type === 'MultiLineString') return geom.coordinates;
    if (geom.type === 'Polygon') return geom.coordinates;
    if (geom.type === 'MultiPolygon') return geom.coordinates.flat();
    return [];
}

// Bounding box [minLng, minLat, maxLng, maxLat] of a line coordinate array
function lineBounds(coords) {
    let a = Infinity, b = Infinity, c = -Infinity, d = -Infinity;
    for (const [x, y] of coords) {
        if (x < a) a = x; if (y < b) b = y; if (x > c) c = x; if (y > d) d = y;
    }
    return [a, b, c, d];
}
const bboxOverlap = (p, q) => !(p[2] < q[0] || p[0] > q[2] || p[3] < q[1] || p[1] > q[3]);
const ptInBbox = (pt, bb) => pt[0] >= bb[0] && pt[0] <= bb[2] && pt[1] >= bb[1] && pt[1] <= bb[3];

/**
 * @param {string} districtKey   e.g. "vijayawada"
 * @param {Array}  features      array of single-Polygon AOI features
 * @param {AbortSignal} [signal] cancels stale work when the AOI changes
 * @returns {Promise<{perPolygon: Array, totals: object, fetched: object}>}
 */
export async function computeAOIStats(districtKey, features, signal) {
    if (!districtKey || !features?.length) {
        return { perPolygon: [], totals: { areaKm2: 0, buildings: 0, waterbodies: 0, roadKm: 0 }, fetched: {} };
    }

    // Union bbox of all AOI polygons (single fetch covers them all)
    let minLng = Infinity, minLat = Infinity, maxLng = -Infinity, maxLat = -Infinity;
    const featBboxes = features.map(f => {
        const b = getAOIBounds(f);
        if (b.minLng < minLng) minLng = b.minLng;
        if (b.minLat < minLat) minLat = b.minLat;
        if (b.maxLng > maxLng) maxLng = b.maxLng;
        if (b.maxLat > maxLat) maxLat = b.maxLat;
        return [b.minLng, b.minLat, b.maxLng, b.maxLat];
    });
    const bbox = `${minLng},${minLat},${maxLng},${maxLat}`;

    const [buildings, roads, waters] = await Promise.all([
        fetchLayer(districtKey, 'buildings', bbox, signal),
        fetchLayer(districtKey, 'roads', bbox, signal),
        fetchLayer(districtKey, 'waterbodies', bbox, signal),
    ]);
    if (signal?.aborted) throw new DOMException('aborted', 'AbortError');

    // ── Reclassify by `fclass` ──
    // The backend mis-files water/road polygons inside the "buildings" layer
    // (class_id 1) and its dedicated waterbodies/openareas endpoints are empty.
    // So we read the real type from each feature's `fclass` instead of trusting
    // which endpoint it came from. This makes water bodies show up correctly and
    // stops them inflating the building count.
    const fcOf = (f) => String(f?.properties?.fclass || '').toLowerCase();
    const isWater = (c) => WATER_CLASSES.has(c);
    const isRoad = (c) => ROAD_CLASSES.has(c);

    // Merge everything we got, de-duplicated by osm_id where present
    const merged = [...(buildings.features || []), ...(waters.features || [])];
    const seen = new Set();
    const allFeatures = [];
    for (const f of merged) {
        const id = f?.properties?.osm_id;
        if (id != null) { if (seen.has(id)) continue; seen.add(id); }
        allFeatures.push(f);
    }

    const buildingPts = allFeatures
        .filter(f => { const c = fcOf(f); return !isWater(c) && !isRoad(c); })
        .map(f => geomCentroid(f.geometry)).filter(Boolean);
    const waterPts = allFeatures
        .filter(f => isWater(fcOf(f)))
        .map(f => geomCentroid(f.geometry)).filter(Boolean);
    // Roads come from the roads endpoint plus any road-tagged feature in the catch-all
    const roadFeatures = [...(roads.features || []), ...allFeatures.filter(f => isRoad(fcOf(f)))];
    const roadLines = roadFeatures.flatMap(f => geomToLines(f.geometry)).map(c => ({ c, bb: lineBounds(c) }));

    const perPolygon = features.map((f, idx) => {
        const fb = featBboxes[idx];
        let buildingCount = 0, waterCount = 0, roadKm = 0;
        // bbox-prefilter points before the ray-cast (big speedup on dense areas)
        for (const p of buildingPts) if (ptInBbox(p, fb) && isPointInAOI(p[0], p[1], f)) buildingCount++;
        for (const p of waterPts) if (ptInBbox(p, fb) && isPointInAOI(p[0], p[1], f)) waterCount++;
        for (const { c, bb } of roadLines) if (bboxOverlap(bb, fb)) roadKm += lineLengthKmInFeature(c, f);
        return {
            index: idx,
            areaKm2: computeAreaKm2(f),
            buildings: buildingCount,
            waterbodies: waterCount,
            roadKm: Math.round(roadKm * 100) / 100,
        };
    });

    const totals = {
        areaKm2: Math.round(perPolygon.reduce((s, p) => s + p.areaKm2, 0) * 100) / 100,
        buildings: perPolygon.reduce((s, p) => s + p.buildings, 0),
        waterbodies: perPolygon.reduce((s, p) => s + p.waterbodies, 0),
        roadKm: Math.round(perPolygon.reduce((s, p) => s + p.roadKm, 0) * 100) / 100,
    };

    return {
        perPolygon,
        totals,
        fetched: {
            buildings: buildingPts.length,
            roads: roadLines.length,
            waterbodies: waterPts.length,
        },
    };
}
