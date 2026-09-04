"""
GET /api/profile?platform_id=&time=

Returns depth-vs-variable arrays for one Argo float / glider profile.
Powers the click-to-inspect profile popover chart. Optionally compares the observation
against the nearest model grid cell (source=, time_idx=) — a deterministic, non-ML
model-vs-observation delta diagnostic (README §14), not a trained model.

Uses argopy 1.4.0 — pin carefully (§6, §15):
  - Python >= 3.11
  - Incompatible with xarray 2024.3.0–2025.6.1
  - Local cache checked first (§20 Rule 8)
"""

from __future__ import annotations

import asyncio
import logging
import threading

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("tarang.endpoint.profile")
router = APIRouter(tags=["instruments"])

# HDF5 (which the Argo/glider .nc files are stored in) isn't safe for concurrent access from
# multiple threads without a thread-safe build — two profile requests landing close together
# (each run via loop.run_in_executor's default thread pool) can open the same file at the same
# time and raise a raw "HDF error" from deep inside the C library. This has a real trigger, not
# just a theoretical one: the frontend's dedupe() helper retries a request whose in-flight
# promise was aborted by a caller that gave up (e.g. React StrictMode's mount->cleanup->mount) —
# but the ABORTED request's server-side work keeps running to completion regardless (FastAPI
# doesn't cancel a background thread just because the client disconnected), so it can genuinely
# overlap with the retry's own file access. Serialize local-cache reads so that can't happen.
_argo_cache_lock = threading.Lock()


@router.get("/profile")
async def get_profile(
    request:     Request,
    platform_id: str = Query(..., description="Argo float WMO ID or glider ID"),
    time:        str | None = Query(None, description="ISO date filter (optional)"),
    source:      str | None = Query(None, description="Optional model source ID for delta comparison"),
    time_idx:    int = Query(0, description="Model time step index for delta comparison"),
    mode:        str = Query("live", description="Data mode (live|cached)"),
):
    """
    Returns:
      {
        "platform_id": "1234567",
        "lat": 12.5, "lon": 88.3,
        "time": "2026-08-01T00:00:00",
        "depth":       [0, 10, 20, 50, 100, ...],
        "temperature": [28.5, 27.2, 26.1, ...],
        "salinity":    [33.5, 34.1, 34.8, ...],
        "units": { "depth": "m", "temperature": "degree_Celsius", "salinity": "psu" },
        "model_temperature": [28.4, 27.1, ...], # optional, only when source= is given
        "delta_temperature": [-0.1, -0.1, ...], # optional
        "model_salinity":    [...],             # optional
        "delta_salinity":    [...],             # optional
      }
    """
    loop = asyncio.get_running_loop()
    registry = request.app.state.registry

    # Instrument type (argo / glider / ctd / bgc / mooring / adcp) from PostGIS, so the UI can
    # label the popover correctly instead of always saying "Argo Float".
    db = getattr(request.app.state, "db", None)
    inst_type = None
    if db is not None:
        try:
            meta = await db.get_profile_meta(platform_id)
            inst_type = meta.get("type") if meta else None
        except Exception as e:
            logger.debug(f"instrument-type lookup failed for {platform_id}: {e}")

    def fetch_profile():
        # Try every local cache file (data/argo/*.nc + data/glider/*.nc) — no fixed
        # platform_id→file mapping, and _load_from_local_cache resolves either shape.
        import glob
        import os

        data_dir = os.getenv("DATA_DIR", "data")
        cache_files = sorted(
            glob.glob(os.path.join(data_dir, "argo", "*.nc"))
            + glob.glob(os.path.join(data_dir, "glider", "*.nc"))
            + glob.glob(os.path.join(data_dir, "ctd", "*.nc"))
            + glob.glob(os.path.join(data_dir, "bgc", "*.nc"))
            + glob.glob(os.path.join(data_dir, "mooring", "*.nc"))
            + glob.glob(os.path.join(data_dir, "adcp", "*.nc"))
        )

        profile = None
        with _argo_cache_lock:
            for cache_path in cache_files:
                try:
                    profile = _load_from_local_cache(cache_path, platform_id)
                    break
                except ValueError:
                    continue  # platform not in this file — try the next one

        if profile is None:
            # Not cached. The live argopy fallback needs internet + argopy (not in
            # requirements.txt). Fail cleanly rather than timing out / raising ImportError.
            offline = os.getenv("OFFLINE_MODE", "false").strip().lower() in ("1", "true", "yes", "on")
            if offline:
                raise ValueError(
                    f"Platform {platform_id} not in local cache and OFFLINE_MODE is on — "
                    f"run argo_ingest.py / glider_ingest.py with network first."
                )
            try:
                import argopy  # noqa: F401
            except ImportError:
                raise ValueError(
                    f"Platform {platform_id} not in local cache and argopy is not installed."
                )
            logger.warning(
                f"Platform {platform_id} not found in any local cache under '{data_dir}'. "
                "Falling back to live argopy query — run argo_ingest.py before demo! (§15)"
            )
            profile = _load_from_argopy(platform_id)

        # Deterministic model-vs-observation delta (README §14) — not ML, a straight numeric
        # comparison between this profile and the nearest model grid cell at the same location.
        if source and profile:
            try:
                import numpy as np
                adapter = registry.get_adapter(source)

                if profile["temperature"] and "temperature" in profile["units"]:
                    model_depths, model_temp = adapter.get_profile_at("water_temp", profile["lat"], profile["lon"], time_idx)
                    # Ignore NaNs/missing values for interpolation by masking
                    valid_mask = model_temp != adapter._extract_cf_meta(adapter.open(None), "water_temp", adapter._resolve_depth_levels(adapter.open(None))).missing_value
                    if np.any(valid_mask):
                        interp_temp = np.interp(profile["depth"], model_depths[valid_mask], model_temp[valid_mask], left=np.nan, right=np.nan)
                        profile["model_temperature"] = [float(v) if not np.isnan(v) else None for v in interp_temp]
                        profile["delta_temperature"] = [float(m - o) if m is not None and o is not None else None
                                                        for m, o in zip(profile["model_temperature"], profile["temperature"])]

                if profile["salinity"] and "salinity" in profile["units"]:
                    model_depths, model_sal = adapter.get_profile_at("salinity", profile["lat"], profile["lon"], time_idx)
                    valid_mask = model_sal != adapter._extract_cf_meta(adapter.open(None), "salinity", adapter._resolve_depth_levels(adapter.open(None))).missing_value
                    if np.any(valid_mask):
                        interp_sal = np.interp(profile["depth"], model_depths[valid_mask], model_sal[valid_mask], left=np.nan, right=np.nan)
                        profile["model_salinity"] = [float(v) if not np.isnan(v) else None for v in interp_sal]
                        profile["delta_salinity"] = [float(m - o) if m is not None and o is not None else None
                                                     for m, o in zip(profile["model_salinity"], profile["salinity"])]

            except Exception as e:
                logger.warning(f"Failed to fetch model delta for {platform_id} from {source}: {e}")

        return profile

    try:
        profile_data = await loop.run_in_executor(None, fetch_profile)
    except Exception as e:
        logger.error(f"Profile fetch failed for {platform_id}: {e}")
        return JSONResponse(
            status_code=404,
            content={"error": f"Profile not found for platform {platform_id}: {e!s}"}
        )

    if isinstance(profile_data, dict):
        profile_data["instrument_type"] = inst_type or profile_data.get("instrument_type")

    return JSONResponse(content=profile_data)


def _load_from_local_cache(cache_path: str, platform_id: str) -> dict:
    """
    Load a profile from a locally cached Argo/glider NetCDF file.

    Every Argo cache file this pipeline has actually produced (via argo_ingest.py's ERDDAP
    tabledap fetch, or an argopy point export) is a FLAT per-measurement table — one row per
    (float, cycle, depth level), indexed by a bare dimension ("row" or "N_POINTS"), not the
    classic multi-profile format (N_PROF dimension) this function originally assumed. That
    mismatch meant ds.dims.get("N_PROF", 0) was always 0 and EVERY platform 404'd regardless of
    ID. Column names also vary by source (lowercase from ERDDAP, UPPERCASE from argopy, and
    glider files use trajectory/profile_id/depth/temperature/salinity) — see the identical
    resolution helper in argo_ingest.py's ingest_to_postgis().
    """
    import xarray as xr

    # ALWAYS close the handle. Leaked HDF5/netCDF4 file handles accumulate and,
    # when Python's GC finalizes one while another request holds the same
    # process-wide cached handle mid-read, raise a C-level "NetCDF: HDF error"
    # that crashes the uvicorn worker (→ dropdowns blank until it respawns).
    ds = xr.open_dataset(cache_path)
    try:
        return _extract_profile_rows(ds, cache_path, platform_id)
    finally:
        try:
            ds.close()
        except Exception:
            pass


def _extract_profile_rows(ds, cache_path: str, platform_id: str) -> dict:
    import numpy as np

    def _col(*candidates: str) -> str | None:
        return next((c for c in candidates if c in ds.variables), None)

    # Argo columns or glider (trajectory/profile_id/depth/temperature/salinity).
    col_platform = _col("platform_number", "PLATFORM_NUMBER", "trajectory", "TRAJECTORY")
    col_cycle    = _col("cycle_number", "CYCLE_NUMBER", "profile_id", "PROFILE_ID")
    col_pres     = _col("pres", "PRES", "pressure", "depth", "DEPTH")
    col_temp     = _col("temp", "TEMP", "temperature", "TEMPERATURE")
    col_psal     = _col("psal", "PSAL", "salinity", "SALINITY")
    col_chl      = _col("chlorophyll", "CHLA", "chla", "CHLOROPHYLL", "chl_a", "chla_adjusted")
    col_o2       = _col("oxygen", "DOXY", "doxy", "OXYGEN", "dissolved_oxygen")
    col_no3      = _col("nitrate", "NITRATE", "no3", "NO3")
    col_ph       = _col("ph", "PH_IN_SITU_TOTAL", "ph_in_situ_total", "PH")
    col_cu       = _col("current_u", "u", "U", "eastward_sea_water_velocity", "water_u")
    col_cv       = _col("current_v", "v", "V", "northward_sea_water_velocity", "water_v")
    col_cspd     = _col("current_speed", "speed", "SPEED", "sea_water_speed")
    col_lat      = _col("latitude", "LATITUDE")
    col_lon      = _col("longitude", "LONGITUDE")
    col_time     = _col("time", "TIME", "JULD")

    if col_platform is None:
        raise ValueError(f"'{cache_path}' has no platform/float ID column")

    mask = ds[col_platform].values.astype(str) == str(platform_id)
    if not mask.any():
        raise ValueError(f"Platform {platform_id} not found in {cache_path}")

    dim = ds[col_platform].dims[0]
    sub = ds.isel({dim: mask})

    # Multiple profiles (cycles) for this float can be in the file — use the most recent one,
    # not an arbitrary/first row, so the chart reflects the float's latest known state.
    if col_cycle is not None:
        cycles = sub[col_cycle].values
        latest_cycle = cycles[np.argmax(cycles)]
        sub = sub.isel({dim: sub[col_cycle].values == latest_cycle})

    # Sort by depth so the chart draws a sane depth-ordered profile, not scan order.
    if col_pres is not None:
        order = np.argsort(sub[col_pres].values)
        sub = sub.isel({dim: order})

    def _values(col: str | None) -> list:
        return sub[col].values.flatten().tolist() if col else []

    lat  = float(sub[col_lat].values.flat[0])  if col_lat  else 0.0
    lon  = float(sub[col_lon].values.flat[0])  if col_lon  else 0.0
    time = str(sub[col_time].values.flat[0])   if col_time else None

    result = {
        "platform_id": platform_id,
        "lat": lat,
        "lon": lon,
        "time": time,
        "depth":       _values(col_pres),
        "temperature": _values(col_temp),
        "salinity":    _values(col_psal),
        "units": {
            "depth":       "dbar",
            "temperature": "degree_Celsius",
            "salinity":    "psu",
        }
    }
    if col_chl is not None:
        result["chlorophyll"] = _values(col_chl)
        result["units"]["chlorophyll"] = "mg m-3"
    if col_o2 is not None:
        result["oxygen"] = _values(col_o2)
        result["units"]["oxygen"] = "micromole kg-1"
    if col_no3 is not None:
        result["nitrate"] = _values(col_no3)
        result["units"]["nitrate"] = "micromole kg-1"
    if col_ph is not None:
        result["ph"] = _values(col_ph)
        result["units"]["ph"] = "total scale"

    # ADCP / mooring current profiles — derive speed from u/v if only components are present.
    if col_cspd is not None or (col_cu is not None and col_cv is not None):
        u_vals = _values(col_cu)
        v_vals = _values(col_cv)
        if col_cspd is not None:
            spd = _values(col_cspd)
        else:
            spd = [float(np.hypot(a, b)) for a, b in zip(u_vals, v_vals)]
        result["current_speed"] = spd
        if u_vals:
            result["current_u"] = u_vals
        if v_vals:
            result["current_v"] = v_vals
        result["units"]["current_speed"] = "m s-1"
    return result


def _load_from_argopy(platform_id: str) -> dict:
    """Live argopy fetch — fallback only, never called during demo."""
    from argopy import DataFetcher as ArgoDataFetcher

    loader = ArgoDataFetcher(src="gdac").float(int(platform_id))
    ds = loader.to_xarray()

    # Return the most recent profile
    pres  = ds["PRES"].values[-1].tolist()
    temp  = ds["TEMP"].values[-1].tolist()
    psal  = ds["PSAL"].values[-1].tolist()
    lat   = float(ds["LATITUDE"].values[-1])
    lon   = float(ds["LONGITUDE"].values[-1])
    time  = str(ds["TIME"].values[-1])

    return {
        "platform_id": platform_id,
        "lat": lat, "lon": lon, "time": time,
        "depth": pres, "temperature": temp, "salinity": psal,
        "units": {"depth": "dbar", "temperature": "degree_Celsius", "salinity": "psu"},
    }
