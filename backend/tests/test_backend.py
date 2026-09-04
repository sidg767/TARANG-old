"""
Backend Test Suite — Phase 0 smoke tests.

Run: pytest backend/tests/ -v

Tests:
  1. Registry loads all YAML manifests
  2. NetCDFAdapter opens the fixture dataset
  3. /api/metadata returns valid JSON with depth_levels
  4. /api/slice returns binary (correct content-type + parseable header)
  5. /api/health returns 200
  6. Binary response header is valid JSON with required fields
"""

import json
import struct
import sys
from pathlib import Path

import numpy as np
import pytest

# Ensure the project root is on the path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def fixture_nc_path(tmp_path_factory):
    """Create a tiny synthetic NetCDF file for testing (no network required)."""
    try:
        import xarray as xr
    except ImportError:
        pytest.skip("xarray not installed")

    tmp = tmp_path_factory.mktemp("data")
    nc_path = tmp / "fixture_air.nc"

    # Build a minimal CF-compliant ocean NetCDF in memory
    lat = np.linspace(5, 25, 20, dtype=np.float32)
    lon = np.linspace(80, 100, 40, dtype=np.float32)
    depth = np.array([0, 10, 50, 100, 200, 500], dtype=np.float32)
    time = np.array([0, 1, 2], dtype=np.float64)

    rng = np.random.default_rng(42)
    water_temp = rng.uniform(5, 35, size=(len(time), len(depth), len(lat), len(lon))).astype(np.float32)

    ds = xr.Dataset({
        "water_temp": xr.DataArray(
            data=water_temp,
            dims=["time", "depth", "lat", "lon"],
            coords={"time": time, "depth": depth, "lat": lat, "lon": lon},
            attrs={
                "standard_name": "sea_water_temperature",
                "long_name":     "Water Temperature",
                "units":         "degC",
                "_FillValue":    -30000.0,
                "valid_min":     -5.0,
                "valid_max":     40.0,
            }
        )
    }, attrs={"Conventions": "CF-1.6", "title": "Test fixture"})

    ds.to_netcdf(str(nc_path))
    return str(nc_path)


# ── Registry tests ────────────────────────────────────────────────────────────

def test_registry_loads(tmp_path):
    """Registry loader should load all YAML manifests from registry/."""
    from backend.app.registry.loader import RegistryLoader

    registry_dir = Path(__file__).parent.parent.parent / "registry"
    if not registry_dir.exists():
        pytest.skip("registry/ directory not found")

    loader = RegistryLoader(str(registry_dir))
    loader.load_all()

    ids = list(loader.manifest_ids())
    assert len(ids) >= 1, "Expected at least one manifest"
    assert "hycom_water_temp" in ids, "hycom_water_temp manifest missing"


def test_external_adapter_plugin_is_used_by_registry(tmp_path):
    """A registered sensor plugin should work through YAML without core edits."""
    from backend.app.adapters import DelimitedTextAdapter
    from backend.app.plugins import register_adapter
    from backend.app.registry.loader import RegistryLoader

    register_adapter("TestSensorAdapter", DelimitedTextAdapter)
    (tmp_path / "sensor.yaml").write_text(
        "id: sensor\nadapter: TestSensorAdapter\nsource: sensor.csv\nvariable: temperature\n"
    )

    loader = RegistryLoader(str(tmp_path))
    loader.load_all()

    assert list(loader.manifest_ids()) == ["sensor"]
    assert isinstance(loader.get_adapter("sensor"), DelimitedTextAdapter)


# ── Adapter tests ─────────────────────────────────────────────────────────────

def test_netcdf_adapter_open(fixture_nc_path):
    """NetCDFAdapter should open the fixture dataset."""
    from backend.app.adapters.netcdf_adapter import NetCDFAdapter

    manifest = {
        "id":           "test_temp",
        "label":        "Test Temperature",
        "adapter":      "NetCDFAdapter",
        "source":       fixture_nc_path,
        "local_cache":  fixture_nc_path,
        "variable":     "water_temp",
        "standard_name": "sea_water_temperature",
        "units":        "degC",
        "valid_min":    -5.0,
        "valid_max":    40.0,
        "missing_value": -30000.0,
        "depth_levels": [0, 10, 50, 100, 200, 500],
    }
    adapter = NetCDFAdapter(manifest)
    ds = adapter.open()
    assert "water_temp" in ds.data_vars
    assert ds.sizes["depth"] == 6


def test_netcdf_adapter_metadata(fixture_nc_path):
    """NetCDFAdapter.get_metadata() should return non-uniform depth_levels."""
    from backend.app.adapters.netcdf_adapter import NetCDFAdapter

    manifest = {
        "id": "test_temp", "label": "Test", "adapter": "NetCDFAdapter",
        "source": fixture_nc_path, "local_cache": fixture_nc_path,
        "variable": "water_temp", "standard_name": "sea_water_temperature",
        "units": "degC", "valid_min": -5.0, "valid_max": 40.0,
        "missing_value": -30000.0, "depth_levels": [0, 10, 50, 100, 200, 500],
    }
    adapter = NetCDFAdapter(manifest)
    meta = adapter.get_metadata()

    assert "depth_levels" in meta
    assert len(meta["depth_levels"]) == 6
    assert meta["depth_levels"] == [0.0, 10.0, 50.0, 100.0, 200.0, 500.0]
    assert "water_temp" in meta["cf_metadata"]
    assert meta["cf_metadata"]["water_temp"]["units"] == "degC"


def test_netcdf_adapter_slice(fixture_nc_path):
    """NetCDFAdapter.get_slice() should return float32 array with CF metadata."""
    from backend.app.adapters.netcdf_adapter import NetCDFAdapter

    manifest = {
        "id": "test_temp", "label": "Test", "adapter": "NetCDFAdapter",
        "source": fixture_nc_path, "local_cache": fixture_nc_path,
        "variable": "water_temp", "standard_name": "sea_water_temperature",
        "units": "degC", "valid_min": -5.0, "valid_max": 40.0,
        "missing_value": -30000.0, "depth_levels": [0, 10, 50, 100, 200, 500],
    }
    adapter = NetCDFAdapter(manifest)
    result  = adapter.get_slice("water_temp", depth_m=10, time_idx=0, bbox=(80, 5, 100, 25))

    assert result.data.dtype == np.float32
    assert result.data.ndim  == 2     # (lat, lon)
    assert result.meta.units == "degC"
    assert result.meta.standard_name == "sea_water_temperature"
    # depth must have snapped to an actual level
    assert result.depth_m in [0.0, 10.0, 50.0, 100.0, 200.0, 500.0]


# ── Binary response tests ─────────────────────────────────────────────────────

def test_binary_response_format():
    """make_binary_response() should produce the correct [len][header][body] format."""
    from backend.app.endpoints.binary import make_binary_response

    data   = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32)
    header = {
        "variable": "water_temp", "units": "degC",
        "standard_name": "sea_water_temperature",
        "missing_value": -30000.0, "valid_min": -5.0, "valid_max": 40.0,
        "depth_levels": [0, 10, 50], "bounds": {},
    }
    response = make_binary_response(header, data)
    buf = response.body

    # Parse header length
    header_len = struct.unpack("<I", buf[:4])[0]
    parsed_header = json.loads(bytes(buf[4:4+header_len]))
    body = np.frombuffer(buf[4+header_len:], dtype=np.float32)

    assert parsed_header["variable"] == "water_temp"
    assert parsed_header["units"]    == "degC"
    assert parsed_header["shape"]    == [2, 2]
    assert len(body) == 4
    assert float(body[0]) == pytest.approx(1.0)


# ── FastAPI endpoint smoke tests ──────────────────────────────────────────────

@pytest.fixture
def app_client(fixture_nc_path):
    """Create a test client with the fixture dataset registered, bypassing lifespan."""
    from backend.app.main import app  # noqa: I001
    from backend.app.registry.loader import RegistryLoader
    from fastapi.testclient import TestClient

    # Build a minimal registry pointing at the fixture
    manifest = {
        "id": "fixture_temp", "label": "Fixture", "adapter": "NetCDFAdapter",
        "source": fixture_nc_path, "local_cache": fixture_nc_path,
        "variable": "water_temp", "standard_name": "sea_water_temperature",
        "units": "degC", "valid_min": -5.0, "valid_max": 40.0,
        "missing_value": -30000.0, "depth_levels": [0, 10, 50, 100, 200, 500],
    }
    from backend.app.adapters.netcdf_adapter import NetCDFAdapter
    adapter = NetCDFAdapter(manifest)

    registry = RegistryLoader.__new__(RegistryLoader)
    registry._manifests = {"fixture_temp": manifest}
    registry._adapters  = {"fixture_temp": adapter}
    registry.manifest_ids   = lambda: iter(registry._manifests.keys())
    registry.all_manifests  = lambda: list(registry._manifests.values())
    registry.get_adapter    = lambda manifest_id: registry._adapters[manifest_id]
    registry.get_manifest   = lambda manifest_id: registry._manifests[manifest_id]

    # No-op cache (no Redis needed in tests)
    class NoopCache:
        async def connect(self): pass
        async def close(self): pass
        # metric=... kwarg mirrors the real RedisCache.get_or_compute signature
        # (added in db53db1 for the /metrics last-updated tracking). The no-op cache
        # ignores it — it records no metrics — but must accept it or every
        # slice/volume/isosurface endpoint call raises TypeError under test.
        async def get_or_compute(self, key, ttl, fn, metric=None): return await fn()
        def metadata_key(self, s):    return f"meta:{s}"
        def slice_key(self, *a):      return "slice:test"
        def volume_key(self, *a):     return "volume:test"
        def isosurface_key(self, *a): return "iso:test"

    app.state.registry = registry
    app.state.cache    = NoopCache()
    app.state.db       = None

    # We need to mock RedisCache so the lifespan doesn't try to connect
    from unittest.mock import patch
    patcher1 = patch("backend.app.cache.RedisCache.connect", return_value=None)
    patcher1.start()

    with TestClient(app, raise_server_exceptions=True) as client:
        app.state.registry = registry
        app.state.cache = NoopCache()
        app.state.db = None
        yield client

    patcher1.stop()


def test_health(app_client):
    """Health check must return 200."""
    r = app_client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_metadata_endpoint(app_client):
    """Metadata endpoint must return depth_levels and CF metadata."""
    r = app_client.get("/api/metadata?source=fixture_temp")
    assert r.status_code == 200
    data = r.json()
    assert "depth_levels" in data
    assert len(data["depth_levels"]) == 6
    assert "cf_metadata" in data


def test_slice_endpoint_binary(app_client):
    """Slice endpoint must return binary with correct header."""
    r = app_client.get("/api/slice?source=fixture_temp&var=water_temp&depth=10&time=0&bbox=80,5,100,25")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"

    buf = r.content
    header_len = struct.unpack("<I", buf[:4])[0]
    header = json.loads(buf[4:4+header_len])

    assert header["variable"]      == "water_temp"
    assert header["units"]         == "degC"
    assert header["standard_name"] == "sea_water_temperature"
    assert "depth_levels" in header
    assert len(header["shape"])    == 2


def test_wms_getfeatureinfo(app_client):
    """WMS GetFeatureInfo returns the data value under a clicked pixel."""
    r = app_client.get(
        "/api/wms?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetFeatureInfo"
        "&QUERY_LAYERS=fixture_temp&LAYERS=fixture_temp&CRS=CRS:84"
        "&BBOX=80,5,100,25&WIDTH=200&HEIGHT=200&I=100&J=100&INFO_FORMAT=application/json"
    )
    assert r.status_code == 200
    feat = r.json()["features"][0]
    assert feat["properties"]["variable"] == "water_temp"
    assert "value" in feat["properties"]

    # Capabilities must advertise the operation and queryable layers.
    caps = app_client.get("/api/wms?REQUEST=GetCapabilities").text
    assert "GetFeatureInfo" in caps
    assert 'queryable="1"' in caps

    # WMS 1.3.0 axis order: EPSG:4326 BBOX is lat,lon and must resolve to the
    # same cell as the equivalent CRS:84 (lon,lat) request.
    base = ("/api/wms?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetFeatureInfo"
            "&QUERY_LAYERS=fixture_temp&WIDTH=200&HEIGHT=200&I=100&J=100&INFO_FORMAT=application/json")
    a = app_client.get(base + "&CRS=CRS:84&BBOX=80,5,100,25").json()["features"][0]
    b = app_client.get(base + "&CRS=EPSG:4326&BBOX=5,80,25,100").json()["features"][0]
    assert a["geometry"]["coordinates"] == b["geometry"]["coordinates"]


def test_ogc_endpoints_directory(app_client):
    """The OGC endpoint directory lists per-source access URLs."""
    r = app_client.get("/api/ogc/endpoints")
    assert r.status_code == 200
    body = r.json()
    assert body["conventions"] == "CF-1.8"
    src = next(s for s in body["sources"] if s["id"] == "fixture_temp")
    assert "GetMap" in src["wms_option_b"]
    assert "GetCoverage" in src["wcs_option_b"]
    # THREDDS links only appear for sources served from data/netcdf/ — not this tmp fixture.
    assert "opendap" not in src


def test_registry_reload_never_wipes(tmp_path, monkeypatch):
    """A reload that transiently reads 0 manifests must keep the existing set."""
    import pathlib

    from backend.app.registry.loader import RegistryLoader

    (tmp_path / "s.yaml").write_text(
        "id: s1\nadapter: DelimitedTextAdapter\nsource: x.csv\nvariable: v\n"
    )
    reg = RegistryLoader(str(tmp_path))
    reg.load_all()
    assert list(reg.manifest_ids()) == ["s1"]

    # Simulate a phantom empty directory read (Docker bind-mount hiccup).
    monkeypatch.setattr(pathlib.Path, "glob", lambda self, pat: iter([]))
    reg.reload()
    assert list(reg.manifest_ids()) == ["s1"]  # unchanged, not wiped


def test_delimited_text_depth_time(tmp_path):
    """DelimitedTextAdapter builds a (time, depth, lat, lon) cube from a long CSV."""
    import pandas as pd

    from backend.app.adapters.delimited_text_adapter import DelimitedTextAdapter

    rows = []
    for t in ("2026-08-01", "2026-08-02"):
        for d in (0, 20, 100):
            for la in (8, 12, 16):
                for lo in (82, 88, 94):
                    rows.append((la, lo, d, t, 25 - d * 0.05, 34 + d * 0.001))
    csv = tmp_path / "ts.csv"
    pd.DataFrame(rows, columns=["lat", "lon", "depth", "time", "temperature", "salinity"]).to_csv(csv, index=False)

    a = DelimitedTextAdapter({"id": "x", "source": str(csv), "local_cache": str(csv), "variable": "temperature"})
    meta = a.get_metadata()
    assert meta["available_variables"] == ["temperature", "salinity"]
    assert meta["depth_levels"] == [0.0, 20.0, 100.0]
    assert meta["time_range"]["steps"] == 2

    sr = a.get_slice("salinity", 100, 1, (80, 5, 96, 18))
    assert sr.data.shape == (3, 3)
    assert abs(sr.depth_m - 100.0) < 1e-6
    vr = a.get_volume("temperature", 0, (80, 5, 96, 18))
    assert vr.data.shape == (3, 3, 3)


def test_derived_water_masses(app_client):
    """k-means water-mass classification returns a label grid + ordered centroids."""
    cat = app_client.get("/api/derived").json()
    assert any(p["id"] == "water_masses" for p in cat["products"])

    r = app_client.get(
        "/api/derived/water_masses?source=fixture_temp&time=0&depth=0&bbox=80,8,95,20&k=3"
    )
    # fixture_temp has only temperature — expect a clean 422, not a crash.
    assert r.status_code == 422


def test_adcp_mooring_profiles(tmp_path, monkeypatch):
    """Seeded ADCP/mooring NetCDFs load through the profile cache reader."""
    import backend.app.ingest.seed_additional_sensors as seeder
    from backend.app.endpoints.profile import _load_from_local_cache

    seeder._write_profiles_nc(tmp_path / "mooring" / "m.nc", seeder.MOORINGS, with_chl=False)
    seeder._write_current_profiles_nc(tmp_path / "adcp" / "a.nc", seeder.ADCP_STATIONS)

    mooring = _load_from_local_cache(str(tmp_path / "mooring" / "m.nc"), seeder.MOORINGS[0][0])
    assert len(mooring["depth"]) > 5
    assert mooring["temperature"] and mooring["salinity"]

    adcp = _load_from_local_cache(str(tmp_path / "adcp" / "a.nc"), seeder.ADCP_STATIONS[0][0])
    assert adcp["current_speed"] and all(v >= 0 for v in adcp["current_speed"])
    assert adcp["units"]["current_speed"] == "m s-1"


def test_registry_upload_netcdf(fixture_nc_path, tmp_path, monkeypatch):
    """Uploading a NetCDF introspects it, writes a manifest, and hot-reloads it in."""
    from backend.app.endpoints import upload as upload_mod  # noqa: I001
    from backend.app.main import app
    from backend.app.registry.loader import RegistryLoader
    from fastapi.testclient import TestClient

    reg_dir = tmp_path / "registry"
    reg_dir.mkdir()
    up_dir = tmp_path / "uploads"
    monkeypatch.setattr(upload_mod, "REGISTRY_DIR", reg_dir)
    monkeypatch.setattr(upload_mod, "UPLOAD_DIR", up_dir)

    registry = RegistryLoader(str(reg_dir))
    registry.load_all()
    app.state.registry = registry
    app.state.db = None

    with TestClient(app) as client, open(fixture_nc_path, "rb") as fh:
        app.state.registry = registry
        app.state.db = None
        r = client.post(
            "/api/registry/upload",
            files={"file": ("my_ocean_grid.nc", fh, "application/x-netcdf")},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "my_ocean_grid"
    assert body["adapter"] == "NetCDFAdapter"
    assert body["variable"] == "water_temp"
    assert len(body["bbox"]) == 4
    assert (reg_dir / "my_ocean_grid.uploaded.yaml").exists()
    assert "my_ocean_grid" in list(registry.manifest_ids())
