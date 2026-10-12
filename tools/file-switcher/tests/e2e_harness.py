"""Shared fixture for tests that drive the real backend end to end.

These tests run ``folder-switcher.ps1`` for real against a temporary directory,
through the same code path the application uses.  Nothing is mocked except the
dialog boxes, which would otherwise block on input, and Tk's window mapping.

The point is to exercise what actually happens to files, so a test failing here
means the tool is broken rather than that a simulation drifted.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PS = ROOT / "folder-switcher.ps1"
POLICY = "RemoteSigned"  # local scripts are permitted under the default policy


def run_backend(config: Path, state_root: Path, *args: str, timeout: int = 90) -> dict:
    """Invoke the backend directly and return its parsed JSON result."""
    command = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
               "-ExecutionPolicy", POLICY, "-File", str(PS),
               "-Config", str(config), "-StateRoot", str(state_root), *args]
    proc = subprocess.run(command, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError(f"backend produced no JSON; stderr={proc.stderr!r}")
    return json.loads(lines[-1])


class SyncRunner:
    """Runs the real backend and reports through the callback, synchronously.

    The application uses a worker thread plus a queue; here the callback fires
    inline so a test can drive results by calling ``app.poll()``.
    """

    def __init__(self, config: Path, state_root: Path, callback):
        self.config = Path(config)
        self.state_root = Path(state_root)
        self.callback = callback
        self.running = False
        self.calls: list[list[str]] = []

    def run(self, args, callback=None):
        self.running = True
        self.calls.append(list(args))
        proc = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", POLICY, "-File", str(PS),
             "-Config", str(self.config), "-StateRoot", str(self.state_root), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=90)
        self.running = False
        (callback or self.callback)({
            "returncode": proc.returncode, "stdout": proc.stdout,
            "stderr": proc.stderr, "duration": 0.0, "command": []})

    def actions(self) -> list[str]:
        return [c[c.index("-Action") + 1] for c in self.calls if "-Action" in c]


class E2ETestCase(unittest.TestCase):
    """A temp workspace laid out the way the tool expects, with no mocking of I/O."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fsx-e2e-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # Config and state live outside the target: the store rejects overlap.
        self.workspace = self.tmp / "cfg"
        self.workspace.mkdir()
        self.target = self.tmp / "game"
        (self.target / "sub").mkdir(parents=True)
        self.config = self.workspace / "folder-config.json"
        self.state_root = self.workspace / "folder-data"
        self.sources: dict[str, Path] = {}
        self.apps: list = []
        self.confirms: list = []

    # ---------- fixtures ----------
    def make_source(self, name: str, files: dict[str, bytes | str]) -> Path:
        """A directory to capture a version's content from."""
        root = self.tmp / name
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, str):
                path.write_text(content, encoding="utf-8")
            else:
                path.write_bytes(content)
        self.sources[name] = root
        return root

    def write_target(self, files: dict[str, bytes | str]) -> None:
        for relative, content in files.items():
            path = self.target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, str):
                path.write_text(content, encoding="utf-8")
            else:
                path.write_bytes(content)

    def write_config(self, managed: list[str], states: list[str]) -> None:
        self.config.write_text(json.dumps({
            "schemaVersion": 2,
            "defaultProfileId": "p1",
            "profiles": [{
                "id": "p1", "name": "e2e", "targetRoot": str(self.target),
                "managed": [{"path": p} for p in managed],
                "states": [{"id": s, "name": s, "snapshotRoot": None} for s in states],
                "guards": [], "cacheDirectories": []}]}), encoding="utf-8")

    def capture(self, state: str, source: str) -> dict:
        """Capture through the backend, exactly as the wizard's finish does."""
        result = run_backend(self.config, self.state_root, "-Action", "Capture",
                             "-ProfileId", "p1", "-StateId", state,
                             "-SourceRoot", str(self.sources[source]))
        self.assertTrue(result["ok"], f"capture {state} failed: {result}")
        return result

    def apply_state(self, state: str) -> dict:
        """Apply a version directly, the way the tool would after a confirmation.

        Passes ``-AllowUnknown`` so the move is permitted from any starting
        content; used by tests that need the target in a known version before
        the behavior under test can happen.
        """
        digest = run_backend(self.config, self.state_root, "-Action", "Diff",
                             "-ProfileId", "p1", "-StateId", state,
                             "-AllowUnknown")["data"]["planDigest"]
        result = run_backend(self.config, self.state_root, "-Action", "Apply",
                             "-ProfileId", "p1", "-StateId", state,
                             "-AllowUnknown", "-ExpectedPlanDigest", digest)
        self.assertTrue(result["ok"], f"apply {state} failed: {result}")
        return result

    # ---------- app ----------
    def make_app(self):
        """The real application, with dialogs answered instead of shown."""
        from fsx.ui import app as app_module

        self._patchers = [
            mock.patch.object(app_module.dialogs, "show_error",
                              side_effect=lambda *a, **k: self._record("error", a)),
            mock.patch.object(app_module.dialogs, "show_message",
                              side_effect=lambda *a, **k: self._record("message", a)),
            mock.patch.object(app_module.dialogs, "confirm", return_value=True),
            mock.patch.object(app_module.dialogs, "confirm_switch", return_value=True),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        app = app_module.App(config_path=self.config, state_root=self.state_root,
                             script_path=PS, log_root=self.workspace / "logs",
                             runner=None, auto_refresh=False)
        self.apps.append(app)
        self.addCleanup(self._destroy_app, app)
        app.runner = SyncRunner(self.config, self.state_root,
                                lambda r: app.events.put(r))
        app.load_config()
        return app

    def _record(self, kind: str, args: tuple) -> None:
        self.dialogs.append((kind, args))

    @property
    def dialogs(self) -> list:
        if not hasattr(self, "_dialogs"):
            self._dialogs = []
        return self._dialogs

    def _destroy_app(self, app) -> None:
        try:
            if getattr(app, "_poll_id", None):
                app.after_cancel(app._poll_id)
                app._poll_id = None
        except tk.TclError:
            pass
        try:
            if app.winfo_exists():
                app.destroy()
        except tk.TclError:
            pass

    def pump(self, app, rounds: int = 8) -> None:
        """Drain queued results and let Tk settle, as the app's timer would."""
        for _ in range(rounds):
            app.poll()
            app.update_idletasks()
            app.update()

    def errors(self) -> list:
        return [d for d in self.dialogs if d[0] == "error"]

    # ---------- assertions ----------
    def assert_target_is(self, relative: str, expected: str | bytes) -> None:
        path = self.target / relative
        if isinstance(expected, bytes):
            self.assertEqual(path.read_bytes(), expected,
                             f"{relative} content changed unexpectedly")
        else:
            self.assertEqual(path.read_text(encoding="utf-8"), expected,
                             f"{relative} content changed unexpectedly")

    def assert_unchanged(self, relative: str, expected: str | bytes) -> None:
        self.assert_target_is(relative, expected)
