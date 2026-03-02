import React, { useEffect, useRef, useState, useCallback } from "react";
import mapboxgl from "mapbox-gl";
import "mapbox-gl/dist/mapbox-gl.css";
import Draggable from "react-draggable";
import { addArcGISFeatureLayer, removeLayerGroup } from "../utils/mapLayers";
import { load as lercLoad, decode as lercDecode } from "lerc";
import proj4 from "proj4";

proj4.defs("EPSG:32644", "+proj=utm +zone=44 +datum=WGS84 +units=m +no_defs");

const MAPBOX_TOKEN = import.meta.env.VITE_MAPBOX_TOKEN;

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
            droneImagery: "https://tiledimageservices5.arcgis.com/73n8CSGpSSyHr1T9/arcgis/rest/services/Drone_img_vijayvada/ImageServer",
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
    ctx.clearRect(0, 0, canvas.width, canvas.height); // crucial for missing tiles

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
                        const hasMask = Boolean(mask);
                        for (let i = 0; i < width * height; i++) {
                            const offset = i * 4;
                            dest[offset] = red ? red[i] : 0;
                            dest[offset + 1] = green ? green[i] : 0;
                            dest[offset + 2] = blue ? blue[i] : 0;
                            dest[offset + 3] = (hasMask && mask && !mask[i]) ? 0 : 255;
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
        // We use PNG because JPEG strips alpha logic, but WebP is also good and fast.
        // We stick to what user's reference App had, but WebP reduces RAM.
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
    const [baseMap, setBaseMap] = useState("dark-v11");
    const [loading, setLoading] = useState(null);
    const [coords, setCoords] = useState(null);
    const [panelOpen, setPanelOpen] = useState(true);
    const panelRef = useRef(null); // Ref for Draggable

    // Track which layers are toggled on
    const [activeLayers, setActiveLayers] = useState({});
    const [maskOn, setMaskOn] = useState(false);
    const [droneOn, setDroneOn] = useState(false);

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

        const refreshDroneLevel = () => {
            if (mapRef.current) {
                updateDroneState(mapRef.current);
            }
        };
        map.on("zoomend", refreshDroneLevel);

        map.on("load", () => { mapRef.current = map; });
        return () => {
            mapRef.current = null;
            maskLoadedRef.current = false; maskLevelRef.current = -1;
            droneLoadedRef.current = false; droneCurrentLevelRef.current = null;
            if (map) {
                map.off("zoomend", refreshDroneLevel);
                map.remove();
            }
        };
    }, [baseMap, maskOn, droneOn]);

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

        // Remove old drone layer
        if (map.getLayer("drone-layer")) map.removeLayer("drone-layer");
        if (map.getSource("drone-source")) map.removeSource("drone-source");
        setDroneOn(false);

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
            map.addLayer({
                id: layerId,
                type: "raster",
                source: sourceId,
                paint: { "raster-opacity": 1.0, "raster-resampling": "nearest" }
            }, firstFeatureId);
        }
        if (droneOn) {
            if (!map.getLayer(layerId) || map.getLayoutProperty(layerId, 'visibility') !== 'visible') {
                map.setLayoutProperty(layerId, "visibility", "visible");
            }
        }
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
                .catch(err => {
                    console.error("Drone failed:", err);
                })
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
                            setTimeout(() => {
                                if (droneOn && !droneBuildingRef.current) buildNext(pending);
                            }, 0);
                        }
                    }
                });
        };

        const cached = droneCacheRef.current.get(targetLevel);
        if (cached) {
            applyDroneLayerToMap(map, cached.dataUrl, cached.coordinates);
            droneCurrentLevelRef.current = targetLevel;
            return;
        }

        if (droneBuildingRef.current) {
            dronePendingLevelRef.current = targetLevel;
            return;
        }

        buildNext(targetLevel);
    }, [droneOn, selectedState, selectedDistrict]);

    const toggleDrone = useCallback(() => {
        const map = mapRef.current;
        if (!map || !selectedDistrict) return;
        const dist = (DISTRICTS[selectedState] || []).find(d => d.name === selectedDistrict);

        if (droneOn) {
            if (map.getLayer("drone-layer")) map.setLayoutProperty("drone-layer", "visibility", "none");
            setDroneOn(false);
        } else {
            setDroneOn(true);
            setTimeout(() => { // Let state update so updateDroneState sees droneOn=true
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
                                        <label
                                            className="flex items-center gap-2.5 py-1.5 px-2 rounded-md hover:bg-white/5 cursor-pointer mb-1"
                                            onClick={(e) => { e.preventDefault(); toggleMask(); }}
                                        >
                                            <div className={`w-8 h-4 rounded-full relative transition-colors ${maskOn ? "bg-[#0EA5E9]" : "bg-white/20"}`}>
                                                <div className={`absolute top-0.5 w-3 h-3 rounded-full bg-white shadow transition-all ${maskOn ? "left-4.5" : "left-0.5"}`} />
                                            </div>
                                            <span className="text-xs select-none">🎭 Land Use Mask</span>
                                        </label>
                                    )}

                                    {/* Drone Imagery toggle */}
                                    {currentDist.droneImagery && (
                                        <label
                                            className="flex items-center gap-2.5 py-1.5 px-2 rounded-md hover:bg-white/5 cursor-pointer mb-1"
                                            onClick={(e) => { e.preventDefault(); toggleDrone(); }}
                                        >
                                            <div className={`w-8 h-4 rounded-full relative transition-colors ${droneOn ? "bg-[#0EA5E9]" : "bg-white/20"}`}>
                                                <div className={`absolute top-0.5 w-3 h-3 rounded-full bg-white shadow transition-all ${droneOn ? "left-4.5" : "left-0.5"}`} />
                                            </div>
                                            <span className="text-xs select-none">🚁 Drone Imagery</span>
                                        </label>
                                    )}

                                    {/* Feature layers */}
                                    {currentDist.layers.map(layer => {
                                        const layerId = `${currentDist.name.toLowerCase()}-${layer.name}`;
                                        const isOn = activeLayers[layerId];
                                        return (
                                            <label
                                                key={layer.id}
                                                className="flex items-center gap-2.5 py-1.5 px-2 rounded-md hover:bg-white/5 cursor-pointer"
                                                onClick={(e) => { e.preventDefault(); toggleLayer(currentDist, layer); }}
                                            >
                                                <div className={`w-8 h-4 rounded-full relative transition-colors ${isOn ? "bg-[#0EA5E9]" : "bg-white/20"}`}>
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
                </Draggable>
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
