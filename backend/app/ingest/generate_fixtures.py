#!/usr/bin/env python3
"""
generate_fixtures.py — TARANG SIH 2026 PS 26067

Generates oceanographically realistic NetCDF4 fixture files for the
Indian Ocean / Bay of Bengal demo region (80-100°E, 5-25°N).

These are NOT arbitrary random numbers — they follow real Indian Ocean
climatological patterns:
  - Sea Surface Temperature: 27–30°C in BoB, cooler at depth
  - Salinity: 30–34 PSU (BoB is fresher due to Brahmaputra/Ganges input)
  - Mixed layer depth: ~50m in summer
  - Seasonal thermocline: sharp gradient 50–200m

Run inside the backend container:
  docker compose exec backend python backend/app/ingest/generate_fixtures.py

Or directly:
  python generate_fixtures.py
"""

import os
from datetime import datetime
from pathlib import Path

import netCDF4 as nc
import numpy as np
from global_land_mask import globe

# ── Output path ───────────────────────────────────────────────────────────────
OUT_DIR = Path(os.getenv("DATA_DIR", "data")) / "netcdf"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Grid definition ───────────────────────────────────────────────────────────
# Covers India's full EEZ / northern Indian Ocean: Arabian Sea (west), Bay of Bengal
# (east), Lakshadweep & Andaman seas. 58–100°E, 2–26°N at 0.25° resolution.
LON = np.linspace(58.0, 100.0, 169)   # 0.25° resolution
LAT = np.linspace(2.0,  26.0,  97)
DEP = np.array([0, 5, 10, 20, 30, 50, 75, 100, 150, 200,
                250, 300, 400, 500, 750, 1000, 1500, 2000, 3000, 4000])
N_TIME = 8   # 8 daily snapshots

NLAT, NLON, NDEP = len(LAT), len(LON), len(DEP)
LONS, LATS = np.meshgrid(LON, LAT)

# The synthetic fields below are pure functions of lat/lon/depth — they have no notion of
# coastline, so without this they paint a "temperature"/"salinity" value over India, Myanmar,
# Bangladesh and Sri Lanka too. is_land() expects longitude in [-180, 180]; our grid is already
# in that range (80-100°E) so no wraparound handling is needed.
LAND_MASK = globe.is_land(LATS, LONS)  # (NLAT, NLON) bool — True where dry land

# ── Realistic temperature field (°C) ──────────────────────────────────────────
def make_temperature() -> np.ndarray:
    """4D array (time, depth, lat, lon) — oceanographically realistic."""
    T = np.zeros((N_TIME, NDEP, NLAT, NLON), dtype=np.float32)

    for ti in range(N_TIME):
        day_offset = ti * 1.0
        for di, depth in enumerate(DEP):
            # SST pattern: warm pool centre ~90°E, 15°N
            sst_base = 29.5
            sst_grad = (
                - 0.03 * np.abs(LONS - 88)      # cooling away from centre lon
                - 0.05 * np.abs(LATS - 13)      # cooling away from centre lat
                + 0.15 * np.sin(2*np.pi * day_offset/30)  # 30-day oscillation
                + 0.3  * np.random.randn(NLAT, NLON) * 0.05  # noise
                # Arabian Sea summer upwelling: cool band hugging the Somali/Oman boundary
                - 2.0 * np.exp(-((LONS - 57)**2) / 45.0) * np.clip((LATS - 6) / 16.0, 0, 1)
            )
            sst = np.clip(sst_base + sst_grad, 25.0, 31.0)

            # Depth profile: mixed layer (0-50m) then sharp thermocline
            if depth <= 50:
                layer_temp = sst - depth * 0.015
            elif depth <= 200:
                # Thermocline: steep ~0.05°C/m
                mixed_t = sst - 50 * 0.015
                layer_temp = mixed_t - (depth - 50) * 0.055
            elif depth <= 1000:
                # Deep ocean: 8°C at 200m → 4°C at 1000m
                t200 = sst - 50*0.015 - 150*0.055
                layer_temp = t200 - (depth - 200) * 0.005
            else:
                # Abyssal: 2-3°C
                layer_temp = 2.5 + 0.5 * np.random.randn(NLAT, NLON) * 0.02

            # Add mesoscale eddies (warm/cold core) — Bay of Bengal + Arabian Sea
            eddy_warm = 1.5 * np.exp(-((LONS-87)**2 + (LATS-18)**2)/25)
            eddy_cold = -1.2 * np.exp(-((LONS-93)**2 + (LATS-10)**2)/20)
            eddy_as_warm = 1.3 * np.exp(-((LONS-64)**2 + (LATS-17)**2)/22)   # Great Whirl-type
            eddy_as_cold = -1.1 * np.exp(-((LONS-68)**2 + (LATS-12)**2)/18)
            eddy_factor = np.exp(-depth / 150)  # eddies decay with depth
            layer_temp = layer_temp + (eddy_warm + eddy_cold + eddy_as_warm + eddy_as_cold) * eddy_factor

            T[ti, di] = np.clip(layer_temp, 1.0, 32.0)

    return T

# ── Realistic salinity field (PSU) ────────────────────────────────────────────
def make_salinity() -> np.ndarray:
    """4D array (time, depth, lat, lon). BoB is fresher (low salinity) due to river runoff."""
    S = np.zeros((N_TIME, NDEP, NLAT, NLON), dtype=np.float32)

    for ti in range(N_TIME):
        for di, depth in enumerate(DEP):
            # Surface: BoB freshwater lens (30-32 PSU near river mouths)
            s_surf = (
                33.5
                - 2.5 * np.exp(-((LONS - 88)**2 + (LATS - 20)**2) / 30)   # Ganges-Brahmaputra plume
                - 1.0 * np.exp(-((LONS - 80)**2 + (LATS - 11)**2) / 10)   # Sri Lanka coast
                + 0.5 * np.exp(-((LONS - 95)**2 + (LATS - 15)**2) / 15)   # Andaman sea saltier
                + 2.0 * np.exp(-((LONS - 64)**2 + (LATS - 18)**2) / 70)   # Arabian Sea high-salinity water (evaporation-driven)
            )

            if depth <= 100:
                layer_sal = s_surf + depth * 0.012
            elif depth <= 500:
                layer_sal = s_surf + 100*0.012 + (depth-100) * 0.004
            else:
                # Deep: relatively uniform 34.8 PSU
                layer_sal = 34.8 + 0.05 * np.random.randn(NLAT, NLON) * 0.1

            S[ti, di] = np.clip(layer_sal, 28.0, 36.0)

    return S

# ── Realistic current field (m/s) ─────────────────────────────────────────────
def make_currents() -> tuple[np.ndarray, np.ndarray]:
    """(u, v) 4D (time, depth, lat, lon): a plausible BoB gyre + coastal jet, decaying with depth."""
    U = np.zeros((N_TIME, NDEP, NLAT, NLON), dtype=np.float32)
    V = np.zeros((N_TIME, NDEP, NLAT, NLON), dtype=np.float32)

    # Bay of Bengal gyre centre ~90°E, 15°N; streamfunction ψ ∝ exp(-r²), (u,v) = (-∂ψ/∂y, ∂ψ/∂x)
    r2 = ((LONS - 90) ** 2 + (LATS - 15) ** 2) / 60.0
    psi = np.exp(-r2)
    u_surf = -(-2 * (LATS - 15) / 60.0) * psi        # -∂ψ/∂y
    v_surf = (-2 * (LONS - 90) / 60.0) * psi         #  ∂ψ/∂x

    # Arabian Sea gyre centre ~65°E, 15°N (opposite rotation sense)
    r2_as = ((LONS - 65) ** 2 + (LATS - 15) ** 2) / 55.0
    psi_as = 0.9 * np.exp(-r2_as)
    u_surf = u_surf + (2 * (LATS - 15) / 55.0) * psi_as
    v_surf = v_surf - (2 * (LONS - 65) / 55.0) * psi_as

    # East India Coastal Current along the western BoB boundary (~80-82°E)
    jet = 0.8 * np.exp(-((LONS - 81) ** 2) / 4.0) * np.clip((LATS - 6) / 12.0, 0, 1)
    # West India Coastal Current along the eastern Arabian Sea boundary (~71-73°E)
    wicc = 0.7 * np.exp(-((LONS - 72) ** 2) / 5.0) * np.clip((LATS - 8) / 14.0, 0, 1)
    u_surf = u_surf * 1.2
    v_surf = (v_surf + jet - wicc) * 1.2

    for ti in range(N_TIME):
        phase = 1.0 + 0.1 * np.sin(2 * np.pi * ti / 30)
        for di, depth in enumerate(DEP):
            decay = np.exp(-depth / 250.0)   # currents weaken with depth
            U[ti, di] = np.clip(u_surf * phase * decay, -2.0, 2.0)
            V[ti, di] = np.clip(v_surf * phase * decay, -2.0, 2.0)

    return U, V


# ── Write NetCDF4 files ────────────────────────────────────────────────────────
def write_netcdf(path: Path, var_name: str, data: np.ndarray,
                 long_name: str, units: str, valid_min: float, valid_max: float):
    ds = nc.Dataset(str(path), "w", format="NETCDF4")
    ds.Conventions = "CF-1.8"
    ds.title       = f"TARANG Indian Ocean {long_name} — SIH 2026 PS 26067"
    ds.institution = "MoES/INCOIS"
    ds.source      = "Climatological simulation for TARANG SIH demo"
    ds.history     = f"Generated {datetime.utcnow().isoformat()}Z by generate_fixtures.py"

    ds.createDimension("time",      N_TIME)
    ds.createDimension("depth",     NDEP)
    ds.createDimension("latitude",  NLAT)
    ds.createDimension("longitude", NLON)

    # Coordinate variables
    tv = ds.createVariable("time",      "f8", ("time",))
    tv.units    = "days since 2026-08-01 00:00:00"
    tv.calendar = "gregorian"
    tv[:] = np.arange(N_TIME, dtype=np.float64)

    dv = ds.createVariable("depth",     "f4", ("depth",))
    dv.units     = "m"
    dv.positive  = "down"
    dv[:] = DEP

    latv = ds.createVariable("latitude",  "f4", ("latitude",))
    latv.units = "degrees_north"
    latv[:] = LAT

    lonv = ds.createVariable("longitude", "f4", ("longitude",))
    lonv.units = "degrees_east"
    lonv[:] = LON

    # Data variable
    v = ds.createVariable(var_name, "f4",
                          ("time", "depth", "latitude", "longitude"),
                          fill_value=-30000.0, zlib=True, complevel=6)
    v.long_name   = long_name
    v.units       = units
    v.valid_min   = valid_min
    v.valid_max   = valid_max
    v.standard_name = var_name
    # Mask land cells across every time/depth so the frontend's colormap discard (val ==
    # _FillValue) hides them instead of painting an ocean variable over dry land.
    data = data.copy()
    data[:, :, LAND_MASK] = -30000.0
    v[:] = data

    ds.close()
    print(f"  ✓  Written: {path}  ({path.stat().st_size / 1024:.0f} kB)")


def write_netcdf_multi(path: Path, variables: list[dict]):
    """Write several data variables into one CF NetCDF (the combined HYCOM fixture).
    Each entry: {name, data, long_name, units, standard_name, valid_min, valid_max}."""
    ds = nc.Dataset(str(path), "w", format="NETCDF4")
    ds.Conventions = "CF-1.8"
    ds.title       = "TARANG HYCOM GLBy0.08 Bay of Bengal subset — SIH 2026 PS 26067"
    ds.institution = "MoES/INCOIS"
    ds.source      = "Climatological simulation for TARANG SIH demo (stand-in for HYCOM GLBy0.08 expt_93.0)"
    ds.history     = f"Generated {datetime.utcnow().isoformat()}Z by generate_fixtures.py"

    ds.createDimension("time",      N_TIME)
    ds.createDimension("depth",     NDEP)
    ds.createDimension("latitude",  NLAT)
    ds.createDimension("longitude", NLON)

    tv = ds.createVariable("time", "f8", ("time",))
    tv.units = "days since 2026-08-01 00:00:00"
    tv.calendar = "gregorian"
    tv[:] = np.arange(N_TIME, dtype=np.float64)

    dv = ds.createVariable("depth", "f4", ("depth",))
    dv.units = "m"
    dv.positive = "down"
    dv[:] = DEP

    latv = ds.createVariable("latitude", "f4", ("latitude",))
    latv.units = "degrees_north"
    latv[:] = LAT
    lonv = ds.createVariable("longitude", "f4", ("longitude",))
    lonv.units = "degrees_east"
    lonv[:] = LON

    for spec in variables:
        v = ds.createVariable(spec["name"], "f4",
                              ("time", "depth", "latitude", "longitude"),
                              fill_value=-30000.0, zlib=True, complevel=6)
        v.long_name     = spec["long_name"]
        v.units         = spec["units"]
        v.standard_name = spec.get("standard_name", spec["name"])
        v.valid_min     = spec["valid_min"]
        v.valid_max     = spec["valid_max"]
        # Same land mask as write_netcdf() — see its comment.
        data = spec["data"].copy()
        data[:, :, LAND_MASK] = -30000.0
        v[:] = data

    ds.close()
    print(f"  ✓  Written: {path}  ({path.stat().st_size / 1024:.0f} kB)  [vars: {[s['name'] for s in variables]}]")


if __name__ == "__main__":
    print("TARANG fixture generator — Indian Ocean / Bay of Bengal")
    print(f"Output → {OUT_DIR.resolve()}\n")

    print("Generating temperature field (this takes ~5s)...")
    T = make_temperature()
    write_netcdf(
        OUT_DIR / "hycom_water_temp.nc",
        "water_temp", T,
        "Sea Water Temperature", "degC",
        valid_min=1.0, valid_max=32.0,
    )

    print("Generating salinity field...")
    S = make_salinity()
    write_netcdf(
        OUT_DIR / "hycom_salinity.nc",
        "salinity", S,
        "Sea Water Practical Salinity", "1",
        valid_min=28.0, valid_max=36.0,
    )

    print("Generating currents field...")
    U, V = make_currents()

    print("Writing combined multi-variable HYCOM fixture...")
    write_netcdf_multi(
        OUT_DIR / "hycom_bob.nc",
        [
            {"name": "water_temp", "data": T, "long_name": "Sea Water Temperature",
             "units": "degC", "standard_name": "sea_water_temperature",
             "valid_min": 1.0, "valid_max": 32.0},
            {"name": "salinity", "data": S, "long_name": "Sea Water Practical Salinity",
             "units": "1", "standard_name": "sea_water_practical_salinity",
             "valid_min": 28.0, "valid_max": 36.0},
            {"name": "water_u", "data": U, "long_name": "Eastward Sea Water Velocity",
             "units": "m s-1", "standard_name": "eastward_sea_water_velocity",
             "valid_min": -2.0, "valid_max": 2.0},
            {"name": "water_v", "data": V, "long_name": "Northward Sea Water Velocity",
             "units": "m s-1", "standard_name": "northward_sea_water_velocity",
             "valid_min": -2.0, "valid_max": 2.0},
        ],
    )

    # registry/copernicus_temp.yaml and copernicus_salinity.yaml have no fixture generator of
    # their own — they're labeled "LIVE" and fall back to a real Copernicus Marine fetch outside
    # this bbox (needs COPERNICUS_USERNAME/PASSWORD), but had nothing at all for the demo region
    # without those credentials. Reuse the same physically-grounded BoB fields above under the
    # variable names/attrs those manifests actually declare (thetao/so — Copernicus' real CF
    # names), rather than leaving them with zero local data.
    print("Generating Copernicus-labeled temperature field (reuses HYCOM field above)...")
    write_netcdf(
        OUT_DIR / "copernicus_bob_temp.nc",
        "thetao", T,
        "Sea Water Potential Temperature", "degrees_C",
        valid_min=-10.0, valid_max=40.0,
    )

    print("Generating Copernicus-labeled salinity field (reuses HYCOM field above)...")
    write_netcdf(
        OUT_DIR / "copernicus_bob_salinity.nc",
        "so", S,
        "Sea Water Salinity", "1e-3",
        valid_min=0.0, valid_max=50.0,
    )

    # Synthetic stand-in for copernicus_currents.nc under CMEMS' real variable names (uo/vo),
    # so the currents source always has a file. download_copernicus.py overwrites this with
    # real Copernicus data when run — same fallback pattern as temp/salinity above.
    print("Generating Copernicus-labeled currents field (reuses HYCOM u/v above)...")
    write_netcdf_multi(
        OUT_DIR / "copernicus_currents.nc",
        [
            {"name": "uo", "data": U, "long_name": "Eastward Sea Water Velocity",
             "units": "m s-1", "standard_name": "eastward_sea_water_velocity",
             "valid_min": -3.0, "valid_max": 3.0},
            {"name": "vo", "data": V, "long_name": "Northward Sea Water Velocity",
             "units": "m s-1", "standard_name": "northward_sea_water_velocity",
             "valid_min": -3.0, "valid_max": 3.0},
        ],
    )

    # Offline fallbacks for the merged INCOIS + Copernicus sources and the Copernicus-BGC
    # source — download_incois.py / download_copernicus.py overwrite these with real data.
    print("Generating merged Copernicus Marine fallback (T/S/currents)...")
    write_netcdf_multi(
        OUT_DIR / "copernicus_marine.nc",
        [
            {"name": "thetao", "data": T, "long_name": "Sea Water Potential Temperature",
             "units": "degrees_C", "standard_name": "sea_water_potential_temperature",
             "valid_min": -10.0, "valid_max": 40.0},
            {"name": "so", "data": S, "long_name": "Sea Water Salinity",
             "units": "1e-3", "standard_name": "sea_water_salinity",
             "valid_min": 0.0, "valid_max": 45.0},
            {"name": "uo", "data": U, "long_name": "Eastward Sea Water Velocity",
             "units": "m s-1", "standard_name": "eastward_sea_water_velocity",
             "valid_min": -3.0, "valid_max": 3.0},
            {"name": "vo", "data": V, "long_name": "Northward Sea Water Velocity",
             "units": "m s-1", "standard_name": "northward_sea_water_velocity",
             "valid_min": -3.0, "valid_max": 3.0},
        ],
    )

    print("Generating merged INCOIS Ocean State fallback (SST + currents)...")
    write_netcdf_multi(
        OUT_DIR / "incois_ocean.nc",
        [
            {"name": "analysed_sst", "data": T, "long_name": "Sea Surface Temperature",
             "units": "degC", "standard_name": "sea_surface_temperature",
             "valid_min": 0.0, "valid_max": 40.0},
            {"name": "uo", "data": U, "long_name": "Eastward Sea Water Velocity",
             "units": "m s-1", "standard_name": "eastward_sea_water_velocity",
             "valid_min": -3.0, "valid_max": 3.0},
            {"name": "vo", "data": V, "long_name": "Northward Sea Water Velocity",
             "units": "m s-1", "standard_name": "northward_sea_water_velocity",
             "valid_min": -3.0, "valid_max": 3.0},
        ],
    )

    # Synthetic chlorophyll-a: surface-maximum, coastal/upwelling/river-plume enriched,
    # decaying with depth (deep chlorophyll max near ~50 m).
    print("Generating Copernicus-BGC chlorophyll fallback...")
    CHL = np.zeros((N_TIME, NDEP, NLAT, NLON), dtype=np.float32)
    coast = (0.9 * np.exp(-((LONS - 72) ** 2) / 8) + 0.7 * np.exp(-((LONS - 82) ** 2) / 6))
    plume = 1.4 * np.exp(-((LONS - 88) ** 2 + (LATS - 20) ** 2) / 40)
    somali = 1.1 * np.exp(-((LONS - 58) ** 2) / 30) * np.clip((LATS - 6) / 16, 0, 1)
    surf_chl = np.clip(0.06 + 0.12 * np.exp(-((LATS - 12) ** 2) / 120)
                       + coast * np.clip((26 - LATS) / 24, 0, 1) * 0.5 + plume + somali, 0.03, 8.0)
    for ti in range(N_TIME):
        for di, depth in enumerate(DEP):
            dcm = 1.0 + 0.6 * np.exp(-((depth - 50) / 30) ** 2)     # deep chlorophyll max ~50 m
            CHL[ti, di] = surf_chl * dcm * np.exp(-depth / 120.0)
    write_netcdf(
        OUT_DIR / "copernicus_chlorophyll.nc",
        "chl", CHL,
        "Chlorophyll-a Concentration", "mg m-3",
        valid_min=0.0, valid_max=20.0,
    )

    print("\nDone! Fixture NetCDF files written successfully.")
    print("Backend registry will auto-discover these via the YAML manifests.")
