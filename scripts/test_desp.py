#!/usr/bin/env python3
"""DESP capability probe — Step 0 of docs/DESP_FREE_IMAGERY_PLAN.md.

Confirms we can:
  1. Auth against Copernicus Data Space Ecosystem (DESP) using the existing
     CDSE_CLIENT_ID / CDSE_CLIENT_SECRET creds.
  2. Search the OData catalog for Sentinel-1 IW GRD + Sentinel-2 L2A products
     over the Persian Gulf AOI in the last 14 days.
  3. Range-download the first 1 MB of one product per collection.

No PUs are spent (DESP catalog + product download is free; PUs only apply to
Sentinel Hub's Process API). No data is persisted.

Run from the repo root:
    cd ~/GitHub/oil-inventory-dashboard
    uv run python scripts/test_desp.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

# Reuse the dashboard's env loader so CDSE creds come from the same .env
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipelines"))
from _env import load_repo_env  # noqa: E402

load_repo_env()

TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
CATALOG_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
DOWNLOAD_URL_TPL = "https://download.dataspace.copernicus.eu/odata/v1/Products({id})/$value"

# Persian Gulf AOI — matches scheduler.AOIS[0]
AOI_BBOX = (54.0, 24.0, 60.0, 28.0)   # (lon_min, lat_min, lon_max, lat_max)
LOOKBACK_DAYS = 14


# ─── Auth ────────────────────────────────────────────────────────────────


def get_token() -> str:
    cid = os.environ.get("CDSE_CLIENT_ID")
    csec = os.environ.get("CDSE_CLIENT_SECRET")
    if not cid or not csec:
        raise RuntimeError(
            "CDSE_CLIENT_ID / CDSE_CLIENT_SECRET missing from env. "
            "Add them to .env or export before running."
        )
    r = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": cid,
            "client_secret": csec,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


# ─── Catalog search ──────────────────────────────────────────────────────


def _bbox_to_polygon(bbox: tuple[float, float, float, float]) -> str:
    """OData wants WKT POLYGON with (lon lat) pairs, closing back to start."""
    lon_min, lat_min, lon_max, lat_max = bbox
    return (
        f"POLYGON(({lon_min} {lat_min},{lon_max} {lat_min},"
        f"{lon_max} {lat_max},{lon_min} {lat_max},{lon_min} {lat_min}))"
    )


def search_products(
    token: str, collection: str, name_filter: str,
    bbox: tuple[float, float, float, float], lookback_days: int,
    extra_filter: str = "",
    top: int = 10,
) -> list[dict]:
    """Query OData for products matching collection + name pattern + bbox
    + time window. Returns the JSON `value` array (one entry per product)."""
    cutoff = (datetime.now(tz=timezone.utc)
              - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    poly = _bbox_to_polygon(bbox)
    f = (
        f"Collection/Name eq '{collection}'"
        f" and contains(Name,'{name_filter}')"
        f" and OData.CSC.Intersects(area=geography'SRID=4326;{poly}')"
        f" and ContentDate/Start gt {cutoff}"
    )
    if extra_filter:
        f += " and " + extra_filter
    url = (
        f"{CATALOG_URL}?$filter={quote(f)}"
        f"&$orderby=ContentDate/Start desc"
        f"&$top={top}"
    )
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    r.raise_for_status()
    return r.json().get("value", [])


# ─── Range download (1 MB partial) ───────────────────────────────────────


def partial_download(token: str, product_id: str, n_bytes: int = 1_048_576) -> dict:
    """HTTP Range request for the first `n_bytes` of the product. Returns
    {status, content_length, content_type, sample_bytes} — no disk write."""
    url = DOWNLOAD_URL_TPL.format(id=product_id)
    r = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Range": f"bytes=0-{n_bytes - 1}",
        },
        timeout=120,
        stream=True,
        allow_redirects=True,
    )
    # We don't .raise_for_status() — 206 is the expected case
    content = b""
    if r.status_code in (200, 206):
        # Pull exactly the bytes requested
        for chunk in r.iter_content(chunk_size=65536):
            content += chunk
            if len(content) >= n_bytes:
                break
    return {
        "status": r.status_code,
        "content_length": int(r.headers.get("Content-Length", -1)),
        "content_type": r.headers.get("Content-Type"),
        "downloaded_bytes": len(content),
        "first_4_bytes_hex": content[:4].hex() if content else None,
    }


# ─── Main ────────────────────────────────────────────────────────────────


def fmt_size(b: int | None) -> str:
    if b is None:
        return "?"
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if b >= scale:
            return f"{b / scale:.2f} {unit}"
    return f"{b} B"


def main() -> int:
    print("─── DESP capability test ──────────────────────────────────────")
    print(f"AOI: Persian Gulf bbox {AOI_BBOX}")
    print(f"Lookback: {LOOKBACK_DAYS} days")
    print()

    # Step 0.1 — auth
    print("[1] Authenticating with CDSE OAuth (client_credentials)…")
    try:
        token = get_token()
    except requests.HTTPError as e:
        print(f"  FAIL  auth: HTTP {e.response.status_code}")
        print(f"        body: {e.response.text[:300]}")
        return 1
    except Exception as e:
        print(f"  FAIL  auth: {type(e).__name__}: {e}")
        return 1
    print(f"  OK    token acquired (len={len(token)})")
    print()

    overall_ok = True

    # Step 0.2 — search both collections
    for collection, name_filter, extra in [
        ("SENTINEL-1", "GRD", "contains(Name,'IW')"),
        # S2 L2A is the atmospherically-corrected level; filter cloud cover via Attributes.
        ("SENTINEL-2", "MSIL2A",
         "Attributes/OData.CSC.DoubleAttribute/any(att:att/Name eq 'cloudCover' and att/OData.CSC.DoubleAttribute/Value lt 30.0)"),
    ]:
        print(f"[2] Catalog search: {collection} (filter Name~'{name_filter}', extra: {extra[:60]}{'…' if len(extra)>60 else ''})")
        try:
            products = search_products(
                token, collection, name_filter, AOI_BBOX,
                LOOKBACK_DAYS, extra_filter=extra, top=5,
            )
        except requests.HTTPError as e:
            print(f"  FAIL  search: HTTP {e.response.status_code}")
            print(f"        body: {e.response.text[:300]}")
            overall_ok = False
            print()
            continue
        except Exception as e:
            print(f"  FAIL  search: {type(e).__name__}: {e}")
            overall_ok = False
            print()
            continue

        print(f"  OK    {len(products)} product(s) returned (top 5 shown)")
        if not products:
            print(f"  WARN  no products in last {LOOKBACK_DAYS} days for this collection over Persian Gulf")
            overall_ok = False
            print()
            continue
        for p in products:
            size = p.get("ContentLength")
            print(f"        - {p.get('Name')}  ({fmt_size(size)})  start={p.get('ContentDate', {}).get('Start')}  id={p.get('Id')}")

        # Step 0.3 — partial download of the first product
        first = products[0]
        print(f"[3] Partial download (1 MB Range request) of {first.get('Name')}…")
        try:
            dl = partial_download(token, first["Id"], n_bytes=1_048_576)
        except Exception as e:
            print(f"  FAIL  download: {type(e).__name__}: {e}")
            overall_ok = False
            print()
            continue

        if dl["status"] in (200, 206) and dl["downloaded_bytes"] >= 1_000_000:
            print(f"  OK    HTTP {dl['status']}, got {fmt_size(dl['downloaded_bytes'])} (Content-Length={fmt_size(dl['content_length'])}, Content-Type={dl['content_type']}, magic={dl['first_4_bytes_hex']})")
        else:
            print(f"  FAIL  HTTP {dl['status']}, downloaded {fmt_size(dl['downloaded_bytes'])}")
            overall_ok = False
        print()

    print("─── result ────────────────────────────────────────────────────")
    if overall_ok:
        print("PASS — DESP capability confirmed for both Sentinel-1 GRD + Sentinel-2 L2A.")
        return 0
    print("FAIL — see errors above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
