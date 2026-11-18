"""Gerber & Excellon normalization and EasyEDA/JLCPCB packaging utilities."""

from __future__ import annotations

import datetime
import hashlib
import os
import random
import re
from typing import Dict, List, Optional, Tuple


HOW_TO_ORDER_TXT = (
    "How to Order PCB\r\n\r\n"
    "Please refer to:\r\n"
    "https://prodocs.easyeda.com/en/pcb/order-order-pcb/index.html"
)

# Mapping from KiCad plot layer suffixes to JLCPCB / EasyEDA Pro filenames
KICAD_TO_JLC_FILENAMES: Dict[str, str] = {
    "F_Cu": "Gerber_TopLayer.GTL",
    "B_Cu": "Gerber_BottomLayer.GBL",
    "F_Mask": "Gerber_TopSolderMaskLayer.GTS",
    "B_Mask": "Gerber_BottomSolderMaskLayer.GBS",
    "F_Paste": "Gerber_TopPasteMaskLayer.GTP",
    "B_Paste": "Gerber_BottomPasteMaskLayer.GBP",
    "F_Silkscreen": "Gerber_TopSilkscreenLayer.GTO",
    "F_SilkS": "Gerber_TopSilkscreenLayer.GTO",
    "B_Silkscreen": "Gerber_BottomSilkscreenLayer.GBO",
    "B_SilkS": "Gerber_BottomSilkscreenLayer.GBO",
    "Edge_Cuts": "Gerber_BoardOutlineLayer.GKO",
}

_INNER_LAYER_RE = re.compile(r"In(\d+)_Cu", re.IGNORECASE)
_APERTURE_DEF_RE = re.compile(r"(?m)^%ADD(\d{2,4})(\D[^\n]*)$")
_APERTURE_USE_RE = re.compile(r"(?m)(?:^[ \t]*|G54)D(\d{2,4})\*")
_LEADING_DOT_AP_RE = re.compile(r"(?m)^%ADD\d+[A-Z][^*]*\*%$")
_LEADING_DOT_VAL_RE = re.compile(r"([,X])\.(\d)")
_DEPRECATED_ATTR_RE = re.compile(r"%(?:IR|IP|OF|MI|SF)[^%]*%\n?")
_COMBINED_FS_MO_RE = re.compile(r"%\s*(FS[^*%]*)\*\s*(MO[^*%]*)\*\s*%")
_COMBINED_MO_FS_RE = re.compile(r"%\s*(MO[^*%]*)\*\s*(FS[^*%]*)\*\s*%")
_BARE_D_CODE_RE = re.compile(r"^(\s*)(D\d{2,4}\*)")
_EXCELLON_TOOL_DOT_RE = re.compile(r"(?m)^(T\d+C)\.(\d)")


def map_kicad_gerber_filename(filename: str) -> Optional[str]:
    """Map a KiCad Gerber or drill filename to JLCPCB/EasyEDA Pro naming."""
    lower = filename.lower()
    if lower.endswith(".drl"):
        if "npth" in lower:
            return "Drill_NPTH_Through.DRL"
        return "Drill_PTH_Through.DRL"

    for suffix, jlc_name in KICAD_TO_JLC_FILENAMES.items():
        if f"-{suffix.lower()}." in lower or lower.endswith(f"{suffix.lower()}.gbr"):
            return jlc_name

    m = _INNER_LAYER_RE.search(filename)
    if m:
        num = int(m.group(1))
        return f"Gerber_InnerLayer{num}.G{num}"

    return None


def normalize_excellon_drill(content: str) -> str:
    """Normalize Excellon drill tool diameters that omit a leading zero."""
    return _EXCELLON_TOOL_DOT_RE.sub(r"\g<1>0.\2", content)


def add_easyeda_header(content: str, timestamp: Optional[datetime.datetime] = None) -> str:
    """Prepend EasyEDA Pro Gerber header expected by JLCPCB's colorful parser."""
    dt = timestamp or datetime.datetime.now()
    header = (
        f"G04 EasyEDA Pro v4.1.105, {dt.strftime('%Y-%m-%d %H:%M:%S')}*\n"
        "G04 Gerber Generator version 0.3*\n"
    )
    normalized = content.replace("\r\n", "\n")
    return header + normalized


def add_hash_aperture(content: str, rng: Optional[random.Random] = None) -> str:
    """Strip unused apertures and append JLCPCB's MD5-verified circular aperture."""
    normalized = content.replace("\r\n", "\n")
    matches = list(_APERTURE_DEF_RE.finditer(normalized))
    if not matches:
        return normalized

    used_numbers = {int(m.group(1)) for m in _APERTURE_USE_RE.finditer(normalized)}

    # Remove unused aperture definitions (e.g., an existing hash aperture)
    delete_ranges: List[Tuple[int, int]] = []
    for m in matches:
        num = int(m.group(1))
        if num in used_numbers:
            continue
        end = m.end()
        if end < len(normalized) and normalized[end] == "\n":
            end += 1
        delete_ranges.append((m.start(), end))

    cleaned = normalized
    for start, end in reversed(delete_ranges):
        cleaned = cleaned[:start] + cleaned[end:]

    matches_after = list(_APERTURE_DEF_RE.finditer(cleaned))
    if matches_after:
        max_num = max(int(m.group(1)) for m in matches_after)
        last_end = matches_after[-1].end()
        if last_end < len(cleaned) and cleaned[last_end] == "\n":
            insertion_point = last_end + 1
        else:
            cleaned = cleaned[:last_end] + "\n" + cleaned[last_end:]
            insertion_point = last_end + 1
        target_num = max_num + 1
    else:
        if not cleaned.endswith("\n"):
            cleaned += "\n"
        insertion_point = len(cleaned)
        target_num = 10

    md5_hex = hashlib.md5(cleaned.encode("utf-8")).hexdigest()
    hash_val = int(md5_hex[-2:], 16) % 100
    rand = rng or random.Random()
    prefix = rand.randint(0, 99)
    size = f"0.{prefix:02d}{hash_val:02d}"
    if size == "0.0000":
        size = "0.0100"

    hash_line = f"%ADD{target_num}C,{size}*%\n"
    return cleaned[:insertion_point] + hash_line + cleaned[insertion_point:]


def verify_hash_aperture(content: str) -> bool:
    """Verify whether the last circular 4-decimal aperture matches JLCPCB's MD5 rule."""
    normalized = content.replace("\r\n", "\n")
    matches = list(_APERTURE_DEF_RE.finditer(normalized))
    if not matches:
        return True
    last = matches[-1]
    line = last.group(0)
    m = re.search(r"C,(\d+\.\d{4})\*%\r?$", line)
    if not m:
        return True
    expected_suffix = int(m.group(1)[-2:])
    end = last.end()
    if end < len(normalized) and normalized[end] == "\n":
        end += 1
    stripped = normalized[: last.start()] + normalized[end:]
    md5_hex = hashlib.md5(stripped.encode("utf-8")).hexdigest()
    return (int(md5_hex[-2:], 16) % 100) == expected_suffix


def process_gerber_for_jlc(
    content: str,
    inject_header: bool = True,
    ensure_g54: bool = False,
) -> str:
    """Normalize a KiCad Gerber string for JLCPCB EasyEDA-compatible upload."""
    result = content.replace("\r\n", "\n")
    result = _COMBINED_FS_MO_RE.sub(r"%\1*%\n%\2*%", result)
    result = _COMBINED_MO_FS_RE.sub(r"%\1*%\n%\2*%", result)
    result = _DEPRECATED_ATTR_RE.sub("", result)
    result = _LEADING_DOT_AP_RE.sub(
        lambda m: _LEADING_DOT_VAL_RE.sub(r"\g<1>0.\2", m.group(0)),
        result,
    )

    if inject_header and "EasyEDA Pro" not in result[:300]:
        result = add_easyeda_header(result)

    if ensure_g54:
        lines = []
        for line in result.split("\n"):
            stripped = line.lstrip()
            if stripped.startswith("%ADD") or stripped.startswith("G54D"):
                lines.append(line)
            else:
                m = _BARE_D_CODE_RE.match(line)
                if m:
                    lines.append(f"{m.group(1)}G54{m.group(2)}{line[m.end():]}")
                else:
                    lines.append(line)
        result = "\n".join(lines)

    return add_hash_aperture(result)
