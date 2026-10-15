from __future__ import annotations

import importlib.util
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

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


def check(name: str, ident: int, *, app: str, status: str = "completed", conclusion: str | None = "success", sha: str = SHA):
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
        self.assertIn("PENDING Validate Harness", proc.stdout)

    def run_wait(self, snapshots, *, timeout=30, interval=15, request_seconds=0):
        # Виртуальное время проверяет общий бюджет без реальных пауз и запросов.
        now = [0.0]
        stdout, stderr = io.StringIO(), io.StringIO()
        responses = iter(snapshots)

        def fetch(*args, **kwargs):
            now[0] += request_seconds
            snapshot = next(responses)
            if isinstance(snapshot, Exception):
                raise snapshot
            return subprocess.CompletedProcess(args[0], 0, json.dumps({"check_runs": snapshot}))

        def sleep(seconds):
            now[0] += seconds

        with (
            mock.patch.object(self.module.subprocess, "run", side_effect=fetch) as fetch_mock,
            mock.patch.object(self.module.time, "monotonic", side_effect=lambda: now[0]),
            mock.patch.object(self.module.time, "sleep", side_effect=sleep) as sleep_mock,
            redirect_stdout(stdout), redirect_stderr(stderr),
        ):
            code = self.module.wait_for_checks(
                "ai-development-harness/ai-development-harness-template",
                sha=SHA, qualification_app_slug=APP,
                timeout_seconds=timeout, poll_interval_seconds=interval,
            )
        return code, fetch_mock, sleep_mock, stdout.getvalue(), stderr.getvalue()

    def pending_runs(self, status="in_progress"):
        runs = self.good_runs()
        runs[-1].update(status=status, conclusion=None)
        return runs

    def test_wait_returns_immediately_for_success(self) -> None:
        code, fetch, sleep, out, err = self.run_wait([self.good_runs()])
        self.assertEqual(code, 0, err)
        self.assertEqual(fetch.call_count, 1)
        sleep.assert_not_called()
        self.assertIn("RELEASE GATES: PASS", out)

    def test_wait_refreshes_pending_checks_until_success(self) -> None:
        for status in ("queued", "in_progress", "waiting", "pending", "requested"):
            with self.subTest(status=status):
                code, fetch, sleep, out, err = self.run_wait([
                    self.pending_runs(status), self.good_runs(),
                ])
                self.assertEqual(code, 0, err)
                self.assertEqual(fetch.call_count, 2)
                sleep.assert_called_once_with(15)
                self.assertIn("RELEASE GATES: PENDING", out)
                self.assertIn("RELEASE GATES: PASS", out)
                for call in fetch.call_args_list:
                    self.assertIn(f"/commits/{SHA}/check-runs?per_page=100", call.args[0][-1])

    def test_wait_allows_delayed_check_creation(self) -> None:
        code, fetch, sleep, out, err = self.run_wait([
            self.good_runs()[:-1], self.good_runs(),
        ])
        self.assertEqual(code, 0, err)
        self.assertEqual(fetch.call_count, 2)
        self.assertIn("MISSING Validate Windows boundaries", out)

    def test_wait_fails_immediately_for_unsuccessful_conclusions(self) -> None:
        for conclusion in ("failure", "cancelled", "timed_out", "skipped", "neutral", "action_required", "stale", None):
            with self.subTest(conclusion=conclusion):
                runs = self.pending_runs()
                runs[0]["conclusion"] = conclusion
                code, fetch, sleep, out, err = self.run_wait([runs])
                self.assertEqual(code, 1, err)
                self.assertEqual(fetch.call_count, 1)
                sleep.assert_not_called()
                self.assertIn("RELEASE GATES: FAIL", out)

    def test_wait_stops_when_running_check_fails(self) -> None:
        failed = self.good_runs()
        failed[-1]["conclusion"] = "failure"
        code, fetch, sleep, out, err = self.run_wait([self.pending_runs(), failed])
        self.assertEqual(code, 1, err)
        self.assertEqual(fetch.call_count, 2)
        self.assertIn("NOT_SUCCESS Validate Windows boundaries", out)

    def test_wait_rejects_wrong_app_and_unknown_status(self) -> None:
        for update in ({"app": {"slug": "untrusted"}}, {"status": "unknown"}):
            with self.subTest(update=update):
                runs = self.pending_runs()
                runs[-1].update(update)
                code, fetch, sleep, out, err = self.run_wait([runs])
                self.assertEqual(code, 1, err)
                sleep.assert_not_called()
                self.assertIn("RELEASE GATES: FAIL", out)

    def test_wait_does_not_reuse_old_success_during_rerun(self) -> None:
        runs = self.good_runs()
        runs.append(check("Validate Windows boundaries", 5, app="github-actions", status="queued", conclusion=None))
        code, fetch, sleep, out, err = self.run_wait([runs, runs])
        self.assertEqual(code, 1)
        self.assertIn("PENDING Validate Windows boundaries id=5", out)
        self.assertNotIn("RELEASE GATES: PASS", out)
        self.assertIn("TIMEOUT", err)

    def test_wait_times_out_for_missing_or_unfinished_checks(self) -> None:
        wrong_sha = self.good_runs()
        wrong_sha[-1]["head_sha"] = "b" * 40
        for runs in (self.pending_runs(), self.good_runs()[:-1], wrong_sha):
            with self.subTest(runs=runs):
                code, fetch, sleep, out, err = self.run_wait([runs, runs], timeout=20)
                self.assertEqual(code, 1)
                self.assertEqual(fetch.call_count, 2)
                self.assertEqual(sleep.call_args_list, [mock.call(15), mock.call(5)])
                self.assertIn("TIMEOUT", err)
                self.assertIn("timeout_seconds=20", err)

    def test_wait_budget_includes_api_time(self) -> None:
        code, fetch, sleep, out, err = self.run_wait([
            self.pending_runs(), self.good_runs(),
        ], timeout=20, interval=5, request_seconds=8)
        self.assertEqual(code, 1)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args_list[1].kwargs["timeout"], 7)
        self.assertNotIn("RELEASE GATES: PASS", out)
        self.assertIn("TIMEOUT", err)

    def test_wait_blocks_api_errors_without_disclosing_output(self) -> None:
        errors = (
            subprocess.CalledProcessError(1, "gh", stderr="secret"),
            subprocess.TimeoutExpired("gh", 30, stderr="secret"),
            OSError("secret"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                code, fetch, sleep, out, err = self.run_wait([error])
                self.assertEqual(code, 2)
                sleep.assert_not_called()
                self.assertIn("BLOCKED", err)
                self.assertNotIn("secret", out + err)

    def test_wait_reports_timeout_when_api_exhausts_budget(self) -> None:
        code, fetch, sleep, out, err = self.run_wait(
            [subprocess.TimeoutExpired("gh", 20)], timeout=20, request_seconds=20,
        )
        self.assertEqual(code, 1)
        sleep.assert_not_called()
        self.assertIn("RELEASE GATES: TIMEOUT", err)

    def test_wait_blocks_malformed_api_response(self) -> None:
        for response in ("not json", '{"check_runs": null}'):
            with (
                self.subTest(response=response),
                mock.patch.object(self.module.subprocess, "run", return_value=subprocess.CompletedProcess("gh", 0, response)),
                redirect_stderr(io.StringIO()) as err,
            ):
                code = self.module.wait_for_checks(
                    "owner/repo", sha=SHA, qualification_app_slug=APP,
                    timeout_seconds=30, poll_interval_seconds=15,
                )
                self.assertEqual(code, 2)
                self.assertIn("BLOCKED", err.getvalue())

    def test_cli_rejects_nonpositive_wait_budgets(self) -> None:
        for option in ("--timeout-seconds", "--poll-interval-seconds"):
            for value in ("0", "-1"):
                with self.subTest(option=option, value=value):
                    proc = subprocess.run(
                        [sys.executable, str(SCRIPT), "--repository", "owner/repo",
                         "--sha", SHA, "--qualification-app-slug", APP, option, value],
                        capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(proc.returncode, 2)
                    self.assertIn("must be positive", proc.stderr)


if __name__ == "__main__":
    unittest.main()
