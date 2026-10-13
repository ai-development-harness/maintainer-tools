#!/usr/bin/env python3
"""Безопасное продвижение release-canary на следующий опубликованный Harness release.

Скрипт выполняет только локальные deterministic gates. Он НЕ публикует release,
НЕ обходит code review и НЕ сливает Pull Request. GitHub workflow отвечает за
разрешение immutable refs, выдачу scoped token, создание ветки и PR.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tomllib
from typing import Any


SEMVER = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SHA = re.compile(r"^[0-9a-f]{40}$")
MAX_UPDATE_ATTEMPTS = 12


class PromotionError(RuntimeError):
    """Обязательный gate остановил продвижение до создания PR."""


def version(tag: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(tag)
    if match is None:
        raise PromotionError(f"invalid stable release tag: {tag!r}")
    return tuple(int(n) for n in match.groups())


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PromotionError(f"expected JSON object: {path}")
    return value


def select_release(lock: dict[str, Any], releases: list[Any], source_repo: str) -> dict[str, Any]:
    """Выбирает ближайший ОПУБЛИКОВАННЫЙ stable выше принятой версии canary.

    Не перескакиваем через релизы: qualification должна проверять переход ровно
    предыдущий stable -> новый candidate, даже если накопилось несколько tags.
    """
    source = lock.get("source")
    if not isinstance(source, dict) or source.get("repository") != source_repo:
        raise PromotionError("canary lock references unexpected Harness source repository")
    baseline = lock.get("release")
    if not isinstance(baseline, str):
        raise PromotionError("missing canary lock.release")
    baseline_tag = "v" + baseline
    baseline_version = version(baseline_tag)
    if source.get("ref") != baseline_tag:
        raise PromotionError("canary lock release and source.ref differ")
    if not isinstance(source.get("commit"), str) or SHA.fullmatch(source["commit"]) is None:
        raise PromotionError("canary lock lacks exact source commit")

    # gh api --paginate --slurp возвращает массив страниц, а одиночный GET —
    # обычный массив. Принимаем обе формы; не принимаем частично испорченный JSON.
    flat: list[dict[str, Any]] = []
    for page in releases:
        if isinstance(page, list):
            if not all(isinstance(item, dict) for item in page):
                raise PromotionError("malformed releases page")
            flat.extend(page)
        elif isinstance(page, dict):
            flat.append(page)
        else:
            raise PromotionError("malformed releases response")

    available: dict[str, dict[str, Any]] = {}
    for item in flat:
        tag = item.get("tag_name")
        if (
            not isinstance(tag, str)
            or SEMVER.fullmatch(tag) is None
            or item.get("draft") is not False
            or item.get("prerelease") is not False
            or not isinstance(item.get("published_at"), str)
        ):
            continue
        if version(tag) <= baseline_version:
            continue
        if tag in available:
            raise PromotionError(f"duplicate published release: {tag}")
        available[tag] = item

    if not available:
        return {"status": "NO_UPDATE", "current": baseline_tag}
    tag = min(available, key=version)
    return {"status": "PROMOTE", "current": baseline_tag, "target": tag}


def run(
    args: list[str], root: Path, *, timeout: int = 900, expected_code: int = 0
) -> str:
    """Выполняем команду без shell; ошибка/timeout всегда останавливают pipeline."""
    try:
        proc = subprocess.run(
            args,
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PromotionError(f"command could not finish: {args[0]}: {exc}") from exc
    if proc.returncode != expected_code:
        diagnostic = (proc.stderr or proc.stdout).strip()[-4000:]
        raise PromotionError(
            f"command failed (exit={proc.returncode}): {' '.join(args)}: {diagnostic}"
        )
    return proc.stdout.strip()


def git(root: Path, *args: str) -> str:
    return run(["git", *args], root, timeout=120)


def command_json(args: list[str], root: Path, *, timeout: int = 900) -> dict[str, Any]:
    data = run(args, root, timeout=timeout)
    try:
        value = json.loads(data)
    except json.JSONDecodeError as exc:
        raise PromotionError(f"invalid JSON from {args[1]}: {data[-800:]}") from exc
    if not isinstance(value, dict):
        raise PromotionError(f"non-object JSON from {args[1]}")
    return value


def parse_manifest_release(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r'(?m)^  release: "([^"]+)"\s*$', text)
    if match is None:
        raise PromotionError("manifest harness.release not found")
    return match.group(1)


def assert_identity(project: Path, source_repo: str, expected: str, target_sha: str | None = None) -> None:
    """Manifest, lock и exact pin обязаны говорить об одном release."""
    manifest = parse_manifest_release(project / ".harness/manifest.yaml")
    lock = read_object(project / ".harness/harness.lock.json")
    source = lock.get("source")
    if manifest != expected or lock.get("release") != expected:
        raise PromotionError(f"manifest/lock version mismatch: {manifest} / {lock.get('release')} != {expected}")
    if not isinstance(source, dict) or source.get("repository") != source_repo:
        raise PromotionError("unexpected locked Harness source")
    if source.get("ref") != "v" + expected:
        raise PromotionError("wrong locked Harness source ref")
    if target_sha is not None and source.get("commit") != target_sha:
        raise PromotionError("updated lock is not pinned to exact published tag SHA")


def is_managed(path: str, patterns: list[str], lock_path: str) -> bool:
    return path == lock_path or any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def ownership(project: Path, baseline_sha: str) -> tuple[list[str], str]:
    policy = tomllib.loads(git(project, "show", f"{baseline_sha}:.harness/harness-update.toml"))
    classes = policy["ownership"]
    patterns: list[str] = []
    for key in ("harness_owned", "shared", "marker_merge"):
        patterns.extend(classes[key])
    return patterns, policy["state"]["lock_file"]


def tracked_project_snapshot(
    project: Path, baseline_sha: str, patterns: list[str], lock_path: str
) -> dict[str, tuple[str, str]]:
    """Снимок bytes/mode проектных файлов из точного Git commit, не рабочего дерева."""
    output = subprocess.run(
        ["git", "ls-tree", "-rz", "--full-tree", baseline_sha],
        cwd=project,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout
    result: dict[str, tuple[str, str]] = {}
    for record in output.split(b"\0"):
        if not record:
            continue
        meta, raw_path = record.split(b"\t", 1)
        mode, kind, oid = meta.decode("ascii").split()
        path = raw_path.decode("utf-8")
        if kind == "blob" and not is_managed(path, patterns, lock_path):
            result[path] = (mode, oid)
    return result


def verify_preservation(
    project: Path,
    before: dict[str, tuple[str, str]],
    patterns: list[str],
    lock_path: str,
) -> int:
    """Ни один исходный project-owned blob/mode не должен исчезнуть или измениться.

    Новые managed файлы допустимы; из новых project-owned допускаются только
    immutable update reports. Произвольные добавления не скрываем как harmless.
    """
    for path, (mode, oid) in before.items():
        actual_path = project / path
        if mode == "120000":
            if not actual_path.is_symlink():
                raise PromotionError(f"project-owned symlink disappeared: {path}")
            actual = os.fsencode(os.readlink(actual_path))
        elif actual_path.is_file():
            actual = actual_path.read_bytes()
            actual_mode = "100755" if actual_path.stat().st_mode & stat.S_IXUSR else "100644"
            if actual_mode != mode:
                raise PromotionError(f"project-owned mode changed: {path}")
        else:
            raise PromotionError(f"project-owned file disappeared: {path}")
        # Git blob OID: SHA1("blob <length>\0" + bytes), как в git ls-tree.
        blob_sha = hashlib.sha1(b"blob " + str(len(actual)).encode() + b"\0" + actual).hexdigest()
        if blob_sha != oid:
            raise PromotionError(f"project-owned content changed: {path}")

    paths = set(git(project, "ls-files", "-c", "-o", "--exclude-standard").splitlines())
    for path in paths - set(before):
        if is_managed(path, patterns, lock_path):
            continue
        if re.fullmatch(r"planning/harness-updates/UPDATE-[A-Za-z0-9TZ-]+\.md", path):
            continue
        raise PromotionError(f"unexpected project-owned file introduced: {path}")
    return len(before)


def canonical_digest(project: Path) -> str:
    """Проверка идемпотентности по реальным canonical байтам, включая untracked."""
    names = subprocess.run(
        ["git", "ls-files", "-z", "-c", "-o", "--exclude-standard"],
        cwd=project,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout
    digest = hashlib.sha256()
    for raw in sorted(set(part for part in names.split(b"\0") if part)):
        name = os.fsdecode(raw)
        path = project / name
        digest.update(raw + b"\0")
        if path.is_symlink():
            digest.update(b"symlink\0" + os.fsencode(os.readlink(path)))
        elif path.is_file():
            digest.update(path.read_bytes())
        else:
            digest.update(b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


def validate_graph(source: Path, installed: str, target: str) -> None:
    graph = read_object(source / ".harness/harness-update-graph.json")
    transitions = graph.get("transitions")
    if not isinstance(transitions, list):
        raise PromotionError("invalid source update graph")
    matching = [
        x for x in transitions
        if isinstance(x, dict) and x.get("from") == installed and x.get("to") == target
    ]
    if len(matching) != 1:
        raise PromotionError(f"no unique direct trusted transition from {installed} to {target}")


def schema_gate(project: Path) -> str:
    cmd = [sys.executable, ".harness/tools/migrate-project-schema.py", "--check", "--json"]
    proc = subprocess.run(cmd, cwd=project, capture_output=True, text=True, check=False, timeout=180)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise PromotionError(f"schema gate returned invalid JSON: {proc.stderr[-1000:]}") from exc
    if data.get("status") == "MIGRATION_REQUIRED":
        command_json(
            [sys.executable, ".harness/tools/migrate-project-schema.py", "--json"],
            project,
            timeout=240,
        )
        data = command_json(cmd, project, timeout=180)
    if data.get("status") != "CURRENT":
        raise PromotionError(f"project schema not CURRENT: {data}")
    return "CURRENT"


def update(project: Path, tag: str) -> list[str]:
    statuses: list[str] = []
    for _ in range(MAX_UPDATE_ATTEMPTS):
        result = command_json(
            [sys.executable, ".harness/tools/harness-update.py", "apply", "--to", tag, "--json"],
            project,
            timeout=900,
        )
        state = result.get("status")
        if state not in {"UPDATED", "UPDATER_RELOAD_REQUIRED", "NO_UPDATE"}:
            raise PromotionError(f"updater did not accept stable release: {result}")
        statuses.append(state)
        if state == "NO_UPDATE":
            if len(statuses) == 1:
                raise PromotionError("upgrade did not apply any transition")
            if result.get("repositoryMutated"):
                raise PromotionError("NO_UPDATE still mutated canonical repository")
            return statuses
        # Каждый запуск — отдельный Python process; новые runtime-модули
        # загружаются заново. Не продолжаем старый in-process execution.
    raise PromotionError("updater reload sequence exceeded safety bound")


def promote(
    project: Path,
    source: Path,
    *,
    baseline_sha: str,
    target_tag: str,
    target_sha: str,
    source_repo: str,
    run_url: str,
) -> dict[str, Any]:
    if SHA.fullmatch(baseline_sha) is None or SHA.fullmatch(target_sha) is None:
        raise PromotionError("baseline and target commit IDs must be exact 40-char SHA1")
    version(target_tag)
    if git(project, "rev-parse", "HEAD") != baseline_sha:
        raise PromotionError("checkout is not the accepted canary baseline")
    if git(project, "status", "--porcelain=v1", "--untracked-files=all"):
        raise PromotionError("canary checkout is dirty before promotion")
    if git(source, "rev-parse", "HEAD") != target_sha:
        raise PromotionError("Harness source checkout SHA differs from published tag")
    if git(source, "rev-parse", "--verify", f"refs/tags/{target_tag}^{{commit}}") != target_sha:
        raise PromotionError("target tag did not resolve to immutable target SHA")
    # Публикация maintenance-ветки не должна менять canary основной release-линии.
    git(source, "merge-base", "--is-ancestor", target_sha, "refs/remotes/origin/main")

    baseline_lock = read_object(project / ".harness/harness.lock.json")
    base_version = baseline_lock.get("release")
    if not isinstance(base_version, str):
        raise PromotionError("canary baseline lock lacks release")
    assert_identity(project, source_repo, base_version)
    if baseline_lock["source"].get("commit") != git(source, "rev-parse", "--verify", f"refs/tags/v{base_version}^{{commit}}"):
        raise PromotionError("canary baseline source commit is not exact previous stable")
    validate_graph(source, "v" + base_version, target_tag)

    patterns, lock_path = ownership(project, baseline_sha)
    before = tracked_project_snapshot(project, baseline_sha, patterns, lock_path)

    statuses = update(project, target_tag)
    schema_status = schema_gate(project)
    for name in ("status", "doctor"):
        outcome = command_json(
            [sys.executable, ".harness/tools/harness-ux.py", name, "--json"], project, timeout=180
        )
        if outcome.get("status") != "PASS":
            raise PromotionError(f"HARNESS {name.upper()} blocked: {outcome}")

    run([sys.executable, ".harness/tools/validate.py", "--mode", "manual"], project)
    tests = command_json(
        [sys.executable, ".harness/tools/run-self-tests.py", "--json"], project, timeout=1500
    )
    if tests.get("status") != "PASS":
        raise PromotionError(f"self-tests failed: {tests.get('status')}")
    count = tests.get("count")
    if not isinstance(count, int) or count < 1:
        raise PromotionError("self-test suite was empty or lacked count")

    preserved = verify_preservation(project, before, patterns, lock_path)
    assert_identity(project, source_repo, target_tag[1:], target_sha=target_sha)

    # Отдельный повтор APPLY уже сделан в update(). Теперь дополнительно
    # доказываем, что NO_UPDATE действительно не изменил canonical bytes.
    digest_before = canonical_digest(project)
    repeated = command_json(
        [sys.executable, ".harness/tools/harness-update.py", "apply", "--to", target_tag, "--json"],
        project,
        timeout=900,
    )
    if repeated.get("status") != "NO_UPDATE" or repeated.get("repositoryMutated"):
        raise PromotionError(f"repeated APPLY was not a canonical no-op: {repeated}")
    if canonical_digest(project) != digest_before:
        raise PromotionError("repeated APPLY changed canonical bytes")

    evidence = project / f"docs/baseline-promotions/{target_tag}.md"
    if evidence.exists():
        raise PromotionError(f"promotion evidence already exists: {evidence}")
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        "---\nschema: 1\n---\n"
        f"# Baseline promotion — {target_tag}\n\n"
        "## Identity\n\n"
        f"- Accepted baseline commit: \x60{baseline_sha}\x60\n"
        f"- Previous stable Harness: \x60v{base_version}\x60\n"
        f"- Published target: \x60{target_tag}\x60\n"
        f"- Exact target Harness SHA: \x60{target_sha}\x60\n"
        f"- Promotion run: {run_url}\n\n"
        "## Gates\n\n"
        "| Gate | Result |\n| --- | --- |\n"
        "| Exact stable identities and direct update route | PASS |\n"
        f"| Primary APPLY / reload sequence | {' -> '.join(statuses)} |\n"
        f"| Project schema | {schema_status} |\n"
        "| HARNESS STATUS / DOCTOR | PASS / PASS |\n"
        "| Validator | PASS |\n"
        f"| Full self-tests | PASS ({count} tests) |\n"
        f"| Project-owned preservation | PASS ({preserved} files) |\n"
        "| Repeated APPLY | NO_UPDATE |\n"
        "| Repeated APPLY canonical mutation | PASS |\n\n"
        "## Decision\n\n"
        "All deterministic promotion gates passed. The baseline is NOT accepted "
        "until an independent PR CI run succeeds and the PR is reviewed and merged.\n",
        encoding="utf-8",
        newline="\n",
    )
    return {
        "status": "PASS",
        "previous": "v" + base_version,
        "target": target_tag,
        "targetSha": target_sha,
        "baselineSha": baseline_sha,
        "selfTests": count,
        "preservedProjectFiles": preserved,
        "applyStatuses": statuses,
        "evidence": evidence.relative_to(project).as_posix(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    select = actions.add_parser("select")
    select.add_argument("--lock", type=Path, required=True)
    select.add_argument("--releases", type=Path, required=True)
    select.add_argument("--source-repository", required=True)

    upgrade = actions.add_parser("promote")
    upgrade.add_argument("--project", type=Path, required=True)
    upgrade.add_argument("--source", type=Path, required=True)
    upgrade.add_argument("--baseline-sha", required=True)
    upgrade.add_argument("--target-tag", required=True)
    upgrade.add_argument("--target-sha", required=True)
    upgrade.add_argument("--source-repository", required=True)
    upgrade.add_argument("--run-url", required=True)

    args = parser.parse_args()
    try:
        if args.action == "select":
            result = select_release(
                read_object(args.lock),
                json.loads(args.releases.read_text(encoding="utf-8")),
                args.source_repository,
            )
        else:
            result = promote(
                args.project.resolve(), args.source.resolve(),
                baseline_sha=args.baseline_sha,
                target_tag=args.target_tag,
                target_sha=args.target_sha,
                source_repo=args.source_repository,
                run_url=args.run_url,
            )
    except (PromotionError, OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"status": "FAIL", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
