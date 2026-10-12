"""Where bundled resources and per-user data live.

Two roots, deliberately separate:

- ``RESOURCE_ROOT`` holds read-only files shipped with the tool: the backend
  script and the config template.  Under a one-file PyInstaller build these are
  unpacked into a throwaway temp directory (``sys._MEIPASS``), so nothing may
  ever be written here.
- ``DATA_ROOT`` holds everything the tool creates: the config, snapshots,
  transaction journals and logs.  It lives under the user's own profile, so the
  tool keeps working from a read-only or network location and two users on one
  machine never share state.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    # One-file builds extract the payload to a one-shot temp dir; one-dir builds
    # leave it beside the executable.  Prefer _MEIPASS whenever it exists.
    RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
else:
    RESOURCE_ROOT = Path(__file__).resolve().parent.parent

_DATA_ROOT_OVERRIDE = os.environ.get("ANYTESTTOOLS_DATA_ROOT")
if _DATA_ROOT_OVERRIDE and _DATA_ROOT_OVERRIDE.strip():
    DATA_ROOT = Path(_DATA_ROOT_OVERRIDE).expanduser().resolve(strict=False)
else:
    DATA_ROOT = (Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
                 / "AnyTestTools" / "FolderSwitcher").resolve(strict=False)

SCRIPT = RESOURCE_ROOT / "folder-switcher.ps1"
TEMPLATE = RESOURCE_ROOT / "templates" / "folder-config.template.json"
CONFIG = DATA_ROOT / "folder-config.json"
STATE_ROOT = DATA_ROOT / "folder-data"
LOG_ROOT = DATA_ROOT / "logs"

EMPTY_CONFIG: dict = {"schemaVersion": 2, "defaultProfileId": "", "profiles": []}
