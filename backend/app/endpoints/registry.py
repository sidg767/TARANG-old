"""
POST /api/registry/reload   — trigger hot-reload of all YAML manifests
GET  /api/registry/status   — inspect reload count and loaded source IDs

These endpoints are the third hot-reload trigger (§20 Rule 6).
The first two are:
  1. watchdog filesystem watcher (automatic)
  2. SIGHUP signal (POSIX only)

These HTTP endpoints are especially useful during the live SIH demo where
dropping a new YAML into registry/ and hitting POST /api/registry/reload
makes the new layer appear in the frontend layer selector instantly,
demonstrating the "extensible design" without any code changes.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("tarang.endpoint.registry")
router = APIRouter(tags=["registry"])


@router.post("/registry/reload")
async def reload_registry(request: Request):
    """
    Hot-reload all YAML manifests without restarting the container.

    Useful during the live demo to add a new data source:
      1. Drop a new *.yaml into registry/
      2. POST /api/registry/reload   (or just wait 1–2s for watchdog to fire)
      3. The new source appears in GET /api/sources

    Returns summary of loaded source IDs.
    """
    registry = request.app.state.registry

    # Run in a threadpool so the lock-based reload() doesn't block the event loop
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, registry.reload)

    logger.info(f"HTTP reload: {result}")
    return JSONResponse(content=result)


@router.get("/registry/status")
async def registry_status(request: Request):
    """
    Inspect current registry state — useful for debugging during the demo.

    Returns:
      {
        "reload_count": 3,
        "sources": ["hycom_water_temp", "hycom_salinity", ...],
        "watcher_active": true
      }
    """
    registry = request.app.state.registry
    watcher_active = (
        registry._observer is not None
        and registry._observer.is_alive()
        if hasattr(registry, "_observer")
        else False
    )
    return JSONResponse(content={
        "reload_count": registry.reload_count,
        "sources": list(registry.manifest_ids()),
        "watcher_active": watcher_active,
    })
