#!/usr/bin/env python3
"""Build a KiCad Plugin and Content Manager (PCM) installable ZIP package."""

from __future__ import annotations

import io
import json
import os
import zipfile

from PIL import Image


ROOT = os.path.dirname(os.path.abspath(__file__))

PLUGIN_FILES = [
    "__init__.py",
    "plugin.py",
    "gui.py",
    "kicad_adapter.py",
    "jlc_colorful.py",
    "jlc_gerber.py",
    "sexpr.py",
    "icon.png",
]


def build_pcm_zip(out_dir: str = "") -> str:
    with open(os.path.join(ROOT, "metadata.json"), "r", encoding="utf-8") as f:
        meta = json.load(f)

    version = meta["versions"][0]["version"]
    out_dir = out_dir or os.path.join(ROOT, "dist")
    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, f"kicad-jlc-colorful-silkscreen-v{version}.zip")

    # Prepare 64x64 PCM icon for resources/icon.png
    with Image.open(os.path.join(ROOT, "icon.png")) as im:
        icon64 = im.convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        icon64.save(buf, format="PNG")
        resources_icon_bytes = buf.getvalue()

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(os.path.join(ROOT, "metadata.json"), arcname="metadata.json")
        zf.writestr("resources/icon.png", resources_icon_bytes)
        for rel in PLUGIN_FILES:
            zf.write(os.path.join(ROOT, rel), arcname=f"plugins/{rel}")

    return zip_path


if __name__ == "__main__":
    out = build_pcm_zip()
    print(f"Built PCM package: {out}")
