"""Native initialization owns contract replacement and baseline publication together."""
from contextlib import contextmanager
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

import pytest
from herdr import evaluator
from herdr.evaluator import init_loop, read_baseline_lint, write_baseline_lint
from tests.test_task_baseline_process_cleanup import child_command, ready
from tests.test_task_loop_noninteractive import ROOT, load_cli

CLI = ROOT / 'bin/herdr-loop'


def init_argv(target, goal, lint):
    return [sys.executable, str(CLI), 'init', '--dir', str(target),
            '--goal', goal, '--test-cmd', "printf '5 passed in 0.01s\\n'",
            '--lint-cmd', lint]


@pytest.mark.parametrize('operation', ['init', 'eval'])
def test_native_init_keeps_contract_owned_through_baseline(tmp_path, operation):
    loop = init_loop(tmp_path, 'old contract', test_cmd='true')
    write_baseline_lint(loop, 5)
    release = tmp_path / 'release'
    lint = ("if mkdir baseline-owner 2>/dev/null; then touch ready; "
            f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.02; done; fi; "
            "printf '2 problems (2 errors, 0 warnings)\\n';exit 1")
    owner = subprocess.Popen(init_argv(tmp_path, 'original contract', lint),
                             cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready(tmp_path, owner)
        before = {name: (loop / name).read_bytes() if (loop / name).exists() else None
                  for name in ['GOAL.md', 'EVAL_DONE.json', 'BASELINE_LINT.json']}
        args = (init_argv(tmp_path, 'replacement contract', 'true') if operation == 'init'
                else [sys.executable, str(CLI), 'eval', '--dir', str(tmp_path)])
        competitor = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=10)
        assert competitor.returncode == 75, competitor.stdout + competitor.stderr
        assert before == {name: (loop / name).read_bytes() if (loop / name).exists() else None
                          for name in before}
    finally:
        release.touch()
        owner.communicate(timeout=10)
    assert owner.returncode == 0
    assert read_baseline_lint(loop) == (2, 0)


@pytest.mark.parametrize('ignore_term', [False, True])
def test_native_init_sigterm_cleans_its_baseline_descendants(tmp_path, ignore_term):
    command = child_command(tmp_path, ignore_term=ignore_term)
    process = subprocess.Popen(init_argv(tmp_path, 'owned native baseline', command),
                               cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready(tmp_path, process)
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=10)
        time.sleep(2.1 if ignore_term else .9)
        assert process.returncode == 143
        assert not (tmp_path / 'late-write').exists()
        assert not (tmp_path / '.herdr-loop/BASELINE_LINT.json').exists()
        recovered = subprocess.run(init_argv(tmp_path, 'recovered contract', 'true'),
                                   cwd=ROOT, capture_output=True, text=True, timeout=10)
        assert recovered.returncode == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=10)


def test_task_publishes_current_baseline_before_first_unlock(tmp_path, monkeypatch):
    loop = init_loop(tmp_path, 'old task', test_cmd='true')
    write_baseline_lint(loop, 5)
    original = evaluator.evaluation_lock
    visible_at_release = []

    @contextmanager
    def observed_lock(directory):
        with original(directory):
            yield
        visible_at_release.append(read_baseline_lint(directory))

    monkeypatch.setattr(evaluator, 'evaluation_lock', observed_lock)
    task = load_cli('task_atomic_init_baseline', 'herdr-task')
    task.auto_init_task_loop(tmp_path, 'new task', [], test_cmd='true',
                             lint_cmd="printf '2 problems (2 errors, 0 warnings)\\n';exit 1",
                             node='implementation')
    assert visible_at_release == [(2, 0)]
    assert read_baseline_lint(loop) == (2, 0)


def test_plain_reinitialization_invalidates_previous_contract_baseline(tmp_path):
    loop = init_loop(tmp_path, 'old contract', test_cmd='true')
    write_baseline_lint(loop, 5)
    init_loop(tmp_path, 'new contract', test_cmd='true')
    assert not (loop / 'BASELINE_LINT.json').exists()
    assert read_baseline_lint(loop) == (0, 0)


def test_task_with_no_lint_debt_does_not_inherit_previous_baseline(tmp_path):
    loop = init_loop(tmp_path, 'old contract', test_cmd='true')
    write_baseline_lint(loop, 5)
    task = load_cli('task_atomic_init_no_debt', 'herdr-task')
    task.auto_init_task_loop(tmp_path, 'new task', [], test_cmd='true', lint_cmd='true', node='implementation')
    assert not (loop / 'BASELINE_LINT.json').exists()
    assert read_baseline_lint(loop) == (0, 0)


def test_native_initialization_preserves_legitimate_existing_lint_debt(tmp_path):
    lint = "printf '2 problems (2 errors, 0 warnings)\\n';exit 1"
    result = subprocess.run(init_argv(tmp_path, 'normal debt', lint),
                            cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert read_baseline_lint(tmp_path / '.herdr-loop') == (2, 0)
    evaluate = subprocess.run([sys.executable, str(CLI), 'eval', '--dir', str(tmp_path)],
                              cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert evaluate.returncode == 0, evaluate.stdout + evaluate.stderr



def test_native_init_timeout_is_unsuccessful_and_cannot_reuse_old_debt(tmp_path):
    loop = init_loop(tmp_path, 'old contract', test_cmd='true')
    write_baseline_lint(loop, 5)
    command = child_command(tmp_path, ignore_term=True)
    wrapper = """import runpy,subprocess,sys,time
from pathlib import Path
original=subprocess.Popen.communicate
def bounded(process,*args,**kwargs):
    if kwargs.get('timeout')==120:
        target=Path(sys.argv[sys.argv.index('--dir')+1])
        deadline=time.monotonic()+10
        while not (target/'ready').exists() and time.monotonic()<deadline:
            assert process.poll() is None
            time.sleep(.01)
        assert (target/'ready').exists()
        kwargs['timeout']=.2
    return original(process,*args,**kwargs)
subprocess.Popen.communicate=bounded
sys.argv=sys.argv[1:]
runpy.run_path(sys.argv[0],run_name='__main__')
"""
    result = subprocess.run([sys.executable, '-c', wrapper] + init_argv(tmp_path, 'timed out contract', command)[1:],
                            cwd=ROOT, capture_output=True, text=True, timeout=10)
    time.sleep(2.1)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not (tmp_path / 'late-write').exists()
    assert not (loop / 'BASELINE_LINT.json').exists()
    assert read_baseline_lint(loop) == (0, 0)
