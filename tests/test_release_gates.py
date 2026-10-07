from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "release_gates.py"
SHA = "a" * 40
APP = "ai-harness-release-bot"


def load_module():
    spec = importlib.util.spec_from_file_location("release_gates", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(name: str, ident: int, *, app: str, status: str = "completed", conclusion: str = "success", sha: str = SHA):
    return {
        "id": ident,
        "name": name,
        "head_sha": sha,
        "status": status,
        "conclusion": conclusion,
        "started_at": f"2026-10-07T00:00:{ident:02d}Z",
        "app": {"slug": app},
    }


class ReleaseGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()

    def good_runs(self):
        return [
            check("Harness Release Qualification", 1, app=APP),
            check("Validate Harness", 2, app="github-actions"),
            check("Validate Python 3.11 compatibility", 3, app="github-actions"),
            check("Validate Windows boundaries", 4, app="github-actions"),
        ]

    def test_all_exact_sha_checks_pass(self) -> None:
        ok, diagnostics = self.module.evaluate(
            self.good_runs(),
            sha=SHA,
            qualification_app_slug=APP,
        )
        self.assertTrue(ok, diagnostics)

    def test_missing_check_fails(self) -> None:
        runs = self.good_runs()[:-1]
        ok, diagnostics = self.module.evaluate(
            runs, sha=SHA, qualification_app_slug=APP
        )
        self.assertFalse(ok)
        self.assertTrue(any("MISSING Validate Windows boundaries" in x for x in diagnostics))

    def test_wrong_sha_does_not_count(self) -> None:
        runs = self.good_runs()
        runs[0]["head_sha"] = "b" * 40
        ok, diagnostics = self.module.evaluate(
            runs, sha=SHA, qualification_app_slug=APP
        )
        self.assertFalse(ok)
        self.assertTrue(any("MISSING Harness Release Qualification" in x for x in diagnostics))

    def test_latest_duplicate_wins(self) -> None:
        runs = self.good_runs()
        runs.extend([
            check("Harness Release Qualification", 8, app=APP, conclusion="failure"),
        ])
        ok, diagnostics = self.module.evaluate(
            runs, sha=SHA, qualification_app_slug=APP
        )
        self.assertFalse(ok)
        self.assertTrue(any("NOT_SUCCESS Harness Release Qualification id=8" in x for x in diagnostics))

    def test_wrong_app_fails(self) -> None:
        runs = self.good_runs()
        runs[0]["app"] = {"slug": "github-actions"}
        ok, diagnostics = self.module.evaluate(
            runs, sha=SHA, qualification_app_slug=APP
        )
        self.assertFalse(ok)
        self.assertTrue(any("INVALID_APP Harness Release Qualification" in x for x in diagnostics))

    def test_cli_rejects_in_progress_integrity(self) -> None:
        runs = self.good_runs()
        runs[1]["status"] = "in_progress"
        runs[1]["conclusion"] = None
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "checks.json"
            path.write_text(json.dumps({"check_runs": runs}), encoding="utf-8")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--checks-json",
                    str(path),
                    "--sha",
                    SHA,
                    "--qualification-app-slug",
                    APP,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(proc.returncode, 1, (proc.stdout, proc.stderr))
        self.assertIn("NOT_SUCCESS Validate Harness", proc.stdout)


if __name__ == "__main__":
    unittest.main()
