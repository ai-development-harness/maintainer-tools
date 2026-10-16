"""Настоящий ancestry shell не оставляет auto-maintenance перед удалением clone."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).parents[1]


def script() -> str:
    workflow = (ROOT / '.github/workflows/release-qualification.yml').read_text()
    step = workflow.split('- name: Verify exact target SHA and branch ancestry\n', 1)[1].split('      - name:', 1)[0]
    return textwrap.dedent(step.split('        run: |\n', 1)[1])


class QualificationCleanupTest(unittest.TestCase):
    def run_step(self, *, remove_guard: bool = False, unrelated: bool = False):
        with tempfile.TemporaryDirectory(prefix='qualification-cleanup-') as tmp:
            root = Path(tmp); repo = root / 'source'; repo.mkdir()
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=str(root / 'empty-config'))
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], env=env, text=True).strip()
            git('init', '-q', '-b', 'main')
            git('config', 'user.name', 'Проверка'); git('config', 'user.email', 'test@example.invalid')
            (repo / 'file').write_text('данные', encoding='utf-8')
            git('add', 'file'); git('commit', '-qm', 'Начальное состояние')
            target = git('rev-parse', 'HEAD')
            if unrelated:
                git('checkout', '--orphan', 'unrelated', '-q')
                git('commit', '-qm', 'Независимое состояние')
                target = git('rev-parse', 'HEAD')
            binary = root / 'bin'; binary.mkdir()
            calls = root / 'fetch-calls'
            shim = binary / 'git-wrapper.py'
            shim.write_text('''import os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
real = os.environ['REAL_GIT']
if 'fetch' in args:
    prefix = args[:args.index('fetch')]
    for key, expected in [('gc.auto', '0'), ('maintenance.auto', 'false')]:
        result = subprocess.run([real, *prefix, 'config', '--get', key], capture_output=True, text=True)
        if result.returncode or result.stdout.strip() != expected:
            sys.exit(86)
    with open(os.environ['FETCH_CALLS'], 'a') as stream:
        stream.write('fetch\\n')
if 'remote' in args and 'add' in args:
    args[-1] = os.environ['LOCAL_SOURCE']
sys.exit(subprocess.call([real, *args]))
''', encoding='utf-8')
            (binary / 'git').write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(shim)) + ' "$@"\n')
            (binary / 'gh').write_text('#!/bin/sh\nprintf "%s\\n" "$TARGET_SHA"\n')
            (binary / 'git').chmod(0o755); (binary / 'gh').chmod(0o755)
            temporary = root / 'temporary'; temporary.mkdir()
            env.update(PATH=str(binary) + os.pathsep + os.environ['PATH'], REAL_GIT=shutil.which('git'),
                       FETCH_CALLS=str(calls), LOCAL_SOURCE=str(repo), TMPDIR=str(temporary),
                       TARGET_REPOSITORY='fixture/repo', TARGET_REF='main', TARGET_SHA=target, GH_TOKEN='fixture')
            body = script()
            if remove_guard:
                body = '\n'.join(line for line in body.splitlines() if 'config gc.auto' not in line and 'config maintenance.auto' not in line)
            result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', body], env=env,
                                    capture_output=True, text=True, timeout=30)
            count = len(calls.read_text().splitlines()) if calls.exists() else 0
            self.assertEqual(list(temporary.iterdir()), [], 'EXIT cleanup должен удалить временную копию')
            return result, count

    def test_fetch_disables_background_maintenance_before_cleanup(self):
        result, count = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(count, 2)

    def test_missing_guard_is_detected(self):
        result, count = self.run_step(remove_guard=True)
        self.assertEqual(result.returncode, 86)
        self.assertEqual(count, 0)

    def test_unrelated_sha_remains_blocked(self):
        result, count = self.run_step(unrelated=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not reachable', result.stderr)
        self.assertEqual(count, 2)
