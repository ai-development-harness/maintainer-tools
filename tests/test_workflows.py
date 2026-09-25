from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).parents[1]
PREPARE = ROOT / ".github" / "workflows" / "prepare-release.yml"
PUBLISH = ROOT / ".github" / "workflows" / "publish-release.yml"
CONFIG = ROOT / "config" / "release.json"


def input_block(text: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^      {re.escape(name)}:\\n(?P<body>(?:^        .*(?:\\n|$))+)",
        text,
    )
    if not match:
        raise AssertionError(f"workflow input {name!r} not found")
    return match.group("body")


class ReleaseWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.prepare = PREPARE.read_text(encoding="utf-8")
        self.publish = PUBLISH.read_text(encoding="utf-8")
        self.config = json.loads(CONFIG.read_text(encoding="utf-8"))

    def test_harness_branch_is_required_and_has_no_default(self) -> None:
        for text in (self.prepare, self.publish):
            block = input_block(text, "harness_branch")
            self.assertIn("required: true", block)
            self.assertIn("type: string", block)
            self.assertNotIn("default:", block)

    def test_no_hidden_default_harness_branch_remains(self) -> None:
        self.assertNotIn("defaultBranch", self.config)
        for text in (self.prepare, self.publish):
            self.assertNotIn("default_branch", text)
            self.assertNotIn("defaultBranch", text)

    def test_both_workflows_validate_explicit_branch_name(self) -> None:
        for text in (self.prepare, self.publish):
            self.assertIn("RAW_HARNESS_BRANCH", text)
            self.assertIn('git check-ref-format --branch "$HARNESS_BRANCH"', text)
            self.assertIn("there is no default branch", text)
            self.assertIn('ls-remote --exit-code --heads origin "refs/heads/${HARNESS_BRANCH}"', text)

    def test_prepare_uses_selected_branch_as_checkout_and_pr_base(self) -> None:
        self.assertIn("ref: ${{ steps.normalize.outputs.harness_branch }}", self.prepare)
        self.assertIn('--base "$HARNESS_BRANCH"', self.prepare)
        self.assertIn("Harness source branch moved during release preparation", self.prepare)
        self.assertIn("SOURCE_SHA: ${{ steps.source.outputs.source_sha }}", self.prepare)

    def test_publish_binds_release_pr_and_commit_to_selected_branch(self) -> None:
        self.assertIn("ref: ${{ steps.normalize.outputs.harness_branch }}", self.publish)
        self.assertIn('--base "$HARNESS_BRANCH"', self.publish)
        self.assertIn(
            'merge-base --is-ancestor "$MERGE_SHA" "refs/remotes/origin/${HARNESS_BRANCH}"',
            self.publish,
        )
        self.assertIn('git -C target checkout --detach "$MERGE_SHA"', self.publish)


if __name__ == "__main__":
    unittest.main()
