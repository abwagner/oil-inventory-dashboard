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


# ---------------------------------------------------------------------------
# Storage backend
# ---------------------------------------------------------------------------
#
# The dashboard supports two storage modes for parquet / SAR raster files:
#
#   1. Local filesystem (default) — DATA_DIR points to a directory on disk.
#      Used for laptop dev and the simplest single-machine deploy.
#
#   2. S3-compatible (MinIO, AWS, etc.) — S3_BUCKET names the bucket; pandas /
#      pyarrow / rasterio see s3:// URLs and read directly via fsspec/s3fs.
#      Set when both S3_BUCKET and AWS_ACCESS_KEY_ID are present.
#      AWS_ENDPOINT_URL points at MinIO when self-hosting.
#
# In either mode the sqlite EIA database stays on local disk (sqlite is not a
# good fit for object storage). EIA_DB_PATH always resolves to a local path.
# ---------------------------------------------------------------------------


def s3_enabled() -> bool:
    """True when S3-compatible storage is configured. Reads/writes for parquet
    + SAR rasters route through fsspec; sqlite stays local regardless."""
    return bool(
        os.environ.get("S3_BUCKET")
        and os.environ.get("AWS_ACCESS_KEY_ID")
        and os.environ.get("AWS_SECRET_ACCESS_KEY")
    )


def storage_root() -> str:
    """Root URI for non-sqlite storage. 's3://<bucket>' when s3_enabled() else
    DATA_DIR (local path string)."""
    if s3_enabled():
        return f"s3://{os.environ['S3_BUCKET']}"
    return str(Path(os.environ.get("DATA_DIR", str(REPO_ROOT / "data"))))


def data_uri(*parts: str) -> str:
    """Build a URI under storage_root() from path components.

    Both forms returned here are accepted by pandas, pyarrow, and rasterio
    (when fsspec / s3fs / boto3 are installed)."""
    root = storage_root()
    if not parts:
        return root
    if root.startswith("s3://"):
        suffix = "/".join(p.strip("/\\") for p in parts)
        return f"{root}/{suffix}"
    return str(Path(root, *parts))


def storage_fs():
    """Return the fsspec filesystem matching storage_root().

    Used for existence checks, listing, mtime — operations that don't have a
    clean local-vs-S3 native API otherwise. Read/write of file contents should
    just pass data_uri() to pandas / pyarrow / rasterio directly."""
    import fsspec  # local import — fsspec is only needed when this is called

    if s3_enabled():
        return fsspec.filesystem(
            "s3",
            key=os.environ.get("AWS_ACCESS_KEY_ID"),
            secret=os.environ.get("AWS_SECRET_ACCESS_KEY"),
            client_kwargs={"endpoint_url": os.environ["AWS_ENDPOINT_URL"]}
            if os.environ.get("AWS_ENDPOINT_URL")
            else {},
        )
    return fsspec.filesystem("file")


def db_path() -> Path:
    """Local sqlite database path. Always local — sqlite doesn't run on S3."""
    override = os.environ.get("EIA_DB_PATH")
    if override:
        return Path(override)
    base = Path(os.environ.get("DATA_DIR", str(REPO_ROOT / "data")))
    return base / "eia-dashboard" / "eia_data.db"
