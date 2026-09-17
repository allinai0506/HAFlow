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
import tempfile
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


class GateVerdictUnitTest(unittest.TestCase):
    """Gate nodes: machine-readable verdict from report file / terminal."""

    def setUp(self):
        self.ctrl = _load_controller("ctrl_auto_verdict_test")
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-verdict-")
        self.clone = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _task(self, node="test"):
        return {
            "task_id": "t-gate",
            "workflow_id": "wf-1",
            "node": node,
            "stage": node,
            "status": "agent_done",
            "clone_path": str(self.clone),
            "pane_id": "w1:p9",
        }

    def _write_verdict_file(self, payload):
        path = self.clone / ".herdr" / "gate-verdict.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_read_from_file(self):
        self._write_verdict_file({"verdict": "blocked", "note": "D7 金额残留"})
        verdict, note, source = self.ctrl.read_gate_verdict(self._task())
        self.assertEqual(verdict, "blocked")
        self.assertEqual(note, "D7 金额残留")
        self.assertIn("file", source)

    def test_conflicting_signals_are_ambiguous(self):
        self._write_verdict_file({"verdict": "pass", "note": ""})
        with patch.object(
            self.ctrl, "_verdict_from_screen", return_value=("blocked", "x")
        ):
            verdict, _, _ = self.ctrl.read_gate_verdict(self._task())
        self.assertIsNone(verdict)

    def test_missing_signals_are_ambiguous(self):
        with patch.object(
            self.ctrl, "_verdict_from_screen", return_value=(None, "")
        ):
            verdict, _, _ = self.ctrl.read_gate_verdict(self._task())
        self.assertIsNone(verdict)

    def _run_verdict(self, verdict_source, node="test", env=None):
        task = self._task(node=node)
        with patch.object(self.ctrl, "get_task", return_value=task), \
             patch.object(
                 self.ctrl, "workflow_config_for",
                 return_value={"nodes": [{"id": node}]},
             ), \
             patch.object(
                 self.ctrl, "read_gate_verdict", return_value=verdict_source
             ), \
             patch.object(
                 self.ctrl.subprocess,
                 "run",
                 return_value=subprocess.CompletedProcess([], 0, "ok", ""),
             ) as run_mock, \
             patch.dict("os.environ", env or {}):
            return self.ctrl.try_auto_verdict("t-gate"), run_mock

    def test_pass_verdict_completes_gate_task(self):
        result, run_mock = self._run_verdict(("pass", "", "file"))
        self.assertTrue(result)
        cmd = run_mock.call_args[0][0]
        self.assertIn("set", cmd)
        self.assertIn("--verdict", cmd)
        self.assertIn("pass", cmd)

    def test_blocked_verdict_carries_note(self):
        result, run_mock = self._run_verdict(("blocked", "D7 金额残留", "file"))
        self.assertTrue(result)
        cmd = run_mock.call_args[0][0]
        self.assertIn("blocked", cmd)
        self.assertIn("D7 金额残留", cmd)

    def test_ambiguous_signal_falls_back(self):
        result, run_mock = self._run_verdict((None, "", ""))
        self.assertFalse(result)
        run_mock.assert_not_called()

    def test_non_gate_node_is_ignored(self):
        result, run_mock = self._run_verdict(("pass", "", "file"), node="implementation")
        self.assertFalse(result)
        run_mock.assert_not_called()

    def test_env_switch_disables_auto_verdict(self):
        result, run_mock = self._run_verdict(
            ("pass", "", "file"), env={"HERDR_AUTO_VERDICT": "0"}
        )
        self.assertFalse(result)
        run_mock.assert_not_called()

    def test_screen_marker_parsing(self):
        screen = (
            "some output\n"
            "HERDR_GATE_VERDICT: blocked\n"
            "HERDR_GATE_NOTE: D6 清空后仍为空\n"
        )
        with patch.object(self.ctrl, "_verdict_from_file", return_value=(None, "")), \
             patch.object(
                 self.ctrl.subprocess,
                 "run",
                 return_value=subprocess.CompletedProcess([], 0, screen, ""),
             ):
            verdict, note, source = self.ctrl.read_gate_verdict(self._task())
        self.assertEqual(verdict, "blocked")
        self.assertEqual(note, "D6 清空后仍为空")
        self.assertEqual(source, "screen")


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
             patch.object(self.ctrl, "try_auto_verdict", return_value=False), \
             patch.object(self.ctrl, "attention_clear"), \
             patch.object(self.ctrl, "finalize_completed_task") as finalize, \
             patch.object(self.ctrl, "coordinator_status") as coord_status, \
             patch.object(self.ctrl.subprocess, "run") as run_mock:
            self.ctrl._process_coordinator_item(self._item(), threading.Lock())

        finalize.assert_called_once_with("t1")
        coord_status.assert_not_called()
        run_mock.assert_not_called()

    def test_gate_auto_verdict_finalizes_without_prompting_coordinator(self):
        with patch.object(self.ctrl, "try_auto_accept", return_value=False), \
             patch.object(self.ctrl, "try_auto_verdict", return_value=True), \
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
             patch.object(self.ctrl, "try_auto_verdict", return_value=False), \
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
