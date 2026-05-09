"""
CDSE Sentinel Hub Process API client for Sentinel-1 IW GRD ingestion.

Three subcommands:
  list    — STAC catalog search; print scenes covering an AOI + time range
  fetch   — single Process API mosaic call for an AOI + time range
  ingest  — incremental: catalog scan + per-scene fetch, organized by AOI/date

Auth via OAuth client_credentials. Reads CDSE_CLIENT_ID / CDSE_CLIENT_SECRET
from the repo-root .env (or process environment).

Usage:
  list    python sentinel_sar.py list \\
              --bbox 56.0 26.0 57.0 26.7 \\
              --from 2026-04-01T00:00:00Z --to 2026-04-15T23:59:59Z

  fetch   python sentinel_sar.py fetch \\
              --bbox 56.40 26.40 56.55 26.55 \\
              --from 2026-04-01T00:00:00Z --to 2026-04-15T23:59:59Z \\
              --width 512 --height 512 \\
              --output /tmp/sar.tif

  ingest  python sentinel_sar.py ingest \\
              --aoi-name hormuz \\
              --bbox 56.0 26.0 57.0 26.7 \\
              --from 2026-04-25T00:00:00Z --to 2026-05-07T00:00:00Z \\
              --width 2500 --height 1750 \\
              --output-dir "$DATA_DIR/sentinel_sar"
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import structlog
from dotenv import load_dotenv

from _env import load_repo_env; load_repo_env()
log = structlog.get_logger()

TOKEN_URL   = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"
CATALOG_URL = "https://sh.dataspace.copernicus.eu/api/v1/catalog/1.0.0/search"

# Process API caps width and height each at 2500. Larger total outputs are
# tiled into a grid of sub-bboxes; each sub-bbox is one Process API call.
MAX_TILE_DIM = 2500

# Evalscript: VV linear backscatter -> sigma0 in dB. Pixels with no return
# (e.g. ocean glassy regions) are clamped to -50 dB rather than -inf.
EVALSCRIPT_VV_SIGMA0_DB = """//VERSION=3
function setup() {
  return {
    input: ["VV"],
    output: { bands: 1, sampleType: "FLOAT32" }
  };
}
function evaluatePixel(s) {
  return [s.VV > 0 ? 10 * Math.log(s.VV) / Math.LN10 : -50];
}
"""


# --- Auth -----------------------------------------------------------------

def get_token(client_id: str, client_secret: str) -> str:
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"auth failed {resp.status_code}: {resp.text[:300]}")
    return resp.json()["access_token"]


def require_creds() -> tuple[str, str]:
    cid = os.environ.get("CDSE_CLIENT_ID")
    csec = os.environ.get("CDSE_CLIENT_SECRET")
    if not cid or not csec:
        log.error("missing_creds", hint="Set CDSE_CLIENT_ID / CDSE_CLIENT_SECRET in env or workspace .env")
        sys.exit(1)
    return cid, csec


# --- Catalog --------------------------------------------------------------

def catalog_search(
    token: str,
    bbox: tuple[float, float, float, float],
    time_from: str,
    time_to: str,
    polarization: str = "DV",
    acquisition_mode: str = "IW",
    limit: int = 100,
) -> list[dict]:
    """STAC search for Sentinel-1 GRD scenes intersecting bbox in time window.

    Returns a list of {id, datetime, orbit_state, relative_orbit, polarizations,
    acquisition_mode, geometry, properties} dicts, sorted oldest-first.
    """
    body = {
        "bbox": list(bbox),
        "datetime": f"{time_from}/{time_to}",
        "collections": ["sentinel-1-grd"],
        "limit": limit,
        "filter": {
            "op": "and",
            "args": [
                {"op": "=", "args": [{"property": "polarization"}, polarization]},
                {"op": "=", "args": [{"property": "sar:instrument_mode"}, acquisition_mode]},
            ],
        },
        "filter-lang": "cql2-json",
    }
    resp = requests.post(
        CATALOG_URL,
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    if resp.status_code != 200:
        # Some CDSE deployments reject the cql2-json filter; retry without it.
        body.pop("filter", None)
        body.pop("filter-lang", None)
        resp = requests.post(
            CATALOG_URL,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
    if resp.status_code != 200:
        raise RuntimeError(f"catalog {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    out = []
    for f in data.get("features", []):
        props = f.get("properties") or {}
        # Client-side polarization filter as a safety net
        pols = props.get("polarization") or props.get("s1:polarization")
        if pols and isinstance(pols, str) and polarization and polarization != pols:
            continue
        out.append({
            "id": f.get("id"),
            "datetime": props.get("datetime"),
            "orbit_state": props.get("sat:orbit_state") or props.get("orbitDirection"),
            "relative_orbit": props.get("sat:relative_orbit") or props.get("s1:relativeOrbitNumber"),
            "polarization": pols,
            "acquisition_mode": props.get("sar:instrument_mode"),
            "geometry": f.get("geometry"),
            "properties": props,
        })
    out.sort(key=lambda r: r["datetime"] or "")
    return out


# --- Process --------------------------------------------------------------

def fetch_s1_sigma0(
    token: str,
    bbox: tuple[float, float, float, float],
    time_from: str,
    time_to: str,
    width: int,
    height: int,
) -> tuple[bytes, dict]:
    """POST to Process API. Returns (image_bytes, response_headers)."""
    body = {
        "input": {
            "bounds": {
                "bbox": list(bbox),
                "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
            },
            "data": [{
                "type": "sentinel-1-grd",
                "dataFilter": {
                    "timeRange": {"from": time_from, "to": time_to},
                    "polarization": "DV",
                    "acquisitionMode": "IW",
                    "resolution": "HIGH",
                },
                "processing": {
                    "orthorectify": "true",
                    "backCoeff": "SIGMA0_ELLIPSOID",
                },
            }],
        },
        "output": {
            "width": width,
            "height": height,
            "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}],
        },
        "evalscript": EVALSCRIPT_VV_SIGMA0_DB,
    }
    resp = requests.post(
        PROCESS_URL,
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=300,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"process api {resp.status_code}: {resp.text[:500]}")
    return resp.content, dict(resp.headers)


def fetch_single_scene(
    token: str,
    bbox: tuple[float, float, float, float],
    scene_datetime_iso: str,
    width: int,
    height: int,
    seconds_window: int = 5,
) -> tuple[bytes, dict]:
    """Fetch the single Sentinel-1 acquisition at scene_datetime_iso.

    Narrows the Process API time window to ±seconds_window around the scene's
    own datetime so the response is one acquisition, not a mosaic.
    """
    dt = datetime.fromisoformat(scene_datetime_iso.replace("Z", "+00:00"))
    return fetch_s1_sigma0(
        token, bbox,
        time_from=(dt - timedelta(seconds=seconds_window)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        time_to=  (dt + timedelta(seconds=seconds_window)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        width=width, height=height,
    )


# --- Tiling helpers -------------------------------------------------------

def compute_tiles(
    bbox: tuple[float, float, float, float],
    total_width: int,
    total_height: int,
    max_dim: int = MAX_TILE_DIM,
) -> list[dict]:
    """Split a bbox + total output dims into a grid of tiles each ≤ max_dim.

    Row 0 is the northernmost row (max lat); col 0 is the westernmost (min lon).
    Each tile dict has: row, col, bbox, width, height.
    Pixel rows/cols are distributed evenly with the remainder spread across
    leading tiles; degree extents are split equally so all tiles cover equal
    geographic area.
    """
    lon_min, lat_min, lon_max, lat_max = bbox
    n_cols = max(1, math.ceil(total_width / max_dim))
    n_rows = max(1, math.ceil(total_height / max_dim))

    base_w, extra_w = divmod(total_width, n_cols)
    col_widths = [base_w + (1 if c < extra_w else 0) for c in range(n_cols)]
    base_h, extra_h = divmod(total_height, n_rows)
    row_heights = [base_h + (1 if r < extra_h else 0) for r in range(n_rows)]

    lon_step = (lon_max - lon_min) / n_cols
    lat_step = (lat_max - lat_min) / n_rows

    tiles = []
    for r in range(n_rows):
        for c in range(n_cols):
            tile_bbox = (
                lon_min + c * lon_step,
                lat_max - (r + 1) * lat_step,
                lon_min + (c + 1) * lon_step,
                lat_max - r * lat_step,
            )
            tiles.append({
                "row": r, "col": c,
                "bbox": tile_bbox,
                "width": col_widths[c], "height": row_heights[r],
            })
    return tiles


def bboxes_intersect(a, b) -> bool:
    """Both bboxes are (lon_min, lat_min, lon_max, lat_max)."""
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def polygon_bbox(geom: dict | None) -> tuple[float, float, float, float] | None:
    """Bounding box of a GeoJSON-style Polygon (or first ring of MultiPolygon)."""
    if not geom:
        return None
    t = geom.get("type")
    if t == "Polygon":
        ring = geom.get("coordinates", [[]])[0]
    elif t == "MultiPolygon":
        ring = []
        for poly in geom.get("coordinates", []):
            if poly:
                ring.extend(poly[0])
    else:
        return None
    if not ring:
        return None
    lons = [pt[0] for pt in ring]
    lats = [pt[1] for pt in ring]
    return (min(lons), min(lats), max(lons), max(lats))


def fetch_scene_tiled(
    token: str,
    scene: dict,
    total_bbox: tuple[float, float, float, float],
    total_width: int,
    total_height: int,
    seconds_window: int = 5,
) -> list[dict]:
    """Fetch one Sentinel-1 acquisition over total_bbox, tiled to fit Process API.

    Returns a list of {row, col, bbox, width, height, bytes, headers, pu_spent,
    skipped, skip_reason} dicts — one per tile in the grid. Tiles that don't
    intersect the scene's footprint are skipped (no PU charged).
    """
    tiles = compute_tiles(total_bbox, total_width, total_height)
    scene_bbox = polygon_bbox(scene.get("geometry"))
    sdt = scene["datetime"]
    dt = datetime.fromisoformat(sdt.replace("Z", "+00:00"))
    time_from = (dt - timedelta(seconds=seconds_window)).strftime("%Y-%m-%dT%H:%M:%SZ")
    time_to   = (dt + timedelta(seconds=seconds_window)).strftime("%Y-%m-%dT%H:%M:%SZ")

    out = []
    for t in tiles:
        result = {**t}
        if scene_bbox and not bboxes_intersect(scene_bbox, t["bbox"]):
            result.update({"bytes": None, "headers": None, "pu_spent": 0.0,
                           "skipped": True, "skip_reason": "no_geom_overlap"})
            out.append(result)
            continue
        try:
            data, headers = fetch_s1_sigma0(
                token, t["bbox"],
                time_from=time_from, time_to=time_to,
                width=t["width"], height=t["height"],
            )
            pu = header_pu(headers)
            try:
                pu_f = float(pu) if pu is not None else 0.0
            except (TypeError, ValueError):
                pu_f = 0.0
            result.update({"bytes": data, "headers": headers, "pu_spent": pu_f,
                           "skipped": False, "skip_reason": None})
        except Exception as e:
            result.update({"bytes": None, "headers": None, "pu_spent": 0.0,
                           "skipped": True, "skip_reason": f"error: {e}"})
        out.append(result)
    return out


def header_pu(headers: dict) -> str | None:
    return next((v for k, v in headers.items() if k.lower() == "x-processingunits-spent"), None)


# --- Ingest (incremental, AOI-organized) ----------------------------------

def _state_path(output_dir: Path, aoi_name: str) -> Path:
    return output_dir / aoi_name / "_state.json"


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {"last_processed_time": None, "scenes_seen": []}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"last_processed_time": None, "scenes_seen": []}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def cmd_ingest(args) -> int:
    cid, csec = require_creds()
    log.info("auth_start")
    token = get_token(cid, csec)
    log.info("auth_ok")

    bbox = tuple(args.bbox)
    aoi_dir = args.output_dir / args.aoi_name
    state_path = args.state_file or _state_path(args.output_dir, args.aoi_name)
    state = _load_state(state_path)

    # If state has a last_processed_time and caller didn't override --from, pick up there.
    time_from = args.time_from
    if not time_from and state.get("last_processed_time"):
        time_from = state["last_processed_time"]
    if not time_from:
        time_from = (datetime.now(tz=timezone.utc) - timedelta(days=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
    time_to = args.time_to or datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    log.info("catalog_search", aoi=args.aoi_name, bbox=bbox, time_from=time_from, time_to=time_to)
    scenes = catalog_search(token, bbox, time_from, time_to)
    log.info("catalog_result", count=len(scenes))

    seen = set(state.get("scenes_seen") or [])
    fetched = 0
    skipped = 0
    pu_total = 0.0
    latest_dt = state.get("last_processed_time")

    n_cols = max(1, math.ceil(args.width / MAX_TILE_DIM))
    n_rows = max(1, math.ceil(args.height / MAX_TILE_DIM))
    log.info("tile_grid", rows=n_rows, cols=n_cols,
             effective_res_lon_per_px=round((bbox[2]-bbox[0]) / args.width, 5),
             effective_res_lat_per_px=round((bbox[3]-bbox[1]) / args.height, 5))

    for s in scenes:
        sid = s["id"]
        sdt = s["datetime"]
        if not sid or not sdt:
            continue
        if sid in seen:
            skipped += 1
            continue

        date_dir  = aoi_dir / sdt[:10]              # YYYY-MM-DD
        scene_dir = date_dir / sid                  # one dir per scene; tiles go inside
        scene_json_path = scene_dir / "_scene.json"
        if scene_json_path.exists():
            seen.add(sid)
            skipped += 1
            continue

        log.info("fetch_scene", scene=sid, datetime=sdt, orbit=s.get("orbit_state"),
                 relative_orbit=s.get("relative_orbit"))
        try:
            tile_results = fetch_scene_tiled(token, s, bbox, args.width, args.height)
        except Exception as e:
            log.error("fetch_failed", scene=sid, error=str(e))
            continue

        scene_pu = sum(t.get("pu_spent") or 0.0 for t in tile_results)
        scene_bytes = sum(len(t["bytes"]) for t in tile_results if t.get("bytes"))
        n_kept = sum(1 for t in tile_results if not t.get("skipped"))
        n_skipped = sum(1 for t in tile_results if t.get("skipped"))
        pu_total += scene_pu

        scene_dir.mkdir(parents=True, exist_ok=True)
        tile_manifest = []
        for t in tile_results:
            tile_name = f"tile_r{t['row']}_c{t['col']}.tif"
            entry = {
                "row": t["row"], "col": t["col"],
                "bbox": list(t["bbox"]),
                "width": t["width"], "height": t["height"],
                "pu_spent": t.get("pu_spent"),
                "skipped": t.get("skipped"),
                "skip_reason": t.get("skip_reason"),
                "size_bytes": len(t["bytes"]) if t.get("bytes") else 0,
                "filename": tile_name if not t.get("skipped") else None,
            }
            tile_manifest.append(entry)
            if not t.get("skipped") and t.get("bytes"):
                (scene_dir / tile_name).write_bytes(t["bytes"])

        scene_json_path.write_text(json.dumps({
            "scene_id": sid,
            "datetime": sdt,
            "orbit_state": s.get("orbit_state"),
            "relative_orbit": s.get("relative_orbit"),
            "polarization": s.get("polarization"),
            "acquisition_mode": s.get("acquisition_mode"),
            "aoi_bbox": list(bbox),
            "total_width": args.width,
            "total_height": args.height,
            "tile_grid": {"rows": n_rows, "cols": n_cols, "max_dim": MAX_TILE_DIM},
            "tiles": tile_manifest,
            "pu_spent_total": scene_pu,
            "size_bytes_total": scene_bytes,
            "fetched_at_utc": datetime.now(tz=timezone.utc).isoformat(),
            "stac_properties": s.get("properties"),
        }, indent=2))
        log.info("scene_written", scene=sid, tiles_kept=n_kept, tiles_skipped=n_skipped,
                 pu=round(scene_pu, 2), size_bytes=scene_bytes)

        seen.add(sid)
        fetched += 1
        if not latest_dt or sdt > latest_dt:
            latest_dt = sdt

    state["scenes_seen"] = sorted(seen)
    state["last_processed_time"] = latest_dt or time_to
    _save_state(state_path, state)

    log.info("ingest_done", aoi=args.aoi_name, fetched=fetched, skipped=skipped,
             total_pu=round(pu_total, 2), state=str(state_path))
    return 0


def cmd_list(args) -> int:
    cid, csec = require_creds()
    token = get_token(cid, csec)
    scenes = catalog_search(token, tuple(args.bbox), args.time_from, args.time_to)
    log.info("catalog_result", count=len(scenes))
    for s in scenes:
        print(f"{s['datetime']}  {s.get('orbit_state','?'):>11}  rel_orbit={s.get('relative_orbit','?')}  {s['id']}")
    return 0


def cmd_fetch(args) -> int:
    cid, csec = require_creds()
    token = get_token(cid, csec)
    log.info("process_start", bbox=args.bbox, time_from=args.time_from, time_to=args.time_to,
             width=args.width, height=args.height)
    data, headers = fetch_s1_sigma0(
        token, tuple(args.bbox), args.time_from, args.time_to, args.width, args.height
    )
    pu = header_pu(headers)
    log.info("process_ok", bytes=len(data), pu_spent=pu)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(data)
    log.info("written", output=str(args.output), size_bytes=len(data))
    return 0


def main():
    parser = argparse.ArgumentParser(description="Sentinel-1 IW GRD via CDSE Sentinel Hub")
    subs = parser.add_subparsers(dest="cmd", required=True)

    pl = subs.add_parser("list", help="STAC catalog search; print scenes")
    pl.add_argument("--bbox", nargs=4, type=float, required=True,
                    metavar=("LON_MIN", "LAT_MIN", "LON_MAX", "LAT_MAX"))
    pl.add_argument("--from", dest="time_from", required=True)
    pl.add_argument("--to",   dest="time_to",   required=True)
    pl.set_defaults(func=cmd_list)

    pf = subs.add_parser("fetch", help="single Process API mosaic call")
    pf.add_argument("--bbox", nargs=4, type=float, required=True,
                    metavar=("LON_MIN", "LAT_MIN", "LON_MAX", "LAT_MAX"))
    pf.add_argument("--from", dest="time_from", required=True)
    pf.add_argument("--to",   dest="time_to",   required=True)
    pf.add_argument("--width",  type=int, default=512)
    pf.add_argument("--height", type=int, default=512)
    pf.add_argument("--output", required=True, type=Path)
    pf.set_defaults(func=cmd_fetch)

    pi = subs.add_parser("ingest", help="incremental catalog scan + per-scene fetch")
    pi.add_argument("--aoi-name", required=True)
    pi.add_argument("--bbox", nargs=4, type=float, required=True,
                    metavar=("LON_MIN", "LAT_MIN", "LON_MAX", "LAT_MAX"))
    pi.add_argument("--from", dest="time_from", default=None,
                    help="Override start time (default: state file's last_processed_time, or now-12d)")
    pi.add_argument("--to",   dest="time_to",   default=None,
                    help="Override end time (default: now)")
    pi.add_argument("--width",  type=int, required=True)
    pi.add_argument("--height", type=int, required=True)
    pi.add_argument("--output-dir", required=True, type=lambda p: Path(p).expanduser())
    pi.add_argument("--state-file", default=None, type=Path,
                    help="Override state file path (default: <output-dir>/<aoi-name>/_state.json)")
    pi.set_defaults(func=cmd_ingest)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
