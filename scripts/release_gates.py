#!/usr/bin/env python3
"""Проверка обязательных Check Runs exact SHA и ограниченное ожидание их завершения."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

QUALIFICATION = "Harness Release Qualification"
INTEGRITY = (
    "Validate Harness",
    "Validate Python 3.11 compatibility",
    "Validate Windows boundaries",
)
HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")
PENDING_STATUSES = {"queued", "in_progress", "waiting", "pending", "requested"}


def _load(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read check-runs JSON: {exc}") from exc
    return _parse(payload)


def _parse(payload: Any) -> list[dict[str, Any]]:
    runs = payload.get("check_runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        raise ValueError("check-runs JSON must contain check_runs list")
    return [item for item in runs if isinstance(item, dict)]


def _latest(runs: list[dict[str, Any]], name: str, sha: str) -> dict[str, Any] | None:
    matches = [
        item
        for item in runs
        if item.get("name") == name and item.get("head_sha") == sha
    ]
    if not matches:
        return None
    return max(
        matches,
        key=lambda item: (
            int(item.get("id") or 0),
            str(item.get("started_at") or ""),
        ),
    )


def evaluate_state(
    runs: list[dict[str, Any]],
    *,
    sha: str,
    qualification_app_slug: str,
) -> tuple[str, list[str]]:
    diagnostics: list[str] = []
    ok = True
    pending = False

    expected = [(QUALIFICATION, qualification_app_slug)]
    expected.extend((name, "github-actions") for name in INTEGRITY)

    for name, app_slug in expected:
        item = _latest(runs, name, sha)
        if item is None:
            diagnostics.append(f"MISSING {name} on {sha}")
            pending = True
            continue

        actual_app = None
        app = item.get("app")
        if isinstance(app, dict):
            actual_app = app.get("slug")

        status = item.get("status")
        conclusion = item.get("conclusion")
        check_id = item.get("id")

        if actual_app != app_slug:
            diagnostics.append(
                f"INVALID_APP {name} id={check_id}: expected {app_slug!r}, got {actual_app!r}"
            )
            ok = False
            continue

        # Проверка может стартовать позже qualification: ожидание допустимо только
        # для известного незавершённого состояния и доверенного App на том же SHA.
        if status in PENDING_STATUSES and conclusion is None:
            diagnostics.append(f"PENDING {name} id={check_id}: status={status!r}")
            pending = True
            continue

        if status != "completed" or conclusion != "success":
            diagnostics.append(
                f"NOT_SUCCESS {name} id={check_id}: status={status!r} conclusion={conclusion!r}"
            )
            ok = False
            continue

        diagnostics.append(
            f"PASS {name} id={check_id} app={actual_app} sha={sha}"
        )

    return ("FAIL" if not ok else "PENDING" if pending else "PASS"), diagnostics


def evaluate(
    runs: list[dict[str, Any]],
    *,
    sha: str,
    qualification_app_slug: str,
) -> tuple[bool, list[str]]:
    state, diagnostics = evaluate_state(
        runs, sha=sha, qualification_app_slug=qualification_app_slug
    )
    return state == "PASS", diagnostics


def wait_for_checks(
    repository: str,
    *,
    sha: str,
    qualification_app_slug: str,
    timeout_seconds: int,
    poll_interval_seconds: int,
) -> int:
    # Общий бюджет включает запросы к GitHub и паузы. Отдельный timeout шага
    # workflow страхует зависание процесса; см. README.md, Publish Harness Release.
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = subprocess.run(
                [
                    "gh", "api", "-H", "Accept: application/vnd.github+json",
                    f"repos/{repository}/commits/{sha}/check-runs?per_page=100",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
                timeout=max(0.001, min(30, deadline - time.monotonic())),
            )
            runs = _parse(json.loads(response.stdout))
        except (OSError, subprocess.SubprocessError, ValueError):
            if time.monotonic() >= deadline:
                break
            print("RELEASE GATES: BLOCKED: не удалось прочитать Check Runs GitHub", file=sys.stderr)
            return 2

        if time.monotonic() >= deadline:
            break
        state, diagnostics = evaluate_state(
            runs, sha=sha, qualification_app_slug=qualification_app_slug
        )
        for line in diagnostics:
            print(line, flush=True)
        print(f"RELEASE GATES: {state} exact_sha={sha}", flush=True)
        if state != "PENDING":
            return 0 if state == "PASS" else 1
        time.sleep(min(poll_interval_seconds, max(0, deadline - time.monotonic())))

    print(
        f"RELEASE GATES: TIMEOUT exact_sha={sha} timeout_seconds={timeout_seconds}",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify exact-SHA qualification and Harness Integrity checks."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--checks-json", type=Path)
    source.add_argument("--repository")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--poll-interval-seconds", type=int, default=15)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--qualification-app-slug", required=True)
    args = parser.parse_args()

    if HEX40.fullmatch(args.sha) is None:
        parser.error("--sha must be an exact 40-hex Git commit SHA")
    if not args.qualification_app_slug.strip():
        parser.error("--qualification-app-slug must not be empty")
    if args.timeout_seconds <= 0 or args.poll_interval_seconds <= 0:
        parser.error("timeout and poll interval must be positive")
    if args.repository is not None:
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository) is None:
            parser.error("--repository must look like owner/name")
        return wait_for_checks(
            args.repository,
            sha=args.sha,
            qualification_app_slug=args.qualification_app_slug.strip(),
            timeout_seconds=args.timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )

    try:
        runs = _load(args.checks_json)
    except ValueError as exc:
        print(f"release gate check: BLOCKED: {exc}", file=sys.stderr)
        return 2

    ok, diagnostics = evaluate(
        runs,
        sha=args.sha,
        qualification_app_slug=args.qualification_app_slug.strip(),
    )
    for line in diagnostics:
        print(line)

    if ok:
        print(f"RELEASE GATES: PASS exact_sha={args.sha}")
        return 0

    print(f"RELEASE GATES: FAIL exact_sha={args.sha}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
