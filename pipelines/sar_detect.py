"""
CFAR ship detection on Sentinel-1 sigma0 dB rasters.

Runs a sliding-window constant-false-alarm-rate detector on each .tif under
an AOI directory and writes detections.parquet alongside the source raster.
For each cluster of detected pixels, records centroid lat/lon, sigma0_db
peak, area, and an `on_land` flag from a global 1 km land mask.

Usage:
    python sar_detect.py \\
        --scene-dir "$DATA_DIR/sentinel_sar/<aoi>" \\
        --summary

CFAR config defaults are tuned for Sentinel-1 IW GRD VV at ~40 m/px:
    --window 31    (~1.2 km training window)
    --k 6.0        (threshold = local_mean + k * local_std)
    --min-area 2   (drop single-pixel noise)
    --max-area 80  (drop landmass clusters and structures bigger than VLCC)

Sidecar parquet schema (one row per detection cluster):
    scene_id, datetime, lat, lon, sigma0_peak_db, area_px, on_land
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
import structlog
from global_land_mask import globe
from scipy import ndimage

log = structlog.get_logger()

FILL_DB = -50.0  # evalscript clamps no-data pixels to -50 dB


def cfar_detect(arr: np.ndarray, window: int, k: float) -> np.ndarray:
    """Sliding-window CFAR mask. Returns boolean array of detection pixels.

    Computes local mean and stddev over a `window × window` neighborhood
    (no guard region for v0 simplicity — fine when target ≪ window).
    Pixels with no data (== FILL_DB) are excluded from both target and
    background estimation.
    """
    h = arr.astype(np.float32)
    valid = h > (FILL_DB + 0.1)
    h_filled = np.where(valid, h, 0.0)
    valid_f = valid.astype(np.float32)

    counts = ndimage.uniform_filter(valid_f, size=window, mode="reflect")
    sums   = ndimage.uniform_filter(h_filled, size=window, mode="reflect")
    sq_sums = ndimage.uniform_filter(h_filled * h_filled, size=window, mode="reflect")

    safe = counts > 0
    means = np.where(safe, sums / np.maximum(counts, 1e-6), 0.0)
    var = np.where(safe, sq_sums / np.maximum(counts, 1e-6) - means * means, 0.0)
    stds = np.sqrt(np.maximum(var, 0.0))

    return valid & (h > means + k * stds)


def cluster_detections(
    mask: np.ndarray,
    sigma0: np.ndarray,
    transform,
    min_area: int,
    max_area: int,
) -> list[dict]:
    """Connected-component clustering. Returns list of detection dicts."""
    labeled, n = ndimage.label(mask)
    if n == 0:
        return []

    out = []
    # Compute per-cluster stats efficiently
    sums = ndimage.sum_labels(np.ones_like(mask, dtype=np.int32), labeled, range(1, n + 1))
    centroids = ndimage.center_of_mass(mask.astype(np.float32), labeled, range(1, n + 1))
    peaks = ndimage.maximum(sigma0, labeled, range(1, n + 1))

    for label_id, (area, centroid, peak) in enumerate(zip(sums, centroids, peaks), start=1):
        area_int = int(area)
        if area_int < min_area or area_int > max_area:
            continue
        cy, cx = centroid
        lon, lat = transform * (float(cx), float(cy))
        out.append({
            "lat": float(lat),
            "lon": float(lon),
            "sigma0_peak_db": float(peak),
            "area_px": area_int,
        })
    return out


def find_sidecar(tif_path: Path) -> dict | None:
    """Locate the per-scene sidecar JSON.

    Tiled layout: `<scene_dir>/_scene.json` next to the tif's parent.
    Legacy flat layout: `<scene_id>.json` next to the tif (fallback).
    """
    candidates = [
        tif_path.parent / "_scene.json",   # tiled layout
        tif_path.with_suffix(".json"),     # legacy flat layout
    ]
    for p in candidates:
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                continue
    return None


def process_scene(
    tif_path: Path,
    window: int,
    k: float,
    min_area: int,
    max_area: int,
) -> tuple[pd.DataFrame, dict]:
    with rasterio.open(tif_path) as ds:
        sigma0 = ds.read(1)
        transform = ds.transform

    mask = cfar_detect(sigma0, window=window, k=k)
    dets = cluster_detections(mask, sigma0, transform, min_area=min_area, max_area=max_area)

    # Annotate each detection with on_land via global_land_mask (1 km res)
    if dets:
        lats = np.array([d["lat"] for d in dets])
        lons = np.array([d["lon"] for d in dets])
        # global_land_mask wants lat in [-90,90], lon in [-180,180]
        on_land_arr = globe.is_land(lats, lons)
        for d, on_land in zip(dets, on_land_arr):
            d["on_land"] = bool(on_land)

    sidecar = find_sidecar(tif_path) or {}
    df = pd.DataFrame(dets)
    if not df.empty:
        df.insert(0, "scene_id", sidecar.get("scene_id") or tif_path.stem)
        df.insert(1, "datetime", sidecar.get("datetime"))
        df.insert(2, "orbit_state", sidecar.get("orbit_state"))

    summary = {
        "scene": tif_path.name,
        "scene_id": sidecar.get("scene_id"),
        "datetime": sidecar.get("datetime"),
        "orbit_state": sidecar.get("orbit_state"),
        "valid_px_pct": float((sigma0 > FILL_DB + 0.1).mean() * 100),
        "total_detections": len(dets),
        "over_water": int(sum(1 for d in dets if not d.get("on_land"))),
        "on_land": int(sum(1 for d in dets if d.get("on_land"))),
    }
    return df, summary


def main():
    parser = argparse.ArgumentParser(description="CFAR ship detection on Sentinel-1 sigma0 dB rasters")
    parser.add_argument("--scene-dir", required=True, type=lambda p: Path(p).expanduser(),
                        help="AOI dir (recursively walks for *.tif files)")
    parser.add_argument("--window",   type=int,   default=31,  help="CFAR training window (px)")
    parser.add_argument("--k",        type=float, default=6.0, help="Threshold = mean + k*std")
    parser.add_argument("--min-area", type=int,   default=2,   help="Min cluster area (px)")
    parser.add_argument("--max-area", type=int,   default=80,  help="Max cluster area (px)")
    parser.add_argument("--summary",  action="store_true",     help="Print per-scene summary")
    args = parser.parse_args()

    if not args.scene_dir.is_dir():
        log.error("scene_dir_missing", path=str(args.scene_dir))
        sys.exit(1)

    tifs = sorted(args.scene_dir.rglob("*.tif"))
    log.info("scan_start", count=len(tifs))

    summaries: list[dict] = []
    for tif in tifs:
        try:
            df, summary = process_scene(tif, args.window, args.k, args.min_area, args.max_area)
        except Exception as e:
            log.error("process_failed", tif=str(tif), error=str(e))
            continue

        out_path = tif.with_name(tif.stem + "_detections.parquet")
        if not df.empty:
            df.to_parquet(out_path, index=False)
        else:
            # Still write an empty parquet to record we processed this scene
            empty = pd.DataFrame(columns=[
                "scene_id", "datetime", "orbit_state",
                "lat", "lon", "sigma0_peak_db", "area_px", "on_land",
            ])
            empty.to_parquet(out_path, index=False)

        log.info("processed", scene=tif.name,
                 total=summary["total_detections"], water=summary["over_water"],
                 land=summary["on_land"], valid_px_pct=round(summary["valid_px_pct"], 1))
        summaries.append(summary)

    if args.summary and summaries:
        # Group by scene_id (from sidecar) so tiled scenes report as one row
        from collections import defaultdict
        by_scene: dict[str, list[dict]] = defaultdict(list)
        for s in summaries:
            by_scene[s.get("scene_id") or s["scene"]].append(s)

        print()
        print(f"{'datetime':<22} {'orbit':<11} {'tiles':>5} {'total':>6} {'water':>6} {'land':>5}  scene_id")
        for scene_id in sorted(by_scene, key=lambda k: (by_scene[k][0].get("datetime") or "")):
            group = by_scene[scene_id]
            first = group[0]
            dt = (first.get("datetime") or "")[:19]
            orb = (first.get("orbit_state") or "?")[:11]
            total = sum(s["total_detections"] for s in group)
            water = sum(s["over_water"] for s in group)
            land  = sum(s["on_land"] for s in group)
            print(f"{dt:<22} {orb:<11} {len(group):>5} "
                  f"{total:>6} {water:>6} {land:>5}  {scene_id[:60]}")
        totals = {
            "tiles": len(summaries),
            "scenes": len(by_scene),
            "total_detections": sum(s["total_detections"] for s in summaries),
            "over_water": sum(s["over_water"] for s in summaries),
            "on_land": sum(s["on_land"] for s in summaries),
        }
        print()
        print(f"Totals: {totals['scenes']} scenes ({totals['tiles']} tiles) · "
              f"{totals['total_detections']} detections "
              f"({totals['over_water']} over water, {totals['on_land']} on land)")


if __name__ == "__main__":
    main()
