import React, { useEffect, useRef, useState, useMemo } from "react";
import mapboxgl from "mapbox-gl";
import "mapbox-gl/dist/mapbox-gl.css";
import StatCard from "../components/StatCard";
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
        { icon: "🏠", label: "Total Properties", value: s.properties, trend: t.properties, color: "#0B5FA5" },
        { icon: "📐", label: "Open Plots", value: s.plots, trend: t.plots, color: "#16a34a" },
        { icon: "💧", label: "Water Bodies", value: s.water, trend: t.water, color: "#2563eb" },
        { icon: "🔄", label: "Change Detections", value: s.changes, trend: t.changes, color: "#d97706" },
        { icon: "🛣️", label: "Road Network (km)", value: s.roads, trend: 0, color: "#6b7280" },
        { icon: "🏙️", label: "Built-up (km²)", value: s.builtup, trend: 0, color: "#7c3aed" },
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
        map.addControl(new mapboxgl.NavigationControl(), "top-right");
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
                            outline: { "line-color": "#7B2D8E", "line-width": 3 },
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

    return (
        <div className="fade-in" style={{ display: "flex", flexDirection: "column", gap: "24px" }}>
            {/* Header with selectors */}
            <div className="dash-section">
                <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: "12px" }}>
                    <div>
                        <h2 style={{ fontSize: "18px", fontWeight: 800, color: "var(--text-primary)" }}>
                            Dashboard — {selectedDistrict || "Andhra Pradesh"} Urban Monitoring
                        </h2>
                        <p style={{ fontSize: "13px", color: "var(--text-muted)", marginTop: "4px" }}>
                            AI-powered property identification and land use mapping statistics
                        </p>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
                        <div>
                            <label style={{ fontSize: "10px", fontWeight: 600, color: "var(--text-muted)", display: "block", marginBottom: "4px", textTransform: "uppercase", letterSpacing: "0.8px" }}>STATE</label>
                            <select disabled className="dark-select" style={{ width: "auto" }}>
                                <option>Andhra Pradesh</option>
                            </select>
                        </div>
                        <div>
                            <label style={{ fontSize: "10px", fontWeight: 600, color: "var(--text-muted)", display: "block", marginBottom: "4px", textTransform: "uppercase", letterSpacing: "0.8px" }}>DISTRICT</label>
                            <select
                                value={selectedDistrict}
                                onChange={e => setSelectedDistrict(e.target.value)}
                                className="dark-select"
                                data-tour="home-district"
                                style={{ width: "auto" }}
                            >
                                <option value="">All Districts</option>
                                {DISTRICT_NAMES.map(d => <option key={d} value={d}>{d}</option>)}
                            </select>
                        </div>
                    </div>
                </div>
            </div>

            {/* Stat cards */}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))", gap: "12px" }}>
                {STATS.map((s) => (
                    <StatCard key={s.label} {...s} />
                ))}
            </div>

            {/* Charts + Map */}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "16px" }}>
                {/* Bar chart */}
                <div className="dash-section">
                    <div className="dash-section-title">Properties by District</div>
                    <ResponsiveContainer width="100%" height={220}>
                        <BarChart data={districtBarData} layout="vertical" margin={{ left: 0, right: 10 }}>
                            <XAxis type="number" tick={{ fontSize: 10, fill: '#64748b' }} tickFormatter={v => `${(v / 1000).toFixed(0)}k`} />
                            <YAxis type="category" dataKey="name" tick={{ fontSize: 10, fill: '#94a3b8' }} width={90} />
                            <Tooltip formatter={v => v.toLocaleString()} contentStyle={{ background: '#1e293b', border: '1px solid rgba(71,85,105,0.4)', borderRadius: '6px', color: '#f1f5f9' }} />
                            <Bar dataKey="Properties" fill="#14b8a6" radius={[0, 4, 4, 0]} />
                        </BarChart>
                    </ResponsiveContainer>
                </div>

                {/* Mini map */}
                <div className="dash-section" style={{ padding: 0, overflow: "hidden" }}>
                    <div style={{ padding: "16px 16px 8px" }}>
                        <div className="dash-section-title" style={{ marginBottom: 0 }}>
                            {selectedDistrict || "Andhra Pradesh"} Overview
                        </div>
                    </div>
                    <div ref={mapContainerRef} style={{ height: "240px", width: "100%" }} />
                </div>

                {/* Pie chart */}
                <div className="dash-section">
                    <div className="dash-section-title">Land Use Distribution</div>
                    <ResponsiveContainer width="100%" height={220}>
                        <PieChart>
                            <Pie data={data.landUse} cx="50%" cy="50%" innerRadius={50} outerRadius={80} dataKey="value" paddingAngle={2}>
                                {data.landUse.map((e, i) => <Cell key={i} fill={e.color} />)}
                            </Pie>
                            <Legend iconSize={8} wrapperStyle={{ fontSize: 10, color: '#94a3b8' }} />
                            <Tooltip formatter={v => `${v}%`} contentStyle={{ background: '#1e293b', border: '1px solid rgba(71,85,105,0.4)', borderRadius: '6px', color: '#f1f5f9' }} />
                        </PieChart>
                    </ResponsiveContainer>
                </div>
            </div>

            {/* Monthly trend */}
            <div className="dash-section">
                <div className="dash-section-title">Monthly Property Identification Trend</div>
                <ResponsiveContainer width="100%" height={200}>
                    <AreaChart data={data.monthly} margin={{ left: 10, right: 10, top: 5 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke="rgba(71,85,105,0.3)" />
                        <XAxis dataKey="month" tick={{ fontSize: 11, fill: '#94a3b8' }} />
                        <YAxis tick={{ fontSize: 10, fill: '#64748b' }} tickFormatter={v => v >= 1000000 ? `${(v / 1000000).toFixed(2)}M` : `${(v / 1000).toFixed(0)}k`} />
                        <Tooltip formatter={v => v.toLocaleString()} contentStyle={{ background: '#1e293b', border: '1px solid rgba(71,85,105,0.4)', borderRadius: '6px', color: '#f1f5f9' }} />
                        <Area type="monotone" dataKey="properties" stroke="#14b8a6" fill="rgba(20,184,166,0.15)" strokeWidth={2} />
                    </AreaChart>
                </ResponsiveContainer>
            </div>

            {/* District table */}
            <div className="dash-section" data-tour="home-summary" style={{ padding: 0, overflow: "hidden" }}>
                <div style={{ padding: "16px", borderBottom: "1px solid var(--border-default)" }}>
                    <div className="dash-section-title" style={{ marginBottom: 0 }}>District-wise Summary</div>
                </div>
                <div style={{ overflowX: "auto" }}>
                    <table style={{ width: "100%", fontSize: "13px", borderCollapse: "collapse" }}>
                        <thead>
                            <tr style={{ background: "var(--bg-tertiary)" }}>
                                <th style={{ padding: "10px 16px", textAlign: "left", fontWeight: 600, color: "var(--text-muted)", fontSize: "11px", textTransform: "uppercase", letterSpacing: "0.5px" }}>District</th>
                                <th style={{ padding: "10px 16px", textAlign: "right", fontWeight: 600, color: "var(--text-muted)", fontSize: "11px", textTransform: "uppercase", letterSpacing: "0.5px" }}>Properties</th>
                                <th style={{ padding: "10px 16px", textAlign: "right", fontWeight: 600, color: "var(--text-muted)", fontSize: "11px", textTransform: "uppercase", letterSpacing: "0.5px" }}>Open Plots</th>
                                <th style={{ padding: "10px 16px", textAlign: "right", fontWeight: 600, color: "var(--text-muted)", fontSize: "11px", textTransform: "uppercase", letterSpacing: "0.5px" }}>Water Bodies</th>
                                <th style={{ padding: "10px 16px", textAlign: "right", fontWeight: 600, color: "var(--text-muted)", fontSize: "11px", textTransform: "uppercase", letterSpacing: "0.5px" }}>Changes</th>
                                <th style={{ padding: "10px 16px", textAlign: "center", fontWeight: 600, color: "var(--text-muted)", fontSize: "11px", textTransform: "uppercase", letterSpacing: "0.5px" }}>Status</th>
                            </tr>
                        </thead>
                        <tbody>
                            {DISTRICT_NAMES.map(name => {
                                const d = DISTRICT_DATA[name];
                                const isSelected = selectedDistrict === name;
                                return (
                                    <tr key={name}
                                        style={{ cursor: "pointer", borderBottom: "1px solid rgba(71,85,105,0.2)", background: isSelected ? "var(--accent-dim)" : "transparent", transition: "background 0.15s" }}
                                        onClick={() => setSelectedDistrict(isSelected ? "" : name)}
                                        onMouseEnter={e => { if (!isSelected) e.currentTarget.style.background = 'rgba(20,184,166,0.05)'; }}
                                        onMouseLeave={e => { if (!isSelected) e.currentTarget.style.background = 'transparent'; }}
                                    >
                                        <td style={{ padding: "10px 16px", fontWeight: 600, color: "var(--text-primary)" }}>
                                            {isSelected && <span style={{ color: "var(--accent)", marginRight: "4px" }}>▶</span>}
                                            {name}
                                        </td>
                                        <td style={{ padding: "10px 16px", textAlign: "right", color: "var(--text-secondary)", fontFamily: "var(--font-mono)", fontSize: "12px" }}>{d.stats.properties}</td>
                                        <td style={{ padding: "10px 16px", textAlign: "right", color: "var(--text-secondary)", fontFamily: "var(--font-mono)", fontSize: "12px" }}>{d.stats.plots}</td>
                                        <td style={{ padding: "10px 16px", textAlign: "right", color: "var(--text-secondary)", fontFamily: "var(--font-mono)", fontSize: "12px" }}>{d.stats.water}</td>
                                        <td style={{ padding: "10px 16px", textAlign: "right", color: "var(--text-secondary)", fontFamily: "var(--font-mono)", fontSize: "12px" }}>{d.stats.changes}</td>
                                        <td style={{ padding: "10px 16px", textAlign: "center" }}>
                                            <span style={{ display: "inline-flex", alignItems: "center", gap: "4px", padding: "2px 10px", borderRadius: "12px", fontSize: "11px", fontWeight: 600, background: "var(--green-dim)", color: "var(--green)" }}>
                                                ● Active
                                            </span>
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
