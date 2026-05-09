#!/usr/bin/env bash
# Run sentinel_sar.py ingest + sar_detect + sar_aggregate for all configured AOIs.
# Idempotent — the ingest step skips scenes already on disk.
#
# Defaults to <repo>/data/sentinel_sar for output. Override via env vars:
#   REPO        path to repo (default: directory containing this script's parent)
#   PYTHON      python interpreter (default: $REPO/.venv/bin/python or `python3`)
#   DATA_DIR    data root (default: $REPO/data)
#   OUTPUT_DIR  SAR output dir (default: $DATA_DIR/sentinel_sar)
#   SINCE/UNTIL ISO-8601 UTC window (default: last 3 days)

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
REPO=${REPO:-$(dirname "$SCRIPT_DIR")}

if [[ -x "$REPO/.venv/bin/python" ]]; then
    PYTHON=${PYTHON:-$REPO/.venv/bin/python}
else
    PYTHON=${PYTHON:-python3}
fi

PIPELINES=$REPO/pipelines
DATA_DIR=${DATA_DIR:-$REPO/data}
OUTPUT_DIR=${OUTPUT_DIR:-$DATA_DIR/sentinel_sar}

# 3-day lookback handles missed runs gracefully; the state file suppresses redo.
SINCE=${SINCE:-$(date -u -d "3 days ago" +%Y-%m-%dT%H:%M:%SZ)}
UNTIL=${UNTIL:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}

mkdir -p "$OUTPUT_DIR"

echo "[$(date -u +%FT%TZ)] sar-ingest-all start  since=$SINCE  until=$UNTIL  output=$OUTPUT_DIR"

run_aoi() {
    local name=$1 lon_min=$2 lat_min=$3 lon_max=$4 lat_max=$5 width=$6 height=$7
    echo "[$(date -u +%FT%TZ)] === $name ==="
    "$PYTHON" "$PIPELINES/sentinel_sar.py" ingest \
        --aoi-name "$name" \
        --bbox "$lon_min" "$lat_min" "$lon_max" "$lat_max" \
        --from "$SINCE" --to "$UNTIL" \
        --width "$width" --height "$height" \
        --output-dir "$OUTPUT_DIR"
    "$PYTHON" "$PIPELINES/sar_detect.py"   --scene-dir "$OUTPUT_DIR/$name"
    "$PYTHON" "$PIPELINES/sar_aggregate.py" --scene-dir "$OUTPUT_DIR/$name"
}

# AOI table — name lon_min lat_min lon_max lat_max width height
# Output res ~88 m/px at 26°N latitude (4 px per VLCC).
run_aoi  persian_gulf_oman   54.0  24.0   60.0  28.0  5000  3333
run_aoi  usgc               -98.0  26.0  -88.0  31.0  8000  4000

echo "[$(date -u +%FT%TZ)] sar-ingest-all done"
