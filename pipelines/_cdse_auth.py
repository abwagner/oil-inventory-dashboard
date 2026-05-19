"""CDSE OAuth — password-grant access tokens for DESP catalog + downloads.

The Sentinel Hub-registered client (`CDSE_CLIENT_ID` + `_SECRET`) only
mints tokens valid for catalog endpoints; downloads return HTTP 401
with `code=DAT-ZIP-609 ("Token audience not allowed")`. Programmatic
downloads need a token tied to user identity. CDSE documents the
Resource Owner Password Credentials grant via the public `cdse-public`
client for this — `pipelines/sentinel_s1_grd.py` and friends use this
module to get a fresh access token per pipeline invocation.

Token lifetime is ~10 minutes; we don't bother with refresh-token state
because each pipeline run is short and just mints a new token at startup.

Env vars (load via `_env.load_repo_env()`):
    CDSE_USERNAME  — your CDSE registration email
    CDSE_PASSWORD  — your CDSE password

See `docs/DESP_FREE_IMAGERY_PLAN.md` for the decision rationale and
`scripts/test_desp.py` for the original probe.
"""

from __future__ import annotations

import os

import requests

_TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu"
    "/auth/realms/CDSE/protocol/openid-connect/token"
)
_CLIENT_ID = "cdse-public"
_DEFAULT_TIMEOUT = 30


class CdseAuthError(RuntimeError):
    """Raised when CDSE auth can't be performed (missing env vars or
    rejected credentials). Catchable separately from generic RequestException
    so pipeline code can offer a config-specific error message."""


def get_access_token(timeout: int = _DEFAULT_TIMEOUT) -> str:
    """Return a fresh CDSE access token via password grant.

    Tokens are short-lived (~10 min); call this once at the start of a
    pipeline run and reuse the returned string across all CDSE requests
    in that run. For long-running processes that span >10 min, call
    again when a 401 surfaces — re-auth is cheap.

    Raises CdseAuthError when env vars are missing or auth fails. Lets
    the caller distinguish "I'm not configured" from "the network is
    flaky" — the latter still propagates as requests.HTTPError.
    """
    user = os.environ.get("CDSE_USERNAME")
    pwd = os.environ.get("CDSE_PASSWORD")
    if not user or not pwd:
        raise CdseAuthError(
            "CDSE_USERNAME / CDSE_PASSWORD missing from env. "
            "Add them to your .env (or whichever file _env.load_repo_env "
            "is reading) — see docs/DESP_FREE_IMAGERY_PLAN.md."
        )
    r = requests.post(
        _TOKEN_URL,
        data={
            "grant_type": "password",
            "client_id": _CLIENT_ID,
            "username": user,
            "password": pwd,
        },
        timeout=timeout,
    )
    if r.status_code == 401:
        # Most common cause: stale CDSE_PASSWORD after a CDSE-side reset.
        raise CdseAuthError(
            f"CDSE rejected the password grant (HTTP 401). Username/password "
            f"likely stale; sign in at https://dataspace.copernicus.eu to "
            f"verify, then update CDSE_PASSWORD in .env. Body: {r.text[:200]}"
        )
    r.raise_for_status()
    body = r.json()
    token = body.get("access_token")
    if not token:
        raise CdseAuthError(
            f"CDSE token endpoint returned 200 but no access_token: {body}"
        )
    return token


def authed_session(token: str | None = None) -> requests.Session:
    """Return a `requests.Session` with Authorization preset for CDSE.

    If `token` is None, mints one via `get_access_token()`. Useful when
    a pipeline makes many CDSE calls in sequence and wants connection
    pooling. NOTE: `requests` strips the Authorization header on
    cross-origin redirects (which CDSE's download endpoint triggers when
    it redirects to `zipper.dataspace.copernicus.eu`). For download
    flows, callers should follow redirects manually or use the helper
    in pipelines/sentinel_s1_grd.py.
    """
    if token is None:
        token = get_access_token()
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {token}"
    return s
