import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from profile_store import (
    ConfigConflictError,
    ProfileStore,
    ProfileStoreError,
    config_hash,
    import_legacy,
    new_id,
    validate_config,
)


class ProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "store"
        self.target = self.root / "target"
        self.target.mkdir()
        self.config_path = self.workspace / "profiles.json"
        self.store = ProfileStore(self.config_path)

    def tearDown(self):
        self.temp.cleanup()

    def data(self, states=None):
        return {
            "schemaVersion": 2,
            "defaultProfileId": "main",
            "profiles": [{
                "id": "main",
                "name": "Main",
                "targetRoot": str(self.target),
                "managed": [{"path": "src/main.ts"}],
                "states": states if states is not None else [{"id": "dev", "name": "Dev", "snapshotRoot": None}],
                "guards": [],
                "cacheDirectories": [],
                "unknown": {"keep": True},
            }],
            "unknownTopLevel": [1, 2],
        }

    def test_roundtrip_and_unknown_fields_survive(self):
        value = self.data()
        self.store.save(value)
        loaded = self.store.load()
        self.assertEqual(loaded["unknownTopLevel"], [1, 2])
        self.assertEqual(loaded["profiles"][0]["unknown"], {"keep": True})
        loaded["profiles"][0]["name"] = "Renamed"
        loaded["profiles"][0]["states"] = []
        self.store.save(loaded)
        self.assertEqual(self.store.load()["profiles"][0]["name"], "Renamed")
        self.assertEqual(self.store.load()["profiles"][0]["states"], [])
        self.assertTrue(self.config_path.with_name("profiles.json.bak").exists())

    def test_zero_states_are_allowed(self):
        validate_config(self.data(states=[]), self.workspace)

    def test_target_roots_overlap(self):
        other = self.root / "target" / "nested"
        data = self.data()
        data["profiles"].append({
            "id": "other", "name": "Other", "targetRoot": str(other),
            "managed": [], "states": [], "guards": [], "cacheDirectories": [],
        })
        with self.assertRaises(ProfileStoreError):
            validate_config(data, self.workspace)

    def test_traversal_duplicate_paths_and_unsafe_ids(self):
        for path in ("../x.ts", "a/../x.ts", "a:ads.ts", "C:\\x.ts"):
            data = self.data()
            data["profiles"][0]["managed"] = [{"path": path}]
            with self.subTest(path=path), self.assertRaises(ProfileStoreError):
                validate_config(data, self.workspace)
        data = self.data()
        data["profiles"][0]["managed"] = [{"path": "A.ts"}, {"path": "a.ts"}]
        with self.assertRaises(ProfileStoreError):
            validate_config(data, self.workspace)
        data = self.data()
        data["profiles"][0]["id"] = "bad.id"
        with self.assertRaises(ProfileStoreError):
            validate_config(data, self.workspace)

    def test_external_conflict(self):
        self.store.save(self.data())
        loaded = self.store.load()
        self.config_path.write_text(json.dumps(self.data()), encoding="utf-8")
        loaded["profiles"][0]["name"] = "local edit"
        with self.assertRaises(ConfigConflictError):
            self.store.save(loaded)

    def test_atomic_save_failure_does_not_destroy_original(self):
        self.store.save(self.data())
        original = self.config_path.read_bytes()
        changed = self.data()
        changed["profiles"][0]["name"] = "changed"
        with mock.patch("profile_store.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                self.store.save(changed)
        self.assertEqual(self.config_path.read_bytes(), original)
        self.assertFalse(any(item.suffix == ".tmp" for item in self.workspace.iterdir()))

    def test_new_id_and_hash(self):
        identifier = new_id()
        self.assertRegex(identifier, r"^[a-zA-Z0-9_-]+$")
        self.assertIsNone(config_hash(self.config_path))

    def test_import_legacy_is_read_only_and_returns_candidates(self):
        legacy = self.root / "config.json"
        legacy.write_text(json.dumps({
            "schemaVersion": 1,
            "targetRoot": str(self.target),
            "cacheDirectories": ["cache"],
            "modes": {"daily": {}, "automation": {}},
            "entries": [{
                "target": "src/a.ts",
                "sources": {
                    "daily": "snapshots/daily/src/a.ts",
                    "automation": "snapshots/automation/src/a.ts",
                },
            }],
        }), encoding="utf-8")
        destination = self.root / "new.json"
        result = import_legacy(legacy, destination)
        self.assertEqual(result["schemaVersion"], 2)
        self.assertEqual(len(result["migration_candidates"]), 2)
        self.assertFalse(destination.exists())
        self.assertEqual(result["profiles"][0]["states"][0]["snapshotRoot"], str((self.root / "snapshots/daily/src").resolve()))


if __name__ == "__main__":
    unittest.main()
