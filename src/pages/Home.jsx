import React, { useEffect, useRef, useState, useMemo } from "react";
import mapboxgl from "mapbox-gl";
import "mapbox-gl/dist/mapbox-gl.css";
import StatCard from "../components/StatCard";
import {
    BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer,
    PieChart, Pie, Cell, Legend, AreaChart, Area, CartesianGrid,
} from "recharts";

const MAPBOX_TOKEN = import.meta.env.VITE_MAPBOX_TOKEN;

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
    const [selectedDistrict, setSelectedDistrict] = useState("");

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

    // Fly to district on selection
    useEffect(() => {
        if (mapRef.current && selectedDistrict && DISTRICT_DATA[selectedDistrict]) {
            const d = DISTRICT_DATA[selectedDistrict];
            mapRef.current.flyTo({ center: d.center, zoom: d.zoom, duration: 1500 });
        } else if (mapRef.current && !selectedDistrict) {
            mapRef.current.flyTo({ center: [80.0, 15.9], zoom: 6.2, duration: 1500 });
        }
    }, [selectedDistrict]);

    return (
        <div className="space-y-6">
            {/* Header with selectors */}
            <div className="bg-white rounded-lg border border-gray-200 p-4 sm:p-5 shadow-sm">
                <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
                    <div>
                        <h2 className="text-lg sm:text-xl font-bold text-gray-900">
                            Dashboard — {selectedDistrict || "Andhra Pradesh"} Urban Monitoring
                        </h2>
                        <p className="text-sm text-gray-500 mt-1">
                            AI-powered property identification and land use mapping statistics
                        </p>
                    </div>
                    <div className="flex items-center gap-3">
                        <div>
                            <label className="text-[10px] font-medium text-gray-400 block mb-0.5">STATE</label>
                            <select disabled className="bg-gray-100 border border-gray-200 rounded-lg px-3 py-1.5 text-sm text-gray-700">
                                <option>Andhra Pradesh</option>
                            </select>
                        </div>
                        <div>
                            <label className="text-[10px] font-medium text-gray-400 block mb-0.5">DISTRICT</label>
                            <select
                                value={selectedDistrict}
                                onChange={e => setSelectedDistrict(e.target.value)}
                                className="bg-white border border-gray-200 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-[#0B5FA5]"
                            >
                                <option value="">All Districts</option>
                                {DISTRICT_NAMES.map(d => <option key={d} value={d}>{d}</option>)}
                            </select>
                        </div>
                    </div>
                </div>
            </div>

            {/* Stat cards */}
            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
                {STATS.map((s) => (
                    <StatCard key={s.label} {...s} />
                ))}
            </div>

            {/* Charts + Map */}
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
                {/* Bar chart */}
                <div className="bg-white rounded-lg border border-gray-200 p-4 shadow-sm">
                    <h3 className="text-sm font-semibold text-gray-900 mb-3">Properties by District</h3>
                    <ResponsiveContainer width="100%" height={220}>
                        <BarChart data={districtBarData} layout="vertical" margin={{ left: 0, right: 10 }}>
                            <XAxis type="number" tick={{ fontSize: 10 }} tickFormatter={v => `${(v / 1000).toFixed(0)}k`} />
                            <YAxis type="category" dataKey="name" tick={{ fontSize: 10 }} width={90} />
                            <Tooltip formatter={v => v.toLocaleString()} />
                            <Bar dataKey="Properties" fill="#0B5FA5" radius={[0, 4, 4, 0]} />
                        </BarChart>
                    </ResponsiveContainer>
                </div>

                {/* Mini map */}
                <div className="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden">
                    <div className="p-4 pb-2">
                        <h3 className="text-sm font-semibold text-gray-900">
                            {selectedDistrict || "Andhra Pradesh"} Overview
                        </h3>
                    </div>
                    <div ref={mapContainerRef} style={{ height: "240px", width: "100%" }} />
                </div>

                {/* Pie chart */}
                <div className="bg-white rounded-lg border border-gray-200 p-4 shadow-sm">
                    <h3 className="text-sm font-semibold text-gray-900 mb-3">Land Use Distribution</h3>
                    <ResponsiveContainer width="100%" height={220}>
                        <PieChart>
                            <Pie data={data.landUse} cx="50%" cy="50%" innerRadius={50} outerRadius={80} dataKey="value" paddingAngle={2}>
                                {data.landUse.map((e, i) => <Cell key={i} fill={e.color} />)}
                            </Pie>
                            <Legend iconSize={8} wrapperStyle={{ fontSize: 10 }} />
                            <Tooltip formatter={v => `${v}%`} />
                        </PieChart>
                    </ResponsiveContainer>
                </div>
            </div>

            {/* Monthly trend */}
            <div className="bg-white rounded-lg border border-gray-200 p-4 shadow-sm">
                <h3 className="text-sm font-semibold text-gray-900 mb-3">Monthly Property Identification Trend</h3>
                <ResponsiveContainer width="100%" height={200}>
                    <AreaChart data={data.monthly} margin={{ left: 10, right: 10, top: 5 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
                        <XAxis dataKey="month" tick={{ fontSize: 11 }} />
                        <YAxis tick={{ fontSize: 10 }} tickFormatter={v => v >= 1000000 ? `${(v / 1000000).toFixed(2)}M` : `${(v / 1000).toFixed(0)}k`} />
                        <Tooltip formatter={v => v.toLocaleString()} />
                        <Area type="monotone" dataKey="properties" stroke="#0B5FA5" fill="#0B5FA520" strokeWidth={2} />
                    </AreaChart>
                </ResponsiveContainer>
            </div>

            {/* District table */}
            <div className="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden">
                <div className="p-4 border-b border-gray-100">
                    <h3 className="text-sm font-semibold text-gray-900">District-wise Summary</h3>
                </div>
                <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                        <thead className="bg-gray-50 text-gray-600">
                            <tr>
                                <th className="py-2.5 px-4 text-left font-medium">District</th>
                                <th className="py-2.5 px-4 text-right font-medium">Properties</th>
                                <th className="py-2.5 px-4 text-right font-medium">Open Plots</th>
                                <th className="py-2.5 px-4 text-right font-medium">Water Bodies</th>
                                <th className="py-2.5 px-4 text-right font-medium">Changes</th>
                                <th className="py-2.5 px-4 text-center font-medium">Status</th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-gray-100">
                            {DISTRICT_NAMES.map(name => {
                                const d = DISTRICT_DATA[name];
                                const isSelected = selectedDistrict === name;
                                return (
                                    <tr key={name}
                                        className={`hover:bg-gray-50 transition-colors cursor-pointer ${isSelected ? "bg-blue-50" : ""}`}
                                        onClick={() => setSelectedDistrict(isSelected ? "" : name)}
                                    >
                                        <td className="py-2.5 px-4 font-medium text-gray-900">
                                            {isSelected && <span className="text-[#0B5FA5] mr-1">▶</span>}
                                            {name}
                                        </td>
                                        <td className="py-2.5 px-4 text-right">{d.stats.properties}</td>
                                        <td className="py-2.5 px-4 text-right">{d.stats.plots}</td>
                                        <td className="py-2.5 px-4 text-right">{d.stats.water}</td>
                                        <td className="py-2.5 px-4 text-right">{d.stats.changes}</td>
                                        <td className="py-2.5 px-4 text-center">
                                            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-700">
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
