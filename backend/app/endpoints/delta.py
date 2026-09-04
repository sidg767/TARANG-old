from __future__ import annotations

import asyncio
import logging

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from backend.app.endpoints.binary import parse_bbox

logger = logging.getLogger("tarang.endpoint.delta")
router = APIRouter(tags=["analytics"])


@router.get("/delta")
async def get_delta(
    request: Request,
    source: str = Query(..., description="Model registry source ID"),
    time_idx: int = Query(0, description="Model time step index"),
    bbox: str = Query("80,5,100,25", description="minLon,minLat,maxLon,maxLat"),
    limit: int = Query(500, description="Max instruments to fetch"),
):
    """
    Returns spatial delta (model vs observation) for all floats in the region.
    """
    db = request.app.state.db
    registry = request.app.state.registry

    if not db:
        raise HTTPException(500, "Database not configured")

    try:
        adapter = registry.get_adapter(source)
    except KeyError:
        raise HTTPException(404, f"Unknown source '{source}'")

    try:
        bbox_tuple = parse_bbox(bbox)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # 1. Fetch instruments in bbox
    rows = await db.query_instruments(bbox_tuple, limit=limit)
    if not rows:
        return JSONResponse(content={"deltas": []})

    # 2. Extract model grid for bbox (surface level)
    def compute_deltas():
        adapter_var = adapter.manifest.get("variable")
        std_name = adapter.manifest.get("standard_name")
        
        is_temp = std_name == "sea_water_potential_temperature"
        is_sal = std_name == "sea_water_salinity"
        
        if not is_temp and not is_sal:
            raise ValueError(f"Source {source} is neither temperature nor salinity")

        # Import the profile loader to fetch observation profiles
        import glob
        import os

        from backend.app.endpoints.profile import (
            _argo_cache_lock,
            _load_from_local_cache,
        )
        
        data_dir = os.getenv("DATA_DIR", "data")
        cache_files = sorted(
            glob.glob(os.path.join(data_dir, "argo", "*.nc"))
            + glob.glob(os.path.join(data_dir, "glider", "*.nc"))
        )

        deltas = []
        for row in rows:
            platform_id = str(row["platform_id"])
            profile = None
            
            with _argo_cache_lock:
                for cache_path in cache_files:
                    try:
                        profile = _load_from_local_cache(cache_path, platform_id)
                        break
                    except ValueError:
                        continue
                        
            if not profile:
                continue
                
            # Get model profile at this location
            try:
                model_depths, model_vals = adapter.get_profile_at(adapter_var, profile["lat"], profile["lon"], time_idx)
                ds_open = adapter.open(None)
                try:
                    cf_meta = adapter._extract_cf_meta(ds_open, adapter_var, adapter._resolve_depth_levels(ds_open))
                finally:
                    try:
                        ds_open.close()
                    except Exception:
                        pass
                valid_mask = model_vals != cf_meta.missing_value
                
                if np.any(valid_mask):
                    obs_depths = profile["depth"]
                    obs_vals = profile["temperature"] if is_temp else profile["salinity"]
                    
                    if not obs_vals or not obs_depths:
                        continue
                        
                    # Interpolate model to observation depths
                    interp_model = np.interp(obs_depths, model_depths[valid_mask], model_vals[valid_mask], left=np.nan, right=np.nan)
                    
                    # Take the surface delta (first valid depth)
                    delta_val = None
                    for m, o in zip(interp_model, obs_vals):
                        if not np.isnan(m) and o is not None:
                            delta_val = float(m - o)
                            break
                            
                    if delta_val is not None:
                        deltas.append({
                            "platform_id": platform_id,
                            "lat": profile["lat"],
                            "lon": profile["lon"],
                            "delta": delta_val,
                            "type": "temperature" if is_temp else "salinity"
                        })
            except Exception as e:
                logger.warning(f"Failed to compute delta for {platform_id}: {e}")
                
        return deltas

    loop = asyncio.get_running_loop()
    try:
        deltas = await loop.run_in_executor(None, compute_deltas)
    except Exception as e:
        logger.error(f"Delta computation failed: {e}")
        raise HTTPException(500, f"Computation failed: {e}")

    return JSONResponse(content={"deltas": deltas})
