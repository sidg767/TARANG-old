"""
Hand-rolled WCS 2.0.1 endpoint — TARANG SIH 2026 PS 26067

Implements a minimal but spec-compliant OGC Web Coverage Service (WCS 2.0.1) that
serves raw NetCDF subsets directly from our cached HYCOM data.

WHY THIS EXISTS (§3 Option B):
  THREDDS Data Server provides a full WCS implementation when running.
  This module is an *alternative* used when:
    a) THREDDS is unavailable (e.g., service restart during demo)
    b) We need direct integration with our registry/adapter system
    c) We want to demonstrate OGC compliance without external dependencies

  Activated by setting OPTION_B_MODE=true in docker-compose.yml or .env.

OPERATIONS IMPLEMENTED:
  GetCapabilities  → returns XML capabilities listing all registered coverages
  DescribeCoverage → returns detailed XML description of a specific coverage
  GetCoverage      → returns NetCDF4 subset for requested BBOX/time/depth

SPEC REFERENCE:
  OGC WCS 2.0.1: https://www.ogc.org/standards/wcs
  OGC WCS 2.0 NetCDF extension: OGC 09-146r8
"""

from __future__ import annotations

import io
import logging

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

logger = logging.getLogger("tarang.wcs")
router = APIRouter(tags=["OGC WCS"])

# ── WCS Capabilities XML ──────────────────────────────────────────────────────

_CAPABILITIES_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<wcs:Capabilities xmlns:wcs="http://www.opengis.net/wcs/2.0"
  xmlns:ows="http://www.opengis.net/ows/1.1"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  version="2.0.1">

  <ows:ServiceIdentification>
    <ows:Title>TARANG Ocean Coverage Service</ows:Title>
    <ows:Abstract>
      OGC WCS 2.0.1 endpoint for TARANG ocean data (HYCOM, Argo profiles).
      MoES/INCOIS · Smart India Hackathon 2026 · PS 26067.
      Provides raw NetCDF4 subsets of Indian Ocean variables:
      sea water temperature, salinity, current velocity components (U/V).
    </ows:Abstract>
    <ows:ServiceType>WCS</ows:ServiceType>
    <ows:ServiceTypeVersion>2.0.1</ows:ServiceTypeVersion>
  </ows:ServiceIdentification>

  <ows:OperationsMetadata>
    <ows:Operation name="GetCapabilities">
      <ows:DCP><ows:HTTP>
        <ows:Get xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="{base_url}/wcs"/>
      </ows:HTTP></ows:DCP>
    </ows:Operation>
    <ows:Operation name="DescribeCoverage">
      <ows:DCP><ows:HTTP>
        <ows:Get xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="{base_url}/wcs"/>
      </ows:HTTP></ows:DCP>
    </ows:Operation>
    <ows:Operation name="GetCoverage">
      <ows:DCP><ows:HTTP>
        <ows:Get xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="{base_url}/wcs"/>
      </ows:HTTP></ows:DCP>
    </ows:Operation>
  </ows:OperationsMetadata>

  <wcs:Contents>
    {coverage_summaries}
  </wcs:Contents>
</wcs:Capabilities>
"""

_COVERAGE_SUMMARY_TEMPLATE = """\
    <wcs:CoverageSummary>
      <wcs:CoverageId>{coverage_id}</wcs:CoverageId>
      <wcs:CoverageSubtype>GridCoverage</wcs:CoverageSubtype>
      <ows:Title>{label}</ows:Title>
      <ows:Abstract>{description}</ows:Abstract>
      <ows:WGS84BoundingBox>
        <ows:LowerCorner>-180 -90</ows:LowerCorner>
        <ows:UpperCorner>180 90</ows:UpperCorner>
      </ows:WGS84BoundingBox>
    </wcs:CoverageSummary>
"""

_DESCRIBE_COVERAGE_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<wcs:CoverageDescriptions xmlns:wcs="http://www.opengis.net/wcs/2.0"
  xmlns:gml="http://www.opengis.net/gml/3.2"
  xmlns:swe="http://www.opengis.net/swe/2.0"
  version="2.0.1">
  <wcs:CoverageDescription gml:id="{coverage_id}">
    <gml:domainSet>
      <gml:Grid gml:id="{coverage_id}_grid" dimension="4">
        <gml:limits>
          <gml:GridEnvelope>
            <gml:low>0 0 0 0</gml:low>
            <gml:high>{time_steps} {depth_steps} {lat_steps} {lon_steps}</gml:high>
          </gml:GridEnvelope>
        </gml:limits>
        <gml:axisLabels>time depth latitude longitude</gml:axisLabels>
      </gml:Grid>
    </gml:domainSet>
    <wcs:ServiceParameters>
      <wcs:CoverageSubtype>GridCoverage</wcs:CoverageSubtype>
      <wcs:nativeFormat>application/x-netcdf4</wcs:nativeFormat>
    </wcs:ServiceParameters>
    <swe:DataRecord>
      <swe:field name="{variable}">
        <swe:Quantity definition="http://vocab.nerc.ac.uk/standard_name/{standard_name}">
          <swe:label>{label}</swe:label>
          <swe:uom code="{units}"/>
          <swe:constraint>
            <swe:AllowedValues>
              <swe:interval>{valid_min} {valid_max}</swe:interval>
            </swe:AllowedValues>
          </swe:constraint>
        </swe:Quantity>
      </swe:field>
    </swe:DataRecord>
  </wcs:CoverageDescription>
</wcs:CoverageDescriptions>
"""


# ── WCS Endpoints ─────────────────────────────────────────────────────────────

@router.get("/wcs", summary="OGC WCS 2.0.1 — GetCapabilities / DescribeCoverage / GetCoverage")
async def wcs(
    request: Request,
    SERVICE: str = Query("WCS"),
    REQUEST: str = Query("GetCapabilities"),
    VERSION: str = Query("2.0.1"),
    COVERAGEID: str | None = Query(None, alias="COVERAGEID"),
    SUBSET_time: str | None = Query(None, alias="SUBSET[time]"),
    SUBSET_depth: str | None = Query(None, alias="SUBSET[depth]"),
    SUBSET_lat: str | None = Query(None, alias="SUBSET[latitude]"),
    SUBSET_lon: str | None = Query(None, alias="SUBSET[longitude]"),
    RANGESUBSET: str | None = Query(None, description="WCS 2.0 range subset — variable/band name to export"),
    FORMAT: str | None = Query("application/x-netcdf4"),
):
    """
    OGC WCS 2.0.1 endpoint.

    **GetCapabilities**: lists all registered data sources as WCS coverages.
    **DescribeCoverage**: detailed description of a specific coverage.
    **GetCoverage**: returns a NetCDF4 file (subset of the cached HYCOM data).
    """
    req_upper = REQUEST.upper()

    if req_upper == "GETCAPABILITIES":
        return await _get_capabilities(request)
    elif req_upper == "DESCRIBECOVERAGE":
        if not COVERAGEID:
            raise HTTPException(400, "COVERAGEID is required for DescribeCoverage")
        return await _describe_coverage(request, COVERAGEID)
    elif req_upper == "GETCOVERAGE":
        if not COVERAGEID:
            raise HTTPException(400, "COVERAGEID is required for GetCoverage")
        return await _get_coverage(request, COVERAGEID, SUBSET_time, SUBSET_depth, SUBSET_lat, SUBSET_lon, RANGESUBSET)
    else:
        raise HTTPException(400, f"Unsupported WCS REQUEST: '{REQUEST}'. Supported: GetCapabilities, DescribeCoverage, GetCoverage")


async def _get_capabilities(request: Request) -> Response:
    """Return WCS 2.0.1 capabilities XML listing all registered coverages."""
    registry = request.app.state.registry
    base_url = str(request.base_url).rstrip("/")

    summaries = ""
    for manifest in registry.all_manifests():
        summaries += _COVERAGE_SUMMARY_TEMPLATE.format(
            coverage_id=manifest["id"],
            label=manifest.get("label", manifest["id"]),
            description=manifest.get("description", ""),
        )

    xml = _CAPABILITIES_XML.format(base_url=base_url, coverage_summaries=summaries)
    return Response(content=xml, media_type="application/xml")


async def _describe_coverage(request: Request, coverage_id: str) -> Response:
    """Return detailed WCS coverage description XML."""
    registry = request.app.state.registry

    try:
        manifest = registry.get_manifest(coverage_id)
        adapter  = registry.get_adapter(coverage_id)
    except KeyError:
        raise HTTPException(404, f"Coverage '{coverage_id}' not found. Available: {list(registry.manifest_ids())}")

    meta = adapter.get_metadata()
    dims = meta.get("dimensions", {})

    xml = _DESCRIBE_COVERAGE_XML.format(
        coverage_id=coverage_id,
        variable=manifest.get("variable", coverage_id),
        standard_name=manifest.get("standard_name", manifest.get("variable", coverage_id)),
        label=manifest.get("label", coverage_id),
        units=manifest.get("units", "1"),
        valid_min=manifest.get("valid_min", -1e9),
        valid_max=manifest.get("valid_max",  1e9),
        time_steps=dims.get("time", 1) - 1,
        depth_steps=dims.get("depth", 1) - 1,
        lat_steps=dims.get("lat", 1) - 1,
        lon_steps=dims.get("lon", 1) - 1,
    )
    return Response(content=xml, media_type="application/xml")


async def _get_coverage(
    request: Request,
    coverage_id: str,
    subset_time: str | None,
    subset_depth: str | None,
    subset_lat: str | None,
    subset_lon: str | None,
    range_subset: str | None = None,
) -> Response:
    """
    Return a NetCDF4 file subset.

    Subset format follows WCS 2.0.1 KVP syntax:
      SUBSET[time]=(0,3)
      SUBSET[depth]=(0,10)
      SUBSET[latitude]=(5.0,25.0)
      SUBSET[longitude]=(80.0,100.0)
    """
    registry = request.app.state.registry

    try:
        adapter  = registry.get_adapter(coverage_id)
        manifest = registry.get_manifest(coverage_id)
    except KeyError:
        raise HTTPException(404, f"Coverage '{coverage_id}' not found. Available: {list(registry.manifest_ids())}")

    # Parse optional index subsets (time/depth) and geographic subsets (lat/lon)
    def parse_index_range(s: str | None) -> tuple[int, int] | None:
        if s is None:
            return None
        s = s.strip("() ")
        parts = s.split(",")
        if len(parts) == 2:
            return (int(parts[0]), int(parts[1]))
        elif len(parts) == 1:
            v = int(parts[0])
            return (v, v)
        return None

    def parse_float_range(s: str | None) -> tuple[float, float] | None:
        if s is None:
            return None
        s = s.strip("() ")
        parts = s.split(",")
        if len(parts) == 2:
            return (float(parts[0]), float(parts[1]))
        return None

    time_range  = parse_index_range(subset_time)
    depth_range = parse_index_range(subset_depth)
    lat_range   = parse_float_range(subset_lat)
    lon_range   = parse_float_range(subset_lon)

    try:
        # Get metadata to know dimension sizes and real (non-uniform) depth levels
        meta = adapter.get_metadata()
        manifest = registry.get_manifest(coverage_id)
        dims = meta.get("dimensions", {})
        n_time  = dims.get("time",  1)
        depth_levels = meta.get("depth_levels") or [0]
        n_depth = len(depth_levels)
        avail = meta["available_variables"]
        # WCS 2.0 RangeSubset — let the caller pick which variable/band to export.
        variable = range_subset if (range_subset and range_subset in avail) else avail[0]

        t0, t1 = time_range  if time_range  else (0, n_time  - 1)
        d0, d1 = depth_range if depth_range else (0, n_depth - 1)

        # get_slice needs a bbox; fall back to the source's default region if not requested.
        default_bbox = manifest.get("default_bbox") or manifest.get("local_cache_bbox") or [80, 5, 100, 25]
        min_lon, max_lon = lon_range if lon_range else (default_bbox[0], default_bbox[2])
        min_lat, max_lat = lat_range if lat_range else (default_bbox[1], default_bbox[3])
        bbox = (min_lon, min_lat, max_lon, max_lat)

        slices = []
        coord_lat = coord_lon = None
        out_depths = []
        for ti in range(t0, t1 + 1):
            for di in range(d0, d1 + 1):
                depth_m = depth_levels[di] if di < len(depth_levels) else depth_levels[-1]
                result = adapter.get_slice(variable, depth_m, ti, bbox)
                slices.append(result.data)
                if coord_lat is None:
                    coord_lat = np.asarray(result.lat, dtype="f4")
                    coord_lon = np.asarray(result.lon, dtype="f4")
                if ti == t0:
                    out_depths.append(float(result.depth_m))

        if not slices:
            raise ValueError("No data slices returned")

        # Stack into (time, depth, lat, lon)
        n_t = t1 - t0 + 1
        n_d = d1 - d0 + 1
        arr = np.array(slices).reshape(n_t, n_d, slices[0].shape[0], slices[0].shape[1])

    except Exception as e:
        logger.warning(f"WCS GetCoverage data error for '{coverage_id}': {e}")
        raise HTTPException(500, f"Failed to extract coverage data: {e}")

    # Write to a temp NetCDF4 file (netCDF4 needs a real path).
    try:
        import netCDF4 as nc4  # type: ignore
        buf = io.BytesIO()

        import os
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".nc", delete=False) as tmp:
            tmp_path = tmp.name

        ds = nc4.Dataset(tmp_path, "w", format="NETCDF4")
        ds.Conventions = "CF-1.8"
        ds.setncattr_string("title", f"TARANG WCS GetCoverage — {manifest.get('label', coverage_id)}")
        ds.setncattr_string("institution", "MoES/INCOIS — Smart India Hackathon 2026 PS 26067")
        ds.setncattr_string("source", manifest.get("source", ""))

        ds.createDimension("time",      n_t)
        ds.createDimension("depth",     n_d)
        ds.createDimension("latitude",  arr.shape[2])
        ds.createDimension("longitude", arr.shape[3])

        # CF coordinate variables so the coverage is georeferenceable.
        tv = ds.createVariable("time", "i4", ("time",))
        tv.units = "days since 2000-01-01 00:00:00"
        tv.standard_name = "time"
        tv[:] = np.arange(t0, t1 + 1, dtype="i4")

        dv = ds.createVariable("depth", "f4", ("depth",))
        dv.units = "m"; dv.positive = "down"; dv.standard_name = "depth"
        dv[:] = np.asarray(out_depths, dtype="f4") if out_depths else np.zeros(n_d, dtype="f4")

        if coord_lat is not None:
            latv = ds.createVariable("latitude", "f4", ("latitude",))
            latv.units = "degrees_north"; latv.standard_name = "latitude"
            latv[:] = coord_lat
            lonv = ds.createVariable("longitude", "f4", ("longitude",))
            lonv.units = "degrees_east"; lonv.standard_name = "longitude"
            lonv[:] = coord_lon

        var_name = variable
        vcf = (meta.get("cf_metadata") or {}).get(variable, {})
        v = ds.createVariable(var_name, "f4", ("time", "depth", "latitude", "longitude"),
                              fill_value=manifest.get("missing_value", -30000.0), zlib=True, complevel=4)
        v.standard_name = vcf.get("standard_name") or manifest.get("standard_name", "")
        v.long_name      = vcf.get("long_name") or manifest.get("long_name", "")
        v.units          = vcf.get("units") or manifest.get("units", "1")
        v.valid_min      = vcf.get("valid_min", manifest.get("valid_min", -1e9))
        v.valid_max      = vcf.get("valid_max", manifest.get("valid_max",  1e9))
        v[:] = arr

        ds.close()

        with open(tmp_path, "rb") as f:
            nc_bytes = f.read()
        os.unlink(tmp_path)

    except ImportError:
        # Fallback: return as raw NPY if netCDF4 isn't installed
        logger.warning("netCDF4 not available — falling back to raw NumPy .npy format")
        npy_buf = io.BytesIO()
        np.save(npy_buf, arr)
        nc_bytes = npy_buf.getvalue()
        return Response(content=nc_bytes, media_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{coverage_id}.npy"'})

    return Response(
        content=nc_bytes,
        media_type="application/x-netcdf4",
        headers={"Content-Disposition": f'attachment; filename="{coverage_id}.nc"'},
    )
