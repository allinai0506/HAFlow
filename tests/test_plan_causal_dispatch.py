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

    def test_plan_node_without_executor_does_not_inject_impossible_executor_dependency(self):
        """When a custom plan node only configures a reviewer/challenger without executor, do not inject impossible dependency."""
        reviewer_only_plan_node = {
            "id": "plan",
            "label": "3计划",
            "purpose": "审查计划",
            "required_outputs": ["方案对抗审查与可行性风险评估"],
            "agent_policy": {
                "max_concurrency": 1,
                "roles": [
                    {
                        "name": "reviewer",
                        "label": "方案审查",
                        "goal": "审查",
                        "outputs": ["方案对抗审查与可行性风险评估"],
                    },
                ],
            },
        }
        res = plan_stage_dispatch(self.wf_id, reviewer_only_plan_node, [], "用户需求文本")
        self.assertEqual(res.get("mode"), "dispatch", "Must dispatch configured role without stalling on non-existent executor")
        specs = res.get("specs", [])
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["dispatch_role"], "reviewer")

    def test_auto_acceptance_accepts_challenger_and_persists_verdict(self):
        """When challenger review has valid sections and explicit BLOCKED verdict, auto-acceptance accepts task and records blocked verdict."""
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

        ports = DummyPorts()
        res = try_auto_accept(ports, chal_task_id)
        self.assertTrue(res.accepted, "Auto-acceptance must accept a structurally valid challenger review even if blocked")
        ports.set_task_status.assert_called_once()
        args, kwargs = ports.set_task_status.call_args
        self.assertEqual(args[0], chal_task_id)
        self.assertEqual(args[1], "completed")
        meta = kwargs.get("metadata", {})
        self.assertEqual(meta.get("stage_verdict"), "blocked")
        self.assertIn("blocked", meta.get("stage_verdict_note", ""))

    def test_auto_acceptance_rejects_challenger_when_verdict_is_missing(self):
        """When challenger review has valid sections but lacks a clear verdict, auto-acceptance rejects."""
        from services.controller.auto_acceptance import try_auto_accept
        from herdr import workflow_docs

        chal_task_id = f"{self.wf_id}-plan-chal-noverdict"
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
        no_verdict_body = (
            "## 方案对抗性审查与可行性评估\n"
            "对主方案进行破防性质询与审查，重点核查架构耦合度、死锁与复杂度风险。"
            "进行了方案分析与逻辑排查，针对各个关键模块的调用链路进行了核实，但审查者未在此处给出明确的通过或阻断裁决结论。\n"
            "Plan-Ref: n-plan-1\n"
        )
        workflow_docs.append_note(
            self.wf_id, kind="review", title="未定结论审查", body=no_verdict_body,
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

        ports = DummyPorts()
        res = try_auto_accept(ports, chal_task_id)
        self.assertFalse(res.accepted)
        self.assertEqual(res.reason_code, "challenger_verdict_missing")
        ports.set_task_status.assert_not_called()

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

    def test_auto_acceptance_uses_notes_as_authoritative_over_older_clone_review_file(self):
        """When challenger publishes passing review in shared notes, auto-acceptance accepts it even if clone file has old blocked review."""
        from services.controller.auto_acceptance import try_auto_accept
        from herdr import workflow_docs
        import tempfile, os

        with tempfile.TemporaryDirectory() as tmpdir:
            # Old blocked review in clone file
            with open(os.path.join(tmpdir, "challenger-review.md"), "w", encoding="utf-8") as f:
                f.write("## 方案对抗性审查与可行性评估\n[BLOCKED] 存在严重问题\nHERDR_GATE_VERDICT: blocked")

            chal_task_id = f"{self.wf_id}-plan-chal-updated"
            chal_task = {
                "task_id": chal_task_id,
                "workflow_id": self.wf_id,
                "node": "plan",
                "stage": "plan",
                "dispatch_role": "challenger",
                "clone_path": tmpdir,
                "status": "agent_done",
                "version": 1,
                "run_id": "run-1",
                "execution_id": "exec-1",
            }

            # Updated passing review published in shared notes
            passing_body = (
                "## 方案对抗性审查与可行性评估\n"
                "### 1. 计划完备性评估\n合格\n"
                "### 2. 潜在单点故障\n无\n"
                "### 3. 回退方案评估\n可行\n"
                "### 4. 边界条件覆盖\n完整\n"
                "### 5. 验收方案可行性\n可行\n\n"
                "最终裁决：通过\n[PASS]"
            )
            workflow_docs.append_note(
                self.wf_id, kind="review", title="最新通过审查", body=passing_body,
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
            self.assertTrue(res.accepted, f"Auto-acceptance should accept updated note review over older clone file: {res.reason_code} {res.detail}")
            DummyPorts.set_task_status.assert_called_once()

    def test_auto_acceptance_role_detection_prioritizes_explicit_metadata_over_workflow_id(self):
        """When workflow ID contains 'review' (e.g. wf-review-fix), explicit executor role is not misclassified as challenger."""
        from services.controller.auto_acceptance import try_auto_accept
        from herdr import workflow_docs

        wf_id = f"{self.wf_id}-review-fix"
        exec_task_id = f"{wf_id}-plan-executor"
        exec_task = {
            "task_id": exec_task_id,
            "workflow_id": wf_id,
            "node": "plan",
            "stage": "plan",
            "dispatch_role": "executor",
            "status": "agent_done",
            "version": 1,
            "run_id": "run-1",
            "execution_id": "exec-1",
        }

        # Another task published a passing review note
        workflow_docs.append_note(
            wf_id, kind="review", title="审查结论通过",
            body="## 方案对抗性审查与可行性评估\n[PASS] 通过",
            node="plan", task_id=f"{wf_id}-plan-challenger"
        )

        import re as re_mod
        class DummyPorts:
            auto_accept_enabled = lambda self: True
            get_task = lambda self, tid: exec_task
            node_is_gate = lambda self, *a: False
            workflow_config_for = lambda self, wid: {"workflow_template": "software-development-v1"}
            _task_version = lambda self, t: 1
            set_task_status = mock.MagicMock(return_value=True)
        DummyPorts.re = re_mod

        # Executor has not produced a plan; it must NOT be accepted using challenger's passing review!
        res = try_auto_accept(DummyPorts(), exec_task_id)
        self.assertFalse(res.accepted, "Executor must not be accepted using another task's passing review")
        DummyPorts.set_task_status.assert_not_called()

    def test_plan_stage_dispatch_preserves_genuine_blocked_verdict_without_blind_redispatch(self):
        """When challenger legitimately blocks the CURRENT plan, direct dispatch preserves refusal and does NOT blind redispatch."""
        from herdr import workflow_docs

        plan_task = {
            "task_id": f"{self.wf_id}-plan-executor",
            "workflow_id": self.wf_id,
            "node": "plan",
            "dispatch_role": "executor",
            "status": "completed",
        }
        chal_task = {
            "task_id": f"{self.wf_id}-plan-challenger",
            "workflow_id": self.wf_id,
            "node": "plan",
            "dispatch_role": "challenger",
            "status": "completed",
        }

        # Plan note published at ts=100
        plan_note = workflow_docs.append_note(
            self.wf_id, kind="plan", title="当前实施方案",
            body="## 技术方案与架构设计\n当前方案\n## 任务拆分与依赖关系\n- Task 1\n## 修改文件范围清单\n- Modify: x.py\n## 每个 Task 目标与验收标准\n- pytest\n## 风险与应对策略\n无",
            node="plan", task_id=plan_task["task_id"]
        )

        # Challenger review genuinely blocks the current plan at ts=200
        workflow_docs.append_note(
            self.wf_id, kind="review", title="针对当前方案的阻断审查",
            body=f"## 方案对抗性审查与可行性评估\n发现不可接受的死锁缺陷，拒绝放行。\nPlan-Ref: {plan_note['note_id']}\nHERDR_GATE_VERDICT: blocked",
            node="plan", task_id=chal_task["task_id"]
        )

        res = plan_stage_dispatch(self.wf_id, self.plan_node, [plan_task, chal_task], "需求文本")
        self.assertNotEqual(res.get("mode"), "dispatch", "Must NOT dispatch when current plan is legitimately blocked")
        self.assertEqual(len(res.get("specs", [])), 0, "Must NOT emit specs to redispatch challenger against unchanged blocked plan")

        from herdr.plan_parser import verify_plan_and_challenger
        ok, reason, details = verify_plan_and_challenger(self.wf_id, [plan_task, chal_task])
        self.assertFalse(ok)
        self.assertIn("challenger_blocked", reason, "Verification must report challenger_blocked for the current plan")

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
            "workflow_template": "software-development-v1",
            "staged_dev_flow": True,
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

    def test_challenger_blocked_triggers_same_node_redo_and_sequential_causal_redispatch(self):
        """When challenger gives [BLOCKED], auto_acceptance accepts it with verdict 'blocked',
        node_acceptance supersedes tasks with same_node_redo, and plan_stage_dispatch
        sequentially redispatches executor-r2 first (with blocker note), then challenger-r2."""
        from herdr import node_dispatch_store, workflow_docs
        from herdr.node_acceptance import acceptance_actions
        from services.controller.auto_acceptance import try_auto_accept
        from herdr.task_resources import begin_launch_intent, finish_launch_intent

        # 1. Setup completed executor with valid plan deliverable
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
        exec_task = {
            "task_id": f"{self.wf_id}-plan-executor", "workflow_id": self.wf_id, "node": "plan",
            "stage": "plan", "dispatch_role": "executor", "status": "completed", "run_id": "run-exec-1",
            "launch_intent_id": intent1["intent_id"], "execution_id": "exec-1",
            "dispatch_operation_id": op1_id,
        }
        self.store.save_task(exec_task)
        finish_launch_intent(self.store, intent1)

        plan_note = workflow_docs.append_note(
            self.wf_id, kind="plan", title="初始技术架构方案",
            body="## 技术方案与架构设计\n模块拆分说明\n## 任务拆分与依赖关系\n- Task 1\n## 修改文件范围清单\n- Modify: core.py\n## 每个 Task 目标与验收标准\n- 验证命令: pytest\n## 风险与应对策略\n无",
            node="plan", task_id=f"{self.wf_id}-plan-executor"
        )

        # 2. Challenger is dispatched and finishes in agent_done
        chal_task_id = f"{self.wf_id}-plan-challenger"
        chal_task = {
            "task_id": chal_task_id, "workflow_id": self.wf_id, "node": "plan",
            "stage": "plan", "dispatch_role": "challenger", "status": "agent_done",
            "run_id": "run-chal-1", "execution_id": "exec-1", "version": 1,
        }
        self.store.save_task(chal_task)

        # Challenger delivers review note with explicit BLOCKED verdict
        blocked_body = (
            "## 方案对抗性审查与可行性评估\n"
            "对主方案进行破防性质询与审查，发现严重设计矛盾：红测退出条件与用例不符。\n"
            f"Plan-Ref: {plan_note['note_id']}\n"
            "HERDR_GATE_VERDICT: blocked: 红测退出条件与用例不符"
        )
        workflow_docs.append_note(
            self.wf_id, kind="review", title="方案审查报告", body=blocked_body,
            node="plan", task_id=chal_task_id
        )

        # 3. try_auto_accept is called for challenger:
        # MUST succeed (accepted=True), transition status to 'completed', and persist stage_verdict='blocked'
        accept_res = try_auto_accept(self.ctrl, chal_task_id)
        self.assertTrue(accept_res.accepted, "Challenger review with valid structure and explicit BLOCKED verdict must be accepted")
        updated_chal = self.store.get_task(chal_task_id)
        self.assertEqual(updated_chal["status"], "completed")
        self.assertEqual(updated_chal["stage_verdict"], "blocked")

        # Simulate cleanup of tasks (completed -> cleaned)
        exec_task["status"] = "cleaned"
        self.store.save_task(exec_task)
        updated_chal["status"] = "cleaned"
        self.store.save_task(updated_chal)

        # 4. node_acceptance evaluates the plan node:
        # Both tasks are settled (cleaned), and one has stage_verdict='blocked'.
        # Must produce a 'rework' action for live tasks!
        tasks_in_store = self.store.list_tasks(workflow_id=self.wf_id)
        actions = acceptance_actions(self.wf_cfg, tasks_in_store, self.wf_id)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["op"], "rework")
        self.assertEqual(actions[0]["node_id"], "plan")
        self.assertIn("红测退出条件", actions[0]["note"])

        # Apply the rework action: tasks become superseded with same_node_redo
        from services.controller.node_acceptance import apply_node_acceptance
        written = apply_node_acceptance(self.ctrl, self.wf_id, self.wf_cfg, tasks_in_store)
        self.assertEqual(written, 2)

        # 5. Direct dispatch after rework:
        # Because challenger depends on executor, direct dispatch must ONLY dispatch executor-r2!
        tasks_after_rework = self.store.list_tasks(workflow_id=self.wf_id)
        dispatch_res1 = plan_stage_dispatch(self.wf_id, self.wf_cfg["nodes"][1], tasks_after_rework, self.wf["requirement"])
        self.assertEqual(dispatch_res1["mode"], "dispatch")
        self.assertEqual(len(dispatch_res1["specs"]), 1)
        spec1 = dispatch_res1["specs"][0]
        self.assertEqual(spec1["task_id"], f"{self.wf_id}-plan-executor-r2")
        self.assertEqual(spec1["dispatch_role"], "executor")
        self.assertEqual(spec1["supersedes"], f"{self.wf_id}-plan-executor")
        self.assertIn("红测退出条件", spec1.get("last_failure_note") or "")

        # 6. Simulate executor-r2 completing and delivering updated plan
        exec_r2_task = {
            "task_id": f"{self.wf_id}-plan-executor-r2", "workflow_id": self.wf_id, "node": "plan",
            "stage": "plan", "dispatch_role": "executor", "status": "cleaned",
            "run_id": "run-exec-2", "execution_id": "exec-1",
            "supersedes": f"{self.wf_id}-plan-executor",
        }
        self.store.save_task(exec_r2_task)
        old_exec = self.store.get_task(f"{self.wf_id}-plan-executor")
        old_exec["superseded_by"] = f"{self.wf_id}-plan-executor-r2"
        self.store.save_task(old_exec)

        new_plan_note = workflow_docs.append_note(
            self.wf_id, kind="plan", title="修订后的技术架构方案",
            body="## 技术方案与架构设计\n修订后的方案设计\n## 任务拆分与依赖关系\n- Task 1\n## 修改文件范围清单\n- Modify: core.py\n## 每个 Task 目标与验收标准\n- 验证命令: pytest\n## 风险与应对策略\n无风险",
            node="plan", task_id=f"{self.wf_id}-plan-executor-r2"
        )

        # 7. Direct dispatch after executor-r2 is complete:
        # Now challenger-r2 must be dispatched, targeting the NEW plan note!
        tasks_after_exec_r2 = self.store.list_tasks(workflow_id=self.wf_id)
        dispatch_res2 = plan_stage_dispatch(self.wf_id, self.wf_cfg["nodes"][1], tasks_after_exec_r2, self.wf["requirement"])
        self.assertEqual(dispatch_res2["mode"], "dispatch")
        self.assertEqual(len(dispatch_res2["specs"]), 1)
        spec2 = dispatch_res2["specs"][0]
        self.assertEqual(spec2["task_id"], f"{self.wf_id}-plan-challenger-r2")
        self.assertEqual(spec2["dispatch_role"], "challenger")
        self.assertEqual(spec2["supersedes"], f"{self.wf_id}-plan-challenger")
        self.assertEqual(spec2["plan_ref"], new_plan_note["note_id"])


class TestPlanSameNodeRedoDurableClosure(unittest.TestCase):
    """Keep Controller, CLI admission, launch intents and SQLite registration real."""

    def setUp(self):
        import os
        import subprocess

        import pytest

        from herdr import state_db
        from tests.test_recovery_entrypoints import _real_committed_launch_scene

        self.env = mock.patch.dict(os.environ)
        self.env.start()
        TestControllerPlanCausalIntegration.setUp(self)
        self.mp = pytest.MonkeyPatch()
        real_run = subprocess.run
        self.cli, self.store, _, self.calls, _ = _real_committed_launch_scene(self.tmp_dir, self.mp)
        with state_db.get_db_connection(self.store.db_path) as conn:
            conn.execute("DELETE FROM tasks WHERE task_id='old'")
            conn.execute("DELETE FROM workflows WHERE workflow_id='wf'")
        self.wf['project_root'] = str(self.tmp_dir / 'repo')
        self.wf['base_branch'] = 'main'
        self.store.save_workflow(self.wf)
        self.mp.setattr(self.cli, 'project_for_workflow', self.store.get_workflow)
        self.mp.setattr(self.cli, 'workflow_config_for', lambda wid: self.store.get_workflow(wid)['config'])
        self.native_transport = self.cli.subprocess.run
        import shutil

        from herdr.experience.client import ExperienceClient
        self.mp.setattr(ExperienceClient, '_request', lambda *a, **kw: {'ok': True, 'rules': []})
        shutil.copytree(self.tmp_dir / 'repo', self.tmp_dir / 'clones' / 'new', dirs_exist_ok=True)
        self.launches = []

        def transport(cmd, **kwargs):
            from herdr.task_cli_parser import build_parser
            if len(cmd) > 1 and cmd[1] == 'launch':
                args = build_parser().parse_args(cmd[1:])
                self.launches.append(args)
                self.cli.launch_task(args)
                return subprocess.CompletedProcess(cmd, 0, '', '')
            if cmd[0] == 'ps':
                return real_run(cmd, **kwargs)
            return self.native_transport(cmd, **kwargs)

        self.mp.setattr(self.ctrl.subprocess, 'run', transport)
        # Candidate availability is external to this dispatch test; CLI still validates
        # its temporary Git source and real launch identities.
        self.mp.setattr(self.ctrl, '_dispatch_candidate_ready', lambda *a, **kw: True)
        self.mp.setattr(self.ctrl, 'finalize_completed_task', self._cleanup)
        self.mp.setenv('HERDR_DIRECT_STAGE_DISPATCH', '1')
        self.mp.setenv('HERDR_AUTO_ACCEPT', '1')

    def tearDown(self):
        self.mp.undo()
        TestControllerPlanCausalIntegration.tearDown(self)
        self.env.stop()

    def _cleanup(self, task_id):
        # Native pane cleanup is substituted; accepted state is persisted by Controller.
        task = self.store.get_task(task_id)
        if task['status'] == 'completed':
            self.store.transition_task(task_id, 'cleanup_ready', 'test_native_cleanup')
            self.store.transition_task(task_id, 'cleaned', 'test_native_cleanup')

    def _sweep(self):
        for _ in range(4):
            self.ctrl.check_workflow_stage_advance(self.wf_id)

    def _launch_next(self, role):
        self._sweep()
        item = self.ctrl.coordinator_queue.get_nowait()
        assert item['node_id'] == 'plan'
        before = len(self.launches)
        self.ctrl._handle_coordinator_item(item)
        assert len(self.launches) == before + 1
        args = self.launches[-1]
        assert args.dispatch_role == role
        task = self.store.get_task(args.task_id)
        assert task['workflow_id'] == self.wf_id
        assert task['execution_id'] == 'exec-1'
        assert task['dispatch_operation_id'] == item['dispatch_operation_id']
        assert task['launch_intent_id']
        return task

    def _plan(self, task):
        from herdr import workflow_docs
        return workflow_docs.append_note(self.wf_id, kind='plan', title='技术方案', node='plan',
            task_id=task['task_id'], body=(
                '## 技术方案与架构设计\n模块职责边界与调用链已明确。\n'
                '## 任务拆分与依赖关系\n- Task 1: 修复持久化派发。\n'
                '## 修改文件范围清单\n- Modify: herdr/direct_dispatch.py\n'
                '## 每个 Task 目标与验收标准\n- 验证命令: pytest -q tests\n'
                '## 风险与应对策略\n回滚本次修改，保持状态格式兼容。'))['note_id']

    def _done(self, task, *, review=None, plan_ref=None):
        from herdr import workflow_docs
        if review:
            workflow_docs.append_note(self.wf_id, kind='review', title='方案对抗性质询', node='plan',
                task_id=task['task_id'], body=(
                    '## 方案对抗性审查与可行性评估\n'
                    '已核查架构、因果调用链、持久化边界及恢复路径，完成可行性风险评估。\n'
                    f'Plan-Ref: {plan_ref}\nHERDR_GATE_VERDICT: {review}'))
        self.store.transition_task(task['task_id'], 'agent_done', 'test_completion_receipt')
        self._done_event(task)
        assert self.store.get_task(task['task_id'])['status'] == 'cleaned'

    def _done_event(self, task):
        self.ctrl._handle_coordinator_item({'kind': 'task_event', 'task_id': task['task_id'],
            'event_type': 'done', 'key': task['task_id'] + ':done'})

    def _redo(self):
        from services.controller.node_acceptance import apply_node_acceptance
        executor = self._launch_next('executor')
        ref = self._plan(executor)
        self._done(executor)
        challenger = self._launch_next('challenger')
        self._done(challenger, review='blocked', plan_ref=ref)
        assert self.store.get_task(challenger['task_id'])['stage_verdict'] == 'blocked'
        assert apply_node_acceptance(self.ctrl, self.wf_id, self.wf_cfg,
                                     self.store.list_tasks(workflow_id=self.wf_id)) == 2
        for task in (executor, challenger):
            current = self.store.get_task(task['task_id'])
            assert current['status'] == 'superseded'
            assert current['replacement_pending'] is True
        self._snapshot('redo')
        return executor, challenger

    def _operation(self):
        from herdr import node_dispatch_store as nd
        return nd.operation_for_node(self.store.db_path, self.wf_id, 'plan')

    def _snapshot(self, phase):
        import json

        from herdr import recovery_store
        workflow = self.store.get_workflow(self.wf_id)
        snapshot = {'phase': phase, 'unhealthy_agents': workflow.get('unhealthy_agents'),
            'operations': [
                {'id': op['id'], 'status': op['status'], 'attempts': op['attempts'],
                 'payload': {k: op['payload'].get(k) for k in ('predecessors', 'prior_operation_id')},
                 'detail': {k: op['detail'].get(k) for k in ('expected_task_ids', 'reason')}}
                for op in recovery_store.list_operations(self.store.db_path, self.wf_id)
                if op['payload'].get('kind') == 'node_dispatch'],
            'tasks': [{k: task.get(k) for k in ('task_id', 'run_id', 'execution_id', 'dispatch_role',
                'agent', 'status', 'failure_reason', 'supersedes', 'superseded_by',
                'dispatch_operation_id', 'launch_intent_id')}
                for task in self.store.list_tasks(workflow_id=self.wf_id)]}
        print('SQLITE_STATE ' + json.dumps(snapshot, ensure_ascii=False))

    def test_same_node_redo_registers_executor_then_challenger_and_completes_node(self):
        old_executor, old_challenger = self._redo()
        executor = self._launch_next('executor')
        op1 = self._operation()
        assert op1['payload']['predecessors'] == [
            {'task_id': old_executor['task_id'], 'run_id': old_executor['run_id']}]
        assert op1['status'] == 'resolved'
        assert executor['supersedes'] == old_executor['task_id']
        assert executor['run_id'] != old_executor['run_id']
        assert not self.ctrl.is_node_complete(self.wf_id, 'plan')
        self._snapshot('executor-r2 registered')
        ref = self._plan(executor)
        self._done(executor)
        challenger = self._launch_next('challenger')
        op2 = self._operation()
        assert op2['payload']['prior_operation_id'] == op1['id']
        assert op2['payload']['predecessors'] == [
            {'task_id': old_challenger['task_id'], 'run_id': old_challenger['run_id']}]
        assert op2['status'] == 'resolved'
        assert challenger['supersedes'] == old_challenger['task_id']
        assert f'Plan-Ref: {ref}' in self.launches[-1].prompt
        self._snapshot('challenger-r2 registered')
        self._done(challenger, review='pass', plan_ref=ref)
        assert self.ctrl.is_node_complete(self.wf_id, 'plan')
        self._sweep()
        assert self.ctrl.coordinator_queue.get_nowait()['node_id'] == 'implementation'
        self._snapshot('plan complete')

    def test_causal_wait_for_executor_does_not_create_challenger_generation_or_escalate(self):
        from herdr import node_dispatch_store as nd
        self._redo()
        executor = self._launch_next('executor')
        op = self._operation()
        for now in (op['updated_at'] + 1000, op['updated_at'] + 10000):
            nd.reconcile_workflow(self.store.db_path, self.wf_id, now=now)
            self._sweep()
            assert self._operation()['id'] == op['id']
            assert self._operation()['status'] == 'resolved'
            assert self.ctrl.coordinator_queue.empty()
            assert not self.ctrl.is_node_complete(self.wf_id, 'plan')
        assert self.store.get_task(executor['task_id'])['status'] == 'dispatched'

    def test_duplicate_sweeps_and_done_events_are_idempotent(self):
        from herdr import recovery_store
        self._redo()
        executor = self._launch_next('executor')
        self._plan(executor)
        self._done(executor)
        self._done_event(executor)
        challenger = self._launch_next('challenger')
        before = (len(self.store.list_tasks()), len(recovery_store.list_operations(self.store.db_path, self.wf_id)), len(self.launches))
        for _ in range(3):
            self._done_event(executor)
            self._sweep()
        assert self.ctrl.coordinator_queue.empty()
        assert before == (len(self.store.list_tasks()), len(recovery_store.list_operations(self.store.db_path, self.wf_id)), len(self.launches))
        assert challenger['dispatch_role'] == 'challenger'

    def test_challenge_launch_missing_reports_exact_missing_task_after_deadline(self):
        import subprocess

        from herdr import node_dispatch_store as nd
        _, old_challenger = self._redo()
        executor = self._launch_next('executor')
        self._plan(executor)
        self._done(executor)
        self._sweep()
        item = self.ctrl.coordinator_queue.get_nowait()
        # Unknown CLI transport outcome, with no registered Task or absence receipt.
        self.mp.setattr(self.ctrl.subprocess, 'run', lambda cmd, **kw:
                        subprocess.CompletedProcess(cmd, 1, '', 'transport outcome unknown'))
        self.ctrl._handle_coordinator_item(item)
        op = self._operation()
        nd.reconcile_workflow(self.store.db_path, self.wf_id, now=op['detail']['deadline_at'] + 1)
        op = self._operation()
        assert op['status'] == 'waiting_human'
        assert op['detail']['reason'] == 'dispatch_task_missing'
        assert op['detail']['missing_expected_task_ids'] == [old_challenger['task_id'] + '-r2']
        assert op['detail']['missing_predecessor_ids'] == [old_challenger['task_id']]

    def _restart_controller(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('redo_restarted_controller', self.root / 'services/herdr-controller.py')
        ctrl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ctrl)
        for name in ('_get_store', 'load_tasks', 'workflow_closed', 'project_for_workflow',
                     'workflow_config_for', 'coordinator_pane_for_workflow', 'coordinator_status',
                     'get_stage_policy', 'shared_docs_block', 'maybe_compact_coordinator',
                     '_scheduler_frozen_candidate_identity', '_scheduler_resolve_candidate_and_plan',
                     '_scheduler_join_gate_allows', 'notify_attention', '_dispatch_candidate_ready',
                     'finalize_completed_task'):
            self.mp.setattr(ctrl, name, getattr(self.ctrl, name))
        ctrl.STAGE_STATE_FILE = str(self.tmp_dir / 'stage-state.json')
        self.ctrl = ctrl

    def test_controller_restart_resumes_challenger_generation_without_duplicate_dispatch(self):
        from herdr import node_dispatch_store as nd
        self._redo()
        executor = self._launch_next('executor')
        executor_op = self._operation()
        # Restart after registration: resolved stage must not be resent.
        self._restart_controller()
        self._sweep()
        assert self.ctrl.coordinator_queue.empty()
        assert self._operation()['id'] == executor_op['id']
        self._plan(executor)
        self._done(executor)
        self._sweep()
        queued = self.ctrl.coordinator_queue.get_nowait()
        op = self._operation()
        assert queued['dispatch_operation_id'] == op['id']
        # Lose an in-memory claimed item before start; a fresh Controller reclaims
        # the same SQLite generation once its bounded lease expires.
        self._restart_controller()
        now = op['lease_until'] + 1
        self.mp.setattr(self.ctrl.time, 'time', lambda: now)
        nd.reconcile_workflow(self.store.db_path, self.wf_id, now=now)
        challenger = self._launch_next('challenger')
        assert challenger['dispatch_operation_id'] == op['id']
        assert len([a for a in self.launches if a.task_id.endswith('challenger-r2')]) == 1
        self._sweep()
        assert self.ctrl.coordinator_queue.empty()

    def test_executor_quota_exhaustion_redispatches_executor_before_challenger(self):
        import json
        import subprocess

        from herdr import agent_router, deep_preflight
        self._redo()
        self.wf = self.store.get_workflow(self.wf_id)
        self.wf.update(project_id='controlled', healthy_agents=['codex', 'opencode'], unhealthy_agents={})
        self.wf['config']['nodes'][1]['agent_policy']['preferred'] = ['codex', 'opencode']
        self.store.save_workflow(self.wf)
        pool = self.tmp_dir / 'pools.json'
        pool.write_text(json.dumps({'projects': {'controlled': {
            'allowed_agents': ['codex', 'opencode'], 'disabled_agents': [], 'max_concurrency_per_agent': 3}}}))
        self.mp.setattr(agent_router, 'POOLS_FILE', pool)
        self.mp.setattr(agent_router, 'RESERVATIONS_FILE', self.tmp_dir / 'reservations.json')
        self.mp.setattr(agent_router, 'ROUTER_LOCK_FILE', self.tmp_dir / 'router.lock')
        self.mp.setattr(agent_router, '_get_store', lambda: self.store)
        self.mp.setattr(agent_router, 'workflow_config_for', lambda wid: self.store.get_workflow(wid)['config'])
        self.mp.setattr(deep_preflight, 'preflight_identity', lambda *a, **kw: {'verifiable': True, 'fingerprint': 'controlled'})
        self.mp.setattr(deep_preflight, 'inspect', lambda *a, **kw: [
            {'agent': agent, 'final_status': 'READY', 'request_verified': True,
             'preflight_identity': {'verifiable': True, 'fingerprint': 'controlled'}}
            for agent in kw['target_agents']])
        self.mp.setattr(self.cli, 'choose_agent', agent_router.choose_agent)
        self.mp.setattr(self.cli, 'release_agent_reservation', agent_router.release_agent_reservation)
        executor = self._launch_next('executor')
        assert executor['agent'] == 'codex'
        self.mp.setattr(self.ctrl, '_completion_marker_snapshot', lambda task: (False, "You've reached your usage limit. Try again later."))
        self.ctrl.handle_event(executor['task_id'], 'blocked')
        failed = self.store.get_task(executor['task_id'])
        assert failed['status'] == 'failed'
        assert failed['failure_reason'] == 'token_exhausted'
        assert self.store.get_workflow(self.wf_id)['unhealthy_agents']['codex'] == 'TOKEN_EXHAUSTED'
        self._snapshot('executor quota exhausted')
        prior_transport = self.ctrl.subprocess.run
        def recovery_transport(cmd, **kw):
            if len(cmd) > 1 and cmd[1] == 'supersede':
                self.cli.supersede_task(cmd[2], allow_pending=True, reason='auto-recover: infrastructure failure')
                return subprocess.CompletedProcess(cmd, 0, '', '')
            return prior_transport(cmd, **kw)
        self.mp.setattr(self.ctrl.subprocess, 'run', recovery_transport)
        assert self.ctrl.recover_infra_failed_tasks(self.wf_id)
        assert self.store.get_task(executor['task_id'])['status'] == 'superseded'
        replacement = self._launch_next('executor')
        assert replacement['task_id'].endswith('executor-r3')
        assert replacement['agent'] == 'opencode'
        assert replacement['agent'] != executor['agent']
        assert replacement['run_id'] != executor['run_id']
        assert replacement['supersedes'] == executor['task_id']
        self._snapshot('quota recovery registered')
        self._sweep()
        assert self.ctrl.coordinator_queue.empty()
        active = [t for t in self.store.list_tasks(workflow_id=self.wf_id)
                  if t.get('dispatch_role') == 'executor' and t['status'] == 'dispatched']
        assert [t['task_id'] for t in active] == [replacement['task_id']]
        ref = self._plan(replacement)
        self._done(replacement)
        # A is exhausted and B authored the plan: two agents cannot provide an
        # independent reviewer. Preserve that Router boundary, then provide a
        # controlled C reviewer to exercise the remaining dispatch/Plan-Ref chain.
        with self.assertRaises(agent_router.RouterIsolationRejection):
            agent_router.choose_agent(self.wf_id, 'plan', 'docs', requested='auto',
                                      dispatch_role='challenger', dispatch_round=2)
        pool.write_text(json.dumps({'projects': {'controlled': {
            'allowed_agents': ['codex', 'opencode', 'claude'], 'disabled_agents': []}}}))
        self.store.update_workflow_metadata(self.wf_id, {'healthy_agents': ['opencode', 'claude']})
        challenger = self._launch_next('challenger')
        assert f'Plan-Ref: {ref}' in self.launches[-1].prompt
        assert challenger['dispatch_role'] == 'challenger'

    def test_controller_restart_after_start_does_not_replay_unknown_launch(self):
        import subprocess

        from herdr import node_dispatch_store as nd
        self._redo()
        self._sweep()
        item = self.ctrl.coordinator_queue.get_nowait()
        self.mp.setattr(self.ctrl.subprocess, 'run', lambda cmd, **kw:
                        subprocess.CompletedProcess(cmd, 1, '', 'transport unknown'))
        self.ctrl._handle_coordinator_item(item)
        op = self._operation()
        assert op['started'] == 1
        assert op['status'] == 'awaiting_result'
        self._restart_controller()
        now = op['updated_at'] + 120
        self.mp.setattr(self.ctrl.time, 'time', lambda: now)
        nd.reconcile_workflow(self.store.db_path, self.wf_id, now=now)
        self._sweep()
        assert self.ctrl.coordinator_queue.empty()
        assert self._operation()['id'] == op['id']
        assert self._operation()['attempts'] == 1
        assert self.store.get_task(self.wf_id + '-plan-executor-r2') is None


if __name__ == "__main__":
    unittest.main()
