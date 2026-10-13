from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

MODULE_PATH = Path(__file__).parents[1] / "scripts" / "canary_promotion.py"
spec = importlib.util.spec_from_file_location("canary_promotion_helper", MODULE_PATH)
promotion = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(promotion)


def lock(version: str = "0.11.5") -> dict:
    return {
        "release": version,
        "source": {
            "repository": "ai-development-harness/ai-development-harness-template",
            "ref": "v" + version,
            "commit": "a" * 40,
        },
    }


def release(tag: str, *, draft: bool = False, prerelease: bool = False, published: str = "2026-10-13T21:00:00Z") -> dict:
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease, "published_at": published}


SOURCE_REPO = "ai-development-harness/ai-development-harness-template"


class SelectPublishedReleaseTest(unittest.TestCase):
    def test_selects_first_missed_stable_in_version_order(self) -> None:
        data = [
            [release("v0.11.8"), release("v0.11.6"), release("v0.11.7", prerelease=True)],
            [release("v0.11.5"), release("v0.11.4"), release("v0.11.99", draft=True)],
        ]
        self.assertEqual(
            promotion.select_release(lock(), data, SOURCE_REPO),
            {"status": "PROMOTE", "current": "v0.11.5", "target": "v0.11.6"},
        )

    def test_no_update_when_canary_is_current(self) -> None:
        self.assertEqual(
            promotion.select_release(lock("0.11.6"), [release("v0.11.6"), release("v0.11.5")], SOURCE_REPO),
            {"status": "NO_UPDATE", "current": "v0.11.6"},
        )

    def test_ignores_unpublished_and_prerelease(self) -> None:
        data = [
            release("v0.11.8", published=None),
            release("v0.11.7", prerelease=True),
            release("v0.11.6", draft=True),
        ]
        self.assertEqual(promotion.select_release(lock(), data, SOURCE_REPO)["status"], "NO_UPDATE")

    def test_accepts_single_page_response(self) -> None:
        selected = promotion.select_release(lock(), [release("v0.11.6")], SOURCE_REPO)
        self.assertEqual(selected["target"], "v0.11.6")

    def test_duplicate_published_tag_fails_closed(self) -> None:
        with self.assertRaisesRegex(promotion.PromotionError, "duplicate published"):
            promotion.select_release(lock(), [[release("v0.11.6")], [release("v0.11.6")]], SOURCE_REPO)

    def test_missing_source_pin_and_mismatch_fail_closed(self) -> None:
        invalid = lock()
        invalid["source"]["commit"] = "bad"
        with self.assertRaisesRegex(promotion.PromotionError, "exact source commit"):
            promotion.select_release(invalid, [release("v0.11.6")], SOURCE_REPO)
        invalid = lock()
        invalid["source"]["ref"] = "v0.11.4"
        with self.assertRaisesRegex(promotion.PromotionError, "differ"):
            promotion.select_release(invalid, [release("v0.11.6")], SOURCE_REPO)
        with self.assertRaisesRegex(promotion.PromotionError, "unexpected Harness source"):
            promotion.select_release(lock(), [], "another/harness")

    def test_skips_non_semver_tag(self) -> None:
        result = promotion.select_release(lock(), [release("latest"), release("v0.11.6")], SOURCE_REPO)
        self.assertEqual(result["target"], "v0.11.6")


class UpdateProtocolTest(unittest.TestCase):
    def test_reload_and_idempotent_no_update(self) -> None:
        events = iter([
            {"status": "UPDATER_RELOAD_REQUIRED", "current": "v0.11.6"},
            {"status": "NO_UPDATE", "current": "v0.11.6", "repositoryMutated": False},
        ])
        with patch.object(promotion, "command_json", side_effect=lambda *_args, **_kw: next(events)):
            self.assertEqual(
                promotion.update(Path("/ignored"), "v0.11.6"),
                ["UPDATER_RELOAD_REQUIRED", "NO_UPDATE"],
            )

    def test_updated_then_noop(self) -> None:
        events = iter([{"status": "UPDATED"}, {"status": "NO_UPDATE"}])
        with patch.object(promotion, "command_json", side_effect=lambda *_args, **_kw: next(events)):
            self.assertEqual(promotion.update(Path("/ignored"), "v0.11.6"), ["UPDATED", "NO_UPDATE"])

    def test_initial_noop_cannot_fake_upgrade(self) -> None:
        with patch.object(promotion, "command_json", return_value={"status": "NO_UPDATE"}):
            with self.assertRaisesRegex(promotion.PromotionError, "did not apply"):
                promotion.update(Path("/ignored"), "v0.11.6")

    def test_noop_mutation_is_not_pass(self) -> None:
        events = iter([{"status": "UPDATED"}, {"status": "NO_UPDATE", "repositoryMutated": True}])
        with patch.object(promotion, "command_json", side_effect=lambda *_args, **_kw: next(events)):
            with self.assertRaisesRegex(promotion.PromotionError, "mutated"):
                promotion.update(Path("/ignored"), "v0.11.6")

    def test_unknown_update_status_is_blocked(self) -> None:
        with patch.object(promotion, "command_json", return_value={"status": "BLOCKED"}):
            with self.assertRaisesRegex(promotion.PromotionError, "did not accept"):
                promotion.update(Path("/ignored"), "v0.11.6")


class SafeGitPreservationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.write(".harness/harness-update.toml", (
            "[state]\nlock_file = '.harness/harness.lock.json'\n"
            "[ownership]\nharness_owned = ['.harness/tools/**']\n"
            "shared = ['.github/**']\nmarker_merge = []\n"
        ))
        self.write(".harness/tools/check.py", "print('v1')\n")
        self.write("docs/requirements/REQ-001.md", "unchanged requirements\n")
        self.write("docs/adr/ADR-001.md", "accepted decision\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "baseline")
        self.baseline = self.git("rev-parse", "HEAD")
        self.patterns, self.lock_path = promotion.ownership(self.project, self.baseline)
        self.snapshot = promotion.tracked_project_snapshot(
            self.project, self.baseline, self.patterns, self.lock_path
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, relative: str, content: str) -> None:
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.project, capture_output=True, text=True, check=True
        ).stdout.strip()

    def test_update_allows_only_managed_changes_and_new_immutable_report(self) -> None:
        self.write(".harness/tools/check.py", "print('v2')\n")
        self.write(".github/workflows/harness-integrity.yml", "name: Test\n")
        self.write("planning/harness-updates/UPDATE-20261014T010000Z.md", "# Update\n")
        self.assertEqual(
            promotion.verify_preservation(self.project, self.snapshot, self.patterns, self.lock_path),
            len(self.snapshot),
        )

    def test_project_owned_mutation_fails(self) -> None:
        self.write("docs/adr/ADR-001.md", "tampered accepted decision\n")
        with self.assertRaisesRegex(promotion.PromotionError, "content changed"):
            promotion.verify_preservation(self.project, self.snapshot, self.patterns, self.lock_path)

    def test_project_owned_deletion_fails(self) -> None:
        (self.project / "docs/requirements/REQ-001.md").unlink()
        with self.assertRaisesRegex(promotion.PromotionError, "disappeared"):
            promotion.verify_preservation(self.project, self.snapshot, self.patterns, self.lock_path)

    def test_arbitrary_new_project_file_fails(self) -> None:
        self.write("docs/sneaky.md", "# extra\n")
        with self.assertRaisesRegex(promotion.PromotionError, "unexpected project-owned"):
            promotion.verify_preservation(self.project, self.snapshot, self.patterns, self.lock_path)

    def test_canonical_digest_detects_changes(self) -> None:
        first = promotion.canonical_digest(self.project)
        self.write(".harness/tools/check.py", "print('new')\n")
        self.assertNotEqual(first, promotion.canonical_digest(self.project))

    def test_direct_graph_edge_required(self) -> None:
        self.write(".harness/harness-update-graph.json", json.dumps({
            "transitions": [{"from": "v0.11.5", "to": "v0.11.6"}]
        }))
        promotion.validate_graph(self.project, "v0.11.5", "v0.11.6")
        with self.assertRaisesRegex(promotion.PromotionError, "no unique direct"):
            promotion.validate_graph(self.project, "v0.11.4", "v0.11.6")


if __name__ == "__main__":
    unittest.main()
