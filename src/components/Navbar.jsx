import React, { useState } from "react";
import { Link, useLocation } from "react-router-dom";

const NAV_ITEMS = [
    { path: "/", label: "Home" },
    { path: "/mapping", label: "Mapping" },
    { path: "/upload", label: "Upload & Analysis" },
    { path: "/datalogs", label: "Data Logs" },
    { path: "/dss", label: "DSS" },
];

export default function Navbar() {
    const [open, setOpen] = useState(false);
    const location = useLocation();

    const isActive = (path) => {
        if (path === "/") return location.pathname === "/";
        return location.pathname.startsWith(path);
    };

    const navItemClass = (path) =>
        `px-3 py-2 text-sm text-white/90 hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-white/60 rounded ${isActive(path) ? "bg-white/20 text-white font-semibold" : ""
        }`;

    return (
        <nav className="bg-[#0B5FA5] text-white" aria-label="Primary">
            <div className="max-w-7xl mx-auto px-3 sm:px-4">
                <div className="flex items-center justify-between h-12">
                    <button
                        className="sm:hidden p-2 -ml-2"
                        aria-label="Toggle menu"
                        onClick={() => setOpen((v) => !v)}
                    >
                        <span className="block w-6 h-0.5 bg-white mb-1"></span>
                        <span className="block w-6 h-0.5 bg-white mb-1"></span>
                        <span className="block w-6 h-0.5 bg-white"></span>
                    </button>

                    <ul className="hidden sm:flex items-center gap-2">
                        {NAV_ITEMS.map((item) => (
                            <li key={item.path}>
                                <Link to={item.path} className={navItemClass(item.path)}>
                                    {item.label}
                                </Link>
                            </li>
                        ))}
                    </ul>
                </div>

                {open && (
                    <div className="sm:hidden pb-2">
                        {NAV_ITEMS.map((item) => (
                            <Link
                                key={item.path}
                                to={item.path}
                                className={`block py-2 text-sm ${isActive(item.path) ? "font-semibold" : "text-white/80"}`}
                                onClick={() => setOpen(false)}
                            >
                                {item.label}
                            </Link>
                        ))}
                    </div>
                )}
            </div>
        </nav>
    );
}
