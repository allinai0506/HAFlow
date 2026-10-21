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

    def test_all_terminal_workflow_statuses_filter_failed_tasks(self):
        """Workflows in any terminal state (completed, failed, halted, closed, abandoned, archived, cancelled)
        must not leak their child tasks into alerts."""
        terminal_statuses = [
            "completed", "failed", "halted", "closed", "abandoned", "archived", "cancelled"
        ]
        fake_tasks = []
        fake_workflows = {}
        for idx, st in enumerate(terminal_statuses):
            wid = f"wf-term-{idx}"
            tid = f"task-failed-{idx}"
            fake_tasks.append({
                "task_id": tid,
                "workflow_id": wid,
                "project_id": "proj-1",
                "status": "failed",
                "failure_reason": f"error in {st} workflow",
            })
            fake_workflows[wid] = {"workflow_id": wid, "status": st}

        with patch("console.inventory.tasks", return_value=fake_tasks), \
             patch("console.inventory.workflows", return_value=fake_workflows), \
             patch("console.runtime.spaces", return_value=[]), \
             patch("console.runtime.service_status", return_value={}):
            ov = runtime.overview()
            self.assertEqual(ov["alerts"], [])

    def test_task_without_project_id_resolves_project_from_workflow(self):
        """Tasks created without explicit project_id (e.g. router failure tasks)
        must resolve project_id and project_name from the workflow."""
        fake_tasks = [
            {
                "task_id": "router-failure-task-1",
                "workflow_id": "wf-running-1",
                "status": "failed",
                "failure_reason": "router error",
            }
        ]
        fake_workflows = {
            "wf-running-1": {
                "workflow_id": "wf-running-1",
                "status": "running",
                "project_id": "proj-resolved-1",
                "project_name": "Project Resolved",
            }
        }
        with patch("console.inventory.tasks", return_value=fake_tasks), \
             patch("console.inventory.workflows", return_value=fake_workflows), \
             patch("console.runtime.spaces", return_value=[]), \
             patch("console.runtime.service_status", return_value={}):
            ov = runtime.overview()
            self.assertEqual(len(ov["alerts"]), 1)
            alert = ov["alerts"][0]
            self.assertEqual(alert["task_id"], "router-failure-task-1")
            self.assertEqual(alert["project_id"], "proj-resolved-1")
            self.assertEqual(alert["project_name"], "Project Resolved")


if __name__ == "__main__":
    unittest.main()
