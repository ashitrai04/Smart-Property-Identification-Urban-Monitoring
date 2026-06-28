import React, { useState, useMemo } from "react";

const LOGS_DATA = [
    { id: 1, date: "2025-02-28", district: "Visakhapatnam", type: "Change Detection", area: "Ward 42 — Maddilapalem", changes: 47, status: "Completed", method: "Sentinel-2" },
    { id: 2, date: "2025-02-25", district: "Vijayawada", type: "Property Survey", area: "Zone B — Benz Circle", changes: 128, status: "Completed", method: "Drone Imagery" },
    { id: 3, date: "2025-02-20", district: "Guntur", type: "Mask Update", area: "Nagarampalem Ward", changes: 33, status: "Completed", method: "Satellite Mask" },
    { id: 4, date: "2025-02-18", district: "Visakhapatnam", type: "New Construction", area: "Gajuwaka Industrial", changes: 212, status: "Review", method: "AI Segmentation" },
    { id: 5, date: "2025-02-15", district: "Nellore", type: "Water Body Change", area: "Mypadu Beach Rd", changes: 8, status: "Completed", method: "NDWI Analysis" },
    { id: 6, date: "2025-02-12", district: "Anantapur", type: "Land Use Change", area: "Penukonda Highway", changes: 56, status: "Completed", method: "Sentinel-2" },
    { id: 7, date: "2025-02-10", district: "Visakhapatnam", type: "Road Extension", area: "Beach Road Flyover", changes: 19, status: "Verified", method: "Drone Imagery" },
    { id: 8, date: "2025-02-08", district: "Guntur", type: "Open Plot Survey", area: "AT Agraharam", changes: 91, status: "Completed", method: "AI Segmentation" },
    { id: 9, date: "2025-01-30", district: "Vijayawada", type: "Monthly Analysis", area: "Full District", changes: 347, status: "Completed", method: "Multi-source" },
    { id: 10, date: "2025-01-28", district: "Visakhapatnam", type: "Monthly Analysis", area: "Full District", changes: 521, status: "Completed", method: "Multi-source" },
    { id: 11, date: "2025-01-25", district: "Nellore", type: "Encroachment Detect", area: "Canal Zone", changes: 14, status: "Review", method: "AI Segmentation" },
    { id: 12, date: "2025-01-22", district: "Anantapur", type: "Monthly Analysis", area: "Full District", changes: 189, status: "Completed", method: "Multi-source" },
];

const DISTRICTS_FILTER = ["All", "Visakhapatnam", "Vijayawada", "Guntur", "Anantapur", "Nellore"];
const TYPES_FILTER = ["All", "Change Detection", "Property Survey", "Mask Update", "New Construction", "Monthly Analysis", "Water Body Change", "Land Use Change"];
const STATUS_COLORS = { "Completed": "bg-green-500/20 text-green-400", "Review": "bg-yellow-500/20 text-yellow-400", "Verified": "bg-blue-500/20 text-blue-400" };

export default function DataLogs() {
    const [distFilter, setDistFilter] = useState("All");
    const [typeFilter, setTypeFilter] = useState("All");
    const [page, setPage] = useState(0);
    const perPage = 8;

    const filtered = useMemo(() =>
        LOGS_DATA.filter(l => (distFilter === "All" || l.district === distFilter) && (typeFilter === "All" || l.type === typeFilter)),
        [distFilter, typeFilter]);

    const paged = filtered.slice(page * perPage, (page + 1) * perPage);
    const totalPages = Math.ceil(filtered.length / perPage);

    const exportCSV = () => {
        const headers = ["Date", "District", "Type", "Area", "Changes", "Status", "Method"];
        const rows = filtered.map(l => [l.date, l.district, l.type, l.area, l.changes, l.status, l.method]);
        const csv = [headers, ...rows].map(r => r.join(",")).join("\n");
        const blob = new Blob([csv], { type: "text/csv" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = `data_logs_${new Date().toISOString().slice(0, 10)}.csv`;
        a.click();
    };

    return (
        <div className="space-y-6">
            <div className="bg-[var(--bg-card)] backdrop-blur-md rounded-lg border border-[var(--border-default)] p-4 sm:p-5 shadow-sm flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
                <div>
                    <h2 className="text-lg sm:text-xl font-bold text-[var(--text-primary)]">Data Logs</h2>
                    <p className="text-sm text-[var(--text-muted)] mt-1">Change detection and analysis history</p>
                </div>
                <button onClick={exportCSV} className="inline-flex items-center gap-1.5 px-4 py-2 bg-[var(--accent)] text-white text-sm font-medium rounded-lg hover:bg-[#094d87] transition-colors">
                    📥 Export CSV
                </button>
            </div>

            {/* Filters */}
            <div className="flex flex-wrap gap-3">
                <select value={distFilter} onChange={e => { setDistFilter(e.target.value); setPage(0); }}
                    className="bg-[var(--bg-card)] text-[var(--text-primary)] border border-[var(--border-default)] rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-[var(--accent)]">
                    {DISTRICTS_FILTER.map(d => <option key={d} value={d}>{d === "All" ? "All Districts" : d}</option>)}
                </select>
                <select value={typeFilter} onChange={e => { setTypeFilter(e.target.value); setPage(0); }}
                    className="bg-[var(--bg-card)] text-[var(--text-primary)] border border-[var(--border-default)] rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-[var(--accent)]">
                    {TYPES_FILTER.map(t => <option key={t} value={t}>{t === "All" ? "All Types" : t}</option>)}
                </select>
                <span className="text-xs text-[var(--text-muted)] self-center">{filtered.length} results</span>
            </div>

            {/* Summary */}
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                <div className="bg-[var(--bg-card)] backdrop-blur-md rounded-lg border border-[var(--border-default)] p-4 shadow-sm">
                    <p className="text-xs text-[var(--text-muted)]">Total Entries</p>
                    <p className="text-xl font-bold text-[var(--text-primary)]">{filtered.length}</p>
                </div>
                <div className="bg-[var(--bg-card)] backdrop-blur-md rounded-lg border border-[var(--border-default)] p-4 shadow-sm">
                    <p className="text-xs text-[var(--text-muted)]">Total Changes</p>
                    <p className="text-xl font-bold text-[var(--accent)]">{filtered.reduce((s, l) => s + l.changes, 0).toLocaleString()}</p>
                </div>
                <div className="bg-[var(--bg-card)] backdrop-blur-md rounded-lg border border-[var(--border-default)] p-4 shadow-sm">
                    <p className="text-xs text-[var(--text-muted)]">Completed</p>
                    <p className="text-xl font-bold text-green-600">{filtered.filter(l => l.status === "Completed").length}</p>
                </div>
                <div className="bg-[var(--bg-card)] backdrop-blur-md rounded-lg border border-[var(--border-default)] p-4 shadow-sm">
                    <p className="text-xs text-[var(--text-muted)]">Under Review</p>
                    <p className="text-xl font-bold text-yellow-600">{filtered.filter(l => l.status === "Review").length}</p>
                </div>
            </div>

            {/* Table */}
            <div className="bg-[var(--bg-card)] backdrop-blur-md rounded-lg border border-[var(--border-default)] shadow-sm overflow-hidden">
                <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                        <thead className="bg-[var(--bg-tertiary)] text-[var(--text-muted)]">
                            <tr>
                                <th className="py-2.5 px-4 text-left font-medium">Date</th>
                                <th className="py-2.5 px-4 text-left font-medium">District</th>
                                <th className="py-2.5 px-4 text-left font-medium">Type</th>
                                <th className="py-2.5 px-4 text-left font-medium">Area</th>
                                <th className="py-2.5 px-4 text-right font-medium">Changes</th>
                                <th className="py-2.5 px-4 text-center font-medium">Method</th>
                                <th className="py-2.5 px-4 text-center font-medium">Status</th>
                                <th className="py-2.5 px-4 text-center font-medium">Export</th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-[var(--border-default)]">
                            {paged.map(l => (
                                <tr key={l.id} className="hover:bg-[var(--bg-tertiary)] transition-colors">
                                    <td className="py-2.5 px-4 text-xs text-[var(--text-muted)]">{l.date}</td>
                                    <td className="py-2.5 px-4 font-medium text-[var(--text-primary)] text-xs">{l.district}</td>
                                    <td className="py-2.5 px-4 text-xs">{l.type}</td>
                                    <td className="py-2.5 px-4 text-xs text-[var(--text-muted)] max-w-[180px] truncate">{l.area}</td>
                                    <td className="py-2.5 px-4 text-right text-xs font-semibold">{l.changes}</td>
                                    <td className="py-2.5 px-4 text-center text-[10px] text-[var(--text-muted)]">{l.method}</td>
                                    <td className="py-2.5 px-4 text-center">
                                        <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium ${STATUS_COLORS[l.status] || "bg-[var(--bg-secondary)] text-[var(--text-muted)]"}`}>
                                            ● {l.status}
                                        </span>
                                    </td>
                                    <td className="py-2.5 px-4 text-center">
                                        <button className="text-[var(--accent)] hover:text-[#094d87] text-xs font-medium">🖼️ Image</button>
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
                {totalPages > 1 && (
                    <div className="flex items-center justify-between px-4 py-3 border-t border-[var(--border-default)]">
                        <button onClick={() => setPage(p => Math.max(0, p - 1))} disabled={page === 0} className="text-xs text-[var(--accent)] hover:text-[#094d87] disabled:text-gray-300">← Previous</button>
                        <span className="text-xs text-[var(--text-muted)]">Page {page + 1} of {totalPages}</span>
                        <button onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))} disabled={page >= totalPages - 1} className="text-xs text-[var(--accent)] hover:text-[#094d87] disabled:text-gray-300">Next →</button>
                    </div>
                )}
            </div>
        </div>
    );
}
