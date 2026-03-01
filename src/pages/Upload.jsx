import React, { useState, useRef } from "react";

const SEGMENTATION_API = "https://amrender-unified-cartographer-api.hf.space/predict";
const MAX_FILE_SIZE = 4.5 * 1024 * 1024; // 4.5 MB

const ANALYSIS_TYPES = [
    { id: "segment", label: "AI Segmentation", desc: "Run AI segmentation on satellite/drone imagery (max 4.5 MB per image)" },
    { id: "boundary", label: "Boundary Analysis", desc: "Upload a shapefile boundary for land use analysis" },
    { id: "mask", label: "Mask Overlay", desc: "Upload a mask TIFF for change detection comparison" },
    { id: "change", label: "Change Detection", desc: "Compare uploaded data with existing satellite imagery" },
];

const MASK_LEGEND = [
    { label: "Buildings", color: "#EF4444" },
    { label: "Roads", color: "#EAB308" },
    { label: "Water Bodies", color: "#3B82F6" },
    { label: "Open Plots / Barren", color: "#9CA3AF" },
];

const ACCEPTED = ".shp,.shx,.dbf,.prj,.tif,.tiff,.geojson,.json,.zip,.jpg,.jpeg,.png";

export default function Upload() {
    const [files, setFiles] = useState([]);
    const [analysisType, setAnalysisType] = useState("segment");
    const [processing, setProcessing] = useState(false);
    const [progress, setProgress] = useState("");
    const [error, setError] = useState(null);
    // Each result: { inputName, inputUrl, maskUrl, status, error? }
    const [segResults, setSegResults] = useState([]);
    const [dummyResult, setDummyResult] = useState(null);
    const abortRef = useRef(false);

    const handleDrop = (e) => {
        e.preventDefault();
        e.stopPropagation();
        setFiles(prev => [...prev, ...Array.from(e.dataTransfer.files)]);
    };

    const handleFileInput = (e) => {
        setFiles(prev => [...prev, ...Array.from(e.target.files)]);
    };

    const removeFile = (idx) => { setFiles(prev => prev.filter((_, i) => i !== idx)); };

    // ── Send one image to the API ──
    const segmentOneFile = async (file) => {
        const formData = new FormData();
        formData.append("file", file);
        const resp = await fetch(SEGMENTATION_API, { method: "POST", body: formData });
        if (!resp.ok) {
            const errText = await resp.text().catch(() => "Unknown error");
            throw new Error(`API ${resp.status}: ${errText}`);
        }
        const blob = await resp.blob();
        return URL.createObjectURL(blob);
    };

    // ── Process multiple images sequentially ──
    const runSegmentation = async () => {
        const imageFiles = files.filter(f => /\.(jpg|jpeg|png|tif|tiff)$/i.test(f.name));
        if (imageFiles.length === 0) {
            setError("Please upload at least one image (.jpg, .png, or .tif) for segmentation.");
            return;
        }

        const oversized = imageFiles.filter(f => f.size > MAX_FILE_SIZE);
        if (oversized.length > 0) {
            setError(`${oversized.length} file(s) exceed 4.5 MB limit: ${oversized.map(f => f.name).join(", ")}`);
            return;
        }

        setSegResults([]);
        abortRef.current = false;

        for (let i = 0; i < imageFiles.length; i++) {
            if (abortRef.current) break;
            const file = imageFiles[i];
            setProgress(`Processing ${i + 1} of ${imageFiles.length}: ${file.name}...`);

            const inputUrl = URL.createObjectURL(file);
            // Add a "loading" placeholder
            setSegResults(prev => [...prev, { inputName: file.name, inputUrl, maskUrl: null, status: "processing" }]);

            try {
                const maskUrl = await segmentOneFile(file);
                // Update last entry with mask
                setSegResults(prev => prev.map((r, idx) =>
                    idx === prev.length - 1 ? { ...r, maskUrl, status: "done" } : r
                ));
            } catch (err) {
                setSegResults(prev => prev.map((r, idx) =>
                    idx === prev.length - 1 ? { ...r, status: "error", error: err.message } : r
                ));
            }
        }
        setProgress("");
    };

    // ── Dummy analysis ──
    const runDummyAnalysis = async () => {
        setProgress("Running analysis pipeline...");
        await new Promise(r => setTimeout(r, 3000));
        setDummyResult({
            status: "success",
            summary: "Analysis complete. 2,847 features detected across the uploaded boundary.",
            details: {
                "Total Features": "2,847", "Buildings Detected": "1,923",
                "Open Plots": "524", "Water Bodies": "12",
                "Road Segments": "388", "Processing Time": "4.2s",
                "Model": "YOLOv8-Seg (EC2)", "Confidence": "94.3%",
            },
        });
        setProgress("");
    };

    const handleSubmit = async () => {
        if (files.length === 0) return;
        setProcessing(true);
        setError(null);
        setDummyResult(null);

        if (analysisType === "segment") {
            await runSegmentation();
        } else {
            await runDummyAnalysis();
        }
        setProcessing(false);
    };

    const clearResults = () => {
        segResults.forEach(r => { if (r.inputUrl) URL.revokeObjectURL(r.inputUrl); if (r.maskUrl) URL.revokeObjectURL(r.maskUrl); });
        setSegResults([]);
        setDummyResult(null);
        setError(null);
    };

    const completedCount = segResults.filter(r => r.status === "done").length;
    const totalImages = files.filter(f => /\.(jpg|jpeg|png|tif|tiff)$/i.test(f.name)).length;

    return (
        <div className="space-y-6">
            <div className="bg-white rounded-lg border border-gray-200 p-4 sm:p-5 shadow-sm">
                <h2 className="text-lg sm:text-xl font-bold text-gray-900">Upload & Analysis</h2>
                <p className="text-sm text-gray-500 mt-1">
                    Upload satellite/drone images for AI segmentation or boundary files for analysis
                </p>
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
                {/* Upload zone + results */}
                <div className="lg:col-span-2 space-y-4">
                    <div
                        onDrop={handleDrop}
                        onDragOver={e => { e.preventDefault(); e.stopPropagation(); }}
                        className="bg-white border-2 border-dashed border-gray-300 rounded-lg p-8 text-center hover:border-[#0B5FA5] hover:bg-blue-50/30 transition-colors cursor-pointer"
                        onClick={() => document.getElementById("file-input").click()}
                    >
                        <div className="text-4xl mb-3">📁</div>
                        <p className="text-sm font-medium text-gray-900">Drag & drop files here</p>
                        <p className="text-xs text-gray-400 mt-1">or click to browse • multiple files supported</p>
                        <p className="text-[10px] text-gray-400 mt-2">Accepts: .jpg, .png, .tif, .tiff, .shp, .shx, .dbf, .prj, .geojson, .zip</p>
                        {analysisType === "segment" && (
                            <p className="text-[10px] text-[#0B5FA5] mt-1 font-medium">🧠 Upload multiple satellite tiles — each will be processed sequentially (max 4.5 MB each)</p>
                        )}
                        <input id="file-input" type="file" multiple accept={ACCEPTED} onChange={handleFileInput} className="hidden" />
                    </div>

                    {/* File list */}
                    {files.length > 0 && (
                        <div className="bg-white rounded-lg border border-gray-200 p-4 shadow-sm">
                            <div className="flex items-center justify-between mb-3">
                                <h3 className="text-sm font-semibold text-gray-900">Selected Files ({files.length})</h3>
                                <button onClick={() => setFiles([])} className="text-[10px] text-red-400 hover:text-red-600">Clear All</button>
                            </div>
                            <div className="space-y-2 max-h-40 overflow-y-auto">
                                {files.map((f, i) => (
                                    <div key={i} className="flex items-center justify-between bg-gray-50 rounded-lg px-3 py-2">
                                        <div className="flex items-center gap-2 min-w-0">
                                            <span className="text-sm">{/\.(jpg|jpeg|png)$/i.test(f.name) ? "🖼️" : "📄"}</span>
                                            <span className="text-xs font-medium text-gray-900 truncate">{f.name}</span>
                                            <span className="text-[10px] text-gray-400">{(f.size / 1024).toFixed(1)} KB</span>
                                            {f.size > MAX_FILE_SIZE && <span className="text-[10px] text-red-500 font-medium">⚠ Too large</span>}
                                        </div>
                                        <button onClick={() => removeFile(i)} className="text-red-400 hover:text-red-600 text-xs">✕</button>
                                    </div>
                                ))}
                            </div>
                        </div>
                    )}

                    {/* Error */}
                    {error && (
                        <div className="bg-red-50 border border-red-200 rounded-lg p-4 shadow-sm">
                            <div className="flex items-center gap-2 mb-1">
                                <span className="text-lg">❌</span>
                                <h3 className="text-sm font-semibold text-red-700">Error</h3>
                            </div>
                            <p className="text-xs text-red-600">{error}</p>
                        </div>
                    )}

                    {/* Dummy result for non-segment */}
                    {dummyResult && (
                        <div className="bg-white rounded-lg border border-green-200 p-4 shadow-sm">
                            <div className="flex items-center gap-2 mb-3">
                                <span className="text-lg">✅</span>
                                <h3 className="text-sm font-semibold text-green-700">Analysis Complete</h3>
                            </div>
                            <p className="text-xs text-gray-600 mb-3">{dummyResult.summary}</p>
                            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                                {Object.entries(dummyResult.details).map(([k, v]) => (
                                    <div key={k} className="bg-gray-50 rounded-lg p-2">
                                        <p className="text-[10px] text-gray-400">{k}</p>
                                        <p className="text-sm font-bold text-gray-900">{v}</p>
                                    </div>
                                ))}
                            </div>
                        </div>
                    )}

                    {/* ── Segmentation Results ── */}
                    {segResults.length > 0 && (
                        <div className="space-y-4">
                            <div className="flex items-center justify-between">
                                <div className="flex items-center gap-2">
                                    <h3 className="text-sm font-semibold text-gray-900">🧠 Segmentation Results</h3>
                                    <span className="text-[10px] bg-[#0B5FA5] text-white px-2 py-0.5 rounded-full">
                                        {completedCount}/{totalImages} done
                                    </span>
                                </div>
                                <button onClick={clearResults} className="text-[10px] text-gray-400 hover:text-red-500">Clear Results</button>
                            </div>

                            {/* Color legend */}
                            <div className="bg-white rounded-lg border border-gray-200 p-3 shadow-sm">
                                <p className="text-[10px] text-gray-400 font-medium mb-2">MASK COLOR LEGEND</p>
                                <div className="flex flex-wrap gap-3">
                                    {MASK_LEGEND.map(l => (
                                        <div key={l.label} className="flex items-center gap-1.5">
                                            <span className="w-3 h-3 rounded-sm shrink-0 border border-black/10" style={{ background: l.color }} />
                                            <span className="text-xs text-gray-700">{l.label}</span>
                                        </div>
                                    ))}
                                </div>
                            </div>

                            {/* Each result: side-by-side input + mask */}
                            {segResults.map((r, idx) => (
                                <div key={idx} className="bg-white rounded-lg border border-gray-200 p-4 shadow-sm">
                                    <div className="flex items-center justify-between mb-3">
                                        <div className="flex items-center gap-2">
                                            <span className="text-xs font-medium text-gray-900">📸 {r.inputName}</span>
                                            {r.status === "processing" && (
                                                <span className="text-[10px] bg-yellow-100 text-yellow-700 px-2 py-0.5 rounded-full animate-pulse">Processing...</span>
                                            )}
                                            {r.status === "done" && (
                                                <span className="text-[10px] bg-green-100 text-green-700 px-2 py-0.5 rounded-full">✓ Done</span>
                                            )}
                                            {r.status === "error" && (
                                                <span className="text-[10px] bg-red-100 text-red-700 px-2 py-0.5 rounded-full">✕ Failed</span>
                                            )}
                                        </div>
                                        {r.maskUrl && (
                                            <a href={r.maskUrl} download={`mask_${r.inputName}`} className="text-[10px] text-[#0B5FA5] hover:text-[#094d87] font-medium">
                                                📥 Download
                                            </a>
                                        )}
                                    </div>

                                    <div className="grid grid-cols-2 gap-3">
                                        {/* Input */}
                                        <div>
                                            <p className="text-[10px] text-gray-400 mb-1 font-medium">INPUT — Satellite Image</p>
                                            <img src={r.inputUrl} alt={`Input: ${r.inputName}`} className="rounded-lg border border-gray-200 w-full object-contain max-h-[350px] bg-gray-100" />
                                        </div>
                                        {/* Output */}
                                        <div>
                                            <p className="text-[10px] text-gray-400 mb-1 font-medium">OUTPUT — AI Segmentation Mask</p>
                                            {r.status === "processing" && (
                                                <div className="rounded-lg border border-gray-200 w-full h-[200px] bg-gray-50 flex flex-col items-center justify-center gap-2">
                                                    <div className="w-6 h-6 border-2 border-[#0B5FA5]/30 border-t-[#0B5FA5] rounded-full animate-spin" />
                                                    <span className="text-xs text-gray-400">Segmenting...</span>
                                                </div>
                                            )}
                                            {r.status === "done" && r.maskUrl && (
                                                <img src={r.maskUrl} alt={`Mask: ${r.inputName}`} className="rounded-lg border border-[#0B5FA5]/20 w-full object-contain max-h-[350px] bg-gray-100" />
                                            )}
                                            {r.status === "error" && (
                                                <div className="rounded-lg border border-red-200 w-full h-[200px] bg-red-50 flex flex-col items-center justify-center gap-1">
                                                    <span className="text-2xl">⚠️</span>
                                                    <span className="text-xs text-red-500">{r.error || "Failed"}</span>
                                                </div>
                                            )}
                                        </div>
                                    </div>
                                </div>
                            ))}
                        </div>
                    )}
                </div>

                {/* Sidebar */}
                <div className="space-y-4">
                    <div className="bg-white rounded-lg border border-gray-200 p-4 shadow-sm">
                        <h3 className="text-sm font-semibold text-gray-900 mb-3">Analysis Type</h3>
                        <div className="space-y-2">
                            {ANALYSIS_TYPES.map(at => (
                                <label
                                    key={at.id}
                                    className={`block p-3 rounded-lg border cursor-pointer transition-colors ${analysisType === at.id ? "border-[#0B5FA5] bg-blue-50/40" : "border-gray-200 hover:border-gray-300"}`}
                                >
                                    <input type="radio" name="analysis" value={at.id} checked={analysisType === at.id}
                                        onChange={() => { setAnalysisType(at.id); clearResults(); }} className="sr-only" />
                                    <p className="text-xs font-medium text-gray-900">{at.label}</p>
                                    <p className="text-[10px] text-gray-400 mt-0.5">{at.desc}</p>
                                </label>
                            ))}
                        </div>
                    </div>

                    <button
                        onClick={handleSubmit}
                        disabled={files.length === 0 || processing}
                        className={`w-full py-2.5 rounded-lg text-sm font-semibold transition-colors ${files.length > 0 && !processing ? "bg-[#0B5FA5] text-white hover:bg-[#094d87]" : "bg-gray-200 text-gray-400 cursor-not-allowed"}`}
                    >
                        {processing ? progress || "Processing..." : analysisType === "segment" ? `🧠 Run AI Segmentation${totalImages > 1 ? ` (${totalImages} images)` : ""}` : "Run Analysis"}
                    </button>

                    {processing && analysisType === "segment" && (
                        <button onClick={() => { abortRef.current = true; }} className="w-full py-2 rounded-lg text-xs font-medium text-red-500 border border-red-200 hover:bg-red-50 transition-colors">
                            ✕ Stop Processing
                        </button>
                    )}

                    {analysisType === "segment" ? (
                        <div className="bg-blue-50 border border-blue-200 rounded-lg p-3 space-y-2">
                            <p className="text-[10px] text-blue-700 font-medium">🧠 AI Segmentation</p>
                            <p className="text-[10px] text-blue-600">
                                Sends each image to the Unified Cartographer model on HuggingFace. Images are processed one at a time. Max 4.5 MB per file.
                            </p>
                            <div className="border-t border-blue-200 pt-2">
                                <p className="text-[10px] text-blue-700 font-medium mb-1">Color Legend:</p>
                                {MASK_LEGEND.map(l => (
                                    <div key={l.label} className="flex items-center gap-1.5 mb-0.5">
                                        <span className="w-2.5 h-2.5 rounded-sm" style={{ background: l.color }} />
                                        <span className="text-[10px] text-blue-600">{l.label}</span>
                                    </div>
                                ))}
                            </div>
                        </div>
                    ) : (
                        <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-3">
                            <p className="text-[10px] text-yellow-700 font-medium">⚡ Backend Pipeline</p>
                            <p className="text-[10px] text-yellow-600 mt-1">
                                Analysis runs on AWS EC2 with YOLOv8-Seg model. Better engine coming for production.
                            </p>
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
