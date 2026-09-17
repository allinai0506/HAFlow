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

    def test_dynamic_initial_dispatch_has_no_specs(self):
        for parallel, max_agents in ((True, 3), (True, 1), (False, 3)):
            with self.subTest(parallel=parallel, max_agents=max_agents):
                node = _node(
                    id="implementation", parallel=parallel,
                    agent_policy={"max_agents": max_agents},
                )
                plan = dd.plan_stage_dispatch("wf-1", node, [], "需求")
                self.assertEqual(plan["mode"], "fallback")
                self.assertEqual(plan["specs"], [])

    def test_non_agent_never_produces_specs(self):
        for node_type in ("human", "tool", "gate", "unknown"):
            for tasks in ([], [_task("old", "superseded")]):
                with self.subTest(node_type=node_type, tasks=tasks):
                    plan = dd.plan_stage_dispatch(
                        "wf-1", _node(node_type=node_type), tasks, "需求"
                    )
                    self.assertEqual(plan["mode"], "fallback")
                    self.assertEqual(plan["specs"], [])

    def test_malformed_roles_cannot_enable_static_multi(self):
        for roles in (1, "executor", {"name": "executor"}, ({"name": "executor"},)):
            with self.subTest(roles=roles):
                plan = dd.plan_stage_dispatch(
                    "wf-1", _node(parallel=True, agent_policy={
                        "max_agents": 3, "roles": roles,
                    }), [], "需求",
                )
                self.assertEqual(plan["mode"], "fallback")
                self.assertEqual(plan["specs"], [])

    def test_dynamic_node_with_active_tasks_waits(self):
        plan = dd.plan_stage_dispatch(
            "wf-1", _node(parallel=True, agent_policy={"max_agents": 3}),
            [_task("existing", "working")], "需求",
        )
        self.assertEqual(plan["mode"], "wait")
        self.assertEqual(plan["specs"], [])

    def test_explicit_static_single_dispatch(self):
        plan = dd.plan_stage_dispatch(
            "wf-1", _node(node_type="agent", parallel=False,
                          agent_policy={"max_agents": 1}), [], "需求",
        )
        self.assertEqual(plan["mode"], "dispatch")
        self.assertEqual(len(plan["specs"]), 1)

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
        # 谱系去重：只补派最新一发（r2 -> r3），历史作废任务不得重复补派。
        self.assertEqual(
            [s["task_id"] for s in plan["specs"]],
            ["wf-1-test-backend-r3"],
        )

    def test_lineage_dedup_prevents_redispatch_amplification(self):
        """Lessons §61 事故复现：r2/r3 都曾进过 awaiting，旧逻辑会一次派 2 个。

        修正后：同一谱系只补派最新一发；不同角色（不同谱系根）仍各自补派。
        """
        tasks = [
            _task("wf-1-test-backend", "superseded"),
            _task("wf-1-test-backend-r2", "superseded"),
            _task("wf-1-test-backend-r3", "superseded"),
            _task("wf-1-test-frontend", "superseded"),
        ]
        plan = dd.plan_stage_dispatch("wf-1", _node(), tasks, "需求")
        self.assertEqual(
            [s["task_id"] for s in plan["specs"]],
            ["wf-1-test-backend-r4", "wf-1-test-frontend-r2"],
        )

    def test_active_lineage_head_blocks_older_awaiting(self):
        """谱系最新一发已被人工作废、但同谱系仍有在跑任务时，禁止再补派。"""
        tasks = [
            _task("wf-1-test-auto", "superseded"),
            _task("wf-1-test-auto-r2", "superseded"),
            _task("wf-1-test-auto-r3", "superseded"),
            _task("wf-1-test-auto-r4", "working"),
            _task("wf-1-test-auto-r5", "superseded"),
        ]
        plan = dd.plan_stage_dispatch("wf-1", _node(), tasks, "需求")
        self.assertEqual(plan["mode"], "wait")
        self.assertEqual(plan["specs"], [])

    def test_fully_superseded_lineage_redispatches_newest_once(self):
        tasks = [
            _task("wf-1-test-auto", "superseded"),
            _task("wf-1-test-auto-r2", "superseded"),
            _task("wf-1-test-auto-r3", "superseded"),
        ]
        plan = dd.plan_stage_dispatch("wf-1", _node(), tasks, "需求")
        self.assertEqual(
            [s["task_id"] for s in plan["specs"]],
            ["wf-1-test-auto-r4"],
        )

    def test_redispatch_of_plain_base_id(self):
        tasks = [_task("wf-1-test-backend", "superseded")]
        plan = dd.plan_stage_dispatch("wf-1", _node(), tasks, "需求")
        self.assertEqual(
            [s["task_id"] for s in plan["specs"]],
            ["wf-1-test-backend-r2"],
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

    def test_gate_contract_injected_only_for_gate_nodes(self):
        gate_plan = dd.plan_stage_dispatch(
            "wf-1", _node(), [], "需求", gate_contract=True
        )
        prompt = gate_plan["specs"][0]["prompt"]
        self.assertIn("HERDR_GATE_VERDICT", prompt)
        self.assertIn("gate-verdict.json", prompt)

        plain_plan = dd.plan_stage_dispatch("wf-1", _node(), [], "需求")
        self.assertNotIn("HERDR_GATE_VERDICT", plain_plan["specs"][0]["prompt"])

    def test_gate_verdict_path_prefers_outside_clone_state_dir(self):
        """门禁结论文件必须落到 clone 外状态目录（避免污染交付）。"""
        task_id = "wf-1-test-auto"
        default_path = dd.gate_verdict_path(task_id)
        self.assertTrue(default_path.endswith(f"gate-verdicts/{task_id}.json"))
        self.assertIn(".herdr-controller", default_path)

        with patch.dict(
            os.environ, {"HERDR_GATE_VERDICT_DIR": "/tmp/herdr-verdicts"}
        ):
            self.assertEqual(
                dd.gate_verdict_path(task_id),
                f"/tmp/herdr-verdicts/{task_id}.json",
            )

            # 契约把绝对路径写进 prompt（含任务 id），并保留 clone 内兜底说明
            plan = dd.plan_stage_dispatch(
                "wf-1", _node(), [], "需求", gate_contract=True
            )
            prompt = plan["specs"][0]["prompt"]
            self.assertIn("/tmp/herdr-verdicts", prompt)
            self.assertIn("wf-1-test-auto.json", prompt)

    def test_gate_contract_carried_into_redispatch(self):
        tasks = [_task("wf-1-test-backend", "superseded")]
        plan = dd.plan_stage_dispatch(
            "wf-1", _node(), tasks, "需求", gate_contract=True
        )
        self.assertIn("HERDR_GATE_VERDICT", plan["specs"][0]["prompt"])

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

    def test_initial_dispatch_with_roles(self):
        node = _node(
            id="requirements",
            label="2需求分析",
            purpose="理解需求并形成可验收需求。",
            agent_policy={
                "max_agents": 2,
                "roles": [
                    {
                        "name": "executor",
                        "label": "主执行者",
                        "goal": "梳理需求范围、业务规则与清晰可验收标准",
                        "outputs": ["需求规格与验收标准"],
                    },
                    {
                        "name": "challenger",
                        "label": "对抗性质询者",
                        "goal": "对抗性破防审查、挖掘隐式假设与潜在缺陷",
                        "outputs": ["需求对抗审查与边界漏洞清单"],
                    },
                ],
            },
        )
        plan = dd.plan_stage_dispatch("wf-1", node, [], "用户权限系统")
        self.assertEqual(plan["mode"], "dispatch")
        self.assertEqual(len(plan["specs"]), 2)
        spec0, spec1 = plan["specs"]
        self.assertEqual(spec0["task_id"], "wf-1-requirements-executor")
        self.assertEqual(spec1["task_id"], "wf-1-requirements-challenger")
        self.assertIn("需求规格与验收标准", spec0["acceptance"])
        self.assertIn("需求对抗审查与边界漏洞清单", spec1["acceptance"])
        self.assertIn("梳理需求范围", spec0["prompt"])
        self.assertIn("对抗性破防审查", spec1["prompt"])


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
            # 阶段边界的总指挥 /compact 注入有独立契约测试;
            # 这里打桩,保证本组测试只观察派发行为。
            patch.object(self.ctrl, "maybe_compact_coordinator"),
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

    def test_direct_advance_triggers_coordinator_compaction(self):
        # 2026-09-17 效率优化:阶段推进成功即触发总指挥 /compact 注入
        # (上下文卫生,压长回合耗时)。
        with patch.object(
            self.ctrl, "maybe_compact_coordinator"
        ) as compact_mock:
            result = self.ctrl.try_direct_stage_advance(self._item())

        self.assertTrue(result)
        compact_mock.assert_called_once_with(
            "wf-1", reason="stage_advance:test"
        )

    def test_launch_timeout_is_bounded_and_falls_back(self):
        seen_timeouts = []

        def hung(cmd, **kwargs):
            seen_timeouts.append(kwargs.get("timeout"))
            raise subprocess.TimeoutExpired(cmd, timeout=kwargs.get("timeout"))

        with patch.object(self.ctrl.subprocess, "run", side_effect=hung):
            result = self.ctrl.try_direct_stage_advance(self._item())
        self.assertFalse(result)
        self.assertEqual(self.notified, [])
        self.assertEqual(self.commands, [])
        self.assertTrue(seen_timeouts)
        self.assertTrue(all(t is not None and t > 0 for t in seen_timeouts))

    def test_partial_launch_timeout_reports_partial(self):
        node = _node(
            id="requirements",
            agent_policy={
                "max_agents": 2,
                "roles": [
                    {"name": "executor", "goal": "做 A"},
                    {"name": "challenger", "goal": "做 B"},
                ],
            },
        )
        calls = {"n": 0}

        def flaky(cmd, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                self.commands.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "", "")
            raise subprocess.TimeoutExpired(cmd, timeout=kwargs.get("timeout"))

        with patch.object(self.ctrl.subprocess, "run", side_effect=flaky), \
             patch("builtins.print") as output:
            result = self.ctrl.try_direct_stage_advance(self._item(node=node))
        self.assertFalse(result)
        self.assertEqual(self.notified, [])
        printed = " ".join(str(c) for c in output.call_args_list)
        self.assertIn("PARTIAL", printed)
        self.assertIn("wf-1-requirements-executor", printed)

    def test_dynamic_direct_dispatch_does_not_launch(self):
        result = self.ctrl.try_direct_stage_advance(self._item(node=_node(
            parallel=True, agent_policy={"max_agents": 3},
        )))
        self.assertFalse(result)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.notified, [])

    def test_non_agent_stage_event_blocks_all_agent_paths(self):
        for node_type in ("human", "tool", "gate", "unknown"):
            for enabled in ("0", "1"):
                with self.subTest(node_type=node_type, enabled=enabled), \
                     patch.dict(os.environ, {"HERDR_DIRECT_STAGE_DISPATCH": enabled}), \
                     patch.object(self.ctrl, "coordinator_pane_for_workflow", return_value=None) as coordinator, \
                     patch("builtins.print") as output:
                    self.ctrl._handle_coordinator_item(
                        self._item(node=_node(node_type=node_type))
                    )
                    coordinator.assert_not_called()
                    self.assertEqual(self.commands, [])
                    self.assertEqual(self.notified, [])
                    self.assertTrue(any(
                        "STAGE ADVANCE BLOCKED" in str(call)
                        for call in output.call_args_list
                    ))

    def test_missing_item_node_resolves_native_workflow_node(self):
        item = self._item()
        item.pop("node")
        with patch.object(self.ctrl, "workflow_config_for", return_value={
            "nodes": [_node(node_type="human")],
        }), patch.object(self.ctrl, "coordinator_pane_for_workflow", return_value=None) as coordinator:
            self.ctrl._handle_coordinator_item(item)
        coordinator.assert_not_called()
        self.assertEqual(self.commands, [])
        self.assertEqual(self.notified, [])

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
