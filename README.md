# KiCad JLC Colorful Silkscreen

A KiCad 9/10 Action Plugin (and standalone CLI) for exporting **JLCPCB Full-Color UV Silkscreen** fabrication packages (`.FCTS`, `.FCBS`, `.FCBO`, `.FCBM`) directly from KiCad PCB Editor.

## How It Works

JLCPCB's multi-color silkscreen pipeline expects EasyEDA-style Gerber filenames and 4 additional files:

- `Fabrication_ColorfulBoardOutlineMark.FCBM` — plain Gerber with the board outline (`Edge.Cuts`) and 3 interior `0.508 mm` alignment marks.
- `Fabrication_ColorfulBoardOutlineLayer.FCBO` — RSA-2048 + AES-128-GCM encrypted board outline SVG.
- `Fabrication_ColorfulTopSilkscreen.FCTS` — encrypted SVG (`10-mil` units, inverted Y) containing top artwork + `clipPath` cutouts for `F.Mask` openings.
- `Fabrication_ColorfulBottomSilkscreen.FCBS` — encrypted SVG containing bottom artwork + `clipPath` cutouts for `B.Mask` openings.

This plugin reads your board directly from KiCad (`Edge.Cuts`, `F.Mask`, `B.Mask`, copper/silkscreen layers, and `Reference Image` items placed on user layers such as `User.1` / `User.2` or `Dwgs.User`), plots JLCPCB-formatted Gerbers & Drill files, and generates the encrypted colorful silkscreen files in one `.zip`.

## Usage in KiCad

1. Open your board in KiCad PCB Editor.
2. Place your artwork using **Place → Add Reference Image** onto a layer (e.g. `User.1` for Top, `User.2` for Bottom), and position/scale/rotate it right on your PCB — **or** select a PNG/JPG directly inside the plugin dialog.
3. Click the **JLC Colorful Silkscreen** toolbar button (or **Tools → External Plugins → JLC Colorful Silkscreen**).
4. Choose Top/Bottom sources, check the preview, and click **Export JLCPCB ZIP**.
5. Upload the generated `.zip` to JLCPCB and select **Colorful Silkscreen** (EasyEDA-compatible Gerber format).

## Installation

Symlink or copy this folder into your KiCad scripting plugins directory:

```bash
ln -s ~/kicad-jlc-colorful-silkscreen ~/.local/share/kicad/10.0/scripting/plugins/kicad_jlc_colorful_silkscreen
```

Then restart KiCad (or click **Tools → External Plugins → Refresh Plugins**).

### Python Dependencies

- `Pillow`
- `cryptography` (or `pycryptodome`)

## CLI Usage

```bash
python3 cli.py path/to/board.kicad_pcb --top-layer User.1 --bottom-layer User.2 -o jlcpcb_colorful.zip
# or using external PNGs fitted to board outline:
python3 cli.py path/to/board.kicad_pcb --top-image top.png --bottom-image bottom.png -o jlcpcb_colorful.zip
```
