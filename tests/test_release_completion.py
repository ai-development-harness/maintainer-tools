from __future__ import annotations

import base64
import copy
import importlib.util
import json
import io
from pathlib import Path
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "release_completion", Path(__file__).parents[1] / "scripts/release_completion.py")
completion = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(completion)


class ReleaseCompletionTest(unittest.TestCase):
    def setUp(self):
        self.tag = "v0.13.1"
        self.release = {"tag_name": self.tag, "draft": False, "prerelease": False,
                        "published_at": "2026-10-16T08:00:00Z"}
        self.releases = [self.release]
        self.baseline = "b" * 40
        self.lock = {"release": "0.13.1", "source": {"repository": completion.SOURCE,
                     "ref": self.tag, "commit": "a" * 40}}
        self.manifest = 'project:\n  initialized: true\nharness:\n  release: "0.13.1"\n'
        self.pr = {"state": "closed", "merged_at": "2026-10-16T08:30:00Z",
                   "head": {"ref": "promotion/" + self.tag, "repo": {"full_name": completion.CANARY}},
                   "base": {"ref": "main", "repo": {"full_name": completion.CANARY}},
                   "merge_commit_sha": "c" * 40, "html_url": "https://github.com/example/pull/14"}
        self.prs = [self.pr]
        self.comparison = "ahead"
        self.branch_reads = 0
        self.release_reads = 0
        self.move_main = False
        self.new_release = False

    def api(self, endpoint, *, pages=False):
        if "/releases?" in endpoint:
            self.release_reads += 1
            if self.new_release and self.release_reads > 1:
                return [dict(self.release, tag_name="v0.13.2")]
            return self.releases
        if "/releases/tags/" in endpoint:
            return self.release
        if "/commits/" in endpoint:
            return {"sha": "a" * 40}
        if "/branches/main" in endpoint:
            self.branch_reads += 1
            return {"commit": {"sha": "d" * 40 if self.move_main and self.branch_reads > 1 else self.baseline}}
        if "/contents/" in endpoint:
            self.assertTrue(endpoint.endswith("?ref=" + self.baseline))
            data = json.dumps(self.lock) if "lock.json" in endpoint else self.manifest
            return {"encoding": "base64", "content": base64.b64encode(data.encode()).decode()}
        if "/pulls?" in endpoint:
            self.assertTrue(pages)
            return self.prs
        if "/compare/" in endpoint:
            return {"status": self.comparison}
        self.fail(endpoint)

    def verify(self, *, latest=False):
        with patch.object(completion, "api", side_effect=self.api):
            return completion.verify(None if latest else self.tag)

    def test_complete_with_later_project_commit_on_main(self):
        result = self.verify()
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["sourceCommit"], "a" * 40)
        self.assertEqual(result["canaryCommit"], self.baseline)

    def test_same_merge_and_main_accepted(self):
        self.comparison = "identical"
        self.assertEqual(self.verify()["status"], "COMPLETE")

    def test_old_baseline_and_wrong_source_pin_block(self):
        for key, value in (("ref", "v0.13.0"), ("commit", "d" * 40), ("repository", "other/repo")):
            with self.subTest(key=key):
                original = copy.deepcopy(self.lock)
                self.lock["source"][key] = value
                with self.assertRaisesRegex(completion.CompletionError, "не закреплён"):
                    self.verify()
                self.lock = original
        self.lock["release"] = "0.13.0"
        with self.assertRaises(completion.CompletionError):
            self.verify()

    def test_manifest_disagreement_blocks_even_with_correct_lock(self):
        self.manifest = 'project:\n  release: "0.13.1"\nharness:\n  release: "0.13.0"\n'
        with self.assertRaisesRegex(completion.CompletionError, "Manifest"):
            self.verify()

    def test_missing_open_closed_unmerged_and_ambiguous_pr_block(self):
        for prs in ([], [dict(self.pr, merged_at=None)], [dict(self.pr, state="open")],
                    [self.pr, self.pr]):
            with self.subTest(prs=prs):
                self.prs = prs
                with self.assertRaisesRegex(completion.CompletionError, "единственного"):
                    self.verify()

    def test_wrong_pr_branch_repository_and_missing_merge_sha_block(self):
        for replacement in (
            {"head": {"ref": "promotion/v0.13.0", "repo": {"full_name": completion.CANARY}}},
            {"head": {"ref": "promotion/" + self.tag, "repo": {"full_name": "other/fork"}}},
            {"base": {"ref": "other", "repo": {"full_name": completion.CANARY}}},
            {"merge_commit_sha": None},
        ):
            with self.subTest(replacement=replacement):
                self.prs = [dict(self.pr, **replacement)]
                with self.assertRaises(completion.CompletionError):
                    self.verify()

    def test_merge_outside_main_blocks(self):
        for status in ("behind", "diverged", None):
            self.comparison = status
            with self.assertRaisesRegex(completion.CompletionError, "отсутствует"):
                self.verify()

    def test_draft_prerelease_unpublished_block(self):
        for key, value in (("draft", True), ("prerelease", True), ("published_at", None)):
            with self.subTest(key=key):
                self.release[key] = value
                with self.assertRaises(completion.CompletionError):
                    self.verify()
                self.setUp()

    def test_latest_stable_uses_semver_not_api_order(self):
        self.releases = [dict(self.release, tag_name="v0.9.9"), self.release,
                         dict(self.release, tag_name="v0.14.0", draft=True),
                         dict(self.release, tag_name="v0.15.0", prerelease=True)]
        self.assertEqual(self.verify(latest=True)["release"], self.tag)

    def test_no_stable_blocks(self):
        self.releases = []
        with self.assertRaises(completion.CompletionError):
            self.verify(latest=True)

    def test_main_movement_and_new_release_require_retry(self):
        self.move_main = True
        with self.assertRaisesRegex(completion.CompletionError, "изменился"):
            self.verify()
        self.move_main = False
        self.new_release = True
        with self.assertRaisesRegex(completion.CompletionError, "новый stable"):
            self.verify(latest=True)

    def test_api_failure_cannot_be_complete(self):
        with patch.object(completion, "api", side_effect=completion.CompletionError("API")):
            with self.assertRaises(completion.CompletionError):
                completion.verify(self.tag)

    def test_cli_failure_returns_nonzero_and_blocked(self):
        for error in (completion.CompletionError("Promotion не принят"),
                      ValueError("внутренний ответ API")):
            output = io.StringIO()
            with patch.object(completion, "verify", side_effect=error), \
                    patch("sys.argv", ["release_completion.py"]), patch("sys.stdout", output):
                self.assertEqual(completion.main(), 1)
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "BLOCKED")
            self.assertNotIn("внутренний ответ API", result["reason"])


if __name__ == "__main__":
    unittest.main()
