"""
Binary response helper shared across slice/volume/isosurface endpoints.

Wire format (§8.6):
  ┌─────────────────────────────────────────┐
  │  4 bytes: header length (uint32, LE)    │
  │  N bytes: JSON header (UTF-8)           │
  │  M bytes: raw Float32Array body (LE)    │
  └─────────────────────────────────────────┘

This is the MOST IMPORTANT performance decision in the backend.
A (40, 850, 1500) float32 volume is ~200 MB raw; ~600-1000 MB as JSON.
NEVER return numeric grids as JSON arrays. (§8.6, §20 Rule 3)
"""

from __future__ import annotations

import struct

import numpy as np
import orjson
from fastapi import Response

CONTENT_TYPE = "application/octet-stream"


def make_binary_response(header: dict, data: np.ndarray) -> Response:
    """
    Build a binary multipart HTTP response:
      [4-byte header_len][JSON header bytes][float32 body bytes]

    The frontend reads:
      1. First 4 bytes → header_len (uint32 LE)
      2. Next header_len bytes → JSON.parse() → metadata (shape, units, depth_levels, ...)
      3. Remaining bytes → new Float32Array(buffer) → GPU texture

    Args:
        header: dict conforming to §8.6 (shape, dtype, variable, units, ...)
        data:   numpy ndarray — will be converted to float32 and serialised
    """
    # Always float32 — required by Three.js DataTexture/Data3DTexture
    arr = data.astype(np.float32)

    # Inject shape into header (never trust caller to pre-fill this)
    header["shape"] = list(arr.shape)
    header["dtype"] = "float32"

    # Serialise header with orjson (fast, handles numpy types)
    header_bytes: bytes = orjson.dumps(header)
    header_len = struct.pack("<I", len(header_bytes))  # uint32 little-endian

    # Raw body — C-contiguous float32, row-major
    body_bytes: bytes = arr.tobytes(order="C")

    payload = header_len + header_bytes + body_bytes
    return Response(content=payload, media_type=CONTENT_TYPE)


def parse_bbox(bbox_str: str) -> tuple[float, float, float, float]:
    """
    Parse a bbox query param "minLon,minLat,maxLon,maxLat" → tuple.
    Validates range (§20 Rule 5 — bbox required on every request).
    """
    try:
        parts = [float(x) for x in bbox_str.split(",")]
        if len(parts) != 4:
            raise ValueError
        min_lon, min_lat, max_lon, max_lat = parts
        if min_lon >= max_lon or min_lat >= max_lat:
            raise ValueError("bbox min must be < max")
        return min_lon, min_lat, max_lon, max_lat
    except (ValueError, TypeError) as e:
        raise ValueError(
            f"Invalid bbox '{bbox_str}'. Expected 'minLon,minLat,maxLon,maxLat'. "
            f"Example: '80,5,100,25' (Bay of Bengal). Error: {e}"
        )
