"""Sentinel-1 GRD sigma0 calibration — DN → linear → dB.

What we replace: Sentinel Hub's Process API does this server-side and
returns sigma0_db GeoTIFFs directly. When we download raw GRD products
from DESP (free of PUs), we have to calibrate locally.

Reference: https://sentinels.copernicus.eu/web/sentinel/radiometric-calibration-of-level-1-products
ESA's calibration equation for GRD products:

    sigma0_linear(i, j) = |DN(i, j)|^2 / sigmaNought(i, j)^2

where sigmaNought is a per-pixel LUT in the SAFE archive at
`annotation/calibration/calibration-s1*.xml`. The LUT is sparse — given
at a grid of (line, pixel) samples — so we bilinear-interpolate to the
full image resolution.

This module is intentionally numpy-only (no rasterio dependency) so the
LUT logic can be unit-tested against synthetic inputs. The pipeline
glue layer in `sentinel_s1_grd.py` reads the GRD tiff with rasterio
and passes the DN array here.

Functions:
    parse_calibration_lut(safe_dir_or_zip, polarisation='vv') -> dict
    apply_calibration(dn, lut)                                -> ndarray (linear)
    linear_to_db(linear, floor_db=-50.0)                      -> ndarray (dB)
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np


# DN values below ~10 → calibration ratio is dominated by noise; linear
# sigma0 can be tiny. Clamp the dB floor to keep downstream stats sane.
_DEFAULT_DB_FLOOR = -50.0


def parse_calibration_lut(
    safe_path: Path | str, polarisation: str = "vv",
) -> dict:
    """Parse the sparse sigma0 calibration LUT from a S1 GRD SAFE archive.

    Returns:
        {
            "lines":     ndarray[N] — sample line indices (axis-0 of image)
            "pixels":    ndarray[M] — sample pixel indices (axis-1)
            "sigmaNought": ndarray[N, M] — LUT values; calibrated_linear =
                          |DN|^2 / sigmaNought^2 after bilinear-interp to
                          full image dims.
        }

    `safe_path` may be a directory (extracted .SAFE) or a .zip file
    containing the .SAFE structure. polarisation: 'vv', 'vh', 'hh', 'hv'.
    """
    safe_path = Path(safe_path)
    xml_text = _read_calibration_xml(safe_path, polarisation)
    root = ET.fromstring(xml_text)

    # The XML structure: calibration > calibrationVectorList > calibrationVector
    # Each vector has line=<int>, pixel="<list>", sigmaNought="<list>".
    vectors = root.findall(".//calibrationVector")
    if not vectors:
        raise ValueError(
            f"No <calibrationVector> elements found in calibration XML "
            f"for polarisation {polarisation!r}"
        )

    lines = np.array([int(v.findtext("line")) for v in vectors], dtype=np.int32)
    # All vectors share the same pixel grid (sparse columns). Read from the first.
    pixels_text = vectors[0].findtext("pixel")
    pixels = np.fromstring(pixels_text, dtype=np.int32, sep=" ")
    sigmaNought = np.array([
        np.fromstring(v.findtext("sigmaNought"), dtype=np.float32, sep=" ")
        for v in vectors
    ])
    if sigmaNought.shape != (len(lines), len(pixels)):
        raise ValueError(
            f"sigmaNought shape {sigmaNought.shape} doesn't match "
            f"(lines={len(lines)}, pixels={len(pixels)})"
        )
    return {"lines": lines, "pixels": pixels, "sigmaNought": sigmaNought}


def _read_calibration_xml(safe_path: Path, polarisation: str) -> str:
    """Locate + read the calibration XML for one polarisation.

    SAFE structure: <SAFE_DIR>/annotation/calibration/calibration-s1[abcd]-iw-grd-<pol>-...xml
    """
    needle = f"calibration-s1"
    polneedle = f"-grd-{polarisation.lower()}-"

    def _matches(name: str) -> bool:
        n = name.lower()
        return ("annotation/calibration/" in n
                and needle in n and polneedle in n and n.endswith(".xml"))

    if safe_path.is_dir():
        # Extracted .SAFE directory
        for p in safe_path.rglob("annotation/calibration/calibration-*.xml"):
            if polneedle in p.name.lower():
                return p.read_text()
        raise FileNotFoundError(
            f"No calibration XML for pol={polarisation!r} under {safe_path}"
        )
    # Treat as zip (the .SAFE.zip we downloaded)
    with zipfile.ZipFile(safe_path) as zf:
        for info in zf.infolist():
            if _matches(info.filename):
                return zf.read(info).decode("utf-8")
    raise FileNotFoundError(
        f"No calibration XML for pol={polarisation!r} in zip {safe_path}"
    )


def apply_calibration(dn: np.ndarray, lut: dict) -> np.ndarray:
    """Apply the LUT to convert DN values to linear sigma0.

    Bilinear-interpolates `lut["sigmaNought"]` from its sparse
    (lines × pixels) grid to the full image shape, then computes
    |DN|^2 / sigmaNought^2.

    For a 25000 × 16000 GRD image, the LUT is typically 11 × 200 (sparse);
    interpolation is the expensive step. We do it in pure numpy via
    `np.interp` along each axis, which is sufficient for this resolution.
    """
    h, w = dn.shape
    L = lut["lines"]
    P = lut["pixels"]
    SN = lut["sigmaNought"]

    # Interpolate along pixel-axis first to get LUT @ (lines, full_pixels)
    full_pixel_idx = np.arange(w, dtype=np.float32)
    SN_w = np.empty((SN.shape[0], w), dtype=np.float32)
    for i, _ in enumerate(L):
        SN_w[i] = np.interp(full_pixel_idx, P, SN[i])

    # Then interpolate along line-axis to get full (lines × pixels) grid.
    # Memory: a 25000 × 16000 float32 array is 1.6 GB — at this scale we
    # interpolate row-by-row to avoid the materialised LUT.
    dn_sq = (dn.astype(np.float32)) ** 2
    sigma0 = np.empty_like(dn_sq)
    full_line_idx = np.arange(h, dtype=np.float32)
    # Pre-build the per-row LUT for each output row by interpolating SN_w
    # over the line axis.
    # For each output row r, sigmaNought_row = interp_along_lines(SN_w[:, :], target=r)
    # We can do this column-by-column with np.interp (vectorised along output rows).
    # Avoid building the full 2D LUT — process in chunks to keep memory bounded.
    chunk = 1024
    for r0 in range(0, h, chunk):
        r1 = min(r0 + chunk, h)
        target = full_line_idx[r0:r1]
        # SN_w_chunk_lut: (chunk_rows, w) interpolated along line axis
        # np.interp doesn't vectorise across columns — loop over columns.
        # For typical GRD widths (~25k), this is the slow path; OK for v1.
        # Optimization opportunity: use scipy.interpolate.RegularGridInterpolator.
        sn_chunk = np.empty((r1 - r0, w), dtype=np.float32)
        for col in range(w):
            sn_chunk[:, col] = np.interp(target, L, SN_w[:, col])
        sigma0[r0:r1] = dn_sq[r0:r1] / (sn_chunk * sn_chunk)
    return sigma0


def linear_to_db(linear: np.ndarray, floor_db: float = _DEFAULT_DB_FLOOR) -> np.ndarray:
    """Convert linear sigma0 to dB. NaN-safe; clamp very-low values."""
    out = np.full_like(linear, floor_db, dtype=np.float32)
    pos = linear > 0
    out[pos] = 10.0 * np.log10(linear[pos])
    # Clamp anything below floor (including very small positive values)
    return np.maximum(out, floor_db)
