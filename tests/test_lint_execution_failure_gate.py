"""Execution failures cannot be discounted as existing static-analysis debt."""
import json
import shlex
import subprocess
import sys

import pytest
from herdr.evaluator import (calculate_metrics, capture_lint_baseline, init_loop,
                            is_converged, read_baseline_lint, write_baseline_lint)
from tests.test_task_loop_noninteractive import ROOT, load_cli


@pytest.mark.parametrize('code', [124, 126, 127, 137, 143, -15])
@pytest.mark.parametrize('step', ['lint', 'type'])
def test_execution_failure_vetoes_even_a_contaminated_baseline(code, step):
    args = {step + '_output': 'tool execution failed', step + '_exit_code': code,
            'baseline_' + step + '_errors': 100}
    metrics = calculate_metrics('1 passed in 0.01s', 0, **args)
    assert not is_converged(metrics)
    assert metrics.composite_score < 100


@pytest.mark.parametrize('code', [1, 2])
def test_real_reported_static_debt_retains_delta_contract(code):
    metrics = calculate_metrics('1 passed in 0.01s', 0,
                                lint_output='2 problems (2 errors, 0 warnings)',
                                lint_exit_code=code, baseline_lint_errors=2)
    assert is_converged(metrics)


@pytest.mark.parametrize('command', ['herdr_nonexistent_linter_c28c', 'exit 126', 'exit 137'])
def test_failed_capture_never_publishes_fake_debt(tmp_path, command):
    loop = init_loop(tmp_path, 'execution status', test_cmd='true')
    with pytest.raises(RuntimeError):
        capture_lint_baseline(loop, command, tmp_path)
    assert not (loop / 'BASELINE_LINT.json').exists()
    assert read_baseline_lint(loop) == (0, 0)


@pytest.mark.parametrize('kind', ['missing', 'not_executable'])
def test_native_cli_fails_init_and_persists_nonconvergence_with_old_debt(tmp_path, kind):
    command = 'herdr_nonexistent_linter_c28c'
    if kind == 'not_executable':
        tool = tmp_path / 'not-executable'
        tool.write_text('#!/bin/sh\nexit 0\n')
        tool.chmod(0o600)
        command = shlex.quote(str(tool))
    result = subprocess.run([sys.executable, str(ROOT / 'bin/herdr-loop'), 'init',
                             '--dir', str(tmp_path), '--goal', 'lint must execute',
                             '--test-cmd', "printf '1 passed in 0.01s\\n'",
                             '--lint-cmd', command], cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    loop = tmp_path / '.herdr-loop'
    assert not (loop / 'BASELINE_LINT.json').exists()
    # Existing bad baseline from an earlier release must not fake-green.
    write_baseline_lint(loop, 100)
    cli = load_cli('c28c_loop_' + kind, 'herdr-loop')
    metrics, converged, _ = cli.run_evaluation(tmp_path)
    assert not converged
    snapshot = json.loads((loop / 'EVAL_DONE.json').read_text())
    assert not snapshot['converged']
    assert metrics.details['lint_exit_code'] == (127 if kind == 'missing' else 126)


def test_task_best_effort_warns_without_fake_baseline(tmp_path, capsys):
    task = load_cli('c28c_task', 'herdr-task')
    task.auto_init_task_loop(tmp_path, 'unavailable lint', [], test_cmd='true',
                            lint_cmd='herdr_nonexistent_linter_c28c', node='implementation')
    assert 'Failed to auto-init loop' in capsys.readouterr().out
    assert not (tmp_path / '.herdr-loop/BASELINE_LINT.json').exists()


def test_native_evaluation_of_old_polluted_baseline_is_blocked(tmp_path):
    loop = init_loop(tmp_path, 'old release baseline',
                     test_cmd="printf '1 passed in 0.01s\\n'",
                     lint_cmd='herdr_nonexistent_linter_c28c')
    write_baseline_lint(loop, 100)
    cli = load_cli('c28c_legacy_eval', 'herdr-loop')
    metrics, converged, _ = cli.run_evaluation(tmp_path)
    assert metrics.details['lint_exit_code'] == 127
    assert metrics.new_lint_errors == 0
    assert not converged
    assert not json.loads((loop / 'EVAL_DONE.json').read_text())['converged']


def test_actual_tool_exit_two_can_capture_reported_debt(tmp_path):
    command = "printf 'Found 2 errors\\n'; exit 2"
    loop = init_loop(tmp_path, 'typescript existing debt',
                     test_cmd="printf '1 passed in 0.01s\\n'", lint_cmd=command,
                     capture_baseline=True)
    assert read_baseline_lint(loop) == (2, 0)
    cli = load_cli('c28c_normal_type_debt', 'herdr-loop')
    _, converged, _ = cli.run_evaluation(tmp_path)
    assert converged
