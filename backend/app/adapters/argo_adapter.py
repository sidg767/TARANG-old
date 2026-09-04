"""
ArgoAdapter — for Argo profile data.
Unlike gridded NetCDF files, Argo data is point-based (N_PROF).
It does not support 2D slices or 3D volume raymarching.
The frontend uses the /api/instruments and /api/profile endpoints for Argo,
but this adapter provides the metadata.
"""

from __future__ import annotations

import logging

import xarray as xr

from backend.app.adapters.base import DataSourceAdapter, SliceResult, VolumeResult

logger = logging.getLogger("tarang.adapters.argo")


class ArgoAdapter(DataSourceAdapter):
    def __init__(self, manifest: dict):
        super().__init__(manifest)

    def open(self, bbox=None) -> xr.Dataset:
        raise NotImplementedError(
            "ArgoAdapter is point-data only; use /api/instruments and /api/profile "
            "instead of opening a gridded xr.Dataset."
        )

    def get_metadata(self) -> dict:
        variable = self.variable
        return {
            "source_id":           self.manifest["id"],
            "label":               self.manifest.get("label", self.manifest["id"]),
            "available_variables": [variable],
            "cf_metadata": {
                variable: {
                    "standard_name": self.manifest.get("standard_name", variable),
                    "long_name":     self.manifest.get("long_name", variable),
                    "units":         self.manifest.get("units", "unknown"),
                    "valid_min":     self.manifest.get("valid_min", -9999.0),
                    "valid_max":     self.manifest.get("valid_max",  9999.0),
                    "missing_value": self.manifest.get("missing_value", 99999.0),
                }
            },
            "depth_levels": [],
            "time_range":   {},
            "dimensions":   {},
        }

    def get_slice(self, variable: str, depth_m: float, time_idx: int, bbox: tuple[float, float, float, float]) -> SliceResult:
        raise NotImplementedError("Argo data does not support slice endpoints; use /api/instruments")

    def get_volume(self, variable: str, time_idx: int, bbox: tuple[float, float, float, float]) -> VolumeResult:
        raise NotImplementedError("Argo data does not support volume endpoints")
