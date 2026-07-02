import React, { useState, useCallback } from "react";
import { Routes, Route, useLocation } from "react-router-dom";
import { AnimatePresence } from "framer-motion";
import BootOverlay from "./components/BootOverlay";
import Navbar from "./components/Navbar";
import PlatformTour from "./components/PlatformTour";
import Home from "./pages/Home";
import Mapping from "./pages/Mapping";
import Upload from "./pages/Upload";
import DataLogs from "./pages/DataLogs";
import DSS from "./pages/DSS";

function App() {
    const location = useLocation();
    const isFullWidth = location.pathname.startsWith("/mapping") || location.pathname.startsWith("/dss");
    const [booted, setBooted] = useState(false);
    const handleBootComplete = useCallback(() => setBooted(true), []);

    return (
        <>
            <AnimatePresence>
                {!booted && <BootOverlay onComplete={handleBootComplete} />}
            </AnimatePresence>

            <div className="app-layout">
                {/* Primary navigation (Dronacharya style) */}
                <Navbar />

                {/* Main content */}
                {isFullWidth ? (
                    <main id="main" className="app-main-fullscreen">
                        <Routes>
                            <Route path="/mapping" element={<Mapping />} />
                            <Route path="/dss" element={<DSS />} />
                        </Routes>
                    </main>
                ) : (
                    <main id="main" className="app-main-content">
                        <section className="content-container">
                            <Routes>
                                <Route path="/" element={<Home />} />
                                <Route path="/upload" element={<Upload />} />
                                <Route path="/datalogs" element={<DataLogs />} />
                            </Routes>
                        </section>
                    </main>
                )}

                {/* Global guided tour (launch button + auto-playing walkthrough) */}
                {booted && <PlatformTour />}
            </div>
        </>
    );
}

export default App;
