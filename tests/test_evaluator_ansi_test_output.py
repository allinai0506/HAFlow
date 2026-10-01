"""Terminal decoration must not turn passing test titles into failures."""
import json
import shlex

import pytest
from herdr.evaluator import calculate_metrics, init_loop, is_converged, parse_test_output
from tests.test_task_loop_noninteractive import load_cli


@pytest.mark.parametrize('summary', ['Tests  1 passed (1)', 'Tests: 1 passed, 1 total'])
@pytest.mark.parametrize('marker', ['✓', '√'])
@pytest.mark.parametrize('title', ['展示 FAIL 状态', 'literal ✕ icon'])
def test_colored_passing_title_remains_passing(summary, marker, title):
    output = '\x1b[32m' + marker + '\x1b[0m ' + title + '\n' + summary
    metrics = calculate_metrics(output, 0)
    assert metrics.failing_tests == []
    assert metrics.passed_tests == metrics.total_tests == 1
    assert metrics.composite_score == 100
    assert is_converged(metrics)


@pytest.mark.parametrize('summary', ['Tests  1 failed | 1 passed (2)', 'Tests: 1 failed, 1 passed, 2 total'])
def test_true_colored_failure_retains_clean_name(summary):
    output = '\x1b[32m✓\x1b[0m green FAIL title\n\x1b[31m✕\x1b[0m actual broken assertion\n' + summary
    passed, total, failing = parse_test_output(output, 1)
    assert (passed, total) == (1, 2)
    assert failing == ['actual broken assertion']
    assert not is_converged(calculate_metrics(output, 1))


def test_colored_green_summary_does_not_override_nonzero_exit():
    metrics = calculate_metrics('\x1b[32m✓\x1b[0m green FAIL title\nTests  1 passed (1)', 1)
    assert not is_converged(metrics)


def test_plain_passing_failure_word_title_remains_passing():
    assert is_converged(calculate_metrics('✓ plain FAIL title\nTests  1 passed (1)', 0))


def test_native_runner_colored_log_converges_and_preserves_original_bytes(tmp_path):
    output = '\x1b[32m✓\x1b[0m 展示 FAIL 状态\nTests  1 passed (1)\n'
    loop = init_loop(tmp_path, 'actual ANSI test output',
                     test_cmd='printf %s ' + shlex.quote(output), lint_cmd='true')
    cli = load_cli('c15b_color', 'herdr-loop')
    metrics, converged, _ = cli.run_evaluation(tmp_path)
    assert converged
    assert metrics.failing_tests == []
    assert (loop / 'logs/test.log').read_bytes() == output.encode()
    assert json.loads((loop / 'EVAL_DONE.json').read_text())['converged']
