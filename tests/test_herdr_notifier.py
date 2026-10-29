"""Tests for services/herdr-notifier.py notify dispatch, deep-link URL building, and fallback."""

import shutil
import unittest
from unittest.mock import MagicMock, patch
import importlib.machinery
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_notifier():
    path = ROOT / "services" / "herdr-notifier.py"
    spec = importlib.util.spec_from_loader(
        "herdr_notifier_test",
        importlib.machinery.SourceFileLoader("herdr_notifier_test", str(path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestHerdrNotifier(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.notifier = _load_notifier()

    def test_notify_uses_terminal_notifier_when_available(self):
        with patch.object(self.notifier.shutil, "which", return_value="/opt/homebrew/bin/terminal-notifier"), \
             patch.object(self.notifier.subprocess, "run") as mock_run:
            self.notifier.notify(
                "Title",
                "Sub",
                "Msg",
                url="http://127.0.0.1:8765/?workflow_id=wf-1&task_id=t-1",
            )
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            self.assertTrue(cmd[0].endswith("terminal-notifier"))
            self.assertIn("-title", cmd)
            self.assertIn("Title", cmd)
            self.assertIn("-subtitle", cmd)
            self.assertIn("Sub", cmd)
            self.assertIn("-message", cmd)
            self.assertIn("Msg", cmd)
            self.assertIn("-open", cmd)
            self.assertIn("http://127.0.0.1:8765/?workflow_id=wf-1&task_id=t-1", cmd)
            self.assertIn("-sound", cmd)
            self.assertIn("Glass", cmd)

    def test_notify_falls_back_to_osascript_when_terminal_notifier_missing(self):
        with patch.object(self.notifier.shutil, "which", return_value=None), \
             patch.object(self.notifier.subprocess, "run") as mock_run:
            self.notifier.notify(
                "Title",
                "Sub",
                "Msg",
                url="http://127.0.0.1:8765/?workflow_id=wf-1&task_id=t-1",
            )
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            self.assertEqual(cmd[0], "osascript")
            self.assertEqual(cmd[1], "-e")
            self.assertIn('display notification "Msg"', cmd[2])
            self.assertIn('with title "Title"', cmd[2])
            self.assertIn('subtitle "Sub"', cmd[2])

    def test_build_console_url_helper(self):
        url_task = self.notifier.build_console_url(workflow_id="wf-test", task_id="task-123")
        self.assertIn("http://127.0.0.1:8765/", url_task)
        self.assertIn("workflow_id=wf-test", url_task)
        self.assertIn("task_id=task-123", url_task)

        url_wf = self.notifier.build_console_url(workflow_id="wf-test")
        self.assertIn("http://127.0.0.1:8765/?workflow_id=wf-test", url_wf)
        self.assertNotIn("task_id=", url_wf)

    def test_scan_notifies_finalize_escalated_and_dedups(self):
        tasks_data = {
            "tasks": [
                {
                    "task_id": "wf-1-impl",
                    "workflow_id": "wf-1",
                    "project_name": "my-project",
                    "agent": "qodercli",
                    "pane_id": "w1:p1",
                    "status": "completed",
                    "finalize_escalated": True,
                    "finalize_escalate_reason": "retry_exhausted",
                }
            ]
        }
        with patch.object(self.notifier, "load", return_value=tasks_data), \
             patch.object(self.notifier, "save") as mock_save, \
             patch.object(self.notifier, "notify") as mock_notify:
            # First scan with initialized state (prior un-escalated)
            initial_state = {
                "initialized": True,
                "last_status": {"wf-1-impl": "completed"},
                "last_escalated": {},
                "completed_workflows": [],
            }
            new_state = self.notifier.scan(initial_state)
            mock_notify.assert_called_once()
            args = mock_notify.call_args[0]
            self.assertEqual(args[0], "Herdr Factory · 终化升级（需人工）")
            self.assertIn("wf-1-impl", args[1])
            self.assertIn("retry_exhausted", args[2])
            self.assertTrue(new_state["last_escalated"].get("wf-1-impl"))

            # Second scan: same escalated state, must NOT notify again (deduped)
            mock_notify.reset_mock()
            new_state = self.notifier.scan(new_state)
            mock_notify.assert_not_called()

            # Third scan: escalation cleared
            tasks_data["tasks"][0]["finalize_escalated"] = False
            new_state = self.notifier.scan(new_state)
            mock_notify.assert_not_called()
            self.assertNotIn("wf-1-impl", new_state["last_escalated"])


if __name__ == "__main__":
    unittest.main()
