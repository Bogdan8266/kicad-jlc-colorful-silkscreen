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

    def test_encrypt_structure(self):
        bounds = BoardBounds(0.0, 20.0, 0.0, 20.0)
        png = composite_artwork_to_png(
            bounds,
            [PlacedArtwork(_make_png(), 10.0, 10.0, 20.0, 20.0)],
            dpi=150,
        )
        marks = compute_interior_marks(bounds)
        svg = build_colorful_svg("top", bounds, png, [], marks)
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
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0].layer, "Dwgs.User")

    def test_full_pcbnew_export(self):
        import pcbnew
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
