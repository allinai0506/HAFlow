"""Suite-scale smoke default regressions (§197 leftover ①).

wf-haflow-1024-02 measured reality: the full serial suite needs ~16h (the old
wrapper died at 1800s having reached 3%), xdist is known-flaky in CI, and the
controller dispatch never passes ``--test-cmd`` — so the auto-selected bare
``pytest`` made every inner loop on a CI-scale repo structurally
non-convergent, however honest the receipts became after PR #344.

INV-EVAL-04: the auto-init default must fit its own evaluation budget. When a
cheap collection probe shows the suite cannot plausibly fit, the default
becomes a collection smoke (``pytest -q --collect-only``) that is explicitly
labelled non-authoritative in the GOAL contract — correctness verification
stays with in-pane targeted runs and the test gate. Explicit ``--test-cmd``
contracts and repos under the threshold keep today's behaviour.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr.evaluator import (
    LOOP_DIR_NAME,
    apply_suite_scale_policy,
    init_loop,
    probe_suite_size,
    suite_smoke_min_tests,
)


class SuiteSmokeMinTestsTest(unittest.TestCase):
    def test_default_threshold(self):
        self.assertEqual(suite_smoke_min_tests(), 5000)

    def test_env_override(self):
        with patch.dict(os.environ, {"HERDR_LOOP_SMOKE_MIN_TESTS": "42"}):
            self.assertEqual(suite_smoke_min_tests(), 42)

    def test_invalid_env_falls_back_to_default(self):
        with patch.dict(os.environ, {"HERDR_LOOP_SMOKE_MIN_TESTS": "bogus"}):
            self.assertEqual(suite_smoke_min_tests(), 5000)


class ProbeSuiteSizeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        (self.repo / "tests").mkdir()
        (self.repo / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _test_file(self, name, body):
        (self.repo / "tests" / name).write_text(body)

    def test_counts_collected_tests(self):
        self._test_file("test_a.py", "def test_one(): pass\ndef test_two(): pass\n")
        self._test_file("test_b.py", "def test_three(): pass\n")
        self.assertEqual(probe_suite_size(self.repo), 3)

    def test_broken_collection_returns_none(self):
        self._test_file(
            "test_broken.py",
            "import nonexistent_module_xyz\ndef test_x(): pass\n",
        )
        self.assertIsNone(probe_suite_size(self.repo))

    def test_runner_failure_returns_none(self):
        def broken_runner(cmd, timeout):
            raise subprocess.TimeoutExpired(cmd, timeout)

        self.assertIsNone(probe_suite_size(self.repo, runner=broken_runner))

    def test_unparseable_output_returns_none(self):
        def empty_runner(cmd, timeout):
            return "pytest: unknown option -- not-a-flag\n"

        self.assertIsNone(probe_suite_size(self.repo, runner=empty_runner))


class ApplySuiteScalePolicyTest(unittest.TestCase):
    def test_oversized_default_becomes_collection_smoke(self):
        result = apply_suite_scale_policy("pytest", 6449)
        self.assertEqual(result["command"], "pytest -q --collect-only")
        self.assertEqual(result["mode"], "collect_smoke")

    def test_sized_suite_keeps_full_run(self):
        result = apply_suite_scale_policy("pytest", 4999)
        self.assertEqual(result["command"], "pytest")
        self.assertEqual(result["mode"], "run")

    def test_unknown_size_keeps_full_run(self):
        result = apply_suite_scale_policy("pytest", None)
        self.assertEqual(result["mode"], "run")

    def test_explicit_contract_never_rewritten(self):
        result = apply_suite_scale_policy("pytest -q tests/test_only.py", 99999)
        self.assertEqual(result["command"], "pytest -q tests/test_only.py")
        self.assertEqual(result["mode"], "run")

    def test_env_threshold_moves_boundary(self):
        with patch.dict(os.environ, {"HERDR_LOOP_SMOKE_MIN_TESTS": "10"}):
            self.assertEqual(apply_suite_scale_policy("pytest", 10)["mode"], "collect_smoke")
            self.assertEqual(apply_suite_scale_policy("pytest", 9)["mode"], "run")


class SmokeGoalContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.target = Path(self.tmp.name) / "repo"
        self.target.mkdir()
        (self.target / "tests").mkdir()
        (self.target / "tests" / "test_a.py").write_text("def test_ok(): pass\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_smoke_mode_labels_goal_non_authoritative(self):
        loop_dir = init_loop(
            target_dir=self.target,
            goal="demo",
            test_cmd="pytest -q --collect-only",
            lint_cmd="true",
            capture_baseline=False,
            test_mode="collect_smoke",
        )
        goal = (loop_dir / "GOAL.md").read_text(encoding="utf-8")
        self.assertIn("收集冒烟", goal)
        self.assertIn("test 门禁", goal)
        # The typed repro marker must stay the last line (existing invariant).
        self.assertTrue(goal.splitlines()[-1].startswith("<!-- HERDR_REPRO_REQUIRED"))

    def test_run_mode_has_no_smoke_label(self):
        loop_dir = init_loop(
            target_dir=self.target,
            goal="demo",
            test_cmd="pytest",
            lint_cmd="true",
            capture_baseline=False,
        )
        self.assertNotIn("收集冒烟", (loop_dir / "GOAL.md").read_text(encoding="utf-8"))


class CollectOnlyParseTest(unittest.TestCase):
    """A clean collect-only receipt is healthy but records zero execution."""

    def test_clean_collection_is_zero_evidence_not_failure(self):
        from herdr.evaluator import parse_test_output

        output = "6449 tests collected in 2.09s\n"
        self.assertEqual(parse_test_output(output, 0), (0, 0, []))

    def test_broken_collection_stays_honest_failure(self):
        from herdr.evaluator import parse_test_output

        output = (
            "ERROR collecting tests/test_broken.py\n"
            "Interrupted: 1 error during collection\n"
            "no tests collected, 1 error in 0.07s\n"
        )
        passed, total, failing = parse_test_output(output, 2)
        self.assertEqual(passed, 0)
        self.assertGreaterEqual(total, 1)
        self.assertTrue(failing)


class AutoInitSmokeChainTest(unittest.TestCase):
    """Real chain: auto-init probes the clone and switches the default."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clone = Path(self.tmp.name) / "clone"
        (self.clone / "tests").mkdir(parents=True)
        (self.clone / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
        (self.clone / "tests" / "test_a.py").write_text("def test_ok(): pass\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _init(self):
        from herdr.task_cli_launch import auto_init_task_loop

        auto_init_task_loop(str(self.clone), "goal text", ["- do"], node="craft")

    def test_small_suite_keeps_full_pytest(self):
        self._init()
        script = (self.clone / LOOP_DIR_NAME / "EVALUATOR.sh").read_text(encoding="utf-8")
        self.assertIn('test.log" pytest\n', script)
        self.assertNotIn("collect-only", script)

    def test_below_threshold_env_switches_to_smoke(self):
        with patch.dict(os.environ, {"HERDR_LOOP_SMOKE_MIN_TESTS": "1"}):
            self._init()
        loop_dir = self.clone / LOOP_DIR_NAME
        script = (loop_dir / "EVALUATOR.sh").read_text(encoding="utf-8")
        self.assertIn("pytest -q --collect-only", script)
        goal = (loop_dir / "GOAL.md").read_text(encoding="utf-8")
        self.assertIn("收集冒烟", goal)


if __name__ == "__main__":
    unittest.main()
