"""
Redis Cache Layer

Wraps redis-py with a simple get_or_compute(key, ttl, fn) pattern.
Used by every endpoint to cache hot slices so timeline scrubbing feels instant.

Cache key conventions:
  slice:    slice:{source_id}:{var}:{depth_m}:{time_idx}:{bbox_str}
  volume:   volume:{source_id}:{var}:{time_idx}:{bbox_str}
  iso:      iso:{source_id}:{var}:{threshold}:{time_idx}:{bbox_str}
  meta:     meta:{source_id}

TTLs:
  slice     300 s  (5 min — hot during active scrubbing)
  volume    600 s  (10 min — large payload, hold longer)
  isosurface 120 s (recomputed often when threshold changes)
  metadata  3600 s (1 hour — rarely changes)
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable

import redis.asyncio as aioredis

logger = logging.getLogger("tarang.cache")

TTL_SLICE      = 300
TTL_VOLUME     = 600
TTL_ISOSURFACE = 120
TTL_METADATA   = 3600

# Hash holding "last updated" metrics for every region/source/var combination ever fetched.
# Unlike the data caches above (which expire in minutes), entries here persist indefinitely so
# the frontend can show "last updated" even for a region whose data cache has since expired.
METRICS_HASH = "tarang:metrics:last_updated"


class RedisCache:

    def __init__(self, redis_url: str):
        self._url = redis_url
        self._client: aioredis.Redis | None = None
        # In-memory fallback so metrics still work when Redis is down/unset — same reasoning
        # as the rest of this class degrading gracefully rather than failing requests.
        self._metrics_mem: dict[str, dict] = {}

    async def connect(self) -> None:
        if not self._url:
            logger.warning("REDIS_URL not set — caching disabled, every request recomputes")
            return

        self._client = aioredis.from_url(
            self._url,
            encoding="utf-8",
            decode_responses=False,  # binary — our payloads are raw bytes
        )
        await self._client.ping()
        logger.info(f"Redis connected: {self._url}")

    async def disconnect(self) -> None:
        if self._client:
            await self._client.aclose()

    async def get_bytes(self, key: str) -> bytes | None:
        """Return cached bytes or None if cache miss."""
        if not self._client:
            return None
        try:
            return await self._client.get(key)
        except Exception as e:
            logger.warning(f"Redis GET failed for key '{key}': {e}")
            return None

    async def set_bytes(self, key: str, value: bytes, ttl: int) -> None:
        """Cache raw bytes with a TTL (seconds)."""
        if not self._client:
            return
        try:
            await self._client.setex(key, ttl, value)
        except Exception as e:
            logger.warning(f"Redis SET failed for key '{key}': {e}")

    async def get_or_compute(
        self,
        key: str,
        ttl: int,
        compute_fn: Callable[[], Awaitable[bytes]],
        metric: dict | None = None,
    ) -> bytes:
        """
        Cache-aside pattern:
          1. Check Redis for key
          2. On miss: call compute_fn(), cache result, return it
          3. On hit: return cached bytes directly

        If `metric` is given (e.g. {"kind": "slice", "source": ..., "var": ..., "bbox": ...}),
        records a "last updated" entry for it — see record_metric().
        """
        start = time.monotonic()
        cached = await self.get_bytes(key)
        if cached is not None:
            logger.debug(f"Cache HIT: {key}")
            if metric is not None:
                await self.record_metric(key, metric, cache_hit=True, duration_ms=(time.monotonic() - start) * 1000)
            return cached

        logger.debug(f"Cache MISS: {key} — computing...")
        result = await compute_fn()
        await self.set_bytes(key, result, ttl)
        if metric is not None:
            await self.record_metric(key, metric, cache_hit=False, duration_ms=(time.monotonic() - start) * 1000)
        return result

    async def record_metric(self, key: str, metric: dict, cache_hit: bool, duration_ms: float) -> None:
        """Record when a region/source/var was last fetched, for the frontend's cache-status panel."""
        entry = {**metric, "key": key, "cache_hit": cache_hit, "duration_ms": round(duration_ms, 1), "updated_at": time.time()}
        self._metrics_mem[key] = entry
        if not self._client:
            return
        try:
            await self._client.hset(METRICS_HASH, key, json.dumps(entry))
        except Exception as e:
            logger.warning(f"Redis metric record failed for '{key}': {e}")

    async def get_all_metrics(self) -> list[dict]:
        """All recorded 'last updated' entries, newest first."""
        entries: dict[str, dict] = dict(self._metrics_mem)
        if self._client:
            try:
                raw = await self._client.hgetall(METRICS_HASH)
                for k, v in raw.items():
                    key = k.decode() if isinstance(k, bytes) else k
                    entries[key] = json.loads(v)
            except Exception as e:
                logger.warning(f"Redis metrics fetch failed: {e}")
        return sorted(entries.values(), key=lambda e: e["updated_at"], reverse=True)

    @staticmethod
    def bbox_to_str(bbox: tuple) -> str:
        """Normalise bbox to a cache-key-safe string."""
        return f"{bbox[0]:.2f}_{bbox[1]:.2f}_{bbox[2]:.2f}_{bbox[3]:.2f}"

    def slice_key(self, source_id: str, var: str, depth_m: float, time_idx: int, bbox: tuple, mode: str = "live") -> str:
        return f"slice:{source_id}:{var}:{depth_m:.1f}:{time_idx}:{self.bbox_to_str(bbox)}:{mode}"

    def volume_key(self, source_id: str, var: str, time_idx: int, bbox: tuple, mode: str = "live") -> str:
        return f"volume:{source_id}:{var}:{time_idx}:{self.bbox_to_str(bbox)}:{mode}"

    def isosurface_key(self, source_id: str, var: str, threshold: float, time_idx: int, bbox: tuple, mode: str = "live") -> str:
        return f"iso:{source_id}:{var}:{threshold:.4f}:{time_idx}:{self.bbox_to_str(bbox)}:{mode}"

    def metadata_key(self, source_id: str) -> str:
        return f"meta:{source_id}"
