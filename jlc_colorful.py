"""JLCPCB colorful silkscreen generator (.FCTS, .FCBS, .FCBO, .FCBM)."""

from __future__ import annotations

import base64
import io
import math
import os
import re
import secrets
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw, ImageOps

if __package__:
    from .jlc_gerber import add_hash_aperture
else:
    from jlc_gerber import add_hash_aperture


JLC_RSA_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAzPtuUqJecaR/wWtctGT8
QuVslmDH3Ut3s8c1Ls4A+M9rwpeLjgDUqfcrSrTHBrl5k/dOeJEWMeNF7STWS5jo
WZE0H60cvf2bhormC9S6CRwq4Lw0ua0YQMo66R/qCtLVa5w6WkaPCz4b0xaHWtej
JH49C0T67rU2DkepXuMPpwNCflMU+WgEQioZEldUTD6gYpu2U5GrW4AE0AQiIo+j
e7tgN8PlBMbMaEfu0LokZyth1ugfuLAgyogWnedAegQmPZzAUe36Sni94AsDlhxm
mjFl+WQZzD3MclbEY6KQB5XL8zCR/J6pCUUwfHantLxY/gQi0XJG5hWWtDyH/fR2
lwIDAQAB
-----END PUBLIC KEY-----"""


def mm_to_mil_10(val_mm: float) -> float:
    """Convert millimeters to EasyEDA's 10-mil SVG user units (1 unit = 0.254 mm)."""
    return val_mm / 0.254


@dataclass
class BoardBounds:
    """Axis-aligned board outline bounding box in KiCad mm coordinates (Y down)."""

    min_x: float
    max_x: float
    min_y: float
    max_y: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y


@dataclass
class PlacedArtwork:
    """A single raster artwork placed on the PCB in KiCad mm coordinates (Y down)."""

    png_bytes: bytes
    center_x_mm: float
    center_y_mm: float
    width_mm: float
    height_mm: float
    angle_deg: float = 0.0
    mirror_x: bool = False


# ---------------------------------------------------------------------------
# Geometry helpers (polygons, interior alignment marks)
# ---------------------------------------------------------------------------


def point_in_polygon(px: float, py: float, poly: Sequence[Tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon check."""
    inside = False
    n = len(poly)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > py) != (yj > py)) and (
            px < (xj - xi) * (py - yi) / ((yj - yi) if abs(yj - yi) > 1e-12 else 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


def _dist_point_to_segment(
    px: float, py: float, x1: float, y1: float, x2: float, y2: float
) -> float:
    dx = x2 - x1
    dy = y2 - y1
    len_sq = dx * dx + dy * dy
    if len_sq <= 1e-12:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / len_sq))
    proj_x = x1 + t * dx
    proj_y = y1 + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def _min_edge_distance(px: float, py: float, poly: Sequence[Tuple[float, float]]) -> float:
    n = len(poly)
    best = float("inf")
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        best = min(best, _dist_point_to_segment(px, py, x1, y1, x2, y2))
    return best


def _triangle_area(a: Tuple[float, float], b: Tuple[float, float], c: Tuple[float, float]) -> float:
    return abs((a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1])) * 0.5)


def compute_interior_marks(
    bounds: BoardBounds,
    outline_poly: Optional[Sequence[Tuple[float, float]]] = None,
    margin_mm: float = 0.75,
) -> List[Tuple[float, float]]:
    """Compute 3 non-collinear alignment mark coordinates strictly inside the board outline."""
    if not outline_poly or len(outline_poly) < 3:
        m = min(margin_mm, bounds.width * 0.1, bounds.height * 0.1)
        return [
            (bounds.min_x + m, bounds.min_y + m),
            (bounds.min_x + m, bounds.max_y - m),
            (bounds.max_x - m, bounds.max_y - m),
        ]

    # Sample a grid inside the bounding box and keep points strictly inside the polygon
    # with clearance from edges >= min_clear.
    for min_clear in (margin_mm, 0.4, 0.2, 0.05):
        candidates: List[Tuple[float, float]] = []
        steps = 24
        for ix in range(1, steps):
            x = bounds.min_x + bounds.width * (ix / steps)
            for iy in range(1, steps):
                y = bounds.min_y + bounds.height * (iy / steps)
                if point_in_polygon(x, y, outline_poly) and _min_edge_distance(x, y, outline_poly) >= min_clear:
                    candidates.append((x, y))

        if len(candidates) >= 3:
            # Greedy farthest-point + max-area triangle selection
            cx = sum(p[0] for p in candidates) / len(candidates)
            cy = sum(p[1] for p in candidates) / len(candidates)
            p1 = max(candidates, key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)
            p2 = max(candidates, key=lambda p: (p[0] - p1[0]) ** 2 + (p[1] - p1[1]) ** 2)
            p3 = max(candidates, key=lambda p: _triangle_area(p1, p2, p))
            if _triangle_area(p1, p2, p3) > 1e-4:
                return [p1, p2, p3]

    # Ultimate fallback: centroid + small offsets
    cx = (bounds.min_x + bounds.max_x) * 0.5
    cy = (bounds.min_y + bounds.max_y) * 0.5
    dx = bounds.width * 0.2
    dy = bounds.height * 0.2
    return [(cx - dx, cy - dy), (cx - dx, cy + dy), (cx + dx, cy + dy)]


# ---------------------------------------------------------------------------
# Gerber aperture geometry, arc sampling, mask parsing, and layer rendering
# ---------------------------------------------------------------------------


_FS_RE = re.compile(r"%FS([LT])([AI])X(\d)(\d)Y(\d)(\d)\*%")
_ADD_RE = re.compile(r"^%ADD(\d+)([A-Za-z_][A-Za-z0-9_]*)(?:,([^*]+))?\*%$")
_COORD_RE = re.compile(r"(?:X([+-]?\d+))?(?:Y([+-]?\d+))?(?:I([+-]?\d+))?(?:J([+-]?\d+))?")
_G_PREFIX_RE = re.compile(r"^(?:G0[123]|G7[45])+")


def _parse_gerber_coord(raw: str, int_digits: int, dec_digits: int, leading_zero_omitted: bool) -> float:
    sign = -1.0 if raw.startswith("-") else 1.0
    digits = raw.lstrip("+-")
    if leading_zero_omitted:
        val = int(digits) if digits else 0
        return sign * (val / (10 ** dec_digits))
    total = int_digits + dec_digits
    digits = digits.ljust(total, "0")
    val = int(digits) if digits else 0
    return sign * (val / (10 ** dec_digits))


def _circle_svg_path(cx_mm: float, cy_kicad_mm: float, d_mm: float) -> str:
    cx = mm_to_mil_10(cx_mm)
    cy = mm_to_mil_10(cy_kicad_mm)
    r = mm_to_mil_10(d_mm) * 0.5
    if r <= 0:
        return ""
    return (
        f"M {cx + r:.5f} {cy:.5f} "
        f"A {r:.5f} {r:.5f} 0 1 0 {cx - r:.5f} {cy:.5f} "
        f"A {r:.5f} {r:.5f} 0 1 0 {cx + r:.5f} {cy:.5f} Z "
    )


def _poly_svg_path(pts_gbr: Sequence[Tuple[float, float]]) -> str:
    if len(pts_gbr) < 3:
        return ""
    parts = []
    for idx, (px, py_gbr) in enumerate(pts_gbr):
        cmd = "M" if idx == 0 else "L"
        parts.append(f"{cmd} {mm_to_mil_10(px):.5f} {mm_to_mil_10(-py_gbr):.5f}")
    return " ".join(parts) + " Z "


def _rotate_pts_ccw(pts: Sequence[Tuple[float, float]], angle_rad: float) -> List[Tuple[float, float]]:
    c = math.cos(angle_rad)
    s = math.sin(angle_rad)
    return [(x * c - y * s, x * s + y * c) for x, y in pts]


def _circle_polygon(cx: float, cy: float, radius: float, steps: int = 16) -> List[Tuple[float, float]]:
    return [
        (cx + radius * math.cos(2.0 * math.pi * k / steps), cy + radius * math.sin(2.0 * math.pi * k / steps))
        for k in range(steps)
    ]


def _stadium_polygon(
    p1: Tuple[float, float], p2: Tuple[float, float], radius: float, arc_steps: int = 8
) -> List[Tuple[float, float]]:
    """Build CCW stadium (rounded segment) polygon in Gerber Y-up mm."""
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    if math.hypot(dx, dy) < 1e-9 or radius <= 0.0:
        return _circle_polygon(p1[0], p1[1], max(radius, 1e-4))
    base = math.atan2(dy, dx)
    pts: List[Tuple[float, float]] = []
    for k in range(arc_steps + 1):
        a = base - math.pi * 0.5 + math.pi * (k / arc_steps)
        pts.append((p2[0] + radius * math.cos(a), p2[1] + radius * math.sin(a)))
    for k in range(arc_steps + 1):
        a = base + math.pi * 0.5 + math.pi * (k / arc_steps)
        pts.append((p1[0] + radius * math.cos(a), p1[1] + radius * math.sin(a)))
    return pts


def _rounded_poly_from_corners(
    corners: Sequence[Tuple[float, float]], radius: float, arc_steps: int = 4
) -> List[Tuple[float, float]]:
    """Expand inner corner centers outward by radius into a CCW rounded polygon."""
    unique: List[Tuple[float, float]] = []
    for pt in corners:
        if not any(math.hypot(pt[0] - u[0], pt[1] - u[1]) < 1e-6 for u in unique):
            unique.append(pt)

    if not unique:
        return []
    if len(unique) == 1:
        return _circle_polygon(unique[0][0], unique[0][1], max(radius, 1e-4))
    if len(unique) == 2:
        return _stadium_polygon(unique[0], unique[1], radius)

    cx = sum(p[0] for p in unique) / len(unique)
    cy = sum(p[1] for p in unique) / len(unique)
    sorted_pts = sorted(unique, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))

    if radius <= 1e-6:
        return sorted_pts

    n = len(sorted_pts)
    out: List[Tuple[float, float]] = []
    for i in range(n):
        prev_pt = sorted_pts[(i - 1) % n]
        cur_pt = sorted_pts[i]
        next_pt = sorted_pts[(i + 1) % n]

        vx1, vy1 = cur_pt[0] - prev_pt[0], cur_pt[1] - prev_pt[1]
        vx2, vy2 = next_pt[0] - cur_pt[0], next_pt[1] - cur_pt[1]
        l1 = math.hypot(vx1, vy1)
        l2 = math.hypot(vx2, vy2)
        if l1 < 1e-9 or l2 < 1e-9:
            out.append(cur_pt)
            continue

        ux1, uy1 = vx1 / l1, vy1 / l1
        ux2, uy2 = vx2 / l2, vy2 / l2
        # Outward right-hand normals for CCW polygon in Gerber Y-up
        a1 = math.atan2(-ux1, uy1)
        a2 = math.atan2(-ux2, uy2)
        delta = a2 - a1
        while delta < 0.0:
            delta += 2.0 * math.pi
        if delta > math.pi + 1e-3:
            delta = 0.0

        for k in range(arc_steps + 1):
            a = a1 + delta * (k / arc_steps)
            out.append((cur_pt[0] + radius * math.cos(a), cur_pt[1] + radius * math.sin(a)))
    return out


def _aperture_geometry(ap_type: str, params_str: str, unit_scale: float) -> Tuple[str, object]:
    """Return ('circle', diameter_mm) or ('poly', [(x, y_gbr), ...]) for a Gerber aperture."""
    parts = [p for p in params_str.split("X") if p] if params_str else []
    nums: List[float] = []
    for p in parts:
        try:
            nums.append(float(p))
        except ValueError:
            pass

    s = unit_scale
    if ap_type == "C" and nums:
        return ("circle", nums[0] * s)
    if ap_type == "R" and len(nums) >= 2:
        hw, hh = 0.5 * nums[0] * s, 0.5 * nums[1] * s
        return ("poly", [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)])
    if ap_type == "O" and len(nums) >= 2:
        w, h = nums[0] * s, nums[1] * s
        if abs(w - h) < 1e-6:
            return ("circle", w)
        if w > h:
            d = 0.5 * (w - h)
            return ("poly", _stadium_polygon((-d, 0.0), (d, 0.0), 0.5 * h))
        d = 0.5 * (h - w)
        return ("poly", _stadium_polygon((0.0, -d), (0.0, d), 0.5 * w))
    if ap_type == "P" and nums:
        r = 0.5 * nums[0] * s
        n = max(3, int(round(nums[1]))) if len(nums) >= 2 else 6
        rot = math.radians(nums[2]) if len(nums) >= 3 else 0.0
        return ("poly", [(r * math.cos(rot + 2.0 * math.pi * k / n), r * math.sin(rot + 2.0 * math.pi * k / n)) for k in range(n)])
    if ap_type == "RotRect" and len(nums) >= 3:
        hw, hh = 0.5 * nums[0] * s, 0.5 * nums[1] * s
        ang = math.radians(nums[2])
        return ("poly", _rotate_pts_ccw([(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)], ang))
    if ap_type == "RoundRect":
        if len(nums) >= 9:
            r = nums[0] * s
            corners = [
                (nums[1] * s, nums[2] * s),
                (nums[3] * s, nums[4] * s),
                (nums[5] * s, nums[6] * s),
                (nums[7] * s, nums[8] * s),
            ]
            if len(nums) >= 10 and abs(nums[9]) > 1e-6:
                corners = _rotate_pts_ccw(corners, math.radians(nums[9]))
            return ("poly", _rounded_poly_from_corners(corners, r))
        if len(nums) >= 5:
            r = 0.5 * nums[0] * s
            p0 = (nums[1] * s, nums[2] * s)
            p1 = (nums[3] * s, nums[4] * s)
            corners = [p0, p1, (-p0[0], -p0[1]), (-p1[0], -p1[1])]
            return ("poly", _rounded_poly_from_corners(corners, r))
    if ap_type == "HorizOval" and len(nums) >= 5:
        r = 0.5 * nums[0] * s
        p1 = (nums[1] * s, nums[2] * s)
        p2 = (nums[3] * s, nums[4] * s)
        if len(nums) >= 6 and abs(nums[5]) > 1e-6:
            p1, p2 = _rotate_pts_ccw([p1, p2], math.radians(nums[5]))
        return ("poly", _stadium_polygon(p1, p2, r))

    if nums:
        hw = 0.5 * max(abs(v) for v in nums) * s
        if hw > 0:
            return ("poly", [(-hw, -hw), (hw, -hw), (hw, hw), (-hw, hw)])
    return ("poly", [(-0.25, -0.25), (0.25, -0.25), (0.25, 0.25), (-0.25, 0.25)])


def _sample_gerber_arc(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    i_off: float,
    j_off: float,
    cw: bool,
    multi_quadrant: bool,
) -> List[Tuple[float, float]]:
    """Sample a Gerber G02/G03 arc from (x0, y0) to (x1, y1) in Gerber Y-up mm."""
    if multi_quadrant:
        cx, cy = x0 + i_off, y0 + j_off
    else:
        ai, aj = abs(i_off), abs(j_off)
        best_c = (x0 + i_off, y0 + j_off)
        best_err = float("inf")
        for sx in (1.0, -1.0):
            for sy in (1.0, -1.0):
                tcx, tcy = x0 + sx * ai, y0 + sy * aj
                r0 = math.hypot(x0 - tcx, y0 - tcy)
                r1 = math.hypot(x1 - tcx, y1 - tcy)
                a0 = math.atan2(y0 - tcy, x0 - tcx)
                a1 = math.atan2(y1 - tcy, x1 - tcx)
                d = a1 - a0
                if cw:
                    if d > 0.0:
                        d -= 2.0 * math.pi
                else:
                    if d < 0.0:
                        d += 2.0 * math.pi
                if abs(d) <= math.pi * 0.5 + 1e-3:
                    err = abs(r0 - r1)
                    if err < best_err:
                        best_err = err
                        best_c = (tcx, tcy)
        cx, cy = best_c

    r = math.hypot(x0 - cx, y0 - cy)
    if r < 1e-9:
        return [(x1, y1)]

    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    delta = a1 - a0
    if cw:
        if delta > 0.0:
            delta -= 2.0 * math.pi
        if abs(delta) < 1e-9 and multi_quadrant and math.hypot(x1 - x0, y1 - y0) < 1e-6:
            delta = -2.0 * math.pi
    else:
        if delta < 0.0:
            delta += 2.0 * math.pi
        if abs(delta) < 1e-9 and multi_quadrant and math.hypot(x1 - x0, y1 - y0) < 1e-6:
            delta = 2.0 * math.pi

    steps = max(4, min(128, int(math.ceil(abs(delta) * r / 0.05))))
    pts = [
        (cx + r * math.cos(a0 + delta * (k / steps)), cy + r * math.sin(a0 + delta * (k / steps)))
        for k in range(1, steps)
    ]
    pts.append((x1, y1))
    return pts


def _parse_gerber_primitives(content: str) -> Iterable[Tuple[str, bool, object]]:
    """Yield (kind, polarity_dark, payload) in Gerber Y-up mm from RS-274X text."""
    zero_omitted = True
    int_digits, dec_digits = 4, 6
    unit_scale = 1.0

    apertures: Dict[int, Tuple[str, object]] = {}
    current_ap: Optional[Tuple[str, object]] = None
    polarity_dark = True
    interp_mode = "linear"
    multi_quadrant = True
    cur_x = 0.0
    cur_y_gbr = 0.0
    region_active = False
    region_pts: List[Tuple[float, float]] = []

    flat = re.sub(r"%([^%]*)%", lambda m: "%" + m.group(1).replace("\r", "").replace("\n", "") + "%", content)

    for raw_line in flat.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("G04"):
            continue

        if line == "%MOMM*%":
            unit_scale = 1.0
            continue
        if line == "%MOIN*%":
            unit_scale = 25.4
            continue
        if line == "%LPD*%":
            polarity_dark = True
            continue
        if line == "%LPC*%":
            polarity_dark = False
            continue

        fs_m = _FS_RE.match(line)
        if fs_m:
            zero_omitted = fs_m.group(1) == "L"
            int_digits = int(fs_m.group(3))
            dec_digits = int(fs_m.group(4))
            continue

        add_m = _ADD_RE.match(line)
        if add_m:
            code = int(add_m.group(1))
            ap_type = add_m.group(2)
            params = add_m.group(3) or ""
            apertures[code] = _aperture_geometry(ap_type, params, unit_scale)
            continue

        if line.startswith("%"):
            continue

        if "G36*" in line:
            region_active = True
            region_pts = []
            line = line.replace("G36*", "")
            if not line:
                continue
        if "G37*" in line:
            if region_active and len(region_pts) >= 3:
                yield ("region", polarity_dark, list(region_pts))
            region_active = False
            region_pts = []
            line = line.replace("G37*", "")
            if not line:
                continue

        # Handle standalone or prefixed G01/G02/G03/G74/G75 mode codes
        gm = _G_PREFIX_RE.match(line)
        if gm:
            for gcode in re.findall(r"G\d\d", gm.group(0)):
                if gcode == "G01":
                    interp_mode = "linear"
                elif gcode == "G02":
                    interp_mode = "cw"
                elif gcode == "G03":
                    interp_mode = "ccw"
                elif gcode == "G74":
                    multi_quadrant = False
                elif gcode == "G75":
                    multi_quadrant = True
            line = line[gm.end():]
            if not line or line == "*":
                continue

        sel_m = re.match(r"^(?:G54)?D(\d{2,})\*$", line)
        if sel_m:
            current_ap = apertures.get(int(sel_m.group(1)))
            continue

        op_m = re.search(r"D0([123])\*$", line)
        if not op_m:
            continue
        op = op_m.group(1)
        coord_part = line[: op_m.start()]
        cm = _COORD_RE.search(coord_part)

        next_x, next_y = cur_x, cur_y_gbr
        i_off, j_off = 0.0, 0.0
        if cm:
            if cm.group(1) is not None:
                next_x = _parse_gerber_coord(cm.group(1), int_digits, dec_digits, zero_omitted) * unit_scale
            if cm.group(2) is not None:
                next_y = _parse_gerber_coord(cm.group(2), int_digits, dec_digits, zero_omitted) * unit_scale
            if cm.group(3) is not None:
                i_off = _parse_gerber_coord(cm.group(3), int_digits, dec_digits, zero_omitted) * unit_scale
            if cm.group(4) is not None:
                j_off = _parse_gerber_coord(cm.group(4), int_digits, dec_digits, zero_omitted) * unit_scale

        if op == "2":  # Move
            if region_active:
                if len(region_pts) >= 3:
                    yield ("region", polarity_dark, list(region_pts))
                region_pts = [(next_x, next_y)]
        elif op == "1":  # Interpolate
            if interp_mode in ("cw", "ccw") and (abs(i_off) > 0.0 or abs(j_off) > 0.0):
                seg_pts = _sample_gerber_arc(
                    cur_x,
                    cur_y_gbr,
                    next_x,
                    next_y,
                    i_off,
                    j_off,
                    cw=(interp_mode == "cw"),
                    multi_quadrant=multi_quadrant,
                )
            else:
                seg_pts = [(next_x, next_y)]

            if region_active:
                if not region_pts:
                    region_pts.append((cur_x, cur_y_gbr))
                region_pts.extend(seg_pts)
            else:
                stroke_d = 0.15
                if current_ap and current_ap[0] == "circle":
                    stroke_d = float(current_ap[1])
                elif current_ap and current_ap[0] == "poly":
                    pts_ap = current_ap[1]
                    if pts_ap:
                        xs = [p[0] for p in pts_ap]
                        ys = [p[1] for p in pts_ap]
                        stroke_d = max(max(xs) - min(xs), max(ys) - min(ys), 0.05)
                yield ("stroke", polarity_dark, ([(cur_x, cur_y_gbr)] + seg_pts, stroke_d))
        elif op == "3":  # Flash
            if current_ap:
                kind, payload = current_ap
                if kind == "circle":
                    yield ("flash_circle", polarity_dark, (next_x, next_y, float(payload)))
                elif kind == "poly":
                    translated = [(next_x + lx, next_y + ly) for lx, ly in payload]
                    yield ("flash_poly", polarity_dark, translated)

        cur_x, cur_y_gbr = next_x, next_y

    if region_active and len(region_pts) >= 3:
        yield ("region", polarity_dark, list(region_pts))


def parse_solder_mask_gerber(content: str) -> List[str]:
    """Parse a Gerber solder mask layer into SVG path data strings (10-mil, Y down)."""
    shapes: List[str] = []
    for kind, dark, payload in _parse_gerber_primitives(content):
        if not dark:
            continue
        if kind == "flash_circle":
            cx, cy_gbr, d_mm = payload
            p = _circle_svg_path(cx, -cy_gbr, d_mm)
            if p:
                shapes.append(p)
        elif kind in ("flash_poly", "region"):
            p = _poly_svg_path(payload)
            if p:
                shapes.append(p)
        elif kind == "stroke":
            pts_gbr, d_mm = payload
            if d_mm > 0.0 and len(pts_gbr) >= 2:
                r = 0.5 * d_mm
                for i in range(len(pts_gbr) - 1):
                    p = _poly_svg_path(_stadium_polygon(pts_gbr[i], pts_gbr[i + 1], r))
                    if p:
                        shapes.append(p)
    return shapes


def render_gerber_layer_to_mask(
    content: str,
    bounds: BoardBounds,
    canvas_w: int,
    canvas_h: int,
) -> Image.Image:
    """Rasterize a Gerber layer into an 8-bit grayscale mask in Top-view canvas coordinates."""
    mask = Image.new("L", (canvas_w, canvas_h), 0)
    draw = ImageDraw.Draw(mask)
    sx = canvas_w / max(bounds.width, 1e-6)
    sy = canvas_h / max(bounds.height, 1e-6)

    def to_px(x_mm: float, y_gbr_mm: float) -> Tuple[float, float]:
        return ((x_mm - bounds.min_x) * sx, (-y_gbr_mm - bounds.min_y) * sy)

    for kind, dark, payload in _parse_gerber_primitives(content):
        fill = 255 if dark else 0
        if kind == "flash_circle":
            cx, cy_gbr, d_mm = payload
            px, py = to_px(cx, cy_gbr)
            rx = 0.5 * d_mm * sx
            ry = 0.5 * d_mm * sy
            if rx > 0.0 and ry > 0.0:
                draw.ellipse([px - rx, py - ry, px + rx, py + ry], fill=fill)
        elif kind in ("flash_poly", "region"):
            pts_px = [to_px(x, y) for x, y in payload]
            if len(pts_px) >= 3:
                draw.polygon(pts_px, fill=fill)
        elif kind == "stroke":
            pts_gbr, d_mm = payload
            if len(pts_gbr) < 2:
                continue
            pts_px = [to_px(x, y) for x, y in pts_gbr]
            w_px = max(1, int(round(d_mm * sx)))
            draw.line(pts_px, fill=fill, width=w_px)
            if w_px >= 2:
                r_px = 0.5 * w_px
                for vx, vy in pts_px:
                    draw.ellipse([vx - r_px, vy - r_px, vx + r_px, vy + r_px], fill=fill)

    return mask


# ---------------------------------------------------------------------------
# Artwork compositing + SVG + Encryption
# ---------------------------------------------------------------------------


def composite_artwork_to_png(
    bounds: BoardBounds,
    artworks: Sequence[PlacedArtwork],
    dpi: int = 600,
    white_background: bool = True,
    mirror_result_x: bool = False,
    silkscreen_gerber: str = "",
    silkscreen_color: Tuple[int, int, int] = (255, 255, 255),
) -> bytes:
    """Render placed artworks (and optional KiCad silkscreen Gerber) onto a board-sized PNG."""
    px_per_mm = dpi / 25.4
    canvas_w = max(1, int(round(bounds.width * px_per_mm)))
    canvas_h = max(1, int(round(bounds.height * px_per_mm)))

    # Clamp extreme canvas dimensions to avoid OOM on gigantic boards
    max_dim = 16000
    if max(canvas_w, canvas_h) > max_dim:
        scale_down = max_dim / max(canvas_w, canvas_h)
        canvas_w = max(1, int(round(canvas_w * scale_down)))
        canvas_h = max(1, int(round(canvas_h * scale_down)))
        px_per_mm *= scale_down

    bg = (255, 255, 255, 255) if white_background else (0, 0, 0, 0)
    canvas = Image.new("RGBA", (canvas_w, canvas_h), bg)

    for art in artworks:
        im = Image.open(io.BytesIO(art.png_bytes)).convert("RGBA")
        if art.mirror_x:
            im = ImageOps.mirror(im)

        target_w = max(1, int(round(art.width_mm * px_per_mm)))
        target_h = max(1, int(round(art.height_mm * px_per_mm)))
        im = im.resize((target_w, target_h), Image.Resampling.LANCZOS)

        if abs(art.angle_deg) > 1e-4:
            # KiCad angles are counter-clockwise in screen space
            im = im.rotate(art.angle_deg, resample=Image.Resampling.BICUBIC, expand=True)

        cx_px = (art.center_x_mm - bounds.min_x) * px_per_mm
        cy_px = (art.center_y_mm - bounds.min_y) * px_per_mm
        paste_x = int(round(cx_px - im.width * 0.5))
        paste_y = int(round(cy_px - im.height * 0.5))
        canvas.alpha_composite(im, dest=(paste_x, paste_y))

    if mirror_result_x:
        canvas = ImageOps.mirror(canvas)

    if silkscreen_gerber:
        silk_mask = render_gerber_layer_to_mask(silkscreen_gerber, bounds, canvas_w, canvas_h)
        overlay = Image.new("RGBA", (canvas_w, canvas_h), (*silkscreen_color, 255))
        overlay.putalpha(silk_mask)
        canvas.alpha_composite(overlay)

    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()


def _outline_svg_path(
    bounds: BoardBounds,
    outline_poly: Optional[Sequence[Tuple[float, float]]],
    clip_margin_10mil: float = 0.8374,
) -> str:
    if outline_poly and len(outline_poly) >= 3:
        pts = [
            (mm_to_mil_10(x), mm_to_mil_10(y))
            for x, y in outline_poly
        ]
        parts = [f"M {pts[0][0]:.5f} {pts[0][1]:.5f} L "]
        for x, y in pts[1:]:
            parts.append(f"{x:.5f} {y:.5f} ")
        parts.append(f"{pts[0][0]:.5f} {pts[0][1]:.5f} ")
        return "".join(parts)

    min_x = mm_to_mil_10(bounds.min_x) + clip_margin_10mil
    max_x = mm_to_mil_10(bounds.max_x) - clip_margin_10mil
    min_y = mm_to_mil_10(bounds.min_y) + clip_margin_10mil
    max_y = mm_to_mil_10(bounds.max_y) - clip_margin_10mil
    return f"M {max_x:.5f} {max_y:.5f} L {min_x:.5f} {max_y:.5f} {min_x:.5f} {min_y:.5f} {max_x:.5f} {min_y:.5f} {max_x:.5f} {max_y:.5f} "


def build_colorful_svg(
    side: str,
    bounds: BoardBounds,
    png_bytes: bytes,
    mask_paths: Sequence[str],
    marks_mm: Sequence[Tuple[float, float]],
    outline_poly: Optional[Sequence[Tuple[float, float]]] = None,
    white_background: bool = True,
) -> str:
    """Build the SVG payload for FCTS ('top') or FCBS ('bottom')."""
    expand = 0.5  # 10-mil units

    min_x = mm_to_mil_10(bounds.min_x)
    max_x = mm_to_mil_10(bounds.max_x)
    min_y = mm_to_mil_10(bounds.min_y)
    max_y = mm_to_mil_10(bounds.max_y)
    w = mm_to_mil_10(bounds.width)
    h = mm_to_mil_10(bounds.height)
    center_x = min_x + w * 0.5

    with Image.open(io.BytesIO(png_bytes)) as im:
        img_w, img_h = im.size
    b64_uri = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")

    # mark-points attribute uses Gerber Y (-y_kicad) in 10-mil units
    mark_str = " ".join(
        f"{mm_to_mil_10(mx):.5f} {mm_to_mil_10(-my):.5f}" for mx, my in marks_mm
    )

    clip0_d = _outline_svg_path(bounds, outline_poly)
    outer_rect = (
        f"M {max_x + expand:.5f} {min_y - expand:.5f} "
        f"L {max_x + expand:.5f} {max_y + expand:.5f} "
        f"{min_x - expand:.5f} {max_y + expand:.5f} "
        f"{min_x - expand:.5f} {min_y - expand:.5f} "
        f"{max_x + expand:.5f} {min_y - expand:.5f} "
    )
    clip1_d = outer_rect + "".join(mask_paths)

    if side == "bottom":
        g_transform = f"scale(-1 1) translate({-2.0 * center_x:.6f} 0)"
        img_matrix = f"matrix({-(w / img_w):.8f} 0 0 {h / img_h:.8f} {max_x:.6f} {min_y:.6f})"
    else:
        g_transform = "scale(1 1) translate(0 0)"
        img_matrix = f"matrix({w / img_w:.8f} 0 0 {h / img_h:.8f} {min_x:.6f} {min_y:.6f})"

    bg_rect = ""
    if white_background:
        bg_d = (
            f"M {min_x - expand:.5f} {max_y + expand:.5f} "
            f"L {min_x - expand:.5f} {min_y - expand:.5f} "
            f"{max_x + expand:.5f} {min_y - expand:.5f} "
            f"{max_x + expand:.5f} {max_y + expand:.5f} "
            f"{min_x - expand:.5f} {max_y + expand:.5f} "
        )
        bg_rect = (
            f'    <path d="{bg_d}" fill="#FFFFFF" stroke="none" stroke-width="0" id="background"/>\n'
        )

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        f'<svg width="{bounds.width:.6f}mm" height="{bounds.height:.6f}mm" '
        f'boardBox="{min_x:.5f} {min_y:.5f} {w:.5f} {h:.5f}" '
        f'viewBox="{min_x:.5f} {min_y:.5f} {w:.5f} {h:.5f}" '
        'version="1.1" eda-version="2.6(2026-09-15)" '
        f'mark-points="{escape(mark_str)}" '
        'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
        'xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        'xmlns="http://www.w3.org/2000/svg" '
        'xmlns:svg="http://www.w3.org/2000/svg">\n'
        "  <defs>\n"
        '    <clipPath id="clipPath0">\n'
        f'      <path d="{clip0_d}" id="outline0" stroke="none" style="fill-opacity:1;fill-rule:nonzero;fill:black;"/>\n'
        "    </clipPath>\n"
        "  </defs>\n"
        "  <defs>\n"
        '    <clipPath id="clipPath1" clip-path="url(#clipPath0)">\n'
        f'      <path d="{clip1_d}" id="solder1" stroke="none" style="fill-opacity:1;fill-rule:evenodd;clip-rule:evenodd;fill:black;"/>\n'
        "    </clipPath>\n"
        "  </defs>\n"
        f'  <g clip-path="url(#clipPath1)" transform="{g_transform}">\n'
        f"{bg_rect}"
        f'    <image width="{img_w}" height="{img_h}" preserveAspectRatio="none" '
        f'xlink:href="{b64_uri}" transform="{img_matrix}"/>\n'
        "  </g>\n"
        "</svg>\n"
    )


def build_board_outline_svg(
    bounds: BoardBounds,
    outline_poly: Optional[Sequence[Tuple[float, float]]] = None,
) -> str:
    """Build the SVG payload for Fabrication_ColorfulBoardOutlineLayer.FCBO."""
    ox = mm_to_mil_10(bounds.min_x)
    oy = mm_to_mil_10(bounds.min_y)
    w = mm_to_mil_10(bounds.width)
    h = mm_to_mil_10(bounds.height)

    if outline_poly and len(outline_poly) >= 3:
        d = _outline_svg_path(bounds, outline_poly, clip_margin_10mil=0.0)
        shape_elem = f'  <path d="{d}" fill="none" stroke="green" stroke-width="2" id="boardOutline"/>\n'
    else:
        shape_elem = (
            f'  <rect x="{ox:.5f}" y="{oy:.5f}" width="{w:.5f}" height="{h:.5f}" '
            'fill="none" stroke="green" stroke-width="2" id="boardOutline"/>\n'
        )

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        f'<svg width="{bounds.width:.6f}mm" height="{bounds.height:.6f}mm" '
        f'viewBox="{ox:.5f} {oy:.5f} {w:.5f} {h:.5f}" '
        'version="1.1" '
        'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
        'xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        'xmlns="http://www.w3.org/2000/svg" '
        'xmlns:svg="http://www.w3.org/2000/svg">\n'
        f"{shape_elem}"
        "</svg>\n"
    )


def _format_gerber_45(x_mm: float, y_gbr_mm: float) -> Tuple[str, str]:
    xi = int(round(x_mm * 100_000.0))
    yi = int(round(y_gbr_mm * 100_000.0))
    return f"{xi}", f"{yi}"


def build_outline_mark_gerber(
    bounds: BoardBounds,
    marks_mm: Sequence[Tuple[float, float]],
    outline_poly: Optional[Sequence[Tuple[float, float]]] = None,
) -> str:
    """Build plain Gerber Fabrication_ColorfulBoardOutlineMark.FCBM."""
    poly = list(outline_poly) if outline_poly and len(outline_poly) >= 3 else [
        (bounds.min_x, bounds.min_y),
        (bounds.max_x, bounds.min_y),
        (bounds.max_x, bounds.max_y),
        (bounds.min_x, bounds.max_y),
    ]

    import datetime

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "G04 Layer: Fabrication_ColorfulBoardOutlineMark*",
        f"G04 EasyEDA Pro v4.1.105, {now}*",
        "G04 Gerber Generator version 0.3*",
        "G04 Scale: 100 percent, Rotated: No, Reflected: No*",
        "G04 Dimensions in millimeters*",
        "G04 Leading zeros omitted, absolute positions, 4 integers and 5 decimals*",
        "G04 Generated by one-click*",
        "%FSLAX45Y45*%",
        "%MOMM*%",
        "%ADD10C,0.2*%",
        "%ADD11C,0.508*%",
        "G75*",
        "",
        "G04 colorScreenSilk_board Start*",
        "G04 PolygonModel Start*",
        "G54D10*",
    ]

    sx, sy = _format_gerber_45(poly[0][0], -poly[0][1])
    lines.append(f"G01X{sx}Y{sy}D02*")
    for x_mm, y_mm in poly[1:] + [poly[0]]:
        gx, gy = _format_gerber_45(x_mm, -y_mm)
        lines.append(f"G01X{gx}Y{gy}D01*")

    lines.extend([
        "G04 PolygonModel End*",
        "G04 colorScreenSilk_board End*",
        "",
        "G04 colorScreenSilk_Mark Start*",
        "G54D11*",
    ])

    for mx, my in marks_mm:
        gx, gy = _format_gerber_45(mx, -my)
        lines.append(f"G01X{gx}Y{gy}D03*")

    lines.extend([
        "G04 colorScreenSilk_Mark End*",
        "",
        "M02*",
        "",
    ])

    return add_hash_aperture("\n".join(lines))


def encrypt_jlc_svg(svg_text: str, public_key_pem: str = JLC_RSA_PUBLIC_KEY_PEM) -> bytes:
    """Encrypt an SVG payload into JLCPCB's RSA-2048 + AES-128-GCM binary container."""
    aes_key = secrets.token_bytes(16)
    aes_iv = secrets.token_bytes(16)
    plaintext = svg_text.encode("utf-8")

    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        pub = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
        oaep = asym_padding.OAEP(
            mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        )
        enc_key = pub.encrypt(aes_key, oaep)
        enc_iv = pub.encrypt(aes_iv, oaep)
        ciphertext = AESGCM(aes_key).encrypt(aes_iv, plaintext, None)
        return enc_key + enc_iv + ciphertext
    except ImportError:
        from Crypto.Cipher import AES, PKCS1_OAEP
        from Crypto.Hash import SHA256
        from Crypto.PublicKey import RSA

        rsa_key = RSA.import_key(public_key_pem)
        cipher_rsa = PKCS1_OAEP.new(rsa_key, hashAlgo=SHA256)
        enc_key = cipher_rsa.encrypt(aes_key)
        enc_iv = cipher_rsa.encrypt(aes_iv)

        cipher_aes = AES.new(aes_key, AES.MODE_GCM, nonce=aes_iv)
        ciphertext, tag = cipher_aes.encrypt_and_digest(plaintext)
        return enc_key + enc_iv + ciphertext + tag
