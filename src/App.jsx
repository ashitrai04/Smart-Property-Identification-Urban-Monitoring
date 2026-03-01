import React from "react";
import { Routes, Route, useLocation } from "react-router-dom";
import TopStrip from "./components/TopStrip";
import BrandingHeader from "./components/BrandingHeader";
import Navbar from "./components/Navbar";
import Footer from "./components/Footer";
import Home from "./pages/Home";
import Mapping from "./pages/Mapping";
import Upload from "./pages/Upload";
import DataLogs from "./pages/DataLogs";
import DSS from "./pages/DSS";

function App() {
    const location = useLocation();
    const isFullWidth = location.pathname.startsWith("/mapping") || location.pathname.startsWith("/dss");

    return (
        <div className="min-h-dvh flex flex-col bg-gray-50">
            {/* Utility strip */}
            <TopStrip />

            {/* Moving disclaimer banner */}
            <div className="w-full bg-red-600 text-white">
                <div className="relative overflow-hidden">
                    <div className="app-home-marquee py-2">
                        <span className="px-4">
                            This website does not belong to any government organization. Smart Property Identification  by Yantrikaran Innovations Pvt. Ltd.
                        </span>
                    </div>
                </div>
            </div>

            {/* Ministry branding header */}
            <BrandingHeader />

            {/* Primary navigation */}
            <Navbar />

            {/* Main content */}
            {isFullWidth ? (
                <main id="main" className="flex-1" style={{ minHeight: "calc(100dvh - 260px)" }}>
                    <Routes>
                        <Route path="/mapping" element={<Mapping />} />
                        <Route path="/dss" element={<DSS />} />
                    </Routes>
                </main>
            ) : (
                <main id="main" className="flex-1">
                    <section className="max-w-7xl mx-auto px-3 sm:px-4 py-6 sm:py-8 w-full">
                        <Routes>
                            <Route path="/" element={<Home />} />
                            <Route path="/upload" element={<Upload />} />
                            <Route path="/datalogs" element={<DataLogs />} />
                        </Routes>
                    </section>
                </main>
            )}

            {/* Footer */}
            {!isFullWidth && <Footer />}

            {/* Bottom disclaimer */}
            <div className="w-full bg-red-600 text-white mt-0">
                <div className="relative overflow-hidden">
                    <div className="app-home-marquee py-2">
                        <span className="px-4">
                            This website does not belong to any government organization. Smart Property Identification by Yantrikaran Innovations Pvt. Ltd.
                        </span>
                    </div>
                </div>
            </div>
        </div>
    );
}

export default App;
