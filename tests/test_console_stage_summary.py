"""Regression tests for console stage_summary superseded handling.

The 2026-09-12 bug: a superseded task fell through the stage_summary
if/else chain to 'mixed', which the UI rendered as 处理中 even though every
task on the stage was terminal and the controller considered it complete.
"""

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def load_console():
    path = ROOT / "console" / "herdr_factory_console.py"
    spec = importlib.util.spec_from_loader(
        "herdr_console_stage_summary_test",
        importlib.machinery.SourceFileLoader(
            "herdr_console_stage_summary_test", str(path)
        ),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestStageSummarySuperseded(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.module = load_console()

    def test_superseded_task_does_not_make_stage_mixed(self):
        tasks = [
            {"task_id": "t1", "stage": "plan", "status": "cleaned"},
            {"task_id": "t2", "stage": "plan", "status": "superseded",
             "superseded_by": "t3"},
            {"task_id": "t3", "stage": "plan", "status": "cleaned"},
        ]
        summary = self.module.stage_summary(tasks, "plan")
        self.assertEqual(summary["status"], "cleaned")
        self.assertEqual(summary["count"], 2)
        self.assertEqual(len(summary["tasks"]), 3)

    def test_cleaned_with_superseded_by_is_excluded(self):
        tasks = [
            {"task_id": "t1", "stage": "plan", "status": "cleaned",
             "superseded_by": "t2"},
            {"task_id": "t2", "stage": "plan", "status": "cleaned"},
        ]
        summary = self.module.stage_summary(tasks, "plan")
        self.assertEqual(summary["status"], "cleaned")
        self.assertEqual(summary["count"], 1)

    def test_all_superseded_stage_is_marked_superseded(self):
        tasks = [
            {"task_id": "t1", "stage": "plan", "status": "superseded",
             "superseded_by": "t2"},
        ]
        summary = self.module.stage_summary(tasks, "plan")
        self.assertEqual(summary["status"], "superseded")
        self.assertEqual(summary["count"], 0)

    def test_empty_stage_still_waiting(self):
        summary = self.module.stage_summary([], "plan")
        self.assertEqual(summary["status"], "waiting")
        self.assertEqual(summary["count"], 0)

    def test_committed_tasks_make_stage_cleaned(self):
        tasks = [
            {"task_id": "impl-1", "stage": "implementation", "status": "committed"},
            {"task_id": "impl-2", "stage": "implementation", "status": "committed"},
        ]
        summary = self.module.stage_summary(tasks, "implementation")
        self.assertEqual(summary["status"], "cleaned")
        self.assertEqual(summary["count"], 2)

    def test_completed_task_statuses_make_stage_cleaned(self):
        tasks = [
            {"task_id": "t1", "stage": "implementation", "status": "completed"},
            {"task_id": "t2", "stage": "implementation", "status": "integrated"},
            {"task_id": "t3", "stage": "implementation", "status": "cleanup_ready"},
            {"task_id": "t4", "stage": "implementation", "status": "cleaned"},
        ]
        summary = self.module.stage_summary(tasks, "implementation")
        self.assertEqual(summary["status"], "cleaned")
        self.assertEqual(summary["count"], 4)

    def test_human_status_covers_new_statuses(self):
        for status, expected in (
            ("superseded", "已取代"),
            ("in_progress", "运行中"),
            ("empty", "无任务"),
        ):
            with self.subTest(status=status):
                self.assertIn(
                    f"{status}:'{expected}'",
                    self.module.HTML_TEMPLATE,
                )


if __name__ == "__main__":
    unittest.main()
