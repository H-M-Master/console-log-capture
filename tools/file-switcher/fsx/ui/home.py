"""The screen a non-technical user sees: current version, then one button each.

No profile, state, managed-file, digest or transaction vocabulary appears here.
A version is just a name the user chose; switching is one click plus one
confirmation.  Everything technical lives behind the 高级管理 entry.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from ..model import Config, Item
from . import dialogs


class HomeView(ttk.Frame):
    """Version buttons and the two ways out of this screen."""

    def __init__(self, parent: tk.Misc, *, on_switch: Callable[[str], None],
                 on_wizard: Callable[[], None], on_advanced: Callable[[], None],
                 on_undo: Callable[[], None]):
        super().__init__(parent, padding=24)
        self._on_switch = on_switch
        self._on_wizard = on_wizard
        self._on_advanced = on_advanced
        self._on_undo = on_undo
        self._buttons: list[ttk.Button] = []
        self._busy = False

        ttk.Label(self, text="文件版本切换", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            self,
            text="在你保存过的版本之间一键切换。只有你勾选管理的文件会被替换，"
                 "目标文件夹里的其它文件不会被动。",
            wraplength=760, justify="left", style="Note.TLabel",
        ).pack(anchor="w", pady=(6, 18))

        self.current = ttk.Label(self, text="正在读取…", style="State.TLabel")
        self.current.pack(anchor="w")
        self.detail = ttk.Label(self, text="", style="Muted.TLabel")
        self.detail.pack(anchor="w", pady=(4, 16))

        self.versions = ttk.Frame(self)
        self.versions.pack(fill="x", pady=(0, 18))

        self.empty_hint = ttk.Label(
            self, text="还没有可用版本。点下面的“设置 / 添加版本”开始。",
            style="Warn.TLabel", wraplength=760, justify="left")

        bottom = ttk.Frame(self)
        bottom.pack(side="bottom", fill="x", pady=(24, 0))
        self._wizard_button = ttk.Button(bottom, text="设置 / 添加版本",
                                         style="Home.TButton", command=self._on_wizard)
        self._wizard_button.pack(side="left")
        self._undo_button = ttk.Button(bottom, text="撤销上一次切换",
                                       style="Home.TButton", command=self._on_undo)
        self._undo_button.pack(side="left", padx=8)
        self._advanced_button = ttk.Button(bottom, text="高级管理",
                                           style="Home.TButton", command=self._on_advanced)
        self._advanced_button.pack(side="right")

    # ---------- rendering ----------
    def show(self, config: Config, status: dict[str, Any] | None) -> None:
        """Rebuild the version buttons from config plus the last status read."""
        self._clear_buttons()
        self._apply_busy_to_fixed()
        profile_id = config.initial_profile_id()
        states = config.states(profile_id)
        ready = [s for s in states if config.is_captured(profile_id, s.id)]

        if not ready:
            self.empty_hint.pack(anchor="w", before=self.versions)
            if states:
                self.current.configure(text="还没有保存过内容，暂时无法切换。",
                                       style="Warn.TLabel")
                self.detail.configure(
                    text="请先为每个版本保存一次当前内容，之后就能一键切换。")
            else:
                self.current.configure(text="还没有设置过。", style="Warn.TLabel")
                self.detail.configure(text="")
            return
        self.empty_hint.pack_forget()

        current = self._current_state(status, ready, profile_id)
        if current is not None:
            self.current.configure(text=f"当前版本：{current.display_name}",
                                   style="Good.TLabel")
        else:
            self.current.configure(text="当前版本：没有匹配到已保存的版本",
                                   style="Warn.TLabel")

        target_root = config.target_root(profile_id)
        self.detail.configure(text=f"目标文件夹：{target_root}" if target_root else "")

        for state in ready:
            is_current = current is not None and state.id == current.id
            text = f"✓ 当前版本：{state.display_name}" if is_current \
                else f"切换到：{state.display_name}"
            button = ttk.Button(self.versions, text=text, style="Version.TButton",
                                command=lambda s=state.id, n=state.display_name:
                                self._on_switch(s, n))
            button.pack(fill="x", pady=5)
            if self._busy:
                button.configure(state="disabled")
            self._buttons.append(button)

    @staticmethod
    def _current_state(status: dict[str, Any] | None,
                       ready: list[Item], profile_id: str) -> Item | None:
        """Which ready version the target folder currently matches exactly."""
        if not status:
            return None
        exact = next((r for r in (status.get("states") or [])
                      if r.get("exact") and not r.get("error")), None)
        if not exact:
            return None
        return next((s for s in ready if s.id == str(exact.get("id"))), None)

    def _clear_buttons(self) -> None:
        for button in self._buttons:
            button.destroy()
        self._buttons = []

    # ---------- state ----------
    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._apply_busy_to_fixed()
        for button in self._buttons:
            button.configure(state="disabled" if busy else "normal")

    def _apply_busy_to_fixed(self) -> None:
        """Keep the always-present buttons in step with the busy flag.

        ``show`` rebuilds the layout, so it has to re-apply this or a busy
        window would briefly offer buttons that start a second operation.
        """
        for widget in (self._wizard_button, self._undo_button, self._advanced_button):
            widget.configure(state="disabled" if self._busy else "normal")

    def set_undo_enabled(self, enabled: bool) -> None:
        """Only offer undo when there is a transaction that can actually roll back."""
        try:
            self._undo_button.configure(state="normal" if enabled else "disabled")
        except tk.TclError:
            pass

    def flash(self, text: str, style: str = "Warn.TLabel") -> None:
        """One-line feedback under the title, used after a switch."""
        self.current.configure(text=text, style=style)


def ask_switch(parent: tk.Misc, name: str, changed: int | None = None) -> bool:
    """The single confirmation on the happy path."""
    return dialogs.confirm_switch(parent, name, changed)
