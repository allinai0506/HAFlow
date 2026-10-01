#!/opt/homebrew/bin/python3
"""Tests for Outer Loop and herdr-task verify-metrics integration."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))


class OuterLoopMetricsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-outer-loop-")
        self.clone_dir = Path(self.tmp.name) / "clone"
        self.clone_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_file = Path(self.tmp.name) / "tasks.json"

        # Initialize mock task
        self.task_id = "test-fix-task-001"
        self.task_data = {
            "tasks": [
                {
                    "task_id": self.task_id,
                    "workflow_id": "wf-test-01",
                    "stage": "implementation",
                    "node": "implementation",
                    "status": "completed",
                    "clone_path": str(self.clone_dir),
                    "goal": "修复多角色超管判定缺陷",
                    "acceptance_criteria": ["单元测试全过", "复现用例通过"],
                }
            ]
        }
        self.tasks_file.write_text(json.dumps(self.task_data, ensure_ascii=False))

    def tearDown(self):
        self.tmp.cleanup()

    def test_verify_metrics_missing_file(self):
        task_bin = HERDR_ROOT / "bin" / "herdr-task"
        env = os.environ.copy()
        env["TASKS_FILE"] = str(self.tasks_file)

        # Run verify-metrics before loop init (should fail with exit 3)
        res = subprocess.run(
            [str(task_bin), "verify-metrics", self.task_id],
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertEqual(res.returncode, 3)
        self.assertIn("missing", res.stdout.lower() + res.stderr.lower())

    def test_verify_metrics_if_present_missing(self):
        task_bin = HERDR_ROOT / "bin" / "herdr-task"
        env = os.environ.copy()
        env["TASKS_FILE"] = str(self.tasks_file)

        # With --if-present, missing metrics should skip cleanly (exit 0)
        res = subprocess.run(
            [str(task_bin), "verify-metrics", self.task_id, "--if-present"],
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("skipped", res.stdout.lower())

    def test_verify_metrics_score_threshold(self):
        task_bin = HERDR_ROOT / "bin" / "herdr-task"
        loop_bin = HERDR_ROOT / "bin" / "herdr-loop"
        env = os.environ.copy()
        env["TASKS_FILE"] = str(self.tasks_file)

        # Initialize loop in clone
        subprocess.run(
            [
                str(loop_bin), "init",
                "--dir", str(self.clone_dir),
                "--goal", "Fix multi-role issue",
                "--test-cmd", "python3 -c 'exit(0)'",
                "--repro-cmd", "python3 -c 'exit(1)'",  # repro fails initially
            ],
            check=True,
        )

        # First eval (repro fails -> score < 100)
        subprocess.run([str(loop_bin), "eval", "--dir", str(self.clone_dir)])

        # verify-metrics should fail threshold
        res_fail = subprocess.run(
            [str(task_bin), "verify-metrics", self.task_id, "--min-score", "99.9"],
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertEqual(res_fail.returncode, 5)
        self.assertIn("passed=False", res_fail.stdout)

        # Reconfigure evaluator to pass (simulating bug fixed)
        evaluator_sh = self.clone_dir / ".herdr-loop" / "EVALUATOR.sh"
        evaluator_sh.write_text("""#!/bin/bash
mkdir -p .herdr-loop/logs
echo "1 passed in 0.01s" > .herdr-loop/logs/test.log
# The successful replacement must provide this evaluation's quality/repro logs.
: > .herdr-loop/logs/lint.log
echo "1 passed in 0.01s" > .herdr-loop/logs/repro.log
echo "TEST_EXIT=0"
echo "LINT_EXIT=0"
echo "REPRO_EXIT=0"
exit 0
""")
        subprocess.run([str(loop_bin), "eval", "--dir", str(self.clone_dir)], check=True)

        # verify-metrics should now succeed
        res_pass = subprocess.run(
            [str(task_bin), "verify-metrics", self.task_id, "--min-score", "99.9"],
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertEqual(res_pass.returncode, 0)
        self.assertIn("passed=True", res_pass.stdout)
        self.assertIn("HERDR_METRICS_RESULT=", res_pass.stdout)


if __name__ == "__main__":
    unittest.main()
