"""Verifier node identity must come from declared template semantics.

Regression for wf-haflow-1021-01: code-craft-v1 declares its gates as
cleanroom_test / cleanroom_review, so every protection keyed on the literal
ids {'test', 'review'} silently skipped them. Both cleanroom stations were
dispatched with candidate_sha=None and verified the baseline instead of the
frozen candidate, so a whole gate round produced conclusions about the wrong
version.
"""

import unittest
from pathlib import Path

from herdr.node_roles import (
    candidate_identity_applies,
    is_verifier_node,
    is_verifier_node_id,
    is_verifier_task,
    verifier_node_ids,
)
from herdr.workflow import load_template

TEMPLATES = Path(__file__).resolve().parents[1] / "workflow_templates"


def template(name):
    return load_template(str(TEMPLATES / f"{name}.yaml"))


class VerifierNodeIdentityTest(unittest.TestCase):
    def test_code_craft_cleanroom_gates_are_verifiers(self):
        self.assertEqual(
            verifier_node_ids(template("code-craft-v1")),
            {"cleanroom_test", "cleanroom_review"},
        )

    def test_legacy_literal_ids_keep_their_classification(self):
        for name in ("software-development-v1", "spec-development-v1"):
            with self.subTest(template=name):
                self.assertEqual(verifier_node_ids(template(name)), {"test", "review"})

    def test_producer_nodes_are_not_verifiers(self):
        nodes = {n["id"]: n for n in template("code-craft-v1")["nodes"]}
        self.assertFalse(is_verifier_node(nodes["craft"]))
        self.assertFalse(is_verifier_node(nodes["wrapup"]))
        self.assertTrue(is_verifier_node(nodes["cleanroom_test"]))
        self.assertTrue(is_verifier_node(nodes["cleanroom_review"]))

    def test_declared_task_type_alias_review_is_a_verifier(self):
        self.assertTrue(is_verifier_node({"id": "audit", "default_task_type": "review"}))

    def test_business_task_type_does_not_make_a_node_a_verifier(self):
        # resolve_task_type falls back to DEFAULT_TASK_TYPE for business types;
        # a node declaring `quote` produces deliverables, it does not verify one.
        self.assertFalse(is_verifier_node({"id": "quote", "default_task_type": "quote"}))

    def test_templates_without_a_declared_gate_have_no_verifiers(self):
        for name in ("business-task-v1", "context-smoke-test", "customer-service",
                     "general-task-v1", "seo-audit-v1"):
            with self.subTest(template=name):
                self.assertEqual(verifier_node_ids(template(name)), set())

    def test_newly_recognised_business_verifiers(self):
        self.assertEqual(verifier_node_ids(template("bidding")), {"compliance"})
        self.assertEqual(
            verifier_node_ids(template("business-research-v1")), {"executive_briefing"}
        )

    def test_normalized_dispatch_node_keeps_its_classification(self):
        # normalize_node drops default_task_type in favour of the resolved
        # task_type plus the raw declared_task_type; dispatch-time callers only
        # ever see that shape.
        from herdr.direct_dispatch import normalize_node

        for node_id, declared, expected in (
            ("cleanroom_test", "test", True),
            ("cleanroom_review", "review", True),
            ("test", None, True),
            ("craft", "feat", False),
            ("wrapup", "docs", False),
            ("quote", "quote", False),
        ):
            node = normalize_node({"id": node_id, "default_task_type": declared})
            self.assertIsNotNone(node, node_id)
            self.assertNotIn("default_task_type", node)
            with self.subTest(node_id=node_id):
                self.assertEqual(is_verifier_node(node), expected)

    def test_malformed_nodes_never_raise(self):
        for node in (None, {}, "test", 42, {"id": None}, {"default_task_type": None}):
            with self.subTest(node=node):
                self.assertFalse(is_verifier_node(node))
        self.assertEqual(verifier_node_ids(None), set())
        self.assertEqual(verifier_node_ids({}), set())
        self.assertEqual(verifier_node_ids({"nodes": [None, "x"]}), set())


class VerifierTaskIdentityTest(unittest.TestCase):
    """Persisted tasks carry no node dict, only the type captured at dispatch."""

    def test_cleanroom_task_shape_from_production(self):
        self.assertTrue(is_verifier_task(
            {"node": "cleanroom_test", "task_type": "test", "integration_mode": "none"}))
        self.assertFalse(is_verifier_task(
            {"node": "craft", "task_type": "feat", "integration_mode": "git"}))

    def test_legacy_task_without_a_task_type_still_classifies(self):
        self.assertTrue(is_verifier_task({"node": "test"}))
        self.assertTrue(is_verifier_task({"stage": "review"}))

    def test_declared_type_wins_over_resolved_type(self):
        self.assertTrue(is_verifier_task(
            {"node": "audit", "declared_task_type": "review", "task_type": "test"}))

    def test_malformed_tasks_never_raise(self):
        for task in (None, {}, "test", {"node": None, "task_type": None}):
            with self.subTest(task=task):
                self.assertFalse(is_verifier_task(task))


class VerifierNodeIdTest(unittest.TestCase):
    """Id-level entry point for callers that never receive the node dict."""

    CONFIG = {"nodes": [
        {"id": "craft", "default_task_type": "feat"},
        {"id": "cleanroom_test", "default_task_type": "test"},
    ]}

    def test_declared_verifier_id_needs_the_config(self):
        self.assertTrue(is_verifier_node_id(self.CONFIG, "cleanroom_test"))
        self.assertFalse(is_verifier_node_id(self.CONFIG, "craft"))

    def test_legacy_ids_survive_an_unresolvable_config(self):
        # A missing definition must not silently downgrade a gate that used to
        # be protected.
        for config in (None, {}, {"nodes": []}):
            for node_id in ("test", "review"):
                with self.subTest(config=config, node_id=node_id):
                    self.assertTrue(is_verifier_node_id(config, node_id))

    def test_declared_verifier_is_absent_without_the_config(self):
        self.assertFalse(is_verifier_node_id(None, "cleanroom_test"))

    def test_malformed_ids_never_raise(self):
        for node_id in (None, "", "   ", 42):
            with self.subTest(node_id=node_id):
                self.assertFalse(is_verifier_node_id(self.CONFIG, node_id))


class CandidateIdentityScopeTest(unittest.TestCase):
    """Candidate SHA is a Git fact; a context workflow has nothing to prove."""

    def test_git_and_legacy_workflows_carry_candidate_identity(self):
        self.assertTrue(candidate_identity_applies({"execution": {"mode": "git"}}))
        self.assertTrue(candidate_identity_applies({}))
        self.assertTrue(candidate_identity_applies(None))

    def test_context_workflows_have_no_candidate_identity(self):
        self.assertFalse(candidate_identity_applies({"execution": {"mode": "context"}}))


if __name__ == "__main__":
    unittest.main()
