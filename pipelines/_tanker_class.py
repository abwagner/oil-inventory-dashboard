"""
Tanker class + capacity lookup.

Maps AIS ShipStaticData (length, beam, draught) to a tanker class and an
estimated barrels-of-crude-on-board figure. Used by `/api/ships` to compute
the "crude on water" aggregate.

Caveats baked in here, not in callers:
  * Class boundaries are length-only with beam as a tiebreaker. AIS dimensions
    are operator-reported and can drift by a few meters; we use overlap-
    tolerant ranges instead of hard cutoffs.
  * Laden ratio is clamped to [0.4, 1.0] to bound the damage from stale or
    spoofed draught reports (TDD §9.7: "draught … often zero or stale … the
    dark fleet under-reports draught precisely to obscure cargo state").
  * Below ~160m we don't classify — those vessels are too small to be crude
    carriers in any meaningful trade-flow sense.
  * Capacity figures are nominal class deadweights, not per-vessel deadweights.
    Real-world spread within a class is ±20% but the dashboard's headline
    aggregate is directional, not authoritative.

All functions accept None / NaN inputs and return None / 0 rather than raising.
"""

from __future__ import annotations

import math
from typing import Optional

# Nominal deadweight capacity, in barrels of crude (1 metric ton ≈ 7.33 bbl
# crude at typical 0.86 specific gravity; class deadweights below are the
# round-number trader convention).
_CLASS_CAPACITY_BBL: dict[str, int] = {
    "VLCC":      2_000_000,
    "Suezmax":   1_000_000,
    "Aframax":     700_000,
    "Panamax":     500_000,
    "Handysize":   300_000,
}

# Length boundaries (meters). Overlap tolerance with beam tiebreakers below.
_CLASS_LENGTH_RANGES: list[tuple[str, float, float]] = [
    ("VLCC",      290.0, 360.0),
    ("Suezmax",   265.0, 289.9),
    ("Aframax",   240.0, 264.9),
    ("Panamax",   200.0, 239.9),
    ("Handysize", 160.0, 199.9),
]

# Default laden ratio when draught or design-draught are missing/zero/stale.
# 0.6 is roughly the fleet-average over a typical transit cycle (laden + ballast
# legs averaged); it's a defensible middle ground that won't anchor the
# aggregate to either extreme.
_DEFAULT_LADEN_RATIO = 0.6
_LADEN_CLAMP = (0.4, 1.0)


def _coerce(v) -> Optional[float]:
    """Cast `v` to float, treating None / NaN / 0 / negative as None."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or f <= 0:
        return None
    return f


def classify(length_m, beam_m=None) -> Optional[str]:
    """Map (length_m, beam_m) → tanker class string, or None if insufficient.

    Length-only classification works for ~95% of cases; beam is used here only
    to break ties at the boundary (e.g., a 290m × 32m vessel is Panamax-beam
    despite VLCC-length — these are typically LNG carriers, container ships,
    or specialty vessels and are excluded by returning None).
    """
    L = _coerce(length_m)
    if L is None or L < 160:
        return None
    for name, lo, hi in _CLASS_LENGTH_RANGES:
        if lo <= L <= hi:
            # Beam-based exclusion: a "VLCC-length" vessel with beam <40m
            # is not a VLCC tanker (real VLCCs are 50-60m beam).
            B = _coerce(beam_m)
            if name == "VLCC" and B is not None and B < 40:
                return None
            if name == "Suezmax" and B is not None and B < 35:
                return None
            return name
    return None


def capacity_bbl(tanker_class: Optional[str]) -> int:
    """Nominal class deadweight in barrels. 0 if class is None/unknown."""
    if not tanker_class:
        return 0
    return _CLASS_CAPACITY_BBL.get(tanker_class, 0)


def laden_ratio(draught_m, design_draught_m) -> float:
    """Clamped ratio of current draught to design (max) draught.

    Returns the default ratio when either input is missing — including the
    common case where AIS broadcasts `MaximumStaticDraught` but the live
    PositionReport doesn't carry a current draught (most class-A transponders
    don't update draught on every PR).
    """
    d = _coerce(draught_m)
    md = _coerce(design_draught_m)
    if d is None or md is None:
        return _DEFAULT_LADEN_RATIO
    ratio = d / md
    return max(_LADEN_CLAMP[0], min(_LADEN_CLAMP[1], ratio))


def barrels_estimate(
    length_m, beam_m, draught_m, design_draught_m,
) -> tuple[int, Optional[str]]:
    """Return (barrels, class). Returns (0, None) when class can't be inferred.

    barrels = class_capacity × clamped_laden_ratio. When draught data is
    missing we still return capacity × default-ratio (0.6) so the aggregate
    isn't artificially deflated by AIS transponders that don't repeat draught.
    """
    cls = classify(length_m, beam_m)
    if cls is None:
        return 0, None
    return int(round(capacity_bbl(cls) * laden_ratio(draught_m, design_draught_m))), cls
