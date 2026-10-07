from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).parents[1]
PREPARE = ROOT / ".github" / "workflows" / "prepare-release.yml"
PUBLISH = ROOT / ".github" / "workflows" / "publish-release.yml"
QUALIFICATION = ROOT / ".github" / "workflows" / "release-qualification.yml"
CONFIG = ROOT / "config" / "release.json"


def input_block(text: str, name: str) -> str:
    match = re.search(
        rf"(?m)^      {re.escape(name)}:\n(?P<body>(?:^        [^\n]*(?:\n|$))+)",
        text,
    )
    if not match:
        raise AssertionError(f"workflow input {name!r} not found")
    return match.group("body")


class ReleaseWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.prepare = PREPARE.read_text(encoding="utf-8")
        self.publish = PUBLISH.read_text(encoding="utf-8")
        self.qualification = QUALIFICATION.read_text(encoding="utf-8")
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

    def test_qualification_is_reusable_and_manual(self) -> None:
        self.assertIn("workflow_call:", self.qualification)
        self.assertIn("workflow_dispatch:", self.qualification)
        for name in ("target_repository", "target_ref", "target_sha"):
            self.assertGreaterEqual(self.qualification.count(f"{name}:"), 2)

    def test_qualification_binds_explicit_check_to_exact_target_sha(self) -> None:
        self.assertIn('"name": "Harness Release Qualification"', self.qualification)
        self.assertIn('"head_sha": os.environ["TARGET_SHA"]', self.qualification)
        self.assertIn("permission-checks: write", self.qualification)
        self.assertIn("CHECK_ID: ${{ needs.preflight.outputs.check_id }}", self.qualification)
        self.assertIn("if: always()", self.qualification)

    def test_qualification_uses_canonical_core_entrypoints(self) -> None:
        self.assertIn("--lane current", self.qualification)
        self.assertIn("--lane minimum", self.qualification)
        self.assertIn("--lane windows", self.qualification)
        self.assertIn("release-upgrade-qualification.py", self.qualification)
        self.assertNotIn("run-stress-tests.py", self.qualification)

    def test_private_canary_access_is_scoped_and_exact(self) -> None:
        self.assertIn('default: "ai-development-harness/release-canary"', self.qualification)
        self.assertIn("${{ steps.inputs.outputs.canary_name }}", self.qualification)
        self.assertIn("permission-contents: read", self.qualification)
        self.assertIn("canary_sha=", self.qualification)
        self.assertIn("Release App cannot resolve private canary branch", self.qualification)

    def test_prepare_exports_and_qualifies_exact_candidate_sha(self) -> None:
        self.assertIn("candidate_sha: ${{ steps.release-commit.outputs.candidate_sha }}", self.prepare)
        self.assertIn("id: release-commit", self.prepare)
        self.assertIn('echo "candidate_sha=$CANDIDATE_SHA" >> "$GITHUB_OUTPUT"', self.prepare)
        self.assertIn("uses: ./.github/workflows/release-qualification.yml", self.prepare)
        self.assertIn("target_sha: ${{ needs.prepare.outputs.candidate_sha }}", self.prepare)
        self.assertIn("target_ref: ${{ needs.prepare.outputs.release_branch }}", self.prepare)

    def test_qualification_is_read_only_for_release_state(self) -> None:
        forbidden = (
            "gh release create",
            "git tag ",
            "git push origin",
            "scripts/release.py prepare",
        )
        for item in forbidden:
            self.assertNotIn(item, self.qualification)


if __name__ == "__main__":
    unittest.main()
