"""
Load environment variables from the repo-root .env.

By default reads `.env` from the repo root (two levels up from this file).
Override with the `ENV_FILE` environment variable to point at a different
file (e.g. a docker secret or a 1Password-rendered file in a parent repo).

Usage:
    from _env import load_repo_env
    load_repo_env()
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent


def env_file_path() -> Path:
    override = os.environ.get("ENV_FILE")
    if override:
        return Path(override).expanduser()
    return REPO_ROOT / ".env"


def load_repo_env() -> Path:
    """Load env vars from the repo-root .env (or ENV_FILE override).
    Returns the path used. Missing files are silently ignored — most variables
    can also come from the host environment, which is the case under Docker."""
    p = env_file_path()
    load_dotenv(p)
    return p


def sar_enabled() -> bool:
    """SAR ingest is enabled when CDSE creds are present and SAR_ENABLED is not
    explicitly set to a falsy value. Sentinel Hub PUs cost real money — see
    docs/WTI_Tanker_Forecast_TDD.md §4.2.2.1."""
    explicit = os.environ.get("SAR_ENABLED")
    if explicit is not None:
        return explicit.strip().lower() in ("1", "true", "yes", "on")
    return bool(os.environ.get("CDSE_CLIENT_ID") and os.environ.get("CDSE_CLIENT_SECRET"))


def aisstream_enabled() -> bool:
    """AIS ingest requires aisstream.io WebSocket key."""
    return bool(os.environ.get("AISSTREAM_API_KEY"))
