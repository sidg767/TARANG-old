"""
TARANG — FastAPI Application Entrypoint
SIH 2026 · PS 26067 · MoES/INCOIS

TARANG Backend API

Routers:
  /api/metadata
  /api/slice
  /api/volume
  /api/isosurface
  /api/instruments
  /api/profile
  /api/registry
  /api/copilot

AI Copilot:
  /api/copilot
  Provides Gemini-powered natural-language assistance for TARANG.
"""

import logging
import os
from contextlib import asynccontextmanager

# Loads .env into os.environ if present — docker-compose substitutes .env values itself, but
# nothing did this for a plain `uvicorn backend.app.main:app` run outside Docker, so
# COPERNICUS_USERNAME/PASSWORD (and anything else in .env.example) were silently ignored unless
# exported into the shell by hand. python-dotenv is already a pinned dependency for exactly this.
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.app.cache import RedisCache
from backend.app.db import Database
from backend.app.endpoints import (
    copilot,
    delta,
    derived,
    eddy,
    instruments,
    isosurface,
    metadata,
    metrics,
    ogc,
    preview,
    profile,
    slice_,
    upload,
    volume,
)
from backend.app.endpoints import registry as registry_endpoint
from backend.app.registry.loader import RegistryLoader

logger = logging.getLogger("tarang")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "info").upper()
)


# ─────────────────────────────────────────────────────────────────────────────
# Global services
# ─────────────────────────────────────────────────────────────────────────────

_registry: RegistryLoader | None = None
_cache: RedisCache | None = None
_db: Database | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Application lifespan
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application startup and shutdown lifecycle.

    Startup:
      1. Load YAML registry
      2. Start registry watcher
      3. Connect Redis
      4. Connect PostGIS if DATABASE_URL exists

    Shutdown:
      1. Disconnect Redis
      2. Disconnect PostGIS
      3. Stop registry watcher
    """

    global _registry, _cache, _db

    logger.info("TARANG backend starting up...")

    # ─────────────────────────────────────────────────────────────────────────
    # Load YAML registry
    # ─────────────────────────────────────────────────────────────────────────

    registry_dir = os.getenv(
        "REGISTRY_DIR",
        "registry"
    )

    _registry = RegistryLoader(registry_dir)

    # Retry the cold-start load a few times — on a freshly-started Docker Desktop
    # bind mount the directory can be visible before its files are, which would
    # otherwise boot the API with an empty registry (all dropdowns blank).
    import time as _time
    for _attempt in range(5):
        _registry.load_all()
        if list(_registry.manifest_ids()):
            break
        logger.warning("Registry empty on startup attempt %d — retrying in 1s...", _attempt + 1)
        _time.sleep(1)

    logger.info(
        "Registry loaded: %s plugins",
        list(_registry.manifest_ids())
    )

    app.state.registry = _registry

    # ─────────────────────────────────────────────────────────────────────────
    # Start filesystem watcher
    # ─────────────────────────────────────────────────────────────────────────

    try:
        _registry.start_watcher()

        logger.info(
            "Registry filesystem watcher started"
        )

    except Exception as e:
        logger.warning(
            "Registry watcher could not start: %s",
            e
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Connect Redis
    # ─────────────────────────────────────────────────────────────────────────

    redis_url = os.getenv(
        "REDIS_URL",
        "redis://localhost:6379/0"
    )

    _cache = RedisCache(redis_url)

    try:
        await _cache.connect()

        logger.info(
            "Redis connected successfully"
        )

    except Exception as e:
        logger.warning(
            "Redis unavailable: %s",
            e
        )

    app.state.cache = _cache

    # ─────────────────────────────────────────────────────────────────────────
    # Connect PostGIS
    # ─────────────────────────────────────────────────────────────────────────

    database_url = os.getenv(
        "DATABASE_URL",
        ""
    )

    if database_url:

        try:
            _db = Database(database_url)

            await _db.connect()

            await _db.ensure_schema()

            logger.info(
                "PostGIS connected and schema verified"
            )

        except Exception as e:

            logger.warning(
                "PostGIS connection failed: %s",
                e
            )

            _db = None

    else:

        logger.warning(
            "DATABASE_URL not set — "
            "instrument endpoints will return empty results"
        )

        _db = None

    app.state.db = _db

    # ─────────────────────────────────────────────────────────────────────────
    # AI Copilot (OpenRouter) API status
    # ─────────────────────────────────────────────────────────────────────────

    if os.getenv("OPENROUTER_API_KEY"):

        logger.info(
            "OPENROUTER_API_KEY detected — AI Copilot enabled"
        )

    else:

        logger.warning(
            "OPENROUTER_API_KEY not set — "
            "AI Copilot will not work until the key is configured"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Backend ready
    # ─────────────────────────────────────────────────────────────────────────

    logger.info(
        "TARANG backend ready ✓"
    )

    yield

    # ─────────────────────────────────────────────────────────────────────────
    # Shutdown
    # ─────────────────────────────────────────────────────────────────────────

    logger.info(
        "TARANG backend shutting down..."
    )

    if _cache:

        try:
            await _cache.disconnect()

        except Exception as e:

            logger.warning(
                "Redis shutdown error: %s",
                e
            )

    if _db:

        try:
            await _db.disconnect()

        except Exception as e:

            logger.warning(
                "PostGIS shutdown error: %s",
                e
            )

    if _registry:

        try:
            _registry.stop_watcher()

        except Exception as e:

            logger.warning(
                "Registry watcher shutdown error: %s",
                e
            )

    logger.info(
        "TARANG backend shutdown complete"
    )


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI application
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(

    title="TARANG Ocean Visualization API",

    description=(
        "Backend API for TARANG — "
        "Web-Based Interactive 3D Ocean Visualization Platform. "

        "SIH 2026 · PS 26067 · MoES/INCOIS. "

        "Provides binary-serialized depth-slice, volume, "
        "and isosurface data from HYCOM, "
        "INCOIS-GODAS and Copernicus ocean model output, "
        "plus Argo float profiles and an AI-powered "
        "natural-language Ocean Copilot."
    ),

    version="1.0.0",

    docs_url="/api/docs",

    redoc_url="/api/redoc",

    lifespan=lifespan,
)


# ─────────────────────────────────────────────────────────────────────────────
# CORS
# ─────────────────────────────────────────────────────────────────────────────

app.add_middleware(

    CORSMiddleware,

    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
        "http://localhost",
    ],

    allow_credentials=True,

    allow_methods=[
        "GET",
        "POST",
        "OPTIONS",
    ],

    allow_headers=[
        "*"
    ],
)


# ─────────────────────────────────────────────────────────────────────────────
# Standard TARANG API routers
# ─────────────────────────────────────────────────────────────────────────────

app.include_router(
    metadata.router,
    prefix="/api"
)

app.include_router(
    slice_.router,
    prefix="/api"
)

app.include_router(
    volume.router,
    prefix="/api"
)

app.include_router(
    isosurface.router,
    prefix="/api"
)

app.include_router(
    instruments.router,
    prefix="/api"
)

app.include_router(
    profile.router,
    prefix="/api"
)

app.include_router(
    eddy.router,
    prefix="/api"
)

app.include_router(
    registry_endpoint.router,
    prefix="/api"
)

app.include_router(
    delta.router,
    prefix="/api"
)

app.include_router(
    metrics.router,
    prefix="/api"
)

app.include_router(
    preview.router,
    prefix="/api"
)

app.include_router(
    ogc.router,
    prefix="/api"
)

app.include_router(
    upload.router,
    prefix="/api"
)

app.include_router(
    derived.router,
    prefix="/api"
)


# ─────────────────────────────────────────────────────────────────────────────
# AI COPILOT ROUTER
# ─────────────────────────────────────────────────────────────────────────────

app.include_router(
    copilot.router,
    prefix="/api"
)


# ─────────────────────────────────────────────────────────────────────────────
# Option B — WMS/WCS
# ─────────────────────────────────────────────────────────────────────────────

# Option B: hand-rolled OGC endpoints (PS requirement)
from backend.app.wms_wcs import wcs, wms

app.include_router(
    wms.router,
    prefix="/api"
)

app.include_router(
    wcs.router,
    prefix="/api"
)

logger.info("WMS/WCS endpoints mounted for OGC compliance")


# ─────────────────────────────────────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────────────────────────────────────

@app.get(
    "/health",
    tags=["system"]
)
async def health():
    """
    Backend health check.

    Used by Docker, CI and local development.
    """

    registry_size = 0

    if getattr(
        app.state,
        "registry",
        None
    ):

        registry_size = len(
            list(
                app.state.registry.manifest_ids()
            )
        )

    return JSONResponse({

        "status": "ok",

        "service": "tarang-backend",

        "registry_size": registry_size,

        "ai_copilot": bool(
            os.getenv("OPENROUTER_API_KEY")
        ),

    })


# ─────────────────────────────────────────────────────────────────────────────
# Root endpoint
# ─────────────────────────────────────────────────────────────────────────────

@app.get(
    "/",
    include_in_schema=False
)
async def root():

    return JSONResponse({

        "message": "TARANG API",

        "docs": "/api/docs",

        "health": "/health",

        "copilot": "/api/copilot",

    })