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
        self.config_path = self.root / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "targetRepository": "example/harness",
                    "defaultBranch": "main",
                    "releaseBranchPrefix": "release/",
                    "layouts": [
                        {
                            "name": "harness",
                            "manifestPath": ".harness/manifest.yaml",
                            "lockPath": ".harness/harness.lock.json",
                            "updateGraphPath": ".harness/harness-update-graph.json",
                            "validatorPath": ".harness/tools/validate.py",
                        },
                        {
                            "name": "legacy-project",
                            "manifestPath": ".project/manifest.yaml",
                            "lockPath": ".project/harness.lock.json",
                            "updateGraphPath": ".project/harness-update-graph.json",
                            "validatorPath": "tools/harness/validate.py",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.write_layout(
            ".project",
            validator="tools/harness/validate.py",
            release_version="0.2.4",
        )
        self.config = release.load_config(self.config_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_layout(
        self,
        directory: str,
        *,
        validator: str,
        release_version: str = "0.2.4",
    ) -> None:
        base = self.root / directory
        base.mkdir(parents=True, exist_ok=True)
        (base / "manifest.yaml").write_text(
            f'harness:\n  version: "1"\n  release: "{release_version}"\n',
            encoding="utf-8",
        )
        (base / "harness.lock.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "harnessVersion": "1",
                    "release": release_version,
                    "source": {
                        "repository": "example/harness",
                        "ref": f"v{release_version}",
                    },
                    "updatedAt": None,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (base / "harness-update-graph.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "latest": f"v{release_version}",
                    "transitions": [
                        {
                            "from": "v0.2.3",
                            "to": f"v{release_version}",
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
        validator_path = self.root / validator
        validator_path.parent.mkdir(parents=True, exist_ok=True)
        validator_path.write_text("# validator fixture\n", encoding="utf-8")

    def test_resolve_layout_accepts_legacy_layout(self) -> None:
        layout = release.resolve_layout(self.root, self.config)
        self.assertEqual(layout["name"], "legacy-project")

    def test_resolve_layout_accepts_harness_layout(self) -> None:
        for path in (
            self.root / ".project",
            self.root / "tools" / "harness" / "validate.py",
        ):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                for child in sorted(path.rglob("*"), reverse=True):
                    if child.is_file():
                        child.unlink()
                    elif child.is_dir():
                        child.rmdir()
                path.rmdir()

        self.write_layout(
            ".harness",
            validator=".harness/tools/validate.py",
            release_version="0.2.4",
        )

        layout = release.resolve_layout(self.root, self.config)
        self.assertEqual(layout["name"], "harness")

    def test_resolve_layout_rejects_ambiguous_complete_layouts(self) -> None:
        self.write_layout(
            ".harness",
            validator=".harness/tools/validate.py",
            release_version="0.2.4",
        )

        with self.assertRaisesRegex(release.ReleaseError, "multiple complete layouts"):
            release.resolve_layout(self.root, self.config)

    def test_resolve_layout_rejects_missing_layout(self) -> None:
        (self.root / ".project" / "manifest.yaml").unlink()

        with self.assertRaisesRegex(release.ReleaseError, "no complete release layout found"):
            release.resolve_layout(self.root, self.config)

    def test_state_accepts_consistent_metadata(self) -> None:
        current, _, _ = release.state(self.root, self.config)
        self.assertEqual(current, "v0.2.4")

    def test_prepare_updates_manifest_lock_and_graph(self) -> None:
        args = type(
            "Args",
            (),
            {
                "config": self.config_path,
                "repo_dir": self.root,
                "version": "v0.2.5",
                "reload_required": False,
            },
        )()
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

    def test_prepare_can_require_reload(self) -> None:
        args = type(
            "Args",
            (),
            {
                "config": self.config_path,
                "repo_dir": self.root,
                "version": "v0.2.5",
                "reload_required": True,
            },
        )()
        release.command_prepare(args)

        _, _, graph = release.state(self.root, self.config)
        self.assertEqual(
            graph["transitions"][-1],
            {
                "from": "v0.2.4",
                "to": "v0.2.5",
                "kind": "standard",
                "reloadRequired": True,
            },
        )

    def test_prepare_rejects_inconsistent_current_metadata(self) -> None:
        lock_path = self.root / ".project" / "harness.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["release"] = "0.2.3"
        lock_path.write_text(json.dumps(lock), encoding="utf-8")

        args = type(
            "Args",
            (),
            {
                "config": self.config_path,
                "repo_dir": self.root,
                "version": "v0.2.5",
                "reload_required": False,
            },
        )()
        with self.assertRaises(release.ReleaseError):
            release.command_prepare(args)

    def test_prepare_rejects_non_increasing_version(self) -> None:
        args = type(
            "Args",
            (),
            {
                "config": self.config_path,
                "repo_dir": self.root,
                "version": "v0.2.4",
                "reload_required": False,
            },
        )()
        with self.assertRaises(release.ReleaseError):
            release.command_prepare(args)

    def test_verify_requires_requested_version(self) -> None:
        args = type(
            "Args",
            (),
            {
                "config": self.config_path,
                "repo_dir": self.root,
                "version": "v0.2.5",
            },
        )()
        with self.assertRaises(release.ReleaseError):
            release.command_verify(args)


if __name__ == "__main__":
    unittest.main()
