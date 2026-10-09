import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "folder-switcher.ps1"


def run_backend(config, action, **kwargs):
    command = [
        "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
        "-Config", str(config),
    ]
    for key, value in kwargs.items():
        if isinstance(value, bool):
            if value:
                command.append("-" + key)
        elif value is not None:
            command.extend(["-" + key, str(value)])
    command.extend(["-Action", action])
    proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    try:
        payload = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        payload = None
    return proc, payload


class FolderBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="folder-backend-"))
        self.target = self.tmp / "target"
        self.target.mkdir()
        self.source_a = self.tmp / "source-a"
        self.source_b = self.tmp / "source-b"
        self.source_a.mkdir()
        self.source_b.mkdir()
        # All backend state and Cocos-like fixture files stay under this temp tree.
        self.state_root = self.tmp / "state"
        self.config = self.tmp / "folder-config.json"
        self._write_config()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_config(self, managed=None):
        managed = managed if managed is not None else ["a.bin", "子目录/文件.txt"]
        cfg = {
            "schemaVersion": 2,
            "defaultProfileId": "p",
            "profiles": [{
                "id": "p", "name": "test", "targetRoot": str(self.target),
                "managed": [{"path": p} for p in managed],
                "guards": [], "cacheDirectories": [],
                # Empty snapshots are deliberately null, never an empty string.
                "states": [{"id": "A", "name": "A", "snapshotRoot": None},
                           {"id": "B", "name": "B", "snapshotRoot": None}],
            }],
        }
        self.config.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def _run(self, action, **kwargs):
        kwargs.setdefault("StateRoot", self.state_root)
        return run_backend(self.config, action, **kwargs)

    def _seed(self, root, a, b, include_b=True):
        if include_b:
            (root / "子目录").mkdir(exist_ok=True)
        (root / "a.bin").write_bytes(a)
        if include_b:
            (root / "子目录" / "文件.txt").write_text(b, encoding="utf-8")

    def _capture(self, state, source, **kwargs):
        proc, data = self._run("Capture", StateId=state, SourceRoot=source, **kwargs)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(data["ok"], data)
        return data

    def _diff(self, state, **kwargs):
        proc, data = self._run("Diff", StateId=state, **kwargs)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(data["ok"], data)
        return data

    def _apply(self, state, plan, **kwargs):
        proc, data = self._run(
            "Apply", StateId=state, ExpectedPlanDigest=plan["data"]["planDigest"], **kwargs
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(data["ok"], data)
        return data

    def test_capture_versions_and_unicode_binary(self):
        self._seed(self.source_a, b"A\x00\xff", "中文A")
        a = self._capture("A", self.source_a)
        self._seed(self.source_b, b"B\x01\xfe", "中文B")
        b = self._capture("B", self.source_b)
        self.assertNotEqual(a["data"]["snapshotRoot"], b["data"]["snapshotRoot"])
        self.assertEqual(a["data"]["missingCount"], 0)
        proc, status = self._run("Status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(status["ok"])

    def test_known_state_a_to_b_transition(self):
        self._seed(self.source_a, b"A", "A")
        self._capture("A", self.source_a)
        self._seed(self.source_b, b"B", "B")
        self._capture("B", self.source_b)
        plan_a = self._diff("A")
        self._apply("A", plan_a)
        plan_b = self._diff("B")
        self.assertEqual(plan_b["data"]["counts"]["modify"], 2)
        self.assertFalse(plan_b["data"]["blockers"])
        self._apply("B", plan_b)
        self.assertEqual((self.target / "a.bin").read_bytes(), b"B")
        self.assertEqual((self.target / "子目录" / "文件.txt").read_text(encoding="utf-8"), "B")

    def test_manifest_coverage_blocks_unsafe_remove(self):
        # A manifest omitting a configured managed path is rejected before a remove plan.
        self._seed(self.source_a, b"A", "A")
        self._capture("A", self.source_a)
        self._seed(self.source_b, b"B", "unused", include_b=True)
        b_capture = self._capture("B", self.source_b)
        # Keep the manifest valid while making B omit one managed file.
        b_manifest = Path(b_capture["data"]["snapshotRoot"]) / "manifest.json"
        manifest = json.loads(b_manifest.read_text(encoding="utf-8-sig"))
        manifest["files"] = [item for item in manifest["files"] if item["path"] == "a.bin"]
        b_manifest.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        plan_a = self._diff("A")
        self._apply("A", plan_a)
        proc, payload = self._run("Diff", StateId="B", AllowManagedDelete=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(payload["ok"])
        self.assertIn("coverage", payload["error"]["message"].lower())

    def test_dry_run_and_apply_requires_digest(self):
        self._seed(self.source_a, b"A", "A")
        self._capture("A", self.source_a)
        proc, diff = self._run("Diff", StateId="A")
        self.assertTrue(diff["ok"])
        before = (self.target / "a.bin").exists()
        proc, preview = self._run("Apply", StateId="A", DryRun=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(preview["data"]["dryRun"])
        self.assertEqual(before, (self.target / "a.bin").exists())
        proc, fail = self._run("Apply", StateId="A")
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(fail["ok"])
        proc, ok = self._run("Apply", StateId="A", ExpectedPlanDigest=diff["data"]["planDigest"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(ok["ok"], ok)

    def test_stale_digest_is_rejected_after_target_changes(self):
        self._seed(self.source_a, b"A", "A")
        self._capture("A", self.source_a)
        self._apply("A", self._diff("A"))
        self._seed(self.source_b, b"B", "B")
        self._capture("B", self.source_b)
        plan = self._diff("B")
        (self.target / "a.bin").write_bytes(b"stale")
        proc, payload = self._run("Apply", StateId="B", ExpectedPlanDigest=plan["data"]["planDigest"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(payload["ok"])
        self.assertIn("stale", payload["error"]["message"].lower())

    def test_unmanaged_file_protected_and_unknown_guard(self):
        (self.target / "unmanaged.txt").write_text("keep", encoding="utf-8")
        self._seed(self.source_a, b"A", "A")
        self._capture("A", self.source_a)
        (self.target / "a.bin").write_bytes(b"outside")
        proc, diff = self._run("Diff", StateId="A")
        self.assertIn("target-conflict:a.bin", diff["data"]["blockers"])
        proc, preview = self._run("Apply", StateId="A", DryRun=True, AllowUnknown=True)
        self.assertTrue(preview["ok"])
        proc, applied = self._run("Apply", StateId="A", ExpectedPlanDigest=preview["data"]["planDigest"], AllowUnknown=True)
        self.assertTrue(applied["ok"], applied)
        self.assertEqual((self.target / "unmanaged.txt").read_text(encoding="utf-8"), "keep")

    def test_manifest_coverage_and_managed_mismatch_are_reported(self):
        self._write_config(["a.bin"])
        self._seed(self.source_a, b"A", "unused")
        self._capture("A", self.source_a)
        self._write_config(["a.bin", "new.txt"])
        proc, payload = self._run("Status")
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["data"]["snapshotErrors"])
        self.assertTrue(payload["data"]["snapshotErrors"][0]["message"])

    def test_path_traversal_rejected(self):
        self._write_config(["../escape.txt"])
        proc, payload = self._run("Status")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(payload["error"]["code"], "operation-failed")


if __name__ == "__main__":
    unittest.main()
