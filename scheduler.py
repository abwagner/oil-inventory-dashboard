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
- omr_ingest: monthly on the 14th, 18:00 UTC. IEA OMR drops mid-month
  (typically the 11th–14th); 14th covers late releases without slipping into
  the next month. Discovery walks back through monthly URLs so a one-day
  miss still picks up the right issue. Reschedule via OMR_PDF_URL when you
  want to backfill a specific historical issue.

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


# --- S3 mirror -----------------------------------------------------------
#
# Pipelines write to local DATA_DIR (rasterio + Path-based I/O assume a real
# filesystem). After each job we mirror that job's output subtree to MinIO so
# the dashboard, which reads via fsspec from s3://$S3_BUCKET, sees fresh data.
# Mirror is a no-op when S3_BUCKET / AWS_* aren't set, so behaviour matches
# the prior local-only mode if you drop the env vars again.

def _s3fs():
    """Build an s3fs filesystem for the configured object store. Returns
    (fs, bucket) when fully configured, (None, None) otherwise."""
    bucket = os.environ.get("S3_BUCKET")
    endpoint = os.environ.get("AWS_ENDPOINT_URL")
    key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not (bucket and endpoint and key and secret):
        return None, None
    import s3fs  # local import — only loaded when mirror is actually used
    return s3fs.S3FileSystem(
        key=key, secret=secret,
        client_kwargs={"endpoint_url": endpoint},
    ), bucket


def mirror_to_s3(local_root: Path, s3_prefix: str = "") -> None:
    """Walk local_root and upload every file whose S3 counterpart is missing
    or has a different size. Identical files (same size) are skipped, so
    repeated calls are cheap. No-op when S3 isn't configured."""
    fs, bucket = _s3fs()
    if fs is None:
        log.debug("mirror skipped: S3 not configured")
        return
    if not local_root.exists():
        log.debug("mirror skipped: %s does not exist", local_root)
        return
    prefix = s3_prefix.strip("/")
    uploaded = skipped = 0
    bytes_up = 0
    for src in sorted(local_root.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(local_root).as_posix()
        dst = f"{bucket}/{prefix}/{rel}" if prefix else f"{bucket}/{rel}"
        local_size = src.stat().st_size
        try:
            info = fs.info(dst)
            if info.get("size") == local_size:
                skipped += 1
                continue
        except FileNotFoundError:
            pass
        try:
            fs.put(str(src), dst)
            uploaded += 1
            bytes_up += local_size
        except Exception as e:
            log.warning("mirror %s -> s3://%s failed: %s", src, dst, e)
    log.info("mirror %s -> s3://%s/%s: %d uploaded (%.1f KB), %d skipped",
             local_root, bucket, prefix, uploaded, bytes_up / 1024, skipped)


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
    # Singapore Strait + Malacca Strait — chokepoint for TD3C (MEG → China)
    # and TD22 (USGC → China) routes, plus Singapore's Eastern + Western OPL
    # anchorages which historically dominate Asian floating storage. Bbox
    # covers Malacca's narrow eastern section (~Port Klang southward) through
    # the Singapore Strait and into the South China Sea entrance. ~120 m/px
    # to match the existing AOIs' effective resolution (the higher cos(lat)
    # factor near the equator makes this ~3700 px for 4° lon vs. Persian
    # Gulf's 5000 px for 6° lon at lat 26°).
    {
        "name":   "singapore_malacca",
        "bbox":   (102.0, 0.5, 106.0, 4.0),
        "size":   (3700, 3250),
    },
    # Red Sea + Bab el-Mandeb — Suez bypass chokepoint and the current
    # Houthi attack zone. Bbox covers Bab el-Mandeb (Yemen / Djibouti) and
    # ~5° north into the Red Sea where most attacks have occurred. Real-time
    # signal: re-routings around the Cape of Good Hope show up as a sudden
    # transient-traffic drop here. Small AOI, ~3-4k PU/month.
    {
        "name":   "red_sea_bab_mandeb",
        "bbox":   (42.0, 11.0, 46.0, 16.0),
        "size":   (3600, 4600),
    },
    # Yellow Sea + Bohai Bay — primary Chinese crude unloading region.
    # Bohai (Tianjin / Dalian) is where Russia's ESPO crude lands; Qingdao
    # in the Yellow Sea is where MEG crude lands. Anchorage queues here are
    # the most direct Chinese demand signal. ~7°×7° at lat 37°N → ~5200×6500
    # px at ~120 m/px ≈ ~8-9k PU/month. Doesn't include Ningbo-Zhoushan
    # (~30°N) — that's a separate AOI worth adding if PU budget allows.
    {
        "name":   "yellow_sea_bohai",
        "bbox":   (118.0, 34.0, 125.0, 41.0),
        "size":   (5200, 6500),
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

    # History dir accumulates one parquet per Phase 1 tick (~700 KB/day) so we
    # have backtest depth — TDD §4.2.1 Phase 1 specifies "day-partitioned snapshots"
    # which the prior implementation skipped. Hive layout for predicate pushdown.
    history_dir = DATA_DIR / "aisstream" / "positions"

    cmd = [
        PYTHON, str(PIPELINES / "aisstream_phase1.py"),
        "--manifest", str(manifest),
        "--duration-minutes", "30",
        "--output", str(output),
        "--history-dir", str(history_dir),
    ]
    log.info("ais_phase1 starting: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
        log.info("ais_phase1 done")
        mirror_to_s3(DATA_DIR / "aisstream", "aisstream")
    except subprocess.CalledProcessError as e:
        log.error("ais_phase1 failed (exit %s)", e.returncode)


# --- Job: AIS static refresh (weekly manifest update) ---------------------

def run_ais_static_refresh():
    """Refresh the tanker MMSI manifest weekly. Subscribes only to
    `ShipStaticData` for 2 hours — long enough to see the static fields for
    most active tankers (name, IMO, dimensions, type), short enough to be
    much lighter than a full Phase 0 census. New tankers entering service
    get picked up; retired MMSIs eventually drop out of the broadcast set.

    Output is a fresh `summary_<run_ts>.json` in DATA_DIR/aisstream/census/.
    `latest_manifest()` already picks the lexicographically latest summary,
    so the next Phase 1 tick automatically uses the refreshed manifest with
    no further wiring.

    TDD §4.2.1 Phase 1 specifies a weekly STATIC collector parallel to the
    position collector; this implements that collector."""
    census_dir = DATA_DIR / "aisstream" / "census"
    census_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        PYTHON, str(PIPELINES / "aisstream_census.py"),
        "--output", str(census_dir),
        "--message-types", "ShipStaticData",
        "--duration-seconds", "7200",  # 2h
    ]
    log.info("ais_static_refresh starting: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
        log.info("ais_static_refresh done")
        mirror_to_s3(DATA_DIR / "aisstream", "aisstream")
    except subprocess.CalledProcessError as e:
        log.error("ais_static_refresh failed (exit %s)", e.returncode)


# --- Job: NOAA Marine Cadastre AIS catch-up (monthly) --------------------

def run_marine_cadastre_catchup():
    """Pull the prior month's NOAA Marine Cadastre AIS data for the USGC AOI.
    NOAA publishes with ~1 month lag, so we target the most recently completed
    calendar month. The underlying script is idempotent (skips dates whose raw
    output already exists), so a missed cron tick is automatically caught up
    by the next month's run."""
    today = datetime.now(tz=timezone.utc).date()
    first_of_this_month = today.replace(day=1)
    last_of_prior = first_of_this_month - timedelta(days=1)
    first_of_prior = last_of_prior.replace(day=1)
    cmd = [
        PYTHON, str(PIPELINES / "marine_cadastre_ingest.py"),
        "--from", first_of_prior.isoformat(),
        "--to",   last_of_prior.isoformat(),
        "--output-root", str(DATA_DIR / "aisstream"),
    ]
    log.info("marine_cadastre_catchup starting: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
        log.info("marine_cadastre_catchup done")
        mirror_to_s3(DATA_DIR / "aisstream", "aisstream")
    except subprocess.CalledProcessError as e:
        log.error("marine_cadastre_catchup failed (exit %s)", e.returncode)


# --- Job: IEA OMR (monthly) -----------------------------------------------

def run_omr_ingest():
    """Pull the latest free OMR PDF, parse Tables 1/1a/1b, upsert into sqlite.

    Reaches into pipelines/omr.py rather than going via the dashboard's HTTP
    endpoint to keep the scheduler decoupled (it doesn't need the FastAPI app
    running). Failures are logged and swallowed — the next cron tick retries.
    """
    cmd = [PYTHON, str(PIPELINES / "omr.py")]
    log.info("omr_ingest starting: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
        log.info("omr_ingest done")
        # OMR writes a sqlite db + monthly PDF cache under DATA_DIR; mirror so
        # downstream queries that read via fsspec see the same artifact.
        mirror_to_s3(DATA_DIR, "")
    except subprocess.CalledProcessError as e:
        log.warning("omr_ingest failed (exit %s) — IEA may not have published "
                    "a free version this month; will retry next cron tick",
                    e.returncode)


# --- Job: Sentinel-1 GRD ingest via DESP (daily, free of PUs) -------------

def run_s1_grd_ingest():
    """Daily Sentinel-1 GRD ingest via DESP raw products (free).

    Parallel path to run_sar_ingest (which uses Sentinel Hub Process API
    and bills PUs). Same downstream pipeline: after ingest, sar_detect.py
    + sar_aggregate.py run against the new tiles. Output dir is separate
    (`sentinel_s1_grd/<aoi>/...`) so the two paths can be A/B-compared.
    Daily cadence is comfortable since DESP is free; the existing weekly
    SH cron stays as backup until A/B parity is confirmed (OID-7
    acceptance, then OID-9 backfill).
    """
    output_dir = DATA_DIR / "sentinel_s1_grd"
    output_dir.mkdir(parents=True, exist_ok=True)

    until = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    since = (datetime.now(tz=timezone.utc) - timedelta(days=3)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    for aoi in AOIS:
        name = aoi["name"]
        bbox = aoi["bbox"]
        width, height = aoi["size"]
        log.info("s1_grd_ingest aoi=%s bbox=%s size=%dx%d", name, bbox, width, height)

        ingest_cmd = [
            PYTHON, str(PIPELINES / "sentinel_s1_grd.py"),
            "--aoi-name", name,
            "--bbox", *(str(x) for x in bbox),
            "--width", str(width), "--height", str(height),
            "--from", since, "--to", until,
            "--output-dir", str(output_dir),
        ]
        try:
            subprocess.run(ingest_cmd, check=True)
        except subprocess.CalledProcessError as e:
            log.error("s1_grd_ingest %s failed (exit %s); skipping detect/aggregate",
                      name, e.returncode)
            continue

        scene_dir = output_dir / name
        try:
            subprocess.run([PYTHON, str(PIPELINES / "sar_detect.py"),
                            "--scene-dir", str(scene_dir)], check=True)
            subprocess.run([PYTHON, str(PIPELINES / "sar_aggregate.py"),
                            "--scene-dir", str(scene_dir)], check=True)
        except subprocess.CalledProcessError as e:
            log.error("s1_grd_ingest %s detect/aggregate failed (exit %s)",
                      name, e.returncode)


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
            continue
        mirror_to_s3(scene_dir, f"sentinel_sar/{name}")


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
        # Weekly STATIC collector: Sunday 12:00 UTC. Refreshes the MMSI
        # manifest so new tankers enter the universe and retired MMSIs drop
        # out. Picked far from the SAR Sunday 06:00 job to avoid contention.
        scheduler.add_job(run_ais_static_refresh, "cron",
                          day_of_week="sun", hour=12, minute=0,
                          id="ais_static_refresh", max_instances=1, coalesce=True)
        if needs_first_ais_run():
            # Kick a one-shot 30s after boot so logs settle before the job runs.
            kick_at = datetime.now(tz=timezone.utc) + timedelta(seconds=30)
            scheduler.add_job(run_ais_phase1, "date", run_date=kick_at,
                              id="ais_phase1_first_run", max_instances=1)
            log.info("first-run AIS Phase 1 queued for %s (manifest present, snapshot stale/missing)", kick_at.isoformat())
    else:
        log.warning("AISSTREAM_API_KEY not set — skipping ais_phase1 schedule")

    # OMR: monthly on the 14th at 18:00 UTC. Always scheduled — no creds needed
    # for the free release. Pipeline degrades cleanly when IEA hasn't published.
    scheduler.add_job(run_omr_ingest, "cron", day=14, hour=18, minute=0,
                      id="omr_ingest", max_instances=1, coalesce=True)

    # NOAA Marine Cadastre USGC AIS catch-up: 5th of each month at 03:00 UTC.
    # NOAA publishes the prior month's data with ~1 month lag, so the 5th
    # leaves a comfortable margin. Free archive, no creds. Pipeline is
    # idempotent and per-day, so missed ticks recover automatically.
    scheduler.add_job(run_marine_cadastre_catchup, "cron",
                      day=5, hour=3, minute=0,
                      id="marine_cadastre_catchup", max_instances=1, coalesce=True)

    # Sentinel-1 GRD via DESP: daily at 05:00 UTC. Free of PUs, so we can
    # afford daily cadence vs the weekly Sentinel Hub job. Needs
    # CDSE_USERNAME + CDSE_PASSWORD in env (separate from Sentinel Hub's
    # CLIENT_ID/SECRET); skipped if either is missing.
    if os.environ.get("CDSE_USERNAME") and os.environ.get("CDSE_PASSWORD"):
        scheduler.add_job(run_s1_grd_ingest, "cron", hour=5, minute=0,
                          id="s1_grd_ingest", max_instances=1, coalesce=True)
    else:
        log.warning("CDSE_USERNAME/PASSWORD missing — skipping S1 GRD daily schedule")

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

    # Boot-time mirror: bring MinIO up to parity with whatever's in /data right
    # now (e.g. anything produced while S3 was unconfigured). Incremental — only
    # uploads files whose S3 counterpart is missing or a different size, so
    # subsequent boots are fast. Silently no-ops when S3 isn't configured.
    mirror_to_s3(DATA_DIR, "")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler shutdown")


if __name__ == "__main__":
    main()
