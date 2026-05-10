# oil-inventory-dashboard

Self-hosted oil markets dashboard:

- **US crude inventories** — EIA Weekly Petroleum Status Report (WPSR)
- **Global supply / demand** — EIA Short-Term Energy Outlook (STEO)
- **Regional inventories** — by PADD region with WoW + YoY deltas
- **Tanker positions** — AIS (aisstream.io) + Sentinel-1 SAR (Copernicus / CDSE)

<img width="1768" height="4657" alt="image" src="https://github.com/user-attachments/assets/5b66cf2e-ca46-44d9-a20f-c634212fc1dd" />


All data is fetched from public sources you authenticate to with your own
free-tier keys; nothing is shipped pre-baked. The dashboard renders empty-state
cards on first run and walks you through bootstrapping each pipeline.

```
+-------------------------------------------+
|  EIA inventories     [no data yet]   [Pull EIA data]
|  AIS tanker manifest [no manifest]   [Run AIS census (24h)]
|  SAR detections      [no data yet]   [Run SAR ingest]
+-------------------------------------------+
```

## Quick start

```bash
# 1. Clone + enter
git clone https://github.com/abwagner/oil-inventory-dashboard.git
cd oil-inventory-dashboard

# 2. Configure (all keys are optional — leave a block blank to disable that pipeline)
cp .env.example .env
$EDITOR .env

# 3. Install + run
uv sync                          # or: python -m venv .venv && pip install -r requirements.txt
.venv/bin/uvicorn app:app --port 8050
# → http://localhost:8050
```

The dashboard auto-pulls EIA on first boot (no key needed; it falls back to
public XLS downloads). AIS and SAR ingestion are opt-in via the **Setup**
banner at the top of the page.

### Docker

```bash
docker build -t oil-inventory-dashboard .
docker run -p 8050:8050 -v $(pwd)/data:/data --env-file .env oil-inventory-dashboard
```

For the scheduler container alongside the dashboard, see [Scheduler](#scheduler).

## API keys

| Provider | Used for | Free tier? | Where |
|---|---|---|---|
| [EIA Open Data](https://www.eia.gov/opendata/register.php) | EIA v2 API (optional — XLS fallback works keyless) | Yes | `EIA_API_KEY` |
| [aisstream.io](https://aisstream.io) | AIS WebSocket (tanker positions) | Yes | `AISSTREAM_API_KEY` |
| [Copernicus Data Space](https://dataspace.copernicus.eu) | Sentinel-1 SAR (radar tanker detections) | Yes — but PUs cost real $$ if you exceed quota | `CDSE_CLIENT_ID`, `CDSE_CLIENT_SECRET` |

## Costs & quotas

Most pipelines are free at the volumes the default config produces. **SAR is
the exception** and it's important to understand the model before enabling it:

- **Sentinel Hub** (under CDSE) charges in *Processing Units* (PUs). The free
  tier ships ~30k PU/month.
- The default AOIs (Persian Gulf / Strait of Hormuz, US Gulf Coast) at
  ~88 m/px run **~4–8k PU per acquisition** when fully tiled. With ~20
  acquisitions/AOI/month from the S1A+S1C+S1D constellation, you'll see
  ~8–10k PU/month for two AOIs.
- Each additional AOI roughly proportionally adds to the bill. Adding West
  Africa + Brazil + Malacca on top of the defaults can push past the free
  tier in two weeks. See `docs/WTI_Tanker_Forecast_TDD.md` §4.2.2.1 for the
  detailed empirical PU model.
- **Over-quota behavior**: the order endpoint returns HTTP 403 and tile
  responses become watermarked. Your subscription is suspended until top-up
  or upgrade. There is no surprise overage bill — but there is also no
  documented pay-per-PU model, so you'd have to upgrade plans to recover.

If you don't want to enable SAR, leave `CDSE_CLIENT_ID` blank or set
`SAR_ENABLED=false`. The dashboard hides the SAR setup row and the scheduler
skips the SAR job.

EIA, STEO, and aisstream.io are unmetered for the volumes used here.

## How a fresh user kicks off ingestion

The **Setup** banner at the top of the page exposes one button per missing
pipeline. Each button calls a `POST /api/ingest/<pipeline>` endpoint, which
launches the job as a background task. The page polls `/api/status` while the
job runs and refreshes the relevant chart panels when it completes.

| Pipeline | Trigger button | Typical duration | Notes |
|---|---|---|---|
| EIA + STEO | "Pull EIA data" | ~30 s | Auto-runs on first dashboard boot. |
| AIS census | "Run AIS census (24h)" | 24 h (configurable) | Discovers crude-tanker MMSIs to filter on. Run once, then keep the manifest. |
| AIS Phase 1 | "Pull positions" | 30 min | Snapshot of latest tanker positions. Available after census. Re-runs on a 4 h cron. |
| SAR ingest | "Run SAR ingest" | ~10–20 min | Sentinel-1 IW GRD over each configured AOI → CFAR detect → cluster. Re-runs weekly. |

Manual CLI invocation is also available — each pipeline file under `pipelines/`
has its own `--help`. The HTTP endpoints are thin wrappers around them.

## Scheduler

For unattended deployment, run `scheduler.py` as a separate process. It
re-runs AIS Phase 1 every 4 hours and SAR ingest weekly, and on boot will
auto-kick AIS Phase 1 if a manifest is present but the snapshot is stale.

```bash
# Local — alongside the dashboard
.venv/bin/python scheduler.py

# Docker — second container sharing the same volume + .env
docker run -v $(pwd)/data:/data --env-file .env \
    oil-inventory-dashboard python scheduler.py
```

The scheduler skips any job whose credentials are missing — so a setup with
`AISSTREAM_API_KEY` set but no CDSE creds runs only the AIS schedule.

## API endpoints

| Endpoint | Returns |
|---|---|
| `GET /` | Dashboard HTML |
| `GET /api/status` | Per-pipeline row counts, last-refresh timestamps, ingest state |
| `GET /api/data?months=N` | Weekly EIA series for last N months |
| `GET /api/data/yoy?series=…` | Year-over-year overlay for one series |
| `GET /api/regional` | PADD-level inventory map |
| `GET /api/global?months=N` | STEO monthly OECD/world supply-demand |
| `GET /api/ships` | Latest AIS tanker positions |
| `GET /api/sar_detections` | Transient over-water SAR clusters across AOIs |
| `POST /api/ingest/eia` | Kick EIA + STEO refresh (idempotent) |
| `POST /api/ingest/ais` | Kick AIS Phase 1 (30 min capture) |
| `POST /api/ingest/ais-census?duration_seconds=N` | Kick AIS Phase 0 census |
| `POST /api/ingest/sar` | Kick SAR ingest + detect + aggregate |

All ingest endpoints are non-blocking: they return `{status: "started"}` and
update `INGEST_STATE` so `/api/status` reports `running`. They return `409` if
a duplicate trigger comes in for an already-running pipeline.

## Repo layout

```
oil-inventory-dashboard/
├── app.py                FastAPI dashboard + ingest endpoints
├── scheduler.py          APScheduler daemon (separate process)
├── templates/index.html  D3 + Chart.js front end
├── pipelines/            One process per file, all CLI-invocable
│   ├── _env.py                   .env loader + feature gates
│   ├── steo.py                   EIA STEO monthly xlsx
│   ├── aisstream_census.py       Phase 0 baseline census
│   ├── aisstream_phase1.py       Phase 1 position collector
│   ├── aisstream_snapshot_from_census.py
│   ├── sentinel_sar.py           CDSE Sentinel Hub Process API client
│   ├── sar_detect.py             CFAR ship detection on sigma0 dB rasters
│   └── sar_aggregate.py          Cross-acquisition KDTree clustering
├── scripts/sar-ingest-all.sh    Wrapper for ad-hoc / cron SAR runs
├── docs/
│   └── WTI_Tanker_Forecast_TDD.md      Observation pipeline (SAR + AIS)
├── Dockerfile            Two-process image (uvicorn + scheduler)
├── pyproject.toml
├── requirements.txt      (kept in sync for the slim Docker base)
└── .env.example
```

## Data layout

The dashboard supports two storage backends:

- **Local filesystem** (default): everything under `DATA_DIR` (default `./data/`).
- **S3-compatible** (MinIO, AWS S3, etc.): non-sqlite reads pull from
  `s3://<S3_BUCKET>/...` when `S3_BUCKET` + `AWS_ACCESS_KEY_ID` +
  `AWS_SECRET_ACCESS_KEY` are all set. The sqlite EIA database always lives on
  local disk (sqlite isn't S3-friendly).

Subdirectories / object key prefixes used:

- `eia-dashboard/eia_data.db` — sqlite, **local only**
- `aisstream/census/summary_*.json` + `raw_*.parquet` — Phase 0 census output
- `aisstream/snapshots/tanker_positions_latest.parquet` — live AIS snapshot,
  rewritten every 4 h by Phase 1
- `sentinel_sar/<aoi>/<date>/<scene_id>/{tile_*.tif,_scene.json}` — per-tile SAR
- `sentinel_sar/<aoi>/{aggregated_detections.parquet,clusters.parquet}` —
  CFAR + KDTree-clustered output

### Hybrid setup: server reads S3, laptop pipelines write local

A common setup splits read and write hosts:

- **Server** runs the dashboard with `S3_BUCKET` set → reads everything from MinIO.
- **Laptop** runs the pipelines (AIS Phase 1, SAR ingest) with `S3_BUCKET`
  unset → writes parquet/tif to local `DATA_DIR`.
- A periodic `mc mirror` (cron, systemd timer, or manual) lifts laptop writes
  into the bucket so the server sees fresh data.

Pipeline-side S3 writes (so the laptop writes directly to s3:// without the
mirror step) are tracked as a follow-up — see `pipelines/_env.py` for the
`data_uri()` / `storage_fs()` helpers that future PR will plumb through the
pipelines themselves.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `EIA_API_KEY` | (unset → XLS fallback) | EIA v2 API key |
| `AISSTREAM_API_KEY` | (unset → AIS disabled) | aisstream.io WebSocket key |
| `S3_BUCKET` | (unset → local mode) | When set, dashboard reads parquet + SAR rasters from `s3://<bucket>/...` |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | (unset → local mode) | S3 credentials. Required alongside `S3_BUCKET` |
| `AWS_ENDPOINT_URL` | (unset → AWS S3) | MinIO / custom S3 endpoint (e.g. `https://s3.example.com`) |
| `CDSE_CLIENT_ID`, `CDSE_CLIENT_SECRET` | (unset → SAR disabled) | Copernicus Data Space OAuth |
| `SAR_ENABLED` | `true` if CDSE set, else `false` | Override SAR gate |
| `DATA_DIR` | `<repo>/data` | Root for sqlite + parquet + SAR rasters |
| `EIA_DB_PATH` | `$DATA_DIR/eia-dashboard/eia_data.db` | Sqlite location override |
| `ENV_FILE` | `<repo>/.env` | .env path override (Docker secrets, etc.) |
| `TZ` | `UTC` | Cron expression timezone |

## License

MIT — see [LICENSE](LICENSE).
