# WTI & Tanker Rate Forecast Engine
## Technical Design Document

**Version 0.1 — April 13, 2026**

---

## 1. Purpose & Scope

This document describes the design of a tool to produce daily directional forecasts for two quantities: the spot price of West Texas Intermediate (WTI) crude oil, and the charter rates for crude oil tankers across the three major trade routes. The tool is intended to support a trading decision process with a 1-to-3-month horizon during the 2026 Hormuz crisis and its aftermath.

### 1.1 What the tool is

The tool is a daily batch process that ingests public-domain commodity and maritime data, updates a physical model of global crude oil flows and tanker capacity, layers a statistical residual correction on top of the physical model's outputs, and produces two directional forecasts with confidence bands and explanations. The results are delivered as a morning briefing artifact the operator can consume in under five minutes.

### 1.2 What the tool is not

- It is not a real-time system. The target update frequency is once per day.
- It is not a trade execution system. It produces forecasts, not orders.
- It is not position-aware. It does not track the user's portfolio, P&L, or Greeks.
- It is not a replacement for commercial data services. It consumes free and public data only in the initial version.
- It is not a point-estimate price predictor. It produces directional calls and confidence ranges, not precise dollar targets.

### 1.3 Guiding principles

- **Physics first, statistics second.** The tool models what can be computed from first principles before applying statistical corrections for what it cannot observe.
- **Honesty over precision.** Directional calls with confidence are more useful than point estimates with false precision.
- **Modular design.** Data sources, models, and outputs are independent modules with clear interfaces. The tool must be extensible without rewrites.
- **Local first, cloud ready.** The initial deployment is a single-machine process but nothing in the design should prevent cloud deployment later.
- **Fail visibly.** When data is missing or stale, the tool degrades gracefully and tells the operator what went wrong rather than silently producing low-quality forecasts.

---

## 2. Outputs

The tool produces two primary forecast signals, each accompanied by supporting diagnostics. Both signals are expressed as directional calls with confidence bands rather than point estimates.

### 2.1 WTI crude oil price signal

A directional forecast of WTI spot price movement at two horizons (1 week and 4 weeks), comprising:

- **Direction bucket:** STRONG UP, UP, FLAT, DOWN, STRONG DOWN
- **Confidence:** a probability estimate reflecting the model's internal agreement
- **Expected range:** a low-high band anchored to the current spot price
- **Primary driver attribution:** which physical inputs contributed most to the call
- **Risk flags:** identifiable headline events that could invalidate the call

### 2.2 Tanker rate signal

A directional forecast of time charter equivalent (TCE) rates at the same two horizons, computed independently for each of the three major routes:

- **TD3C:** Middle East Gulf to China, VLCC (270,000 mt benchmark)
- **TD15:** West Africa to China, VLCC (260,000 mt benchmark)
- **TD22:** US Gulf Coast to China, VLCC (270,000 mt benchmark)

Each route produces the same structure as the WTI signal: direction bucket, confidence, expected range, driver attribution, and risk flags.

---

## 3. Modeling Approach

The tool uses a physics-first, statistics-second architecture. A deterministic physical model produces a baseline forecast from observable flows. A statistical residual model then corrects the physical baseline using historical patterns of where the physics was wrong.

### 3.1 Why this architecture

Commodity markets have a physical floor: barrels must be produced, transported, stored, and consumed. A good physical model captures that structure and produces forecasts that are intellectually defensible. However, physical models cannot observe everything — sentiment, positioning, dark fleet activity, unobservable bilateral contracts, and discretionary government action all matter. A pure physical model systematically misses these.

A pure statistical model has the opposite failure mode: it captures historical correlations without understanding structure, which makes it brittle during regime changes (exactly when forecasts matter most). The 2026 Hormuz crisis is a regime change, so a purely statistical model trained on 2019-2025 data would be dangerously confident about the wrong things.

The residual correction approach combines the strengths of both. The physical model handles the structural logic (supply-demand balance, tanker capacity, routing). The statistical layer learns from historical residuals what the physical model tends to miss and applies a correction, but only to the extent that historical patterns still apply.

### 3.2 Physical model: WTI price

The WTI price model is built on a weekly US crude balance equation:

> **Weekly US Stock Change = Production + Imports + SPR Release − Refinery Runs − Exports**

When this quantity is persistently negative over multiple weeks, stocks draw and price tends to rise. When it is positive, stocks build and price tends to fall. The magnitude and duration of the imbalance is the primary driver of directional signal.

The physical model produces outputs in the following chain:

- **Observed inputs:** EIA weekly petroleum data, daily futures prices, SPR level
- **Intermediate state:** current inventory trajectory, refining capacity utilization, export demand proxy, Brent-WTI spread
- **Physical output:** directional call based on recent stock trajectory and term structure

The physical WTI model does not attempt to predict an exact price. It predicts whether the physical conditions support a rising, falling, or flat market over the forecast horizon.

### 3.3 Physical model: tanker rates

The tanker rate model is built on a capacity utilization ratio:

> **Tightness = Demanded ton-miles per day / Effective fleet capacity**

Where:

- **Demanded ton-miles per day** is the sum across all active routes of (cargo volume in tons × voyage distance in miles), integrated over a forward window
- **Effective fleet capacity** is (fleet size × average vessel capacity × average speed × utilization), adjusted down for ships currently in drydock, sanctions-limited, or committed to time charters

Tightness is a known predictor of tanker rates. When it exceeds approximately 0.95, rates spike nonlinearly because small additional demand must bid against a nearly-saturated fleet. When it drops below 0.85, rates decline as owners compete for cargo.

The critical insight for the current environment is that effective capacity is being reduced by routing changes even though the fleet is stable. A VLCC on a USGC-to-China Cape-of-Good-Hope route takes roughly twice as long per voyage as the same ship on a Middle East Gulf-to-China Hormuz route. This effectively cuts capacity in half for each ship that reroutes, without requiring any ship removal from the market.

The physical tanker model computes these quantities for each route separately, then aggregates to produce a directional forecast per route.

### 3.4 Statistical residual model

The residual model learns, on historical data, the systematic difference between physical model output and realized market outcomes. It is trained on the time series of residuals with a feature set that includes:

- **Macro factors:** dollar index, treasury yields, equity indices, VIX
- **Market positioning:** CFTC commitments of traders data, put/call ratios
- **Term structure signals:** backwardation depth, calendar spreads
- **News sentiment proxy:** frequency of specific keywords in headline feeds (if available)
- **Regime indicators:** crisis flags, OPEC meeting proximity, seasonality

The residual model is deliberately kept simple. A small model with a handful of features is more robust than a complex one when the available training data spans at most a few years. The goal is to catch systematic misses, not to replace the physical model.

### 3.5 Confidence estimation

Confidence is computed from three sources of uncertainty:

- **Physical model uncertainty:** how close is the current state to the threshold between direction buckets? A marginal signal should have lower confidence than a clearly-directional one.
- **Residual model uncertainty:** how large is the historical forecast error at comparable states? A state well-represented in history has lower uncertainty.
- **Data quality:** how fresh and complete are the inputs? A forecast built from stale data gets penalized.

These combine into a single confidence number, but the briefing also shows the components so the operator can see where uncertainty is coming from.

### 3.6 Multi-model ensemble

Where multiple modeling approaches would produce useful information, the tool runs them in parallel and reports all of them. This applies specifically to the statistical residual layer: a linear regression, a gradient-boosted tree model, and a simple nearest-neighbor historical analog matcher can all be run on the same features and their forecasts compared.

Ensemble disagreement is informative. If three models agree, confidence is high. If they disagree, the operator should know that and act accordingly. This is more honest than a single model producing a single number with manufactured confidence.

> **Key tradeoff: model complexity vs data availability**
>
> Sophisticated models need lots of training data. The operator has at most 5-6 years of daily data for most inputs, and the current market regime is unique in that history.
>
> The design chooses simple, interpretable models over complex ones, and uses ensembles to express uncertainty rather than stacking models to reduce it. Nothing in the model is more complex than can be sanity-checked by reading the code.

---

## 4. Data Sources

Data sources are divided into three tiers based on their role in the system: historical training data (needed for model calibration), current-state inputs (needed for daily inference), and validation sources (used for backtesting and quality checks).

### 4.1 Historical training data

These sources must provide clean, consistent history back as far as possible. The target is 2019 to present, which covers pre-crisis normal markets, COVID, the Russia-Ukraine shock, and the current Hormuz crisis.

| Source | Content | Granularity | Access |
|--------|---------|-------------|--------|
| EIA Weekly Petroleum Status | US crude production, imports, exports, refinery runs, inventories by region | Weekly, back to 1982 | Free API |
| EIA SPR | Strategic Petroleum Reserve level and releases | Weekly | Free API |
| CME / ICE futures | WTI, Brent, gasoline, diesel, heating oil front-month and term structure | Daily, back decades | Public price feeds |
| Baltic Exchange indices | BDTI, BCTI, and route-specific TCE rates (TD3C, TD15, TD22, etc.) | Daily, back decades | Public weekly reports |
| US Dollar Index (DXY) | Macro factor for correlation analysis | Daily | Public |
| Treasury yields | Curve points used as macro context | Daily | Public (FRED) |
| CFTC Commitments of Traders | Positioning in WTI, Brent, heating oil, gasoline | Weekly | Free |

### 4.2 Current-state inputs

These sources drive the daily forecast but do not require deep history. Fresh data is essential; historical coverage is a bonus.

| Source | Content | Refresh | Notes |
|--------|---------|---------|-------|
| SpotMarketCap | Live VLCC rates, fixtures, chokepoint counts, war tracker | Daily | Best current-state aggregator, free |
| aisstream.io | Live global AIS feed (PositionReport + ShipStaticData) over WebSocket | Streaming | Free with API key. Real-time only, no archive — see §4.2.1 |
| Kpler free blog | Analytical commentary on flows, rerouting, sanctions | 1-2 weeks lag | Free, human-readable |
| Vortexa free blog | Same category as Kpler, different angle | 1-2 weeks lag | Free, human-readable |
| BTS / WIMS | US port tanker counts and berth status | Daily | Free government source |
| Reuters / Argus headlines | Breaking news on OPEC, sanctions, conflict | Real-time | Free headlines only |
| Copernicus DSE / Sentinel Hub | Sentinel-1 SAR imagery for ship detection in fixed AOIs (chokepoints, terminals) | Per-pass (~6-12 day revisit) | Free with registration; 30k PU/month on the Process API — see §4.2.2 |
| Umbra Open Data (AWS) | High-resolution X-band SPOTLIGHT SAR (16 cm–1 m) over opportunistic AOIs | Whenever Umbra customers task a relevant location | Free, CC-BY-4.0, unmetered S3 access; coverage of our AOIs is sparse — see §4.2.4 |

### 4.2.1 AIS ingestion strategy (aisstream.io)

The AIS feed is the single most consequential current-state input for the tanker model: §3.3's tightness ratio depends on observing both effective fleet capacity (denominator) and demanded ton-miles per day (numerator), and neither can be computed from chokepoint counts alone. Both quantities require a periodic snapshot of the **full crude-tanker fleet**, not just vessels in named regions.

aisstream.io imposes two constraints that shape the ingestion design:

- **Real-time only.** There is no historical query endpoint. Anything the model needs for backtesting must either be recorded going forward (and history grows organically) or sourced from a different provider. See §11 for the resulting open question.
- **No server-side ship-type filter.** A subscription requires a `BoundingBoxes` field (use a single global bbox for fleet-wide visibility) and optionally accepts an MMSI list. Ship-type filtering happens client-side by joining `PositionReport` against `ShipStaticData` messages.

Given those constraints, AIS ingestion is built in three phases. The first phase is a one-shot characterization run; only its results justify proceeding to the second.

**Phase 0 — Baseline census (one-shot).** Subscribe globally for a representative window (24–72 hours) and record everything. The output is a summary characterizing what aisstream actually delivers: the count of distinct crude-tanker MMSIs (ship type 80–89 per ITU-R M.1371) versus the published global fleet (~3,200 in the operator's prior, ~2,300–2,400 in 2024-2025 active VLCC/Suezmax/Aframax counts depending on subcategory definitions); the message-rate and bandwidth profile (for sizing Phase 1); and the per-MMSI position-report cadence (which bounds the snapshot frequency Phase 1 can deliver). If observed coverage is materially below published fleet counts, the right next step is to evaluate a different provider rather than build features on a source the model cannot rely on.

**Phase 1 — Ongoing collection (cron-driven, two collectors).** Once Phase 0 validates coverage, ingestion splits along the natural cadence boundary in the data:

- A weekly *static* collector subscribes to `ShipStaticData` only, maintaining a canonical MMSI universe of crude tankers. This is a slow-moving reference dataset (vessel name, IMO, dimensions, max draught) used both to scope the position collector and to estimate per-vessel deadweight capacity.
- A frequent *position* collector (initial cadence: every 1–4 hours, sized after Phase 0) subscribes with `FiltersShipMMSI` set to the canonical tanker list, captures positions during a short window, and writes day-partitioned snapshots. Filtering by MMSI keeps message volume tractable and reduces the noise of non-tanker traffic.

This split fits the broader system's batch-pipeline model (TDD §6) without standing up always-on infrastructure that nothing else in the project requires. A long-running daemon collector is a future option if Phase 0 shows that snapshot windows materially undersample the fleet — the parquet schema does not change in either case.

**Phase 2 — Historical depth (open question).** Backtesting the tanker model (§7.1) needs AIS history that aisstream cannot supply. The decision is captured under §11.

#### 4.2.1.1 Phase 0 outcome and follow-ups (May 2026)

The April 16, 2026 Phase 0 run captured 3,635 distinct tanker MMSIs over 24 hours — passing the vessel-count threshold against the published fleet. The geographic distribution of those positions, however, revealed a structural coverage gap: 46% of the observed crude (type 80) fleet sat in NW Europe, with **zero** crude tankers seen in the Persian Gulf or Red Sea and effectively none in West Africa, the Russian/Chinese coasts, or the broader Indian Ocean. aisstream's terrestrial AIS receiver network is concentrated where European, US, and select Asian volunteers operate; high-value MEG and West African loading regions are blind.

Two follow-ups:

- **Coverage threshold expanded.** Future Phase 0 evaluations require both vessel-count and per-region density checks against published fleet distribution. The existing AIS pipeline is treated as best-effort coverage where it works, not a global view.
- **Position staleness filter (deferred).** A snapshot that includes every tanker's last-known position regardless of age conflates live activity with months-old dots. Downstream consumers should filter to a freshness window (initial recommendation: 7 days), and tankers without a fresh position should be excluded from counts and maps. Not yet implemented; tracked as a follow-up.

The geographic gap shifts priority to §4.2.2 (SAR) and §4.2.3 (port-call data) as the first-order solutions for the regions terrestrial AIS misses, rather than additional work on the AIS pipeline itself.

### 4.2.2 Sentinel-1 SAR ingestion strategy (Copernicus)

The AIS feed in §4.2.1 observes only ships that broadcast. A meaningful share of the dark fleet — sanctioned Russian, Iranian, and Venezuelan tonnage — disables AIS near origin or destination terminals to obscure the leg. Detecting these vessels requires an independent, all-weather sensor. Sentinel-1 synthetic aperture radar (SAR) provides one in the free tier and is the planned source for the §9.2 unobserved-flows term.

Sentinel-1 IW GRD (Interferometric Wide swath, Ground Range Detected) imagery is published via the Copernicus Data Space Ecosystem (CDSE) at https://dataspace.copernicus.eu. Free registration grants 30,000 processing units per month on the Sentinel Hub Process API — enough for periodic acquisitions over a small set of high-value AOIs:

- Strait of Hormuz (TD3C origin)
- Strait of Malacca and East China Sea approaches (TD3C/TD15/TD22 destinations)
- US Gulf Coast export terminals (TD22 origin)
- Brazil and West Africa loading terminals (TD15 origin)

SAR is preferred over Sentinel-2 MSI optical for this use because dark-fleet activity is unaffected by daytime or cloud-free constraints; SAR penetrates cloud and works at night. Both Sentinel-1A and Sentinel-1C are operational as of May 2026 (S1B failed in 2022; S1C launched late 2025 and exited commissioning by early 2026). With two satellites, equatorial revisit is approximately 3 days, giving ~10 acquisitions per AOI per 12-day window from a typical AOI bbox that intersects 3–4 relative orbits. The cadence is adequate for the 1-to-3-month forecast horizon and supports sub-weekly detection at chokepoints.

Ingestion proceeds in phases analogous to AIS. The first phase is a one-shot characterization run; only its results justify proceeding to the second.

**Phase 0 — Coverage characterization (one-shot).** Acquire one revisit cycle over Hormuz and one US Gulf AOI. The output is a budget table (PUs consumed per AOI per pass), a detection precision/recall estimate using contemporaneous AIS as ground truth, and a measurement of range/azimuth ambiguity false-positive rate near coastline. If the free tier cannot support the desired AOI footprint at the chosen cadence, the right next step is to evaluate paid CDSE tiers or descope AOIs rather than build features on incomplete coverage.

Concrete steps for the one-shot run: (1) register for a CDSE account and configure an OAuth client for the Sentinel Hub Process API; (2) define AOIs in WGS84 — Hormuz strait at roughly 26.0–26.7°N / 56.0–57.0°E and the USGC ship channels at roughly 28.5–30.0°N / 95.5–93.0°W; (3) pull one revisit cycle (~12 days) of Sentinel-1 IW GRD VV-polarization scenes per AOI (VV is the best polarization for distinguishing vessel returns from sea clutter); (4) run sliding-window CFAR detection on each scene; (5) cross-reference detections against contemporaneous AIS positions within ±15 min / ±2 km tolerance to estimate precision/recall; (6) sum PU consumption per AOI per pass and extrapolate to a monthly budget against the 30k free-tier ceiling.

#### 4.2.2.1 PU budget estimate

Sentinel Hub bills work in processing units (PU). The free tier ceiling is 30,000 PU/month. The PU formula approximates `(output_pixels / 512²) × bands × dataType_factor × orthorectification_factor`. For a Sentinel-1 IW GRD VV-only request at 10m resolution over an 80 km × 80 km AOI, this comes out to roughly **300–700 PU per scene**; the spread reflects choice of output format (UInt16 vs Float32), exact bbox, and any enabled antialiasing or ortho factor. With both S1A and S1C operational, the equator revisit is ~3 days, and a typical AOI intersects 3–4 distinct relative orbits — yielding **~20 acquisitions per AOI per month** (verified empirically against Hormuz: 12 scenes in 12 days).

Phase 0 (one revisit cycle, ~2 passes per AOI):

| AOI | Scenes | Est PU |
|-----|--------|--------|
| Hormuz | 2 | ~1,000 |
| USGC (Houston/Galveston) | 2 | ~1,000 |
| Malacca / Singapore | 2 | ~1,000 |
| **Subtotal** | **6** | **~3,000** |

Phase 1 ongoing (~20 scenes/AOI/month with both S1A + S1C):

| AOI count | Scenes/mo | Est PU/mo | % of free tier |
|-----------|-----------|-----------|----------------|
| 3 (Hormuz, USGC, Malacca) | 60 | ~19,500 | 65% |
| 5 (+ Brazil, W Africa) | 100 | ~32,500 | **108% — over** |
| 7 (+ E China Sea, Mediterranean) | 140 | ~45,500 | **152% — over** |

The estimate has ~2× uncertainty until validated by real Phase 0 calls. If empirical PU/scene comes in at 1,000 instead of 500, the 5-AOI Phase 1 footprint becomes ~25,000 PU/mo — still inside the 30k ceiling but with no re-run headroom.

**Empirical anchor (May 7, 2026 smoke test).** A 512×512 FLOAT32 sigma0 request over a 16 km × 16 km tile near Hormuz (≈ 32 m/px effective) consumed **1.33 PU**, with `processing.orthorectify=true` and `backCoeff=SIGMA0_ELLIPSOID`. PU scales near-linearly with output pixel count, so a full 80 km × 80 km Hormuz AOI at 10 m/px (8000 × 8000 px = 244 such tiles) extrapolates to ~325 PU per scene — at the low end of the 300–700 prior estimate.

**Process API hard limit:** width and height are each capped at 2500 px. Going above triggers a `COMMON_BAD_PAYLOAD` 400 (and crucially, no PU billed). Tiling is required if a single AOI demands more than 2500 px in either dimension; otherwise the effective resolution is `bbox_extent / 2500`.

**Phase 0 ingest result (May 7, 2026, Hormuz).** The full 12-day Hormuz Phase 0 ran through `sentinel_sar.py ingest` over a 1° × 0.7° bbox at 2500×1750 px (≈ 40 m/px). Twelve scenes returned by the catalog (six S1A, six S1C, three relative orbits ascending + two descending). All twelve fetched successfully at exactly **22.25 PU/scene = 267 PU total**. Every scene was identical PU regardless of input scene size, confirming PU is a function of output pixels only. Scene file sizes ranged from 27 KB (footprint barely intersects bbox) to 11 MB (full coverage), suggesting an obvious Phase 1 optimization: pre-filter catalog features by bbox/footprint intersection area before issuing Process API calls, to avoid paying PU for near-empty rasters. Image content sanity-checked: median sea backscatter −19.5 dB, mean −19.3 dB, with bright pixel tail extending to +22.7 dB (ships + land structures).

**Phase 0 detection result (May 7, 2026, Hormuz).** A scipy-based sliding-window CFAR detector (`sar_detect.py`, training window 31 px ≈ 1.2 km, threshold k = 6σ, area filter 2–80 px) ran on all twelve scenes and produced **149 candidate detections — 144 over water, 5 over land** (the 5 are mostly coastal port-area returns flagged by the 1 km global land mask's coastal ambiguity). Per-scene yield correlates strongly with footprint coverage: scenes with ≥60% valid pixels returned 12–42 detections each, marginal-coverage scenes returned 0–7. Detection density and geographic spread match published Hormuz vessel traffic counts (~50–100 vessels in the full strait at any moment).

**Phase 0 expansion (May 7, 2026, Persian Gulf + Gulf of Oman).** The narrow 1° × 0.7° Hormuz AOI was an undercount; expanding to 6° × 4° (54–60°E, 24–28°N) at 88 m/px effective resolution covers the strait, both Gulf approaches, the Fujairah anchorage, and major UAE/Iranian export terminals. The tile loop in the ingest pipeline splits a >2500-px request into a 2×2 grid of Process API calls, with a polygon-bbox pre-filter that skips tiles a given Sentinel-1 acquisition's footprint doesn't intersect. Empirical: 51 scenes catalogued, 91 of 204 candidate tiles fetched (113 skipped), **1,928 PU total** (6.4% of monthly tier).

Across those 51 scenes the CFAR detector produced **2,744 raw detections — 2,484 over water**, 260 on land. By 0.5° geographic bin the over-water hotspots are: Abu Dhabi terminals at 25.5°N / 55.0°E (410 detections), Fujairah anchorage at 25.5°N / 56.5°E (179), Strait of Hormuz proper at 26.0°N / 56.0°E (172), and Gulf of Oman entrance at 25.0°N / 56.5°E (159). These match known crude tanker infrastructure exactly.

**Cross-acquisition aggregation (`sar_aggregate.py`).** Raw per-acquisition detections double-count the same vessel that's anchored across multiple passes, and include fixed structures (oil platforms, mooring towers, FSOs) that produce persistent bright returns identical to anchored ships. The aggregator clusters detections within a 300 m radius using a KDTree pair-query + connected-components on the resulting sparse adjacency graph. Each unique cluster is then labeled persistent (appears in ≥3 distinct scenes) or transient (1–2 scenes).

Empirical (12-day window over the expanded AOI):
- 1,925 unique clustered objects (down from 2,744 raw detections)
- 208 persistent — 202 over water (oil platforms, FSOs, mooring towers, long-anchored vessels at sites like Das Island, Zakum field, Larak Island), 6 on land
- 1,717 transient — 1,509 over water (vessels caught in 1–2 passes, almost all in transit), 208 on land

The transient-over-water count of 1,509 unique vessels over 12 days plausibly maps to 800–1,000 vessels present at any single moment, consistent with public estimates of Persian Gulf + Gulf of Oman traffic. Per-acquisition snapshots peak at ~330 over-water detections (a single best-coverage pass).

For downstream consumers: persistent over-water clusters are the candidate fixed-infrastructure mask referenced in §11; transient over-water clusters are the population the §3.3 tanker model needs to count.

**Phase 0 second AOI — USGC (May 7, 2026).** Same pipeline rerun over the US Gulf Coast (-98°W to -88°W, 26°N to 31°N, 10° × 5° at 88 m/px → 4 × 2 tile grid): 31 scenes, 67 of 248 candidate tiles fetched (the rest geometry-skipped), **1,363 PU**. CFAR + aggregator: 1,679 raw detections → 1,592 unique clusters → **830 transient over water + 761 on land + 1 persistent**. Two notes:

- The on-land count is much higher than Persian Gulf (47% vs 12%). USGC has complex coastal geometry (Houston Ship Channel, Galveston Bay, Mississippi delta) that the 1 km global land mask aggressively flags — many of those "land" detections are real port-anchored ships. A finer coastline mask would recover most of them.
- The persistent count (1 vs Persian Gulf's 208) reflects two things: USGC has fewer dense offshore-platform clusters than the Persian Gulf, and the 12-day window's spatial coverage at any given lat/lon is shallower than Persian Gulf's, so the ≥3-scene persistence threshold is harder to meet. Real platforms (Mars/Ursa fields, etc.) likely appear in 1–2 scenes here.

Geographic distribution matches known USGC crude tanker activity precisely — the densest 0.25° bins are Galveston/Houston VLCC lightering anchorage (29.25°N, 94.5°W), LOOP (29.0°N, 89.75°W — the only US VLCC-class crude port), and the Sabine-Neches waterway (29.5°N, 93.75°W). The pipeline scales to a second AOI without code changes; cumulative Phase 0 spend across both AOIs is **3,291 PU (~11% of monthly tier).**

Two findings worth carrying forward:

1. **Static-infrastructure persistence.** The same coordinates near 26.24–26.27°N / 56.27°E showed as the brightest detection across four separate acquisitions — almost certainly fixed structures (platforms, buoys), not ships. Single-scene CFAR cannot distinguish stationary metallic structures from ships. The fix is multi-temporal background subtraction or a curated static-infrastructure exclusion mask; flagged as an open question (§11).
2. **AIS cross-reference deferred.** The existing AIS snapshot is from April 16, 2026 — three weeks stale relative to the May 5/6 SAR scenes. Time-aligned cross-referencing requires running the AIS Phase 1 collector concurrently with SAR ingestion. For Phase 0 the detector is validated by detection counts and geographic plausibility alone; precision/recall against AIS ground truth is a Phase 1 deliverable.

Revised footprint:

| AOI count | Scenes/mo | Empirical PU/mo | % of free tier |
|-----------|-----------|-----------------|----------------|
| 3 (Hormuz, USGC, Malacca) | 60 | ~19,500 | 65% |
| 5 (+ Brazil, W Africa) | 100 | ~32,500 | **108% — over** |
| 7 (+ E China Sea, Mediterranean) | 140 | ~45,500 | **152% — over** |

With both S1A and S1C operational the free tier no longer comfortably covers a 5-AOI Phase 1. Choices: (a) scope to 3 AOIs and stay under 65%, (b) downsample resolution from 10 m to 20 m per scene (~80 PU instead of ~325, restoring 5-AOI fit at ~8,000 PU/mo), or (c) deduplicate adjacent S1 frames within the same pass (Phase 0 catalog showed paired 25-second products covering the same orbit — fetching only the one whose footprint best contains the AOI center halves PU/pass). The right choice is empirical: Phase 0 should test ship detection at 20 m vs 10 m and report whether the lower resolution still resolves VLCC/Suezmax vessels reliably. If yes, option (b) is the cleanest and preserves AOI breadth.

**Over-quota behavior.** Under Planet (which acquired Sentinel Hub), the documented behavior on quota exhaustion is hard enforcement: order endpoints return HTTP 403, tile-streaming requests return watermarked output, and the subscription is suspended until either top-up purchase or plan upgrade. No documented pay-per-PU model exists.

**Paid-tier pricing.** Not transparently published on the Planet pricing page as of May 2026 — that page redirects through marketing landing without a public PU/$ table. Historical Sentinel Hub self-service tiers (pre-acquisition) were Exploration at ~€30/mo for ~100k PU and Basic at ~€300/mo for ~1M PU, but these may have changed and require contacting sales to confirm. For v0 planning, treat 30k PU/month as a hard ceiling and prioritize AOIs accordingly rather than assume any specific upgrade pricing.

**Phase 1 — Ongoing acquisition (cron-driven).** Periodic Sentinel Hub Process API calls extract calibrated SAR backscatter over each AOI. Ship detection runs client-side. The detection method is captured under §11; both CFAR (sliding-window constant false alarm rate) and pre-trained SAR ship-detection models are viable. Output schema: detection points (lat, lon, estimated length, acquisition timestamp, AOI id, confidence). One parquet per acquisition, day-partitioned.

**Phase 2 — Dark-fleet cross-reference.** For each Sentinel detection, join against AIS positions within a tolerance window (±15 minutes temporal, ±2 km spatial). Detections without an AIS match become candidate dark-fleet observations. The resulting time series feeds the §3.4 residual model as an "unobserved flows" feature, partially closing the §9.2 gap.

This work is not part of v0. It is scoped here so that v0's data store and AIS schemas anticipate the future join (timestamps in UTC, positions in WGS84 decimal degrees, MMSI-keyed).

### 4.2.3 Port call scraping (deferred — likely not needed)

Earlier design considered scraping port-call data from USACE, Singapore MPA, Rotterdam, and VesselFinder to support voyage classification (§9.7) and to add tanker presence in regions where AIS is blind. After the §4.2.1.1 coverage finding, this work is **deferred indefinitely** for two reasons:

1. **Redundant where AIS works.** In regions with strong terrestrial AIS coverage (US, NW Europe, parts of Asia), port events can be derived directly from AIS tracks: define static terminal-proximity polygons in WGS84 once, then any AIS position within a polygon is an arrival/anchor event for that terminal. This is feature-engineering metadata (Layer 2 in §5.1), not an ingestion pipeline. The terminal taxonomy is bounded — major crude loading and discharge terminals are tens, not hundreds, of polygons.
2. **Unhelpful where AIS is blind.** Persian Gulf, Red Sea, and West African loading countries do not publish open vessel movement data. A scraper of free sources cannot bridge this gap.

Voyage classification therefore follows the polygon-derived approach: position-in-polygon → arrival event; departure when the position leaves the polygon and stays outside for a debounce window (initial: 30 minutes). Polygons are static config, version-controlled.

The exception worth flagging: Singapore MPA publishes open vessel movement data and sits in a corridor with weak AIS coverage. If Phase 1 SAR over Malacca produces unsatisfactory results, returning to Singapore MPA as a complementary source is reasonable. Until that point, port-call ingestion is not on the build queue.

### 4.2.4 Umbra Open Data SAR (opportunistic supplement)

Umbra Lab publishes a CC-BY-4.0 SAR archive on AWS Open Data (`s3://umbra-open-data-catalog/`, STAC-indexed). The imagery is X-band SPOTLIGHT mode at sub-meter resolution (16 cm – 1 m), far higher than Sentinel-1's C-band 5–20 m. Access is unmetered: no API quota, no key required.

The catch is coverage. Umbra collects what their paying customers task — not what an external user requests. A sample of recent items (May 2026) showed the bulk of collections over the US interior (likely calibration sites in Utah and Nevada), random global locations driven by tasking, and a few coastal areas. **Persian Gulf and USGC — the two AOIs we care about most — show approximately zero coverage.** Some opportunistic items exist for Malacca, West Africa, and parts of East Asia.

Use case in this project:

- **Not a primary source.** Cannot replace Sentinel-1 because we cannot request collection. Coverage is what Umbra customers happen to image.
- **Validation, where overlap exists.** An Umbra spotlight scene over an AOI on the same day as a Sentinel-1 IW pass gives sub-meter ground truth for evaluating our CFAR detector on lower-resolution imagery.
- **Opportunistic backfill** for one-off events (e.g., post-incident imagery of a chokepoint) where Umbra happened to image at the right moment.

Implementation cost is low — the STAC catalog is browsable from S3 with no authentication, and the §4.2.2 AOI bbox list can be reused as a spatial filter against new Umbra items on a daily cron. Treat as a low-priority enrichment added after Sentinel-1 Phase 1 stabilizes.

### 4.3 Validation sources

After the physical and statistical models produce forecasts, validation sources are used to score forecast quality over time and to detect when the model is drifting from reality.

- Realized WTI spot prices at each forecast horizon
- Realized Baltic TCE rates at each forecast horizon
- Published forecasts from commercial services (when available in free summaries) for sanity comparison

### 4.4 Data source reliability and fallbacks

Every data source will eventually fail: websites change structure, APIs change terms, government releases delay. The design assumes all sources are unreliable and handles failures explicitly.

Each source has:

- A primary fetcher that pulls fresh data
- A freshness check that confirms the data is current
- A validation check that confirms the data looks reasonable (ranges, types, schemas)
- A fallback strategy: use cached data if fresh data is unavailable, and degrade the forecast confidence accordingly
- An alerting threshold: if a source has been unavailable for more than N days, the operator is told

The residual model is specifically designed to tolerate missing features. If a feature is unavailable on a given day, the model produces a forecast using whatever is available and reports lower confidence.

---

## 5. System Architecture

The system is organized as a directed pipeline of independent modules, each with a defined input and output interface. Modules do not share state directly; they communicate through the data store. This makes the system testable at any boundary and replaceable at any boundary.

### 5.1 Module layers

The pipeline has five layers, each composed of one or more modules:

#### Layer 1: Ingestion

One module per data source. Each ingestion module exposes a standard interface: fetch the latest data, fetch a historical range, validate the result, and write to the data store. Ingestion modules are the only components that know about the outside world; everything else downstream reads from the data store.

#### Layer 2: Feature engineering

Transforms raw ingested data into features used by the models. This includes rolling averages, year-over-year comparisons, derived quantities (effective fleet capacity, ton-miles per day), and normalization. Feature engineering is deterministic and pure: given the same raw data, it produces the same features.

#### Layer 3: Physical models

Two modules: one for WTI, one for tanker rates. Each is a pure function that takes features in, applies the physical balance equations, and produces a directional forecast with physical-model-only confidence. These modules do not require training; they encode structural relationships directly.

#### Layer 4: Statistical correction

The residual models run here. They take the physical forecast and the feature vector, produce a residual correction, and produce a combined forecast. This layer has training mode (fit the model on historical data) and inference mode (produce today's correction). Training is done offline and on demand; inference is done daily.

#### Layer 5: Reporting

Takes the final forecasts, the diagnostic data, and the historical forecast log, and produces the morning briefing artifact. This layer is templated so that the same forecast data can be rendered in different formats (email, PDF, dashboard) without touching the models.

### 5.2 Data store

A single local data store holds raw ingested data, derived features, forecast history, and model artifacts. The initial implementation uses a file-based approach (for simplicity and to avoid server dependencies), but the access layer is abstracted so it can be swapped for a hosted database later without touching model code.

The data store has four logical namespaces:

- **Raw:** as-received data from each ingestion source, with provenance metadata
- **Features:** computed feature vectors, indexed by date
- **Forecasts:** historical forecast outputs with horizon, timestamp, and realized outcome once known
- **Models:** serialized statistical models and their training metadata

### 5.3 Configuration

All parameters that might reasonably change — data source URLs, API keys, model hyperparameters, forecast horizons, confidence thresholds, report templates — live in configuration files, not code. This lets the operator adjust behavior without touching the implementation and makes the tool easy to redeploy in different environments.

### 5.4 Separation of concerns

The architecture enforces strict boundaries:

- Ingestion modules never produce forecasts
- Physical models never fetch data
- Statistical models never contain hardcoded physics
- Reporting modules never compute forecasts

These boundaries exist because they make debugging possible. When a forecast is wrong, the operator can inspect each layer independently: was the raw data wrong, was the feature engineering wrong, was the physical model wrong, was the statistical correction wrong, or was the report wrong?

---

## 6. Daily Process

The daily run is a single end-to-end pipeline invocation. It is designed to be idempotent (running it twice on the same day produces the same result), observable (every step logs its progress), and recoverable (if it fails halfway, it can resume from the last good state).

### 6.1 Sequence

The daily run proceeds through the following steps in order:

| Step | Activity | Typical duration | Failure mode |
|------|----------|------------------|--------------|
| 1 | Ingest from all data sources in parallel | 2-10 minutes | Source unavailable → use cached |
| 2 | Validate ingested data (freshness, schema, ranges) | < 30 seconds | Validation fail → alert, flag feature |
| 3 | Compute feature vectors for the current date | < 30 seconds | Missing feature → partial inference |
| 4 | Run physical WTI model | < 10 seconds | Should not fail |
| 5 | Run physical tanker model for each route | < 30 seconds | Should not fail |
| 6 | Run statistical correction models | < 60 seconds | Model missing → physical only |
| 7 | Combine physical + statistical → final forecast | < 10 seconds | Should not fail |
| 8 | Check for anomalies and flag risks | < 30 seconds | Should not fail |
| 9 | Update forecast history log | < 10 seconds | Should not fail |
| 10 | Render morning briefing artifact | < 60 seconds | Template fail → raw output |

### 6.2 Parallelism and dependencies

Ingestion is the slowest step and is run in parallel across all sources because they are independent. All downstream steps are sequential because each depends on the previous one's output. The total wall time is dominated by ingestion — typically 5-10 minutes — with the actual modeling taking less than 2 minutes combined.

### 6.3 Scheduling

The pipeline is designed to run once per day at a time chosen by the operator. The recommended time is early morning before the operator needs the briefing, and after overnight data updates from Asian and European sources. A typical choice would be 5-6 AM local time.

If the operator wants to rerun with updated data during the day, the pipeline can be invoked manually. Each run is timestamped and stored independently, so multiple runs per day do not overwrite each other.

### 6.4 Failure handling

A failed pipeline run does not silently produce a stale forecast. The operator is notified of any failure, and the briefing artifact is marked with the failure state. Partial runs (some sources succeeded, others failed) produce partial forecasts with degraded confidence and explicit notes about what's missing.

The pipeline tolerates the following failure modes without aborting entirely: individual data source unavailable, individual feature missing, individual statistical model unavailable, or transient API errors recoverable by retry. Unrecoverable failures — data store corruption, critical physical model failure, missing configuration — abort the run and alert the operator.

---

## 7. Validation & Quality

A forecast tool is only useful if the operator trusts it, and trust is earned through validation. The tool must support three distinct validation workflows: initial model validation during development, ongoing forecast scoring during operation, and drift detection when the model's behavior changes unexpectedly.

### 7.1 Initial model validation (backtesting)

Before any model is deployed, it must be backtested on historical data using a strict walk-forward methodology. The model is trained on data up to time T, produces a forecast for time T+horizon, and the forecast is compared to the realized outcome. This is repeated for every T in a held-out period.

The backtest produces:

- **Directional accuracy:** what fraction of forecasts called the correct direction bucket
- **Confidence calibration:** when the model says 70% confident, is it actually right 70% of the time?
- **Error distribution:** histogram of forecast errors at each horizon
- **Performance by regime:** does accuracy differ between calm markets and crisis markets?
- **Worst-case drawdowns:** the largest consecutive run of wrong calls

A model is only approved for operation if its backtest performance meets predefined thresholds. The specific thresholds depend on the operator's risk tolerance but should exceed what a naive persistence-based forecast ("tomorrow will be like today") achieves.

### 7.2 Ongoing forecast scoring

In operation, every forecast is logged with its timestamp, horizon, and model version. When the forecast horizon passes, the realized outcome is also logged. This produces a continuously growing dataset of forecast-outcome pairs that lets the operator monitor accuracy over time.

The daily briefing includes a small section showing recent forecast performance: directional hit rate over the last 4, 13, and 26 weeks at each horizon. This creates accountability and lets the operator see when the model is losing its edge.

### 7.3 Drift detection

If the input feature distributions drift significantly from the training distribution, the statistical residual model may silently degrade. Drift detection runs as part of the daily pipeline, monitoring the distance between recent feature values and the training distribution. When drift exceeds a threshold, the operator is alerted and the confidence on affected forecasts is reduced.

### 7.4 Sanity checks

Beyond statistical validation, the tool runs simple sanity checks on every forecast:

- **Physical consistency:** if rates are forecast up, does the tool's physical model agree? If not, why?
- **Cross-signal consistency:** if WTI is forecast strongly up but tanker rates are forecast flat, that's suspicious — tight crude usually means tight shipping
- **Magnitude check:** is the forecast within the historical range of weekly moves? A forecast outside historical norms is flagged
- **Input staleness:** are all inputs less than N days old? Stale data penalizes confidence

Failed sanity checks do not block the forecast, but they are surfaced prominently in the briefing so the operator can evaluate whether to trust the call.

---

## 8. Deployment Plan

### 8.1 Phase 0: Local development

The first version runs entirely on the operator's workstation. All data sources are accessed from that machine, the data store is a local file store, and the briefing is rendered as a local file the operator opens manually. This phase exists to validate the pipeline end-to-end before any operational commitment.

### 8.2 Phase 1: Local scheduled

Same components as Phase 0, but the daily pipeline runs on a schedule via the operating system's native scheduler. The briefing is delivered via local notification or file drop. The operator consumes the briefing without manual invocation. This is the steady state for single-user operation.

### 8.3 Phase 2: Cloud-ready

The pipeline is packaged for deployment to a cloud environment. The data store is migrated from local files to a hosted database, with the data access abstraction making this transparent to the model code. The pipeline runs on a scheduled cloud task, and the briefing is delivered via email or web dashboard. The operator can access the briefing from any device.

Phase 2 is optional and is only undertaken if Phase 1 has proven useful enough to justify the added operational complexity and cost. The design does not force Phase 2 on the operator.

### 8.4 What must not change across phases

The module interfaces, the data access layer, and the model implementations must not change between phases. Only the environment-specific parts (scheduling, data store backend, delivery mechanism) change. This is enforced by the separation of concerns in the architecture.

---

## 9. Known Limitations & Risks

This section exists to set correct expectations. Every limitation listed here is something the tool will not handle well, and the operator should understand these before relying on the output.

### 9.1 Regime change

The statistical residual model is trained on history. If the current market enters a regime unlike anything in the training data — for example, a full-scale war between major powers, a novel sanctions regime, or a fundamental shift in Chinese demand — the residual model may produce misleading corrections. The physical model is more robust because it encodes structural relationships, but even the physical model may fail if the new regime changes how markets respond to the structural state (for example, if governments impose price controls).

**Mitigation:** the tool surfaces confidence indicators prominently and will reduce confidence when current state is far from training distribution. The operator should always read the diagnostic section, not just the headline.

### 9.2 Unobservable flows

A significant fraction of crude oil flows are not observable in AIS or public commercial data. Iranian dark fleet activity, Venezuelan exports via transshipment, and Russian crude via intermediary ports all move real barrels that the physical model does not see. Commercial services like Kpler and Vortexa infer these flows from satellite imagery and proxy signals; their inferred output is not available in the free tier used by this tool.

**Mitigation:** the tool accounts for an "unobserved flows" term in the physical balance with two stacked correctives. The statistical residual layer estimates its magnitude indirectly from historical patterns. A planned Sentinel-1 SAR ingestion path (§4.2.2) observes a portion of dark-fleet activity directly by cross-referencing radar ship detections against AIS — this replaces inference with measurement for the AOIs the SAR pipeline covers, but is not part of v0.

### 9.3 News and sentiment

Markets react to headlines faster than any daily-update tool can track. A single headline — OPEC announcement, ceasefire, military incident — can move prices more than a week of physical data changes. This tool is a 1-to-3-month directional aid, not a reaction-to-news system.

**Mitigation:** the tool is explicitly scoped to the medium-term directional question and does not claim to handle news reactions. The operator should treat the briefing as one input among many.

### 9.4 Data source brittleness

Web scraping of free data sources is inherently brittle. Sites change their HTML, APIs change their terms, government releases are delayed. Any given source can become unavailable without warning.

**Mitigation:** each source is isolated behind a standard interface, with fallback caching and explicit degradation. The operator is alerted when sources fail and can replace individual ingestion modules without affecting the rest of the system.

### 9.5 Overfitting risk

With only 5-6 years of reliable daily data, any complex model is at risk of overfitting to the training period. The tool mitigates this by keeping models simple, using cross-validation during training, and running ensembles so the operator sees disagreement when models diverge. But the fundamental constraint remains: short history limits what any model can learn.

### 9.6 Coupling effects

The WTI and tanker models are built separately in v0 but are physically coupled: higher rates affect export economics, which affect WTI, which affects the Brent-WTI spread, which affects routing decisions, which affect tanker demand. Treating them as independent forecasts introduces systematic error whenever the coupling is strong. The design acknowledges this and plans to add coupling terms in later versions.

### 9.7 AIS observability ceiling

AIS supplies ship positions and self-declared identifiers; nothing else. Three properties the tanker model needs but AIS cannot directly supply:

- **Load state.** Whether a vessel is loaded or in ballast cannot be read from a position report. It must be inferred from instantaneous draught (broadcast in some position messages but often zero or stale) compared against the vessel's `MaximumStaticDraught` from `ShipStaticData`. The inference is noisy and breaks for the dark fleet, which under-reports draught precisely to obscure cargo state.
- **Voyage intent.** The `destination` field in `ShipStaticData` is operator-entered free text — common values include "FOR ORDERS", abbreviated port codes, regional descriptors, or blank. It is unreliable as a routing input. True voyage classification requires segmenting the position track into voyages and attributing each leg's origin and destination from terminal-proximity polygons around known crude terminals.
- **Identity verification.** AIS broadcasts the operator-set MMSI and name. Spoofing — broadcasting the identity of a different, legitimate vessel — is documented in dark-fleet activity. Detection requires an external sensor (Sentinel-1 SAR per §4.2.2) or a vessel registry cross-reference.

**Mitigation:** any v0 ships pipeline that reads only AIS is scoped to position-finding. Load-state inference, voyage classification, and identity cross-checks are explicit follow-on workstreams. UI surfaces built on AIS positions alone should be labeled accordingly — "tanker positions," not "tanker activity" — so the operator is not misled into reading more into the dots than the data supports.

---

## 10. Roadmap

The tool is built in versioned increments. Each version is usable on its own and adds capability the previous version lacked. Versions are not bound to a schedule; each moves forward when the previous version has proven itself and the next increment is justified.

### 10.1 v0: Pipeline and physical models

- Ingestion modules for all planned free data sources
- Feature engineering for the inputs needed by physical models
- Physical WTI balance model, producing directional calls from weekly stock data
- Physical tanker rate model, producing per-route directional calls
- Morning briefing template with forecasts and diagnostics
- Forecast history logging

v0 is complete when the daily briefing is produced reliably for a full week without manual intervention, and the physical forecasts match what the operator would conclude from reading the raw data.

### 10.2 v1: Statistical residual correction

- Backtest framework with walk-forward methodology
- First statistical residual models (linear and tree-based)
- Combined physical + statistical forecasts
- Confidence calibration and ensemble disagreement reporting
- Drift detection

v1 is complete when backtested directional accuracy exceeds the v0 physical-only baseline at both 1-week and 4-week horizons.

### 10.3 v2: Coupling and refinement

- Explicit WTI-tanker coupling terms in the physical model
- Additional feature sources as identified during v1 operation
- Better anomaly detection and automatic alerting
- Forecast performance dashboard as part of the briefing

### 10.4 v3: Cloud deployment

- Migrate data store to hosted backend
- Schedule pipeline in cloud task runner
- Deliver briefing via email or hosted dashboard
- Multi-device access for the operator

### 10.5 Future possibilities

Longer-term enhancements that are out of scope for the initial roadmap but worth noting:

- Product-specific forecasts (diesel, gasoline, jet fuel, naphtha)
- Refining margin modeling as a coupling bridge
- Equity return forecasting from commodity forecasts
- Scenario analysis tool (what-if for specific geopolitical events)
- Option pricing integration to translate directional calls into specific contract recommendations
- Sentinel-1 SAR dark-fleet ingestion (see §4.2.2). Targets a post-v1 increment once the AIS pipeline is producing reliable join keys and the residual model has stabilized enough to absorb a new feature without overfitting.

---

## 11. Open Questions

This section captures decisions that need to be made during v0 implementation but that did not have an obvious right answer during design. Each should be resolved before the corresponding component is built.

### Training window length

The design targets 2019-present as the training window. However, the operator may decide that including the COVID period distorts the model because of its extreme state. An alternative is to train on 2022-present (post-COVID, including Russia-Ukraine and the current crisis). This should be decided empirically by backtesting both choices.

### Forecast horizon selection

The design specifies 1-week and 4-week horizons, but the operator's actual trade horizon is 1-3 months. Adding a 12-week horizon would match the operator's decision window more precisely, at the cost of lower backtest statistical power. This should be revisited after v0 is in operation.

### Ensemble combination rule

When multiple statistical models disagree, the tool needs a rule for combining them into a single forecast. Options include simple averaging, confidence-weighted averaging, and "show all and flag disagreement." The design defaults to showing all, but the operator may prefer a combined number.

### Alerting thresholds

At what confidence level should a forecast change trigger an alert? What about anomaly detection? These thresholds need tuning against operational experience — set too low and the operator is flooded, too high and important changes are missed.

### SAR ship detection method

§4.2.2 Phase 1 leaves the detection method unspecified. The two viable approaches are CFAR (constant false alarm rate sliding-window detector — simple, no training data, well-understood failure modes near land) and a pre-trained ML model (open-weight options exist; better small-vessel recall but more maintenance). The choice should be made after Phase 0 produces an AOI with AIS-confirmed ground truth, by running both detectors on the same imagery and comparing precision/recall against the broadcasting subset. Phase 0 (May 2026, Hormuz) validated CFAR's basic sanity at 40 m/px; the ML alternative has not been attempted.

### Static-infrastructure exclusion

Phase 0 confirmed that single-scene CFAR cannot distinguish ships from fixed metallic structures (oil platforms, buoys, mooring towers, fish farms). The Hormuz Phase 0 run had four detections at the same coordinates across separate acquisitions — almost certainly a stationary structure misclassified as a ship. Two viable mitigations: (a) multi-temporal background subtraction — subtract a per-pixel rolling-median sigma0 image from the current scene, suppressing anything that's been bright across many dates; (b) a curated static-infrastructure exclusion mask (open data sources include OpenStreetMap "man_made=*" tags for offshore infrastructure). (a) is automatic but requires N≥10 scenes per AOI to estimate background. (b) is manual but immediately useful. The right path is probably (a) for Phase 1 routine use, with (b) as a fallback for AOIs with insufficient background imagery.

### Time-aligned AIS for SAR validation

Phase 0 deferred the SAR ↔ AIS cross-reference because the AIS snapshot in hand was 3 weeks older than the SAR scenes. Real validation requires running the AIS Phase 1 collector (§4.2.1 Phase 1) concurrently with SAR ingestion so positions are available within ±15 minutes of each scene's acquisition time. Until that pipeline exists, the SAR detector is validated only on detection counts and geographic plausibility, not on per-detection precision/recall.

### Historical data acquisition

Some historical data sources (particularly tanker rate history from Baltic Exchange archives and historical AIS position snapshots) are difficult to obtain for free. The operator needs to decide whether to pay for one-time historical data purchases to bootstrap the training set, or to start with whatever is available and let the training set grow organically as the tool operates.

This question is now sharpest for AIS (§4.2.1): the chosen feed, aisstream.io, has no archive, so backtest depth on tanker-side features grows roughly one calendar month per calendar month of operation. Three options to weigh:

- **Wait.** Start collecting now and accept that early backtests are short and statistically weak. Acceptable if v0 prioritizes the WTI side and treats the tanker model as provisional.
- **Marine Cadastre (NOAA, free).** Provides historical AIS for US waters back to 2009. Useful for partial TD22 (USGC → China) backfill but irrelevant for TD3C and TD15. Lowest cost, narrowest scope.
- **Paid global AIS history** (Spire, MarineTraffic, VT Explorer, equivalents). Buys immediate global backtest depth at significant one-time cost. Worth it only if backtested tanker accuracy is the binding constraint on operator trust.

The recommended default is to wait through Phase 0/1 of AIS ingestion, evaluate live forecast performance against realized rates for at least one quarter, and only then revisit the paid-history decision with concrete evidence of where the model is weak.
