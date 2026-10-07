#!/usr/bin/env python3
"""Validate exact-SHA release gates from GitHub Check Runs JSON."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any

QUALIFICATION = "Harness Release Qualification"
INTEGRITY = (
    "Validate Harness",
    "Validate Python 3.11 compatibility",
    "Validate Windows boundaries",
)
HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")


def _load(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read check-runs JSON: {exc}") from exc
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


def evaluate(
    runs: list[dict[str, Any]],
    *,
    sha: str,
    qualification_app_slug: str,
) -> tuple[bool, list[str]]:
    diagnostics: list[str] = []
    ok = True

    expected = [(QUALIFICATION, qualification_app_slug)]
    expected.extend((name, "github-actions") for name in INTEGRITY)

    for name, app_slug in expected:
        item = _latest(runs, name, sha)
        if item is None:
            diagnostics.append(f"MISSING {name} on {sha}")
            ok = False
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

        if status != "completed" or conclusion != "success":
            diagnostics.append(
                f"NOT_SUCCESS {name} id={check_id}: status={status!r} conclusion={conclusion!r}"
            )
            ok = False
            continue

        diagnostics.append(
            f"PASS {name} id={check_id} app={actual_app} sha={sha}"
        )

    return ok, diagnostics


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify exact-SHA qualification and Harness Integrity checks."
    )
    parser.add_argument("--checks-json", required=True, type=Path)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--qualification-app-slug", required=True)
    args = parser.parse_args()

    if HEX40.fullmatch(args.sha) is None:
        parser.error("--sha must be an exact 40-hex Git commit SHA")
    if not args.qualification_app_slug.strip():
        parser.error("--qualification-app-slug must not be empty")

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
