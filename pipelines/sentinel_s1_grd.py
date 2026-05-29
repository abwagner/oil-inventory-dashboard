"""Sentinel-1 IW GRD ingest pipeline via DESP (free of PUs).

Parallel path to `sentinel_sar.py` (which uses Sentinel Hub's Process API
and bills PUs). Same output schema — tile_r{row}_c{col}.tif + _scene.json
per scene — so `sar_detect.py` + `sar_aggregate.py` consume the output
unchanged. The two paths can run side-by-side under separate data dirs
(`data/sentinel_s1_grd/<aoi>/` vs `data/sentinel_sar/<aoi>/`) for A/B
validation.

Flow per scene:
  1. OData search: find scenes intersecting AOI bbox in time window
  2. Skip-have: drop scenes already on disk (atomic via _scene.json marker)
  3. Download .SAFE.zip to a staging dir (chunked, Range-resume on partial)
  4. Calibrate DN → sigma0 dB via _s1_calibration
  5. Reproject to EPSG:4326 + clip to AOI bbox
  6. Tile to row/col grid at target (width, height) for the AOI
  7. Write tile_r{row}_c{col}.tif files + _scene.json marker
  8. Delete the .SAFE.zip (raw products are 1-2 GB; archive separately if you want history)

CLI:
    uv run python pipelines/sentinel_s1_grd.py \\
        --aoi-name persian_gulf_oman \\
        --bbox 54.0 24.0 60.0 28.0 \\
        --width 5000 --height 3333 \\
        --from 2026-05-12T00:00:00Z --to 2026-05-19T00:00:00Z \\
        --output-dir data/sentinel_s1_grd

See docs/DESP_FREE_IMAGERY_PLAN.md for the surrounding design.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import numpy as np
import rasterio
import rasterio.warp
import requests
import structlog

from _cdse_auth import CdseAuthError, get_access_token
from _env import load_repo_env
from _s1_calibration import apply_calibration, linear_to_db, parse_calibration_lut

load_repo_env()

log = structlog.get_logger("sentinel_s1_grd")


CATALOG_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
DOWNLOAD_URL_TPL = "https://download.dataspace.copernicus.eu/odata/v1/Products({id})/$value"
DEFAULT_TIMEOUT = 120
CHUNK_BYTES = 8 * 1024 * 1024   # 8 MB — large enough to amortise overhead, small enough to checkpoint frequently
MAX_DOWNLOAD_RETRIES = 3


# ─── Data model ──────────────────────────────────────────────────────────


@dataclass
class SceneMeta:
    """Metadata for one S1 GRD product returned by the OData catalog."""
    id: str               # OData product UUID — feeds the /$value download URL
    name: str             # e.g. S1A_IW_GRDH_1SDV_20260516T022408_…_4AAC_COG.SAFE
    content_length: int   # bytes
    start_time: str       # ISO timestamp of acquisition start
    polarisation: str = "vv"

    @property
    def scene_id(self) -> str:
        """Short id derived from the product name (strip the `.SAFE` suffix)."""
        return self.name.removesuffix(".SAFE")

    @property
    def date_dir(self) -> str:
        """YYYY-MM-DD date partition for `data/sentinel_s1_grd/<aoi>/<date>/`."""
        return self.start_time[:10]


# ─── OID-3: Catalog search + skip-have ───────────────────────────────────


def _bbox_to_wkt_polygon(bbox: tuple[float, float, float, float]) -> str:
    lon_min, lat_min, lon_max, lat_max = bbox
    return (
        f"POLYGON(({lon_min} {lat_min},{lon_max} {lat_min},"
        f"{lon_max} {lat_max},{lon_min} {lat_max},{lon_min} {lat_min}))"
    )


def _canonical_acquisition_key(name: str) -> str:
    """Group key shared by the COG and legacy variants of the same scene.

    DESP exposes each S1 acquisition twice — once as a Cloud-Optimized
    GeoTIFF (`..._<crc>_COG.SAFE`) and once in legacy format
    (`..._<crc>.SAFE`). The two have different per-product CRC16 tags but
    cover identical data. Strip both `_COG` and the trailing CRC tag to
    get a key that's stable across the pair.

        S1C_IW_GRDH_1SDV_20260519T015838_20260519T015900_007716_00FAE8_37CD_COG.SAFE
        S1C_IW_GRDH_1SDV_20260519T015838_20260519T015900_007716_00FAE8_7F59.SAFE
                                                                       ↑↑↑↑↑↑↑↑↑↑ vary
    both → "S1C_IW_GRDH_1SDV_20260519T015838_20260519T015900_007716_00FAE8"
    """
    name = name.removesuffix(".SAFE")
    if name.endswith("_COG"):
        name = name[:-4]
    return name.rsplit("_", 1)[0]


def search_scenes(
    bbox: tuple[float, float, float, float],
    start: str, end: str, token: str,
    top: int = 50,
    prefer_cog: bool = True,
) -> list[SceneMeta]:
    """OData search for S1 IW GRD products intersecting `bbox` in [start, end).

    `start` and `end` are ISO timestamps (Z-suffixed UTC). `top` caps the
    result set — Sentinel-1 has 6-day revisit at most AOIs so a 14-day
    window typically yields <20 scenes per AOI.

    DESP returns both COG and legacy variants of each acquisition; with
    `prefer_cog=True` (default) we keep one per acquisition, COG-favored
    where available. COG is ~half the size of legacy.
    """
    poly = _bbox_to_wkt_polygon(bbox)
    filter_clauses = " and ".join([
        "Collection/Name eq 'SENTINEL-1'",
        "contains(Name,'GRD')",
        "contains(Name,'IW')",
        f"OData.CSC.Intersects(area=geography'SRID=4326;{poly}')",
        f"ContentDate/Start gt {start}",
        f"ContentDate/Start lt {end}",
    ])
    url = (
        f"{CATALOG_URL}?$filter={quote(filter_clauses)}"
        f"&$orderby=ContentDate/Start desc&$top={top}"
    )
    r = requests.get(
        url, headers={"Authorization": f"Bearer {token}"},
        timeout=DEFAULT_TIMEOUT,
    )
    r.raise_for_status()
    raw: list[SceneMeta] = []
    for p in r.json().get("value", []):
        raw.append(SceneMeta(
            id=p["Id"], name=p["Name"],
            content_length=int(p.get("ContentLength") or 0),
            start_time=p["ContentDate"]["Start"],
        ))
    if not prefer_cog:
        return raw

    # Dedup: one scene per acquisition, COG variant preferred.
    by_acq: dict[str, SceneMeta] = {}
    for s in raw:
        key = _canonical_acquisition_key(s.name)
        existing = by_acq.get(key)
        if existing is None:
            by_acq[key] = s
            continue
        # Pick the COG variant when we see the pair.
        existing_cog = "_COG" in existing.name
        this_cog = "_COG" in s.name
        if this_cog and not existing_cog:
            by_acq[key] = s
    n_deduped = len(raw) - len(by_acq)
    if n_deduped:
        log.info("dedup_cog_variants", before=len(raw), after=len(by_acq),
                 removed=n_deduped)
    return list(by_acq.values())


def filter_already_ingested(
    scenes: list[SceneMeta], aoi_name: str, output_root: Path,
) -> list[SceneMeta]:
    """Drop scenes whose `_scene.json` marker exists on disk.

    Scene ingest is considered complete only after `_scene.json` is
    written (last step). Partial-state scene dirs are re-attempted —
    deliberate, so an interrupted run resumes cleanly.
    """
    keep = []
    for s in scenes:
        marker = (output_root / aoi_name / s.date_dir / s.scene_id / "_scene.json")
        if marker.exists():
            log.info("skip_already_ingested", scene=s.scene_id, aoi=aoi_name)
            continue
        keep.append(s)
    return keep


# ─── OID-4: Product download + retry/resume ──────────────────────────────


def _follow_download_redirects(
    url: str, token: str, headers: dict, timeout: int,
) -> requests.Response:
    """DESP's download endpoint redirects to a different host; requests
    strips Authorization on cross-origin redirects, so follow manually."""
    redirects = 0
    while redirects < 5:
        r = requests.get(
            url, headers={"Authorization": f"Bearer {token}", **headers},
            timeout=timeout, stream=True, allow_redirects=False,
        )
        if r.status_code in (301, 302, 303, 307, 308):
            url = r.headers["Location"]
            redirects += 1
            continue
        return r
    raise RuntimeError(f"Too many redirects ({redirects}) downloading {url}")


def download_product(
    scene: SceneMeta, dest_path: Path, token: str,
    chunk_bytes: int = CHUNK_BYTES,
    max_retries: int = MAX_DOWNLOAD_RETRIES,
) -> Path:
    """Download `scene` to `dest_path` (~1-2 GB). Streams in chunks; resumes
    via HTTP Range on partial files; retries 5xx with exponential backoff.

    Validates ZIP magic bytes on completion before returning.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    url = DOWNLOAD_URL_TPL.format(id=scene.id)

    for attempt in range(1, max_retries + 1):
        existing = dest_path.stat().st_size if dest_path.exists() else 0
        if existing > 0 and scene.content_length and existing >= scene.content_length:
            log.info("download_already_complete", scene=scene.scene_id,
                     bytes=existing)
            break
        headers = {"Range": f"bytes={existing}-"} if existing > 0 else {}
        log.info("download_start", scene=scene.scene_id, attempt=attempt,
                 resume_from=existing, expected_total=scene.content_length)
        try:
            r = _follow_download_redirects(url, token, headers, timeout=DEFAULT_TIMEOUT)
            if r.status_code not in (200, 206):
                log.warning("download_status_unexpected",
                            scene=scene.scene_id, status=r.status_code)
                # Retry on 5xx; bail on 4xx
                if 500 <= r.status_code < 600 and attempt < max_retries:
                    time.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
            mode = "ab" if existing > 0 and r.status_code == 206 else "wb"
            with open(dest_path, mode) as f:
                for chunk in r.iter_content(chunk_size=chunk_bytes):
                    if chunk:
                        f.write(chunk)
            break
        except requests.RequestException as e:
            if attempt >= max_retries:
                raise
            backoff = 2 ** attempt
            log.warning("download_retry", scene=scene.scene_id, attempt=attempt,
                        error=str(e)[:200], backoff_seconds=backoff)
            time.sleep(backoff)

    # Validate ZIP magic
    with open(dest_path, "rb") as f:
        magic = f.read(4)
    if magic != b"PK\x03\x04":
        raise RuntimeError(
            f"Downloaded {dest_path} has bad ZIP magic bytes {magic!r}; expected PK\\x03\\x04"
        )
    return dest_path


# ─── OID-6: Calibrate + reproject + tile ─────────────────────────────────


def _find_measurement_tiff(safe_zip: Path, polarisation: str) -> str:
    """Return the path-inside-zip of the measurement TIFF for `polarisation`.

    Inside a .SAFE archive: measurement/s1[a-d]-iw-grd-<pol>-...tiff
    """
    needle = f"-grd-{polarisation.lower()}-"
    with zipfile.ZipFile(safe_zip) as zf:
        for info in zf.infolist():
            n = info.filename.lower()
            if "measurement/" in n and needle in n and n.endswith(".tiff"):
                return info.filename
    raise FileNotFoundError(
        f"No measurement TIFF for pol={polarisation!r} in {safe_zip}"
    )


def process_scene(
    safe_zip: Path,
    aoi_bbox: tuple[float, float, float, float],
    target_size: tuple[int, int],
    scene_dir: Path,
    scene: SceneMeta,
    polarisation: str = "vv",
    tile_max_dim: int = 2500,
) -> dict:
    """Calibrate, reproject to EPSG:4326 over the AOI bbox, and tile to
    `target_size` (width, height) split into ~tile_max_dim chunks.

    Writes tile_r{row}_c{col}.tif files + _scene.json into scene_dir.
    Returns the scene_json dict for inspection.
    """
    # ── Read measurement raster from inside the zip (no extraction) ──
    inner = _find_measurement_tiff(safe_zip, polarisation)
    vsi_uri = f"/vsizip/{safe_zip}/{inner}"
    log.info("reading_measurement", scene=scene.scene_id, inner=inner)
    with rasterio.open(vsi_uri) as src:
        dn = src.read(1)
        gcps, gcp_crs = src.gcps

    # ── Calibrate DN → sigma0 dB ──
    log.info("calibrating", scene=scene.scene_id, shape=dn.shape)
    lut = parse_calibration_lut(safe_zip, polarisation)
    sigma0_linear = apply_calibration(dn, lut)
    sigma0_db = linear_to_db(sigma0_linear).astype(np.float32)

    # ── Reproject GCP-based src → EPSG:4326 clipped to AOI bbox ──
    target_w, target_h = target_size
    lon_min, lat_min, lon_max, lat_max = aoi_bbox
    dst_transform = rasterio.transform.from_bounds(
        lon_min, lat_min, lon_max, lat_max, target_w, target_h,
    )
    dst_crs = "EPSG:4326"
    dst = np.full((target_h, target_w), np.nan, dtype=np.float32)
    rasterio.warp.reproject(
        source=sigma0_db,
        destination=dst,
        src_crs=gcp_crs,
        gcps=gcps,
        dst_transform=dst_transform,
        dst_crs=dst_crs,
        resampling=rasterio.warp.Resampling.bilinear,
        src_nodata=np.nan,
        dst_nodata=np.nan,
    )

    # ── Tile the AOI-clipped raster into row/col grid ──
    scene_dir.mkdir(parents=True, exist_ok=True)
    n_cols = max(1, (target_w + tile_max_dim - 1) // tile_max_dim)
    n_rows = max(1, (target_h + tile_max_dim - 1) // tile_max_dim)
    tile_w = target_w // n_cols
    tile_h = target_h // n_rows
    tile_manifest: list[dict] = []
    for row in range(n_rows):
        for col in range(n_cols):
            x0 = col * tile_w
            y0 = row * tile_h
            x1 = (col + 1) * tile_w if col < n_cols - 1 else target_w
            y1 = (row + 1) * tile_h if row < n_rows - 1 else target_h
            tile_arr = dst[y0:y1, x0:x1]
            tile_lon_min = lon_min + (x0 / target_w) * (lon_max - lon_min)
            tile_lon_max = lon_min + (x1 / target_w) * (lon_max - lon_min)
            # Geographic Y: row 0 is at lat_max (north); flip y for bbox
            tile_lat_max = lat_max - (y0 / target_h) * (lat_max - lat_min)
            tile_lat_min = lat_max - (y1 / target_h) * (lat_max - lat_min)
            tile_bbox = (tile_lon_min, tile_lat_min, tile_lon_max, tile_lat_max)
            tile_transform = rasterio.transform.from_bounds(
                tile_lon_min, tile_lat_min, tile_lon_max, tile_lat_max,
                x1 - x0, y1 - y0,
            )
            tile_name = f"tile_r{row}_c{col}.tif"
            with rasterio.open(
                scene_dir / tile_name, "w",
                driver="GTiff", width=x1 - x0, height=y1 - y0,
                count=1, dtype=tile_arr.dtype,
                crs=dst_crs, transform=tile_transform, nodata=np.nan,
                compress="deflate", tiled=True, blockxsize=256, blockysize=256,
            ) as out:
                out.write(tile_arr, 1)
            tile_manifest.append({
                "row": row, "col": col,
                "bbox": list(tile_bbox),
                "width": x1 - x0, "height": y1 - y0,
                "filename": tile_name,
                "size_bytes": (scene_dir / tile_name).stat().st_size,
                "skipped": False,
            })

    # ── Manifest matching sentinel_sar.py's _scene.json schema ──
    scene_json = {
        "scene_id": scene.scene_id,
        "datetime": scene.start_time,
        "polarization": polarisation.upper(),
        "acquisition_mode": "IW",
        "source": "desp-grd",  # distinguishes from "sentinel-hub" path
        "aoi_bbox": list(aoi_bbox),
        "total_width": target_w,
        "total_height": target_h,
        "tile_grid": {"rows": n_rows, "cols": n_cols, "max_dim": tile_max_dim},
        "tiles": tile_manifest,
    }
    (scene_dir / "_scene.json").write_text(json.dumps(scene_json, indent=2))
    log.info("scene_written", scene=scene.scene_id, n_tiles=len(tile_manifest),
             scene_dir=str(scene_dir))
    return scene_json


# ─── Orchestrator + CLI ──────────────────────────────────────────────────


def ingest_aoi(
    aoi_name: str,
    bbox: tuple[float, float, float, float],
    target_size: tuple[int, int],
    start: str, end: str,
    output_root: Path,
    polarisation: str = "vv",
    cleanup_zip: bool = True,
) -> dict:
    """Ingest one AOI: search → download → process for each unseen scene."""
    token = get_access_token()
    scenes = search_scenes(bbox, start, end, token)
    log.info("scenes_found", aoi=aoi_name, n=len(scenes))
    scenes = filter_already_ingested(scenes, aoi_name, output_root)
    log.info("scenes_to_ingest", aoi=aoi_name, n=len(scenes))

    stats = {"aoi": aoi_name, "downloaded": 0, "processed": 0, "errors": 0}
    staging_dir = output_root / aoi_name / "_staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    for s in scenes:
        try:
            zip_path = staging_dir / f"{s.scene_id}.SAFE.zip"
            download_product(s, zip_path, token)
            stats["downloaded"] += 1
            scene_dir = output_root / aoi_name / s.date_dir / s.scene_id
            process_scene(zip_path, bbox, target_size, scene_dir, s,
                          polarisation=polarisation)
            stats["processed"] += 1
            if cleanup_zip:
                zip_path.unlink()
        except Exception as e:
            log.error("scene_failed", scene=s.scene_id, error=str(e)[:300])
            stats["errors"] += 1
    return stats


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    p = argparse.ArgumentParser(description="Sentinel-1 GRD ingest via DESP (free)")
    p.add_argument("--aoi-name", required=True)
    p.add_argument("--bbox", required=True, type=float, nargs=4,
                   metavar=("LON_MIN", "LAT_MIN", "LON_MAX", "LAT_MAX"))
    p.add_argument("--width", type=int, required=True)
    p.add_argument("--height", type=int, required=True)
    p.add_argument("--from", dest="start", required=True,
                   help="ISO UTC, e.g. 2026-05-12T00:00:00Z")
    p.add_argument("--to", dest="end", required=True)
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--polarisation", default="vv", choices=["vv", "vh", "hh", "hv"])
    p.add_argument("--keep-zip", action="store_true",
                   help="Don't delete the .SAFE.zip after processing (for debugging)")
    args = p.parse_args()

    try:
        stats = ingest_aoi(
            aoi_name=args.aoi_name,
            bbox=tuple(args.bbox), target_size=(args.width, args.height),
            start=args.start, end=args.end,
            output_root=args.output_dir,
            polarisation=args.polarisation,
            cleanup_zip=not args.keep_zip,
        )
    except CdseAuthError as e:
        log.error("auth_failed", error=str(e))
        return 2
    log.info("done", stats=stats)
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
