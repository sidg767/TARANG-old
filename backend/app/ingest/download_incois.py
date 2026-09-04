#!/usr/bin/env python3
"""
download_incois.py — TARANG SIH 2026 PS 26067

Pulls LIVE data from the INCOIS THREDDS server (https://incois.gov.in/thredds/) — the
Indian government's own operational ocean products — and writes CF-normalised NetCDF into
data/netcdf/ for the registry to serve:

  data/netcdf/incois_currents.nc     osf/currents  NIO_HOOFS operational surface currents (uo/vo)
  data/netcdf/incois_sst.nc          osf/sst       operational SST forecast

(INCOIS' THREDDS osf/chl only carries a Pacific-Islands VIIRS cut — chlorophyll for the
 Indian Ocean comes from Copernicus instead, see download_copernicus.py.)

INCOIS' OSF files roll DAILY and carry a forecast window, so this picks the newest file in
each catalog automatically. Their variables use Ferret-style names (LON/LAT/TAXIS/DEPTH1_1,
U/V/SST/chlor_a) — we rename to the CF names the rest of the app expects.

Run inside the backend container:
  docker compose exec backend python backend/app/ingest/download_incois.py

Demo day: run this the morning of, THEN set OFFLINE_MODE=true — offline mode then serves
that day's real INCOIS forecast from the local cache.
"""
from __future__ import annotations

import os
import re
import ssl
import sys
import urllib.request
from pathlib import Path

import numpy as np
import xarray as xr

OUT_DIR = Path(os.getenv("DATA_DIR", "data")) / "netcdf"
OUT_DIR.mkdir(parents=True, exist_ok=True)

THREDDS = "https://incois.gov.in/thredds"
# India EEZ / north Indian Ocean — matches generate_fixtures.py and the Copernicus cache.
WEST, EAST, SOUTH, NORTH = 58.0, 100.0, 2.0, 26.0

# INCOIS' TLS chain is often incomplete; this is a public read of government open data.
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE


def _latest_urlpath(catalog: str, name_re: str) -> str | None:
    """Newest urlPath in a THREDDS catalog whose filename matches name_re (YYYYMMDD wins)."""
    url = f"{THREDDS}/catalog/{catalog}/catalog.xml"
    with urllib.request.urlopen(url, context=_SSL, timeout=60) as r:
        xml = r.read().decode("utf-8", "replace")
    hits = re.findall(r'urlPath="([^"]+)"', xml)
    cand = [h for h in hits if re.search(name_re, h)]
    if not cand:
        return None
    # sort by the 8-digit date embedded in the filename
    cand.sort(key=lambda h: (re.findall(r"(\d{8})", h) or ["0"])[-1])
    return cand[-1]


def _open(urlpath: str) -> xr.Dataset:
    return xr.open_dataset(f"{THREDDS}/dodsC/{urlpath}")


def _subset_ll(ds: xr.Dataset, lon="LON", lat="LAT") -> xr.Dataset:
    lo = ds[lon].values
    la = ds[lat].values
    lon_sel = slice(WEST, EAST) if lo[0] < lo[-1] else slice(EAST, WEST)
    lat_sel = slice(SOUTH, NORTH) if la[0] < la[-1] else slice(NORTH, SOUTH)
    return ds.sel({lon: lon_sel, lat: lat_sel})


def _write(ds: xr.Dataset, path: Path):
    enc = {v: {"zlib": True, "complevel": 4, "dtype": "float32"} for v in ds.data_vars}
    tmp = path.with_suffix(".tmp")
    ds.to_netcdf(tmp, encoding=enc)
    tmp.replace(path)
    print(f"  wrote {path}  ({path.stat().st_size/1e6:.1f} MB)  vars={list(ds.data_vars)} dims={dict(ds.sizes)}")


def do_currents() -> bool:
    up = _latest_urlpath("osf/currents", r"CURRENTS_NIO_\d{8}\.nc$")
    if not up:
        print("  currents: no NIO file in catalogue"); return False
    print(f"  currents: {up}")
    ds = _subset_ll(_open(up))[["U", "V"]]
    ds = ds.rename({"U": "uo", "V": "vo", "LON": "longitude", "LAT": "latitude",
                    "TAXIS": "time", "DEPTH1_1": "depth"})
    for v, sn, ln in [("uo", "eastward_sea_water_velocity", "Eastward Sea Water Velocity"),
                      ("vo", "northward_sea_water_velocity", "Northward Sea Water Velocity")]:
        ds[v] = ds[v].where(np.abs(ds[v]) < 1e30)
        ds[v].attrs.update(standard_name=sn, long_name=ln, units="m s-1",
                           valid_min=-3.0, valid_max=3.0)
    ds.attrs.update(title="INCOIS NIO-HOOFS operational surface currents",
                    institution="INCOIS", source="https://incois.gov.in/thredds osf/currents")
    _write(ds, OUT_DIR / "incois_currents.nc")
    return True


def do_sst() -> bool:
    up = _latest_urlpath("osf/sst", r"SST_IO_\d{8}\.nc$")
    if not up:
        print("  sst: no SST_IO file in catalogue"); return False
    print(f"  sst: {up}")
    ds = _subset_ll(_open(up))[["SST"]]
    ds = ds.rename({"SST": "analysed_sst", "LON": "longitude", "LAT": "latitude",
                    "TAXIS": "time", "DEPTH1_1": "depth"})
    ds["analysed_sst"] = ds["analysed_sst"].where(np.abs(ds["analysed_sst"]) < 1e30)
    ds["analysed_sst"].attrs.update(standard_name="sea_surface_temperature",
                                    long_name="Sea Surface Temperature", units="degC",
                                    valid_min=0.0, valid_max=40.0)
    ds.attrs.update(title="INCOIS operational SST forecast", institution="INCOIS",
                    source="https://incois.gov.in/thredds osf/sst")
    _write(ds, OUT_DIR / "incois_sst.nc")
    return True


if __name__ == "__main__":
    print(f"INCOIS THREDDS ingest → {OUT_DIR.resolve()}  (EEZ {WEST}-{EAST}E, {SOUTH}-{NORTH}N)")
    ok = []
    for name, fn in [("currents", do_currents), ("sst", do_sst)]:
        try:
            ok.append(fn())
        except Exception as e:
            print(f"  {name}: FAILED — {type(e).__name__}: {e}")
            ok.append(False)
    n = sum(1 for x in ok if x)

    # Merge SST + currents into one multi-variable file — the `incois_ocean` registry source,
    # whose Variable selector switches between analysed_sst / uo / vo.
    if n == 2:
        try:
            parts = [xr.open_dataset(OUT_DIR / f) for f in ("incois_sst.nc", "incois_currents.nc")]
            merged = xr.merge(parts, compat="override", join="override")
            merged.attrs.update(title="INCOIS Ocean State Forecast — India EEZ (SST + currents)",
                                institution="INCOIS")
            enc = {v: {"zlib": True, "complevel": 4, "dtype": "float32"} for v in merged.data_vars}
            merged.to_netcdf(OUT_DIR / "incois_ocean.nc", encoding=enc)
            print(f"  merged → incois_ocean.nc  vars={list(merged.data_vars)}")
        except Exception as e:
            print(f"  merge skipped: {e}")

    print(f"\n{n}/2 INCOIS products cached." + ("" if n == 2 else " Missing ones fall back to the synthetic fixture."))
    sys.exit(0 if n else 1)
