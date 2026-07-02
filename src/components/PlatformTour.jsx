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

// Set a React-controlled <select>/<input> the way a real user would —
// through the native setter so React's onChange fires.
function setNativeValue(el, value) {
    if (!el) return;
    const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype
        : el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
            : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, value); else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
}

// Poll an arbitrary condition (used to wait for real task completion).
async function waitUntil(fn, timeout = 30000, interval = 400) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeout) {
        try { if (fn()) return true; } catch (_) { /* keep polling */ }
        await sleep(interval);
    }
    return false;
}

/*
 * Each step: spotlight `target`, show guidance copy, then run `script` —
 * a sequence of sub-actions where the cursor GLIDES to the actual control,
 * presses it (ripple), and the real click / real select-change happens.
 *   { sel, click:true }          → glide + real el.click()
 *   { sel, select:"v" }          → glide + set a real <select> value
 *   { sel, input:"v" }           → glide + type a real <input> value
 *   { sel, run: fn }             → glide + ripple, then run fn (for file "uploads")
 *   { wait: ms }                 → pause so the result can be watched
 * `waitFor` polls until the real task finishes (AI runs, stats, reports).
 */
const STEPS = [
    // ── HOME ──
    { id: "welcome", route: "/", target: '[data-tour="brand"]', title: "Smart Property Identification",
      desc: "This platform keeps an <strong>AI-maintained map of urban Andhra Pradesh</strong> — every building, road, water body and open plot, kept current from satellite imagery. The tour will now operate the platform for you, exactly the way you would.", hold: 7000 },

    { id: "home-district", route: "/", target: '[data-tour="home-district"]', title: "Start on the Dashboard",
      desc: "The Home page summarises the entire state. Use the <strong>District dropdown</strong> to focus anywhere — the mini-map flies to that district and every stat updates. Let's pick <strong>Vijayawada</strong>.",
      script: [ { sel: '[data-tour="home-district"]', select: "Vijayawada", wait: 2600 } ], hold: 4500 },

    { id: "home-summary", route: "/", target: '[data-tour="home-summary"]', title: "Compare Districts at a Glance",
      desc: "Further down, the <strong>district-wise summary</strong> lines up properties, open plots, water bodies and detected changes for all five districts — useful for spotting where growth is fastest.",
      script: [ { sel: '[data-tour="home-summary"]', silent: true, wait: 1000 } ], hold: 6000 },

    // ── MAPPING ──
    { id: "map-intro", route: "/mapping", target: '[data-tour="basemap"]', title: "The Mapping Workspace",
      desc: "This is where you explore the AI's results on a live map. The <strong>Layers panel</strong> on the left controls what you see; the <strong>Area of Interest</strong> panel on the right measures whatever you point it at.", hold: 6500 },

    { id: "basemap", route: "/mapping", target: '[data-tour="basemap"]', title: "Pick Your Base Map",
      desc: "Five map styles are available — <strong>Streets</strong> for addresses and navigation, <strong>Dark</strong> when you want overlays to stand out, and <strong>Satellite</strong> for true ground detail. Watch each one load.",
      script: [
          { sel: '[data-tour-bm="streets-v12"]', click: true, run: async () => { await callTour("mapping", "waitIdle"); }, wait: 1600 },
          { sel: '[data-tour-bm="dark-v11"]', click: true, run: async () => { await callTour("mapping", "waitIdle"); }, wait: 1600 },
          { sel: '[data-tour-bm="satellite-streets-v12"]', click: true, run: async () => { await callTour("mapping", "waitIdle"); }, wait: 1200 },
      ], hold: 3500 },

    { id: "map-district", route: "/mapping", target: '[data-tour="map-district"]', title: "Load a District",
      desc: "Choosing a district loads its AI data and flies the map there. Selecting <strong>Vijayawada</strong> — notice its layer list appear below once it loads.",
      script: [ { sel: '[data-tour="map-district"] select', select: "Vijayawada", run: async () => { await callTour("mapping", "waitIdle"); }, wait: 1800 } ], hold: 4500 },

    { id: "boundary", route: "/mapping", target: '[data-tour="layer-boundary"]', title: "Layer 1 — District Boundary",
      desc: "Each switch adds one layer to the map. <strong>Boundary</strong> draws the official district limits — always turn this on first so you know exactly what area you're looking at.",
      script: [ { sel: '[data-tour="layer-boundary-knob"]', click: true, wait: 3200 } ], hold: 4500 },

    { id: "mask", route: "/mapping", target: '[data-tour="toggle-mask"]', title: "Layer 2 — AI Land Use Mask",
      desc: "The <strong>Land Use Mask</strong> is the model's reading of every pixel: <strong>red</strong> buildings, <strong>yellow</strong> roads, <strong>blue</strong> water, <strong>grey</strong> open ground. Zoom anywhere and check it against the imagery underneath.",
      script: [ { sel: '[data-tour="toggle-mask-knob"]', click: true, wait: 4200 } ], hold: 5000 },

    { id: "aoi-draw", route: "/mapping", target: '[data-tour="aoi-panel"]', title: "Measure Any Area — Draw an AOI",
      desc: "Need numbers for a specific site? Click <strong>Draw AOI</strong> and outline it on the map. We'll trace a small area on the city's edge — everything outside it gets clipped away.",
      script: [
          { sel: '[data-tour="aoi-draw-btn"]', click: true, wait: 1100 },
          { run: async () => { await callTour("mapping", "drawDemoAOI", TOUR_AOI); }, wait: 2400 },
      ], hold: 4000 },

    { id: "aoi-stats", route: "/mapping", target: '[data-tour="aoi-stats"]', title: "Instant Area Statistics",
      desc: "The platform counts what falls <strong>inside your boundary</strong>: number of buildings, water bodies, and total road length in km — plus the area itself. No GIS software or expertise needed.",
      waitFor: { check: () => { const el = document.querySelector('[data-tour="aoi-stats"]'); return el && !/Analyzing/i.test(el.textContent); }, timeout: 60000 },
      hold: 8000 },

    { id: "aoi-upload", route: "/mapping", target: '[data-tour="aoi-panel"]', title: "Or Upload Your Own Boundaries",
      desc: "Already have survey boundaries? <strong>Upload</strong> accepts GeoJSON, Shapefile, KML and GeoPackage. Here's a municipal <strong>ward-boundary file</strong> — every ward lands on the map as its own parcel.",
      script: [
          { sel: '[data-tour="aoi-upload-btn"]', run: async () => {
              const f = await fetchFile("/tour/WS_Boundaries.geojson", "WS_Boundaries.geojson", "application/geo+json");
              await callTour("mapping", "uploadParcels", f);
              await callTour("mapping", "waitIdle");
          }, wait: 2600 },
      ], hold: 6000 },

    { id: "aoi-parcel", route: "/mapping", target: '[data-tour="aoi-stats"]', title: "Click a Parcel for Its Own Numbers",
      desc: "With multiple parcels loaded, <strong>click any one on the map</strong> to get that parcel's own building count, water bodies, road length and area — ward-level or plot-level assessment in one click.",
      script: [ { run: async () => { await callTour("mapping", "selectParcel", 0); }, wait: 2400 } ], hold: 7500 },

    // ── UPLOAD & ANALYSIS ──
    { id: "upload-intro", route: "/upload", target: '[data-tour="atype-segment"]', title: "Analyse Your Own Imagery",
      desc: "Bring your own satellite or drone photos. <strong>AI Segmentation</strong> maps a single image; <strong>Change Detection</strong> compares two dates. Files up to <strong>500 MB</strong> are handled through temporary cloud storage.",
      script: [ { sel: '[data-tour="atype-segment"]', click: true, wait: 900 } ], hold: 5000 },

    { id: "upload-file", route: "/upload", target: '[data-tour="upload-zone"]', title: "Add an Image",
      desc: "Drag & drop or click to browse — JPG, PNG and GeoTIFF all work. We're adding a <strong>satellite chip</strong>; because it's a GeoTIFF, the platform also reads exactly where on Earth it belongs.",
      script: [ { sel: '[data-tour="upload-zone"]', run: async () => { await callTour("upload", "addSegFile", "/tour/chip_9216_57344.tif", "chip_9216_57344.tif"); }, wait: 1800 } ], hold: 3500 },

    { id: "run-seg", route: "/upload", target: '[data-tour="run-btn"]', title: "Run AI Segmentation",
      desc: "One click sends the image to the <strong>SegFormer-B5 model</strong> in the cloud. It returns a colour-coded map of buildings, roads, water and open land, with the percentage of each. <em>This takes about 15–40 seconds — the tour will wait for the result.</em>",
      script: [ { sel: '[data-tour="run-btn"]', click: true, wait: 1200 } ],
      waitFor: { sel: '[data-tour="plot-overlay"]', timeout: 90000 },
      postTarget: 'img[alt^="Mask"]', hold: 8000 },

    { id: "plot-overlay", route: "/upload", target: '[data-tour="plot-overlay"]', title: "Put the Result Back on the Map",
      desc: "Because the image is geo-referenced, <strong>Plot Detection Overlay on Map</strong> places the AI's mask at its true location on the Mapping page — with an opacity slider so you can compare it against the live imagery.",
      script: [ { sel: '[data-tour="plot-overlay"]', click: true, wait: 2200 } ],
      postTarget: '[data-tour="det-overlay"]', hold: 7000 },

    { id: "change-setup", route: "/upload", target: '[data-tour="atype-change"]', title: "Change Detection — Two Dates",
      desc: "To see <strong>what changed</strong>, switch to Change Detection and provide a <strong>PAST</strong> and a <strong>PRESENT</strong> image of the same area. Loading two chips of the same neighbourhood taken at different times.",
      script: [
          { sel: '[data-tour="atype-change"]', click: true, wait: 900 },
          { sel: '[data-tour="cd-uploads"]', run: async () => { await callTour("upload", "setChangeFiles", "/tour/chip_9216_57344.tif", "past.tif", "/tour/chip_13824_14848.tif", "present.tif"); }, wait: 2000 },
      ], hold: 4000 },

    { id: "run-change", route: "/upload", target: '[data-tour="run-btn"]', title: "Run Change Detection",
      desc: "The model maps both dates and compares them pixel by pixel: <strong>cyan</strong> = new construction, <strong>red</strong> = demolished, <strong>orange</strong> = new roads, <strong>purple</strong> = other land-use change — with the affected area of each. <em>Again ~20–40 seconds; waiting for the result.</em>",
      script: [ { sel: '[data-tour="run-btn"]', click: true, wait: 1200 } ],
      waitFor: { sel: 'img[alt="Change detection output"]', timeout: 90000 },
      postTarget: 'img[alt="Change detection output"]', hold: 9000 },

    // ── DATA LOGS ──
    { id: "datalogs", route: "/datalogs", target: '[data-tour="datalogs"]', title: "Data Logs — Your Audit Trail",
      desc: "Every job you run — segmentations, change detections, report generations — is recorded here as <strong>recent activity</strong>, with its district, date and status. Useful for reviewing what's been processed and when.", hold: 7000 },

    // ── DSS ──
    { id: "dss-config", route: "/dss", target: '[data-tour="dss-form"]', title: "Decision Support — Build a Report",
      desc: "The <strong>DSS</strong> turns analysis into official reports. Pick the <strong>district</strong>, set the <strong>reporting period</strong>, and tick the <strong>data types</strong> to include — watch each field being filled.",
      script: [
          { sel: '[data-tour="dss-district"] select', select: "Vijayawada", wait: 1300 },
          { sel: '[data-tour="dss-dates"] > div:nth-child(1) input', input: "2026-01-01", wait: 1000 },
          { sel: '[data-tour="dss-dates"] > div:nth-child(2) input', input: "2026-03-31", wait: 1000 },
          { sel: '[data-tour="dss-datatypes"] label', click: true, wait: 1100 },
      ], hold: 4500 },

    { id: "dss-generate", route: "/dss", target: '[data-tour="dss-generate"]', title: "Generate the Report",
      desc: "<strong>Generate Report</strong> compiles the live statistics into a summary — property counts, detected changes and governance insights — ready to export as a print-ready PDF for ULB officials.",
      script: [ { sel: '[data-tour="dss-generate"]', click: true, wait: 1500 } ],
      waitFor: { check: () => /Report Summary/i.test(document.body.textContent), timeout: 30000 },
      hold: 8000 },

    { id: "done", route: "/dss", target: '[data-tour="brand"]', title: "You've Seen the Full Workflow",
      desc: "Dashboard → live mapping & area analytics → AI segmentation & change detection → activity logs → official reports. <strong>Now it's yours to explore.</strong> Replay this walkthrough anytime from the <strong>Guided Tour</strong> button at the bottom-right.", hold: 9000 },
];

const CursorSvg = () => (
    <svg viewBox="0 0 24 24" fill="none"><path d="M5.65 2.92L19.08 12.03C19.56 12.35 19.36 13.1 18.79 13.13L12.48 13.46L9.8 19.23C9.56 19.75 8.8 19.67 8.68 19.12L5.05 3.61C4.94 3.12 5.28 2.67 5.65 2.92Z" fill="#2dd4bf" stroke="#0d9488" strokeWidth="0.8" /></svg>
);

function TourWelcome({ onStart, onDismiss }) {
    return (
        <div className="tour-welcome">
            <div className="tour-welcome-card">
                <div className="tour-welcome-icon"><Compass size={26} /></div>
                <div className="tour-welcome-title">See the Platform Drive Itself</div>
                <div className="tour-welcome-subtitle">A guided walkthrough will operate Smart Property Identification for you — selecting districts, switching layers, drawing areas, running the AI and generating a report — while explaining every feature along the way. About 4 minutes.</div>
                <div className="tour-welcome-features">
                    <div className="tour-welcome-feature"><BarChart2 size={13} /> State Dashboard</div>
                    <div className="tour-welcome-feature"><MapPin size={13} /> Live Mapping & AOI</div>
                    <div className="tour-welcome-feature"><Layers size={13} /> AI Segmentation</div>
                    <div className="tour-welcome-feature"><UploadIcon size={13} /> Change Detection</div>
                    <div className="tour-welcome-feature"><Database size={13} /> Activity Logs</div>
                    <div className="tour-welcome-feature"><Compass size={13} /> DSS Reports</div>
                </div>
                <div className="tour-welcome-actions">
                    <button className="tour-btn-dismiss" onClick={onDismiss}>Explore on my own</button>
                    <button className="tour-btn-start" onClick={onStart}><Play size={15} /> Start the Tour</button>
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
    const [working, setWorking] = useState(false);       // long AI wait in progress
    const timer = useRef(null);
    const siRef = useRef(0); siRef.current = si;
    const pausedRef = useRef(false); pausedRef.current = paused;
    const locRef = useRef(location.pathname); locRef.current = location.pathname;
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
        setPhase("idle"); setSi(0); setPaused(false); setTtVis(false); setSr(null); setWorking(false);
    }, []);

    // Tooltip placement: prefer beside the spotlight (right → left → below → above),
    // NEVER overlapping the highlighted element.
    const posTT = useCallback((rect) => {
        const tw = 410, th = 250, m = 14, vw = window.innerWidth, vh = window.innerHeight;
        if (!rect) { setTp({ x: vw - tw - m, y: vh - th - m }); return; }
        const clampY = (y) => Math.min(Math.max(y, m), vh - th - m);
        const clampX = (x) => Math.min(Math.max(x, m), vw - tw - m);
        const candidates = [
            { x: rect.left + rect.width + m, y: clampY(rect.top) },                    // right
            { x: rect.left - tw - m, y: clampY(rect.top) },                            // left
            { x: clampX(rect.left), y: rect.top + rect.height + m },                   // below
            { x: clampX(rect.left), y: rect.top - th - m },                            // above
        ];
        for (const c of candidates) {
            if (c.x < m || c.x + tw > vw - m || c.y < m || c.y + th > vh - m) continue;
            const overlaps = !(c.x + tw < rect.left || c.x > rect.left + rect.width || c.y + th < rect.top || c.y > rect.top + rect.height);
            if (!overlaps) { setTp(c); return; }
        }
        setTp({ x: vw - tw - m, y: vh - th - m }); // safe corner fallback
    }, []);

    // Spotlight an element + glide the cursor to it. Returns its rect.
    const focus = useCallback(async (sel, { moveCursor = true, pad = 8 } = {}) => {
        const el = await waitForEl(sel, 4000);
        if (!el) { setSr(null); posTT(null); return null; }
        const r0 = el.getBoundingClientRect();
        if (r0.top < 60 || r0.bottom > window.innerHeight - 20) {
            el.scrollIntoView({ behavior: "smooth", block: "center" });
            await sleep(650);
        }
        const r = el.getBoundingClientRect();
        const s = { top: r.top - pad, left: r.left - pad, width: r.width + pad * 2, height: r.height + pad * 2 };
        setSr(s); posTT(s);
        if (moveCursor) { setCur({ x: r.left + r.width / 2, y: r.top + r.height / 2 }); await sleep(760); } // glide time
        return r;
    }, [posTT]);

    // Press animation + click ripple at the current cursor target.
    const press = useCallback(async (r) => {
        if (r) setRipple({ x: r.left + r.width / 2, y: r.top + r.height / 2, id: Date.now() });
        setClicking(true); await sleep(230); setClicking(false); await sleep(120);
    }, []);

    // Navigate like a person: click the real navbar link.
    const goRoute = useCallback(async (route) => {
        if (locRef.current === route) return;
        const link = document.querySelector(`nav a[href="${route}"], .app-layout a[href="${route}"], a[href="${route}"]`);
        if (link) {
            const r = await focus(`a[href="${route}"]`, { pad: 6 });
            await press(r);
            link.click();
        } else {
            navigate(route);
        }
        await sleep(1000);
    }, [focus, press, navigate]);

    const goNext = useCallback(() => {
        if (timer.current) clearTimeout(timer.current);
        const n = siRef.current + 1;
        if (n >= STEPS.length) { exit(); return; }
        setSi(n);
    }, [exit]);

    // Execute one step: route → spotlight + copy → scripted sub-actions → completion wait → hold.
    useEffect(() => {
        if (phase !== "running") return;
        let cancelled = false;
        const s = STEPS[si]; if (!s) { exit(); return; }
        setTtVis(false); setWorking(false);
        (async () => {
            if (s.route) await goRoute(s.route);
            if (cancelled) return;
            await focus(s.target);
            setTtVis(true);
            await sleep(2100);                        // let the copy be read before acting
            if (cancelled) return;

            for (const item of (s.script || [])) {
                if (cancelled) return;
                let r = null;
                if (item.sel) r = await focus(item.sel, { pad: 6 });
                if (cancelled) return;
                if (!item.silent && (item.click || item.select !== undefined || item.input !== undefined || item.run)) await press(r);
                const el = item.sel ? document.querySelector(item.sel) : null;
                try {
                    if (item.click && el) el.click();
                    if (item.select !== undefined && el) setNativeValue(el, item.select);
                    if (item.input !== undefined && el) setNativeValue(el, item.input);
                    if (item.run) await item.run();
                } catch (e) { console.warn("tour sub-action failed:", e); }
                if (item.silent && item.sel) el?.scrollIntoView({ behavior: "smooth", block: "center" });
                if (item.wait) await sleep(item.wait);
            }
            if (cancelled) return;

            if (s.waitFor) {                          // wait for the real task to finish
                setWorking(true);
                if (s.waitFor.sel) await waitForEl(s.waitFor.sel, s.waitFor.timeout || 60000);
                else if (s.waitFor.check) await waitUntil(s.waitFor.check, s.waitFor.timeout || 60000);
                setWorking(false);
            }
            if (cancelled) return;

            await focus(s.postTarget || s.target);    // re-anchor on the outcome
            if (!pausedRef.current) timer.current = setTimeout(() => { if (siRef.current === si) goNext(); }, s.hold || 5000);
        })();
        return () => { cancelled = true; if (timer.current) clearTimeout(timer.current); };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [si, phase]);

    const togglePause = () => {
        setPaused((p) => {
            const np = !p;
            if (np) { if (timer.current) clearTimeout(timer.current); }
            else { const s = STEPS[siRef.current]; timer.current = setTimeout(() => goNext(), (s?.hold || 5000) / 2); }
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
                                {working && (
                                    <div className="tour-working"><span className="tour-working-spinner" /> Working — waiting for this to finish…</div>
                                )}
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
