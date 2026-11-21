import { useCallback, useEffect, useRef, useState } from "react";
import { Eye, EyeOff, ImagePlus, Play, RotateCcw, X } from "lucide-react";
import { logActivity } from "../lib/activityLog";
import { gpuOnlyFetch, useGpuStatus } from "../lib/modelApi";
import { Button, Card, Pill, SectionHeader, Spinner } from "./ui";

/*
 * Water change between two dates, read from the land-cover model's classes.
 *
 * No separate network and no separate weights: the 9-class land-cover model
 * already predicts `water`, so change is that class differenced across two
 * epochs on one shared grid. It also means this improves whenever land cover
 * does.
 *
 * What makes it worth having over a gain/loss mask is that it classifies
 * WHAT REPLACED the water. Water that became building, road or developed
 * ground is encroachment; water that became bare soil or vegetation is
 * recession - a level drop, seasonal or otherwise. Same square metres,
 * completely different finding, and the model already knows which.
 *
 * Both images must cover the same ground. They are resampled onto one grid
 * at the coarser of the two resolutions before anything is compared, because
 * differencing maps that are not aligned measures the misalignment.
 */
const ACCEPT = /\.(jpe?g|png|webp|bmp|tiff?|jp2)$/i;
const MAX_MB = 200;

async function compare(past, present, signal) {
    const fd = new FormData();
    fd.append("past", past, past.name);
    fd.append("present", present, present.name);
    fd.append("want_overlay", "true");
    const r = await gpuOnlyFetch("/waterchange/predict", { method: "POST", body: fd, signal });
    if (!r.ok) {
        let detail = `HTTP ${r.status}`;
        try { detail = (await r.json()).detail || detail; } catch { /* not JSON */ }
        if (r.status === 404) detail = "This GPU server has no water-change model installed.";
        if (r.status === 400) detail += " — a plain JPG/PNG carries no ground resolution; upload GeoTIFFs.";
        throw new Error(detail);
    }
    return r.json();
}

function Drop({ id, file, onPick, label, hint }) {
    return (
        <div
            onDrop={(e) => { e.preventDefault(); onPick(e.dataTransfer.files?.[0]); }}
            onDragOver={(e) => e.preventDefault()}
            onClick={() => document.getElementById(id).click()}
            className={`cursor-pointer rounded-lg border-2 border-dashed p-5 text-center transition-colors ${
                file ? "border-[var(--ok)] bg-[var(--ok-dim)]"
                     : "border-[var(--border-default)] hover:border-[var(--accent)] hover:bg-[var(--accent-dim)]"}`}
        >
            <ImagePlus className="mx-auto mb-2 h-6 w-6 text-[var(--text-muted)]" />
            <p className="text-xs font-medium text-[var(--text-primary)]">{label}</p>
            <p className="mt-1 text-[10px] text-[var(--text-muted)]">
                {file ? file.name : hint}
            </p>
            <input id={id} type="file" className="hidden" accept="image/*,.tif,.tiff,.jp2"
                   onChange={(e) => onPick(e.target.files?.[0])} />
        </div>
    );
}

export default function WaterChangePanel() {
    const gpu = useGpuStatus();
    const [past, setPast] = useState(null);
    const [present, setPresent] = useState(null);
    const [res, setRes] = useState(null);
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState("");
    const [showOverlay, setShowOverlay] = useState(true);
    const [legend, setLegend] = useState([]);
    const abortRef = useRef(null);

    useEffect(() => () => abortRef.current?.abort(), []);

    // The key comes from the server, so it cannot disagree with the pixels.
    useEffect(() => {
        let gone = false;
        gpuOnlyFetch("/waterchange/legend")
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => { if (!gone && d?.classes) setLegend(d.classes); })
            .catch(() => { /* panel still works */ });
        return () => { gone = true; };
    }, [gpu.ok]);

    const pick = useCallback((setter) => (f) => {
        if (!f) return;
        if (!ACCEPT.test(f.name)) { setErr(`${f.name} — unsupported format`); return; }
        if (f.size > MAX_MB * 1024 * 1024) {
            setErr(`${f.name} — ${(f.size / 1048576).toFixed(0)} MB, over the ${MAX_MB} MB limit`);
            return;
        }
        setErr(""); setter(f); setRes(null);
    }, []);

    const run = async () => {
        if (!past || !present) return;
        setBusy(true); setErr(""); setRes(null);
        abortRef.current = new AbortController();
        try {
            const out = await compare(past, present, abortRef.current.signal);
            setRes(out);
            logActivity?.({ kind: "model", label: "Water change — 2 epochs compared" });
        } catch (e) {
            if (e.name !== "AbortError") {
                setErr(e.message === "GPU_OFFLINE"
                    ? "The GPU server is offline. Start it with start_everything.bat."
                    : e.message);
            }
        } finally { setBusy(false); }
    };

    const st = res?.stats;
    const rows = (res?.classes || []).filter(
        (r) => r.name !== "no water" && (r.area_m2 || 0) > 0);

    return (
        <Card>
            <SectionHeader
                title="Water change"
                sub="Gained, lost, encroached or receded between two dates — DINOv3 land cover, differenced"
                right={
                    <div className="flex items-center gap-1.5">
                        <Pill tone={gpu.ok ? "ok" : "warn"}>{gpu.ok ? "GPU online" : "GPU offline"}</Pill>
                    </div>
                }
            />

            {legend.length > 0 && (
                <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1.5">
                    {legend.filter((c) => c.id > 0).map((c) => (
                        <span key={c.name} className="flex items-center gap-1.5" title={c.note}>
                            <span className="h-3 w-3 rounded-sm" style={{ background: c.color }} aria-hidden="true" />
                            <span className="text-[11px] text-[var(--text-secondary)]">{c.label}</span>
                        </span>
                    ))}
                </div>
            )}

            <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
                <Drop id="wc-past" file={past} onPick={pick(setPast)}
                      label="Earlier date" hint="e.g. the 2017 satellite — GeoTIFF preferred" />
                <Drop id="wc-present" file={present} onPick={pick(setPresent)}
                      label="Later date" hint="e.g. the 2026 satellite — same ground" />
            </div>

            {err && (
                <p className="mt-3 rounded-md border border-[var(--danger)] bg-[var(--danger-dim)] px-3 py-2 text-[11px] text-[var(--danger)]">
                    {err}
                </p>
            )}

            <div className="mt-3 flex flex-wrap items-center gap-2">
                <Button onClick={run} disabled={busy || !gpu.ok || !past || !present}>
                    {busy ? <Spinner className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
                    {busy ? "Comparing…" : "Compare the two dates"}
                </Button>
                {busy && (
                    <Button variant="ghost" onClick={() => abortRef.current?.abort()}>
                        <RotateCcw className="h-3.5 w-3.5" /> Cancel
                    </Button>
                )}
                {res?.overlay_png && (
                    <Button variant="ghost" onClick={() => setShowOverlay((v) => !v)}>
                        {showOverlay ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                        {showOverlay ? "Hide change" : "Show change"}
                    </Button>
                )}
                {(past || present) && !busy && (
                    <Button variant="ghost" onClick={() => { setPast(null); setPresent(null); setRes(null); setErr(""); }}>
                        <X className="h-3.5 w-3.5" /> Clear
                    </Button>
                )}
            </div>

            {st && (
                <div className="mt-3 rounded-lg border border-[var(--border-default)] bg-[var(--surface-1)] p-3">
                    <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-[11px]">
                        <span className="text-[var(--text-primary)]">
                            water {(st.water_area_a_m2 / 1e4).toFixed(2)} ha →{" "}
                            {(st.water_area_b_m2 / 1e4).toFixed(2)} ha
                        </span>
                        <span className="mono" style={{
                            color: st.net_change_m2 < 0 ? "var(--danger)" : "var(--ok)" }}>
                            {st.net_change_m2 > 0 ? "+" : ""}{(st.net_change_m2 / 1e4).toFixed(2)} ha
                            {st.net_change_pct != null && ` (${st.net_change_pct > 0 ? "+" : ""}${st.net_change_pct}%)`}
                        </span>
                        <span className="mono text-[10px] text-[var(--text-muted)]">
                            {st.work_gsd_m?.toFixed?.(2)} m/px · {st.seconds}s
                        </span>
                    </div>

                    <div className="mt-2 space-y-1.5">
                        {rows.map((r) => (
                            <div key={r.name} className="flex items-center gap-2" title={r.note}>
                                <span className="h-3 w-3 shrink-0 rounded-sm" style={{ background: r.color }} />
                                <span className="w-28 shrink-0 text-[11px] text-[var(--text-primary)]">{r.label}</span>
                                <span className="mono ml-auto text-[11px] text-[var(--text-secondary)]">
                                    {r.area_m2?.toLocaleString()} m²
                                    <span className="ml-2 text-[var(--text-muted)]">{r.area_ha} ha</span>
                                </span>
                            </div>
                        ))}
                    </div>

                    {/* A thin rim of change round every shoreline is registration,
                        not a finding. Saying so beats having it believed. */}
                    <p className="mt-2 text-[10px] text-[var(--text-muted)]">
                        Water bodies under {st.min_water_m2} m² and change patches under{" "}
                        {st.min_change_m2} m² are dropped as noise — a one-pixel rim along a
                        shoreline is alignment between the two dates, not change.
                    </p>
                </div>
            )}

            {res?.overlay_png && showOverlay && (
                <div className="mt-3 overflow-hidden rounded-md border border-[var(--border-subtle)]">
                    <img src={`data:image/png;base64,${res.overlay_png}`}
                         alt="Water change" className="block w-full" />
                </div>
            )}
        </Card>
    );
}
