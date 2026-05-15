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

# curl_cffi uses curl-impersonate to mimic real browser TLS fingerprints,
# bypassing iea.org's Cloudflare bot detection that 403s plain requests/
# httpx/urllib clients. Optional: if not installed (older deploy), we fall
# back to plain `requests` and auto-discovery silently degrades to
# "set OMR_PDF_URL manually."
try:
    from curl_cffi import requests as _cffi_requests  # type: ignore
    _HAVE_CURL_CFFI = True
except ImportError:
    _cffi_requests = None
    _HAVE_CURL_CFFI = False

from _env import db_path, load_repo_env

load_repo_env()

log = logging.getLogger("omr")

# Browser UA — used only when curl_cffi is unavailable; curl_cffi sets its
# own headers as part of its impersonation.
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
# Chrome version to impersonate via curl_cffi. Bump as Cloudflare's
# fingerprint database evolves; chrome131 is current at the time of writing.
_IMPERSONATE = "chrome131"

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

# Filename date prefix, e.g. "-14APR2026_OilMarketReport" -> (14, APR, 2026).
# Allow one or more underscores between the date and "OilMarketReport"; IEA
# varies the suffix across releases (`_Free_version1`, `_publicversion`, none,
# sometimes a leading double-underscore on the latter).
_FILENAME_DATE_RE = re.compile(
    r"-(\d{1,2})([A-Z]{3})(\d{4})_+OilMarketReport",
    re.IGNORECASE,
)


def candidate_report_urls(today: datetime | None = None) -> list[str]:
    """Probe the current month first, then walk back 4 prior months.

    IEA publishes the report mid-month; if today is before the release we want
    to still find last month's edition. Walking back through prior months also
    makes the call robust if the most recent month wasn't published free.

    Kept for the iea.org scrape fallback path even though primary discovery
    is now via DuckDuckGo search — iea.org sits behind Cloudflare's JS
    challenge and 403s every non-JS client (curl_cffi included).
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


def _search_ddg_for_blob_urls(query: str, timeout: int = 15) -> list[str]:
    """Query DuckDuckGo's HTML interface and extract any iea.blob URLs.

    DuckDuckGo indexes the iea.blob CDN's PDF URLs (the OMR PDFs are public,
    just gated behind a Cloudflare-protected iea.org HTML page). Their HTML
    results page wraps each result link as `/l/?uddg=<encoded-url>` —
    extract + url-decode to recover the real CDN URLs. Free, no API key,
    handles both the current-month release and historical issues in one
    response.
    """
    from urllib.parse import quote, unquote
    ddg_url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
    try:
        # curl_cffi works fine here too; DDG is more permissive than iea.org
        # but we use the same client to keep the dependency surface small.
        if _HAVE_CURL_CFFI:
            resp = _cffi_requests.get(ddg_url, impersonate=_IMPERSONATE,
                                      timeout=timeout, allow_redirects=True)
        else:
            resp = requests.get(ddg_url, headers={"User-Agent": _UA},
                                timeout=timeout, allow_redirects=True)
    except Exception as e:
        log.warning("ddg_fetch_failed query=%r error=%s", query, e)
        return []
    if resp.status_code != 200:
        log.warning("ddg_not_200 query=%r status=%d", query, resp.status_code)
        return []
    html = resp.text if hasattr(resp, "text") else resp.content.decode("utf-8", "replace")
    # DDG result links: /l/?uddg=<percent-encoded-target>&rut=...
    out: list[str] = []
    for m in re.finditer(r'uddg=([^&"\'<>]+)', html):
        decoded = unquote(m.group(1))
        if "iea.blob.core.windows.net" in decoded and "OilMarketReport" in decoded:
            # de-dupe while preserving order
            if decoded not in out:
                out.append(decoded)
    return out


def fetch_html(url: str, timeout: int = 30) -> str | None:
    """GET an iea.org HTML page. Returns None on 4xx/5xx.

    Uses curl_cffi with a Chrome TLS fingerprint when available — required
    to get past Cloudflare's bot check on iea.org. Falls back to `requests`
    + a browser UA when curl_cffi isn't installed (will likely 403).
    """
    if _HAVE_CURL_CFFI:
        try:
            resp = _cffi_requests.get(
                url, impersonate=_IMPERSONATE, timeout=timeout, allow_redirects=True,
            )
        except Exception as e:
            log.warning("html_fetch_failed url=%s mode=curl_cffi error=%s", url, e)
            return None
    else:
        try:
            resp = requests.get(
                url, headers={"User-Agent": _UA}, timeout=timeout, allow_redirects=True,
            )
        except requests.RequestException as e:
            log.warning("html_fetch_failed url=%s mode=requests error=%s", url, e)
            return None
    if resp.status_code != 200:
        log.info("html_not_200 url=%s status=%d mode=%s",
                 url, resp.status_code, "curl_cffi" if _HAVE_CURL_CFFI else "requests")
        return None
    return resp.text


def find_latest_pdf_url() -> str | None:
    """Discover the latest free OMR PDF URL.

    Strategy: query DuckDuckGo for `site:iea.blob.core.windows.net
    OilMarketReport <year>`, parse iea.blob URLs from the results, and pick
    the one with the most recent date prefix in its filename. Falls back to
    scraping iea.org HTML if DDG returns nothing (likely 403s under
    Cloudflare's JS challenge — but cheap to try).
    """
    today = datetime.now(tz=timezone.utc)
    # Query the current year first, then prior year — covers the late-January
    # window where the current year has nothing indexed yet.
    candidates: list[str] = []
    for year in (today.year, today.year - 1):
        candidates.extend(_search_ddg_for_blob_urls(
            f"site:iea.blob.core.windows.net OilMarketReport {year}"
        ))
        if candidates:
            break  # don't query prior year unless current year yielded nothing

    if candidates:
        # Sort by date prefix in the filename (most recent first), pick top.
        def _key(u: str) -> tuple:
            d = report_date_from_url(u)
            return (d or "0000-00-00",)
        candidates.sort(key=_key, reverse=True)
        pick = candidates[0]
        log.info("found_pdf_url_via_ddg pick=%s candidates=%d", pick, len(candidates))
        return pick

    # Fallback: iea.org scrape (almost certainly 403s but kept as a path)
    log.info("ddg_yielded_nothing trying_iea_org_scrape")
    for url in candidate_report_urls():
        html = fetch_html(url)
        if html is None:
            continue
        m = _BLOB_URL_RE.search(html)
        if m:
            log.info("found_pdf_url_via_iea_org page=%s pdf=%s", url, m.group())
            return m.group()
    return None


# ---------------------------------------------------------------------------
# Download + text extraction
# ---------------------------------------------------------------------------


def download_pdf(url: str, timeout: int = 60) -> bytes:
    """Download a PDF. Uses curl_cffi when available for Cloudflare-protected
    paths; falls back to `requests`. Raises on non-200.
    """
    log.info("downloading pdf url=%s mode=%s",
             url, "curl_cffi" if _HAVE_CURL_CFFI else "requests")
    if _HAVE_CURL_CFFI:
        resp = _cffi_requests.get(
            url, impersonate=_IMPERSONATE, timeout=timeout, allow_redirects=True,
        )
    else:
        resp = requests.get(
            url, headers={"User-Agent": _UA}, timeout=timeout, allow_redirects=True,
        )
    if resp.status_code != 200:
        raise RuntimeError(f"PDF download failed: HTTP {resp.status_code} for {url}")
    return resp.content


def pdf_to_text(pdf_bytes: bytes) -> str:
    """Run `pdftotext -raw` (from poppler-utils) on the PDF bytes.

    Originally used `-layout` which preserves column geometry — but the May
    2026 OMR was authored with a different PDF generator that emits Table 1
    in column-major order when `-layout` is used (header / value / header /
    value …). `-raw` ignores layout entirely and emits text in reading
    order, which gives consistent row-major output for both April and May
    PDF layouts. The downstream parser zips values to columns by index, so
    column geometry isn't needed.
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        tmp = f.name
    try:
        out = subprocess.run(
            ["pdftotext", "-raw", tmp, "-"],
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

# Anchor regexes for each table. Each table accepts EITHER the "Table N"
# title line OR the descriptive subtitle, because the line that appears
# before the data varies between PDF generators (April 2026's free OMR has
# only the subtitle preceding the data; May 2026 has both). The patterns
# are mutually exclusive between tables to avoid false matches:
#   - Table 1's subtitle ("WORLD OIL SUPPLY AND DEMAND") must end-of-line,
#     so Table 1a's longer subtitle doesn't get scooped here.
#   - Table 1a's subtitle is identified by "CHANGES FROM LAST MONTH".
#   - Table 1b's subtitle is "WORLD OIL PRODUCTION".
_TABLE_ANCHORS = {
    "1":  re.compile(
        r"^\s*(?:Table 1\b(?![ab])|WORLD OIL SUPPLY AND DEMAND\s*$)",
        re.IGNORECASE,
    ),
    "1a": re.compile(
        r"^\s*(?:Table 1a\b|.*CHANGES FROM LAST MONTH)",
        re.IGNORECASE,
    ),
    "1b": re.compile(
        r"^\s*(?:Table 1b\b|WORLD OIL PRODUCTION)",
        re.IGNORECASE,
    ),
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


def label_and_remainder(line: str, first_value_col: int = 0) -> tuple[str, str]:
    """Split `line` into (row_label, value_region).

    Strategy: find the first decimal-value token (`\\d+\\.\\d+`) and split
    there. Everything before is the label, everything from the token onward
    is the value region. Works for both `pdftotext -layout` (label and
    values are widely spaced) and `pdftotext -raw` (label and values are
    single-space separated). Footnote-marker digits attached to row labels
    are integers, not decimals, so they stay in the label and get cleaned
    by `clean_row_label`.

    `first_value_col` is no longer used (kept for backward compat with
    existing call sites); pass anything.
    """
    m = _VALUE_RE.search(line)
    if m:
        return line[:m.start()].rstrip(), line[m.start():]
    return line.rstrip(), ""


def looks_like_section(line: str, first_value_col: int = 0) -> bool:
    """A section header has letters in the label region but NO numeric values
    in the value region."""
    label, rest = label_and_remainder(line)
    if not label:
        return False
    if _VALUE_RE.search(rest):
        return False
    return any(ch.isalpha() for ch in label)


def _is_real_anchor(lines: list[str], idx: int, max_lookahead: int = 10) -> bool:
    """An anchor is 'real' if a period-header row (>=3 period tokens) appears
    within the next `max_lookahead` lines. Filters out incidental "Table 1"
    matches inside other tables' multi-line titles or in page footers.
    """
    n = len(lines)
    upper = min(idx + 1 + max_lookahead, n)
    for j in range(idx + 1, upper):
        if len(parse_periods_from_header(lines[j])) >= 3:
            return True
    return False


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
        # Stop at the start of the next table — but only if it's a *real*
        # anchor (period header follows within 10 lines). Without this check
        # we'd break on "Table 1" / "Table 1a" / "Table 1b" sub-strings that
        # appear inside other tables' titles or footers.
        if (
            any(anchor.match(line) for anchor in _TABLE_ANCHORS.values())
            and _is_real_anchor(lines, i)
        ):
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


# Heuristic for "this line has only numeric tokens / whitespace / signs" —
# used to recognise a values-only row split off from its label.
_VALUES_ONLY_LINE_RE = re.compile(r"^[\s\d.\-]+$")


def _preprocess_raw_text(text: str) -> str:
    """Merge label-only lines with the next values-only line.

    `pdftotext -raw` separates footnote-superscripted row labels from their
    values onto adjacent lines (e.g. "Americas1\\n25.7 26.4 …"). Rejoin so
    the rest of the parser sees one row per record.

    Heuristic: the candidate label line has letters but no decimal value;
    the next line consists of only digits/decimals/signs/whitespace AND has
    at least one decimal value. Section-header lines like "NON-OECD SUPPLY"
    are usually followed by another label line (e.g. "Eurasia"), which has
    letters and therefore fails the values-only check — left untouched.
    """
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        next_line = lines[i + 1] if i + 1 < len(lines) else ""
        if (
            line.strip()
            and not _VALUE_RE.search(line)
            and any(c.isalpha() for c in line)
            and next_line.strip()
            and _VALUES_ONLY_LINE_RE.match(next_line)
            and _VALUE_RE.search(next_line)
        ):
            out.append(line.rstrip() + " " + next_line.lstrip())
            i += 2
        else:
            out.append(line)
            i += 1
    return "\n".join(out)


def parse_tables(text: str) -> list[dict]:
    """Parse Tables 1, 1a, 1b from the OMR PDF text.

    Each table is parsed AT MOST ONCE, even if its anchor string recurs
    (e.g. "Table 1" appears inside Table 1a's multi-line title and in page
    footers). An anchor is only committed when a period-header row exists
    within 10 lines after it.
    """
    text = _preprocess_raw_text(text)
    lines = text.splitlines()
    n = len(lines)
    out: list[dict] = []
    i = 0
    seen: set[str] = set()
    while i < n:
        line = lines[i]
        matched_table = None
        for tid, anchor in _TABLE_ANCHORS.items():
            if tid in seen:
                continue
            if anchor.match(line) and _is_real_anchor(lines, i):
                matched_table = tid
                break
        if matched_table is None:
            i += 1
            continue
        records, next_i = parse_one_table(lines, i, matched_table)
        # Mark seen regardless of records — even a failed parse shouldn't get
        # re-attempted on the same anchor line.
        seen.add(matched_table)
        if records:
            out.extend(records)
            log.info("parsed_table id=%s rows=%d", matched_table, len(records))
        else:
            log.warning("parsed_table_empty id=%s anchor=%d", matched_table, i)
        i = max(next_i, i + 1)
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
