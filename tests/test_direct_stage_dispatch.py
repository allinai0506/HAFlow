"""Rules-based stage dispatch tests (pure planner + controller shell wiring).

Covers the latency optimization where routine node advancement no longer
requires a coordinator LLM turn:

  1. direct_dispatch.plan_stage_dispatch: initial dispatch, fix-loop subset
     re-dispatch, wait, and fallback decisions (pure, no I/O).
  2. controller.try_direct_stage_advance: launch wiring, wait handling,
     disabled/failure fallback to the coordinator path.
  3. controller.wait_for_coordinator_decision: realistic budget + attention
     backoff on timeout.
  4. controller.sync_awake_guard: caffeinate lifetime management.
"""

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr import direct_dispatch as dd


def _load_controller(name="ctrl_direct_dispatch_test"):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(
            name, str(HERDR_ROOT / "services" / "herdr-controller.py")
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _node(**overrides):
    node = {
        "id": "test",
        "label": "5测试",
        "purpose": "验证实现结果是否满足需求与验收标准。",
        "default_task_type": "test",
        "default_integration_mode": "none",
        "required_outputs": ["测试执行记录", "测试结论"],
        "rules": ["以验证为主，不得随意修改代码"],
    }
    node.update(overrides)
    return node


def _task(task_id, status, node="test", **extra):
    task = {
        "task_id": task_id,
        "workflow_id": "wf-1",
        "node": node,
        "stage": node,
        "status": status,
        "goal": "验证摘要截断修复",
        "acceptance_criteria": ["回归通过", "边界覆盖"],
        "created_at": 100.0,
    }
    task.update(extra)
    return task


class PlanStageDispatchTest(unittest.TestCase):
    def test_initial_dispatch_from_node_config(self):
        plan = dd.plan_stage_dispatch("wf-1", _node(), [], "优化摘要展示")
        self.assertEqual(plan["mode"], "dispatch")
        self.assertEqual(len(plan["specs"]), 1)
        spec = plan["specs"][0]
        self.assertEqual(spec["task_id"], "wf-1-test-auto")
        self.assertEqual(spec["task_type"], "test")
        self.assertEqual(spec["integration_mode"], "none")
        self.assertIn("优化摘要展示", spec["prompt"])
        self.assertIn("验证实现结果", spec["prompt"])
        self.assertIn("测试执行记录", spec["acceptance"])
        self.assertIn("改动范围以 herdr-task verify-baseline 为准", spec["acceptance"])

    def test_initial_dispatch_requires_requirement(self):
        plan = dd.plan_stage_dispatch("wf-1", _node(), [], "")
        self.assertEqual(plan["mode"], "fallback")
        self.assertIn("requirement", plan["reason"])

    def test_fallback_without_purpose_or_id(self):
        self.assertEqual(
            dd.plan_stage_dispatch("wf-1", _node(purpose=""), [], "r")["mode"],
            "fallback",
        )
        self.assertEqual(
            dd.plan_stage_dispatch("wf-1", {"id": ""}, [], "r")["mode"],
            "fallback",
        )
        self.assertEqual(
            dd.plan_stage_dispatch("wf-1", None, [], "r")["mode"],
            "fallback",
        )

    def test_redispatch_only_superseded_subset(self):
        tasks = [
            _task(
                "wf-1-test-backend",
                "superseded",
                stage_verdict="blocked",
                stage_verdict_note="B1 回填缺失",
            ),
            _task(
                "wf-1-test-frontend",
                "cleaned",
                stage_verdict="pass",
            ),
            _task("wf-1-review-a", "cleaned", node="review"),
        ]
        plan = dd.plan_stage_dispatch("wf-1", _node(), tasks, "需求")
        self.assertEqual(plan["mode"], "dispatch")
        self.assertEqual(len(plan["specs"]), 1)
        spec = plan["specs"][0]
        self.assertEqual(spec["task_id"], "wf-1-test-backend-r2")
        self.assertIn("B1 回填缺失", spec["prompt"])
        self.assertEqual(spec["acceptance"][:2], ["回归通过", "边界覆盖"])
        self.assertIn(
            "改动范围以 herdr-task verify-baseline 为准", spec["acceptance"]
        )

    def test_redispatch_id_skips_existing(self):
        tasks = [
            _task("wf-1-test-backend", "superseded"),
            _task("wf-1-test-backend-r2", "superseded"),
        ]
        plan = dd.plan_stage_dispatch("wf-1", _node(), tasks, "需求")
        self.assertEqual(
            [s["task_id"] for s in plan["specs"]],
            ["wf-1-test-backend-r3", "wf-1-test-backend-r4"],
        )

    def test_wait_when_active_tasks_exist(self):
        tasks = [_task("wf-1-test-a", "working")]
        plan = dd.plan_stage_dispatch("wf-1", _node(), tasks, "需求")
        self.assertEqual(plan["mode"], "wait")
        self.assertEqual(plan["specs"], [])

    def test_replaced_superseded_task_is_not_awaited(self):
        tasks = [
            _task("wf-1-test-old", "superseded", superseded_by="wf-1-test-new"),
            _task("wf-1-test-new", "working"),
        ]
        plan = dd.plan_stage_dispatch("wf-1", _node(), tasks, "需求")
        self.assertEqual(plan["mode"], "wait")

    def test_other_workflow_tasks_are_ignored(self):
        tasks = [
            {
                "task_id": "other-1",
                "workflow_id": "wf-2",
                "node": "test",
                "stage": "test",
                "status": "working",
            }
        ]
        plan = dd.plan_stage_dispatch("wf-1", _node(), tasks, "需求")
        self.assertEqual(plan["mode"], "dispatch")


class MergeNodePolicyTest(unittest.TestCase):
    POLICY = {
        "label": "5测试",
        "purpose": "验证实现结果是否满足需求与验收标准。",
        "default_task_type": "test",
        "default_integration_mode": "none",
        "required_outputs": ["测试范围", "测试结论"],
        "rules": ["测试任务默认只读"],
    }

    def test_empty_node_fields_fall_back_to_policy(self):
        merged = dd.merge_node_policy(
            {"id": "test", "purpose": "", "required_outputs": [], "rules": []},
            self.POLICY,
        )
        plan = dd.plan_stage_dispatch("wf-1", merged, [], "需求")
        self.assertEqual(plan["mode"], "dispatch")
        self.assertEqual(plan["specs"][0]["task_type"], "test")
        self.assertIn("测试范围", plan["specs"][0]["acceptance"])

    def test_node_values_override_policy(self):
        merged = dd.merge_node_policy(
            {"id": "test", "purpose": "节点自定义职责", "rules": ["节点规则"]},
            self.POLICY,
        )
        plan = dd.plan_stage_dispatch("wf-1", merged, [], "需求")
        self.assertIn("节点自定义职责", plan["specs"][0]["prompt"])
        self.assertIn("节点规则", plan["specs"][0]["prompt"])

    def test_policy_and_node_missing_still_falls_back(self):
        merged = dd.merge_node_policy({"id": "test"}, {})
        plan = dd.plan_stage_dispatch("wf-1", merged, [], "需求")
        self.assertEqual(plan["mode"], "fallback")


class TryDirectStageAdvanceTest(unittest.TestCase):
    def setUp(self):
        self.ctrl = _load_controller()
        self.notified = []
        self.commands = []

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
                    "requirement": "优化摘要展示",
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

    def _item(self, node=None):
        return {
            "kind": "stage_advance",
            "workflow_id": "wf-1",
            "stage": "implementation",
            "node_id": "test",
            "next_stage": "test",
            "node": node if node is not None else _node(),
        }

    def test_direct_dispatch_launches_and_marks_notified(self):
        result = self.ctrl.try_direct_stage_advance(self._item())
        self.assertTrue(result)
        self.assertEqual(self.notified, ["test"])
        self.assertEqual(len(self.commands), 1)
        cmd = self.commands[0]
        self.assertIn("launch", cmd)
        self.assertIn("wf-1-test-auto", cmd)
        self.assertIn("--node", cmd)
        self.assertIn("test", cmd)
        self.assertIn("--agent", cmd)
        self.assertIn("auto", cmd)

    def test_launch_failure_falls_back_to_coordinator(self):
        with patch.object(
            self.ctrl.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1, "", "boom"),
        ):
            result = self.ctrl.try_direct_stage_advance(self._item())
        self.assertFalse(result)
        self.assertEqual(self.notified, [])

    def test_wait_mode_marks_notified_without_launch(self):
        with patch.object(
            self.ctrl,
            "load_tasks",
            return_value=[_task("wf-1-test-a", "working")],
        ):
            result = self.ctrl.try_direct_stage_advance(self._item())
        self.assertTrue(result)
        self.assertEqual(self.notified, ["test"])
        self.assertEqual(self.commands, [])

    def test_disabled_by_env(self):
        with patch.dict(os.environ, {"HERDR_DIRECT_STAGE_DISPATCH": "0"}):
            result = self.ctrl.try_direct_stage_advance(self._item())
        self.assertFalse(result)
        self.assertEqual(self.commands, [])

    def test_missing_node_config_falls_back(self):
        result = self.ctrl.try_direct_stage_advance(
            self._item(node={"id": "test", "label": "test"})
        )
        self.assertFalse(result)
        self.assertEqual(self.commands, [])

    def test_stage_policy_fills_empty_node_purpose(self):
        with patch.object(
            self.ctrl,
            "get_stage_policy",
            return_value={
                "purpose": "验证实现结果是否满足需求与验收标准。",
                "default_task_type": "test",
                "required_outputs": ["测试结论"],
                "rules": ["测试任务默认只读"],
            },
        ):
            result = self.ctrl.try_direct_stage_advance(
                self._item(node={"id": "test", "label": "5测试", "purpose": ""})
            )
        self.assertTrue(result)
        self.assertEqual(len(self.commands), 1)
        cmd = self.commands[0]
        self.assertIn("test", cmd)
        self.assertIn("wf-1-test-auto", cmd)


class DecisionTimeoutTest(unittest.TestCase):
    def setUp(self):
        self.ctrl = _load_controller("ctrl_decision_timeout_test")
        self.notes = []

    def test_timeout_records_attention_backoff(self):
        task = {
            "task_id": "t1",
            "workflow_id": "wf-1",
            "status": "agent_done",
        }
        with patch.object(self.ctrl, "get_task", return_value=task), \
             patch.object(self.ctrl, "attention_get", return_value={}), \
             patch.object(
                 self.ctrl,
                 "attention_note",
                 side_effect=lambda *a, **k: self.notes.append((a, k)),
             ):
            decision = self.ctrl.wait_for_coordinator_decision(
                "t1", timeout=0.01
            )

        self.assertEqual(decision, "agent_done")
        self.assertEqual(len(self.notes), 1)
        _, kwargs = self.notes[0]
        self.assertEqual(kwargs["reason"], "decision_timeout")
        self.assertIn("next_retry_at", kwargs)


class SyncAwakeGuardTest(unittest.TestCase):
    class _FakeProc:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    def setUp(self):
        self.ctrl = _load_controller("ctrl_awake_guard_test")
        self.ctrl._awake_guard_proc = None
        self.addCleanup(setattr, self.ctrl, "_awake_guard_proc", None)

    def test_spawns_once_and_releases(self):
        proc = self._FakeProc()
        with patch.object(self.ctrl.sys, "platform", "darwin"), \
             patch.object(self.ctrl.shutil, "which", return_value="/usr/bin/caffeinate"), \
             patch.object(
                 self.ctrl.subprocess, "Popen", return_value=proc
             ) as popen:
            self.ctrl.sync_awake_guard(["wf-1"])
            self.ctrl.sync_awake_guard(["wf-1"])
            self.assertEqual(popen.call_count, 1)

            self.ctrl.sync_awake_guard([])
            self.assertTrue(proc.terminated)
            self.assertIsNone(self.ctrl._awake_guard_proc)

    def test_disabled_by_env(self):
        with patch.object(self.ctrl.sys, "platform", "darwin"), \
             patch.object(self.ctrl.shutil, "which", return_value="/usr/bin/caffeinate"), \
             patch.object(self.ctrl.subprocess, "Popen") as popen, \
             patch.dict(os.environ, {"HERDR_AWAKE_GUARD": "0"}):
            self.ctrl.sync_awake_guard(["wf-1"])
        self.assertEqual(popen.call_count, 0)


if __name__ == "__main__":
    unittest.main()
