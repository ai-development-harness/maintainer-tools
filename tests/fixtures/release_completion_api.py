"""Изолированный gh для regression-тестов Prepare; реальные запросы запрещены."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote

SOURCE = "ai-development-harness/ai-development-harness-template"
CANARY = "ai-development-harness/release-canary"
TAG = "v0.13.1"
TARGET = "a" * 40
BASELINE = "b" * 40
MERGE = "c" * 40


def responses(scenario: str) -> dict:
    release = {"tag_name": TAG, "draft": False, "prerelease": False,
               "published_at": "2026-10-16T08:00:00Z"}
    version = "0.13.0" if scenario == "stale-baseline" else "0.13.1"
    lock = {"release": version, "source": {"repository": SOURCE, "ref": "v" + version,
            "commit": "d" * 40 if scenario == "stale-baseline" else TARGET}}
    pr = {"state": "closed", "merged_at": "2026-10-16T08:30:00Z",
          "merge_commit_sha": MERGE,
          "html_url": f"https://github.com/{CANARY}/pull/14",
          "head": {"ref": "promotion/" + TAG, "repo": {"full_name": CANARY}},
          "base": {"ref": "main", "repo": {"full_name": CANARY}}}

    def contents(text: str) -> dict:
        return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}

    # Запрос state=closed не возвращает открытый promotion PR. При актуальном
    # lock пустой ответ должен независимо блокировать выпуск до merge.
    prs = [] if scenario == "unmerged-promotion" else [pr]
    head = quote(CANARY.split("/")[0] + ":promotion/" + TAG, safe="")
    return {
        f"repos/{SOURCE}/releases?per_page=100": [[release]],
        f"repos/{SOURCE}/releases/tags/{TAG}": release,
        f"repos/{SOURCE}/commits/{TAG}": {"sha": TARGET},
        f"repos/{CANARY}/branches/main": {"commit": {"sha": BASELINE}},
        f"repos/{CANARY}/contents/.harness/harness.lock.json?ref={BASELINE}": contents(json.dumps(lock)),
        f"repos/{CANARY}/contents/.harness/manifest.yaml?ref={BASELINE}": contents(f'harness:\n  release: "{version}"\n'),
        f"repos/{CANARY}/pulls?state=closed&base=main&head={head}&per_page=100": [prs],
        f"repos/{CANARY}/compare/{MERGE}...{BASELINE}": {"status": "ahead"},
    }


def main() -> int:
    args = sys.argv[1:]
    with Path(os.environ["PREPARE_TEST_REQUESTS"]).open("a") as out:
        out.write(json.dumps(args) + "\n")
    scenario = os.environ["PREPARE_TEST_SCENARIO"]
    if scenario not in ("accepted", "stale-baseline", "unmerged-promotion"):
        return 2
    data = responses(scenario)
    if len(args) < 2 or args[0] != "api" or args[1] not in data:
        return 2
    expected_flags = ["--paginate", "--slurp"] if isinstance(data[args[1]], list) else []
    if args[2:] != expected_flags:
        return 2
    print(json.dumps(data[args[1]]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
