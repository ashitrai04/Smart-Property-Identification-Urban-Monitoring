import React, { useState } from 'react';
import { GUNTUR_LAYERS, LAYER_COLORS, LOCATIONS, MAP_STYLES, MASK_COLORS } from './MapView';

export default function LayerPanel({ layers, onToggle, onFlyTo, mapStyle, onStyleChange }) {
    const [collapsed, setCollapsed] = useState(false);

    if (collapsed) {
        return (
            <button
                className="panel-toggle-btn"
                onClick={() => setCollapsed(false)}
                title="Show Layers"
            >
                ☰
            </button>
        );
    }

    return (
        <div className="layer-panel">
            {/* Header */}
            <div className="panel-header">
                <span className="panel-title">🗺️ Map Layers</span>
                <button
                    className="panel-collapse-btn"
                    onClick={() => setCollapsed(true)}
                    title="Collapse"
                >
                    ✕
                </button>
            </div>

            {/* Base Map Style Switcher */}
            <div className="layer-section">
                <div className="section-header">
                    <span className="section-title">Base Map</span>
                </div>
                <div className="style-switcher">
                    {MAP_STYLES.map((style) => (
                        <button
                            key={style.id}
                            className={`style-btn ${mapStyle === style.id ? 'active' : ''}`}
                            onClick={() => onStyleChange(style.id)}
                            title={style.label}
                        >
                            <span className="style-icon">{style.icon}</span>
                            <span className="style-label">{style.label}</span>
                        </button>
                    ))}
                </div>
            </div>

            {/* Visakhapatnam Section */}
            <div className="layer-section">
                <div className="section-header">
                    <span className="section-title">Visakhapatnam</span>
                    <button
                        className="fly-btn"
                        onClick={() => onFlyTo(LOCATIONS.visakhapatnam)}
                    >
                        ↗ Fly To
                    </button>
                </div>

                <div
                    className={`layer-item ${layers['vizag-mask'] ? 'active' : ''}`}
                    onClick={() => onToggle('vizag-mask')}
                >
                    <label className="toggle-switch" onClick={(e) => e.stopPropagation()}>
                        <input
                            type="checkbox"
                            checked={layers['vizag-mask']}
                            onChange={() => onToggle('vizag-mask')}
                        />
                        <span className="toggle-slider" />
                    </label>
                    <span
                        className="layer-dot"
                        style={{ background: LAYER_COLORS['vizag-mask'].dot }}
                    />
                    <span className="layer-label">Land Use Segmentation</span>
                </div>
            </div>

            {/* Guntur Section */}
            <div className="layer-section">
                <div className="section-header">
                    <span className="section-title">Guntur</span>
                    <button
                        className="fly-btn"
                        onClick={() => onFlyTo(LOCATIONS.guntur)}
                    >
                        ↗ Fly To
                    </button>
                </div>

                {GUNTUR_LAYERS.map((layer) => (
                    <div
                        key={layer.id}
                        className={`layer-item ${layers[layer.id] ? 'active' : ''}`}
                        onClick={() => onToggle(layer.id)}
                    >
                        <label className="toggle-switch" onClick={(e) => e.stopPropagation()}>
                            <input
                                type="checkbox"
                                checked={layers[layer.id]}
                                onChange={() => onToggle(layer.id)}
                            />
                            <span className="toggle-slider" />
                        </label>
                        <span
                            className="layer-dot"
                            style={{ background: layer.color.dot }}
                        />
                        <span className="layer-label">{layer.label}</span>
                    </div>
                ))}
            </div>

            {/* Legend */}
            <div className="legend-section">
                <div className="legend-title">Legend</div>
                <div className="legend-items">
                    {/* Mask classification legend */}
                    <div className="legend-group-label">Mask Classification</div>
                    {[
                        { label: 'Vegetation', color: `rgb(${MASK_COLORS[1][0]},${MASK_COLORS[1][1]},${MASK_COLORS[1][2]})` },
                        { label: 'Built-up', color: `rgb(${MASK_COLORS[2][0]},${MASK_COLORS[2][1]},${MASK_COLORS[2][2]})` },
                        { label: 'Water', color: `rgb(${MASK_COLORS[3][0]},${MASK_COLORS[3][1]},${MASK_COLORS[3][2]})` },
                        { label: 'Barren', color: `rgb(${MASK_COLORS[4][0]},${MASK_COLORS[4][1]},${MASK_COLORS[4][2]})` },
                    ].map((item) => (
                        <div key={item.label} className="legend-item">
                            <span className="legend-color" style={{ background: item.color }} />
                            <span className="legend-label">{item.label}</span>
                        </div>
                    ))}
                    {/* Feature layer legend */}
                    <div className="legend-group-label" style={{ marginTop: '6px' }}>Feature Layers</div>
                    {GUNTUR_LAYERS.map((l) => (
                        <div key={l.label} className="legend-item">
                            <span className="legend-color" style={{ background: l.color.outline }} />
                            <span className="legend-label">{l.label}</span>
                        </div>
                    ))}
                </div>
            </div>
        </div>
    );
}
