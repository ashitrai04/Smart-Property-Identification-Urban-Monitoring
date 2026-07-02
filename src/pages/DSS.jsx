import React, { useState, useRef, useEffect } from "react";
import mapboxgl from "mapbox-gl";
import "mapbox-gl/dist/mapbox-gl.css";
import {
    BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell,
} from "recharts";
import jsPDF from "jspdf";
import autoTable from "jspdf-autotable";
import { registerTour, unregisterTour } from "../tour/tourBus";
import { API_BASE } from "../utils/mapLayers";
import { fetchActivitiesInRange, logActivity } from "../lib/activityLog";

const MAPBOX_TOKEN = import.meta.env.VITE_MAPBOX_TOKEN;

const DISTRICTS_LIST = [
    { name: "Visakhapatnam", key: "visakhapatnam", center: [83.25, 17.93], zoom: 11 },
    { name: "Vijayawada", key: "vijayawada", center: [80.62, 16.51], zoom: 11 },
    { name: "Guntur", key: "guntur", center: [80.45, 16.30], zoom: 11 },
    { name: "Anantapur", key: "anantapur", center: [77.60, 14.68], zoom: 10 },
    { name: "Nellore", key: "nellore", center: [79.99, 14.44], zoom: 10 },
];

const DATA_TYPES = [
    "Land Use Classification",
    "Property Identification",
    "Change Detection",
    "Water Body Analysis",
    "Road Network",
    "Building Footprints",
];

// Real validation metrics of the deployed SegFormer-B5 model (technical report)
const MODEL_CARD = {
    name: "SegFormer-B5 (84M params)",
    miou: 0.5474,
    classIoU: { Background: 0.880, Building: 0.430, Road: 0.431, Water: 0.635, "Open Land": 0.281 },
};

const km2 = (m2) => (m2 > 0 ? (m2 / 1e6).toFixed(2) : null);

/**
 * Build a report from REAL sources only:
 *  - district inventory from the platform backend (/districts/{key} + /stats)
 *  - platform activity (AI runs, AOI analyses) actually recorded in the period
 */
async function generateReport(district, dateFrom, dateTo) {
    const key = district.key;

    const [meta, stats] = await Promise.all([
        fetch(`${API_BASE}/api/districts/${key}`).then(r => r.ok ? r.json() : null).catch(() => null),
        fetch(`${API_BASE}/api/districts/${key}/stats`).then(r => r.ok ? r.json() : null).catch(() => null),
    ]);
    const s = stats?.stats || {};
    const counts = meta?.layer_counts || {};

    const buildings = s.buildings?.count ?? counts.buildings ?? 0;
    const waterbodies = s.waterbodies?.count ?? counts.waterbodies ?? 0;
    const openareas = s.openareas?.count ?? counts.openareas ?? 0;
    const roads = s.roads?.count ?? counts.roads ?? 0;
    const builtAreaKm2 = km2(s.buildings?.total_area_m2 || 0);
    const avgPropM2 = s.buildings?.avg_area_m2 ? Math.round(s.buildings.avg_area_m2) : null;

    // Real platform activity in the reporting period (this district + custom uploads)
    const { rows: activity, source: activitySource } = await fetchActivitiesInRange(dateFrom, dateTo, { district: district.name });

    const byType = {};
    activity.forEach(r => { byType[r.type] = (byType[r.type] || 0) + 1; });

    // Aggregate REAL change-detection results recorded in the period
    const cdRuns = activity.filter(r => r.type === "Change Detection" && r.status === "Completed" && r.metrics);
    const cdAvg = {};
    if (cdRuns.length) {
        const keys = ["New Construction", "Demolished", "New Road", "Other Change", "Unchanged"];
        keys.forEach(k => {
            const vals = cdRuns.map(r => parseFloat(r.metrics[k])).filter(v => !Number.isNaN(v));
            if (vals.length) cdAvg[k] = +(vals.reduce((a, b) => a + b, 0) / vals.length).toFixed(2);
        });
    }

    // Real monthly activity distribution
    const monthly = {};
    activity.forEach(r => {
        const d = new Date(r.created_at);
        const label = d.toLocaleString("en", { month: "short", year: "2-digit" });
        monthly[label] = (monthly[label] || 0) + 1;
    });

    return {
        district: district.name, key,
        generatedAt: new Date().toLocaleString(),
        period: { from: dateFrom, to: dateTo },
        backendOnline: !!(meta || stats),
        inventory: {
            buildings, waterbodies, openareas, roads,
            builtAreaKm2, avgPropM2,
            totalFeatures: stats?.total_features ?? meta?.total_features ?? 0,
            roadTypes: s.roads?.road_types || null,
        },
        activity, activitySource, byType,
        cd: { runs: cdRuns.length, avg: cdAvg },
        monthly: Object.entries(monthly).map(([month, runs]) => ({ month, runs })),
    };
}

export default function DSS() {
    const [selectedState] = useState("Andhra Pradesh");
    const [selectedDistrict, setSelectedDistrict] = useState("");
    const [selectedDataTypes, setSelectedDataTypes] = useState(["Land Use Classification"]);
    const [dateFrom, setDateFrom] = useState(() => new Date(Date.now() - 90 * 86400000).toISOString().slice(0, 10));
    const [dateTo, setDateTo] = useState(() => new Date().toISOString().slice(0, 10));
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
        const dist = DISTRICTS_LIST.find(d => d.name === selectedDistrict);
        if (!dist) return;
        setGenerating(true);
        try {
            const newReport = await generateReport(dist, dateFrom, dateTo);
            setReport(newReport);
            logActivity({
                type: "Report Generated", district: dist.name, area: "Full District",
                status: "Completed",
                metrics: {
                    Buildings: newReport.inventory.buildings,
                    "Water Bodies": newReport.inventory.waterbodies,
                    "Activity entries": newReport.activity.length,
                },
                meta: { period: `${dateFrom} → ${dateTo}` },
            });
        } catch (e) {
            console.error("report failed:", e);
        }
        setGenerating(false);
    };

    // Expose DSS controls to the guided tour (re-register each render for fresh closures)
    useEffect(() => {
        registerTour("dss", {
            selectDistrict: (n) => setSelectedDistrict(n),
            setDates: (f, t) => { setDateFrom(f); setDateTo(t); },
            toggleDataType: (dt) => toggleDataType(dt),
            generate: () => handleGenerate(),
        });
    });
    useEffect(() => () => unregisterTour("dss"), []);

    const exportReport = () => {
        if (!report) return;
        const inv = report.inventory;

        const doc = new jsPDF();
        const margin = 20;
        const pageWidth = doc.internal.pageSize.width;
        let startY = 30;

        const addHeader = (title) => {
            doc.addPage();
            doc.setFillColor(11, 95, 165);
            doc.rect(0, 0, pageWidth, 25, "F");
            doc.setTextColor(255, 255, 255);
            doc.setFont("helvetica", "bold");
            doc.setFontSize(16);
            doc.text(title, margin, 17);
            doc.setTextColor(0, 0, 0);
            startY = 40;
        };
        const renderText = (arr, fontSize = 11, fontStyle = "normal", indent = 0) => {
            doc.setFont("helvetica", fontStyle);
            doc.setFontSize(fontSize);
            arr.forEach(blob => {
                const lines = doc.splitTextToSize(blob, pageWidth - margin * 2 - indent);
                lines.forEach(line => {
                    if (startY > doc.internal.pageSize.height - 20) { doc.addPage(); startY = margin; }
                    doc.text(line, margin + indent, startY);
                    startY += (fontSize >= 14 ? 8 : 6);
                });
                startY += 4;
            });
            startY += 4;
        };
        const table = (head, body, opts = {}) => {
            autoTable(doc, {
                startY, head: [head], body, theme: "striped",
                headStyles: { fillColor: [11, 95, 165] },
                margin: { left: margin, right: margin }, ...opts,
            });
            startY = doc.lastAutoTable.finalY + 10;
        };

        // ── Title page ──
        doc.setFillColor(240, 245, 250);
        doc.rect(0, 0, pageWidth, doc.internal.pageSize.height, "F");
        doc.setTextColor(11, 95, 165);
        doc.setFont("helvetica", "bold");
        doc.setFontSize(26);
        doc.text("DISTRICT ANALYSIS REPORT", pageWidth / 2, 70, { align: "center" });
        doc.setFontSize(18);
        doc.setTextColor(50, 50, 50);
        doc.text(doc.splitTextToSize("AI-Enabled Smart Property Identification and Urban Monitoring System", pageWidth - 40), pageWidth / 2, 90, { align: "center" });
        doc.setFontSize(14);
        doc.setFont("helvetica", "normal");
        doc.text(`District: ${report.district}`, pageWidth / 2, 130, { align: "center" });
        doc.text(`State: ${selectedState}`, pageWidth / 2, 140, { align: "center" });
        doc.text(`Reporting Period: ${report.period.from} to ${report.period.to}`, pageWidth / 2, 150, { align: "center" });
        doc.setFontSize(12);
        doc.setFont("helvetica", "italic");
        doc.text(`Generated On: ${report.generatedAt}`, pageWidth / 2, 190, { align: "center" });
        doc.setFont("helvetica", "bold");
        doc.setFontSize(13);
        doc.setTextColor(11, 95, 165);
        doc.text("System-Generated Output for Urban Local Body (ULB) Officials", pageWidth / 2, 230, { align: "center" });
        doc.setTextColor(0, 0, 0);

        // ── 1. Executive Summary ──
        addHeader("1. Executive Summary");
        renderText([
            `This report is generated from the platform's live district database and the analysis activity actually performed on the platform between ${report.period.from} and ${report.period.to}.`,
        ]);
        renderText(["Key figures (live database):"], 12, "bold");
        renderText([
            `• ${inv.buildings.toLocaleString()} building footprints on record${inv.builtAreaKm2 ? `, totalling ${inv.builtAreaKm2} km² of built-up area` : ""}.`,
            inv.avgPropM2 ? `• Average property footprint: ${inv.avgPropM2} m².` : null,
            `• ${inv.waterbodies.toLocaleString()} water bodies and ${inv.openareas.toLocaleString()} open plots on record.`,
            `• ${report.activity.length} analysis activities recorded on the platform in the reporting period.`,
        ].filter(Boolean));
        renderText(["Model provenance:"], 12, "bold");
        renderText([
            `Detections are produced by ${MODEL_CARD.name}, validated at an overall mIoU of ${MODEL_CARD.miou} on a held-out set of 2,736 chips across five AP districts (per-class IoU is listed in Section 2).`,
        ]);

        // ── 2. Data Sources & Model ──
        addHeader("2. Data Sources & Model");
        renderText(["Data sources used by the platform:"], 12, "bold");
        renderText([
            "• Satellite imagery: ESRI World Imagery (R&D licensing; 1m/50cm GSD commercial imagery planned for production).",
            "• Label provenance: OpenStreetMap vectors (southern India) + Google Open Buildings footprints, rasterized via a QGIS pipeline.",
            "• District vector/raster layers served from the platform backend (GeoPackage per district, Cloudflare R2 storage).",
        ]);
        renderText(["Model validation metrics (real, from training):"], 12, "bold");
        table(["Class", "IoU"], Object.entries(MODEL_CARD.classIoU).map(([k, v]) => [k, v.toFixed(3)]).concat([["Overall mIoU", MODEL_CARD.miou.toFixed(4)]]));

        // ── 3. District Inventory ──
        addHeader("3. District Inventory (Live Database)");
        renderText([`Feature inventory for ${report.district} as currently held in the platform database:`]);
        table(["Metric", "Value"], [
            ["Building footprints", inv.buildings.toLocaleString()],
            ["Built-up area", inv.builtAreaKm2 ? `${inv.builtAreaKm2} km²` : "—"],
            ["Average property footprint", inv.avgPropM2 ? `${inv.avgPropM2} m²` : "—"],
            ["Water bodies", inv.waterbodies.toLocaleString()],
            ["Open plots", inv.openareas.toLocaleString()],
            ["Road features", inv.roads.toLocaleString()],
            ["Total classified features", inv.totalFeatures.toLocaleString()],
        ]);
        if (inv.roadTypes && Object.keys(inv.roadTypes).length) {
            renderText(["Road classification breakdown:"], 12, "bold");
            table(["Road class", "Count"], Object.entries(inv.roadTypes).map(([k, v]) => [k, String(v)]));
        }
        if (!inv.openareas && !inv.roads) {
            renderText(["Note: open-plot and road vector coverage for this district is still being ingested; counts will populate as dataset cleaning completes."], 9, "italic");
        }

        // ── 4. Change Detection (real runs) ──
        addHeader("4. Change Detection Summary");
        if (report.cd.runs > 0) {
            renderText([`${report.cd.runs} change-detection ${report.cd.runs === 1 ? "analysis was" : "analyses were"} performed on the platform in this period. Averaged results:`]);
            table(["Change category", "Avg. share of analysed area"], Object.entries(report.cd.avg).map(([k, v]) => [k, `${v}%`]));
        } else {
            renderText([
                "No change-detection analyses were recorded on the platform during this reporting period.",
                "To populate this section, run past-vs-present comparisons in Upload & Analysis → Change Detection; each run is logged automatically and aggregated here.",
            ]);
        }

        // ── 5. Platform Activity Log (real) ──
        addHeader("5. Platform Activity Log");
        if (report.activity.length) {
            renderText([`${report.activity.length} recorded activities in the period (${report.activitySource === "supabase" ? "cloud-synced log" : "this browser's local log"}):`]);
            table(["Date", "Type", "Target", "Result", "Status"],
                report.activity.slice(0, 25).map(r => [
                    new Date(r.created_at).toLocaleDateString(),
                    r.type,
                    (r.area || r.district || "—").slice(0, 34),
                    r.metrics ? Object.entries(r.metrics).slice(0, 2).map(([k, v]) => `${k}: ${v}`).join(", ").slice(0, 40) : "—",
                    r.status,
                ]), { styles: { fontSize: 8 } });
            if (report.activity.length > 25) renderText([`(showing the 25 most recent of ${report.activity.length})`], 9, "italic");
        } else {
            renderText(["No platform activity was recorded in this period."]);
        }

        // ── 6. Governance Insights ──
        addHeader("6. Governance Insights & Decision Support");
        renderText([
            inv.avgPropM2 && inv.avgPropM2 < 120
                ? `• The average footprint of ${inv.avgPropM2} m² indicates dense, small-parcel housing — ward-level AOI analysis (Mapping → Area of Interest) is recommended before regularization drives.`
                : `• Use ward-level AOI analysis (Mapping → Area of Interest) to obtain per-parcel counts before field verification.`,
            `• ${inv.waterbodies.toLocaleString()} monitored water bodies: run periodic change detection on their buffers to flag encroachment early.`,
            report.cd.runs > 0
                ? `• Averaged change results in this period show ${report.cd.avg["New Construction"] ?? 0}% new construction in analysed areas — prioritise field checks there.`
                : `• Schedule monthly change-detection runs for high-growth wards so this report can quantify construction trends.`,
            `• All figures above are reproducible: inventory from the live district database, activity from the platform log${report.activitySource === "supabase" ? " (cloud)" : " (local browser)"}.`,
        ]);

        // ── footer ──
        const pageCount = doc.internal.getNumberOfPages();
        for (let i = 1; i <= pageCount; i++) {
            doc.setPage(i);
            doc.setFontSize(8);
            doc.setFont("helvetica", "normal");
            doc.setTextColor(150);
            doc.setDrawColor(200, 200, 200);
            doc.line(margin, doc.internal.pageSize.height - 15, pageWidth - margin, doc.internal.pageSize.height - 15);
            doc.text("System Generated Output | ULB Official Documentation", margin, doc.internal.pageSize.height - 8);
            doc.text(`Page ${i} of ${pageCount}`, pageWidth - margin, doc.internal.pageSize.height - 8, { align: "right" });
        }

        doc.save(`District_Report_${report.district}_${new Date().toISOString().slice(0, 10)}.pdf`);
    };

    const typeChart = report ? Object.entries(report.byType).map(([type, n]) => ({ type: type.replace(" Detection", " Det."), n })) : [];
    const BAR_COLORS = ["#14b8a6", "#3b82f6", "#f59e0b", "#8b5cf6", "#ef4444", "#10b981"];

    return (
        <div className="flex flex-col lg:flex-row w-full h-full">
            {/* Map */}
            <div className="flex-1 relative" style={{ minHeight: "400px" }}>
                <div ref={mapContainerRef} style={{ position: "absolute", top: 0, left: 0, right: 0, bottom: 0 }} />
            </div>

            {/* Sidebar */}
            <div className="w-full lg:w-[400px] shrink-0 bg-[var(--bg-secondary)] border-l border-[var(--border-default)] overflow-y-auto">
                <div className="p-4 border-b border-[var(--border-default)] bg-gradient-to-r from-[var(--accent)] to-[#0a7a6a]">
                    <h2 className="text-base font-bold text-white">Decision Support System</h2>
                    <p className="text-xs text-white/70 mt-0.5">Reports from the live database & real platform activity</p>
                </div>

                <div className="p-4 space-y-3 border-b border-[var(--border-default)]" data-tour="dss-form">
                    <div>
                        <label className="text-xs font-medium text-[var(--text-muted)] block mb-1">State</label>
                        <input value={selectedState} disabled className="w-full bg-[var(--bg-primary)] text-[var(--text-primary)] text-sm border border-[var(--border-default)] rounded-lg px-3 py-2" />
                    </div>
                    <div data-tour="dss-district">
                        <label className="text-xs font-medium text-[var(--text-muted)] block mb-1">District</label>
                        <select value={selectedDistrict} onChange={e => setSelectedDistrict(e.target.value)}
                            className="w-full bg-[var(--bg-primary)] text-[var(--text-primary)] text-sm border border-[var(--border-default)] rounded-lg px-3 py-2 focus:outline-none focus:ring-1 focus:ring-[var(--accent)]">
                            <option value="">Select District...</option>
                            {DISTRICTS_LIST.map(d => <option key={d.name} value={d.name}>{d.name}</option>)}
                        </select>
                    </div>
                    <div className="grid grid-cols-2 gap-2" data-tour="dss-dates">
                        <div>
                            <label className="text-xs font-medium text-[var(--text-muted)] block mb-1">From</label>
                            <input type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)}
                                className="w-full bg-[var(--bg-primary)] text-[var(--text-primary)] text-xs border border-[var(--border-default)] rounded-lg px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-[var(--accent)]" />
                        </div>
                        <div>
                            <label className="text-xs font-medium text-[var(--text-muted)] block mb-1">To</label>
                            <input type="date" value={dateTo} onChange={e => setDateTo(e.target.value)}
                                className="w-full bg-[var(--bg-primary)] text-[var(--text-primary)] text-xs border border-[var(--border-default)] rounded-lg px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-[var(--accent)]" />
                        </div>
                    </div>
                </div>

                <div className="p-4 border-b border-[var(--border-default)]" data-tour="dss-datatypes">
                    <label className="text-xs font-medium text-[var(--text-muted)] block mb-2">Data Types</label>
                    <div className="space-y-1.5">
                        {DATA_TYPES.map(dt => (
                            <label key={dt} className="flex items-center gap-2 cursor-pointer" data-tour={`dss-dt-${dt.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`}>
                                <input type="checkbox" checked={selectedDataTypes.includes(dt)} onChange={() => toggleDataType(dt)}
                                    className="rounded border-[var(--border-default)] text-[var(--accent)] focus:ring-[#0B5FA5]" />
                                <span className="text-xs text-[var(--text-secondary)]">{dt}</span>
                            </label>
                        ))}
                    </div>
                </div>

                <div className="p-4 border-b border-[var(--border-default)]">
                    <button onClick={handleGenerate} disabled={!selectedDistrict || generating} data-tour="dss-generate"
                        className={`w-full py-2.5 rounded-lg text-sm font-semibold transition-colors ${selectedDistrict && !generating ? "bg-[var(--accent)] text-white hover:bg-[#094d87]" : "bg-[var(--bg-tertiary)] text-[var(--text-muted)] cursor-not-allowed"
                            }`}>
                        {generating ? "Generating..." : "Generate Report"}
                    </button>
                </div>

                {report && (
                    <div className="p-4 space-y-4">
                        <div className="flex flex-col gap-3">
                            <h3 className="text-sm font-bold text-[var(--text-primary)]">{report.district} Report Summary</h3>
                            <button onClick={exportReport}
                                className="w-full py-3 bg-green-600 hover:bg-green-700 text-white rounded-lg text-sm font-bold shadow-md transition-colors flex items-center justify-center gap-2">
                                <span>📥 Download District Report (PDF)</span>
                            </button>
                        </div>
                        <p className="text-[10px] text-[var(--text-muted)]">
                            {report.generatedAt} · inventory: {report.backendOnline ? "live database" : "backend unreachable"} · activity: {report.activitySource === "supabase" ? "cloud log" : "local log"}
                        </p>

                        {/* Real inventory tiles */}
                        <div className="grid grid-cols-2 gap-2">
                            {[
                                ["Buildings on record", report.inventory.buildings.toLocaleString()],
                                ["Built-up area", report.inventory.builtAreaKm2 ? `${report.inventory.builtAreaKm2} km²` : "—"],
                                ["Avg footprint", report.inventory.avgPropM2 ? `${report.inventory.avgPropM2} m²` : "—"],
                                ["Water bodies", report.inventory.waterbodies.toLocaleString()],
                                ["Open plots", report.inventory.openareas.toLocaleString()],
                                ["Activity in period", report.activity.length],
                            ].map(([k, v]) => (
                                <div key={k} className="bg-[var(--bg-tertiary)] rounded-lg p-2">
                                    <p className="text-[10px] text-[var(--text-muted)]">{k}</p>
                                    <p className="text-sm font-bold text-[var(--text-primary)]">{v}</p>
                                </div>
                            ))}
                        </div>

                        {/* Real change-detection aggregate */}
                        <div className="bg-[var(--bg-tertiary)] rounded-lg p-3">
                            <p className="text-xs font-medium text-[var(--text-secondary)] mb-1.5">Change Detection (this period)</p>
                            {report.cd.runs > 0 ? (
                                <div className="space-y-1">
                                    <p className="text-[11px] text-[var(--text-muted)]">{report.cd.runs} run{report.cd.runs > 1 ? "s" : ""} · averaged share of analysed area:</p>
                                    {Object.entries(report.cd.avg).map(([k, v]) => (
                                        <div key={k} className="flex justify-between text-[11px]">
                                            <span className="text-[var(--text-muted)]">{k}</span>
                                            <span className="font-semibold text-[var(--text-primary)]">{v}%</span>
                                        </div>
                                    ))}
                                </div>
                            ) : (
                                <p className="text-[11px] text-[var(--text-muted)]">No change-detection runs recorded in this period. Run one in Upload &amp; Analysis and it will appear here and in the PDF.</p>
                            )}
                        </div>

                        {/* Real activity mix */}
                        {typeChart.length > 0 && (
                            <div>
                                <p className="text-xs font-medium text-[var(--text-muted)] mb-2">Platform activity in period (by type)</p>
                                <ResponsiveContainer width="100%" height={140}>
                                    <BarChart data={typeChart} layout="vertical" margin={{ left: 8 }}>
                                        <XAxis type="number" allowDecimals={false} tick={{ fontSize: 9 }} />
                                        <YAxis type="category" dataKey="type" width={110} tick={{ fontSize: 9 }} />
                                        <Tooltip />
                                        <Bar dataKey="n" radius={[0, 3, 3, 0]}>
                                            {typeChart.map((_, i) => <Cell key={i} fill={BAR_COLORS[i % BAR_COLORS.length]} />)}
                                        </Bar>
                                    </BarChart>
                                </ResponsiveContainer>
                            </div>
                        )}

                        {/* Real monthly activity */}
                        {report.monthly.length > 0 && (
                            <div>
                                <p className="text-xs font-medium text-[var(--text-muted)] mb-2">Monthly activity</p>
                                <ResponsiveContainer width="100%" height={120}>
                                    <BarChart data={report.monthly}>
                                        <XAxis dataKey="month" tick={{ fontSize: 9 }} />
                                        <YAxis allowDecimals={false} tick={{ fontSize: 9 }} />
                                        <Tooltip />
                                        <Bar dataKey="runs" fill="#14b8a6" radius={[3, 3, 0, 0]} />
                                    </BarChart>
                                </ResponsiveContainer>
                            </div>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
}
