"""NOAA Marine Cadastre AIS backfill pipeline.

Backfills historical AIS for the **USGC AOI** (TDD §4.2.1 §11) using NOAA
Marine Cadastre's free archive (coast.noaa.gov/htdata/CMSP/AISDataHandler/).
Coverage is US territorial waters only — sufficient for TD22 (USGC→Asia)
backtest depth but useless for TD3C / TD15 destinations.

Per day, this script:
  1. Downloads the daily AIS .zip from NOAA (one CSV per file post-2015).
  2. Streams the CSV in chunks, filtering to crude-tanker MMSIs
     (VesselType in 80-89) within the USGC bbox.
  3. Normalizes to the aisstream Phase 1 schema (mmsi/time_utc/latitude/...).
  4. Writes two outputs:
     - Raw filtered                → marinecadastre_raw/date=YYYY-MM-DD/data.parquet
     - 4-hour resampled snapshots  → positions/source=marinecadastre/date=YYYY-MM-DD/HH-00.parquet
       (six buckets: 00,04,08,12,16,20 UTC — matches the aisstream Phase 1 cadence
       so backtests see apples-to-apples data)

Idempotent: skips dates where the raw output already exists. Re-run a date by
deleting its raw_root/date=YYYY-MM-DD directory first.

Usage:
    # Smoke test — one week
    python pipelines/marine_cadastre_ingest.py --from 2024-01-01 --to 2024-01-07

    # Full backfill — 2019 through 2026
    python pipelines/marine_cadastre_ingest.py --from 2019-01-01 --to 2026-04-30

    # Monthly catch-up (NOAA publishes ~1 month lagged)
    python pipelines/marine_cadastre_ingest.py --from 2026-04-01 --to 2026-04-30
"""

from __future__ import annotations

import argparse
import http.client
import io
import logging
import socket
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

# USGC AOI bbox — same as the SAR pipeline's usgc AOI in scheduler.py.
USGC_BBOX = (-98.0, 26.0, -88.0, 31.0)   # lon_min, lat_min, lon_max, lat_max

# Crude tankers per ITU-R M.1371 ShipType codes 80-89.
TANKER_VESSEL_TYPES = set(range(80, 90))

NOAA_URL_TEMPLATE = (
    "https://coast.noaa.gov/htdata/CMSP/AISDataHandler/"
    "{year}/AIS_{year}_{month:02d}_{day:02d}.zip"
)

# 4-hour buckets matching the aisstream Phase 1 cron (0,4,8,12,16,20 UTC).
RESAMPLE_HOURS = (0, 4, 8, 12, 16, 20)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s marine_cadastre %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("marine_cadastre")


def url_for(d: date) -> str:
    return NOAA_URL_TEMPLATE.format(year=d.year, month=d.month, day=d.day)


def _download_zip(url: str, max_attempts: int = 4) -> bytes | None:
    """Fetch a NOAA AIS zip with bounded retries. Returns None on 404/403
    (NOAA has no file for that date); raises after the final attempt for
    other failures. Transient errors retried: IncompleteRead (partial body),
    socket timeouts, BadStatusLine, generic URLError network failures.

    These files are 100-300 MB and the NOAA endpoint drops connections
    intermittently — observed ~2-of-7 failure rate on a clean 7-day pull
    without retry. Backoff is 2/4/8s; total worst case ~14s of sleeps."""
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "oil-inventory-dashboard/1.0"})
            with urllib.request.urlopen(req, timeout=240) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (404, 403):
                return None
            last_exc = e
        except (http.client.IncompleteRead,
                http.client.BadStatusLine,
                socket.timeout,
                urllib.error.URLError,
                ConnectionError) as e:
            last_exc = e
        if attempt < max_attempts:
            sleep_s = 2 ** attempt
            log.warning("download retry attempt=%d/%d sleep=%ds err=%s url=%s",
                        attempt, max_attempts, sleep_s, type(last_exc).__name__, url)
            time.sleep(sleep_s)
    assert last_exc is not None
    raise last_exc


def download_and_filter(d: date) -> pd.DataFrame | None:
    """Download one day's NOAA AIS zip and return tanker rows in the USGC bbox.
    Returns None if NOAA has no file for that date (404)."""
    url = url_for(d)
    zip_bytes = _download_zip(url)
    if zip_bytes is None:
        return None

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            log.warning("no_csv_in_zip date=%s contents=%s", d, zf.namelist())
            return None
        if len(csv_names) > 1:
            log.warning("multi_csv_in_zip date=%s using=%s skip=%s",
                        d, csv_names[0], csv_names[1:])
        lon_min, lat_min, lon_max, lat_max = USGC_BBOX
        kept = []
        with zf.open(csv_names[0]) as csv_f:
            # Stream in chunks — single day can be 5-15M rows
            for chunk in pd.read_csv(
                csv_f,
                chunksize=500_000,
                dtype={"MMSI": "int64", "VesselType": "Int64"},
                parse_dates=["BaseDateTime"],
                low_memory=False,
                on_bad_lines="warn",
            ):
                mask = (
                    chunk["VesselType"].isin(TANKER_VESSEL_TYPES)
                    & chunk["LAT"].between(lat_min, lat_max)
                    & chunk["LON"].between(lon_min, lon_max)
                )
                if mask.any():
                    kept.append(chunk.loc[mask])
        return pd.concat(kept, ignore_index=True) if kept else pd.DataFrame()


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Map NOAA column names to the aisstream Phase 1 schema, preserving the
    extra NOAA fields (VesselName, IMO, Length, Width, Draft) which the
    aisstream feed doesn't broadcast and would otherwise be lost."""
    return pd.DataFrame({
        "mmsi":          df["MMSI"].astype("int64"),
        "time_utc":      pd.to_datetime(df["BaseDateTime"], utc=True),
        "latitude":      df["LAT"].astype("float64"),
        "longitude":     df["LON"].astype("float64"),
        "sog":           df.get("SOG", pd.NA).astype("float32"),
        "cog":           df.get("COG", pd.NA).astype("float32"),
        "true_heading":  df.get("Heading", pd.NA).astype("float32"),
        "nav_status":    df.get("Status"),
        # NOAA-only columns kept for richer downstream analysis
        "vessel_name":   df.get("VesselName"),
        "imo":           df.get("IMO"),
        "call_sign":     df.get("CallSign"),
        "vessel_type":   df["VesselType"].astype("Int64"),
        "length_m":      df.get("Length", pd.NA).astype("float32"),
        "width_m":       df.get("Width", pd.NA).astype("float32"),
        "draught_m":     df.get("Draft", pd.NA).astype("float32"),
    })


def resample_to_buckets(df: pd.DataFrame) -> dict[int, pd.DataFrame]:
    """Bucket positions into 4-hour windows starting at 0,4,8,12,16,20 UTC.
    Each bucket gets one row per MMSI (the latest position in that window)."""
    if df.empty:
        return {}
    df = df.copy()
    df["_bucket"] = (df["time_utc"].dt.hour // 4) * 4
    out: dict[int, pd.DataFrame] = {}
    for bucket_hour, grp in df.groupby("_bucket"):
        latest = (
            grp.sort_values("time_utc")
               .groupby("mmsi", as_index=False).tail(1)
               .drop(columns=["_bucket"])
        )
        out[int(bucket_hour)] = latest
    return out


def process_day(d: date, raw_root: Path, positions_root: Path,
                skip_existing: bool = True) -> str:
    """Return one of: 'processed', 'skipped', 'no-data', 'empty'."""
    raw_path = raw_root / f"date={d:%Y-%m-%d}" / "data.parquet"
    if skip_existing and raw_path.exists():
        return "skipped"

    raw_df = download_and_filter(d)
    if raw_df is None:
        log.info("no_data date=%s (NOAA 404 — not published or gap)", d)
        return "no-data"
    if raw_df.empty:
        log.info("empty_filter date=%s (zero tanker rows in USGC bbox)", d)
        return "empty"

    normalized = normalize(raw_df)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    normalized.to_parquet(raw_path, index=False, engine="pyarrow")

    buckets = resample_to_buckets(normalized)
    for hour, bucket_df in buckets.items():
        bucket_path = (positions_root / f"date={d:%Y-%m-%d}" / f"{hour:02d}-00.parquet")
        bucket_path.parent.mkdir(parents=True, exist_ok=True)
        bucket_df.to_parquet(bucket_path, index=False, engine="pyarrow")

    log.info("processed date=%s raw_rows=%d tanker_mmsis=%d buckets=%d",
             d, len(normalized), normalized["mmsi"].nunique(), len(buckets))
    return "processed"


def main() -> int:
    ap = argparse.ArgumentParser(description="NOAA Marine Cadastre AIS backfill (USGC)")
    ap.add_argument("--from", dest="start", required=True,
                    type=lambda s: date.fromisoformat(s),
                    help="Inclusive start date (YYYY-MM-DD)")
    ap.add_argument("--to", dest="end", required=True,
                    type=lambda s: date.fromisoformat(s),
                    help="Inclusive end date (YYYY-MM-DD)")
    ap.add_argument("--output-root", type=Path,
                    default=Path("/data/aisstream"),
                    help="Parent dir; raw → marinecadastre_raw/, "
                         "resampled → positions/source=marinecadastre/")
    ap.add_argument("--no-skip-existing", action="store_true",
                    help="Reprocess dates whose raw output already exists")
    args = ap.parse_args()

    if args.end < args.start:
        log.error("--to %s is before --from %s", args.end, args.start)
        return 1

    raw_root = args.output_root / "marinecadastre_raw"
    positions_root = args.output_root / "positions" / "source=marinecadastre"

    days = (args.end - args.start).days + 1
    log.info("backfill_start days=%d window=%s..%s output=%s",
             days, args.start, args.end, args.output_root)

    counts = {"processed": 0, "skipped": 0, "no-data": 0, "empty": 0, "error": 0}
    d = args.start
    while d <= args.end:
        try:
            outcome = process_day(d, raw_root, positions_root,
                                  skip_existing=not args.no_skip_existing)
            counts[outcome] += 1
        except Exception as e:
            log.error("date=%s failed: %s: %s", d, type(e).__name__, e)
            counts["error"] += 1
        d += timedelta(days=1)

    log.info("backfill_done %s", counts)
    return 0 if counts["error"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
