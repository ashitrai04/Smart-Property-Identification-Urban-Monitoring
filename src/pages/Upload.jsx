import React, { useState, useRef } from "react";
import { fromBlob } from "geotiff";

const SEGMENTATION_API = "https://amrender-segformer-b5.hf.space/predict";
const CHANGE_DETECTION_API = "https://amrender-urban-change-detector.hf.space/api/process_temporal_change";
const MAX_FILE_SIZE = 4.5 * 1024 * 1024; // 4.5 MB

const ANALYSIS_TYPES = [
    { id: "segment", label: "AI Segmentation", desc: "Run AI segmentation on satellite/drone imagery (max 4.5 MB per image)" },
    { id: "change", label: "Change Detection", desc: "Compare past & present satellite images to detect urban changes" },
    { id: "boundary", label: "Boundary Analysis", desc: "Upload a shapefile boundary for land use analysis" },
    { id: "mask", label: "Mask Overlay", desc: "Upload a mask TIFF for change detection comparison" },
];

const MASK_LEGEND = [
    { label: "Buildings", color: "#EF4444" },
    { label: "Roads", color: "#EAB308" },
    { label: "Water Bodies", color: "#3B82F6" },
    { label: "Open Plots / Barren", color: "#9CA3AF" },
];

const CHANGE_LEGEND = [
    { label: "New Construction / Urban Growth", color: "#00FFFF" },
    { label: "Demolished / Cleared", color: "#EF4444" },
    { label: "Vegetation Loss", color: "#F59E0B" },
    { label: "Land Use Change", color: "#8B5CF6" },
    { label: "No Change", color: "transparent", border: true },
];

const ACCEPTED = ".shp,.shx,.dbf,.prj,.tif,.tiff,.geojson,.json,.zip,.jpg,.jpeg,.png,.bmp,.webp";

// ── Convert any image file to a displayable data URL ──
async function fileToPreviewUrl(file) {
    const name = file.name.toLowerCase();
    if (/\.(jpg|jpeg|png|gif|webp|bmp|svg)$/.test(name)) {
        return URL.createObjectURL(file);
    }
    if (/\.(tif|tiff)$/.test(name)) {
        try {
            const tiff = await fromBlob(file);
            const image = await tiff.getImage();
            const width = image.getWidth();
            const height = image.getHeight();
            const rasters = await image.readRasters();
            const canvas = document.createElement("canvas");
            canvas.width = width;
            canvas.height = height;
            const ctx = canvas.getContext("2d");
            const imgData = ctx.createImageData(width, height);
            const numBands = rasters.length;
            for (let i = 0; i < width * height; i++) {
                if (numBands >= 3) {
                    imgData.data[i * 4] = rasters[0][i];
                    imgData.data[i * 4 + 1] = rasters[1][i];
                    imgData.data[i * 4 + 2] = rasters[2][i];
                    imgData.data[i * 4 + 3] = numBands >= 4 ? rasters[3][i] : 255;
                } else {
                    const v = rasters[0][i];
                    imgData.data[i * 4] = v;
                    imgData.data[i * 4 + 1] = v;
                    imgData.data[i * 4 + 2] = v;
                    imgData.data[i * 4 + 3] = 255;
                }
            }
            ctx.putImageData(imgData, 0, 0);
            return canvas.toDataURL("image/png");
        } catch (e) {
            console.warn("TIFF decode failed:", e);
            return null;
        }
    }
    return URL.createObjectURL(file);
}

export default function Upload() {
    const [files, setFiles] = useState([]);
    const [analysisType, setAnalysisType] = useState("segment");
    const [processing, setProcessing] = useState(false);
    const [progress, setProgress] = useState("");
    const [error, setError] = useState(null);
    const [segResults, setSegResults] = useState([]);
    const [dummyResult, setDummyResult] = useState(null);
    const abortRef = useRef(false);

    // ── Change Detection state ──
    const [cdPastFile, setCdPastFile] = useState(null);
    const [cdPresentFile, setCdPresentFile] = useState(null);
    const [cdVolumeKnob, setCdVolumeKnob] = useState(11.0);
    const [cdThreshold, setCdThreshold] = useState(0.85);
    // { pastUrl, presentUrl, changeUrl, status, error? }
    const [cdResult, setCdResult] = useState(null);

    const handleDrop = (e) => {
        e.preventDefault();
        e.stopPropagation();
        setFiles(prev => [...prev, ...Array.from(e.dataTransfer.files)]);
    };

    const handleFileInput = (e) => {
        setFiles(prev => [...prev, ...Array.from(e.target.files)]);
    };

    const removeFile = (idx) => { setFiles(prev => prev.filter((_, i) => i !== idx)); };

    // ── Send one image to the segmentation API ──
    const segmentOneFile = async (file) => {
        const formData = new FormData();
        formData.append("file", file);
        const resp = await fetch(SEGMENTATION_API, { method: "POST", body: formData });
        if (!resp.ok) {
            const errText = await resp.text().catch(() => "Unknown error");
            throw new Error(`API ${resp.status}: ${errText}`);
        }
        const data = await resp.json();
        // API returns { master_map_base64, raw_mask_base64 } as JPEG base64
        const b64 = data.master_map_base64 || data.raw_mask_base64;
        return `data:image/jpeg;base64,${b64}`;
    };

    // ── Process multiple images sequentially ──
    const runSegmentation = async () => {
        const imageFiles = files.filter(f => /\.(jpg|jpeg|png|tif|tiff|webp|bmp)$/i.test(f.name));
        if (imageFiles.length === 0) {
            setError("Please upload at least one image (.jpg, .png, .tif, .webp, .bmp) for segmentation.");
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

            const inputUrl = await fileToPreviewUrl(file);
            setSegResults(prev => [...prev, { inputName: file.name, inputUrl, maskUrl: null, status: "processing" }]);

            try {
                const maskUrl = await segmentOneFile(file);
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

    // ── Change Detection ──
    const runChangeDetection = async () => {
        if (!cdPastFile || !cdPresentFile) {
            setError("Please upload both a PAST and a PRESENT image for change detection.");
            return;
        }
        if (cdPastFile.size > MAX_FILE_SIZE) {
            setError(`Past image (${cdPastFile.name}) exceeds 4.5 MB limit.`);
            return;
        }
        if (cdPresentFile.size > MAX_FILE_SIZE) {
            setError(`Present image (${cdPresentFile.name}) exceeds 4.5 MB limit.`);
            return;
        }

        setProgress("Generating previews...");
        const pastUrl = await fileToPreviewUrl(cdPastFile);
        const presentUrl = await fileToPreviewUrl(cdPresentFile);

        setCdResult({ pastUrl, presentUrl, changeUrl: null, status: "processing" });

        setProgress("📡 Preparing images...");
        try {
            const GRADIO_BASE = "https://amrender-urban-change-detector.hf.space";

            // Convert TIF files to PNG blobs, pass other formats through
            const fileToPngBlob = async (file) => {
                const name = file.name.toLowerCase();
                if (/\.(tif|tiff)$/.test(name)) {
                    // Convert TIF → canvas → PNG blob
                    const previewDataUrl = await fileToPreviewUrl(file);
                    if (previewDataUrl) {
                        const resp = await fetch(previewDataUrl);
                        return new File([await resp.blob()], file.name.replace(/\.(tif|tiff)$/i, ".png"), { type: "image/png" });
                    }
                }
                return file;
            };

            const pastBlob = await fileToPngBlob(cdPastFile);
            const presentBlob = await fileToPngBlob(cdPresentFile);

            // Step 1: Upload files to Gradio server
            setProgress("📡 Uploading images to AI server...");
            const uploadFile = async (file) => {
                const form = new FormData();
                form.append("files", file);
                const resp = await fetch(`${GRADIO_BASE}/gradio_api/upload`, { method: "POST", body: form });
                if (!resp.ok) throw new Error(`Upload failed: ${resp.status}`);
                const paths = await resp.json();
                return paths[0]; // server file path like "/tmp/gradio/.../filename.png"
            };

            const pastPath = await uploadFile(pastBlob);
            const presentPath = await uploadFile(presentBlob);

            // Step 2: Submit processing job → get event_id
            setProgress("📡 Submitting to Urban Change Detector AI...");
            const callResp = await fetch(`${GRADIO_BASE}/gradio_api/call/process_temporal_change`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    data: [
                        { path: pastPath, orig_name: pastBlob.name, size: pastBlob.size, mime_type: "image/png", is_stream: false, meta: { _type: "gradio.FileData" } },
                        { path: presentPath, orig_name: presentBlob.name, size: presentBlob.size, mime_type: "image/png", is_stream: false, meta: { _type: "gradio.FileData" } },
                    ],
                }),
            });
            if (!callResp.ok) {
                const errText = await callResp.text().catch(() => "Unknown error");
                throw new Error(`API call failed ${callResp.status}: ${errText}`);
            }
            const { event_id } = await callResp.json();
            if (!event_id) throw new Error("No event_id returned");

            // Step 3: Get SSE result stream
            setProgress("📡 AI is analyzing changes... please wait...");
            const sseResp = await fetch(`${GRADIO_BASE}/gradio_api/call/process_temporal_change/${event_id}`);
            if (!sseResp.ok) throw new Error(`Result fetch failed: ${sseResp.status}`);
            const sseText = await sseResp.text();

            // Parse SSE — find the "data:" line with JSON array of image objects
            let changeUrl = null;
            for (const line of sseText.split("\n")) {
                if (line.startsWith("data:")) {
                    try {
                        const data = JSON.parse(line.slice(5).trim());
                        if (Array.isArray(data) && data.length > 0 && data[0]?.url) {
                            changeUrl = data[0].url; // Growth Highlighted change map
                            break;
                        }
                    } catch (e) { /* skip non-JSON data lines */ }
                }
            }

            if (!changeUrl) throw new Error("No change map returned. Response: " + sseText.slice(0, 200));

            // Fetch the result image and display it
            const imgResp = await fetch(changeUrl);
            const blob = await imgResp.blob();
            setCdResult(prev => ({ ...prev, changeUrl: URL.createObjectURL(blob), status: "done" }));
        } catch (err) {
            setCdResult(prev => ({ ...prev, status: "error", error: err.message }));
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
                "Model": "Unified Cartographer (HF)", "Confidence": "94.3%",
            },
        });
        setProgress("");
    };

    const handleSubmit = async () => {
        if (analysisType === "change") {
            if (!cdPastFile || !cdPresentFile) {
                setError("Please upload both a PAST and a PRESENT image.");
                return;
            }
        } else if (files.length === 0) {
            return;
        }
        setProcessing(true);
        setError(null);
        setDummyResult(null);

        if (analysisType === "segment") {
            await runSegmentation();
        } else if (analysisType === "change") {
            await runChangeDetection();
        } else {
            await runDummyAnalysis();
        }
        setProcessing(false);
    };

    const clearResults = () => {
        segResults.forEach(r => { if (r.inputUrl) URL.revokeObjectURL(r.inputUrl); if (r.maskUrl) URL.revokeObjectURL(r.maskUrl); });
        setSegResults([]);
        setDummyResult(null);
        setCdResult(null);
        setCdPastFile(null);
        setCdPresentFile(null);
        setError(null);
    };

    const completedCount = segResults.filter(r => r.status === "done").length;
    const totalImages = files.filter(f => /\.(jpg|jpeg|png|tif|tiff)$/i.test(f.name)).length;

    // ── Check if submit button should be enabled ──
    const canSubmit = analysisType === "change"
        ? (cdPastFile && cdPresentFile && !processing)
        : (files.length > 0 && !processing);

    return (
        <div className="space-y-6">
            <div className="bg-white rounded-lg border border-gray-200 p-4 sm:p-5 shadow-sm">
                <h2 className="text-lg sm:text-xl font-bold text-gray-900">Upload & Analysis</h2>
                <p className="text-sm text-gray-500 mt-1">
                    Upload satellite/drone images for AI segmentation, change detection, or boundary analysis
                </p>
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
                {/* Upload zone + results */}
                <div className="lg:col-span-2 space-y-4">

                    {/* ── CHANGE DETECTION: Dual Upload ── */}
                    {analysisType === "change" ? (
                        <div className="space-y-4">
                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                                {/* Past image upload */}
                                <div
                                    onDrop={e => { e.preventDefault(); e.stopPropagation(); const f = e.dataTransfer.files[0]; if (f) setCdPastFile(f); }}
                                    onDragOver={e => { e.preventDefault(); e.stopPropagation(); }}
                                    onClick={() => document.getElementById("cd-past-input").click()}
                                    className={`bg-white border-2 border-dashed rounded-lg p-6 text-center cursor-pointer transition-colors ${cdPastFile ? "border-green-300 bg-green-50/30" : "border-gray-300 hover:border-[#0B5FA5] hover:bg-blue-50/30"}`}
                                >
                                    <div className="text-3xl mb-2">🕒</div>
                                    <p className="text-xs font-medium text-gray-900">PAST Image</p>
                                    <p className="text-[10px] text-gray-400 mt-1">Upload the older satellite image</p>
                                    {cdPastFile ? (
                                        <div className="mt-2 flex items-center justify-center gap-1">
                                            <span className="text-[10px] text-green-700 font-medium truncate max-w-[140px]">✓ {cdPastFile.name}</span>
                                            <button onClick={e => { e.stopPropagation(); setCdPastFile(null); }} className="text-red-400 hover:text-red-600 text-xs ml-1">✕</button>
                                        </div>
                                    ) : (
                                        <p className="text-[10px] text-gray-400 mt-2">Click or drag & drop</p>
                                    )}
                                    <input id="cd-past-input" type="file" accept=".jpg,.jpeg,.png,.tif,.tiff,.webp,.bmp"
                                        onChange={e => { if (e.target.files[0]) setCdPastFile(e.target.files[0]); }} className="hidden" />
                                </div>

                                {/* Present image upload */}
                                <div
                                    onDrop={e => { e.preventDefault(); e.stopPropagation(); const f = e.dataTransfer.files[0]; if (f) setCdPresentFile(f); }}
                                    onDragOver={e => { e.preventDefault(); e.stopPropagation(); }}
                                    onClick={() => document.getElementById("cd-present-input").click()}
                                    className={`bg-white border-2 border-dashed rounded-lg p-6 text-center cursor-pointer transition-colors ${cdPresentFile ? "border-green-300 bg-green-50/30" : "border-gray-300 hover:border-[#0B5FA5] hover:bg-blue-50/30"}`}
                                >
                                    <div className="text-3xl mb-2">📍</div>
                                    <p className="text-xs font-medium text-gray-900">PRESENT Image</p>
                                    <p className="text-[10px] text-gray-400 mt-1">Upload the recent satellite image</p>
                                    {cdPresentFile ? (
                                        <div className="mt-2 flex items-center justify-center gap-1">
                                            <span className="text-[10px] text-green-700 font-medium truncate max-w-[140px]">✓ {cdPresentFile.name}</span>
                                            <button onClick={e => { e.stopPropagation(); setCdPresentFile(null); }} className="text-red-400 hover:text-red-600 text-xs ml-1">✕</button>
                                        </div>
                                    ) : (
                                        <p className="text-[10px] text-gray-400 mt-2">Click or drag & drop</p>
                                    )}
                                    <input id="cd-present-input" type="file" accept=".jpg,.jpeg,.png,.tif,.tiff,.webp,.bmp"
                                        onChange={e => { if (e.target.files[0]) setCdPresentFile(e.target.files[0]); }} className="hidden" />
                                </div>
                            </div>

                            {/* Sensitivity controls */}
                            <div className="bg-white rounded-lg border border-gray-200 p-4 shadow-sm">
                                <p className="text-xs font-semibold text-gray-900 mb-3">Detection Sensitivity</p>
                                <div className="grid grid-cols-2 gap-4">
                                    <div>
                                        <label className="text-[10px] text-gray-500 font-medium block mb-1">
                                            Volume Knob: <span className="text-gray-900">{cdVolumeKnob.toFixed(1)}</span>
                                        </label>
                                        <input type="range" min="1" max="20" step="0.5" value={cdVolumeKnob}
                                            onChange={e => setCdVolumeKnob(parseFloat(e.target.value))}
                                            className="w-full h-1.5 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-[#0B5FA5]" />
                                        <p className="text-[9px] text-gray-400 mt-0.5">Higher = more sensitive to small changes</p>
                                    </div>
                                    <div>
                                        <label className="text-[10px] text-gray-500 font-medium block mb-1">
                                            Threshold: <span className="text-gray-900">{cdThreshold.toFixed(2)}</span>
                                        </label>
                                        <input type="range" min="0.5" max="1.0" step="0.01" value={cdThreshold}
                                            onChange={e => setCdThreshold(parseFloat(e.target.value))}
                                            className="w-full h-1.5 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-[#0B5FA5]" />
                                        <p className="text-[9px] text-gray-400 mt-0.5">Lower = detects more subtle changes</p>
                                    </div>
                                </div>
                            </div>
                        </div>
                    ) : (
                        /* ── Standard file upload (Segmentation, Boundary, Mask) ── */
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
                    )}

                    {/* File list (for non-change detection) */}
                    {analysisType !== "change" && files.length > 0 && (
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

                    {/* Dummy result for non-segment/non-change */}
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

                    {/* ── Change Detection Results ── */}
                    {cdResult && (
                        <div className="space-y-4">
                            <div className="flex items-center justify-between">
                                <div className="flex items-center gap-2">
                                    <h3 className="text-sm font-semibold text-gray-900">🔍 Change Detection Results</h3>
                                    {cdResult.status === "processing" && (
                                        <span className="text-[10px] bg-yellow-100 text-yellow-700 px-2 py-0.5 rounded-full animate-pulse">Processing...</span>
                                    )}
                                    {cdResult.status === "done" && (
                                        <span className="text-[10px] bg-green-100 text-green-700 px-2 py-0.5 rounded-full">✓ Complete</span>
                                    )}
                                    {cdResult.status === "error" && (
                                        <span className="text-[10px] bg-red-100 text-red-700 px-2 py-0.5 rounded-full">✕ Failed</span>
                                    )}
                                </div>
                                <button onClick={clearResults} className="text-[10px] text-gray-400 hover:text-red-500">Clear Results</button>
                            </div>

                            {/* Color legend */}
                            <div className="bg-white rounded-lg border border-gray-200 p-3 shadow-sm">
                                <p className="text-[10px] text-gray-400 font-medium mb-2">CHANGE DETECTION LEGEND</p>
                                <div className="flex flex-wrap gap-3">
                                    {CHANGE_LEGEND.map(l => (
                                        <div key={l.label} className="flex items-center gap-1.5">
                                            <span className="w-3 h-3 rounded-sm shrink-0" style={{
                                                background: l.border ? "transparent" : l.color,
                                                border: l.border ? "2px solid #9CA3AF" : `1px solid ${l.color}80`,
                                            }} />
                                            <span className="text-xs text-gray-700">{l.label}</span>
                                        </div>
                                    ))}
                                </div>
                            </div>

                            {/* Three-column display: Past | Present | Change Map */}
                            <div className="bg-white rounded-lg border border-gray-200 p-4 shadow-sm">
                                <div className="grid grid-cols-3 gap-3">
                                    {/* Past */}
                                    <div>
                                        <p className="text-[10px] text-gray-400 mb-1 font-medium">🕒 PAST Image</p>
                                        {cdResult.pastUrl ? (
                                            <img src={cdResult.pastUrl} alt="Past satellite" className="rounded-lg border border-gray-200 w-full object-contain max-h-[350px] bg-gray-100" />
                                        ) : (
                                            <div className="rounded-lg border border-gray-200 w-full h-[200px] bg-gray-50 flex items-center justify-center">
                                                <span className="text-xs text-gray-400">No preview</span>
                                            </div>
                                        )}
                                    </div>
                                    {/* Present */}
                                    <div>
                                        <p className="text-[10px] text-gray-400 mb-1 font-medium">📍 PRESENT Image</p>
                                        {cdResult.presentUrl ? (
                                            <img src={cdResult.presentUrl} alt="Present satellite" className="rounded-lg border border-gray-200 w-full object-contain max-h-[350px] bg-gray-100" />
                                        ) : (
                                            <div className="rounded-lg border border-gray-200 w-full h-[200px] bg-gray-50 flex items-center justify-center">
                                                <span className="text-xs text-gray-400">No preview</span>
                                            </div>
                                        )}
                                    </div>
                                    {/* Change Map Output */}
                                    <div>
                                        <p className="text-[10px] text-gray-400 mb-1 font-medium">🔥 CHANGE MAP — AI Output</p>
                                        {cdResult.status === "processing" && (
                                            <div className="rounded-lg border border-gray-200 w-full h-[200px] bg-gray-50 flex flex-col items-center justify-center gap-2">
                                                <div className="w-6 h-6 border-2 border-[#0B5FA5]/30 border-t-[#0B5FA5] rounded-full animate-spin" />
                                                <span className="text-xs text-gray-400">Detecting changes...</span>
                                            </div>
                                        )}
                                        {cdResult.status === "done" && cdResult.changeUrl && (
                                            <img src={cdResult.changeUrl} alt="Change detection output" className="rounded-lg border border-[#0B5FA5]/20 w-full object-contain max-h-[350px] bg-gray-100" />
                                        )}
                                        {cdResult.status === "error" && (
                                            <div className="rounded-lg border border-red-200 w-full h-[200px] bg-red-50 flex flex-col items-center justify-center gap-1">
                                                <span className="text-2xl">⚠️</span>
                                                <span className="text-xs text-red-500">{cdResult.error || "Failed"}</span>
                                            </div>
                                        )}
                                    </div>
                                </div>

                                {/* Download button */}
                                {cdResult.status === "done" && cdResult.changeUrl && (
                                    <div className="mt-3 flex justify-end">
                                        <a href={cdResult.changeUrl} download="ai_change_detection_output.png"
                                            className="text-xs text-[#0B5FA5] hover:text-[#094d87] font-medium flex items-center gap-1">
                                            📥 Download Change Map
                                        </a>
                                    </div>
                                )}
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
                                        <div>
                                            <p className="text-[10px] text-gray-400 mb-1 font-medium">INPUT — Satellite Image</p>
                                            {r.inputUrl ? (
                                                <img src={r.inputUrl} alt={`Input: ${r.inputName}`} className="rounded-lg border border-gray-200 w-full object-contain max-h-[350px] bg-gray-100" />
                                            ) : (
                                                <div className="rounded-lg border border-gray-200 w-full h-[200px] bg-gray-50 flex flex-col items-center justify-center gap-1">
                                                    <span className="text-3xl">🖼️</span>
                                                    <span className="text-xs text-gray-500 font-medium">{r.inputName}</span>
                                                    <span className="text-[10px] text-gray-400">Preview not available for this format</span>
                                                </div>
                                            )}
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
                        disabled={!canSubmit}
                        className={`w-full py-2.5 rounded-lg text-sm font-semibold transition-colors ${canSubmit ? "bg-[#0B5FA5] text-white hover:bg-[#094d87]" : "bg-gray-200 text-gray-400 cursor-not-allowed"}`}
                    >
                        {processing
                            ? progress || "Processing..."
                            : analysisType === "segment"
                                ? `🧠 Run AI Segmentation${totalImages > 1 ? ` (${totalImages} images)` : ""}`
                                : analysisType === "change"
                                    ? "🔍 Run Change Detection"
                                    : "Run Analysis"
                        }
                    </button>

                    {processing && (analysisType === "segment" || analysisType === "change") && (
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
                    ) : analysisType === "change" ? (
                        <div className="bg-orange-50 border border-orange-200 rounded-lg p-3 space-y-2">
                            <p className="text-[10px] text-orange-700 font-medium">🔍 Change Detection</p>
                            <p className="text-[10px] text-orange-600">
                                Upload two images of the same area from different time periods. The AI model (Urban Change Detector) compares them semantically to detect construction, demolition, and land use changes.
                            </p>
                            <div className="border-t border-orange-200 pt-2">
                                <p className="text-[10px] text-orange-700 font-medium mb-1">Parameters:</p>
                                <p className="text-[10px] text-orange-600">• <strong>Volume Knob</strong>: Higher = more sensitive</p>
                                <p className="text-[10px] text-orange-600">• <strong>Threshold</strong>: Lower = detects subtle changes</p>
                            </div>
                        </div>
                    ) : (
                        <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-3">
                            <p className="text-[10px] text-yellow-700 font-medium">⚡ Backend Pipeline</p>
                            <p className="text-[10px] text-yellow-600 mt-1">
                                Analysis runs on HuggingFace Spaces using Unified Cartographer & Urban Change Detector models.
                            </p>
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
