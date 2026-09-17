"""Rule-based acceptance (auto-accept) tests.

Regression cover for the wf-nexusarchive-0917-01 latency incident
(2026-09-17, lessons §61): every `agent_done` event queued behind a single
coordinator LLM turn. Non-gate acceptance waits alone consumed ~2.6h of an
8h workflow (requirements 75min, implementation 42min, test 33min), and the
coordinator's long verification turns additionally blocked gate events.

Contract under test (services/herdr-controller.py):
1. `try_auto_accept` completes a non-gate node task when `verify-baseline`
   reports controlled file changes (TASK_CHANGED); gate nodes
   (test/review/wrapup) and change-less tasks keep the coordinator path.
2. `HERDR_AUTO_ACCEPT=0` disables the fast path entirely.
3. The done-event handler invokes the fast path before any coordinator
   prompt; on success it finalizes and never prompts the coordinator.
"""

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))


def _load_controller(name="ctrl_auto_accept_test"):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(
            name, str(HERDR_ROOT / "services" / "herdr-controller.py")
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BASELINE_CHANGED = (
    "TASK_CHANGED:\n"
    "- [tracked] src/app.py\n"
    'HERDR_BASELINE_RESULT={"task_id": "t1", "baseline_match": false, '
    '"changes": [{"type": "tracked", "path": "src/app.py"}]}\n'
)
BASELINE_MATCH = (
    "BASELINE_MATCH\n"
    'HERDR_BASELINE_RESULT={"task_id": "t1", "baseline_match": true, "changes": []}\n'
)


def _task(task_id="t1", node="implementation", status="agent_done"):
    return {
        "task_id": task_id,
        "workflow_id": "wf-1",
        "node": node,
        "stage": node,
        "status": status,
    }


class AutoAcceptUnitTest(unittest.TestCase):
    def setUp(self):
        self.ctrl = _load_controller()

    def _run(self, node, baseline_output, env=None, status="completed"):
        task = _task(node=node)
        env = env or {}
        with patch.object(self.ctrl, "get_task", return_value=task), \
             patch.object(
                 self.ctrl, "workflow_config_for",
                 return_value={"nodes": [{"id": node}]},
             ), \
             patch.object(
                 self.ctrl.subprocess, "run",
                 return_value=subprocess.CompletedProcess(
                     [], 0, baseline_output, ""
                 ),
             ) as run_mock, \
             patch.object(
                 self.ctrl, "set_task_status", return_value=True
             ) as set_status, \
             patch.dict("os.environ", env):
            result = self.ctrl.try_auto_accept("t1")
        return result, set_status, run_mock

    def test_non_gate_with_changes_is_completed(self):
        result, set_status, _ = self._run("implementation", BASELINE_CHANGED)
        self.assertTrue(result)
        set_status.assert_called_once_with("t1", "completed")

    def test_requirements_and_plan_are_non_gate(self):
        for node in ("requirements", "plan"):
            with self.subTest(node=node):
                result, set_status, _ = self._run(node, BASELINE_CHANGED)
                self.assertTrue(result)
                set_status.assert_called_once()

    def test_gate_nodes_never_auto_accept(self):
        for node in ("test", "review", "wrapup"):
            with self.subTest(node=node):
                result, set_status, run_mock = self._run(node, BASELINE_CHANGED)
                self.assertFalse(result)
                set_status.assert_not_called()
                run_mock.assert_not_called()

    def test_baseline_match_falls_back_to_coordinator(self):
        result, set_status, _ = self._run("implementation", BASELINE_MATCH)
        self.assertFalse(result)
        set_status.assert_not_called()

    def test_env_switch_disables_fast_path(self):
        result, set_status, run_mock = self._run(
            "implementation", BASELINE_CHANGED, env={"HERDR_AUTO_ACCEPT": "0"}
        )
        self.assertFalse(result)
        set_status.assert_not_called()
        run_mock.assert_not_called()

    def test_non_agent_done_status_is_ignored(self):
        task = _task(status="working")
        with patch.object(self.ctrl, "get_task", return_value=task), \
             patch.object(self.ctrl, "set_task_status") as set_status:
            self.assertFalse(self.ctrl.try_auto_accept("t1"))
        set_status.assert_not_called()

    def test_raw_task_changed_marker_is_accepted(self):
        result, set_status, _ = self._run(
            "implementation", "TASK_CHANGED:\n- [tracked] a.py\n"
        )
        self.assertTrue(result)
        set_status.assert_called_once()


class DoneEventWiringTest(unittest.TestCase):
    """The done-event handler must take the fast path before any prompt."""

    def setUp(self):
        self.ctrl = _load_controller("ctrl_auto_accept_wiring_test")

    def _item(self):
        return {
            "task_id": "t1",
            "event_type": "done",
            "key": "t1:done",
        }

    def test_auto_accept_finalizes_without_prompting_coordinator(self):
        with patch.object(self.ctrl, "try_auto_accept", return_value=True), \
             patch.object(self.ctrl, "attention_clear"), \
             patch.object(self.ctrl, "finalize_completed_task") as finalize, \
             patch.object(self.ctrl, "coordinator_status") as coord_status, \
             patch.object(self.ctrl.subprocess, "run") as run_mock:
            self.ctrl._process_coordinator_item(self._item(), threading.Lock())

        finalize.assert_called_once_with("t1")
        coord_status.assert_not_called()
        run_mock.assert_not_called()

    def test_fallback_still_prompts_coordinator(self):
        with patch.object(self.ctrl, "try_auto_accept", return_value=False), \
             patch.object(self.ctrl, "get_task", return_value=_task()), \
             patch.object(self.ctrl, "coordinator_status", return_value="idle"), \
             patch.object(
                 self.ctrl, "coordinator_pane_for_workflow", return_value="w1:p1"
             ), \
             patch.object(self.ctrl, "build_coordinator_message", return_value="m"), \
             patch.object(
                 self.ctrl.subprocess,
                 "run",
                 return_value=subprocess.CompletedProcess([], 1, "", "no-ack"),
             ) as run_mock, \
             patch.object(self.ctrl, "wait_for_coordinator_decision") as decision, \
             patch.object(self.ctrl, "attention_note"), \
             patch.object(self.ctrl, "attention_get", return_value={}):
            self.ctrl._process_coordinator_item(self._item(), threading.Lock())

        run_mock.assert_called()
        decision.assert_not_called()


if __name__ == "__main__":
    unittest.main()
