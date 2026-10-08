import React, { useEffect, useRef, useState, useCallback } from "react";
import mapboxgl from "mapbox-gl";
import "mapbox-gl/dist/mapbox-gl.css";
import MapboxDraw from "@mapbox/mapbox-gl-draw";
import "@mapbox/mapbox-gl-draw/dist/mapbox-gl-draw.css";
import {
    BarChart3, CheckCircle2, ChevronDown, ChevronUp, Crosshair, GripVertical, Layers, PanelLeftClose, PanelLeftOpen,
    PenLine, Plane, Target, Upload as UploadIcon, X,
} from "lucide-react";
import Draggable from "react-draggable";
import { Button, Card, Divider, Empty, Pill, SectionHeader, Spinner, ToggleRow } from "../components/ui";
import { useBaseMap } from "../lib/mapPrefs";
import ServerImagery from "../components/ServerImagery";
import DroneGrid from "../components/DroneGrid";
import DroneLayers from "../components/DroneLayers";
import { useGpuStatus } from "../lib/modelApi";
import { addArcGISFeatureLayer, addLocalGeoJSONLayer, reloadVisibleLayers, removeLayerGroup } from "../utils/mapLayers";
import { parseAOIFile, getFeaturesBounds, computeTotalAreaKm2, unionGeometry, polygonCentroid } from "../utils/aoiUtils";
import { registerTour, unregisterTour } from "../tour/tourBus";
import { computeAOIStats, warmBackend } from "../utils/aoiStats";
import { logActivity } from "../lib/activityLog";
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
                { id: 0, name: "boundary", label: "Boundary", color: "#CCFF00", isBoundary: true },
                { id: 1, name: "buildings", label: "Buildings", isBuilding: true },
                { id: 2, name: "openareas", label: "Open Areas", color: "#FBBF24" },
                { id: 3, name: "roads", label: "Roads", color: "#22D3EE", isRoad: true },
                { id: 4, name: "waterbodies", label: "Waterbodies", color: "#38BDF8" },
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
                { id: 0, name: "boundary", label: "Boundary", color: "#CCFF00", isBoundary: true },
                { id: 1, name: "buildings", label: "Buildings", color: "#FF4FD8" },
                { id: 2, name: "openareas", label: "Open Areas", color: "#FBBF24" },
                { id: 3, name: "roads", label: "Roads", color: "#22D3EE", isRoad: true },
                { id: 4, name: "waterbodies", label: "Waterbodies", color: "#38BDF8" },
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
                { id: 0, name: "boundary", label: "Boundary", color: "#CCFF00", isBoundary: true },
                { id: 1, name: "buildings", label: "Buildings", color: "#FF4FD8" },
                { id: 2, name: "openareas", label: "Open Areas", color: "#FBBF24" },
                { id: 3, name: "roads", label: "Roads", color: "#22D3EE", isRoad: true },
                { id: 4, name: "waterbodies", label: "Waterbodies", color: "#38BDF8" },
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
                { id: 0, name: "boundary", label: "Boundary", color: "#CCFF00", isBoundary: true },
                { id: 1, name: "buildings", label: "Buildings", color: "#FF4FD8" },
                { id: 2, name: "openareas", label: "Open Areas", color: "#FBBF24" },
                { id: 3, name: "roads", label: "Roads", color: "#22D3EE", isRoad: true },
                { id: 4, name: "waterbodies", label: "Waterbodies", color: "#38BDF8" },
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
                { id: 0, name: "boundary", label: "Boundary", color: "#CCFF00", isBoundary: true },
                { id: 1, name: "buildings", label: "Buildings", color: "#FF4FD8" },
                { id: 2, name: "openareas", label: "Open Areas", color: "#FBBF24" },
                { id: 3, name: "roads", label: "Roads", color: "#22D3EE", isRoad: true },
                { id: 4, name: "waterbodies", label: "Waterbodies", color: "#38BDF8" },
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

const CHANGE_LAYERS = [
    { label: "New construction (open → building)", color: "#10B981" },
    { label: "Encroachment (water → building)", color: "#F43F5E" },
    { label: "Demolition / clearing (building → open)", color: "#8B5CF6" },
    { label: "New road / access (open → road)", color: "#F59E0B" },
    { label: "Monthly change summary", color: "#3B82F6" },
    { label: "Quarterly change summary", color: "#06B6D4" },
    { label: "Yearly change summary", color: "#EAB308" },
];

const LEGEND = [
    { label: "Boundary", color: "#CCFF00", hollow: true },
    { label: "Buildings", color: "#FF4FD8" },
    { label: "Roads", color: "#22D3EE" },
    { label: "Water", color: "#38BDF8" },
    { label: "Open areas", color: "#FBBF24" },
];

// Land-use mask colours come from the backend raster colormap — data, not chrome.
const MASK_LEGEND = [
    { label: "Bldg (high)", color: "#DC2626" },
    { label: "Bldg (med)", color: "#F97316" },
    { label: "Bldg (low)", color: "#FBBF24" },
    { label: "Roads", color: "#EAB308" },
    { label: "Water", color: "#3B82F6" },
    { label: "Open", color: "#9CA3AF" },
];

// Mirrors utils/mapLayers.js: below these zooms a layer is left empty (too many features).
const LAYER_MIN_ZOOM = { buildings: 13, roads: 11, waterbodies: 10 };

/* Sentinel's alert pulse, for a line: the glow swells three times, then settles. */
function pulseGlow(map, layerId, base = 0.3, peak = 0.85, ms = 2400) {
    if (!map?.getLayer(layerId) || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    const t0 = performance.now();
    const step = (t) => {
        if (!map.getLayer(layerId)) return;
        const p = Math.min(1, (t - t0) / ms);
        const wave = (1 - Math.cos(p * Math.PI * 6)) / 2;          // three swells
        try {
            map.setPaintProperty(layerId, "line-opacity-transition", { duration: 0 });
            map.setPaintProperty(layerId, "line-opacity", base + (peak - base) * wave * (1 - p * 0.4));
        } catch (_) { return; }
        if (p < 1) requestAnimationFrame(step);
        else try { map.setPaintProperty(layerId, "line-opacity", base); } catch (_) { }
    };
    requestAnimationFrame(step);
}

/* ── Opening descent: globe → Andhra Pradesh, once a session ── */
const AP_HOME = { center: [80.0, 15.9], zoom: 6.5 };
const SPACE = { center: [79.0, 19.0], zoom: 1.35 };
const ATMOSPHERE = {
    color: "rgba(140, 170, 220, 0.45)",
    "high-color": "rgba(30, 60, 120, 0.9)",
    "horizon-blend": 0.06,
    "space-color": "#03060c",
    "star-intensity": 0.35,
};
const DESCENT_KEY = "sp-descent-played";
function descentPending() {
    try {
        if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return false;
        return !sessionStorage.getItem(DESCENT_KEY);
    } catch { return false; }
}
const isNarrow = () => typeof window !== "undefined" && window.innerWidth < 760;

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
    const [baseMap, setBaseMap] = useBaseMap(); // switcher lives in the command bar
    const [loading, setLoading] = useState(null);
    const [coords, setCoords] = useState(null);
    const [mapZoom, setMapZoom] = useState(AP_HOME.zoom);
    // Left rail (layers): open on desktop, tucked away on phones.
    const [railOpen, setRailOpen] = useState(() => !isNarrow());
    // Floating AOI card: draggable, and can be minimised to its header.
    const [aoiMin, setAoiMin] = useState(() => isNarrow());
    const aoiCardRef = useRef(null);
    const statsCardRef = useRef(null);
    const detCardRef = useRef(null);
    const cameraRef = useRef(null); // survives base-map rebuilds so switching style keeps the view

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
    const statsAbortRef = useRef(null);                       // cancels stale stats requests on redraw

    // ── Detection overlay (from Upload & Analysis → "Plot on Map") ──
    const [detectionOverlay, setDetectionOverlay] = useState(null); // { url, bounds, name }
    const [detectionOpacity, setDetectionOpacity] = useState(0.85);
    const detectionOverlayRef = useRef(null);
    const addDetectionRef = useRef(null);
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
        // First visit this session: descend from the whole globe to the state —
        // it says where this is before saying what is in it.
        const descend = !cameraRef.current && descentPending();
        const cam = cameraRef.current || AP_HOME;
        const map = new mapboxgl.Map({
            container: mapContainerRef.current,
            style: `mapbox://styles/mapbox/${baseMap}`,
            center: descend ? SPACE.center : cam.center,
            zoom: descend ? SPACE.zoom : cam.zoom,
            bearing: cam.bearing || 0,
            pitch: cam.pitch || 0,
            projection: descend ? "globe" : undefined,
            antialias: true,
            attributionControl: false,
        });
        map.addControl(new mapboxgl.AttributionControl({ compact: true }), "bottom-left");
        // Dev-only handle for debugging layers from the console.
        if (import.meta.env.DEV) window.__SP_MAP__ = map;
        map.on("moveend", () => {
            const c = map.getCenter();
            cameraRef.current = { center: [c.lng, c.lat], zoom: map.getZoom(), bearing: map.getBearing(), pitch: map.getPitch() };
        });
        if (descend) {
            map.once("style.load", () => { try { map.setFog(ATMOSPHERE); } catch (_) { } });
            map.once("load", () => {
                try { sessionStorage.setItem(DESCENT_KEY, "1"); } catch (_) { }
                setTimeout(() => {
                    if (!mapContainerRef.current) return;
                    map.flyTo({ ...AP_HOME, pitch: 0, bearing: 0, duration: 5200, curve: 1.25, essential: true });
                    map.once("moveend", () => {
                        try { map.setFog(null); map.setProjection(null); } catch (_) { }
                    });
                }, 600);
            });
        }
        map.addControl(new mapboxgl.NavigationControl(), "bottom-right");
        map.addControl(new mapboxgl.ScaleControl({ unit: "metric" }), "bottom-right");
        map.on("mousemove", e => setCoords({ lng: e.lngLat.lng.toFixed(5), lat: e.lngLat.lat.toFixed(5) }));
        map.on("zoomend", () => setMapZoom(map.getZoom()));
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
            [`${layerId}-glow`, fillId, outlineId, lineId, layerId].forEach(lid => {
                if (map.getLayer(lid)) map.setLayoutProperty(lid, "visibility", "none");
            });
            activeLayerIdsRef.current.delete(layerId);
            activeLayerConfigsRef.current = activeLayerConfigsRef.current.filter(c => c.id !== layerId);
            setActiveLayers(prev => ({ ...prev, [layerId]: false }));
        } else if (alreadyLoaded) {
            // Already loaded — just show again (instant!)
            [`${layerId}-glow`, fillId, outlineId, lineId, layerId].forEach(lid => {
                if (map.getLayer(lid)) map.setLayoutProperty(lid, "visibility", "visible");
            });
            activeLayerIdsRef.current.add(layerId);
            activeLayerConfigsRef.current.push({ id: layerId, district: dist.districtKey || dist.name.toLowerCase(), layer: layer.name });
            setActiveLayers(prev => ({ ...prev, [layerId]: true }));
            // Re-apply AOI clip to the re-shown layer
            if (aoiActive && aoiFeaturesRef.current) {
                const withinFilter = ['within', unionGeometry(aoiFeaturesRef.current)];
                [`${layerId}-glow`, fillId, outlineId, lineId, layerId].forEach(lid => {
                    if (map.getLayer(lid)) { try { map.setFilter(lid, withinFilter); } catch (_) {} }
                });
                ensureAOIOnTop(map);
            }
        } else {
            // First time loading — fetch from backend
            setLoading(`Loading ${layer.label}...`);
            try {
                let paintOverrides;
                // Paint lifted from Sentinel's map/layers.ts: loud neon over the
                // imagery, a blurred glow under every edge that must be read.
                const zw = (a, b) => ["interpolate", ["linear"], ["zoom"], 8, a, 16, b];
                if (layer.isBoundary) {
                    // Sentinel's state line: electric lime, glow + crisp edge.
                    paintOverrides = {
                        glow: { "line-color": layer.color, "line-width": zw(7, 16), "line-opacity": 0.3, "line-blur": 6 },
                        fill: { "fill-color": layer.color, "fill-opacity": 0.03 },
                        outline: { "line-color": layer.color, "line-width": zw(1.8, 3.6), "line-opacity": 1 },
                        line: { "line-color": layer.color, "line-width": zw(1.8, 3.6) },
                    };
                } else if (layer.isRoad) {
                    // Sentinel's road/highway treatment: glow underneath, bright edge.
                    paintOverrides = {
                        glow: { "line-color": layer.color, "line-width": zw(3, 9), "line-opacity": 0.22, "line-blur": 3 },
                        fill: { "fill-color": layer.color, "fill-opacity": 0.55 },
                        outline: { "line-color": layer.color, "line-width": zw(0.5, 1.4), "line-opacity": 0.95 },
                        line: { "line-color": layer.color, "line-width": zw(0.9, 3.2) },
                    };
                } else {
                    // Areas (buildings, water, open ground): Sentinel's district fill.
                    paintOverrides = {
                        fill: { "fill-color": layer.color, "fill-opacity": layer.name === "buildings" ? 0.42 : 0.32 },
                        outline: { "line-color": layer.color, "line-width": zw(0.4, 1.2), "line-opacity": 0.9 },
                        line: { "line-color": layer.color, "line-width": 1.5 },
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
                if (layer.isBoundary) pulseGlow(map, `${layerId}-glow`);

                // Apply AOI filter to newly loaded layer if AOI is active,
                // and keep the clip mask above it so data stays inside the boundary.
                if (aoiActive && aoiFeaturesRef.current) {
                    const withinFilter = ['within', unionGeometry(aoiFeaturesRef.current)];
                    [`${layerId}-glow`, fillId, outlineId, lineId, layerId].forEach(lid => {
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
        // ?v bump busts old cached tiles (e.g. after the Visakhapatnam colormap fix)
        const tileUrl = `${API_BASE}/api/districts/${encodeURIComponent(distKey)}/raster/tiles/{z}/{x}/{y}.png?v=2`;

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
            [`${layerId}-glow`, `${layerId}-fill`, `${layerId}-outline`, `${layerId}-line`, layerId].forEach(lid => {
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
                paint: { 'fill-color': '#2DD4BF', 'fill-opacity': 0.06 }
            });
        }
        applyAOIRasterMask(map, features);
        if (!map.getLayer(AOI_OUTLINE_LAYER)) {
            map.addLayer({
                id: AOI_OUTLINE_LAYER, type: 'line', source: AOI_SOURCE,
                paint: { 'line-color': '#2DD4BF', 'line-width': 2.5, 'line-dasharray': [4, 2] }
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
            const t = stats?.totals || {};
            logActivity({
                type: "AOI Analysis", district: districtName,
                area: `${features.length > 1 ? `${features.length} parcels · ` : ""}${t.areaKm2 ?? "?"} km²`,
                status: "Completed",
                metrics: { Buildings: t.buildings ?? 0, "Water Bodies": t.waterbodies ?? 0, "Roads (km)": t.roadKm ?? 0, "Area (km²)": t.areaKm2 ?? 0 },
            });
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
            logActivity({ type: "Boundary Upload", district: selectedDistrict || null, area: file.name, status: "Completed", metrics: { Parcels: features.length } });
        } catch (err) {
            console.error('AOI parse error:', err);
            alert(`Failed to parse AOI file: ${err.message}`);
            logActivity({ type: "Boundary Upload", area: file.name, status: "Failed", meta: { error: String(err.message).slice(0, 160) } });
        }
        setLoading(null);
    }, [selectedDistrict]);

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

            [`${layerId}-glow`, fillId, outlineId, lineId, layerId].forEach(lid => {
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
    // Expose Mapping controls to the guided tour (re-register each render for fresh closures)
    useEffect(() => {
        registerTour("mapping", {
            setBaseMap: (id) => setBaseMap(id),
            selectDistrict: (n) => handleDistrictSelect(n),
            toggleBoundary: () => {
                const d = (DISTRICTS[selectedState] || []).find(x => x.name === selectedDistrict);
                const b = d?.layers.find(l => l.isBoundary || l.name === "boundary");
                if (d && b) toggleLayer(d, b);
            },
            toggleMask: () => toggleMask(),
            waitIdle: () => new Promise((res) => {
                const m = mapRef.current; if (!m) return res();
                let done = false; const f = () => { if (!done) { done = true; res(); } };
                m.once("idle", f); setTimeout(f, 3500);
            }),
            drawDemoAOI: (feat) => applyAOI([feat]),
            uploadParcels: async (file) => { const feats = await parseAOIFile(file); applyAOI(feats); },
            selectParcel: (i) => focusParcel(i),
        });
    });
    useEffect(() => () => unregisterTour("mapping"), []);

    // The map's column changes width when the rail collapses; keep the canvas in step.
    useEffect(() => {
        const el = mapContainerRef.current;
        if (!el || typeof ResizeObserver === "undefined") return;
        const ro = new ResizeObserver(() => mapRef.current?.resize());
        ro.observe(el);
        return () => ro.disconnect();
    }, []);

    // The walkthrough points at controls in both rails — make sure they are showing.
    useEffect(() => {
        const open = () => { setRailOpen(true); setAoiMin(false); };
        window.addEventListener("sp:tour", open);
        return () => window.removeEventListener("sp:tour", open);
    }, []);

    const currentDist = (DISTRICTS[selectedState] || []).find(d => d.name === selectedDistrict);

    const getMap = useCallback(() => mapRef.current, []);
    const gpuStatus = useGpuStatus();
    const activeCount = Object.values(activeLayers).filter(Boolean).length + (maskOn ? 1 : 0) + (droneOn ? 1 : 0);
    const flyToDistrict = () => currentDist && mapRef.current?.flyTo({ center: currentDist.center, zoom: currentDist.zoom, duration: 1500 });

    return (
        <div className="relative flex h-full w-full overflow-hidden" style={{ background: "var(--ink)" }}>
            <input
                ref={aoiFileInputRef}
                type="file"
                accept=".geojson,.json,.shp,.zip,.kml,.gpkg"
                onChange={handleAOIUpload}
                className="hidden"
            />

            {/* ═══════════ LEFT RAIL — layers ═══════════ */}
            {railOpen ? (
                <aside
                    className="anim-slide-in-left z-20 flex shrink-0 flex-col overflow-y-auto"
                    style={{ width: "var(--rail-w)", background: "var(--surface)", borderRight: "1px solid var(--line)" }}
                >
                    {/* Headline */}
                    <div className="px-3 pb-2 pt-3">
                        <div className="mb-1 flex items-center justify-between">
                            <span className="panel-title">Layers</span>
                            <button
                                onClick={() => setRailOpen(false)}
                                className="rounded-[5px] p-1 hover:bg-[var(--surface-2)]"
                                style={{ color: "var(--text-mute)" }}
                                title="Collapse"
                                aria-label="Collapse layers"
                            >
                                <PanelLeftClose size={13} />
                            </button>
                        </div>
                        <div className="flex items-baseline gap-1.5">
                            <span className="mono text-[22px] font-semibold leading-none" style={{ color: "var(--signal)" }}>{activeCount}</span>
                            <span className="text-[11px]" style={{ color: "var(--text-dim)" }}>
                                {activeCount === 1 ? "layer" : "layers"} on · {selectedDistrict || "no district loaded"}
                            </span>
                        </div>
                    </div>

                    <Divider />

                    {/* Region */}
                    <SectionHeader>Region</SectionHeader>
                    <div className="space-y-2 px-3 pb-2.5">
                        <label className="block">
                            <span className="mb-1 block text-[10px]" style={{ color: "var(--text-mute)" }}>State</span>
                            <select
                                value={selectedState}
                                onChange={e => { setSelectedState(e.target.value); setSelectedDistrict(null); }}
                                className="dark-select"
                            >
                                {STATES.map(s => <option key={s.name} value={s.name}>{s.name}</option>)}
                            </select>
                        </label>
                        <div data-tour="map-district">
                            <span className="mb-1 flex items-center justify-between text-[10px]" style={{ color: "var(--text-mute)" }}>
                                District
                                {currentDist && (
                                    <button onClick={flyToDistrict} className="mono flex items-center gap-1 hover:underline" style={{ color: "var(--signal)" }}>
                                        <Crosshair size={10} /> Fly to
                                    </button>
                                )}
                            </span>
                            <select
                                value={selectedDistrict || ""}
                                onChange={e => handleDistrictSelect(e.target.value)}
                                className="dark-select"
                            >
                                <option value="">Select district…</option>
                                {(DISTRICTS[selectedState] || []).map(d => <option key={d.name} value={d.name}>{d.name}</option>)}
                            </select>
                        </div>
                    </div>

                    <Divider />

                    {/* District layers */}
                    <SectionHeader right={currentDist && <span className="mono text-[10px]" style={{ color: "var(--text-mute)" }}>{currentDist.name}</span>}>
                        AI layers
                    </SectionHeader>
                    {currentDist ? (
                        <div className="px-1.5 pb-1" data-tour="map-layers">
                            {currentDist.hasMask && (
                                <ToggleRow
                                    tour="toggle-mask"
                                    on={maskOn}
                                    onClick={toggleMask}
                                    knobProps={{ "data-tour": "toggle-mask-knob", onClick: toggleMask }}
                                    label="Land-use mask"
                                    sub="SegFormer-B5 · per-pixel classes"
                                    icon={<Layers size={12} style={{ color: "var(--signal)" }} />}
                                />
                            )}
                            {currentDist.droneImagery && (
                                <ToggleRow
                                    on={droneOn}
                                    onClick={toggleDrone}
                                    label="Drone imagery"
                                    sub="High-resolution orthomosaic"
                                    icon={<Plane size={12} style={{ color: "#38BDF8" }} />}
                                />
                            )}
                            {currentDist.layers.map(layer => {
                                const layerId = `${currentDist.name.toLowerCase()}-${layer.name}`;
                                const toggle = () => toggleLayer(currentDist, layer);
                                return (
                                    <ToggleRow
                                        key={layer.id}
                                        tour={`layer-${layer.name}`}
                                        on={!!activeLayers[layerId]}
                                        onClick={toggle}
                                        knobProps={{ "data-tour": `layer-${layer.name}-knob`, onClick: toggle }}
                                        colour={layer.color}
                                        label={layer.label}
                                        sub={activeLayers[layerId] && LAYER_MIN_ZOOM[layer.name] && mapZoom < LAYER_MIN_ZOOM[layer.name]
                                            ? `Zoom in to see ${layer.label.toLowerCase()} (level ${LAYER_MIN_ZOOM[layer.name]}+)` : undefined}
                                        icon={layer.color && (
                                            <span className="inline-block h-[10px] w-[10px] shrink-0 rounded-[3px]" style={{ background: layer.color, boxShadow: `0 0 6px ${layer.color}` }} />
                                        )}
                                    />
                                );
                            })}
                            {currentDist.layers.length === 0 && <Empty>Layers for {currentDist.name} are still being processed.</Empty>}
                        </div>
                    ) : (
                        <Empty>Pick a district above to load its AI-extracted layers.</Empty>
                    )}

                    <Divider />

                    {/* Ongole drone survey grid — always available, drone clips render beneath it */}
                    <DroneGrid key={`grid-${baseMap}`} getMap={getMap} />
                    <DroneLayers key={`drone-${baseMap}|${gpuStatus.base}`} getMap={getMap} />
                    <Divider />

                    {/* Ongole satellite, two dates — land use for each + change detection between them */}
                    <SectionHeader>Ongole satellite · 2017 → 2026</SectionHeader>
                    <DroneLayers key={`sat-ongole-${baseMap}|${gpuStatus.base}`} getMap={getMap} district="ongole" kind="satellite" />
                    <Divider />

                    {/* Guntur drone survey — same detections as Ongole */}
                    <SectionHeader>Guntur drone survey</SectionHeader>
                    <DroneLayers key={`drone-guntur-${baseMap}|${gpuStatus.base}`} getMap={getMap} district="guntur" />
                    <Divider />

                    {/* Imagery + batch results from the GPU server (rebuilt with the map on style change) */}
                    {gpuStatus.base && <><ServerImagery key={`${baseMap}|${gpuStatus.base}`} getMap={getMap} /><Divider /></>}

                    {/* Change detection — not wired yet; shown so the roadmap is visible */}
                    <SectionHeader right={<Pill colour="var(--alert)">In progress</Pill>}>Change detection</SectionHeader>
                    <div className="px-1.5 pb-1">
                        {CHANGE_LAYERS.map(cd => (
                            <ToggleRow key={cd.label} disabled on={false} colour={cd.color} label={cd.label}
                                icon={<span className="inline-block h-[10px] w-[10px] shrink-0 rounded-[3px]" style={{ background: cd.color }} />} />
                        ))}
                    </div>

                    <Divider />

                    {/* Legend — vector layers glow like Sentinel's; the mask keeps its data colours */}
                    <SectionHeader>Legend</SectionHeader>
                    <div className="px-3 pb-3">
                        <div className="mb-1.5 text-[10px]" style={{ color: "var(--text-mute)" }}>AI layers</div>
                        <div className="grid grid-cols-2 gap-x-3 gap-y-2">
                            {LEGEND.map(l => (
                                <div key={l.label} className="flex min-w-0 items-center gap-2">
                                    <span className="shrink-0" style={{
                                        width: 13, height: l.hollow ? 3 : 11, borderRadius: l.hollow ? 2 : 3,
                                        background: l.color, boxShadow: `0 0 6px ${l.color}`,
                                    }} />
                                    <span className="truncate text-[11px]" style={{ color: "var(--text-dim)" }}>{l.label}</span>
                                </div>
                            ))}
                        </div>
                        <div className="mb-1.5 mt-3 text-[10px]" style={{ color: "var(--text-mute)" }}>Land-use mask</div>
                        <div className="grid grid-cols-3 gap-x-2 gap-y-1.5">
                            {MASK_LEGEND.map(l => (
                                <div key={l.label} className="flex min-w-0 items-center gap-1.5">
                                    <span className="h-[9px] w-[9px] shrink-0 rounded-[2px]" style={{ background: l.color }} />
                                    <span className="truncate text-[10.5px]" style={{ color: "var(--text-dim)" }}>{l.label}</span>
                                </div>
                            ))}
                        </div>
                    </div>

                    <Divider />

                    {/* Tools */}
                    <SectionHeader>Tools</SectionHeader>
                    <div className="flex flex-col gap-1 px-2.5 pb-3">
                        <Button className="w-full !justify-start" onClick={() => setAoiMin(false)}>
                            <Target size={13} /> Area of interest
                        </Button>
                        <Button className="w-full !justify-start" onClick={startDrawAOI}>
                            <PenLine size={13} /> Draw an AOI
                        </Button>
                        <Button className="w-full !justify-start" onClick={() => aoiFileInputRef.current?.click()}>
                            <UploadIcon size={13} /> Upload boundary file
                        </Button>
                    </div>
                </aside>
            ) : (
                <aside
                    className="z-20 flex w-[46px] shrink-0 flex-col items-center gap-1 py-2"
                    style={{ background: "var(--surface)", borderRight: "1px solid var(--line)" }}
                >
                    <button onClick={() => setRailOpen(true)} className="rounded-[6px] p-2 hover:bg-[var(--surface-2)]"
                        style={{ color: "var(--text-dim)" }} title="Show layers" aria-label="Show layers">
                        <PanelLeftOpen size={15} />
                    </button>
                    <div className="my-1 h-px w-6" style={{ background: "var(--line)" }} />
                    <span className="mono text-[12px] font-semibold" style={{ color: "var(--signal)" }} title="Layers on">{activeCount}</span>
                    <div className="my-1 h-px w-6" style={{ background: "var(--line)" }} />
                    {[
                        { icon: Target, label: "Area of interest", fn: () => setAoiMin(false) },
                        { icon: PenLine, label: "Draw an AOI", fn: startDrawAOI },
                        { icon: UploadIcon, label: "Upload boundary file", fn: () => aoiFileInputRef.current?.click() },
                    ].map(({ icon: Icon, label, fn }) => (
                        <button key={label} onClick={fn} title={label} aria-label={label}
                            className="rounded-[6px] p-2 hover:bg-[var(--surface-2)]" style={{ color: "var(--text-dim)" }}>
                            <Icon size={15} />
                        </button>
                    ))}
                </aside>
            )}

            {/* ═══════════ MAP ═══════════ */}
            <main className="relative min-w-0 flex-1">
                {/* Inline: mapbox-gl.css sets .mapboxgl-map { position: relative }, which beats the Tailwind utility. */}
                <div ref={mapContainerRef} style={{ position: "absolute", inset: 0 }} />

                {/* Drawing hint */}
                {drawMode && (
                    <div className="anim-fade-up pointer-events-none absolute left-1/2 top-3 z-20 -translate-x-1/2">
                        <Card className="flex items-center gap-2 px-3 py-2" style={{ boxShadow: "var(--sh-md)" }}>
                            <span className="live-dot signal" />
                            <span className="text-[12px]" style={{ color: "var(--text)" }}>Click to add vertices · double-click to finish</span>
                        </Card>
                    </div>
                )}

                {/* Loading */}
                {loading && (
                    <div className="anim-fade-up absolute bottom-3 left-3 z-20 w-[240px] max-w-[calc(100%-24px)]">
                        <Card className="overflow-hidden" style={{ boxShadow: "var(--sh-md)" }}>
                            <div className="flex items-center gap-2 px-3 py-2">
                                <Spinner size={13} />
                                <span className="truncate text-[11px]" style={{ color: "var(--text)" }}>{loading}</span>
                            </div>
                            <div className="progress-sweep" />
                        </Card>
                    </div>
                )}

                {/* Coordinates */}
                {coords && (
                    <div className="pointer-events-none absolute bottom-3 left-1/2 z-10 -translate-x-1/2 rounded-[6px] px-2.5 py-1"
                        style={{ background: "rgba(11, 18, 32, 0.82)", border: "1px solid var(--line)" }}>
                        <span className="coords-display">{coords.lat}°N · {coords.lng}°E</span>
                    </div>
                )}

                {/* ═══════════ AOI — floating card, drag it by its header ═══════════ */}
                <Draggable nodeRef={aoiCardRef} handle=".aoi-drag" cancel="button" bounds="parent">
                    <div ref={aoiCardRef} data-tour="aoi-panel"
                        className="anim-fade absolute right-3 top-3 z-30 w-[300px] max-w-[calc(100%-24px)] overflow-hidden rounded-[8px]"
                        style={{ background: "var(--surface)", border: "1px solid var(--line)", boxShadow: "var(--sh-lg)" }}>
                        <div className="aoi-drag flex cursor-move select-none items-center gap-1.5 px-2.5"
                            style={{ height: 38, borderBottom: aoiMin ? "none" : "1px solid var(--line)" }}>
                            <GripVertical size={12} style={{ color: "var(--text-mute)" }} />
                            <span className="panel-title flex flex-1 items-center gap-1.5">
                                <Target size={12} style={{ color: "var(--signal)" }} /> Area of interest
                            </span>
                            {aoiActive && <span className="live-dot signal" title="AOI active" />}
                            <button onClick={() => setAoiMin(v => !v)} className="rounded-[5px] p-1 hover:bg-[var(--surface-2)]"
                                style={{ color: "var(--text-dim)" }} aria-label={aoiMin ? "Expand" : "Minimise"} title={aoiMin ? "Expand" : "Minimise"}>
                                {aoiMin ? <ChevronDown size={13} /> : <ChevronUp size={13} />}
                            </button>
                        </div>
                        {!aoiMin && (
                            <div className="p-3">
                                {drawMode ? (
                                    <div className="space-y-2">
                                        <div className="flex items-start gap-2 rounded-[6px] px-2.5 py-2"
                                            style={{ background: "var(--signal-dim)", border: "1px solid var(--signal)" }}>
                                            <span className="live-dot signal mt-1 shrink-0" />
                                            <span className="text-[11px] leading-relaxed" style={{ color: "var(--text)" }}>
                                                Click on the map to add vertices, then double-click to finish.
                                            </span>
                                        </div>
                                        <Button variant="ghost" className="w-full" onClick={cancelDraw} style={{ color: "var(--critical)" }}>
                                            Cancel drawing
                                        </Button>
                                    </div>
                                ) : aoiActive && aoiFeatures?.length ? (
                                    <div className="space-y-2">
                                        <div className="flex items-center gap-2.5 rounded-[6px] px-2.5 py-2"
                                            style={{ background: "var(--signal-dim)", border: "1px solid var(--signal)" }}>
                                            <CheckCircle2 size={15} style={{ color: "var(--signal)" }} className="shrink-0" />
                                            <div className="min-w-0 flex-1">
                                                <p className="truncate text-[12px] font-medium" style={{ color: "var(--text)" }}>AOI active · data clipped</p>
                                                <p className="mono mt-0.5 truncate text-[10.5px]" style={{ color: "var(--text-dim)" }}>
                                                    {aoiFeatures.length > 1 ? `${aoiFeatures.length} polygons · ` : ""}{computeTotalAreaKm2(aoiFeatures)} km²
                                                </p>
                                            </div>
                                        </div>
                                        <Button variant={statsOpen ? "active" : "primary"} className="w-full" onClick={toggleStats}>
                                            <BarChart3 size={13} />
                                            {statsLoading ? "Analysing…" : statsOpen ? "Hide statistics" : "View statistics"}
                                        </Button>
                                        <div className="grid grid-cols-3 gap-1.5">
                                            <Button onClick={clearAOI} style={{ color: "var(--critical)" }}>Clear</Button>
                                            <Button onClick={startDrawAOI}>Redraw</Button>
                                            <Button data-tour="aoi-upload-btn" onClick={() => aoiFileInputRef.current?.click()}>Upload</Button>
                                        </div>
                                    </div>
                                ) : (
                                    <div className="space-y-2.5">
                                        <div className="grid grid-cols-2 gap-1.5">
                                            <Button variant="primary" data-tour="aoi-draw-btn" onClick={startDrawAOI}>
                                                <PenLine size={13} /> Draw AOI
                                            </Button>
                                            <Button onClick={() => aoiFileInputRef.current?.click()}>
                                                <UploadIcon size={13} /> Upload
                                            </Button>
                                        </div>
                                        <p className="text-[11px] leading-relaxed" style={{ color: "var(--text-dim)" }}>
                                            Draw a polygon or upload a boundary file — the map and every count are then limited to that area.
                                        </p>
                                        <div className="flex flex-wrap gap-1">
                                            {["GeoJSON", "Shapefile", "KML", "GeoPackage"].map(f => <Pill key={f} mono>{f}</Pill>)}
                                        </div>
                                    </div>
                                )}
                            </div>
                        )}
                    </div>
                </Draggable>

                            {/* AOI statistics */}
                            {aoiActive && statsOpen && (
                                <Draggable nodeRef={statsCardRef} handle=".stats-drag" cancel="button" bounds="parent">
                                <div ref={statsCardRef} data-tour="aoi-stats"
                                    className="anim-fade absolute right-3 top-[236px] z-30 flex max-h-[55vh] w-[300px] max-w-[calc(100%-24px)] flex-col overflow-hidden rounded-[8px]"
                                    style={{ background: "var(--surface)", border: "1px solid var(--line)", boxShadow: "var(--sh-lg)" }}>
                                    <div className="stats-drag shrink-0 cursor-move select-none" style={{ borderBottom: "1px solid var(--line)" }}>
                                    <SectionHeader right={
                                        <button onClick={() => setStatsOpen(false)} className="rounded-[5px] p-0.5 hover:bg-[var(--surface-2)]"
                                            style={{ color: "var(--text-mute)" }} aria-label="Hide statistics"><X size={12} /></button>
                                    }><span className="flex items-center gap-1.5"><GripVertical size={12} style={{ color: "var(--text-mute)" }} /><BarChart3 size={12} style={{ color: "var(--signal)" }} /> AOI statistics</span></SectionHeader>
                                    </div>
                                    <div className="min-h-0 space-y-3 overflow-y-auto p-3">
                                        {statsLoading ? (
                                            <div className="py-3">
                                                <div className="mb-2 flex items-center gap-2" style={{ color: "var(--text-dim)" }}>
                                                    <Spinner size={13} /><span className="text-[11px]">Analyzing AOI…</span>
                                                </div>
                                                <div className="progress-sweep rounded" />
                                            </div>
                                        ) : !aoiStats ? (
                                            <Empty>No analytics yet. Select the district that contains this AOI, or check that the model backend is online.</Empty>
                                        ) : (() => {
                                            const sel = selectedParcel != null ? aoiStats.perPolygon[selectedParcel] : null;
                                            const headline = sel || aoiStats.totals;
                                            return (
                                                <>
                                                    <div>
                                                        <div className="mb-1.5 flex items-center justify-between">
                                                            <span className="text-[10px] uppercase tracking-[0.09em]" style={{ color: "var(--text-mute)" }}>
                                                                {sel ? `Polygon ${selectedParcel + 1} · selected` : `Total${aoiStats.perPolygon.length > 1 ? ` · ${aoiStats.perPolygon.length} polygons` : ""}`}
                                                            </span>
                                                            {sel && (
                                                                <button onClick={() => setSelectedParcel(null)} className="text-[10px] hover:underline" style={{ color: "var(--signal)" }}>Show total</button>
                                                            )}
                                                        </div>
                                                        <div className="stagger grid grid-cols-2 gap-1.5">
                                                            {[
                                                                { label: "Buildings", value: headline.buildings.toLocaleString(), color: "#EF4444" },
                                                                { label: "Waterbodies", value: headline.waterbodies.toLocaleString(), color: "#3B82F6" },
                                                                { label: "Roads (km)", value: headline.roadKm.toLocaleString(), color: "#EAB308" },
                                                                { label: "Area (km²)", value: headline.areaKm2.toLocaleString(), color: "var(--signal)" },
                                                            ].map(s => (
                                                                <div key={s.label} className="relative overflow-hidden rounded-[6px] px-2.5 py-2"
                                                                    style={{ background: "var(--surface-2)", border: "1px solid var(--line)" }}>
                                                                    <span className="absolute inset-x-0 top-0 h-[2px]" style={{ background: s.color }} />
                                                                    <div className="truncate text-[9.5px] uppercase tracking-[0.09em]" style={{ color: "var(--text-mute)" }}>{s.label}</div>
                                                                    <div className="mono mt-1 text-[17px] font-semibold leading-none" style={{ color: "var(--text)" }}>{s.value}</div>
                                                                </div>
                                                            ))}
                                                        </div>
                                                    </div>

                                                    {aoiStats.perPolygon.length > 1 && (
                                                        <div>
                                                            <div className="mb-1.5 text-[10px] uppercase tracking-[0.09em]" style={{ color: "var(--text-mute)" }}>Parcels · tap to select</div>
                                                            <div className="space-y-1">
                                                                {aoiStats.perPolygon.map(p => {
                                                                    const active = selectedParcel === p.index;
                                                                    return (
                                                                        <button
                                                                            key={p.index}
                                                                            onClick={() => focusParcel(p.index)}
                                                                            className="w-full rounded-[6px] px-2.5 py-2 text-left transition-colors hover:bg-[var(--surface-2)]"
                                                                            style={{
                                                                                border: `1px solid ${active ? "var(--alert)" : "var(--line)"}`,
                                                                                background: active ? "var(--alert-dim)" : "transparent",
                                                                            }}
                                                                        >
                                                                            <div className="mb-0.5 flex items-center justify-between">
                                                                                <span className="text-[11.5px] font-medium" style={{ color: active ? "var(--alert)" : "var(--text)" }}>Polygon {p.index + 1}</span>
                                                                                <span className="mono text-[10px]" style={{ color: "var(--text-mute)" }}>{p.areaKm2} km²</span>
                                                                            </div>
                                                                            <div className="mono flex items-center gap-3 text-[10px]" style={{ color: "var(--text-dim)" }}>
                                                                                <span><b style={{ color: "var(--text)" }}>{p.buildings.toLocaleString()}</b> bldg</span>
                                                                                <span><b style={{ color: "var(--text)" }}>{p.waterbodies.toLocaleString()}</b> water</span>
                                                                                <span><b style={{ color: "var(--text)" }}>{p.roadKm}</b> km road</span>
                                                                            </div>
                                                                        </button>
                                                                    );
                                                                })}
                                                            </div>
                                                        </div>
                                                    )}

                                                    <p className="text-[10px] leading-relaxed" style={{ color: "var(--text-mute)" }}>
                                                        {aoiStats.perPolygon.length > 1 ? "Tap a parcel on the map or in the list to see its own counts. " : ""}
                                                        Counts come from the district vector data; road length is the portion inside the boundary.
                                                    </p>
                                                </>
                                            );
                                        })()}
                                    </div>
                                </div>
                                </Draggable>
                            )}

                            {/* Detection overlay from Upload & Analysis */}
                            {detectionOverlay && (
                                <Draggable nodeRef={detCardRef} handle=".det-drag" cancel="button" bounds="parent">
                                <div ref={detCardRef} data-tour="det-overlay"
                                    className="anim-fade absolute bottom-28 right-3 z-30 w-[264px] max-w-[calc(100%-24px)] overflow-hidden rounded-[8px]"
                                    style={{ background: "var(--surface)", border: "1px solid var(--line)", boxShadow: "var(--sh-lg)" }}>
                                    <div className="det-drag cursor-move select-none" style={{ borderBottom: "1px solid var(--line)" }}>
                                    <SectionHeader right={
                                        <button onClick={removeDetectionOverlay} title="Remove overlay" className="rounded-[5px] p-0.5 hover:bg-[var(--surface-2)]"
                                            style={{ color: "var(--critical)" }} aria-label="Remove overlay"><X size={12} /></button>
                                    }><span className="flex items-center gap-1.5"><GripVertical size={12} style={{ color: "var(--text-mute)" }} /> Detection overlay</span></SectionHeader>
                                    </div>
                                    <div className="space-y-2.5 p-3">
                                        <p className="mono truncate text-[11px]" style={{ color: "var(--text-dim)" }} title={detectionOverlay.name}>{detectionOverlay.name}</p>
                                        <label className="block">
                                            <span className="mb-1.5 flex items-center justify-between text-[10px]" style={{ color: "var(--text-mute)" }}>
                                                <span>Opacity</span><span className="mono">{Math.round(detectionOpacity * 100)}%</span>
                                            </span>
                                            <input type="range" min="0.1" max="1" step="0.05" value={detectionOpacity}
                                                onChange={e => setDetectionOpacity(parseFloat(e.target.value))} className="w-full cursor-pointer" />
                                        </label>
                                        <Button className="w-full" onClick={() => {
                                            const m = mapRef.current; const b = detectionOverlay.bounds;
                                            if (m && b) m.fitBounds([[b.west, b.south], [b.east, b.north]], { padding: 60, duration: 1200 });
                                        }}>
                                            <Crosshair size={12} /> Zoom to overlay
                                        </Button>
                                    </div>
                                </div>
                                </Draggable>
                            )}
            </main>
        </div>
    );
}
