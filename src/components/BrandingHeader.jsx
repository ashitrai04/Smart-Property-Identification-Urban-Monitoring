import React from "react";

export default function BrandingHeader() {
    return (
        <header className="bg-white border-b">
            <div className="max-w-7xl mx-auto px-3 sm:px-4 py-3 md:py-4 flex items-center justify-between gap-4">
                <div className="flex items-center gap-3 sm:gap-4">
                    {/* Yi Logo — mimicking gov emblem style */}
                    <div className="h-16 w-16 sm:h-20 sm:w-20 md:h-24 md:w-24 rounded-xl bg-[#1a1a40] flex items-center justify-center shrink-0">
                        <span className="text-white font-extrabold text-2xl sm:text-3xl md:text-4xl" style={{ fontFamily: 'Georgia, serif' }}>Yi</span>
                    </div>
                    <div>
                        <h1 className="text-sm sm:text-base md:text-lg font-bold text-gray-900 leading-tight">
                            Yantrikaran Innovations Pvt. Ltd.
                        </h1>
                        <p className="text-[11px] sm:text-xs md:text-sm text-gray-500 leading-tight mt-0.5">
                            Smart Property Identification & Urban Monitoring
                        </p>
                        <p className="text-[10px] sm:text-[11px] text-gray-400 leading-tight">
                            Municipal Administration & Urban Development Department — Andhra Pradesh
                        </p>
                    </div>
                </div>
                <div className="flex items-center gap-3 sm:gap-4">
                    {/* AP Emblem placeholder */}
                    <div className="hidden sm:flex flex-col items-center gap-1">
                        <div className="h-14 w-14 md:h-18 md:w-18 rounded-full bg-gradient-to-br from-blue-600 to-blue-800 flex items-center justify-center text-white font-bold text-lg md:text-xl shadow">
                            AP
                        </div>
                        <span className="text-[9px] text-gray-400 font-medium">ANDHRA PRADESH</span>
                    </div>
                </div>
            </div>
        </header>
    );
}
