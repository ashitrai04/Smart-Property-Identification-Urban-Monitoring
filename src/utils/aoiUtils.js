/**
 * AOI (Area of Interest) Utilities
 * Parsing GeoJSON, Shapefile (.shp/.zip), KML, GeoPackage (.gpkg) files
 * Computing area, bounds, and point-in-polygon checks
 */
import { open as shpOpen } from 'shapefile';
import { unzipSync } from 'fflate';
import proj4 from 'proj4';

// Convert a Uint8Array view into a standalone ArrayBuffer (what `shapefile` expects)
const toArrayBuffer = (u8) => u8.buffer.slice(u8.byteOffset, u8.byteOffset + u8.byteLength);

// ════════════════════════════════════════════════════════════════════
// Coordinate normalization — uploads are often in a PROJECTED CRS
// (UTM / Web-Mercator), not WGS84 lng/lat. Mapbox needs EPSG:4326, so we
// detect the source CRS and reproject every coordinate to lng/lat.
// ════════════════════════════════════════════════════════════════════
const UTM_WGS84 = (zone, south) => `+proj=utm +zone=${zone} ${south ? '+south ' : ''}+datum=WGS84 +units=m +no_defs`;

// Map a numeric EPSG code to a proj4 definition (handles all UTM/WGS84 zones)
function epsgToProjDef(code) {
    code = Number(code);
    if (!code) return null;
    if (code === 4326 || code === 4979 || code === 4267 || code === 4269) return 'EPSG:4326';
    if (code === 3857 || code === 900913 || code === 102100 || code === 102113 || code === 3785) return 'EPSG:3857';
    if (code >= 32601 && code <= 32660) return UTM_WGS84(code - 32600, false); // WGS84 / UTM north
    if (code >= 32701 && code <= 32760) return UTM_WGS84(code - 32700, true);  // WGS84 / UTM south
    return null;
}

// Pull an EPSG code out of a GeoJSON crs name like "urn:ogc:def:crs:EPSG::32644"
function parseCrsName(name) {
    if (!name || typeof name !== 'string') return null;
    if (/CRS84|EPSG[:]{1,2}\s*4326/i.test(name)) return 'EPSG:4326';
    const m = name.match(/EPSG[:]{1,2}\s*(\d{3,6})/i) || name.match(/(\d{4,6})\s*$/);
    return m ? epsgToProjDef(m[1]) : null;
}

// First numeric [x, y] position found anywhere in a geometry's coordinate tree
function firstPosition(geometry) {
    let c = geometry?.coordinates;
    while (Array.isArray(c) && Array.isArray(c[0])) c = c[0];
    return (Array.isArray(c) && typeof c[0] === 'number') ? c : null;
}

const isLikelyWgs84 = ([x, y]) => Math.abs(x) <= 180 && Math.abs(y) <= 90;

// Recursively map every position in a coordinate tree through fn
function mapPositions(coords, fn) {
    if (typeof coords[0] === 'number') return fn(coords);
    return coords.map(c => mapPositions(c, fn));
}

// Reproject a Feature's geometry into WGS84 lng/lat, in place. Tries the most
// reliable hints first (.prj WKT, declared crs), then falls back to inference.
function normalizeToWgs84(feature, hint = {}) {
    if (!feature?.geometry?.coordinates) return feature;
    const sample = firstPosition(feature.geometry);
    if (!sample || isLikelyWgs84(sample)) return feature; // already lng/lat

    const [sx, sy] = sample;
    const candidates = [];
    if (hint.prjWkt) candidates.push(hint.prjWkt);          // shapefile .prj (WKT) — most reliable
    const fromName = parseCrsName(hint.crsName);
    if (fromName) candidates.push(fromName);                // declared GeoJSON crs member
    // Inference from coordinate magnitude (no declared CRS):
    const MERC = 20037509;
    if (Math.abs(sx) >= 100000 && Math.abs(sx) <= 900000 && Math.abs(sy) <= 10000000) {
        // Looks like UTM easting/northing. Region default (Andhra Pradesh = zone 44N)
        // is tried FIRST — a wrong zone can still land in-range but in the wrong place.
        [44, 43, 45].forEach(z => candidates.push(UTM_WGS84(z, false)));
    }
    if (Math.abs(sx) <= MERC && Math.abs(sy) <= MERC && Math.abs(sx) > 900000) {
        candidates.push('EPSG:3857'); // Web Mercator (x magnitude ≫ UTM easting)
    }
    candidates.push(UTM_WGS84(44, false)); // final region default fallback

    for (const def of candidates) {
        if (!def || def === 'EPSG:4326') continue;
        try {
            const [lng, lat] = proj4(def, 'EPSG:4326', [sx, sy]);
            if (Number.isFinite(lng) && Number.isFinite(lat) && Math.abs(lng) <= 180 && Math.abs(lat) <= 90) {
                feature.geometry.coordinates = mapPositions(feature.geometry.coordinates, (p) => {
                    const o = proj4(def, 'EPSG:4326', [p[0], p[1]]);
                    return [o[0], o[1]];
                });
                return feature;
            }
        } catch (_) { /* try next candidate */ }
    }
    throw new Error('Could not interpret the coordinate system. Please reproject the file to WGS84 (EPSG:4326).');
}

// ─── Parse a .geojson / .json file → array of polygon features ───
export async function parseGeoJSONFile(file) {
    const text = await file.text();
    const geojson = JSON.parse(text);
    const crsName = geojson?.crs?.properties?.name;
    return extractPolygons(geojson).map(f => normalizeToWgs84(f, { crsName }));
}

// ─── Read all features from a shapefile source (shp [+ dbf]) ───
async function readShapefileSource(shpBuffer, dbfBuffer) {
    const source = await shpOpen(shpBuffer, dbfBuffer || undefined);
    const features = [];
    while (true) {
        const result = await source.read();
        if (result.done) break;
        features.push(result.value);
    }
    return { type: "FeatureCollection", features };
}

// ─── Parse a .shp (optionally a sibling .dbf is not available) or a .zip bundle ───
export async function parseShapefileZip(file) {
    const arrayBuffer = await file.arrayBuffer();
    const fileName = file.name.toLowerCase();

    if (fileName.endsWith('.shp')) {
        // Direct .shp file (geometry only — no attributes / no .prj for CRS)
        const fc = await readShapefileSource(arrayBuffer);
        return extractPolygons(fc).map(f => normalizeToWgs84(f));
    }

    if (fileName.endsWith('.zip')) {
        // Extract .shp, .dbf and .prj from ZIP (handles BOTH stored & deflated entries)
        const { shpBuffer, dbfBuffer, prjText } = extractShpFromZip(arrayBuffer);
        if (!shpBuffer) throw new Error("No .shp file found inside the ZIP archive");
        const fc = await readShapefileSource(shpBuffer, dbfBuffer);
        return extractPolygons(fc).map(f => normalizeToWgs84(f, { prjWkt: prjText }));
    }

    throw new Error("Unsupported shapefile format. Please upload .shp or .zip");
}

// ─── Extract .shp, .dbf and .prj from a ZIP ArrayBuffer (fflate handles compression) ───
function extractShpFromZip(zipBuffer) {
    const files = unzipSync(new Uint8Array(zipBuffer));
    let shpBuffer = null, dbfBuffer = null, prjText = null;
    for (const name of Object.keys(files)) {
        const lower = name.toLowerCase();
        if (lower.endsWith('.shp')) shpBuffer = toArrayBuffer(files[name]);
        else if (lower.endsWith('.dbf')) dbfBuffer = toArrayBuffer(files[name]);
        else if (lower.endsWith('.prj')) prjText = new TextDecoder().decode(files[name]);
    }
    return { shpBuffer, dbfBuffer, prjText };
}

// ─── Parse a GeoPackage (.gpkg) — SQLite-backed vector container ───
// Uses @ngageoint/geopackage (dynamic import so the heavy lib + wasm only load
// when a user actually uploads a GeoPackage). The sql.js wasm is served from /public.
export async function parseGeoPackage(file) {
    let mod;
    try {
        mod = await import('@ngageoint/geopackage');
    } catch (e) {
        throw new Error("GeoPackage support failed to load. Try reinstalling dependencies.");
    }
    const { GeoPackageAPI, setSqljsWasmLocateFile } = mod;
    if (typeof setSqljsWasmLocateFile === 'function') {
        setSqljsWasmLocateFile((f) => `/${f}`); // resolves /sql-wasm.wasm from public/
    }

    const bytes = new Uint8Array(await file.arrayBuffer());
    let gp;
    try {
        gp = await GeoPackageAPI.open(bytes);
    } catch (e) {
        throw new Error(`Could not open GeoPackage: ${e.message || e}`);
    }

    try {
        const tables = gp.getFeatureTables() || [];
        if (!tables.length) throw new Error("GeoPackage contains no feature tables");

        for (const table of tables) {
            const features = [];
            try {
                for (const feat of gp.iterateGeoJSONFeatures(table)) {
                    features.push(feat);
                    if (features.length > 20000) break; // safety cap
                }
            } catch (_) { continue; }
            const fc = { type: "FeatureCollection", features };
            try {
                const polys = extractPolygons(fc); // first table that yields polygons wins
                return polys.map(f => normalizeToWgs84(f));
            } catch (_) { /* no polygon here — try next table */ }
        }
        throw new Error("No polygon geometry found in the GeoPackage");
    } finally {
        try { gp.close(); } catch (_) { }
    }
}

// ─── Parse a KML file ───
export async function parseKML(file) {
    const text = await file.text();
    const parser = new DOMParser();
    const doc = parser.parseFromString(text, "application/xml");

    const features = [];
    // Extract Placemarks with Polygon geometry
    const placemarks = doc.getElementsByTagName("Placemark");
    for (let i = 0; i < placemarks.length; i++) {
        const pm = placemarks[i];
        const polygons = pm.getElementsByTagName("Polygon");
        for (let j = 0; j < polygons.length; j++) {
            const coordsText = polygons[j].getElementsByTagName("coordinates")[0]?.textContent;
            if (!coordsText) continue;
            const coords = coordsText.trim().split(/\s+/).map(c => {
                const [lng, lat] = c.split(",").map(Number);
                return [lng, lat];
            }).filter(c => !isNaN(c[0]) && !isNaN(c[1]));

            if (coords.length >= 3) {
                // Ensure ring is closed
                if (coords[0][0] !== coords[coords.length - 1][0] ||
                    coords[0][1] !== coords[coords.length - 1][1]) {
                    coords.push([...coords[0]]);
                }
                features.push({
                    type: "Feature",
                    properties: {},
                    geometry: { type: "Polygon", coordinates: [coords] }
                });
            }
        }
    }

    if (features.length === 0) throw new Error("No polygon found in KML file");
    return features.map(f => normalizeToWgs84(f)); // KML is WGS84 by spec, normalize defensively
}

// ─── Extract ALL Polygon features from GeoJSON ───
// MultiPolygons are split into one Feature per polygon so each can be shown and
// analysed individually. Returns an array of single-Polygon Features.
function extractPolygons(geojson) {
    if (!geojson) throw new Error("Invalid GeoJSON");
    const out = [];

    const pushGeometry = (geom, props) => {
        if (!geom) return;
        if (geom.type === "Polygon") {
            out.push({ type: "Feature", properties: props || {}, geometry: { type: "Polygon", coordinates: geom.coordinates } });
        } else if (geom.type === "MultiPolygon") {
            geom.coordinates.forEach((polyCoords, i) => {
                out.push({ type: "Feature", properties: { ...(props || {}), _part: i }, geometry: { type: "Polygon", coordinates: polyCoords } });
            });
        }
    };

    if (geojson.type === "Feature") {
        pushGeometry(geojson.geometry, geojson.properties);
    } else if (geojson.type === "FeatureCollection" && Array.isArray(geojson.features)) {
        geojson.features.forEach(f => pushGeometry(f.geometry, f.properties));
    } else if (geojson.type === "Polygon" || geojson.type === "MultiPolygon") {
        pushGeometry(geojson, {});
    }

    if (out.length === 0) throw new Error("No polygon geometry found in uploaded file");
    return out;
}

// ─── Compute bounding box of an AOI polygon ───
export function getAOIBounds(aoiFeature) {
    const coords = aoiFeature.geometry.coordinates[0]; // outer ring
    let minLng = Infinity, maxLng = -Infinity, minLat = Infinity, maxLat = -Infinity;
    for (const [lng, lat] of coords) {
        if (lng < minLng) minLng = lng;
        if (lng > maxLng) maxLng = lng;
        if (lat < minLat) minLat = lat;
        if (lat > maxLat) maxLat = lat;
    }
    return { minLng, maxLng, minLat, maxLat };
}

// ─── Compute area in km² using the Shoelace formula on WGS84 ───
export function computeAreaKm2(aoiFeature) {
    const coords = aoiFeature.geometry.coordinates[0];
    const n = coords.length;
    if (n < 3) return 0;

    // Approximate using the spherical excess formula
    const toRad = d => d * Math.PI / 180;
    const R = 6371; // Earth radius in km

    let area = 0;
    for (let i = 0; i < n; i++) {
        const j = (i + 1) % n;
        const k = (i + 2) % n;
        area += toRad(coords[j][0] - coords[i][0]) *
            (2 + Math.sin(toRad(coords[i][1])) + Math.sin(toRad(coords[j][1])));
    }
    area = Math.abs(area) * R * R / 2;

    // Simpler: use the Surveyor's formula with cos(lat) correction
    let A = 0;
    const midLat = coords.reduce((s, c) => s + c[1], 0) / n;
    const cosLat = Math.cos(toRad(midLat));
    for (let i = 0; i < n - 1; i++) {
        const x1 = coords[i][0] * cosLat, y1 = coords[i][1];
        const x2 = coords[i + 1][0] * cosLat, y2 = coords[i + 1][1];
        A += x1 * y2 - x2 * y1;
    }
    A = Math.abs(A) / 2;
    // Convert degrees² to km²: 1 degree ≈ 111.32 km
    const km2 = A * 111.32 * 111.32;
    return Math.round(km2 * 100) / 100;
}

// ─── Check if a point is inside the AOI polygon (ray-casting) ───
export function isPointInAOI(lng, lat, aoiFeature) {
    const ring = aoiFeature.geometry.coordinates[0];
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
        const xi = ring[i][0], yi = ring[i][1];
        const xj = ring[j][0], yj = ring[j][1];
        const intersect = ((yi > lat) !== (yj > lat)) &&
            (lng < (xj - xi) * (lat - yi) / (yj - yi) + xi);
        if (intersect) inside = !inside;
    }
    return inside;
}

// ─── Bounding box across MANY polygon features ───
export function getFeaturesBounds(features) {
    let minLng = Infinity, maxLng = -Infinity, minLat = Infinity, maxLat = -Infinity;
    for (const f of features) {
        const b = getAOIBounds(f);
        if (b.minLng < minLng) minLng = b.minLng;
        if (b.maxLng > maxLng) maxLng = b.maxLng;
        if (b.minLat < minLat) minLat = b.minLat;
        if (b.maxLat > maxLat) maxLat = b.maxLat;
    }
    return { minLng, maxLng, minLat, maxLat };
}

// ─── Total area (km²) across many polygon features ───
export function computeTotalAreaKm2(features) {
    return Math.round(features.reduce((s, f) => s + computeAreaKm2(f), 0) * 100) / 100;
}

// ─── A MultiPolygon geometry combining all AOI polygons (for Mapbox `within`) ───
export function unionGeometry(features) {
    return { type: 'MultiPolygon', coordinates: features.map(f => f.geometry.coordinates) };
}

// ─── Great-circle distance between two [lng,lat] points, in km ───
export function haversineKm(a, b) {
    const toRad = d => d * Math.PI / 180;
    const R = 6371;
    const dLat = toRad(b[1] - a[1]);
    const dLng = toRad(b[0] - a[0]);
    const lat1 = toRad(a[1]), lat2 = toRad(b[1]);
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
}

// ─── Centroid (average of outer-ring vertices) of a polygon feature ───
export function polygonCentroid(feature) {
    const ring = feature.geometry.coordinates[0];
    let x = 0, y = 0, n = 0;
    for (let i = 0; i < ring.length - 1; i++) { x += ring[i][0]; y += ring[i][1]; n++; }
    return n ? [x / n, y / n] : ring[0];
}

// ─── Length (km) of a LineString that falls INSIDE a polygon feature ───
// Sums each segment whose midpoint is inside the polygon — a fast clip approximation.
export function lineLengthKmInFeature(lineCoords, aoiFeature) {
    let km = 0;
    for (let i = 0; i < lineCoords.length - 1; i++) {
        const a = lineCoords[i], b = lineCoords[i + 1];
        const mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
        if (isPointInAOI(mid[0], mid[1], aoiFeature)) km += haversineKm(a, b);
    }
    return km;
}

// ─── Auto-detect file type and parse → ALWAYS returns an array of polygon features ───
export async function parseAOIFile(file) {
    const name = file.name.toLowerCase();
    if (name.endsWith('.geojson') || name.endsWith('.json')) {
        return parseGeoJSONFile(file);
    }
    if (name.endsWith('.shp')) {
        return parseShapefileZip(file);
    }
    if (name.endsWith('.zip')) {
        return parseShapefileZip(file);
    }
    if (name.endsWith('.kml')) {
        return parseKML(file);
    }
    if (name.endsWith('.gpkg')) {
        return parseGeoPackage(file);
    }
    throw new Error(`Unsupported file format: ${name}. Supported: .geojson, .json, .shp, .zip, .kml, .gpkg`);
}
