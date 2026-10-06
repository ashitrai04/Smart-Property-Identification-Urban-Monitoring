import { useState, useMemo, useEffect, useCallback } from "react";
import { fetchActivities, supabaseEnabled } from "../lib/activityLog";

const TYPE_ICONS = {
    "AI Segmentation": "🧠",
    "Change Detection": "🔍",
    "AOI Analysis": "📐",
    "Boundary Upload": "🗺️",
    "Report Generated": "📄",
    "Pothole Detection": "🛣️",
};
const STATUS_COLORS = {
    Completed: "bg-[var(--ok-dim)] text-[var(--ok)] border border-[var(--ok)]/30",
    Failed: "bg-[var(--critical-dim)] text-[var(--critical)] border border-[var(--critical)]/30",
};

function metricsSummary(m) {
    if (!m || typeof m !== "object") return "—";
    return Object.entries(m).slice(0, 3).map(([k, v]) => `${k}: ${v}`).join(" · ");
}

export default function DataLogs() {
    const [rows, setRows] = useState([]);
    const [source, setSource] = useState("local");
    const [loading, setLoading] = useState(true);
    const [distFilter, setDistFilter] = useState("All");
    const [typeFilter, setTypeFilter] = useState("All");
    const [page, setPage] = useState(0);
    const perPage = 10;

    const load = useCallback(async () => {
        setLoading(true);
        const { rows: r, source: s } = await fetchActivities({ limit: 300 });
        setRows(r); setSource(s); setLoading(false);
    }, []);

    useEffect(() => {
        load();
        // live-update when any page logs a new activity
        const onNew = () => load();
        window.addEventListener("sp-activity", onNew);
        return () => window.removeEventListener("sp-activity", onNew);
    }, [load]);

    const districts = useMemo(() => ["All", ...Array.from(new Set(rows.map(r => r.district).filter(Boolean)))], [rows]);
    const types = useMemo(() => ["All", ...Array.from(new Set(rows.map(r => r.type).filter(Boolean)))], [rows]);

    const filtered = useMemo(() =>
        rows.filter(r => (distFilter === "All" || r.district === distFilter) && (typeFilter === "All" || r.type === typeFilter)),
        [rows, distFilter, typeFilter]);

    const paged = filtered.slice(page * perPage, (page + 1) * perPage);
    const totalPages = Math.max(1, Math.ceil(filtered.length / perPage));

    const counts = useMemo(() => ({
        seg: filtered.filter(r => r.type === "AI Segmentation").length,
        cd: filtered.filter(r => r.type === "Change Detection").length,
        aoi: filtered.filter(r => r.type === "AOI Analysis" || r.type === "Boundary Upload").length,
        failed: filtered.filter(r => r.status === "Failed").length,
    }), [filtered]);

    const exportCSV = () => {
        const headers = ["Timestamp", "Type", "District", "Target", "Metrics", "Status"];
        const esc = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
        const lines = filtered.map(r => [r.created_at, r.type, r.district || "", r.area || "", metricsSummary(r.metrics), r.status].map(esc).join(","));
        const blob = new Blob([[headers.join(","), ...lines].join("\n")], { type: "text/csv" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = `activity_logs_${new Date().toISOString().slice(0, 10)}.csv`;
        a.click();
    };

    return (
        <div className="space-y-4" data-tour="datalogs">
            <div className="page-head">
                <div>
                    <div className="page-eyebrow">Audit trail</div>
                    <h1 className="page-title">Data logs</h1>
                    <p className="page-sub">
                        Every analysis run on this platform, recorded automatically — segmentations, change detections, AOI analyses and reports.
                    </p>
                </div>
                <div className="flex items-center gap-2">
                    <span className={`text-[10px] px-2 py-1 rounded-full font-medium ${supabaseEnabled ? "bg-[var(--ok-dim)] text-[var(--ok)]" : "bg-[var(--alert-dim)] text-[var(--alert)]"}`}
                        title={supabaseEnabled ? "Logs are synced to Supabase" : "Add VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY to sync logs to the cloud (see docs/SUPABASE_SETUP.md)"}>
                        {supabaseEnabled ? "● Cloud sync: on" : "● Cloud sync: off (local only)"}
                    </span>
                    <button onClick={load} className="px-2.5 py-1.5 text-[12px] font-medium border border-[var(--border-default)] rounded-[6px] text-[var(--text-secondary)] hover:bg-[var(--surface-2)] transition-colors">↻ Refresh</button>
                    <button onClick={exportCSV} disabled={!filtered.length}
                        className="inline-flex items-center gap-1.5 px-2.5 py-1.5 bg-[var(--accent)] text-[#04201C] text-[12px] font-semibold rounded-[6px] hover:brightness-110 transition-[filter] disabled:opacity-40">
                        Export CSV
                    </button>
                </div>
            </div>

            {/* Filters */}
            <div className="flex flex-wrap gap-3">
                <select value={distFilter} onChange={e => { setDistFilter(e.target.value); setPage(0); }}
                    className="dark-select" style={{ width: "auto", minWidth: 150 }}>
                    {districts.map(d => <option key={d} value={d}>{d === "All" ? "All Districts" : d}</option>)}
                </select>
                <select value={typeFilter} onChange={e => { setTypeFilter(e.target.value); setPage(0); }}
                    className="dark-select" style={{ width: "auto", minWidth: 150 }}>
                    {types.map(t => <option key={t} value={t}>{t === "All" ? "All Types" : t}</option>)}
                </select>
                <span className="text-xs text-[var(--text-muted)] self-center">{filtered.length} entries · {source === "supabase" ? "cloud" : "this browser"}</span>
            </div>

            {/* Summary — real counts */}
            <div className="stagger grid grid-cols-2 sm:grid-cols-4 gap-3">
                {[
                    ["AI segmentations", counts.seg, "#2DD4BF"],
                    ["Change detections", counts.cd, "#38BDF8"],
                    ["AOI / boundary analyses", counts.aoi, "#A78BFA"],
                    ["Failed runs", counts.failed, counts.failed ? "#EF4444" : "#64748B"],
                ].map(([label, n, colour]) => (
                    <div key={label} className="stat-card" style={{ "--stat-colour": colour }}>
                        <div style={{ flex: 1, minWidth: 0 }}>
                            <p className="stat-card-label">{label}</p>
                            <p className="stat-card-value" style={{ color: n && colour === "#EF4444" ? colour : undefined }}>{n}</p>
                        </div>
                    </div>
                ))}
            </div>

            {/* Table */}
            <div className="bg-[var(--surface)] rounded-[8px] border border-[var(--border-default)] shadow-sm overflow-hidden">
                {loading ? (
                    <div className="p-10 text-center text-sm text-[var(--text-muted)]">
                        <span className="inline-block w-5 h-5 border-2 border-[var(--accent)]/30 border-t-[var(--accent)] rounded-full animate-spin align-middle mr-2" />
                        Loading activity…
                    </div>
                ) : filtered.length === 0 ? (
                    <div className="p-10 text-center space-y-2">
                        <p className="text-3xl">🗂️</p>
                        <p className="text-sm font-medium text-[var(--text-primary)]">No activity recorded yet</p>
                        <p className="text-xs text-[var(--text-muted)] max-w-md mx-auto">
                            This log fills up as you use the platform: run an <strong>AI Segmentation</strong> or <strong>Change Detection</strong> in Upload &amp; Analysis,
                            draw an <strong>Area of Interest</strong> on the Mapping page, or generate a <strong>DSS report</strong> — each run appears here with its real results.
                        </p>
                    </div>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="data-table">
                            <thead>
                                <tr>
                                    <th className="py-2.5 px-4 text-left font-medium">When</th>
                                    <th className="py-2.5 px-4 text-left font-medium">Type</th>
                                    <th className="py-2.5 px-4 text-left font-medium">District</th>
                                    <th className="py-2.5 px-4 text-left font-medium">Target</th>
                                    <th className="py-2.5 px-4 text-left font-medium">Results</th>
                                    <th className="py-2.5 px-4 text-center font-medium">Status</th>
                                </tr>
                            </thead>
                            <tbody>
                                {paged.map(r => (
                                    <tr key={r.id}>
                                        <td className="mono text-[11px] text-[var(--text-muted)] whitespace-nowrap">
                                            {new Date(r.created_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}
                                        </td>
                                        <td className="py-2.5 px-4 text-xs font-medium text-[var(--text-primary)] whitespace-nowrap">
                                            {TYPE_ICONS[r.type] || "•"} {r.type}
                                        </td>
                                        <td className="py-2.5 px-4 text-xs">{r.district || "—"}</td>
                                        <td className="py-2.5 px-4 text-xs text-[var(--text-muted)] max-w-[200px] truncate" title={r.area || ""}>{r.area || "—"}</td>
                                        <td className="py-2.5 px-4 text-xs text-[var(--text-secondary)] max-w-[260px] truncate" title={metricsSummary(r.metrics)}>{metricsSummary(r.metrics)}</td>
                                        <td className="py-2.5 px-4 text-center">
                                            <span className={`inline-flex items-center gap-1 px-1.5 py-[2px] rounded-[4px] text-[10px] font-medium ${STATUS_COLORS[r.status] || "bg-[var(--bg-secondary)] text-[var(--text-muted)]"}`}>
                                                ● {r.status}
                                            </span>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
                {totalPages > 1 && (
                    <div className="flex items-center justify-between px-4 py-3 border-t border-[var(--border-default)]">
                        <button onClick={() => setPage(p => Math.max(0, p - 1))} disabled={page === 0} className="text-xs text-[var(--accent)] hover:brightness-125 disabled:opacity-40">← Previous</button>
                        <span className="text-xs text-[var(--text-muted)]">Page {page + 1} of {totalPages}</span>
                        <button onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))} disabled={page >= totalPages - 1} className="text-xs text-[var(--accent)] hover:brightness-125 disabled:opacity-40">Next →</button>
                    </div>
                )}
            </div>
        </div>
    );
}
