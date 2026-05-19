#!/usr/bin/env python3
"""One-shot probe: does CDSE issue long-lived refresh tokens under
scope=offline_access? Run AFTER the main test_desp.py is passing.

If CDSE allows it: the printed refresh token will have a much later
`exp` claim (typically days+ rather than 1 hour) and can be stored in
.env as CDSE_REFRESH_TOKEN to drop CDSE_PASSWORD.

If CDSE rejects it: we keep CDSE_USERNAME + CDSE_PASSWORD in .env and
password-grant on each pipeline invocation (no token state to manage).
"""

from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipelines"))
from _env import load_repo_env  # noqa: E402

load_repo_env()

import requests  # noqa: E402

TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"


def decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    pad = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(pad))
    except Exception:
        return {}


def main() -> int:
    user = os.environ.get("CDSE_USERNAME")
    pwd = os.environ.get("CDSE_PASSWORD")
    if not (user and pwd):
        print("Need CDSE_USERNAME + CDSE_PASSWORD in env.")
        return 1

    for scope in (None, "openid", "openid offline_access"):
        print(f"\n─── scope={scope!r} ───")
        data = {
            "grant_type": "password",
            "client_id": "cdse-public",
            "username": user, "password": pwd,
        }
        if scope:
            data["scope"] = scope
        r = requests.post(TOKEN_URL, data=data, timeout=30)
        if r.status_code != 200:
            print(f"  FAIL  HTTP {r.status_code}: {r.text[:200]}")
            continue
        body = r.json()
        rt = body.get("refresh_token", "")
        claims = decode_jwt_payload(rt)
        iat = claims.get("iat")
        exp = claims.get("exp")
        rt_typ = claims.get("typ")
        rt_scope = claims.get("scope", "")
        if iat and exp:
            seconds = exp - iat
            human = f"{seconds}s ≈ {seconds/3600:.1f}h ≈ {seconds/86400:.1f}d"
            iat_iso = datetime.fromtimestamp(iat, tz=timezone.utc).isoformat()
            exp_iso = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
        else:
            seconds = None
            human = "(no exp claim — possibly never expires)"
            iat_iso = exp_iso = "?"
        print(f"  OK    refresh_token typ={rt_typ!r}  lifetime={human}")
        print(f"        iat={iat_iso}  exp={exp_iso}")
        print(f"        scope on rt: {rt_scope}")
        # Don't print the actual token here — assume we're echoing into a
        # terminal that may be captured.
        print(f"        refresh_token length: {len(rt)} chars (redacted)")

    print("\nIf any scope yields a multi-day or no-expiry refresh token, that's the")
    print("one to use for CDSE_REFRESH_TOKEN. Otherwise stick with password-on-disk.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
