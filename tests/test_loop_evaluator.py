#!/opt/homebrew/bin/python3
"""Tests for Herdr Autonomous Evaluation Engine (herdr/evaluator.py)."""

import unittest
from pathlib import Path
import sys

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr.evaluator import (
    MetricVector,
    calculate_metrics,
    effective_defects,
    is_converged,
    parse_lint_output,
    parse_test_output,
    read_baseline_lint,
    render_evaluation_markdown,
    render_metrics_markdown,
    write_baseline_lint,
)


class EvaluatorTest(unittest.TestCase):
    def test_parse_pytest_output_success(self):
        output = """
============================= test session starts ==============================
rootdir: /Users/user/HAFlow
collected 5 items

test_sample.py .....                                                     [100%]

============================== 5 passed in 0.12s ===============================
"""
        passed, total, failing = parse_test_output(output, 0)
        self.assertEqual(passed, 5)
        self.assertEqual(total, 5)
        self.assertEqual(failing, [])

    def test_parse_pytest_output_failure(self):
        output = """
=================================== FAILURES ===================================
_________________________________ test_addition _________________________________
    def test_addition():
>       assert 1 + 1 == 3
E       assert 2 == 3

FAILED test_sample.py::test_addition - assert 2 == 3
=========================== short test summary info ============================
FAILED test_sample.py::test_addition - assert 2 == 3
========================= 1 failed, 4 passed in 0.15s ==========================
"""
        passed, total, failing = parse_test_output(output, 1)
        self.assertEqual(passed, 4)
        self.assertEqual(total, 5)
        self.assertIn("test_sample.py::test_addition", failing)

    def test_parse_vitest_output(self):
        output = """
 ✓ src/store/auth.test.ts (10 tests)
 ✕ src/store/roles.test.ts (2 tests | 1 failed)
   ✕ should handle multi-role array correctly

 Test Files  1 failed | 1 passed (2)
      Tests  1 failed | 11 passed (12)
   Start at  15:20:00
   Duration  412ms
"""
        passed, total, failing = parse_test_output(output, 1)
        self.assertEqual(passed, 11)
        self.assertEqual(total, 12)
        self.assertTrue(any("should handle multi-role" in f for f in failing))

    def test_parse_lint_output(self):
        eslint_out = "12 problems (2 errors, 10 warnings)"
        self.assertEqual(parse_lint_output(eslint_out, 1), 2)

        tsc_out = "Found 3 errors in 2 files."
        self.assertEqual(parse_lint_output(tsc_out, 1), 3)

        clean_out = ""
        self.assertEqual(parse_lint_output(clean_out, 0), 0)

    def test_calculate_metrics_full_pass(self):
        test_out = "10 passed in 0.1s"
        metrics = calculate_metrics(
            test_output=test_out,
            test_exit_code=0,
            lint_output="",
            lint_exit_code=0,
            type_output="",
            type_exit_code=0,
        )
        self.assertEqual(metrics.composite_score, 100.0)
        self.assertTrue(is_converged(metrics))

    def test_calculate_metrics_failing_defect(self):
        test_out = "1 failed, 9 passed in 0.1s\nFAILED test_foo.py::test_bar"
        metrics = calculate_metrics(
            test_output=test_out,
            test_exit_code=1,
            lint_output="Found 1 error",
            lint_exit_code=1,
        )
        self.assertLess(metrics.composite_score, 100.0)
        self.assertFalse(is_converged(metrics))
        self.assertIn("test_foo.py::test_bar", metrics.failing_tests)
        self.assertEqual(metrics.lint_errors, 1)

    def test_repro_test_weight(self):
        test_out = "5 passed in 0.1s"
        # Repro test failed
        metrics = calculate_metrics(
            test_output=test_out,
            test_exit_code=0,
            repro_output="FAILED repro_test.py::test_repro",
            repro_exit_code=1,
        )
        self.assertFalse(is_converged(metrics))
        self.assertEqual(metrics.repro, 0.0)

        # Repro test passed
        metrics_pass = calculate_metrics(
            test_output=test_out,
            test_exit_code=0,
            repro_output="1 passed in 0.01s",
            repro_exit_code=0,
        )
        self.assertTrue(is_converged(metrics_pass))
        self.assertEqual(metrics_pass.repro, 100.0)

    def test_render_markdown_outputs(self):
        metrics = MetricVector(
            correctness=80.0,
            quality=90.0,
            scope=100.0,
            repro=0.0,
            composite_score=68.0,
            passed_tests=4,
            total_tests=5,
            failing_tests=["test_feature.py::test_edge_case"],
            lint_errors=1,
            has_repro_test=True,
        )
        card = render_metrics_markdown(metrics, iteration=1, max_iterations=3)
        self.assertIn("量化评估指标卡", card)
        self.assertIn("68.0 / 100.0", card)

        eval_md = render_evaluation_markdown(metrics, iteration=1, max_iterations=3)
        self.assertIn("待修复阻断项", eval_md)
        self.assertIn("test_feature.py::test_edge_case", eval_md)

    def test_parse_unittest_output(self):
        # Passing unittest run
        pass_out = "Ran 7 tests in 0.003s\n\nOK"
        passed, total, failing = parse_test_output(pass_out, 0)
        self.assertEqual(passed, 7)
        self.assertEqual(total, 7)
        self.assertEqual(failing, [])

        # Failing unittest run
        fail_out = """
FAIL: test_foo (test_app.AppTest.test_foo)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "test_app.py", line 10, in test_foo
    self.assertEqual(1, 2)
AssertionError: 1 != 2

----------------------------------------------------------------------
Ran 4 tests in 0.005s

FAILED (failures=1)
"""
        passed, total, failing = parse_test_output(fail_out, 1)
        self.assertEqual(passed, 3)
        self.assertEqual(total, 4)
        self.assertEqual(failing, ["test_foo (test_app.AppTest.test_foo)"])

    def test_parse_lint_output_clean_on_exit_zero(self):
        # Even if warning or text contains the word 'error', exit_code=0 means success
        text = "Warning: deprecated API usage, may cause an Error in future versions."
        self.assertEqual(parse_lint_output(text, 0), 0)

    def test_baseline_debt_does_not_block_convergence(self):
        # T1 case: tests green, 2562 pre-existing lint, no new defects.
        metrics = calculate_metrics(
            test_output="1122 passed in 51.38s",
            test_exit_code=0,
            lint_output="Found 2562 errors.",
            lint_exit_code=1,
            baseline_lint_errors=2562,
        )
        self.assertEqual(metrics.lint_errors, 2562)
        self.assertEqual(metrics.new_lint_errors, 0)
        self.assertEqual(metrics.quality, 100.0)
        self.assertEqual(metrics.composite_score, 100.0)
        self.assertTrue(is_converged(metrics))

    def test_new_lint_still_blocks_convergence(self):
        metrics = calculate_metrics(
            test_output="1122 passed in 51.38s",
            test_exit_code=0,
            lint_output="Found 2563 errors.",
            lint_exit_code=1,
            baseline_lint_errors=2562,
        )
        self.assertEqual(metrics.new_lint_errors, 1)
        self.assertLess(metrics.composite_score, 100.0)
        self.assertFalse(is_converged(metrics))

    def test_no_baseline_preserves_absolute_gate(self):
        metrics = calculate_metrics(
            test_output="10 passed in 0.1s",
            test_exit_code=0,
            lint_output="Found 1 error",
            lint_exit_code=1,
        )
        self.assertLess(metrics.composite_score, 100.0)
        self.assertFalse(is_converged(metrics))

    def test_effective_defects_never_negative(self):
        self.assertEqual(effective_defects(2560, 2562), 0)
        self.assertEqual(effective_defects(2563, 2562), 1)
        self.assertEqual(effective_defects(0, 0), 0)

    def test_baseline_roundtrip_and_missing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            loop_dir = Path(tmp) / ".herdr-loop"
            self.assertEqual(read_baseline_lint(loop_dir), (0, 0))
            write_baseline_lint(loop_dir, 2562, 0)
            self.assertEqual(read_baseline_lint(loop_dir), (2562, 0))


if __name__ == "__main__":
    unittest.main()
