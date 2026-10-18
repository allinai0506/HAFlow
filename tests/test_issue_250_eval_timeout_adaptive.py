"""Regression tests for Issue #250: herdr-loop eval timeout configuration and adaptive scaling.

Issue #250:
- herdr-loop eval single execution budget was hardcoded to 300s, while repo test suite
  takes ~753s -> exit 124 timeout.
- Solution: support explicit --timeout, environment variables (HERDR_LOOP_EVAL_TIMEOUT,
  HERDR_EVAL_TIMEOUT), GOAL.md contract, and adaptive budget scaling for large repos.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr.evaluator import (
    DEFAULT_EVALUATION_TIMEOUT,
    MAX_ADAPTIVE_EVALUATION_TIMEOUT,
    resolve_evaluation_timeout,
)


class EvalTimeoutResolutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.target_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_timeout_for_small_workspace(self):
        timeout = resolve_evaluation_timeout(self.target_dir)
        self.assertEqual(timeout, DEFAULT_EVALUATION_TIMEOUT)
        self.assertEqual(timeout, 300.0)

    def test_explicit_timeout_takes_highest_precedence(self):
        with patch.dict(os.environ, {"HERDR_LOOP_EVAL_TIMEOUT": "900"}):
            timeout = resolve_evaluation_timeout(self.target_dir, explicit_timeout=1200.0)
            self.assertEqual(timeout, 1200.0)

    def test_env_var_herdr_loop_eval_timeout(self):
        with patch.dict(os.environ, {"HERDR_LOOP_EVAL_TIMEOUT": "950"}):
            timeout = resolve_evaluation_timeout(self.target_dir)
            self.assertEqual(timeout, 950.0)

    def test_env_var_herdr_eval_timeout_fallback(self):
        with patch.dict(os.environ, {"HERDR_EVAL_TIMEOUT": "850"}, clear=True):
            timeout = resolve_evaluation_timeout(self.target_dir)
            self.assertEqual(timeout, 850.0)

    def test_goal_md_comment_timeout(self):
        loop_dir = self.target_dir / ".herdr-loop"
        loop_dir.mkdir(parents=True)
        goal_md = loop_dir / "GOAL.md"
        goal_md.write_text(
            "# Task Goal\n<!-- HERDR_EVAL_TIMEOUT: 1500 -->\n- **测试命令**: `pytest`\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {}, clear=True):
            timeout = resolve_evaluation_timeout(self.target_dir)
            self.assertEqual(timeout, 1500.0)

    def test_goal_md_markdown_line_timeout(self):
        loop_dir = self.target_dir / ".herdr-loop"
        loop_dir.mkdir(parents=True)
        goal_md = loop_dir / "GOAL.md"
        goal_md.write_text(
            "# Task Goal\n- **评估超时**: 1400\n- **测试命令**: `pytest`\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {}, clear=True):
            timeout = resolve_evaluation_timeout(self.target_dir)
            self.assertEqual(timeout, 1400.0)

    def test_adaptive_scaling_for_large_test_suites(self):
        tests_dir = self.target_dir / "tests"
        tests_dir.mkdir(parents=True)
        # Create 100 dummy test files
        for i in range(100):
            (tests_dir / f"test_mod_{i}.py").write_text("# dummy test\n", encoding="utf-8")

        with patch.dict(os.environ, {}, clear=True):
            timeout = resolve_evaluation_timeout(self.target_dir)
            # 100 test files * 4.0s = 400.0s > default 300.0s
            self.assertEqual(timeout, 400.0)

    def test_adaptive_scaling_caps_at_maximum(self):
        tests_dir = self.target_dir / "tests"
        tests_dir.mkdir(parents=True)
        # Create 600 dummy test files
        for i in range(600):
            (tests_dir / f"test_mod_{i}.py").write_text("# dummy test\n", encoding="utf-8")

        with patch.dict(os.environ, {}, clear=True):
            timeout = resolve_evaluation_timeout(self.target_dir)
            self.assertEqual(timeout, MAX_ADAPTIVE_EVALUATION_TIMEOUT)
            self.assertEqual(timeout, 1800.0)
