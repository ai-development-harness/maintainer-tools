#!/usr/bin/env python3
"""Deterministic release metadata helper for AI Development Harness maintainers."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

SEMVER_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class ReleaseError(RuntimeError):
    pass


def semver(value: str) -> tuple[int, int, int]:
    match = SEMVER_RE.fullmatch(value)
    if not match:
        raise ReleaseError(f"invalid version '{value}'; expected vMAJOR.MINOR.PATCH")
    return tuple(int(part) for part in match.groups())


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReleaseError(f"missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseError(f"expected JSON object in {path}")
    return value


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_config(path: Path) -> dict:
    config = read_json(path)
    required = {
        "targetRepository",
        "defaultBranch",
        "releaseBranchPrefix",
        "manifestPath",
        "lockPath",
        "updateGraphPath",
    }
    missing = sorted(required - config.keys())
    if missing:
        raise ReleaseError(f"config missing keys: {', '.join(missing)}")
    return config


def paths(repo: Path, config: dict) -> tuple[Path, Path, Path]:
    return (
        repo / config["manifestPath"],
        repo / config["lockPath"],
        repo / config["updateGraphPath"],
    )


def manifest_release(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r'(?m)^  release:\s*["\']?([^"\'\s#]+)', text)
    if not match:
        raise ReleaseError(f"cannot find harness.release in {path}")
    return "v" + match.group(1).removeprefix("v")


def set_manifest_release(path: Path, version: str) -> None:
    text = path.read_text(encoding="utf-8")
    target = version.removeprefix("v")
    updated, count = re.subn(
        r'(?m)^(  release:\s*)["\']?[^"\'\s#]+["\']?',
        rf'\g<1>"{target}"',
        text,
        count=1,
    )
    if count != 1:
        raise ReleaseError(f"cannot update harness.release in {path}")
    path.write_text(updated, encoding="utf-8")


def validate_graph(graph: dict) -> None:
    if graph.get("schemaVersion") != 1:
        raise ReleaseError("update graph schemaVersion must be 1")
    latest = graph.get("latest")
    if not isinstance(latest, str):
        raise ReleaseError("update graph latest must be a string")
    semver(latest)
    transitions = graph.get("transitions")
    if not isinstance(transitions, list):
        raise ReleaseError("update graph transitions must be an array")

    outgoing: dict[str, str] = {}
    nodes = {latest}
    for index, edge in enumerate(transitions):
        if not isinstance(edge, dict):
            raise ReleaseError(f"transition[{index}] must be an object")
        source = edge.get("from")
        target = edge.get("to")
        if not isinstance(source, str) or not isinstance(target, str):
            raise ReleaseError(f"transition[{index}] from/to must be strings")
        source_v = semver(source)
        target_v = semver(target)
        if target_v <= source_v:
            raise ReleaseError(f"transition[{index}] must move forward: {source} -> {target}")
        if source in outgoing:
            raise ReleaseError(f"ambiguous update graph: multiple outgoing transitions from {source}")
        outgoing[source] = target
        nodes.update({source, target})
        kind = edge.get("kind")
        if kind not in {"standard", "bridge"}:
            raise ReleaseError(f"transition[{index}] has invalid kind: {kind}")
        if not isinstance(edge.get("reloadRequired"), bool):
            raise ReleaseError(f"transition[{index}] reloadRequired must be boolean")
        if kind == "bridge" and not str(edge.get("reason", "")).strip():
            raise ReleaseError(f"transition[{index}] bridge requires reason")

    if latest in outgoing:
        raise ReleaseError(f"update graph latest {latest} must be terminal")

    for start in sorted(nodes):
        current = start
        seen: set[str] = set()
        while current != latest:
            if current in seen:
                raise ReleaseError(f"update graph cycle detected from {start}")
            seen.add(current)
            nxt = outgoing.get(current)
            if nxt is None:
                raise ReleaseError(f"update graph node {start} cannot reach latest {latest}")
            current = nxt


def state(repo: Path, config: dict) -> tuple[str, dict, dict]:
    manifest_path, lock_path, graph_path = paths(repo, config)
    manifest = manifest_release(manifest_path)
    lock = read_json(lock_path)
    graph = read_json(graph_path)
    validate_graph(graph)

    lock_release = lock.get("release")
    lock_ref = lock.get("source", {}).get("ref") if isinstance(lock.get("source"), dict) else None
    graph_latest = graph.get("latest")
    expected_plain = manifest.removeprefix("v")

    errors = []
    if lock_release != expected_plain:
        errors.append(f"lock.release={lock_release!r}, expected {expected_plain!r}")
    if lock_ref != manifest:
        errors.append(f"lock.source.ref={lock_ref!r}, expected {manifest!r}")
    if graph_latest != manifest:
        errors.append(f"graph.latest={graph_latest!r}, expected {manifest!r}")
    if errors:
        raise ReleaseError("current release metadata is inconsistent: " + "; ".join(errors))
    return manifest, lock, graph


def command_current(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    current, _, _ = state(args.repo_dir, config)
    print(current)


def command_prepare(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    current, lock, graph = state(args.repo_dir, config)
    target = args.version
    if semver(target) <= semver(current):
        raise ReleaseError(f"target {target} must be newer than current {current}")

    all_nodes = {graph["latest"]}
    for edge in graph["transitions"]:
        all_nodes.update({edge["from"], edge["to"]})
    if target in all_nodes:
        raise ReleaseError(f"target {target} already exists in update graph")

    manifest_path, lock_path, graph_path = paths(args.repo_dir, config)
    set_manifest_release(manifest_path, target)

    lock["release"] = target.removeprefix("v")
    source = lock.setdefault("source", {})
    if not isinstance(source, dict):
        raise ReleaseError("lock.source must be an object")
    source["ref"] = target
    write_json(lock_path, lock)

    graph["latest"] = target
    graph["transitions"].append(
        {"from": current, "to": target, "kind": "standard", "reloadRequired": False}
    )
    validate_graph(graph)
    write_json(graph_path, graph)

    resulting, _, _ = state(args.repo_dir, config)
    if resulting != target:
        raise ReleaseError(f"postcondition failed: expected {target}, got {resulting}")
    print(f"prepared {current} -> {target}")


def command_verify(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    current, _, graph = state(args.repo_dir, config)
    if current != args.version:
        raise ReleaseError(f"metadata points to {current}, expected {args.version}")
    incoming = [edge for edge in graph["transitions"] if edge.get("to") == args.version]
    if not incoming:
        raise ReleaseError(f"release {args.version} has no incoming update transition")
    print(f"release metadata verified: {args.version}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--config", type=Path, required=True)
    sub = root.add_subparsers(dest="command", required=True)
    for name, handler in (("current", command_current), ("prepare", command_prepare), ("verify", command_verify)):
        cmd = sub.add_parser(name)
        cmd.add_argument("--repo-dir", type=Path, required=True)
        if name != "current":
            cmd.add_argument("--version", required=True)
        cmd.set_defaults(handler=handler)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        args.handler(args)
    except (OSError, ReleaseError) as exc:
        print(f"RELEASE ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
