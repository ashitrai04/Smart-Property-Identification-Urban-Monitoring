import React, { useRef, useEffect, useState, useCallback, forwardRef, useImperativeHandle } from 'react';
import mapboxgl from 'mapbox-gl';
import { load as lercLoad, decode as lercDecode } from 'lerc';
import proj4 from 'proj4';

// Define UTM Zone 44N
proj4.defs('EPSG:32644', '+proj=utm +zone=44 +datum=WGS84 +units=m +no_defs');

const MAPBOX_TOKEN = import.meta.env.VITE_MAPBOX_TOKEN;
const ARCGIS_IMAGE_SERVER = 'https://tiledimageservices5.arcgis.com/73n8CSGpSSyHr1T9/arcgis/rest/services/visakhapatnam_mask/ImageServer';
const ARCGIS_FEATURE_SERVER = 'https://services5.arcgis.com/73n8CSGpSSyHr1T9/arcgis/rest/services/final_visakhapatnam/FeatureServer';

const MAP_STYLES = [
    { id: 'dark-v11', label: 'Dark', icon: '🌙' },
    { id: 'satellite-streets-v12', label: 'Satellite', icon: '🛰️' },
    { id: 'streets-v12', label: 'Streets', icon: '🛣️' },
    { id: 'light-v11', label: 'Light', icon: '☀️' },
    { id: 'outdoors-v12', label: 'Outdoors', icon: '🏔️' },
];

// 4 class colors: vegetation, buildings, water, roads/barren
const MASK_COLORS = {
    1: [34, 197, 94, 200],    // 🟢 Green — vegetation / open areas
    2: [245, 158, 11, 200],   // 🟠 Orange — buildings / built-up
    3: [59, 130, 246, 200],   // 🔵 Blue — water bodies
    4: [148, 163, 184, 200],  // ⚪ Gray — roads / barren land
};

const LAYER_COLORS = {
    'vizag-mask': { fill: 'rgba(139, 92, 246, 0.35)', outline: '#8b5cf6', dot: '#8b5cf6' },
    'guntur-boundary': { fill: 'rgba(59, 130, 246, 0.15)', outline: '#3b82f6', dot: '#3b82f6' },
    'guntur-buildings': { fill: 'rgba(245, 158, 11, 0.45)', outline: '#f59e0b', dot: '#f59e0b' },
    'guntur-openarea': { fill: 'rgba(34, 197, 94, 0.35)', outline: '#22c55e', dot: '#22c55e' },
    'guntur-roads': { fill: 'rgba(148, 163, 184, 0.45)', outline: '#94a3b8', dot: '#94a3b8' },
    'guntur-waterbodies': { fill: 'rgba(6, 182, 212, 0.45)', outline: '#06b6d4', dot: '#06b6d4' },
};

const GUNTUR_LAYERS = [
    { id: 'guntur-boundary', serverId: 0, label: 'Boundary', color: LAYER_COLORS['guntur-boundary'] },
    { id: 'guntur-buildings', serverId: 1, label: 'Buildings', color: LAYER_COLORS['guntur-buildings'] },
    { id: 'guntur-openarea', serverId: 2, label: 'Open Areas', color: LAYER_COLORS['guntur-openarea'] },
    { id: 'guntur-roads', serverId: 3, label: 'Roads', color: LAYER_COLORS['guntur-roads'] },
    { id: 'guntur-waterbodies', serverId: 4, label: 'Waterbodies', color: LAYER_COLORS['guntur-waterbodies'] },
];

const LOCATIONS = {
    visakhapatnam: { center: [83.25, 17.93], zoom: 12 },
    guntur: { center: [80.45, 16.30], zoom: 11 },
};

// ── ArcGIS tile scheme (WKID 32644) ──
const TILE_ORIGIN = { x: -5120763.26769827, y: 9997963.94301857 };
// Full extent
const EXTENT = {
    xmin: 712492.695837956, ymin: 1952042.85329722,
    xmax: 765051.695837956, ymax: 2016671.85329722,
};

function levelRes(lev) { return 256 / Math.pow(2, lev); }
function levelSpan(lev) { return 256 * levelRes(lev); }

// Map Mapbox zoom → best ArcGIS level
function zoomToLevel(z) {
    if (z <= 9) return 0;
    if (z <= 10) return 1;
    if (z <= 11) return 2;
    if (z <= 12) return 3;
    if (z <= 13) return 4;
    if (z <= 14) return 5;
    if (z <= 15) return 6;
    if (z <= 16) return 7;
    return 8;
}

// ── LERC WASM init ──
let lercReady = false;
const lercReadyP = lercLoad({ locateFile: (n) => `/${n}` }).then(() => {
    lercReady = true;
    console.log('LERC WASM ready');
});

// ── Decoded tile cache: "level/row/col" → pixel array | null ──
const decodedTileCache = new Map();

async function getDecodedTile(level, row, col) {
    const key = `${level}/${row}/${col}`;
    if (decodedTileCache.has(key)) return decodedTileCache.get(key);
    if (!lercReady) await lercReadyP;
    try {
        const resp = await fetch(`${ARCGIS_IMAGE_SERVER}/tile/${level}/${row}/${col}`);
        if (!resp.ok) { decodedTileCache.set(key, null); return null; }
        const buf = await resp.arrayBuffer();
        const decoded = lercDecode(buf);
        const pixels = decoded.pixels[0];
        decodedTileCache.set(key, pixels);
        return pixels;
    } catch {
        decodedTileCache.set(key, null);
        return null;
    }
}

// ── Build a colorized mask image for the visible viewport at the right resolution ──
// Returns { dataUrl, coordinates } for Mapbox image source
async function buildMaskForViewport(map, onProgress) {
    const bounds = map.getBounds();
    const zoom = Math.round(map.getZoom());
    const arcLevel = zoomToLevel(zoom);
    const res = levelRes(arcLevel);
    const span = levelSpan(arcLevel);

    // Convert viewport corners to UTM 44N
    const corners = {
        tl: proj4('EPSG:4326', 'EPSG:32644', [bounds.getWest(), bounds.getNorth()]),
        tr: proj4('EPSG:4326', 'EPSG:32644', [bounds.getEast(), bounds.getNorth()]),
        bl: proj4('EPSG:4326', 'EPSG:32644', [bounds.getWest(), bounds.getSouth()]),
        br: proj4('EPSG:4326', 'EPSG:32644', [bounds.getEast(), bounds.getSouth()]),
    };

    // Clamp to mask extent
    const utmXmin = Math.max(EXTENT.xmin, Math.min(corners.tl[0], corners.bl[0]));
    const utmXmax = Math.min(EXTENT.xmax, Math.max(corners.tr[0], corners.br[0]));
    const utmYmin = Math.max(EXTENT.ymin, Math.min(corners.bl[1], corners.br[1]));
    const utmYmax = Math.min(EXTENT.ymax, Math.max(corners.tl[1], corners.tr[1]));

    if (utmXmin >= utmXmax || utmYmin >= utmYmax) return null; // No overlap

    // Tile range
    const colMin = Math.floor((utmXmin - TILE_ORIGIN.x) / span);
    const colMax = Math.floor((utmXmax - TILE_ORIGIN.x) / span);
    const rowMin = Math.floor((TILE_ORIGIN.y - utmYmax) / span);
    const rowMax = Math.floor((TILE_ORIGIN.y - utmYmin) / span);
    const numCols = colMax - colMin + 1;
    const numRows = rowMax - rowMin + 1;
    const total = numCols * numRows;

    // Limit tile fetches for performance
    if (total > 200) {
        console.warn(`Too many tiles (${total}) at level ${arcLevel}, capping`);
        return null;
    }

    // Fetch all needed tiles in parallel
    let done = 0;
    const promises = [];
    for (let r = rowMin; r <= rowMax; r++) {
        for (let c = colMin; c <= colMax; c++) {
            promises.push(
                getDecodedTile(arcLevel, r, c).then(() => {
                    done++;
                    if (onProgress) onProgress(Math.round((done / total) * 100));
                })
            );
        }
    }
    await Promise.all(promises);

    // Build output canvas — each pixel covers `res` meters
    // The canvas represents the UTM rectangle from (colMin*span, rowMin*span) to ((colMax+1)*span, (rowMax+1)*span)
    const canvasW = numCols * 256;
    const canvasH = numRows * 256;

    // Cap canvas size for performance
    const maxDim = 2048;
    let outW = canvasW;
    let outH = canvasH;
    let scale = 1;
    if (outW > maxDim || outH > maxDim) {
        scale = maxDim / Math.max(outW, outH);
        outW = Math.round(outW * scale);
        outH = Math.round(outH * scale);
    }

    const canvas = document.createElement('canvas');
    canvas.width = outW;
    canvas.height = outH;
    const ctx = canvas.getContext('2d');
    const imgData = ctx.createImageData(outW, outH);
    const data = imgData.data;

    // The full UTM extent of the stitched area
    const stMinX = TILE_ORIGIN.x + colMin * span;
    const stMaxY = TILE_ORIGIN.y - rowMin * span;
    const stMaxX = TILE_ORIGIN.x + (colMax + 1) * span;
    const stMinY = TILE_ORIGIN.y - (rowMax + 1) * span;
    const stW = stMaxX - stMinX;
    const stH = stMaxY - stMinY;

    // For each output pixel, find the source LERC pixel
    for (let py = 0; py < outH; py++) {
        const utmY = stMaxY - (py / outH) * stH;
        for (let px = 0; px < outW; px++) {
            const utmX = stMinX + (px / outW) * stW;

            const col = Math.floor((utmX - TILE_ORIGIN.x) / span);
            const row = Math.floor((TILE_ORIGIN.y - utmY) / span);

            const tilePixels = decodedTileCache.get(`${arcLevel}/${row}/${col}`);
            if (!tilePixels) continue;

            const tileMinX = TILE_ORIGIN.x + col * span;
            const tileMaxY = TILE_ORIGIN.y - row * span;
            const srcX = Math.floor((utmX - tileMinX) / res);
            const srcY = Math.floor((tileMaxY - utmY) / res);
            if (srcX < 0 || srcX >= 256 || srcY < 0 || srcY >= 256) continue;

            const val = tilePixels[srcY * 256 + srcX];
            const color = MASK_COLORS[val];
            if (color) {
                const idx = (py * outW + px) * 4;
                data[idx] = color[0]; data[idx + 1] = color[1];
                data[idx + 2] = color[2]; data[idx + 3] = color[3];
            }
        }
    }

    ctx.putImageData(imgData, 0, 0);
    const dataUrl = canvas.toDataURL('image/png');

    // Project the stitch corners back to WGS84
    const tlWgs = proj4('EPSG:32644', 'EPSG:4326', [stMinX, stMaxY]);
    const trWgs = proj4('EPSG:32644', 'EPSG:4326', [stMaxX, stMaxY]);
    const brWgs = proj4('EPSG:32644', 'EPSG:4326', [stMaxX, stMinY]);
    const blWgs = proj4('EPSG:32644', 'EPSG:4326', [stMinX, stMinY]);

    return {
        dataUrl,
        coordinates: [
            [tlWgs[0], tlWgs[1]], // top-left
            [trWgs[0], trWgs[1]], // top-right
            [brWgs[0], brWgs[1]], // bottom-right
            [blWgs[0], blWgs[1]], // bottom-left
        ],
        level: arcLevel,
        zoom,
    };
}

// ── Guntur fetch tracking ──
const fetchedLayers = new Set();

// ================== COMPONENT ==================
const MapView = forwardRef(function MapView({ layers, mapStyle }, ref) {
    const mapContainerRef = useRef(null);
    const mapRef = useRef(null);
    const [loading, setLoading] = useState(null);
    const [coords, setCoords] = useState(null);
    const maskLoadedRef = useRef(false);
    const maskLevelRef = useRef(-1);
    const loadingMaskRef = useRef(false);
    const debounceRef = useRef(null);

    useImperativeHandle(ref, () => ({
        flyTo(loc) {
            mapRef.current?.flyTo({ center: loc.center, zoom: loc.zoom, duration: 2000, essential: true });
        },
    }));

    // Load / update mask for current viewport
    const updateMask = useCallback(async (map) => {
        if (loadingMaskRef.current) return;
        if (!layers['vizag-mask']) return;

        const zoom = Math.round(map.getZoom());
        const newLevel = zoomToLevel(zoom);

        // Skip if same level and already loaded
        if (newLevel === maskLevelRef.current && maskLoadedRef.current) return;

        loadingMaskRef.current = true;
        setLoading('Loading Mask...');

        try {
            const result = await buildMaskForViewport(map, (pct) => {
                setLoading(`Loading Mask... ${pct}%`);
            });

            if (!result || !map.getCanvas()) {
                loadingMaskRef.current = false;
                setLoading(null);
                return;
            }

            // Update or create the image source
            const source = map.getSource('vizag-mask-source');
            if (source) {
                source.updateImage({ url: result.dataUrl, coordinates: result.coordinates });
            } else {
                map.addSource('vizag-mask-source', {
                    type: 'image',
                    url: result.dataUrl,
                    coordinates: result.coordinates,
                });
                map.addLayer({
                    id: 'vizag-mask-layer',
                    type: 'raster',
                    source: 'vizag-mask-source',
                    paint: { 'raster-opacity': 0.8, 'raster-fade-duration': 200 },
                });
            }

            maskLoadedRef.current = true;
            maskLevelRef.current = newLevel;
        } catch (err) {
            console.error('Mask update failed:', err);
        }
        loadingMaskRef.current = false;
        setLoading(null);
    }, [layers]);

    // Debounced mask update on zoom/pan
    const debouncedMaskUpdate = useCallback((map) => {
        if (debounceRef.current) clearTimeout(debounceRef.current);
        debounceRef.current = setTimeout(() => updateMask(map), 500);
    }, [updateMask]);

    // ── Initialize map ──
    useEffect(() => {
        mapboxgl.accessToken = MAPBOX_TOKEN;

        const map = new mapboxgl.Map({
            container: mapContainerRef.current,
            style: `mapbox://styles/mapbox/${mapStyle || 'dark-v11'}`,
            center: [80.6, 16.8],
            zoom: 7,
            antialias: true,
        });

        map.addControl(new mapboxgl.NavigationControl(), 'top-right');
        map.addControl(new mapboxgl.ScaleControl({ unit: 'metric' }), 'bottom-right');

        map.on('mousemove', (e) => {
            setCoords({ lng: e.lngLat.lng.toFixed(5), lat: e.lngLat.lat.toFixed(5) });
        });

        // Reload mask at new resolution on zoom/move end
        map.on('moveend', () => {
            if (layers['vizag-mask'] && mapRef.current) {
                debouncedMaskUpdate(map);
            }
        });

        map.on('load', () => {
            addGunturSources(map);
            mapRef.current = map;
        });

        return () => {
            mapRef.current = null;
            maskLoadedRef.current = false;
            maskLevelRef.current = -1;
            map.remove();
        };
    }, [mapStyle]);

    // ── Guntur layers ──
    function addGunturSources(map) {
        GUNTUR_LAYERS.forEach((lc) => {
            const srcId = `${lc.id}-source`;
            if (map.getSource(srcId)) return;
            map.addSource(srcId, { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
            map.addLayer({ id: `${lc.id}-fill`, type: 'fill', source: srcId, paint: { 'fill-color': lc.color.fill, 'fill-opacity': 0.7 }, layout: { visibility: 'none' } });
            map.addLayer({ id: `${lc.id}-outline`, type: 'line', source: srcId, paint: { 'line-color': lc.color.outline, 'line-width': 1.5, 'line-opacity': 0.8 }, layout: { visibility: 'none' } });

            map.on('click', `${lc.id}-fill`, (e) => {
                if (!e.features.length) return;
                const p = e.features[0].properties;
                let html = `<div class="popup-title">${lc.label}</div>`;
                for (const k of ['ST_NAME', 'PC_NAME', 'district', 'state', 'Res', 'Shape__Area']) {
                    if (p[k] != null && p[k] !== '') {
                        let v = p[k];
                        if (k === 'Shape__Area') v = parseFloat(v).toLocaleString(undefined, { maximumFractionDigits: 2 }) + ' m²';
                        html += `<div class="popup-row"><span class="popup-key">${k}</span><span class="popup-value">${v}</span></div>`;
                    }
                }
                new mapboxgl.Popup({ maxWidth: '280px' }).setLngLat(e.lngLat).setHTML(html).addTo(map);
            });
            map.on('mouseenter', `${lc.id}-fill`, () => { map.getCanvas().style.cursor = 'pointer'; });
            map.on('mouseleave', `${lc.id}-fill`, () => { map.getCanvas().style.cursor = ''; });
        });
    }

    const fetchGunturLayer = useCallback(async (lc) => {
        if (fetchedLayers.has(lc.id)) return null;
        fetchedLayers.add(lc.id);
        try {
            setLoading(`Loading ${lc.label}...`);
            const resp = await fetch(`${ARCGIS_FEATURE_SERVER}/${lc.serverId}/query?where=1%3D1&outFields=*&outSR=4326&f=geojson&resultRecordCount=5000`);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            return await resp.json();
        } catch (err) {
            console.error(`Fetch ${lc.label} failed:`, err);
            fetchedLayers.delete(lc.id);
            return null;
        } finally { setLoading(null); }
    }, []);

    // ── Layer visibility ──
    useEffect(() => {
        const map = mapRef.current;
        if (!map) return;

        // Mask
        if (layers['vizag-mask']) {
            if (!maskLoadedRef.current) {
                updateMask(map);
            }
            if (map.getLayer('vizag-mask-layer')) {
                map.setLayoutProperty('vizag-mask-layer', 'visibility', 'visible');
            }
        } else {
            if (map.getLayer('vizag-mask-layer')) {
                map.setLayoutProperty('vizag-mask-layer', 'visibility', 'none');
            }
        }

        // Guntur
        GUNTUR_LAYERS.forEach(async (lc) => {
            const vis = layers[lc.id];
            if (map.getLayer(`${lc.id}-fill`)) {
                map.setLayoutProperty(`${lc.id}-fill`, 'visibility', vis ? 'visible' : 'none');
                map.setLayoutProperty(`${lc.id}-outline`, 'visibility', vis ? 'visible' : 'none');
            }
            if (vis && !fetchedLayers.has(lc.id)) {
                const gj = await fetchGunturLayer(lc);
                if (gj?.features) { const s = map.getSource(`${lc.id}-source`); if (s) s.setData(gj); }
            }
        });
    }, [layers, updateMask, fetchGunturLayer]);

    return (
        <>
            <div ref={mapContainerRef} className="map-container" />
            {loading && (
                <div className="loading-overlay">
                    <div className="spinner" />
                    <span className="loading-text">{loading}</span>
                </div>
            )}
            {coords && (
                <div className="coords-display">
                    {coords.lat}°N, {coords.lng}°E
                </div>
            )}
        </>
    );
});

export default MapView;
export { GUNTUR_LAYERS, LAYER_COLORS, LOCATIONS, MAP_STYLES, MASK_COLORS };
