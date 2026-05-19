# Plan: Free Sentinel-1 GRD + Sentinel-2 optical via DESP

## Context

The current SAR pipeline (`pipelines/sentinel_sar.py`) uses **Sentinel Hub's
Process API**, which is convenient (built-in tiling, projection, cloud filtering)
but bills in Processing Units (PUs) at ~30k/month free-tier ceiling. We're
running at ~25-30k PU/month across 5 AOIs as of 2026-05 — effectively at the
cap, with no headroom for historical backfill.

The same raw Sentinel-1 GRD products that Sentinel Hub processes for us are
also available **free of PUs** via the **Copernicus Data Space Ecosystem
(DESP)** — OData / STAC catalog + direct product download. The same CDSE
credentials we use for Sentinel Hub grant DESP access (Sentinel Hub auths
through DESP).

This unlocks two things we currently can't afford:

1. **Historical SAR backfill** — multi-month retrospective coverage with no
   PU spend. Lets `sar_floating_storage_history` populate retroactively
   instead of waiting 3+ months for it to accumulate forward.
2. **Sentinel-2 optical** as an additional data source — 10 m/px visible-band
   imagery that, when not cloud-blocked, can discriminate VLCC from Suezmax
   (SAR at our 120 m/px cannot).

We're **adding** these pipelines, not replacing the existing Sentinel Hub
path. The Sentinel Hub Process API is still the fastest way to get fresh
near-real-time scenes; DESP-raw is for backfill + new optical coverage,
where the per-scene latency (download + local processing) is acceptable.

## What we're NOT doing

- **Phase 4 (SAR↔AIS fusion for dark-fleet inference)** stays out of scope
  per [TDD §12.4](WTI_Tanker_Forecast_TDD.md#124-phase-4-designed-not-yet-implemented--sar-ais-fusion-dark-fleet-inference)
  — the dashboard's purpose is overall world tanker movements, not
  sanctions-specific tracking.
- **Replacing Sentinel Hub** — the existing path stays. DESP-raw runs in
  parallel for cost-asymmetric workloads (backfill, S2).
- **Sub-vessel-class discrimination from SAR alone** — still impossible at
  120 m/px. Optical (Step 2) is how we'd get this if needed.

## Step 0 — DESP capability test (this session, ~30 min)

Throwaway probe script `scripts/test_desp.py`. Goals:

1. **Auth**: CDSE OAuth2 client-credentials flow using existing
   `CDSE_CLIENT_ID` + `CDSE_CLIENT_SECRET`. Expects a Bearer token from
   `https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token`.
2. **OData catalog search** over Persian Gulf bbox (54°-60°E, 24°-28°N),
   last 14 days, for two collections:
   - `SENTINEL-1` IW GRD products (`contains(Name, 'GRD')`)
   - `SENTINEL-2` L2A products (`contains(Name, 'MSIL2A')`) with cloud
     cover < 30%
3. **Metadata report**: per-collection product count, average size,
   sample names, footprints.
4. **Partial download**: HTTP Range request for the first 1 MB of one
   product per collection to confirm the download endpoint works without
   pulling 8 GB.

**Pass criteria**: token obtained, ≥1 product per collection found,
partial download returns 206 with the requested bytes. No PUs spent, no
data persisted to disk.

Output: append findings to this doc under "Step 0 results" once run.

## Step 1 — Sentinel-1 GRD ingest pipeline

New `pipelines/sentinel_s1_grd.py` running parallel to `sentinel_sar.py`.

**Inputs**: AOI bbox list (reuse `scheduler.AOIS`), `--from` / `--to` date
range, output dir.

**Flow**:
1. Catalog search → list of S1 IW GRD scenes intersecting bbox in window
2. Filter: skip scenes whose `scene_id` is already on disk under
   `data/sentinel_s1_grd/<aoi>/<date>/<scene_id>/`
3. Download GRD product (~4-8 GB per scene) via the OData `/$value`
   endpoint
4. Calibrate to sigma0 dB locally:
   - Read raw GRD pixel values (DN, dimensionless)
   - Apply per-product calibration LUT (in `<product>/annotation/calibration/*.xml`)
   - Convert to sigma0 linear, then to dB
   - Reproject to EPSG:4326 with rasterio
5. Tile into ~2500 px slices matching existing `sentinel_sar.py` output
   schema → `data/sentinel_s1_grd/<aoi>/<date>/<scene_id>/tile_*.tif` +
   `_scene.json`
6. `sar_detect.py` + `sar_aggregate.py` consume these tiles unchanged
   (they don't care which source produced the tiles)

**Dependencies**: `rasterio` (already in `requirements.txt`), optionally
`pyroSAR` for the calibration LUT path (or implement the LUT parse
ourselves — 100 lines of XML + numpy).

**Scheduler entry**: daily cron (free, so we can be aggressive about
freshness). Trims any scenes older than the `sar_aggregate.py` lookback
to keep disk usage bounded — or alternatively, archive them to MinIO.

**A/B with Sentinel Hub**: same AOIs, separate data dir
(`data/sentinel_s1_grd/` vs `data/sentinel_sar/`). The dashboard reads
both via fsspec; the `clusters.parquet` from each path lives separately
so we can compare detection rates before switching over.

## Step 2 — Sentinel-2 optical pipeline

New `pipelines/sentinel_s2.py` + new `pipelines/s2_detect.py`.

**Catalog**: same OData interface, `Collection eq 'SENTINEL-2'`,
`contains(Name, 'MSIL2A')` (Level-2A = atmospherically corrected, much
easier to work with than L1C), `cloud_cover < 30%`.

**Vessel detection (different physics from SAR's CFAR)**:
- L2A products give per-band reflectance at 10 m / 20 m / 60 m
  resolutions depending on the band
- Use Bands B2 (blue), B3 (green), B8 (NIR) at 10 m/px
- NDWI water mask: `(B3 - B8) / (B3 + B8)` > 0.3 ⇒ water. Filters out
  land + clouds (clouds have low NDWI).
- Vessel detection: bright pixels over water. Simple intensity threshold
  on B8 NIR or B3 green works for tankers (steel hulls reflect
  brightly vs dark water).
- Output: same schema as `sar_detect.py`'s `aggregated_detections.parquet`
  (lat, lon, datetime, scene_id, brightness, area_px, on_land=False).

**What we get vs SAR**:
- Higher visual resolution (10 m vs 120 m) → can distinguish VLCC
  (~330 m = 33 px) from Suezmax (~270 m = 27 px) by visible length
- Loaded-vs-ballast cue via waterline height (laden tankers ride lower)
- Visible bunkering / STS oil sheens

**What we lose vs SAR**:
- Clouds: 30-60% of overpasses are unusable depending on AOI
- Optical only works in daylight (SAR is day/night)

**Output dir**: `data/sentinel_s2/<aoi>/<date>/<scene_id>/`. Detections
flow into a separate `s2_clusters.parquet` per AOI initially. We'll
decide whether to merge with SAR clusters in Step 4.

## Step 3 — Backfill 3-6 months

Once Steps 1 + 2 work against fresh data and pass an A/B vs Sentinel Hub:

1. Run `sentinel_s1_grd.py --from 2025-11-01 --to 2026-05-15` against
   each AOI. Wall-clock: bandwidth-bound (~4-8 GB/scene × ~20 scenes/AOI/
   month × 6 months × 5 AOIs ≈ 2-5 TB total). Probably overnight per AOI.
2. Re-run `sar_detect.py` + `sar_aggregate.py` over the backfilled scenes.
3. The Phase 3b history accumulator (`/api/sar_floating_storage`) reads
   each new `clusters.parquet` and snapshots into
   `sar_floating_storage_history` keyed on the date portion of
   `last_seen`. So the history table populates retroactively from
   the backfill output.
4. Optionally archive raw GRD products to MinIO after processing to
   reclaim local disk.

After Step 3, the floating-storage trend chart shows ~6 months of real
history at each terminal instead of empty / one-dot.

## Step 4 — Cross-source enrichment (optional, post-3)

If S2 detection works well enough:

- For each persistent SAR cluster at a key terminal, look up the most
  recent cloud-free S2 scene of that bbox
- Run optical vessel-class measurement (longest connected bright region)
- Store on the cluster row: `optical_class`, `optical_confidence`,
  `optical_observed_at`
- Dashboard: terminal cards optionally show a class-distribution chip
  ("Singapore EOPL: 18 anchored — 6 VLCC, 8 Suezmax, 4 Aframax")

Pure enhancement layer; nothing depends on it.

## Risks + open questions

- **Bandwidth**: 4-8 GB per S1 scene. Backfill is ~2-5 TB; even fresh-
  daily cron is ~100-200 GB/day. Need to confirm server's link can sustain
  this, and that we don't choke MinIO when archiving.
- **Calibration accuracy**: implementing sigma0 calibration from the
  GRD's annotation XML correctly is fiddly. If pyroSAR adds <100 MB to
  the image, prefer using it. If it pulls in a Java JVM (yes, it does
  for SNAP-based ops), we may write the LUT parser ourselves.
- **Detection-rate parity**: will DESP-raw + local CFAR produce the same
  clusters as Sentinel Hub did? Probably yes (same input data), but
  numerical drift in calibration is possible. Step 1's A/B step is
  designed to surface this.
- **S2 cloud-cover variance by AOI**: Persian Gulf is typically clear,
  but Singapore is often clouded. Effective optical coverage will be
  highly uneven across AOIs. Worth measuring in Step 0.
- **Storage growth**: long-term, the `data/sentinel_s1_grd/` and
  `data/sentinel_s2/` dirs grow unboundedly. The tile outputs are
  smallish (~tens of MB per scene after the GRD → sigma0 conversion),
  but raw GRD products before processing need to be deleted or
  archived to MinIO.

## Step 0 results (2026-05-16)

**Auth + catalog: ✅ pass.** `client_credentials` token using the existing
`CDSE_CLIENT_ID` + `CDSE_CLIENT_SECRET` worked for both:
- Sentinel-1 IW GRD search: 5 products returned for Persian Gulf in last
  14 days, sizes 850 MB – 1.7 GB each (GRDH SDV polarisation, both
  `_COG.SAFE` and standard `.SAFE` formats present).
- Sentinel-2 L2A search (with `cloud_cover < 30%`): 5 products returned,
  sizes 860 MB – 1.2 GB each. Multiple tiles covering the AOI from the
  same orbit pass.

**Download: ❌ blocked on token audience.** HTTP 401 from
`download.dataspace.copernicus.eu` with body:
```json
{"code":"DAT-ZIP-609","message":"Token audience not allowed"}
```

Root cause: the client_credentials token issued via the user's existing
OAuth client (created through Sentinel Hub's dashboard) lacks the audience
claim required by the download endpoint. CDSE documents this pattern:
catalog endpoints accept any valid Bearer token; download endpoints check
the token's `aud` claim and reject tokens not minted for the download
audience.

**Inspection of the existing token's claims** (`scripts/test_desp.py`
scope probe, also tried with various scopes):

```
azp=sh-a893db22-daa4-4e14-8ac9-966ba13e2ddb
aud=None
scope=email profile user-context
```

The `sh-` prefix on `azp` confirms the client was registered through
Sentinel Hub's dashboard, which mints tokens with no `aud` claim. The
download endpoint specifically requires a download-audience token. Scope
tweaks won't fix this — the client itself is the wrong shape.

**Initial speculation that "register a CDSE-native OAuth client" would
fix this turned out to be wrong.** CDSE doesn't expose self-service
OAuth-client registration outside Sentinel Hub's dashboard (which only
creates the same `sh-` clients). The CDSE account portal at
identity.dataspace.copernicus.eu/auth/realms/CDSE/account/ only manages
*consents* for existing apps; there's no "Create client" UI.

**The actually-documented path for programmatic downloads is Resource
Owner Password Credentials grant via the public `cdse-public` client.**
See https://documentation.dataspace.copernicus.eu/APIs/Token.html.

To minimize the long-lived-password concern, the script supports three
auth modes in priority order:

1. **Refresh-token grant** (`CDSE_REFRESH_TOKEN` in .env) — preferred for
   long-running deployments. Refresh tokens are easier to revoke than a
   password and don't need re-issue on a schedule (CDSE refresh tokens
   are long-lived). Get one via path 2 below, then store it.
2. **Password grant** (`CDSE_USERNAME` + `CDSE_PASSWORD` in .env) —
   one-time use to bootstrap a refresh token, then drop the password and
   only keep the refresh token. The script prints the new refresh_token
   on every password-grant call so capturing it is trivial.
3. **Client_credentials** (existing `sh-*` client) — fallback for
   catalog-only operations. Won't work for downloads.

**Decision required before Step 1**: do a one-time password-grant auth
to mint a refresh token, then operate on the refresh token going forward.
Username + initial password get used once; the refresh token rotates
out of the way of the password.

### Step 0 follow-up (2026-05-19): downloads PASS, refresh-token lifetime is short

Re-ran `scripts/test_desp.py` on swagner-server after adding
`CDSE_USERNAME` + `CDSE_PASSWORD` to its `.env`:

- Auth: password grant via `cdse-public` ✅
- Catalog: both S1 and S2 ✅ (same as before)
- **Downloads: HTTP 200, 1 MB binary, `Content-Type: application/zip`,
  magic bytes `504b0304` (PK header) ✅** — confirmed for both S1 GRD
  and S2 L2A products.

DESP raw-imagery capability is fully unblocked.

**Wrinkle**: the emitted refresh token has `exp = iat + 3600` — a
**1-hour lifetime**, much shorter than the typical multi-day Keycloak
default. That's too short to be useful as a long-lived secret in `.env`
for a weekly cron — by the time the next pipeline run fires, the
refresh token is long expired.

Practical options:

- **Keep `CDSE_USERNAME` + `CDSE_PASSWORD` in `.env`**, password-grant on
  every pipeline invocation. Simple, reliable. The password is .env's
  long-lived secret; ephemeral access + refresh tokens never persist.
- **Probe `scope=offline_access`** (`scripts/probe_offline_scope.py`) to
  see if CDSE issues offline-type refresh tokens with much longer
  lifetimes under that scope. If yes, drop the password and store only
  the long-lived refresh token. If no, keep the password.

Either way: Step 0 is done. Step 1 (S1 GRD ingest pipeline) is now
unblocked.

**Observed scene sizes inform Step 3 budget**:
- S1 IW GRD: ~1 GB/scene (range: 850 MB – 1.7 GB)
- S2 L2A: ~1 GB/scene (range: 860 MB – 1.2 GB)
- For 5 AOIs × 20 scenes/AOI/month × 6 months backfill ≈ 600 scenes ×
  ~1 GB = ~600 GB raw per source. Roughly half what the doc's earlier
  "2-5 TB" handwave suggested. Still significant but well within a single
  bulk-storage drive.
