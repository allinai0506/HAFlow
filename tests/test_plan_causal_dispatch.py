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
            "body": "## 技术方案与架构设计\n核心架构解耦\n## 任务拆分与依赖关系\n- Task 1\n## 每个 Task 目标与验收标准\n验收通过\n## 风险与应对策略\n无风险",
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


if __name__ == "__main__":

    unittest.main()
