from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "resolve_release_pr.py"
TITLE = "chore: подготовить release v0.11.3"
MERGE_SHA = "e76f471f6557e3b2758778f0c8f14a6b32ee273c"


def load_module():
    spec = importlib.util.spec_from_file_location("resolve_release_pr", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pr(number: int, *, merged: bool, sha: str | None = None) -> dict:
    return {
        "number": number,
        "title": TITLE,
        "url": f"https://example.invalid/pull/{number}",
        "state": "MERGED" if merged else "CLOSED",
        "mergedAt": "2026-10-07T13:22:11Z" if merged else None,
        "mergeCommit": {"oid": sha} if sha else None,
    }


class ReleasePrResolverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()

    def test_ignores_failed_closed_candidates_and_selects_unique_merged_pr(self) -> None:
        prs = [
            pr(271, merged=False),
            pr(274, merged=False),
            pr(277, merged=False),
            pr(280, merged=True, sha=MERGE_SHA),
        ]

        selected = self.module.resolve_merged_release_pr(
            prs,
            expected_title=TITLE,
        )

        self.assertEqual(selected["number"], 280)
        self.assertEqual(selected["mergeCommit"]["oid"], MERGE_SHA)

    def test_blocks_when_no_merged_release_pr_exists(self) -> None:
        prs = [
            pr(271, merged=False),
            pr(274, merged=False),
        ]

        with self.assertRaisesRegex(
            self.module.ReleasePrResolutionError,
            "got 0",
        ):
            self.module.resolve_merged_release_pr(prs, expected_title=TITLE)

    def test_blocks_when_multiple_merged_release_prs_exist(self) -> None:
        prs = [
            pr(280, merged=True, sha=MERGE_SHA),
            pr(281, merged=True, sha="a" * 40),
        ]

        with self.assertRaisesRegex(
            self.module.ReleasePrResolutionError,
            "got 2",
        ):
            self.module.resolve_merged_release_pr(prs, expected_title=TITLE)

    def test_blocks_merged_pr_without_exact_merge_sha(self) -> None:
        prs = [pr(280, merged=True)]

        with self.assertRaisesRegex(
            self.module.ReleasePrResolutionError,
            "has no exact merge commit SHA",
        ):
            self.module.resolve_merged_release_pr(prs, expected_title=TITLE)

    def test_ignores_same_branch_pr_with_different_title(self) -> None:
        other = pr(270, merged=True, sha="b" * 40)
        other["title"] = "unrelated"
        selected = self.module.resolve_merged_release_pr(
            [other, pr(280, merged=True, sha=MERGE_SHA)],
            expected_title=TITLE,
        )
        self.assertEqual(selected["number"], 280)


if __name__ == "__main__":
    unittest.main()
