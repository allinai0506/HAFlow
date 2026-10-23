import unittest
from pathlib import Path
from herdr.workflow import load_template, validate_workflow_dag


class TestSoftwareDevelopmentV1Template(unittest.TestCase):
    def setUp(self):
        self.tmpl = load_template(str(Path(__file__).resolve().parents[1] / "workflow_templates/software-development-v1.yaml"))
        self.nodes = self.tmpl["nodes"]
        self.node_map = {n["id"]: n for n in self.nodes}

    def test_dag_is_valid(self):
        validate_workflow_dag(self.nodes)
        self.assertEqual(len(self.nodes), 6)
        expected_ids = [
            "requirements", "plan", "implementation", "test", "review", "wrapup"
        ]
        self.assertEqual([n["id"] for n in self.nodes], expected_ids)

    def test_no_nodes_have_unbounded_parallelism_rule(self):
        unbounded_phrase = "同一阶段允许多个 Task 使用不同 Agent 并行协作"
        for node in self.nodes:
            rules_text = " ".join(node.get("rules", []))
            self.assertNotIn(
                unbounded_phrase,
                rules_text,
                f"Node '{node['id']}' contains unbounded parallelism phrase",
            )

    def test_requirements_is_a_single_drafter(self):
        node = self.node_map["requirements"]
        policy = node.get("agent_policy", {})
        self.assertEqual(node.get("max_tasks_per_node"), 1)
        self.assertEqual(policy.get("max_concurrency"), 1)
        self.assertFalse(node.get("parallel"))

        roles = policy.get("roles", [])
        role_names = [r.get("name") for r in roles]
        self.assertEqual(role_names, ["executor"])

        outputs = node.get("required_outputs", [])
        self.assertTrue(any("需求规格与验收标准" in o for o in outputs))
        self.assertFalse(any("对抗" in o for o in outputs))
        rules = " ".join(node.get("rules", []))
        self.assertIn("unified-dev-flow", rules)
        self.assertIn("brainstorming", rules)
        self.assertIn("工作区", rules)

    def test_nodes_follow_unified_dev_flow_stages(self):
        plan = " ".join(self.node_map["plan"].get("rules", []))
        implementation = " ".join(self.node_map["implementation"].get("rules", []))
        test = " ".join(self.node_map["test"].get("rules", []))
        review = " ".join(self.node_map["review"].get("rules", []))
        wrapup = " ".join(self.node_map["wrapup"].get("rules", []))
        self.assertIn("writing-plans", plan)
        self.assertIn("plan-eng-review", plan)
        self.assertIn("test-driven-development", implementation)
        self.assertIn("ai-slop-cleaner", implementation)
        self.assertIn("verification-before-completion", test)
        self.assertIn("非界面改动不要强开浏览器", test)
        self.assertIn("google-code-review", review)
        self.assertIn("三轮", review)
        self.assertIn("交付停在需求记下的上限", wrapup)
        self.assertIn("knowledge-capture", wrapup)

    def test_plan_dual_adversarial_spec(self):
        node = self.node_map["plan"]
        policy = node.get("agent_policy", {})
        self.assertEqual(policy.get("max_concurrency"), 2)
        self.assertTrue(node.get("parallel"))

        roles = policy.get("roles", [])
        role_names = [r.get("name") for r in roles]
        self.assertEqual(role_names, ["executor", "challenger"])

        outputs = node.get("required_outputs", [])
        self.assertTrue(any("技术架构与解耦任务拆解" in o for o in outputs))
        self.assertTrue(any("方案对抗审查与可行性风险评估" in o for o in outputs))

    def test_implementation_adaptive_parallel_spec(self):
        node = self.node_map["implementation"]
        policy = node.get("agent_policy", {})
        self.assertEqual(policy.get("max_concurrency"), 3)
        self.assertTrue(node.get("parallel"))

        rules_text = " ".join(node.get("rules", []))
        self.assertTrue("解耦" in rules_text or "并发" in rules_text)

    def test_test_stage_exclusion_and_single_pane(self):
        node = self.node_map["test"]
        policy = node.get("agent_policy", {})
        self.assertEqual(policy.get("max_concurrency"), 1)
        self.assertFalse(node.get("parallel"))
        self.assertEqual(policy.get("exclude_stage_agents"), ["implementation", "wrapup"])

    def test_review_stage_exclusion_and_single_pane(self):
        node = self.node_map["review"]
        policy = node.get("agent_policy", {})
        self.assertEqual(policy.get("max_concurrency"), 1)
        self.assertFalse(node.get("parallel"))
        self.assertEqual(policy.get("exclude_stage_agents"), ["implementation", "wrapup"])

    def test_wrapup_stage_exclusion_and_single_pane(self):
        node = self.node_map["wrapup"]
        policy = node.get("agent_policy", {})
        self.assertEqual(policy.get("max_concurrency"), 1)
        self.assertFalse(node.get("parallel"))
        self.assertEqual(policy.get("exclude_stage_agents"), ["implementation"])

    def test_wrapup_requires_delivery_pr_before_finish(self):
        """交付 PR 是 wrapup 的必做前置：push + 建 PR，且严禁自动合并。

        回归背景：wf-nexusarchive-0917-01 收官时 wrapup 只做只读合并确认并
        DEFERRED，全链路（herdr-task integrate / six-step-finish）无 push 与
        PR 创建动作，交付分支停留在目标仓本地，PR 只能由人工/总指挥补交。
        """
        rules = "\n".join(self.node_map["wrapup"].get("rules", []))
        self.assertIn("交付 PR 前置", rules)
        self.assertIn("herdr-task create-pr", rules)
        self.assertIn("herdr-task commit", rules)
        self.assertIn("herdr-task integrate", rules)
        self.assertIn("严禁在收尾中自动执行 PR 合并", rules)
        self.assertIn("PR URL", rules)
        # 六步执行规则的未合入分支必须携带 PR URL 指引
        self.assertIn("已建 PR 但未合入", rules)

    def test_wrapup_requires_pr_review_closure(self):
        """PR 审核闭环是 wrapup 的必做前置：CI 全绿 + 未决 P1=0 + findings 处置。

        回归背景：wf-haflow-1015-01 收尾时未创建交付 PR、也未检查 GitHub
        （Codex）审核 findings，P1/P2 在交付后才发现，导致返工。规则缺失点：
        六步收尾从未包含『抓取 PR 审核结果并闭环』这一环，且 PR 职责可被
        『属控制器职责』措辞规避。
        """
        rules = "\n".join(self.node_map["wrapup"].get("rules", []))
        self.assertIn("PR 审核闭环", rules)
        self.assertIn("gh pr checks", rules)
        self.assertIn("未决 P1", rules)
        # 职责单一性：不得以“属控制器职责”跳过 PR 入口或审核闭环
        self.assertIn("属控制器职责", rules)
        # 机器回执：完成转换由 record-review-closure 回执校验（非纯文本）
        self.assertIn("record-review-closure", rules)
        # 分页：多页 PR 不得只查第一页而漏报 findings
        self.assertIn("--paginate", rules)
        # 结构化 findings：回执须报机器可读的 P1=0 计数
        self.assertIn("--unresolved-p1", rules)
        # 修复提交再门禁：新 head 必须重新过独立门禁/重建交付身份
        self.assertIn("新的有效交付身份", rules)
        # 契约启用标记：新工作流 wrapup 节点必须显式 opt-in（兼容旧工作流）
        self.assertTrue(self.node_map["wrapup"].get("review_closure_required"))
        outputs = self.node_map["wrapup"].get("required_outputs", [])
        self.assertTrue(any("审核闭环证据" in o for o in outputs))
        self.assertTrue(any("CI 状态" in o for o in outputs))


class TestNodeBudgets(unittest.TestCase):
    def test_bundled_budgets_survive_normalization(self):
        from pathlib import Path
        template = load_template(str(Path(__file__).resolve().parents[1] / 'workflow_templates/software-development-v1.yaml'))
        self.assertEqual({n['id']: n['max_tasks_per_node'] for n in template['nodes']},
                         {'requirements': 1, 'plan': 2, 'implementation': 12,
                          'test': 4, 'review': 4, 'wrapup': 1})
