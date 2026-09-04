"""
GET /api/slice/preview?source=&var=&time=

A coarse, downsampled slice covering a source's FULL default_bbox (its entire ocean coverage,
not the user's currently-selected region), cached for hours. The frontend fetches this once per
source/variable and crops it client-side to whatever region gets picked — giving an instant
placeholder gradient the moment a region is selected, while the real higher-resolution regional
fetch (/api/slice) is still in flight.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response

from backend.app.endpoints.binary import make_binary_response

logger = logging.getLogger("tarang.endpoint.preview")
router = APIRouter(tags=["data"])

TTL_PREVIEW = 6 * 3600  # 6h — coarse enough that staleness barely matters visually
MAX_PREVIEW_DIM = 220   # cap the grid — big enough that coastlines/gradients read as real,
                         # still a fraction of a full-resolution regional fetch


@router.get("/slice/preview")
async def get_slice_preview(
    request: Request,
    source: str = Query(..., description="Registry source ID"),
    var:    str = Query(..., description="Variable name"),
    time:   int = Query(0, description="Time step index (0-based)"),
):
    registry = request.app.state.registry
    cache    = request.app.state.cache

    try:
        adapter  = registry.get_adapter(source)
        manifest = registry.get_manifest(source)
    except KeyError:
        raise HTTPException(404, f"Unknown source '{source}'")

    bbox_tuple = tuple(manifest.get("default_bbox", [-180, -90, 180, 90]))
    key = f"preview:{source}:{var}:{time}"

    async def compute() -> bytes:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: adapter.get_slice(var, 0.0, time, bbox_tuple)
        )
        data = result.data
        lat_n, lon_n = data.shape
        lat_stride = max(1, lat_n // MAX_PREVIEW_DIM)
        lon_stride = max(1, lon_n // MAX_PREVIEW_DIM)
        coarse = data[::lat_stride, ::lon_stride]

        header = {
            **result.meta.to_header_dict(),
            "depth_actual_m": result.depth_m,
            "time":           result.time_str,
        }
        resp = make_binary_response(header, coarse)
        return resp.body

    raw = await cache.get_or_compute(key, TTL_PREVIEW, compute, metric={
        "kind": "preview", "source": source, "var": var, "bbox": cache.bbox_to_str(bbox_tuple),
    })
    return Response(content=raw, media_type="application/octet-stream")
