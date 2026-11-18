"""wxPython dialog for KiCad JLC Colorful Silkscreen ActionPlugin."""

from __future__ import annotations

import io
import os
import traceback

import pcbnew
import wx
from PIL import Image, ImageDraw

if __package__:
    from .jlc_colorful import composite_artwork_to_png, compute_interior_marks
    from .kicad_adapter import (
        USER_LAYER_CHOICES,
        ExportOptions,
        collect_side_artworks,
        export_jlc_colorful_zip,
        read_board_outline,
        read_live_pcb_images,
    )
else:
    from jlc_colorful import composite_artwork_to_png, compute_interior_marks
    from kicad_adapter import (
        USER_LAYER_CHOICES,
        ExportOptions,
        collect_side_artworks,
        export_jlc_colorful_zip,
        read_board_outline,
        read_live_pcb_images,
    )


class ColorfulSilkscreenDialog(wx.Dialog):
    """GUI dialog for configuring and exporting JLCPCB colorful silkscreen."""

    def __init__(self, parent, board, on_close=None):
        super().__init__(
            parent,
            title="JLCPCB Colorful Silkscreen Exporter",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.board = board
        self._on_close = on_close

        board_file = board.GetFileName() or ""
        if board_file:
            base_dir = os.path.dirname(board_file)
            stem = os.path.splitext(os.path.basename(board_file))[0]
            default_zip = os.path.join(base_dir, f"{stem}_jlcpcb_colorful.zip")
        else:
            default_zip = os.path.expanduser("~/jlcpcb_colorful.zip")

        self._build_ui(default_zip)
        self.Bind(wx.EVT_CLOSE, self._handle_close)
        self.SetMinSize((760, 620))
        self.Fit()
        self.CentreOnParent()
        self._refresh_preview()

    def _build_ui(self, default_zip: str) -> None:
        root = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            self,
            label=(
                "Tip: You can place PNG/JPG images directly on your PCB via "
                "Place -> Add Reference Image (e.g. on User.1 / User.2), "
                "or pick an image file below to fit onto the board."
            ),
        )
        intro.Wrap(720)
        root.Add(intro, 0, wx.ALL | wx.EXPAND, 10)

        # Top / Bottom side configuration
        sides_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.top_controls = self._create_side_panel(
            "Top Silkscreen (.FCTS)", default_layer="User.1", default_mode=0
        )
        self.bottom_controls = self._create_side_panel(
            "Bottom Silkscreen (.FCBS)", default_layer="User.2", default_mode=0
        )
        sides_sizer.Add(self.top_controls["box"], 1, wx.RIGHT | wx.EXPAND, 8)
        sides_sizer.Add(self.bottom_controls["box"], 1, wx.EXPAND, 0)
        root.Add(sides_sizer, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

        # Preview area
        preview_box = wx.StaticBoxSizer(wx.HORIZONTAL, self, "Live Preview (Board Outline + Artwork)")
        self.top_preview_bmp = wx.StaticBitmap(self, size=(280, 180))
        self.bottom_preview_bmp = wx.StaticBitmap(self, size=(280, 180))

        top_prev_col = wx.BoxSizer(wx.VERTICAL)
        top_prev_col.Add(wx.StaticText(self, label="Top (as viewed from Top):"), 0, wx.BOTTOM, 4)
        top_prev_col.Add(self.top_preview_bmp, 1, wx.EXPAND)

        bot_prev_col = wx.BoxSizer(wx.VERTICAL)
        bot_prev_col.Add(wx.StaticText(self, label="Bottom (as viewed from Bottom):"), 0, wx.BOTTOM, 4)
        bot_prev_col.Add(self.bottom_preview_bmp, 1, wx.EXPAND)

        preview_box.Add(top_prev_col, 1, wx.ALL | wx.EXPAND, 6)
        preview_box.Add(bot_prev_col, 1, wx.ALL | wx.EXPAND, 6)
        root.Add(preview_box, 1, wx.ALL | wx.EXPAND, 10)

        # Options
        opt_box = wx.StaticBoxSizer(wx.VERTICAL, self, "Export Options")
        grid = wx.GridSizer(2, 3, 6, 12)

        self.chk_mask = wx.CheckBox(self, label="Carve Solder Mask pads (F.Mask/B.Mask)")
        self.chk_mask.SetValue(True)
        self.chk_white_bg = wx.CheckBox(self, label="White UV base background (#FFFFFF)")
        self.chk_white_bg.SetValue(True)
        self.chk_mirror_bot = wx.CheckBox(self, label="Auto-mirror Bottom PNG (readable)")
        self.chk_mirror_bot.SetValue(True)
        self.chk_silk = wx.CheckBox(self, label="Include KiCad F/B.Silkscreen")
        self.chk_silk.SetValue(True)
        self.chk_refill = wx.CheckBox(self, label="Refill copper zones")
        self.chk_refill.SetValue(False)

        dpi_row = wx.BoxSizer(wx.HORIZONTAL)
        dpi_row.Add(wx.StaticText(self, label="DPI:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.dpi_choice = wx.Choice(self, choices=["300", "600", "1200"])
        self.dpi_choice.SetSelection(1)  # 600 DPI
        dpi_row.Add(self.dpi_choice, 0, wx.ALIGN_CENTER_VERTICAL)

        grid.Add(self.chk_mask)
        grid.Add(self.chk_white_bg)
        grid.Add(self.chk_mirror_bot)
        grid.Add(self.chk_silk)
        grid.Add(self.chk_refill)
        grid.Add(dpi_row)
        opt_box.Add(grid, 0, wx.ALL | wx.EXPAND, 6)

        for chk in (self.chk_white_bg, self.chk_mirror_bot):
            chk.Bind(wx.EVT_CHECKBOX, lambda _e: self._refresh_preview())

        # Output zip path
        out_row = wx.BoxSizer(wx.HORIZONTAL)
        out_row.Add(wx.StaticText(self, label="Output ZIP:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.txt_out = wx.TextCtrl(self, value=default_zip)
        btn_browse_out = wx.Button(self, label="Browse...")
        btn_browse_out.Bind(wx.EVT_BUTTON, self._on_browse_out)
        out_row.Add(self.txt_out, 1, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 6)
        out_row.Add(btn_browse_out, 0, wx.ALIGN_CENTER_VERTICAL)
        opt_box.Add(out_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 6)

        root.Add(opt_box, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

        # Status + Action buttons
        self.lbl_status = wx.StaticText(self, label="Ready.")
        root.Add(self.lbl_status, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 10)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_refresh = wx.Button(self, label="Refresh Preview")
        btn_refresh.Bind(wx.EVT_BUTTON, lambda _e: self._refresh_preview())
        btn_export = wx.Button(self, label="Export JLCPCB ZIP")
        btn_export.SetDefault()
        btn_export.Bind(wx.EVT_BUTTON, self._on_export)
        btn_close = wx.Button(self, wx.ID_CLOSE, label="Close")
        btn_close.Bind(wx.EVT_BUTTON, lambda _e: self.Close())

        btn_row.Add(btn_refresh, 0, wx.RIGHT, 8)
        btn_row.AddStretchSpacer()
        btn_row.Add(btn_export, 0, wx.RIGHT, 8)
        btn_row.Add(btn_close, 0)
        root.Add(btn_row, 0, wx.ALL | wx.EXPAND, 10)

        self.SetSizer(root)

    def _create_side_panel(self, title: str, default_layer: str, default_mode: int) -> dict:
        box = wx.StaticBoxSizer(wx.VERTICAL, self, title)
        mode_choice = wx.Choice(
            self,
            choices=["From KiCad Layer (Reference Images)", "From Image File (Fit to Board)", "Disabled"],
        )
        mode_choice.SetSelection(default_mode)
        box.Add(mode_choice, 0, wx.ALL | wx.EXPAND, 6)

        layer_row = wx.BoxSizer(wx.HORIZONTAL)
        layer_row.Add(wx.StaticText(self, label="Layer:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        layer_choice = wx.Choice(self, choices=USER_LAYER_CHOICES)
        if default_layer in USER_LAYER_CHOICES:
            layer_choice.SetSelection(USER_LAYER_CHOICES.index(default_layer))
        else:
            layer_choice.SetSelection(0)
        layer_row.Add(layer_choice, 1, wx.EXPAND)
        box.Add(layer_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 6)

        file_row = wx.BoxSizer(wx.HORIZONTAL)
        file_txt = wx.TextCtrl(self, value="")
        file_btn = wx.Button(self, label="Pick...")
        file_row.Add(file_txt, 1, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 4)
        file_row.Add(file_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        box.Add(file_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 6)

        def sync_enabled(_evt=None):
            sel = mode_choice.GetSelection()
            layer_choice.Enable(sel == 0)
            file_txt.Enable(sel == 1)
            file_btn.Enable(sel == 1)
            self._refresh_preview()

        def pick_file(_evt):
            with wx.FileDialog(
                self,
                "Select PNG or JPG Artwork",
                wildcard="Image files (*.png;*.jpg;*.jpeg;*.bmp)|*.png;*.jpg;*.jpeg;*.bmp",
                style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
            ) as dlg:
                if dlg.ShowModal() == wx.ID_OK:
                    file_txt.SetValue(dlg.GetPath())
                    mode_choice.SetSelection(1)
                    sync_enabled()

        mode_choice.Bind(wx.EVT_CHOICE, sync_enabled)
        layer_choice.Bind(wx.EVT_CHOICE, lambda _e: self._refresh_preview())
        file_btn.Bind(wx.EVT_BUTTON, pick_file)

        # Initial enabled state without triggering preview prematurely
        layer_choice.Enable(default_mode == 0)
        file_txt.Enable(default_mode == 1)
        file_btn.Enable(default_mode == 1)

        return {
            "box": box,
            "mode": mode_choice,
            "layer": layer_choice,
            "file": file_txt,
        }

    def _current_options(self) -> ExportOptions:
        mode_map = {0: "layer", 1: "file", 2: "none"}
        return ExportOptions(
            output_zip_path=self.txt_out.GetValue().strip(),
            top_mode=mode_map[self.top_controls["mode"].GetSelection()],
            top_layer=self.top_controls["layer"].GetStringSelection(),
            top_image_path=self.top_controls["file"].GetValue().strip(),
            bottom_mode=mode_map[self.bottom_controls["mode"].GetSelection()],
            bottom_layer=self.bottom_controls["layer"].GetStringSelection(),
            bottom_image_path=self.bottom_controls["file"].GetValue().strip(),
            carve_solder_mask=self.chk_mask.GetValue(),
            white_background=self.chk_white_bg.GetValue(),
            mirror_bottom=self.chk_mirror_bot.GetValue(),
            include_pcb_silkscreen=self.chk_silk.GetValue(),
            dpi=int(self.dpi_choice.GetStringSelection() or "600"),
            refill_zones=self.chk_refill.GetValue(),
        )

    def _render_preview_bitmap(
        self, bounds, outline_poly, artworks, mirror_view: bool, target_size=(280, 180)
    ) -> wx.Bitmap:
        w_box, h_box = target_size
        bg = Image.new("RGBA", (w_box, h_box), (40, 44, 52, 255))

        bw = max(bounds.width, 1e-3)
        bh = max(bounds.height, 1e-3)
        scale = min((w_box - 16) / bw, (h_box - 16) / bh)
        disp_w = max(1, int(round(bw * scale)))
        disp_h = max(1, int(round(bh * scale)))
        ox = (w_box - disp_w) // 2
        oy = (h_box - disp_h) // 2

        if artworks:
            if not self.chk_white_bg.GetValue():
                # Draw subtle checkerboard under transparent artwork
                checker = Image.new("RGBA", (disp_w, disp_h), (58, 62, 72, 255))
                cdraw = ImageDraw.Draw(checker)
                step = 8
                for cy in range(0, disp_h, step):
                    for cx in range(0, disp_w, step):
                        if ((cx // step) + (cy // step)) % 2 == 0:
                            cdraw.rectangle([cx, cy, cx + step - 1, cy + step - 1], fill=(78, 84, 96, 255))
                bg.alpha_composite(checker, (ox, oy))

            png_bytes = composite_artwork_to_png(
                bounds,
                artworks,
                dpi=150,
                white_background=self.chk_white_bg.GetValue(),
                mirror_result_x=mirror_view,
            )
            comp = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
            comp = comp.resize((disp_w, disp_h), Image.Resampling.LANCZOS)
            bg.alpha_composite(comp, (ox, oy))

        # Always draw board outline + alignment marks on top so contour stays visible
        draw = ImageDraw.Draw(bg)

        def to_px(x_mm: float, y_mm: float):
            rx = (bounds.max_x - x_mm) if mirror_view else (x_mm - bounds.min_x)
            ry = y_mm - bounds.min_y
            return (ox + rx * scale, oy + ry * scale)

        if outline_poly and len(outline_poly) >= 3:
            pts = [to_px(x, y) for x, y in outline_poly]
            # Draw dark halo then bright green outline for visibility over any image
            draw.line(pts + [pts[0]], fill=(0, 0, 0, 220), width=4)
            draw.line(pts + [pts[0]], fill=(0, 230, 118, 255), width=2)
        else:
            rect = [ox, oy, ox + disp_w - 1, oy + disp_h - 1]
            draw.rectangle(rect, outline=(0, 0, 0, 220), width=4)
            draw.rectangle(rect, outline=(0, 230, 118, 255), width=2)

        marks = compute_interior_marks(bounds, outline_poly=outline_poly)
        for mx, my in marks:
            px, py = to_px(mx, my)
            r = 3
            draw.ellipse([px - r - 1, py - r - 1, px + r + 1, py + r + 1], fill=(0, 0, 0, 220))
            draw.ellipse([px - r, py - r, px + r, py + r], fill=(255, 214, 0, 255))

        return self._pil_to_wx_bitmap(bg)

    @staticmethod
    def _pil_to_wx_bitmap(pil_img: Image.Image) -> wx.Bitmap:
        rgb = pil_img.convert("RGB")
        w, h = rgb.size
        return wx.Bitmap.FromBuffer(w, h, rgb.tobytes())

    def _refresh_preview(self) -> None:
        try:
            board = pcbnew.GetBoard() or self.board
            bounds, outline_poly = read_board_outline(board)
            opts = self._current_options()
            live_imgs = (
                read_live_pcb_images(board)
                if (opts.top_mode == "layer" or opts.bottom_mode == "layer")
                else []
            )
            top_art = collect_side_artworks("top", opts, bounds, live_imgs, board=board)
            bot_art = collect_side_artworks("bottom", opts, bounds, live_imgs, board=board)

            self.top_preview_bmp.SetBitmap(
                self._render_preview_bitmap(bounds, outline_poly, top_art, mirror_view=False)
            )
            # For physical bottom preview, if auto-mirror is OFF, flip preview so user sees physical back
            self.bottom_preview_bmp.SetBitmap(
                self._render_preview_bitmap(
                    bounds,
                    outline_poly,
                    bot_art,
                    mirror_view=not opts.mirror_bottom,
                )
            )
            self.lbl_status.SetLabel(
                f"Board: {bounds.width:.2f} x {bounds.height:.2f} mm | "
                f"Top images: {len(top_art)} | Bottom images: {len(bot_art)}"
            )
        except Exception as exc:
            self.lbl_status.SetLabel(f"Preview note: {exc}")

    def _on_browse_out(self, _evt) -> None:
        with wx.FileDialog(
            self,
            "Save JLCPCB Gerber ZIP",
            defaultFile=os.path.basename(self.txt_out.GetValue()) or "jlcpcb_colorful.zip",
            wildcard="ZIP files (*.zip)|*.zip",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.txt_out.SetValue(dlg.GetPath())

    def _on_export(self, _evt) -> None:
        opts = self._current_options()
        if not opts.output_zip_path:
            wx.MessageBox("Please specify an output ZIP path.", "Error", wx.OK | wx.ICON_ERROR, self)
            return

        try:
            board = pcbnew.GetBoard() or self.board
            wx.BeginBusyCursor()
            result = export_jlc_colorful_zip(
                board,
                opts,
                progress_cb=lambda msg: (self.lbl_status.SetLabel(msg), wx.YieldIfNeeded()),
            )
            self.lbl_status.SetLabel(
                f"Exported {result['file_count']} files -> {result['zip_path']}"
            )
            wx.MessageBox(
                f"Export successful!\n\n"
                f"ZIP: {result['zip_path']}\n"
                f"Board: {result['board_width_mm']:.2f} x {result['board_height_mm']:.2f} mm\n"
                f"Top artwork items: {result['top_images']}\n"
                f"Bottom artwork items: {result['bottom_images']}\n"
                f"Total files in ZIP: {result['file_count']}",
                "JLCPCB Colorful Silkscreen",
                wx.OK | wx.ICON_INFORMATION,
                self,
            )
        except Exception as exc:
            traceback.print_exc()
            wx.MessageBox(f"Export failed:\n{exc}", "Error", wx.OK | wx.ICON_ERROR, self)
        finally:
            if wx.IsBusy():
                wx.EndBusyCursor()

    def _handle_close(self, _evt) -> None:
        if self._on_close:
            self._on_close()
        self.Destroy()
