"""Unit tests for pure logic: error translation, protocol parsing, lookups.

Behavior that touches the filesystem or the backend is verified end to end in
``test_e2e_app.py``.  This file stays free of fakes precisely because it tests
things that need no backend: the translation table, the protocol validator, the
configuration lookups, and the metadata editing rules.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fsx import backend, errors
from fsx.model import Config
from fsx.ui import dialogs


class ErrorTranslationTests(unittest.TestCase):
    """Every failure reaches the user in Chinese, never as raw backend text."""

    def test_known_code_becomes_a_chinese_explanation(self):
        payload = {"error": {"code": "guard-process-running",
                             "message": "Guard process is running."}}
        headline, reason, action, detail = dialogs.explain_error(payload)
        self.assertNotIn("Guard", headline + reason + action)
        self.assertIn("关闭", action)
        self.assertIn("guard-process-running", detail)  # detail keeps the code

    def test_unrecognized_code_still_hides_the_english(self):
        payload = {"error": {"code": "some-future-code",
                             "message": "Something exploded in the backend."}}
        headline, reason, action, detail = dialogs.explain_error(payload)
        self.assertNotIn("exploded", headline + reason + action)
        self.assertIn("some-future-code", detail)

    def test_every_known_code_has_chinese_and_no_jargon(self):
        for code, (headline, reason, action) in errors.EXPLANATIONS.items():
            with self.subTest(code=code):
                combined = headline + reason + action
                self.assertTrue(headline.strip(), code)
                self.assertTrue(action.strip(), f"{code} has no next step")
                self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in combined),
                                f"{code} has no Chinese text")
                for jargon in ("digest", "SHA-256", "schemaVersion", "manifest"):
                    self.assertNotIn(jargon, combined, code)


class ProtocolTests(unittest.TestCase):
    """What the runner produces must be validated before anything trusts it."""

    def _result(self, stdout, returncode=0):
        return {"returncode": returncode, "stdout": stdout, "stderr": ""}

    def test_valid_envelope_passes_through(self):
        payload = {"schemaVersion": 2, "ok": True, "action": "status",
                   "data": {}, "error": None}
        parsed = backend.parse_result(self._result(json.dumps(payload)))
        self.assertEqual(parsed["action"], "status")

    def test_unparseable_output_is_a_backend_error(self):
        with self.assertRaises(backend.BackendError) as ctx:
            backend.parse_result(self._result("not json at all"))
        self.assertEqual(ctx.exception.code, "backend-bad-json")

    def test_empty_output_is_a_backend_error(self):
        with self.assertRaises(backend.BackendError) as ctx:
            backend.parse_result(self._result(""))
        self.assertEqual(ctx.exception.code, "backend-no-output")

    def test_wrong_schema_is_refused(self):
        payload = {"schemaVersion": 1, "ok": True, "action": "status",
                   "data": {}, "error": None}
        with self.assertRaises(backend.BackendError) as ctx:
            backend.parse_result(self._result(json.dumps(payload)))
        self.assertEqual(ctx.exception.code, "backend-contract")

    def test_missing_field_is_refused(self):
        payload = {"schemaVersion": 2, "ok": True, "action": "status", "data": {}}
        with self.assertRaises(backend.BackendError) as ctx:
            backend.parse_result(self._result(json.dumps(payload)))
        self.assertEqual(ctx.exception.code, "backend-contract")

    def test_exit_code_mismatch_is_refused_in_both_directions(self):
        ok_but_failed = {"schemaVersion": 2, "ok": True, "action": "status",
                         "data": {}, "error": None}
        with self.assertRaises(backend.BackendError) as ctx:
            backend.parse_result(self._result(json.dumps(ok_but_failed), returncode=1))
        self.assertEqual(ctx.exception.code, "backend-exit-mismatch")

        failed_but_succeeded = {"schemaVersion": 2, "ok": False, "action": "status",
                                "data": None, "error": {"code": "x", "message": "y"}}
        with self.assertRaises(backend.BackendError) as ctx:
            backend.parse_result(self._result(json.dumps(failed_but_succeeded), returncode=0))
        self.assertEqual(ctx.exception.code, "backend-exit-mismatch")

    def test_failure_envelope_is_returned_for_the_caller_to_handle(self):
        payload = {"schemaVersion": 2, "ok": False, "action": "status", "data": None,
                   "error": {"code": "path-unsafe-relative", "message": "bad"}}
        parsed = backend.parse_result(self._result(json.dumps(payload), returncode=1))
        self.assertFalse(parsed["ok"])
        self.assertEqual(backend.error_from(parsed).code, "path-unsafe-relative")

    def test_backend_error_is_explainable_like_a_payload(self):
        error = backend.BackendError("backend-timeout", "查询超时")
        headline, reason, action, detail = backend.explain(error)
        self.assertIn("超时", headline)
        self.assertIn("backend-timeout", detail)


class RunnerCommandTests(unittest.TestCase):
    """The command line is built in one place and must not disable policy."""

    def test_default_command_does_not_bypass_execution_policy(self):
        runner = backend.PowerShellRunner()
        command = runner.command(["-Action", "Status"])
        self.assertIn("-ExecutionPolicy", command)
        policy = command[command.index("-ExecutionPolicy") + 1]
        self.assertEqual(policy, "RemoteSigned")
        self.assertNotIn("Bypass", command)

    def test_command_carries_the_configured_paths(self):
        runner = backend.PowerShellRunner(script_path="s.ps1", config_path="c.json",
                                          state_root="st")
        command = runner.command(["-Action", "Status"])
        self.assertEqual(command[command.index("-File") + 1], "s.ps1")
        self.assertEqual(command[command.index("-Config") + 1], "c.json")
        self.assertEqual(command[command.index("-StateRoot") + 1], "st")


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

    def test_state_lookup_is_by_id_not_label(self):
        self.assertIsNone(self.config.state("p1", "稳定 [v1]"))
        self.assertEqual(self.config.state("p1", "s2").id, "s2")

    def test_captured_means_snapshot_present_and_not_stale(self):
        self.assertTrue(self.config.is_captured("p1", "s1"))
        self.assertFalse(self.config.is_captured("p1", "s2"))
        self.data["profiles"][0]["states"][0]["needsCapture"] = True
        self.assertFalse(Config(self.data).is_captured("p1", "s1"))

    def test_managed_paths_are_normalized_for_comparison(self):
        self.assertIn("sub\\file.txt", self.config.managed_paths("p1"))

    def test_relative_target_root_resolves_against_config_dir(self):
        self.assertEqual(self.config.target_root("p1"), Path("C:/base/rel-target"))

    def test_initial_profile_falls_back_when_default_is_missing(self):
        self.data["defaultProfileId"] = "gone"
        self.assertEqual(Config(self.data).initial_profile_id(), "p1")

    def test_empty_config_is_recognized(self):
        self.assertTrue(Config({}).is_empty)
        self.assertFalse(self.config.is_empty)


class MetadataRuleTests(unittest.TestCase):
    """Rules that decide what may be written, checked without a window."""

    def test_safe_relative_rejects_traversal_and_absolute_paths(self):
        from fsx.ui.app import safe_relative
        for bad in ("../escape.txt", "..\\escape.txt", "C:\\abs.txt", "", "  "):
            with self.subTest(path=bad):
                with self.assertRaises(ValueError):
                    safe_relative(bad)

    def test_safe_relative_normalizes_separators(self):
        from fsx.ui.app import safe_relative
        self.assertEqual(safe_relative("sub/one.txt"), "sub\\one.txt")

    def test_plan_code_maps_blockers_to_the_right_explanation(self):
        from fsx.ui.app import plan_code_for
        self.assertEqual(plan_code_for(["target-conflict:a.txt"]), "plan-target-conflict")
        self.assertEqual(plan_code_for(["delete-not-authorized:a.txt"]),
                         "plan-delete-unauthorized")
        self.assertEqual(plan_code_for(["delete-hash-unrecognized:a.txt"]),
                         "plan-delete-unrecognized")
        self.assertEqual(plan_code_for(["something-else"]), "plan-blocked")

    def test_status_and_kind_text_are_chinese(self):
        from fsx.ui.app import kind_text, status_text
        for kind in ("add", "modify", "remove", "unchanged"):
            self.assertTrue(any("\u4e00" <= c <= "\u9fff" for c in kind_text(kind)))
        self.assertEqual(status_text("recovery-required"), "需要恢复")
        self.assertEqual(status_text("completed"), "已完成")


if __name__ == "__main__":
    unittest.main()
