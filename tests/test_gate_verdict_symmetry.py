"""Gate verdict symmetry tests.

Live regression (2026-09-17): plan-challenger recorded verdict=blocked,
yet the automatic path still launched the test node (test-auto), while the
manual advance path refused. Root cause: GATE_DEFAULTS only covers
test/review/wrapup, so plan/requirements blocked verdicts are invisible to
the sweep fix-loop, and try_direct never checks verdicts at all.

Contract: a task-level blocked verdict on any dependency must stop
automatic advance (funnel to coordinator adjudication) without destroying
downstream work. Voiding the verdict (supersede) must resume advance.
"""

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))


def _load_controller(name="ctrl_gate_symmetry_test"):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(
            name, str(HERDR_ROOT / "services" / "herdr-controller.py")
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ctrl = _load_controller()


def _task(task_id, status, node, **extra):
    task = {
        "task_id": task_id,
        "workflow_id": "wf-1",
        "node": node,
        "stage": node,
        "status": status,
        "created_at": 100.0,
    }
    task.update(extra)
    return task


def _node(node_id="test", depends_on=None, **overrides):
    node = {
        "id": node_id,
        "label": node_id,
        "purpose": "验证实现结果是否满足需求与验收标准。",
        "default_task_type": "test",
        "default_integration_mode": "none",
        "required_outputs": ["测试结论"],
        "rules": [],
        "depends_on": list(depends_on or []),
    }
    node.update(overrides)
    return node


def _workflow_cfg():
    return {
        "nodes": [
            {"id": "plan", "label": "plan", "depends_on": []},
            {"id": "implementation", "label": "impl", "depends_on": ["plan"]},
            {"id": "test", "label": "test", "depends_on": ["implementation", "plan"]},
        ]
    }


class SweepBlockedVerdictTest(unittest.TestCase):
    def _patch_env(self, registry):
        from herdr import liveness

        tmp = tempfile.TemporaryDirectory(prefix="herdr-gatesym-")
        self.addCleanup(tmp.cleanup)
        workflows_file = Path(tmp.name) / "workflows.json"
        workflows_file.write_text(json.dumps(
            {"workflows": {"wf-1": {"status": "in_progress"}}}))
        store = liveness.EpisodeStore(str(Path(tmp.name) / "attention.json"))
        # Isolate the state store: _get_store() derives state.db from the
        # TASKS_FILE directory, so point it at this tmp dir instead of the
        # real ~/.herdr-controller/state.db (which may hold a completed wf-1).
        _prev_tasks_file = getattr(_ctrl, "TASKS_FILE", None)
        _ctrl.TASKS_FILE = str(Path(tmp.name) / "tasks.json")
        self.addCleanup(setattr, _ctrl, "TASKS_FILE", _prev_tasks_file)
        self.fixloop_calls = []
        self.advance_marks = []
        self.notifies = []
        _prev_workflows_file = getattr(_ctrl, "WORKFLOWS_FILE", None)
        _ctrl.WORKFLOWS_FILE = str(workflows_file)
        self.addCleanup(setattr, _ctrl, "WORKFLOWS_FILE", _prev_workflows_file)

        patchers = [
            patch.object(_ctrl, "workflow_config_for",
                         return_value=_workflow_cfg()),
            patch.object(_ctrl, "project_for_workflow",
                         return_value={"startup_ready": True}),
            patch.object(_ctrl, "coordinator_pane_for_workflow",
                         return_value="wA:p1"),
            patch.object(_ctrl, "load_stage_state", return_value={}),
            patch.object(_ctrl, "save_stage_state",
                         side_effect=lambda s: None),
            patch.object(_ctrl, "load_tasks", return_value=registry),
            patch.object(_ctrl, "_attention_store", store),
            patch.object(_ctrl, "notify_attention",
                         side_effect=lambda *a, **k: self.notifies.append(a)),
            patch.object(
                _ctrl, "mark_stage_advance_queued",
                side_effect=lambda wf, node: self.advance_marks.append(node) or True,
            ),
            patch.object(
                _ctrl, "handle_fix_loop",
                side_effect=lambda wf, gate, cfg, wcfg:
                    self.fixloop_calls.append(gate),
            ),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def _plan_blocked_registry(self):
        return [
            _task("plan-e", "cleaned", "plan", stage_verdict="pass"),
            _task("plan-c", "cleaned", "plan", stage_verdict="blocked",
                  stage_verdict_note="P0 未修"),
            _task("impl-a", "cleaned", "implementation", stage_verdict="pass"),
            _task("impl-b", "cleaned", "implementation", stage_verdict="pass"),
        ]

    def test_blocked_dep_stops_auto_advance_without_destruction(self):
        registry = self._plan_blocked_registry()
        self._patch_env(registry)

        _ctrl.check_workflow_stage_advance("wf-1")

        self.assertEqual(self.advance_marks, [])
        self.assertEqual(self.fixloop_calls, [])
        self.assertEqual(len(self.notifies), 1)
        self.assertTrue(all(
            t["status"] != "superseded" for t in registry))
        episode = _ctrl._attention_store.get("wf-1:upstream_blocked:test")
        self.assertIsNotNone(episode)
        self.assertEqual(episode.get("reason"), "upstream_blocked")

    def test_second_sweep_does_not_renotify(self):
        self._patch_env(self._plan_blocked_registry())

        _ctrl.check_workflow_stage_advance("wf-1")
        _ctrl.check_workflow_stage_advance("wf-1")

        self.assertEqual(len(self.notifies), 1)

    def test_voided_verdict_resumes_advance(self):
        registry = [
            _task("plan-e", "cleaned", "plan", stage_verdict="pass"),
            _task("plan-c", "superseded", "plan", stage_verdict="blocked",
                  superseded_by="plan-c2"),
            _task("plan-c2", "cleaned", "plan", stage_verdict="pass"),
            _task("impl-a", "cleaned", "implementation", stage_verdict="pass"),
            _task("impl-b", "cleaned", "implementation", stage_verdict="pass"),
        ]
        self._patch_env(registry)

        _ctrl.check_workflow_stage_advance("wf-1")

        self.assertEqual(self.advance_marks, ["test"])
        self.assertEqual(self.notifies, [])


class DirectBlockedDepTest(unittest.TestCase):
    def setUp(self):
        self.ctrl = _load_controller("ctrl_gate_sym_dispatch_test")
        self.commands = []
        self.notified = []

        def fake_run(cmd, **kwargs):
            self.commands.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        self.patchers = [
            patch.object(
                self.ctrl,
                "project_for_workflow",
                return_value={
                    "startup_ready": True,
                    "project_root": "/tmp/proj",
                    "coordinator_pane_id": "w1:p1",
                    "requirement": "需求",
                },
            ),
            patch.object(self.ctrl, "load_tasks", return_value=[]),
            patch.object(self.ctrl, "get_stage_policy", return_value={}),
            patch.object(
                self.ctrl, "latest_branch_for_node", return_value=None
            ),
            patch.object(
                self.ctrl,
                "mark_stage_advance_notified",
                side_effect=lambda wf, node: self.notified.append(node),
            ),
            patch.object(self.ctrl.subprocess, "run", side_effect=fake_run),
        ]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

    def _item(self, node):
        return {
            "kind": "stage_advance",
            "workflow_id": "wf-1",
            "stage": "implementation",
            "node_id": "test",
            "next_stage": "test",
            "node": node,
        }

    def _tasks_with_plan(self, verdict):
        return [
            _task("plan-e", "cleaned", "plan", stage_verdict="pass"),
            _task("plan-c", "cleaned", "plan", stage_verdict=verdict,
                  stage_verdict_note="P0"),
        ]

    def test_blocked_upstream_dep_refuses_launch(self):
        with patch.object(
            self.ctrl, "load_tasks",
            return_value=self._tasks_with_plan("blocked"),
        ), patch("builtins.print") as output:
            result = self.ctrl.try_direct_stage_advance(
                self._item(_node(depends_on=["plan"])))
        self.assertFalse(result)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.notified, [])
        self.assertTrue(any(
            "BLOCKED" in str(call) for call in output.call_args_list))

    def test_passed_upstream_dep_proceeds(self):
        with patch.object(
            self.ctrl, "load_tasks",
            return_value=self._tasks_with_plan("pass"),
        ):
            result = self.ctrl.try_direct_stage_advance(
                self._item(_node(depends_on=["plan"])))
        self.assertTrue(result)
        self.assertEqual(len(self.commands), 1)

    def test_unknown_deps_fail_open_as_before(self):
        result = self.ctrl.try_direct_stage_advance(self._item(_node()))
        self.assertTrue(result)
        self.assertEqual(len(self.commands), 1)

    def test_legacy_node_resolves_deps_from_workflow_config(self):
        node = _node()
        del node["depends_on"]
        with patch.object(
            self.ctrl, "load_tasks",
            return_value=self._tasks_with_plan("blocked"),
        ), patch.object(
            self.ctrl, "workflow_config_for",
            return_value={"nodes": [
                {"id": "plan", "depends_on": []},
                {"id": "test", "depends_on": ["plan"]},
            ]},
        ), patch("builtins.print") as output:
            result = self.ctrl.try_direct_stage_advance(self._item(node))
        self.assertFalse(result)
        self.assertEqual(self.commands, [])
        self.assertTrue(any(
            "BLOCKED" in str(call) for call in output.call_args_list))

    def test_malformed_config_fails_open(self):
        node = _node()
        del node["depends_on"]
        with patch.object(
            self.ctrl, "load_tasks",
            return_value=self._tasks_with_plan("blocked"),
        ), patch.object(
            self.ctrl, "workflow_config_for",
            return_value={"nodes": [{"id": "test", "depends_on": ["ghost"]}]},
        ):
            result = self.ctrl.try_direct_stage_advance(self._item(node))
        self.assertTrue(result)
        self.assertEqual(len(self.commands), 1)

    def test_stages_only_config_fails_open(self):
        node = _node()
        del node["depends_on"]
        with patch.object(
            self.ctrl, "load_tasks",
            return_value=self._tasks_with_plan("blocked"),
        ), patch.object(
            self.ctrl, "workflow_config_for",
            return_value={"stages": [{"key": "test"}]},
        ):
            result = self.ctrl.try_direct_stage_advance(self._item(node))
        self.assertTrue(result)
        self.assertEqual(len(self.commands), 1)


class ConsolePollWindowTest(unittest.TestCase):
    def test_poll_window_covers_backend_timeout(self):
        text = (HERDR_ROOT / "console" / "herdr_factory_console.py"
                ).read_text(encoding="utf-8")
        start = text.index("async function waitForWorkflowJob")
        end = text.index("async function submitNewWorkflowAsync")
        block = text[start:end]
        self.assertIn("i<=620", block)
        self.assertNotIn("i<=180", block)


if __name__ == "__main__":
    unittest.main()
