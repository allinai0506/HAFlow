import pytest
import time
from herdr.workflow_progress import assess_workflow, candidate_responsible_source
from herdr import node_dispatch as core


def test_dynamic_node_attribution_supports_custom_node_name():
    """Verify that candidate_responsible_source and assess_workflow dynamically resolve
    custom implementation nodes such as 'craft' from code-craft-v1 templates."""
    candidate_sha = "a" * 40
    integrated_sha = "b" * 40
    wid = "wf-dynamic-node-test"
    eid = "exec-1"

    workflow = {
        "workflow_id": wid,
        "execution_id": eid,
        "status": "running",
        "candidate_sha": integrated_sha,
        "startup_ready": True,
    }

    config = {
        "nodes": [
            {"id": "craft", "role": "implementation"},
            {"id": "review", "role": "verifier", "gate": "craft"},
        ]
    }

    craft_task = {
        "task_id": "task-craft-1",
        "workflow_id": wid,
        "execution_id": eid,
        "node": "craft",
        "stage": "craft",
        "status": "completed",
        "run_id": "run-craft-1",
        "commit": candidate_sha,
        "integrated_commit": integrated_sha,
    }

    review_task = {
        "task_id": "task-review-1",
        "workflow_id": wid,
        "execution_id": eid,
        "node": "review",
        "stage": "review",
        "status": "completed",
        "stage_verdict": "blocked",
        "candidate_sha": integrated_sha,
        "run_id": "run-review-1",
        "commit": integrated_sha,
    }

    tasks = [craft_task, review_task]

    # Direct candidate_responsible_source test
    source_node, source_targets = candidate_responsible_source(
        workflow, tasks, integrated_sha, retry_node="craft"
    )
    assert source_node == "craft"
    assert source_targets == ["task-craft-1"]

    # Full assess_workflow recovery obligation test
    assessment = assess_workflow(workflow, config, tasks)
    assert assessment["can_advance"] is False
    assert len(assessment["obligations"]) == 1

    obligation = assessment["obligations"][0]
    assert obligation["retry_node"] == "craft"
    assert obligation["affected_task_ids"] == ["task-craft-1"]
    assert obligation["status"] == "pending"  # Crucial: pending, NOT waiting_human!
    assert obligation["reason"] == "blocked_gate"


def test_multitask_fallback_when_exact_candidate_sha_differs():
    """Verify that when parallel tasks cannot be resolved to a single candidate_sha,
    assess_workflow safely falls back to all active tasks under the retry node."""
    integrated_sha = "c" * 40
    wid = "wf-multitask-test"
    eid = "exec-2"

    workflow = {
        "workflow_id": wid,
        "execution_id": eid,
        "status": "running",
        "candidate_sha": integrated_sha,
        "startup_ready": True,
    }

    config = {
        "nodes": [
            {"id": "craft", "role": "implementation"},
            {"id": "test", "role": "verifier", "gate": "craft"},
        ]
    }

    # Parallel tasks where commit metadata differed or was merged
    task_core = {
        "task_id": "task-craft-core",
        "workflow_id": wid,
        "execution_id": eid,
        "node": "craft",
        "stage": "craft",
        "status": "completed",
        "run_id": "run-1",
        "commit": "1" * 40,
    }
    task_console = {
        "task_id": "task-craft-console",
        "workflow_id": wid,
        "execution_id": eid,
        "node": "craft",
        "stage": "craft",
        "status": "completed",
        "run_id": "run-2",
        "commit": "2" * 40,
    }
    gate_task = {
        "task_id": "task-test-gate",
        "workflow_id": wid,
        "execution_id": eid,
        "node": "test",
        "stage": "test",
        "status": "completed",
        "stage_verdict": "blocked",
        "candidate_sha": integrated_sha,
        "run_id": "run-gate",
        "commit": integrated_sha,
    }

    tasks = [task_core, task_console, gate_task]
    assessment = assess_workflow(workflow, config, tasks)
    assert len(assessment["obligations"]) == 1
    obligation = assessment["obligations"][0]

    assert obligation["retry_node"] == "craft"
    assert set(obligation["affected_task_ids"]) == {"task-craft-core", "task-craft-console"}
    assert obligation["status"] == "pending"


def test_node_dispatch_predecessor_changed_supersedes_terminal_worker():
    """Verify that when predecessors change and the worker has already finished execution,
    node_dispatch returns 'superseded' with incremented replan_count rather than halting."""
    wid = "wf-dispatch-test"
    eid = "exec-3"
    op_id = "op-node-1"

    workflow = {
        "workflow_id": wid,
        "execution_id": eid,
        "status": "running",
    }
    config = {
        "nodes": [
            {"id": "node-1"},
        ]
    }

    predecessors = [{"task_id": "t-old-pred", "run_id": "r-old"}]
    payload = core.payload(workflow, config, config["nodes"][0], predecessors)

    operation = {
        "id": op_id,
        "workflow_id": wid,
        "status": "running",
        "started": True,
        "identity_key": payload["identity_key"],
        "payload": payload,
        "detail": {
            "replan_count": 0,
        },
    }

    # Predecessor has changed (replacement_pending is True, successor declared)
    tasks = [
        {
            "task_id": "t-old-pred",
            "workflow_id": wid,
            "node": "node-1",
            "stage": "node-1",
            "execution_id": eid,
            "run_id": "r-old",
            "status": "superseded",
            "superseded_by": "t-new-pred",
            "replacement_pending": True,
        },
        {
            "task_id": "t-new-pred",
            "workflow_id": wid,
            "node": "node-1",
            "stage": "node-1",
            "execution_id": eid,
            "run_id": "r-new",
            "status": "completed",
        },
        # The operation's own task has already completed
        {
            "task_id": "t-op-task",
            "workflow_id": wid,
            "node": "node-1",
            "stage": "node-1",
            "execution_id": eid,
            "dispatch_operation_id": op_id,
            "run_id": "r-task-1",
            "launch_intent_id": "intent-1",
            "status": "completed",
        },
    ]

    now = time.time()
    outcome = core.result(operation, workflow, config, tasks, now)
    assert outcome is not None
    status, detail = outcome

    assert status == "superseded"
    assert detail["reason"] == "dispatch_predecessor_changed"
    assert detail["replan_count"] == 1


def test_node_dispatch_replan_exhaustion():
    """Verify that when replan_count reaches limit (2), node_dispatch safely halts to waiting_human."""
    wid = "wf-dispatch-limit"
    eid = "exec-4"
    op_id = "op-limit"

    workflow = {
        "workflow_id": wid,
        "execution_id": eid,
        "status": "running",
    }
    config = {
        "nodes": [{"id": "node-1"}]
    }

    predecessors = [{"task_id": "t-pred", "run_id": "r-1"}]
    payload = core.payload(workflow, config, config["nodes"][0], predecessors)

    operation = {
        "id": op_id,
        "workflow_id": wid,
        "status": "running",
        "started": False,
        "identity_key": payload["identity_key"],
        "payload": payload,
        "detail": {
            "replan_count": 2,  # Already attempted twice
        },
    }

    tasks = [
        {
            "task_id": "t-pred",
            "workflow_id": wid,
            "node": "node-1",
            "stage": "node-1",
            "execution_id": eid,
            "run_id": "r-1",
            "status": "superseded",
            "superseded_by": "t-other",
            "replacement_pending": True,
        },
    ]

    outcome = core.result(operation, workflow, config, tasks, time.time())
    assert outcome is not None
    status, detail = outcome

    assert status == "waiting_human"
    assert detail["reason"] == "dispatch_replan_exhausted"


def test_node_dispatch_waits_for_active_worker():
    """Verify that when an operation's worker task is actively running (working or dispatched),
    it does not supersede immediately to prevent concurrent git conflicts."""
    wid = "wf-active-worker"
    eid = "exec-5"
    op_id = "op-active"

    workflow = {
        "workflow_id": wid,
        "execution_id": eid,
        "status": "running",
    }
    config = {
        "nodes": [{"id": "node-1"}]
    }

    predecessors = [{"task_id": "t-pred", "run_id": "r-1"}]
    payload = core.payload(workflow, config, config["nodes"][0], predecessors)

    operation = {
        "id": op_id,
        "workflow_id": wid,
        "status": "running",
        "started": True,
        "identity_key": payload["identity_key"],
        "payload": payload,
        "detail": {
            "replan_count": 0,
        },
    }

    # Verify both 'working' and 'dispatched' statuses (authoritative ACTIVE_TASK_STATUSES)
    for active_status in ("working", "dispatched", "rework"):
        tasks = [
            {
                "task_id": "t-pred",
                "workflow_id": wid,
                "node": "node-1",
                "stage": "node-1",
                "execution_id": eid,
                "run_id": "r-1",
                "status": "superseded",
                "superseded_by": "t-other",
                "replacement_pending": True,
            },
            {
                "task_id": "t-active-task",
                "workflow_id": wid,
                "node": "node-1",
                "stage": "node-1",
                "execution_id": eid,
                "dispatch_operation_id": op_id,
                "run_id": "r-active",
                "launch_intent_id": "intent-active",
                "status": active_status,
            },
        ]

        outcome = core.result(operation, workflow, config, tasks, time.time())
        assert outcome is not None
        status, detail = outcome

        assert status == "waiting_human"
        assert detail["reason"] == "dispatch_predecessor_changed"
        assert "仍在执行" in detail["decision_needed"]


def test_gate_placeholder_normalized_distinction():
    """Verify that normalized empty gate {} with gate_placeholder_normalized=True
    is never mistaken for a declared gate targeting 'implementation'."""
    from herdr.workflow_progress import gate_retry_node, _gate

    nodes = {
        "craft": {"id": "craft"},
        "docs": {"id": "docs", "gate": {}, "gate_placeholder_normalized": True},
        "review": {"id": "review", "gate": {"retry_node": "craft"}},
    }
    cfg = {"nodes": list(nodes.values())}

    # docs has no declared gate
    assert _gate(nodes, "docs") is None
    assert gate_retry_node(cfg, "docs") == ""

    # review has an explicit gate
    assert _gate(nodes, "review") == "craft"
    assert gate_retry_node(cfg, "review") == "craft"


def test_replan_budget_carried_across_replacement_operations(tmp_path):
    """Verify that node_dispatch_store carries replan_count across successive
    replacement operations until the circuit breaker (2) is reached in the store."""
    from herdr import node_dispatch_store, recovery_store
    from herdr.state_store import SQLiteStateStore

    db_path = tmp_path / "replan.db"
    store = SQLiteStateStore(db_path)

    wid = "wf-replan-carry"
    eid = "exec-replan"
    workflow = {
        "workflow_id": wid,
        "execution_id": eid,
        "status": "running",
        "config": {
            "nodes": [
                {"id": "node-0"},
                {"id": "node-1", "depends_on": ["node-0"]},
            ]
        },
    }
    store.save_workflow(workflow)

    # Initial upstream task
    store.save_task({
        "task_id": "t-up-1",
        "workflow_id": wid,
        "execution_id": eid,
        "node": "node-0",
        "stage": "node-0",
        "status": "completed",
        "stage_verdict": "pass",
    })

    # Initial node-1 task failed and needs redispatch
    store.save_task({
        "task_id": "t-node1-1",
        "workflow_id": wid,
        "execution_id": eid,
        "node": "node-1",
        "stage": "node-1",
        "run_id": "r-1",
        "status": "superseded",
        "replacement_pending": True,
    })

    # First reconciliation creates initial operation (generation 1)
    node_dispatch_store.reconcile_workflow(db_path, wid, now=10)
    ops = [op for op in recovery_store.list_operations(db_path, wid)
           if op['payload'].get('kind') == 'node_dispatch']
    assert len(ops) == 1
    op1 = ops[0]
    assert op1['detail'].get('replan_count', 0) == 0

    # Mark op1 superseded with replan_count = 1 (simulating result() after predecessor changed)
    with recovery_store._transaction(db_path) as conn:
        node_dispatch_store._write(
            conn, op1, 'superseded',
            {'reason': 'dispatch_predecessor_changed', 'replan_count': 1},
            now=15,
        )

    # Reconcile again: new replacement operation (generation 2) MUST inherit replan_count = 1
    node_dispatch_store.reconcile_workflow(db_path, wid, now=20)
    op2 = node_dispatch_store.operation_for_node(db_path, wid, 'node-1')
    assert op2 is not None
    assert op2['id'] != op1['id']
    assert op2['detail'].get('replan_count') == 1, "Replacement operation must inherit replan_count!"
