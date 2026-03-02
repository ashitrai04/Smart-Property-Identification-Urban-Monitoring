import React, { useEffect, useRef, useState, useCallback } from "react";
import mapboxgl from "mapbox-gl";
import "mapbox-gl/dist/mapbox-gl.css";
import Draggable from "react-draggable";
import { addArcGISFeatureLayer, removeLayerGroup } from "../utils/mapLayers";
import { load as lercLoad, decode as lercDecode } from "lerc";
import proj4 from "proj4";

proj4.defs("EPSG:32644", "+proj=utm +zone=44 +datum=WGS84 +units=m +no_defs");

const MAPBOX_TOKEN = import.meta.env.VITE_MAPBOX_TOKEN;

// Service URLs for Masking
const DISTRICT_SERVICE = 'https://services5.arcgis.com/73n8CSGpSSyHr1T9/arcgis/rest/services/district_boundary/FeatureServer/0';
const STATE_SERVICE = 'https://services5.arcgis.com/73n8CSGpSSyHr1T9/arcgis/rest/services/state_boundary/FeatureServer/0';

// Land Covers legend
const LAND_COVER_LEGEND = [
  { label: 'Water', color: '#5b98d7' },
  { label: 'Trees', color: '#4c7b4e' },
  { label: 'Flooded Veg', color: '#7c86bf' },
  { label: 'Crops', color: '#da9949' },
  { label: 'Built Area', color: '#b53728' },
  { label: 'Bare Ground', color: '#a39b90' },
  { label: 'Snow/Ice', color: '#b6e9fe' },
  { label: 'Clouds', color: '#616161' },
  { label: 'Rangeland', color: '#e3e2c6' },
];

const SENTINEL_SOURCE = 'sentinel-lulc';
const SENTINEL_LAYER = 'sentinel-lulc-layer';
const SENTINEL_MASK_SOURCE = 'sentinel-mask-src';
const SENTINEL_MASK_LAYER = 'sentinel-mask-layer';
const SENTINEL_LULC_URL = 'https://livingatlas.esri.in/server/rest/services/Sentinel_Lulc/MapServer/export?bbox={bbox-epsg-3857}&bboxSR=3857&imageSR=3857&size=256,256&format=png32&transparent=true&f=image';

// ───────── DATA CONFIG ─────────
const STATES = [{ name: "Andhra Pradesh", center: [80.0, 15.9], zoom: 6.5 }];

const DISTRICTS = {
    "Andhra Pradesh": [
        {
            name: "Visakhapatnam",
            center: [83.25, 17.93],
            zoom: 11,
            featureServer: "https://services5.arcgis.com/73n8CSGpSSyHr1T9/arcgis/rest/services/final_visakhapatnam/FeatureServer",
            imageServer: "https://tiledimageservices5.arcgis.com/73n8CSGpSSyHr1T9/arcgis/rest/services/VISAKHA_RASTER/ImageServer",
            hasMask: true,
            layers: [
                { id: 0, name: "boundary", label: "Boundary", color: "#7B2D8E", isBoundary: true },
                { id: 1, name: "buildings", label: "Buildings", isBuilding: true },
                { id: 2, name: "openareas", label: "Open Areas", color: "#9CA3AF" },
                { id: 3, name: "roads", label: "Roads", color: "#EAB308", isRoad: true },
                { id: 4, name: "waterbodies", label: "Waterbodies", color: "#3B82F6" },
            ],
        },
        {
            name: "Vijayawada",
            center: [80.62, 16.51],
            zoom: 11,
            featureServer: "https://services5.arcgis.com/73n8CSGpSSyHr1T9/arcgis/rest/services/vijayawada_layers/FeatureServer",
            imageServer: null,
            hasMask: false,
            layers: [
                { id: 0, name: "boundary", label: "Boundary", color: "#7B2D8E", isBoundary: true },
                { id: 1, name: "buildings", label: "Buildings", color: "#EF4444" },
                { id: 2, name: "openareas", label: "Open Areas", color: "#9CA3AF" },
                { id: 3, name: "roads", label: "Roads", color: "#EAB308", isRoad: true },
                { id: 4, name: "waterbodies", label: "Waterbodies", color: "#3B82F6" },
            ],
        },
        {
            name: "Guntur",
            center: [80.45, 16.30],
            zoom: 11,
            featureServer: "https://services5.arcgis.com/73n8CSGpSSyHr1T9/arcgis/rest/services/guntur_layer/FeatureServer",
            imageServer: null,
            hasMask: false,
            layers: [
                { id: 0, name: "boundary", label: "Boundary", color: "#7B2D8E", isBoundary: true },
                { id: 1, name: "buildings", label: "Buildings", color: "#EF4444" },
                { id: 2, name: "openareas", label: "Open Areas", color: "#9CA3AF" },
                { id: 3, name: "roads", label: "Roads", color: "#EAB308", isRoad: true },
                { id: 4, name: "waterbodies", label: "Waterbodies", color: "#3B82F6" },
            ],
        },
        {
            name: "Anantapur",
            center: [77.60, 14.68],
            zoom: 10,
            featureServer: "https://services5.arcgis.com/73n8CSGpSSyHr1T9/arcgis/rest/services/anantapur_layers/FeatureServer",
            imageServer: null,
            hasMask: false,
            layers: [
                { id: 0, name: "boundary", label: "Boundary", color: "#7B2D8E", isBoundary: true },
                { id: 1, name: "buildings", label: "Buildings", color: "#EF4444" },
                { id: 2, name: "openareas", label: "Open Areas", color: "#9CA3AF" },
                { id: 3, name: "roads", label: "Roads", color: "#EAB308", isRoad: true },
                { id: 4, name: "waterbodies", label: "Waterbodies", color: "#3B82F6" },
            ],
        },
        {
            name: "Nellore",
            center: [79.99, 14.44],
            zoom: 10,
            featureServer: "https://services5.arcgis.com/73n8CSGpSSyHr1T9/arcgis/rest/services/nellore_shpfiles/FeatureServer",
            imageServer: null,
            hasMask: false,
            layers: [
                { id: 0, name: "boundary", label: "Boundary", color: "#7B2D8E", isBoundary: true },
                { id: 1, name: "buildings", label: "Buildings", color: "#EF4444" },
                { id: 2, name: "openareas", label: "Open Areas", color: "#9CA3AF" },
                { id: 3, name: "roads", label: "Roads", color: "#EAB308", isRoad: true },
                { id: 4, name: "waterbodies", label: "Waterbodies", color: "#3B82F6" },
            ],
        },
    ],
};

// Mask colors — matching segmentation legend
const MASK_COLORS = {
    1: [220, 38, 38, 200],     // Dark Red — Buildings (High Confidence ≥0.75)
    2: [249, 115, 22, 200],    // Orange  — Buildings (Medium Confidence ≥0.70)
    3: [251, 191, 36, 200],    // Amber   — Buildings (Low Confidence ≥0.65)
    4: [234, 179, 8, 200],     // Yellow  — Roads
    5: [59, 130, 246, 200],    // Blue    — Waterbodies
    6: [156, 163, 175, 200],   // Gray    — Open Areas
};

const BASE_MAPS = [
    { id: "dark-v11", label: "Dark", icon: "🌙" },
    { id: "satellite-streets-v12", label: "Satellite", icon: "🛰️" },
    { id: "streets-v12", label: "Streets", icon: "🛣️" },
    { id: "light-v11", label: "Light", icon: "☀️" },
    { id: "outdoors-v12", label: "Outdoors", icon: "🏔️" },
];

// ── LERC ──
const TILE_ORIGIN = { x: -5120763.26769827, y: 9997963.94301857 };
const EXTENT = { xmin: 712492.695837956, ymin: 1952042.85329722, xmax: 765051.695837956, ymax: 2016671.85329722 };

function levelRes(lev) { return 256 / Math.pow(2, lev); }
function levelSpan(lev) { return 256 * levelRes(lev); }
function zoomToLevel(z) {
    if (z <= 9) return 0; if (z <= 10) return 1; if (z <= 11) return 2;
    if (z <= 12) return 3; if (z <= 13) return 4; if (z <= 14) return 5;
    if (z <= 15) return 6; if (z <= 16) return 7; return 8;
}

let lercReady = false;
const lercReadyP = lercLoad({ locateFile: n => `/${n}` }).then(() => { lercReady = true; });

const decodedTileCache = new Map();
async function getDecodedTile(imageServer, level, row, col) {
    const key = `${level}/${row}/${col}`;
    if (decodedTileCache.has(key)) return decodedTileCache.get(key);
    if (!lercReady) await lercReadyP;
    try {
        const resp = await fetch(`${imageServer}/tile/${level}/${row}/${col}`);
        if (!resp.ok) { decodedTileCache.set(key, null); return null; }
        const decoded = lercDecode(await resp.arrayBuffer());
        const pixels = decoded.pixels[0];
        decodedTileCache.set(key, pixels);
        return pixels;
    } catch { decodedTileCache.set(key, null); return null; }
}

async function buildMaskForViewport(map, imageServer, onProgress) {
    const bounds = map.getBounds();
    const zoom = Math.round(map.getZoom());
    const arcLevel = zoomToLevel(zoom);
    const res = levelRes(arcLevel);
    const span = levelSpan(arcLevel);

    const corners = {
        tl: proj4("EPSG:4326", "EPSG:32644", [bounds.getWest(), bounds.getNorth()]),
        tr: proj4("EPSG:4326", "EPSG:32644", [bounds.getEast(), bounds.getNorth()]),
        bl: proj4("EPSG:4326", "EPSG:32644", [bounds.getWest(), bounds.getSouth()]),
        br: proj4("EPSG:4326", "EPSG:32644", [bounds.getEast(), bounds.getSouth()]),
    };

    const utmXmin = Math.max(EXTENT.xmin, Math.min(corners.tl[0], corners.bl[0]));
    const utmXmax = Math.min(EXTENT.xmax, Math.max(corners.tr[0], corners.br[0]));
    const utmYmin = Math.max(EXTENT.ymin, Math.min(corners.bl[1], corners.br[1]));
    const utmYmax = Math.min(EXTENT.ymax, Math.max(corners.tl[1], corners.tr[1]));
    if (utmXmin >= utmXmax || utmYmin >= utmYmax) return null;

    const colMin = Math.floor((utmXmin - TILE_ORIGIN.x) / span);
    const colMax = Math.floor((utmXmax - TILE_ORIGIN.x) / span);
    const rowMin = Math.floor((TILE_ORIGIN.y - utmYmax) / span);
    const rowMax = Math.floor((TILE_ORIGIN.y - utmYmin) / span);
    const numCols = colMax - colMin + 1, numRows = rowMax - rowMin + 1;
    const total = numCols * numRows;
    if (total > 200) return null;

    let done = 0;
    const promises = [];
    for (let r = rowMin; r <= rowMax; r++)
        for (let c = colMin; c <= colMax; c++)
            promises.push(getDecodedTile(imageServer, arcLevel, r, c).then(() => { done++; if (onProgress) onProgress(Math.round((done / total) * 100)); }));
    await Promise.all(promises);

    const maxDim = 2048;
    let outW = numCols * 256, outH = numRows * 256;
    if (outW > maxDim || outH > maxDim) { const s = maxDim / Math.max(outW, outH); outW = Math.round(outW * s); outH = Math.round(outH * s); }

    const canvas = document.createElement("canvas");
    canvas.width = outW; canvas.height = outH;
    const ctx = canvas.getContext("2d");
    const imgData = ctx.createImageData(outW, outH);
    const data = imgData.data;

    const stMinX = TILE_ORIGIN.x + colMin * span, stMaxY = TILE_ORIGIN.y - rowMin * span;
    const stMaxX = TILE_ORIGIN.x + (colMax + 1) * span, stMinY = TILE_ORIGIN.y - (rowMax + 1) * span;
    const stW = stMaxX - stMinX, stH = stMaxY - stMinY;

    for (let py = 0; py < outH; py++) {
        const utmY = stMaxY - (py / outH) * stH;
        for (let px = 0; px < outW; px++) {
            const utmX = stMinX + (px / outW) * stW;
            const col = Math.floor((utmX - TILE_ORIGIN.x) / span);
            const row = Math.floor((TILE_ORIGIN.y - utmY) / span);
            const tilePixels = decodedTileCache.get(`${arcLevel}/${row}/${col}`);
            if (!tilePixels) continue;
            const tileMinX = TILE_ORIGIN.x + col * span, tileMaxY = TILE_ORIGIN.y - row * span;
            const srcX = Math.floor((utmX - tileMinX) / res), srcY = Math.floor((tileMaxY - utmY) / res);
            if (srcX < 0 || srcX >= 256 || srcY < 0 || srcY >= 256) continue;
            const val = tilePixels[srcY * 256 + srcX];
            const color = MASK_COLORS[val];
            if (color) { const idx = (py * outW + px) * 4; data[idx] = color[0]; data[idx + 1] = color[1]; data[idx + 2] = color[2]; data[idx + 3] = color[3]; }
        }
    }
    ctx.putImageData(imgData, 0, 0);
    const dataUrl = canvas.toDataURL("image/png");
    const tlWgs = proj4("EPSG:32644", "EPSG:4326", [stMinX, stMaxY]);
    const trWgs = proj4("EPSG:32644", "EPSG:4326", [stMaxX, stMaxY]);
    const brWgs = proj4("EPSG:32644", "EPSG:4326", [stMaxX, stMinY]);
    const blWgs = proj4("EPSG:32644", "EPSG:4326", [stMinX, stMinY]);
    return { dataUrl, coordinates: [[tlWgs[0], tlWgs[1]], [trWgs[0], trWgs[1]], [brWgs[0], brWgs[1]], [blWgs[0], blWgs[1]]], level: arcLevel };
}

// ==================== COMPONENT ====================
// ==================== COMPONENT ====================
export default function Mapping() {
    const mapContainerRef = useRef(null);
    const mapRef = useRef(null);

    const [selectedState, setSelectedState] = useState("Andhra Pradesh");
    const [selectedDistrict, setSelectedDistrict] = useState(null);
    // Note: If you implement village-level feature server later
    const [selectedVillage, setSelectedVillage] = useState("");
    const [baseMap, setBaseMap] = useState("dark-v11");
    const [loading, setLoading] = useState(null);
    const [coords, setCoords] = useState(null);
    const [panelOpen, setPanelOpen] = useState(true);
    const panelRef = useRef(null); // Ref for Draggable

    const [activeLayers, setActiveLayers] = useState({});
    const [maskOn, setMaskOn] = useState(false);
    
    // Sentinel Control
    const [showSentinel, setShowSentinel] = useState(false);
    const sentinelMaskGeomRef = useRef(null);

    const maskLoadedRef = useRef(false);
    const maskLevelRef = useRef(-1);
    const loadingMaskRef = useRef(false);
    const debounceRef = useRef(null);
    const activeLayerIdsRef = useRef(new Set());

    // ── Initialize map ──
    useEffect(() => {
        mapboxgl.accessToken = MAPBOX_TOKEN;
        const map = new mapboxgl.Map({
            container: mapContainerRef.current,
            style: `mapbox://styles/mapbox/${baseMap}`,
            center: [80.0, 15.9],
            zoom: 6.5,
            antialias: true,
        });
        map.addControl(new mapboxgl.NavigationControl(), "top-right");
        map.addControl(new mapboxgl.ScaleControl({ unit: "metric" }), "bottom-right");
        map.on("mousemove", e => setCoords({ lng: e.lngLat.lng.toFixed(5), lat: e.lngLat.lat.toFixed(5) }));
        map.on("moveend", () => {
            if (maskOn && mapRef.current) {
                if (debounceRef.current) clearTimeout(debounceRef.current);
                debounceRef.current = setTimeout(() => updateMask(mapRef.current), 500);
            }
        });
        map.on("load", () => { mapRef.current = map; });
        return () => { mapRef.current = null; maskLoadedRef.current = false; maskLevelRef.current = -1; map.remove(); };
    }, [baseMap]);

    // ── Sentinel LULC Functions ──
    const addSentinelLayer = useCallback((map) => {
        if (!showSentinel) return;
        const sourceId = SENTINEL_SOURCE;
        const layerId = SENTINEL_LAYER;
        
        if (!map.getSource(sourceId)) {
            map.addSource(sourceId, {
                type: 'raster',
                tiles: [SENTINEL_LULC_URL],
                tileSize: 256,
                attribution: '© Esri Living Atlas India'
            });
        }
        
        if (!map.getLayer(layerId)) {
            const style = map.getStyle();
            let beforeId;
            if (style && Array.isArray(style.layers)) {
                const preferred = ['waterway-label', 'settlement-label', 'place-label'];
                beforeId = preferred.find(id => style.layers.some(l => l.id === id));
                if (!beforeId) {
                    const sym = style.layers.find(l => l.type === 'symbol');
                    beforeId = sym ? sym.id : undefined;
                }
            }
            const layerDef = {
                id: layerId,
                type: 'raster',
                source: sourceId,
                paint: { 'raster-opacity': 0.95, 'raster-fade-duration': 0 }
            };
            try {
                if (beforeId) map.addLayer(layerDef, beforeId);
                else map.addLayer(layerDef);
            } catch (e) {
                try { map.addLayer(layerDef); } catch (_) {}
            }
        }
    }, [showSentinel]);

    const removeSentinelLayer = useCallback((map) => {
        if (map.getLayer(SENTINEL_LAYER)) try { map.removeLayer(SENTINEL_LAYER); } catch (_) {}
        if (map.getSource(SENTINEL_SOURCE)) try { map.removeSource(SENTINEL_SOURCE); } catch (_) {}
    }, []);

    const addSentinelMask = useCallback((map, geom) => {
        if (!geom) return;
        let outer = [[-179.9, -85], [179.9, -85], [179.9, 85], [-179.9, 85], [-179.9, -85]];
        const polygons = [];
        if (geom.type === 'Polygon') polygons.push(geom.coordinates);
        else if (geom.type === 'MultiPolygon') for (const p of geom.coordinates) polygons.push(p);
        else return;
        
        let holes = polygons.map(rings => rings[0]).filter(Boolean);
        const ringArea = (ring) => {
            let sum = 0;
            for (let i = 0; i < ring.length - 1; i++) {
                sum += (ring[i+1][0] - ring[i][0]) * (ring[i+1][1] + ring[i][1]);
            }
            return sum;
        };
        const isCCW = (ring) => ringArea(ring) < 0;
        if (!isCCW(outer)) outer = [...outer].reverse();
        holes = holes.map(h => (isCCW(h) ? [...h].reverse() : h));
        
        const maskFeature = {
            type: 'Feature',
            properties: {},
            geometry: { type: 'Polygon', coordinates: [outer, ...holes] }
        };
        
        if (!map.getSource(SENTINEL_MASK_SOURCE)) {
            map.addSource(SENTINEL_MASK_SOURCE, {
                type: 'geojson',
                data: { type: 'FeatureCollection', features: [maskFeature] }
            });
        } else {
            const src = map.getSource(SENTINEL_MASK_SOURCE);
            if (src && src.setData) src.setData({ type: 'FeatureCollection', features: [maskFeature] });
        }
        
        if (!map.getLayer(SENTINEL_MASK_LAYER)) {
            map.addLayer({
                id: SENTINEL_MASK_LAYER,
                type: 'fill',
                source: SENTINEL_MASK_SOURCE,
                paint: { 'fill-color': '#ffffff', 'fill-opacity': 1.0 }
            });
        }
    }, []);

    const removeSentinelMask = useCallback((map) => {
        if (map.getLayer(SENTINEL_MASK_LAYER)) try { map.removeLayer(SENTINEL_MASK_LAYER); } catch (_) {}
        if (map.getSource(SENTINEL_MASK_SOURCE)) try { map.removeSource(SENTINEL_MASK_SOURCE); } catch (_) {}
    }, []);

    const showStateBoundary = useCallback(async (stateName) => {
        const whereByName = `State_FSI='${stateName.replace(/'/g, "''")}'`;
        const url = `${STATE_SERVICE}/query?where=${encodeURIComponent(whereByName)}&outFields=*&f=geojson`;
        try {
            const resp = await fetch(url);
            const data = await resp.json();
            if (data?.features?.length && data.features[0].geometry) {
                sentinelMaskGeomRef.current = data.features[0].geometry;
            }
        } catch (error) { console.error('Error fetching state boundary:', error); }
    }, []);

    const showDistrictBoundary = useCallback(async (stateName, districtName) => {
        if (!stateName || !districtName) return;
        try {
            const districtWhere = `district='${districtName.replace(/'/g, "''")}'`;
            const url = `${DISTRICT_SERVICE}/query?where=${encodeURIComponent(districtWhere)}&outFields=*&f=geojson`;
            const resp = await fetch(url);
            const data = await resp.json();
            if (data?.features?.length && data.features[0].geometry) {
                sentinelMaskGeomRef.current = data.features[0].geometry;
            }
        } catch (e) { console.error('Error fetching district boundary:', e); }
    }, []);

    const updateSentinelMask = useCallback(async () => {
        const map = mapRef.current;
        if (!map || !showSentinel) return;
        try {
            let geom = sentinelMaskGeomRef.current || null;
            if (!geom) {
                setLoading("Fetching region boundary...");
                if (selectedState && selectedDistrict) await showDistrictBoundary(selectedState, selectedDistrict);
                else if (selectedState) await showStateBoundary(selectedState);
                geom = sentinelMaskGeomRef.current || null;
                setLoading(null);
            }
            removeSentinelMask(map);
            if (geom) {
                addSentinelLayer(map);
                addSentinelMask(map, geom);
            } else {
                removeSentinelLayer(map);
            }
        } catch (e) {
            console.error('Failed to update sentinel mask:', e);
            removeSentinelMask(map);
            setLoading(null);
        }
    }, [showSentinel, selectedState, selectedDistrict, removeSentinelMask, addSentinelLayer, addSentinelMask, showDistrictBoundary, showStateBoundary]);

    // Sentinel Toggle Effect
    useEffect(() => {
        const map = mapRef.current;
        if (!map) return;
        
        if (showSentinel) {
            sentinelMaskGeomRef.current = null;
            if (!selectedState && !selectedDistrict && !selectedVillage) {
                alert('Select a State (or District/Village) to view Sentinel LULC.');
                setShowSentinel(false);
                return;
            }
            if (!map.isStyleLoaded()) map.once('style.load', () => addSentinelLayer(map));
            else addSentinelLayer(map);
            updateSentinelMask();
        } else {
            removeSentinelLayer(map);
            removeSentinelMask(map);
            
            const onIdle = () => { try { removeSentinelLayer(map); removeSentinelMask(map); } catch (_) {} };
            const onStyle = () => { try { removeSentinelLayer(map); removeSentinelMask(map); } catch (_) {} };
            try { map.once('idle', onIdle); } catch (_) {}
            try { map.once('style.load', onStyle); } catch (_) {}
            
            return () => {
                try { map.off('idle', onIdle); } catch (_) {}
                try { map.off('style.load', onStyle); } catch (_) {}
            };
        }
    }, [showSentinel, updateSentinelMask, addSentinelLayer, removeSentinelLayer, removeSentinelMask, selectedState, selectedDistrict, selectedVillage]);

    // Update mask whenever selection changes while active
    useEffect(() => {
        const map = mapRef.current;
        sentinelMaskGeomRef.current = null;
        if (!showSentinel) return;
        if (map) removeSentinelMask(map);
        if (selectedState) updateSentinelMask();
        else if (map) removeSentinelLayer(map);
    }, [selectedState, selectedDistrict, selectedVillage, showSentinel, removeSentinelMask, updateSentinelMask, removeSentinelLayer]);


    // ── District selection ──
    const handleDistrictSelect = useCallback((districtName) => {
        const map = mapRef.current;
        if (!map) return;
        const dists = DISTRICTS[selectedState] || [];
        const dist = dists.find(d => d.name === districtName);
        if (!dist) return;

        // Clear old layers
        activeLayerIdsRef.current.forEach(id => removeLayerGroup(map, id));
        activeLayerIdsRef.current.clear();
        setActiveLayers({});
        setMaskOn(false);
        maskLoadedRef.current = false;

        // Remove old mask
        if (map.getLayer("vizag-mask-layer")) map.removeLayer("vizag-mask-layer");
        if (map.getSource("vizag-mask-source")) map.removeSource("vizag-mask-source");

        setSelectedDistrict(districtName);
        map.flyTo({ center: dist.center, zoom: dist.zoom, duration: 1500 });
    }, [selectedState]);

    // ── Toggle a feature layer ──
    const toggleLayer = useCallback(async (dist, layer) => {
        const map = mapRef.current;
        if (!map) return;
        const layerId = `${dist.name.toLowerCase()}-${layer.name}`;
        const isOn = activeLayers[layerId];

        if (isOn) {
            removeLayerGroup(map, layerId);
            activeLayerIdsRef.current.delete(layerId);
            setActiveLayers(prev => ({ ...prev, [layerId]: false }));
        } else {
            setLoading(`Loading ${layer.label}...`);
            try {
                let paintOverrides;
                if (layer.isBoundary) {
                    paintOverrides = {
                        fill: { "fill-color": "transparent", "fill-opacity": 0 },
                        outline: { "line-color": "#7B2D8E", "line-width": 3 },
                        line: { "line-color": "#7B2D8E", "line-width": 3 },
                    };
                } else if (layer.isRoad) {
                    paintOverrides = {
                        fill: { "fill-color": "transparent", "fill-opacity": 0 },
                        outline: { "line-color": "#EAB308", "line-width": 2 },
                        line: { "line-color": "#EAB308", "line-width": 2 },
                    };
                } else if (layer.isBuilding) {
                    const bldgColorExpr = [
                        "case",
                        [">=", ["get", "conf"], 0.75], "#DC2626",
                        [">=", ["get", "conf"], 0.7], "#F97316",
                        [">=", ["get", "conf"], 0.65], "#FBBF24",
                        "#EF4444" // Default
                    ];
                    paintOverrides = {
                        fill: { "fill-color": bldgColorExpr, "fill-opacity": 0.4 },
                        outline: { "line-color": bldgColorExpr, "line-width": 1.5 },
                        line: { "line-color": bldgColorExpr, "line-width": 2 },
                        circle: { "circle-color": bldgColorExpr },
                    };
                } else {
                    paintOverrides = {
                        fill: { "fill-color": layer.color, "fill-opacity": 0.4 },
                        outline: { "line-color": layer.color, "line-width": 1.5 },
                        line: { "line-color": layer.color, "line-width": 2 },
                        circle: { "circle-color": layer.color },
                    };
                }

                await addArcGISFeatureLayer(map, {
                    id: layerId,
                    featureServerUrl: `${dist.featureServer}/${layer.id}`,
                    where: layer.where || "1=1",
                    fit: false,
                    paintOverrides,
                    onProgress: (loaded, total) => {
                        if (total > 2000) {
                            setLoading(`Loading ${layer.label}: ${loaded.toLocaleString()} / ${total.toLocaleString()} features...`);
                        }
                    },
                });
                activeLayerIdsRef.current.add(layerId);
                setActiveLayers(prev => ({ ...prev, [layerId]: true }));

                // Click popup
                const fillId = `${layerId}-fill`;
                if (map.getLayer(fillId)) {
                    map.on("click", fillId, (e) => {
                        if (!e.features?.length) return;
                        const p = e.features[0].properties;
                        let html = `<div class="popup-title">${layer.label}</div>`;
                        Object.entries(p).slice(0, 8).forEach(([k, v]) => {
                            if (v != null && v !== "" && k !== "OBJECTID" && k !== "FID") {
                                if (k === "Shape__Area") v = parseFloat(v).toLocaleString(undefined, { maximumFractionDigits: 2 }) + " m²";
                                html += `<div class="popup-row"><span class="popup-key">${k}</span><span class="popup-value">${v}</span></div>`;
                            }
                        });
                        new mapboxgl.Popup({ maxWidth: "280px" }).setLngLat(e.lngLat).setHTML(html).addTo(map);
                    });
                    map.on("mouseenter", fillId, () => { map.getCanvas().style.cursor = "pointer"; });
                    map.on("mouseleave", fillId, () => { map.getCanvas().style.cursor = ""; });
                }
            } catch (err) {
                console.error(`Failed to load ${layer.label}:`, err);
            }
            setLoading(null);
        }
    }, [activeLayers]);

    // ── Toggle mask ──
    const updateMask = useCallback(async (map) => {
        if (loadingMaskRef.current || !selectedDistrict) return;
        const dist = (DISTRICTS[selectedState] || []).find(d => d.name === selectedDistrict);
        if (!dist?.imageServer) return;

        const zoom = Math.round(map.getZoom());
        const newLevel = zoomToLevel(zoom);
        if (newLevel === maskLevelRef.current && maskLoadedRef.current) return;

        loadingMaskRef.current = true;
        setLoading("Loading Mask...");
        try {
            const result = await buildMaskForViewport(map, dist.imageServer, pct => setLoading(`Loading Mask... ${pct}%`));
            if (!result || !map.getCanvas()) { loadingMaskRef.current = false; setLoading(null); return; }
            const source = map.getSource("vizag-mask-source");
            if (source) source.updateImage({ url: result.dataUrl, coordinates: result.coordinates });
            else {
                map.addSource("vizag-mask-source", { type: "image", url: result.dataUrl, coordinates: result.coordinates });
                map.addLayer({ id: "vizag-mask-layer", type: "raster", source: "vizag-mask-source", paint: { "raster-opacity": 0.8, "raster-resampling": "nearest" } });
            }
            maskLoadedRef.current = true;
            maskLevelRef.current = newLevel;
        } catch (err) { console.error("Mask failed:", err); }
        loadingMaskRef.current = false;
        setLoading(null);
    }, [selectedState, selectedDistrict]);

    const toggleMask = useCallback(() => {
        const map = mapRef.current;
        if (!map) return;
        if (maskOn) {
            if (map.getLayer("vizag-mask-layer")) map.setLayoutProperty("vizag-mask-layer", "visibility", "none");
            setMaskOn(false);
        } else {
            setMaskOn(true);
            if (!maskLoadedRef.current) updateMask(map);
            else if (map.getLayer("vizag-mask-layer")) map.setLayoutProperty("vizag-mask-layer", "visibility", "visible");
        }
    }, [maskOn, updateMask]);

    // Get current district config
    const currentDist = (DISTRICTS[selectedState] || []).find(d => d.name === selectedDistrict);

    return (
        <div className="relative w-full" style={{ height: "calc(100vh - 260px)", minHeight: "500px" }}>
            {/* Map */}
            <div ref={mapContainerRef} style={{ position: "absolute", top: 0, left: 0, right: 0, bottom: 0 }} />

            {/* Panel toggle */}
            <button
                onClick={() => setPanelOpen(v => !v)}
                className="absolute top-3 left-3 z-20 bg-[#0B1E3E]/90 text-white w-9 h-9 rounded-lg flex items-center justify-center shadow-lg hover:bg-[#0B1E3E] transition-colors text-sm"
                title={panelOpen ? "Close panel" : "Open panel"}
            >
                {panelOpen ? "✕" : "☰"}
            </button>

            {/* Layer Panel */}
            {panelOpen && (
                <Draggable nodeRef={panelRef} handle=".drag-handle" bounds="parent">
                    <div ref={panelRef} className="absolute top-3 left-14 z-10 w-72 max-h-[calc(100%-24px)] overflow-y-auto bg-[#0B1E3E]/95 backdrop-blur-md text-white rounded-xl shadow-2xl border border-white/10 flex flex-col">
                        <div className="p-4 border-b border-white/10 drag-handle cursor-move flex items-center justify-between bg-white/5 hover:bg-white/10 transition-colors">
                            <h3 className="text-sm font-bold tracking-wide select-none">🗺️ Map Layers</h3>
                            <div className="flex gap-1">
                                <span className="w-1.5 h-1.5 rounded-full bg-white/30" />
                                <span className="w-1.5 h-1.5 rounded-full bg-white/30" />
                                <span className="w-1.5 h-1.5 rounded-full bg-white/30" />
                            </div>
                        </div>

<<<<<<< HEAD
                            {/* Custom LULC toggle using Sentinel */}
                            <label className="flex items-center justify-between py-1.5 px-2 rounded-md hover:bg-white/5 cursor-pointer mb-2 border border-white/5 bg-white/5">
                                <div className="flex items-center gap-2">
                                    <span className="text-sm">🌍</span>
                                    <span className="text-xs font-semibold text-[#0EA5E9]">Land Covers (LULC)</span>
                                </div>
                                <div
                                    className={`w-8 h-4 rounded-full relative transition-colors ${showSentinel ? "bg-[#0EA5E9]" : "bg-white/20"}`}
                                    onClick={() => setShowSentinel(!showSentinel)}
                                >
                                    <div className={`absolute top-0.5 w-3 h-3 rounded-full bg-white shadow transition-all ${showSentinel ? "left-4.5" : "left-0.5"}`} />
                                </div>
                            </label>

                            {/* Legacy local mask toggle */}
                            {currentDist.hasMask && (
                                <label className="flex items-center gap-2.5 py-1.5 px-2 rounded-md hover:bg-white/5 cursor-pointer mb-1">
                                    <div className={`w-8 h-4 rounded-full relative transition-colors ${maskOn ? "bg-[#0EA5E9]" : "bg-white/20"}`}
                                        onClick={toggleMask}
                                    >
                                        <div className={`absolute top-0.5 w-3 h-3 rounded-full bg-white shadow transition-all ${maskOn ? "left-4.5" : "left-0.5"}`} />
                                    </div>
                                    <span className="text-xs">🎭 District Mask (Local)</span>
                                </label>
                            )}

                            {/* Feature layers */}
                            {currentDist.layers.map(layer => {
                                const layerId = `${currentDist.name.toLowerCase()}-${layer.name}`;
                                const isOn = activeLayers[layerId];
                                return (
                                    <label key={layer.id} className="flex items-center gap-2.5 py-1.5 px-2 rounded-md hover:bg-white/5 cursor-pointer">
                                        <div
                                            className={`w-8 h-4 rounded-full relative transition-colors ${isOn ? "bg-[#0EA5E9]" : "bg-white/20"}`}
                                            onClick={() => toggleLayer(currentDist, layer)}
=======
                        <div className="flex-1 overflow-y-auto min-h-0">
                            {/* Base Map */}
                            <div className="p-3 border-b border-white/10 shrink-0">
                                <p className="text-[10px] uppercase tracking-wider text-white/50 mb-2">Base Map</p>
                                <div className="flex flex-wrap gap-1.5">
                                    {BASE_MAPS.map(bm => (
                                        <button
                                            key={bm.id}
                                            onClick={() => setBaseMap(bm.id)}
                                            className={`px-2 py-1.5 text-[10px] rounded-md flex flex-col items-center gap-0.5 transition-colors ${baseMap === bm.id ? "bg-[#0EA5E9] text-white" : "bg-white/10 text-white/70 hover:bg-white/20"
                                                }`}
>>>>>>> 9e705c287377f4bfe2709148dfb4afd337a15630
                                        >
                                            <span>{bm.icon}</span>
                                            <span>{bm.label}</span>
                                        </button>
                                    ))}
                                </div>
                            </div>

                            {/* State selector */}
                            <div className="p-3 border-b border-white/10 shrink-0">
                                <p className="text-[10px] uppercase tracking-wider text-white/50 mb-2">State</p>
                                <select
                                    value={selectedState}
                                    onChange={e => { setSelectedState(e.target.value); setSelectedDistrict(null); }}
                                    className="w-full bg-white/10 text-white text-xs rounded-md px-2 py-1.5 border border-white/20 focus:outline-none focus:ring-1 focus:ring-[#0EA5E9]"
                                >
                                    {STATES.map(s => <option key={s.name} value={s.name} className="bg-[#0B1E3E]">{s.name}</option>)}
                                </select>
                            </div>

                            {/* District selector */}
                            <div className="p-3 border-b border-white/10 shrink-0">
                                <p className="text-[10px] uppercase tracking-wider text-white/50 mb-2">District</p>
                                <select
                                    value={selectedDistrict || ""}
                                    onChange={e => handleDistrictSelect(e.target.value)}
                                    className="w-full bg-white/10 text-white text-xs rounded-md px-2 py-1.5 border border-white/20 focus:outline-none focus:ring-1 focus:ring-[#0EA5E9]"
                                >
                                    <option value="" className="bg-[#0B1E3E]">Select District...</option>
                                    {(DISTRICTS[selectedState] || []).map(d => (
                                        <option key={d.name} value={d.name} className="bg-[#0B1E3E]">{d.name}</option>
                                    ))}
                                </select>
                            </div>

                            {/* District layers */}
                            {currentDist && (
                                <div className="p-3 border-b border-white/10 shrink-0">
                                    <div className="flex items-center justify-between mb-2">
                                        <p className="text-[10px] uppercase tracking-wider text-white/50">{currentDist.name} Layers</p>
                                        <button
                                            onClick={() => mapRef.current?.flyTo({ center: currentDist.center, zoom: currentDist.zoom, duration: 1500 })}
                                            className="text-[10px] text-[#0EA5E9] hover:text-white transition-colors"
                                        >
                                            ↗ Fly To
                                        </button>
                                    </div>

                                    {/* Mask toggle */}
                                    {currentDist.hasMask && (
                                        <label className="flex items-center gap-2.5 py-1.5 px-2 rounded-md hover:bg-white/5 cursor-pointer mb-1">
                                            <div className={`w-8 h-4 rounded-full relative transition-colors ${maskOn ? "bg-[#0EA5E9]" : "bg-white/20"}`}
                                                onClick={toggleMask}
                                            >
                                                <div className={`absolute top-0.5 w-3 h-3 rounded-full bg-white shadow transition-all ${maskOn ? "left-4.5" : "left-0.5"}`} />
                                            </div>
                                            <span className="text-xs select-none">🎭 Land Use Mask</span>
                                        </label>
                                    )}

                                    {/* Feature layers */}
                                    {currentDist.layers.map(layer => {
                                        const layerId = `${currentDist.name.toLowerCase()}-${layer.name}`;
                                        const isOn = activeLayers[layerId];
                                        return (
                                            <label key={layer.id} className="flex items-center gap-2.5 py-1.5 px-2 rounded-md hover:bg-white/5 cursor-pointer">
                                                <div
                                                    className={`w-8 h-4 rounded-full relative transition-colors ${isOn ? "bg-[#0EA5E9]" : "bg-white/20"}`}
                                                    onClick={() => toggleLayer(currentDist, layer)}
                                                >
                                                    <div className={`absolute top-0.5 w-3 h-3 rounded-full bg-white shadow transition-all ${isOn ? "left-4.5" : "left-0.5"}`} />
                                                </div>
                                                <span className="w-2 h-2 rounded-full shrink-0" style={{ background: layer.color || "#EF4444" }} />
                                                <span className="text-xs select-none">{layer.label}</span>
                                            </label>
                                        );
                                    })}

                                    {currentDist.layers.length === 0 && (
                                        <p className="text-xs text-white/40 italic px-2 py-2">Layers coming soon for {currentDist.name}</p>
                                    )}
                                </div>
                            )}

                            {/* Change Detection (Semantic) Layers */}
                            <div className="p-3 border-b border-white/10 shrink-0">
                                <p className="text-[10px] uppercase tracking-wider text-white/50 mb-2">Change Detection (Semantic)</p>
                                <div className="space-y-1">
                                    {[
                                        { label: "New Construction (Open → Building)", color: "#10B981" },
                                        { label: "Encroachment / Reclamation (Water → Building)", color: "#F43F5E" },
                                        { label: "Demolition / Clearing (Building → Open)", color: "#8B5CF6" },
                                        { label: "New Road / Access (Open → Road)", color: "#F59E0B" },
                                        { label: "Monthly Change Summary Layer", color: "#3B82F6" },
                                        { label: "Quarterly Change Summary Layer", color: "#06B6D4" },
                                        { label: "Yearly Change Summary Layer", color: "#EAB308" },
                                    ].map((cd, idx) => (
                                        <label key={idx} className="flex items-center gap-2.5 py-1.5 px-2 rounded-md hover:bg-white/5 cursor-not-allowed opacity-60">
                                            <div className="w-8 h-4 rounded-full relative bg-white/10 shrink-0">
                                                <div className="absolute top-0.5 left-0.5 w-3 h-3 rounded-full bg-white/30 shadow" />
                                            </div>
                                            <span className="w-2 h-2 rounded-full shrink-0" style={{ background: cd.color }} />
                                            <div className="flex flex-col flex-1 min-w-0">
                                                <span className="text-[11px] select-none truncate" title={cd.label}>{cd.label}</span>
                                                <span className="text-[9px] text-white/40 italic mt-0.5">— In Progress</span>
                                            </div>
                                        </label>
                                    ))}
                                </div>
                            </div>

                            {/* Legend */}
                            <div className="p-3 shrink-0">
                                <p className="text-[10px] uppercase tracking-wider text-white/50 mb-2">Legend</p>
                                <div className="space-y-1">
                                    <p className="text-[10px] text-white/40 uppercase mb-1">Layer Colors</p>
                                    <div className="grid grid-cols-2 gap-1">
                                        {[
                                            { label: "Boundary", color: "#7B2D8E", hollow: true },
                                            { label: "Bldg (High)", color: "#DC2626" },
                                            { label: "Bldg (Med)", color: "#F97316" },
                                            { label: "Bldg (Low)", color: "#FBBF24" },
                                            { label: "Roads", color: "#EAB308", hollow: true },
                                            { label: "Water", color: "#3B82F6" },
                                            { label: "Open Areas", color: "#9CA3AF" },
                                        ].map(l => (
                                            <div key={l.label} className="flex items-center gap-1.5">
                                                <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{
                                                    background: l.hollow ? "transparent" : l.color,
                                                    border: l.hollow ? `2px solid ${l.color}` : `1px solid ${l.color}80`,
                                                }} />
                                                <span className="text-[10px] text-white/60 select-none">{l.label}</span>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
<<<<<<< HEAD
                    {/* Sentinel LULC Legend */}
                    {showSentinel && (
                        <div className="p-3 border-t border-white/10 bg-[#0B1E3E]/60">
                            <p className="text-[10px] uppercase tracking-wider text-white/50 mb-2">LULC Categories</p>
                            <div className="grid grid-cols-2 gap-y-1.5 gap-x-2">
                                {LAND_COVER_LEGEND.map(lc => (
                                    <div key={lc.label} className="flex items-center gap-1.5">
                                        <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ background: lc.color }} />
                                        <span className="text-[10px] text-white/80">{lc.label}</span>
                                    </div>
                                ))}
                            </div>
                        </div>
                    )}
                </div>
=======
                </Draggable>
>>>>>>> 9e705c287377f4bfe2709148dfb4afd337a15630
            )}

            {/* Loading indicator */}
            {loading && (
                <div className="absolute bottom-4 right-4 z-20 bg-[#0B1E3E]/90 text-white px-4 py-2 rounded-lg shadow-lg flex items-center gap-2 text-xs">
                    <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                    {loading}
                </div>
            )}

            {/* Coordinates */}
            {coords && (
                <div className="absolute bottom-4 left-1/2 -translate-x-1/2 z-10 bg-[#0B1E3E]/80 text-white/80 px-3 py-1 rounded-full text-[10px] backdrop-blur-sm">
                    {coords.lat}°N, {coords.lng}°E
                </div>
            )}
        </div>
    );
}
