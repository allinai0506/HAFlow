import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

# Ensure herdr root is on sys.path
HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr.workflow import (
    list_templates,
    load_template,
    validate_workflow_dag,
    get_ready_nodes,
    is_workflow_completed,
    normalize_workflow,
)
from herdr.agent_router import choose_agent, _candidate_order
from herdr import workflow as workflow_engine


class TestWorkflowTemplates(unittest.TestCase):
    """Test loading, parsing, and listing workflow templates."""

    def test_list_templates_bundled(self):
        templates = list_templates()
        self.assertIn("software-development-v1", templates)
        self.assertIn("bidding", templates)
        self.assertIn("customer-service", templates)
        self.assertIn("seo-audit-v1", templates)
        self.assertIn("general-task-v1", templates)

        dev_tmpl = templates["software-development-v1"]
        self.assertEqual(dev_tmpl["node_count"], 6)

        bidding_tmpl = templates["bidding"]
        self.assertEqual(bidding_tmpl["node_count"], 7)

        seo_tmpl = templates["seo-audit-v1"]
        self.assertEqual(seo_tmpl["node_count"], 4)

        gen_tmpl = templates["general-task-v1"]
        self.assertEqual(gen_tmpl["node_count"], 3)

    def test_load_template_content(self):
        tmpl = load_template("software-development-v1")
        self.assertEqual(tmpl["name"], "software-development-v1")
        self.assertEqual(len(tmpl["nodes"]), 6)

        node_ids = [n["id"] for n in tmpl["nodes"]]
        self.assertEqual(
            node_ids,
            ["requirements", "plan", "implementation", "test", "review", "wrapup"]
        )

        # First node has no depends_on
        self.assertEqual(tmpl["nodes"][0].get("depends_on", []), [])
        # Second node depends on first
        self.assertEqual(tmpl["nodes"][1].get("depends_on", []), ["requirements"])

    def test_load_seo_audit_template(self):
        tmpl = load_template("seo-audit-v1")
        self.assertEqual(tmpl["name"], "seo-audit-v1")
        self.assertEqual(len(tmpl["nodes"]), 4)
        node_ids = [n["id"] for n in tmpl["nodes"]]
        self.assertEqual(
            node_ids,
            [
                "tech_crawling_audit",
                "keyword_and_content_matrix",
                "remediation_roadmap",
                "executive_delivery",
            ],
        )
        validate_workflow_dag(tmpl["nodes"])

    def test_load_general_task_template(self):
        tmpl = load_template("general-task-v1")
        self.assertEqual(tmpl["name"], "general-task-v1")
        self.assertEqual(len(tmpl["nodes"]), 3)
        node_ids = [n["id"] for n in tmpl["nodes"]]
        self.assertEqual(
            node_ids,
            ["intake_and_scoping", "deep_execution", "review_and_delivery"],
        )
        validate_workflow_dag(tmpl["nodes"])

    def test_load_non_existent_template(self):
        with self.assertRaises(FileNotFoundError):
            load_template("non_existent_template_xyz")


class TestDAGValidation(unittest.TestCase):
    """Test DAG validation including Kahn's algorithm and cycle detection."""

    def test_valid_dag(self):
        nodes = [
            {"id": "step1"},
            {"id": "step2", "depends_on": ["step1"]},
            {"id": "step3", "depends_on": ["step2"]},
        ]
        # Should not raise
        validate_workflow_dag(nodes)

    def test_branching_and_merging_dag(self):
        nodes = [
            {"id": "parse"},
            {"id": "branch_a", "depends_on": ["parse"]},
            {"id": "branch_b", "depends_on": ["parse"]},
            {"id": "merge", "depends_on": ["branch_a", "branch_b"]},
        ]
        # Valid parallel branching and merge
        validate_workflow_dag(nodes)

    def test_cycle_detection(self):
        nodes = [
            {"id": "a", "depends_on": ["c"]},
            {"id": "b", "depends_on": ["a"]},
            {"id": "c", "depends_on": ["b"]},
        ]
        with self.assertRaises(ValueError) as ctx:
            validate_workflow_dag(nodes)
        self.assertIn("cycle", str(ctx.exception).lower())

    def test_unknown_dependency(self):
        nodes = [
            {"id": "a", "depends_on": ["ghost_step"]},
        ]
        with self.assertRaises(ValueError) as ctx:
            validate_workflow_dag(nodes)
        self.assertIn("unknown node", str(ctx.exception).lower())

    def test_duplicate_node_ids(self):
        nodes = [
            {"id": "a"},
            {"id": "a"},
        ]
        with self.assertRaises(ValueError) as ctx:
            validate_workflow_dag(nodes)
        self.assertIn("Duplicate node ID", str(ctx.exception))


class TestDAGProgression(unittest.TestCase):
    """Test DAG dependency advancement and completion tracking."""

    def setUp(self):
        self.workflow = {
            "name": "diamond-workflow",
            "nodes": [
                {"id": "start"},
                {"id": "branch_left", "depends_on": ["start"]},
                {"id": "branch_right", "depends_on": ["start"]},
                {"id": "end", "depends_on": ["branch_left", "branch_right"]},
            ]
        }

    def test_initial_ready_nodes(self):
        ready = get_ready_nodes(self.workflow, set())
        self.assertEqual([n["id"] for n in ready], ["start"])

    def test_parallel_nodes_ready(self):
        # Once "start" is done, both branches should become ready
        ready = get_ready_nodes(self.workflow, {"start"})
        ready_ids = sorted([n["id"] for n in ready])
        self.assertEqual(ready_ids, ["branch_left", "branch_right"])

    def test_join_waits_for_all_dependencies(self):
        # Only branch_left is done -> end should NOT be ready yet
        ready = get_ready_nodes(self.workflow, {"start", "branch_left"})
        self.assertEqual([n["id"] for n in ready], ["branch_right"])

        # Both branches done -> end becomes ready
        ready = get_ready_nodes(self.workflow, {"start", "branch_left", "branch_right"})
        self.assertEqual([n["id"] for n in ready], ["end"])

    def test_workflow_completion(self):
        all_completed = {"start", "branch_left", "branch_right", "end"}
        self.assertTrue(is_workflow_completed(self.workflow, all_completed))

        partial = {"start", "branch_left"}
        self.assertFalse(is_workflow_completed(self.workflow, partial))


class TestWorkflowNormalization(unittest.TestCase):
    """Test backward-compatible normalization between stages and nodes."""

    def test_normalize_legacy_stages_to_nodes(self):
        legacy = {
            "stages": ["requirements", "plan", "implementation"],
            "stage_policies": {
                "requirements": {"default_task_type": "explore"},
                "plan": {"default_task_type": "plan"},
            }
        }
        normalized = normalize_workflow(legacy)
        self.assertIn("nodes", normalized)
        self.assertEqual(len(normalized["nodes"]), 3)
        self.assertEqual(normalized["nodes"][0]["id"], "requirements")
        self.assertEqual(normalized["nodes"][1]["depends_on"], ["requirements"])
        self.assertEqual(normalized["nodes"][0]["default_task_type"], "explore")

    def test_normalize_nodes_to_legacy_stages(self):
        modern = {
            "name": "custom-flow",
            "nodes": [
                {"id": "step_one", "label": "Step 1"},
                {"id": "step_two", "label": "Step 2", "depends_on": ["step_one"]},
            ]
        }
        normalized = normalize_workflow(modern)
        self.assertIn("stages", normalized)
        self.assertEqual([s["key"] for s in normalized["stages"]], ["step_one", "step_two"])


class TestNodeAgentPolicyRouting(unittest.TestCase):
    """Test node-level agent policy evaluation in router."""

    def test_candidate_order_with_preferred(self):
        pool = {
            "allowed_agents": ["claude", "codex", "opencode", "pi"],
            "stage_preferences": {},
            "task_type_preferences": {},
        }
        policy = {
            "preferred": ["codex", "claude"],
            "exclude": ["pi"],
        }
        order = _candidate_order(pool, "plan", "plan", node_policy=policy)
        # preferred agents must be first
        self.assertEqual(order[:2], ["codex", "claude"])
        self.assertNotIn("pi", order)

    def test_candidate_order_fallback(self):
        pool = {
            "allowed_agents": ["opencode", "claude", "codex"],
            "stage_preferences": {"review": ["claude"]},
            "task_type_preferences": {},
        }
        order = _candidate_order(pool, "review", "review", node_policy=None)
        self.assertEqual(order[0], "claude")


def test_control_policy_defaults_to_automatic():
    cfg = normalize_workflow({"name": "x", "nodes": [{"id": "build"}]})
    assert workflow_engine.resolve_control_policy(cfg, "build", "advance") == {
        "mode": "automatic", "source": "default",
    }


@pytest.mark.parametrize("workflow_mode,node_mode,expected", [
    ("supervised", None, {"mode": "confirmation_required", "source": "workflow"}),
    ("automatic", None, {"mode": "automatic", "source": "workflow"}),
    (None, "supervised", {"mode": "confirmation_required", "source": "node"}),
    (None, "automatic", {"mode": "automatic", "source": "node"}),
    ("supervised", "automatic", {"mode": "automatic", "source": "node"}),
    ("automatic", "supervised", {"mode": "confirmation_required", "source": "node"}),
])
def test_control_policy_inheritance_and_node_override(workflow_mode, node_mode, expected):
    workflow = {"name": "x", "nodes": [{"id": "build"}]}
    if workflow_mode is not None:
        workflow["control_policy"] = {"mode": workflow_mode}
    if node_mode is not None:
        workflow["nodes"][0]["control_mode"] = node_mode
    cfg = normalize_workflow(workflow)
    assert workflow_engine.resolve_control_policy(cfg, "build", "advance") == expected


@pytest.mark.parametrize("node_mode", ["automatic", "supervised"])
def test_control_policy_action_confirmation_overrides_node_mode(node_mode):
    cfg = normalize_workflow({
        "name": "x",
        "control_policy": {"mode": "automatic"},
        "nodes": [{"id": "acceptance", "control_mode": node_mode,
                   "confirm_before": ["advance"]}],
    })
    assert workflow_engine.resolve_control_policy(cfg, "acceptance", "advance") == {
        "mode": "confirmation_required", "source": "action",
    }
    assert workflow_engine.resolve_control_policy(cfg, "acceptance", "retry") == {
        "mode": "automatic" if node_mode == "automatic" else "confirmation_required",
        "source": "node",
    }


@pytest.mark.parametrize("mode", ["manual", "AUTO", "", None, False, 1, [], {}])
@pytest.mark.parametrize("level", ["workflow", "node"])
def test_control_policy_rejects_unknown_modes(mode, level):
    workflow = {"nodes": [{"id": "build"}]}
    if level == "workflow":
        workflow["control_policy"] = {"mode": mode}
    else:
        workflow["nodes"][0]["control_mode"] = mode
    with pytest.raises(ValueError, match="control_policy.mode|control_mode"):
        normalize_workflow(workflow)


@pytest.mark.parametrize("policy", [None, False, "automatic", [], 1])
def test_control_policy_rejects_non_mapping_policy(policy):
    with pytest.raises(ValueError, match="control_policy"):
        normalize_workflow({"control_policy": policy, "nodes": [{"id": "build"}]})


@pytest.mark.parametrize("actions", [
    "advance", None, False, {}, ("advance",), ["advance", "advance"],
    [1], [None], [[]], [""], ["   "],
])
def test_control_policy_rejects_invalid_confirm_before(actions):
    with pytest.raises(ValueError, match="confirm_before"):
        normalize_workflow({"nodes": [{"id": "build", "confirm_before": actions}]})


def test_control_policy_normalization_preserves_input_and_is_idempotent():
    workflow = {
        "control_policy": {"mode": "automatic"},
        "nodes": [{"id": "build", "control_mode": "supervised",
                   "confirm_before": ["advance", "retry"]}],
    }
    original = copy.deepcopy(workflow)
    normalized = workflow_engine.normalize_control_policy(workflow)
    assert workflow == original
    assert normalized == original
    assert workflow_engine.normalize_control_policy(normalized) == normalized
    normalized["control_policy"]["mode"] = "supervised"
    normalized["nodes"][0]["confirm_before"].append("replan")
    assert workflow == original
    cfg = normalize_workflow(workflow)
    assert normalize_workflow(cfg) == cfg


def test_control_policy_legacy_stage_policy_is_preserved():
    cfg = normalize_workflow({
        "control_policy": {"mode": "supervised"},
        "stages": ["build"],
        "stage_policies": {"build": {"control_mode": "automatic",
                                     "confirm_before": ["advance"]}},
    })
    assert workflow_engine.resolve_control_policy(cfg, "build", "advance") == {
        "mode": "confirmation_required", "source": "action",
    }
    assert workflow_engine.resolve_control_policy(cfg, "build", "retry") == {
        "mode": "automatic", "source": "node",
    }


def test_control_policy_is_pinned_in_registered_workflow_snapshot(tmp_path, monkeypatch):
    from herdr import projects, workflow_docs
    from herdr.state_store import SQLiteStateStore

    monkeypatch.setenv(workflow_docs.DOCS_DIR_ENV, str(tmp_path / "workflows"))
    store = SQLiteStateStore(db_path=tmp_path / "state.db")
    monkeypatch.setattr(projects, "_get_store", lambda: store)
    source = tmp_path / "workflow.json"
    source.write_text(json.dumps({
        "control_policy": {"mode": "supervised"},
        "nodes": [{"id": "build", "control_mode": "automatic",
                   "confirm_before": ["advance"]}],
    }), encoding="utf-8")
    project = {
        "project_id": "policy-test", "project_name": "Policy test",
        "project_root": str(tmp_path), "workspace_id": "test-workspace",
        "coordinator_pane_id": None, "workflow_file": str(source),
    }
    projects.register_workflow("wf-control-policy", project)
    source.write_text(json.dumps({"nodes": [{"id": "build"}]}), encoding="utf-8")
    cfg = projects.workflow_config_for("wf-control-policy")
    assert cfg["control_policy"] == {"mode": "supervised"}
    assert cfg["nodes"][0]["control_mode"] == "automatic"
    assert cfg["nodes"][0]["confirm_before"] == ["advance"]
    assert workflow_engine.resolve_control_policy(cfg, "build", "advance") == {
        "mode": "confirmation_required", "source": "action",
    }
    assert workflow_engine.resolve_control_policy(cfg, "build", "retry") == {
        "mode": "automatic", "source": "node",
    }


if __name__ == "__main__":
    unittest.main()
