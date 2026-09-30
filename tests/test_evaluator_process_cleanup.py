"""Owned evaluator descendants cannot outlive timeout, interrupt or return."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

import pytest
from herdr.evaluator import init_loop

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'bin' / 'herdr-loop'


def load_loop():
    loader = importlib.machinery.SourceFileLoader('loop_cleanup', str(CLI))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def setup_runner(tmp_path, *, wait=True, ignore_term=False):
    directory = init_loop(tmp_path, 'owned descendants', test_cmd='true')
    child = tmp_path / 'child.py'
    child.write_text('import signal,time\nfrom pathlib import Path\n' +
                     ('signal.signal(signal.SIGTERM, signal.SIG_IGN)\n' if ignore_term else '') +
                     "Path('ready').touch()\ntime.sleep(0.8)\nPath('late-write').touch()\n")
    script = directory / 'EVALUATOR.sh'
    script.write_text('#!/bin/bash\nmkdir -p .herdr-loop/logs\n' +
                      "printf '5 passed in 0.1s\\n' > .herdr-loop/logs/test.log\n" +
                      ': > .herdr-loop/logs/lint.log\necho TEST_EXIT=0\necho LINT_EXIT=0\n' +
                      shlex.quote(sys.executable) + ' ' + shlex.quote(str(child)) +
                      ' >/dev/null 2>&1 &\nwhile [ ! -f ready ]; do sleep 0.01; done\n' + ('wait\n' if wait else 'exit 0\n'))
    script.chmod(0o755)
    return directory


def await_ready(tmp_path, process):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if (tmp_path / 'ready').exists():
            return
        assert process.poll() is None, process.communicate()
        time.sleep(0.01)
    raise AssertionError('child did not reach controlled ready barrier')


@pytest.mark.parametrize('ignore_term', [False, True])
def test_timeout_cleans_owned_children_and_persists_rejection(tmp_path, monkeypatch, ignore_term):
    directory = setup_runner(tmp_path, ignore_term=ignore_term)
    loop = load_loop()
    original = subprocess.Popen.communicate
    def short_timeout(process, *args, **kwargs):
        if kwargs.get('timeout') == 300:
            await_ready(tmp_path, process)
            kwargs['timeout'] = 0.4
        return original(process, *args, **kwargs)
    monkeypatch.setattr(subprocess.Popen, 'communicate', short_timeout)
    metrics, converged, _ = loop.run_evaluation(tmp_path)
    assert (tmp_path / 'ready').exists()
    time.sleep(0.9)
    assert not (tmp_path / 'late-write').exists()
    assert not converged
    assert metrics.details['evaluation_exit_code'] == 124
    assert json.loads((directory / 'EVAL_DONE.json').read_text())['converged'] is False
    # No persistent lock ownership after cleanup.
    init_loop(tmp_path, 'timeout recovery', test_cmd='true')


@pytest.mark.parametrize('interrupt', [signal.SIGTERM, signal.SIGINT])
def test_cli_interrupt_cleans_children_before_releasing_owner(tmp_path, interrupt):
    setup_runner(tmp_path)
    process = subprocess.Popen([sys.executable, str(CLI), 'eval', '--dir', str(tmp_path)],
                               cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        await_ready(tmp_path, process)
        process.send_signal(interrupt)  # Only our own CLI child.
        process.communicate(timeout=10)
        time.sleep(0.9)
        assert not (tmp_path / 'late-write').exists()
        assert process.returncode != 0
        init_loop(tmp_path, 'interrupt recovery', test_cmd='true')
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


def test_normal_return_cleans_background_children_without_killing_unrelated_process(tmp_path):
    setup_runner(tmp_path, wait=False)
    sibling = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(10)'])
    try:
        _, converged, _ = load_loop().run_evaluation(tmp_path)
        time.sleep(0.9)
        assert not (tmp_path / 'late-write').exists()
        assert converged
        assert sibling.poll() is None
    finally:
        sibling.terminate()  # Only our independently owned control child.
        sibling.wait(timeout=10)


def test_completed_foreground_work_remains_valid(tmp_path):
    directory = init_loop(tmp_path, 'normal execution', test_cmd="printf '5 passed in 0.1s\\n'")
    metrics, converged, _ = load_loop().run_evaluation(tmp_path)
    assert converged and metrics.passed_tests == 5
    assert json.loads((directory / 'EVAL_DONE.json').read_text())['converged']


def test_live_group_permission_denial_is_not_ignored(monkeypatch):
    loop = load_loop()
    process = type('Owned', (), {'pid': 12345})()
    def denied(*args):
        raise PermissionError('controlled live denial')
    monkeypatch.setattr(loop.os, 'killpg', denied)
    monkeypatch.setattr(loop.subprocess, 'run', lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, '12345 S\n'))
    with pytest.raises(PermissionError):
        loop._signal_owned_group(process, signal.SIGTERM)
