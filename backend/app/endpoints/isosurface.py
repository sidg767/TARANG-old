"""
GET /api/isosurface?source=&var=&threshold=&time=&bbox=

Runs skimage.measure.marching_cubes server-side.
Returns verts (Float32Array) + faces (Uint32Array) + normals (Float32Array)
as a multipart binary payload for direct upload to THREE.BufferGeometry.

Algorithm: Lewiner et al. (2003) — method='lewiner' is the default in scikit-image.
Faster, resolves topological ambiguity, returns verts/faces/normals/values.
(§8.5, §6)
"""

from __future__ import annotations

import asyncio
import logging
import struct

import numpy as np
import orjson
from fastapi import APIRouter, HTTPException, Query, Request, Response

from backend.app.cache import TTL_ISOSURFACE
from backend.app.endpoints.binary import parse_bbox

logger = logging.getLogger("tarang.endpoint.isosurface")
router = APIRouter(tags=["data"])


def _build_isosurface_binary(
    verts: np.ndarray,
    faces: np.ndarray,
    normals: np.ndarray,
    header: dict
) -> bytes:
    """
    Binary layout (§8.5):
      [4 bytes: header_len]
      [header_len bytes: JSON header]
      [verts: float32, shape (N,3)]
      [normals: float32, shape (N,3)]
      [faces: uint32, shape (M,3)]

    Header contains:
      n_verts, n_faces, variable, units, threshold, time, ...
    """
    header["n_verts"]  = len(verts)
    header["n_faces"]  = len(faces)
    header["dtype_verts"]  = "float32"
    header["dtype_faces"]  = "uint32"

    header_bytes = orjson.dumps(header)
    header_len   = struct.pack("<I", len(header_bytes))

    verts_bytes   = verts.astype(np.float32).tobytes()
    normals_bytes = normals.astype(np.float32).tobytes()
    faces_bytes   = faces.astype(np.uint32).tobytes()

    return header_len + header_bytes + verts_bytes + normals_bytes + faces_bytes


@router.get("/isosurface")
async def get_isosurface(
    request:   Request,
    source:    str   = Query(...),
    var:       str   = Query(...),
    threshold: float = Query(..., description="Isosurface level in variable's units"),
    time:      int   = Query(0),
    bbox:      str   = Query("80,5,100,25"),
    mode:      str   = Query("live"),
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

    key = cache.isosurface_key(source, var, threshold, time, bbox_tuple, mode)

    async def compute() -> bytes:
        from skimage import measure

        from backend.app.endpoints.volume import _data_executor

        loop = asyncio.get_running_loop()

        # 1. Fetch volume (checks its own cache)
        vol_result = await loop.run_in_executor(
            _data_executor,
            lambda: adapter.get_volume(var, time, bbox_tuple, mode)
        )
        volume = vol_result.data  # (depth, lat, lon) float32

        # 2. Run marching cubes (CPU-bound — thread pool)
        def run_marching_cubes():
            # Replace NaN/fill values with threshold-1 so they're outside the surface
            vol_clean = np.where(np.isfinite(volume), volume, threshold - 1)
            try:
                verts, faces, normals, _ = measure.marching_cubes(
                    vol_clean,
                    level=threshold,
                    method="lewiner",     # Lewiner et al. (2003) — faster, topologically correct
                    allow_degenerate=False,
                )
            except ValueError as e:
                logger.warning(f"marching_cubes returned empty result: {e}")
                return (
                    np.zeros((0, 3), dtype=np.float32),
                    np.zeros((0, 3), dtype=np.uint32),
                    np.zeros((0, 3), dtype=np.float32),
                )
            return verts, faces, normals

        verts, faces, normals = await loop.run_in_executor(None, run_marching_cubes)

        header = {
            **vol_result.meta.to_header_dict(),
            "threshold": threshold,
            "time": vol_result.time_str,
            # marching_cubes verts are in VOXEL INDEX space (0..shape[i]-1 per axis) — the
            # frontend needs the actual array shape it ran on (which may be downsampled from
            # the raw depth_levels count, see NetCDFAdapter._downsample_volume) to scale verts
            # back into real degree/metre space.
            "volume_shape": list(volume.shape),  # (depth, lat, lon)
        }
        return _build_isosurface_binary(verts, faces, normals, header)

    raw = await cache.get_or_compute(key, TTL_ISOSURFACE, compute, metric={
        "kind": "isosurface", "source": source, "var": var, "bbox": bbox,
    })
    return Response(content=raw, media_type="application/octet-stream")
