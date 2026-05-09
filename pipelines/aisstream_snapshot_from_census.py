"""Extract latest tanker positions from a Phase 0 aisstream census.

The Phase 0 census (aisstream_census.py) writes raw_<ts>_part_NNNN.parquet files
with the schema [received_at_utc, time_utc, mmsi, message_type, raw_json] and a
companion summary_<ts>.json containing tanker_manifest. This script collapses
that across the run into a single snapshot of the last known position for each
crude-tanker MMSI, joined with the static manifest fields.

This is a stop-gap viewer for §9.7 "tanker positions" until the live Phase 1
collector exists. It does not produce a streaming pipeline; it is a one-shot
that reads the census once.

Usage:
    python aisstream_snapshot_from_census.py \\
        --census-dir /path/to/census/ \\
        --output     /path/to/snapshot.parquet
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
import structlog

log = structlog.get_logger()


def find_summary(census_dir: Path) -> Path:
    candidates = sorted(census_dir.glob("summary_*.json"))
    if not candidates:
        raise FileNotFoundError(f"no summary_*.json in {census_dir}")
    return candidates[-1]


def load_manifest(summary_path: Path) -> dict[int, dict]:
    with summary_path.open() as f:
        s = json.load(f)
    manifest = s.get("tanker_manifest", [])
    return {int(m["mmsi"]): m for m in manifest}


def extract_latest_positions(census_dir: Path, manifest: dict[int, dict]) -> pd.DataFrame:
    """Stream raw parquets and keep the latest PositionReport per tanker MMSI."""
    tanker_mmsis = set(manifest.keys())
    latest: dict[int, dict] = {}

    parts = sorted(census_dir.glob("raw_*.parquet"))
    log.info("scan_start", parts=len(parts), tankers=len(tanker_mmsis))

    for i, part in enumerate(parts):
        try:
            df = pd.read_parquet(part, columns=["mmsi", "message_type", "time_utc", "raw_json"])
        except Exception as e:
            log.warning("part_read_failed", part=part.name, error=str(e))
            continue

        df = df[(df["message_type"] == "PositionReport") & (df["mmsi"].isin(tanker_mmsis))]
        if df.empty:
            continue

        for mmsi, ts, raw in zip(df["mmsi"].values, df["time_utc"].values, df["raw_json"].values):
            mmsi = int(mmsi)
            if mmsi in latest and ts <= latest[mmsi]["time_utc"]:
                continue
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            pos = (msg.get("Message") or {}).get("PositionReport") or {}
            meta = msg.get("MetaData") or {}
            lat = pos.get("Latitude")
            if lat is None:
                lat = meta.get("latitude")
            lon = pos.get("Longitude")
            if lon is None:
                lon = meta.get("longitude")
            if lat is None or lon is None:
                continue
            latest[mmsi] = {
                "mmsi": mmsi,
                "time_utc": ts,
                "latitude": float(lat),
                "longitude": float(lon),
                "sog": pos.get("Sog"),
                "cog": pos.get("Cog"),
                "true_heading": pos.get("TrueHeading"),
                "nav_status": pos.get("NavigationalStatus"),
                "name_meta": (meta.get("ShipName") or "").strip() or None,
            }

        if (i + 1) % 100 == 0:
            log.info("scan_progress", parts_read=i + 1, parts_total=len(parts), tankers_seen=len(latest))

    log.info("scan_done", tankers_with_position=len(latest), tankers_in_manifest=len(tanker_mmsis))

    rows = []
    for mmsi, pos_row in latest.items():
        m = manifest.get(mmsi, {})
        name = (m.get("name") or "").strip() or pos_row.get("name_meta")
        rows.append({
            **{k: v for k, v in pos_row.items() if k != "name_meta"},
            "name": name,
            "ship_type": m.get("ship_type"),
            "imo": m.get("imo") or None,
            "max_draught_m": m.get("max_draught_m"),
            "destination": (m.get("destination") or "").strip() or None,
        })
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description="Extract latest tanker positions from a Phase 0 census")
    parser.add_argument("--census-dir", required=True, type=Path, help="Directory containing raw_*.parquet + summary_*.json")
    parser.add_argument("--output", required=True, type=Path, help="Output parquet path")
    args = parser.parse_args()

    if not args.census_dir.is_dir():
        log.error("census_dir_missing", path=str(args.census_dir))
        sys.exit(1)

    summary_path = find_summary(args.census_dir)
    log.info("summary_found", path=str(summary_path))
    manifest = load_manifest(summary_path)
    if not manifest:
        log.error("manifest_empty")
        sys.exit(1)

    df = extract_latest_positions(args.census_dir, manifest)
    if df.empty:
        log.error("no_positions_extracted")
        sys.exit(1)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(args.output, index=False, engine="pyarrow")
    log.info("written", output=str(args.output), rows=len(df))


if __name__ == "__main__":
    main()
