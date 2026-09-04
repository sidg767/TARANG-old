"""
GET /api/eddy?source=&time=&bbox=&threshold=
GET /api/front?source=&var=&time=&bbox=&threshold=
"""
from __future__ import annotations

import asyncio
import logging

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

# netCDF4/HDF5 isn't thread-safe for concurrent access to the process-wide cached file handle
# (see the long note on _NETCDF_IO_LOCK in netcdf_adapter). get_slice/get_volume already hold it;
# eddy/front open + read the same files in their own executor thread, so without taking the same
# lock a Volume-workspace burst (volume + slice + eddy + front all at once) raises "NetCDF: HDF
# error". Reuse the adapter's lock so all NetCDF I/O in the process serialises through one gate.
from backend.app.adapters.netcdf_adapter import _NETCDF_IO_LOCK
from backend.app.endpoints.binary import parse_bbox

logger = logging.getLogger("tarang.endpoint.eddy")
router = APIRouter(tags=["analytics"])

# Common (eastward, northward) sea-water-velocity variable-name pairs across the datasets this
# app ingests: HYCOM (water_u/water_v), HF-radar fixture (current_u/current_v), Copernicus
# (uo/vo), CF standard names, and bare u/v.
_UV_PAIRS = [
    ("water_u", "water_v"),
    ("current_u", "current_v"),
    ("uo", "vo"),
    ("eastward_sea_water_velocity", "northward_sea_water_velocity"),
    ("u", "v"),
]


def _find_uv(data_vars) -> tuple[str, str] | None:
    names = set(data_vars)
    for u, v in _UV_PAIRS:
        if u in names and v in names:
            return u, v
    return None

@router.get("/eddy")
async def get_eddy(
    request: Request,
    source: str = Query(..., description="Registry source ID"),
    time: int = Query(0, description="Time step index (0-based)"),
    bbox: str = Query("80,5,100,25", description="minLon,minLat,maxLon,maxLat"),
    threshold: float | None = Query(None, description="Okubo-Weiss threshold (auto-scaled if omitted)"),
):
    registry = request.app.state.registry
    try:
        adapter = registry.get_adapter(source)
    except KeyError:
        raise HTTPException(404, f"Unknown source '{source}'")

    try:
        bbox_tuple = parse_bbox(bbox)
    except ValueError as e:
        raise HTTPException(400, str(e))

    def compute():
        min_lon, min_lat, max_lon, max_lat = bbox_tuple

        # adapter.open() locks _NETCDF_IO_LOCK internally for the file open (and the lock is NOT
        # reentrant) - so open OUTSIDE the lock, then take it only for the subset + .values read.
        ds = adapter.open(bbox_tuple)
        try:
          with _NETCDF_IO_LOCK:
            lat_dim = "latitude" if "latitude" in ds.dims else "lat"
            lon_dim = "longitude" if "longitude" in ds.dims else "lon"

            subset = ds.sel(**{
                lat_dim: slice(min_lat, max_lat),
                lon_dim: slice(min_lon, max_lon),
            })

            if "time" in subset.dims:
                subset = subset.isel(time=min(time, subset.sizes["time"] - 1))

            if "depth" in subset.dims or "lev" in subset.dims:
                depth_dim = "depth" if "depth" in subset.dims else "lev"
                subset = subset.isel(**{depth_dim: 0})

            uv = _find_uv(subset.data_vars)
            if uv is None:
                raise ValueError(
                    f"Source '{source}' has no current-velocity variables "
                    f"(need one of {[p[0] for p in _UV_PAIRS]}) — pick a currents source for eddies"
                )
            u = subset[uv[0]].values.astype(np.float64)
            v = subset[uv[1]].values.astype(np.float64)

            lats = subset[lat_dim].values.astype(np.float64)
            lons = subset[lon_dim].values.astype(np.float64)
        finally:
            try:
                ds.close()
            except Exception:
                pass

        if lats.size < 3 or lons.size < 3:
            return []   # this source doesn't cover the requested bbox — no eddies, not an error
        
        dy = np.gradient(lats) * 111320.0
        dx = np.gradient(lons) * 111320.0
        
        dy_2d = dy[:, None] * np.ones_like(lons)[None, :]
        dx_2d = dx[None, :] * np.cos(np.radians(lats))[:, None]

        dy_2d = np.where(dy_2d == 0, 1e-6, dy_2d)
        dx_2d = np.where(dx_2d == 0, 1e-6, dx_2d)

        du_dy, du_dx = np.gradient(u)
        dv_dy, dv_dx = np.gradient(v)
        
        du_dy /= dy_2d
        du_dx /= dx_2d
        dv_dy /= dy_2d
        dv_dx /= dx_2d

        s_n = du_dx - dv_dy
        s_s = dv_dx + du_dy
        omega = dv_dx - du_dy
        
        W = s_n**2 + s_s**2 - omega**2

        # Okubo-Weiss < 0 marks rotation-dominated (eddy) water. Rather than threshold-and-blob
        # (which merges into one map-spanning region on a smooth field), find LOCAL MINIMA of W
        # — each is a distinct eddy core — that are also strongly negative.
        try:
            from scipy import ndimage
        except Exception:
            return []

        Wf = ndimage.gaussian_filter(np.nan_to_num(W, nan=0.0), sigma=1.0)
        Wneg = -Wf[Wf < 0]
        if Wneg.size == 0:
            return []
        thr = threshold if (threshold and threshold > 0) else float(np.percentile(Wneg, 70))

        win = 7
        is_min = (Wf == ndimage.minimum_filter(Wf, size=win)) & (Wf < -thr)
        ys, xs = np.where(is_min)

        # Drop candidates hugging the subset edge — np.gradient is one-sided there, so W is
        # unreliable and "eddies" pile up along the bbox border.
        lat_lo, lat_hi = lats.min() + 0.75, lats.max() - 0.75
        lon_lo, lon_hi = lons.min() + 0.75, lons.max() - 0.75

        raw = []
        for ci, cj in zip(ys.tolist(), xs.tolist()):
            la, lo = float(lats[ci]), float(lons[cj])
            if not (lat_lo <= la <= lat_hi and lon_lo <= lo <= lon_hi):
                continue
            rot = float(omega[ci, cj])
            strength = float(-Wf[ci, cj]) / (thr + 1e-30)
            raw.append({
                "lat": la, "lon": lo,
                "type": "warm" if rot < 0 else "cold",
                "w_value": float(W[ci, cj]),
                "radius_km": float(60 + 45 * np.tanh(strength / 6.0)),
            })
        raw.sort(key=lambda c: c["w_value"])   # strongest first

        # Spatial non-max suppression: real (turbulent) current fields produce dozens of
        # near-coincident minima that render as an unreadable pile of overlapping circles.
        # Keep the strongest in any ~2° neighbourhood.
        kept: list[dict] = []
        for c in raw:
            if all(abs(c["lat"] - k["lat"]) > 2.0 or abs(c["lon"] - k["lon"]) > 2.0 for k in kept):
                kept.append(c)
            if len(kept) >= 10:
                break
        return kept

    loop = asyncio.get_running_loop()
    try:
        cells = await loop.run_in_executor(None, compute)
    except Exception as e:
        logger.error(f"Eddy computation failed: {e}")
        raise HTTPException(500, f"Computation failed: {e}")

    return JSONResponse(content={"cells": cells})

@router.get("/front")
async def get_front(
    request: Request,
    source: str = Query(..., description="Registry source ID"),
    var: str = Query("water_temp", description="Variable name for gradient (water_temp or salinity)"),
    time: int = Query(0, description="Time step index (0-based)"),
    bbox: str = Query("80,5,100,25", description="minLon,minLat,maxLon,maxLat"),
    threshold: float | None = Query(None, description="Gradient threshold (auto-scaled if omitted)"),
):
    registry = request.app.state.registry
    try:
        adapter = registry.get_adapter(source)
    except KeyError:
        raise HTTPException(404, f"Unknown source '{source}'")

    try:
        bbox_tuple = parse_bbox(bbox)
    except ValueError as e:
        raise HTTPException(400, str(e))

    def compute():
        min_lon, min_lat, max_lon, max_lat = bbox_tuple

        # adapter.open() locks _NETCDF_IO_LOCK internally for the file open (and the lock is NOT
        # reentrant) - so open OUTSIDE the lock, then take it only for the subset + .values read.
        ds = adapter.open(bbox_tuple)
        try:
          with _NETCDF_IO_LOCK:
            lat_dim = "latitude" if "latitude" in ds.dims else "lat"
            lon_dim = "longitude" if "longitude" in ds.dims else "lon"

            subset = ds.sel(**{
                lat_dim: slice(min_lat, max_lat),
                lon_dim: slice(min_lon, max_lon),
            })

            if "time" in subset.dims:
                subset = subset.isel(time=min(time, subset.sizes["time"] - 1))

            if "depth" in subset.dims or "lev" in subset.dims:
                depth_dim = "depth" if "depth" in subset.dims else "lev"
                subset = subset.isel(**{depth_dim: 0})

            if var not in subset.data_vars:
                raise ValueError(f"Source '{source}' is missing variable '{var}'")

            data = subset[var].values.astype(np.float64)

            lats = subset[lat_dim].values.astype(np.float64)
            lons = subset[lon_dim].values.astype(np.float64)
        finally:
            try:
                ds.close()
            except Exception:
                pass

        if lats.size < 3 or lons.size < 3:
            return []

        dy = np.gradient(lats) * 111320.0
        dx = np.gradient(lons) * 111320.0
        
        dy_2d = dy[:, None] * np.ones_like(lons)[None, :]
        dx_2d = dx[None, :] * np.cos(np.radians(lats))[:, None]

        dy_2d = np.where(dy_2d == 0, 1e-6, dy_2d)
        dx_2d = np.where(dx_2d == 0, 1e-6, dx_2d)

        grad_y, grad_x = np.gradient(data)
        grad_mag = np.sqrt((grad_x / dx_2d)**2 + (grad_y / dy_2d)**2)

        # Auto-scale to this field (see the eddy endpoint's note) — a fixed °C/m default is
        # meaningless across datasets of different smoothness.
        thr = threshold
        if thr is None or thr <= 0:
            finite = grad_mag[np.isfinite(grad_mag) & (grad_mag > 0)]
            thr = float(np.percentile(finite, 97.5)) if finite.size else 0.0

        cells = []
        mask = (grad_mag > thr) & ~np.isnan(grad_mag)
        
        for i in range(len(lats)):
            for j in range(len(lons)):
                if mask[i, j]:
                    cells.append({"lat": float(lats[i]), "lon": float(lons[j]), "gradient_magnitude": float(grad_mag[i, j])})
                    
        return cells

    loop = asyncio.get_running_loop()
    try:
        cells = await loop.run_in_executor(None, compute)
    except Exception as e:
        logger.error(f"Front computation failed: {e}")
        raise HTTPException(500, f"Computation failed: {e}")

    return JSONResponse(content={"cells": cells})
