"""Evaluator per-step budget regressions from wf-haflow-1024-02.

The 2026-10-24 incident: the generated EVALUATOR.sh ran a bare full-suite
``pytest`` (6405 items); the evaluation wrapper's own timeout (adaptive cap
1800s) killed the whole script mid-step, so TEST_EXIT/LINT_EXIT receipts never
emitted and ``evaluator_exit:124`` + ``missing_exit_receipt`` became blockers
no agent behaviour could ever clear — a structurally non-convergent loop that
silently ground until the sentinel watchdog killed it.

INV-EVAL-03: every generated EVALUATOR.sh bounds each step internally and
always emits its exit receipts; the wrapper timeout written into the GOAL
contract strictly exceeds the sum of the inner step budgets, so the wrapper is
the outer belt, not the only belt.
"""

from __future__ import annotations

import os
import re
import shlex
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr.evaluator import (
    LOOP_DIR_NAME,
    init_loop,
    resolve_evaluation_timeout,
    run_evaluation_command,
)

ROOT = Path(__file__).resolve().parent.parent


class EvaluatorStepBudgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.target = Path(self.tmp.name) / "repo"
        self.target.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _init(self, **env):
        test_cmd = env.pop("test_cmd", "pytest")
        with patch.dict(os.environ, env):
            loop_dir = init_loop(
                target_dir=self.target,
                goal="demo goal",
                acceptance="- [ ] green",
                test_cmd=test_cmd,
                lint_cmd="true",
                capture_baseline=False,
            )
        return loop_dir

    def test_script_carries_step_budgets_and_wrapper_margin(self):
        loop_dir = self._init(
            HERDR_LOOP_TEST_BUDGET="900",
            HERDR_LOOP_LINT_BUDGET="120",
        )
        script = (loop_dir / "EVALUATOR.sh").read_text(encoding="utf-8")
        self.assertIn("TEST_BUDGET=900", script)
        self.assertIn("LINT_BUDGET=120", script)
        self.assertIn("run_bounded", script)

        goal = (loop_dir / "GOAL.md").read_text(encoding="utf-8")
        marker = re.search(r"<!--\s*HERDR_EVAL_TIMEOUT:\s*([0-9.]+)\s*-->", goal)
        self.assertIsNotNone(marker, "GOAL contract must pin the wrapper timeout")
        wrapper = float(marker.group(1))
        self.assertGreaterEqual(wrapper, 900 + 120)

        resolved = resolve_evaluation_timeout(self.target)
        self.assertEqual(resolved, wrapper)

    def test_repro_marker_stays_last_line(self):
        loop_dir = self._init(
            HERDR_LOOP_TEST_BUDGET="900",
            HERDR_LOOP_LINT_BUDGET="120",
            HERDR_LOOP_REPRO_BUDGET="300",
        )
        # init_loop has no repro parameter exposed here; write the contract
        # the way the launcher does and re-check ordering invariants instead.
        goal_lines = (loop_dir / "GOAL.md").read_text(encoding="utf-8").splitlines()
        self.assertTrue(goal_lines[-1].startswith("<!-- HERDR_REPRO_REQUIRED"))

    def test_breached_step_emits_receipt_and_script_exits_zero(self):
        loop_dir = self._init(
            test_cmd="sleep 30",
            HERDR_LOOP_TEST_BUDGET="4",
            HERDR_LOOP_LINT_BUDGET="30",
        )
        script = loop_dir / "EVALUATOR.sh"
        exit_code, stdout, _stderr = run_evaluation_command(
            [str(script)], self.target, timeout=120
        )
        self.assertEqual(exit_code, 0, f"EVALUATOR.sh must survive step breaches: {stdout}")
        self.assertIn("TEST_EXIT=124", stdout)
        self.assertIn("LINT_EXIT=0", stdout)
        self.assertTrue((loop_dir / "logs" / "test.log").exists())

    def test_fast_step_passes_through_native_exit(self):
        loop_dir = self._init(
            test_cmd="true",
            HERDR_LOOP_TEST_BUDGET="30",
            HERDR_LOOP_LINT_BUDGET="30",
        )
        script = loop_dir / "EVALUATOR.sh"
        exit_code, stdout, _stderr = run_evaluation_command(
            [str(script)], self.target, timeout=120
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("TEST_EXIT=0", stdout)
        self.assertIn("LINT_EXIT=0", stdout)

    def test_failing_step_exit_is_preserved(self):
        loop_dir = self._init(
            test_cmd="sh -c 'exit 3'",
            HERDR_LOOP_TEST_BUDGET="30",
            HERDR_LOOP_LINT_BUDGET="30",
        )
        script = loop_dir / "EVALUATOR.sh"
        exit_code, stdout, _stderr = run_evaluation_command(
            [str(script)], self.target, timeout=120
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("TEST_EXIT=3", stdout)

    def test_pipeline_failure_preserves_pipefail_receipt(self):
        """Codex P1 (PR #344): the bounded step must keep bash pipefail.

        The pre-budget subshell inherited ``set -o pipefail``; a plain
        ``sh -c`` would let ``false | cat`` report success and converge on a
        failed upstream check.
        """
        loop_dir = self._init(
            test_cmd="false | cat",
            HERDR_LOOP_TEST_BUDGET="30",
            HERDR_LOOP_LINT_BUDGET="30",
        )
        script = loop_dir / "EVALUATOR.sh"
        exit_code, stdout, _stderr = run_evaluation_command(
            [str(script)], self.target, timeout=120
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("TEST_EXIT=1", stdout)
        self.assertNotIn("TEST_EXIT=0", stdout)

    def test_breached_step_kills_descendants_before_next_step(self):
        """Codex P2 (PR #344): a budget breach must not leak a live tree.

        Terminating only the step shell orphaned its children, which kept
        running through the lint/repro steps and mutated files mid-evaluation.
        """
        child = self.target / "child.py"
        child.write_text(
            "import time\ntime.sleep(4)\nopen('late-write', 'w').close()\n",
            encoding="utf-8",
        )
        loop_dir = self._init(
            test_cmd=f"python3 {shlex.quote(str(child))}; true",
            HERDR_LOOP_TEST_BUDGET="2",
            HERDR_LOOP_LINT_BUDGET="10",
        )
        script = loop_dir / "EVALUATOR.sh"
        exit_code, stdout, _stderr = run_evaluation_command(
            [str(script)], self.target, timeout=120
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("TEST_EXIT=124", stdout)
        self.assertFalse((self.target / "late-write").exists())
        time.sleep(3)
        self.assertFalse((self.target / "late-write").exists())


if __name__ == "__main__":
    unittest.main()
