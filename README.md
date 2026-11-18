# KiCad JLC Colorful Silkscreen

A KiCad 9/10 Action Plugin and standalone CLI tool for exporting **JLCPCB multi-color UV silkscreen** manufacturing packages (`.FCTS`, `.FCBS`, `.FCBO`, `.FCBM`) directly from KiCad PCB Editor without rebuilding your board in EasyEDA.

![JLCPCB Colorful Silkscreen Exporter](pics/Pasted%20image.png)

## Features

- **Native KiCad Layer Workflow**: Place full-color PNG/JPG images directly on your PCB using **Place -> Add Reference Image** (for example on `User.1` for Top and `User.2` for Bottom), then move, scale, and rotate them visually over your layout.
- **External Image Mode**: Optionally pick a PNG/JPG file directly in the plugin dialog to fit across the board bounding box.
- **Live Preview**: Shows the composited Top and Bottom artwork together with the `Edge.Cuts` polygon outline and the three interior alignment marks.
- **Non-Rectangular Board Support**: Extracts the real closed `Edge.Cuts` polygon and automatically computes 3 non-collinear `0.508 mm` alignment marks strictly inside the board outline.
- **Solder Mask Cutouts**: Parses `F.Mask` and `B.Mask` openings (`evenodd` SVG `clipPath`) so UV ink is masked off exposed pads and vias.
- **One-Click JLCPCB ZIP Export**: Plots KiCad Gerbers and Excellon drill files, normalizes filenames and headers to EasyEDA Pro format (`G04 EasyEDA Pro`, `G54` aperture selections, MD5 verification aperture), and bundles the encrypted `.FCTS` / `.FCBS` / `.FCBO` + `.FCBM` layers into a single upload-ready `.zip`.
- **Standalone CLI**: Supports automated/headless export via `cli.py`.

## Installation

### Dependencies

KiCad's bundled/system Python needs:

- `Pillow`
- `cryptography` (or `pycryptodome` as fallback)

On Linux (Fedora/Debian/Ubuntu), if not already installed:

```bash
pip install Pillow cryptography
```

### Plugin Setup

Clone or symlink this directory into your KiCad scripting plugins folder:

- **Linux (KiCad 10)**:
  ```bash
  ln -s ~/kicad-jlc-colorful-silkscreen ~/.local/share/kicad/10.0/scripting/plugins/kicad_jlc_colorful_silkscreen
  ```
- **Windows**:
  Copy the folder to `%USERPROFILE%\Documents\KiCad\10.0\scripting\plugins\kicad_jlc_colorful_silkscreen`
- **macOS**:
  Copy or symlink to `~/Documents/KiCad/10.0/scripting/plugins/kicad_jlc_colorful_silkscreen`

Then restart KiCad or run **Tools -> External Plugins -> Refresh Plugins** in PCB Editor.

## Usage in KiCad

1. Open your `.kicad_pcb` in KiCad PCB Editor.
2. Add artwork using **Place -> Add Reference Image** onto a user layer (defaults: `User.1` for Top, `User.2` for Bottom) and position/scale/rotate it on the board.
3. Click the **JLC Colorful Silkscreen** toolbar button (or **Tools -> External Plugins -> JLC Colorful Silkscreen**).
4. Verify the **Live Preview** for Top and Bottom, configure options:
   - **Carve Solder Mask pads (`F.Mask`/`B.Mask`)**: excludes exposed pads from the silkscreen print area.
   - **White UV base background (`#FFFFFF`)**: adds the white under-layer used by JLCPCB's UV printer.
   - **Auto-mirror Bottom PNG**: mirrors bottom artwork horizontally when the source image is drawn right-reading.
   - **Include KiCad `F/B.Silkscreen`**: includes standard monochrome silkscreen Gerbers alongside the color layers.
   - **DPI**: `300`, `600`, or `1200` DPI composite resolution.
5. Click **Export JLCPCB ZIP** and upload the generated `.zip` to JLCPCB.

Note: On JLCPCB, multi-color silkscreen typically requires selecting compatible PCB options (FR-4, White solder mask, ENIG surface finish, and the multi-color silkscreen option).

## CLI Usage

```bash
# Export using Reference Images placed on User.1 and User.2:
python3 cli.py path/to/board.kicad_pcb --top-layer User.1 --bottom-layer User.2 -o jlcpcb_colorful.zip

# Export using external PNGs fitted to the board outline:
python3 cli.py path/to/board.kicad_pcb --top-image top.png --bottom-image bottom.png -o jlcpcb_colorful.zip
```

Run `python3 cli.py --help` for all options (`--no-top`, `--no-bottom`, `--no-mask-cutout`, `--transparent-bg`, `--no-mirror-bottom`, `--dpi`).

## Output Format Overview

Alongside standard EasyEDA-named Gerbers (`Gerber_TopLayer.GTL`, `Gerber_BoardOutlineLayer.GKO`, `Drill_PTH_Through.DRL`, etc.), the plugin generates:

- `Fabrication_ColorfulBoardOutlineMark.FCBM`: Plain Gerber containing the board outline polygon (`colorScreenSilk_board`) and 3 interior `0.508 mm` alignment flashes (`colorScreenSilk_Mark`).
- `Fabrication_ColorfulBoardOutlineLayer.FCBO`: Encrypted board outline SVG.
- `Fabrication_ColorfulTopSilkscreen.FCTS`: RSA-2048 OAEP (SHA-256) + AES-128-GCM encrypted SVG (`eda-version="2.6(2026-09-15)"`, `10-mil` coordinate units) for the top layer.
- `Fabrication_ColorfulBottomSilkscreen.FCBS`: Encrypted SVG for the bottom layer.

## Running Tests

```bash
python3 -m unittest discover -s tests -v
```

## Acknowledgements

Format documentation and prior community research:
- `Xerbo/JLC-FCTS-RE`
- `HalfSweet/TransJLC` and `HalfSweet/FuckJLCColorfulSilkscreen`

## License

MIT License. See [LICENSE](LICENSE).
