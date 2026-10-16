#!/usr/bin/env python3
"""Проверяет принятие опубликованного релиза в release-canary через read-only API."""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from urllib.parse import quote

SOURCE = "ai-development-harness/ai-development-harness-template"
CANARY = "ai-development-harness/release-canary"
SEMVER = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SHA = re.compile(r"^[0-9a-f]{40}$")


class CompletionError(RuntimeError):
    """Релиз опубликован, но его завершение пока не доказано."""


def api(endpoint: str, *, pages: bool = False):
    # Не печатаем stderr gh: для диагностики достаточно endpoint и exit code.
    command = ["gh", "api", endpoint]
    if pages:
        command += ["--paginate", "--slurp"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise CompletionError(f"GitHub API недоступен: {endpoint} (exit={result.returncode})")
    data = json.loads(result.stdout)
    return [item for page in data for item in page] if pages else data


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CompletionError(message)


def latest_stable() -> str:
    releases = api(f"repos/{SOURCE}/releases?per_page=100", pages=True)
    tags = [r["tag_name"] for r in releases if
            isinstance(r.get("tag_name"), str) and SEMVER.fullmatch(r["tag_name"])
            and r.get("draft") is False and r.get("prerelease") is False
            and r.get("published_at")]
    require(bool(tags), "Нет опубликованного stable release для проверки canary")
    return max(tags, key=lambda tag: tuple(map(int, tag[1:].split("."))))


def content(path: str, commit: str) -> str:
    data = api(f"repos/{CANARY}/contents/{path}?ref={commit}")
    require(data.get("encoding") == "base64", "Неизвестный формат GitHub contents")
    return base64.b64decode(data["content"]).decode("utf-8")


def verify(tag: str | None = None) -> dict:
    latest = tag is None
    tag = tag or latest_stable()
    require(SEMVER.fullmatch(tag) is not None, "Ожидается stable tag vX.Y.Z")
    release = api(f"repos/{SOURCE}/releases/tags/{tag}")
    require(release.get("tag_name") == tag and release.get("draft") is False
            and release.get("prerelease") is False and bool(release.get("published_at")),
            f"{tag} не является опубликованным stable release")
    target = api(f"repos/{SOURCE}/commits/{tag}")["sha"]
    baseline = api(f"repos/{CANARY}/branches/main")["commit"]["sha"]
    require(SHA.fullmatch(target) is not None and SHA.fullmatch(baseline) is not None,
            "GitHub не вернул точные commit SHA")

    # Файлы читаются из одного снимка main, а не из ветки promotion или рабочего дерева.
    # Одинаковая версия без точного source.commit не подтверждает принятие релиза.
    lock = json.loads(content(".harness/harness.lock.json", baseline))
    require(lock.get("release") == tag[1:] and lock.get("source", {}).get("repository") == SOURCE
            and lock["source"].get("ref") == tag and lock["source"].get("commit") == target,
            f"Canary main ещё не закреплён на {tag} / {target}; завершите promotion PR")
    manifest = content(".harness/manifest.yaml", baseline)
    section = re.search(r"(?m)^harness:\s*\n((?:[ \t]+[^\n]*\n|\n)*)", manifest + "\n")
    require(section is not None and re.search(
        rf'(?m)^  release: [\"\']?{re.escape(tag[1:])}[\"\']?\s*$', section[1]) is not None,
        "Manifest canary main не совпадает с release lock")

    owner = CANARY.split("/")[0]
    branch = "promotion/" + tag
    prs = api(f"repos/{CANARY}/pulls?state=closed&base=main&head={quote(owner + ':' + branch, safe='')}&per_page=100", pages=True)
    merged = [pr for pr in prs if pr.get("merged_at") and pr.get("state") == "closed"
              and pr.get("base", {}).get("ref") == "main"
              and pr.get("base", {}).get("repo", {}).get("full_name") == CANARY
              and pr.get("head", {}).get("ref") == branch
              and pr.get("head", {}).get("repo", {}).get("full_name") == CANARY]
    require(len(merged) == 1, f"Нет единственного влитого promotion PR для {tag}")
    pr = merged[0]
    merge = pr.get("merge_commit_sha", "")
    require(isinstance(merge, str) and SHA.fullmatch(merge) is not None,
            "Promotion PR не содержит точный merge SHA")
    comparison = api(f"repos/{CANARY}/compare/{merge}...{baseline}")
    require(comparison.get("status") in ("ahead", "identical"),
            "Merge promotion PR отсутствует в истории принятого canary main")

    # Смена main или нового stable во время чтения требует повторной проверки.
    require(api(f"repos/{CANARY}/branches/main")["commit"]["sha"] == baseline,
            "Canary main изменился во время проверки; повторите gate")
    if latest:
        require(latest_stable() == tag, "Опубликован новый stable; повторите gate")
    return {"status": "COMPLETE", "release": tag, "sourceCommit": target,
            "canaryCommit": baseline, "promotionPr": pr["html_url"], "mergeCommit": merge}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="Проверить конкретный vX.Y.Z; по умолчанию последний stable")
    args = parser.parse_args()
    try:
        result = verify(args.version)
    except (CompletionError, OSError, subprocess.TimeoutExpired, ValueError,
            KeyError, TypeError, AttributeError) as exc:
        reason = str(exc) if isinstance(exc, CompletionError) else "Не удалось прочитать корректные данные GitHub"
        print(json.dumps({"status": "BLOCKED", "reason": reason}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
