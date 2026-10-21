import unittest
from unittest.mock import patch
from console import runtime


class TestConsoleOverviewAlerts(unittest.TestCase):
    def test_completed_workflow_failed_tasks_not_in_alerts(self):
        fake_tasks = [
            {
                "task_id": "impl-old-failed",
                "workflow_id": "wf-completed-1",
                "project_id": "proj-1",
                "status": "failed",
                "failure_reason": "timeout",
            },
            {
                "task_id": "impl-active-failed",
                "workflow_id": "wf-running-1",
                "project_id": "proj-1",
                "status": "failed",
                "failure_reason": "syntax error",
                "stage": "implementation",
                "agent": "codex",
                "pane_id": "w1:p1",
            },
        ]
        fake_workflows = {
            "wf-completed-1": {"workflow_id": "wf-completed-1", "status": "completed"},
            "wf-running-1": {"workflow_id": "wf-running-1", "status": "running"},
        }
        with patch("console.inventory.tasks", return_value=fake_tasks), \
             patch("console.inventory.workflows", return_value=fake_workflows), \
             patch("console.runtime.spaces", return_value=[]), \
             patch("console.runtime.service_status", return_value={}):
            ov = runtime.overview()
            alert_task_ids = [a["task_id"] for a in ov["alerts"]]
            self.assertNotIn("impl-old-failed", alert_task_ids)
            self.assertIn("impl-active-failed", alert_task_ids)
            active_alert = next(a for a in ov["alerts"] if a["task_id"] == "impl-active-failed")
            self.assertEqual(active_alert["stage"], "implementation")
            self.assertEqual(active_alert["agent"], "codex")
            self.assertIn("syntax error", active_alert["reason"])

    def test_superseded_failed_tasks_not_in_alerts(self):
        fake_tasks = [
            {
                "task_id": "impl-r1",
                "workflow_id": "wf-running-1",
                "project_id": "proj-1",
                "status": "failed",
                "failure_reason": "error 1",
            },
            {
                "task_id": "impl-r2",
                "workflow_id": "wf-running-1",
                "project_id": "proj-1",
                "status": "working",
                "supersedes": "impl-r1",
            },
        ]
        fake_workflows = {
            "wf-running-1": {"workflow_id": "wf-running-1", "status": "running"},
        }
        with patch("console.inventory.tasks", return_value=fake_tasks), \
             patch("console.inventory.workflows", return_value=fake_workflows), \
             patch("console.runtime.spaces", return_value=[]), \
             patch("console.runtime.service_status", return_value={}):
            ov = runtime.overview()
            alert_task_ids = [a["task_id"] for a in ov["alerts"]]
            self.assertNotIn("impl-r1", alert_task_ids)
            self.assertNotIn("impl-r2", alert_task_ids)
            self.assertEqual(len(ov["alerts"]), 0)


if __name__ == "__main__":
    unittest.main()
