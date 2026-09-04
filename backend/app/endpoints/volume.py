"""
GET /api/volume?source=&var=&time=&bbox=

Full depth column as 3D (depth, lat, lon) Float32Array — for raymarching.
Larger payload than slice → longer Redis TTL (10 min).
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException, Query, Request, Response

from backend.app.cache import TTL_VOLUME
from backend.app.endpoints.binary import make_binary_response, parse_bbox

logger = logging.getLogger("tarang.endpoint.volume")
router = APIRouter(tags=["data"])

# Dedicated executor for heavy NetCDF I/O and live fetching
# High worker count ensures multiple live fetches (e.g. spamming the UI) 
# don't exhaust the pool and cause Gateway Timeouts.
_data_executor = ThreadPoolExecutor(max_workers=64, thread_name_prefix="DataPool")


@router.get("/volume")
async def get_volume(
    request: Request,
    source: str   = Query(...),
    var:    str   = Query(...),
    time:   int   = Query(0),
    bbox:   str   = Query("80,5,100,25"),
    mode:   str   = Query("live"),
):
    registry = request.app.state.registry
    cache    = request.app.state.cache

    try:
        adapter = registry.get_adapter(source)
    except KeyError:
        raise HTTPException(404, f"Unknown source '{source}'")

    try:
        bbox_tuple = parse_bbox(bbox)
    except ValueError as e:
        raise HTTPException(400, str(e))

    key = cache.volume_key(source, var, time, bbox_tuple, mode)

    async def compute() -> bytes:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            _data_executor,
            lambda: adapter.get_volume(var, time, bbox_tuple, mode)
        )
        header = {
            **result.meta.to_header_dict(),
            "time": result.time_str,
        }
        resp = make_binary_response(header, result.data)
        return resp.body

    raw = await cache.get_or_compute(key, TTL_VOLUME, compute, metric={
        "kind": "volume", "source": source, "var": var, "bbox": bbox,
    })
    return Response(content=raw, media_type="application/octet-stream")
