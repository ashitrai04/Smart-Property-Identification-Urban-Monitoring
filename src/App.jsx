import React, { useState, useCallback } from "react";
import { Routes, Route, useLocation } from "react-router-dom";
import { AnimatePresence } from "framer-motion";
import BootOverlay from "./components/BootOverlay";
import CommandBar from "./components/CommandBar";
import PlatformTour from "./components/PlatformTour";
import Home from "./pages/Home";
import Mapping from "./pages/Mapping";
import Upload from "./pages/Upload";
import DataLogs from "./pages/DataLogs";
import DSS from "./pages/DSS";

// The boot check plays once a session; reloads go straight to work.
const BOOT_KEY = "sp-boot-played";
function bootSeen() {
    try { return !!sessionStorage.getItem(BOOT_KEY); } catch { return false; }
}

function App() {
    const location = useLocation();
    const isFullWidth = location.pathname.startsWith("/mapping") || location.pathname.startsWith("/dss");
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
                        <Routes>
                            <Route path="/mapping" element={<Mapping />} />
                            <Route path="/dss" element={<DSS />} />
                        </Routes>
                    </main>
                ) : (
                    <main id="main" key={location.pathname} className="app-main-content">
                        <section className="content-container anim-fade-up">
                            <Routes>
                                <Route path="/" element={<Home />} />
                                <Route path="/upload" element={<Upload />} />
                                <Route path="/datalogs" element={<DataLogs />} />
                            </Routes>
                        </section>
                    </main>
                )}

                {/* Guided walkthrough — launched from the command bar's Guide button */}
                {booted && <PlatformTour />}
            </div>
        </>
    );
}

export default App;
