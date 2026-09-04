"""
GET /api/slice?source=&var=&depth=&time=&bbox=

Returns a 2D (lat, lon) depth-slice as binary (§8.6 wire format).
This is the most-called endpoint — cache is checked first on every request.

Query params:
  source  str   registry ID (e.g. "hycom_water_temp")
  var     str   variable name (e.g. "water_temp")
  depth   float depth in meters — will snap to nearest actual level (§8.1)
  time    int   time step index (0-based)
  bbox    str   "minLon,minLat,maxLon,maxLat" (e.g. "80,5,100,25")
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException, Query, Request

from backend.app.cache import TTL_SLICE
from backend.app.endpoints.binary import make_binary_response, parse_bbox

logger = logging.getLogger("tarang.endpoint.slice")
router = APIRouter(tags=["data"])

_data_executor = ThreadPoolExecutor(max_workers=64, thread_name_prefix="DataPool")


@router.get("/slice")
async def get_slice(
    request: Request,
    source: str = Query(..., description="Registry source ID"),
    var:    str = Query(..., description="Variable name"),
    depth:  float = Query(..., description="Depth in meters (snaps to nearest level)"),
    time:   int   = Query(0,   description="Time step index (0-based)"),
    bbox:   str   = Query("80,5,100,25", description="minLon,minLat,maxLon,maxLat"),
    mode:   str   = Query("live", description="Data mode (live|cached)"),
):
    registry = request.app.state.registry
    cache    = request.app.state.cache

    # ── Validate ──────────────────────────────────────────────────────────────
    try:
        adapter = registry.get_adapter(source)
    except KeyError:
        raise HTTPException(404, f"Unknown source '{source}'")

    try:
        bbox_tuple = parse_bbox(bbox)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # ── Cache key ─────────────────────────────────────────────────────────────
    key = cache.slice_key(source, var, depth, time, bbox_tuple, mode)

    async def compute() -> bytes:
        loop = asyncio.get_running_loop()
        # xarray .compute() is CPU-bound — run in a thread pool
        result = await loop.run_in_executor(
            _data_executor,
            lambda: adapter.get_slice(var, depth, time, bbox_tuple, mode)
        )
        # Build binary payload
        header = {
            **result.meta.to_header_dict(),
            "depth_actual_m": result.depth_m,
            "time":           result.time_str,
        }
        resp = make_binary_response(header, result.data)
        return resp.body

    raw = await cache.get_or_compute(key, TTL_SLICE, compute, metric={
        "kind": "slice", "source": source, "var": var, "bbox": bbox,
    })

    from fastapi import Response
    return Response(content=raw, media_type="application/octet-stream")
