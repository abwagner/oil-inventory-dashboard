## oil-inventory-dashboard + scheduler image.
##
## Build:   docker build -t oil-inventory-dashboard .
## Run:     docker run -p 8050:8050 \
##              -v <data>:/data \
##              --env-file <your.env> \
##              oil-inventory-dashboard
##
## Configuration is via env vars (see .env.example). To mount an .env file
## instead, set ENV_FILE to its in-container path:
##   docker run ... -v /host/path/.env:/secrets/.env:ro -e ENV_FILE=/secrets/.env ...
##
## Two entrypoints in this image — pick via docker-compose `command`:
##   uvicorn (default): serves the FastAPI dashboard on :8050.
##   scheduler:         runs APScheduler with the AIS / SAR jobs.

FROM python:3.12-slim

# rasterio's manylinux wheel bundles GDAL — no system gdal package required.
# Just ca-certificates (HTTPS to API providers) and tzdata (cron-style schedules).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY pipelines  /app/pipelines
COPY templates  /app/templates
COPY app.py scheduler.py /app/

EXPOSE 8050

# Default: dashboard. The scheduler container in docker-compose overrides this
# with `command: ["python", "scheduler.py"]`.
RUN mkdir -p /etc/tanker
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8050", "--no-access-log"]
