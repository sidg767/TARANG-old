"""
NetCDFAdapter — xarray-based adapter for local NetCDF files and OPeNDAP URLs.

Handles:
  - HYCOM GLBy0.08 (OPeNDAP: tds.hycom.org)
  - INCOIS GODAS (OPeNDAP: las.incois.gov.in)
  - Copernicus Marine (local NetCDF via copernicusmarine download)
  - Any CF-1.6 compliant NetCDF file

Engine priority: netCDF4 > h5netcdf > scipy
DO NOT use PyNIO — unmaintained (§6).

CRITICAL rules enforced here:
  - (§20 Rule 8) Always check local_cache first, fall back to OPeNDAP
  - (§20 Rule 5) Every .sel() is bbox-scoped — never loads a global grid
  - (§8.1) CF metadata extracted once and threaded through
  - (§8.1) depth_levels are non-uniform — snap to nearest, never interpolate
"""

from __future__ import annotations

import logging
import math
import os
import threading
from pathlib import Path

import numpy as np
import xarray as xr

from backend.app.adapters.base import (
    DataSourceAdapter,
    SliceResult,
    VolumeResult,
)

logger = logging.getLogger("tarang.adapters.netcdf")

# copernicusmarine.subset() fetches NetCDF chunks from an S3-compatible backend
# (s3.waw3-1.cloudferro.com) via botocore, which defaults to a 10-connection pool
# (botocore.httpsession.URLLib3Session(max_pool_connections=10)). Its own chunk-download
# concurrency regularly exceeds that, so every live fetch logs a flood of harmless but noisy
# "Connection pool is full, discarding connection" warnings. Widen the default once, here,
# rather than 10-connections-worth of thrash on every region search. Best-effort: if botocore's
# internals ever change shape, this silently no-ops instead of breaking startup.
try:
    import botocore.httpsession as _bc_httpsession

    _orig_urllib3session_init = _bc_httpsession.URLLib3Session.__init__

    def _patched_urllib3session_init(self, *args, **kwargs):
        kwargs.setdefault("max_pool_connections", 50)
        _orig_urllib3session_init(self, *args, **kwargs)

    _bc_httpsession.URLLib3Session.__init__ = _patched_urllib3session_init
except Exception as e:
    logger.debug(f"Could not widen botocore's default connection pool size: {e}")

# Per-(source,variable,bbox) locks so concurrent requests for the SAME live Copernicus fetch
# serialize instead of racing — without this, N layers/depth-levels/time-steps all active at
# once (e.g. slice+volume+eddy+fronts) each independently see cache_file.exists() == False and
# each kick off their own copernicusmarine.subset() call for the identical bbox+variable, at the
# same time. That's genuinely observed in practice: the exact same bbox fetched 3-4x concurrently,
# each contending for the same S3 connection pool ("Connection pool is full, discarding
# connection") — wasteful, slow, and the reason a freshly-picked region could take far longer to
# render than a single fetch should. `_LIVE_FETCH_LOCKS_GUARD` only protects creating a new Lock
# per key without a race; the per-key Lock itself is what actually serializes the fetch.
_live_fetch_locks: dict[str, threading.Lock] = {}
_live_fetch_locks_guard = threading.Lock()

# xarray's CachingFileManager caches the underlying netCDF4/HDF5 file handle by path in a
# process-wide LRU, shared across every open_dataset() call regardless of which adapter instance
# or thread requested it — so "open fresh on every request" (see open()'s docstring) does NOT
# mean a fresh OS-level handle. netCDF4/HDF5 is not thread-safe for concurrent access to that
# shared handle: two request-handling threads reading/closing it at the same time corrupted a
# file ID in practice (`RuntimeError: NetCDF: Not a valid ID`, raised inside a __del__ finalizer
# during GC — severe enough to take the whole process down, not just fail one request).
#
# Scope deliberately excludes copernicusmarine.subset()'s network download (already serialized
# per-bbox by _lock_for, and takes minutes) — holding THIS lock across that would make the whole
# API single-threaded for the duration of any live fetch, even for unrelated already-cached
# regions. Only the actual local file open + array materialization is serialized here.
_NETCDF_IO_LOCK = threading.Lock()


def _lock_for(cache_key: str) -> threading.Lock:
    with _live_fetch_locks_guard:
        if cache_key not in _live_fetch_locks:
            _live_fetch_locks[cache_key] = threading.Lock()
        return _live_fetch_locks[cache_key]


# Matches scripts/warm_ocean_cache.py's tiling exactly (20° boxes, lon from -180, lat from a
# multiple of 20). The region picker's click box is 24°x24° (SceneManager.tsx's PICK_SPAN_DEG),
# WIDER than one grid cell, so a single request routinely spans 2-4 adjacent cells — expanding
# to one bigger cell to "cover" it would create a never-warmed, unpredictably-sized box instead
# of reusing what's already cached. Stitching the covering cells together locally (below) is
# what actually reuses the warmed cache for a real click/drag pick.
_GRID_SIZE_DEG = 20.0


def _grid_cells_covering(bbox: tuple[float, float, float, float]) -> list[tuple[float, float]]:
    """(cell_min_lon, cell_min_lat) for every _GRID_SIZE_DEG cell overlapping bbox."""
    min_lon, min_lat, max_lon, max_lat = bbox
    lon0 = math.floor(min_lon / _GRID_SIZE_DEG) * _GRID_SIZE_DEG
    lat0 = math.floor(min_lat / _GRID_SIZE_DEG) * _GRID_SIZE_DEG
    cells = []
    lon = lon0
    while lon < max_lon:
        lat = lat0
        while lat < max_lat:
            cells.append((lon, lat))
            lat += _GRID_SIZE_DEG
        lon += _GRID_SIZE_DEG
    return cells


def _offline_mode() -> bool:
    """True → never make an outbound internet call."""
    return os.getenv("OFFLINE_MODE", "false").strip().lower() in ("1", "true", "yes", "on")


class NetCDFAdapter(DataSourceAdapter):
    """
    Adapter for CF-compliant NetCDF datasets accessed either:
      1. As a local file  (local_cache path, preferred for demo stability)
      2. Via OPeNDAP URL  (source_url, requires network)

    All subsetting is lazy — xarray .sel() before .compute(), so only the
    requested bytes travel over the wire / are read from disk.
    """

    def __init__(self, manifest: dict):
        super().__init__(manifest)
        self._depth_levels: list[float] = manifest.get("depth_levels") or []
        self._decode_times: bool = manifest.get("decode_times", True)
        self._ds: xr.Dataset | None = None  # lazy-opened dataset

    # ── Internal: does the local cache actually cover this request? ────────────
    def _local_cache_covers(self, bbox: tuple[float, float, float, float] | None) -> bool:
        """
        True if local_cache exists on disk AND either we don't know its extent
        (local_cache_bbox unset — assume global) or the requested bbox SUBSTANTIALLY
        overlaps the cached extent. We use the cache (xarray .sel clips to whatever the
        file actually holds) whenever most of the request is covered — a click-pick box
        that spills a degree past the cached edge should not fall through to a live fetch.
        A genuinely different sea (little/no overlap) still goes live, so a researcher
        searching elsewhere never silently gets this region's data back.
        """
        if not self.local_cache or not Path(self.local_cache).exists():
            return False
        if bbox is None or self.local_cache_bbox is None:
            return True
        min_lon, min_lat, max_lon, max_lat = bbox
        c_min_lon, c_min_lat, c_max_lon, c_max_lat = self.local_cache_bbox

        ix = max(0.0, min(max_lon, c_max_lon) - max(min_lon, c_min_lon))
        iy = max(0.0, min(max_lat, c_max_lat) - max(min_lat, c_min_lat))
        inter = ix * iy
        req = max((max_lon - min_lon) * (max_lat - min_lat), 1e-9)
        return inter / req >= 0.4

    def _open_local_or_configured_url(self, use_local: bool, override_path: str | None = None) -> xr.Dataset:
        source = override_path if override_path is not None else (self.local_cache if use_local else self.source_url)
        if use_local:
            logger.debug(f"Using local cache: {source}")
        logger.info(f"Opening dataset: {source}")

        # Engine priority: netCDF4 > h5netcdf > scipy (§6)
        # Locked — see _NETCDF_IO_LOCK's comment: concurrent opens of the same underlying
        # file handle (xarray caches these process-wide) corrupt netCDF4/HDF5's C-level state.
        with _NETCDF_IO_LOCK:
            for engine in ["netcdf4", "h5netcdf", "scipy"]:
                try:
                    ds = xr.open_dataset(
                        source,
                        engine=engine,
                        mask_and_scale=True,
                        decode_times=self._decode_times,
                        # No chunks parameter, let xarray manage memory without dask
                    )
                    logger.info(f"Opened with engine '{engine}': {list(ds.data_vars)}")
                    return ds
                except Exception as e:
                    logger.debug(f"Engine '{engine}' failed: {e}")
                    continue

        raise RuntimeError(
            f"Could not open dataset '{source}' with any available engine "
            "(netCDF4, h5netcdf, scipy). Check installation of netCDF4>=1.6."
        )

    def _find_covering_cache(
        self, cache_dir: Path, bbox: tuple[float, float, float, float]
    ) -> Path | None:
        """Smallest live_cache/*.nc file for this variable whose own bbox fully contains `bbox`."""
        if not cache_dir.exists():
            return None
        min_lon, min_lat, max_lon, max_lat = bbox
        prefix = f"live_{self.variable}_"
        eps = 0.05
        best: tuple[Path, float] | None = None
        for f in cache_dir.glob(f"{prefix}*.nc"):
            parts = f.stem[len(prefix):].split("_")
            if len(parts) != 4:
                continue
            try:
                f_lo, f_la, f_Lo, f_La = (float(p) for p in parts)
            except ValueError:
                continue
            if (f_lo - eps <= min_lon and max_lon <= f_Lo + eps
                    and f_la - eps <= min_lat and max_lat <= f_La + eps):
                area = (f_Lo - f_lo) * (f_La - f_la)
                if best is None or area < best[1]:
                    best = (f, area)
        return best[0] if best else None

    def _cell_cache_file(self, cache_dir: Path, cell: tuple[float, float]) -> Path:
        clon, clat = cell
        key = f"live_{self.variable}_{clon:.1f}_{clat:.1f}_{clon + _GRID_SIZE_DEG:.1f}_{clat + _GRID_SIZE_DEG:.1f}"
        return cache_dir / f"{key}.nc"

    def _open_live_copernicus(
        self, bbox: tuple[float, float, float, float], mode: str = "live", allow_fetch: bool = True
    ) -> xr.Dataset:
        """Bbox-scoped fetch from Copernicus Marine via subset() (far faster than lazy zarr here).

        mode="cached" → read pre-warmed cells over local HTTP byte-range (the B2 landing-page path).
        allow_fetch=False → cache-only: reuse anything already in live_cache/ (grid-cell stitch or
        an exact-bbox file), but raise instead of hitting the network. Used by OFFLINE_MODE so a
        region fetched in a previous online session keeps working with no connectivity.
        """
        from datetime import datetime, timedelta

        if mode == "cached":
            # Direct HTTP Byte-Range Streaming via xarray + h5netcdf
            covering_cells = _grid_cells_covering(bbox)
            datasets = []
            with _NETCDF_IO_LOCK:
                for c in covering_cells:
                    clon, clat = c
                    key = f"live_{self.variable}_{clon:.1f}_{clat:.1f}_{clon + _GRID_SIZE_DEG:.1f}_{clat + _GRID_SIZE_DEG:.1f}"
                    url = f"http://127.0.0.1:8080/{key}.nc"
                    logger.info(f"Opening super-fast cached B2 data over HTTP: {url}")
                    ds = xr.open_dataset(url, engine="h5netcdf", mask_and_scale=True, decode_times=self._decode_times)
                    datasets.append(ds)
            if len(datasets) == 1:
                return datasets[0]
            return xr.combine_by_coords(datasets, combine_attrs="override")

        cache_dir = Path("data/netcdf/live_cache")
        cache_dir.mkdir(parents=True, exist_ok=True)

        # If every _GRID_SIZE_DEG cell overlapping this bbox is already warmed on disk, stitch
        # them together locally — zero network call. A click pick is 24°x24° (wider than one
        # 20° cell — see _grid_cells_covering's comment), so this is the common case once the
        # globe has been warmed, not an edge case.
        covering_cells = _grid_cells_covering(bbox)
        cell_files = [self._cell_cache_file(cache_dir, c) for c in covering_cells]
        if cell_files and all(f.exists() for f in cell_files):
            if len(cell_files) == 1:
                logger.info(f"Using cached live fetch: {cell_files[0]}")
                return self._open_local_or_configured_url(use_local=False, override_path=str(cell_files[0]))
            logger.info(
                f"Stitching {len(cell_files)} pre-warmed grid cells locally for bbox={bbox}: "
                f"{[f.name for f in cell_files]}"
            )
            datasets = [
                self._open_local_or_configured_url(use_local=False, override_path=str(f))
                for f in cell_files
            ]
            return xr.combine_by_coords(datasets, combine_attrs="override")

        min_lon, min_lat, max_lon, max_lat = bbox

        # Any single cached file that fully CONTAINS this bbox works too — get_slice/get_volume
        # will .sel() the sub-region out of it. This is what makes a small drag/click inside an
        # already-fetched sea (e.g. a corner of the cached Arabian Sea box) resolve instantly
        # offline instead of falling back to the wrong fixture.
        covering = self._find_covering_cache(cache_dir, bbox)
        if covering is not None:
            logger.info(f"Using cached live fetch covering bbox={bbox}: {covering.name}")
            return self._open_local_or_configured_url(use_local=False, override_path=str(covering))

        # Not fully covered by warmed cells — fetch exactly the requested bbox live (no
        # expansion/snapping: a mis-sized novel cell wouldn't be reusable later anyway, so there's
        # no benefit to fetching more than what was actually asked for).
        # Cache by rounded bbox + variable so re-querying the same region reuses the file.
        cache_key = f"live_{self.variable}_{min_lon:.1f}_{min_lat:.1f}_{max_lon:.1f}_{max_lat:.1f}"
        cache_file = cache_dir / f"{cache_key}.nc"

        if cache_file.exists():
            logger.info(f"Using cached live fetch: {cache_file}")
            return self._open_local_or_configured_url(use_local=False, override_path=str(cache_file))

        # Serialize concurrent requests for this exact bbox+variable — see _lock_for's comment.
        # Whichever caller gets the lock first does the real fetch; everyone else blocks here,
        # then (now that the first caller finished) finds cache_file already on disk and just
        # opens it, instead of every caller racing to independently re-download the same subset.
        if not allow_fetch:
            raise FileNotFoundError(
                f"OFFLINE_MODE: no cached live data covering bbox={bbox} for '{self.variable}'"
            )

        import copernicusmarine

        with _lock_for(cache_key):
            if cache_file.exists():   # someone else finished the fetch while we were waiting
                logger.info(f"Using cached live fetch (fetched while waiting): {cache_file}")
                return self._open_local_or_configured_url(use_local=False, override_path=str(cache_file))

            logger.info(
                f"local_cache doesn't cover bbox={bbox}; fetching live from Copernicus Marine "
                f"dataset '{self.live_dataset_id}' -> {cache_file}"
            )
            end = datetime.utcnow()
            # Kept small (0-100m, 1 day) to minimize live-fetch bandwidth/disk — see
            # maximum_depth's comment above for the same trade-off applied to depth. This
            # dataset (…P1D-m = daily mean) means 1 day = exactly 1 time step, so a freshly
            # live-fetched region has NO time-scrubbing range until re-fetched wider; get_slice's
            # time_idx clamp (below) turns an out-of-range scrub into "use the only step
            # available" + a warning, instead of a hard IndexError.
            start = end - timedelta(days=1)
            copernicusmarine.subset(
                dataset_id=self.live_dataset_id,
                variables=[self.variable],
                minimum_longitude=min_lon,
                maximum_longitude=max_lon,
                minimum_latitude=min_lat,
                maximum_latitude=max_lat,
                # Datasets' shallowest level is rarely exactly 0m (e.g. one in use here starts at
                # 0.494m), and that minimum varies per dataset — requesting 0 is harmless
                # (copernicusmarine just clips to the actual shallowest level) but logs a "subset
                # selection exceeds dataset coordinates" warning on every single fetch.
                # minimum_depth=None defaults to the dataset's own native minimum, which selects
                # that same shallowest level without the noise, dataset-agnostically.
                #
                # maximum_depth=100 (not 1000): Copernicus samples depth densely near the surface
                # — verified directly against a real fetch, 22 of this dataset's 35 levels are
                # already <=100m — so this trims ~35% of the download/disk size per box, on top
                # of not paying for the deep levels most surface-gradient searches never look at.
                # Trade-off (accepted): a region cached under this limit only has data down to
                # ~100m. A depth-slider pick or Volume/Isosurface render below that will silently
                # snap to the deepest level actually present in the cached file (xarray's
                # `.sel(method="nearest")` in get_slice/get_volume) instead of the real deep
                # value, until that bbox is one day live-fetched again (e.g. after this file's
                # cache entry is manually cleared) with a deeper range.
                minimum_depth=None,
                maximum_depth=100,
                start_datetime=start.strftime("%Y-%m-%dT00:00:00"),
                end_datetime=end.strftime("%Y-%m-%dT00:00:00"),
                output_filename=cache_file.name,
                output_directory=str(cache_dir),
                username=os.environ.get("COPERNICUS_USERNAME"),
                password=os.environ.get("COPERNICUS_PASSWORD"),
                overwrite=True,
            )
        return self._open_local_or_configured_url(use_local=False, override_path=str(cache_file))

    def open(self, bbox: tuple[float, float, float, float] | None = None, mode: str = "live") -> xr.Dataset:
        """
        Open the data source lazily. Must use the local_cache path if it exists AND covers
        `bbox` (§20 Rule 8 — cache before you query live). Falls back to a live, bbox-scoped
        fetch (via live_dataset_id) or source_url otherwise.
        Returns an xarray Dataset opened lazily (no data pulled yet).
        """
        if mode == "cached" and bbox is not None:
            # Bypass all local checks and go straight to the B2 HTTP server
            return self._open_live_copernicus(bbox, mode="cached")

        if self._local_cache_covers(bbox):
            return self._open_local_or_configured_url(use_local=True)

        # OFFLINE_MODE: no outbound calls. Still prefer bbox-scoped data already on disk from a
        # previous online session (live_cache/) — "offline" means no network, not "throw away
        # data we already have". Only then fall back to the (BoB-only) local_cache fixture, and
        # finally source_url (the in-cluster THREDDS).
        if _offline_mode():
            if self.live_dataset_id and bbox is not None:
                try:
                    return self._open_live_copernicus(bbox, allow_fetch=False)
                except Exception as e:
                    logger.info(f"OFFLINE_MODE: {e}; falling back to local_cache")
            if self.local_cache and Path(self.local_cache).exists():
                logger.info(f"OFFLINE_MODE: serving cached '{self.local_cache}' for bbox {bbox}")
                return self._open_local_or_configured_url(use_local=True)
            logger.warning(f"OFFLINE_MODE: no local cache; using {self.source_url}")
            return self._open_local_or_configured_url(use_local=False)

        if self.live_dataset_id and bbox is not None:
            try:
                return self._open_live_copernicus(bbox)
            except Exception as e:
                logger.warning(f"Live Copernicus fetch failed ({e}); falling back to source_url")
        logger.warning(
            f"Local cache not found/doesn't cover bbox at '{self.local_cache}'. "
            f"Falling back to configured source: {self.source_url}. "
        )
        return self._open_local_or_configured_url(use_local=False)

    def get_metadata(self) -> dict:
        """
        Return metadata dict driving all frontend selectors.
        CF metadata is sourced from the dataset — never hardcoded.

        IMPORTANT: Always prefer the local cache for metadata. Metadata (variable
        names, depth levels, units, CF attributes) is identical across all regions
        for a given dataset — there's no reason to open a massive global OPeNDAP
        endpoint just to read variable attributes. The local cache is fast (0.16s)
        vs the global OPeNDAP which can take 12+ seconds just to open lazily.
        """
        # Prefer local cache for metadata — it's fast and has the same CF attrs
        if self.local_cache and Path(self.local_cache).exists():
            ds = self._open_local_or_configured_url(use_local=True)
        else:
            ds = self.open()  # fallback to configured source

        with _NETCDF_IO_LOCK:
            try:
                # Gather available variables (skip coordinate variables)
                available_vars = []
                cf_meta = {}
                for vname in ds.data_vars:
                    var = ds[vname]
                    if var.attrs.get("standard_name") or var.attrs.get("long_name"):
                        available_vars.append(vname)
                        cf_meta[vname] = {
                            "standard_name": var.attrs.get("standard_name", vname),
                            "long_name":     var.attrs.get("long_name", vname),
                            "units":         var.attrs.get("units", "unknown"),
                            "valid_min":     float(var.attrs.get("valid_min", -9999)),
                            "valid_max":     float(var.attrs.get("valid_max",  9999)),
                            # xarray's mask_and_scale=True decode moves _FillValue out of .attrs into
                            # .encoding — see the identical fix/comment in base.py's _extract_cf_meta,
                            # which this duplicated (and inherited the same bug from).
                            "missing_value": float(var.attrs.get("_FillValue", var.encoding.get("_FillValue", np.nan))),
                        }

                # Resolve actual depth levels from dataset, fallback to manifest
                depth_levels = self._resolve_depth_levels(ds)

                # Time range
                time_range = {}
                if "time" in ds.coords:
                    times = ds.coords["time"].values
                    time_range = {
                        "start": str(times[0]),
                        "end":   str(times[-1]),
                        "steps": len(times),
                    }

                return {
                    "source_id":          self.manifest["id"],
                    "label":              self.manifest.get("label", self.manifest["id"]),
                    "available_variables": available_vars,
                    "cf_metadata":        cf_meta,
                    "depth_levels":       depth_levels,  # non-uniform, explicit list
                    "time_range":         time_range,
                    "dimensions": {k: int(v) for k, v in ds.sizes.items()},
                }
            finally:
                ds.close()

    def get_slice(
        self,
        variable: str,
        depth_m: float,
        time_idx: int,
        bbox: tuple[float, float, float, float],
        mode: str = "live",
    ) -> SliceResult:
        """
        Fetch a 2D (lat, lon) depth-slice at the nearest actual depth level.
        ALL subsetting is done before .compute() — never loads the full grid.
        """
        ds = self.open(bbox, mode=mode)   # not locked — may trigger a slow live fetch; see _NETCDF_IO_LOCK's comment
        min_lon, min_lat, max_lon, max_lat = bbox

        with _NETCDF_IO_LOCK:
            try:
                lat_dim = "latitude" if "latitude" in ds.dims else "lat"
                lon_dim = "longitude" if "longitude" in ds.dims else "lon"
                # ── Subset to bbox first (smallest possible read) ──────────────────────
                if variable not in ds.variables:
                    variable = self.manifest.get("variable", list(ds.variables.keys())[0])
                subset = ds[variable].sel(**{
                    lat_dim: slice(min_lat, max_lat),
                    lon_dim: slice(min_lon, max_lon),
                })

                # ── Select time step ──────────────────────────────────────────────────
                if "time" in subset.dims:
                    # Live-fetched windows are now only 1 day wide (see _open_live_copernicus's
                    # comment) — often exactly 1 time step. A frontend scrub to an index beyond
                    # what's actually cached would otherwise raise a hard IndexError; clamp to the
                    # last available step instead, same spirit as the depth snap below.
                    n_time = subset.sizes["time"]
                    if time_idx >= n_time:
                        logger.warning(
                            f"get_slice: requested time_idx={time_idx} out of range "
                            f"(only {n_time} step(s) available in this cached window) — "
                            f"using the last available step instead."
                        )
                        time_idx = n_time - 1
                    subset = subset.isel(time=time_idx)
                    time_str = str(ds.coords["time"].values[time_idx])
                else:
                    time_str = "static"

                # ── Snap to nearest actual depth level (NON-UNIFORM — §8.1) ──────────
                actual_depth_m = depth_m
                if "depth" in subset.dims or "lev" in subset.dims:
                    depth_dim = "depth" if "depth" in subset.dims else "lev"
                    subset = subset.sel({depth_dim: depth_m}, method="nearest")
                    actual_depth_m = float(subset.coords[depth_dim].values)
                    # A live-fetched cache file only goes as deep as maximum_depth requested at
                    # fetch time (currently 100m — see _open_live_copernicus's comment). A big snap
                    # gap here means the requested depth wasn't actually available and this is
                    # silently returning the shallowest cached level instead — worth knowing about.
                    if abs(actual_depth_m - depth_m) > 50:
                        logger.warning(
                            f"get_slice: requested depth={depth_m}m snapped to {actual_depth_m}m "
                            f"(gap={abs(actual_depth_m - depth_m):.0f}m) — the cached file for this "
                            f"region likely doesn't extend that deep."
                        )

                # ── Build CF metadata ─────────────────────────────────────────────────
                depth_levels = self._resolve_depth_levels(ds)
                meta = self._extract_cf_meta(ds, variable, depth_levels)

                # ── Compute (pulls only the subset bytes) and replace NaNs ────────────
                arr = subset.values.astype(np.float32)
                # mask_and_scale=True replaces missing data with NaN. We must convert it back
                # to a numerical value so WebGL can correctly compare and discard land pixels.
                arr = np.nan_to_num(arr, nan=meta.missing_value)
                meta.bounds = {
                    "lat": [float(min_lat), float(max_lat)],
                    "lon": [float(min_lon), float(max_lon)],
                    "depth": [float(actual_depth_m)],
                }

                return SliceResult(
                    data=arr,
                    meta=meta,
                    lat=ds.coords[lat_dim].sel(**{lat_dim: slice(min_lat, max_lat)}).values.astype(np.float32),
                    lon=ds.coords[lon_dim].sel(**{lon_dim: slice(min_lon, max_lon)}).values.astype(np.float32),
                    depth_m=actual_depth_m,
                    time_str=time_str,
                )
            finally:
                ds.close()

    def get_volume(
        self,
        variable: str,
        time_idx: int,
        bbox: tuple[float, float, float, float],
        mode: str = "live",
    ) -> VolumeResult:
        """
        Fetch the full depth column as 3D (depth, lat, lon) for raymarching.
        This is the largest payload — cache aggressively in Redis.
        Downsamples if the regional cube exceeds GPU-safe resolution limits.
        """
        ds = self.open(bbox, mode=mode)   # not locked — may trigger a slow live fetch; see _NETCDF_IO_LOCK's comment
        min_lon, min_lat, max_lon, max_lat = bbox

        with _NETCDF_IO_LOCK:
            try:
                lat_dim = "latitude" if "latitude" in ds.dims else "lat"
                lon_dim = "longitude" if "longitude" in ds.dims else "lon"
                # ── bbox subset first ─────────────────────────────────────────────────
                if variable not in ds.variables:
                    variable = self.manifest.get("variable", list(ds.variables.keys())[0])
                subset = ds[variable].sel(**{
                    lat_dim: slice(min_lat, max_lat),
                    lon_dim: slice(min_lon, max_lon),
                })

                # ── time step ─────────────────────────────────────────────────────────
                if "time" in subset.dims:
                    # See get_slice's identical comment — 1-day live windows may have only 1 step.
                    n_time = subset.sizes["time"]
                    if time_idx >= n_time:
                        logger.warning(
                            f"get_volume: requested time_idx={time_idx} out of range "
                            f"(only {n_time} step(s) available in this cached window) — "
                            f"using the last available step instead."
                        )
                        time_idx = n_time - 1
                    subset = subset.isel(time=time_idx)
                    time_str = str(ds.coords["time"].values[time_idx])
                else:
                    time_str = "static"

                # ── Compute ───────────────────────────────────────────────────────────
                arr = subset.values.astype(np.float32)  # (depth, lat, lon)

                # Replace NaNs with missing value so WebGL textures don't break/bleed
                depth_levels = self._resolve_depth_levels(ds)
                meta = self._extract_cf_meta(ds, variable, depth_levels)
                arr = np.nan_to_num(arr, nan=meta.missing_value)

                # ── GPU safety: downsample if too large ───────────────────────────────
                # Target: max 64 * 256 * 256 floats ≈ 4M samples for safe WebGL texture
                MAX_GPU_SAMPLES = 64 * 256 * 256
                if arr.size > MAX_GPU_SAMPLES:
                    arr = self._downsample_volume(arr, MAX_GPU_SAMPLES)
                    logger.info(f"Volume downsampled to shape {arr.shape} for GPU safety")

                meta.bounds = {
                    "lat":   [float(min_lat), float(max_lat)],
                    "lon":   [float(min_lon), float(max_lon)],
                    "depth": [float(depth_levels[0]), float(depth_levels[-1])] if depth_levels else [],
                }

                return VolumeResult(
                    data=arr,
                    meta=meta,
                    lat=ds.coords[lat_dim].sel(**{lat_dim: slice(min_lat, max_lat)}).values.astype(np.float32),
                    lon=ds.coords[lon_dim].sel(**{lon_dim: slice(min_lon, max_lon)}).values.astype(np.float32),
                    time_str=time_str,
                )
            finally:
                ds.close()

    def get_profile_at(
        self,
        variable: str,
        lat: float,
        lon: float,
        time_idx: int = 0
    ) -> tuple[np.ndarray, np.ndarray]:
        """
        Extract a vertical profile at the nearest grid cell to (lat, lon).
        Returns (depths, values) as 1D numpy arrays.
        """
        ds = self.open(None)   # not locked — may trigger a slow live fetch; see _NETCDF_IO_LOCK's comment

        with _NETCDF_IO_LOCK:
            try:
                lat_dim = "latitude" if "latitude" in ds.dims else "lat"
                lon_dim = "longitude" if "longitude" in ds.dims else "lon"

                subset = ds[variable].sel(**{
                    lat_dim: lat,
                    lon_dim: lon
                }, method="nearest")

                if "time" in subset.dims:
                    n_time = subset.sizes["time"]
                    if time_idx >= n_time:
                        logger.warning(
                            f"get_profile_at: requested time_idx={time_idx} out of range "
                            f"(only {n_time} step(s) available in this cached window) — "
                            f"using the last available step instead."
                        )
                        time_idx = n_time - 1
                    subset = subset.isel(time=time_idx)

                depth_levels = self._resolve_depth_levels(ds)
                depth_arr = np.array(depth_levels, dtype=np.float32)

                arr = subset.values.astype(np.float32)
                meta = self._extract_cf_meta(ds, variable, depth_levels)
                arr = np.nan_to_num(arr, nan=meta.missing_value)

                return depth_arr, arr
            finally:
                ds.close()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _resolve_depth_levels(self, ds: xr.Dataset) -> list[float]:
        """
        Return actual depth levels from the dataset (preferred) or the manifest.
        Never assume uniform spacing.
        """
        for dim in ("depth", "lev", "z"):
            if dim in ds.coords:
                return [float(v) for v in ds.coords[dim].values]
        # Fallback: use manifest depth_levels
        return self._depth_levels

    @staticmethod
    def _downsample_volume(arr: np.ndarray, max_samples: int) -> np.ndarray:
        """
        Uniform downsampling of a 3D (depth, lat, lon) array to fit GPU limit.
        Uses stride-based slicing — no interpolation, fast, deterministic.
        """
        d, la, lo = arr.shape
        total = d * la * lo
        if total <= max_samples:
            return arr
        # Find the largest stride s such that (d/s)*(la/s)*(lo/s) <= max_samples
        s = 1
        while (d // (s + 1)) * (la // (s + 1)) * (lo // (s + 1)) > max_samples:
            s += 1
        return arr[::s, ::s, ::s]
