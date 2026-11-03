import unittest
from unittest import mock
from herdr.direct_dispatch import plan_stage_dispatch
from herdr.scheduler import node_is_complete


class TestPlanCausalDispatch(unittest.TestCase):
    def setUp(self):
        self.wf_id = "wf-test-causal-01"
        self.plan_node = {
            "id": "plan",
            "label": "3计划",
            "purpose": "把需求拆解为工程方案与任务",
            "default_task_type": "docs",
            "default_integration_mode": "none",
            "required_outputs": ["技术架构与解耦任务拆解", "方案对抗审查与可行性风险评估"],
            "agent_policy": {
                "max_concurrency": 2,
                "parallel": True,
                "roles": [
                    {
                        "name": "executor",
                        "label": "技术架构与解耦拆分",
                        "goal": "技术方案设计与任务拆解",
                        "outputs": ["技术架构与解耦任务拆解"],
                    },
                    {
                        "name": "challenger",
                        "label": "方案对抗性质询与可行性评估",
                        "depends_on_role": "executor",
                        "goal": "对抗性质询与可行性审查",
                        "outputs": ["方案对抗审查与可行性风险评估"],
                    },
                ],
            },
        }

    def test_plan_node_inherently_requires_executor_before_challenger_even_without_depends_on_role(self):
        """Even if depends_on_role is omitted from plan node configuration, challenger inherently depends on executor."""
        legacy_plan_node = {
            "id": "plan",
            "label": "3计划",
            "purpose": "把需求拆解为工程方案与任务",
            "required_outputs": ["技术架构与解耦任务拆解", "方案对抗审查与可行性风险评估"],
            "agent_policy": {
                "max_concurrency": 2,
                "parallel": True,
                "roles": [
                    {
                        "name": "executor",
                        "label": "技术架构与解耦拆分",
                        "goal": "技术方案设计与任务拆解",
                        "outputs": ["技术架构与解耦任务拆解"],
                    },
                    {
                        "name": "challenger",
                        "label": "方案对抗性质询与可行性评估",
                        # NO depends_on_role specified!
                        "goal": "对抗性质询与可行性审查",
                        "outputs": ["方案对抗审查与可行性风险评估"],
                    },
                ],
            },
        }
        res = plan_stage_dispatch(self.wf_id, legacy_plan_node, [], "用户需求文本")
        self.assertEqual(res.get("mode"), "dispatch")
        specs = res.get("specs", [])
        self.assertEqual(len(specs), 1, "Must ONLY dispatch executor initially even if depends_on_role is missing")
        self.assertEqual(specs[0]["dispatch_role"], "executor")

    def test_auto_acceptance_rejects_challenger_with_blocked_verdict(self):
        """Auto acceptance must strictly reject challenger reviews that contain [BLOCKED] verdict."""
        from services.controller.auto_acceptance import try_auto_accept
        from herdr import workflow_docs
        import tempfile, os

        chal_task_id = f"{self.wf_id}-plan-challenger"
        chal_task = {
            "task_id": chal_task_id,
            "workflow_id": self.wf_id,
            "node": "plan",
            "stage": "plan",
            "dispatch_role": "challenger",
            "status": "agent_done",
            "version": 1,
            "run_id": "run-1",
            "execution_id": "exec-1",
        }
        blocked_body = (
            "## 方案对抗性审查与可行性评估\n"
            "对主方案进行破防性质询与审查，重点核查架构耦合度、死锁与复杂度风险。\n"
            "经过详细排查，当前方案存在严重的架构死锁与错配风险，无法支撑后续工程实现。\n"
            "Plan-Ref: n-plan-1\n"
            "HERDR_GATE_VERDICT: blocked"
        )
        workflow_docs.append_note(
            self.wf_id, kind="review", title="对抗质询审查", body=blocked_body,
            node="plan", task_id=chal_task_id
        )

        import re as re_mod
        class DummyPorts:
            auto_accept_enabled = lambda self: True
            get_task = lambda self, tid: chal_task
            node_is_gate = lambda self, *a: False
            workflow_config_for = lambda self, wid: {"workflow_template": "software-development-v1"}
            _task_version = lambda self, t: 1
            set_task_status = mock.MagicMock(return_value=True)
        DummyPorts.re = re_mod

        res = try_auto_accept(DummyPorts(), chal_task_id)
        self.assertFalse(res.accepted, "Auto-acceptance must NOT accept a blocked challenger review")
        self.assertEqual(res.reason_code, "challenger_blocked")
        DummyPorts.set_task_status.assert_not_called()

    def test_plan_parser_prioritizes_mismatch_and_outdated_over_blocked_verdict(self):
        """When challenger review has [BLOCKED] but targets a mismatched or outdated plan, parser reports mismatch/outdated."""
        from herdr.plan_parser import verify_plan_and_challenger
        from herdr import workflow_docs

        chal_task = {
            "task_id": f"{self.wf_id}-plan-challenger",
            "workflow_id": self.wf_id,
            "node": "plan",
            "dispatch_role": "challenger",
            "status": "completed",
        }
        # Valid fresh plan at ts=200
        plan_note = workflow_docs.append_note(
            self.wf_id, kind="plan", title="新实施计划",
            body="## 技术方案与架构设计\n新架构方案\n## 任务拆分与依赖关系\n- Task 1: 实现\n## 修改文件范围清单\n- Modify: a.py\n## 每个 Task 目标与验收标准\n- pytest\n## 风险与应对策略\n无风险",
            node="plan", task_id=f"{self.wf_id}-plan-executor"
        )
        # Blocked review on old plan at ts=100
        workflow_docs.append_note(
            self.wf_id, kind="review", title="审查错配旧计划",
            body="## 方案对抗性审查与可行性评估\n[BLOCKED] 方案对抗审查：被审计划错配。\nPlan-Ref: old-legacy-plan.md\nHERDR_GATE_VERDICT: blocked",
            node="plan", task_id=f"{self.wf_id}-plan-challenger"
        )

        ok, reason, details = verify_plan_and_challenger(self.wf_id, [chal_task])
        self.assertFalse(ok)
        # Must report version mismatch (or outdated), NOT challenger_blocked
        self.assertIn("challenger_plan_version_mismatch", reason, "Review targeting old-legacy-plan.md must be diagnosed as version mismatch, not blocked")

    def test_initial_dispatch_only_dispatches_executor_when_dependent_role_exists(self):
        """Initial dispatch of a node with dependent roles dispatches only the prerequisite role."""
        res = plan_stage_dispatch(self.wf_id, self.plan_node, [], "用户需求文本")
        self.assertEqual(res.get("mode"), "dispatch")
        specs = res.get("specs", [])
        self.assertEqual(len(specs), 1, "Only executor should be dispatched initially, challenger must wait")
        self.assertEqual(specs[0]["dispatch_role"], "executor")
        self.assertEqual(specs[0]["task_id"], f"{self.wf_id}-plan-executor")

    def test_challenger_dispatched_after_executor_completes_with_plan_ref(self):
        """Once executor completes and produces a plan note, challenger is dispatched with Plan-Ref."""
        exec_task = {
            "task_id": f"{self.wf_id}-plan-executor",
            "workflow_id": self.wf_id,
            "node": "plan",
            "stage": "plan",
            "dispatch_role": "executor",
            "status": "completed",
        }
        plan_note = {
            "note_id": "n-plan-12345",
            "node": "plan",
            "kind": "plan",
            "title": "技术架构方案",
            "body": "## 技术方案与架构设计\n核心架构解耦设计与系统划分说明，明确模块边界。\n## 任务拆分与依赖关系\n- Task 1: 核心模块实现\n## 修改文件范围清单\n- Modify: core.py\n## 每个 Task 目标与验收标准\n- 验证命令: pytest -q test_core.py\n## 风险与应对策略\n无系统性循环依赖风险。",
            "task_id": f"{self.wf_id}-plan-executor",
            "ts": 100.0,
        }

        with mock.patch("herdr.workflow_docs.load_notes", return_value=[plan_note]):
            res = plan_stage_dispatch(self.wf_id, self.plan_node, [exec_task], "用户需求文本")
            self.assertEqual(res.get("mode"), "dispatch")
            specs = res.get("specs", [])
            self.assertEqual(len(specs), 1, "Challenger should be dispatched now")
            self.assertEqual(specs[0]["dispatch_role"], "challenger")
            self.assertEqual(specs[0]["task_id"], f"{self.wf_id}-plan-challenger")
            self.assertIn("Plan-Ref: n-plan-12345", specs[0]["prompt"])

    def test_node_not_complete_until_all_roles_finished(self):
        """Node with required roles is not complete when only executor has finished."""
        exec_task = {
            "task_id": f"{self.wf_id}-plan-executor",
            "workflow_id": self.wf_id,
            "node": "plan",
            "dispatch_role": "executor",
            "status": "completed",
        }
        # Only executor finished
        self.assertFalse(node_is_complete([exec_task], node=self.plan_node))

        # Both executor and challenger finished
        chal_task = {
            "task_id": f"{self.wf_id}-plan-challenger",
            "workflow_id": self.wf_id,
            "node": "plan",
            "dispatch_role": "challenger",
            "status": "completed",
        }
        self.assertTrue(node_is_complete([exec_task, chal_task], node=self.plan_node))

    def test_challenger_outdated_auto_redispatch(self):
        """When plan is updated after challenger review, controller automatically redispatches challenger."""
        exec_task = {
            "task_id": f"{self.wf_id}-plan-executor",
            "workflow_id": self.wf_id,
            "node": "plan",
            "stage": "plan",
            "dispatch_role": "executor",
            "status": "completed",
        }
        chal_task = {
            "task_id": f"{self.wf_id}-plan-challenger",
            "workflow_id": self.wf_id,
            "node": "plan",
            "stage": "plan",
            "dispatch_role": "challenger",
            "status": "completed",
        }
        # Executor published fresh plan at ts=200 (> 100 chars, valid sections)
        plan_note = {
            "note_id": "n-plan-fresh",
            "node": "plan",
            "kind": "plan",
            "title": "更新后的技术架构方案",
            "body": "## 技术方案与架构设计\n核心模块解耦架构设计说明，降低系统耦合度。\n## 任务拆分与依赖关系\n- Task 1: 核心接口实现\n## 修改文件范围清单\n- Modify: core.py\n## 每个 Task 目标与验收标准\n- 验证命令: pytest -q test_core.py\n## 风险与应对策略\n无不可控风险。",
            "task_id": f"{self.wf_id}-plan-executor",
            "ts": 200.0,
        }
        # Challenger published older review at ts=100 (> 100 chars)
        chal_note = {
            "note_id": "n-chal-stale",
            "node": "plan",
            "kind": "review",
            "title": "旧方案对抗质询与审查",
            "body": "## 方案对抗性审查与可行性评估\n对主方案进行破防性质询，重点审查复杂度与死锁风险。\nPlan-Ref: n-plan-fresh\n经全面评估，架构边界清晰，未发现循环依赖或阻塞缺陷，准予通过。\nHERDR_GATE_VERDICT: pass",
            "task_id": f"{self.wf_id}-plan-challenger",
            "ts": 100.0,
        }

        with mock.patch("herdr.workflow_docs.load_notes", return_value=[chal_note, plan_note]):
            # When plan is updated after challenger review, plan_stage_dispatch for plan node
            # detects challenger_outdated (ts 200 > 100) and automatically redispatches challenger.
            res = plan_stage_dispatch(self.wf_id, self.plan_node, [exec_task, chal_task], "用户需求文本")
            self.assertEqual(res.get("mode"), "dispatch")
            specs = res.get("specs", [])
            self.assertEqual(len(specs), 1, "Should redispatch challenger")
            self.assertEqual(specs[0]["dispatch_role"], "challenger")
            self.assertEqual(specs[0].get("redispatch_of"), f"{self.wf_id}-plan-challenger")
            self.assertEqual(specs[0].get("plan_ref"), "n-plan-fresh")


class TestControllerPlanCausalIntegration(unittest.TestCase):
    def setUp(self):
        import tempfile, importlib.util, os
        from pathlib import Path
        from herdr.state_store import get_state_store, reset_state_store

        self.root = Path(__file__).resolve().parents[1]
        self.tmp_dir_obj = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp_dir_obj.name)
        self.db_path = self.tmp_dir / "state.db"
        os.environ["HOME"] = str(self.tmp_dir)
        os.environ["HERDR_STATE_DB"] = str(self.db_path)
        os.environ["TASKS_FILE"] = str(self.tmp_dir / "tasks.json")
        os.environ["WORKFLOWS_FILE"] = str(self.tmp_dir / "workflows.json")
        os.environ["HERDR_COORDINATOR_INTAKE"] = "1"
        reset_state_store()
        self.store = get_state_store(self.db_path)

        self.wf_id = "wf-ctl-causal-01"
        self.wf_cfg = {
            "nodes": [
                {"id": "requirements", "label": "1需求", "purpose": "确认需求规格", "depends_on": []},
                {
                    "id": "plan",
                    "label": "3计划",
                    "purpose": "制定技术架构与任务拆分并审查",
                    "depends_on": ["requirements"],
                    "parallel": True,
                    "default_task_type": "docs",
                    "default_integration_mode": "none",
                    "required_outputs": ["技术架构与解耦任务拆解", "方案对抗审查与可行性风险评估"],
                    "agent_policy": {
                        "roles": [
                            {"name": "executor", "label": "架构师", "goal": "技术方案", "outputs": ["技术架构与解耦任务拆解"]},
                            {"name": "challenger", "label": "质询者", "depends_on_role": "executor", "goal": "对抗审查", "outputs": ["方案对抗审查与可行性风险评估"]},
                        ]
                    },
                },
                {"id": "implementation", "label": "4实现", "purpose": "代码实现", "depends_on": ["plan"]},
            ]
        }

        self.wf = {
            "workflow_id": self.wf_id,
            "execution_id": "exec-1",
            "status": "running",
            "created_at": 100,
            "startup_ready": True,
            "requirement": "构建解耦模块与验收体系",
            "config": self.wf_cfg,
        }
        self.store.save_workflow(self.wf)
        self.store.save_task({
            "task_id": f"{self.wf_id}-req-1",
            "workflow_id": self.wf_id,
            "node": "requirements",
            "stage": "requirements",
            "status": "cleaned",
        })

        spec = importlib.util.spec_from_file_location("ctl_mod", self.root / "services/herdr-controller.py")
        self.ctrl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.ctrl)

        for name, function in {
            "_get_store": lambda: self.store,
            "load_tasks": self.store.list_tasks,
            "workflow_closed": lambda _: False,
            "project_for_workflow": lambda wid: self.store.get_workflow(wid),
            "workflow_config_for": lambda wid: self.store.get_workflow(wid)["config"],
            "coordinator_pane_for_workflow": lambda _: "external-coordinator",
            "coordinator_status": lambda _: "idle",
            "get_stage_policy": lambda _: {},
            "shared_docs_block": lambda *a, **kw: "",
            "maybe_compact_coordinator": lambda *a, **kw: None,
            "_scheduler_frozen_candidate_identity": lambda *a: (None, None),
            "_scheduler_resolve_candidate_and_plan": lambda *a: (None, False),
            "_scheduler_join_gate_allows": lambda *a: True,
            "notify_attention": lambda *a, **kw: True,
        }.items():
            setattr(self.ctrl, name, function)
        self.ctrl.STAGE_STATE_FILE = str(self.tmp_dir / "stage-state.json")

    def tearDown(self):
        from herdr.state_store import reset_state_store
        reset_state_store()
        self.tmp_dir_obj.cleanup()

    def test_causal_dispatch_lifecycle_executor_then_challenger_then_implementation(self):
        """End-to-end controller & durable dispatch test:
        1. First dispatch only has Executor.
        2. When Executor completes without plan deliverable, Challenger does NOT start.
        3. Once valid plan deliverable is persisted, Controller creates durable Challenger op and queues Challenger.
        4. When Challenger passes, Controller advances to implementation.
        """
        from herdr import node_dispatch_store, workflow_docs
        from herdr.task_resources import begin_launch_intent, finish_launch_intent

        # 1. First controller sweep: queues plan
        self.ctrl.check_workflow_stage_advance(self.wf_id)
        self.assertFalse(self.ctrl.coordinator_queue.empty())
        q1 = self.ctrl.coordinator_queue.get(block=False)
        self.assertEqual(q1["node_id"], "plan")
        op1_id = q1["dispatch_operation_id"]
        owner1 = q1["dispatch_owner"]

        # Planner generates initial spec: only executor
        plan_res = plan_stage_dispatch(self.wf_id, self.wf_cfg["nodes"][1], self.store.list_tasks(), self.wf["requirement"])
        self.assertEqual(plan_res["mode"], "dispatch")
        self.assertEqual(len(plan_res["specs"]), 1)
        self.assertEqual(plan_res["specs"][0]["dispatch_role"], "executor")

        # Simulate launch and completion of executor
        node_dispatch_store.start(self.store.db_path, op1_id, owner1)
        intent1 = begin_launch_intent(
            self.store, workflow_id=self.wf_id, node_id="plan", role="executor",
            candidate_sha=None, task_id=f"{self.wf_id}-plan-executor",
            dispatch_operation_id=op1_id, execution_id="exec-1", run_id="run-exec-1"
        )["intent"]
        self.store.save_task({
            "task_id": f"{self.wf_id}-plan-executor",
            "workflow_id": self.wf_id,
            "node": "plan",
            "stage": "plan",
            "dispatch_role": "executor",
            "status": "completed",
            "run_id": "run-exec-1",
            "launch_intent_id": intent1["intent_id"],
            "execution_id": "exec-1",
            "dispatch_operation_id": op1_id,
        })
        finish_launch_intent(self.store, intent1)

        # 2. Sweep without plan deliverable: Challenger must NOT launch, implementation must NOT advance
        self.ctrl.mark_stage_advance_notified(self.wf_id, "plan")
        self.ctrl.check_workflow_stage_advance(self.wf_id)
        self.assertTrue(self.ctrl.coordinator_queue.empty(), "Challenger must not queue before plan deliverable is present")
        self.assertFalse(self.ctrl.is_node_complete(self.wf_id, "plan"))
        self.assertFalse(self.ctrl.is_node_complete(self.wf_id, "implementation"))

        # 3. Executor persists valid plan deliverable
        plan_body = (
            "## 技术方案与架构设计\n"
            "系统核心架构解耦设计与系统划分说明，明确模块职责与调用约束。\n"
            "## 任务拆分与依赖关系\n"
            "- Task 1: 核心模块实现\n"
            "## 修改文件范围清单\n"
            "- Modify: core.py\n"
            "## 每个 Task 目标与验收标准\n"
            "- 验证命令: pytest -q test_core.py\n"
            "## 风险与应对策略\n"
            "系统不存在架构循环依赖或死锁风险。"
        )
        note_plan = workflow_docs.append_note(
            self.wf_id, kind="plan", title="技术架构方案", body=plan_body,
            node="plan", task_id=f"{self.wf_id}-plan-executor"
        )
        plan_ref = note_plan["note_id"]

        # Sweep with valid plan deliverable:
        # Pass 1 revokes stale 'notified' lock; Pass 2 claims new Challenger op and queues plan
        self.ctrl.check_workflow_stage_advance(self.wf_id)
        self.ctrl.check_workflow_stage_advance(self.wf_id)
        self.assertFalse(self.ctrl.coordinator_queue.empty(), "Challenger stage advance must be queued")
        q2 = self.ctrl.coordinator_queue.get(block=False)
        self.assertEqual(q2["node_id"], "plan")
        op2_id = q2["dispatch_operation_id"]
        owner2 = q2["dispatch_owner"]
        self.assertNotEqual(op1_id, op2_id, "Challenger must have its own durable dispatch operation generation")

        # Planner now produces Challenger spec with Plan-Ref injected
        plan_res2 = plan_stage_dispatch(self.wf_id, self.wf_cfg["nodes"][1], self.store.list_tasks(), self.wf["requirement"])
        self.assertEqual(plan_res2["mode"], "dispatch")
        self.assertEqual(len(plan_res2["specs"]), 1)
        self.assertEqual(plan_res2["specs"][0]["dispatch_role"], "challenger")
        self.assertEqual(plan_res2["specs"][0]["plan_ref"], plan_ref)
        self.assertIn(f"Plan-Ref: {plan_ref}", plan_res2["specs"][0]["prompt"])

        # 4. Simulate launch and completion of Challenger
        node_dispatch_store.start(self.store.db_path, op2_id, owner2)
        intent2 = begin_launch_intent(
            self.store, workflow_id=self.wf_id, node_id="plan", role="challenger",
            candidate_sha=None, task_id=f"{self.wf_id}-plan-challenger",
            dispatch_operation_id=op2_id, execution_id="exec-1", run_id="run-chal-1"
        )["intent"]
        self.store.save_task({
            "task_id": f"{self.wf_id}-plan-challenger",
            "workflow_id": self.wf_id,
            "node": "plan",
            "stage": "plan",
            "dispatch_role": "challenger",
            "status": "completed",
            "run_id": "run-chal-1",
            "launch_intent_id": intent2["intent_id"],
            "execution_id": "exec-1",
            "dispatch_operation_id": op2_id,
            "stage_verdict": "pass",
        })
        finish_launch_intent(self.store, intent2)

        chal_body = (
            "## 方案对抗性审查与可行性评估\n"
            "架构解耦合理，经评估未发现循环依赖或阻塞缺陷，准予通过。\n"
            f"Plan-Ref: {plan_ref}\n"
            "HERDR_GATE_VERDICT: pass"
        )
        workflow_docs.append_note(
            self.wf_id, kind="review", title="对抗性质询审查", body=chal_body,
            node="plan", task_id=f"{self.wf_id}-plan-challenger"
        )

        # Both roles completed and review passed -> advances to implementation!
        self.ctrl.mark_stage_advance_notified(self.wf_id, "plan")
        self.ctrl.check_workflow_stage_advance(self.wf_id)
        self.assertFalse(self.ctrl.coordinator_queue.empty())
        q3 = self.ctrl.coordinator_queue.get(block=False)
        self.assertEqual(q3["node_id"], "implementation", "Must advance to implementation once both roles complete")

    def test_controller_reopens_challenger_when_plan_is_updated_after_review(self):
        """When plan is updated after challenger review, Controller automatically reopens review."""
        from herdr import node_dispatch_store, workflow_docs
        from herdr.task_resources import begin_launch_intent, finish_launch_intent

        # Setup completed plan with old review (chal ts=100)
        self.ctrl.check_workflow_stage_advance(self.wf_id)
        q1 = self.ctrl.coordinator_queue.get(block=False)
        op1_id = q1["dispatch_operation_id"]
        owner1 = q1["dispatch_owner"]
        node_dispatch_store.start(self.store.db_path, op1_id, owner1)
        intent1 = begin_launch_intent(
            self.store, workflow_id=self.wf_id, node_id="plan", role="executor",
            candidate_sha=None, task_id=f"{self.wf_id}-plan-executor",
            dispatch_operation_id=op1_id, execution_id="exec-1", run_id="run-exec-1"
        )["intent"]
        self.store.save_task({
            "task_id": f"{self.wf_id}-plan-executor", "workflow_id": self.wf_id, "node": "plan",
            "dispatch_role": "executor", "status": "completed", "run_id": "run-exec-1",
            "launch_intent_id": intent1["intent_id"], "execution_id": "exec-1",
            "dispatch_operation_id": op1_id,
        })
        finish_launch_intent(self.store, intent1)

        # Save initial plan at ts=50
        note_plan1 = workflow_docs.append_note(
            self.wf_id, kind="plan", title="初始技术架构方案",
            body="## 技术方案与架构设计\n初始设计\n## 任务拆分与依赖关系\n- Task 1\n## 修改文件范围清单\n- Modify: core.py\n## 每个 Task 目标与验收标准\n- 验证命令: pytest\n## 风险与应对策略\n无风险",
            node="plan", task_id=f"{self.wf_id}-plan-executor"
        )
        # Advance and launch challenger
        self.ctrl.mark_stage_advance_notified(self.wf_id, "plan")
        self.ctrl.check_workflow_stage_advance(self.wf_id)
        self.ctrl.check_workflow_stage_advance(self.wf_id)
        q2 = self.ctrl.coordinator_queue.get(block=False)
        op2_id = q2["dispatch_operation_id"]
        owner2 = q2["dispatch_owner"]
        node_dispatch_store.start(self.store.db_path, op2_id, owner2)
        intent2 = begin_launch_intent(
            self.store, workflow_id=self.wf_id, node_id="plan", role="challenger",
            candidate_sha=None, task_id=f"{self.wf_id}-plan-challenger",
            dispatch_operation_id=op2_id, execution_id="exec-1", run_id="run-chal-1"
        )["intent"]
        self.store.save_task({
            "task_id": f"{self.wf_id}-plan-challenger", "workflow_id": self.wf_id, "node": "plan",
            "dispatch_role": "challenger", "status": "completed", "run_id": "run-chal-1",
            "launch_intent_id": intent2["intent_id"], "execution_id": "exec-1",
            "dispatch_operation_id": op2_id, "stage_verdict": "pass",
        })
        finish_launch_intent(self.store, intent2)
        note_chal1 = workflow_docs.append_note(
            self.wf_id, kind="review", title="旧方案对抗审查",
            body=f"## 方案对抗性审查与可行性评估\n审查通过\nPlan-Ref: {note_plan1['note_id']}\nHERDR_GATE_VERDICT: pass",
            node="plan", task_id=f"{self.wf_id}-plan-challenger"
        )

        # Now, executor publishes updated plan at ts=200 (> review ts)
        note_plan2 = workflow_docs.append_note(
            self.wf_id, kind="plan", title="更新后的技术架构方案",
            body="## 技术方案与架构设计\n最新模块架构\n## 任务拆分与依赖关系\n- Task 1\n## 修改文件范围清单\n- Modify: core.py\n## 每个 Task 目标与验收标准\n- 验证命令: pytest\n## 风险与应对策略\n无风险",
            node="plan", task_id=f"{self.wf_id}-plan-executor"
        )

        # Mark plan as notified
        self.ctrl.mark_stage_advance_notified(self.wf_id, "plan")

        # Controller sweep:
        # 1. is_node_complete('plan') must be False (challenger_outdated)!
        self.assertFalse(self.ctrl.is_node_complete(self.wf_id, "plan"), "Plan must not be complete when review is outdated")
        # 2. Sweep pass 1 revokes the 'notified' latch because of staged role pending redispatch
        self.ctrl.check_workflow_stage_advance(self.wf_id)
        # 3. Sweep pass 2 queues plan for challenger redispatch
        self.ctrl.check_workflow_stage_advance(self.wf_id)
        self.assertFalse(self.ctrl.coordinator_queue.empty())
        q_redispatch = self.ctrl.coordinator_queue.get(block=False)
        self.assertEqual(q_redispatch["node_id"], "plan")

        # 4. Plan stage dispatch returns Challenger redispatch spec targeting the fresh plan
        plan_res = plan_stage_dispatch(self.wf_id, self.wf_cfg["nodes"][1], self.store.list_tasks(), self.wf["requirement"])
        self.assertEqual(plan_res["mode"], "dispatch")
        self.assertEqual(len(plan_res["specs"]), 1)
        self.assertEqual(plan_res["specs"][0]["dispatch_role"], "challenger")
        self.assertEqual(plan_res["specs"][0]["redispatch_of"], f"{self.wf_id}-plan-challenger")
        self.assertEqual(plan_res["specs"][0]["plan_ref"], note_plan2["note_id"])

    def test_controller_redispatches_challenger_when_previous_review_was_blocked_and_new_plan_exists(self):
        """When challenger previously gave [BLOCKED] on stale/mismatched plan and executor delivered new plan, controller redispatches challenger."""
        from herdr import node_dispatch_store, workflow_docs
        from herdr.task_resources import begin_launch_intent, finish_launch_intent

        # Setup executor and challenger
        self.ctrl.check_workflow_stage_advance(self.wf_id)
        q1 = self.ctrl.coordinator_queue.get(block=False)
        op1_id = q1["dispatch_operation_id"]
        owner1 = q1["dispatch_owner"]
        node_dispatch_store.start(self.store.db_path, op1_id, owner1)
        intent1 = begin_launch_intent(
            self.store, workflow_id=self.wf_id, node_id="plan", role="executor",
            candidate_sha=None, task_id=f"{self.wf_id}-plan-executor",
            dispatch_operation_id=op1_id, execution_id="exec-1", run_id="run-exec-1"
        )["intent"]
        self.store.save_task({
            "task_id": f"{self.wf_id}-plan-executor", "workflow_id": self.wf_id, "node": "plan",
            "dispatch_role": "executor", "status": "completed", "run_id": "run-exec-1",
            "launch_intent_id": intent1["intent_id"], "execution_id": "exec-1",
            "dispatch_operation_id": op1_id,
        })
        finish_launch_intent(self.store, intent1)

        # Challenger ran and gave [BLOCKED] on mismatched plan at ts=100
        chal_task = {
            "task_id": f"{self.wf_id}-plan-challenger", "workflow_id": self.wf_id, "node": "plan",
            "dispatch_role": "challenger", "status": "cleaned", "run_id": "run-chal-1",
            "execution_id": "exec-1",
        }
        self.store.save_task(chal_task)
        note_chal_blocked = workflow_docs.append_note(
            self.wf_id, kind="review", title="旧错配计划审查",
            body="## 方案对抗性审查与可行性评估\n[BLOCKED] 方案对抗审查：被审计划错配。\nPlan-Ref: legacy-mismatched-plan.md\nHERDR_GATE_VERDICT: blocked",
            node="plan", task_id=f"{self.wf_id}-plan-challenger"
        )

        # Executor subsequently delivers valid fresh plan at ts=200
        note_plan = workflow_docs.append_note(
            self.wf_id, kind="plan", title="合规实施计划",
            body="## 技术方案与架构设计\n合规架构设计\n## 任务拆分与依赖关系\n- Task 1: 核心实现\n## 修改文件范围清单\n- Modify: core.py\n## 每个 Task 目标与验收标准\n- 验证命令: pytest\n## 风险与应对策略\n无风险",
            node="plan", task_id=f"{self.wf_id}-plan-executor"
        )

        self.ctrl.mark_stage_advance_notified(self.wf_id, "plan")

        # Controller sweep must recognize that the review is mismatched/outdated,
        # revoke the notified lock and queue challenger redispatch!
        self.assertFalse(self.ctrl.is_node_complete(self.wf_id, "plan"))
        self.ctrl.check_workflow_stage_advance(self.wf_id)
        self.ctrl.check_workflow_stage_advance(self.wf_id)
        self.assertFalse(self.ctrl.coordinator_queue.empty(), "Controller must queue challenger redispatch when new plan exists")
        q_redispatch = self.ctrl.coordinator_queue.get(block=False)
        self.assertEqual(q_redispatch["node_id"], "plan")

        # 5. Capacity admission on real boundary: max_tasks_per_node=2 allows replacement
        from types import SimpleNamespace
        from herdr.task_cli_resources import check_launch_capacity
        self.wf_cfg["nodes"][1]["max_tasks_per_node"] = 2
        tasks_in_store = self.store.list_tasks(workflow_id=self.wf_id)
        # Verify 2 tasks already in store
        self.assertEqual(len([t for t in tasks_in_store if t.get("node") == "plan"]), 2)
        args = SimpleNamespace(
            workflow_id=self.wf_id,
            node="plan",
            task_id=f"{self.wf_id}-plan-challenger-r2",
            supersedes=f"{self.wf_id}-plan-challenger",
            supersede_reason="redispatch outdated review",
            dispatch_role="challenger",
            ack_overflow=False,
        )
        # Should NOT raise ValueError (task count 3 exceeds max_tasks_per_node=2)
        check_launch_capacity(args, self.wf, tasks_in_store, "plan", get_store=lambda: self.store)

    def test_explicit_role_priority_when_workflow_id_contains_challenger_substring(self):
        """Workflow ID containing 'challenger' does not misidentify executor as challenger."""
        wf_chal_id = "wf-challenger-fix"
        plan_node = {
            "id": "plan",
            "label": "3计划",
            "purpose": "把需求拆解为工程方案与任务",
            "required_outputs": ["技术架构与解耦任务拆解", "方案对抗审查与可行性风险评估"],
            "agent_policy": {
                "max_concurrency": 2,
                "parallel": True,
                "roles": [
                    {"name": "executor", "label": "架构", "goal": "方案", "outputs": ["技术架构与解耦任务拆解"]},
                    {"name": "challenger", "label": "对抗", "depends_on_role": "executor", "goal": "审查", "outputs": ["方案对抗审查与可行性风险评估"]},
                ],
            },
        }
        # Executor task with explicit role metadata 'executor'
        exec_task = {
            "task_id": f"{wf_chal_id}-plan-executor",
            "workflow_id": wf_chal_id,
            "node": "plan",
            "dispatch_role": "executor",
            "status": "completed",
        }
        # Challenger task with explicit role metadata 'challenger'
        chal_task = {
            "task_id": f"{wf_chal_id}-plan-challenger",
            "workflow_id": wf_chal_id,
            "node": "plan",
            "dispatch_role": "challenger",
            "status": "completed",
        }
        tasks = [exec_task, chal_task]

        # In stage_state is_node_complete: single completed executor with explicit role does not trigger require_challenger
        from types import SimpleNamespace
        ports = SimpleNamespace(
            load_tasks=lambda: [exec_task],
            workflow_config_for=lambda wid: {"nodes": [{"id": "plan"}]},
            scheduler_core=None,
            _reverification_reused_node=lambda wid, nid: False,
        )
        from services.controller.stage_state import is_node_complete
        # When only executor is active/completed and scheduler considers complete (single-role or unroled fallback)
        self.assertTrue(is_node_complete(ports, wf_chal_id, "plan"))

        # Create outdated plan/review history to trigger challenger redispatch
        from herdr import workflow_docs
        n1 = workflow_docs.append_note(
            wf_chal_id, kind="plan", title="初始计划",
            body="## 技术方案与架构设计\n方案1\n## 任务拆分与依赖关系\n- Task 1\n## 修改文件范围清单\n- Modify: f.py\n## 每个 Task 目标与验收标准\n- pytest\n## 风险与应对策略\n无",
            node="plan", task_id=f"{wf_chal_id}-plan-executor"
        )
        c1 = workflow_docs.append_note(
            wf_chal_id, kind="review", title="审查",
            body=f"## 方案对抗性审查与可行性评估\n审查\nPlan-Ref: {n1['note_id']}\nHERDR_GATE_VERDICT: pass",
            node="plan", task_id=f"{wf_chal_id}-plan-challenger"
        )
        n2 = workflow_docs.append_note(
            wf_chal_id, kind="plan", title="更新计划",
            body="## 技术方案与架构设计\n方案2\n## 任务拆分与依赖关系\n- Task 1\n## 修改文件范围清单\n- Modify: f.py\n## 每个 Task 目标与验收标准\n- pytest\n## 风险与应对策略\n无",
            node="plan", task_id=f"{wf_chal_id}-plan-executor"
        )

        # When redispatching challenger, ensure predecessor selected is challenger, not executor
        res = plan_stage_dispatch(
            wf_chal_id,
            plan_node,
            tasks,
            "需求",
            project_root=self.tmp_dir,
        )
        self.assertEqual(res["mode"], "dispatch")
        self.assertEqual(len(res["specs"]), 1)
        spec = res["specs"][0]
        self.assertEqual(spec["dispatch_role"], "challenger")
        self.assertEqual(spec["supersedes"], f"{wf_chal_id}-plan-challenger", "Must bind previous round of same role")
        self.assertNotEqual(spec["supersedes"], f"{wf_chal_id}-plan-executor")


if __name__ == "__main__":


    unittest.main()
