"""Shared styles and DPI handling.

Windows scales the display by 125%/150% on most laptops, which makes a
fixed-size Tk layout either overflow or leave the last row unreachable.  The
scaling factor is read once from the window system and every explicit size in
the UI is expressed through :func:`scale`.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# Palette: a small set of named roles keeps the two screens visually consistent
# and makes the "is this state good or bad" judgement a property of the theme
# rather than something every label picks for itself.
COLORS = {
    "good": "#0b7a2f",
    "warn": "#9a5b00",
    "danger": "#b00020",
    "note": "#3a5a8c",
    "muted": "#5c5c5c",
}

FONT_FAMILY = "Microsoft YaHei UI"
MONO_FAMILY = "Consolas"


class Theme:
    """Scaling and ttk styles, built once per root window."""

    def __init__(self, root: tk.Misc):
        self.root = root
        self.factor = self._detect_scale()
        self._build()

    def _detect_scale(self) -> float:
        """Display scaling relative to 100%.

        Tk reports pixels per point via ``tk scaling``; 1.333 is the usual value
        at 100% on Windows, so the ratio against that is the DPI factor.
        """
        try:
            scaling = float(self.root.tk.call("tk", "scaling"))
        except Exception:
            return 1.0
        factor = scaling / 1.3333 if scaling > 0 else 1.0
        # Clamp: below 1.0 the layout gets cramped, above 2.0 it wastes space.
        return max(1.0, min(2.0, round(factor, 2)))

    def scale(self, value: int) -> int:
        return int(round(value * self.factor))

    def _font(self, size: int, weight: str = "normal") -> tuple[str, int, str]:
        return (FONT_FAMILY, self.scale(size), weight)

    def _build(self) -> None:
        style = ttk.Style(self.root)
        for candidate in ("vista", "winnative", "clam"):
            try:
                style.theme_use(candidate)
                break
            except tk.TclError:
                continue

        for name, size, weight in (("Title.TLabel", 17, "bold"),
                                   ("Section.TLabel", 12, "bold"),
                                   ("State.TLabel", 12, "bold"),
                                   ("Body.TLabel", 10, "normal")):
            style.configure(name, font=self._font(size, weight))
        style.configure("Note.TLabel", font=self._font(10), foreground=COLORS["note"])
        style.configure("Muted.TLabel", font=self._font(10), foreground=COLORS["muted"])
        style.configure("Good.TLabel", font=self._font(12, "bold"), foreground=COLORS["good"])
        style.configure("Warn.TLabel", font=self._font(12, "bold"), foreground=COLORS["warn"])
        style.configure("Danger.TLabel", font=self._font(11, "bold"), foreground=COLORS["danger"])

        # Version buttons on the home screen are the primary action: give them
        # real height so they read as buttons rather than as a list of links.
        style.configure("Version.TButton", font=self._font(12, "bold"),
                        padding=(self.scale(16), self.scale(14)))
        style.configure("Home.TButton", font=self._font(10),
                        padding=(self.scale(12), self.scale(7)))
        style.configure("Treeview", font=self._font(10), rowheight=self.scale(22))
        style.configure("Treeview.Heading", font=self._font(10, "bold"))

        default = tkfont.nametofont("TkDefaultFont")
        default.configure(family=FONT_FAMILY, size=self.scale(10))
        self.mono = tkfont.Font(family=MONO_FAMILY, size=self.scale(9))
