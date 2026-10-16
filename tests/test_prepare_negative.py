"""Regression для отказа Prepare до выдачи write-токена и создания branch/PR."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).parents[1]
GATE = "Require completed stable canary promotion"
MUTATIONS = ("Create scoped GitHub App token", "Prepare metadata",
             "Commit and push release branch", "Create release pull request")


def prepare_contract() -> tuple[str, list[str]]:
    """Связывает subprocess-тест с реальным workflow, без копии команды gate."""
    workflow = (ROOT / ".github/workflows/prepare-release.yml").read_text()
    prepare = workflow.split("  prepare:\n", 1)[1].split("\n  qualify:\n", 1)[0]
    steps = dict(re.findall(r"(?ms)^      - name: ([^\n]+)\n(.*?)(?=^      - name: |\Z)", prepare))
    names = list(steps)
    assert GATE in steps, "Prepare потерял обязательный gate"
    gate = steps[GATE]
    assert not re.search(r"(?m)^    (?:if|continue-on-error):", prepare), "Job не должен обходить отказ gate"
    assert not re.search(r"(?m)^        (?:if|continue-on-error|shell|working-directory):", gate), "Gate должен выполняться без обходов"
    commands = re.findall(r"(?m)^        run: (.+)$", gate)
    assert commands == ["python scripts/release_completion.py"], "Gate должен проверять последний stable и сохранять exit code"
    for name in MUTATIONS:
        assert name in steps, f"Не найден проверяемый шаг: {name}"
        assert names.index(GATE) < names.index(name), f"Mutation выполняется до gate: {name}"
        # GitHub Actions пропускает последующие шаги после failure по умолчанию.
        # always()/failure() и continue-on-error нарушат этот проверяемый контракт.
        assert not re.search(r"(?m)^        (?:if|continue-on-error):", steps[name]), f"Mutation обходит отказ gate: {name}"
    qualification = workflow.split("\n  qualify:\n", 1)[1]
    assert re.search(r"(?m)^    needs: prepare$", qualification), "Qualification должна зависеть от Prepare"
    assert not re.search(r"(?m)^    (?:if|continue-on-error):", qualification), "Qualification обходит отказ Prepare"
    return commands[0], list(MUTATIONS)


class PrepareNegativeRegressionTest(unittest.TestCase):
    def run_scenario(self, scenario: str) -> tuple[subprocess.CompletedProcess, list[str], list[list[str]]]:
        command, mutations = prepare_contract()
        with tempfile.TemporaryDirectory(prefix="prepare-regression-") as directory:
            temp = Path(directory)
            requests = temp / "requests.jsonl"
            markers = temp / "mutations.jsonl"
            # В subprocess запускается production helper. Подменён только gh;
            # fixture не умеет обращаться к сети или выполнять GitHub mutations.
            gh = temp / "gh"
            gh.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " +
                          shlex.quote(str(ROOT / "tests/fixtures/release_completion_api.py")) + ' "$@"\n')
            gh.chmod(0o755)
            (temp / "python").symlink_to(sys.executable)
            script = command + "\n"
            for name in mutations:
                script += "printf '%s\\n' " + shlex.quote(json.dumps(name)) + " >> " + shlex.quote(str(markers)) + "\n"
            env = dict(os.environ, PATH=str(temp) + os.pathsep + os.environ["PATH"],
                       PREPARE_TEST_SCENARIO=scenario, PREPARE_TEST_REQUESTS=str(requests),
                       GH_TOKEN="", GITHUB_TOKEN="", PYTHONDONTWRITEBYTECODE="1")
            # -e -o pipefail совпадает с fail-fast shell GitHub Actions.
            result = subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
                                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
            observed = [json.loads(line) for line in markers.read_text().splitlines()] if markers.exists() else []
            calls = [json.loads(line) for line in requests.read_text().splitlines()] if requests.exists() else []
            self.assertTrue(calls, "Тест должен выполнить настоящий helper и gh boundary")
            self.assertTrue(all(call[0] == "api" for call in calls))
            return result, observed, calls

    def test_stale_baseline_blocks_before_all_mutations(self):
        result, mutations, calls = self.run_scenario("stale-baseline")
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertIn("Canary main ещё не закреплён", payload["reason"])
        self.assertEqual(mutations, [])
        self.assertTrue(any("harness.lock.json" in call[1] for call in calls))

    def test_unmerged_promotion_blocks_even_with_current_baseline(self):
        result, mutations, calls = self.run_scenario("unmerged-promotion")
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertIn("Нет единственного влитого promotion PR", payload["reason"])
        self.assertEqual(mutations, [])
        self.assertTrue(any("manifest.yaml" in call[1] for call in calls))
        self.assertTrue(any("/pulls?state=closed" in call[1] for call in calls))

    def test_accepted_baseline_reaches_all_mutation_markers(self):
        # Положительный контроль не позволяет принять сломанный fixture или
        # безусловный отказ helper за доказательство корректной блокировки.
        result, mutations, _ = self.run_scenario("accepted")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "COMPLETE")
        self.assertEqual(mutations, list(MUTATIONS))


if __name__ == "__main__":
    unittest.main()
