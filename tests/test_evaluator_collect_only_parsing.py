"""Regression tests for evaluator test output parsing.

Specifically guards against misparsing pytest --collect-only output where test names
contain embedded summary strings (e.g. `4015 passed in 3.00s`, `FAIL`, `Tests  1 passed (1)`),
which caused Recovery Op 372067 in workflow wf-haflow-1101-01.
"""
from pathlib import Path
import pytest
from herdr.evaluator import calculate_metrics, is_converged, parse_test_output


def test_collect_only_with_embedded_summaries_in_test_names():
    """Diagnostic Report 372067: collect-only output with embedded summary strings.

    Test names contain `4015 passed in 3.00s`, `FAIL`, `Tests  1 passed (1)`, etc.
    Collect-only exit code is 0.
    Must return (0, 0, []) instead of fabricating 36145 passes and 4 fake failures.
    """
    output = (
        "tests/test_evaluator_execution_status.py::test_nonzero_exit_cannot_converge_after_green_summary[1-4015 passed in 3.00s]\n"
        "tests/test_evaluator_execution_status.py::test_nonzero_exit_cannot_converge_after_green_summary[124-4015 passed in 3.00s]\n"
        "tests/test_evaluator_ansi_test_output.py::test_colored_passing_title_remains_passing[\u5c55\u793a FAIL \u72b6\u6001-\u2713-Tests  1 passed (1)]\n"
        "tests/test_evaluator_ansi_test_output.py::test_colored_passing_title_remains_passing[\u5c55\u793a FAIL \u72b6\u6001-\u2713-Tests: 1 passed, 1 total]\n"
        "tests/test_evaluator_ansi_test_output.py::test_colored_passing_title_remains_passing[\u5c55\u793a FAIL \u72b6\u6001-\u221a-Tests  1 passed (1)]\n"
        "tests/test_evaluator_ansi_test_output.py::test_colored_passing_title_remains_passing[\u5c55\u793a FAIL \u72b6\u6001-\u221a-Tests: 1 passed, 1 total]\n"
        "tests/test_evaluator_ansi_test_output.py::test_true_colored_failure_retains_clean_name[Tests  1 failed | 1 passed (2)]\n"
        "\n"
        "6816 tests collected in 7.15s\n"
    )
    passed, total, failing = parse_test_output(output, 0)
    assert passed == 0
    assert total == 0
    assert failing == []


def test_real_execution_with_tricky_test_names_counts_accurately():
    """During real execution, test names with summary substrings must not inflate counts or add fake failures."""
    output = (
        "tests/test_evaluator_execution_status.py::test_nonzero_exit_cannot_converge_after_green_summary[1-4015 passed in 3.00s] PASSED\n"
        "tests/test_evaluator_ansi_test_output.py::test_colored_passing_title_remains_passing[\u5c55\u793a FAIL \u72b6\u6001-\u2713-Tests  1 passed (1)] PASSED\n"
        "\n"
        "============================== 2 passed in 0.15s ==============================\n"
    )
    passed, total, failing = parse_test_output(output, 0)
    assert passed == 2
    assert total == 2
    assert failing == []


def test_real_execution_with_genuine_failure_and_tricky_names():
    """Genuine failures must be captured even when other tests have tricky names."""
    output = (
        "tests/test_evaluator_ansi_test_output.py::test_colored_passing_title_remains_passing[\u5c55\u793a FAIL \u72b6\u6001-\u2713-Tests  1 passed (1)] PASSED\n"
        "FAILED tests/test_real.py::test_broken - AssertionError: expected True but got False\n"
        "\n"
        "========================= 1 failed, 1 passed in 0.20s =========================\n"
    )
    passed, total, failing = parse_test_output(output, 1)
    assert passed == 1
    assert total == 2
    assert failing == ["tests/test_real.py::test_broken"]


def test_broken_collect_only_remains_failure():
    """Collection error must not be silenced by collect-only handling."""
    output = (
        "ERROR collecting tests/test_syntax_error.py\n"
        "Interrupted: 1 error during collection\n"
        "=========================== 1 error in 0.05s ===========================\n"
    )
    passed, total, failing = parse_test_output(output, 2)
    assert passed == 0
    assert total >= 1
    assert "collecting tests/test_syntax_error.py" in failing


def test_mixed_test_runners_both_accounted():
    """Mixed runner outputs in the same run must aggregate without cross-contamination."""
    output = (
        " PASS  src/components/Button.test.tsx\n"
        "Tests:       3 passed, 3 total\n"
        "\n"
        " ✓ test/calc.test.ts > sum\n"
        " Tests  2 passed (2)\n"
        "\n"
        "============================== 5 passed in 0.50s ==============================\n"
    )
    passed, total, failing = parse_test_output(output, 0)
    assert passed == 10  # 3 + 2 + 5
    assert total == 10
    assert failing == []


def test_real_diagnostic_log_from_workflow_file():
    """If the actual test.log from the workflow run exists on disk, test directly against it."""
    log_path = Path("/Users/user/.herdr-controller/clones/wf-haflow-1101-01-test-auto/.herdr-loop/logs/test.log")
    if log_path.exists():
        raw_output = log_path.read_text(encoding="utf-8", errors="replace")
        passed, total, failing = parse_test_output(raw_output, 0)
        assert passed == 0
        assert total == 0
        assert failing == []
        metrics = calculate_metrics(raw_output, 0)
        assert metrics.failing_tests == []
        assert metrics.passed_tests == 0
        assert metrics.total_tests == 0
