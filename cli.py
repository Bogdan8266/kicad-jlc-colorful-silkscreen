#!/usr/bin/env python3
"""Standalone CLI for exporting JLCPCB colorful silkscreen ZIP from a .kicad_pcb."""

from __future__ import annotations

import argparse
import os
import sys

import pcbnew

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from kicad_adapter import ExportOptions, export_jlc_colorful_zip


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Export JLCPCB Gerbers + colorful silkscreen (.FCTS/.FCBS/.FCBO/.FCBM) from KiCad PCB."
    )
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument("-o", "--output", default="", help="Output ZIP path")
    parser.add_argument("--top-layer", default="User.1", help="KiCad layer for top artwork (default: User.1)")
    parser.add_argument("--bottom-layer", default="User.2", help="KiCad layer for bottom artwork (default: User.2)")
    parser.add_argument("--top-image", default="", help="External PNG/JPG for top side (fits to board)")
    parser.add_argument("--bottom-image", default="", help="External PNG/JPG for bottom side (fits to board)")
    parser.add_argument("--no-top", action="store_true", help="Disable top colorful silkscreen")
    parser.add_argument("--no-bottom", action="store_true", help="Disable bottom colorful silkscreen")
    parser.add_argument("--no-mask-cutout", action="store_true", help="Do not carve out solder mask openings")
    parser.add_argument("--transparent-bg", action="store_true", help="Omit white UV background layer")
    parser.add_argument("--no-mirror-bottom", action="store_true", help="Do not auto-mirror bottom artwork")
    parser.add_argument("--no-pcb-silkscreen", action="store_true", help="Do not overlay KiCad F/B.Silkscreen onto colorful layers")
    parser.add_argument("--silkscreen-color", default="#FFFFFF", help="Hex RGB color for overlaid KiCad silkscreen (default: #FFFFFF)")
    parser.add_argument("--dpi", type=int, default=600, help="Composite raster DPI (default: 600)")

    args = parser.parse_args(argv)
    hex_str = args.silkscreen_color.strip().lstrip("#")
    if len(hex_str) != 6:
        print(f"Error: Invalid --silkscreen-color '{args.silkscreen_color}' (expected #RRGGBB)", file=sys.stderr)
        return 1
    try:
        silk_rgb = (int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16))
    except ValueError:
        print(f"Error: Invalid --silkscreen-color '{args.silkscreen_color}' (expected #RRGGBB)", file=sys.stderr)
        return 1

    pcb_path = os.path.abspath(args.pcb)
    if not os.path.isfile(pcb_path):
        print(f"Error: PCB file not found: {pcb_path}", file=sys.stderr)
        return 1

    out_zip = args.output
    if not out_zip:
        stem = os.path.splitext(os.path.basename(pcb_path))[0]
        out_zip = os.path.join(os.path.dirname(pcb_path), f"{stem}_jlcpcb_colorful.zip")

    top_mode = "none" if args.no_top else ("file" if args.top_image else "layer")
    bottom_mode = "none" if args.no_bottom else ("file" if args.bottom_image else "layer")

    board = pcbnew.LoadBoard(pcb_path)
    opts = ExportOptions(
        output_zip_path=out_zip,
        top_mode=top_mode,
        top_layer=args.top_layer,
        top_image_path=args.top_image,
        bottom_mode=bottom_mode,
        bottom_layer=args.bottom_layer,
        bottom_image_path=args.bottom_image,
        carve_solder_mask=not args.no_mask_cutout,
        white_background=not args.transparent_bg,
        mirror_bottom=not args.no_mirror_bottom,
        include_pcb_silkscreen=not args.no_pcb_silkscreen,
        silkscreen_color=silk_rgb,
        dpi=args.dpi,
    )

    res = export_jlc_colorful_zip(board, opts, progress_cb=lambda m: print(f"[*] {m}"))
    print(f"Done! Wrote {res['file_count']} files to {res['zip_path']}")
    print(f"Board: {res['board_width_mm']:.2f} x {res['board_height_mm']:.2f} mm")
    print(f"Top images: {res['top_images']}, Bottom images: {res['bottom_images']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
