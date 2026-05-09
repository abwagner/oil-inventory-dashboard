"""
Phase 1 AIS position collector for the canonical tanker manifest.

Subscribes to aisstream.io with `FiltersShipMMSI` set to the manifest from a
Phase 0 census, streams for a configured duration, and writes a single
position snapshot parquet keyed on MMSI (one row per vessel, latest position
seen during the window). Schema matches what the eia-dashboard reads from
`tanker_positions_snapshot.parquet` so it's a drop-in replacement for the
Phase 0 extractor's output.

Designed to be cron-invoked: every N hours, overwrite the snapshot, dashboard
shows fresh data on next refresh.

Usage:
    python aisstream_phase1.py \\
        --manifest "$DATA_DIR/aisstream/census/summary_<run_ts>.json" \\
        --duration-minutes 30 \\
        --output   "$DATA_DIR/aisstream/snapshots/tanker_positions_latest.parquet"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal as signal_module
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import structlog
import websockets
from dotenv import load_dotenv

from _env import load_repo_env; load_repo_env()
log = structlog.get_logger()

WS_URL = "wss://stream.aisstream.io/v0/stream"
GLOBAL_BBOX = [[-90.0, -180.0], [90.0, 180.0]]
RECV_TIMEOUT_SECONDS = 30.0
RECONNECT_BACKOFF_SECONDS = 5.0
LOG_PROGRESS_EVERY = 2000


def load_manifest(summary_path: Path) -> dict[int, dict]:
    with summary_path.open() as f:
        s = json.load(f)
    manifest = s.get("tanker_manifest") or []
    return {int(m["mmsi"]): m for m in manifest}


async def collect_positions(
    api_key: str,
    manifest: dict[int, dict],
    duration_seconds: int,
) -> list[dict]:
    """Stream for duration_seconds; return one row per MMSI with latest position."""
    latest: dict[int, dict] = {}
    static: dict[int, dict] = {}
    n_msgs = 0
    n_position = 0

    deadline = time.monotonic() + duration_seconds
    stop_event = asyncio.Event()

    def request_stop(*_):
        if not stop_event.is_set():
            log.info("stop_requested")
            stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal_module.SIGINT, signal_module.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            pass

    # aisstream silently closes the connection if FiltersShipMMSI exceeds an
    # undocumented cap (observed: subscribing with 3,635 MMSIs reconnects in a
    # tight loop with no messages received). Filter client-side instead — same
    # output, costs ~census-equivalent bandwidth (acceptable for periodic runs).
    manifest_mmsis = set(manifest.keys())
    subscription = {
        "APIKey": api_key,
        "BoundingBoxes": [GLOBAL_BBOX],
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
    }

    log.info("subscribing", manifest_size=len(manifest_mmsis),
             duration_seconds=duration_seconds, filter_mode="client-side")

    while not stop_event.is_set() and time.monotonic() < deadline:
        try:
            async with websockets.connect(
                WS_URL, ping_interval=20, ping_timeout=20, max_size=2**20
            ) as ws:
                await asyncio.wait_for(ws.send(json.dumps(subscription)), timeout=3.0)
                log.info("subscribed")

                while not stop_event.is_set() and time.monotonic() < deadline:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        raw = await asyncio.wait_for(
                            ws.recv(), timeout=min(RECV_TIMEOUT_SECONDS, remaining)
                        )
                    except asyncio.TimeoutError:
                        log.warning("recv_idle", seconds=RECV_TIMEOUT_SECONDS,
                                    n_msgs=n_msgs, n_with_position=len(latest))
                        continue

                    raw_str = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
                    try:
                        msg = json.loads(raw_str)
                    except json.JSONDecodeError:
                        continue

                    # aisstream sends an error envelope as `{"error": "..."}` for bad subs.
                    if isinstance(msg, dict) and msg.get("error") and "Message" not in msg:
                        log.error("server_error", error=msg.get("error"))
                        return []

                    n_msgs += 1
                    if n_msgs % LOG_PROGRESS_EVERY == 0:
                        log.info("progress", msgs=n_msgs, position_msgs=n_position,
                                 mmsis_with_position=len(latest))

                    mtype = msg.get("MessageType")
                    metadata = msg.get("MetaData") or {}
                    mmsi_raw = metadata.get("MMSI")
                    if mmsi_raw is None:
                        continue
                    try:
                        mmsi = int(mmsi_raw)
                    except (TypeError, ValueError):
                        continue
                    if mmsi not in manifest_mmsis:
                        continue

                    ts = metadata.get("time_utc") or ""

                    if mtype == "PositionReport":
                        pr = (msg.get("Message") or {}).get("PositionReport") or {}
                        lat = pr.get("Latitude")
                        if lat is None:
                            lat = metadata.get("latitude")
                        lon = pr.get("Longitude")
                        if lon is None:
                            lon = metadata.get("longitude")
                        if lat is None or lon is None:
                            continue
                        n_position += 1
                        existing = latest.get(mmsi)
                        if existing and ts and ts <= existing.get("time_utc", ""):
                            continue
                        latest[mmsi] = {
                            "mmsi": mmsi,
                            "time_utc": ts,
                            "latitude": float(lat),
                            "longitude": float(lon),
                            "sog": pr.get("Sog"),
                            "cog": pr.get("Cog"),
                            "true_heading": pr.get("TrueHeading"),
                            "nav_status": pr.get("NavigationalStatus"),
                            "name_meta": (metadata.get("ShipName") or "").strip() or None,
                        }
                    elif mtype == "ShipStaticData":
                        ssd = (msg.get("Message") or {}).get("ShipStaticData") or {}
                        static[mmsi] = {
                            "name": (ssd.get("Name") or "").strip() or None,
                            "destination": (ssd.get("Destination") or "").strip() or None,
                            "max_draught_m": ssd.get("MaximumStaticDraught"),
                            "imo": ssd.get("ImoNumber"),
                        }

        except asyncio.TimeoutError:
            log.warning("subscribe_timeout", backoff_seconds=RECONNECT_BACKOFF_SECONDS)
            await asyncio.sleep(RECONNECT_BACKOFF_SECONDS)
        except websockets.WebSocketException as e:
            log.warning("ws_error", error=str(e), backoff_seconds=RECONNECT_BACKOFF_SECONDS)
            await asyncio.sleep(RECONNECT_BACKOFF_SECONDS)
        except OSError as e:
            log.warning("network_error", error=str(e), backoff_seconds=RECONNECT_BACKOFF_SECONDS)
            await asyncio.sleep(RECONNECT_BACKOFF_SECONDS)

    log.info("collection_done",
             total_messages=n_msgs, position_messages=n_position,
             mmsis_with_position=len(latest), mmsis_with_static=len(static))

    rows = []
    for mmsi, pos in latest.items():
        m = manifest.get(mmsi, {})
        s = static.get(mmsi, {})
        name = (s.get("name") or pos.get("name_meta")
                or (m.get("name") or "").strip() or None)
        imo = s.get("imo") or m.get("imo") or None
        rows.append({
            **{k: v for k, v in pos.items() if k != "name_meta"},
            "name": name,
            "ship_type": m.get("ship_type"),
            "imo": imo if imo else None,
            "max_draught_m": s.get("max_draught_m") or m.get("max_draught_m"),
            "destination": (s.get("destination")
                            or (m.get("destination") or "").strip() or None),
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 1 AIS position collector")
    parser.add_argument("--manifest", required=True, type=lambda p: Path(p).expanduser(),
                        help="Phase 0 summary_*.json with tanker_manifest")
    parser.add_argument("--duration-minutes", type=float, default=30.0)
    parser.add_argument("--output", required=True, type=lambda p: Path(p).expanduser(),
                        help="Output parquet path (overwritten on each run)")
    args = parser.parse_args()

    api_key = os.environ.get("AISSTREAM_API_KEY")
    if not api_key:
        log.error("no_api_key", hint="Set AISSTREAM_API_KEY in env or workspace .env")
        return 1

    if not args.manifest.exists():
        log.error("manifest_missing", path=str(args.manifest))
        return 1

    manifest = load_manifest(args.manifest)
    if not manifest:
        log.error("manifest_empty")
        return 1
    log.info("manifest_loaded", mmsis=len(manifest))

    duration_s = int(args.duration_minutes * 60)
    rows = asyncio.run(collect_positions(api_key, manifest, duration_s))

    if not rows:
        log.error("no_positions_collected")
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_parquet(args.output, index=False, engine="pyarrow")
    log.info("written", output=str(args.output), rows=len(df))
    return 0


if __name__ == "__main__":
    sys.exit(main())
