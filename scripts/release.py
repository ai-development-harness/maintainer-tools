#!/usr/bin/env python3
"""Deterministic release metadata helper for AI Development Harness maintainers."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

SEMVER_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

LAYOUT_KEYS = {
    "name",
    "manifestPath",
    "lockPath",
    "updateGraphPath",
    "validatorPath",
}


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


def _validate_layout(layout: object, index: int) -> dict:
    if not isinstance(layout, dict):
        raise ReleaseError(f"config layouts[{index}] must be an object")
    missing = sorted(LAYOUT_KEYS - layout.keys())
    if missing:
        raise ReleaseError(f"config layouts[{index}] missing keys: {', '.join(missing)}")
    for key in LAYOUT_KEYS:
        if not isinstance(layout[key], str) or not layout[key].strip():
            raise ReleaseError(f"config layouts[{index}].{key} must be a non-empty string")
    return layout


def load_config(path: Path) -> dict:
    config = read_json(path)
    required = {
        "targetRepository",
        "defaultBranch",
        "releaseBranchPrefix",
    }
    missing = sorted(required - config.keys())
    if missing:
        raise ReleaseError(f"config missing keys: {', '.join(missing)}")

    layouts = config.get("layouts")
    if layouts is None:
        legacy_keys = {"manifestPath", "lockPath", "updateGraphPath"}
        legacy_missing = sorted(legacy_keys - config.keys())
        if legacy_missing:
            raise ReleaseError(
                "config must define non-empty layouts[] or legacy keys: "
                + ", ".join(sorted(legacy_keys))
            )
        config["layouts"] = [
            {
                "name": "configured",
                "manifestPath": config["manifestPath"],
                "lockPath": config["lockPath"],
                "updateGraphPath": config["updateGraphPath"],
                "validatorPath": config.get("validatorPath", "tools/harness/validate.py"),
            }
        ]
    elif not isinstance(layouts, list) or not layouts:
        raise ReleaseError("config layouts must be a non-empty array")

    names: set[str] = set()
    normalized = []
    for index, layout in enumerate(config["layouts"]):
        checked = _validate_layout(layout, index)
        if checked["name"] in names:
            raise ReleaseError(f"config layout name is duplicated: {checked['name']}")
        names.add(checked["name"])
        normalized.append(dict(checked))
    config["layouts"] = normalized

    mirrors = config.get("updateGraphMirrors", [])
    if not isinstance(mirrors, list) or any(
        not isinstance(item, str) or not item.strip() for item in mirrors
    ):
        raise ReleaseError("config updateGraphMirrors must be a string array")
    if len(set(mirrors)) != len(mirrors):
        raise ReleaseError("config updateGraphMirrors must not contain duplicates")
    config["updateGraphMirrors"] = list(mirrors)
    return config


def resolve_layout(repo: Path, config: dict) -> dict:
    """Resolve exactly one complete release layout.

    Layout selection is intentionally based on the repository contents rather than
    release version. This lets maintainer-tools operate before and after a Harness
    namespace migration without teaching the release workflow version-specific rules.
    """

    complete: list[dict] = []
    diagnostics: list[str] = []

    for layout in config["layouts"]:
        required_paths = [
            layout["manifestPath"],
            layout["lockPath"],
            layout["updateGraphPath"],
            layout["validatorPath"],
        ]
        missing = [path for path in required_paths if not (repo / path).is_file()]
        if not missing:
            complete.append(layout)
            diagnostics.append(f"{layout['name']}: complete")
        else:
            diagnostics.append(f"{layout['name']}: missing {', '.join(missing)}")

    if len(complete) == 1:
        return complete[0]
    if len(complete) > 1:
        names = ", ".join(layout["name"] for layout in complete)
        raise ReleaseError(
            "ambiguous release layout: multiple complete layouts found: "
            f"{names}. Remove the obsolete layout before releasing."
        )
    raise ReleaseError("no complete release layout found; " + "; ".join(diagnostics))


def paths(repo: Path, config: dict) -> tuple[Path, Path, Path]:
    layout = resolve_layout(repo, config)
    return (
        repo / layout["manifestPath"],
        repo / layout["lockPath"],
        repo / layout["updateGraphPath"],
    )


def graph_mirror_paths(repo: Path, config: dict) -> list[Path]:
    """Configured compatibility mirrors canonical update graph."""
    return [repo / value for value in config.get("updateGraphMirrors", [])]


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

    mirror_errors = []
    for mirror_path in graph_mirror_paths(repo, config):
        if mirror_path.resolve() == graph_path.resolve():
            continue
        try:
            mirror = read_json(mirror_path)
        except ReleaseError as exc:
            mirror_errors.append(str(exc))
            continue
        if mirror != graph:
            mirror_errors.append(
                f"update graph mirror {mirror_path} differs from canonical {graph_path}"
            )

    lock_release = lock.get("release")
    lock_ref = lock.get("source", {}).get("ref") if isinstance(lock.get("source"), dict) else None
    graph_latest = graph.get("latest")
    expected_plain = manifest.removeprefix("v")

    errors = list(mirror_errors)
    if lock_release != expected_plain:
        errors.append(f"lock.release={lock_release!r}, expected {expected_plain!r}")
    if lock_ref != manifest:
        errors.append(f"lock.source.ref={lock_ref!r}, expected {manifest!r}")
    if graph_latest != manifest:
        errors.append(f"graph.latest={graph_latest!r}, expected {manifest!r}")
    if errors:
        raise ReleaseError("current release metadata is inconsistent: " + "; ".join(errors))
    return manifest, lock, graph


def command_layout(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    layout = resolve_layout(args.repo_dir, config)
    print(json.dumps(layout, ensure_ascii=False, sort_keys=True))


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
    # Release snapshot не может содержать self-pin на собственный commit:
    # SHA release commit появляется только после commit/merge/tag. Точный
    # source.commit записывается уже в project lock самим updater/adoption,
    # когда immutable tag существует и его OID можно доказать.
    source.pop("commit", None)
    write_json(lock_path, lock)

    transition_kind = getattr(args, "transition_kind", "standard")
    transition_reason = (getattr(args, "transition_reason", None) or "").strip()
    if transition_kind not in {"standard", "bridge"}:
        raise ReleaseError("transition kind must be standard or bridge")
    if transition_kind == "bridge" and not transition_reason:
        raise ReleaseError("bridge transition requires non-empty reason")
    if transition_kind == "standard" and transition_reason:
        raise ReleaseError("standard transition must not define bridge reason")

    graph["latest"] = target
    # reloadRequired и kind — осознанные свойства перехода между релизами.
    # Helper не пытается угадывать их по diff: maintainer задаёт значения явно
    # при Prepare Harness Release.
    edge = {
        "from": current,
        "to": target,
        "kind": transition_kind,
        "reloadRequired": bool(args.reload_required),
    }
    if transition_kind == "bridge":
        edge["reason"] = transition_reason
    graph["transitions"].append(edge)
    validate_graph(graph)
    write_json(graph_path, graph)
    for mirror_path in graph_mirror_paths(args.repo_dir, config):
        if mirror_path.resolve() == graph_path.resolve():
            continue
        if not mirror_path.is_file():
            raise ReleaseError(f"missing update graph mirror: {mirror_path}")
        write_json(mirror_path, graph)

    resulting, _, _ = state(args.repo_dir, config)
    if resulting != target:
        raise ReleaseError(f"postcondition failed: expected {target}, got {resulting}")
    print(f"prepared {current} -> {target}")


def command_verify(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    current, lock, graph = state(args.repo_dir, config)
    if current != args.version:
        raise ReleaseError(f"metadata points to {current}, expected {args.version}")
    source = lock.get("source")
    if not isinstance(source, dict):
        raise ReleaseError("lock.source must be an object")
    if "commit" in source:
        raise ReleaseError(
            "release snapshot lock must not contain source.commit; "
            "self commit OID does not exist until the release commit is created"
        )
    incoming = [edge for edge in graph["transitions"] if edge.get("to") == args.version]
    if not incoming:
        raise ReleaseError(f"release {args.version} has no incoming update transition")
    print(f"release metadata verified: {args.version}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--config", type=Path, required=True)
    sub = root.add_subparsers(dest="command", required=True)
    for name, handler in (
        ("layout", command_layout),
        ("current", command_current),
        ("prepare", command_prepare),
        ("verify", command_verify),
    ):
        cmd = sub.add_parser(name)
        cmd.add_argument("--repo-dir", type=Path, required=True)
        if name in {"prepare", "verify"}:
            cmd.add_argument("--version", required=True)
        if name == "prepare":
            cmd.add_argument(
                "--reload-required",
                action="store_true",
                help="Пометить новый release transition как требующий reload updater/runtime.",
            )
            cmd.add_argument(
                "--transition-kind",
                choices=["standard", "bridge"],
                default="standard",
                help="Тип нового перехода update graph.",
            )
            cmd.add_argument(
                "--transition-reason",
                help="Обязательная причина для bridge transition.",
            )
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
