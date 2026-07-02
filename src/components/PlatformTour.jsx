import { useState, useEffect, useRef, useCallback } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";
import { Play, Pause, ChevronRight, Compass, MapPin, Upload as UploadIcon, BarChart2, Layers, Database } from "lucide-react";
import { callTour, fetchFile, sleep, waitForEl } from "../tour/tourBus";
import "./PlatformTour.css";

// A small, low-density AOI on the Vijayawada outskirts (fast to analyse)
const TOUR_AOI = {
    type: "Feature", properties: { name: "Tour AOI" },
    geometry: { type: "Polygon", coordinates: [[[80.565, 16.475], [80.579, 16.475], [80.579, 16.487], [80.565, 16.487], [80.565, 16.475]]] },
};

// click = show a click ripple + cursor press before running the action
const STEPS = [
    // ── HOME ──
    { id: "home", route: "/", target: '[data-tour="brand"]', title: "Welcome to Smart Property Identification",
      desc: "An <strong>AI-powered urban monitoring platform</strong> for Andhra Pradesh. This guided tour drives the whole workflow itself — dashboard, live mapping, AI analysis and reporting. Sit back and watch.", delay: 6500 },
    { id: "home-district", route: "/", target: '[data-tour="home-district"]', title: "Select a District", click: true,
      desc: "Choosing <strong>Vijayawada</strong>. The mini-map flies to the district and loads its boundary, and every stat card and chart updates.", delay: 5500,
      action: async () => { await callTour("home", "selectDistrict", "Vijayawada"); await sleep(2000); } },
    { id: "home-summary", route: "/", target: '[data-tour="home-summary"]', title: "District-wise Summary",
      desc: "Scrolling to the <strong>district-wise summary</strong> — a comparative table of properties, plots, water bodies and detected changes. Click any row to focus that district.", delay: 6000,
      action: async () => { document.querySelector('[data-tour="home-summary"]')?.scrollIntoView({ behavior: "smooth", block: "center" }); await sleep(1200); } },

    // ── MAPPING ──
    { id: "map", route: "/mapping", target: '[data-tour="basemap"]', title: "Mapping — the Core Workspace",
      desc: "This is the heart of the platform: an interactive map with switchable base maps, AI land-use layers, area-of-interest analytics and detection overlays.", delay: 6000 },
    { id: "basemap", route: "/mapping", target: '[data-tour="basemap"]', title: "Base Map Styles", click: true,
      desc: "Trying <strong>Streets</strong> then <strong>Dark</strong>, then returning to <strong>Satellite</strong> — each style fully loads before moving on.", delay: 3000,
      action: async () => {
          await callTour("mapping", "setBaseMap", "streets-v12"); await callTour("mapping", "waitIdle"); await sleep(1400);
          await callTour("mapping", "setBaseMap", "dark-v11"); await callTour("mapping", "waitIdle"); await sleep(1400);
          await callTour("mapping", "setBaseMap", "satellite-streets-v12"); await callTour("mapping", "waitIdle"); await sleep(600);
      } },
    { id: "map-district", route: "/mapping", target: '[data-tour="map-district"]', title: "Choose District — Vijayawada", click: true,
      desc: "Selecting <strong>Vijayawada</strong>. The map flies in and the district's layers become available.", delay: 4500,
      action: async () => { await callTour("mapping", "selectDistrict", "Vijayawada"); await callTour("mapping", "waitIdle"); await sleep(1500); } },
    { id: "boundary", route: "/mapping", target: '[data-tour="map-layers"]', title: "Turn On the Boundary Layer", click: true,
      desc: "Enabling the <strong>district boundary</strong> first — it frames the area we're about to analyse.", delay: 5000,
      action: async () => { await callTour("mapping", "toggleBoundary"); await sleep(2500); } },
    { id: "mask", route: "/mapping", target: '[data-tour="map-layers"]', title: "Turn On the Land Use Mask", click: true,
      desc: "Enabling the <strong>AI Land Use Mask</strong> — the SegFormer-B5 classification (buildings, roads, water, open land) overlaid on the imagery.", delay: 5500,
      action: async () => { await callTour("mapping", "toggleMask"); await sleep(3500); } },
    { id: "aoi-draw", route: "/mapping", target: '[data-tour="aoi-panel"]', title: "Draw an Area of Interest", click: true,
      desc: "In the <strong>Area of Interest</strong> panel you can draw a polygon or upload a boundary. Drawing a small AOI on the Vijayawada outskirts — a low-density area so analytics compute quickly.", delay: 5000,
      action: async () => { await callTour("mapping", "drawDemoAOI", TOUR_AOI); await sleep(2500); } },
    { id: "aoi-stats", route: "/mapping", target: '[data-tour="aoi-stats"]', title: "AOI Statistics",
      desc: "The platform clips the data to the boundary and counts <strong>buildings, water bodies and road length</strong> inside it. This runs on the cloud backend — give it a moment to finish.", delay: 11000 },
    { id: "aoi-upload", route: "/mapping", target: '[data-tour="aoi-panel"]', title: "Or Upload a Boundary File", click: true,
      desc: "Instead of drawing, you can <strong>upload</strong> a GeoJSON / Shapefile / KML / GeoPackage. Loading a multi-parcel ward-boundary file — every parcel is plotted on the map.", delay: 7000,
      action: async () => { const f = await fetchFile("/tour/WS_Boundaries.geojson", "WS_Boundaries.geojson", "application/geo+json"); await callTour("mapping", "uploadParcels", f); await callTour("mapping", "waitIdle"); await sleep(2500); } },
    { id: "aoi-parcel", route: "/mapping", target: '[data-tour="aoi-stats"]', title: "Tap a Parcel for Its Stats", click: true,
      desc: "With a multi-polygon upload you can <strong>click any parcel</strong> to get that parcel's own building / water / road counts and area — ideal for per-ward or per-plot assessment.", delay: 8000,
      action: async () => { await callTour("mapping", "selectParcel", 0); await sleep(2500); } },

    // ── UPLOAD & ANALYSIS ──
    { id: "upload", route: "/upload", target: '[data-tour="upload-zone"]', title: "Upload & Analysis",
      desc: "Run the AI on your own imagery. <strong>AI Segmentation</strong> classifies a single image; <strong>Change Detection</strong> compares two. Files up to 500 MB stream via temporary cloud storage.", delay: 6000,
      action: async () => { await callTour("upload", "setAnalysisType", "segment"); await sleep(600); } },
    { id: "upload-seg", route: "/upload", target: '[data-tour="upload-zone"]', title: "Drag & Drop an Image", click: true,
      desc: "Adding a satellite chip (<strong>chip_9216_57344.tif</strong>) for segmentation.", delay: 4500,
      action: async () => { await callTour("upload", "addSegFile", "/tour/chip_9216_57344.tif", "chip_9216_57344.tif"); await sleep(1500); } },
    { id: "run-seg", route: "/upload", target: '[data-tour="run-btn"]', title: "Run AI Segmentation", click: true,
      desc: "Running the <strong>SegFormer-B5</strong> model — a colour-coded mask (buildings / roads / water / open) plus class stats. On the free CPU tier this takes ~15–40s; waiting for the result…", delay: 45000,
      action: async () => { await callTour("upload", "run"); await sleep(1500); } },
    { id: "plot-overlay", route: "/upload", target: '[data-tour="plot-overlay"]', title: "Plot the Detection on the Map", click: true,
      desc: "For a GeoTIFF you can push the detection mask straight onto the Mapping page as a <strong>geo-referenced overlay</strong> with opacity and zoom controls.", delay: 6000,
      action: async () => { const b = await waitForEl('[data-tour="plot-overlay"]', 4000); b?.click(); await sleep(1500); } },
    { id: "change", route: "/upload", target: '[data-tour="cd-uploads"]', title: "Change Detection — Two Images", click: true,
      desc: "Switching to <strong>Change Detection</strong>. Uploading a PAST and a PRESENT image of the same area; the model segments both and diffs the masks.", delay: 6000,
      action: async () => { await callTour("upload", "setAnalysisType", "change"); await sleep(600); await callTour("upload", "setChangeFiles", "/tour/chip_9216_57344.tif", "past.tif", "/tour/chip_13824_14848.tif", "present.tif"); await sleep(1800); } },
    { id: "run-change", route: "/upload", target: '[data-tour="run-btn"]', title: "Run Change Detection", click: true,
      desc: "The result highlights <strong>new construction, demolition, new roads</strong> and other land-use changes, with per-category area percentages.", delay: 45000,
      action: async () => { await callTour("upload", "run"); await sleep(1500); } },

    // ── DATA LOGS ──
    { id: "datalogs", route: "/datalogs", target: '[data-tour="datalogs"]', title: "Data Logs",
      desc: "Every analysis and processing run is recorded here as <strong>recent activity</strong> — an audit trail of segmentation, change-detection and report jobs across districts.", delay: 6000 },

    // ── DSS ──
    { id: "dss", route: "/dss", target: '[data-tour="dss-form"]', title: "Decision Support System", click: true,
      desc: "The <strong>DSS</strong> generates official reports. Choosing a district, an analysis date range and the data types to include.", delay: 5500,
      action: async () => { await callTour("dss", "selectDistrict", "Vijayawada"); await sleep(1500); await callTour("dss", "setDates", "2025-01-01", "2025-02-28"); await callTour("dss", "toggleDataType", "Property Identification"); await sleep(1500); } },
    { id: "dss-generate", route: "/dss", target: '[data-tour="dss-generate"]', title: "Generate the Report", click: true,
      desc: "Clicking <strong>Generate Report</strong> pulls live stats and builds a full, print-ready PDF (executive summary, property results, change detection, governance insights) for ULB officials.", delay: 8000,
      action: async () => { await callTour("dss", "generate"); await sleep(2500); } },
    { id: "done", route: "/dss", target: '[data-tour="dss-form"]', title: "That's the Full Workflow!",
      desc: "From dashboard → mapping & AOI analytics → AI segmentation & change detection → data logs → decision-support reports. Explore any section yourself, or replay this tour anytime from the <strong>Guided Tour</strong> button.", delay: 8000 },
];

const CursorSvg = () => (
    <svg viewBox="0 0 24 24" fill="none"><path d="M5.65 2.92L19.08 12.03C19.56 12.35 19.36 13.1 18.79 13.13L12.48 13.46L9.8 19.23C9.56 19.75 8.8 19.67 8.68 19.12L5.05 3.61C4.94 3.12 5.28 2.67 5.65 2.92Z" fill="#2dd4bf" stroke="#0d9488" strokeWidth="0.8" /></svg>
);

function TourWelcome({ onStart, onDismiss }) {
    return (
        <div className="tour-welcome">
            <div className="tour-welcome-card">
                <div className="tour-welcome-icon"><Compass size={26} /></div>
                <div className="tour-welcome-title">Platform Guided Tour</div>
                <div className="tour-welcome-subtitle">Watch the complete Smart Property Identification workflow — dashboard, live mapping, AI segmentation, change detection and reports — as an auto-playing walkthrough.</div>
                <div className="tour-welcome-features">
                    <div className="tour-welcome-feature"><BarChart2 size={13} /> Dashboard</div>
                    <div className="tour-welcome-feature"><MapPin size={13} /> Mapping & AOI</div>
                    <div className="tour-welcome-feature"><Layers size={13} /> AI Segmentation</div>
                    <div className="tour-welcome-feature"><UploadIcon size={13} /> Change Detection</div>
                    <div className="tour-welcome-feature"><Database size={13} /> Data Logs</div>
                    <div className="tour-welcome-feature"><Compass size={13} /> DSS Reports</div>
                </div>
                <div className="tour-welcome-actions">
                    <button className="tour-btn-dismiss" onClick={onDismiss}>Skip for now</button>
                    <button className="tour-btn-start" onClick={onStart}><Play size={15} /> Start Tour</button>
                </div>
            </div>
        </div>
    );
}

export default function PlatformTour() {
    const navigate = useNavigate();
    const location = useLocation();
    const [phase, setPhase] = useState("idle"); // idle | welcome | running
    const [si, setSi] = useState(0);
    const [paused, setPaused] = useState(false);
    const [sr, setSr] = useState(null);                 // spotlight rect
    const [tp, setTp] = useState({ x: 0, y: 0 });        // tooltip pos
    const [cur, setCur] = useState({ x: window.innerWidth / 2, y: window.innerHeight / 2 });
    const [clicking, setClicking] = useState(false);
    const [ripple, setRipple] = useState(null);
    const [ttVis, setTtVis] = useState(false);
    const timer = useRef(null);
    const siRef = useRef(0); siRef.current = si;
    const pausedRef = useRef(false); pausedRef.current = paused;
    const step = STEPS[si] || null;

    // Auto-show the welcome popup on first load
    useEffect(() => {
        if (!localStorage.getItem("sp_tour_seen")) {
            const t = setTimeout(() => setPhase((p) => (p === "idle" ? "welcome" : p)), 900);
            return () => clearTimeout(t);
        }
    }, []);

    useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
    useEffect(() => {
        const h = (e) => { if (e.key === "Escape") exit(); };
        if (phase === "running" || phase === "welcome") window.addEventListener("keydown", h);
        return () => window.removeEventListener("keydown", h);
    }, [phase]);

    const exit = useCallback(() => {
        if (timer.current) clearTimeout(timer.current);
        localStorage.setItem("sp_tour_seen", "true");
        setPhase("idle"); setSi(0); setPaused(false); setTtVis(false); setSr(null);
    }, []);

    const posTT = useCallback((rect) => {
        const tw = 410, th = 220, m = 16;
        if (!rect) { setTp({ x: window.innerWidth - tw - m, y: window.innerHeight - th - m }); return; }
        let y = rect.top + rect.height + m + th < window.innerHeight ? rect.top + rect.height + m
            : rect.top - th - m > 0 ? rect.top - th - m : Math.max(m, (window.innerHeight - th) / 2);
        let x = rect.left; if (x + tw > window.innerWidth - m) x = window.innerWidth - tw - m; if (x < m) x = m;
        setTp({ x, y });
    }, []);

    // spotlight + glide cursor to a target selector
    const focus = useCallback(async (sel) => {
        const el = await waitForEl(sel, 3500);
        if (!el) { setSr(null); posTT(null); return null; }
        if (el.getBoundingClientRect().top < 0 || el.getBoundingClientRect().bottom > window.innerHeight)
            el.scrollIntoView({ behavior: "smooth", block: "center" });
        await sleep(el.getBoundingClientRect().top < 0 ? 500 : 0);
        const r = el.getBoundingClientRect(), pad = 8;
        const s = { top: r.top - pad, left: r.left - pad, width: r.width + pad * 2, height: r.height + pad * 2 };
        setSr(s); posTT(s);
        setCur({ x: r.left + r.width / 2, y: r.top + r.height / 2 });
        return r;
    }, [posTT]);

    const goNext = useCallback(() => {
        if (timer.current) clearTimeout(timer.current);
        const n = siRef.current + 1;
        if (n >= STEPS.length) { exit(); return; }
        setSi(n);
    }, [exit]);

    // run a step when si changes (while running)
    useEffect(() => {
        if (phase !== "running") return;
        let cancelled = false;
        const s = STEPS[si]; if (!s) { exit(); return; }
        setTtVis(false); setSr(null);
        (async () => {
            if (s.route && location.pathname !== s.route) { navigate(s.route); await sleep(1000); }
            if (cancelled) return;
            const r = await focus(s.target);      // spotlight + glide cursor
            setTtVis(true);
            await sleep(900);                      // let it glide + user read
            if (cancelled) return;
            if (s.action) {
                if (s.click && r) { setRipple({ x: r.left + r.width / 2, y: r.top + r.height / 2, id: Date.now() }); setClicking(true); await sleep(280); setClicking(false); }
                try { await s.action({ callTour, fetchFile, sleep, navigate }); } catch (e) { console.warn("tour step action:", e); }
                if (cancelled) return;
                await focus(s.target);             // re-anchor (layout may have shifted)
            }
            if (!pausedRef.current) timer.current = setTimeout(() => { if (siRef.current === si) goNext(); }, s.delay || 5000);
        })();
        return () => { cancelled = true; if (timer.current) clearTimeout(timer.current); };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [si, phase]);

    useEffect(() => {
        if (phase !== "running") return;
        const onResize = () => focus(STEPS[siRef.current]?.target);
        window.addEventListener("resize", onResize);
        return () => window.removeEventListener("resize", onResize);
    }, [phase, focus]);

    const togglePause = () => {
        setPaused((p) => {
            const np = !p;
            if (np) { if (timer.current) clearTimeout(timer.current); }
            else { const s = STEPS[siRef.current]; timer.current = setTimeout(() => goNext(), (s?.delay || 5000) / 2); }
            return np;
        });
    };

    if (phase === "idle") {
        return (
            <button className="tour-launch-btn" onClick={() => { setSi(0); setPaused(false); setPhase("welcome"); }} title="Guided platform tour">
                <Compass size={15} /> <span>Guided Tour</span>
            </button>
        );
    }

    return (
        <AnimatePresence>
            {phase === "welcome" && (
                <motion.div key="w" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.3 }}>
                    <TourWelcome onStart={() => { setSi(0); setPhase("running"); }} onDismiss={exit} />
                </motion.div>
            )}
            {phase === "running" && step && (
                <motion.div key="t" className="tour-overlay" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.3 }}>
                    {sr ? <div className="tour-spotlight" style={{ top: sr.top, left: sr.left, width: sr.width, height: sr.height }} />
                        : <div className="tour-backdrop active" />}
                    {/* gliding cursor */}
                    <div className={`tour-cursor ${clicking ? "clicking" : ""}`} style={{ left: cur.x, top: cur.y }}><CursorSvg /></div>
                    <AnimatePresence>
                        {ripple && <motion.div key={ripple.id} className="tour-cursor-ripple" style={{ left: ripple.x, top: ripple.y }}
                            initial={{ opacity: 1 }} animate={{ opacity: 0 }} transition={{ duration: 0.6 }} onAnimationComplete={() => setRipple(null)} />}
                    </AnimatePresence>
                    <AnimatePresence>
                        {ttVis && (
                            <motion.div key={`tt${si}`} className="tour-tooltip" style={{ left: tp.x, top: tp.y }}
                                initial={{ opacity: 0, y: 8, scale: 0.96 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, scale: 0.96 }} transition={{ duration: 0.2 }}>
                                <div className="tour-tooltip-glow" />
                                <div className="tour-tooltip-header">
                                    <span className="tour-tooltip-badge">{si + 1}</span>
                                    <span className="tour-tooltip-title">{step.title}</span>
                                </div>
                                <p className="tour-tooltip-desc" dangerouslySetInnerHTML={{ __html: step.desc }} />
                                <div className="tour-tooltip-footer">
                                    <div className="tour-tooltip-progress">
                                        <div className="tour-tooltip-progress-bar"><div className="tour-tooltip-progress-fill" style={{ width: `${((si + 1) / STEPS.length) * 100}%` }} /></div>
                                        <span>{si + 1}/{STEPS.length}</span>
                                    </div>
                                    <div className="tour-tooltip-actions">
                                        <button className="tour-btn tour-btn-skip" onClick={exit}>Skip</button>
                                        <button className="tour-btn tour-btn-pause" onClick={togglePause}>{paused ? <Play size={12} /> : <Pause size={12} />}</button>
                                        <button className="tour-btn tour-btn-next" onClick={goNext}>{si + 1 >= STEPS.length ? "Finish" : "Next"} <ChevronRight size={13} /></button>
                                    </div>
                                </div>
                            </motion.div>
                        )}
                    </AnimatePresence>
                </motion.div>
            )}
        </AnimatePresence>
    );
}
