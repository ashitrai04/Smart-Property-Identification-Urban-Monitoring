import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { motion } from "framer-motion";
import { BarChart2, ChevronDown, Compass, Database, Globe, Layers, LayoutDashboard, Map as MapIcon, Moon, Mountain, Satellite, Sun, Upload } from "lucide-react";
import { API_BASE } from "../utils/mapLayers";
import { Button, Pill } from "./ui";
import { useBreakpoint } from "../lib/useBreakpoint";
import { probeGpu, useGpuStatus } from "../lib/modelApi";
import { BASE_MAPS, useBaseMap } from "../lib/mapPrefs";

const BASEMAP_ICON = {
    "dark-v11": Moon,
    "satellite-streets-v12": Satellite,
    "streets-v12": MapIcon,
    "light-v11": Sun,
    "outdoors-v12": Mountain,
};

/* Map view switcher — same control as Sentinel's, shown where there is a map to switch. */
function BaseMapMenu({ tight }) {
    const [baseMap, setBaseMap] = useBaseMap();
    const [open, setOpen] = useState(false);
    const current = BASE_MAPS.find((b) => b.id === baseMap) || BASE_MAPS[0];
    return (
        <div className="relative shrink-0">
            <Button data-tour="basemap" onClick={() => setOpen((v) => !v)} aria-expanded={open} title="Base map">
                <Layers size={13} />
                {!tight && current.label}
                <ChevronDown size={11} style={{ opacity: 0.6, transform: open ? "rotate(180deg)" : "none", transition: "transform 150ms" }} />
            </Button>
            {open && (
                <>
                    <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
                    <div
                        className="anim-fade-up absolute left-0 top-full z-50 mt-1 w-[176px] overflow-hidden rounded-[8px] py-1"
                        style={{ background: "var(--surface)", border: "1px solid var(--line)", boxShadow: "var(--sh-lg)" }}
                    >
                        {BASE_MAPS.map((b) => {
                            const Icon = BASEMAP_ICON[b.id] || MapIcon;
                            const on = b.id === baseMap;
                            return (
                                <button
                                    key={b.id}
                                    data-tour-bm={b.id}
                                    onClick={() => { setBaseMap(b.id); setOpen(false); }}
                                    className="flex w-full items-center gap-2 px-3 py-[7px] text-left text-[12px] hover:bg-[var(--surface-2)]"
                                    style={{ color: on ? "var(--signal)" : "var(--text)" }}
                                >
                                    <Icon size={13} style={{ color: on ? "var(--signal)" : "var(--text-mute)" }} />
                                    <span className="flex-1">{b.label}</span>
                                    {on && <span className="text-[10px]">●</span>}
                                </button>
                            );
                        })}
                    </div>
                </>
            )}
        </div>
    );
}

const NAV_ITEMS = [
    { path: "/", label: "Overview", icon: LayoutDashboard },
    { path: "/mapping", label: "Mapping", icon: Globe },
    { path: "/upload", label: "Analysis", icon: Upload },
    { path: "/datalogs", label: "Data logs", icon: Database },
    { path: "/dss", label: "Reports", icon: BarChart2 },
];

/* The inference backend sleeps when idle (Hugging Face Space), so the bar says
   whether the model is reachable rather than claiming "online" unconditionally. */
function useBackendStatus() {
    const [status, setStatus] = useState("checking");
    useEffect(() => {
        let live = true;
        const check = async () => {
            const ctl = new AbortController();
            const t = setTimeout(() => ctl.abort(), 8000);
            try {
                const r = await fetch(`${API_BASE}/api/health`, { cache: "no-store", signal: ctl.signal });
                if (live) setStatus(r.ok ? "online" : "degraded");
            } catch {
                if (live) setStatus("offline");
            } finally {
                clearTimeout(t);
            }
        };
        check();
        const iv = setInterval(check, 45_000);
        return () => { live = false; clearInterval(iv); };
    }, []);
    return status;
}

const STATUS_META = {
    checking: { label: "Connecting", colour: "var(--text-mute)", dot: "" },
    online: { label: "Data online", colour: "var(--ok)", dot: "" },
    degraded: { label: "Data degraded", colour: "var(--alert)", dot: "warn" },
    offline: { label: "Data offline", colour: "var(--critical)", dot: "down" },
};

export default function CommandBar() {
    const location = useLocation();
    const bp = useBreakpoint();
    const tight = bp !== "wide";
    const tiny = bp === "narrow";
    const status = useBackendStatus();
    const gpu = useGpuStatus();
    useEffect(() => {
        probeGpu(true);
        const iv = setInterval(() => probeGpu(true), 45_000);
        return () => clearInterval(iv);
    }, []);
    const [clock, setClock] = useState(() => new Date());

    useEffect(() => {
        const t = setInterval(() => setClock(new Date()), 1000);
        return () => clearInterval(t);
    }, []);

    const isActive = (path) => (path === "/" ? location.pathname === "/" : location.pathname.startsWith(path));
    const sm = STATUS_META[status];

    return (
        <header
            className="relative z-40 flex shrink-0 items-center gap-2 px-3"
            style={{ height: "var(--bar-h)", background: "var(--surface)", borderBottom: "1px solid var(--line)" }}
        >
            {/* Wordmark */}
            <div data-tour="brand" className="flex shrink-0 items-center gap-2.5 pr-1">
                <img src="/yi.png" alt="" className="h-[26px] w-[26px] rounded-[6px]" />
                {!tiny && (
                    <div className="leading-none">
                        <div className="display text-[14px] font-bold tracking-tight" style={{ color: "var(--text)" }}>
                            Urbanly
                        </div>
                        <div className="mt-[3px] text-[9px] uppercase tracking-[0.14em]" style={{ color: "var(--text-mute)" }}>
                            Urban monitoring
                        </div>
                    </div>
                )}
            </div>

            <div className="mx-1 h-5 w-px shrink-0" style={{ background: "var(--line)" }} />

            {/* Workspaces */}
            <nav aria-label="Primary" className="flex min-w-0 items-center gap-1 overflow-x-auto">
                {NAV_ITEMS.map(({ path, label, icon: Icon }) => {
                    const active = isActive(path);
                    return (
                        <Link
                            key={path}
                            to={path}
                            title={label}
                            aria-current={active ? "page" : undefined}
                            className="relative flex shrink-0 items-center gap-1.5 rounded-[6px] px-2.5 py-1.5 text-[12px] font-medium transition-colors duration-150 hover:bg-[var(--surface-2)]"
                            style={{ color: active ? "var(--signal)" : "var(--text-dim)" }}
                        >
                            {active && (
                                <motion.span
                                    layoutId="cmdbar-active"
                                    className="absolute inset-0 rounded-[6px]"
                                    style={{ background: "var(--signal-dim)", border: "1px solid var(--signal)" }}
                                    transition={{ type: "spring", stiffness: 520, damping: 38 }}
                                />
                            )}
                            <Icon size={13} className="relative" />
                            {!tight && <span className="relative">{label}</span>}
                        </Link>
                    );
                })}
            </nav>

            {location.pathname.startsWith("/mapping") && (
                <>
                    <div className="mx-1 h-5 w-px shrink-0" style={{ background: "var(--line)" }} />
                    <BaseMapMenu tight={tiny} />
                </>
            )}

            <div className="flex-1" />

            {/* Where the AI runs: GPU server, or the Hugging Face fallback */}
            {!tiny && gpu.base && (
                <Pill colour={gpu.ok ? "var(--ok)" : "var(--alert)"}>
                    <span className={`live-dot ${gpu.ok ? "" : "warn"}`} />
                    {!tight && (gpu.ok ? `AI · GPU${gpu.info?.gpu?.free_gb != null ? ` ${gpu.info.gpu.free_gb} GB free` : ""}` : "AI · HF fallback")}
                </Pill>
            )}

            {/* District data backend reachability */}
            {!tiny && (
                <Pill colour={sm.colour}>
                    <span className={`live-dot ${sm.dot}`} style={status === "checking" ? { background: "var(--text-mute)", boxShadow: "none" } : undefined} />
                    {!tight && sm.label}
                </Pill>
            )}

            {/* A demo that drives the real interface, not a video of it. */}
            <Button onClick={() => window.dispatchEvent(new Event("sp:tour"))} title="Guided walkthrough of the platform">
                <Compass size={13} />
                {!tight && "Guide"}
            </Button>

            {/* Clock */}
            {!tiny && (
                <div className="mono px-1 text-[12px] tabular-nums" style={{ color: "var(--text-dim)" }} title="India Standard Time">
                    {clock.toLocaleTimeString("en-GB", { timeZone: "Asia/Kolkata", hour12: false })}
                    <span className="ml-1 text-[9px]" style={{ color: "var(--text-mute)" }}>IST</span>
                </div>
            )}
        </header>
    );
}
