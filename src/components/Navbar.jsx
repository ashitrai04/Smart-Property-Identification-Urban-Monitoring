import React, { useState, useEffect } from "react";
import { Link, useLocation } from "react-router-dom";
import { motion } from "framer-motion";
import { Home, Globe, Upload, Database, BarChart2, Clock } from "lucide-react";

const NAV_ITEMS = [
    { path: "/", label: "Home", icon: Home },
    { path: "/mapping", label: "Mapping", icon: Globe },
    { path: "/upload", label: "Upload & Analysis", icon: Upload },
    { path: "/datalogs", label: "Data Logs", icon: Database },
    { path: "/dss", label: "DSS", icon: BarChart2 },
];

export default function Navbar() {
    const [open, setOpen] = useState(false);
    const location = useLocation();
    const [time, setTime] = useState(new Date());

    useEffect(() => {
        const timer = setInterval(() => setTime(new Date()), 1000);
        return () => clearInterval(timer);
    }, []);

    const isActive = (path) => {
        if (path === "/") return location.pathname === "/";
        return location.pathname.startsWith(path);
    };

    return (
        <nav className="navbar" aria-label="Primary" style={{ 
            display: 'flex', alignItems: 'center', height: '56px', padding: '0 16px',
            background: 'linear-gradient(180deg, rgba(15, 23, 42, 0.98) 0%, rgba(10, 14, 23, 0.98) 100%)',
            borderBottom: '1px solid var(--border-default)', backdropFilter: 'blur(20px)', zIndex: 100, flexShrink: 0
        }}>
            {/* Left: Brand Logo & Title */}
            <div className="navbar-brand" data-tour="brand" style={{ display: 'flex', alignItems: 'center', gap: '10px', marginRight: 'auto' }}>
                <div className="brand-icon logo-frame" style={{ 
                    width: '38px', height: '38px', flexShrink: 0, background: 'rgba(2, 6, 23, 0.9)', 
                    border: '1px solid rgba(20, 184, 166, 0.35)', borderRadius: '8px', display: 'flex', 
                    alignItems: 'center', justifyContent: 'center', overflow: 'hidden', position: 'relative' 
                }}>
                    <img src="/yi.png" alt="YI" style={{ width: '100%', height: '100%', objectFit: 'contain' }} onError={(e) => { e.target.style.display='none'; }} />
                    <span style={{ position: 'absolute', color: 'white', fontWeight: 800, fontSize: '14px', zIndex: -1 }}>Yi</span>
                </div>
                <div className="brand-text-container" style={{ display: 'flex', flexDirection: 'column', justifyContent: 'center' }}>
                    <span style={{ fontSize: '14px', fontWeight: 700, letterSpacing: '0.5px', color: 'var(--text-primary)', lineHeight: '1.2' }}>SMART PROPERTY IDENTIFICATION</span>
                    <span style={{ fontSize: '10px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>AI-Powered Urban Monitoring Platform</span>
                </div>
            </div>

            {/* Center: Navigation Tabs */}
            <div className={`navbar-tabs${open ? " open" : ""}`} style={{ position: 'absolute', left: '50%', transform: 'translateX(-50%)', display: 'flex', gap: '8px' }}>
                {NAV_ITEMS.map((item) => {
                    const Icon = item.icon;
                    const active = isActive(item.path);
                    return (
                        <Link
                            key={item.path}
                            to={item.path}
                            className={`nav-tab${active ? " active" : ""}`}
                            onClick={() => setOpen(false)}
                            style={{ 
                                position: 'relative', display: 'flex', alignItems: 'center', gap: '6px', 
                                padding: '6px 14px', borderRadius: '6px', textDecoration: 'none',
                                color: active ? 'white' : 'var(--text-secondary)', fontSize: '12px',
                                fontWeight: 500, transition: 'color 0.2s', zIndex: 1
                            }}
                        >
                            {active && (
                                <motion.div
                                    className="tab-active-pill"
                                    layoutId="activeTabPill"
                                    transition={{ type: "spring", stiffness: 380, damping: 30 }}
                                    style={{
                                        position: 'absolute', inset: 0, background: 'rgba(20, 184, 166, 0.15)',
                                        border: '1px solid rgba(20, 184, 166, 0.4)', borderRadius: '6px', zIndex: -1
                                    }}
                                />
                            )}
                            <Icon size={14} style={{ opacity: active ? 1 : 0.7 }} />
                            <span>{item.label}</span>
                        </Link>
                    );
                })}
            </div>

            {/* Right: Status & Time */}
            <div className="navbar-right" style={{ display: 'flex', alignItems: 'center', gap: '16px', marginLeft: 'auto' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px', background: 'rgba(34, 197, 94, 0.1)', border: '1px solid rgba(34, 197, 94, 0.2)', padding: '4px 10px', borderRadius: '20px' }}>
                    <span style={{ width: '6px', height: '6px', borderRadius: '50%', background: '#22c55e', boxShadow: '0 0 8px #22c55e' }} />
                    <span style={{ fontSize: '10px', fontWeight: 600, color: '#22c55e', textTransform: 'uppercase', letterSpacing: '0.5px' }}>Online</span>
                </div>
                
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px', color: 'var(--text-muted)', fontSize: '11px', fontFamily: 'var(--font-mono)' }}>
                    <Clock size={12} />
                    <span>{time.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false })} IST</span>
                </div>
            </div>

            {/* Mobile Toggle */}
            <button
                className="mobile-menu-btn"
                aria-label="Toggle menu"
                onClick={() => setOpen((v) => !v)}
                style={{ display: 'none' }}
            >
                <span className="menu-line" />
                <span className="menu-line" />
                <span className="menu-line" />
            </button>
        </nav>
    );
}
