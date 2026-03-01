import React from "react";

export default function StatCard({ icon, label, value, trend, color = "#0B5FA5" }) {
    return (
        <div className="bg-white rounded-lg border border-gray-200 p-4 flex items-start gap-3 shadow-sm hover:shadow transition-shadow">
            <div
                className="w-10 h-10 rounded-lg flex items-center justify-center shrink-0 text-lg"
                style={{ background: `${color}20`, color }}
            >
                {icon}
            </div>
            <div className="flex-1 min-w-0">
                <p className="text-[11px] text-gray-500 truncate">{label}</p>
                <p className="text-lg font-bold text-gray-900 leading-tight">{value}</p>
                {trend !== undefined && trend !== 0 && (
                    <p className={`text-[11px] mt-0.5 font-medium ${trend > 0 ? "text-green-600" : "text-red-500"}`}>
                        {trend > 0 ? "▲" : "▼"} {Math.abs(trend)}% from last month
                    </p>
                )}
            </div>
        </div>
    );
}
