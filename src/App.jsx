import React, { useState, useCallback, Suspense, lazy } from "react";
import { Routes, Route, useLocation } from "react-router-dom";
import { AnimatePresence } from "framer-motion";
import BootOverlay from "./components/BootOverlay";
import CommandBar from "./components/CommandBar";







// The boot check plays once a session; reloads go straight to work.
// Routes load on demand. Mapping alone pulls mapbox-gl, geotiff, geopackage and
// shapefile — about 3.8 MB of the bundle — and every page was waiting on it
// before anything could render, which is why the first screen took so long.
const PlatformTour = lazy(() => import("./components/PlatformTour"));
const Home = lazy(() => import("./pages/Home"));
const Mapping = lazy(() => import("./pages/Mapping"));
const Upload = lazy(() => import("./pages/Upload"));
const DataLogs = lazy(() => import("./pages/DataLogs"));
const DSS = lazy(() => import("./pages/DSS"));
const NSV = lazy(() => import("./pages/NSV"));

function RouteFallback() {
    return (
        <div style={{
            display: "flex", alignItems: "center", justifyContent: "center",
            height: "60vh", color: "var(--text-dim)", fontSize: 13,
            fontFamily: "var(--font-mono)", letterSpacing: 1,
        }}>loading workspace…</div>
    );
}

const BOOT_KEY = "sp-boot-played";
function bootSeen() {
    try { return !!sessionStorage.getItem(BOOT_KEY); } catch { return false; }
}

function App() {
    const location = useLocation();
    const isFullWidth = location.pathname.startsWith("/mapping") || location.pathname.startsWith("/dss") || location.pathname.startsWith("/nsv");
    const [booted, setBooted] = useState(bootSeen);
    const handleBootComplete = useCallback(() => {
        try { sessionStorage.setItem(BOOT_KEY, "1"); } catch { /* ignore */ }
        setBooted(true);
    }, []);

    return (
        <>
            <AnimatePresence>
                {!booted && <BootOverlay onComplete={handleBootComplete} />}
            </AnimatePresence>

            <div className="app-layout">
                <CommandBar />

                {/* Keyed by route so each workspace eases in on arrival. */}
                {isFullWidth ? (
                    <main id="main" key={location.pathname} className="app-main-fullscreen anim-fade-up">
                        <Suspense fallback={<RouteFallback />}>
                        <Routes>
                            <Route path="/mapping" element={<Mapping />} />
                            <Route path="/dss" element={<DSS />} />
                            <Route path="/nsv" element={<NSV />} />
                        </Routes>
                        </Suspense>
                    </main>
                ) : (
                    <main id="main" key={location.pathname} className="app-main-content">
                        <section className="content-container anim-fade-up">
                            <Suspense fallback={<RouteFallback />}>
                            <Routes>
                                <Route path="/" element={<Home />} />
                                <Route path="/upload" element={<Upload />} />
                                <Route path="/datalogs" element={<DataLogs />} />
                            </Routes>
                            </Suspense>
                        </section>
                    </main>
                )}

                {/* Guided walkthrough — launched from the command bar's Guide button */}
                {booted && <Suspense fallback={null}><PlatformTour /></Suspense>}
            </div>
        </>
    );
}

export default App;
