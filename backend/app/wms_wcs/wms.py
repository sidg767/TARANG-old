"""
Hand-rolled WMS 1.3.0 endpoint — TARANG SIH 2026 PS 26067

Implements a minimal but spec-compliant OGC Web Map Service (WMS 1.3.0) that
serves coloured PNG tiles directly from our cached NetCDF data.

WHY THIS EXISTS (§3 Option B):
  THREDDS Data Server provides a full WMS implementation when running.
  This module is an *alternative* used when:
    a) THREDDS is unavailable (internet-only demo mode, or service restart)
    b) We need custom colormaps beyond THREDDS defaults
    c) We want to demonstrate OGC compliance without external dependencies

  Activated by setting OPTION_B_MODE=true in docker-compose.yml or .env.

OPERATIONS IMPLEMENTED:
  GetCapabilities  → returns XML capabilities document
  GetMap           → returns colourised PNG tile for a given BBOX/CRS/layer
  GetFeatureInfo   → returns the data value at a clicked pixel (text/plain, HTML, JSON)

SPEC REFERENCE:
  OGC WMS 1.3.0: https://www.ogc.org/standards/wms
"""

from __future__ import annotations

import io
import logging

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from PIL import Image

logger = logging.getLogger("tarang.wms")
router = APIRouter(tags=["OGC WMS"])

# WMS 1.3.0 capabilities: one root <Layer> with the data layers as children.
_CAPABILITIES_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<WMS_Capabilities version="1.3.0" xmlns="http://www.opengis.net/wms"
  xmlns:xlink="http://www.w3.org/1999/xlink"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:schemaLocation="http://www.opengis.net/wms http://schemas.opengis.net/wms/1.3.0/capabilities_1_3_0.xsd">

  <Service>
    <Name>WMS</Name>
    <Title>TARANG Ocean Visualization WMS — MoES/INCOIS SIH 2026 PS 26067</Title>
    <Abstract>
      Hand-rolled OGC WMS 1.3.0 (§4 Option B fallback) serving coloured tiles from
      TARANG's cached CF-1.8 NetCDF for the Indian Ocean / Bay of Bengal.
      CF standard_name and units for each layer are given in its Abstract.
    </Abstract>
    <OnlineResource xlink:type="simple" xlink:href="{base_url}/wms"/>
    <ContactInformation>
      <ContactPersonPrimary>
        <ContactOrganization>TARANG Team — Smart India Hackathon 2026</ContactOrganization>
      </ContactPersonPrimary>
    </ContactInformation>
    <MaxWidth>2048</MaxWidth>
    <MaxHeight>2048</MaxHeight>
  </Service>

  <Capability>
    <Request>
      <GetCapabilities>
        <Format>text/xml</Format>
        <DCPType><HTTP><Get>
          <OnlineResource xlink:type="simple" xlink:href="{base_url}/wms"/>
        </Get></HTTP></DCPType>
      </GetCapabilities>
      <GetMap>
        <Format>image/png</Format>
        <DCPType><HTTP><Get>
          <OnlineResource xlink:type="simple" xlink:href="{base_url}/wms"/>
        </Get></HTTP></DCPType>
      </GetMap>
      <GetFeatureInfo>
        <Format>text/plain</Format>
        <Format>text/html</Format>
        <Format>application/json</Format>
        <DCPType><HTTP><Get>
          <OnlineResource xlink:type="simple" xlink:href="{base_url}/wms"/>
        </Get></HTTP></DCPType>
      </GetFeatureInfo>
    </Request>
    <Exception>
      <Format>XML</Format>
    </Exception>
    <Layer>
      <Title>TARANG Ocean Data</Title>
      <CRS>CRS:84</CRS>
      <CRS>EPSG:4326</CRS>
      <EX_GeographicBoundingBox>
        <westBoundLongitude>-180</westBoundLongitude>
        <eastBoundLongitude>180</eastBoundLongitude>
        <southBoundLatitude>-90</southBoundLatitude>
        <northBoundLatitude>90</northBoundLatitude>
      </EX_GeographicBoundingBox>
{layers_xml}
    </Layer>
  </Capability>
</WMS_Capabilities>
"""

_LAYER_XML_TEMPLATE = """\
      <Layer queryable="1" opaque="0" cascaded="0">
        <Name>{layer_id}</Name>
        <Title>{label}</Title>
        <Abstract>{description} [CF standard_name: {standard_name}; units: {units}]</Abstract>
        <CRS>CRS:84</CRS>
        <CRS>EPSG:4326</CRS>
        <EX_GeographicBoundingBox>
          <westBoundLongitude>{min_lon}</westBoundLongitude>
          <eastBoundLongitude>{max_lon}</eastBoundLongitude>
          <southBoundLatitude>{min_lat}</southBoundLatitude>
          <northBoundLatitude>{max_lat}</northBoundLatitude>
        </EX_GeographicBoundingBox>
        <BoundingBox CRS="CRS:84" minx="{min_lon}" miny="{min_lat}" maxx="{max_lon}" maxy="{max_lat}"/>
        <Dimension name="elevation" units="m" unitSymbol="m" default="{elev_default}"{elev_multi}>{elev_values}</Dimension>
        <Style>
          <Name>{colormap}</Name>
          <Title>{colormap} (matplotlib)</Title>
        </Style>
      </Layer>
"""


# ── Matplotlib colourmap helper ───────────────────────────────────────────────

def _apply_colormap(data: np.ndarray, vmin: float, vmax: float, cmap_name: str = "viridis") -> np.ndarray:
    """
    Convert a 2D float array into an RGBA uint8 array using a named matplotlib colormap.
    NaN values become transparent (alpha=0).
    """
    try:
        import matplotlib
        cmap = matplotlib.colormaps[cmap_name]
    except Exception as e:
        logger.warning(f"Colormap '{cmap_name}' unavailable ({e}) — falling back to grayscale")
        cmap = None

    norm_data = np.clip((data - vmin) / max(vmax - vmin, 1e-9), 0.0, 1.0)

    if cmap is not None:
        rgba = (cmap(norm_data) * 255).astype(np.uint8)  # (H, W, 4)
    else:
        g = (norm_data * 255).astype(np.uint8)
        rgba = np.stack([g, g, g, np.full_like(g, 255)], axis=-1)

    # Transparent NaNs
    nan_mask = np.isnan(data)
    rgba[nan_mask, 3] = 0

    return rgba


def _build_png(rgba: np.ndarray, width: int, height: int) -> bytes:
    """Resize and encode an RGBA array to PNG bytes."""
    img = Image.fromarray(rgba, mode="RGBA")
    img = img.resize((width, height), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ── WMS Endpoints ─────────────────────────────────────────────────────────────

@router.get("/wms", summary="OGC WMS 1.3.0 — GetCapabilities / GetMap")
async def wms(
    request: Request,
    SERVICE: str = Query("WMS"),
    REQUEST: str = Query("GetCapabilities"),
    VERSION: str = Query("1.3.0"),
    LAYERS: str | None = Query(None),
    STYLES: str | None = Query(""),
    CRS: str | None = Query(None),
    BBOX: str | None = Query(None),
    WIDTH: int | None = Query(256),
    HEIGHT: int | None = Query(256),
    FORMAT: str | None = Query("image/png"),
    TRANSPARENT: str | None = Query("TRUE"),
    ELEVATION: float | None = Query(None, description="Depth level in metres (positive-down)"),
    TIME: str | None = Query(None, description="ISO-8601 time step"),
    COLORMAP: str | None = Query(None, description="matplotlib colormap name override"),
    QUERY_LAYERS: str | None = Query(None, description="GetFeatureInfo: layer(s) to query"),
    INFO_FORMAT: str | None = Query("text/plain", description="GetFeatureInfo response format"),
    I: int | None = Query(None, description="GetFeatureInfo: pixel column (from left)"),
    J: int | None = Query(None, description="GetFeatureInfo: pixel row (from top)"),
):
    """
    OGC WMS 1.3.0 endpoint.

    **GetCapabilities**: returns the capabilities XML document listing all
    registered data sources as WMS layers.

    **GetMap**: returns a PNG tile for the requested layer/BBOX/time/depth.
    """
    req_upper = REQUEST.upper()

    if req_upper == "GETCAPABILITIES":
        return await _get_capabilities(request)
    elif req_upper == "GETMAP":
        if not LAYERS:
            raise HTTPException(400, "LAYERS parameter is required for GetMap")
        if not BBOX:
            raise HTTPException(400, "BBOX parameter is required for GetMap")
        return await _get_map(
            request, LAYERS, CRS or "CRS:84", BBOX,
            WIDTH or 256, HEIGHT or 256, ELEVATION, TIME, COLORMAP,
        )
    elif req_upper == "GETFEATUREINFO":
        query_layer = QUERY_LAYERS or LAYERS
        if not query_layer:
            _wms_error(400, "QUERY_LAYERS parameter is required for GetFeatureInfo")
        if not BBOX:
            _wms_error(400, "BBOX parameter is required for GetFeatureInfo")
        if I is None or J is None:
            _wms_error(400, "I and J pixel parameters are required for GetFeatureInfo")
        return await _get_feature_info(
            request, query_layer.split(",")[0], BBOX, CRS or "CRS:84",
            WIDTH or 256, HEIGHT or 256, I, J, ELEVATION, INFO_FORMAT or "text/plain",
        )
    else:
        raise HTTPException(400, f"Unsupported WMS REQUEST: '{REQUEST}'. Supported: GetCapabilities, GetMap, GetFeatureInfo")


def _xml_escape(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _crs_is_latlon(crs: str | None) -> bool:
    """
    WMS 1.3.0 axis order: EPSG:4326 is (lat, lon); CRS:84 is (lon, lat). QGIS and
    other spec-correct clients send EPSG:4326 BBOX as minLat,minLon,maxLat,maxLon.
    """
    if not crs:
        return False
    c = crs.strip().upper().replace("URN:OGC:DEF:CRS:", "").replace("EPSG::", "EPSG:")
    return c in ("EPSG:4326", "4326")


def _parse_wms_bbox(bbox_str: str, crs: str | None) -> tuple[float, float, float, float]:
    """Return (min_lon, min_lat, max_lon, max_lat) regardless of the CRS axis convention."""
    parts = [float(x) for x in bbox_str.split(",")]
    if len(parts) != 4:
        raise ValueError(f"Invalid BBOX '{bbox_str}'. Expected four comma-separated numbers.")
    if _crs_is_latlon(crs):
        min_lat, min_lon, max_lat, max_lon = parts
    else:
        min_lon, min_lat, max_lon, max_lat = parts
    return (min_lon, min_lat, max_lon, max_lat)


async def _get_capabilities(request: Request) -> Response:
    """WMS 1.3.0 capabilities XML, built from the registry manifests (no dataset opens)."""
    registry = request.app.state.registry
    base_url = str(request.base_url).rstrip("/")

    layers_xml = ""
    for manifest in registry.all_manifests():
        bbox = manifest.get("default_bbox") or manifest.get("local_cache_bbox") or [-180, -90, 180, 90]
        depth_levels = manifest.get("depth_levels") or []
        elev_values = ",".join(str(d) for d in depth_levels) if depth_levels else "0"
        layers_xml += _LAYER_XML_TEMPLATE.format(
            layer_id=_xml_escape(manifest["id"]),
            label=_xml_escape(manifest.get("label", manifest["id"])),
            description=_xml_escape(manifest.get("description", "")),
            standard_name=_xml_escape(manifest.get("standard_name", "unknown")),
            units=_xml_escape(manifest.get("units", "unknown")),
            min_lon=bbox[0], min_lat=bbox[1], max_lon=bbox[2], max_lat=bbox[3],
            elev_default=(depth_levels[0] if depth_levels else 0),
            elev_multi=' multipleValues="true"' if len(depth_levels) > 1 else "",
            elev_values=elev_values,
            colormap=_xml_escape(manifest.get("colormap", "viridis")),
        )

    xml = _CAPABILITIES_XML.format(base_url=base_url, layers_xml=layers_xml)
    return Response(content=xml, media_type="text/xml")


async def _get_map(
    request: Request,
    layer_id: str,
    crs: str,
    bbox_str: str,
    width: int,
    height: int,
    elevation: float | None,
    time_str: str | None,
    colormap_override: str | None,
) -> Response:
    """
    Render a depth-slice at the requested BBOX/time/elevation as a PNG tile.

    Strategy:
      1. Look up the layer in the registry
      2. Open the local NetCDF cache via the adapter
      3. Nearest-neighbour interpolate to the requested BBOX grid
      4. Apply the configured colormap (or override)
      5. Encode to PNG and return
    """
    registry = request.app.state.registry

    try:
        adapter = registry.get_adapter(layer_id)
        manifest = registry.get_manifest(layer_id)
    except KeyError:
        _wms_error(404, f"Layer '{layer_id}' not found. Available: {list(registry.manifest_ids())}")

    # Parse BBOX honouring WMS 1.3.0 axis order (EPSG:4326 = lat,lon; CRS:84 = lon,lat)
    try:
        min_lon, min_lat, max_lon, max_lat = _parse_wms_bbox(bbox_str, crs)
    except (ValueError, TypeError):
        _wms_error(400, f"Invalid BBOX '{bbox_str}' for CRS '{crs}'")

    # Clamp output size
    width  = min(max(width,  1), 2048)
    height = min(max(height, 1), 2048)

    # Get a 2D lat×lon slice from the adapter
    try:
        meta = adapter.get_metadata()
        time_idx = 0  # default: latest available time step
        depth_m  = 0.0  # default: surface

        depth_levels = meta.get("depth_levels") or [0]
        if elevation is not None:
            depth_m = elevation
        else:
            depth_m = depth_levels[0]

        variable = meta["available_variables"][0]
        slice_result = adapter.get_slice(variable, depth_m, time_idx, (min_lon, min_lat, max_lon, max_lat))
        data_2d = slice_result.data

    except Exception as e:
        logger.warning(f"WMS GetMap: adapter.get_slice failed for '{layer_id}': {e} — returning transparent tile")
        # Return a transparent tile rather than crashing (graceful degradation)
        transparent = np.zeros((height, width, 4), dtype=np.uint8)
        png = _build_png(transparent, width, height)
        return Response(content=png, media_type="image/png")

    # Colour-map the data
    vmin = manifest.get("valid_min", float(np.nanmin(data_2d) if np.any(np.isfinite(data_2d)) else 0))
    vmax = manifest.get("valid_max", float(np.nanmax(data_2d) if np.any(np.isfinite(data_2d)) else 1))
    cmap_name = colormap_override or manifest.get("colormap", "viridis")

    rgba = _apply_colormap(data_2d, vmin, vmax, cmap_name)
    png  = _build_png(rgba, width, height)

    return Response(content=png, media_type="image/png")


async def _get_feature_info(
    request: Request,
    layer_id: str,
    bbox_str: str,
    crs: str,
    width: int,
    height: int,
    i: int,
    j: int,
    elevation: float | None,
    info_format: str,
) -> Response:
    """
    WMS 1.3.0 GetFeatureInfo — return the data value under the clicked pixel.

    The client sends the same BBOX/WIDTH/HEIGHT as its GetMap plus the pixel
    (I, J) it clicked (I from the left, J from the top). We convert that to a
    lon/lat, pull the depth-slice for the BBOX, and report the nearest cell.
    """
    registry = request.app.state.registry

    try:
        adapter = registry.get_adapter(layer_id)
        manifest = registry.get_manifest(layer_id)
    except KeyError:
        _wms_error(404, f"Layer '{layer_id}' not found. Available: {list(registry.manifest_ids())}")

    try:
        min_lon, min_lat, max_lon, max_lat = _parse_wms_bbox(bbox_str, crs)
    except (ValueError, TypeError):
        _wms_error(400, f"Invalid BBOX '{bbox_str}' for CRS '{crs}'")

    if not (0 <= i < width and 0 <= j < height):
        _wms_error(400, f"Pixel (I={i}, J={j}) is outside the {width}x{height} map")

    # Pixel centre → geographic coordinate. J runs top→down, so latitude descends.
    lon = min_lon + (i + 0.5) / width * (max_lon - min_lon)
    lat = max_lat - (j + 0.5) / height * (max_lat - min_lat)

    try:
        meta = adapter.get_metadata()
        depth_levels = meta.get("depth_levels") or [0]
        depth_m = elevation if elevation is not None else depth_levels[0]
        variable = meta["available_variables"][0]
        sr = adapter.get_slice(variable, depth_m, 0, (min_lon, min_lat, max_lon, max_lat))
    except Exception as e:
        logger.warning(f"WMS GetFeatureInfo: get_slice failed for '{layer_id}': {e}")
        _wms_error(500, f"Could not read data for layer '{layer_id}'")

    iy = int(np.argmin(np.abs(sr.lat - lat)))
    ix = int(np.argmin(np.abs(sr.lon - lon)))
    raw = sr.data[iy, ix]
    value = None if (raw is None or not np.isfinite(raw)) else round(float(raw), 4)

    units = manifest.get("units", "unknown")
    std_name = manifest.get("standard_name", variable)
    fmt = info_format.lower()

    if "json" in fmt:
        import json
        body = json.dumps({
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(float(sr.lon[ix]), 4), round(float(sr.lat[iy]), 4)]},
                "properties": {
                    "layer": layer_id, "variable": variable, "standard_name": std_name,
                    "value": value, "units": units, "elevation_m": depth_m,
                },
            }],
        })
        return Response(content=body, media_type="application/json")

    if "html" in fmt:
        vtxt = "no data" if value is None else f"{value} {units}"
        body = (
            "<html><body><table border='1'>"
            f"<tr><th>Layer</th><td>{_xml_escape(layer_id)}</td></tr>"
            f"<tr><th>Variable</th><td>{_xml_escape(std_name)}</td></tr>"
            f"<tr><th>Value</th><td>{_xml_escape(vtxt)}</td></tr>"
            f"<tr><th>Lon, Lat</th><td>{round(float(sr.lon[ix]), 4)}, {round(float(sr.lat[iy]), 4)}</td></tr>"
            f"<tr><th>Elevation</th><td>{depth_m} m</td></tr>"
            "</table></body></html>"
        )
        return Response(content=body, media_type="text/html")

    # default: text/plain
    vtxt = "no data" if value is None else f"{value} {units}"
    body = (
        f"Layer: {layer_id}\n"
        f"Variable: {std_name} ({variable})\n"
        f"Value: {vtxt}\n"
        f"Location: lon={round(float(sr.lon[ix]), 4)}, lat={round(float(sr.lat[iy]), 4)}\n"
        f"Elevation: {depth_m} m\n"
    )
    return Response(content=body, media_type="text/plain")


def _wms_error(code: int, message: str):
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ServiceExceptionReport version="1.3.0">
  <ServiceException code="{code}">{message}</ServiceException>
</ServiceExceptionReport>"""
    raise HTTPException(status_code=code, detail=xml)
