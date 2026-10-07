#!/usr/bin/env python3
"""Resolve exactly one merged release PR from GitHub PR JSON."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any


class ReleasePrResolutionError(ValueError):
    """Fail-closed release PR resolution error."""


def resolve_merged_release_pr(
    prs: list[dict[str, Any]],
    *,
    expected_title: str,
) -> dict[str, Any]:
    title_matches = [pr for pr in prs if pr.get("title") == expected_title]
    merged_matches = [pr for pr in title_matches if pr.get("mergedAt")]

    if len(merged_matches) != 1:
        summary = ", ".join(
            f"#{pr.get('number')}:{'merged' if pr.get('mergedAt') else 'closed/unmerged'}"
            for pr in title_matches
        ) or "none"
        raise ReleasePrResolutionError(
            "expected exactly one merged release PR titled "
            f"{expected_title!r}, got {len(merged_matches)}; "
            f"title matches: {summary}"
        )

    pr = merged_matches[0]
    merge_sha = (pr.get("mergeCommit") or {}).get("oid")
    if not isinstance(merge_sha, str) or len(merge_sha) != 40:
        raise ReleasePrResolutionError(
            f"merged release PR #{pr.get('number')} has no exact merge commit SHA: "
            f"{pr.get('url')}"
        )

    return pr


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Select the unique merged release PR from gh pr list JSON."
    )
    parser.add_argument("--expected-title", required=True)
    args = parser.parse_args()

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"invalid PR JSON: {exc}", file=sys.stderr)
        return 2

    if not isinstance(payload, list):
        print("PR JSON must be a list", file=sys.stderr)
        return 2

    try:
        pr = resolve_merged_release_pr(
            [item for item in payload if isinstance(item, dict)],
            expected_title=args.expected_title,
        )
    except ReleasePrResolutionError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(pr["mergeCommit"]["oid"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
