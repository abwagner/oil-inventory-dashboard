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
    """HTTP Range request for the first `n_bytes` of the product.

    DESP's download endpoint (`download.dataspace.copernicus.eu`) may
    redirect to `zipper.dataspace.copernicus.eu`; we follow redirects
    manually and re-attach Authorization each hop (requests strips it
    on cross-origin redirects).
    """
    url = DOWNLOAD_URL_TPL.format(id=product_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Range": f"bytes=0-{n_bytes - 1}",
    }
    redirects = 0
    while redirects < 5:
        r = requests.get(url, headers=headers, timeout=120,
                         stream=True, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            new_url = r.headers.get("Location")
            if not new_url:
                break
            url = new_url
            redirects += 1
            continue
        break

    content = b""
    err_body: str | None = None
    if r.status_code in (200, 206):
        for chunk in r.iter_content(chunk_size=65536):
            content += chunk
            if len(content) >= n_bytes:
                break
    elif r.status_code >= 400:
        try:
            err_body = r.text[:400]
        except Exception:
            err_body = "(non-text body)"
    return {
        "status": r.status_code,
        "content_length": int(r.headers.get("Content-Length", -1)),
        "content_type": r.headers.get("Content-Type"),
        "downloaded_bytes": len(content),
        "first_4_bytes_hex": content[:4].hex() if content else None,
        "redirects_followed": redirects,
        "final_url_host": url.split("/")[2] if "://" in url else None,
        "err_body": err_body,
        "www_authenticate": r.headers.get("WWW-Authenticate"),
    }


def get_token_password(
    username: str, password: str, scope: str | None = None,
) -> dict:
    """Resource Owner Password Credentials grant via cdse-public.

    If `scope="offline_access"` is passed and CDSE allows it, the returned
    refresh_token is long-lived (offline-type) rather than the default
    ~1-hour ephemeral refresh. Useful for setting up a pipeline that
    doesn't need the password sitting in .env permanently.
    """
    data = {
        "grant_type": "password",
        "client_id": "cdse-public",
        "username": username,
        "password": password,
    }
    if scope:
        data["scope"] = scope
    r = requests.post(TOKEN_URL, data=data, timeout=30)
    r.raise_for_status()
    return r.json()


def get_token_refresh(refresh_token: str) -> dict:
    """Refresh-token grant via cdse-public. Use this for subsequent auths
    after a one-time password grant — the refresh token can sit in .env
    and is easier to revoke than a password."""
    r = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": "cdse-public",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


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

    # Step 0.1 — auth. Three modes, in order of preference:
    #   (1) refresh-token grant via cdse-public  — needs CDSE_REFRESH_TOKEN
    #       in env, lowest-trust path (refresh tokens are easy to revoke).
    #   (2) password grant via cdse-public       — needs CDSE_USERNAME +
    #       CDSE_PASSWORD; emits a refresh_token you can save to .env to
    #       drop the password afterwards.
    #   (3) client_credentials via existing sh-* client — catalog only;
    #       downloads will 401 with "Token audience not allowed".
    refresh_token = os.environ.get("CDSE_REFRESH_TOKEN")
    username = os.environ.get("CDSE_USERNAME")
    password = os.environ.get("CDSE_PASSWORD")
    auth_mode = None
    new_refresh: str | None = None
    if refresh_token:
        print("[1] Authenticating with CDSE OAuth (refresh_token grant via cdse-public)…")
        try:
            t = get_token_refresh(refresh_token)
            token = t["access_token"]
            new_refresh = t.get("refresh_token")
            auth_mode = "refresh_token"
        except requests.HTTPError as e:
            print(f"  FAIL  refresh grant: HTTP {e.response.status_code}: {e.response.text[:200]}")
            return 1
        print(f"  OK    token acquired via refresh_token (len={len(token)})")
        if new_refresh and new_refresh != refresh_token:
            print("  NOTE  refresh token rotated — update CDSE_REFRESH_TOKEN in .env if you "
                  "want to keep using the new one for subsequent runs.")
    elif username and password:
        print("[1] Authenticating with CDSE OAuth (password grant via cdse-public)…")
        try:
            t = get_token_password(username, password)
            token = t["access_token"]
            new_refresh = t.get("refresh_token")
            auth_mode = "password"
        except requests.HTTPError as e:
            print(f"  FAIL  password grant: HTTP {e.response.status_code}: {e.response.text[:200]}")
            return 1
        print(f"  OK    token acquired via password grant (len={len(token)})")
        if new_refresh:
            print(f"  NOTE  refresh_token returned (len={len(new_refresh)}). To switch to refresh-token "
                  "auth (and drop CDSE_PASSWORD from .env), save this value as CDSE_REFRESH_TOKEN.")
            print(f"        CDSE_REFRESH_TOKEN={new_refresh}")
    else:
        print("[1] Authenticating with CDSE OAuth (client_credentials)…")
        print("    NOTE: no CDSE_REFRESH_TOKEN / CDSE_USERNAME+PASSWORD in env — downloads")
        print("          will 401. Set CDSE_USERNAME + CDSE_PASSWORD once, then save the")
        print("          emitted refresh_token as CDSE_REFRESH_TOKEN going forward.")
        try:
            token = get_token()
            auth_mode = "client_credentials"
        except requests.HTTPError as e:
            print(f"  FAIL  auth: HTTP {e.response.status_code}: {e.response.text[:300]}")
            return 1
        except Exception as e:
            print(f"  FAIL  auth: {type(e).__name__}: {e}")
            return 1
        print(f"  OK    token acquired via client_credentials (len={len(token)})")
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
            print(f"  OK    HTTP {dl['status']}, got {fmt_size(dl['downloaded_bytes'])} (Content-Length={fmt_size(dl['content_length'])}, Content-Type={dl['content_type']}, magic={dl['first_4_bytes_hex']}, redirects={dl['redirects_followed']}, host={dl['final_url_host']})")
        else:
            print(f"  FAIL  HTTP {dl['status']}, downloaded {fmt_size(dl['downloaded_bytes'])}  (redirects={dl['redirects_followed']}, host={dl['final_url_host']})")
            if dl.get("www_authenticate"):
                print(f"        WWW-Authenticate: {dl['www_authenticate']}")
            if dl.get("err_body"):
                print(f"        body: {dl['err_body']}")
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
