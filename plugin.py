"""KiCad ActionPlugin entry point for JLCPCB Colorful Silkscreen."""

from __future__ import annotations

import os
import sys

import pcbnew
import wx

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

if __package__:
    from .gui import ColorfulSilkscreenDialog
else:
    if _PLUGIN_DIR not in sys.path:
        sys.path.insert(0, _PLUGIN_DIR)
    from gui import ColorfulSilkscreenDialog


class JlcColorfulSilkscreenPlugin(pcbnew.ActionPlugin):
    _dialog = None

    def defaults(self):
        self.name = "JLC Colorful Silkscreen"
        self.category = "Fabrication Outputs"
        self.description = (
            "Export JLCPCB multi-color UV silkscreen Gerbers (.FCTS/.FCBS/.FCBO/.FCBM) "
            "from KiCad Reference Images or PNG/JPG artwork."
        )
        self.show_toolbar_button = True
        icon_path = os.path.join(_PLUGIN_DIR, "icon.png")
        if os.path.exists(icon_path):
            self.icon_file_name = icon_path

    @staticmethod
    def _clear_dialog():
        JlcColorfulSilkscreenPlugin._dialog = None

    def Run(self):
        if JlcColorfulSilkscreenPlugin._dialog is not None:
            JlcColorfulSilkscreenPlugin._dialog.Raise()
            return
        board = pcbnew.GetBoard()
        parent = wx.FindWindowByName("PcbFrame")
        dlg = ColorfulSilkscreenDialog(parent, board, on_close=self._clear_dialog)
        JlcColorfulSilkscreenPlugin._dialog = dlg
        dlg.Show()
