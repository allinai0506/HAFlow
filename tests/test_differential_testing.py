import json
import os
import tempfile
import unittest
from pathlib import Path

from herdr.differential_testing import (
    DifferentialTestResult,
    compute_differential_verdict,
    extract_failing_tests,
    read_base_test_snapshot,
    write_base_test_snapshot,
)
from herdr.evaluator import (
    MetricVector,
    calculate_metrics,
    is_converged,
)


class TestDifferentialTesting(unittest.TestCase):
    def test_clean_pass_no_failures(self):
        result = compute_differential_verdict(
            candidate_failures=[],
            base_failures=[],
        )
        self.assertEqual(result.verdict, "pass")
        self.assertFalse(result.pre_existing_ignored)
        self.assertIsNone(result.warning)
        self.assertEqual(result.delta_failures, [])
        self.assertEqual(result.candidate_failures, [])
        self.assertEqual(result.base_failures, [])

    def test_pre_existing_failures_only_passes_with_warning(self):
        """When candidate only has failures that existed on base branch (Delta F = empty),
        verdict is 'pass' with warning 'pre_existing_ignored' and workflow is not blocked.
        """
        base_f = ["tests/test_a.py::test_old_bug", "tests/test_b.py::test_legacy_fail"]
        cand_f = ["tests/test_a.py::test_old_bug", "tests/test_b.py::test_legacy_fail"]

        result = compute_differential_verdict(
            candidate_failures=cand_f,
            base_failures=base_f,
        )
        self.assertEqual(result.verdict, "pass")
        self.assertTrue(result.pre_existing_ignored)
        self.assertEqual(result.warning, "pre_existing_ignored")
        self.assertEqual(result.delta_failures, [])
        self.assertEqual(sorted(result.pre_existing_failures), sorted(base_f))
        self.assertIn("pre_existing_ignored", result.to_dict()["warning"])

    def test_new_failure_introduced_blocks_delivery(self):
        """When candidate introduces new failures (Delta F != empty), verdict is 'blocked'."""
        base_f = ["tests/test_a.py::test_old_bug"]
        cand_f = ["tests/test_a.py::test_old_bug", "tests/test_c.py::test_new_regression"]

        result = compute_differential_verdict(
            candidate_failures=cand_f,
            base_failures=base_f,
        )
        self.assertEqual(result.verdict, "blocked")
        self.assertFalse(result.pre_existing_ignored)
        self.assertEqual(result.delta_failures, ["tests/test_c.py::test_new_regression"])
        self.assertEqual(result.pre_existing_failures, ["tests/test_a.py::test_old_bug"])

    def test_candidate_fixes_some_base_failures(self):
        """When candidate fixes a base failure and introduces none, verdict is 'pass'."""
        base_f = ["test_1", "test_2", "test_3"]
        cand_f = ["test_1"]  # fixed test_2 and test_3

        result = compute_differential_verdict(
            candidate_failures=cand_f,
            base_failures=base_f,
        )
        self.assertEqual(result.verdict, "pass")
        self.assertTrue(result.pre_existing_ignored)
        self.assertEqual(result.delta_failures, [])
        self.assertEqual(sorted(result.fixed_failures), ["test_2", "test_3"])

    def test_extract_failing_tests_from_pytest_output(self):
        output = """
============================= test session starts ==============================
collected 5 items

tests/test_mod.py .F.E.                                                  [100%]

=================================== FAILURES ===================================
_________________________________ test_feature _________________________________
FAILED tests/test_mod.py::test_feature - AssertionError: expected True
ERROR tests/test_mod.py::test_setup - RuntimeError: setup failed
=========================== short test summary info ============================
FAILED tests/test_mod.py::test_feature - AssertionError: expected True
ERROR tests/test_mod.py::test_setup - RuntimeError: setup failed
========================= 1 failed, 1 error, 3 passed in 0.42s =========================
"""
        failures = extract_failing_tests(output, exit_code=1)
        self.assertIn("tests/test_mod.py::test_feature", failures)
        self.assertIn("tests/test_mod.py::test_setup", failures)
        self.assertEqual(len(failures), 2)

    def test_extract_failing_tests_clean_output(self):
        output = """
============================= test session starts ==============================
collected 3 items

tests/test_mod.py ...                                                    [100%]
============================== 3 passed in 0.12s ===============================
"""
        failures = extract_failing_tests(output, exit_code=0)
        self.assertEqual(failures, [])

    def test_snapshot_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_path = Path(tmpdir) / "BASELINE_TEST.json"
            base_failures = ["test_foo::test_1", "test_bar::test_2"]
            metadata = {"base_ref": "origin/main", "commit": "abc1234567"}

            written = write_base_test_snapshot(snap_path, base_failures, metadata=metadata)
            self.assertTrue(written.exists())

            loaded = read_base_test_snapshot(snap_path)
            self.assertEqual(sorted(loaded["failing_tests"]), sorted(base_failures))
            self.assertEqual(loaded["metadata"]["base_ref"], "origin/main")

    def test_read_corrupt_or_missing_snapshot(self):
        missing = Path("/nonexistent/path/BASELINE_TEST.json")
        data = read_base_test_snapshot(missing)
        self.assertEqual(data["failing_tests"], [])

    def test_evaluator_integration_with_baseline_test_failures(self):
        """Test evaluator calculate_metrics and is_converged when baseline_failing_tests are present."""
        test_out = """
FAILED tests/test_old.py::test_legacy - AssertionError
========================= 1 failed, 4 passed in 0.20s =========================
"""
        # Scenario A: Baseline has the same failure -> Delta F is empty -> Converged!
        metrics = calculate_metrics(
            test_output=test_out,
            test_exit_code=1,
            baseline_failing_tests=["tests/test_old.py::test_legacy"],
        )
        self.assertEqual(metrics.new_failing_tests, [])
        self.assertTrue(metrics.details.get("pre_existing_ignored"))
        self.assertEqual(metrics.composite_score, 100.0)
        self.assertTrue(is_converged(metrics))

        # Scenario B: Baseline has NO failures -> Delta F has 1 failure -> Score penalized to 95, not converged
        metrics_blocked = calculate_metrics(
            test_output=test_out,
            test_exit_code=1,
            baseline_failing_tests=[],
        )
        self.assertEqual(metrics_blocked.new_failing_tests, ["tests/test_old.py::test_legacy"])
        self.assertFalse(metrics_blocked.details.get("pre_existing_ignored", False))
        self.assertLess(metrics_blocked.composite_score, 99.9)
        self.assertFalse(is_converged(metrics_blocked))

    def test_cli_differential_test_command(self):
        """End-to-end verification of bin/herdr-task differential-test CLI."""
        import subprocess
        cli = Path(__file__).resolve().parents[1] / "bin" / "herdr-task"

        with tempfile.TemporaryDirectory() as tmpdir:
            base_snap = Path(tmpdir) / "BASELINE_TEST.json"
            write_base_test_snapshot(base_snap, ["tests/test_foo.py::test_legacy"])

            # 1. Candidate with only pre-existing failure -> pass
            cand_out_file = Path(tmpdir) / "cand_pass.txt"
            cand_out_file.write_text(
                "FAILED tests/test_foo.py::test_legacy - AssertionError\n= 1 failed in 0.1s =\n",
                encoding="utf-8",
            )
            proc_pass = subprocess.run(
                [
                    str(cli), "differential-test",
                    "--candidate-output", str(cand_out_file),
                    "--base-snapshot", str(base_snap),
                    "--candidate-exit-code", "1",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc_pass.returncode, 0)
            self.assertIn("verdict=pass", proc_pass.stdout)
            self.assertIn("pre_existing_ignored", proc_pass.stdout)
            self.assertIn('"pre_existing_ignored": true', proc_pass.stdout)

            # 2. Candidate introducing new failure -> exit 1, blocked
            cand_fail_file = Path(tmpdir) / "cand_block.txt"
            cand_fail_file.write_text(
                "FAILED tests/test_bar.py::test_new - AssertionError\n= 1 failed in 0.1s =\n",
                encoding="utf-8",
            )
            proc_block = subprocess.run(
                [
                    str(cli), "differential-test",
                    "--candidate-output", str(cand_fail_file),
                    "--base-snapshot", str(base_snap),
                    "--candidate-exit-code", "1",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc_block.returncode, 1)
            self.assertIn("verdict=blocked", proc_block.stdout)
            self.assertIn("tests/test_bar.py::test_new", proc_block.stdout)

    def test_candidate_crash_with_nonzero_exit_not_masked(self):
        """When candidate test command exits non-zero but outputs no failing test names
        (e.g. runner crash, syntax error, or unparsed summary), it must NOT be masked as pass.
        """
        metrics = calculate_metrics(
            test_output="5 passed in 0.20s",
            test_exit_code=1,
            baseline_failing_tests=["tests/test_legacy.py::test_old"],
        )
        self.assertFalse(metrics.details.get("pre_existing_ignored", False))
        self.assertLessEqual(metrics.composite_score, 95.0)
        self.assertFalse(is_converged(metrics))

    def test_cli_differential_missing_file_fails_closed(self):
        """CLI must fail closed when candidate output file is missing."""
        import subprocess
        cli = Path(__file__).resolve().parents[1] / "bin" / "herdr-task"
        proc = subprocess.run(
            [
                str(cli), "differential-test",
                "--candidate-output", "/nonexistent/candidate_output.txt",
                "--base-output", "/nonexistent/base_output.txt",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
    def test_capture_git_base_test_snapshot(self):
        """Verify capture_git_base_test_snapshot runs test command and writes snapshot with head_commit."""
        from herdr.differential_testing import capture_git_base_test_snapshot
        repo_dir = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp_dir:
            snap_file = Path(tmp_dir) / "base_snapshot.json"
            res = capture_git_base_test_snapshot(
                repo_path=repo_dir,
                base_ref="HEAD",
                test_command="python3 -c \"import sys; print('FAILED tests/t.py::test_fail - AssertionError\\n1 failed in 0.05s'); sys.exit(1)\"",
                output_snapshot_path=snap_file,
            )
            self.assertIn("tests/t.py::test_fail", res["failing_tests"])
            self.assertTrue(snap_file.is_file())
            loaded = read_base_test_snapshot(snap_file)
            self.assertIn("tests/t.py::test_fail", loaded["failing_tests"])
            self.assertIsNotNone(loaded["metadata"].get("head_commit"))


if __name__ == "__main__":
    unittest.main()

