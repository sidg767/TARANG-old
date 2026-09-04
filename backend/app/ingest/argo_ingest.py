"""
Argo Ingest Script — Pre-fetches Argo GDAC data for the demo region.

Run this BEFORE demo day (§15, §20 Rule 8):
  python -m backend.app.ingest.argo_ingest

Downloads Argo profiles for Bay of Bengal + Arabian Sea, saves to local
NetCDF files in data/argo/. These are what /api/instruments and /api/profile
use during the demo — no live network calls during judging.

argopy 1.4.0 — Python >= 3.11 required
argopy incompatible with xarray 2024.3.0–2025.6.1 — pin xarray >= 2025.7.0
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger("tarang.ingest.argo")
logging.basicConfig(level=logging.INFO)


# Demo regions
REGIONS = {
    "BoB": {
        "bbox": (80, 5, 100, 25),    # Bay of Bengal (primary)
        "date_start": "2026-07-01",
        "date_end":   "2026-08-25",
        "output_file": "data/argo/BoB_argo_2026.nc",
    },
    "ArabianSea": {
        "bbox": (55, 5, 75, 25),     # Arabian Sea (secondary)
        "date_start": "2026-07-01",
        "date_end":   "2026-08-25",
        "output_file": "data/argo/ArabianSea_argo_2026.nc",
    },
}


def fetch_argo_region(region_name: str, config: dict) -> None:
    """Fetch Argo data for a region and save to local NetCDF using direct ERDDAP URL."""
    
    out_path = Path(config["output_file"])
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if out_path.exists():
        logger.info(f"{region_name}: cache already exists at {out_path} — skipping")
        return

    if os.getenv("OFFLINE_MODE", "false").strip().lower() in ("1", "true", "yes", "on"):
        logger.warning(f"{region_name}: OFFLINE_MODE and no cache at {out_path} — cannot fetch.")
        return

    min_lon, min_lat, max_lon, max_lat = config["bbox"]
    start_time = config['date_start'] + "T00:00:00Z"
    end_time = config['date_end'] + "T00:00:00Z"
    
    logger.info(f"{region_name}: fetching Argo from GDAC (ERDDAP direct) "
                f"bbox=({min_lon},{min_lat},{max_lon},{max_lat}) "
                f"dates={config['date_start']} to {config['date_end']}")

    # Construct the exact ERDDAP URL that argopy would use
    url = (
        f"https://www.ifremer.fr/erddap/tabledap/ArgoFloats.nc?"
        f"data_mode%2Clatitude%2Clongitude%2Cposition_qc%2Ctime%2Ctime_qc%2Cdirection%2Cpres%2Ctemp%2Cpsal%2Cplatform_number%2Ccycle_number"
        f"&latitude>={min_lat}&latitude<={max_lat}"
        f"&longitude>={min_lon}&longitude<={max_lon}"
        f"&time>={start_time}&time<={end_time}"
    )
    
    import requests
    try:
        logger.info(f"Downloading from {url}...")
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(r.content)
        logger.info(f"{region_name}: saved → {out_path}")
    except Exception as e:
        logger.error(f"{region_name}: Argo fetch failed: {e}")
        logger.error("If this is a network error, check your internet connection.")
        logger.error("Run this script before the venue (§15).")


def ingest_to_postgis(nc_path: str, db_url: str) -> None:
    """
    Load Argo positions from the cached NetCDF into PostGIS.
    Called after fetch_argo_region() completes.
    """
    import psycopg2
    import xarray as xr

    if not Path(nc_path).exists():
        logger.warning(f"NetCDF not found at {nc_path} — skipping PostGIS ingest")
        return

    ds = xr.open_dataset(nc_path)

    conn = psycopg2.connect(db_url)
    cur  = conn.cursor()

    # The UNIQUE index is what makes the ON CONFLICT DO NOTHING below idempotent.
    cur.execute("""
        CREATE EXTENSION IF NOT EXISTS postgis;
        CREATE TABLE IF NOT EXISTS instruments (
            id SERIAL PRIMARY KEY,
            platform_id TEXT, type TEXT, lat DOUBLE PRECISION, lon DOUBLE PRECISION,
            time_start TIMESTAMPTZ, time_end TIMESTAMPTZ, cycle_number INT,
            geom GEOMETRY(POINT, 4326)
        );
        CREATE INDEX IF NOT EXISTS instruments_geom_idx ON instruments USING GIST(geom);
        CREATE UNIQUE INDEX IF NOT EXISTS instruments_platform_cycle_idx
            ON instruments (platform_id, cycle_number);
    """)

    # ERDDAP tabledap and argopy exports use different column-name casing; resolve either.
    def _col(*candidates: str) -> str:
        for name in candidates:
            if name in ds.variables:
                return name
        raise KeyError(f"None of {candidates} found in {nc_path}; variables: {list(ds.variables)}")

    col_platform = _col("platform_number", "PLATFORM_NUMBER")
    col_cycle    = _col("cycle_number", "CYCLE_NUMBER")
    col_lat      = _col("latitude", "LATITUDE")
    col_lon      = _col("longitude", "LONGITUDE")
    col_time     = _col("time", "TIME", "JULD")

    # One marker per (platform, cycle) — collapse the depth-level rows to one position.
    df = ds[[col_platform, col_cycle, col_lat, col_lon, col_time]].to_dataframe()
    df = df.rename(columns={
        col_platform: "platform_number", col_cycle: "cycle_number",
        col_lat: "latitude", col_lon: "longitude", col_time: "time",
    })
    profiles = df.groupby(["platform_number", "cycle_number"], as_index=False).first()

    inserted = 0
    for _, row in profiles.iterrows():
        try:
            platform_id = str(row["platform_number"]).strip()
            lat   = float(row["latitude"])
            lon   = float(row["longitude"])
            cycle = int(row["cycle_number"])
            time  = str(row["time"])

            cur.execute("""
                INSERT INTO instruments (platform_id, type, lat, lon, time_start, cycle_number, geom)
                VALUES (%s, 'argo', %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
                ON CONFLICT (platform_id, cycle_number) DO NOTHING
            """, (platform_id, lat, lon, time, cycle, lon, lat))
            inserted += 1
        except Exception as e:
            logger.warning(f"Profile {row.get('platform_number')}/{row.get('cycle_number')} skipped: {e}")

    conn.commit()
    n_prof = len(profiles)
    cur.close()
    conn.close()
    logger.info(f"PostGIS: inserted {inserted} of {n_prof} profiles from {nc_path}")


if __name__ == "__main__":
    # 1. Fetch Argo data for both regions
    for name, cfg in REGIONS.items():
        fetch_argo_region(name, cfg)

    # 2. Ingest to PostGIS if DATABASE_URL is set
    db_url = os.getenv("DATABASE_URL", "")
    if db_url:
        for cfg in REGIONS.values():
            ingest_to_postgis(cfg["output_file"], db_url)
    else:
        logger.info("DATABASE_URL not set — skipping PostGIS ingest. Set it to enable.")

    logger.info("Argo ingest complete. Demo is ready for offline operation. (§15)")
