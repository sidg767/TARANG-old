#!/usr/bin/env python3
"""
generate_hf_radar_fixture.py — TARANG SIH 2026 PS 26067

Generates a synthetic surface-current NetCDF fixture standing in for a real HF-radar
(High Frequency radar) network — one of the additional sensor types the PS explicitly names
under "Extensible Design" (moorings, HF-radar, ADCP), which had no data source at all until
now despite the frontend's "Vectors" layer checkbox already existing for it.

Real HF-radar arrays (e.g. INCOIS operates a coastal network along the Indian east coast)
measure two-dimensional surface current VECTORS (u, v components), not a single scalar field —
that's the point of this fixture: it's a genuinely different shape of data from everything else
in the registry (temperature, salinity, chlorophyll), proving the plugin architecture handles
vector fields with the same "new YAML manifest, zero code change" pattern (see registry/
hf_radar_currents.yaml) by simply storing both components as data variables in one file, which
NetCDFAdapter.get_metadata() already lists generically as available_variables.

The pattern modeled here is a simplified East India Coastal Current (EICC) — a real, well-
documented western-boundary-style current that runs along the Bay of Bengal's western coast and
reverses direction seasonally — plus a broad basin-scale gyre, both built from closed-form
functions (not random noise) so the field looks like an actual current system, not static.
"""

import os
from datetime import datetime
from pathlib import Path

import netCDF4 as nc
import numpy as np

OUT_DIR = Path(os.getenv("DATA_DIR", "data")) / "netcdf"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Coarser than the temperature/salinity grids on purpose — arrow glyphs need one sample point
# per arrow, not one per pixel; a 0.083 deg grid would mean thousands of overlapping arrows.
# Full India EEZ extent (Arabian Sea + Bay of Bengal), matching generate_fixtures.py.
LON = np.linspace(58.0, 100.0, 43)   # ~1 deg spacing
LAT = np.linspace(2.0,  26.0,  25)
N_TIME = 8

NLAT, NLON = len(LAT), len(LON)
LONS, LATS = np.meshgrid(LON, LAT)


def make_currents():
    """(time, lat, lon) u and v surface current components, in m/s."""
    U = np.zeros((N_TIME, NLAT, NLON), dtype=np.float32)
    V = np.zeros((N_TIME, NLAT, NLON), dtype=np.float32)

    # Distance from the western boundary (coast), in degrees — the EICC hugs the coast and
    # decays offshore, matching real boundary-current structure.
    coast_dist = LONS - 80.0

    for ti in range(N_TIME):
        day_phase = ti / N_TIME * 2 * np.pi

        # EICC: flows northward (positive v) in one phase of the cycle, reverses in the other —
        # real EICC reversal is seasonal (Feb-Sep vs Oct-Jan); compressed to the demo's 8-step
        # window purely so the reversal is visible while scrubbing the time slider.
        eicc_strength = 0.6 * np.sin(day_phase)
        eicc_decay = np.exp(-coast_dist / 3.0)  # confined within ~3 deg of the coast
        v_eicc = eicc_strength * eicc_decay
        u_eicc = 0.15 * eicc_strength * eicc_decay * np.sin(coast_dist)  # slight cross-shore component

        # Basin-scale gyre: simple rotational field about the domain centre, weaker than the
        # boundary current, present everywhere (real BoB circulation has basin-scale eddies).
        cx, cy = 90.0, 15.0
        dx, dy = LONS - cx, LATS - cy
        r = np.sqrt(dx**2 + dy**2) + 1e-6
        gyre_strength = 0.15 * np.exp(-r / 8.0)
        u_gyre = -gyre_strength * dy / r
        v_gyre =  gyre_strength * dx / r

        # Arabian Sea gyre + West India Coastal Current (opposite rotation sense to the BoB gyre)
        axc, ayc = 65.0, 15.0
        adx, ady = LONS - axc, LATS - ayc
        ar = np.sqrt(adx**2 + ady**2) + 1e-6
        as_strength = 0.18 * np.exp(-ar / 8.0)
        u_as =  as_strength * ady / ar
        v_as = -as_strength * adx / ar
        wicc_decay = np.exp(-(LONS - 68.0) / 3.0)
        v_wicc = -0.5 * eicc_strength * np.clip(wicc_decay, 0, 1)   # seasonally reversing, like the EICC

        U[ti] = u_eicc + u_gyre + u_as
        V[ti] = v_eicc + v_gyre + v_as + v_wicc

    return U, V


def write_netcdf(path: Path, U: np.ndarray, V: np.ndarray):
    ds = nc.Dataset(str(path), "w", format="NETCDF4")
    ds.Conventions = "CF-1.8"
    ds.title       = "TARANG Synthetic HF-Radar Surface Currents — SIH 2026 PS 26067"
    ds.institution = "MoES/INCOIS"
    ds.source      = ("Synthetic demo standing in for a real HF-radar network — see "
                       "generate_hf_radar_fixture.py docstring for the modeled current pattern")
    ds.history     = f"Generated {datetime.utcnow().isoformat()}Z by generate_hf_radar_fixture.py"

    ds.createDimension("time",      N_TIME)
    ds.createDimension("latitude",  NLAT)
    ds.createDimension("longitude", NLON)

    tv = ds.createVariable("time", "f8", ("time",))
    tv.units    = "days since 2026-08-01 00:00:00"
    tv.calendar = "gregorian"
    tv[:] = np.arange(N_TIME, dtype=np.float64)

    latv = ds.createVariable("latitude", "f4", ("latitude",))
    latv.units = "degrees_north"
    latv[:] = LAT

    lonv = ds.createVariable("longitude", "f4", ("longitude",))
    lonv.units = "degrees_east"
    lonv[:] = LON

    uv = ds.createVariable("current_u", "f4", ("time", "latitude", "longitude"),
                            fill_value=-30000.0, zlib=True, complevel=6)
    uv.long_name     = "Eastward Surface Current Velocity"
    uv.standard_name = "eastward_sea_water_velocity"
    uv.units         = "m s-1"
    uv.valid_min     = -2.0
    uv.valid_max     = 2.0
    uv[:] = U

    vv = ds.createVariable("current_v", "f4", ("time", "latitude", "longitude"),
                            fill_value=-30000.0, zlib=True, complevel=6)
    vv.long_name     = "Northward Surface Current Velocity"
    vv.standard_name = "northward_sea_water_velocity"
    vv.units         = "m s-1"
    vv.valid_min     = -2.0
    vv.valid_max     = 2.0
    vv[:] = V

    ds.close()
    print(f"  ✓  Written: {path}  ({path.stat().st_size / 1024:.0f} kB)")


if __name__ == "__main__":
    print("TARANG HF-radar fixture generator — Bay of Bengal surface currents")
    print(f"Output -> {OUT_DIR.resolve()}\n")

    U, V = make_currents()
    write_netcdf(OUT_DIR / "hf_radar_currents.nc", U, V)

    print("\nDone. Backend registry will auto-discover this via registry/hf_radar_currents.yaml.")
