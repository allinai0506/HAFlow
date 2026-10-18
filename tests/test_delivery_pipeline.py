#!/usr/bin/env python3
"""Git delivery pipeline projection (herdr/delivery_pipeline.py) — issue #253.

A task that stops at ``committed`` while the scheduler still owns its
integration used to render as the terminal-looking badge「已提交」 with a
competing manual「并入目标分支」button.  These tests pin the projection the
console renders: the pipeline stage, who owns the next step, which downstream
gates the delivery is still waiting on, and the commit/branch/candidate facts.
"""

import pytest

from herdr.delivery_pipeline import (
    delivery_projection,
    pending_gate_nodes,
    scheduler_owns_next_step,
    stage_of,
)

CODE_CRAFT_NODES = [
    {"id": "craft", "label": "1代码工坊", "depends_on": [],
     "default_integration_mode": "git"},
    {"id": "cleanroom_test", "label": "2洁净室测试", "depends_on": ["craft"],
     "gate": {"retry_node": "craft"}},
    {"id": "cleanroom_review", "label": "3洁净室评审", "depends_on": ["craft"],
     "gate": {"retry_node": "craft"}},
    {"id": "wrapup", "label": "4收尾", "depends_on": ["cleanroom_test", "cleanroom_review"]},
]

WORKFLOW = {
    "workflow_id": "wf-1017-01",
    "status": "running",
    "candidate_sha": "85c3a98eb1973f0fd1656e6557aa78268a067da2",
    "config": {"nodes": CODE_CRAFT_NODES},
}


def _task(tid="craft-r2", status="committed", **kw):
    task = {
        "task_id": tid,
        "workflow_id": "wf-1017-01",
        "node": kw.pop("node", "craft"),
        "stage": kw.pop("node", "craft"),
        "status": status,
        "agent": "codex",
        "integration_mode": kw.pop("integration_mode", "git"),
        "commit": kw.pop("commit", "6e5825883b2d9629ce71a7ea6d4d88d33f1df530"),
        "integration_branch": kw.pop(
            "integration_branch", "herdr/integration-craft-r2",
        ),
        "candidate_sha": kw.pop("candidate_sha", WORKFLOW["candidate_sha"]),
        "pane_id": kw.pop("pane_id", "w19:p2G"),
    }
    task.update(kw)
    return task


def _gate_task(nid, verdict, status="cleaned"):
    return {
        "task_id": f"wf-1017-01-{nid}-r2",
        "workflow_id": "wf-1017-01",
        "node": nid,
        "stage": nid,
        "status": status,
        "stage_verdict": verdict,
        "integration_mode": "none",
    }


# ---------------------------------------------------------------- stage_of

def test_stage_of_maps_delivery_statuses():
    assert stage_of("committed") == {"key": "awaiting_integration", "label": "等待集成"}
    assert stage_of("integrated")["key"] == "awaiting_finalize"
    assert stage_of("cleaned")["label"] == "已归档"


@pytest.mark.parametrize("status", ["working", "blocked", "failed", "", None])
def test_stage_of_ignores_non_pipeline_statuses(status):
    assert stage_of(status) is None


# ------------------------------------------------- scheduler-owned git step

def test_running_workflow_owns_committed_git_task():
    assert scheduler_owns_next_step(_task(), WORKFLOW) is True


@pytest.mark.parametrize("wf_status", ["paused", "completed", "cleaned", "archived", "", None])
def test_only_a_running_workflow_claims_ownership(wf_status):
    """A finished/paused workflow is not evidence the scheduler is driving."""
    assert scheduler_owns_next_step(_task(), {**WORKFLOW, "status": wf_status}) is False


def test_missing_workflow_facts_are_not_ownership_evidence():
    assert scheduler_owns_next_step(_task(), {"workflow_id": "wf-1017-01"}) is False


@pytest.mark.parametrize("status", ["integrated", "cleanup_ready", "working", "cleaned"])
def test_other_statuses_are_not_scheduler_owned(status):
    assert scheduler_owns_next_step(_task(status=status), WORKFLOW) is False


def test_non_git_context_and_escalated_tasks_are_not_scheduler_owned():
    assert scheduler_owns_next_step(
        _task(integration_mode="none"), WORKFLOW) is False
    assert scheduler_owns_next_step(
        _task(execution_mode="context"), WORKFLOW) is False
    assert scheduler_owns_next_step(
        _task(finalize_escalated=True, finalize_escalate_reason="integrate_main_dirty"),
        WORKFLOW) is False


# ------------------------------------------------------ pending gate nodes

def test_pending_gates_are_the_pending_downstream_gate_nodes():
    gates = pending_gate_nodes(
        WORKFLOW["config"], _task(), [_task(), _gate_task("cleanroom_test", "")],
    )
    assert gates == [{"id": "cleanroom_test", "label": "2洁净室测试"},
                     {"id": "cleanroom_review", "label": "3洁净室评审"}]


def test_a_gate_override_marks_the_gate_settled():
    workflow = {**WORKFLOW, "gate_overrides": {"cleanroom_review": {"verdict": "pass"}}}
    gates = pending_gate_nodes(
        workflow["config"], _task(), [_task(), _gate_task("cleanroom_test", "pass")],
        workflow=workflow,
    )
    assert gates == []


def test_no_pending_gates_once_every_gate_passed():
    tasks = [_task(), _gate_task("cleanroom_test", "pass"),
             _gate_task("cleanroom_review", "pass")]
    assert pending_gate_nodes(WORKFLOW["config"], _task(), tasks) == []


def test_superseded_gate_verdict_does_not_count():
    tasks = [_task(), _gate_task("cleanroom_test", "pass", status="superseded"),
             _gate_task("cleanroom_review", "pass")]
    assert [g["id"] for g in pending_gate_nodes(WORKFLOW["config"], _task(), tasks)] \
        == ["cleanroom_test"]


def test_nodes_without_a_gate_contract_are_not_gates():
    config = {"nodes": [
        {"id": "craft", "depends_on": []},
        {"id": "docs", "depends_on": ["craft"]},
    ]}
    assert pending_gate_nodes(config, _task(), [_task()]) == []


def test_default_gate_names_are_gates_even_without_an_explicit_contract():
    """workflow_progress._DEFAULT_GATES routes test/review/wrapup back for rework."""
    config = {"nodes": [
        {"id": "implementation", "depends_on": []},
        {"id": "test", "depends_on": ["implementation"]},
    ]}
    task = _task(node="implementation")
    assert [g["id"] for g in pending_gate_nodes(config, task, [task])] == ["test"]


def test_join_node_is_not_a_gate_on_the_upstream_task():
    """wrapup sits downstream of the gates, not of craft itself."""
    gates = [g["id"] for g in pending_gate_nodes(
        WORKFLOW["config"], _task(), [_task()],
    )]
    assert "wrapup" not in gates


# ------------------------------------------------------------ projection

def test_projection_carries_stage_owner_and_pending_gates():
    out = delivery_projection(_task(), WORKFLOW, [_task()],
                              published_at=1792280515.7452)
    assert out["stage"] == "awaiting_integration"
    assert out["stage_label"] == "等待集成"
    assert out["owner"] == "scheduler"
    assert out["scheduler_owned"] is True
    assert out["wait_kind"] == "gate"
    assert out["gates"] == [
        {"id": "cleanroom_test", "label": "2洁净室测试"},
        {"id": "cleanroom_review", "label": "3洁净室评审"},
    ]
    assert out["owner_note"] == "由调度器自动集成，无需人工操作"


def test_gate_wait_takes_priority_over_the_owner_note():
    """The visible reason must name the gate, not just "still waiting"."""
    out = delivery_projection(
        _task(), WORKFLOW,
        [_task(), _gate_task("cleanroom_review", "pass")],
    )
    assert out["wait_kind"] == "gate"
    assert [g["id"] for g in out["gates"]] == ["cleanroom_test"]


def test_owner_note_is_the_reason_once_every_gate_passed():
    tasks = [_task(), _gate_task("cleanroom_test", "pass"),
             _gate_task("cleanroom_review", "pass")]
    out = delivery_projection(_task(), WORKFLOW, tasks)
    assert out["wait_kind"] == "owner"
    assert out["gates"] == []
    assert out["owner_note"] == "由调度器自动集成，无需人工操作"


def test_projection_reads_the_existing_task_and_event_facts():
    out = delivery_projection(
        _task(), WORKFLOW, [_task()], published_at=1792280515.7452,
    )
    assert out["commit"] == "6e5825883b2d9629ce71a7ea6d4d88d33f1df530"
    assert out["commit_short"] == "6e582588"
    assert out["integration_branch"] == "herdr/integration-craft-r2"
    assert out["candidate_sha"] == WORKFLOW["candidate_sha"]
    assert out["candidate_short"] == "85c3a98e"
    assert out["candidate_published_at"] == 1792280515.7452


def test_projection_without_a_publication_event_keeps_the_time_unknown():
    out = delivery_projection(_task(), WORKFLOW, [_task()])
    assert out["candidate_published_at"] is None


def test_projection_does_not_claim_ownership_for_a_paused_workflow():
    out = delivery_projection(_task(), {**WORKFLOW, "status": "paused"}, [_task()])
    assert out["owner"] == "human"
    assert out["scheduler_owned"] is False
    assert "人工" in out["owner_note"]


def test_projection_falls_back_to_the_raw_status_when_not_in_the_pipeline():
    out = delivery_projection(_task(status="working", commit=""), WORKFLOW, [])
    assert out["stage"] == "working"
    assert out["stage_label"] == ""


def test_projection_is_fail_soft_on_garbage():
    out = delivery_projection(None, None, None)
    assert out["task_id"] == ""
    assert out["stage"] == ""
    assert out["gates"] == []
    assert out["scheduler_owned"] is False
