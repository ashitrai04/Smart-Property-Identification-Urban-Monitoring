import React, { useEffect, useRef, useState, useCallback } from "react";
import mapboxgl from "mapbox-gl";
import "mapbox-gl/dist/mapbox-gl.css";
import MapboxDraw from "@mapbox/mapbox-gl-draw";
import "@mapbox/mapbox-gl-draw/dist/mapbox-gl-draw.css";
import Draggable from "react-draggable";
import { addArcGISFeatureLayer, addLocalGeoJSONLayer, reloadVisibleLayers, removeLayerGroup } from "../utils/mapLayers";
import { parseAOIFile, getFeaturesBounds, computeTotalAreaKm2, unionGeometry, polygonCentroid } from "../utils/aoiUtils";
import { computeAOIStats, warmBackend } from "../utils/aoiStats";
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
            dataSource: "local",
            districtKey: "visakhapatnam",
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
            dataSource: "local",
            districtKey: "vijayawada",
            imageServer: null,
            droneImagery: "https://tiledimageservices5.arcgis.com/73n8CSGpSSyHr1T9/arcgis/rest/services/Drone_img_vijayvada/ImageServer",
            hasMask: true,
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
            dataSource: "local",
            districtKey: "guntur",
            imageServer: null,
            hasMask: true,
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
            dataSource: "local",
            districtKey: "anantapur",
            imageServer: null,
            hasMask: true,
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
            dataSource: "local",
            districtKey: "nellore",
            imageServer: null,
            hasMask: true,
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
    for (let r = rowMin; r <= rowMax; r++) {
        for (let c = colMin; c <= colMax; c++) {
            promises.push(
                getDecodedTile(imageServer, arcLevel, r, c).then(() => {
                    done++;
                    if (onProgress) onProgress(Math.round((done / total) * 100));
                })
            );
        }
    }
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

// ── DRONE IMAGERY LERC CONFIG (4326) ──
const DRONE_ORIGIN = { x: -180, y: 90 };
const DRONE_LODS = [
    { level: 0, res: 0.0000666308154761365 },
    { level: 1, res: 0.0000333154077380682 },
    { level: 2, res: 0.0000166577038690341 },
    { level: 3, res: 0.00000832885193451706 },
    { level: 4, res: 0.00000416442596725853 },
    { level: 5, res: 0.00000208221298362926 },
    { level: 6, res: 0.00000104110649181463 },
    { level: 7, res: 5.20553245907316e-7 },
    { level: 8, res: 2.60276622953658e-7 }
];

const VIJAYAWADA_DRONE_EXTENT = {
    xmin: 80.628690247170482,
    ymin: 16.522700957538024,
    xmax: 80.6492427304254,
    ymax: 16.53610806666299,
};

async function buildDroneForLevel(imageServer, level, onProgress) {
    const resolution = DRONE_LODS[level].res;
    const tileLength = 256 * resolution;

    const minCol = Math.floor((VIJAYAWADA_DRONE_EXTENT.xmin - DRONE_ORIGIN.x) / tileLength);
    const maxCol = Math.ceil((VIJAYAWADA_DRONE_EXTENT.xmax - DRONE_ORIGIN.x) / tileLength) - 1;
    const minRow = Math.floor((DRONE_ORIGIN.y - VIJAYAWADA_DRONE_EXTENT.ymax) / tileLength);
    const maxRow = Math.ceil((DRONE_ORIGIN.y - VIJAYAWADA_DRONE_EXTENT.ymin) / tileLength) - 1;

    const cols = maxCol - minCol + 1;
    const rows = maxRow - minRow + 1;
    const totalTiles = cols * rows;

    const canvas = document.createElement("canvas");
    canvas.width = cols * 256;
    canvas.height = rows * 256;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    let loaded = 0;
    const promises = [];

    for (let row = minRow; row <= maxRow; row++) {
        for (let col = minCol; col <= maxCol; col++) {
            promises.push((async () => {
                if (!lercReady) await lercReadyP;
                try {
                    const resp = await fetch(`${imageServer}/tile/${level}/${row}/${col}`);
                    if (resp.ok) {
                        const block = lercDecode(await resp.arrayBuffer());
                        const { width, height, pixels, mask } = block;
                        const red = pixels[0], green = pixels[1], blue = pixels[2];
                        const imgData = new ImageData(width, height);
                        const dest = imgData.data;
                        const hasMaskBand = Boolean(mask);
                        for (let i = 0; i < width * height; i++) {
                            const offset = i * 4;
                            dest[offset] = red ? red[i] : 0;
                            dest[offset + 1] = green ? green[i] : 0;
                            dest[offset + 2] = blue ? blue[i] : 0;
                            dest[offset + 3] = (hasMaskBand && mask && !mask[i]) ? 0 : 255;
                        }
                        ctx.putImageData(imgData, (col - minCol) * 256, (row - minRow) * 256);
                    }
                } catch (e) { }
                loaded++;
                if (onProgress && (loaded % 5 === 0 || loaded === totalTiles)) {
                    onProgress(Math.round((loaded / totalTiles) * 100));
                }
            })());
        }
    }

    await Promise.all(promises);

    const stMinX = DRONE_ORIGIN.x + minCol * tileLength;
    const stMaxY = DRONE_ORIGIN.y - minRow * tileLength;
    const stMaxX = DRONE_ORIGIN.x + (maxCol + 1) * tileLength;
    const stMinY = DRONE_ORIGIN.y - (maxRow + 1) * tileLength;

    return {
        dataUrl: canvas.toDataURL("image/webp", 0.9),
        coordinates: [
            [stMinX, stMaxY],
            [stMaxX, stMaxY],
            [stMaxX, stMinY],
            [stMinX, stMinY]
        ],
        level
    };
}

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
    const [droneOn, setDroneOn] = useState(false);

    // Sentinel Control
    const [showSentinel, setShowSentinel] = useState(false);
    const sentinelMaskGeomRef = useRef(null);

    const maskLoadedRef = useRef(false);
    const maskLevelRef = useRef(-1);
    const loadingMaskRef = useRef(false);

    const droneLoadedRef = useRef(false);
    const droneBuildingRef = useRef(false);
    const dronePendingLevelRef = useRef(null);
    const droneCacheRef = useRef(new Map());
    const droneCurrentLevelRef = useRef(null);

    const debounceRef = useRef(null);
    const activeLayerIdsRef = useRef(new Set());
    const activeLayerConfigsRef = useRef([]);

    // ── AOI (Area of Interest) ──
    const [aoiFeatures, setAoiFeatures] = useState(null);     // Array of Polygon Features (supports multi-polygon uploads)
    const [aoiActive, setAoiActive] = useState(false);        // Whether AOI filtering is on
    const [drawMode, setDrawMode] = useState(false);          // Whether draw tool is active
    const drawRef = useRef(null);                             // MapboxDraw instance
    const aoiFileInputRef = useRef(null);                     // Hidden file input for upload
    const aoiFeaturesRef = useRef(null);                      // Persist AOI (array) across style rebuilds
    const renderAOIRef = useRef(null);                        // Stable handle to re-render AOI on map load
    const districtBoundaryCacheRef = useRef({});              // Cache of district boundary geometries (for auto-detect)
    const aoiClickBoundRef = useRef(null);                    // Tracks which map instance has the parcel-click handler bound

    // ── AOI analytics popup ──
    const [aoiStats, setAoiStats] = useState(null);           // { perPolygon, totals, fetched }
    const [statsLoading, setStatsLoading] = useState(false);
    const [statsOpen, setStatsOpen] = useState(false);
    const [selectedParcel, setSelectedParcel] = useState(null); // index of clicked polygon (multi-polygon AOIs)
    const statsPanelRef = useRef(null);
    const statsAbortRef = useRef(null);                       // cancels stale stats requests on redraw

    // ── Detection overlay (from Upload & Analysis → "Plot on Map") ──
    const [detectionOverlay, setDetectionOverlay] = useState(null); // { url, bounds, name }
    const [detectionOpacity, setDetectionOpacity] = useState(0.85);
    const detectionOverlayRef = useRef(null);
    const addDetectionRef = useRef(null);
    const detPanelRef = useRef(null); // draggable handle for the overlay control
    const DET_SOURCE = 'detection-overlay-src';
    const DET_LAYER = 'detection-overlay-layer';

    // Warm the (Hugging Face) backend on mount so the first AOI analysis isn't
    // blocked by a cold start.
    useEffect(() => { warmBackend(); }, []);

    // Mirrors of toggle state so the map's persistent event handlers stay fresh
    // WITHOUT re-creating the map (re-creating the map was wiping the AOI/layers).
    const maskOnRef = useRef(false);
    const updateMaskRef = useRef(null);
    const updateDroneStateRef = useRef(null);

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
        map.addControl(new mapboxgl.NavigationControl(), "bottom-right");
        map.addControl(new mapboxgl.ScaleControl({ unit: "metric" }), "bottom-right");
        map.on("mousemove", e => setCoords({ lng: e.lngLat.lng.toFixed(5), lat: e.lngLat.lat.toFixed(5) }));
        const vectorDebounceRef = { current: null };
        map.on("moveend", () => {
            if (maskOnRef.current && mapRef.current) {
                if (debounceRef.current) clearTimeout(debounceRef.current);
                debounceRef.current = setTimeout(() => updateMaskRef.current?.(mapRef.current), 500);
            }

            // Reload visible vector layers dynamically (Debounced to prevent stuttering)
            if (mapRef.current && activeLayerConfigsRef.current.length > 0) {
                if (vectorDebounceRef.current) clearTimeout(vectorDebounceRef.current);
                vectorDebounceRef.current = setTimeout(() => {
                    import("../utils/mapLayers").then(({ reloadVisibleLayers }) => {
                        reloadVisibleLayers(mapRef.current, activeLayerConfigsRef.current);
                    });
                }, 600);
            }
        });

        const refreshDroneLevel = () => { if (mapRef.current) updateDroneStateRef.current?.(mapRef.current); };
        map.on("zoomend", refreshDroneLevel);

        map.on("load", () => {
            mapRef.current = map;
            // Re-apply the AOI after a base-map (style) rebuild so it never disappears.
            if (aoiFeaturesRef.current && renderAOIRef.current) {
                try { renderAOIRef.current(map, aoiFeaturesRef.current); } catch (_) { }
            }
            // Re-apply detection overlay after a base-map rebuild
            if (detectionOverlayRef.current && addDetectionRef.current) {
                try { addDetectionRef.current(map, detectionOverlayRef.current, false); } catch (_) { }
            }
        });
        return () => {
            mapRef.current = null;
            maskLoadedRef.current = false; maskLevelRef.current = -1;
            droneLoadedRef.current = false; droneCurrentLevelRef.current = null;
            map.off("zoomend", refreshDroneLevel);
            map.remove();
        };
        // NOTE: only `baseMap` here. maskOn/droneOn must NOT be deps — recreating the
        // map on every layer toggle was erasing the drawn AOI and all loaded layers.
    }, [baseMap]);

    // ── Detection overlay (geo-referenced mask from Upload & Analysis) ──
    const addDetectionOverlay = useCallback((map, payload, fit = true) => {
        if (!map || !payload?.url || !payload?.bounds) return;
        const { url, bounds } = payload;
        const { west, south, east, north } = bounds;
        const coordinates = [[west, north], [east, north], [east, south], [west, south]];
        if (map.getSource(DET_SOURCE)) {
            try { map.getSource(DET_SOURCE).updateImage({ url, coordinates }); } catch (_) { }
        } else {
            map.addSource(DET_SOURCE, { type: 'image', url, coordinates });
        }
        if (!map.getLayer(DET_LAYER)) {
            map.addLayer({ id: DET_LAYER, type: 'raster', source: DET_SOURCE, paint: { 'raster-opacity': detectionOpacity, 'raster-resampling': 'nearest' } });
        }
        if (fit) {
            try { map.fitBounds([[west, south], [east, north]], { padding: 60, duration: 1500 }); } catch (_) { }
        }
    }, [detectionOpacity]);

    const removeDetectionOverlay = useCallback(() => {
        const map = mapRef.current;
        detectionOverlayRef.current = null;
        setDetectionOverlay(null);
        if (!map) return;
        if (map.getLayer(DET_LAYER)) try { map.removeLayer(DET_LAYER); } catch (_) { }
        if (map.getSource(DET_SOURCE)) try { map.removeSource(DET_SOURCE); } catch (_) { }
    }, []);

    // keep the load-handler's stable ref pointed at the latest function
    useEffect(() => { addDetectionRef.current = addDetectionOverlay; });

    // live opacity updates
    useEffect(() => {
        const map = mapRef.current;
        if (map && map.getLayer(DET_LAYER)) {
            try { map.setPaintProperty(DET_LAYER, 'raster-opacity', detectionOpacity); } catch (_) { }
        }
    }, [detectionOpacity]);

    // consume a pending overlay handed over from the Upload & Analysis page (once)
    useEffect(() => {
        let raw;
        try { raw = localStorage.getItem('pendingMapOverlay'); } catch (_) { return; }
        if (!raw) return;
        try {
            const payload = JSON.parse(raw);
            localStorage.removeItem('pendingMapOverlay');
            detectionOverlayRef.current = payload;
            setDetectionOverlay(payload);
            const tryAdd = () => {
                const map = mapRef.current;
                if (map && map.isStyleLoaded()) addDetectionOverlay(map, payload, true);
                else setTimeout(tryAdd, 300);
            };
            tryAdd();
        } catch (e) { console.warn('detection overlay parse failed', e); }
    }, [addDetectionOverlay]);

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
            const layerDef = {
                id: layerId,
                type: 'raster',
                source: sourceId,
                minzoom: 4,
                maxzoom: 16,
                paint: {
                    'raster-opacity': 0.85,
                    'raster-fade-duration': 300,
                    'raster-resampling': 'nearest'
                }
            };
            try {
                const style = map.getStyle();
                let beforeId;
                if (style && style.layers) {
                    // Try to place the Sentinel LULC layer below any label, road, or symbol layers so it acts as a basemap overlay
                    const firstLabelOrLine = style.layers.find(l => l.type === 'symbol' || l.type === 'line' || (l.id && l.id.includes('label')));
                    beforeId = firstLabelOrLine ? firstLabelOrLine.id : undefined;
                }

                if (beforeId) map.addLayer(layerDef, beforeId);
                else map.addLayer(layerDef);
            } catch (e) {
                console.warn("Error injecting Sentinel LULC layer before existing layers. Adding it to the top.", e);
                try { map.addLayer(layerDef); } catch (_) { }
            }
        }
    }, [showSentinel]);

    const removeSentinelLayer = useCallback((map) => {
        if (map.getLayer(SENTINEL_LAYER)) try { map.removeLayer(SENTINEL_LAYER); } catch (_) { }
        if (map.getSource(SENTINEL_SOURCE)) try { map.removeSource(SENTINEL_SOURCE); } catch (_) { }
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
                sum += (ring[i + 1][0] - ring[i][0]) * (ring[i + 1][1] + ring[i][1]);
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
        if (map.getLayer(SENTINEL_MASK_LAYER)) try { map.removeLayer(SENTINEL_MASK_LAYER); } catch (_) { }
        if (map.getSource(SENTINEL_MASK_SOURCE)) try { map.removeSource(SENTINEL_MASK_SOURCE); } catch (_) { }
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
            const distConfig = (DISTRICTS[stateName] || []).find(d => d.name === districtName);

            // Try local backend first
            if (distConfig && distConfig.dataSource === "local") {
                const { API_BASE } = await import("../utils/mapLayers");
                const distKey = distConfig.districtKey || distConfig.name.toLowerCase();
                const url = `${API_BASE}/api/districts/${encodeURIComponent(distKey)}/boundary`;
                const resp = await fetch(url);
                const data = await resp.json();
                if (data?.features?.length && data.features[0].geometry) {
                    sentinelMaskGeomRef.current = data.features[0].geometry;
                    return;
                }
            }

            // Fallback: ArcGIS FeatureServer
            if (distConfig && distConfig.featureServer) {
                const boundaryLayer = distConfig.layers.find(l => l.isBoundary || l.name === 'boundary');
                const layerId = boundaryLayer ? boundaryLayer.id : '0';
                const url = `${distConfig.featureServer}/${layerId}/query?where=1=1&outFields=*&f=geojson`;
                const resp = await fetch(url);
                const data = await resp.json();
                if (data?.features?.length && data.features[0].geometry) {
                    sentinelMaskGeomRef.current = data.features[0].geometry;
                    return;
                }
            }

            // Fallback to Living Atlas generalized geometry
            const districtWhere = `district='${districtName.replace(/'/g, "''")}'`;
            const url = `${DISTRICT_SERVICE}/query?where=${encodeURIComponent(districtWhere)}&outFields=*&f=geojson`;
            const resp = await fetch(url);
            const data = await resp.json();
            if (data?.features?.length && data.features[0].geometry) {
                sentinelMaskGeomRef.current = data.features[0].geometry;
            }
        } catch (e) {
            console.error('Error fetching district boundary:', e);
        }
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

        const loadSentinel = async () => {
            if (showSentinel) {
                sentinelMaskGeomRef.current = null;
                if (!selectedState && !selectedDistrict && !selectedVillage) {
                    alert('Select a State (or District/Village) to view Sentinel LULC.');
                    setShowSentinel(false);
                    return;
                }

                if (!map.isStyleLoaded()) {
                    map.once('style.load', async () => {
                        addSentinelLayer(map);
                        await updateSentinelMask();
                    });
                } else {
                    addSentinelLayer(map);
                    await updateSentinelMask();
                }
            } else {
                removeSentinelLayer(map);
                removeSentinelMask(map);
            }
        };

        loadSentinel();

        // Cleanup functions
        const onIdle = () => { if (!showSentinel) { removeSentinelLayer(map); removeSentinelMask(map); } };
        const onStyle = () => { if (!showSentinel) { removeSentinelLayer(map); removeSentinelMask(map); } };

        map.on('idle', onIdle);
        map.on('style.load', onStyle);

        return () => {
            map.off('idle', onIdle);
            map.off('style.load', onStyle);
        };
    }, [showSentinel, selectedState, selectedDistrict, selectedVillage, addSentinelLayer, removeSentinelLayer, removeSentinelMask, updateSentinelMask]);

    // Update mask whenever selection changes while active
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !showSentinel) return;

        sentinelMaskGeomRef.current = null; // Clear old mask

        const refreshMask = async () => {
            if (selectedState) {
                await updateSentinelMask();
            } else {
                removeSentinelLayer(map);
                removeSentinelMask(map);
            }
        };

        refreshMask();
    }, [selectedState, selectedDistrict, selectedVillage, showSentinel, removeSentinelMask, updateSentinelMask, removeSentinelLayer]);


    // ── District selection ──
    // fly=false is used when auto-switching to the AOI's district (we stay on the AOI).
    const handleDistrictSelect = useCallback((districtName, fly = true) => {
        const map = mapRef.current;
        if (!map) return;
        const dists = DISTRICTS[selectedState] || [];
        const dist = dists.find(d => d.name === districtName);
        if (!dist) return;

        // Clear old layers
        activeLayerIdsRef.current.forEach(id => removeLayerGroup(map, id));
        activeLayerIdsRef.current.clear();
        activeLayerConfigsRef.current = []; // also drop stale reload configs
        setActiveLayers({});
        setMaskOn(false);
        maskLoadedRef.current = false;

        // Remove old mask
        if (map.getLayer("vizag-mask-layer")) map.removeLayer("vizag-mask-layer");
        if (map.getSource("vizag-mask-source")) map.removeSource("vizag-mask-source");

        // Remove old drone layer
        if (map.getLayer("drone-layer")) map.removeLayer("drone-layer");
        if (map.getSource("drone-source")) map.removeSource("drone-source");
        setDroneOn(false);
        droneLoadedRef.current = false;
        droneCacheRef.current.clear();
        droneCurrentLevelRef.current = null;

        setSelectedDistrict(districtName);
        if (fly) map.flyTo({ center: dist.center, zoom: dist.zoom, duration: 1500 });
    }, [selectedState]);

    // ── Toggle a feature layer ──
    const toggleLayer = useCallback(async (dist, layer) => {
        const map = mapRef.current;
        if (!map) return;
        const layerId = `${dist.name.toLowerCase()}-${layer.name}`;
        const isOn = activeLayers[layerId];

        // If already loaded, just toggle visibility (instant, no re-fetch)
        const fillId = `${layerId}-fill`;
        const outlineId = `${layerId}-outline`;
        const lineId = `${layerId}-line`;
        const alreadyLoaded = map.getSource(layerId);

        if (isOn) {
            // Hide all sub-layers
            [fillId, outlineId, lineId, layerId].forEach(lid => {
                if (map.getLayer(lid)) map.setLayoutProperty(lid, "visibility", "none");
            });
            activeLayerIdsRef.current.delete(layerId);
            activeLayerConfigsRef.current = activeLayerConfigsRef.current.filter(c => c.id !== layerId);
            setActiveLayers(prev => ({ ...prev, [layerId]: false }));
        } else if (alreadyLoaded) {
            // Already loaded — just show again (instant!)
            [fillId, outlineId, lineId, layerId].forEach(lid => {
                if (map.getLayer(lid)) map.setLayoutProperty(lid, "visibility", "visible");
            });
            activeLayerIdsRef.current.add(layerId);
            activeLayerConfigsRef.current.push({ id: layerId, district: dist.districtKey || dist.name.toLowerCase(), layer: layer.name });
            setActiveLayers(prev => ({ ...prev, [layerId]: true }));
            // Re-apply AOI clip to the re-shown layer
            if (aoiActive && aoiFeaturesRef.current) {
                const withinFilter = ['within', unionGeometry(aoiFeaturesRef.current)];
                [fillId, outlineId, lineId, layerId].forEach(lid => {
                    if (map.getLayer(lid)) { try { map.setFilter(lid, withinFilter); } catch (_) {} }
                });
                ensureAOIOnTop(map);
            }
        } else {
            // First time loading — fetch from backend
            setLoading(`Loading ${layer.label}...`);
            try {
                let paintOverrides;
                if (layer.isBoundary) {
                    paintOverrides = {
                        fill: { "fill-color": "transparent", "fill-opacity": 0 },
                        outline: { "line-color": layer.color || "#7B2D8E", "line-width": 3 },
                        line: { "line-color": layer.color || "#7B2D8E", "line-width": 3 },
                    };
                } else if (layer.isRoad) {
                    paintOverrides = {
                        fill: { "fill-color": layer.color || "#EAB308", "fill-opacity": 1 },
                        outline: { "line-color": layer.color || "#EAB308", "line-width": 2 },
                        line: { "line-color": layer.color || "#EAB308", "line-width": 2 },
                    };
                } else if (layer.isBuilding) {
                    paintOverrides = {
                        fill: { "fill-color": layer.color || "#DC2626", "fill-opacity": 0.6 },
                        outline: { "line-color": layer.color || "#DC2626", "line-width": 1.5 },
                        line: { "line-color": layer.color || "#DC2626", "line-width": 2 },
                        circle: { "circle-color": layer.color || "#DC2626" },
                    };
                } else {
                    paintOverrides = {
                        fill: { "fill-color": layer.color, "fill-opacity": 0.6 },
                        outline: { "line-color": layer.color, "line-width": 1.5 },
                        line: { "line-color": layer.color, "line-width": 2 },
                        circle: { "circle-color": layer.color },
                    };
                }

                if (dist.dataSource === "local") {
                    await addLocalGeoJSONLayer(map, {
                        id: layerId,
                        district: dist.districtKey || dist.name.toLowerCase(),
                        layer: layer.name,
                        fit: false,
                        paintOverrides,
                        onProgress: (loaded, total) => {
                            if (total > 2000) {
                                setLoading(`Loading ${layer.label}: ${loaded.toLocaleString()} / ${total.toLocaleString()} features...`);
                            }
                        },
                    });
                } else {
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
                }
                activeLayerIdsRef.current.add(layerId);
                activeLayerConfigsRef.current.push({ id: layerId, district: dist.districtKey || dist.name.toLowerCase(), layer: layer.name });
                setActiveLayers(prev => ({ ...prev, [layerId]: true }));

                // Apply AOI filter to newly loaded layer if AOI is active,
                // and keep the clip mask above it so data stays inside the boundary.
                if (aoiActive && aoiFeaturesRef.current) {
                    const withinFilter = ['within', unionGeometry(aoiFeaturesRef.current)];
                    [fillId, outlineId, lineId, layerId].forEach(lid => {
                        if (map.getLayer(lid)) {
                            try { map.setFilter(lid, withinFilter); } catch (_) {}
                        }
                    });
                    ensureAOIOnTop(map);
                }

                // Click popup
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
    }, [activeLayers, aoiActive, aoiFeatures]);

    // ── Local LULC Mask Functions ──
    const MASK_SOURCE_ID = 'local-mask-source';
    const MASK_LAYER_ID = 'local-mask-layer';

    const removeMask = useCallback((map) => {
        if (!map) return;
        if (map.getLayer(MASK_LAYER_ID)) map.removeLayer(MASK_LAYER_ID);
        if (map.getSource(MASK_SOURCE_ID)) map.removeSource(MASK_SOURCE_ID);
        maskLoadedRef.current = false;
    }, []);

    const updateMask = useCallback(async (map) => {
        if (!selectedDistrict || !map) return;
        const dist = (DISTRICTS[selectedState] || []).find(d => d.name === selectedDistrict);
        if (!dist || !dist.hasMask) return;

        const distKey = dist.districtKey || dist.name.toLowerCase();

        // Use API_BASE from our mapLayers util
        const { API_BASE } = await import("../utils/mapLayers");
        const tileUrl = `${API_BASE}/api/districts/${encodeURIComponent(distKey)}/raster/tiles/{z}/{x}/{y}.png`;

        if (!map.getSource(MASK_SOURCE_ID)) {
            map.addSource(MASK_SOURCE_ID, {
                type: 'raster',
                tiles: [tileUrl],
                tileSize: 256,
            });
        } else {
            // Force refresh of tiles if district changed
            map.getSource(MASK_SOURCE_ID).tiles = [tileUrl];
            map.style.sourceCaches[MASK_SOURCE_ID].clearTiles();
            map.style.sourceCaches[MASK_SOURCE_ID].update(map.transform);
        }

        if (!map.getLayer(MASK_LAYER_ID)) {
            const layerDef = {
                id: MASK_LAYER_ID,
                type: 'raster',
                source: MASK_SOURCE_ID,
                paint: {
                    'raster-opacity': 0.7,
                    'raster-fade-duration': 300,
                    'raster-resampling': 'nearest'
                }
            };

            // Put it below the roads/boundaries
            try {
                const style = map.getStyle();
                const firstLabelOrLine = style.layers.find(l => l.type === 'symbol' || l.type === 'line' || (l.id && l.id.includes('label')));
                if (firstLabelOrLine) map.addLayer(layerDef, firstLabelOrLine.id);
                else map.addLayer(layerDef);
            } catch (e) {
                map.addLayer(layerDef);
            }
        }

        maskLoadedRef.current = true;
        // Keep the AOI mask/outline above any newly added raster so clipping holds.
        if (aoiFeaturesRef.current) ensureAOIOnTop(map);
    }, [selectedState, selectedDistrict]);

    const toggleMask = useCallback(() => {
        const map = mapRef.current;
        if (!map) return;
        if (maskOn) {
            if (map.getLayer(MASK_LAYER_ID)) map.setLayoutProperty(MASK_LAYER_ID, "visibility", "none");
            maskOnRef.current = false;
            setMaskOn(false);
        } else {
            maskOnRef.current = true;
            setMaskOn(true);
            if (!maskLoadedRef.current) updateMask(map);
            else {
                if (map.getLayer(MASK_LAYER_ID)) map.setLayoutProperty(MASK_LAYER_ID, "visibility", "visible");
                if (aoiFeaturesRef.current) ensureAOIOnTop(map);
            }
        }
    }, [maskOn, updateMask]);

    // ── Toggle Drone Imagery ──
    const getDroneTargetLevel = (zoom) => {
        if (zoom < 13) return 2;
        if (zoom < 14.5) return 3;
        return 4;
    };

    const applyDroneLayerToMap = (map, url, coordinates) => {
        const sourceId = "drone-source";
        const layerId = "drone-layer";
        if (map.getSource(sourceId)) {
            map.getSource(sourceId).updateImage({ url, coordinates });
        } else {
            map.addSource(sourceId, { type: "image", url, coordinates });
            let firstFeatureId = null;
            for (const activeLid of activeLayerIdsRef.current) {
                firstFeatureId = map.getLayer(`${activeLid}-fill`) ? `${activeLid}-fill` : map.getLayer(`${activeLid}-line`) ? `${activeLid}-line` : activeLid;
                if (firstFeatureId) break;
            }
            map.addLayer({ id: layerId, type: "raster", source: sourceId, paint: { "raster-opacity": 1.0, "raster-resampling": "nearest" } }, firstFeatureId || undefined);
        }
        if (map.getLayer(layerId)) map.setLayoutProperty(layerId, "visibility", "visible");
        // Keep AOI clip above the drone raster.
        if (aoiFeaturesRef.current) ensureAOIOnTop(map);
    };

    const updateDroneState = useCallback((map) => {
        if (!droneOn) return;
        const dist = (DISTRICTS[selectedState] || []).find(d => d.name === selectedDistrict);
        if (!dist?.droneImagery) return;

        const zoom = map.getZoom();
        const targetLevel = getDroneTargetLevel(zoom);

        const buildNext = (levelToBuild) => {
            droneBuildingRef.current = true;
            setLoading(`Decoding ArcGIS tiles (LOD ${levelToBuild})...`);
            buildDroneForLevel(dist.droneImagery, levelToBuild, pct => setLoading(`Decoding ArcGIS tiles (LOD ${levelToBuild})... ${pct}%`))
                .then(result => {
                    droneCacheRef.current.set(result.level, result);
                    if (droneOn) applyDroneLayerToMap(mapRef.current, result.dataUrl, result.coordinates);
                    droneCurrentLevelRef.current = result.level;
                    droneLoadedRef.current = true;
                })
                .catch(err => console.error("Drone failed:", err))
                .finally(() => {
                    droneBuildingRef.current = false;
                    setLoading(null);
                    const pending = dronePendingLevelRef.current;
                    dronePendingLevelRef.current = null;
                    if (pending != null && pending !== levelToBuild && droneOn) {
                        const cached = droneCacheRef.current.get(pending);
                        if (cached) {
                            applyDroneLayerToMap(mapRef.current, cached.dataUrl, cached.coordinates);
                            droneCurrentLevelRef.current = pending;
                        } else {
                            setTimeout(() => { if (droneOn && !droneBuildingRef.current) buildNext(pending); }, 0);
                        }
                    }
                });
        };

        const cached = droneCacheRef.current.get(targetLevel);
        if (cached) { applyDroneLayerToMap(map, cached.dataUrl, cached.coordinates); droneCurrentLevelRef.current = targetLevel; return; }
        if (droneBuildingRef.current) { dronePendingLevelRef.current = targetLevel; return; }
        buildNext(targetLevel);
    }, [droneOn, selectedState, selectedDistrict]);

    const toggleDrone = useCallback(() => {
        const map = mapRef.current;
        if (!map || !selectedDistrict) return;
        if (droneOn) {
            if (map.getLayer("drone-layer")) map.setLayoutProperty("drone-layer", "visibility", "none");
            setDroneOn(false);
        } else {
            setDroneOn(true);
            setTimeout(() => {
                updateDroneState(map);
                if (!droneLoadedRef.current) {
                    map.fitBounds([
                        [VIJAYAWADA_DRONE_EXTENT.xmin, VIJAYAWADA_DRONE_EXTENT.ymin],
                        [VIJAYAWADA_DRONE_EXTENT.xmax, VIJAYAWADA_DRONE_EXTENT.ymax]
                    ], { padding: 40, duration: 2000 });
                }
            }, 0);
        }
    }, [droneOn, selectedDistrict, updateDroneState, selectedState]);

    // Keep the persistent map handlers (moveend/zoomend) pointed at the LATEST
    // updateMask/updateDroneState without re-creating the map.
    useEffect(() => {
        updateMaskRef.current = updateMask;
        updateDroneStateRef.current = updateDroneState;
        renderAOIRef.current = renderAOILayers;
    });

    // ═══════════════════════════════════════════════════════════════
    // ── AOI (Area of Interest) Logic ──
    // ═══════════════════════════════════════════════════════════════
    const AOI_SOURCE = 'aoi-boundary-source';
    const AOI_OUTLINE_LAYER = 'aoi-boundary-outline';
    const AOI_FILL_LAYER = 'aoi-boundary-fill';
    const AOI_MASK_SOURCE = 'aoi-raster-mask-src';
    const AOI_MASK_LAYER = 'aoi-raster-mask-layer';
    const AOI_HIGHLIGHT_LAYER = 'aoi-boundary-highlight';
    const AOI_LABEL_LAYER = 'aoi-boundary-label';

    // Stack order (bottom→top): fill < clip mask < outline < selected-parcel highlight < labels.
    // Called after any new layer is added so clipping survives layer toggles.
    const ensureAOIOnTop = useCallback((map) => {
        if (!map) return;
        [AOI_FILL_LAYER, AOI_MASK_LAYER, AOI_OUTLINE_LAYER, AOI_HIGHLIGHT_LAYER, AOI_LABEL_LAYER].forEach(id => {
            if (map.getLayer(id)) { try { map.moveLayer(id); } catch (_) { } }
        });
    }, []);

    // ── Apply raster mask: hides everything OUTSIDE every AOI polygon ──
    // Inverse-polygon overlay with one hole PER AOI polygon — the universal clip
    // that visually hides BOTH raster and vector data outside the boundaries.
    const applyAOIRasterMask = useCallback((map, features) => {
        const outer = [[-180, -85], [180, -85], [180, 85], [-180, 85], [-180, -85]];
        const ringArea = (ring) => {
            let sum = 0;
            for (let i = 0; i < ring.length - 1; i++) {
                sum += (ring[i + 1][0] - ring[i][0]) * (ring[i + 1][1] + ring[i][1]);
            }
            return sum;
        };
        const isCCW = (ring) => ringArea(ring) < 0;
        const outerRing = isCCW(outer) ? outer : [...outer].reverse();
        const holes = features.map(f => {
            const hole = f.geometry.coordinates[0];
            return isCCW(hole) ? [...hole].reverse() : hole; // holes must wind CW
        });

        const maskFeature = {
            type: 'Feature', properties: {},
            geometry: { type: 'Polygon', coordinates: [outerRing, ...holes] }
        };
        const fc = { type: 'FeatureCollection', features: [maskFeature] };

        if (map.getSource(AOI_MASK_SOURCE)) {
            map.getSource(AOI_MASK_SOURCE).setData(fc);
        } else {
            map.addSource(AOI_MASK_SOURCE, { type: 'geojson', data: fc });
        }
        if (!map.getLayer(AOI_MASK_LAYER)) {
            map.addLayer({
                id: AOI_MASK_LAYER, type: 'fill', source: AOI_MASK_SOURCE,
                paint: { 'fill-color': '#0a0e17', 'fill-opacity': 0.96 }
            });
        }
    }, []);

    // ── Apply vector filter: hide individual features outside the AOI (points/lines) ──
    const applyAOIVectorFilter = useCallback((map, features) => {
        if (!features?.length || !map) return;
        const withinFilter = ['within', unionGeometry(features)];
        activeLayerIdsRef.current.forEach(layerId => {
            [`${layerId}-fill`, `${layerId}-outline`, `${layerId}-line`, layerId].forEach(lid => {
                if (map.getLayer(lid)) {
                    try { map.setFilter(lid, withinFilter); } catch (e) { /* `within` unsupported for some geoms — mask still clips visually */ }
                }
            });
        });
    }, []);

    // ── Click a polygon (multi-polygon AOIs) to select it for per-parcel stats ──
    const onAOIParcelClick = useCallback((e) => {
        if (!e.features?.length) return;
        const idx1 = e.features[0].properties?._idx;
        if (idx1 == null) return;
        const feats = aoiFeaturesRef.current || [];
        if (feats.length <= 1) return; // single polygon — nothing to pick
        setSelectedParcel(prev => (prev === idx1 - 1 ? null : idx1 - 1)); // toggle
        setStatsOpen(true);
    }, []);

    // ── Render AOI layers (fill + clip mask + outline + selected highlight + #labels) WITHOUT flying ──
    // Reused on first apply AND when the map is rebuilt after a base-map change.
    const renderAOILayers = useCallback((map, features) => {
        if (!map || !features?.length) return;
        // Each polygon becomes a numbered feature so multi-polygon AOIs are distinguishable.
        const fc = {
            type: 'FeatureCollection',
            features: features.map((f, i) => ({
                type: 'Feature',
                properties: { _idx: i + 1 },
                geometry: f.geometry,
            }))
        };

        if (map.getSource(AOI_SOURCE)) map.getSource(AOI_SOURCE).setData(fc);
        else map.addSource(AOI_SOURCE, { type: 'geojson', data: fc });

        if (!map.getLayer(AOI_FILL_LAYER)) {
            map.addLayer({
                id: AOI_FILL_LAYER, type: 'fill', source: AOI_SOURCE,
                paint: { 'fill-color': '#14b8a6', 'fill-opacity': 0.05 }
            });
        }
        applyAOIRasterMask(map, features);
        if (!map.getLayer(AOI_OUTLINE_LAYER)) {
            map.addLayer({
                id: AOI_OUTLINE_LAYER, type: 'line', source: AOI_SOURCE,
                paint: { 'line-color': '#14b8a6', 'line-width': 2.5, 'line-dasharray': [4, 2] }
            });
        }
        // Solid highlight for the currently selected parcel (filter set via effect)
        if (!map.getLayer(AOI_HIGHLIGHT_LAYER)) {
            map.addLayer({
                id: AOI_HIGHLIGHT_LAYER, type: 'line', source: AOI_SOURCE,
                filter: ['==', ['get', '_idx'], -1],
                paint: { 'line-color': '#fbbf24', 'line-width': 4 }
            });
        }
        // Bind the parcel-click handler once per map instance (multi-polygon picking)
        if (aoiClickBoundRef.current !== map) {
            map.on('click', AOI_FILL_LAYER, onAOIParcelClick);
            map.on('mouseenter', AOI_FILL_LAYER, () => { if ((aoiFeaturesRef.current || []).length > 1) map.getCanvas().style.cursor = 'pointer'; });
            map.on('mouseleave', AOI_FILL_LAYER, () => { map.getCanvas().style.cursor = ''; });
            aoiClickBoundRef.current = map;
        }
        // Numbered badge at each polygon (only useful when there are several)
        if (!map.getLayer(AOI_LABEL_LAYER)) {
            map.addLayer({
                id: AOI_LABEL_LAYER, type: 'symbol', source: AOI_SOURCE,
                layout: {
                    'text-field': features.length > 1 ? ['to-string', ['get', '_idx']] : '',
                    'text-size': 13, 'text-font': ['DIN Offc Pro Bold', 'Arial Unicode MS Bold'],
                    'text-allow-overlap': true,
                },
                paint: { 'text-color': '#ffffff', 'text-halo-color': '#0a0e17', 'text-halo-width': 1.5 }
            });
        } else {
            map.setLayoutProperty(AOI_LABEL_LAYER, 'text-field', features.length > 1 ? ['to-string', ['get', '_idx']] : '');
        }
        ensureAOIOnTop(map);
    }, [applyAOIRasterMask, ensureAOIOnTop, onAOIParcelClick]);

    // Keep the selected-parcel highlight in sync with state
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !map.getLayer(AOI_HIGHLIGHT_LAYER)) return;
        const filter = selectedParcel == null ? ['==', ['get', '_idx'], -1] : ['==', ['get', '_idx'], selectedParcel + 1];
        try { map.setFilter(AOI_HIGHLIGHT_LAYER, filter); } catch (_) { }
    }, [selectedParcel, aoiFeatures]);

    // Select a parcel from the stats list and fly to it
    const focusParcel = useCallback((idx) => {
        setSelectedParcel(prev => (prev === idx ? null : idx));
        const f = (aoiFeaturesRef.current || [])[idx];
        const map = mapRef.current;
        if (f && map) {
            const b = getFeaturesBounds([f]);
            map.fitBounds([[b.minLng, b.minLat], [b.maxLng, b.maxLat]], { padding: 80, duration: 800 });
        }
    }, []);

    // ── Look up a district's boundary geometry (cached) for auto-detection ──
    const getDistrictBoundaryGeom = useCallback(async (dist) => {
        const key = dist.districtKey || dist.name.toLowerCase();
        if (key in districtBoundaryCacheRef.current) return districtBoundaryCacheRef.current[key];
        let geom = null;
        try {
            const { API_BASE } = await import("../utils/mapLayers");
            const resp = await fetch(`${API_BASE}/api/districts/${encodeURIComponent(key)}/boundary`);
            const data = await resp.json();
            if (data?.features?.length && data.features[0].geometry) geom = data.features[0].geometry;
        } catch (_) { /* ignore — leave null */ }
        districtBoundaryCacheRef.current[key] = geom;
        return geom;
    }, []);

    // Ray-cast point-in-geometry for Polygon / MultiPolygon district boundaries
    const pointInGeometry = (lng, lat, geom) => {
        const inRing = (ring) => {
            let inside = false;
            for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
                const xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
                if (((yi > lat) !== (yj > lat)) && (lng < (xj - xi) * (lat - yi) / (yj - yi) + xi)) inside = !inside;
            }
            return inside;
        };
        const inPoly = (poly) => poly.length && inRing(poly[0]) && !poly.slice(1).some(h => inRing(h));
        if (geom.type === 'Polygon') return inPoly(geom.coordinates);
        if (geom.type === 'MultiPolygon') return geom.coordinates.some(inPoly);
        return false;
    };

    // ── Find which configured district the AOI sits in (null if none) ──
    const detectDistrictForFeatures = useCallback(async (features) => {
        const c = polygonCentroid(features[0]);
        for (const dist of (DISTRICTS[selectedState] || [])) {
            const geom = await getDistrictBoundaryGeom(dist);
            if (geom && pointInGeometry(c[0], c[1], geom)) return dist.name;
        }
        return null;
    }, [selectedState, getDistrictBoundaryGeom]);

    // ── Compute building/water/road stats inside the AOI and open the popup ──
    const refreshAOIStats = useCallback(async (features, districtName) => {
        const dist = (DISTRICTS[selectedState] || []).find(d => d.name === districtName);
        if (!dist || !features?.length) { setAoiStats(null); return; }
        const key = dist.districtKey || dist.name.toLowerCase();

        // Cancel any in-flight analysis (e.g. user redrew the AOI)
        if (statsAbortRef.current) statsAbortRef.current.abort();
        const controller = new AbortController();
        statsAbortRef.current = controller;

        setStatsOpen(true);
        setStatsLoading(true);
        try {
            const stats = await computeAOIStats(key, features, controller.signal);
            if (controller.signal.aborted) return; // a newer request superseded this one
            setAoiStats(stats);
        } catch (e) {
            if (e?.name === 'AbortError') return;  // superseded — ignore
            console.error('AOI stats failed:', e);
            setAoiStats(null);
        } finally {
            if (statsAbortRef.current === controller) {
                statsAbortRef.current = null;
                setStatsLoading(false);
            }
        }
    }, [selectedState]);

    // Toggle the stats popup; when opening, (re)compute for the current district
    // (cached, so it's instant if already fetched). Handles the case where the
    // district was picked AFTER the AOI was drawn.
    const toggleStats = useCallback(() => {
        if (statsOpen) { setStatsOpen(false); return; }
        const feats = aoiFeaturesRef.current;
        if (feats?.length && selectedDistrict) refreshAOIStats(feats, selectedDistrict);
        else setStatsOpen(true); // show empty-state guidance
    }, [statsOpen, selectedDistrict, refreshAOIStats]);

    // ── Apply an AOI (array of polygons) to the map (renders + flies + filters + analyses) ──
    const applyAOI = useCallback((features) => {
        const map = mapRef.current;
        const list = Array.isArray(features) ? features : [features];
        if (!map || !list.length) return;

        setAoiFeatures(list);
        setAoiActive(true);
        setSelectedParcel(null);
        aoiFeaturesRef.current = list; // persist across style rebuilds

        if (drawRef.current) {
            try { map.removeControl(drawRef.current); } catch (_) { }
            drawRef.current = null;
            setDrawMode(false);
        }

        renderAOILayers(map, list);

        const bounds = getFeaturesBounds(list);
        map.fitBounds(
            [[bounds.minLng, bounds.minLat], [bounds.maxLng, bounds.maxLat]],
            { padding: 60, duration: 1500 }
        );

        applyAOIVectorFilter(map, list);

        // Auto-detect the district the AOI falls in, switch to it (without flying
        // away from the AOI), then compute analytics for that district's data.
        (async () => {
            let districtName = selectedDistrict;
            try {
                const detected = await detectDistrictForFeatures(list);
                if (detected && detected !== selectedDistrict) {
                    districtName = detected;
                    handleDistrictSelect(detected, false); // switch, stay on AOI
                    // Re-assert AOI on top after the district switch cleared old layers
                    setTimeout(() => { if (mapRef.current) ensureAOIOnTop(mapRef.current); }, 0);
                }
            } catch (_) { /* detection is best-effort */ }
            if (districtName) refreshAOIStats(list, districtName);
        })();
    }, [renderAOILayers, applyAOIVectorFilter, selectedDistrict, detectDistrictForFeatures, handleDistrictSelect, refreshAOIStats, ensureAOIOnTop]);

    // ── Start draw mode ──
    const startDrawAOI = useCallback(() => {
        const map = mapRef.current;
        if (!map) return;

        // Initialize MapboxDraw if not already
        if (!drawRef.current) {
            const draw = new MapboxDraw({
                displayControlsDefault: false,
                controls: {},
                defaultMode: 'draw_polygon',
                styles: [
                    // Polygon fill
                    { id: 'gl-draw-polygon-fill', type: 'fill', filter: ['all', ['==', '$type', 'Polygon']], paint: { 'fill-color': '#0B5FA5', 'fill-opacity': 0.15 } },
                    // Polygon outline
                    { id: 'gl-draw-polygon-stroke', type: 'line', filter: ['all', ['==', '$type', 'Polygon']], paint: { 'line-color': '#0B5FA5', 'line-width': 2, 'line-dasharray': [3, 2] } },
                    // Vertex points
                    { id: 'gl-draw-point', type: 'circle', filter: ['all', ['==', '$type', 'Point']], paint: { 'circle-radius': 5, 'circle-color': '#0B5FA5' } },
                    // Line while drawing
                    { id: 'gl-draw-line', type: 'line', filter: ['all', ['==', '$type', 'LineString']], paint: { 'line-color': '#0B5FA5', 'line-width': 2, 'line-dasharray': [3, 2] } },
                ]
            });
            map.addControl(draw);
            drawRef.current = draw;

            // Listen for draw.create event
            map.on('draw.create', (e) => {
                const feature = e.features[0];
                if (feature && feature.geometry.type === 'Polygon') {
                    // Remove from draw control and apply as AOI
                    draw.deleteAll();
                    map.removeControl(draw);
                    drawRef.current = null;
                    applyAOI(feature);
                    setDrawMode(false);
                }
            });
        } else {
            drawRef.current.changeMode('draw_polygon');
        }

        setDrawMode(true);
    }, []);

    // ── Upload AOI file ──
    const handleAOIUpload = useCallback(async (e) => {
        const file = e.target.files?.[0];
        if (!file) return;
        e.target.value = ''; // Reset input

        setLoading(`Parsing ${file.name}...`);
        try {
            const features = await parseAOIFile(file); // array of polygon features
            applyAOI(features);
        } catch (err) {
            console.error('AOI parse error:', err);
            alert(`Failed to parse AOI file: ${err.message}`);
        }
        setLoading(null);
    }, []);

    // ── Clear AOI ──
    const clearAOI = useCallback(() => {
        const map = mapRef.current;
        if (!map) return;

        aoiFeaturesRef.current = null;

        // Remove draw control
        if (drawRef.current) {
            try { map.removeControl(drawRef.current); } catch (_) {}
            drawRef.current = null;
        }

        // Stop any in-flight analysis
        if (statsAbortRef.current) { statsAbortRef.current.abort(); statsAbortRef.current = null; }

        // Remove AOI boundary layers
        [AOI_FILL_LAYER, AOI_OUTLINE_LAYER, AOI_HIGHLIGHT_LAYER, AOI_LABEL_LAYER].forEach(lid => {
            if (map.getLayer(lid)) try { map.removeLayer(lid); } catch (_) {}
        });
        if (map.getSource(AOI_SOURCE)) try { map.removeSource(AOI_SOURCE); } catch (_) {}

        // Remove raster mask
        if (map.getLayer(AOI_MASK_LAYER)) try { map.removeLayer(AOI_MASK_LAYER); } catch (_) {}
        if (map.getSource(AOI_MASK_SOURCE)) try { map.removeSource(AOI_MASK_SOURCE); } catch (_) {}

        // Remove vector filters (show all features again)
        activeLayerIdsRef.current.forEach(layerId => {
            const fillId = `${layerId}-fill`;
            const outlineId = `${layerId}-outline`;
            const lineId = `${layerId}-line`;

            [fillId, outlineId, lineId, layerId].forEach(lid => {
                if (map.getLayer(lid)) {
                    try { map.setFilter(lid, null); } catch (_) {}
                }
            });
        });

        setAoiFeatures(null);
        setAoiActive(false);
        setDrawMode(false);
        setAoiStats(null);
        setStatsOpen(false);
        setSelectedParcel(null);
    }, []);

    // ── Cancel drawing without applying ──
    const cancelDraw = useCallback(() => {
        const map = mapRef.current;
        if (!map) return;
        if (drawRef.current) {
            drawRef.current.deleteAll();
            try { map.removeControl(drawRef.current); } catch (_) {}
            drawRef.current = null;
        }
        setDrawMode(false);
    }, []);

    // Get current district config
    const currentDist = (DISTRICTS[selectedState] || []).find(d => d.name === selectedDistrict);

    return (
        <div className="relative w-full h-full">
            {/* Map */}
            <div ref={mapContainerRef} style={{ position: "absolute", top: 0, left: 0, right: 0, bottom: 0 }} />

            {/* HUD Corners */}
            <div className="hud-corner hud-tl" />
            <div className="hud-corner hud-tr" />
            <div className="hud-corner hud-bl" />
            <div className="hud-corner hud-br" />


            {/* Panel toggle */}
            <button
                onClick={() => setPanelOpen(v => !v)}
                className="absolute top-3 left-3 z-20 bg-[var(--bg-secondary)]/90 text-white w-9 h-9 rounded-lg flex items-center justify-center shadow-lg hover:bg-[var(--bg-secondary)] transition-colors text-sm"
                title={panelOpen ? "Close panel" : "Open panel"}
            >
                {panelOpen ? "✕" : "☰"}
            </button>

            {/* Layer Panel */}
            {panelOpen && (
                <Draggable nodeRef={panelRef} handle=".drag-handle" bounds="parent">
                    <div ref={panelRef} className="absolute top-3 left-16 z-10 w-96 max-w-[calc(100vw-88px)] max-h-[calc(100%-24px)] bg-[var(--bg-primary)] backdrop-blur-md text-white rounded-xl shadow-2xl border border-[var(--border-default)] flex flex-col" style={{ boxShadow: '0 10px 30px rgba(0,0,0,0.6)' }}>
                        <div className="px-5 py-3.5 rounded-t-xl border-b border-[var(--border-default)] drag-handle cursor-move flex items-center justify-between bg-white/5 hover:bg-white/10 transition-colors">
                            <h3 className="text-[11px] font-bold tracking-[0.1em] uppercase text-[var(--text-primary)] select-none leading-none">LAYERS</h3>
                            <div className="flex gap-1">
                                <span className="w-1 h-1 rounded-full bg-[var(--text-muted)]" />
                                <span className="w-1 h-1 rounded-full bg-[var(--text-muted)]" />
                                <span className="w-1 h-1 rounded-full bg-[var(--text-muted)]" />
                            </div>
                        </div>

                        <div className="flex-1 overflow-y-auto min-h-0 rounded-b-xl">
                            {/* Base Map */}
                            <div className="px-5 py-4 border-b border-white/10 shrink-0">
                                <p className="text-[10px] uppercase tracking-[0.12em] text-white/50 mb-2.5">Base Map</p>
                                <div className="grid grid-cols-5 gap-1.5">
                                    {BASE_MAPS.map(bm => (
                                        <button
                                            key={bm.id}
                                            onClick={() => setBaseMap(bm.id)}
                                            className={`px-1 py-2 text-[10px] rounded-lg flex flex-col items-center gap-1 transition-colors ${baseMap === bm.id ? "bg-[var(--accent)] text-white" : "bg-white/10 text-white/70 hover:bg-white/20"
                                                }`}
                                        >
                                            <span className="text-sm leading-none">{bm.icon}</span>
                                            <span className="leading-none">{bm.label}</span>
                                        </button>
                                    ))}
                                </div>
                            </div>

                            {/* State selector */}
                            <div className="px-5 py-4 border-b border-[var(--border-default)] shrink-0">
                                <p className="text-[10px] uppercase tracking-[0.12em] text-[var(--text-muted)] mb-2">State</p>
                                <div className="relative">
                                    <select
                                        value={selectedState}
                                        onChange={e => { setSelectedState(e.target.value); setSelectedDistrict(null); }}
                                        className="w-full bg-[var(--bg-secondary)] text-[var(--text-primary)] text-[12px] rounded-lg px-3 py-2 border border-[var(--border-default)] focus:outline-none focus:border-[var(--accent)] appearance-none cursor-pointer"
                                    >
                                        {STATES.map(s => <option key={s.name} value={s.name} className="bg-[var(--bg-primary)]">{s.name}</option>)}
                                    </select>
                                    <div className="absolute inset-y-0 right-2 flex items-center pointer-events-none text-[var(--text-muted)]">
                                        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M6 9l6 6 6-6" /></svg>
                                    </div>
                                </div>
                            </div>

                            {/* District selector */}
                            <div className="px-5 py-4 border-b border-[var(--border-default)] shrink-0">
                                <p className="text-[10px] uppercase tracking-[0.12em] text-[var(--text-muted)] mb-2">District</p>
                                <div className="relative">
                                    <select
                                        value={selectedDistrict || ""}
                                        onChange={e => handleDistrictSelect(e.target.value)}
                                        className="w-full bg-[var(--bg-secondary)] text-[var(--text-primary)] text-[12px] rounded-lg px-3 py-2 border border-[var(--border-default)] focus:outline-none focus:border-[var(--accent)] appearance-none cursor-pointer"
                                    >
                                        <option value="" className="bg-[var(--bg-primary)] text-[var(--text-muted)]">Select District...</option>
                                        {(DISTRICTS[selectedState] || []).map(d => (
                                            <option key={d.name} value={d.name} className="bg-[var(--bg-primary)] text-[var(--text-primary)]">{d.name}</option>
                                        ))}
                                    </select>
                                    <div className="absolute inset-y-0 right-2 flex items-center pointer-events-none text-[var(--text-muted)]">
                                        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M6 9l6 6 6-6" /></svg>
                                    </div>
                                </div>
                            </div>

                            {/* District layers */}
                            {currentDist && (
                                <div className="px-5 py-4 border-b border-[var(--border-default)] shrink-0">
                                    <div className="flex items-center justify-between mb-2.5">
                                        <p className="text-[10px] uppercase tracking-[0.12em] text-[var(--text-muted)] truncate pr-2">{currentDist.name} Layers</p>
                                        <button
                                            onClick={() => mapRef.current?.flyTo({ center: currentDist.center, zoom: currentDist.zoom, duration: 1500 })}
                                            className="text-[9px] font-mono text-[var(--accent)] hover:text-white transition-colors"
                                        >
                                            ↗ FLY TO
                                        </button>
                                    </div>

                                    {/* Custom LULC toggle using Sentinel */}
                                    <label className="flex items-center justify-between py-2.5 px-3 rounded-lg hover:bg-white/5 cursor-pointer mb-2 border border-white/5 bg-white/5">
                                        <div className="flex items-center gap-2.5 min-w-0">
                                            <span className="text-sm shrink-0">🌍</span>
                                            <span className="text-[12px] font-semibold text-[var(--accent)] truncate">Land Covers (LULC)</span>
                                        </div>
                                        <div
                                            className={`w-8 h-4 rounded-full relative transition-colors ${showSentinel ? "bg-[var(--accent)]" : "bg-white/20"}`}
                                            onClick={() => setShowSentinel(!showSentinel)}
                                        >
                                            <div className={`absolute top-0.5 w-3 h-3 rounded-full bg-white shadow transition-all ${showSentinel ? "left-4.5" : "left-0.5"}`} />
                                        </div>
                                    </label>

                                    {/* Mask toggle */}
                                    {currentDist.hasMask && (
                                        <label className="flex items-center gap-3 py-2.5 px-3 rounded-lg hover:bg-white/5 cursor-pointer mb-1">
                                            <div className={`w-8 h-4 rounded-full relative transition-colors ${maskOn ? "bg-[var(--accent)]" : "bg-white/20"}`}
                                                onClick={toggleMask}
                                            >
                                                <div className={`absolute top-0.5 w-3 h-3 rounded-full bg-white shadow transition-all ${maskOn ? "left-4.5" : "left-0.5"}`} />
                                            </div>
                                            <span className="text-xs select-none">🎭 Land Use Mask</span>
                                        </label>
                                    )}

                                    {/* Drone Imagery toggle */}
                                    {currentDist.droneImagery && (
                                        <label
                                            className="flex items-center gap-3 py-2.5 px-3 rounded-lg hover:bg-white/5 cursor-pointer mb-1"
                                            onClick={(e) => { e.preventDefault(); toggleDrone(); }}
                                        >
                                            <div className={`w-8 h-4 rounded-full relative transition-colors ${droneOn ? "bg-[var(--accent)]" : "bg-white/20"}`}>
                                                <div className={`absolute top-0.5 w-3 h-3 rounded-full bg-white shadow transition-all ${droneOn ? "left-4.5" : "left-0.5"}`} />
                                            </div>
                                            <span className="text-xs select-none">🛩️ Drone Imagery</span>
                                        </label>
                                    )}

                                    {/* Feature layers */}
                                    {/* Layers List */}
                                    <div className="space-y-0.5">
                                        {currentDist.layers.map(layer => {
                                            const layerId = `${currentDist.name.toLowerCase()}-${layer.name}`;
                                            const isOn = activeLayers[layerId];
                                            return (
                                                <label key={layer.id} className="flex items-center gap-3 py-2.5 px-3 rounded-lg hover:bg-white/5 cursor-pointer">
                                                    <div
                                                        className={`w-8 h-4 rounded-full relative transition-colors shrink-0 ${isOn ? "bg-[var(--accent)]" : "bg-white/20"}`}
                                                        onClick={() => toggleLayer(currentDist, layer)}
                                                    >
                                                        <div className={`absolute top-0.5 w-3 h-3 rounded-full bg-white shadow transition-all ${isOn ? "left-4.5" : "left-0.5"}`} />
                                                    </div>
                                                    <div className="flex items-center gap-2.5 min-w-0">
                                                        {layer.color && <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ background: layer.color }} />}
                                                        <span className="text-[12px] select-none text-[var(--text-primary)] truncate">{layer.label}</span>
                                                    </div>
                                                </label>
                                            );
                                        })}
                                    </div>

                                    {currentDist.layers.length === 0 && (
                                        <p className="text-xs text-white/40 italic px-2 py-2">Layers coming soon for {currentDist.name}</p>
                                    )}
                                </div>
                            )}

                            {/* Change Detection (Semantic) Layers */}
                            <div className="px-5 py-4 border-b border-white/10 shrink-0">
                                <p className="text-[10px] uppercase tracking-[0.12em] text-white/50 mb-3">Change Detection (Semantic)</p>
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
                                        <label key={idx} className="flex items-center gap-2.5 py-2 px-2 rounded-lg hover:bg-white/5 cursor-not-allowed opacity-60">
                                            <div className="w-8 h-4 rounded-full relative bg-white/10 shrink-0">
                                                <div className="absolute top-0.5 left-0.5 w-3 h-3 rounded-full bg-white/30 shadow" />
                                            </div>
                                            <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ background: cd.color }} />
                                            <div className="flex flex-col flex-1 min-w-0">
                                                <span className="text-[11px] leading-snug select-none" title={cd.label}>{cd.label}</span>
                                                <span className="text-[9px] text-white/40 italic mt-0.5">— In Progress</span>
                                            </div>
                                        </label>
                                    ))}
                                </div>
                            </div>

                            {/* Legend */}
                            <div className="px-5 py-4 shrink-0">
                                <p className="text-[10px] uppercase tracking-[0.12em] text-white/50 mb-3">Legend</p>
                                <div className="space-y-2">
                                    <p className="text-[10px] text-white/40 uppercase tracking-[0.12em] mb-2">Layer Colors</p>
                                    <div className="grid grid-cols-2 gap-x-4 gap-y-3">
                                        {[
                                            { label: "Boundary", color: "#7B2D8E", hollow: true },
                                            { label: "Bldg (High)", color: "#DC2626" },
                                            { label: "Bldg (Med)", color: "#F97316" },
                                            { label: "Bldg (Low)", color: "#FBBF24" },
                                            { label: "Roads", color: "#EAB308", hollow: true },
                                            { label: "Water", color: "#3B82F6" },
                                            { label: "Open Areas", color: "#9CA3AF" },
                                        ].map(l => (
                                            <div key={l.label} className="flex items-center gap-2.5 min-w-0">
                                                <span className="w-3 h-3 rounded-sm shrink-0" style={{
                                                    background: l.hollow ? "transparent" : l.color,
                                                    border: l.hollow ? `2px solid ${l.color}` : `1px solid ${l.color}80`,
                                                }} />
                                                <span className="text-[12px] text-white/70 select-none truncate">{l.label}</span>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            </div>

                            {/* Sentinel LULC Legend */}
                            {showSentinel && (
                                <div className="px-5 py-4 border-t border-white/10 shrink-0 bg-[var(--bg-secondary)]/60">
                                    <p className="text-[10px] uppercase tracking-[0.12em] text-white/50 mb-3">LULC Categories</p>
                                    <div className="grid grid-cols-2 gap-y-3 gap-x-4">
                                        {LAND_COVER_LEGEND.map(lc => (
                                            <div key={lc.label} className="flex items-center gap-2.5">
                                                <span className="w-3 h-3 rounded-sm shrink-0" style={{ background: lc.color }} />
                                                <span className="text-xs text-white/80">{lc.label}</span>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            )}
                        </div>
                    </div>
                </Draggable>
            )}

            {/* ═══════════ AOI TOOLS — separate panel, top-right ═══════════ */}
            <div className="absolute top-3 right-3 z-20 w-80 max-w-[calc(100vw-24px)] rounded-xl bg-[var(--bg-primary)]/95 backdrop-blur-md text-white border border-[var(--border-default)] shadow-2xl" style={{ boxShadow: '0 10px 30px rgba(0,0,0,0.6)' }}>
                {/* Header */}
                <div className="px-4 py-3 border-b border-[var(--border-default)] bg-white/5 rounded-t-xl flex items-center gap-2.5">
                    <svg className="shrink-0" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10" /><circle cx="12" cy="12" r="6" /><circle cx="12" cy="12" r="2" /></svg>
                    <span className="text-[11px] font-bold tracking-[0.15em] uppercase text-[var(--text-primary)] select-none truncate">Area of Interest</span>
                </div>

                {/* Hidden file input for AOI upload */}
                <input
                    ref={aoiFileInputRef}
                    type="file"
                    accept=".geojson,.json,.shp,.zip,.kml,.gpkg"
                    onChange={handleAOIUpload}
                    className="hidden"
                />

                <div className="p-4">
                    {drawMode ? (
                        /* Drawing in progress */
                        <div className="space-y-2.5">
                            <div className="flex items-start gap-2.5 px-3 py-2.5 bg-[var(--accent)]/10 border border-[var(--accent)]/30 rounded-lg">
                                <div className="w-2 h-2 mt-1 rounded-full bg-[var(--accent)] animate-pulse shrink-0" />
                                <span className="text-[11px] leading-relaxed text-[var(--text-accent)]">Click on the map to add vertices, then double-click to finish.</span>
                            </div>
                            <button
                                onClick={cancelDraw}
                                className="w-full py-2 text-[11px] font-medium text-red-400 bg-red-500/10 border border-red-500/20 rounded-lg hover:bg-red-500/20 transition-colors"
                            >
                                Cancel Drawing
                            </button>
                        </div>
                    ) : aoiActive && aoiFeatures?.length ? (
                        /* AOI is active */
                        <div className="space-y-3">
                            <div className="flex items-center gap-2.5 px-3 py-2.5 bg-[var(--accent)]/10 border border-[var(--accent)]/30 rounded-lg">
                                <span className="text-base leading-none shrink-0">✅</span>
                                <div className="flex-1 min-w-0">
                                    <p className="text-[11px] font-semibold text-[var(--text-accent)] truncate">AOI Active · data clipped</p>
                                    <p className="text-[10px] text-[var(--text-secondary)] mt-0.5 truncate">
                                        {aoiFeatures.length > 1 ? `${aoiFeatures.length} polygons · ` : ''}{computeTotalAreaKm2(aoiFeatures)} km²
                                    </p>
                                </div>
                            </div>
                            <button
                                onClick={toggleStats}
                                className="w-full flex items-center justify-center gap-2 py-2 text-[11px] font-semibold text-[var(--text-accent)] bg-[var(--accent)]/10 border border-[var(--accent)]/25 rounded-lg hover:bg-[var(--accent)]/20 transition-colors"
                            >
                                <svg className="shrink-0" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 3v18h18" /><rect x="7" y="11" width="3" height="6" /><rect x="12" y="7" width="3" height="10" /><rect x="17" y="13" width="3" height="4" /></svg>
                                {statsLoading ? 'Analyzing…' : statsOpen ? 'Hide Statistics' : 'View Statistics'}
                            </button>
                            <div className="grid grid-cols-3 gap-2">
                                <button
                                    onClick={clearAOI}
                                    className="py-2 text-[11px] font-medium text-red-400 bg-red-500/10 border border-red-500/20 rounded-lg hover:bg-red-500/20 transition-colors"
                                >
                                    Clear
                                </button>
                                <button
                                    onClick={startDrawAOI}
                                    className="py-2 text-[11px] font-medium text-[var(--text-accent)] bg-[var(--accent)]/10 border border-[var(--accent)]/20 rounded-lg hover:bg-[var(--accent)]/20 transition-colors"
                                >
                                    Redraw
                                </button>
                                <button
                                    onClick={() => aoiFileInputRef.current?.click()}
                                    className="py-2 text-[11px] font-medium text-white/70 bg-white/5 border border-white/10 rounded-lg hover:bg-white/10 transition-colors"
                                >
                                    Upload
                                </button>
                            </div>
                        </div>
                    ) : (
                        /* No AOI — show draw/upload buttons */
                        <div className="space-y-3">
                            <div className="grid grid-cols-2 gap-2.5">
                                <button
                                    onClick={startDrawAOI}
                                    className="flex items-center justify-center gap-2 py-2.5 text-[11px] font-medium text-[var(--text-accent)] bg-[var(--accent)]/10 border border-[var(--accent)]/20 rounded-lg hover:bg-[var(--accent)]/20 transition-colors"
                                >
                                    <svg className="shrink-0" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M12 20h9" /><path d="M16.376 3.622a1 1 0 0 1 3.002 3.002L7.368 18.635a2 2 0 0 1-.855.506l-2.872.838a.5.5 0 0 1-.62-.62l.838-2.872a2 2 0 0 1 .506-.855z" /></svg>
                                    Draw AOI
                                </button>
                                <button
                                    onClick={() => aoiFileInputRef.current?.click()}
                                    className="flex items-center justify-center gap-2 py-2.5 text-[11px] font-medium text-white/80 bg-white/5 border border-white/10 rounded-lg hover:bg-white/10 transition-colors"
                                >
                                    <svg className="shrink-0" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="17 8 12 3 7 8" /><line x1="12" y1="3" x2="12" y2="15" /></svg>
                                    Upload
                                </button>
                            </div>
                            <div className="space-y-1.5 pt-0.5">
                                <p className="text-[10px] leading-relaxed text-[var(--text-secondary)]">
                                    Draw a polygon or upload a boundary file — data is then shown for that area only.
                                </p>
                                <p className="text-[10px] leading-relaxed text-[var(--text-muted)]">
                                    GeoJSON · Shapefile (.shp/.zip) · KML · GeoPackage (.gpkg)
                                </p>
                            </div>
                        </div>
                    )}
                </div>
            </div>

            {/* ═══════════ AOI STATISTICS — draggable popup ═══════════ */}
            {aoiActive && statsOpen && (
                <Draggable nodeRef={statsPanelRef} handle=".stats-drag-handle" bounds="parent">
                    <div ref={statsPanelRef} className="absolute top-64 right-3 z-30 w-80 max-w-[calc(100vw-24px)] rounded-xl bg-[var(--bg-primary)]/97 backdrop-blur-md text-white border border-[var(--border-default)] shadow-2xl flex flex-col max-h-[55vh]" style={{ boxShadow: '0 10px 30px rgba(0,0,0,0.6)' }}>
                        {/* Header (drag handle) */}
                        <div className="stats-drag-handle cursor-move px-4 py-3 rounded-t-xl border-b border-[var(--border-default)] bg-white/5 flex items-center justify-between gap-2">
                            <div className="flex items-center gap-2.5 min-w-0">
                                <svg className="shrink-0" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 3v18h18" /><rect x="7" y="11" width="3" height="6" /><rect x="12" y="7" width="3" height="10" /><rect x="17" y="13" width="3" height="4" /></svg>
                                <span className="text-[11px] font-bold tracking-[0.1em] uppercase text-[var(--text-primary)] select-none truncate">AOI Statistics</span>
                            </div>
                            <button onClick={() => setStatsOpen(false)} className="shrink-0 text-white/50 hover:text-white text-sm leading-none" title="Close">✕</button>
                        </div>

                        <div className="overflow-y-auto min-h-0 rounded-b-xl p-4 space-y-3">
                            {statsLoading ? (
                                <div className="flex items-center gap-2.5 py-6 justify-center text-[var(--text-secondary)]">
                                    <div className="w-4 h-4 border-2 border-white/30 border-t-[var(--accent)] rounded-full animate-spin" />
                                    <span className="text-[11px]">Analyzing AOI…</span>
                                </div>
                            ) : !aoiStats ? (
                                <p className="text-[11px] text-[var(--text-muted)] py-4 text-center leading-relaxed">
                                    No analytics available. Select a district that contains this AOI, or ensure the backend is online.
                                </p>
                            ) : (
                                (() => {
                                    const sel = selectedParcel != null ? aoiStats.perPolygon[selectedParcel] : null;
                                    const headline = sel || aoiStats.totals;
                                    return (
                                        <>
                                            {/* Headline cards — reflect the SELECTED parcel, else the total */}
                                            <div>
                                                <div className="flex items-center justify-between mb-2">
                                                    <p className="text-[10px] uppercase tracking-[0.12em] text-[var(--text-muted)] truncate">
                                                        {sel ? `Polygon ${selectedParcel + 1} (selected)` : `Total${aoiStats.perPolygon.length > 1 ? ` · ${aoiStats.perPolygon.length} polygons` : ''}`}
                                                    </p>
                                                    {sel && (
                                                        <button onClick={() => setSelectedParcel(null)} className="text-[9px] font-medium text-[var(--text-accent)] hover:text-white shrink-0">Show total</button>
                                                    )}
                                                </div>
                                                <div className="grid grid-cols-2 gap-2">
                                                    {[
                                                        { label: 'Buildings', value: headline.buildings.toLocaleString(), color: '#EF4444' },
                                                        { label: 'Waterbodies', value: headline.waterbodies.toLocaleString(), color: '#3B82F6' },
                                                        { label: 'Roads (km)', value: headline.roadKm.toLocaleString(), color: '#EAB308' },
                                                        { label: 'Area (km²)', value: headline.areaKm2.toLocaleString(), color: '#14b8a6' },
                                                    ].map(s => (
                                                        <div key={s.label} className="px-3 py-2.5 rounded-lg bg-white/5 border border-white/10">
                                                            <div className="flex items-center gap-1.5">
                                                                <span className="w-2 h-2 rounded-full shrink-0" style={{ background: s.color }} />
                                                                <span className="text-[9px] uppercase tracking-wide text-[var(--text-muted)] truncate">{s.label}</span>
                                                            </div>
                                                            <p className="text-[16px] font-bold text-[var(--text-primary)] mt-0.5 leading-none">{s.value}</p>
                                                        </div>
                                                    ))}
                                                </div>
                                            </div>

                                            {/* Per-polygon breakdown (clickable) — only when more than one */}
                                            {aoiStats.perPolygon.length > 1 && (
                                                <div>
                                                    <p className="text-[10px] uppercase tracking-[0.12em] text-[var(--text-muted)] mb-2">Parcels · tap to select</p>
                                                    <div className="space-y-1.5">
                                                        {aoiStats.perPolygon.map(p => {
                                                            const active = selectedParcel === p.index;
                                                            return (
                                                                <button
                                                                    key={p.index}
                                                                    onClick={() => focusParcel(p.index)}
                                                                    className={`w-full text-left px-3 py-2 rounded-lg border transition-colors ${active ? 'bg-[var(--accent)]/15 border-[var(--accent)]/50' : 'bg-white/5 border-white/10 hover:bg-white/10'}`}
                                                                >
                                                                    <div className="flex items-center justify-between mb-1">
                                                                        <span className={`text-[11px] font-semibold ${active ? 'text-[#fbbf24]' : 'text-[var(--text-accent)]'}`}>Polygon {p.index + 1}</span>
                                                                        <span className="text-[10px] text-[var(--text-muted)]">{p.areaKm2} km²</span>
                                                                    </div>
                                                                    <div className="flex items-center gap-3 text-[10px] text-[var(--text-secondary)]">
                                                                        <span><span className="text-[var(--text-primary)] font-semibold">{p.buildings.toLocaleString()}</span> bldgs</span>
                                                                        <span><span className="text-[var(--text-primary)] font-semibold">{p.waterbodies.toLocaleString()}</span> water</span>
                                                                        <span><span className="text-[var(--text-primary)] font-semibold">{p.roadKm}</span> km road</span>
                                                                    </div>
                                                                </button>
                                                            );
                                                        })}
                                                    </div>
                                                </div>
                                            )}

                                            <p className="text-[9px] text-[var(--text-muted)] italic leading-relaxed pt-0.5">
                                                {aoiStats.perPolygon.length > 1 ? 'Tap a parcel on the map or in the list to see its individual counts. ' : ''}
                                                Counts come from the district vector data; road length is the portion inside the boundary.
                                            </p>
                                        </>
                                    );
                                })()
                            )}
                        </div>
                    </div>
                </Draggable>
            )}

            {/* Detection overlay control (from Upload & Analysis) — draggable */}
            {detectionOverlay && (
                <Draggable nodeRef={detPanelRef} handle=".det-drag-handle" bounds="parent">
                <div ref={detPanelRef} className="absolute bottom-28 right-3 z-30 w-64 max-w-[calc(100vw-24px)] rounded-xl bg-[var(--bg-primary)]/95 backdrop-blur-md text-white border border-[var(--border-default)] shadow-2xl" style={{ boxShadow: '0 10px 30px rgba(0,0,0,0.6)' }}>
                    <div className="det-drag-handle cursor-move px-3.5 py-2.5 border-b border-[var(--border-default)] bg-white/5 rounded-t-xl flex items-center gap-2">
                        <span className="text-sm shrink-0">🗺️</span>
                        <span className="text-[11px] font-bold tracking-[0.1em] uppercase text-[var(--text-primary)] truncate flex-1 select-none">Detection Overlay</span>
                        <button onClick={removeDetectionOverlay} title="Remove overlay" className="text-red-400 hover:text-red-300 text-xs">✕</button>
                    </div>
                    <div className="p-3.5 space-y-2">
                        <p className="text-[10px] text-[var(--text-secondary)] truncate" title={detectionOverlay.name}>{detectionOverlay.name}</p>
                        <div>
                            <label className="text-[10px] text-[var(--text-muted)] flex items-center justify-between mb-1">
                                <span>Opacity</span><span>{Math.round(detectionOpacity * 100)}%</span>
                            </label>
                            <input type="range" min="0.1" max="1" step="0.05" value={detectionOpacity}
                                onChange={e => setDetectionOpacity(parseFloat(e.target.value))}
                                className="w-full h-1.5 rounded-lg appearance-none cursor-pointer accent-[var(--accent)] bg-white/15" />
                        </div>
                        <button
                            onClick={() => { const m = mapRef.current; const b = detectionOverlay.bounds; if (m && b) m.fitBounds([[b.west, b.south], [b.east, b.north]], { padding: 60, duration: 1200 }); }}
                            className="w-full py-1.5 text-[10px] font-medium text-[var(--text-accent)] bg-[var(--accent)]/10 border border-[var(--accent)]/20 rounded-md hover:bg-[var(--accent)]/20 transition-colors"
                        >
                            ↗ Zoom to overlay
                        </button>
                    </div>
                </div>
                </Draggable>
            )}

            {/* Loading indicator */}
            {loading && (
                <div className="absolute bottom-4 right-4 z-20 bg-[var(--bg-secondary)]/90 text-white px-4 py-2 rounded-lg shadow-lg flex items-center gap-2 text-xs">
                    <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                    {loading}
                </div>
            )}

            {/* Coordinates */}
            {coords && (
                <div className="absolute bottom-4 left-1/2 -translate-x-1/2 z-10 bg-[var(--bg-secondary)]/80 text-white/80 px-3 py-1 rounded-full text-[10px] backdrop-blur-sm">
                    {coords.lat}°N, {coords.lng}°E
                </div>
            )}
        </div>
    );
}
