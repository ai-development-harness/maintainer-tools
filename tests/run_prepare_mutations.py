"""Проверяет чувствительность regression-тестов Prepare в одноразовой копии."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).parents[1]
WORKFLOW = ".github/workflows/prepare-release.yml"
INPUTS = (WORKFLOW, "scripts/release_completion.py", "tests/test_prepare_negative.py",
          "tests/fixtures/release_completion_api.py")
EXPECTED_TESTS = {
    "test_accepted_baseline_reaches_all_mutation_markers",
    "test_stale_baseline_blocks_before_all_mutations",
    "test_unmerged_promotion_blocks_even_with_current_baseline",
}

# Дочерний процесс исполняет неизменённые regression-тесты и возвращает
# структурированный результат: ошибки импорта, skipped и отсутствие тестов не PASS.
CHILD = '''
import importlib.util
import json
import sys
import unittest

spec = importlib.util.spec_from_file_location("prepare_negative", "tests/test_prepare_negative.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
suite = unittest.defaultTestLoader.loadTestsFromModule(module)
def ids(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from ids(test)
        else:
            yield test.id().rsplit(".", 1)[-1]
names = list(ids(suite))
result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
print(json.dumps({"names": names, "testsRun": result.testsRun,
    "failures": [(test.id(), trace) for test, trace in result.failures],
    "errors": len(result.errors), "skipped": len(result.skipped),
    "expectedFailures": len(result.expectedFailures),
    "unexpectedSuccesses": len(result.unexpectedSuccesses)}))
sys.exit(0 if result.wasSuccessful() else 1)
'''


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def block(text: str, name: str) -> re.Match:
    matches = list(re.finditer(
        rf"(?ms)^      - name: {re.escape(name)}\n.*?(?=^      - name: |\Z)", text))
    require(len(matches) == 1, f"Нужен единственный шаг: {name}")
    return matches[0]


def mutations(original: str) -> list[tuple[str, str, str | None]]:
    gate = block(original, "Require completed stable canary promotion")
    removed = original[:gate.start()] + original[gate.end():]
    branch = block(removed, "Commit and push release branch")
    moved = removed[:branch.end()] + gate[0] + removed[branch.end():]
    lines = gate[0].splitlines(keepends=True)
    ignored = original[:gate.start()] + lines[0] + "        continue-on-error: true\n" + "".join(lines[1:]) + original[gate.end():]
    # Меняются только целые YAML-шаги или одно поле: синтаксическая поломка
    # workflow не должна подменять проверку утраты защиты Prepare.
    return [
        ("baseline", original, None),
        ("gate-removed", removed, "Prepare потерял обязательный gate"),
        ("gate-after-branch", moved, "Mutation выполняется до gate:"),
        ("gate-continue-on-error", ignored, "Gate должен выполняться без обходов"),
        ("restored", original, None),
    ]


def check_case(root: Path, name: str, expected: str | None) -> None:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", GH_TOKEN="", GITHUB_TOKEN="")
    result = subprocess.run([sys.executable, "-c", CHILD], cwd=root, env=env,
                            capture_output=True, text=True, timeout=60)
    try:
        report = json.loads(result.stdout)
        require(set(report["names"]) == EXPECTED_TESTS and report["testsRun"] == 3,
                "Изменился набор regression-тестов; требуется явная сверка покрытия")
        require(not any(report[k] for k in ("errors", "skipped", "expectedFailures", "unexpectedSuccesses")),
                "Ошибки или пропущенные тесты не подтверждают чувствительность")
        if expected is None:
            require(result.returncode == 0 and not report["failures"], "Исходные тесты должны проходить")
        else:
            require(result.returncode == 1 and len(report["failures"]) == 3,
                    "Мутация выжила: нужны три ожидаемых падения")
            require(all(trace.strip().splitlines()[-1].startswith("AssertionError: " + expected)
                        for _, trace in report["failures"]), "Тесты упали по другой причине")
    except (ValueError, KeyError, TypeError, IndexError, RuntimeError) as exc:
        print(result.stderr, file=sys.stderr)
        raise RuntimeError(f"{name}: {exc}") from exc
    print(f"{name}: PASS — " + ("3 теста проходят" if expected is None else "3 ожидаемых падения"), flush=True)


def main() -> int:
    before = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in INPUTS}
    try:
        with tempfile.TemporaryDirectory(prefix="prepare-mutations-") as directory:
            root = Path(directory)
            for path in INPUTS:
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / path, target)
            workflow = root / WORKFLOW
            original = workflow.read_text()
            for name, content, expected in mutations(original):
                workflow.write_text(content)
                check_case(root, name, expected)
        require(before == {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in INPUTS},
                "Исходные файлы изменились во время проверки")
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"Prepare mutation checks: FAIL — {exc}", file=sys.stderr)
        return 1
    print("Prepare mutation checks: PASS; исходные файлы не изменены")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
