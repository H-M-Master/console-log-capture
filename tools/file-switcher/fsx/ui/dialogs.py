"""Every dialog the tool shows goes through this module.

The rule that makes the tool usable by non-technical people: no raw backend
text reaches a message box.  ``show_error`` renders a headline, a plain-Chinese
reason, an actionable next step, and — collapsed behind a details toggle — the
original code and message for whoever ends up debugging it.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from .. import backend, errors

APP_TITLE = "文件版本切换"


class ErrorDialog(tk.Toplevel):
    """An error box that leads with what to do, not with what broke."""

    def __init__(self, parent: tk.Misc, headline: str, reason: str,
                 action: str, detail: str = ""):
        super().__init__(parent)
        self.title(APP_TITLE)
        self.transient(parent)
        self.resizable(False, False)
        self._detail_text = detail

        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text=headline, style="Danger.TLabel",
                  font=("Segoe UI", 13, "bold"), wraplength=460).pack(anchor="w")
        if reason:
            ttk.Label(body, text=reason, wraplength=460,
                      justify="left").pack(anchor="w", pady=(8, 0))
        if action:
            ttk.Label(body, text=f"下一步：{action}", wraplength=460,
                      justify="left", style="Note.TLabel").pack(anchor="w", pady=(10, 0))

        if detail:
            toggle = ttk.Button(body, text="显示详细信息", command=self._toggle)
            toggle.pack(anchor="w", pady=(12, 0))
            self._detail = tk.Text(body, height=7, wrap="word", width=62,
                                   font=("Consolas", 9), state="disabled")
            self._detail.insert("1.0", detail)
            self._detail.configure(state="disabled")
            self._detail_visible = False

        ttk.Button(body, text="知道了", command=self._close).pack(anchor="e", pady=(14, 0))
        self.bind("<Escape>", lambda _e: self._close())
        self._center(parent)
        self.grab_set()

    def _toggle(self) -> None:
        self._detail_visible = not self._detail_visible
        if self._detail_visible:
            self._detail.pack(fill="both", expand=True, pady=(8, 0))
        else:
            self._detail.pack_forget()

    def _center(self, parent: tk.Misc) -> None:
        self.update_idletasks()
        try:
            x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
            self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        except tk.TclError:
            pass

    def _close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


def show_error(parent: tk.Misc | None, payload: Any,
               headline: str | None = None) -> None:
    """Show a user-facing failure from anything that can go wrong.

    ``payload`` may be a backend envelope, a raw runner result, or a
    ``BackendError`` — ``backend.explain`` normalizes all three.
    """
    title, reason, action, detail = backend.explain(payload)
    _show(parent, headline or title, reason, action, detail)


def show_message(parent: tk.Misc | None, headline: str, reason: str = "") -> None:
    """Informational message that is not a failure."""
    _show(parent, headline, reason, "", "")


def _show(parent: tk.Misc | None, headline: str, reason: str,
          action: str, detail: str) -> None:
    try:
        dialog = ErrorDialog(parent or tk._default_root, headline, reason, action, detail)
        if parent is not None:
            parent.wait_window(dialog)
    except Exception:
        # Never let the reporter itself become the failure: fall back to a
        # plain box that still leads with the headline and the next step.
        messagebox.showerror(headline, "\n\n".join(x for x in (reason, action) if x))


def confirm(parent: tk.Misc | None, headline: str, what_changes: str,
            what_stays: str = "其它文件不会被改动。") -> bool:
    """A yes/no confirmation that always states the blast radius."""
    text = f"{what_changes}\n\n{what_stays}\n\n确定继续吗？"
    return bool(messagebox.askyesno(headline, text, parent=parent))


def confirm_switch(parent: tk.Misc | None, name: str, changed: int | None = None) -> bool:
    """The one confirmation on the happy path, worded for the home screen."""
    if changed is None:
        head = f"切换到“{name}”？"
        body = "会把你勾选管理的文件替换成这个版本保存的内容。"
    else:
        head = f"切换到“{name}”？将替换 {changed} 个文件。"
        body = "只有你勾选管理的文件会被替换。"
    return confirm(parent, "确认切换", head, f"{body}其它文件保持不动。")


def explain_error(payload: Any) -> tuple[str, str, str, str]:
    """Expose the translation without showing anything; used by tests."""
    return backend.explain(payload)


def known_codes() -> set[str]:
    return errors.known_codes()
