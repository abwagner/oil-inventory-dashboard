# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi>=0.115",
#     "uvicorn>=0.30",
#     "httpx>=0.27",
#     "xlrd>=2.0",
#     "jinja2>=3.1",
#     "openpyxl>=3.1",
#     "pandas>=2.0",
#     "pyarrow>=14.0",
#     "requests>=2.28",
#     "structlog>=24.0",
#     "python-dotenv>=1.0",
#     "s3fs>=2024.1",
#     "curl-cffi>=0.7",
# ]
# ///
"""
EIA Crude Oil Dashboard
Local dashboard for US crude inventory, exports, production, and refinery utilization.
Run with: uv run app.py
"""

import asyncio
import json
import math
import sqlite3
import os
import sys
import logging
from datetime import datetime, timedelta, date, timezone
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
import pandas as pd
import xlrd
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
import uvicorn

# Pipeline modules live alongside the dashboard in this repo. They host the
# env loader plus the data ingestion / detection scripts.
sys.path.insert(0, str(Path(__file__).resolve().parent / "pipelines"))
from _env import (  # noqa: E402
    aisstream_enabled,
    data_uri,
    db_path,
    load_repo_env,
    s3_enabled,
    sar_enabled,
    storage_fs,
    storage_root,
)
load_repo_env()
import steo  # noqa: E402
import omr  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
#
# All non-sqlite reads/writes go through data_uri() / storage_fs() so the same
# code works against local DATA_DIR or s3://<S3_BUCKET>/. The sqlite EIA DB
# always lives on local disk (db_path()) since sqlite isn't S3-friendly.

REPO_ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = REPO_ROOT / "templates"
DB_PATH = db_path()


def resolve_tanker_snapshot() -> str:
    """Live AIS snapshot URI. Resolved at request time so storage-mode changes
    and cron-driven updates land live."""
    return data_uri("aisstream", "snapshots", "tanker_positions_latest.parquet")


EIA_API_KEY = os.environ.get("EIA_API_KEY", "")

# EIA XLS download URLs (no API key needed)
EIA_SERIES = {
    "us_stocks": {
        "xls_url": "https://www.eia.gov/dnav/pet/hist_xls/WCESTUS1w.xls",
        "api_id": "PET.WCESTUS1.W",
        "unit": "thousand_barrels",
        "description": "US Commercial Crude Oil Stocks (excl SPR)",
    },
    "cushing": {
        "xls_url": "https://www.eia.gov/dnav/pet/hist_xls/W_EPC0_SAX_YCUOK_MBBLw.xls",
        "api_id": "PET.W_EPC0_SAX_YCUOK_MBBL.W",
        "unit": "thousand_barrels",
        "description": "Cushing, OK Crude Oil Stocks",
    },
    "exports": {
        "xls_url": "https://www.eia.gov/dnav/pet/hist_xls/WCREXUS2w.xls",
        "api_id": "PET.WCREXUS2.W",
        "unit": "thousand_barrels_per_day",
        "description": "US Crude Oil Exports",
    },
    "production": {
        "xls_url": "https://www.eia.gov/dnav/pet/hist_xls/WCRFPUS2w.xls",
        "api_id": "PET.WCRFPUS2.W",
        "unit": "thousand_barrels_per_day",
        "description": "US Field Production of Crude Oil",
    },
    "utilization": {
        "xls_url": "https://www.eia.gov/dnav/pet/hist_xls/WPULEUS3w.xls",
        "api_id": "PET.WPULEUS3.W",
        "unit": "percent",
        "description": "US Refinery Operable Capacity Utilization",
    },
    "gasoline_stocks": {
        "xls_url": "https://www.eia.gov/dnav/pet/hist_xls/WGTSTUS1w.xls",
        "api_id": "PET.WGTSTUS1.W",
        "unit": "thousand_barrels",
        "description": "US Total Gasoline Stocks",
    },
    "distillate_stocks": {
        "xls_url": "https://www.eia.gov/dnav/pet/hist_xls/WDISTUS1w.xls",
        "api_id": "PET.WDISTUS1.W",
        "unit": "thousand_barrels",
        "description": "US Distillate Fuel Oil Stocks",
    },
    "refinery_inputs": {
        "xls_url": "https://www.eia.gov/dnav/pet/hist_xls/WGIRIUS2w.xls",
        "api_id": "PET.WGIRIUS2.W",
        "unit": "thousand_barrels_per_day",
        "description": "US Refinery Net Input of Crude Oil (drives Cushing forecaster)",
    },
    # ── PADD-level crude oil stocks (excl SPR) ──────────────────────────
    "padd1_stocks": {
        "xls_url": "https://www.eia.gov/dnav/pet/hist_xls/WCESTP11w.xls",
        "api_id": "PET.WCESTP11.W",
        "unit": "thousand_barrels",
        "description": "PADD 1 (East Coast) Crude Oil Stocks excl SPR",
    },
    "padd2_stocks": {
        "xls_url": "https://www.eia.gov/dnav/pet/hist_xls/WCESTP21w.xls",
        "api_id": "PET.WCESTP21.W",
        "unit": "thousand_barrels",
        "description": "PADD 2 (Midwest) Crude Oil Stocks excl SPR",
    },
    "padd3_stocks": {
        "xls_url": "https://www.eia.gov/dnav/pet/hist_xls/WCESTP31w.xls",
        "api_id": "PET.WCESTP31.W",
        "unit": "thousand_barrels",
        "description": "PADD 3 (Gulf Coast) Crude Oil Stocks excl SPR",
    },
    "padd4_stocks": {
        "xls_url": "https://www.eia.gov/dnav/pet/hist_xls/WCESTP41w.xls",
        "api_id": "PET.WCESTP41.W",
        "unit": "thousand_barrels",
        "description": "PADD 4 (Rocky Mountain) Crude Oil Stocks excl SPR",
    },
    "padd5_stocks": {
        "xls_url": "https://www.eia.gov/dnav/pet/hist_xls/WCESTP51w.xls",
        "api_id": "PET.WCESTP51.W",
        "unit": "thousand_barrels",
        "description": "PADD 5 (West Coast) Crude Oil Stocks excl SPR",
    },
}

logger = logging.getLogger("eia-dashboard")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# STEO (Short-Term Energy Outlook) monthly data — global/OECD inventories
STEO_URL = "https://www.eia.gov/outlooks/steo/xls/STEO_m.xlsx"
STEO_SERIES = {
    "pasc_oecd_t3": "OECD commercial stocks (Mbbl)",
    "pasc_us": "US total stocks (Mbbl)",
    "pasc_ooecd_t3": "Other OECD stocks (Mbbl)",
    "t3_stchange_world": "World inventory change (Mbpd)",
    "patc_world": "World consumption (Mbpd)",
    "papr_world": "World production (Mbpd)",
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS eia_weekly (
            series_id TEXT NOT NULL,
            date      TEXT NOT NULL,
            value     REAL,
            PRIMARY KEY (series_id, date)
        );
        CREATE TABLE IF NOT EXISTS steo_monthly (
            series_id TEXT NOT NULL,
            date      TEXT NOT NULL,
            value     REAL,
            PRIMARY KEY (series_id, date)
        );
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        -- Weekly snapshots of SAR floating-storage counts per terminal.
        -- One row per (observed_at, terminal_name). observed_at is the date
        -- portion of the latest SAR cluster observation across all AOIs at
        -- the time the snapshot was taken — i.e. the "as_of" of /api/sar_
        -- floating_storage. The PK ensures repeated calls within a single
        -- observation window upsert rather than duplicate.
        CREATE TABLE IF NOT EXISTS sar_floating_storage_history (
            observed_at      TEXT NOT NULL,
            terminal_name    TEXT NOT NULL,
            persistent_count INTEGER,
            mean_sigma0_db   REAL,
            PRIMARY KEY (observed_at, terminal_name)
        );
        CREATE INDEX IF NOT EXISTS idx_eia_series_date ON eia_weekly(series_id, date);
        CREATE INDEX IF NOT EXISTS idx_steo_series_date ON steo_monthly(series_id, date);
        CREATE INDEX IF NOT EXISTS idx_sar_fs_observed_at
            ON sar_floating_storage_history(observed_at);
        CREATE INDEX IF NOT EXISTS idx_sar_fs_terminal
            ON sar_floating_storage_history(terminal_name);
    """
    )
    # OMR schema lives alongside in the same DB. The pipeline owns its CREATE
    # statements so the CLI works standalone too (--db-path foo.db).
    omr.ensure_schema(conn)
    conn.close()


# ---------------------------------------------------------------------------
# EIA Data Fetching
# ---------------------------------------------------------------------------


async def fetch_xls_series(client: httpx.AsyncClient, series_id: str) -> list[tuple[str, float]]:
    """Download an EIA weekly XLS and return [(date_str, value), ...]."""
    cfg = EIA_SERIES[series_id]
    url = cfg["xls_url"]
    logger.info(f"Fetching {series_id} from {url}")

    resp = await client.get(url, follow_redirects=True, timeout=30)
    resp.raise_for_status()

    workbook = xlrd.open_workbook(file_contents=resp.content)
    sheet = workbook.sheet_by_name("Data 1")

    rows = []
    for i in range(3, sheet.nrows):  # skip header rows
        cell_date = sheet.cell_value(i, 0)
        cell_val = sheet.cell_value(i, 1)
        if not cell_date or not cell_val:
            continue
        # Excel date -> string
        if isinstance(cell_date, float):
            dt = xlrd.xldate_as_datetime(cell_date, workbook.datemode)
            date_str = dt.strftime("%Y-%m-%d")
        else:
            date_str = str(cell_date)[:10]
        rows.append((date_str, float(cell_val)))

    logger.info(f"  {series_id}: {len(rows)} rows, latest {rows[-1][0]} = {rows[-1][1]}")
    return rows


async def fetch_api_series(client: httpx.AsyncClient, series_id: str) -> list[tuple[str, float]]:
    """Fetch from EIA v2 API (requires API key)."""
    cfg = EIA_SERIES[series_id]
    api_id = cfg["api_id"]
    url = f"https://api.eia.gov/v2/seriesid/{api_id}?api_key={EIA_API_KEY}&frequency=weekly&sort[0][column]=period&sort[0][direction]=asc&length=5000"
    logger.info(f"Fetching {series_id} from API")

    resp = await client.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    rows = []
    for rec in data.get("response", {}).get("data", []):
        date_str = rec["period"]
        val = rec.get("value")
        if val is not None:
            rows.append((date_str, float(val)))

    logger.info(f"  {series_id}: {len(rows)} rows from API")
    return rows


async def refresh_all():
    """Fetch all weekly series and STEO monthly data, upsert into SQLite."""
    conn = get_db()
    async with httpx.AsyncClient() as client:
        # --- Weekly EIA series ---
        for series_id in EIA_SERIES:
            try:
                if EIA_API_KEY:
                    rows = await fetch_api_series(client, series_id)
                else:
                    rows = await fetch_xls_series(client, series_id)

                conn.executemany(
                    "INSERT OR REPLACE INTO eia_weekly (series_id, date, value) VALUES (?, ?, ?)",
                    [(series_id, d, v) for d, v in rows],
                )
            except Exception as e:
                logger.error(f"Error fetching {series_id}: {e}")

    # --- STEO monthly (global/OECD inventories) — uses centralized pipeline ---
    try:
        fetch_steo(conn)
    except Exception as e:
        logger.error(f"Error fetching STEO: {e}")

    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        ("last_refresh", datetime.now(tz=timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    logger.info("Refresh complete")


def refresh_omr(conn: sqlite3.Connection) -> dict:
    """Pull the latest free IEA OMR PDF, parse Tables 1/1a/1b, and upsert.

    Discovery walks back through the last few monthly report pages for the blob
    URL; OMR_PDF_URL overrides for explicit issues (subscriber URLs or specific
    archived editions). Returns a small dict so the ingest tracker has a result
    to surface; raises RuntimeError on hard failure (no URL, no records).
    """
    url = os.environ.get("OMR_PDF_URL") or omr.find_latest_pdf_url()
    if not url:
        # iea.org has Cloudflare bot detection so auto-discovery often 403s
        # from non-browser clients. The fix is to set OMR_PDF_URL once a month
        # — find the link on the report page (manually) and paste it in.
        raise RuntimeError(
            "no OMR PDF URL — auto-discovery hit iea.org bot protection; "
            "set OMR_PDF_URL to the blob URL from the latest "
            "iea.org/reports/oil-market-report-<month>-<year> page"
        )
    pdf_bytes = omr.download_pdf(url)
    report_date = (
        omr.report_date_from_url(url)
        or omr.report_date_from_pdf_meta(pdf_bytes)
        or datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    )
    text = omr.pdf_to_text(pdf_bytes)
    records = omr.parse_tables(text)
    if not records:
        raise RuntimeError(f"OMR parser produced 0 records from {url} — PDF layout may have changed")
    n = omr.upsert_records(conn, report_date, records)
    conn.commit()
    logger.info(f"OMR refresh: {n} rows from {url} report_date={report_date}")
    return {"records": n, "report_date": report_date, "url": url}


def fetch_steo(conn: sqlite3.Connection):
    """Fetch STEO via the centralized pipeline and write into sqlite."""
    logger.info("Fetching STEO via tanker/pipelines/steo.py")
    content = steo.download_workbook()
    series_ids = list(STEO_SERIES.keys())
    results = steo.extract_series(content, series_ids, sheet="3atab")

    total = 0
    for sid, df in results.items():
        if df.empty:
            logger.warning(f"  STEO {sid}: no data")
            continue
        rows = [
            (sid, d.strftime("%Y-%m-%d"), round(float(v), 2))
            for d, v in zip(df["date"], df["value"])
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO steo_monthly (series_id, date, value) VALUES (?, ?, ?)",
            rows,
        )
        total += len(rows)
        logger.info(f"  STEO {sid}: {len(rows)} months")
    logger.info(f"  STEO total: {total} data points")


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Auto-refresh on startup if DB is empty or stale. Fire-and-forget so the
    # dashboard accepts connections immediately and the setup banner shows the
    # running state — blocking startup on a 30s EIA fetch makes the first-run
    # UX feel broken.
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM eia_weekly").fetchone()[0]
    last = conn.execute("SELECT value FROM meta WHERE key='last_refresh'").fetchone()
    conn.close()

    needs_refresh = count == 0
    if last:
        last_dt = datetime.fromisoformat(last[0])
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        if datetime.now(tz=timezone.utc) - last_dt > timedelta(hours=12):
            needs_refresh = True

    if needs_refresh:
        logger.info("Auto-refreshing EIA data on startup (background)...")
        asyncio.create_task(_run_tracked("eia", refresh_all()))

    yield


app = FastAPI(title="EIA Crude Dashboard", lifespan=lifespan)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/api/data")
async def get_data(months: int = Query(default=6, ge=1, le=60)):
    """Return merged weekly data for the last N months."""
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=months * 31)).strftime("%Y-%m-%d")
    conn = get_db()

    # Pivot series into columns
    query = """
        SELECT
            d.date,
            MAX(CASE WHEN d.series_id='us_stocks' THEN d.value END) as us_stocks_kbbl,
            MAX(CASE WHEN d.series_id='cushing' THEN d.value END) as cushing_kbbl,
            MAX(CASE WHEN d.series_id='exports' THEN d.value END) as exports_kbpd,
            MAX(CASE WHEN d.series_id='production' THEN d.value END) as production_kbpd,
            MAX(CASE WHEN d.series_id='utilization' THEN d.value END) as utilization_pct,
            MAX(CASE WHEN d.series_id='gasoline_stocks' THEN d.value END) as gasoline_kbbl,
            MAX(CASE WHEN d.series_id='distillate_stocks' THEN d.value END) as distillate_kbbl
        FROM eia_weekly d
        WHERE d.date >= ?
        GROUP BY d.date
        ORDER BY d.date
    """
    rows = conn.execute(query, (cutoff,)).fetchall()
    conn.close()

    data = []
    for r in rows:
        data.append(
            {
                "date": r["date"],
                "us_stocks_mbbl": round(r["us_stocks_kbbl"] / 1000, 3) if r["us_stocks_kbbl"] else None,
                "cushing_mbbl": round(r["cushing_kbbl"] / 1000, 3) if r["cushing_kbbl"] else None,
                "exports_mbpd": round(r["exports_kbpd"] / 1000, 3) if r["exports_kbpd"] else None,
                "production_mbpd": round(r["production_kbpd"] / 1000, 3) if r["production_kbpd"] else None,
                "utilization_pct": r["utilization_pct"],
                "gasoline_mbbl": round(r["gasoline_kbbl"] / 1000, 3) if r["gasoline_kbbl"] else None,
                "distillate_mbbl": round(r["distillate_kbbl"] / 1000, 3) if r["distillate_kbbl"] else None,
            }
        )
    return JSONResponse(data)


@app.get("/api/data/yoy")
async def get_yoy(series: str = Query(default="utilization")):
    """Return year-over-year overlay data for a series."""
    series_map = {
        "utilization": "utilization",
        "us_stocks": "us_stocks",
        "cushing": "cushing",
        "exports": "exports",
        "production": "production",
    }
    sid = series_map.get(series, "utilization")

    conn = get_db()
    rows = conn.execute(
        "SELECT date, value FROM eia_weekly WHERE series_id=? ORDER BY date",
        (sid,),
    ).fetchall()
    conn.close()

    # Group by year and ISO week
    from collections import defaultdict

    by_year = defaultdict(dict)
    for r in rows:
        dt = datetime.strptime(r["date"], "%Y-%m-%d")
        yr = dt.year
        wk = dt.isocalendar()[1]
        by_year[yr][wk] = r["value"]

    # Compute 5-year avg
    current_year = datetime.now(tz=timezone.utc).year
    avg_years = list(range(current_year - 5, current_year))

    result = []
    for wk in range(1, 53):
        entry = {"wk": wk}
        vals = [by_year[y].get(wk) for y in avg_years if by_year[y].get(wk) is not None]
        entry["avg5"] = round(sum(vals) / len(vals), 1) if vals else None
        for yr in [current_year - 2, current_year - 1, current_year]:
            entry[f"y{yr}"] = by_year[yr].get(wk)
        result.append(entry)

    return JSONResponse(result)


@app.get("/api/refresh")
async def trigger_refresh():
    await refresh_all()
    return {"status": "ok", "timestamp": datetime.now(tz=timezone.utc).isoformat()}


@app.get("/api/global")
async def get_global(months: int = Query(default=24, ge=6, le=72)):
    """Return STEO monthly global/OECD inventory and supply/demand data."""
    now = datetime.now(tz=timezone.utc)
    cutoff = (now - timedelta(days=months * 31)).strftime("%Y-%m-%d")
    upper = now.strftime("%Y-%m-01")
    conn = get_db()

    query = """
        SELECT
            d.date,
            MAX(CASE WHEN d.series_id='pasc_oecd_t3' THEN d.value END) as oecd_stocks,
            MAX(CASE WHEN d.series_id='pasc_us' THEN d.value END) as us_stocks,
            MAX(CASE WHEN d.series_id='pasc_ooecd_t3' THEN d.value END) as other_oecd_stocks,
            -MAX(CASE WHEN d.series_id='t3_stchange_world' THEN d.value END) as world_inv_change,
            MAX(CASE WHEN d.series_id='patc_world' THEN d.value END) as world_consumption,
            MAX(CASE WHEN d.series_id='papr_world' THEN d.value END) as world_production
        FROM steo_monthly d
        WHERE d.date >= ? AND d.date <= ?
        GROUP BY d.date
        ORDER BY d.date
    """
    rows = conn.execute(query, (cutoff, upper)).fetchall()
    conn.close()

    data = []
    for r in rows:
        entry = {"date": r["date"][:7]}  # YYYY-MM format
        for key in ["oecd_stocks", "us_stocks", "other_oecd_stocks",
                     "world_inv_change", "world_consumption", "world_production"]:
            entry[key] = round(r[key], 2) if r[key] is not None else None
        data.append(entry)
    return JSONResponse(data)


def _period_sort_key(period: str) -> tuple[int, int]:
    """Order periods chronologically with annual rolling up after Q4 of its year."""
    if "Q" in period:
        q, yr = period.split("Q")
        return (2000 + int(yr), int(q))
    return (int(period), 5)


# Headline rows from the OMR we expose on the dashboard. Section disambiguates
# rows that share a label (e.g. 'Total OECD' in DEMAND vs SUPPLY).
#
# total_supply sources from Table 1b ("World Oil Production w/ OPEC+ current
# agreement") instead of Table 1 — Table 1 leaves OPEC supply blank past the
# current quarter (IEA's forecasting policy), but Table 1b extends supply
# forward by assuming OPEC+ adheres to its published quotas, giving us a full
# 12-quarter line for charting.
_OMR_HEADLINE_ROWS: dict[str, tuple[str, str, str]] = {
    "total_demand":     ("1",  "NON-OECD DEMAND", "Total Demand"),
    "total_supply":     ("1b", "OPEC+ CRUDE",     "Total Supply"),
}


@app.get("/api/omr")
async def get_omr():
    """Return latest OMR issue's headline series: World demand/supply balance,
    stock changes, and the Call-on-OPEC residual. Each series is the per-period
    (quarterly + annual) values from Table 1 of the most recent ingested issue.

    Response shape:
        {
            "report_date": "YYYY-MM-DD",
            "series": {
                "total_demand":   [{period, period_type, value}, ...],
                "total_supply":   [...],
                ...
            }
        }
    """
    conn = get_db()
    rs = conn.execute("SELECT MAX(report_date) FROM omr_monthly").fetchone()
    report_date = rs[0] if rs else None
    if not report_date:
        conn.close()
        return JSONResponse({"report_date": None, "series": {}})

    series: dict[str, list[dict]] = {}
    for key, (table, section, row_label) in _OMR_HEADLINE_ROWS.items():
        rows = conn.execute(
            "SELECT period, period_type, value FROM omr_monthly "
            "WHERE report_date=? AND table_id=? AND section=? AND row_label=?",
            (report_date, table, section, row_label),
        ).fetchall()
        series[key] = sorted(
            [{"period": r["period"], "period_type": r["period_type"],
              "value": round(r["value"], 2) if r["value"] is not None else None}
             for r in rows],
            key=lambda r: _period_sort_key(r["period"]),
        )
    conn.close()
    return JSONResponse({"report_date": report_date, "series": series})


@app.get("/api/regional")
async def get_regional():
    """Return latest PADD-level crude stocks plus WoW and YoY deltas."""
    padd_series = {
        "padd1": ("padd1_stocks", "PADD 1 — East Coast"),
        "padd2": ("padd2_stocks", "PADD 2 — Midwest"),
        "padd3": ("padd3_stocks", "PADD 3 — Gulf Coast"),
        "padd4": ("padd4_stocks", "PADD 4 — Rocky Mountain"),
        "padd5": ("padd5_stocks", "PADD 5 — West Coast"),
        "cushing": ("cushing", "Cushing, OK (sub-PADD 2)"),
    }

    conn = get_db()
    out: dict = {}
    latest_date = None
    for key, (sid, label) in padd_series.items():
        rows = conn.execute(
            "SELECT date, value FROM eia_weekly WHERE series_id=? ORDER BY date DESC LIMIT 60",
            (sid,),
        ).fetchall()
        if not rows:
            out[key] = {"label": label, "current_mbbl": None}
            continue
        last = rows[0]
        prev = rows[1] if len(rows) > 1 else None
        # Look for the row from ~52 weeks ago
        yoy = next((r for r in rows if r["date"] <= _iso_minus_days(last["date"], 350)), None)

        cur = last["value"] / 1000 if last["value"] is not None else None
        wow = (last["value"] - prev["value"]) / 1000 if prev and last["value"] is not None and prev["value"] is not None else None
        yoy_d = (last["value"] - yoy["value"]) / 1000 if yoy and last["value"] is not None and yoy["value"] is not None else None

        out[key] = {
            "label": label,
            "current_mbbl": round(cur, 2) if cur is not None else None,
            "wow_mbbl": round(wow, 2) if wow is not None else None,
            "yoy_mbbl": round(yoy_d, 2) if yoy_d is not None else None,
            "as_of": last["date"],
        }
        if latest_date is None or last["date"] > latest_date:
            latest_date = last["date"]
    conn.close()

    return JSONResponse({"as_of": latest_date, "regions": out})


def _iso_minus_days(date_str: str, days: int) -> str:
    return (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")


import _tanker_class  # noqa: E402  (pipelines/_tanker_class.py — capacity + classifier)


_OIL_ON_WATER_CAVEAT = (
    "AIS coverage is uneven — heavy in NW Europe (~46%), sparse in Persian "
    "Gulf, Red Sea, West Africa (TDD §4.2.1.1). Total represents AIS-visible "
    "tankers only; the unobserved fleet (TDD §9.2) is not included. "
    "Per-vessel barrels = nominal class deadweight × clamped draught:design "
    "ratio (TDD §9.7 on draught reliability)."
)


@app.get("/api/ships")
async def get_ships(stale_days: int = Query(default=7, ge=1, le=30)):
    """Latest tanker positions from the most recent AIS snapshot.

    Each ship record carries an estimated tanker class + barrels-on-board
    figure derived from AIS-broadcast dimensions and draught. The top-level
    `oil_on_water` block aggregates these. Per TDD §9.7: AIS provides only
    self-declared identifiers and position — load state is inferred from
    draught and is noisy, especially for the dark fleet.

    Stale reports (older than `stale_days`, default 7) are dropped per TDD
    §4.2.1.1; override the threshold with `?stale_days=N`.
    """
    snapshot_uri = resolve_tanker_snapshot()
    fs = storage_fs()
    if not fs.exists(snapshot_uri):
        return JSONResponse({"snapshot_at": None, "total": 0, "ships": [],
                              "error": "no AIS snapshot found — run aisstream_phase1.py"})

    df = pd.read_parquet(snapshot_uri)
    snap_at = df["time_utc"].max() if not df.empty else None

    # Position-staleness filter — drop reports older than `stale_days` from
    # the snapshot's most-recent observation. Per-row comparison (not vs. now)
    # so an old snapshot doesn't filter out all of itself. aisstream serializes
    # timestamps as `2026-05-15 10:00:00.123 +0000 UTC` — the trailing ' UTC'
    # duplicates the offset and confuses pandas; strip it before parsing.
    def _parse_ais_ts(s):
        if s is None:
            return pd.NaT
        s = str(s).replace(" UTC", "").strip()
        return pd.to_datetime(s, errors="coerce", utc=True)

    if not df.empty and snap_at:
        cutoff = _parse_ais_ts(snap_at) - pd.Timedelta(days=stale_days)
        if pd.notna(cutoff):
            ts = df["time_utc"].apply(_parse_ais_ts)
            df = df[(ts.isna()) | (ts >= cutoff)].copy()

    # Optional fields that older snapshots may not carry. Tolerate their
    # absence by defaulting to None columns.
    for opt_col in ("length_m", "beam_m", "max_draught_m", "cog",
                    "nav_status", "true_heading"):
        if opt_col not in df.columns:
            df[opt_col] = None

    # Compute per-vessel barrels estimate + class.
    classes: list[str | None] = []
    barrels: list[int] = []
    for _, r in df.iterrows():
        b, c = _tanker_class.barrels_estimate(
            r.get("length_m"), r.get("beam_m"),
            None,                     # current draught: not in AIS PR; AIS
                                      # broadcasts MaximumStaticDraught only.
                                      # Until current draught is wired in, the
                                      # estimate uses the laden_ratio default.
            r.get("max_draught_m"),
        )
        classes.append(c)
        barrels.append(b)
    df = df.assign(tanker_class=classes, barrels_estimate=barrels)

    cols = [
        "mmsi", "name", "ship_type", "latitude", "longitude", "sog", "cog",
        "true_heading", "nav_status", "time_utc", "destination",
        "max_draught_m", "length_m", "beam_m",
        "tanker_class", "barrels_estimate",
    ]
    records = df[cols].to_dict(orient="records")
    for r in records:
        for k, v in list(r.items()):
            if isinstance(v, float) and math.isnan(v):
                r[k] = None
            elif hasattr(v, "item"):  # numpy scalars
                r[k] = v.item()

    # Aggregate "crude on water" — directional, biased toward AIS-visible fleet.
    classified_mask = df["tanker_class"].notna()
    classified = df[classified_mask]
    total_bbl = int(classified["barrels_estimate"].sum())
    by_class: dict[str, dict] = {}
    for cls, grp in classified.groupby("tanker_class"):
        by_class[str(cls)] = {
            "n": int(len(grp)),
            "bbl": int(grp["barrels_estimate"].sum()),
        }
    # Rough laden/ballast split using draught reports where present. When
    # draught is missing we land in the default ratio (0.6) which doesn't tell
    # us laden vs. ballast — those vessels go in `unknown_state_bbl`.
    laden_bbl = ballast_bbl = unknown_state_bbl = 0
    for _, r in classified.iterrows():
        d = r.get("max_draught_m")
        # AIS doesn't broadcast current draught on PRs, so without a separate
        # signal everyone falls into 'unknown'. Reserved for when we wire in
        # current draught (Type-1 PR extension or per-port deepening checks).
        if d is None or pd.isna(d):
            unknown_state_bbl += r["barrels_estimate"]
        else:
            # Stub: treat draught presence as "laden-leaning" and absence as
            # ballast-leaning; this will be replaced when current draught
            # wiring lands.
            laden_bbl += r["barrels_estimate"]

    oil_on_water = {
        "total_bbl": total_bbl,
        "laden_bbl": int(laden_bbl),
        "ballast_bbl": int(ballast_bbl),
        "unknown_state_bbl": int(unknown_state_bbl),
        "by_class": by_class,
        "n_classified": int(classified_mask.sum()),
        "n_unclassified": int(len(df) - classified_mask.sum()),
        "stale_days_filter": stale_days,
        "caveat": _OIL_ON_WATER_CAVEAT,
    }

    return JSONResponse({
        "snapshot_at": snap_at,
        "total": len(records),
        "oil_on_water": oil_on_water,
        "ships": records,
    })


@app.get("/api/sar_detections")
async def get_sar_detections():
    """Return transient over-water SAR vessel detections, aggregated across all AOIs.

    Reads `clusters.parquet` produced by `sar_aggregate.py` from each AOI under
    sentinel_sar/ and filters to clusters that are NOT persistent (so we drop
    fixed infrastructure / long-anchored objects) and NOT on land. The result
    is the candidate vessel set the dashboard's tanker map should overlay.
    """
    sar_root = data_uri("sentinel_sar")
    fs = storage_fs()
    if not fs.exists(sar_root):
        return JSONResponse({"aois": [], "total": 0, "detections": [],
                              "error": "sentinel_sar/ missing"})

    rows: list[dict] = []
    aoi_summaries: list[dict] = []
    last_seen_global: str | None = None

    for aoi_uri in sorted(_list_aoi_dirs(fs, sar_root)):
        aoi_name = aoi_uri.rstrip("/").rsplit("/", 1)[-1]
        clusters_uri = data_uri("sentinel_sar", aoi_name, "clusters.parquet")
        if not fs.exists(clusters_uri):
            continue
        df = pd.read_parquet(clusters_uri)
        if df.empty:
            continue
        for c in ("lat", "lon", "sigma0_max_db", "n_scenes", "n_detections"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c])
        if "is_persistent" in df.columns:
            df["is_persistent"] = df["is_persistent"].astype(bool)
        if "any_on_land" in df.columns:
            df["any_on_land"] = df["any_on_land"].astype(bool)

        kept = df[(~df["is_persistent"]) & (~df["any_on_land"])]
        for _, r in kept.iterrows():
            ls = str(r.get("last_seen") or "")
            if ls and (last_seen_global is None or ls > last_seen_global):
                last_seen_global = ls
            rows.append({
                "aoi": aoi_name,
                "lat": float(r["lat"]),
                "lon": float(r["lon"]),
                "last_seen": ls,
                "n_scenes": int(r.get("n_scenes") or 0),
                "sigma0_max_db": float(r.get("sigma0_max_db") or 0.0),
            })

        aoi_summaries.append({
            "name": aoi_name,
            "transient_water_count": int(len(kept)),
            "persistent_water_count": int(((df["is_persistent"]) & (~df["any_on_land"])).sum()),
        })

    return JSONResponse({
        "aois": aoi_summaries,
        "total": len(rows),
        "last_seen": last_seen_global,
        "detections": rows,
    })


import _terminals  # noqa: E402  pipelines/_terminals.py — terminal hotspots + haversine


def _load_persistent_water_clusters() -> tuple[pd.DataFrame, str | None]:
    """Return DataFrame of persistent, over-water SAR clusters across all AOIs.

    Columns: aoi, lat, lon, n_scenes, n_detections, sigma0_max_db, first_seen,
    last_seen. The persistent-and-not-on-land filter is applied here so the
    callers (`/api/sar_floating_storage`, `/api/sar_anchorages`) don't each
    re-implement it. Returns (df, last_seen_global) where last_seen_global is
    the most recent cluster observation across all AOIs (for "as of" display).
    """
    sar_root = data_uri("sentinel_sar")
    fs = storage_fs()
    if not fs.exists(sar_root):
        return pd.DataFrame(), None

    frames: list[pd.DataFrame] = []
    last_seen_global: str | None = None
    for aoi_uri in sorted(_list_aoi_dirs(fs, sar_root)):
        aoi_name = aoi_uri.rstrip("/").rsplit("/", 1)[-1]
        clusters_uri = data_uri("sentinel_sar", aoi_name, "clusters.parquet")
        if not fs.exists(clusters_uri):
            continue
        df = pd.read_parquet(clusters_uri)
        if df.empty:
            continue
        for c in ("lat", "lon", "sigma0_max_db", "n_scenes", "n_detections"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        for c in ("is_persistent", "any_on_land"):
            if c in df.columns:
                df[c] = df[c].astype(bool)
        kept = df[df["is_persistent"] & (~df["any_on_land"])].copy()
        if kept.empty:
            continue
        kept["aoi"] = aoi_name
        # Track latest observation timestamp globally
        if "last_seen" in kept.columns:
            ls = str(kept["last_seen"].max())
            if ls and (last_seen_global is None or ls > last_seen_global):
                last_seen_global = ls
        frames.append(kept)

    if not frames:
        return pd.DataFrame(), last_seen_global
    return pd.concat(frames, ignore_index=True), last_seen_global


def _snapshot_floating_storage(
    conn: sqlite3.Connection, observed_at: str, terminals: list[dict],
) -> int:
    """Upsert per-terminal counts into the history table. Idempotent on
    (observed_at, terminal_name) so repeated calls within the same SAR
    observation window don't multiply rows. Returns row count inserted/
    replaced."""
    rows = [
        (observed_at, t["name"], t["persistent_count"], t.get("mean_sigma0_db"))
        for t in terminals
    ]
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO sar_floating_storage_history "
        "(observed_at, terminal_name, persistent_count, mean_sigma0_db) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def _floating_storage_history(
    conn: sqlite3.Connection, days: int,
) -> dict[str, list[dict]]:
    """Read per-terminal history for the last `days` days. Returns a dict
    keyed by terminal_name; each value is an ordered list of {observed_at,
    count, sigma0} oldest-first."""
    cutoff = (datetime.now(tz=timezone.utc).date() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT observed_at, terminal_name, persistent_count, mean_sigma0_db "
        "FROM sar_floating_storage_history WHERE observed_at >= ? "
        "ORDER BY terminal_name, observed_at",
        (cutoff,),
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["terminal_name"], []).append({
            "observed_at": r["observed_at"],
            "persistent_count": r["persistent_count"],
            "mean_sigma0_db": r["mean_sigma0_db"],
        })
    return out


@app.get("/api/sar_floating_storage")
async def get_sar_floating_storage(history_days: int = Query(default=120, ge=14, le=730)):
    """Count of persistent (>=3-scene) over-water SAR clusters near each
    named terminal hotspot. A high count at e.g. Singapore Eastern OPL
    implies floating-storage build-up (anchored tankers used as
    contango-trade storage).

    Each call snapshots the current counts into `sar_floating_storage_history`
    keyed on (observed_at, terminal_name) — idempotent within a single SAR
    observation window. As the weekly SAR cron progresses, history
    accumulates and the response's per-terminal `history` array grows.

    Methodology per TDD §12.3: persistent clusters within each terminal's
    radius are counted. Caveats:
      - SAR at ~120 m/px can't distinguish VLCC from Suezmax, so this is
        a vessel COUNT, not a volume estimate.
      - Persistent ≥3 scenes filter catches vessels visible across at least
        ~9 days; quick port calls aren't included.
      - Coverage is limited to configured AOIs (see scheduler.AOIS).
        Terminals in unconfigured regions will report 0 erroneously.
    """
    df, last_seen = _load_persistent_water_clusters()
    out: list[dict] = []
    for term in _terminals.iter_terminals():
        if df.empty:
            count = 0
            mean_sigma0 = None
        else:
            # Vectorised haversine across the cluster set
            import numpy as np
            lat1 = math.radians(term["lat"])
            lat2 = np.radians(df["lat"].to_numpy())
            dlat = lat2 - lat1
            dlon = np.radians(df["lon"].to_numpy() - term["lon"])
            a = (np.sin(dlat / 2) ** 2
                 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2)
            dist_km = 2 * 6371.0 * np.arcsin(np.sqrt(a))
            mask = dist_km <= term["radius_km"]
            count = int(mask.sum())
            mean_sigma0 = (float(df.loc[mask, "sigma0_max_db"].mean())
                           if count and "sigma0_max_db" in df.columns else None)
        out.append({
            **term,
            "persistent_count": count,
            "mean_sigma0_db": (round(mean_sigma0, 1) if mean_sigma0 is not None else None),
        })

    # Accumulate weekly snapshots. observed_at is the date portion of the
    # latest SAR cluster timestamp (the data's "as of"), not now() — keeps
    # the time series tied to data freshness rather than view freshness.
    observed_at: str | None = None
    if last_seen:
        # last_seen is e.g. "2026-05-14T00:26:54Z" — take date portion.
        observed_at = str(last_seen)[:10]
        try:
            conn = get_db()
            _snapshot_floating_storage(conn, observed_at, out)
            history = _floating_storage_history(conn, history_days)
        finally:
            conn.close()
        # Attach per-terminal history (oldest-first) to each terminal record.
        for term in out:
            term["history"] = history.get(term["name"], [])

    return JSONResponse({
        "as_of": last_seen,
        "observed_at": observed_at,
        "terminals": out,
        "methodology": (
            "Counts persistent (>=3-scene) over-water SAR clusters within each "
            "terminal's radius. Vessel count, not volume — SAR at ~120 m/px "
            "cannot distinguish tanker class. Each call snapshots into "
            "sar_floating_storage_history; week-over-week change in `history` "
            "is the actionable signal. See TDD §12.3."
        ),
    })


@app.get("/api/sar_anchorages")
async def get_sar_anchorages(grid_deg: float = Query(default=0.5, ge=0.1, le=2.0)):
    """0.5°-grid density of persistent over-water SAR clusters.

    Useful for spotting anchorage hotspots that ISN'T at a named terminal
    (e.g. growth at an unmapped STS zone). Per TDD §12.3; the actionable
    view is week-over-week change at named terminals (Phase 3b — not yet
    accumulated). For now this is a snapshot heatmap source.
    """
    df, last_seen = _load_persistent_water_clusters()
    cells: list[dict] = []
    if not df.empty:
        # Bin to grid cells (floor to the grid_deg multiple)
        df["lat_bin"] = (df["lat"] / grid_deg).apply(math.floor) * grid_deg
        df["lon_bin"] = (df["lon"] / grid_deg).apply(math.floor) * grid_deg
        grouped = df.groupby(["lat_bin", "lon_bin"], as_index=False).agg(
            count=("lat", "size"),
            mean_sigma0_db=("sigma0_max_db", "mean"),
        )
        # Sort by density descending — most actionable first
        grouped = grouped.sort_values("count", ascending=False)
        for _, r in grouped.iterrows():
            cells.append({
                "lat_bin": float(r["lat_bin"]),
                "lon_bin": float(r["lon_bin"]),
                "count": int(r["count"]),
                "mean_sigma0_db": round(float(r["mean_sigma0_db"]), 1)
                                  if not pd.isna(r["mean_sigma0_db"]) else None,
            })

    return JSONResponse({
        "as_of": last_seen,
        "grid_deg": grid_deg,
        "cells": cells,
    })


@app.get("/api/status")
async def status():
    """Health snapshot — used by the empty-state UI to decide what's missing.

    Returns per-pipeline row counts, latest timestamps, and current ingest
    state (idle/running/error). Frontend polls this every few seconds while
    a job is running."""
    conn = get_db()
    series: dict[str, dict] = {}
    for sid in EIA_SERIES:
        row = conn.execute(
            "SELECT COUNT(*) as n, MAX(date) as latest FROM eia_weekly WHERE series_id=?",
            (sid,),
        ).fetchone()
        series[sid] = {"rows": row["n"], "latest": row["latest"]}
    eia_total = conn.execute("SELECT COUNT(*) FROM eia_weekly").fetchone()[0]

    steo_row = conn.execute(
        "SELECT COUNT(*) as n, MAX(date) as latest FROM steo_monthly"
    ).fetchone()
    last = conn.execute("SELECT value FROM meta WHERE key='last_refresh'").fetchone()
    conn.close()

    return {
        "storage_root": storage_root(),
        "storage_mode": "s3" if s3_enabled() else "local",
        "db_path": str(DB_PATH),
        "eia": {
            "total_rows": eia_total,
            "last_refresh": last[0] if last else None,
            "series": series,
            "ingest": INGEST_STATE["eia"],
        },
        "steo": {
            "rows": steo_row["n"],
            "latest": steo_row["latest"],
        },
        "omr": _omr_status(),
        "ais": _ais_status(),
        "sar": _sar_status(),
    }


def _omr_status() -> dict:
    conn = get_db()
    rs = conn.execute(
        "SELECT MAX(report_date) AS latest, COUNT(DISTINCT report_date) AS issues, "
        "COUNT(*) AS total_rows FROM omr_monthly"
    ).fetchone()
    conn.close()
    return {
        "latest_report": rs["latest"],
        "issues": rs["issues"] or 0,
        "total_rows": rs["total_rows"] or 0,
        "ingest": INGEST_STATE["omr"],
    }


# ---------------------------------------------------------------------------
# Ingest endpoints + state tracking
# ---------------------------------------------------------------------------
#
# Each ingest pipeline is launched as a non-blocking background task. The
# in-memory INGEST_STATE dict tracks running/idle/error per pipeline. Frontend
# polls /api/status to know when a fresh run finishes and the panel can refresh.
#
# Synchronization is "first writer wins": triggering a pipeline that's already
# running returns 409. The scheduler container can run the same jobs on a cron
# trigger; if both fire, you get a duplicate run (rare and idempotent on disk).
# ---------------------------------------------------------------------------

INGEST_PIPELINES = ("eia", "omr", "ais", "ais-census", "sar")
INGEST_STATE: dict[str, dict] = {p: {"status": "idle"} for p in INGEST_PIPELINES}


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _set_state(pipeline: str, status: str, **extra) -> None:
    state = INGEST_STATE[pipeline]
    state["status"] = status
    state.update(extra)


async def _run_tracked(pipeline: str, awaitable) -> None:
    _set_state(pipeline, "running",
               started_at=_now_iso(), finished_at=None, error=None)
    try:
        await awaitable
        _set_state(pipeline, "idle",
                   finished_at=_now_iso(), error=None)
        logger.info(f"ingest {pipeline} done")
    except Exception as e:
        logger.exception(f"ingest {pipeline} failed")
        _set_state(pipeline, "idle",
                   finished_at=_now_iso(), error=str(e)[:500])


def _list_aoi_dirs(fs, sar_root: str) -> list[str]:
    """List immediate subdirs of sentinel_sar/. Works for both local and s3."""
    try:
        entries = fs.ls(sar_root, detail=True)
    except FileNotFoundError:
        return []
    return [e["name"] for e in entries if e.get("type") == "directory"]


def _fs_mtime_iso(fs, uri: str) -> str | None:
    try:
        info = fs.info(uri)
    except (FileNotFoundError, OSError):
        return None
    # Local fsspec returns 'mtime' as float epoch; s3fs returns 'LastModified' as datetime.
    mtime = info.get("mtime")
    if mtime is not None:
        return datetime.fromtimestamp(float(mtime), tz=timezone.utc).isoformat()
    last_modified = info.get("LastModified") or info.get("last_modified")
    if last_modified is not None:
        if isinstance(last_modified, datetime):
            return last_modified.astimezone(timezone.utc).isoformat()
        return str(last_modified)
    return None


def _ais_snapshot_info() -> dict:
    uri = resolve_tanker_snapshot()
    fs = storage_fs()
    if not fs.exists(uri):
        return {"snapshot_at": None, "n_ships": 0, "snapshot_path": uri}
    try:
        df = pd.read_parquet(uri)
        return {
            "snapshot_at": str(df["time_utc"].max()) if not df.empty else None,
            "n_ships": int(len(df)),
            "snapshot_path": uri,
        }
    except Exception as e:
        return {"snapshot_at": None, "n_ships": 0, "error": str(e)[:200]}


def _ais_manifest_info() -> dict:
    census_root = data_uri("aisstream", "census")
    fs = storage_fs()
    if not fs.exists(census_root):
        return {"manifest_at": None, "manifest_mmsis": 0}
    try:
        manifests = sorted(fs.glob(f"{census_root}/summary_*.json"))
    except (FileNotFoundError, OSError):
        return {"manifest_at": None, "manifest_mmsis": 0}
    if not manifests:
        return {"manifest_at": None, "manifest_mmsis": 0}
    latest = manifests[-1]
    info = {
        "manifest_at": _fs_mtime_iso(fs, latest),
        "manifest_mmsis": 0,
        "manifest_path": latest,
    }
    try:
        with fs.open(latest) as f:
            m = json.load(f)
        info["manifest_mmsis"] = len(m.get("tanker_manifest", []) or [])
    except Exception:
        pass
    return info


def _ais_status() -> dict:
    return {
        "enabled": aisstream_enabled(),
        **_ais_snapshot_info(),
        **_ais_manifest_info(),
        "ingest_phase1": INGEST_STATE["ais"],
        "ingest_census": INGEST_STATE["ais-census"],
    }


def _sar_status() -> dict:
    aois: list[dict] = []
    sar_root = data_uri("sentinel_sar")
    fs = storage_fs()
    if fs.exists(sar_root):
        for aoi_uri in sorted(_list_aoi_dirs(fs, sar_root)):
            aoi_name = aoi_uri.rstrip("/").rsplit("/", 1)[-1]
            clusters = data_uri("sentinel_sar", aoi_name, "clusters.parquet")
            if not fs.exists(clusters):
                continue
            try:
                df = pd.read_parquet(clusters)
                n_transient = (
                    int((~df["is_persistent"].astype(bool)).sum())
                    if "is_persistent" in df.columns else int(len(df))
                )
            except Exception:
                df = pd.DataFrame()
                n_transient = 0
            aois.append({
                "name": aoi_name,
                "clusters_at": _fs_mtime_iso(fs, clusters),
                "n_clusters": int(len(df)),
                "n_transient": n_transient,
            })
    return {
        "enabled": sar_enabled(),
        "aois": aois,
        "ingest": INGEST_STATE["sar"],
    }


def _ingest_busy(pipeline: str) -> bool:
    return INGEST_STATE[pipeline].get("status") == "running"


@app.post("/api/ingest/eia")
async def ingest_eia():
    """Pull EIA weekly + STEO monthly. Always available — no API key needed
    (uses public XLS download fallback when EIA_API_KEY is not set)."""
    if _ingest_busy("eia"):
        return JSONResponse({"status": "already_running", "state": INGEST_STATE["eia"]}, status_code=409)
    asyncio.create_task(_run_tracked("eia", refresh_all()))
    return {"status": "started", "pipeline": "eia"}


async def _refresh_omr_async() -> None:
    """Run the synchronous omr.refresh job in a worker thread with its own DB conn."""
    def _go():
        conn = get_db()
        try:
            refresh_omr(conn)
        finally:
            conn.close()
    await asyncio.to_thread(_go)


@app.post("/api/ingest/omr")
async def ingest_omr():
    """Pull the latest free IEA OMR PDF, parse Tables 1/1a/1b, and upsert into
    omr_monthly. Auto-discovers the URL from iea.org by default; override with
    OMR_PDF_URL env var (or run pipelines/omr.py manually with --local-pdf)."""
    if _ingest_busy("omr"):
        return JSONResponse({"status": "already_running", "state": INGEST_STATE["omr"]}, status_code=409)
    asyncio.create_task(_run_tracked("omr", _refresh_omr_async()))
    return {"status": "started", "pipeline": "omr"}


@app.post("/api/ingest/ais")
async def ingest_ais():
    """Run AIS Phase 1 (30-min position capture). Requires AISSTREAM_API_KEY
    and a prior census manifest."""
    if not aisstream_enabled():
        return JSONResponse({"status": "not_configured",
                             "reason": "AISSTREAM_API_KEY not set"}, status_code=400)
    if _ingest_busy("ais"):
        return JSONResponse({"status": "already_running", "state": INGEST_STATE["ais"]}, status_code=409)

    import scheduler as _sched
    asyncio.create_task(_run_tracked("ais", asyncio.to_thread(_sched.run_ais_phase1)))
    return {"status": "started", "pipeline": "ais"}


@app.post("/api/ingest/ais-census")
async def ingest_ais_census(duration_seconds: int = Query(default=86400, ge=60, le=86400 * 7)):
    """Run AIS Phase 0 census (default 24h). Discovers crude-tanker MMSIs to
    feed Phase 1. Long-running — only needed when bootstrapping a fresh setup."""
    if not aisstream_enabled():
        return JSONResponse({"status": "not_configured",
                             "reason": "AISSTREAM_API_KEY not set"}, status_code=400)
    if _ingest_busy("ais-census"):
        return JSONResponse({"status": "already_running", "state": INGEST_STATE["ais-census"]}, status_code=409)

    output_uri = data_uri("aisstream", "census")
    fs = storage_fs()
    if not s3_enabled():
        Path(output_uri).mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(Path(__file__).parent / "pipelines" / "aisstream_census.py"),
        "--output", output_uri,
        "--duration-seconds", str(duration_seconds),
    ]

    async def _run_subprocess():
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"aisstream_census exited {proc.returncode}: {out.decode(errors='replace')[-500:]}")

    asyncio.create_task(_run_tracked("ais-census", _run_subprocess()))
    return {"status": "started", "pipeline": "ais-census", "duration_seconds": duration_seconds}


@app.post("/api/ingest/sar")
async def ingest_sar():
    """Run SAR ingest + CFAR detect + KDTree aggregate for all configured AOIs.
    Sentinel Hub PUs cost real money — see docs/WTI_Tanker_Forecast_TDD.md §4.2.2.1."""
    if not sar_enabled():
        return JSONResponse({"status": "not_configured",
                             "reason": "SAR disabled (SAR_ENABLED=false or CDSE creds missing)"},
                            status_code=400)
    if _ingest_busy("sar"):
        return JSONResponse({"status": "already_running", "state": INGEST_STATE["sar"]}, status_code=409)

    import scheduler as _sched
    asyncio.create_task(_run_tracked("sar", asyncio.to_thread(_sched.run_sar_ingest)))
    return {"status": "started", "pipeline": "sar"}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n  EIA Crude Oil Dashboard")
    print("  http://localhost:8050\n")
    uvicorn.run(app, host="0.0.0.0", port=8050, log_level="info")
