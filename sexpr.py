"""Lightweight KiCad S-expression parser for extracting embedded (image ...) blocks."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class KiCadImageRecord:
    """Represents a parsed (image ...) block from a .kicad_pcb file."""

    x_mm: float
    y_mm: float
    angle_deg: float
    scale: float
    layer: str
    png_bytes: bytes
    uuid: str = ""


def _tokenize_sexpr(text: str):
    """Yield tokens from a KiCad S-expression string."""
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == "(" or ch == ")":
            yield ch
            i += 1
            continue
        if ch == '"':
            i += 1
            buf = []
            while i < n:
                c = text[i]
                if c == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    if nxt == "n":
                        buf.append("\n")
                    elif nxt == "r":
                        buf.append("\r")
                    elif nxt == "t":
                        buf.append("\t")
                    else:
                        buf.append(nxt)
                    i += 2
                    continue
                if c == '"':
                    i += 1
                    break
                buf.append(c)
                i += 1
            yield ("STR", "".join(buf))
            continue
        start = i
        while i < n and text[i] not in " \t\r\n()\"":
            i += 1
        yield text[start:i]


def _extract_image_blocks(pcb_text: str) -> List[str]:
    """Find top-level or nested `(image ...)` blocks in a .kicad_pcb file quickly."""
    blocks: List[str] = []
    search_from = 0
    marker = "(image"
    n = len(pcb_text)

    while True:
        idx = pcb_text.find(marker, search_from)
        if idx == -1:
            break
        after = idx + len(marker)
        if after >= n or pcb_text[after] not in " \t\r\n":
            search_from = after
            continue

        # Walk balanced parentheses respecting quoted strings
        depth = 0
        i = idx
        in_str = False
        while i < n:
            c = pcb_text[i]
            if in_str:
                if c == "\\":
                    i += 2
                    continue
                if c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        blocks.append(pcb_text[idx : i + 1])
                        search_from = i + 1
                        break
            i += 1
        else:
            break

    return blocks


def _parse_block(block: str) -> list:
    tokens = list(_tokenize_sexpr(block))
    pos = 0

    def parse_node():
        nonlocal pos
        tok = tokens[pos]
        if tok == "(":
            pos += 1
            items = []
            while pos < len(tokens) and tokens[pos] != ")":
                items.append(parse_node())
            pos += 1  # consume ')'
            return items
        pos += 1
        if isinstance(tok, tuple) and tok[0] == "STR":
            return tok[1]
        return tok

    return parse_node()


def parse_kicad_pcb_images(pcb_text: str) -> List[KiCadImageRecord]:
    """Extract all `(image ...)` items (board-level and footprint-level) from .kicad_pcb text."""
    records: List[KiCadImageRecord] = []
    for block in _extract_image_blocks(pcb_text):
        node = _parse_block(block)
        if not isinstance(node, list) or not node or node[0] != "image":
            continue

        x_mm = 0.0
        y_mm = 0.0
        angle_deg = 0.0
        scale = 1.0
        layer = ""
        uuid_val = ""
        data_chunks: List[str] = []

        for child in node[1:]:
            if not isinstance(child, list) or not child:
                continue
            tag = child[0]
            if tag == "at" and len(child) >= 3:
                x_mm = float(child[1])
                y_mm = float(child[2])
                if len(child) >= 4:
                    try:
                        angle_deg = float(child[3])
                    except ValueError:
                        pass
            elif tag == "scale" and len(child) >= 2:
                scale = float(child[1])
            elif tag == "layer" and len(child) >= 2:
                layer = str(child[1])
            elif tag == "uuid" and len(child) >= 2:
                uuid_val = str(child[1])
            elif tag == "data":
                for part in child[1:]:
                    if isinstance(part, str):
                        data_chunks.append(part)

        if not data_chunks:
            continue
        try:
            png_bytes = base64.b64decode("".join(data_chunks))
        except Exception:
            continue

        records.append(
            KiCadImageRecord(
                x_mm=x_mm,
                y_mm=y_mm,
                angle_deg=angle_deg,
                scale=scale,
                layer=layer,
                png_bytes=png_bytes,
                uuid=uuid_val,
            )
        )
    return records
