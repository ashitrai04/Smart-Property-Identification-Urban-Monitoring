import React from "react";

// A headline number. The colour is the stat's own (a hairline on top and the
// icon), never the value text — the number stays neutral and readable.
// `icon` may be a component (lucide), an element, or a string.
export default function StatCard({ icon: Icon, label, value, trend, color = "var(--signal)" }) {
    const glyph = React.isValidElement(Icon) || typeof Icon === "string" ? Icon : Icon ? <Icon size={16} /> : null;
    return (
        <div className="stat-card" style={{ "--stat-colour": color }}>
            {glyph && <div className="stat-card-icon" style={{ color }}>{glyph}</div>}
            <div style={{ flex: 1, minWidth: 0 }}>
                <div className="stat-card-label">{label}</div>
                <div className="stat-card-value">{value}</div>
                {trend !== undefined && trend !== 0 && (
                    <div className={`stat-card-trend ${trend > 0 ? "up" : "down"}`}>
                        {trend > 0 ? "▲" : "▼"} {Math.abs(trend)}%
                    </div>
                )}
            </div>
        </div>
    );
}
