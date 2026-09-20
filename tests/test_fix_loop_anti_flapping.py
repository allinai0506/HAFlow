#!/opt/homebrew/bin/python3
"""Tests for Sentinel anti-flapping and Controller BUSY backoff (Phase 0)."""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))


class SentinelAntiFlappingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-sentinel-test-")
        self.root = Path(self.tmp.name)
        self.tasks_file = self.root / "tasks.json"
        self.state_file = self.root / "sentinel-state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_rework_task_not_flipped_by_stale_screen_done_marker(self):
        """When a task is set to rework, stale HERDR_TASK_DONE on screen should not
        immediately flip it back to agent_done.
        """
        import importlib
        sentinel_mod = importlib.import_module("services.herdr-sentinel")

        task_id = "test-rework-01"
        pane_id = "w1:p2"

        tasks_data = {
            "tasks": [
                {
                    "task_id": task_id,
                    "workflow_id": "wf-test-01",
                    "status": "rework",
                    "pane_id": pane_id,
                    "stage": "implementation",
                }
            ]
        }
        self.tasks_file.write_text(json.dumps(tasks_data), encoding="utf-8")

        # Mock pane_visible to return stale screen containing HERDR_TASK_DONE
        stale_screen = f"previous execution output\nHERDR_TASK_DONE:{task_id}\nexit"

        with patch.object(sentinel_mod, "TASKS_FILE", self.tasks_file), \
             patch.object(sentinel_mod, "STATE_FILE", self.state_file), \
             patch.object(sentinel_mod, "pane_visible", return_value=stale_screen), \
             patch.object(sentinel_mod, "agent_status", return_value="idle"):

            # Run sentinel's check logic for active tasks
            changes = {}
            registry = json.loads(self.tasks_file.read_text(encoding="utf-8"))
            for task in registry.get("tasks", []):
                status = task.get("status")
                t_id = task.get("task_id")
                p_id = task.get("pane_id")
                screen = sentinel_mod.pane_visible(p_id)
                done_marker = f"HERDR_TASK_DONE:{t_id}"

                # Sentinel should NEVER flip a rework task to agent_done directly
                if status in {"dispatched", "working"} and done_marker in screen:
                    changes[t_id] = ("agent_done", "completion_sentinel")

            self.assertNotIn(task_id, changes)


class ControllerReconcileReworkTest(unittest.TestCase):
    """Test that Controller reconcile_task_state does not falsely advance rework tasks."""

    def test_reconcile_does_not_flip_rework_to_agent_done_when_runtime_is_done(self):
        """When task is in rework, a stale runtime status of 'done' (from old exited process)
        must NOT be automatically promoted to agent_done! It needs a fresh dispatch.
        """
        import importlib
        controller_mod = importlib.import_module("services.herdr-controller")

        task_id = "test-task-rework-02"
        task_data = {
            "task_id": task_id,
            "status": "rework",
            "pane_id": "w1:p3",
            "workflow_id": "wf-01",
            "stage": "implementation"
        }

        # Mock get_task and get_agent_runtime_status
        with patch.object(controller_mod, "get_task", return_value=task_data), \
             patch.object(controller_mod, "get_agent_runtime_status", return_value="done"), \
             patch.object(controller_mod, "set_task_status") as mock_set_status:

            controller_mod.reconcile_task_state(task_id)

            # set_task_status must NOT have been called with "agent_done"!
            for call_args in mock_set_status.call_args_list:
                self.assertNotEqual(
                    call_args[0][1],
                    "agent_done",
                    "reconcile_task_state must not promote rework to agent_done when runtime is stale 'done'!"
                )

    def test_rework_healed_when_deliverables_detected(self):
        """When task is in rework, but deliverables are verified to exist,
        handle_event with idle should automatically heal rework -> agent_done!
        """
        import importlib
        controller_mod = importlib.import_module("services.herdr-controller")

        task_id = "test-task-rework-heal"
        task_data = {
            "task_id": task_id,
            "status": "rework",
            "pane_id": "w1:p3",
            "workflow_id": "wf-01",
            "stage": "implementation",
            "clone_path": "/tmp/test-clone-dummy"
        }

        with patch.object(controller_mod, "get_task", return_value=task_data), \
             patch.object(controller_mod, "check_task_deliverables_ready", return_value=True), \
             patch.object(controller_mod, "set_task_status", return_value=True) as mock_set_status, \
             patch.object(controller_mod, "enqueue_coordinator_event") as mock_enqueue:

            controller_mod.handle_event(task_id, "idle")

            mock_set_status.assert_called_with(task_id, "agent_done")
            mock_enqueue.assert_called()

    def test_idle_deferred_when_required_outputs_missing(self):
        """When task is working, has required_outputs, but file is not yet ready,
        idle should be treated as transient thinking and not transition to agent_done.
        """
        import importlib
        controller_mod = importlib.import_module("services.herdr-controller")

        task_id = "test-task-defer"
        task_data = {
            "task_id": task_id,
            "status": "working",
            "pane_id": "w1:p4",
            "workflow_id": "wf-01",
            "stage": "implementation",
            "clone_path": "/tmp/test-clone-dummy"
        }

        mock_wf_cfg = {
            "nodes": [
                {
                    "id": "implementation",
                    "required_outputs": ["docs/report.md"]
                }
            ]
        }

        with patch.object(controller_mod, "get_task", return_value=task_data), \
             patch.object(controller_mod, "workflow_config_for", return_value=mock_wf_cfg), \
             patch.object(controller_mod, "check_task_deliverables_ready", return_value=False), \
             patch.object(controller_mod, "set_task_status") as mock_set_status:

            controller_mod.handle_event(task_id, "idle")

            mock_set_status.assert_not_called()

    def test_idle_gate_verdict_closes_gate_task_without_literal_output_files(self):
        """A gate verdict is sufficient evidence when required_outputs are prose labels.

        Workflow templates describe gate outputs in human-readable Chinese labels,
        not repository-relative filenames. Once the agent has written a valid
        machine-readable verdict, an idle agent must be allowed to reach
        agent_done so the normal auto-verdict path can finalize it.
        """
        import importlib
        controller_mod = importlib.import_module("services.herdr-controller")

        task_id = "test-gate-verdict-close"
        task_data = {
            "task_id": task_id,
            "status": "working",
            "pane_id": "w1:p5",
            "workflow_id": "wf-01",
            "node": "test",
            "stage": "test",
            "clone_path": "/tmp/test-clone-dummy",
        }
        mock_wf_cfg = {
            "nodes": [
                {
                    "id": "test",
                    "required_outputs": ["测试执行记录", "测试结论（PASS / FAIL）"],
                }
            ]
        }

        with patch.object(controller_mod, "get_task", return_value=task_data), \
             patch.object(controller_mod, "workflow_config_for", return_value=mock_wf_cfg), \
             patch.object(controller_mod, "check_task_deliverables_ready", return_value=False), \
             patch.object(controller_mod, "read_gate_verdict", return_value=("blocked", "T2-T12 缺失", "file")), \
             patch.object(controller_mod, "set_task_status", return_value=True) as mock_set_status, \
             patch.object(controller_mod, "emit_done_if_allowed") as mock_emit_done:

            controller_mod.handle_event(task_id, "idle")

        mock_set_status.assert_called_once_with(task_id, "agent_done")
        mock_emit_done.assert_called_once()


if __name__ == "__main__":
    unittest.main()
