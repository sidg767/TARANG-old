"""
GET /api/metadata?source=<id>

Returns available variables, CF units, non-uniform depth levels, time range.
This endpoint drives ALL frontend selectors — it must respond before anything renders.
Cached in Redis for 1 hour (metadata rarely changes).
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.app.cache import TTL_METADATA

logger = logging.getLogger("tarang.api.metadata")
router = APIRouter(tags=["data"])

# Dedicated executor for metadata to prevent starvation from slow live fetches in the default pool.
_metadata_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="MetadataPool")



@router.get("/metadata")
async def get_metadata(source: str, request: Request):
    """
    Returns:
      {
        "source_id": "hycom_water_temp",
        "label": "HYCOM — Water Temperature",
        "available_variables": ["water_temp", "salinity", ...],
        "cf_metadata": { "water_temp": { "units": "degC", "standard_name": ..., ... } },
        "depth_levels": [0, 2, 4, 6, ..., 5000],   ← NON-UNIFORM, explicit list
        "time_range": { "start": "...", "end": "...", "steps": 8 },
        "dimensions": { "time": 8, "depth": 40, "lat": 850, "lon": 1500 }
      }
    """
    registry  = request.app.state.registry
    cache     = request.app.state.cache

    registry.ensure_loaded()   # self-heal a wiped worker before it 404s

    # ── Validate source ───────────────────────────────────────────────────────
    try:
        adapter = registry.get_adapter(source)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown source '{source}'. Available: {list(registry.manifest_ids())}"
        )

    # ── Cache check ───────────────────────────────────────────────────────────
    cache_key = cache.metadata_key(source)

    async def compute():
        import logging

        import orjson
        logger = logging.getLogger("tarang.api.metadata")
        logger.info(f"--> compute() started for {source}")
        loop = asyncio.get_running_loop()
        logger.info("--> waiting for metadata executor...")
        meta = await loop.run_in_executor(_metadata_executor, adapter.get_metadata)
        logger.info(f"--> executor finished for {source}")
        return orjson.dumps(meta)

    raw = await cache.get_or_compute(cache_key, TTL_METADATA, compute)
    import orjson
    return JSONResponse(content=orjson.loads(raw))


@router.get("/sources")
async def list_sources(request: Request):
    """List all registered data source IDs and labels. Used to populate the source dropdown."""
    registry = request.app.state.registry
    registry.ensure_loaded()   # self-heal a wiped worker so the dropdown never comes back empty
    sources = [
        {"id": m["id"], "label": m.get("label", m["id"]), "render_type": m.get("render_type", "slice")}
        for m in registry.all_manifests()
    ]
    return JSONResponse(content={"sources": sources})
