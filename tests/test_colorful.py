"""Unit and end-to-end tests for kicad-jlc-colorful-silkscreen."""

import io
import os
import sys
import tempfile
import unittest
import zipfile

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
    point_in_polygon,
)
from jlc_gerber import (
    add_easyeda_header,
    add_hash_aperture,
    map_kicad_gerber_filename,
    verify_hash_aperture,
)
from sexpr import parse_kicad_pcb_images


def _make_png(w=64, h=64, color=(255, 0, 128, 255)) -> bytes:
    im = Image.new("RGBA", (w, h), color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


class TestJlcGerber(unittest.TestCase):
    def test_filename_mapping(self):
        self.assertEqual(map_kicad_gerber_filename("board-F_Cu.gbr"), "Gerber_TopLayer.GTL")
        self.assertEqual(map_kicad_gerber_filename("board-In2_Cu.gbr"), "Gerber_InnerLayer2.G2")
        self.assertEqual(map_kicad_gerber_filename("board-Edge_Cuts.gbr"), "Gerber_BoardOutlineLayer.GKO")
        self.assertEqual(map_kicad_gerber_filename("board-PTH.drl"), "Drill_PTH_Through.DRL")
        self.assertEqual(map_kicad_gerber_filename("board-NPTH.drl"), "Drill_NPTH_Through.DRL")

    def test_hash_aperture_roundtrip(self):
        sample = add_easyeda_header("%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.2*%\nD10*\nX0Y0D03*\nM02*\n")
        hashed = add_hash_aperture(sample)
        self.assertTrue(verify_hash_aperture(hashed))


class TestColorfulLogic(unittest.TestCase):
    def test_interior_marks_non_rectangular_polygon(self):
        # L-shaped board where top-right bbox corner (40, 0) is OUTSIDE the board
        poly = [(0.0, 0.0), (10.0, 0.0), (10.0, 30.0), (40.0, 30.0), (40.0, 40.0), (0.0, 40.0)]
        bounds = BoardBounds(0.0, 40.0, 0.0, 40.0)
        marks = compute_interior_marks(bounds, poly)
        self.assertEqual(len(marks), 3)
        for mx, my in marks:
            self.assertTrue(point_in_polygon(mx, my, poly), f"Mark {(mx, my)} outside L-polygon")

    def test_solder_mask_parser(self):
        gbr = (
            "%FSLAX46Y46*%\n"
            "%MOMM*%\n"
            "%ADD10C,1.5*%\n"
            "%ADD11R,2.0X1.0*%\n"
            "G54D10*\n"
            "X10000000Y-20000000D03*\n"
            "G54D11*\n"
            "X15000000Y-25000000D03*\n"
            "G36*\n"
            "X5000000Y-5000000D02*\n"
            "X6000000Y-5000000D01*\n"
            "X6000000Y-6000000D01*\n"
            "G37*\n"
            "M02*\n"
        )
        paths = parse_solder_mask_gerber(gbr)
        self.assertEqual(len(paths), 3)

    def test_solder_mask_rotated_45_deg_pads(self):
        import math
        import re

        # 2.0 x 1.0 mm pads rotated 45 deg CCW, flashed at (20, -20)
        # RoundRect: r=0.25, inner half-size=(0.75, 0.25) rotated 45 deg
        # HorizOval: w=1.0, p1=(0.353553, 0.353553), p2=(-0.353553, -0.353553)
        gbr = (
            "%FSLAX46Y46*%\n"
            "%MOMM*%\n"
            "%ADD10RoundRect,0.250000X-0.353553X-0.707107X0.707107X0.353553X0.353553X0.707107X-0.707107X-0.353553X0*%\n"
            "%ADD11RotRect,2.000000X1.000000X45.000000*%\n"
            "%ADD12HorizOval,1.000000X0.353553X0.353553X-0.353553X-0.353553X0*%\n"
            "D10*\n"
            "X20000000Y-20000000D03*\n"
            "D11*\n"
            "X20000000Y-20000000D03*\n"
            "D12*\n"
            "X20000000Y-20000000D03*\n"
            "M02*\n"
        )
        paths = parse_solder_mask_gerber(gbr)
        self.assertEqual(len(paths), 3)

        for p in paths:
            coords = re.findall(r"([ML])\s+([+-]?\d+(?:\.\d+)?)\s+([+-]?\d+(?:\.\d+)?)", p)
            rel_mm = [(float(x) * 0.254 - 20.0, float(y) * 0.254 - 20.0) for _, x, y in coords]
            self.assertGreaterEqual(len(rel_mm), 4)
            # Must extend close to 1.0 mm along the 45-deg major axis ((+1,-1)/sqrt(2) or (-1,+1)/sqrt(2) in Y-down)
            max_major = max(abs(x - y) / math.sqrt(2.0) for x, y in rel_mm)
            max_minor = max(abs(x + y) / math.sqrt(2.0) for x, y in rel_mm)
            self.assertGreater(max_major, 0.85)
            self.assertLess(max_minor, 0.56)
            # Upright bounding-box square would put vertices at (+/-0.8, +/-0.8) along minor diagonal
            self.assertFalse(any(x > 0.65 and y > 0.65 for x, y in rel_mm))
            self.assertFalse(any(x < -0.65 and y < -0.65 for x, y in rel_mm))

    def test_silkscreen_gerber_overlay_and_polarity(self):
        bounds = BoardBounds(0.0, 20.0, 0.0, 20.0)
        dark_art = PlacedArtwork(_make_png(64, 64, (20, 20, 20, 255)), 10.0, 10.0, 20.0, 20.0)
        # Line along y=-10 (center Y), G02 arc, G36/G37 square at (2..4, -4..-2),
        # and %LPC*% flash knocking out center (10, -10).
        gbr = (
            "%FSLAX46Y46*%\n"
            "%MOMM*%\n"
            "%LPD*%\n"
            "%ADD10C,0.600000*%\n"
            "%ADD11C,2.000000*%\n"
            "D10*\n"
            "X2000000Y-10000000D02*\n"
            "X18000000Y-10000000D01*\n"
            "G75*\n"
            "G02*\n"
            "X14000000Y-14000000I0J-4000000D01*\n"
            "G01*\n"
            "G36*\n"
            "X2000000Y-2000000D02*\n"
            "X4000000Y-2000000D01*\n"
            "X4000000Y-4000000D01*\n"
            "X2000000Y-4000000D01*\n"
            "X2000000Y-2000000D01*\n"
            "G37*\n"
            "%LPC*%\n"
            "D11*\n"
            "X10000000Y-10000000D03*\n"
            "M02*\n"
        )
        png_bytes = composite_artwork_to_png(
            bounds,
            [dark_art],
            dpi=150,
            white_background=False,
            silkscreen_gerber=gbr,
            silkscreen_color=(255, 255, 255),
        )
        im = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        w, h = im.size
        # Line at x=5mm, y=10mm should be white (255, 255, 255)
        self.assertEqual(im.getpixel((int(w * 0.25), int(h * 0.50)))[:3], (255, 255, 255))
        # Filled G36/G37 square at x=3mm, y=3mm should be white
        self.assertEqual(im.getpixel((int(w * 0.15), int(h * 0.15)))[:3], (255, 255, 255))
        # %LPC*% knocked-out center at x=10mm, y=10mm must stay dark (20, 20, 20)
        self.assertEqual(im.getpixel((int(w * 0.50), int(h * 0.50)))[:3], (20, 20, 20))

    def test_encrypt_structure(self):
        bounds = BoardBounds(0.0, 20.0, 0.0, 20.0)
        png = composite_artwork_to_png(
            bounds,
            [PlacedArtwork(_make_png(), 10.0, 10.0, 20.0, 20.0)],
            dpi=150,
        )
        marks = compute_interior_marks(bounds)
        svg = build_colorful_svg("top", bounds, png, [], marks)
        self.assertIn('eda-version="2.6(2026-09-15)"', svg)
        blob = encrypt_jlc_svg(svg)
        self.assertGreater(len(blob), 512 + 16)
        self.assertEqual(len(blob), 512 + len(svg.encode("utf-8")) + 16)

        fcbm = build_outline_mark_gerber(bounds, marks)
        self.assertIn("colorScreenSilk_Mark", fcbm)
        self.assertTrue(verify_hash_aperture(fcbm))

        fcbo = encrypt_jlc_svg(build_board_outline_svg(bounds))
        self.assertGreater(len(fcbo), 512 + 16)


class TestEndToEndKiCad(unittest.TestCase):
    def test_sexpr_extracts_images_from_h616_board(self):
        pcb = "/home/bodya/h616-board/Hardware/H616_DDR3/H616_devboard.kicad_pcb"
        if not os.path.isfile(pcb):
            self.skipTest("H616_devboard.kicad_pcb not present")
        with open(pcb, "r", encoding="utf-8") as f:
            recs = parse_kicad_pcb_images(f.read())
        self.assertGreaterEqual(len(recs), 2)
        self.assertEqual(recs[0].layer, "Dwgs.User")

    def test_reference_image_embedded_dpi_fallback(self):
        from kicad_adapter import records_to_placed_artworks
        from sexpr import KiCadImageRecord

        # 1200x600 PNG embedded at 1200 DPI -> 25.4 x 12.7 mm at scale=1.0
        buf_1200 = io.BytesIO()
        Image.new("RGBA", (1200, 600), (255, 0, 0, 255)).save(
            buf_1200, format="PNG", dpi=(1200, 1200)
        )
        rec_1200 = KiCadImageRecord(
            x_mm=50.0,
            y_mm=60.0,
            angle_deg=0.0,
            scale=1.0,
            layer="Dwgs.User",
            png_bytes=buf_1200.getvalue(),
            uuid="test-1200",
        )
        arts = records_to_placed_artworks([rec_1200], "User.Drawings", board=None)
        self.assertEqual(len(arts), 1)
        self.assertAlmostEqual(arts[0].width_mm, 25.4, places=2)
        self.assertAlmostEqual(arts[0].height_mm, 12.7, places=2)

        # 300x300 PNG without embedded DPI -> defaults to 300 DPI -> 25.4 x 25.4 mm
        rec_nodpi = KiCadImageRecord(
            x_mm=10.0,
            y_mm=20.0,
            angle_deg=0.0,
            scale=1.0,
            layer="Dwgs.User",
            png_bytes=_make_png(300, 300),
            uuid="test-nodpi",
        )
        arts_nodpi = records_to_placed_artworks([rec_nodpi], "User.Drawings", board=None)
        self.assertEqual(len(arts_nodpi), 1)
        self.assertAlmostEqual(arts_nodpi[0].width_mm, 25.4, places=2)
        self.assertAlmostEqual(arts_nodpi[0].height_mm, 25.4, places=2)

    def test_reference_image_uses_pcbnew_bbox(self):
        try:
            import pcbnew
        except ImportError:
            self.skipTest("pcbnew not installed")
        import base64
        from kicad_adapter import read_live_pcb_images, records_to_placed_artworks

        base_board = pcbnew.BOARD()
        with tempfile.NamedTemporaryFile(suffix=".kicad_pcb", delete=False) as tf:
            base_path = tf.name
        try:
            pcbnew.SaveBoard(base_path, base_board)
            with open(base_path, "r", encoding="utf-8") as f:
                base_txt = f.read()
        finally:
            if os.path.exists(base_path):
                os.unlink(base_path)

        buf = io.BytesIO()
        Image.new("RGBA", (600, 300), (0, 255, 0, 255)).save(buf, format="PNG", dpi=(1200, 1200))
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        img_uuid = "11111111-1111-1111-1111-111111111111"
        img_block = (
            '\t(image\n'
            '\t\t(at 50 50)\n'
            '\t\t(layer "Dwgs.User")\n'
            '\t\t(scale 1.0)\n'
            f'\t\t(uuid "{img_uuid}")\n'
            f'\t\t(data "{b64}")\n'
            '\t)\n'
        )
        pcb_txt = base_txt[: base_txt.rfind(")")] + img_block + ")\n"

        with tempfile.NamedTemporaryFile("w", suffix=".kicad_pcb", delete=False, encoding="utf-8") as tf:
            tf.write(pcb_txt)
            pcb_path = tf.name
        try:
            board = pcbnew.LoadBoard(pcb_path)
            ri = next(
                pcbnew.Cast_to_PCB_REFERENCE_IMAGE(d)
                for d in board.GetDrawings()
                if d.Type() == pcbnew.PCB_REFERENCE_IMAGE_T
            )
            bb = ri.GetBoundingBox()
            recs = read_live_pcb_images(board, pcbnew_mod=pcbnew)
            arts = records_to_placed_artworks(recs, "User.Drawings", board=board, pcbnew_mod=pcbnew)
            self.assertEqual(len(arts), 1)
            self.assertAlmostEqual(arts[0].width_mm, pcbnew.ToMM(bb.GetWidth()), places=4)
            self.assertAlmostEqual(arts[0].height_mm, pcbnew.ToMM(bb.GetHeight()), places=4)
        finally:
            if os.path.exists(pcb_path):
                os.unlink(pcb_path)

    def test_full_pcbnew_export(self):
        try:
            import pcbnew
        except ImportError:
            self.skipTest("pcbnew not installed")
        from kicad_adapter import ExportOptions, export_jlc_colorful_zip

        board = pcbnew.BOARD()
        # Add 30x20mm rectangle on Edge.Cuts
        pts = [(10, 10), (40, 10), (40, 30), (10, 30)]
        for i in range(4):
            seg = pcbnew.PCB_SHAPE(board)
            seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
            seg.SetLayer(pcbnew.Edge_Cuts)
            seg.SetWidth(pcbnew.FromMM(0.2))
            p1, p2 = pts[i], pts[(i + 1) % 4]
            seg.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(p1[0]), pcbnew.FromMM(p1[1])))
            seg.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(p2[0]), pcbnew.FromMM(p2[1])))
            board.Add(seg)

        with tempfile.TemporaryDirectory() as tmp:
            top_img = os.path.join(tmp, "top.png")
            bot_img = os.path.join(tmp, "bot.png")
            with open(top_img, "wb") as f:
                f.write(_make_png(100, 80, (255, 0, 0, 255)))
            with open(bot_img, "wb") as f:
                f.write(_make_png(100, 80, (0, 128, 255, 255)))

            out_zip = os.path.join(tmp, "out.zip")
            opts = ExportOptions(
                output_zip_path=out_zip,
                top_mode="file",
                top_image_path=top_img,
                bottom_mode="file",
                bottom_image_path=bot_img,
                dpi=150,
            )
            res = export_jlc_colorful_zip(board, opts, pcbnew_mod=pcbnew)
            self.assertAlmostEqual(res["board_width_mm"], 30.0, places=2)
            self.assertAlmostEqual(res["board_height_mm"], 20.0, places=2)

            with zipfile.ZipFile(out_zip, "r") as zf:
                names = set(zf.namelist())
                for expected in [
                    "Gerber_TopLayer.GTL",
                    "Gerber_BottomLayer.GBL",
                    "Gerber_BoardOutlineLayer.GKO",
                    "Fabrication_ColorfulTopSilkscreen.FCTS",
                    "Fabrication_ColorfulBottomSilkscreen.FCBS",
                    "Fabrication_ColorfulBoardOutlineLayer.FCBO",
                    "Fabrication_ColorfulBoardOutlineMark.FCBM",
                    "How-to-order-PCB.txt",
                ]:
                    self.assertIn(expected, names)


if __name__ == "__main__":
    unittest.main()
