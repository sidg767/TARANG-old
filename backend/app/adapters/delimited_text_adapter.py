"""
DelimitedTextAdapter — pandas-backed CSV / ASCII ingestion adapter.

Satisfies the PS's "Multi-format ingestion (NetCDF, ASCII)" requirement. The PS
Background is explicit that text files "span multiple depth levels, spatial
grids, and time steps", so this adapter reads a long-format table

    lat, lon[, depth][, time], <var1>[, <var2> ...]

into a proper gridded xarray Dataset with dims (time?, depth?, lat, lon) — the
same shape the NetCDF adapter produces, so the whole slice / volume / isosurface
/ WMS / WCS pipeline works on CSV sources unchanged. Files with no depth/time
column collapse to a 2-D (lat, lon) grid.

Delimiter is sniffed (comma / tab / whitespace). All value columns are exposed,
so the frontend variable selector works on a multi-column CSV.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import xarray as xr

from backend.app.adapters.base import DataSourceAdapter, SliceResult, VolumeResult

logger = logging.getLogger("tarang.adapters.csv")

_LAT_ALIASES = ("lat", "latitude", "y")
_LON_ALIASES = ("lon", "longitude", "x")
_DEPTH_ALIASES = ("depth", "pres", "pressure", "lev", "z")
_TIME_ALIASES = ("time", "date", "datetime", "timestamp")
_MISSING = -9999.0


class DelimitedTextAdapter(DataSourceAdapter):
    """CSV/ASCII → gridded xarray, with optional depth and time axes."""

    def __init__(self, manifest: dict):
        super().__init__(manifest)
        self._ds: xr.Dataset | None = None

    # ── Load ──────────────────────────────────────────────────────────────────
    def open(self, bbox=None, mode: str = "live") -> xr.Dataset:
        if self._ds is not None:
            return self._ds

        source = self.local_cache or self.source_url
        logger.info(f"Loading delimited text: {source}")

        # sep=None + engine='python' sniffs comma / tab / semicolon / whitespace.
        df = pd.read_csv(source, sep=None, engine="python")
        df.columns = [str(c).lower().strip() for c in df.columns]

        def _resolve(aliases: tuple[str, ...]) -> str | None:
            return next((a for a in aliases if a in df.columns), None)

        lat_col = _resolve(_LAT_ALIASES)
        lon_col = _resolve(_LON_ALIASES)
        if not lat_col or not lon_col:
            raise ValueError(f"CSV needs latitude & longitude columns. Found: {list(df.columns)}")
        df = df.rename(columns={lat_col: "lat", lon_col: "lon"})

        depth_col = _resolve(_DEPTH_ALIASES)
        time_col = _resolve(_TIME_ALIASES)
        if depth_col and depth_col != "depth":
            df = df.rename(columns={depth_col: "depth"})
            depth_col = "depth"
        if time_col:
            df = df.rename(columns={time_col: "time"})
            time_col = "time"
            df["time"] = pd.to_datetime(df["time"], errors="coerce")
            df = df.dropna(subset=["time"])

        dims: list[str] = []
        if time_col:
            dims.append("time")
        if depth_col:
            dims.append("depth")
        dims += ["lat", "lon"]

        reserved = {"lat", "lon", "depth", "time"}
        value_cols = [
            c for c in df.columns
            if c not in reserved and pd.api.types.is_numeric_dtype(df[c])
        ]
        if not value_cols:
            raise ValueError(f"CSV has no numeric value column besides {reserved & set(df.columns)}")

        # Long table → gridded cube. mean() collapses duplicate (dim-combo) rows;
        # to_xarray() fills absent combinations with NaN.
        gridded = df.groupby(dims, sort=True)[value_cols].mean().to_xarray()

        # Attach CF attrs — manifest values for the manifest's primary variable,
        # sensible generics for the rest.
        for v in value_cols:
            if v == self.variable:
                gridded[v].attrs.update({
                    "standard_name": self.manifest.get("standard_name", v),
                    "long_name": self.manifest.get("long_name", v),
                    "units": self.manifest.get("units", "unknown"),
                })
            else:
                gridded[v].attrs.setdefault("standard_name", v)
                gridded[v].attrs.setdefault("long_name", v)
                gridded[v].attrs.setdefault("units", "unknown")
            finite = gridded[v].values[np.isfinite(gridded[v].values)]
            gridded[v].attrs.setdefault("valid_min", float(finite.min()) if finite.size else 0.0)
            gridded[v].attrs.setdefault("valid_max", float(finite.max()) if finite.size else 1.0)
            gridded[v].attrs["_FillValue"] = _MISSING

        self._ds = gridded
        return self._ds

    # ── Metadata ──────────────────────────────────────────────────────────────
    def get_metadata(self) -> dict:
        ds = self.open()
        cf_meta = {}
        for v in ds.data_vars:
            a = ds[v].attrs
            cf_meta[v] = {
                "standard_name": a.get("standard_name", v),
                "long_name": a.get("long_name", v),
                "units": a.get("units", "unknown"),
                "valid_min": float(a.get("valid_min", -9999)),
                "valid_max": float(a.get("valid_max", 9999)),
                "missing_value": _MISSING,
            }

        if "depth" in ds.coords:
            depth_levels = [float(d) for d in ds["depth"].values]
        else:
            depth_levels = self.manifest.get("depth_levels") or [0]

        time_range = {}
        if "time" in ds.coords:
            times = ds["time"].values
            time_range = {"start": str(times[0]), "end": str(times[-1]), "steps": len(times)}

        return {
            "source_id": self.manifest["id"],
            "label": self.manifest.get("label", self.manifest["id"]),
            "available_variables": list(ds.data_vars),
            "cf_metadata": cf_meta,
            "depth_levels": depth_levels,
            "time_range": time_range,
            "dimensions": {k: int(v) for k, v in ds.sizes.items()},
        }

    # ── Internal: (var) → 2-D lat×lon DataArray at a depth / time ─────────────
    def _select_2d(self, ds: xr.Dataset, variable: str, depth_m: float, time_idx: int):
        da = ds[variable]
        time_str = "static"
        if "time" in da.dims:
            n = da.sizes["time"]
            ti = max(0, min(int(time_idx), n - 1))
            time_str = str(ds["time"].values[ti])
            da = da.isel(time=ti)
        actual_depth = 0.0
        if "depth" in da.dims:
            da = da.sel(depth=depth_m, method="nearest")
            actual_depth = float(da["depth"].values)
        return da, actual_depth, time_str

    def get_slice(
        self,
        variable: str,
        depth_m: float,
        time_idx: int,
        bbox: tuple[float, float, float, float],
        mode: str = "live",
    ) -> SliceResult:
        ds = self.open()
        min_lon, min_lat, max_lon, max_lat = bbox
        if variable not in ds.data_vars:
            variable = self.variable if self.variable in ds.data_vars else list(ds.data_vars)[0]

        da, actual_depth, time_str = self._select_2d(ds, variable, depth_m, time_idx)
        da = da.sel(lat=slice(min_lat, max_lat), lon=slice(min_lon, max_lon))

        arr = np.nan_to_num(da.values.astype(np.float32), nan=_MISSING)
        meta = self._extract_cf_meta(ds, variable, [actual_depth])
        meta.bounds = {
            "lat": [float(min_lat), float(max_lat)],
            "lon": [float(min_lon), float(max_lon)],
            "depth": [float(actual_depth)],
        }
        return SliceResult(
            data=arr,
            meta=meta,
            lat=da.coords["lat"].values.astype(np.float32),
            lon=da.coords["lon"].values.astype(np.float32),
            depth_m=actual_depth,
            time_str=time_str,
        )

    def get_volume(
        self,
        variable: str,
        time_idx: int,
        bbox: tuple[float, float, float, float],
        mode: str = "live",
    ) -> VolumeResult:
        ds = self.open()
        min_lon, min_lat, max_lon, max_lat = bbox
        if variable not in ds.data_vars:
            variable = self.variable if self.variable in ds.data_vars else list(ds.data_vars)[0]

        da = ds[variable]
        time_str = "static"
        if "time" in da.dims:
            n = da.sizes["time"]
            ti = max(0, min(int(time_idx), n - 1))
            time_str = str(ds["time"].values[ti])
            da = da.isel(time=ti)
        da = da.sel(lat=slice(min_lat, max_lat), lon=slice(min_lon, max_lon))

        if "depth" in da.dims:
            da = da.transpose("depth", "lat", "lon")
            arr = da.values.astype(np.float32)
        else:
            arr = da.values.astype(np.float32)[np.newaxis, :, :]  # degenerate depth

        arr = np.nan_to_num(arr, nan=_MISSING)
        meta = self._extract_cf_meta(ds, variable, self.get_metadata()["depth_levels"])
        meta.bounds = {
            "lat": [float(min_lat), float(max_lat)],
            "lon": [float(min_lon), float(max_lon)],
            "depth": [float(self.get_metadata()["depth_levels"][0]),
                      float(self.get_metadata()["depth_levels"][-1])],
        }
        return VolumeResult(
            data=arr,
            meta=meta,
            lat=da.coords["lat"].values.astype(np.float32),
            lon=da.coords["lon"].values.astype(np.float32),
            time_str=time_str,
        )

    def get_profile_at(
        self, variable: str, lat: float, lon: float, time_idx: int = 0
    ) -> tuple[np.ndarray, np.ndarray]:
        """Nearest-cell vertical profile — used for model-vs-observation deltas."""
        ds = self.open()
        if variable not in ds.data_vars:
            variable = self.variable if self.variable in ds.data_vars else list(ds.data_vars)[0]
        da = ds[variable]
        if "time" in da.dims:
            n = da.sizes["time"]
            da = da.isel(time=max(0, min(int(time_idx), n - 1)))
        da = da.sel(lat=lat, lon=lon, method="nearest")
        depths = (np.asarray(ds["depth"].values, dtype=np.float32)
                  if "depth" in ds.coords else np.array([0.0], dtype=np.float32))
        vals = np.nan_to_num(np.atleast_1d(da.values).astype(np.float32), nan=_MISSING)
        return depths, vals
