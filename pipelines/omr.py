"""
IEA Oil Market Report (OMR) — Tables 1, 1a, 1b parser.

Pulls the monthly free abridged OMR PDF, runs `pdftotext -layout`, and parses
the three opening world supply/demand tables:

  - Table 1:  WORLD OIL SUPPLY AND DEMAND (base balance, levels)
  - Table 1a: Changes from last month's Table 1 (revisions, deltas)
  - Table 1b: World Oil Production w/ OPEC+ agreement applied

Each table has the same column anchors (a mix of annual `2024` and quarterly
`1Q24` labels) and the same section layout (OECD DEMAND / NON-OECD DEMAND /
OECD SUPPLY / NON-OECD SUPPLY / OPEC / STOCK CHANGES & MISC / memo items).

The free OMR PDF availability is **conditional** — IEA has historically only
published free abridged versions during specific events (Middle East tensions,
etc.). The pipeline degrades cleanly: if `--url` 404s, exits nonzero and the
dashboard renders an empty-state card.

Usage:
    # Auto-discover the latest free PDF (scrapes iea.org)
    python omr.py

    # Explicit URL (find it on iea.org/reports/oil-market-report-<month>-<year>)
    python omr.py --url https://iea.blob.core.windows.net/assets/.../-XYZ.pdf

    # Local file (manually downloaded; useful for OMR subscribers too)
    python omr.py --local-pdf /path/to/OilMarketReport.pdf

    # Specify report_date explicitly when auto-detection from filename fails
    python omr.py --local-pdf foo.pdf --report-date 2026-04-14

Output (sqlite):
    Inserts into `omr_monthly` table:
        (report_date, table_id, section, row_label, period, period_type, value)
    PK is (report_date, table_id, section, row_label, period) — successive
    issues accumulate so revisions can be inspected later.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests

from _env import db_path, load_repo_env

load_repo_env()

log = logging.getLogger("omr")

# Browser UA — iea.org and the blob CDN return 403 to default `requests` UA.
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
MONTH_ABBR_TO_NUM = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# ---------------------------------------------------------------------------
# Discovery — find the latest free PDF on iea.org
# ---------------------------------------------------------------------------

# Matches the blob-CDN URL we want to extract from the report HTML page.
# Filename forms seen so far:
#   -14APR2026_OilMarketReport_Free_version1.pdf  (April 2026, "exceptional free")
#   -12MAR2026_OilMarketReport.pdf                (March 2026)
_BLOB_URL_RE = re.compile(
    r"https://iea\.blob\.core\.windows\.net/assets/[^\"'\s>]+OilMarketReport[^\"'\s>]*\.pdf",
    re.IGNORECASE,
)

# Filename date prefix, e.g. "-14APR2026_OilMarketReport" -> (14, APR, 2026)
_FILENAME_DATE_RE = re.compile(
    r"-(\d{1,2})([A-Z]{3})(\d{4})_OilMarketReport",
    re.IGNORECASE,
)


def candidate_report_urls(today: datetime | None = None) -> list[str]:
    """Probe the current month first, then walk back 4 prior months.

    IEA publishes the report mid-month; if today is before the release we want
    to still find last month's edition. Walking back through prior months also
    makes the call robust if the most recent month wasn't published free.
    """
    today = today or datetime.now(tz=timezone.utc)
    out = []
    y, m = today.year, today.month
    for _ in range(5):
        out.append(
            f"https://www.iea.org/reports/oil-market-report-{MONTH_NAMES[m - 1]}-{y}"
        )
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return out


def fetch_html(url: str, timeout: int = 30) -> str | None:
    """GET an iea.org HTML page with a browser UA. Returns None on 4xx/5xx."""
    try:
        resp = requests.get(
            url, headers={"User-Agent": _UA}, timeout=timeout, allow_redirects=True
        )
    except requests.RequestException as e:
        log.warning("html_fetch_failed url=%s error=%s", url, e)
        return None
    if resp.status_code != 200:
        log.info("html_not_200 url=%s status=%d", url, resp.status_code)
        return None
    return resp.text


def find_latest_pdf_url() -> str | None:
    """Walk back through monthly report URLs until one yields a PDF link."""
    for url in candidate_report_urls():
        html = fetch_html(url)
        if html is None:
            continue
        m = _BLOB_URL_RE.search(html)
        if m:
            log.info("found_pdf_url page=%s pdf=%s", url, m.group())
            return m.group()
        log.info("no_pdf_link page=%s", url)
    return None


# ---------------------------------------------------------------------------
# Download + text extraction
# ---------------------------------------------------------------------------


def download_pdf(url: str, timeout: int = 60) -> bytes:
    """Download a PDF with a browser UA. Raises on non-200."""
    log.info("downloading pdf url=%s", url)
    resp = requests.get(
        url, headers={"User-Agent": _UA}, timeout=timeout, allow_redirects=True
    )
    resp.raise_for_status()
    return resp.content


def pdf_to_text(pdf_bytes: bytes) -> str:
    """Run `pdftotext -layout` (from poppler-utils) on the PDF bytes.

    -layout preserves column structure which is critical for our position-based
    parser. The text is returned as a single string with newline separators.
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        tmp = f.name
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", tmp, "-"],
            capture_output=True, check=True, timeout=60,
        )
        return out.stdout.decode("utf-8", errors="replace")
    finally:
        os.unlink(tmp)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

# Period token: `2024`, `1Q24`, `4Q26`, etc. Anchored on word boundaries.
_PERIOD_RE = re.compile(r"\b([1-4]Q\d{2}|\d{4})\b")

# Numeric value (signed decimal): -0.4, 33.8, 100.5
_VALUE_RE = re.compile(r"-?\d+\.\d+")

# Table-section anchors. Match in order; the first one to occur on a line
# updates the current section context for subsequent data rows.
_SECTIONS = {
    "OECD DEMAND",
    "NON-OECD DEMAND",
    "OECD SUPPLY",
    "NON-OECD SUPPLY",
    "OPEC",
    "OPEC+",
    "STOCK CHANGES AND MISCELLANEOUS",
    "Reported OECD",
    "Memo items:",
    "WORLD OIL PRODUCTION",
}

# Anchor regexes that mark the start of each table block. We greedily consume
# data rows after each anchor until the next anchor or a recognised end-marker.
_TABLE_ANCHORS = {
    "1":  re.compile(r"^\s*Table 1\s*$"),
    "1a": re.compile(r"^\s*Table 1a\s*$"),
    "1b": re.compile(r"^\s*Table 1b"),
}

# End-of-table markers (footnote anchors, descriptive prose, next-page header).
_END_OF_TABLE = re.compile(
    r"^\s*("
    r"\d+\s+(Measured|Comprises|Net|OPEC includes|Includes|Total demand)"
    r"|For the purpose of"
    r"|Note: When submitting"
    r"|PAGE\s*\|"
    r"|Oil Market Report\s+Tables"
    r")",
    re.IGNORECASE,
)


def parse_periods_from_header(line: str) -> list[tuple[str, int, int]]:
    """Find period labels in `line` and return [(period, start, end), ...].

    `start` and `end` are byte offsets in the original line.
    """
    return [(m.group(), m.start(), m.end()) for m in _PERIOD_RE.finditer(line)]


def p_type(period: str) -> str:
    return "quarter" if "Q" in period else "annual"


def parse_data_row(line: str, columns: list[str]) -> dict[str, float]:
    """Zip numeric tokens in `line` to column labels (index-based, left-aligned).

    `pdftotext -layout` squashes the OMR period header so its character offsets
    don't line up with the (widely-spaced) value columns underneath. The actual
    contract is simpler: values appear in the same order as headers, and any
    missing values are trailing (e.g. OPEC supply has no values past 1Q26).

    Returns {period: value}. Values past len(columns) — a structural surprise —
    are dropped with a warning.
    """
    values = [float(m.group()) for m in _VALUE_RE.finditer(line)]
    if len(values) > len(columns):
        log.warning(
            "row_has_more_values_than_columns line=%r values=%d cols=%d",
            line.strip()[:80], len(values), len(columns),
        )
        values = values[: len(columns)]
    return {columns[i]: v for i, v in enumerate(values)}


# Footnote suffix: row labels often end with a digit superscript (rendered as
# a regular digit in the PDF text), e.g. "Total OECD2", "Total OPEC4",
# "Call on OPEC crude + Stock ch.6". We strip ONE trailing digit after a
# letter/paren/period (single-digit footnotes only — guards against eating
# real numeric content elsewhere). The `:` on "Memo items:" is preserved.
_FOOTNOTE_SUFFIX_RE = re.compile(r"(?<=[A-Za-z\)\.])\d$")


def clean_row_label(label: str) -> str:
    label = label.strip()
    label = _FOOTNOTE_SUFFIX_RE.sub("", label)
    return label.strip()


def label_and_remainder(line: str, first_value_col: int) -> tuple[str, str]:
    """Split `line` into (row_label, value_region) at `first_value_col`.

    Footnote-marker digits sitting at the end of row labels would otherwise
    get scooped by the value-parsing regex. Keeping them in the label half
    lets clean_row_label() drop them safely.
    """
    cut = max(0, first_value_col - 6)
    return line[:cut].rstrip(), line[cut:]


def looks_like_section(line: str, first_value_col: int) -> bool:
    """A section header has letters in the label region but NO numeric values
    in the value region."""
    label, rest = label_and_remainder(line, first_value_col)
    if not label:
        return False
    if _VALUE_RE.search(rest):
        return False
    return any(ch.isalpha() for ch in label)


def parse_one_table(
    lines: list[str], anchor_idx: int, table_id: str,
) -> tuple[list[dict], int]:
    """Parse rows for the table whose anchor line is `lines[anchor_idx]`.

    Returns (records, next_idx) where next_idx is one past the last consumed
    line so the caller can keep walking.

    Walks forward looking for the column-header row (contains period tokens),
    then consumes data rows until we hit a recognised end-of-table marker.
    """
    n = len(lines)
    # 1) Find the header row (contains a recognisable period like `2022` or `1Q24`)
    i = anchor_idx + 1
    columns: list[str] = []
    first_value_col: int = 0
    while i < n:
        periods = parse_periods_from_header(lines[i])
        # Require >= 3 period tokens to disambiguate from prose
        if len(periods) >= 3:
            columns = [p for p, _, _ in periods]
            first_value_col = periods[0][1]
            break
        i += 1
    if not columns:
        log.warning("no_header_found table=%s anchor=%d", table_id, anchor_idx)
        return [], anchor_idx + 1

    # 2) Walk data rows
    records: list[dict] = []
    current_section = ""
    i += 1
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        # Stop at end-of-table markers (footnotes etc.)
        if _END_OF_TABLE.match(line):
            break
        # Stop at the start of the next table
        if any(anchor.match(line) for anchor in _TABLE_ANCHORS.values()):
            break

        label, rest = label_and_remainder(line, first_value_col)
        if not label and not rest.strip():
            i += 1
            continue

        # Section header? Update context, don't emit data
        if looks_like_section(line, first_value_col):
            stripped = label.strip()
            # Section heuristic: matches a known section name OR is mostly uppercase
            if (
                stripped in _SECTIONS
                or stripped.upper() == stripped
                or stripped.endswith(":")
            ):
                current_section = stripped
                i += 1
                continue
            # Otherwise it's an unexpected blank-value row — log + skip
            log.debug("skipping_orphan_row table=%s label=%r", table_id, label)
            i += 1
            continue

        # Data row
        row_label = clean_row_label(label)
        if not row_label:
            i += 1
            continue
        period_values = parse_data_row(rest, columns)
        for period, value in period_values.items():
            records.append({
                "table_id": table_id,
                "section": current_section,
                "row_label": row_label,
                "period": period,
                "period_type": p_type(period),
                "value": value,
            })
        i += 1

    return records, i


def parse_tables(text: str) -> list[dict]:
    """Parse Tables 1, 1a, 1b from the OMR PDF text.

    Returns a flat list of records ready to upsert into omr_monthly.
    """
    lines = text.splitlines()
    n = len(lines)
    out: list[dict] = []
    i = 0
    seen: set[str] = set()  # parse each table once even if anchor recurs
    while i < n:
        line = lines[i]
        matched_table = None
        for tid, anchor in _TABLE_ANCHORS.items():
            if tid in seen:
                continue
            if anchor.match(line):
                matched_table = tid
                break
        if matched_table is None:
            i += 1
            continue
        records, next_i = parse_one_table(lines, i, matched_table)
        if records:
            seen.add(matched_table)
            out.extend(records)
            log.info("parsed_table id=%s rows=%d", matched_table, len(records))
        i = next_i
    return out


# ---------------------------------------------------------------------------
# Report date inference
# ---------------------------------------------------------------------------


def report_date_from_url(url: str) -> str | None:
    """Extract YYYY-MM-DD from the OMR PDF filename, if possible."""
    m = _FILENAME_DATE_RE.search(url)
    if not m:
        return None
    day = int(m.group(1))
    mon = MONTH_ABBR_TO_NUM.get(m.group(2).upper())
    yr = int(m.group(3))
    if not mon:
        return None
    return f"{yr:04d}-{mon:02d}-{day:02d}"


def report_date_from_pdf_meta(pdf_bytes: bytes) -> str | None:
    """Fall back to the PDF's CreationDate when filename parsing fails."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        tmp = f.name
    try:
        out = subprocess.run(
            ["pdfinfo", tmp], capture_output=True, check=True, timeout=30,
        )
        for line in out.stdout.decode("utf-8", errors="replace").splitlines():
            if line.startswith("CreationDate:"):
                # e.g. "CreationDate:    Mon Apr 13 14:52:04 2026 EDT"
                parts = line.split(None, 1)[1].strip()
                try:
                    dt = datetime.strptime(parts.rsplit(" ", 1)[0],
                                           "%a %b %d %H:%M:%S %Y")
                    return dt.strftime("%Y-%m-%d")
                except ValueError:
                    return None
    finally:
        os.unlink(tmp)
    return None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


SCHEMA = """
CREATE TABLE IF NOT EXISTS omr_monthly (
    report_date TEXT NOT NULL,
    table_id    TEXT NOT NULL,
    section     TEXT NOT NULL,
    row_label   TEXT NOT NULL,
    period      TEXT NOT NULL,
    period_type TEXT NOT NULL,
    value       REAL,
    PRIMARY KEY (report_date, table_id, section, row_label, period)
);
CREATE INDEX IF NOT EXISTS idx_omr_report_date ON omr_monthly(report_date);
CREATE INDEX IF NOT EXISTS idx_omr_table_period ON omr_monthly(table_id, period);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def upsert_records(
    conn: sqlite3.Connection, report_date: str, records: Iterable[dict],
) -> int:
    rows = [
        (report_date, r["table_id"], r["section"], r["row_label"],
         r["period"], r["period_type"], r["value"])
        for r in records
    ]
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO omr_monthly "
        "(report_date, table_id, section, row_label, period, period_type, value) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ap = argparse.ArgumentParser(description="Pull IEA OMR Tables 1/1a/1b")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--url", help="Direct PDF URL (skips auto-discovery)")
    g.add_argument("--local-pdf", help="Local PDF path (skips download)")
    ap.add_argument("--report-date", help="Override report_date (YYYY-MM-DD)")
    ap.add_argument("--db-path", help="Sqlite DB path (default: same as dashboard)")
    args = ap.parse_args()

    # ---- Acquire the PDF ----
    pdf_bytes: bytes
    source: str
    if args.local_pdf:
        pdf_bytes = Path(args.local_pdf).read_bytes()
        source = args.local_pdf
    else:
        url = args.url or os.environ.get("OMR_PDF_URL")
        if not url:
            url = find_latest_pdf_url()
        if not url:
            log.error(
                "no_url_resolved hint=set --url, OMR_PDF_URL, or wait for "
                "iea.org to publish a free version"
            )
            sys.exit(1)
        pdf_bytes = download_pdf(url)
        source = url

    # ---- Determine report_date ----
    if args.report_date:
        report_date = args.report_date
    else:
        report_date = (
            report_date_from_url(source)
            or report_date_from_pdf_meta(pdf_bytes)
            or datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        )

    # ---- Parse ----
    text = pdf_to_text(pdf_bytes)
    records = parse_tables(text)
    if not records:
        log.error("no_records_parsed source=%s — PDF layout may have changed", source)
        sys.exit(1)
    by_table = {}
    for r in records:
        by_table.setdefault(r["table_id"], 0)
        by_table[r["table_id"]] += 1
    log.info("parsed report_date=%s records=%d breakdown=%s",
             report_date, len(records), by_table)

    # ---- Persist ----
    db = Path(args.db_path) if args.db_path else db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        ensure_schema(conn)
        n = upsert_records(conn, report_date, records)
        conn.commit()
        log.info("wrote db=%s rows=%d report_date=%s", db, n, report_date)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
