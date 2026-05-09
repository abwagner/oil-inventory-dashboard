"""
In-container scheduler for the oil-inventory-dashboard.

APScheduler-driven daemon process. Runs as a separate container alongside the
dashboard; both share the data volume.

Scheduled jobs:
- ais_phase1: every 4 hours at :00. 30-minute capture, writes the live AIS
  snapshot. Dashboard's /api/ships picks it up automatically.
- sar_ingest: weekly on Sundays at 06:00 UTC. Per-AOI ingest → CFAR detect →
  cross-acquisition aggregate. Dashboard's /api/sar_detections picks up the
  refreshed clusters.parquet on next request. Skipped if SAR_ENABLED=false
  or CDSE creds are missing.

Configuration via env (see .env.example):
- DATA_DIR (default /data): root for AIS, SAR, EIA storage on disk.
- ENV_FILE (default <repo>/.env): location of the .env file to load.
- SAR_ENABLED (default true if CDSE creds present, else false): toggle SAR job.
- TZ (default UTC): timezone for cron expressions.

AOI list lives in this file. To add or remove an AOI, edit AOIS below.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler

PIPELINES = Path(__file__).resolve().parent / "pipelines"
sys.path.insert(0, str(PIPELINES))
from _env import aisstream_enabled, load_repo_env, sar_enabled  # noqa: E402

load_repo_env()

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(REPO_ROOT / "data")))
PYTHON = sys.executable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("scheduler")


# --- AOI configuration ----------------------------------------------------

# Each AOI: name, bbox (lon_min, lat_min, lon_max, lat_max), output (width, height).
# At ~88 m/px effective resolution near 26°N. Add new entries to ingest more regions.
AOIS = [
    {
        "name":   "persian_gulf_oman",
        "bbox":   (54.0, 24.0, 60.0, 28.0),
        "size":   (5000, 3333),
    },
    {
        "name":   "usgc",
        "bbox":   (-98.0, 26.0, -88.0, 31.0),
        "size":   (8000, 4000),
    },
]


# --- Job: AIS Phase 1 -----------------------------------------------------

def latest_manifest() -> Path | None:
    """Find the most recent Phase 0 census summary in DATA_DIR/aisstream/census/."""
    census_dir = DATA_DIR / "aisstream" / "census"
    candidates = sorted(census_dir.glob("summary_*.json"))
    return candidates[-1] if candidates else None


def run_ais_phase1():
    manifest = latest_manifest()
    if manifest is None:
        log.error("ais_phase1: no manifest found under %s/aisstream/census/", DATA_DIR)
        return

    snapshot_dir = DATA_DIR / "aisstream" / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    output = snapshot_dir / "tanker_positions_latest.parquet"

    cmd = [
        PYTHON, str(PIPELINES / "aisstream_phase1.py"),
        "--manifest", str(manifest),
        "--duration-minutes", "30",
        "--output", str(output),
    ]
    log.info("ais_phase1 starting: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
        log.info("ais_phase1 done")
    except subprocess.CalledProcessError as e:
        log.error("ais_phase1 failed (exit %s)", e.returncode)


# --- Job: SAR ingest + detect + aggregate ---------------------------------

def run_sar_ingest():
    output_dir = DATA_DIR / "sentinel_sar"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Lookback handles missed runs; ingest is incremental via the per-AOI state file.
    until = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    since = (datetime.now(tz=timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

    for aoi in AOIS:
        name = aoi["name"]
        bbox = aoi["bbox"]
        width, height = aoi["size"]
        log.info("sar_ingest aoi=%s bbox=%s size=%dx%d", name, bbox, width, height)

        ingest_cmd = [
            PYTHON, str(PIPELINES / "sentinel_sar.py"), "ingest",
            "--aoi-name", name,
            "--bbox", *(str(x) for x in bbox),
            "--from", since, "--to", until,
            "--width", str(width), "--height", str(height),
            "--output-dir", str(output_dir),
        ]
        try:
            subprocess.run(ingest_cmd, check=True)
        except subprocess.CalledProcessError as e:
            log.error("sar_ingest %s ingest failed (exit %s); skipping detect/aggregate", name, e.returncode)
            continue

        scene_dir = output_dir / name
        try:
            subprocess.run([PYTHON, str(PIPELINES / "sar_detect.py"),
                            "--scene-dir", str(scene_dir)], check=True)
            subprocess.run([PYTHON, str(PIPELINES / "sar_aggregate.py"),
                            "--scene-dir", str(scene_dir)], check=True)
        except subprocess.CalledProcessError as e:
            log.error("sar_ingest %s detect/aggregate failed (exit %s)", name, e.returncode)


# --- Boot -----------------------------------------------------------------

def needs_first_ais_run() -> bool:
    """First-run check: kick AIS Phase 1 immediately when a manifest exists
    but no snapshot has been written yet (or the snapshot is stale)."""
    if latest_manifest() is None:
        return False  # nothing to do — user needs to run census first
    snapshot = DATA_DIR / "aisstream" / "snapshots" / "tanker_positions_latest.parquet"
    if not snapshot.exists():
        return True
    age = datetime.now(tz=timezone.utc).timestamp() - snapshot.stat().st_mtime
    return age > 6 * 3600  # >6h old → refresh on boot


def main():
    sar = sar_enabled()
    ais = aisstream_enabled()
    log.info("scheduler boot DATA_DIR=%s PYTHON=%s aois=%d sar=%s ais=%s",
             DATA_DIR, PYTHON, len(AOIS), sar, ais)

    scheduler = BlockingScheduler(timezone=timezone.utc)

    if ais:
        # AIS Phase 1: every 4 hours at the top of the hour (UTC).
        scheduler.add_job(run_ais_phase1, "cron", hour="0,4,8,12,16,20", minute=0,
                          id="ais_phase1", max_instances=1, coalesce=True)
        if needs_first_ais_run():
            # Kick a one-shot 30s after boot so logs settle before the job runs.
            kick_at = datetime.now(tz=timezone.utc) + timedelta(seconds=30)
            scheduler.add_job(run_ais_phase1, "date", run_date=kick_at,
                              id="ais_phase1_first_run", max_instances=1)
            log.info("first-run AIS Phase 1 queued for %s (manifest present, snapshot stale/missing)", kick_at.isoformat())
    else:
        log.warning("AISSTREAM_API_KEY not set — skipping ais_phase1 schedule")

    if sar:
        # SAR ingest: weekly, Sunday 06:00 UTC. Costs Sentinel Hub PUs —
        # first run on a fresh setup is NOT auto-kicked. Trigger from the
        # dashboard's [Run SAR ingest] button when ready.
        scheduler.add_job(run_sar_ingest, "cron", day_of_week="sun", hour=6, minute=0,
                          id="sar_ingest", max_instances=1, coalesce=True)
    else:
        log.warning("SAR disabled (SAR_ENABLED=false or CDSE creds missing) — skipping sar_ingest schedule")

    for j in scheduler.get_jobs():
        log.info("scheduled: %s next_run=%s", j.id, j.trigger)

    if not scheduler.get_jobs():
        log.error("no jobs scheduled — set AISSTREAM_API_KEY and/or CDSE creds in .env")
        return

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler shutdown")


if __name__ == "__main__":
    main()
