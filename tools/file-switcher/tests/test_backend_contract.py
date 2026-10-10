"""Contract tests for folder-switcher.ps1 error codes.

The GUI decides what to tell the user from error.code, so each code is a
promise the backend makes.  These tests hold the backend to that promise by
running the real script against temporary directories.
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PS = HERE.parent / "folder-switcher.ps1"
# The backend writes JSON with a BOM; read it the same way it writes it.
BOM = "utf-8-sig"


class Backend:
    """Thin wrapper that runs the script and parses its single-line result."""

    def __init__(self, config, state_root):
        self.config = Path(config)
        self.state_root = Path(state_root)

    def __call__(self, action, *extra, config=None, timeout=90):
        cmd = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
               "-ExecutionPolicy", "RemoteSigned", "-File", str(PS),
               "-Config", str(config or self.config),
               "-StateRoot", str(self.state_root), "-Action", action, *extra]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        if not lines:
            raise AssertionError(f"backend produced no JSON; stderr={proc.stderr!r}")
        return json.loads(lines[-1])


class BackendContractTests(unittest.TestCase):
    """Every failure must carry a code the GUI can act on."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fs-contract-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.target = self.tmp / "target"
        self.target.mkdir()
        (self.target / "sub").mkdir()
        # Deliberately matches neither state, so A and B are both real moves.
        (self.target / "sub" / "one.txt").write_text("SCRATCH", encoding="utf-8")
        self.src_a = self.tmp / "a"
        self.src_b = self.tmp / "b"
        for d, tag in ((self.src_a, "A-DAILY"), (self.src_b, "B-AUTO")):
            (d / "sub").mkdir(parents=True)
            (d / "sub" / "one.txt").write_text(tag, encoding="utf-8")
        self.state_root = self.tmp / "folder-data"
        self.config = self.tmp / "folder-config.json"
        self.write_config()
        self.ps = Backend(self.config, self.state_root)
        for state, src in (("a", self.src_a), ("b", self.src_b)):
            result = self.ps("Capture", "-ProfileId", "p1",
                             "-StateId", state, "-SourceRoot", str(src))
            self.assertTrue(result["ok"], result)

    def write_config(self, guards=None, path=None, mutate=None):
        payload = {
            "schemaVersion": 2,
            "defaultProfileId": "p1",
            "profiles": [{
                "id": "p1", "name": "contract", "targetRoot": str(self.target),
                "managed": [{"path": "sub\\one.txt"}],
                "states": [{"id": "a", "name": "A", "snapshotRoot": None},
                           {"id": "b", "name": "B", "snapshotRoot": None}],
                "guards": guards or [], "cacheDirectories": []}]}
        if mutate:
            mutate(payload)
        out = Path(path or self.config)
        # utf-8-sig so the backend's UTF8 reader sees no stray BOM bytes.
        out.write_text(json.dumps(payload), encoding=BOM)
        return out

    def code(self, result):
        self.assertFalse(result["ok"], f"expected failure, got {result}")
        return (result.get("error") or {}).get("code")

    def message(self, result):
        return (result.get("error") or {}).get("message") or ""

    def digest_for(self, state, *extra):
        result = self.ps("Diff", "-ProfileId", "p1", "-StateId", state, *extra)
        self.assertTrue(result["ok"], result)
        return result["data"]["planDigest"]

    def test_guard_process_running_is_its_own_code(self):
        # "explorer" is always running on Windows, so it makes a reliable guard.
        # Note this rewrites the config, which drops the captured snapshotRoot.
        self.write_config(guards=["explorer"])
        self.ps("Capture", "-ProfileId", "p1", "-StateId", "b",
                "-SourceRoot", str(self.src_b))
        result = self.ps("Apply", "-ProfileId", "p1", "-StateId", "b",
                         "-AllowUnknown", "-ExpectedPlanDigest", "0" * 64)
        self.assertEqual(self.code(result), "guard-process-running")
        # The message must not be the doubled form the old Fail() produced.
        self.assertEqual(self.message(result).count("Guard process is running"), 1)

    def test_incomplete_transaction_blocks_apply(self):
        tx = self.state_root / "transactions" / "leftover"
        tx.mkdir(parents=True)
        (tx / "journal.json").write_text(
            json.dumps({"schemaVersion": 2, "status": "applying", "files": []}),
            encoding="utf-8")
        result = self.ps("Apply", "-ProfileId", "p1", "-StateId", "b",
                         "-AllowUnknown", "-ExpectedPlanDigest", "0" * 64)
        self.assertEqual(self.code(result), "transaction-incomplete")

    def test_delete_authorization_is_a_ladder(self):
        # Removing a managed file passes through up to two gates: "this is a
        # delete at all" (-AllowManagedDelete), then "the current content was
        # never saved" (which nothing can authorize).  B is captured from an
        # empty source, so applying it removes the file.
        self.write_config()
        self.ps("Capture", "-ProfileId", "p1", "-StateId", "a",
                "-SourceRoot", str(self.src_a))
        empty = self.tmp / "empty"
        empty.mkdir()
        result = self.ps("Capture", "-ProfileId", "p1", "-StateId", "b",
                         "-SourceRoot", str(empty))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["missingCount"], 1)

        tracked = self.target / "sub" / "one.txt"
        unknown = b"NOT-IN-ANY-SAVED-STATE"
        tracked.write_bytes(unknown)

        # Gate 1: no opt-in at all.
        diff = self.ps("Diff", "-ProfileId", "p1", "-StateId", "b", "-AllowUnknown")
        self.assertTrue(diff["data"]["blockers"], diff["data"])
        blocked = self.ps("Apply", "-ProfileId", "p1", "-StateId", "b", "-AllowUnknown",
                          "-ExpectedPlanDigest", diff["data"]["planDigest"])
        self.assertEqual(self.code(blocked), "plan-delete-unauthorized")
        self.assertEqual(tracked.read_bytes(), unknown)

        # Gate 2: the delete is authorized, but the content still is not known.
        diff = self.ps("Diff", "-ProfileId", "p1", "-StateId", "b", "-AllowUnknown",
                       "-AllowManagedDelete")
        blocked = self.ps("Apply", "-ProfileId", "p1", "-StateId", "b", "-AllowUnknown",
                          "-AllowManagedDelete",
                          "-ExpectedPlanDigest", diff["data"]["planDigest"])
        self.assertEqual(self.code(blocked), "plan-delete-unrecognized")
        self.assertEqual(tracked.read_bytes(), unknown)

        # Both flags together cannot clear that second gate: -AllowUnknown only
        # covers content that *exists*, and a delete has none.  Recovering from
        # here needs a capture of the current content first.
        diff = self.ps("Diff", "-ProfileId", "p1", "-StateId", "b",
                       "-AllowUnknown", "-AllowManagedDelete")
        applied = self.ps("Apply", "-ProfileId", "p1", "-StateId", "b",
                          "-AllowUnknown", "-AllowManagedDelete",
                          "-ExpectedPlanDigest", diff["data"]["planDigest"])
        self.assertEqual(self.code(applied), "plan-delete-unrecognized")
        self.assertEqual(tracked.read_bytes(), unknown)

        # Saving the target's current content under a state makes it known, and
        # then the same delete proceeds with only -AllowManagedDelete.
        self.ps("Capture", "-ProfileId", "p1", "-StateId", "a",
                "-SourceRoot", str(self.target))
        diff = self.ps("Diff", "-ProfileId", "p1", "-StateId", "b", "-AllowManagedDelete")
        applied = self.ps("Apply", "-ProfileId", "p1", "-StateId", "b",
                          "-AllowManagedDelete",
                          "-ExpectedPlanDigest", diff["data"]["planDigest"])
        self.assertTrue(applied["ok"], applied)
        self.assertFalse(tracked.exists())

    def test_rollback_restores_previous_content(self):
        applied = self.ps("Apply", "-ProfileId", "p1", "-StateId", "a", "-AllowUnknown",
                          "-ExpectedPlanDigest",
                          self.digest_for("a", "-AllowUnknown"))
        self.assertTrue(applied["ok"], applied)
        target = self.target / "sub" / "one.txt"
        self.assertEqual(target.read_text(encoding="utf-8"), "A-DAILY")

        applied = self.ps("Apply", "-ProfileId", "p1", "-StateId", "b", "-AllowUnknown",
                          "-ExpectedPlanDigest",
                          self.digest_for("b", "-AllowUnknown"))
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(target.read_text(encoding="utf-8"), "B-AUTO")

        rolled = self.ps("Rollback", "-TransactionId", applied["transaction"]["id"])
        self.assertTrue(rolled["ok"], rolled)
        self.assertEqual(rolled["data"]["status"], "rolled-back")
        self.assertEqual(rolled["data"]["restoredCount"], 1)
        self.assertEqual(target.read_text(encoding="utf-8"), "A-DAILY")

    def test_rollback_lookup_codes(self):
        self.assertEqual(
            self.code(self.ps("Rollback", "-TransactionId", "../escape")),
            "transaction-id-invalid")
        self.assertEqual(
            self.code(self.ps("Rollback", "-TransactionId", "nope")),
            "transaction-not-found")

    def test_config_and_lookup_codes(self):
        self.assertEqual(
            self.code(self.ps("Status", config=self.tmp / "absent.json")),
            "config-not-found")
        self.assertEqual(
            self.code(self.ps("Status", "-ProfileId", "ghost")),
            "profile-not-found")
        self.assertEqual(
            self.code(self.ps("Diff", "-ProfileId", "p1", "-StateId", "ghost")),
            "state-not-found")
        self.assertEqual(
            self.code(self.ps("Diff", "-ProfileId", "p1")),
            "state-id-required")

    def test_plan_gate_codes(self):
        self.assertEqual(
            self.code(self.ps("Apply", "-ProfileId", "p1", "-StateId", "b")),
            "digest-required")
        self.assertEqual(
            self.code(self.ps("Apply", "-ProfileId", "p1", "-StateId", "b",
                              "-ExpectedPlanDigest", "0" * 64)),
            "plan-stale-digest")
        self.assertEqual(
            self.code(self.ps("Apply", "-ProfileId", "p1", "-StateId", "b",
                              "-ExpectedPlanDigest", self.digest_for("b"))),
            "plan-target-conflict")

    def test_schema_and_unsafe_path_codes(self):
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps({"schemaVersion": 1, "profiles": []}), encoding=BOM)
        self.assertEqual(self.code(self.ps("Status", config=bad)), "config-schema")

        unsafe = self.write_config(
            path=self.tmp / "unsafe.json",
            mutate=lambda p: p["profiles"][0].update(managed=[{"path": "../escape.txt"}]))
        self.assertEqual(
            self.code(self.ps("Status", config=unsafe)), "path-unsafe-relative")

    def test_state_not_captured_code(self):
        fresh = self.write_config(
            path=self.tmp / "fresh.json",
            mutate=lambda p: p["profiles"][0]["states"].append(
                {"id": "c", "name": "C", "snapshotRoot": None}))
        self.assertEqual(
            self.code(self.ps("Diff", "-ProfileId", "p1", "-StateId", "c",
                              config=fresh)),
            "state-not-captured")


if __name__ == "__main__":
    unittest.main()
