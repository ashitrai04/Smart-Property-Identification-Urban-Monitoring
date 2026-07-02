import { useState, useEffect, useRef, useCallback } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";
import { Play, Pause, ChevronRight, Compass, MapPin, Upload as UploadIcon, BarChart2, Layers, Database } from "lucide-react";
import { callTour, fetchFile, sleep } from "../tour/tourBus";
import "./PlatformTour.css";

// A small, low-density AOI on the Vijayawada outskirts (fast to analyse)
const TOUR_AOI = {
    type: "Feature", properties: { name: "Tour AOI" },
    geometry: { type: "Polygon", coordinates: [[[80.565, 16.475], [80.579, 16.475], [80.579, 16.487], [80.565, 16.487], [80.565, 16.475]]] },
};

const STEPS = [
    // ── HOME ──
    { id: "home", route: "/", target: '[data-tour="brand"]', title: "Welcome to Smart Property Identification",
      desc: "An <strong>AI-powered urban monitoring platform</strong> for Andhra Pradesh. This guided tour walks through the whole workflow — dashboard, live mapping, AI analysis, and reporting. The Home dashboard summarises property, plot, water-body and change statistics across all districts.", delay: 5500 },
    { id: "home-district", route: "/", target: '[data-tour="home-district"]', title: "Select a District",
      desc: "Choosing <strong>Vijayawada</strong> from the dropdown. The mini-map flies to the district and loads its boundary, while every stat card and chart updates to that district.", delay: 4500,
      action: async () => { await callTour("home", "selectDistrict", "Vijayawada"); } },
    { id: "home-summary", route: "/", target: '[data-tour="home-summary"]', title: "District-wise Summary",
      desc: "Scrolling to the <strong>district-wise summary</strong> — a comparative table of properties, open plots, water bodies and detected changes for every district. Click any row to focus that district.", delay: 4500,
      action: async () => { document.querySelector('[data-tour="home-summary"]')?.scrollIntoView({ behavior: "smooth", block: "center" }); } },

    // ── MAPPING ──
    { id: "map", route: "/mapping", target: '[data-tour="basemap"]', title: "Mapping — the Core Workspace",
      desc: "This is the heart of the platform: an interactive map of AP with switchable base maps, land-use layers, area-of-interest analytics and detection overlays.", delay: 4500 },
    { id: "basemap", route: "/mapping", target: '[data-tour="basemap"]', title: "Base Map Styles",
      desc: "Cycling through <strong>Streets</strong> and <strong>Dark</strong> base maps, then back to <strong>Satellite</strong> — pick whichever context suits the analysis.", delay: 5200,
      action: async () => { await callTour("mapping", "setBaseMap", "streets-v12"); await sleep(1200); await callTour("mapping", "setBaseMap", "dark-v11"); await sleep(1200); await callTour("mapping", "setBaseMap", "satellite-streets-v12"); } },
    { id: "map-district", route: "/mapping", target: '[data-tour="map-district"]', title: "Choose District — Vijayawada",
      desc: "Selecting <strong>Vijayawada</strong>. The map flies in and the district's layers become available.", delay: 4000,
      action: async () => { await callTour("mapping", "selectDistrict", "Vijayawada"); } },
    { id: "boundary", route: "/mapping", target: '[data-tour="map-layers"]', title: "Turn On the Boundary Layer",
      desc: "Enabling the <strong>district boundary</strong> first — it frames the area we're about to analyse.", delay: 4000,
      action: async () => { await callTour("mapping", "toggleBoundary"); } },
    { id: "mask", route: "/mapping", target: '[data-tour="map-layers"]', title: "Turn On the Land Use Mask",
      desc: "Enabling the <strong>AI Land Use Mask</strong> — the SegFormer-B5 classification overlaid on the imagery: buildings, roads, water and open land.", delay: 4500,
      action: async () => { await callTour("mapping", "toggleMask"); } },
    { id: "aoi-draw", route: "/mapping", target: '[data-tour="aoi-panel"]', title: "Draw an Area of Interest",
      desc: "In the <strong>Area of Interest</strong> panel you can draw a polygon or upload a boundary. We're drawing a small AOI on the Vijayawada outskirts — a low-density area so analytics compute quickly.", delay: 4500,
      action: async () => { await callTour("mapping", "drawDemoAOI", TOUR_AOI); } },
    { id: "aoi-stats", route: "/mapping", target: '[data-tour="aoi-stats"]', title: "AOI Statistics",
      desc: "The platform clips the data to the boundary and counts <strong>buildings, water bodies and road length</strong> inside it. This runs on the cloud backend — give it a moment to finish.", delay: 9000 },
    { id: "aoi-upload", route: "/mapping", target: '[data-tour="aoi-panel"]', title: "Or Upload a Boundary File",
      desc: "Instead of drawing, you can <strong>upload</strong> a GeoJSON / Shapefile / KML / GeoPackage. Loading a multi-parcel ward-boundary file — every parcel is plotted on the map.", delay: 6000,
      action: async () => { const f = await fetchFile("/tour/WS_Boundaries.geojson", "WS_Boundaries.geojson", "application/geo+json"); await callTour("mapping", "uploadParcels", f); } },
    { id: "aoi-parcel", route: "/mapping", target: '[data-tour="aoi-stats"]', title: "Tap a Parcel for Its Stats",
      desc: "With a multi-polygon upload you can <strong>click any parcel</strong> to get that parcel's own building/water/road counts and area — perfect for per-ward or per-plot assessment.", delay: 6000,
      action: async () => { await callTour("mapping", "selectParcel", 0); } },

    // ── UPLOAD & ANALYSIS ──
    { id: "upload", route: "/upload", target: '[data-tour="upload-zone"]', title: "Upload & Analysis",
      desc: "Run the AI on your own imagery. <strong>AI Segmentation</strong> classifies a single image; <strong>Change Detection</strong> compares two. Files up to 500 MB are supported via temporary cloud storage.", delay: 5000,
      action: async () => { await callTour("upload", "setAnalysisType", "segment"); } },
    { id: "upload-seg", route: "/upload", target: '[data-tour="upload-zone"]', title: "Drag & Drop an Image",
      desc: "Adding a satellite chip (<strong>chip_9216_57344.tif</strong>) for segmentation.", delay: 4000,
      action: async () => { await callTour("upload", "addSegFile", "/tour/chip_9216_57344.tif", "chip_9216_57344.tif"); } },
    { id: "run-seg", route: "/upload", target: '[data-tour="run-btn"]', title: "Run AI Segmentation",
      desc: "Running the <strong>SegFormer-B5</strong> model. It returns a colour-coded mask (buildings / roads / water / open) plus class-distribution stats. On the free CPU tier this takes ~15–40s — watch the result appear.", delay: 42000,
      action: async () => { await callTour("upload", "run"); } },
    { id: "plot-overlay", route: "/upload", target: '[data-tour="plot-overlay"]', title: "Plot the Detection on the Map",
      desc: "For a GeoTIFF you can push the detection mask straight onto the Mapping page as a <strong>geo-referenced overlay</strong> with opacity and zoom controls.", delay: 5000,
      action: async () => { document.querySelector('[data-tour="plot-overlay"]')?.click(); await sleep(500); } },
    { id: "change", route: "/upload", target: '[data-tour="cd-uploads"]', title: "Change Detection — Two Images",
      desc: "Switching to <strong>Change Detection</strong>. Upload a PAST and a PRESENT image of the same area; the model segments both and diffs the masks.", delay: 4500,
      action: async () => { await callTour("upload", "setAnalysisType", "change"); await sleep(400); await callTour("upload", "setChangeFiles", "/tour/chip_9216_57344.tif", "past.tif", "/tour/chip_13824_14848.tif", "present.tif"); } },
    { id: "run-change", route: "/upload", target: '[data-tour="run-btn"]', title: "Run Change Detection",
      desc: "The result highlights <strong>new construction, demolition, new roads</strong> and other land-use changes, with per-category area percentages.", delay: 42000,
      action: async () => { await callTour("upload", "run"); } },

    // ── DATA LOGS ──
    { id: "datalogs", route: "/datalogs", target: '[data-tour="datalogs"]', title: "Data Logs",
      desc: "Every analysis and processing run is recorded here as <strong>recent activity</strong> — an audit trail of segmentation, change-detection and report jobs across districts.", delay: 5000 },

    // ── DSS ──
    { id: "dss", route: "/dss", target: '[data-tour="dss-form"]', title: "Decision Support System",
      desc: "The <strong>DSS</strong> generates official reports. Choose a district, an analysis date range, and the data types to include.", delay: 4500,
      action: async () => { await callTour("dss", "selectDistrict", "Vijayawada"); await sleep(300); await callTour("dss", "setDates", "2025-01-01", "2025-02-28"); await callTour("dss", "toggleDataType", "Property Identification"); } },
    { id: "dss-generate", route: "/dss", target: '[data-tour="dss-generate"]', title: "Generate the Report",
      desc: "Clicking <strong>Generate Report</strong> pulls live stats and builds a full, print-ready PDF (executive summary, property results, change detection, governance insights) for ULB officials.", delay: 6000,
      action: async () => { await callTour("dss", "generate"); } },
    { id: "done", route: "/dss", target: '[data-tour="dss-form"]', title: "That's the Full Workflow!",
      desc: "From dashboard → mapping & AOI analytics → AI segmentation & change detection → data logs → decision-support reports. Explore any section yourself, or replay this tour anytime from the <strong>Guided Tour</strong> button.", delay: 6000 },
];

const CursorSvg = () => (
    <svg viewBox="0 0 24 24" fill="none"><path d="M5.65 2.92L19.08 12.03C19.56 12.35 19.36 13.1 18.79 13.13L12.48 13.46L9.8 19.23C9.56 19.75 8.8 19.67 8.68 19.12L5.05 3.61C4.94 3.12 5.28 2.67 5.65 2.92Z" fill="#14b8a6" stroke="#0d9488" strokeWidth="0.8" /></svg>
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
                    <button className="tour-btn-dismiss" onClick={onDismiss}>Skip</button>
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
    const [sr, setSr] = useState(null);         // spotlight rect
    const [tp, setTp] = useState({ x: 0, y: 0 });
    const [ttVis, setTtVis] = useState(false);
    const timer = useRef(null);
    const siRef = useRef(0); siRef.current = si;
    const pausedRef = useRef(false); pausedRef.current = paused;
    const step = STEPS[si] || null;

    useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
    useEffect(() => {
        const h = (e) => { if (e.key === "Escape") exit(); };
        if (phase === "running") window.addEventListener("keydown", h);
        return () => window.removeEventListener("keydown", h);
    }, [phase]);

    const exit = useCallback(() => {
        if (timer.current) clearTimeout(timer.current);
        localStorage.setItem("sp_tour_seen", "true");
        setPhase("idle"); setSi(0); setPaused(false); setTtVis(false); setSr(null);
    }, []);

    const posTT = useCallback((rect) => {
        const tw = 400, th = 210, m = 16;
        if (!rect) { setTp({ x: (window.innerWidth - tw) / 2, y: window.innerHeight - th - 40 }); return; }
        let y = rect.top + rect.height + m + th < window.innerHeight ? rect.top + rect.height + m
            : rect.top - th - m > 0 ? rect.top - th - m : Math.max(m, (window.innerHeight - th) / 2);
        let x = rect.left; if (x + tw > window.innerWidth - m) x = window.innerWidth - tw - m; if (x < m) x = m;
        setTp({ x, y });
    }, []);

    const spot = useCallback((sel) => {
        const el = sel ? document.querySelector(sel) : null;
        if (!el) { setSr(null); posTT(null); return; }
        if (el.getBoundingClientRect().top < 0 || el.getBoundingClientRect().bottom > window.innerHeight) {
            el.scrollIntoView({ behavior: "smooth", block: "center" });
        }
        const r = el.getBoundingClientRect(), pad = 8;
        const s = { top: r.top - pad, left: r.left - pad, width: r.width + pad * 2, height: r.height + pad * 2 };
        setSr(s); posTT(s);
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
            if (s.route && location.pathname !== s.route) { navigate(s.route); await sleep(800); }
            if (cancelled) return;
            if (s.action) { try { await s.action({ callTour, fetchFile, sleep, navigate }); } catch (e) { console.warn("tour step action:", e); } }
            if (cancelled) return;
            await sleep(350);
            spot(s.target);
            setTtVis(true);
            if (!pausedRef.current) {
                timer.current = setTimeout(() => { if (siRef.current === si) goNext(); }, s.delay || 4500);
            }
        })();
        return () => { cancelled = true; if (timer.current) clearTimeout(timer.current); };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [si, phase]);

    // reposition spotlight/tooltip on resize
    useEffect(() => {
        if (phase !== "running") return;
        const onResize = () => spot(STEPS[siRef.current]?.target);
        window.addEventListener("resize", onResize);
        return () => window.removeEventListener("resize", onResize);
    }, [phase, spot]);

    const togglePause = () => {
        setPaused((p) => {
            const np = !p;
            if (np) { if (timer.current) clearTimeout(timer.current); }
            else { const s = STEPS[siRef.current]; timer.current = setTimeout(() => goNext(), (s?.delay || 4500) / 2); }
            return np;
        });
    };

    // ── Launch button (always available when idle) ──
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
                        : <div className="tour-backdrop" />}
                    {sr && <div className="tour-cursor" style={{ left: sr.left + sr.width / 2, top: sr.top + sr.height / 2 }}><CursorSvg /></div>}
                    <AnimatePresence>
                        {ttVis && (
                            <motion.div key={`tt${si}`} className="tour-tooltip" style={{ left: tp.x, top: tp.y }}
                                initial={{ opacity: 0, y: 8, scale: 0.96 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, scale: 0.96 }} transition={{ duration: 0.2 }}>
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
