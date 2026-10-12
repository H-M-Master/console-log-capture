"""End-to-end tests: the real application against the real backend.

Each test builds a temporary workspace, runs ``folder-switcher.ps1`` for real an
drives the actual window, then asserts on what happened to the files on disk.
Dialogs are answered rather than displayed; nothing else is faked.

This is where behavior that crosses into the filesystem is verified.  Pure logic
(translations, lookups, protocol parsing) lives in the unit suites.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_harness import E2ETestCase, run_backend


class FirstRunWizardTests(E2ETestCase):
    """A new user with no config must reach a working switch button."""

    def test_wizard_creates_a_version_and_saves_its_content(self):
        from fsx.ui import app as app_module
        from fsx.ui import wizard as wizard_module

        source = self.make_source("stable", {"sub/one.txt": "STABLE"})
        self.write_target({"sub/one.txt": "STABLE", "sub/extra.txt": "KEEP ME"})

        app = self.make_app()
        self.assertFalse(self.config.exists(), "no config should exist yet")
        self.assertEqual(len(app.home._buttons), 0)

        wizard = wizard_module.Wizard(app, on_finish=app._wizard_finish)
        app.update()
        wizard.target_var.set(str(self.target))
        wizard._next()                      # step 1 -> 2, scanning the folder
        app.update()
        self.assertIn("sub\\one.txt", wizard._files)

        wizard.tree.selection_set(["sub\\one.txt"])
        wizard._next()                      # step 2 -> 3
        app.update()
        wizard.name_var.set("稳定版")
        wizard.source_mode.set("external")
        wizard.source_var.set(str(source))
        wizard._next()                      # finish: save + capture
        self.pump(app)

        self.assertFalse(self.errors(), self.errors())
        self.assertTrue(self.config.exists())
        data = json.loads(self.config.read_text(encoding="utf-8"))
        profile = data["profiles"][0]
        self.assertEqual([m["path"] for m in profile["managed"]], ["sub\\one.txt"])
        self.assertTrue(profile["states"][0]["snapshotRoot"])

        # The whole point of the wizard: the version is now switchable.
        self.assertEqual(len(app.home._buttons), 1)
        self.assertIn("稳定版", app.home._buttons[0].cget("text"))

    def test_wizard_cancel_writes_nothing(self):
        from fsx.ui import wizard as wizard_module

        self.write_target({"sub/one.txt": "STABLE"})
        app = self.make_app()
        wizard = wizard_module.Wizard(app, on_finish=lambda *a: None)
        app.update()
        wizard._cancel()
        app.update()
        self.assertFalse(self.config.exists())


class SwitchTests(E2ETestCase):
    """One click must switch the managed files and leave everything else alone."""

    def setUp(self):
        super().setUp()
        self.make_source("a", {"sub/one.txt": "A-DAILY",
                               "sub/two.bin": b"A-DAILY\x00\x01"})
        self.make_source("b", {"sub/one.txt": "B-AUTO",
                               "sub/two.bin": b"B-AUTO\x00\x01"})
        self.write_target({"sub/one.txt": "SCRATCH",
                           "sub/two.bin": b"SCRATCH\x00\x01",
                           "untouched.txt": "DO NOT TOUCH",
                           "sub/also-untouched.txt": "ALSO DO NOT TOUCH"})
        self.write_config(["sub\\one.txt", "sub\\two.bin"], ["a", "b"])
        self.capture("a", "a")
        self.capture("b", "b")

    def _settle_into_a(self, app):
        """Put the target into version A, so switching to B is a known move."""
        digest = run_backend(self.config, self.state_root, "-Action", "Diff",
                             "-ProfileId", "p1", "-StateId", "a",
                             "-AllowUnknown")["data"]["planDigest"]
        result = run_backend(self.config, self.state_root, "-Action", "Apply",
                             "-ProfileId", "p1", "-StateId", "a",
                             "-AllowUnknown", "-ExpectedPlanDigest", digest)
        self.assertTrue(result["ok"], result)
        app.refresh_status()
        self.pump(app)
        # Forget the settling calls; the assertions below are about the click.
        app.runner.calls.clear()

    def test_one_click_switch_changes_managed_files_only(self):
        app = self.make_app()
        self._settle_into_a(app)
        self.assert_target_is("sub/one.txt", "A-DAILY")

        app.switch_to("b", "b")
        self.pump(app)

        self.assertFalse(self.errors(), self.errors())
        # One click: diff to learn the plan, then apply that exact plan.
        self.assertEqual(app.runner.actions()[:2], ["Diff", "Apply"])
        self.assert_target_is("sub/one.txt", "B-AUTO")
        self.assertEqual((self.target / "sub" / "two.bin").read_bytes(),
                         b"B-AUTO\x00\x01")
        # The promise the home screen makes: nothing else is touched.
        self.assert_unchanged("untouched.txt", "DO NOT TOUCH")
        self.assert_unchanged("sub/also-untouched.txt", "ALSO DO NOT TOUCH")

    def test_switch_updates_the_home_screen(self):
        app = self.make_app()
        self._settle_into_a(app)
        self.assertIn("a", app.home.current.cget("text"))
        app.switch_to("b", "b")
        self.pump(app)
        self.assertIn("当前版本：b", app.home.current.cget("text"))

    def test_undo_restores_the_previous_content(self):
        app = self.make_app()
        self._settle_into_a(app)
        app.switch_to("b", "b")
        self.pump(app)
        self.assert_target_is("sub/one.txt", "B-AUTO")

        app.refresh_transactions()
        app._update_undo()
        self.assertEqual(str(app.home._undo_button.cget("state")), "normal")
        app.undo_last_switch()
        self.pump(app)

        self.assertFalse(self.errors(), self.errors())
        self.assert_target_is("sub/one.txt", "A-DAILY")
        self.assertEqual((self.target / "sub" / "two.bin").read_bytes(),
                         b"A-DAILY\x00\x01")
        self.assert_unchanged("untouched.txt", "DO NOT TOUCH")

    def test_content_changed_behind_the_tools_back_stops_the_switch(self):
        app = self.make_app()
        self._settle_into_a(app)
        # Somebody edits a managed file outside the tool.
        (self.target / "sub" / "one.txt").write_text("EDITED BY HAND",
                                                     encoding="utf-8")

        app.switch_to("b", "b")
        self.pump(app)

        self.assertEqual(app.runner.actions(), ["Diff"],
                         "an unrecognized edit must not be overwritten")
        self.assertTrue(self.errors(), "the user must be told why")
        self.assert_target_is("sub/one.txt", "EDITED BY HAND")

    def test_switching_to_the_current_version_changes_nothing(self):
        app = self.make_app()
        self._settle_into_a(app)
        before = {p: p.read_bytes() for p in self.target.rglob("*") if p.is_file()}

        app.switch_to("a", "a")
        self.pump(app)

        after = {p: p.read_bytes() for p in self.target.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_switch_is_refused_while_a_guard_process_runs(self):
        app = self.make_app()
        self._settle_into_a(app)
        # "explorer" always exists, so it makes a reliable stand-in guard.
        data = json.loads(self.config.read_text(encoding="utf-8"))
        data["profiles"][0]["guards"] = ["explorer"]
        self.config.write_text(json.dumps(data), encoding="utf-8")
        app.load_config()

        app.switch_to("b", "b")
        self.pump(app)

        self.assert_target_is("sub/one.txt", "A-DAILY")
        self.assertTrue(self.errors())
        code = self.errors()[-1][1][1]["error"]["code"]
        self.assertEqual(code, "guard-process-running")


class CaptureTests(E2ETestCase):
    """Saving a version's content must leave it usable and switchable."""

    def setUp(self):
        super().setUp()
        self.make_source("only", {"sub/one.txt": "FIRST"})
        self.write_target({"sub/one.txt": "FIRST", "other.txt": "OTHER"})
        self.write_config(["sub\\one.txt"], ["s1"])

    def test_capture_current_makes_the_version_switchable(self):
        from fsx.ui import app as app_module

        app = self.make_app()
        self.assertEqual(len(app.home._buttons), 0)

        app.capture_current()
        self.pump(app)

        self.assertFalse(self.errors(), self.errors())
        self.assertEqual(len(app.home._buttons), 1,
                         "the saved version should offer a switch button")
        data = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertTrue(data["profiles"][0]["states"][0]["snapshotRoot"])

    def test_capture_then_switch_elsewhere_and_back(self):
        """Save the current content, move away, then switch back to it."""
        self.make_source("second", {"sub/one.txt": "SECOND"})
        self.write_config(["sub\\one.txt"], ["s1", "s2"])
        app = self.make_app()

        # Save what is on disk right now as s1.
        app.capture_current()
        self.pump(app)
        self.assertFalse(self.errors(), self.errors())
        self.assert_target_is("sub/one.txt", "FIRST")

        # Capture s2 from a different source directory.
        self.capture("s2", "second")
        app.load_config()
        self.pump(app)

        # Switch to s2, then back to s1.
        digest = run_backend(self.config, self.state_root, "-Action", "Diff",
                             "-ProfileId", "p1", "-StateId", "s2",
                             "-AllowUnknown")["data"]["planDigest"]
        applied = run_backend(self.config, self.state_root, "-Action", "Apply",
                              "-ProfileId", "p1", "-StateId", "s2",
                              "-AllowUnknown", "-ExpectedPlanDigest", digest)
        self.assertTrue(applied["ok"], applied)
        self.assert_target_is("sub/one.txt", "SECOND")

        app._set_active_state("s1")
        app.switch_to("s1", "s1")
        self.pump(app)
        self.assertFalse(self.errors(), self.errors())
        self.assert_target_is("sub/one.txt", "FIRST")
        self.assert_unchanged("other.txt", "OTHER")


class ScanTests(E2ETestCase):
    """Managing files from the advanced screen must not touch the target."""

    def setUp(self):
        super().setUp()
        self.write_target({"one.txt": "ONE", "two.txt": "TWO", "three.txt": "THREE"})
        self.write_config(["one.txt"], ["s1"])

    def test_scan_lists_files_and_marks_managed_ones(self):
        app = self.make_app()
        app.scan()
        self.pump(app)

        rows = app.advanced.scan_tree.get_children()
        self.assertEqual(len(rows), 3)
        managed_column = [app.advanced.scan_tree.item(r, "values")[1] for r in rows]
        self.assertEqual(managed_column.count("是"), 1)
        self.assertEqual(managed_column.count("否"), 2)

    def test_adding_and_removing_managed_paths_updates_the_target_config(self):
        app = self.make_app()
        app.add_managed_paths(["two.txt"])
        paths = [m["path"] for m in app.selected_profile()["managed"]]
        self.assertIn("two.txt", paths)
        # The version must be marked stale, since its saved content predates
        # the new file list.
        self.assertTrue(app.selected_profile()["states"][0].get("needsCapture"))

        app.advanced.scan_tree.insert("", "end", iid="two.txt", values=("two.txt", "是", 3, "x"))
        app.advanced.scan_tree.selection_set("two.txt")
        app.unmanage_selected()
        paths = [m["path"] for m in app.selected_profile()["managed"]]
        self.assertNotIn("two.txt", paths)


class ConfirmationLeakTests(E2ETestCase):
    """A confirmation must apply to one request only, never to the next.

    The home screen confirms before it runs the diff, so the apply step skips
    its own prompt.  That confirmation has to die with the request it belongs
    to.  The case that matters is a request whose result never arrives in a
    usable form: if the flag survived that, the *next* apply would write files
    without asking, which is the one thing the confirmation exists to prevent.
    """

    def setUp(self):
        super().setUp()
        self.make_source("a", {"sub/one.txt": "A"})
        self.make_source("b", {"sub/one.txt": "B"})
        self.write_target({"sub/one.txt": "SCRATCH"})
        self.write_config(["sub\\one.txt"], ["a", "b"])
        self.capture("a", "a")
        self.capture("b", "b")
        # Get the target into a known version so later moves are legitimate.
        self.apply_state("a")

    def _spoil_next_result(self, app):
        """Make the very next reply unreadable, then behave normally again.

        Stands in for a backend that died on one request: the request has no
        usable result, so whatever it had set aside for the follow-up must not
        survive into the next one.
        """
        original = app.runner.run

        def run(args, callback=None):
            app.runner.run = original          # one-shot
            app.runner.running = True
            app.runner.calls.append(list(args))
            app.runner.running = False
            (callback or app.runner.callback)({
                "returncode": 1, "stdout": "backend died", "stderr": "",
                "duration": 0.0, "command": []})

        app.runner.run = run

    def test_unusable_result_does_not_confirm_the_next_apply(self):
        from fsx.ui import app as app_module

        app = self.make_app()
        self._spoil_next_result(app)

        app.switch_to("b", "b")
        self.pump(app)
        self.assertTrue(self.errors(), "the unreadable reply should have reported")
        self.assertFalse(app._switch_confirmed,
                         "the confirmation must not outlive its request")
        self.assert_target_is("sub/one.txt", "A")

        # The user did not confirm this next request, so it must ask.
        app_module.dialogs.confirm.reset_mock()
        app_module.dialogs.confirm.return_value = False   # user declines
        app.advanced.dry_var.set(False)                   # will write if allowed
        app._set_active_state("b")
        app.apply_selected_state()
        self.pump(app)
        self.assertGreaterEqual(app_module.dialogs.confirm.call_count, 1,
                                "an unconfirmed apply must still ask")
        self.assert_target_is("sub/one.txt", "A")

    def test_home_switch_confirms_once_then_applies(self):
        from fsx.ui import app as app_module
        app = self.make_app()
        app_module.dialogs.confirm_switch.reset_mock()
        app_module.dialogs.confirm.reset_mock()
        app.switch_to("b", "b")
        self.pump(app)
        self.assertEqual(app_module.dialogs.confirm_switch.call_count, 1)
        self.assertEqual(app_module.dialogs.confirm.call_count, 0,
                         "the home screen already asked")
        self.assert_target_is("sub/one.txt", "B")


class BusyGuardTests(E2ETestCase):
    """A write must not be interruptible, and two must not overlap."""

    def setUp(self):
        super().setUp()
        self.write_target({"one.txt": "ONE"})
        self.write_config(["one.txt"], ["s1"])

    def test_closing_during_a_write_is_refused(self):
        app = self.make_app()
        app._busy = True
        app._set_busy(True)
        app.close()
        self.assertTrue(app.winfo_exists(), "the window must stay open mid-write")
        self.assertTrue([d for d in self.dialogs if d[0] == "message"],
                        "the user must be told why closing was refused")
        app._busy = False

    def test_a_second_operation_is_refused_while_one_runs(self):
        app = self.make_app()
        app._busy = True
        started = app.call(["-Action", "Status"])
        self.assertFalse(started)
        self.assertEqual(app.runner.actions(), [])
        self.assertTrue([d for d in self.dialogs if d[0] == "message"])

    def test_buttons_are_disabled_while_busy(self):
        app = self.make_app()
        app._set_busy(True)
        self.assertEqual(str(app.home._wizard_button.cget("state")), "disabled")
        self.assertEqual(str(app.home._advanced_button.cget("state")), "disabled")
        app._set_busy(False)
        self.assertEqual(str(app.home._wizard_button.cget("state")), "normal")


if __name__ == "__main__":
    unittest.main()
