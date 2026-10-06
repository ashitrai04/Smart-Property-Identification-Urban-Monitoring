import React, { useEffect, useRef, useState, useMemo } from "react";
import mapboxgl from "mapbox-gl";
import "mapbox-gl/dist/mapbox-gl.css";
import StatCard from "../components/StatCard";
import { Building, Building2, Droplets, GitCompareArrows, LandPlot, Route } from "lucide-react";
import { Pill } from "../components/ui";
import { addArcGISFeatureLayer, removeLayerGroup } from "../utils/mapLayers";
import { registerTour, unregisterTour } from "../tour/tourBus";
import {
    BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer,
    PieChart, Pie, Cell, Legend, AreaChart, Area, CartesianGrid,
} from "recharts";

const MAPBOX_TOKEN = import.meta.env.VITE_MAPBOX_TOKEN;

// ── District FeatureServer URLs (boundary = layer 0) ──
const DISTRICT_FS = {
    "Visakhapatnam": "https://services5.arcgis.com/73n8CSGpSSyHr1T9/arcgis/rest/services/final_visakhapatnam/FeatureServer",
    "Vijayawada": "https://services5.arcgis.com/73n8CSGpSSyHr1T9/arcgis/rest/services/vijayawada_layers/FeatureServer",
    "Guntur": "https://services5.arcgis.com/73n8CSGpSSyHr1T9/arcgis/rest/services/guntur_layer/FeatureServer",
    "Anantapur": "https://services5.arcgis.com/73n8CSGpSSyHr1T9/arcgis/rest/services/anantapur_layers/FeatureServer",
    "Nellore": "https://services5.arcgis.com/73n8CSGpSSyHr1T9/arcgis/rest/services/nellore_shpfiles/FeatureServer",
};

// ── District-wise dummy data ──
const DISTRICT_DATA = {
    "Visakhapatnam": {
        center: [83.25, 17.93], zoom: 11,
        stats: { properties: "2,84,103", plots: "72,418", water: "4,219", changes: "2,847", roads: "12,856", builtup: "428" },
        trends: { properties: 4.2, plots: 2.8, water: -1.3, changes: 12.4 },
        landUse: [{ name: "Built-up", value: 38, color: "#d97706" }, { name: "Vegetation", value: 24, color: "#16a34a" }, { name: "Agriculture", value: 16, color: "#65a30d" }, { name: "Water", value: 12, color: "#2563eb" }, { name: "Barren", value: 10, color: "#9ca3af" }],
        monthly: [{ month: "Jul", properties: 268000, changes: 340 }, { month: "Aug", properties: 272000, changes: 410 }, { month: "Sep", properties: 275000, changes: 280 }, { month: "Oct", properties: 278000, changes: 520 }, { month: "Nov", properties: 281000, changes: 380 }, { month: "Dec", properties: 284000, changes: 450 }],
    },
    "Vijayawada": {
        center: [80.62, 16.51], zoom: 11,
        stats: { properties: "1,98,472", plots: "48,291", water: "3,812", changes: "1,923", roads: "9,240", builtup: "312" },
        trends: { properties: 3.1, plots: 1.9, water: 0.5, changes: 8.7 },
        landUse: [{ name: "Built-up", value: 42, color: "#d97706" }, { name: "Vegetation", value: 18, color: "#16a34a" }, { name: "Agriculture", value: 20, color: "#65a30d" }, { name: "Water", value: 14, color: "#2563eb" }, { name: "Barren", value: 6, color: "#9ca3af" }],
        monthly: [{ month: "Jul", properties: 188000, changes: 220 }, { month: "Aug", properties: 190000, changes: 310 }, { month: "Sep", properties: 192000, changes: 190 }, { month: "Oct", properties: 194000, changes: 350 }, { month: "Nov", properties: 196000, changes: 270 }, { month: "Dec", properties: 198000, changes: 310 }],
    },
    "Guntur": {
        center: [80.45, 16.30], zoom: 11,
        stats: { properties: "1,76,830", plots: "52,108", water: "3,291", changes: "1,487", roads: "8,124", builtup: "286" },
        trends: { properties: 2.4, plots: 3.2, water: -0.8, changes: 6.3 },
        landUse: [{ name: "Built-up", value: 30, color: "#d97706" }, { name: "Vegetation", value: 22, color: "#16a34a" }, { name: "Agriculture", value: 28, color: "#65a30d" }, { name: "Water", value: 10, color: "#2563eb" }, { name: "Barren", value: 10, color: "#9ca3af" }],
        monthly: [{ month: "Jul", properties: 168000, changes: 170 }, { month: "Aug", properties: 170000, changes: 240 }, { month: "Sep", properties: 172000, changes: 150 }, { month: "Oct", properties: 174000, changes: 310 }, { month: "Nov", properties: 175000, changes: 200 }, { month: "Dec", properties: 177000, changes: 260 }],
    },
    "Anantapur": {
        center: [77.60, 14.68], zoom: 10,
        stats: { properties: "1,42,918", plots: "61,204", water: "2,108", changes: "982", roads: "6,892", builtup: "198" },
        trends: { properties: 1.8, plots: 4.1, water: -2.1, changes: 4.9 },
        landUse: [{ name: "Built-up", value: 22, color: "#d97706" }, { name: "Vegetation", value: 18, color: "#16a34a" }, { name: "Agriculture", value: 34, color: "#65a30d" }, { name: "Water", value: 6, color: "#2563eb" }, { name: "Barren", value: 20, color: "#9ca3af" }],
        monthly: [{ month: "Jul", properties: 136000, changes: 110 }, { month: "Aug", properties: 138000, changes: 160 }, { month: "Sep", properties: 139000, changes: 90 }, { month: "Oct", properties: 140000, changes: 200 }, { month: "Nov", properties: 141000, changes: 140 }, { month: "Dec", properties: 143000, changes: 180 }],
    },
    "Nellore": {
        center: [79.99, 14.44], zoom: 10,
        stats: { properties: "1,31,069", plots: "44,826", water: "4,862", changes: "1,284", roads: "5,744", builtup: "168" },
        trends: { properties: 2.9, plots: 2.0, water: 1.2, changes: 7.1 },
        landUse: [{ name: "Built-up", value: 26, color: "#d97706" }, { name: "Vegetation", value: 28, color: "#16a34a" }, { name: "Agriculture", value: 24, color: "#65a30d" }, { name: "Water", value: 14, color: "#2563eb" }, { name: "Barren", value: 8, color: "#9ca3af" }],
        monthly: [{ month: "Jul", properties: 124000, changes: 150 }, { month: "Aug", properties: 126000, changes: 200 }, { month: "Sep", properties: 127000, changes: 120 }, { month: "Oct", properties: 128000, changes: 260 }, { month: "Nov", properties: 130000, changes: 180 }, { month: "Dec", properties: 131000, changes: 220 }],
    },
};

// AP total (aggregated)
const AP_TOTAL = {
    stats: { properties: "12,48,392", plots: "3,21,847", water: "18,492", changes: "8,723", roads: "42,856", builtup: "1,392" },
    trends: { properties: 4.2, plots: 2.8, water: -1.3, changes: 12.4 },
    landUse: [{ name: "Built-up", value: 32, color: "#d97706" }, { name: "Vegetation", value: 22, color: "#16a34a" }, { name: "Agriculture", value: 24, color: "#65a30d" }, { name: "Water", value: 10, color: "#2563eb" }, { name: "Barren", value: 12, color: "#9ca3af" }],
    monthly: [{ month: "Jul", properties: 1120000, changes: 720 }, { month: "Aug", properties: 1145000, changes: 810 }, { month: "Sep", properties: 1168000, changes: 640 }, { month: "Oct", properties: 1192000, changes: 920 }, { month: "Nov", properties: 1218000, changes: 780 }, { month: "Dec", properties: 1248000, changes: 870 }],
};

const DISTRICT_NAMES = Object.keys(DISTRICT_DATA);

// Chart chrome in the console palette
const AXIS = { fontSize: 10, fill: "#64748B", fontFamily: "JetBrains Mono, monospace" };
const AXIS_DIM = { fontSize: 10, fill: "#92A0B5" };
const TOOLTIP = { background: "#131C2B", border: "1px solid #2A3A50", borderRadius: 6, color: "#E7ECF3", fontSize: 11, boxShadow: "0 4px 16px rgba(0,0,0,0.45)" };

export default function Home() {
    const mapContainerRef = useRef(null);
    const mapRef = useRef(null);
    const boundaryIdRef = useRef(null);
    const [selectedDistrict, setSelectedDistrict] = useState("");

    // Expose district selection to the guided tour
    useEffect(() => {
        registerTour("home", { selectDistrict: (n) => setSelectedDistrict(n) });
        return () => unregisterTour("home");
    }, []);

    // Current data based on selection
    const data = selectedDistrict ? DISTRICT_DATA[selectedDistrict] : AP_TOTAL;
    const s = data.stats;
    const t = data.trends;

    const STATS = [
        { icon: Building2, label: "Total properties", value: s.properties, trend: t.properties, color: "#2DD4BF" },
        { icon: LandPlot, label: "Open plots", value: s.plots, trend: t.plots, color: "#34D399" },
        { icon: Droplets, label: "Water bodies", value: s.water, trend: t.water, color: "#38BDF8" },
        { icon: GitCompareArrows, label: "Change detections", value: s.changes, trend: t.changes, color: "#F5A524" },
        { icon: Route, label: "Road network (km)", value: s.roads, trend: 0, color: "#FBBF24" },
        { icon: Building, label: "Built-up (km²)", value: s.builtup, trend: 0, color: "#A78BFA" },
    ];

    const districtBarData = useMemo(() =>
        DISTRICT_NAMES.map(name => ({ name, Properties: parseInt(DISTRICT_DATA[name].stats.properties.replace(/,/g, "")) })),
        []);

    useEffect(() => {
        mapboxgl.accessToken = MAPBOX_TOKEN;
        const map = new mapboxgl.Map({
            container: mapContainerRef.current,
            style: "mapbox://styles/mapbox/satellite-streets-v12",
            center: [80.0, 15.9],
            zoom: 6.2,
            interactive: true,
        });
        map.addControl(new mapboxgl.NavigationControl({ showCompass: false }), "top-right");
        mapRef.current = map;
        return () => { mapRef.current = null; map.remove(); };
    }, []);

    // Fly to district on selection + load boundary
    useEffect(() => {
        const map = mapRef.current;
        if (!map) return;

        // Remove old boundary
        if (boundaryIdRef.current) {
            removeLayerGroup(map, boundaryIdRef.current);
            boundaryIdRef.current = null;
        }

        if (selectedDistrict && DISTRICT_DATA[selectedDistrict]) {
            const d = DISTRICT_DATA[selectedDistrict];
            map.flyTo({ center: d.center, zoom: d.zoom, duration: 1500 });

            // Load boundary from ArcGIS
            const fs = DISTRICT_FS[selectedDistrict];
            if (fs) {
                const bId = `home-boundary-${selectedDistrict.toLowerCase().replace(/\s+/g, "-")}`;
                boundaryIdRef.current = bId;
                // Wait for map to be loaded/styled before adding layers
                const loadBoundary = () => {
                    addArcGISFeatureLayer(map, {
                        id: bId,
                        featureServerUrl: `${fs}/0`, // layer 0 = boundary
                        where: "1=1",
                        fit: false,
                        paintOverrides: {
                            fill: { "fill-color": "transparent", "fill-opacity": 0 },
                            glow: { "line-color": "#CCFF00", "line-width": 9, "line-opacity": 0.3, "line-blur": 6 },
                            outline: { "line-color": "#CCFF00", "line-width": 2.2 },
                        },
                    }).catch(err => console.warn("Home boundary load failed:", err));
                };
                if (map.isStyleLoaded()) loadBoundary();
                else map.once("load", loadBoundary);
            }
        } else {
            map.flyTo({ center: [80.0, 15.9], zoom: 6.2, duration: 1500 });
        }
    }, [selectedDistrict]);

    const scope = selectedDistrict || "Andhra Pradesh";

    return (
        <div className="flex flex-col gap-4">
            {/* Header */}
            <div className="page-head">
                <div>
                    <div className="page-eyebrow">Overview</div>
                    <h1 className="page-title">{scope} urban monitoring</h1>
                    <p className="page-sub">Property identification and land use, extracted by the segmentation model from satellite and drone imagery.</p>
                </div>
                <div className="flex items-end gap-2">
                    <label>
                        <span className="panel-title mb-1 block" style={{ fontSize: 10 }}>State</span>
                        <select disabled className="dark-select" style={{ width: "auto" }}>
                            <option>Andhra Pradesh</option>
                        </select>
                    </label>
                    <label>
                        <span className="panel-title mb-1 block" style={{ fontSize: 10 }}>District</span>
                        <select
                            value={selectedDistrict}
                            onChange={e => setSelectedDistrict(e.target.value)}
                            className="dark-select"
                            data-tour="home-district"
                            style={{ width: "auto", minWidth: 160 }}
                        >
                            <option value="">All districts</option>
                            {DISTRICT_NAMES.map(d => <option key={d} value={d}>{d}</option>)}
                        </select>
                    </label>
                </div>
            </div>

            {/* Stat cards — keyed by scope so they replay their entrance on change */}
            <div key={scope} className="stagger grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))" }}>
                {STATS.map((st) => <StatCard key={st.label} {...st} />)}
            </div>

            {/* Charts + map */}
            <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))" }}>
                <div className="dash-section">
                    <div className="dash-section-title">Properties by district</div>
                    <ResponsiveContainer width="100%" height={220}>
                        <BarChart data={districtBarData} layout="vertical" margin={{ left: 0, right: 10 }}>
                            <XAxis type="number" tick={AXIS} axisLine={false} tickLine={false} tickFormatter={v => `${(v / 1000).toFixed(0)}k`} />
                            <YAxis type="category" dataKey="name" tick={AXIS_DIM} axisLine={false} tickLine={false} width={90} />
                            <Tooltip formatter={v => v.toLocaleString()} contentStyle={TOOLTIP} cursor={{ fill: "rgba(45,212,191,0.06)" }} />
                            <Bar dataKey="Properties" radius={[0, 3, 3, 0]} animationDuration={700}>
                                {districtBarData.map(d => (
                                    <Cell key={d.name} fill={!selectedDistrict || d.name === selectedDistrict ? "#2DD4BF" : "#22304A"} />
                                ))}
                            </Bar>
                        </BarChart>
                    </ResponsiveContainer>
                </div>

                <div className="dash-section" style={{ padding: 0 }}>
                    <div className="flex items-center justify-between px-3.5 pb-2 pt-3.5">
                        <div className="dash-section-title" style={{ marginBottom: 0 }}>{scope} · map</div>
                        <Pill colour="var(--signal)" mono>{selectedDistrict ? "District" : "State"}</Pill>
                    </div>
                    <div ref={mapContainerRef} style={{ height: "236px", width: "100%" }} />
                </div>

                <div className="dash-section">
                    <div className="dash-section-title">Land use distribution</div>
                    <ResponsiveContainer width="100%" height={220}>
                        <PieChart>
                            <Pie data={data.landUse} cx="50%" cy="45%" innerRadius={52} outerRadius={78} dataKey="value"
                                paddingAngle={2} stroke="#131C2B" strokeWidth={2} animationDuration={700}>
                                {data.landUse.map((e, i) => <Cell key={i} fill={e.color} />)}
                            </Pie>
                            <Legend iconSize={8} iconType="square" wrapperStyle={{ fontSize: 10, color: "#92A0B5" }} />
                            <Tooltip formatter={v => `${v}%`} contentStyle={TOOLTIP} />
                        </PieChart>
                    </ResponsiveContainer>
                </div>
            </div>

            {/* Monthly trend */}
            <div className="dash-section">
                <div className="dash-section-title">Monthly property identification trend</div>
                <ResponsiveContainer width="100%" height={200}>
                    <AreaChart data={data.monthly} margin={{ left: 10, right: 10, top: 5 }}>
                        <defs>
                            <linearGradient id="trendFill" x1="0" y1="0" x2="0" y2="1">
                                <stop offset="0%" stopColor="#2DD4BF" stopOpacity={0.28} />
                                <stop offset="100%" stopColor="#2DD4BF" stopOpacity={0} />
                            </linearGradient>
                        </defs>
                        <CartesianGrid strokeDasharray="3 3" stroke="rgba(42,58,80,0.55)" vertical={false} />
                        <XAxis dataKey="month" tick={AXIS_DIM} axisLine={false} tickLine={false} />
                        <YAxis tick={AXIS} axisLine={false} tickLine={false} tickFormatter={v => v >= 1000000 ? `${(v / 1000000).toFixed(2)}M` : `${(v / 1000).toFixed(0)}k`} />
                        <Tooltip formatter={v => v.toLocaleString()} contentStyle={TOOLTIP} cursor={{ stroke: "#2A3A50" }} />
                        <Area type="monotone" dataKey="properties" stroke="#2DD4BF" fill="url(#trendFill)" strokeWidth={2} animationDuration={900}
                            activeDot={{ r: 4, fill: "#2DD4BF", stroke: "#0B1220", strokeWidth: 2 }} />
                    </AreaChart>
                </ResponsiveContainer>
            </div>

            {/* District table */}
            <div className="dash-section" data-tour="home-summary" style={{ padding: 0 }}>
                <div className="px-3.5 py-3" style={{ borderBottom: "1px solid var(--line)" }}>
                    <div className="dash-section-title" style={{ marginBottom: 0 }}>District-wise summary</div>
                </div>
                <div style={{ overflowX: "auto" }}>
                    <table className="data-table">
                        <thead>
                            <tr>
                                <th>District</th>
                                <th style={{ textAlign: "right" }}>Properties</th>
                                <th style={{ textAlign: "right" }}>Open plots</th>
                                <th style={{ textAlign: "right" }}>Water bodies</th>
                                <th style={{ textAlign: "right" }}>Changes</th>
                                <th style={{ textAlign: "center" }}>Status</th>
                            </tr>
                        </thead>
                        <tbody>
                            {DISTRICT_NAMES.map(name => {
                                const d = DISTRICT_DATA[name];
                                const isSelected = selectedDistrict === name;
                                return (
                                    <tr key={name}
                                        onClick={() => setSelectedDistrict(isSelected ? "" : name)}
                                        style={{ cursor: "pointer", background: isSelected ? "var(--signal-dim)" : undefined, boxShadow: isSelected ? "inset 2px 0 0 var(--signal)" : undefined }}
                                    >
                                        <td style={{ fontWeight: 500, color: isSelected ? "var(--signal)" : "var(--text)" }}>{name}</td>
                                        <td className="mono" style={{ textAlign: "right" }}>{d.stats.properties}</td>
                                        <td className="mono" style={{ textAlign: "right" }}>{d.stats.plots}</td>
                                        <td className="mono" style={{ textAlign: "right" }}>{d.stats.water}</td>
                                        <td className="mono" style={{ textAlign: "right" }}>{d.stats.changes}</td>
                                        <td style={{ textAlign: "center" }}>
                                            <Pill colour="var(--ok)"><span className="live-dot" /> Active</Pill>
                                        </td>
                                    </tr>
                                );
                            })}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    );
}
