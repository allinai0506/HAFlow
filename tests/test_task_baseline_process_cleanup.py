"""The real auto-init baseline path owns its command and publication lifecycle."""
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

import pytest
from herdr.evaluator import init_loop, read_baseline_lint
from tests.test_task_loop_noninteractive import load_cli, ROOT


def child_command(tmp_path, *, ignore_term=False):
    child = tmp_path / 'baseline-child.py'
    child.write_text('from pathlib import Path\nimport time,signal\n' +
                     ('signal.signal(signal.SIGTERM,signal.SIG_IGN)\n' if ignore_term else '') +
                     "Path('ready').touch()\ntime.sleep(" + ("2" if ignore_term else ".8") + ")\nPath('late-write').touch()\n")
    return shlex.quote(sys.executable) + ' ' + shlex.quote(str(child)) + ' & wait'


def ready(tmp_path, process):
    end = time.monotonic() + 10
    while not (tmp_path / 'ready').exists() and time.monotonic() < end:
        assert process.poll() is None
        time.sleep(.01)
    assert (tmp_path / 'ready').exists()


@pytest.mark.parametrize('ignore_term', [False, True])
def test_baseline_timeout_cleans_descendants_without_fabricating_baseline(tmp_path, monkeypatch, capsys, ignore_term):
    command = child_command(tmp_path, ignore_term=ignore_term)
    original = subprocess.Popen.communicate
    def short(process, *a, **kw):
        if kw.get('timeout') == 120:
            ready(tmp_path, process)
            kw['timeout'] = .2
        return original(process, *a, **kw)
    monkeypatch.setattr(subprocess.Popen, 'communicate', short)
    task = load_cli('task_baseline_timeout', 'herdr-task')
    task.auto_init_task_loop(tmp_path, 'owned baseline', [], test_cmd='true', lint_cmd=command, node='implementation')
    time.sleep(2.1 if ignore_term else .9)
    assert not (tmp_path / 'late-write').exists()
    assert not (tmp_path / '.herdr-loop/BASELINE_LINT.json').exists()
    assert 'Failed to snapshot lint baseline' in capsys.readouterr().out
    init_loop(tmp_path, 'recovery', test_cmd='true')


def test_successful_baseline_command_keeps_observed_existing_debt(tmp_path):
    task = load_cli('task_baseline_success', 'herdr-task')
    task.auto_init_task_loop(tmp_path, 'normal baseline', [], test_cmd='true',
                             lint_cmd="printf '2 problems (2 errors, 0 warnings)\\n';exit 1", node='implementation')
    assert read_baseline_lint(tmp_path / '.herdr-loop') == (2, 0)


def baseline_process(tmp_path, command):
    code = """import importlib.machinery,importlib.util,sys
from pathlib import Path
loader=importlib.machinery.SourceFileLoader('task_baseline_cli',sys.argv[1]);spec=importlib.util.spec_from_loader(loader.name,loader);task=importlib.util.module_from_spec(spec);loader.exec_module(task)
task.auto_init_task_loop(Path(sys.argv[2]),'baseline signal',[],test_cmd='true',lint_cmd=sys.argv[3],node='implementation')
"""
    return subprocess.Popen([sys.executable, '-c', code, str(ROOT / 'bin/herdr-task'), str(tmp_path), command],
                               cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def test_cli_baseline_sigterm_cleans_before_releasing_namespace(tmp_path):
    command = child_command(tmp_path)
    process = baseline_process(tmp_path, command)
    try:
        ready(tmp_path, process)
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=10)
        time.sleep(.9)
        assert not (tmp_path / 'late-write').exists()
        assert process.returncode != 0
        assert not (tmp_path / '.herdr-loop/BASELINE_LINT.json').exists()
        init_loop(tmp_path, 'recover terminated baseline', test_cmd='true')
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


def test_baseline_capture_owns_inputs_until_publication(tmp_path):
    child = tmp_path / 'baseline-barrier.py'
    child.write_text("from pathlib import Path\nimport time\nPath('ready').touch()\n"
                     "while not Path('release').exists(): time.sleep(.01)\n"
                     "print('2 problems (2 errors, 0 warnings)')\nraise SystemExit(1)\n")
    command = shlex.quote(sys.executable) + ' ' + shlex.quote(str(child))
    first = baseline_process(tmp_path, command)
    try:
        ready(tmp_path, first)
        goal = tmp_path / '.herdr-loop/GOAL.md'
        before = goal.read_bytes()
        competing = subprocess.run([sys.executable, str(ROOT / 'bin/herdr-loop'), 'init',
                                    '--dir', str(tmp_path), '--goal', 'must not replace baseline inputs'],
                                   cwd=ROOT, capture_output=True, text=True, timeout=10)
        assert competing.returncode == 75
        assert goal.read_bytes() == before
    finally:
        (tmp_path / 'release').touch()
        first.communicate(timeout=10)
    assert first.returncode == 0
    assert read_baseline_lint(tmp_path / '.herdr-loop') == (2, 0)
