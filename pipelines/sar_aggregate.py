"""
Cross-acquisition aggregation for SAR detections.

Reads all per-tile detection parquets under an AOI, clusters detections by
spatial proximity (KDTree + connected components), and labels each cluster
as `persistent` (appears in N+ distinct acquisitions) or `transient`. This
gives a deduplicated view of unique objects in the AOI rather than per-pass
detections, suitable for visualization.

Persistent clusters are mostly fixed infrastructure (oil platforms, buoys,
mooring towers) plus vessels anchored throughout the observation window.
Transient clusters are vessels caught on one or two passes — ships actually
moving through.

Usage:
    python sar_aggregate.py \\
        --scene-dir "$DATA_DIR/sentinel_sar/<aoi>" \\
        --radius-meters 300 \\
        --persistent-min-scenes 3 \\
        --summary

Outputs (written to scene-dir):
    aggregated_detections.parquet   one row per detection, with cluster_id and is_persistent
    clusters.parquet                one row per cluster, with centroid and persistence stats
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import structlog
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

log = structlog.get_logger()

DEFAULT_RADIUS_M = 300        # detections within this distance are clustered together
DEFAULT_PERSIST_SCENES = 3    # cluster appears in N+ distinct scenes → persistent


def load_all_detections(scene_dir: Path) -> pd.DataFrame:
    """Load every *_detections.parquet under scene_dir into one DataFrame."""
    frames = []
    for p in sorted(scene_dir.rglob("*_detections.parquet")):
        df = pd.read_parquet(p)
        if df.empty:
            continue
        # All columns come back as `object` dtype from pd.DataFrame(dicts) —
        # cast back to the right types.
        for c in ("lat", "lon", "sigma0_peak_db", "area_px"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c])
        if "on_land" in df.columns:
            df["on_land"] = df["on_land"].astype(bool)
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def cluster_by_proximity(df: pd.DataFrame, radius_m: float) -> np.ndarray:
    """KDTree pair query → connected components on a sparse adjacency graph.

    Returns a `cluster_id` array of length len(df). Self-pairs (i, i) are
    implicit; isolated detections form their own singleton clusters.
    """
    if df.empty:
        return np.array([], dtype=np.int64)

    # Local-flat-earth meters. Good to ~0.1% accuracy at the AOI scale we use.
    lat0 = df["lat"].mean()
    m_per_deg_lat = 111_000.0
    m_per_deg_lon = 111_000.0 * np.cos(np.radians(lat0))
    xy = np.column_stack([
        df["lat"].to_numpy() * m_per_deg_lat,
        df["lon"].to_numpy() * m_per_deg_lon,
    ])

    n = len(xy)
    tree = cKDTree(xy)
    pairs = tree.query_pairs(r=radius_m, output_type="ndarray")
    if len(pairs) == 0:
        return np.arange(n, dtype=np.int64)

    data = np.ones(len(pairs), dtype=np.int8)
    adj = csr_matrix((data, (pairs[:, 0], pairs[:, 1])), shape=(n, n))
    _, labels = connected_components(adj + adj.T, directed=False)
    return labels.astype(np.int64)


def summarize_clusters(df: pd.DataFrame, persist_min_scenes: int) -> pd.DataFrame:
    """One row per cluster, with centroid + persistence stats."""
    g = df.groupby("cluster_id", as_index=False).agg(
        lat=("lat", "mean"),
        lon=("lon", "mean"),
        n_detections=("lat", "size"),
        n_scenes=("scene_id", "nunique"),
        first_seen=("datetime", "min"),
        last_seen=("datetime", "max"),
        sigma0_max_db=("sigma0_peak_db", "max"),
        area_px_max=("area_px", "max"),
        any_on_land=("on_land", "max"),
    )
    g["is_persistent"] = g["n_scenes"] >= persist_min_scenes
    return g


def main():
    parser = argparse.ArgumentParser(description="Cluster + dedupe SAR detections across acquisitions")
    parser.add_argument("--scene-dir", required=True, type=lambda p: Path(p).expanduser(),
                        help="AOI dir with per-tile *_detections.parquet")
    parser.add_argument("--radius-meters", type=float, default=DEFAULT_RADIUS_M,
                        help=f"Cluster radius in meters (default {DEFAULT_RADIUS_M})")
    parser.add_argument("--persistent-min-scenes", type=int, default=DEFAULT_PERSIST_SCENES,
                        help=f"Cluster appears in this many distinct scenes → persistent (default {DEFAULT_PERSIST_SCENES})")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()

    if not args.scene_dir.is_dir():
        log.error("scene_dir_missing", path=str(args.scene_dir))
        sys.exit(1)

    log.info("load_start", scene_dir=str(args.scene_dir))
    df = load_all_detections(args.scene_dir)
    if df.empty:
        log.error("no_detections")
        sys.exit(1)
    log.info("loaded", detections=len(df), scenes=df["scene_id"].nunique())

    log.info("cluster_start", radius_m=args.radius_meters)
    df["cluster_id"] = cluster_by_proximity(df, args.radius_meters)
    clusters = summarize_clusters(df, args.persistent_min_scenes)
    df["is_persistent"] = df["cluster_id"].map(clusters.set_index("cluster_id")["is_persistent"])
    log.info("clustered", n_clusters=len(clusters),
             persistent=int(clusters["is_persistent"].sum()),
             transient=int((~clusters["is_persistent"]).sum()))

    det_path = args.scene_dir / "aggregated_detections.parquet"
    cl_path = args.scene_dir / "clusters.parquet"
    df.to_parquet(det_path, index=False)
    clusters.to_parquet(cl_path, index=False)
    log.info("written", aggregated=str(det_path), clusters=str(cl_path))

    if args.summary:
        n_total_det = len(df)
        n_total_cl = len(clusters)
        n_persist_cl = int(clusters["is_persistent"].sum())
        n_transient_cl = n_total_cl - n_persist_cl
        n_persist_water = int(((clusters["is_persistent"]) & (~clusters["any_on_land"].astype(bool))).sum())
        n_transient_water = int(((~clusters["is_persistent"]) & (~clusters["any_on_land"].astype(bool))).sum())
        n_persist_land = n_persist_cl - n_persist_water
        n_transient_land = n_transient_cl - n_transient_water

        print()
        print(f"Detections (raw): {n_total_det}")
        print(f"Unique objects (clustered):  {n_total_cl}")
        print(f"  Persistent (≥{args.persistent_min_scenes} scenes): {n_persist_cl}")
        print(f"      over water:  {n_persist_water}  (likely platforms / long-anchored)")
        print(f"      on land:     {n_persist_land}")
        print(f"  Transient  (1–{args.persistent_min_scenes - 1} scenes): {n_transient_cl}")
        print(f"      over water:  {n_transient_water}  (likely moving / short-anchored vessels)")
        print(f"      on land:     {n_transient_land}")

        # Show top persistent water locations (likely fixed structures)
        if n_persist_water:
            persist_water = clusters[(clusters["is_persistent"]) & (~clusters["any_on_land"].astype(bool))]
            print()
            print(f"--- top 10 persistent over-water locations (most-seen) ---")
            print(f"{'lat':>7} {'lon':>7} {'scenes':>6} {'dets':>5} {'sigma0_max':>10}")
            for _, r in persist_water.nlargest(10, "n_scenes").iterrows():
                print(f"{r.lat:>7.3f} {r.lon:>7.3f} {int(r.n_scenes):>6} {int(r.n_detections):>5} {r.sigma0_max_db:>10.1f}")


if __name__ == "__main__":
    main()
