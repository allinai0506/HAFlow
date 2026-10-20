"""Tests for gate retry target and retry node convergence (SC-005).

Verifies:
1. validate_workflow_dag validates both retry_target and retry_node.
2. gate_retry_node in herdr.workflow_progress resolves both retry_node and retry_target.
3. resolve_gate_config in services.controller.node_policy resolves both retry_node and retry_target.
4. _retry_node_for_gate in services.controller.selective_facts resolves both retry_node and retry_target.
5. business-research-v1 template correctly resolves retry node as data_extraction, not implementation.
"""

from unittest.mock import MagicMock
import pytest
from herdr import workflow
from herdr.workflow_progress import gate_retry_node
from services.controller import node_policy, selective_facts


def test_dag_validation_valid_retry_fields():
    # Both retry_target and retry_node pointing to valid nodes should pass
    nodes = [
        {"id": "source", "label": "Source", "depends_on": []},
        {"id": "gate_target", "label": "Gate Target", "depends_on": ["source"], "gate": {"retry_target": "source"}},
        {"id": "gate_node", "label": "Gate Node", "depends_on": ["source"], "gate": {"retry_node": "source"}},
    ]
    workflow.validate_workflow_dag(nodes)


def test_dag_validation_invalid_retry_target():
    nodes = [
        {"id": "step1", "label": "Step 1", "depends_on": [], "gate": {"retry_target": "unknown_step"}},
    ]
    with pytest.raises(ValueError, match="gate retry_target references unknown node 'unknown_step'"):
        workflow.validate_workflow_dag(nodes)


def test_dag_validation_invalid_retry_node():
    nodes = [
        {"id": "step1", "label": "Step 1", "depends_on": [], "gate": {"retry_node": "unknown_step"}},
    ]
    with pytest.raises(ValueError, match="gate retry_node references unknown node 'unknown_step'"):
        workflow.validate_workflow_dag(nodes)


def test_workflow_progress_gate_retry_node_resolution():
    # retry_target alias resolution
    config_target = {
        "nodes": [
            {"id": "upstream"},
            {"id": "audit", "gate": {"retry_target": "upstream"}},
        ]
    }
    assert gate_retry_node(config_target, "audit") == "upstream"

    # retry_node standard resolution
    config_node = {
        "nodes": [
            {"id": "upstream"},
            {"id": "audit", "gate": {"retry_node": "upstream"}},
        ]
    }
    assert gate_retry_node(config_node, "audit") == "upstream"

    # priority: retry_node takes precedence if both present
    config_both = {
        "nodes": [
            {"id": "dest1"},
            {"id": "dest2"},
            {"id": "audit", "gate": {"retry_node": "dest1", "retry_target": "dest2"}},
        ]
    }
    assert gate_retry_node(config_both, "audit") == "dest1"

    # fallback to implementation if gate dict is present but has no retry field
    config_empty = {
        "nodes": [
            {"id": "implementation"},
            {"id": "audit", "gate": {"type": "auto"}},
        ]
    }
    assert gate_retry_node(config_empty, "audit") == "implementation"


def test_controller_node_policy_resolve_gate_config():
    ports = MagicMock()
    ports.get_stage_policy.return_value = {}
    ports.GATE_DEFAULTS = {}
    ports.FIX_LOOP_MAX = 3

    # node with retry_target
    node_with_target = {"id": "gate_t", "gate": {"retry_target": "data_source"}}
    cfg = node_policy.resolve_gate_config(ports, node_with_target, "gate_t")
    assert cfg["retry_node"] == "data_source"

    # node with retry_node
    node_with_node = {"id": "gate_n", "gate": {"retry_node": "builder"}}
    cfg = node_policy.resolve_gate_config(ports, node_with_node, "gate_n")
    assert cfg["retry_node"] == "builder"


def test_controller_selective_facts_retry_node_for_gate():
    ports = MagicMock()
    wf_cfg = {
        "nodes": [
            {"id": "extract"},
            {"id": "gate1", "depends_on": ["extract"]},
        ]
    }

    # gate_cfg using retry_target
    gate_cfg_target = {"retry_target": "extract"}
    assert selective_facts._retry_node_for_gate(ports, wf_cfg, gate_cfg_target, "gate1") == "extract"

    # gate_cfg using retry_node
    gate_cfg_node = {"retry_node": "extract"}
    assert selective_facts._retry_node_for_gate(ports, wf_cfg, gate_cfg_node, "gate1") == "extract"


def test_business_research_template_gate_retry():
    tmpl = workflow.load_template("business-research-v1")
    # Gate on executive_briefing must retry to data_extraction, not fallback to implementation
    retry_dest = gate_retry_node(tmpl, "executive_briefing")
    assert retry_dest == "data_extraction"
