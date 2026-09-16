import unittest
from herdr.workflow import load_template, validate_workflow_dag


class TestSoftwareDevelopmentV1Template(unittest.TestCase):
    def setUp(self):
        self.tmpl = load_template("software-development-v1")
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

    def test_requirements_dual_adversarial_spec(self):
        node = self.node_map["requirements"]
        policy = node.get("agent_policy", {})
        self.assertEqual(policy.get("max_agents"), 2)
        self.assertTrue(node.get("parallel"))

        roles = policy.get("roles", [])
        role_names = [r.get("name") for r in roles]
        self.assertEqual(role_names, ["executor", "challenger"])

        outputs = node.get("required_outputs", [])
        self.assertTrue(any("需求规格与验收标准" in o for o in outputs))
        self.assertTrue(any("需求对抗审查与边界漏洞清单" in o for o in outputs))

    def test_plan_dual_adversarial_spec(self):
        node = self.node_map["plan"]
        policy = node.get("agent_policy", {})
        self.assertEqual(policy.get("max_agents"), 2)
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
        self.assertEqual(policy.get("max_agents"), 3)
        self.assertTrue(node.get("parallel"))

        rules_text = " ".join(node.get("rules", []))
        self.assertTrue("解耦" in rules_text or "并发" in rules_text)

    def test_test_stage_exclusion_and_single_pane(self):
        node = self.node_map["test"]
        policy = node.get("agent_policy", {})
        self.assertEqual(policy.get("max_agents"), 1)
        self.assertFalse(node.get("parallel"))
        self.assertEqual(policy.get("exclude_stage_agents"), ["implementation"])

    def test_review_stage_exclusion_and_single_pane(self):
        node = self.node_map["review"]
        policy = node.get("agent_policy", {})
        self.assertEqual(policy.get("max_agents"), 1)
        self.assertFalse(node.get("parallel"))
        self.assertEqual(policy.get("exclude_stage_agents"), ["implementation"])

    def test_wrapup_stage_exclusion_and_single_pane(self):
        node = self.node_map["wrapup"]
        policy = node.get("agent_policy", {})
        self.assertEqual(policy.get("max_agents"), 1)
        self.assertFalse(node.get("parallel"))
        self.assertEqual(policy.get("exclude_stage_agents"), ["implementation"])
