"""
DSM Building Height & Floor (Storey) Extraction Engine
======================================================
High-performance, memory-efficient photogrammetric pipeline to:
1. Filter bare-earth terrain to derive Digital Terrain Model (DTM).
2. Calculate Normalized Digital Surface Model (nDSM = DSM - DTM).
3. Distinguish planar rooftops from textured tree canopies.
4. Delineate building footprint polygons with fast vectorization.
5. Compute accurate 3D metrics: mean height, peak height, base elevation,
   footprint area, and classified floor/storey counts.
"""

import os
import math
import numpy as np
import scipy.ndimage as ndi
import rasterio
import rasterio.features
import rasterio.windows
import geopandas as gpd
from shapely.geometry import shape, Polygon
from skimage import morphology


class DSMHeightEngine:
    def __init__(
        self,
        min_building_height: float = 2.2,
        floor_height: float = 3.0,
        min_building_area: float = 12.0,
        max_roughness: float = 1.35,
        dtm_filter_radius_m: float = 22.0
    ):
        self.min_height = min_building_height
        self.floor_height = floor_height
        self.min_area = min_building_area
        self.max_roughness = max_roughness
        self.dtm_radius_m = dtm_filter_radius_m

    def compute_dtm(self, dsm_data: np.ndarray, res_x: float, res_y: float) -> np.ndarray:
        """
        Derives bare-earth terrain (DTM) from DSM using fast hierarchical separable filtering.
        Takes ~0.15s per 2048x2048 tile.
        """
        pixel_size = (res_x + res_y) / 2.0
        step = 16  # Downsample to ~1m resolution
        sub_dsm = dsm_data[::step, ::step]
        
        # Filter size on downsampled grid
        filter_size = max(5, int((2 * self.dtm_radius_m) / (pixel_size * step)))
        
        # Morphological opening (minimum then maximum filter)
        dtm_sub = ndi.minimum_filter(sub_dsm, size=filter_size)
        dtm_sub = ndi.maximum_filter(dtm_sub, size=filter_size)
        dtm_sub = ndi.gaussian_filter(dtm_sub, sigma=2.0)
        
        # Upsample back to original resolution
        zoom_y = dsm_data.shape[0] / dtm_sub.shape[0]
        zoom_x = dsm_data.shape[1] / dtm_sub.shape[1]
        dtm_full = ndi.zoom(dtm_sub, (zoom_y, zoom_x), order=1)
        
        # Ensure exact matching dimensions
        dtm_full = dtm_full[:dsm_data.shape[0], :dsm_data.shape[1]]
        if dtm_full.shape != dsm_data.shape:
            pad_y = dsm_data.shape[0] - dtm_full.shape[0]
            pad_x = dsm_data.shape[1] - dtm_full.shape[1]
            dtm_full = np.pad(dtm_full, ((0, max(0, pad_y)), (0, max(0, pad_x))), mode='edge')
            
        return dtm_full

    def extract_tile_buildings(
        self,
        dsm_tile: np.ndarray,
        tile_transform,
        crs,
        res_x: float,
        res_y: float,
        nodata: float = -32767.0
    ) -> list:
        """
        Processes a single DSM tile and returns building dictionaries with 3D metrics.
        """
        valid_mask = (dsm_tile > -50) & (dsm_tile != nodata) & (~np.isnan(dsm_tile))
        if np.sum(valid_mask) < 500:
            return []

        clean_dsm = dsm_tile.copy()
        min_elev = np.nanmin(clean_dsm[valid_mask])
        clean_dsm[~valid_mask] = min_elev

        # 1. DTM & nDSM
        dtm = self.compute_dtm(clean_dsm, res_x, res_y)
        ndsm = np.where(valid_mask, clean_dsm - dtm, 0.0)
        ndsm = np.clip(ndsm, 0, 60.0)

        # 2. Vegetation vs Building Roof Discrimination
        dy, dx = np.gradient(ndsm)
        grad_mag = np.sqrt(dx**2 + dy**2)
        local_roughness = ndi.uniform_filter(grad_mag, size=int(1.5 / res_x))

        height_mask = (ndsm >= self.min_height) & valid_mask
        is_roof = height_mask & (local_roughness < self.max_roughness)

        # Fast morphological clean-up
        selem = morphology.disk(max(1, int(0.5 / res_x)))
        clean_roofs = morphology.opening(is_roof, selem)
        clean_roofs = morphology.closing(clean_roofs, selem)
        min_pixels = int(self.min_area / (res_x * res_y))
        clean_roofs = morphology.remove_small_objects(clean_roofs, min_size=min_pixels).astype(np.uint8)

        if np.sum(clean_roofs) == 0:
            return []

        # 3. Fast Vectorization via C-optimized rasterio.features.shapes
        shapes_gen = rasterio.features.shapes(clean_roofs, transform=tile_transform)
        tile_buildings = []

        for geom, val in shapes_gen:
            if val != 1:
                continue

            poly = shape(geom)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if not poly.is_valid or poly.area < self.min_area:
                continue

            # Simplify slightly to regularize edges
            poly_simp = poly.simplify(0.3, preserve_topology=True)
            if not poly_simp.is_valid or poly_simp.area < (self.min_area * 0.8):
                poly_simp = poly

            # Sample elevation inside polygon bounding box
            minx, miny, maxx, maxy = poly_simp.bounds
            r0, c0 = rasterio.transform.rowcol(tile_transform, minx, maxy)
            r1, c1 = rasterio.transform.rowcol(tile_transform, maxx, miny)
            r_start = max(0, min(r0, r1))
            r_end = min(dsm_tile.shape[0], max(r0, r1) + 1)
            c_start = max(0, min(c0, c1))
            c_end = min(dsm_tile.shape[1], max(c0, c1) + 1)

            sub_ndsm = ndsm[r_start:r_end, c_start:c_end]
            sub_dsm = clean_dsm[r_start:r_end, c_start:c_end]
            sub_dtm = dtm[r_start:r_end, c_start:c_end]
            sub_roof = clean_roofs[r_start:r_end, c_start:c_end]

            roof_pixels = sub_ndsm[sub_roof == 1]
            if len(roof_pixels) == 0:
                roof_pixels = sub_ndsm[sub_ndsm >= self.min_height]

            if len(roof_pixels) == 0:
                continue

            mean_h = float(np.mean(roof_pixels))
            max_h = float(np.percentile(roof_pixels, 95))
            min_h = float(np.percentile(roof_pixels, 10))
            roof_elev = float(np.percentile(sub_dsm[sub_roof == 1] if np.any(sub_roof == 1) else sub_dsm, 95))
            base_elev = float(np.median(sub_dtm))

            # Storey / Floor Calculation
            eff_h = max_h
            if eff_h < (self.floor_height + 1.2):
                floors = 1
                floor_label = "1 Floor (G)"
            elif eff_h < (2 * self.floor_height + 1.2):
                floors = 2
                floor_label = "2 Floors (G+1)"
            elif eff_h < (3 * self.floor_height + 1.2):
                floors = 3
                floor_label = "3 Floors (G+2)"
            elif eff_h < (4 * self.floor_height + 1.2):
                floors = 4
                floor_label = "4 Floors (G+3)"
            else:
                floors = max(1, int(round(eff_h / self.floor_height)))
                floor_label = f"{floors} Floors (G+{floors-1})"

            tile_buildings.append({
                "geometry": poly_simp,
                "mean_height_m": round(mean_h, 2),
                "max_height_m": round(max_h, 2),
                "min_height_m": round(min_h, 2),
                "base_elevation_m": round(base_elev, 2),
                "roof_elevation_m": round(roof_elev, 2),
                "area_m2": round(poly_simp.area, 1),
                "perimeter_m": round(poly_simp.length, 1),
                "floors": floors,
                "floor_label": floor_label
            })

        return tile_buildings
