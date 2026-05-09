"""aisstream.io baseline census (Phase 0).

Subscribes globally to aisstream.io for a configurable duration and writes
raw messages plus a summary JSON characterizing what we actually see. This
answers three questions before any downstream feature engineering is built:

  1. Coverage: how many distinct crude-tanker MMSIs (ship_type in 80..89)
     does aisstream report over the window, vs published global fleet?
  2. Volume: messages/sec and bytes/sec for capacity planning of Phase 1.
  3. Cadence: per-MMSI position-report intervals (median, p90).

aisstream constraints:
  - WebSocket only; no historical data.
  - Subscription must be sent within 3 seconds of connection open.
  - BoundingBoxes is required; "global" = single bbox covering [-90,-180]..[90,180].
  - No server-side ship-type filter; ship type is derived client-side from
    ShipStaticData messages.

Usage:
    AISSTREAM_API_KEY=... python aisstream_census.py \\
        --output /path/to/census/ \\
        --duration-seconds 86400

Outputs in --output:
    raw_<run_ts>_part_NNNN.parquet   # raw message log, chunked
    summary_<run_ts>.json            # aggregate stats + tanker MMSI manifest
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal as signal_module
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

import pandas as pd
import structlog
import websockets
from dotenv import load_dotenv

from _env import load_repo_env; load_repo_env()

log = structlog.get_logger()

WS_URL = "wss://stream.aisstream.io/v0/stream"
GLOBAL_BBOX = [[-90.0, -180.0], [90.0, 180.0]]
TANKER_TYPES = set(range(80, 90))  # ITU-R M.1371 ship-type codes for tankers
DEFAULT_MESSAGE_TYPES = ["PositionReport", "ShipStaticData"]
FLUSH_EVERY_MESSAGES = 50_000
FLUSH_EVERY_SECONDS = 60.0
RECONNECT_BACKOFF_SECONDS = 5.0
RECV_TIMEOUT_SECONDS = 30.0


class CensusState:
    """In-memory aggregations. Bounded by #MMSIs (~10^5), not #messages."""

    def __init__(self) -> None:
        self.messages_received = 0
        self.bytes_received = 0
        self.msgs_by_type: Counter[str] = Counter()
        # mmsi -> latest observed ship type (from ShipStaticData)
        self.mmsi_ship_types: dict[int, int] = {}
        # mmsi -> latest observed name/imo/callsign
        self.mmsi_static: dict[int, dict] = {}
        # mmsi -> count of position reports
        self.mmsi_position_counts: Counter[int] = Counter()
        # mmsi -> first/last position-report time_utc strings
        self.mmsi_first_ts: dict[int, str] = {}
        self.mmsi_last_ts: dict[int, str] = {}

    def observe(self, msg: dict) -> None:
        mtype = msg.get("MessageType")
        if not mtype:
            return
        self.msgs_by_type[mtype] += 1
        metadata = msg.get("MetaData") or {}
        mmsi = metadata.get("MMSI")
        if mmsi is None:
            return
        try:
            mmsi = int(mmsi)
        except (TypeError, ValueError):
            return

        if mtype == "ShipStaticData":
            ssd = (msg.get("Message") or {}).get("ShipStaticData") or {}
            stype = ssd.get("Type")
            if isinstance(stype, int):
                self.mmsi_ship_types[mmsi] = stype
            self.mmsi_static[mmsi] = {
                "name": ssd.get("Name") or metadata.get("ShipName"),
                "imo": ssd.get("ImoNumber"),
                "callsign": ssd.get("CallSign"),
                "destination": ssd.get("Destination"),
                "max_draught_m": ssd.get("MaximumStaticDraught"),
            }
        elif mtype == "PositionReport":
            self.mmsi_position_counts[mmsi] += 1
            tstr = metadata.get("time_utc")
            if tstr:
                self.mmsi_first_ts.setdefault(mmsi, tstr)
                self.mmsi_last_ts[mmsi] = tstr


def parse_time_utc(s: str | None) -> datetime | None:
    """aisstream emits e.g. '2026-04-16 10:30:45.123456789 +0000 UTC'."""
    if not s:
        return None
    # strip trailing ' UTC' and trim sub-microsecond precision
    s = s.replace(" UTC", "").strip()
    # split off timezone
    parts = s.rsplit(" ", 1)
    body = parts[0]
    tz = parts[1] if len(parts) == 2 else "+0000"
    # truncate fractional seconds to 6 digits for fromisoformat compatibility
    if "." in body:
        head, frac = body.split(".", 1)
        frac = frac[:6]
        body = f"{head}.{frac}"
    iso = body.replace(" ", "T") + tz[:3] + ":" + tz[3:]
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def median_interval_seconds(first: str | None, last: str | None, count: int) -> float | None:
    if count < 2 or not first or not last:
        return None
    a = parse_time_utc(first)
    b = parse_time_utc(last)
    if not a or not b:
        return None
    span = (b - a).total_seconds()
    if span <= 0:
        return None
    return span / (count - 1)


def write_summary(
    path: Path,
    run_ts: str,
    duration_seconds: int,
    elapsed_seconds: float,
    state: CensusState,
) -> None:
    intervals: list[float] = []
    for mmsi, count in state.mmsi_position_counts.items():
        if count < 10:
            continue
        ivl = median_interval_seconds(
            state.mmsi_first_ts.get(mmsi),
            state.mmsi_last_ts.get(mmsi),
            count,
        )
        if ivl is not None:
            intervals.append(ivl)

    tanker_mmsis = [m for m, t in state.mmsi_ship_types.items() if t in TANKER_TYPES]
    tanker_manifest = []
    for m in sorted(tanker_mmsis):
        st = state.mmsi_static.get(m, {})
        tanker_manifest.append({
            "mmsi": m,
            "ship_type": state.mmsi_ship_types[m],
            "name": st.get("name"),
            "imo": st.get("imo"),
            "callsign": st.get("callsign"),
            "destination": st.get("destination"),
            "max_draught_m": st.get("max_draught_m"),
            "position_reports": int(state.mmsi_position_counts.get(m, 0)),
        })

    type_counts_with_unknown = Counter(state.mmsi_ship_types.values())
    mmsis_with_known_type = sum(type_counts_with_unknown.values())
    mmsis_with_unknown_type = sum(
        1 for m in state.mmsi_position_counts if m not in state.mmsi_ship_types
    )

    summary = {
        "run_started_utc": run_ts,
        "duration_seconds_requested": duration_seconds,
        "duration_seconds_elapsed": round(elapsed_seconds, 1),
        "messages_received": state.messages_received,
        "bytes_received": state.bytes_received,
        "messages_per_second": round(state.messages_received / max(elapsed_seconds, 1.0), 2),
        "bytes_per_second": round(state.bytes_received / max(elapsed_seconds, 1.0), 2),
        "messages_by_type": dict(state.msgs_by_type),
        "unique_mmsis_total": len(set(state.mmsi_position_counts) | set(state.mmsi_ship_types)),
        "unique_mmsis_with_position_report": len(state.mmsi_position_counts),
        "unique_mmsis_with_ship_type": mmsis_with_known_type,
        "unique_mmsis_with_unknown_ship_type": mmsis_with_unknown_type,
        "unique_mmsis_by_ship_type": {
            str(k): v for k, v in sorted(type_counts_with_unknown.items())
        },
        "tanker_mmsi_count": len(tanker_mmsis),
        "tanker_mmsi_count_by_subtype": {
            str(k): v
            for k, v in sorted(
                Counter(t for t in state.mmsi_ship_types.values() if t in TANKER_TYPES).items()
            )
        },
        "position_report_interval_seconds_median": round(median(intervals), 1) if intervals else None,
        "position_report_interval_sample_size": len(intervals),
        "tanker_manifest": tanker_manifest,
    }

    path.write_text(json.dumps(summary, indent=2, default=str))
    log.info(
        "summary_written",
        path=str(path),
        messages=state.messages_received,
        unique_mmsis=summary["unique_mmsis_total"],
        tanker_mmsis=summary["tanker_mmsi_count"],
    )


async def run_census(
    api_key: str,
    output_dir: Path,
    duration_seconds: int,
    message_types: list[str],
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_prefix = output_dir / f"raw_{run_ts}_part_"
    summary_path = output_dir / f"summary_{run_ts}.json"

    state = CensusState()
    started_at = time.monotonic()
    deadline = started_at + duration_seconds
    part = 0
    buffer: list[dict] = []
    last_flush = started_at

    def flush_now() -> None:
        nonlocal part, buffer
        if not buffer:
            return
        df = pd.DataFrame(buffer)
        out = Path(f"{raw_prefix}{part:04d}.parquet")
        df.to_parquet(out, index=False, engine="pyarrow")
        log.info("flushed", part=part, rows=len(df), path=str(out))
        part += 1
        buffer = []

    # Clean-shutdown handler: SIGINT/SIGTERM -> stop loop, flush, write summary.
    stop_event = asyncio.Event()

    def request_stop(*_: object) -> None:
        if not stop_event.is_set():
            log.info("stop_requested")
            stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal_module.SIGINT, signal_module.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            # Windows / unusual envs: fall back to default handling
            pass

    subscription = {
        "APIKey": api_key,
        "BoundingBoxes": [GLOBAL_BBOX],
        "FilterMessageTypes": message_types,
    }

    log.info(
        "census_starting",
        duration_seconds=duration_seconds,
        message_types=message_types,
        output=str(output_dir),
    )

    while not stop_event.is_set() and time.monotonic() < deadline:
        try:
            async with websockets.connect(
                WS_URL,
                ping_interval=20,
                ping_timeout=20,
                max_size=2**20,
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
                        log.warning("recv_idle", seconds=RECV_TIMEOUT_SECONDS)
                        continue

                    if isinstance(raw, bytes):
                        state.bytes_received += len(raw)
                        try:
                            raw_str = raw.decode("utf-8", errors="replace")
                        except Exception:
                            continue
                    else:
                        raw_str = raw
                        state.bytes_received += len(raw_str.encode("utf-8"))

                    state.messages_received += 1

                    try:
                        msg = json.loads(raw_str)
                    except json.JSONDecodeError:
                        log.warning("json_decode_failed")
                        continue

                    state.observe(msg)

                    metadata = msg.get("MetaData") or {}
                    buffer.append({
                        "received_at_utc": datetime.now(timezone.utc).isoformat(),
                        "time_utc": metadata.get("time_utc"),
                        "mmsi": metadata.get("MMSI"),
                        "message_type": msg.get("MessageType"),
                        "raw_json": raw_str,
                    })

                    now = time.monotonic()
                    if (
                        len(buffer) >= FLUSH_EVERY_MESSAGES
                        or (now - last_flush) >= FLUSH_EVERY_SECONDS
                    ):
                        flush_now()
                        last_flush = now

        except asyncio.TimeoutError:
            log.warning("subscribe_timeout", backoff_seconds=RECONNECT_BACKOFF_SECONDS)
            await asyncio.sleep(RECONNECT_BACKOFF_SECONDS)
        except websockets.WebSocketException as e:
            log.warning("ws_error", error=str(e), backoff_seconds=RECONNECT_BACKOFF_SECONDS)
            await asyncio.sleep(RECONNECT_BACKOFF_SECONDS)
        except OSError as e:
            log.warning("network_error", error=str(e), backoff_seconds=RECONNECT_BACKOFF_SECONDS)
            await asyncio.sleep(RECONNECT_BACKOFF_SECONDS)

    flush_now()
    elapsed = time.monotonic() - started_at
    write_summary(summary_path, run_ts, duration_seconds, elapsed, state)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="aisstream.io Phase 0 baseline census")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument(
        "--duration-seconds",
        type=int,
        default=86_400,
        help="How long to stream (default: 86400 = 24h)",
    )
    parser.add_argument(
        "--message-types",
        default=",".join(DEFAULT_MESSAGE_TYPES),
        help=f"Comma-separated aisstream MessageTypes (default: {','.join(DEFAULT_MESSAGE_TYPES)})",
    )
    args = parser.parse_args()

    api_key = os.environ.get("AISSTREAM_API_KEY", "")
    if not api_key:
        log.error("no_api_key", hint="Set AISSTREAM_API_KEY env var")
        return 1

    output_dir = Path(args.output)
    message_types = [t.strip() for t in args.message_types.split(",") if t.strip()]
    if not message_types:
        log.error("no_message_types")
        return 1

    return asyncio.run(
        run_census(
            api_key=api_key,
            output_dir=output_dir,
            duration_seconds=args.duration_seconds,
            message_types=message_types,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
