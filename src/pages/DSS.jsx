import React, { useState, useRef, useEffect } from "react";
import mapboxgl from "mapbox-gl";
import "mapbox-gl/dist/mapbox-gl.css";
import {
    BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer,
    PieChart, Pie, Cell, Legend,
} from "recharts";

const MAPBOX_TOKEN = import.meta.env.VITE_MAPBOX_TOKEN;

const DISTRICTS_LIST = [
    { name: "Visakhapatnam", center: [83.25, 17.93], zoom: 11 },
    { name: "Vijayawada", center: [80.62, 16.51], zoom: 11 },
    { name: "Guntur", center: [80.45, 16.30], zoom: 11 },
    { name: "Anantapur", center: [77.60, 14.68], zoom: 10 },
    { name: "Nellore", center: [79.99, 14.44], zoom: 10 },
];

const DATA_TYPES = [
    "Land Use Classification",
    "Property Identification",
    "Change Detection",
    "Water Body Analysis",
    "Road Network",
    "Building Footprints",
];

function generateReport(district) {
    return {
        district,
        generatedAt: new Date().toLocaleString(),
        summary: {
            "Total Properties": Math.floor(100000 + Math.random() * 200000).toLocaleString(),
            "Open Plots": Math.floor(20000 + Math.random() * 50000).toLocaleString(),
            "Water Bodies": Math.floor(50 + Math.random() * 500).toLocaleString(),
            "Road Length (km)": Math.floor(500 + Math.random() * 2000).toLocaleString(),
            "Built-up Area (km²)": Math.floor(50 + Math.random() * 200).toLocaleString(),
            "Green Cover (%)": (20 + Math.random() * 40).toFixed(1),
        },
        landUse: [
            { name: "Built-up", value: 28 + Math.floor(Math.random() * 10), color: "#d97706" },
            { name: "Vegetation", value: 22 + Math.floor(Math.random() * 10), color: "#16a34a" },
            { name: "Agriculture", value: 18 + Math.floor(Math.random() * 8), color: "#65a30d" },
            { name: "Water", value: 5 + Math.floor(Math.random() * 8), color: "#2563eb" },
            { name: "Barren", value: 8 + Math.floor(Math.random() * 8), color: "#9ca3af" },
        ],
        monthly: [
            { month: "Jul", changes: 120 + Math.floor(Math.random() * 200) },
            { month: "Aug", changes: 150 + Math.floor(Math.random() * 200) },
            { month: "Sep", changes: 90 + Math.floor(Math.random() * 200) },
            { month: "Oct", changes: 180 + Math.floor(Math.random() * 200) },
            { month: "Nov", changes: 130 + Math.floor(Math.random() * 200) },
            { month: "Dec", changes: 160 + Math.floor(Math.random() * 200) },
        ],
    };
}

export default function DSS() {
    const [selectedState] = useState("Andhra Pradesh");
    const [selectedDistrict, setSelectedDistrict] = useState("");
    const [selectedDataTypes, setSelectedDataTypes] = useState(["Land Use Classification"]);
    const [dateFrom, setDateFrom] = useState("2025-01-01");
    const [dateTo, setDateTo] = useState("2025-02-28");
    const [report, setReport] = useState(null);
    const [generating, setGenerating] = useState(false);

    const mapContainerRef = useRef(null);
    const mapRef = useRef(null);

    useEffect(() => {
        if (!mapContainerRef.current) return;
        mapboxgl.accessToken = MAPBOX_TOKEN;
        const map = new mapboxgl.Map({
            container: mapContainerRef.current,
            style: "mapbox://styles/mapbox/satellite-streets-v12",
            center: [80.0, 15.9],
            zoom: 6,
        });
        map.addControl(new mapboxgl.NavigationControl(), "top-right");
        mapRef.current = map;
        return () => map.remove();
    }, []);

    useEffect(() => {
        if (mapRef.current && selectedDistrict) {
            const dist = DISTRICTS_LIST.find(d => d.name === selectedDistrict);
            if (dist) mapRef.current.flyTo({ center: dist.center, zoom: dist.zoom, duration: 1500 });
        }
    }, [selectedDistrict]);

    const toggleDataType = (dt) => {
        setSelectedDataTypes(prev => prev.includes(dt) ? prev.filter(d => d !== dt) : [...prev, dt]);
    };

    const handleGenerate = async () => {
        if (!selectedDistrict) return;
        setGenerating(true);
        await new Promise(r => setTimeout(r, 2000));
        setReport(generateReport(selectedDistrict));
        setGenerating(false);
    };

    const exportReport = () => {
        if (!report) return;
        const lines = [
            `Decision Support System Report — ${report.district}`,
            `Generated: ${report.generatedAt}`,
            `State: ${selectedState}`,
            `Period: ${dateFrom} to ${dateTo}`,
            `Data Types: ${selectedDataTypes.join(", ")}`,
            "", "=== Summary ===",
            ...Object.entries(report.summary).map(([k, v]) => `${k}: ${v}`),
            "", "=== Land Use ===",
            ...report.landUse.map(l => `${l.name}: ${l.value}%`),
        ];
        const blob = new Blob([lines.join("\n")], { type: "text/plain" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = `DSS_Report_${report.district}_${new Date().toISOString().slice(0, 10)}.txt`;
        a.click();
    };

    return (
        <div className="flex flex-col lg:flex-row w-full" style={{ height: "calc(100vh - 260px)", minHeight: "500px" }}>
            {/* Map */}
            <div className="flex-1 relative" style={{ minHeight: "400px" }}>
                <div ref={mapContainerRef} style={{ position: "absolute", top: 0, left: 0, right: 0, bottom: 0 }} />
            </div>

            {/* Sidebar */}
            <div className="w-full lg:w-[380px] bg-white border-l border-gray-200 overflow-y-auto">
                <div className="p-4 border-b border-gray-200 bg-[#0B5FA5] text-white">
                    <h2 className="text-base font-bold">Decision Support System</h2>
                    <p className="text-xs text-white/70 mt-0.5">Generate reports for areas of interest</p>
                </div>

                <div className="p-4 space-y-3 border-b border-gray-200">
                    <div>
                        <label className="text-xs font-medium text-gray-600 block mb-1">State</label>
                        <input value={selectedState} disabled className="w-full bg-gray-100 text-sm border border-gray-200 rounded-lg px-3 py-2" />
                    </div>
                    <div>
                        <label className="text-xs font-medium text-gray-600 block mb-1">District</label>
                        <select value={selectedDistrict} onChange={e => setSelectedDistrict(e.target.value)}
                            className="w-full bg-white text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-[#0B5FA5]">
                            <option value="">Select District...</option>
                            {DISTRICTS_LIST.map(d => <option key={d.name} value={d.name}>{d.name}</option>)}
                        </select>
                    </div>
                    <div className="grid grid-cols-2 gap-2">
                        <div>
                            <label className="text-xs font-medium text-gray-600 block mb-1">From</label>
                            <input type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)}
                                className="w-full text-xs border border-gray-200 rounded-lg px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-[#0B5FA5]" />
                        </div>
                        <div>
                            <label className="text-xs font-medium text-gray-600 block mb-1">To</label>
                            <input type="date" value={dateTo} onChange={e => setDateTo(e.target.value)}
                                className="w-full text-xs border border-gray-200 rounded-lg px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-[#0B5FA5]" />
                        </div>
                    </div>
                </div>

                <div className="p-4 border-b border-gray-200">
                    <label className="text-xs font-medium text-gray-600 block mb-2">Data Types</label>
                    <div className="space-y-1.5">
                        {DATA_TYPES.map(dt => (
                            <label key={dt} className="flex items-center gap-2 cursor-pointer">
                                <input type="checkbox" checked={selectedDataTypes.includes(dt)} onChange={() => toggleDataType(dt)}
                                    className="rounded border-gray-300 text-[#0B5FA5] focus:ring-[#0B5FA5]" />
                                <span className="text-xs text-gray-700">{dt}</span>
                            </label>
                        ))}
                    </div>
                </div>

                <div className="p-4 border-b border-gray-200">
                    <button onClick={handleGenerate} disabled={!selectedDistrict || generating}
                        className={`w-full py-2.5 rounded-lg text-sm font-semibold transition-colors ${selectedDistrict && !generating ? "bg-[#0B5FA5] text-white hover:bg-[#094d87]" : "bg-gray-200 text-gray-400 cursor-not-allowed"
                            }`}>
                        {generating ? "Generating..." : "Generate Report"}
                    </button>
                </div>

                {report && (
                    <div className="p-4 space-y-4">
                        <div className="flex items-center justify-between">
                            <h3 className="text-sm font-bold text-gray-900">{report.district} Report</h3>
                            <button onClick={exportReport} className="text-xs text-[#0B5FA5] hover:text-[#094d87] font-medium">📥 Export</button>
                        </div>
                        <p className="text-[10px] text-gray-400">{report.generatedAt}</p>
                        <div className="grid grid-cols-2 gap-2">
                            {Object.entries(report.summary).map(([k, v]) => (
                                <div key={k} className="bg-gray-50 rounded-lg p-2">
                                    <p className="text-[10px] text-gray-400">{k}</p>
                                    <p className="text-sm font-bold text-gray-900">{v}</p>
                                </div>
                            ))}
                        </div>
                        <div>
                            <p className="text-xs font-medium text-gray-600 mb-2">Land Use Distribution</p>
                            <ResponsiveContainer width="100%" height={160}>
                                <PieChart>
                                    <Pie data={report.landUse} cx="50%" cy="50%" innerRadius={35} outerRadius={60} dataKey="value" paddingAngle={2}>
                                        {report.landUse.map((e, i) => <Cell key={i} fill={e.color} />)}
                                    </Pie>
                                    <Legend iconSize={6} wrapperStyle={{ fontSize: 9 }} />
                                    <Tooltip formatter={v => `${v}%`} />
                                </PieChart>
                            </ResponsiveContainer>
                        </div>
                        <div>
                            <p className="text-xs font-medium text-gray-600 mb-2">Monthly Changes</p>
                            <ResponsiveContainer width="100%" height={120}>
                                <BarChart data={report.monthly}>
                                    <XAxis dataKey="month" tick={{ fontSize: 9 }} />
                                    <YAxis tick={{ fontSize: 9 }} />
                                    <Tooltip />
                                    <Bar dataKey="changes" fill="#0B5FA5" radius={[3, 3, 0, 0]} />
                                </BarChart>
                            </ResponsiveContainer>
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
