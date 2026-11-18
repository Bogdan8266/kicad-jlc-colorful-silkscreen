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
# Solder Mask Gerber parser (RS-274X -> SVG clipPath paths in 10-mil coords)
# ---------------------------------------------------------------------------


_FS_RE = re.compile(r"%FS([LT])([AI])X(\d)(\d)Y(\d)(\d)\*%")
_ADD_RE = re.compile(r"^%ADD(\d+)([A-Za-z_][A-Za-z0-9_]*)(?:,([^*]+))?\*%$")
_COORD_RE = re.compile(r"(?:X([+-]?\d+))?(?:Y([+-]?\d+))?(?:I([+-]?\d+))?(?:J([+-]?\d+))?")


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


def _rect_svg_path(cx_mm: float, cy_kicad_mm: float, w_mm: float, h_mm: float) -> str:
    cx = mm_to_mil_10(cx_mm)
    cy = mm_to_mil_10(cy_kicad_mm)
    hw = mm_to_mil_10(w_mm) * 0.5
    hh = mm_to_mil_10(h_mm) * 0.5
    x0, x1 = cx - hw, cx + hw
    y0, y1 = cy - hh, cy + hh
    return f"M {x0:.5f} {y0:.5f} L {x1:.5f} {y0:.5f} {x1:.5f} {y1:.5f} {x0:.5f} {y1:.5f} {x0:.5f} {y0:.5f} "


def _circle_svg_path(cx_mm: float, cy_kicad_mm: float, d_mm: float) -> str:
    cx = mm_to_mil_10(cx_mm)
    cy = mm_to_mil_10(cy_kicad_mm)
    r = mm_to_mil_10(d_mm) * 0.5
    if r <= 0:
        return ""
    return (
        f"M {cx + r:.5f} {cy:.5f} "
        f"A {r:.5f} {r:.5f} 0 1 0 {cx - r:.5f} {cy:.5f} "
        f"A {r:.5f} {r:.5f} 0 1 0 {cx + r:.5f} {cy:.5f} "
    )


def _aperture_shape_from_def(ap_type: str, params_str: str) -> Tuple[str, float, float]:
    """Return ('circle'|'rect', w_mm, h_mm) for an aperture definition."""
    parts = [p for p in params_str.split("X") if p] if params_str else []
    nums: List[float] = []
    for p in parts:
        try:
            nums.append(float(p))
        except ValueError:
            pass

    if ap_type == "C" and nums:
        return ("circle", nums[0], nums[0])
    if ap_type in ("R", "O") and len(nums) >= 2:
        return ("rect", nums[0], nums[1])
    if ap_type == "P" and nums:
        return ("circle", nums[0], nums[0])
    if ap_type == "RoundRect" and len(nums) >= 3:
        # EasyEDA RoundRect: radius X x1 X y1 X x2 X y2 ...
        xs = nums[1::2]
        ys = nums[2::2]
        if xs and ys:
            return ("rect", max(xs) - min(xs) + nums[0], max(ys) - min(ys) + nums[0])
    if nums:
        # Macro aperture fallback (e.g. KiCad RoundRect macro): estimate bbox
        d = max(abs(v) for v in nums)
        if d > 0:
            return ("rect", d, d)
    return ("rect", 0.5, 0.5)


def parse_solder_mask_gerber(content: str) -> List[str]:
    """Parse a Gerber solder mask layer into SVG path data strings (10-mil, Y down)."""
    zero_omitted = True  # L = leading zeros omitted
    int_digits, dec_digits = 4, 6
    unit_scale = 1.0  # mm

    apertures: Dict[int, Tuple[str, float, float]] = {}
    current_ap: Optional[Tuple[str, float, float]] = None
    cur_x = 0.0
    cur_y_gbr = 0.0  # Gerber Y (negated KiCad Y)
    region_active = False
    region_pts: List[Tuple[float, float]] = []
    shapes: List[str] = []

    # Flatten multi-line %...% blocks before line-by-line scan
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
            apertures[code] = _aperture_shape_from_def(ap_type, params)
            continue

        if line.startswith("%"):
            continue

        # Handle G36 / G37 region toggles
        if "G36*" in line:
            region_active = True
            region_pts = []
            line = line.replace("G36*", "")
            if not line:
                continue
        if "G37*" in line:
            if region_active and len(region_pts) >= 3:
                pts_str = " ".join(
                    f"{'M' if idx == 0 else 'L'} {mm_to_mil_10(px):.5f} {mm_to_mil_10(-py):.5f}"
                    for idx, (px, py) in enumerate(region_pts)
                )
                shapes.append(pts_str + " Z ")
            region_active = False
            region_pts = []
            line = line.replace("G37*", "")
            if not line:
                continue

        # Select aperture (D10* or G54D10*)
        sel_m = re.match(r"^(?:G54)?D(\d{2,})\*$", line)
        if sel_m:
            current_ap = apertures.get(int(sel_m.group(1)))
            continue

        # Operation code (D01 / D02 / D03)
        op_m = re.search(r"D0([123])\*$", line)
        if not op_m:
            continue
        op = op_m.group(1)
        coord_part = re.sub(r"^(?:G0[123]|G75)+", "", line[: op_m.start()])
        cm = _COORD_RE.search(coord_part)
        if cm:
            if cm.group(1) is not None:
                cur_x = _parse_gerber_coord(cm.group(1), int_digits, dec_digits, zero_omitted) * unit_scale
            if cm.group(2) is not None:
                cur_y_gbr = _parse_gerber_coord(cm.group(2), int_digits, dec_digits, zero_omitted) * unit_scale

        if op == "2":  # Move
            if region_active:
                if len(region_pts) >= 3:
                    pts_str = " ".join(
                        f"{'M' if idx == 0 else 'L'} {mm_to_mil_10(px):.5f} {mm_to_mil_10(-py):.5f}"
                        for idx, (px, py) in enumerate(region_pts)
                    )
                    shapes.append(pts_str + " Z ")
                region_pts = [(cur_x, cur_y_gbr)]
        elif op == "1":  # Interpolate
            if region_active:
                if not region_pts:
                    region_pts.append((cur_x, cur_y_gbr))
                else:
                    region_pts.append((cur_x, cur_y_gbr))
        elif op == "3":  # Flash
            if current_ap:
                kind, w_mm, h_mm = current_ap
                if kind == "circle":
                    p = _circle_svg_path(cur_x, -cur_y_gbr, w_mm)
                else:
                    p = _rect_svg_path(cur_x, -cur_y_gbr, w_mm, h_mm)
                if p:
                    shapes.append(p)

    return shapes


# ---------------------------------------------------------------------------
# Artwork compositing + SVG + Encryption
# ---------------------------------------------------------------------------


def composite_artwork_to_png(
    bounds: BoardBounds,
    artworks: Sequence[PlacedArtwork],
    dpi: int = 600,
    white_background: bool = True,
    mirror_result_x: bool = False,
) -> bytes:
    """Render placed artworks onto a board-sized PNG canvas."""
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
