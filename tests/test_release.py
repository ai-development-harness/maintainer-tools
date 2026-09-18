from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

MODULE_PATH = Path(__file__).parents[1] / "scripts" / "release.py"
spec = importlib.util.spec_from_file_location("release_helper", MODULE_PATH)
release = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(release)


class ReleaseHelperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".project").mkdir()
        self.config_path = self.root / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "targetRepository": "example/harness",
                    "defaultBranch": "main",
                    "releaseBranchPrefix": "release/",
                    "manifestPath": ".project/manifest.yaml",
                    "lockPath": ".project/harness.lock.json",
                    "updateGraphPath": ".project/harness-update-graph.json",
                }
            ),
            encoding="utf-8",
        )
        (self.root / ".project" / "manifest.yaml").write_text(
            'harness:\n  version: "1"\n  release: "0.2.4"\n', encoding="utf-8"
        )
        (self.root / ".project" / "harness.lock.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "harnessVersion": "1",
                    "release": "0.2.4",
                    "source": {"repository": "example/harness", "ref": "v0.2.4"},
                    "updatedAt": None,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (self.root / ".project" / "harness-update-graph.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "latest": "v0.2.4",
                    "transitions": [
                        {
                            "from": "v0.2.3",
                            "to": "v0.2.4",
                            "kind": "standard",
                            "reloadRequired": False,
                        }
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.config = release.load_config(self.config_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_state_accepts_consistent_metadata(self) -> None:
        current, _, _ = release.state(self.root, self.config)
        self.assertEqual(current, "v0.2.4")

    def test_prepare_updates_manifest_lock_and_graph(self) -> None:
        args = type("Args", (), {"config": self.config_path, "repo_dir": self.root, "version": "v0.2.5"})()
        release.command_prepare(args)

        current, lock, graph = release.state(self.root, self.config)
        self.assertEqual(current, "v0.2.5")
        self.assertEqual(lock["release"], "0.2.5")
        self.assertEqual(lock["source"]["ref"], "v0.2.5")
        self.assertEqual(graph["latest"], "v0.2.5")
        self.assertEqual(
            graph["transitions"][-1],
            {"from": "v0.2.4", "to": "v0.2.5", "kind": "standard", "reloadRequired": False},
        )

    def test_prepare_rejects_inconsistent_current_metadata(self) -> None:
        lock_path = self.root / ".project" / "harness.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["release"] = "0.2.3"
        lock_path.write_text(json.dumps(lock), encoding="utf-8")

        args = type("Args", (), {"config": self.config_path, "repo_dir": self.root, "version": "v0.2.5"})()
        with self.assertRaises(release.ReleaseError):
            release.command_prepare(args)

    def test_prepare_rejects_non_increasing_version(self) -> None:
        args = type("Args", (), {"config": self.config_path, "repo_dir": self.root, "version": "v0.2.4"})()
        with self.assertRaises(release.ReleaseError):
            release.command_prepare(args)

    def test_verify_requires_requested_version(self) -> None:
        args = type("Args", (), {"config": self.config_path, "repo_dir": self.root, "version": "v0.2.5"})()
        with self.assertRaises(release.ReleaseError):
            release.command_verify(args)


if __name__ == "__main__":
    unittest.main()
