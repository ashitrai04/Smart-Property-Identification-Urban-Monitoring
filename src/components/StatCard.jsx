import React from "react";

export default function StatCard({ icon, label, value, trend, color = "#14b8a6" }) {
    return (
        <div className="stat-card">
            <div className="stat-card-icon">{icon}</div>
            <div style={{ flex: 1, minWidth: 0 }}>
                <div className="stat-card-label">{label}</div>
                <div className="stat-card-value" style={{ color }}>{value}</div>
                {trend !== undefined && trend !== 0 && (
                    <div className={`stat-card-trend ${trend > 0 ? "up" : "down"}`}>
                        {trend > 0 ? "▲" : "▼"} {Math.abs(trend)}%
                    </div>
                )}
            </div>
        </div>
    );
}
