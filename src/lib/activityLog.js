/**
 * Activity log — records every REAL action the platform performs
 * (AI segmentation runs, change detections, AOI analyses, boundary uploads,
 * report generations) so Data Logs and DSS reports reflect actual usage.
 *
 * Storage: Supabase table `activity_logs` when configured (cloud, shared),
 * ALWAYS mirrored to localStorage (works offline / before keys are added).
 */
import { supabase, supabaseEnabled } from "./supabase";

const LS_KEY = "sp_activity_logs";
const LS_MAX = 300;

// Stable anonymous session id so rows from one browser hang together.
function sessionId() {
    let id = localStorage.getItem("sp_session_id");
    if (!id) {
        id = (crypto.randomUUID ? crypto.randomUUID() : `s-${Date.now()}-${Math.random().toString(36).slice(2)}`);
        localStorage.setItem("sp_session_id", id);
    }
    return id;
}

function readLocal() {
    try { return JSON.parse(localStorage.getItem(LS_KEY) || "[]"); } catch (_) { return []; }
}
function writeLocal(rows) {
    try { localStorage.setItem(LS_KEY, JSON.stringify(rows.slice(0, LS_MAX))); } catch (_) { /* quota */ }
}

/**
 * Record one activity. Fire-and-forget (never blocks the UI action itself).
 * @param {object} e
 *   type     "AI Segmentation" | "Change Detection" | "AOI Analysis" |
 *            "Boundary Upload" | "Report Generated" | ...
 *   district district name or "Custom Upload"
 *   area     human label of what was analysed (file name, "2.1 km² AOI", …)
 *   status   "Completed" | "Failed"
 *   metrics  small object of REAL result numbers (counts / percentages)
 */
export function logActivity({ type, district = null, area = null, status = "Completed", metrics = null, meta = null }) {
    const row = {
        id: (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`),
        created_at: new Date().toISOString(),
        session_id: sessionId(),
        type, district, area, status,
        metrics: metrics || null,
        meta: meta || null,
    };
    // local mirror first (always works)
    writeLocal([row, ...readLocal()]);
    // cloud (optional)
    if (supabaseEnabled) {
        supabase.from("activity_logs").insert({
            session_id: row.session_id, type, district, area, status,
            metrics: row.metrics, meta: row.meta,
        }).then(({ error }) => { if (error) console.warn("activity log sync failed:", error.message); });
    }
    window.dispatchEvent(new CustomEvent("sp-activity", { detail: row }));
    return row;
}

/** Fetch activities, newest first. Cloud when available, else local mirror. */
export async function fetchActivities({ limit = 200 } = {}) {
    if (supabaseEnabled) {
        const { data, error } = await supabase
            .from("activity_logs")
            .select("*")
            .order("created_at", { ascending: false })
            .limit(limit);
        if (!error && Array.isArray(data)) return { rows: data, source: "supabase" };
        console.warn("activity fetch fell back to local:", error?.message);
    }
    return { rows: readLocal().slice(0, limit), source: "local" };
}

/** Activities within a date range (used by DSS reports). */
export async function fetchActivitiesInRange(fromISO, toISO, { district = null } = {}) {
    const { rows, source } = await fetchActivities({ limit: 500 });
    const t0 = new Date(fromISO).getTime();
    const t1 = new Date(toISO).getTime() + 86400000; // inclusive end day
    const out = rows.filter(r => {
        const t = new Date(r.created_at).getTime();
        if (Number.isNaN(t) || t < t0 || t > t1) return false;
        if (district && r.district && r.district !== district && r.district !== "Custom Upload") return false;
        return true;
    });
    return { rows: out, source };
}

export { supabaseEnabled };
