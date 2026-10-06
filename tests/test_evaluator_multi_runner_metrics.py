#!/usr/bin/env python3
"""verify-metrics 受管用例计数必须覆盖全部 runner 口径（tech-debt #6）。

现场缺陷：评估器 first-match-wins 且只认 pytest/vitest/jest，后端
Maven Surefire/JUnit 输出（参数化实例：矩阵 98、后端 6905）完全不可见，
前端 4248 曾被误当作整体增量证明。
"""

import sys
from pathlib import Path

import pytest

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr.evaluator import parse_test_output  # noqa: E402


def test_surefire_results_line_is_counted():
    output = (
        "[INFO] Results:\n"
        "[INFO]\n"
        "[INFO] Tests run: 6905, Failures: 0, Errors: 0, Skipped: 98\n"
    )
    passed, total, failing = parse_test_output(output, 0)
    assert (passed, total, failing) == (6807, 6905, [])


def test_surefire_failures_reduce_passed_and_surface_exit(self=None):
    output = "[INFO] Tests run: 100, Failures: 3, Errors: 2, Skipped: 5\n"
    passed, total, _ = parse_test_output(output, 1)
    assert passed == 90
    assert total == 100


def test_frontend_and_backend_are_aggregated_not_first_match_wins():
    output = (
        " Tests  4248 passed (4248)\n"
        " Test Files  312 passed (312)\n"
        "[INFO] Results:\n"
        "[INFO] Tests run: 6905, Failures: 0, Errors: 0, Skipped: 98\n"
    )
    passed, total, failing = parse_test_output(output, 0)
    assert passed == 4248 + 6807
    assert total == 4248 + 6905
    assert failing == []


def test_surefire_per_class_lines_are_not_double_counted():
    output = (
        "[INFO] Tests run: 100, Failures: 0, Errors: 0, Skipped: 0, "
        "Time elapsed: 1.02 s - in com.x.ATest\n"
        "[INFO] Tests run: 200, Failures: 0, Errors: 0, Skipped: 0, "
        "Time elapsed: 2.40 s - in com.x.BTest\n"
        "[INFO] Results:\n"
        "[INFO] Tests run: 300, Failures: 0, Errors: 0, Skipped: 0\n"
    )
    passed, total, _ = parse_test_output(output, 0)
    assert (passed, total) == (300, 300)


def test_pytest_and_vitest_segments_are_summed():
    output = (
        "frontend:\n"
        " Tests  12 passed (12)\n"
        "backend:\n"
        "FAILED tests/test_backend.py::test_a - AssertionError: 1 != 2\n"
        "================= 5 failed, 100 passed in 1.20s =================\n"
    )
    passed, total, failing = parse_test_output(output, 1)
    assert passed == 112
    assert total == 117
    assert failing == ["tests/test_backend.py::test_a"]


def test_single_runner_logs_keep_legacy_semantics():
    output = "Tests  11 failed | 4248 passed (4259)\n"
    passed, total, _ = parse_test_output(output, 1)
    assert (passed, total) == (4248, 4259)


def test_unrecognised_output_falls_back_to_exit_code():
    passed, total, failing = parse_test_output("nothing here\n", 0)
    assert (passed, total) == (1, 1)
    passed, total, failing = parse_test_output("boom\n", 1)
    assert passed == 0
    assert total == 1
    assert failing
