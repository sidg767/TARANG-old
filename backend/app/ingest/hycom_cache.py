"""
HYCOM Cache Script — Pre-fetches Bay of Bengal + Arabian Sea NetCDF subsets.

Run BEFORE demo day (§15, §20 Rule 8):
  python -m backend.app.ingest.hycom_cache

This prevents ANY live network calls to HYCOM OPeNDAP during judging.
HYCOM has documented transient outages during refresh windows (§7, §17).
"""

import logging
from pathlib import Path

import xarray as xr

logger = logging.getLogger("tarang.ingest.hycom")
logging.basicConfig(level=logging.INFO)

HYCOM_OPENDAP = "https://tds.hycom.org/thredds/dodsC/GLBy0.08/expt_93.0"

CACHE_JOBS = [
    {
        "name":      "BoB_primary",
        "bbox":      (80, 5, 100, 25),    # [min_lon, min_lat, max_lon, max_lat]
        "variables": ["water_temp", "salinity", "water_u", "water_v", "surf_el"],
        "n_steps":   8,                   # first 8 time steps
        "output":    "data/netcdf/hycom_bob.nc",
    },
    {
        "name":      "ArabianSea_secondary",
        "bbox":      (55, 5, 75, 25),
        "variables": ["water_temp", "salinity"],
        "n_steps":   4,
        "output":    "data/netcdf/hycom_arabian.nc",
    },
]


def fetch_and_cache(job: dict) -> None:
    """Fetch a HYCOM regional subset and save to local NetCDF."""
    out_path = Path(job["output"])
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if out_path.exists():
        logger.info(f"{job['name']}: already cached at {out_path} — skipping")
        return

    min_lon, min_lat, max_lon, max_lat = job["bbox"]
    logger.info(f"{job['name']}: connecting to HYCOM OPeNDAP…")
    logger.warning("⚠  If this takes > 5 minutes, HYCOM may be in a refresh window. Try again in 1 hour.")

    try:
        # Open lazily — nothing downloaded yet
        ds = xr.open_dataset(HYCOM_OPENDAP, engine="netcdf4", decode_times=False)

        # Subset: NEVER load global (§20 Rule 5)
        subset_vars = {}
        for var in job["variables"]:
            if var not in ds:
                logger.warning(f"Variable '{var}' not found in HYCOM dataset — skipping")
                continue

            data = ds[var].isel(time=slice(0, job["n_steps"])).sel(
                lat=slice(min_lat, max_lat),
                lon=slice(min_lon, max_lon),
            )
            subset_vars[var] = data.compute()  # pull from OPeNDAP NOW
            logger.info(f"  {var}: shape {subset_vars[var].shape}")

        # Build output dataset preserving CF attributes
        out_ds = xr.Dataset(subset_vars)

        # Add global attributes (CF Conventions)
        out_ds.attrs["Conventions"] = "CF-1.6"
        out_ds.attrs["title"]       = f"HYCOM GLBy0.08 {job['name']} subset"
        out_ds.attrs["source"]      = HYCOM_OPENDAP
        out_ds.attrs["tarang_bbox"] = f"{min_lon},{min_lat},{max_lon},{max_lat}"

        out_ds.to_netcdf(str(out_path))
        logger.info(f"{job['name']}: saved → {out_path}")

    except Exception as e:
        logger.error(f"{job['name']}: HYCOM fetch failed: {e}")
        logger.error("Ensure you are connected to the internet and HYCOM is not in a refresh window.")


if __name__ == "__main__":
    for job in CACHE_JOBS:
        fetch_and_cache(job)
    logger.info("HYCOM cache complete. Backend will use local files during demo. (§15)")
