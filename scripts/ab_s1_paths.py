#!/usr/bin/env python3
"""A/B validation: Sentinel Hub vs DESP-raw S1 clusters.

After the daily S1 GRD cron runs alongside the weekly Sentinel Hub cron,
compare their `clusters.parquet` outputs for one AOI. We want to know
whether the DESP-raw + local-calibration path produces equivalent
detections — within ~10% on persistent over-water clusters.

OID-7 acceptance gate: if the two paths agree within 10%, the OID-9
6-month backfill is safe to start. If they disagree by >20%, the
DESP-side calibration or detection is off and needs debugging before
we burn bandwidth.

Usage:
    uv run python scripts/ab_s1_paths.py --aoi persian_gulf_oman
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
# Reuse the dashboard's storage layer so this script reads from MinIO when
# DATA_URI is s3:// (the production setup on swagner-server).
sys.path.insert(0, str(REPO_ROOT / "pipelines"))
from _env import data_uri, load_repo_env, storage_fs  # noqa: E402

load_repo_env()


def _load_clusters(uri: str) -> pd.DataFrame:
    """Read clusters.parquet from either local disk or s3:// via fsspec."""
    fs = storage_fs()
    if not fs.exists(uri):
        print(f"  WARN  {uri} not found — skipping")
        return pd.DataFrame()
    df = pd.read_parquet(uri)
    if "is_persistent" in df.columns:
        df["is_persistent"] = df["is_persistent"].astype(bool)
    if "any_on_land" in df.columns:
        df["any_on_land"] = df["any_on_land"].astype(bool)
    return df


def _persistent_water(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    return df[df["is_persistent"] & (~df["any_on_land"])].copy()


def _mean_nearest_neighbor_km(a: pd.DataFrame, b: pd.DataFrame) -> float | None:
    """For each cluster in `a`, find its nearest neighbor in `b` by lat/lon
    and return the mean great-circle distance in km. Direction is a→b only
    (not symmetric); good enough for parity sanity-checking."""
    if a.empty or b.empty:
        return None
    import math
    R = 6371.0
    lat1 = a["lat"].to_numpy()
    lon1 = a["lon"].to_numpy()
    lat2 = b["lat"].to_numpy()
    lon2 = b["lon"].to_numpy()
    import numpy as np
    # Vectorised pairwise haversine: (n_a, n_b) — fine for hundreds of clusters
    lat1_r = np.radians(lat1)[:, None]
    lon1_r = np.radians(lon1)[:, None]
    lat2_r = np.radians(lat2)[None, :]
    lon2_r = np.radians(lon2)[None, :]
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    h = (np.sin(dlat / 2) ** 2
         + np.cos(lat1_r) * np.cos(lat2_r) * np.sin(dlon / 2) ** 2)
    dist = 2 * R * np.arcsin(np.sqrt(h))
    return float(dist.min(axis=1).mean())


def compare(aoi: str, data_dir: Path | None = None) -> dict:
    if data_dir is not None:
        # Explicit local data dir wins — useful right after running the
        # pipeline locally on swagner-server, before mc-mirror to MinIO.
        sh_path = str(data_dir / "sentinel_sar" / aoi / "clusters.parquet")
        grd_path = str(data_dir / "sentinel_s1_grd" / aoi / "clusters.parquet")
    else:
        # Default: follow the dashboard's storage layer (s3:// in production).
        sh_path = data_uri("sentinel_sar", aoi, "clusters.parquet")
        grd_path = data_uri("sentinel_s1_grd", aoi, "clusters.parquet")

    print(f"\n─── A/B clusters for AOI: {aoi} ───")
    print(f"  SH path:  {sh_path}")
    print(f"  GRD path: {grd_path}")

    sh = _load_clusters(sh_path)
    grd = _load_clusters(grd_path)
    sh_p = _persistent_water(sh)
    grd_p = _persistent_water(grd)

    n_sh = len(sh)
    n_grd = len(grd)
    n_sh_p = len(sh_p)
    n_grd_p = len(grd_p)

    pct = lambda a, b: (
        "n/a (one side empty)" if not (a and b)
        else f"{(abs(a-b)/max(a,b))*100:.1f}% (diff {a-b:+d})"
    )

    print(f"\n  total clusters       SH={n_sh:5d}  GRD={n_grd:5d}  Δ={pct(n_sh, n_grd)}")
    print(f"  persistent water     SH={n_sh_p:5d}  GRD={n_grd_p:5d}  Δ={pct(n_sh_p, n_grd_p)}")

    mean_nn_a = _mean_nearest_neighbor_km(sh_p, grd_p)
    mean_nn_b = _mean_nearest_neighbor_km(grd_p, sh_p)
    if mean_nn_a is not None and mean_nn_b is not None:
        print(f"  mean NN distance     SH→GRD={mean_nn_a:.2f}km   GRD→SH={mean_nn_b:.2f}km")
        print(f"  (clusters within 1km of each other on both sides ≈ matched)")

    # Verdict
    if not (n_sh_p and n_grd_p):
        print("\n  VERDICT: indeterminate — one side has no persistent water clusters")
        return {"verdict": "indeterminate"}
    drift_pct = abs(n_sh_p - n_grd_p) / max(n_sh_p, n_grd_p) * 100
    if drift_pct <= 10:
        verdict = "PASS  (drift ≤10%)"
    elif drift_pct <= 20:
        verdict = "MARGINAL  (drift 10-20% — investigate before OID-9 backfill)"
    else:
        verdict = "FAIL  (drift >20% — debug DESP-side calibration/detection)"
    print(f"\n  VERDICT: {verdict}")
    return {
        "verdict": verdict,
        "sh_persistent": n_sh_p, "grd_persistent": n_grd_p,
        "drift_pct": drift_pct,
        "mean_nn_sh_to_grd_km": mean_nn_a,
        "mean_nn_grd_to_sh_km": mean_nn_b,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="A/B Sentinel Hub vs DESP-raw S1 clusters")
    p.add_argument("--aoi", required=True, help="AOI name (e.g. persian_gulf_oman)")
    p.add_argument("--data-dir", type=Path, default=None,
                   help="Override: read from local DATA_DIR instead of the "
                        "dashboard's storage layer (data_uri). Use this right "
                        "after a local pipeline run, before mc-mirror.")
    args = p.parse_args()
    compare(args.aoi, data_dir=args.data_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
