"""Entry point for the folder switcher.

The window is ``fsx.ui.app.App``.  This module stays deliberately thin: the
PyInstaller spec points at ``folder_app.py``, so keeping the entry point stable
means packaging never depends on the internal layout.

Run from a checkout with:

    python folder_app.py

User data (config, snapshots, journals, logs) is written under
``%LOCALAPPDATA%\\AnyTestTools\\FolderSwitcher`` by default; set
``ANYTESTTOOLS_DATA_ROOT`` to relocate it for tests or portable use.
"""
from __future__ import annotations

import sys
from pathlib import Path
from tkinter import messagebox

if __package__ in (None, ""):
    # Running as a script: make the sibling package importable.
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from fsx.paths import CONFIG, LOG_ROOT, SCRIPT, STATE_ROOT
from fsx.ui.app import App

__all__ = ["App", "main"]


def main() -> None:
    try:
        app = App()
        app.mainloop()
    except Exception as exc:
        try:
            messagebox.showerror("文件版本切换启动失败", str(exc))
        except Exception:
            print(f"folder_app startup failure: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
