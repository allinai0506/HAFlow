import unittest
from pathlib import Path
from herdr.workflow import load_template, validate_workflow_dag


class TestCodeCraftV1Template(unittest.TestCase):
    def setUp(self):
        tmpl_path = Path(__file__).resolve().parents[1] / "workflow_templates" / "code-craft-v1.yaml"
        self.tmpl = load_template(str(tmpl_path))
        self.nodes = self.tmpl["nodes"]
        self.node_map = {n["id"]: n for n in self.nodes}

    def test_dag_is_valid(self):
        validate_workflow_dag(self.nodes)
        self.assertEqual(len(self.nodes), 4)
        expected_ids = ["craft", "cleanroom_test", "cleanroom_review", "wrapup"]
        self.assertEqual([n["id"] for n in self.nodes], expected_ids)

    def test_craft_node_spec(self):
        craft = self.node_map["craft"]
        self.assertEqual(craft.get("depends_on"), [])
        self.assertFalse(craft.get("parallel", True))
        self.assertEqual(craft.get("default_integration_mode"), "git")
        self.assertEqual(craft.get("default_task_type"), "feat")

        policy = craft.get("agent_policy", {})
        self.assertEqual(policy.get("max_concurrency"), 1)
        self.assertFalse(policy.get("parallel", True))
        self.assertTrue(policy.get("allow_soft_degrade"))

        rules_text = " ".join(craft.get("rules", []))
        self.assertIn("单工位", rules_text)
        self.assertIn("RED", rules_text)
        self.assertIn("GREEN", rules_text)

    def test_cleanroom_test_node_spec(self):
        test_node = self.node_map["cleanroom_test"]
        self.assertEqual(test_node.get("depends_on"), ["craft"])
        self.assertFalse(test_node.get("parallel", True))
        self.assertEqual(test_node.get("default_integration_mode"), "none")
        self.assertEqual(test_node.get("default_task_type"), "test")

        policy = test_node.get("agent_policy", {})
        self.assertEqual(policy.get("max_concurrency"), 1)
        self.assertEqual(policy.get("exclude_stage_agents"), ["craft"])
        self.assertTrue(policy.get("allow_soft_degrade"))

    def test_cleanroom_review_node_spec(self):
        review_node = self.node_map["cleanroom_review"]
        self.assertEqual(review_node.get("depends_on"), ["craft"])
        self.assertFalse(review_node.get("parallel", True))
        self.assertEqual(review_node.get("default_integration_mode"), "none")
        self.assertEqual(review_node.get("default_task_type"), "test")

        policy = review_node.get("agent_policy", {})
        self.assertEqual(policy.get("max_concurrency"), 1)
        self.assertEqual(policy.get("exclude_stage_agents"), ["craft"])
        self.assertTrue(policy.get("allow_soft_degrade"))

    def test_wrapup_node_spec(self):
        wrapup = self.node_map["wrapup"]
        self.assertEqual(sorted(wrapup.get("depends_on", [])), sorted(["cleanroom_test", "cleanroom_review"]))
        self.assertFalse(wrapup.get("parallel", True))
        self.assertEqual(wrapup.get("artifact_mode"), "shared_artifacts")

        policy = wrapup.get("agent_policy", {})
        self.assertEqual(policy.get("max_concurrency"), 1)
        self.assertTrue(policy.get("allow_soft_degrade"))

        rules = "\n".join(wrapup.get("rules", []))
        self.assertIn("交付 PR", rules)
        self.assertIn("six-step-finish", rules)

    def test_selective_replan_and_reverification_configs(self):
        replan = self.tmpl.get("selective_replan", {})
        self.assertEqual(replan.get("mode"), "explicit_task_targets")
        self.assertEqual(replan.get("retry_node"), "craft")

        reverif = self.tmpl.get("reverification", {})
        self.assertIn("cleanroom_test", reverif)
        self.assertIn("cleanroom_review", reverif)


if __name__ == "__main__":
    unittest.main()
