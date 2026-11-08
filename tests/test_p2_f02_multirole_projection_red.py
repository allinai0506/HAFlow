"""Regression tests for P2 state machine defect: F-02.

Finding F-02:
Multi-role node completion semantic divergence between visualization projection,
CLI node-status, and scheduling core due to relay signature deficiency in
_node_delivery_complete (missing node argument) and missing role verification.
"""

import json
import io
from contextlib import redirect_stdout
import pytest

from herdr.ops_center_helpers import (
    _node_delivery_complete,
    _collect_workflow_node_metadata,
    _normalize_workflow_nodes,
)
from herdr.ops_center_projection import _build_workflow_cards
from herdr.task_cli_workflow import node_status
from herdr.workflow_graph import workflow_graph_projection


class TestF02MultiRoleNodeDeliveryComplete:
    """Verify _node_delivery_complete accepts node parameter and enforces role completeness."""

    def test_node_delivery_complete_signature_and_partial_role_rejection(self):
        plan_node = {
            "id": "plan",
            "roles": [{"name": "executor"}, {"name": "challenger"}],
        }
        executor_task = {
            "task_id": "wf-1-plan-executor",
            "workflow_id": "wf-1",
            "node": "plan",
            "role": "executor",
            "status": "completed",
        }

        # RED: Calling with node=plan_node must not fail with TypeError,
        # and must return False when only executor role is complete.
        res = _node_delivery_complete([executor_task], node=plan_node)
        assert res is False, "Multi-role node with only executor complete must return False"

        # When challenger is also completed, it must return True
        challenger_task = {
            "task_id": "wf-1-plan-challenger",
            "workflow_id": "wf-1",
            "node": "plan",
            "role": "challenger",
            "status": "completed",
        }
        res_full = _node_delivery_complete([executor_task, challenger_task], node=plan_node)
        assert res_full is True, "Multi-role node with all roles complete must return True"


class TestF02OpsCenterCardProjection:
    """Verify Ops Center cards do not show multi-role nodes as completed prematurely."""

    def test_ops_center_card_partial_roles_not_completed(self):
        workflow = {
            "workflow_id": "wf-multi",
            "nodes": [
                {
                    "id": "plan",
                    "label": "Plan Stage",
                    "roles": [{"name": "executor"}, {"name": "challenger"}],
                }
            ],
        }
        executor_task = {
            "task_id": "wf-multi-plan-executor",
            "workflow_id": "wf-multi",
            "node": "plan",
            "role": "executor",
            "status": "completed",
        }

        cards = _build_workflow_cards(
            workflows_tasks={"wf-multi": [executor_task]},
            now=1000.0,
            workflow_lookup=lambda wid: (workflow, "wf-multi-label"),
        )
        assert len(cards) == 1
        node_card = cards[0]["nodes"][0]
        # RED: Must NOT be completed
        assert node_card["status"] != "completed", (
            f"Expected plan node status not to be 'completed', got {node_card['status']}"
        )

        # When challenger finishes, it should project as completed
        challenger_task = {
            "task_id": "wf-multi-plan-challenger",
            "workflow_id": "wf-multi",
            "node": "plan",
            "role": "challenger",
            "status": "completed",
        }
        cards_done = _build_workflow_cards(
            workflows_tasks={"wf-multi": [executor_task, challenger_task]},
            now=1000.0,
            workflow_lookup=lambda wid: (workflow, "wf-multi-label"),
        )
        assert cards_done[0]["nodes"][0]["status"] == "completed"


class TestF02TaskCLINodeStatus:
    """Verify herdr-task workflow node-status respects multi-role completion."""

    def test_cli_node_status_partial_roles_not_completed(self):
        workflow = {
            "workflow_id": "wf-cli",
            "nodes": [
                {
                    "id": "plan",
                    "node_label": "Plan Stage",
                    "stage_label": "Plan Stage",
                    "roles": [{"name": "executor"}, {"name": "challenger"}],
                }
            ],
        }
        executor_task = {
            "task_id": "wf-cli-plan-executor",
            "workflow_id": "wf-cli",
            "node": "plan",
            "role": "executor",
            "status": "completed",
        }

        output = io.StringIO()
        with redirect_stdout(output):
            node_status(
                "wf-cli",
                "plan",
                resolve_node=lambda *_: {"node_label": "Plan Stage", "stage_label": "Plan Stage"},
                load_tasks=lambda: {"tasks": [executor_task]},
                safe_workflow=lambda wid: (workflow, "wf-cli-label"),
                normalize_workflow_nodes=_normalize_workflow_nodes,
                node_delivery_complete=_node_delivery_complete,
            )
        data = json.loads(output.getvalue())
        # RED: complete must be False, status must not be completed
        assert data["complete"] is False
        assert data["status"] != "completed"

        # With both tasks
        challenger_task = {
            "task_id": "wf-cli-plan-challenger",
            "workflow_id": "wf-cli",
            "node": "plan",
            "role": "challenger",
            "status": "completed",
        }
        output_done = io.StringIO()
        with redirect_stdout(output_done):
            node_status(
                "wf-cli",
                "plan",
                resolve_node=lambda *_: {"node_label": "Plan Stage", "stage_label": "Plan Stage"},
                load_tasks=lambda: {"tasks": [executor_task, challenger_task]},
                safe_workflow=lambda wid: (workflow, "wf-cli-label"),
                normalize_workflow_nodes=_normalize_workflow_nodes,
                node_delivery_complete=_node_delivery_complete,
            )
        data_done = json.loads(output_done.getvalue())
        assert data_done["complete"] is True
        assert data_done["status"] == "completed"


class TestF02WorkflowGraphProjection:
    """Verify Workflow Graph does not mark multi-role nodes as completed prematurely."""

    def test_workflow_graph_partial_roles_not_completed(self):
        workflow = {
            "workflow_id": "wf-graph",
            "nodes": [
                {
                    "id": "plan",
                    "label": "Plan",
                    "roles": [{"name": "executor"}, {"name": "challenger"}],
                },
                {
                    "id": "implementation",
                    "label": "Implementation",
                    "depends_on": ["plan"],
                },
            ],
        }
        executor_task = {
            "task_id": "wf-graph-plan-executor",
            "workflow_id": "wf-graph",
            "node": "plan",
            "role": "executor",
            "status": "completed",
        }

        proj = workflow_graph_projection(workflow, [executor_task])
        plan_node = next(n for n in proj["nodes"] if n["id"] == "plan")
        # RED: plan node must NOT be completed
        assert plan_node["status"] != "completed", (
            f"Expected plan node not to be completed, got {plan_node['status']}"
        )
        # Downstream implementation must NOT be ready
        assert "implementation" not in proj.get("ready_nodes", []), (
            "Implementation node must not be ready when plan roles are incomplete"
        )

        # When challenger is also complete:
        challenger_task = {
            "task_id": "wf-graph-plan-challenger",
            "workflow_id": "wf-graph",
            "node": "plan",
            "role": "challenger",
            "status": "completed",
        }
        proj_done = workflow_graph_projection(workflow, [executor_task, challenger_task])
        plan_node_done = next(n for n in proj_done["nodes"] if n["id"] == "plan")
        assert plan_node_done["status"] == "completed"
        assert "implementation" in proj_done.get("ready_nodes", [])
