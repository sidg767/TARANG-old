"""
GET /api/metrics/last-updated

Returns "last updated" bookkeeping for every region/source/var combination the backend has
ever fetched (slice, volume, isosurface) — persists across data-cache expiry (see
cache.py::METRICS_HASH) so the frontend can show a real timestamp/cache-hit status instead of
guessing why a request is slow or fast.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(tags=["system"])


@router.get("/metrics/last-updated")
async def get_last_updated(request: Request):
    cache = request.app.state.cache
    entries = await cache.get_all_metrics()
    return {"entries": entries}
