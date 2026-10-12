"""GUI tests: the home screen, dialogs, and the paths that write metadata.

These use a synchronous fake runner so no PowerShell process is started and no
real dialog is ever displayed.  The end-to-end coverage that drives the real
backend lives in ``test_folder_backend.py`` and ``test_backend_contract.py``.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tkinter as tk

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fsx import errors
from fsx.model import Config
from fsx.ui import app as app_module
from fsx.ui import dialogs


class FakeRunner:
    """Synchronous runner: the callback fires inline, never on a thread."""

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
        stdout = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        result = {"returncode": self.returncode, "stdout": stdout, "stderr": "", "duration": 0}
        self.running = False
        if callback:
            callback(result)


def make_config(root: Path, *, captured: bool = True) -> Path:
    """Write a config outside the target tree (the store rejects overlaps)."""
    target = root / "target"
    target.mkdir(exist_ok=True)
    (target / "a.txt").write_text("a", encoding="utf-8")
    workspace = root / "cfg"
    workspace.mkdir(exist_ok=True)
    config = workspace / "folder-config.json"
    snapshot = str(workspace / "snap") if captured else None
    config.write_text(json.dumps({
        "schemaVersion": 2,
        "defaultProfileId": "p1",
        "profiles": [{
            "id": "p1", "name": "我的游戏", "targetRoot": str(target),
            "managed": [{"path": "a.txt"}],
            "states": [{"id": "s1", "name": "正式版", "snapshotRoot": snapshot},
                       {"id": "s2", "name": "测试版", "snapshotRoot": None}],
            "guards": [], "cacheDirectories": [],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    return config


class GuiTestCase(unittest.TestCase):
    """Shared fixture: a temp config plus every dialog silenced."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fsx-gui-")
        self.root = Path(self.tmp.name)
        self.config_path = make_config(self.root)
        self.workspace = self.root / "cfg"

        # Dialogs are recorded rather than shown, and default to "yes".
        self.errors: list = []
        self.messages: list = []
        self.confirms: list = []
        self.patchers = [
            mock.patch.object(app_module.dialogs, "show_error",
                              side_effect=lambda *a, **k: self.errors.append(a)),
            mock.patch.object(app_module.dialogs, "show_message",
                              side_effect=lambda *a, **k: self.messages.append(a)),
            mock.patch.object(app_module.dialogs, "confirm", return_value=True),
            mock.patch.object(app_module.dialogs, "confirm_switch", return_value=True),
            mock.patch.object(app_module.filedialog, "askdirectory", return_value=""),
            mock.patch.object(app_module.filedialog, "askopenfilename", return_value=""),
            mock.patch.object(app_module.simpledialog, "askstring", return_value=None),
            mock.patch.object(app_module.Wizard, "__init__", return_value=None),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.apps = []

    def tearDown(self):
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
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def make_app(self, payload=None, **kwargs):
        runner = FakeRunner(payload)
        app = app_module.App(config_path=self.config_path,
                             state_root=self.workspace / "state",
                             log_root=self.workspace / "logs",
                             runner=runner, auto_refresh=False, **kwargs)
        self.apps.append(app)
        return app, runner

    @staticmethod
    def pump(app):
        """Drain queued backend results, then let Tk process pending work.

        The real app drains on an ``after`` timer, which never fires here, so
        the queue is drained explicitly.
        """
        app.poll()
        app.update_idletasks()
        app.update()

    @staticmethod
    def is_visible(widget) -> bool:
        """True when a widget is packed, without needing a mapped window."""
        return widget.winfo_manager() != ""


class HomeScreenTests(GuiTestCase):
    """The screen a non-technical user sees first."""

    def test_home_is_shown_first_with_one_button_per_captured_version(self):
        app, _ = self.make_app()
        self.assertTrue(self.is_visible(app.home))
        self.assertFalse(self.is_visible(app.advanced))
        # Two versions exist; only the captured one gets a button.
        self.assertEqual(len(app.home._buttons), 1)
        self.assertIn("正式版", app.home._buttons[0].cget("text"))

    def test_uncaptured_version_explains_instead_of_offering_a_button(self):
        self.config_path = make_config(self.root, captured=False)
        app, _ = self.make_app()
        self.assertEqual(len(app.home._buttons), 0)
        self.assertIn("保存", app.home.current.cget("text"))

    def test_home_avoids_technical_vocabulary(self):
        """No profile/state/digest/hash jargon may leak onto the home screen."""
        app, _ = self.make_app()
        app.render_status({
            "currentMode": "s1", "targetRoot": "D:\\x", "managedCount": 1,
            "unmanagedCount": 0,
            "states": [{"id": "s1", "name": "正式版", "exact": True, "fileCount": 1}],
            "cacheDirectories": [], "blockedProcesses": []})
        self.pump(app)
        visible = " ".join(
            [app.home.current.cget("text"), app.home.detail.cget("text"),
             app.home.empty_hint.cget("text")]
            + [b.cget("text") for b in app.home._buttons])
        for jargon in ("digest", "SHA-256", "schemaVersion", "受管",
                       "纳管", "manifest", "快照", "采集"):
            self.assertNotIn(jargon, visible)

    def test_status_marks_the_current_version(self):
        app, _ = self.make_app()
        app.render_status({
            "currentMode": "s1", "targetRoot": "D:\\x", "managedCount": 1,
            "unmanagedCount": 0,
            "states": [{"id": "s1", "name": "正式版", "exact": True, "fileCount": 1},
                       {"id": "s2", "name": "测试版", "exact": False, "fileCount": 0,
                        "error": "State s2 has no snapshotRoot."}],
            "cacheDirectories": [], "blockedProcesses": []})
        self.pump(app)
        self.assertIn("正式版", app.home.current.cget("text"))

    def test_unknown_current_version_does_not_claim_a_version(self):
        app, _ = self.make_app()
        app.render_status({
            "currentMode": "unknown", "targetRoot": "D:\\x", "managedCount": 1,
            "unmanagedCount": 0,
            "states": [{"id": "s1", "name": "正式版", "exact": False, "fileCount": 1}],
            "cacheDirectories": [], "blockedProcesses": []})
        self.pump(app)
        self.assertNotIn("正式版", app.home.current.cget("text"))


class SwitchFlowTests(GuiTestCase):
    """One click plus one confirmation must reach Apply with a digest."""

    def _status(self, current="s1"):
        return {
            "schemaVersion": 2, "ok": True, "action": "status",
            "data": {"currentMode": current, "targetRoot": "D:\\x",
                     "managedCount": 1, "unmanagedCount": 0,
                     "states": [{"id": "s1", "name": "正式版", "exact": current == "s1",
                                 "fileCount": 1},
                                {"id": "s2", "name": "测试版", "exact": current == "s2",
                                 "fileCount": 1}],
                     "cacheDirectories": [], "blockedProcesses": []},
            "error": None,
        }

    def test_switch_runs_diff_then_apply_with_the_plan_digest(self):
        app, runner = self.make_app()
        runner.payload = {
            "schemaVersion": 2, "ok": True, "action": "diff",
            "data": {"changes": [], "counts": {"modify": 1}, "blockers": [],
                     "planDigest": "ABC123"},
            "error": None,
        }
        app.switch_to("s2", "测试版")
        self.pump(app)
        actions = [c[c.index("-Action") + 1] for c in runner.calls]
        self.assertEqual(actions, ["Diff", "Apply"])
        apply_call = runner.calls[-1]
        self.assertEqual(apply_call[apply_call.index("-StateId") + 1], "s2")
        self.assertEqual(apply_call[apply_call.index("-ExpectedPlanDigest") + 1], "ABC123")
        self.assertFalse(self.errors, self.errors)

    def test_switch_asks_once_and_only_once(self):
        app, runner = self.make_app()
        runner.payload = {
            "schemaVersion": 2, "ok": True, "action": "diff",
            "data": {"changes": [], "counts": {"modify": 1}, "blockers": [],
                     "planDigest": "ABC"},
            "error": None,
        }
        app.switch_to("s2", "测试版")
        self.pump(app)
        # confirm_switch on the home screen; the apply step must not re-ask.
        self.assertEqual(app_module.dialogs.confirm_switch.call_count, 1)
        self.assertEqual(app_module.dialogs.confirm.call_count, 0)

    def test_declining_the_confirmation_runs_nothing(self):
        app, runner = self.make_app()
        app_module.dialogs.confirm_switch.return_value = False
        app.switch_to("s2", "测试版")
        self.pump(app)
        self.assertEqual(runner.calls, [])

    def test_blockers_stop_before_apply_and_are_explained(self):
        app, runner = self.make_app()
        runner.payload = {
            "schemaVersion": 2, "ok": True, "action": "diff",
            "data": {"changes": [], "counts": {},
                     "blockers": ["target-conflict:a.txt"], "planDigest": "D"},
            "error": None,
        }
        app.switch_to("s2", "测试版")
        self.pump(app)
        actions = [c[c.index("-Action") + 1] for c in runner.calls]
        self.assertEqual(actions, ["Diff"], "must not write when a blocker is present")
        self.assertTrue(self.errors)
        payload = self.errors[-1][1]
        self.assertEqual(payload["error"]["code"], "plan-target-conflict")

    def test_missing_digest_is_reported_not_applied(self):
        app, runner = self.make_app()
        runner.payload = {
            "schemaVersion": 2, "ok": True, "action": "diff",
            "data": {"changes": [], "counts": {}, "blockers": [], "planDigest": ""},
            "error": None,
        }
        app.switch_to("s2", "测试版")
        self.pump(app)
        self.assertEqual([c[c.index("-Action") + 1] for c in runner.calls], ["Diff"])
        self.assertEqual(self.errors[-1][1]["error"]["code"], "digest-required")


class ErrorTranslationTests(GuiTestCase):
    """Every failure reaches the user in Chinese, never as raw backend text."""

    def test_backend_error_code_becomes_a_chinese_explanation(self):
        app, _ = self.make_app()
        app.handle({
            "returncode": 1, "stderr": "",
            "stdout": json.dumps({
                "schemaVersion": 2, "ok": False, "action": "apply", "data": None,
                "error": {"code": "guard-process-running",
                          "message": "Guard process is running."}}),
        })
        self.assertTrue(self.errors)
        payload = self.errors[-1][1]
        title, text, step, extra = dialogs.explain_error(payload)
        self.assertNotIn("Guard", title + text + step)
        self.assertIn("关闭", step)
        self.assertIn("guard-process-running", extra)  # detail keeps the code

    def test_unrecognized_code_still_gets_chinese_and_hides_english(self):
        app, _ = self.make_app()
        app.handle({
            "returncode": 1, "stderr": "",
            "stdout": json.dumps({
                "schemaVersion": 2, "ok": False, "action": "status", "data": None,
                "error": {"code": "some-future-code",
                          "message": "Something exploded in the backend."}}),
        })
        self.assertTrue(self.errors)
        title, text, step, detail = dialogs.explain_error(self.errors[-1][1])
        self.assertNotIn("exploded", title + text + step)
        self.assertIn("some-future-code", detail)

    def test_protocol_failure_is_reported_as_a_chinese_error(self):
        app, _ = self.make_app()
        app.handle({"returncode": 0, "stdout": "not json at all", "stderr": ""})
        self.assertTrue(self.errors)
        self.assertEqual(self.errors[-1][1]["error"]["code"], "backend-bad-json")

    def test_exit_code_mismatch_is_refused(self):
        app, _ = self.make_app()
        app.handle({
            "returncode": 1, "stderr": "",
            "stdout": json.dumps({"schemaVersion": 2, "ok": True, "action": "status",
                                  "data": {}, "error": None}),
        })
        self.assertTrue(self.errors)
        self.assertEqual(self.errors[-1][1]["error"]["code"], "backend-exit-mismatch")

    def test_every_known_code_has_chinese_primitives_and_no_jargon(self):
        for code, (headline, reason, action) in errors.EXPLANATIONS.items():
            with self.subTest(code=code):
                self.assertTrue(headline.strip(), code)
                self.assertTrue(reason.strip(), code)
                self.assertTrue(action.strip(), code)
                combined = headline + reason + action
                self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in combined),
                                f"{code} has no Chinese text")
                for jargon in ("digest", "SHA-256", "schemaVersion", "manifest"):
                    self.assertNotIn(jargon, combined, code)


class MetadataEditingTests(GuiTestCase):
    def test_managed_path_rejects_traversal_without_writing(self):
        app, _ = self.make_app()
        before = self.config_path.read_bytes()
        app.add_managed_paths(["../outside.txt"])
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertTrue(self.errors)
        self.assertEqual(self.errors[-1][1]["error"]["code"], "config-managed-directory")
        profile = app.selected_profile()
        self.assertNotIn("../outside.txt", [m["path"] for m in profile["managed"]])

    def test_adding_a_managed_file_saves_and_marks_versions_stale(self):
        app, _ = self.make_app()
        app.add_managed_paths(["b.txt"])
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        profile = saved["profiles"][0]
        self.assertIn("b.txt", [m["path"] for m in profile["managed"]])
        self.assertTrue(profile["states"][0]["needsCapture"])

    def test_deleting_the_only_profile_is_refused(self):
        app, _ = self.make_app()
        before = self.config_path.read_bytes()
        app.delete_profile()
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertTrue(self.messages)

    def test_adding_a_version_appends_without_touching_the_others(self):
        app, _ = self.make_app()
        app_module.simpledialog.askstring.return_value = "第三个版本"
        app.add_state()
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        names = [s["name"] for s in saved["profiles"][0]["states"]]
        self.assertIn("第三个版本", names)
        self.assertIn("正式版", names)


class BusyStateTests(GuiTestCase):
    def test_close_is_refused_while_writing(self):
        app, runner = self.make_app()
        runner.running = True
        app._set_busy(True)
        app.close()
        self.assertTrue(app.winfo_exists())
        self.assertTrue(self.messages, "closing during a write must warn")

    def test_starting_a_second_operation_is_refused(self):
        app, runner = self.make_app()
        app._busy = True
        self.assertFalse(app.call(["-Action", "Status"]))
        self.assertTrue(self.messages)

    def test_buttons_are_disabled_while_busy(self):
        app, _ = self.make_app()
        app._set_busy(True)
        # ttk's cget returns a Tcl object, so compare through str().
        for button in app.home._buttons:
            self.assertEqual(str(button.cget("state")), "disabled")
        self.assertEqual(str(app.home._wizard_button.cget("state")), "disabled")

    def test_busy_state_survives_a_redraw(self):
        """A redraw while busy must not re-enable the version buttons."""
        app, _ = self.make_app()
        app._set_busy(True)
        app.home.show(app.config, app._last_status)
        for button in app.home._buttons:
            self.assertEqual(str(button.cget("state")), "disabled")


class ModelTests(unittest.TestCase):
    """The id-based lookups that replaced label re-parsing."""

    def setUp(self):
        self.data = {
            "schemaVersion": 2, "defaultProfileId": "p1",
            "profiles": [{
                "id": "p1", "name": "名字 [带方括号]", "targetRoot": "rel-target",
                "managed": [{"path": "Sub\\File.TXT"}],
                "states": [{"id": "s1", "name": "稳定 [v1]", "snapshotRoot": "snap"},
                           {"id": "s2", "name": "测试", "snapshotRoot": None}],
                "guards": [], "cacheDirectories": []}]}
        self.config = Config(self.data, Path("C:/base"))

    def test_names_with_brackets_do_not_break_lookups(self):
        self.assertEqual(self.config.profile("p1").name, "名字 [带方括号]")
        self.assertEqual(self.config.state("p1", "s1").display_name, "稳定 [v1]")

    def test_state_lookup_is_by_id(self):
        self.assertIsNone(self.config.state("p1", "稳定 [v1]"))
        self.assertEqual(self.config.state("p1", "s2").id, "s2")

    def test_captured_means_snapshot_present_and_not_stale(self):
        self.assertTrue(self.config.is_captured("p1", "s1"))
        self.assertFalse(self.config.is_captured("p1", "s2"))
        self.data["profiles"][0]["states"][0]["needsCapture"] = True
        self.assertFalse(Config(self.data).is_captured("p1", "s1"))

    def test_managed_paths_are_normalized(self):
        self.assertIn("sub\\file.txt", self.config.managed_paths("p1"))

    def test_relative_target_root_resolves_against_config_dir(self):
        self.assertEqual(self.config.target_root("p1"), Path("C:/base/rel-target"))

    def test_initial_profile_falls_back_when_default_is_missing(self):
        self.data["defaultProfileId"] = "gone"
        self.assertEqual(Config(self.data).initial_profile_id(), "p1")


if __name__ == "__main__":
    unittest.main()
