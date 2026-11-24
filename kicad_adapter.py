"""KiCad pcbnew adapter for reading board geometry/images and exporting JLCPCB Gerbers."""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PIL import Image

if __package__:
    from .jlc_colorful import (
        BoardBounds,
        PlacedArtwork,
        build_board_outline_svg,
        build_colorful_svg,
        build_outline_mark_gerber,
        composite_artwork_to_png,
        compute_interior_marks,
        encrypt_jlc_svg,
        parse_solder_mask_gerber,
    )
    from .jlc_gerber import (
        HOW_TO_ORDER_TXT,
        map_kicad_gerber_filename,
        normalize_excellon_drill,
        process_gerber_for_jlc,
    )
    from .sexpr import KiCadImageRecord, parse_kicad_pcb_images
else:
    from jlc_colorful import (
        BoardBounds,
        PlacedArtwork,
        build_board_outline_svg,
        build_colorful_svg,
        build_outline_mark_gerber,
        composite_artwork_to_png,
        compute_interior_marks,
        encrypt_jlc_svg,
        parse_solder_mask_gerber,
    )
    from jlc_gerber import (
        HOW_TO_ORDER_TXT,
        map_kicad_gerber_filename,
        normalize_excellon_drill,
        process_gerber_for_jlc,
    )
    from sexpr import KiCadImageRecord, parse_kicad_pcb_images


IU_PER_MM = 1_000_000.0  # KiCad internal units = nanometers
KICAD_IMAGE_DEFAULT_DPI = 300.0
KICAD_IMAGE_BASE_MM_PER_PX = 25.4 / KICAD_IMAGE_DEFAULT_DPI  # Default 300 DPI when PNG omits pHYs

CANONICAL_LAYER_ALIASES: Dict[str, Tuple[str, ...]] = {
    "User.1": ("User.1",),
    "User.2": ("User.2",),
    "User.3": ("User.3",),
    "User.4": ("User.4",),
    "User.5": ("User.5",),
    "User.6": ("User.6",),
    "User.7": ("User.7",),
    "User.8": ("User.8",),
    "User.9": ("User.9",),
    "User.Drawings": ("User.Drawings", "Dwgs.User"),
    "Dwgs.User": ("User.Drawings", "Dwgs.User"),
    "User.Comments": ("User.Comments", "Cmts.User"),
    "Cmts.User": ("User.Comments", "Cmts.User"),
    "User.Eco1": ("User.Eco1", "Eco1.User"),
    "Eco1.User": ("User.Eco1", "Eco1.User"),
    "User.Eco2": ("User.Eco2", "Eco2.User"),
    "Eco2.User": ("User.Eco2", "Eco2.User"),
    "F.Silkscreen": ("F.Silkscreen", "F.SilkS"),
    "F.SilkS": ("F.Silkscreen", "F.SilkS"),
    "B.Silkscreen": ("B.Silkscreen", "B.SilkS"),
    "B.SilkS": ("B.Silkscreen", "B.SilkS"),
}

USER_LAYER_CHOICES: List[str] = [
    "User.1",
    "User.2",
    "User.3",
    "User.4",
    "User.Drawings",
    "User.Comments",
    "User.Eco1",
    "User.Eco2",
    "F.Silkscreen",
    "B.Silkscreen",
]


@dataclass
class ExportOptions:
    """Configuration for generating a JLCPCB colorful silkscreen ZIP."""

    output_zip_path: str
    top_mode: str = "layer"  # "layer" | "file" | "none"
    top_layer: str = "User.1"
    top_image_path: str = ""
    bottom_mode: str = "layer"  # "layer" | "file" | "none"
    bottom_layer: str = "User.2"
    bottom_image_path: str = ""
    carve_solder_mask: bool = True
    white_background: bool = True
    mirror_bottom: bool = True
    include_pcb_silkscreen: bool = True
    silkscreen_color: Tuple[int, int, int] = (255, 255, 255)
    dpi: int = 600
    refill_zones: bool = False


def _pcbnew(mod=None):
    if mod is not None:
        return mod
    import pcbnew

    return pcbnew


def _try_call(obj, name: str, *args) -> bool:
    fn = getattr(obj, name, None)
    if fn is None:
        return False
    try:
        fn(*args)
    except (TypeError, AttributeError, ValueError):
        return False
    return True


def read_board_outline(board, pcbnew_mod=None) -> Tuple[BoardBounds, List[Tuple[float, float]]]:
    """Extract outer board polygon and bounding box in KiCad mm coordinates."""
    pcbnew = _pcbnew(pcbnew_mod)
    polys = pcbnew.SHAPE_POLY_SET()
    ok = board.GetBoardPolygonOutlines(polys, True)

    best_poly: List[Tuple[float, float]] = []
    best_area = -1.0
    if ok and polys.OutlineCount() > 0:
        for idx in range(polys.OutlineCount()):
            outline = polys.Outline(idx)
            pts = [
                (outline.CPoint(i).x / IU_PER_MM, outline.CPoint(i).y / IU_PER_MM)
                for i in range(outline.PointCount())
            ]
            if len(pts) < 3:
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            area = (max(xs) - min(xs)) * (max(ys) - min(ys))
            if area > best_area:
                best_area = area
                best_poly = pts

    if best_poly:
        xs = [p[0] for p in best_poly]
        ys = [p[1] for p in best_poly]
        return BoardBounds(min(xs), max(xs), min(ys), max(ys)), best_poly

    bb = board.GetBoardEdgesBoundingBox()
    if bb.GetWidth() > 0 and bb.GetHeight() > 0:
        min_x = bb.GetX() / IU_PER_MM
        min_y = bb.GetY() / IU_PER_MM
        max_x = (bb.GetX() + bb.GetWidth()) / IU_PER_MM
        max_y = (bb.GetY() + bb.GetHeight()) / IU_PER_MM
        return BoardBounds(min_x, max_x, min_y, max_y), []

    raise RuntimeError("No board outline found on Edge.Cuts.")


def read_live_pcb_images(board, pcbnew_mod=None) -> List[KiCadImageRecord]:
    """Read all `(image ...)` items from the live board (including unsaved edits)."""
    pcbnew = _pcbnew(pcbnew_mod)
    with tempfile.NamedTemporaryFile(suffix=".kicad_pcb", delete=False) as tf:
        tmp_path = tf.name
    try:
        pcbnew.SaveBoard(tmp_path, board)
        with open(tmp_path, "r", encoding="utf-8") as f:
            text = f.read()
        return parse_kicad_pcb_images(text)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _layer_matches(record_layer: str, target_layer: str, board=None) -> bool:
    aliases = set(CANONICAL_LAYER_ALIASES.get(target_layer, (target_layer,)))
    if board is not None:
        try:
            lid = board.GetLayerID(target_layer)
            if lid >= 0:
                aliases.add(board.GetLayerName(lid))
                aliases.add(board.GetStandardLayerName(lid))
        except Exception:
            pass
    return record_layer in aliases


def _collect_board_image_geometry(
    board,
    pcbnew_mod=None,
) -> Dict[str, Tuple[float, float, float, float]]:
    """Collect {uuid: (center_x_mm, center_y_mm, width_mm, height_mm)} from live pcbnew items."""
    if board is None:
        return {}
    try:
        pcbnew = _pcbnew(pcbnew_mod)
        img_type = getattr(pcbnew, "PCB_REFERENCE_IMAGE_T", None)
        cast_fn = getattr(pcbnew, "Cast_to_PCB_REFERENCE_IMAGE", None)
        if img_type is None or cast_fn is None:
            return {}

        geom: Dict[str, Tuple[float, float, float, float]] = {}

        def _record_item(item) -> None:
            if item.Type() != img_type:
                return
            ri = cast_fn(item)
            uuid_str = str(ri.m_Uuid.AsString())
            pos = ri.GetPosition()
            bb = ri.GetBoundingBox()
            if uuid_str and bb.GetWidth() > 0 and bb.GetHeight() > 0:
                geom[uuid_str] = (
                    pos.x / IU_PER_MM,
                    pos.y / IU_PER_MM,
                    bb.GetWidth() / IU_PER_MM,
                    bb.GetHeight() / IU_PER_MM,
                )

        for item in board.GetDrawings():
            _record_item(item)
        for fp in board.GetFootprints():
            for item in fp.GraphicalItems():
                _record_item(item)
        return geom
    except Exception:
        return {}


def _image_dpi_from_pil(im: Image.Image) -> Tuple[float, float]:
    """Read embedded PNG/image DPI metadata, defaulting to KiCad's 300 DPI."""
    dpi = im.info.get("dpi")
    if isinstance(dpi, (tuple, list)) and len(dpi) >= 2:
        try:
            dx = float(dpi[0])
            dy = float(dpi[1])
            if dx > 0 and dy > 0:
                return (dx, dy)
        except (TypeError, ValueError):
            pass
    return (KICAD_IMAGE_DEFAULT_DPI, KICAD_IMAGE_DEFAULT_DPI)


def records_to_placed_artworks(
    records: Sequence[KiCadImageRecord],
    layer_filter: str,
    board=None,
    pcbnew_mod=None,
) -> List[PlacedArtwork]:
    """Convert KiCad `(image ...)` records on a given layer into PlacedArtwork objects."""
    board_geom = _collect_board_image_geometry(board, pcbnew_mod=pcbnew_mod)
    artworks: List[PlacedArtwork] = []
    for rec in records:
        if not _layer_matches(rec.layer, layer_filter, board=board):
            continue
        if rec.uuid and rec.uuid in board_geom and abs(rec.angle_deg) <= 1e-4:
            center_x_mm, center_y_mm, width_mm, height_mm = board_geom[rec.uuid]
        else:
            with Image.open(io.BytesIO(rec.png_bytes)) as im:
                px_w, px_h = im.size
                dpi_x, dpi_y = _image_dpi_from_pil(im)
            width_mm = px_w * (25.4 / dpi_x) * rec.scale
            height_mm = px_h * (25.4 / dpi_y) * rec.scale
            center_x_mm = rec.x_mm
            center_y_mm = rec.y_mm
        artworks.append(
            PlacedArtwork(
                png_bytes=rec.png_bytes,
                center_x_mm=center_x_mm,
                center_y_mm=center_y_mm,
                width_mm=width_mm,
                height_mm=height_mm,
                angle_deg=rec.angle_deg,
            )
        )
    return artworks


def file_to_placed_artwork(image_path: str, bounds: BoardBounds) -> List[PlacedArtwork]:
    """Load an external PNG/JPG/BMP image and fit it to the board bounding box."""
    with Image.open(image_path) as im:
        buf = io.BytesIO()
        im.convert("RGBA").save(buf, format="PNG")
        png_bytes = buf.getvalue()

    return [
        PlacedArtwork(
            png_bytes=png_bytes,
            center_x_mm=(bounds.min_x + bounds.max_x) * 0.5,
            center_y_mm=(bounds.min_y + bounds.max_y) * 0.5,
            width_mm=bounds.width,
            height_mm=bounds.height,
            angle_deg=0.0,
        )
    ]


def collect_side_artworks(
    side: str,
    options: ExportOptions,
    bounds: BoardBounds,
    live_images: Sequence[KiCadImageRecord],
    board=None,
    pcbnew_mod=None,
) -> List[PlacedArtwork]:
    mode = options.top_mode if side == "top" else options.bottom_mode
    if mode == "none":
        return []
    if mode == "file":
        path = options.top_image_path if side == "top" else options.bottom_image_path
        if not path or not os.path.isfile(path):
            return []
        return file_to_placed_artwork(path, bounds)
    layer = options.top_layer if side == "top" else options.bottom_layer
    return records_to_placed_artworks(live_images, layer, board=board, pcbnew_mod=pcbnew_mod)


# ---------------------------------------------------------------------------
# KiCad Gerber + Drill plotting
# ---------------------------------------------------------------------------


def _gerber_layers_to_plot(board, include_silkscreen: bool, pcbnew) -> List[Tuple[int, str, str]]:
    layers: List[Tuple[int, str, str]] = [
        (pcbnew.F_Cu, "F_Cu", "Top copper"),
        (pcbnew.B_Cu, "B_Cu", "Bottom copper"),
    ]
    copper = int(board.GetCopperLayerCount())
    for i in range(1, max(copper - 1, 1)):
        const = getattr(pcbnew, f"In{i}_Cu", None)
        if const is not None:
            layers.append((const, f"In{i}_Cu", f"Inner copper {i}"))
    layers.extend(
        [
            (pcbnew.F_Mask, "F_Mask", "Top solder mask"),
            (pcbnew.B_Mask, "B_Mask", "Bottom solder mask"),
            (pcbnew.F_Paste, "F_Paste", "Top paste"),
            (pcbnew.B_Paste, "B_Paste", "Bottom paste"),
            (pcbnew.Edge_Cuts, "Edge_Cuts", "Board outline"),
        ]
    )
    if include_silkscreen:
        layers.extend(
            [
                (pcbnew.F_SilkS, "F_Silkscreen", "Top silkscreen"),
                (pcbnew.B_SilkS, "B_Silkscreen", "Bottom silkscreen"),
            ]
        )
    return layers


def _plot_kicad_gerbers_and_drills(
    board,
    staging_dir: str,
    include_silkscreen: bool,
    pcbnew,
    only_layers: Optional[Sequence[Tuple[int, str, str]]] = None,
    with_drills: bool = True,
) -> Tuple[List[str], List[str]]:
    os.makedirs(staging_dir, exist_ok=True)
    pctl = pcbnew.PLOT_CONTROLLER(board)
    popt = pctl.GetPlotOptions()
    popt.SetOutputDirectory(staging_dir)
    _try_call(popt, "SetFormat", pcbnew.PLOT_FORMAT_GERBER)
    _try_call(popt, "SetPlotFrameRef", False)
    _try_call(popt, "SetAutoScale", False)
    _try_call(popt, "SetScale", 1)
    _try_call(popt, "SetMirror", False)
    _try_call(popt, "SetUseAuxOrigin", False)
    _try_call(popt, "SetUseGerberAttributes", True)
    _try_call(popt, "SetIncludeGerberNetlistInfo", True)
    _try_call(popt, "SetUseGerberProtelExtensions", True)
    _try_call(popt, "SetCreateGerberJobFile", False)
    _try_call(popt, "SetGerberPrecision", 6)
    _try_call(popt, "SetSubtractMaskFromSilk", True)
    _try_call(popt, "SetPlotValue", True)
    _try_call(popt, "SetPlotReference", True)
    _try_call(popt, "SetSketchPadsOnFabLayers", False)

    target_layers = only_layers if only_layers is not None else _gerber_layers_to_plot(board, include_silkscreen, pcbnew)
    gerbers: List[str] = []
    try:
        for layer_id, suffix, desc in target_layers:
            pctl.SetLayer(layer_id)
            if not pctl.OpenPlotfile(suffix, pcbnew.PLOT_FORMAT_GERBER, desc):
                raise RuntimeError(f"Failed to plot layer {suffix}")
            gerbers.append(str(pctl.GetPlotFileName()))
            pctl.PlotLayer()
    finally:
        pctl.ClosePlot()

    drills: List[str] = []
    if with_drills:
        drill_dir = tempfile.mkdtemp(prefix="drill_", dir=staging_dir)
        writer = pcbnew.EXCELLON_WRITER(board)
        origin_cls = getattr(pcbnew, "VECTOR2I", None) or pcbnew.wxPoint
        writer.SetOptions(False, False, origin_cls(0, 0), False)
        writer.SetFormat(True)
        writer.CreateDrillandMapFilesSet(drill_dir, True, False)

        for name in os.listdir(drill_dir):
            if name.lower().endswith(".drl"):
                src = os.path.join(drill_dir, name)
                dst = os.path.join(staging_dir, name)
                shutil.move(src, dst)
                drills.append(dst)
        shutil.rmtree(drill_dir, ignore_errors=True)
    return gerbers, drills


def plot_silkscreen_gerbers(board, pcbnew_mod=None) -> Tuple[str, str]:
    """Plot F.Silkscreen and B.Silkscreen Gerbers into memory for Live Preview."""
    pcbnew = _pcbnew(pcbnew_mod)
    layers = [
        (pcbnew.F_SilkS, "F_Silkscreen", "Top silkscreen"),
        (pcbnew.B_SilkS, "B_Silkscreen", "Bottom silkscreen"),
    ]
    with tempfile.TemporaryDirectory(prefix="jlc_silk_") as tmpdir:
        gerbers, _ = _plot_kicad_gerbers_and_drills(
            board,
            tmpdir,
            include_silkscreen=True,
            pcbnew=pcbnew,
            only_layers=layers,
            with_drills=False,
        )
        f_silk = ""
        b_silk = ""
        for path in gerbers:
            jlc_name = map_kicad_gerber_filename(os.path.basename(path))
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                txt = f.read()
            if jlc_name == "Gerber_TopSilkscreenLayer.GTO":
                f_silk = txt
            elif jlc_name == "Gerber_BottomSilkscreenLayer.GBO":
                b_silk = txt
        return f_silk, b_silk


def export_jlc_colorful_zip(
    board,
    options: ExportOptions,
    pcbnew_mod=None,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> Dict[str, object]:
    """Export a complete JLCPCB Gerber + colorful silkscreen ZIP archive."""
    pcbnew = _pcbnew(pcbnew_mod)

    def notify(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    if options.refill_zones:
        notify("Filling copper zones...")
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())

    notify("Reading board outline & reference images...")
    bounds, outline_poly = read_board_outline(board, pcbnew_mod=pcbnew)
    live_images: List[KiCadImageRecord] = []
    if options.top_mode == "layer" or options.bottom_mode == "layer":
        live_images = read_live_pcb_images(board, pcbnew_mod=pcbnew)

    top_art = collect_side_artworks("top", options, bounds, live_images, board=board, pcbnew_mod=pcbnew)
    bottom_art = collect_side_artworks("bottom", options, bounds, live_images, board=board, pcbnew_mod=pcbnew)

    with tempfile.TemporaryDirectory(prefix="jlc_color_") as tmpdir:
        raw_dir = os.path.join(tmpdir, "raw")
        out_dir = os.path.join(tmpdir, "jlc")
        os.makedirs(out_dir, exist_ok=True)

        notify("Plotting KiCad Gerbers & drills...")
        gerbers, drills = _plot_kicad_gerbers_and_drills(
            board,
            raw_dir,
            include_silkscreen=options.include_pcb_silkscreen,
            pcbnew=pcbnew,
        )

        f_mask_content = ""
        b_mask_content = ""
        f_silk_content = ""
        b_silk_content = ""
        written_files: List[str] = []

        notify("Converting Gerbers to JLCPCB EasyEDA format...")
        for path in gerbers:
            fname = os.path.basename(path)
            jlc_name = map_kicad_gerber_filename(fname)
            if not jlc_name:
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if jlc_name == "Gerber_TopSolderMaskLayer.GTS":
                f_mask_content = content
            elif jlc_name == "Gerber_BottomSolderMaskLayer.GBS":
                b_mask_content = content
            elif jlc_name == "Gerber_TopSilkscreenLayer.GTO":
                f_silk_content = content
            elif jlc_name == "Gerber_BottomSilkscreenLayer.GBO":
                b_silk_content = content
            processed = process_gerber_for_jlc(content, inject_header=True, ensure_g54=True)
            dst = os.path.join(out_dir, jlc_name)
            with open(dst, "w", encoding="utf-8", newline="\n") as f:
                f.write(processed)
            written_files.append(dst)

        for path in drills:
            fname = os.path.basename(path)
            jlc_name = map_kicad_gerber_filename(fname)
            if not jlc_name:
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            dst = os.path.join(out_dir, jlc_name)
            with open(dst, "w", encoding="utf-8", newline="\n") as f:
                f.write(normalize_excellon_drill(content))
            written_files.append(dst)

        if top_art or bottom_art:
            notify("Generating colorful silkscreen layers (.FCTS/.FCBS/.FCBO/.FCBM)...")
            marks_mm = compute_interior_marks(bounds, outline_poly=outline_poly)

            top_mask_paths = (
                parse_solder_mask_gerber(f_mask_content)
                if (options.carve_solder_mask and f_mask_content)
                else []
            )
            bottom_mask_paths = (
                parse_solder_mask_gerber(b_mask_content)
                if (options.carve_solder_mask and b_mask_content)
                else []
            )

            if top_art:
                top_png = composite_artwork_to_png(
                    bounds,
                    top_art,
                    dpi=options.dpi,
                    white_background=options.white_background,
                    mirror_result_x=False,
                    silkscreen_gerber=(f_silk_content if options.include_pcb_silkscreen else ""),
                    silkscreen_color=options.silkscreen_color,
                )
                top_svg = build_colorful_svg(
                    side="top",
                    bounds=bounds,
                    png_bytes=top_png,
                    mask_paths=top_mask_paths,
                    marks_mm=marks_mm,
                    outline_poly=outline_poly,
                    white_background=options.white_background,
                )
                fcts_path = os.path.join(out_dir, "Fabrication_ColorfulTopSilkscreen.FCTS")
                with open(fcts_path, "wb") as f:
                    f.write(encrypt_jlc_svg(top_svg))
                written_files.append(fcts_path)

            if bottom_art:
                bottom_png = composite_artwork_to_png(
                    bounds,
                    bottom_art,
                    dpi=options.dpi,
                    white_background=options.white_background,
                    mirror_result_x=options.mirror_bottom,
                    silkscreen_gerber=(b_silk_content if options.include_pcb_silkscreen else ""),
                    silkscreen_color=options.silkscreen_color,
                )
                bottom_svg = build_colorful_svg(
                    side="bottom",
                    bounds=bounds,
                    png_bytes=bottom_png,
                    mask_paths=bottom_mask_paths,
                    marks_mm=marks_mm,
                    outline_poly=outline_poly,
                    white_background=options.white_background,
                )
                fcbs_path = os.path.join(out_dir, "Fabrication_ColorfulBottomSilkscreen.FCBS")
                with open(fcbs_path, "wb") as f:
                    f.write(encrypt_jlc_svg(bottom_svg))
                written_files.append(fcbs_path)

            outline_svg = build_board_outline_svg(bounds, outline_poly=outline_poly)
            fcbo_path = os.path.join(out_dir, "Fabrication_ColorfulBoardOutlineLayer.FCBO")
            with open(fcbo_path, "wb") as f:
                f.write(encrypt_jlc_svg(outline_svg))
            written_files.append(fcbo_path)

            fcbm_gerber = build_outline_mark_gerber(bounds, marks_mm, outline_poly=outline_poly)
            fcbm_path = os.path.join(out_dir, "Fabrication_ColorfulBoardOutlineMark.FCBM")
            with open(fcbm_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(fcbm_gerber)
            written_files.append(fcbm_path)

        how_to = os.path.join(out_dir, "How-to-order-PCB.txt")
        with open(how_to, "w", encoding="utf-8", newline="") as f:
            f.write(HOW_TO_ORDER_TXT)
        written_files.append(how_to)

        notify("Creating ZIP archive...")
        os.makedirs(os.path.dirname(os.path.abspath(options.output_zip_path)), exist_ok=True)
        with zipfile.ZipFile(options.output_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for fp in sorted(written_files):
                zf.write(fp, arcname=os.path.basename(fp))

    return {
        "zip_path": options.output_zip_path,
        "board_width_mm": bounds.width,
        "board_height_mm": bounds.height,
        "top_images": len(top_art),
        "bottom_images": len(bottom_art),
        "file_count": len(written_files),
        "files": [os.path.basename(p) for p in sorted(written_files)],
    }
