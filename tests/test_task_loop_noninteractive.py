"""Automatic npm evaluation must finish without an interactive watch session."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_cli(name, filename):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / 'bin' / filename))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.mark.skipif(not shutil.which('npm'), reason='real npm not installed')
def test_default_npm_evaluation_finishes_without_watch(tmp_path, monkeypatch):
    monkeypatch.delenv('CI', raising=False)
    test = tmp_path / 'test.py'
    test.write_text("import os,time\nfrom pathlib import Path\n"
                    "Path('test-started').touch()\n"
                    "print('5 passed in 0.1s', flush=True)\n"
                    "if not os.environ.get('CI'): time.sleep(10)\n")
    command = shlex.quote(sys.executable) + ' ' + shlex.quote(str(test))
    (tmp_path / 'package.json').write_text(json.dumps({'scripts': {'test': command}}))
    task = load_cli('task_loop_noninteractive', 'herdr-task')
    task.auto_init_task_loop(tmp_path, 'noninteractive gate', [], node='implementation')
    original = subprocess.Popen.communicate
    def bounded(process, *args, **kwargs):
        if kwargs.get('timeout') == 300:
            kwargs['timeout'] = 3
        return original(process, *args, **kwargs)
    monkeypatch.setattr(subprocess.Popen, 'communicate', bounded)
    metrics, converged, _ = load_cli('loop_noninteractive', 'herdr-loop').run_evaluation(tmp_path)
    assert (tmp_path / 'test-started').exists()
    assert converged
    assert metrics.passed_tests == 5
    assert metrics.details['evaluation_exit_code'] == 0
    assert 'CI=1 npm test' in (tmp_path / '.herdr-loop' / 'GOAL.md').read_text()


def test_explicit_task_test_command_is_preserved(tmp_path):
    (tmp_path / 'package.json').write_text(json.dumps({'scripts': {'test': 'false'}}))
    command = "printf '2 passed in 0.1s\\n'"
    task = load_cli('task_loop_explicit', 'herdr-task')
    task.auto_init_task_loop(tmp_path, 'explicit module contract', [], test_cmd=command,
                             lint_cmd='true', node='implementation')
    metrics, converged, _ = load_cli('loop_explicit', 'herdr-loop').run_evaluation(tmp_path)
    assert converged and metrics.passed_tests == 2
    goal = (tmp_path / '.herdr-loop' / 'GOAL.md').read_text()
    assert command in goal
    assert 'CI=1 npm test' not in goal
