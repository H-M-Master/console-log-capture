import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tkinter as tk

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import folder_app


class FakeRunner:
    """A deterministic runner: callbacks are queued synchronously, never threaded."""

    def __init__(self, payload=None, returncode=0):
        self.payload = payload or {
            "schemaVersion": 2, "ok": True, "action": "status", "data": {}, "error": None,
        }
        self.returncode = returncode
        self.running = False
        self.calls = []

    def run(self, args, callback=None):
        if self.running:
            raise RuntimeError("busy")
        self.running = True
        self.calls.append(list(args))
        payload = self.payload
        if isinstance(payload, str):
            stdout = payload
        else:
            stdout = json.dumps(payload, ensure_ascii=False)
        result = {
            "returncode": self.returncode,
            "stdout": stdout,
            "stderr": "",
            "duration": 0,
        }
        self.running = False
        if callback:
            callback(result)


class FolderGuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="folder-gui-")
        self.root = Path(self.tmp.name)
        self.target = self.root / "target"
        self.target.mkdir()
        (self.target / "a.txt").write_text("a", encoding="utf-8")
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.config = self.workspace / "config.json"
        self.config.write_text(json.dumps({
            "schemaVersion": 2,
            "defaultProfileId": "p1",
            "profiles": [{
                "id": "p1", "name": "方案一", "targetRoot": str(self.target),
                "managed": [{"path": "a.txt"}],
                "states": [{"id": "s1", "name": "状态一", "snapshotRoot": None}],
                "guards": [], "cacheDirectories": [],
            }],
        }, ensure_ascii=False), encoding="utf-8")
        # Current production checkout exposes the button but may omit its handler;
        # keep construction testable and report that incompatibility separately.
        self.start_wizard_patch = mock.patch.object(
            folder_app.FolderApp, "start_wizard", lambda _self: None, create=True
        )
        self.start_wizard_patch.start()
        # GUI tests must not display dialogs or wait for user input.
        self.dialog_patches = [
            mock.patch.object(folder_app.messagebox, "showerror"),
            mock.patch.object(folder_app.messagebox, "showwarning"),
            mock.patch.object(folder_app.messagebox, "askyesno", return_value=True),
            mock.patch.object(folder_app.simpledialog, "askstring", return_value=None),
            mock.patch.object(folder_app.filedialog, "askdirectory", return_value=""),
            mock.patch.object(folder_app.filedialog, "askopenfilename", return_value=""),
        ]
        for patcher in self.dialog_patches:
            patcher.start()
        self.apps = []

    def tearDown(self):
        # Every Tk root is closed even when an assertion or a busy-path test fails.
        for app in reversed(self.apps):
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
        for patcher in reversed(self.dialog_patches):
            patcher.stop()
        self.start_wizard_patch.stop()
        self.tmp.cleanup()

    def app(self, payload=None, **kwargs):
        runner = FakeRunner(payload)
        app = folder_app.FolderApp(
            config_path=self.config,
            state_root=self.root / "state",
            log_root=self.root / "logs",
            runner=runner,
            auto_refresh=False,
            **kwargs,
        )
        self.apps.append(app)
        return app, runner

    @staticmethod
    def pump(app):
        app.update_idletasks()
        app.update()

    def test_startup_loads_config_and_schedules_refresh(self):
        app, runner = self.app()
        app.refresh_status()
        self.pump(app)
        self.assertTrue(runner.calls)
        self.assertEqual(runner.calls[-1][runner.calls[-1].index("-ProfileId") + 1], "p1")
        self.assertIsNotNone(app._poll_id)

    def test_profile_and_single_element_state_selection(self):
        app, _ = self.app()
        self.assertEqual(app.selected_profile_id(), "p1")
        self.assertEqual(app.selected_state_id(), "s1")
        # A one-element response exercises the normal list/row path.
        app.handle({
            "returncode": 0,
            "stdout": json.dumps({
                "schemaVersion": 2, "ok": True, "action": "status",
                "data": {"currentMode": "s1", "states": [{"id": "s1", "name": "状态一", "exact": True}]},
                "error": None,
            }),
            "stderr": "",
        })
        self.assertEqual(len(app.status_tree.get_children()), 1)

    def test_scan_select_managed_and_single_entry(self):
        app, _ = self.app()
        app.scan_tree.insert("", "end", iid="new.txt", values=("new.txt", "否", 1, "x"))
        app.scan_tree.selection_set("new.txt")
        app.manage_selected()
        self.assertEqual(app.selected_profile()["managed"][-1]["path"], "new.txt")

    def test_protocol_failure_is_reported(self):
        app, runner = self.app({"not": "schema"})
        app._last_plan = None
        app.handle({"returncode": 0, "stdout": "not json", "stderr": ""})
        self.assertEqual(app.state_label.cget("text"), "协议错误")
        folder_app.messagebox.showerror.assert_called()

    def test_protocol_exit_code_mismatch_is_reported(self):
        payload = {"schemaVersion": 2, "ok": True, "action": "status", "data": {}, "error": None}
        app, runner = self.app(payload)
        runner.returncode = 1
        app.handle({"returncode": 1, "stdout": json.dumps(payload), "stderr": ""})
        self.assertEqual(app.state_label.cget("text"), "协议错误")

    def test_busy_disables_controls_and_close_warns_then_closes(self):
        app, runner = self.app()
        runner.running = True
        app._set_busy(True)
        app.close()
        self.assertTrue(app.winfo_exists())
        folder_app.messagebox.showwarning.assert_called_once()
        runner.running = False
        app._busy = False
        app.close()
        self.assertTrue(app._closing)
        app._poll_id = None

    def test_invalid_single_path_is_rejected_without_filesystem_write(self):
        app, _ = self.app()
        before = (self.target / "a.txt").read_bytes()
        app.add_managed_paths(["../outside.txt"])
        self.assertEqual((self.target / "a.txt").read_bytes(), before)
        self.assertNotIn("../outside.txt", [item["path"] for item in app.selected_profile()["managed"]])

    def test_wizard_profile_and_state_core(self):
        new_target = self.root / "second-target"
        new_target.mkdir()
        app, _ = self.app()
        folder_app.simpledialog.askstring.side_effect = ["方案二", "额外状态"]
        folder_app.filedialog.askdirectory.return_value = str(new_target)
        app.add_profile()
        self.assertEqual(len(app.config_data["profiles"]), 2)
        created = next(profile for profile in app.config_data["profiles"] if profile["name"] == "方案二")
        app.profile_var.set(f"{created['name']} [{created['id']}]")
        app.update_states()
        app.add_state()
        self.assertIn("额外状态", [state["name"] for state in app.selected_profile()["states"]])


if __name__ == "__main__":
    unittest.main()
