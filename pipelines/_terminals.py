"""
Named terminal hotspots for SAR-derived floating-storage signals.

Each entry: (name, lat, lon, radius_km, aoi_hint, description). The
radius bounds the search disc for "is this SAR cluster near this terminal";
sized to encompass the visible anchorage area (~ship-length × queue depth
× a margin for AIS-spoofing drift).

The `aoi_hint` is the scheduler AOI most likely to contain the terminal —
purely an optimization hint for the API endpoint (it scans that AOI's
clusters.parquet first), not a hard constraint. A terminal CAN be picked
up by a different AOI's SAR scan if AOIs overlap.

Selection criteria:
  - "Floating storage" hotspots — anchorage areas historically used as
    waterborne storage by traders shorting front-month contracts
    (Singapore Eastern + Western OPL, Bahamas, Persian Gulf inner waters).
  - Major export terminals — anchorage queue = export-side congestion
    (Ras Tanura, Kharg, Cabinda).
  - Major import terminals — anchorage queue = import-side congestion
    (LOOP, Houston, Qingdao, Dalian).
  - Disruption chokepoints — Bab el-Mandeb queue tracking.

Coordinates are anchorage centroids, not port docks. Sourced from
publicly-visible vessel-tracker waypoints; precision ±0.05° is fine since
the radius is 30-50 km.
"""

from __future__ import annotations

import math
from typing import Iterable


# (name, lat, lon, radius_km, aoi_hint, description)
TERMINALS: list[tuple[str, float, float, float, str, str]] = [
    # ── Middle East — primary export terminals ──────────────────────────
    ("Ras Tanura", 26.65, 50.20, 50, "persian_gulf_oman",
     "Saudi Arabia — world's largest crude export terminal (Saudi Aramco)"),
    ("Kharg Island", 29.25, 50.30, 30, "persian_gulf_oman",
     "Iran — primary crude export terminal; floating-storage proxy under sanctions"),
    ("Fujairah", 25.20, 56.40, 40, "persian_gulf_oman",
     "UAE — bunker hub + sanctions ship-to-ship transfer zone (east of Hormuz)"),
    # ── US Gulf — import + export hubs ──────────────────────────────────
    ("Houston Ship Channel", 29.70, -95.10, 40, "usgc",
     "Texas — US Gulf crude import + export hub"),
    ("LOOP", 28.88, -90.02, 30, "usgc",
     "Louisiana Offshore Oil Port — primary US VLCC import terminal"),
    ("Galveston Offshore", 28.80, -94.50, 40, "usgc",
     "Texas — STS transfer + lightering zone for VLCC offloading"),
    # ── SE Asia — Singapore anchorages (canonical floating-storage hotspot) ─
    ("Singapore Eastern OPL", 1.30, 104.00, 30, "singapore_malacca",
     "Singapore anchorage east — primary floating-storage zone"),
    ("Singapore Western OPL", 1.20, 103.65, 30, "singapore_malacca",
     "Singapore anchorage west — bunker + STS zone"),
    # ── China — primary import unloading ────────────────────────────────
    ("Qingdao", 36.10, 120.30, 30, "yellow_sea_bohai",
     "Shandong — major MEG crude import port (Sinopec refineries)"),
    ("Dalian", 38.92, 121.65, 30, "yellow_sea_bohai",
     "Liaoning — Russian ESPO landing + CNPC refineries"),
    ("Bohai Bay (Tianjin)", 38.80, 118.40, 50, "yellow_sea_bohai",
     "Bohai — broad inner-bay refining + storage cluster"),
    # ── Red Sea / Bab el-Mandeb — Suez bypass chokepoint ────────────────
    ("Bab el-Mandeb", 12.60, 43.40, 40, "red_sea_bab_mandeb",
     "Yemen/Djibouti chokepoint — Houthi attack zone; transit-traffic gauge"),
    ("Yanbu", 24.10, 38.05, 30, "red_sea_bab_mandeb",
     "Saudi Arabia — Red Sea-side crude export (East-West Pipeline outlet)"),
]


# Earth radius (km) used by the great-circle distance calc. Average value;
# good to ~0.5% across the latitudes we care about.
_EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two (lat, lon) pairs, in km."""
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    dlat = lat2r - lat1r
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1r) * math.cos(lat2r) * math.sin(dlon / 2) ** 2)
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def nearest_terminal(
    lat: float, lon: float, max_km: float = 100.0,
) -> tuple[str, float] | None:
    """Find the nearest terminal to (lat, lon) within `max_km`. Returns
    (name, distance_km) or None. Cheap linear scan — only ~13 terminals."""
    best: tuple[str, float] | None = None
    for name, t_lat, t_lon, _r, _aoi, _desc in TERMINALS:
        d = haversine_km(lat, lon, t_lat, t_lon)
        if d <= max_km and (best is None or d < best[1]):
            best = (name, d)
    return best


def iter_terminals() -> Iterable[dict]:
    """Yield each terminal as a dict for API serialization."""
    for name, lat, lon, radius_km, aoi_hint, description in TERMINALS:
        yield {
            "name": name,
            "lat": lat,
            "lon": lon,
            "radius_km": radius_km,
            "aoi_hint": aoi_hint,
            "description": description,
        }
