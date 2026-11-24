"""Regression tests for TOKEN_EXHAUSTED startup rollback and node_dispatch auto-reroute."""

import json
import time
from types import SimpleNamespace

from herdr import node_dispatch_store as nd_store
from herdr.state_store import SQLiteStateStore
from herdr.task_cli_launch_worker import WorkerStartupHealthFailure, run_launch_worker
from herdr.task_resources import begin_launch_intent


import pytest

@pytest.mark.parametrize("disposition", ["rolled_back", "borrowed_pane_restored"])
def test_worker_token_exhausted_startup_allows_auto_handover(tmp_path, disposition):
    """When worker exits with TOKEN_EXHAUSTED and safe rollback disposition under agent=auto,
    run_launch_worker marks agent unhealthy and raises WorkerStartupHealthFailure for handover."""
    store = SQLiteStateStore(db_path=tmp_path / "state.db")
    store.save_workflow(
        {
            "workflow_id": "wf-tok",
            "project_root": str(tmp_path),
            "base_branch": "main",
            "status": "running",
            "startup_ready": True,
            "execution_id": "exec-tok",
            "healthy_agents": ["grok", "agy"],
            "unhealthy_agents": {},
        }
    )
    claim = begin_launch_intent(
        store,
        workflow_id="wf-tok",
        node_id="cleanroom_test",
        role="worker",
        candidate_sha="",
        dispatch_round=1,
        task_id="wf-tok-cleanroom_test-auto",
        run_id="run-tok-1",
        execution_id="exec-tok",
    )
    args = SimpleNamespace(
        task_id="wf-tok-cleanroom_test-auto",
        workflow_id="wf-tok",
        source=str(tmp_path),
        agent="auto",
        task_type="test",
    )
    failure_payload = {
        "task_id": "wf-tok-cleanroom_test-auto",
        "run_id": "run-tok-1",
        "disposition": disposition,
        "recovery_required": False,
        "failure_type": "RuntimeError",
    }

    def fake_run_worker(cmd, **_kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="TOKEN_EXHAUSTED: startup_blocked\nHERDR_WORKER_FAILURE="
            + json.dumps(failure_payload)
            + "\n",
        )

    try:
        run_launch_worker(
            args,
            "cleanroom_test",
            "run-tok-1",
            store,
            claim["intent"],
            tmp_path / "clone",
            "grok",
            {"execution_mode": "git"},
            "main",
            None,
            "p1",
            None,
            str(tmp_path),
            run_worker=fake_run_worker,
            get_store=lambda: store,
        )
        raised = False
    except WorkerStartupHealthFailure as exc:
        raised = True
        assert exc.agent == "grok"
        assert exc.status == "TOKEN_EXHAUSTED"
    assert raised is True
    wf = store.get_workflow("wf-tok")
    assert wf["unhealthy_agents"].get("grok") == "TOKEN_EXHAUSTED"


def test_node_dispatch_direct_finished_resets_started_on_resources_absent(tmp_path):
    """If launch fails before creating any task and records resources_absent, direct_finished
    must reset started=0 and return pending (not awaiting_result / prior_delivery_unknown)."""
    db_path = tmp_path / "state.db"
    store = SQLiteStateStore(db_path=db_path)
    store.save_workflow(
        {
            "workflow_id": "wf-nd",
            "project_root": str(tmp_path),
            "base_branch": "main",
            "status": "running",
            "startup_ready": True,
            "execution_id": "exec-nd",
            "created_at": 1.0,
            "config": {"nodes": [{"id": "cleanroom_test", "node_type": "agent"}]},
        }
    )
    now = time.time()
    ops = nd_store.reconcile_workflow(db_path, "wf-nd", intake_enabled=True, now=now)
    op = ops[0]
    claimed = nd_store.claim(db_path, op["id"], "owner-1", now=now)
    started = nd_store.start(db_path, claimed["id"], "owner-1", now=now, expected_task_ids=["wf-nd-cleanroom_test-auto"])
    assert started["started"] == 1

    store.record_event(
        "launch_rejected",
        {
            "dispatch_operation_id": started["id"],
            "dispatch_attempt": started["attempts"],
            "task_id": "wf-nd-cleanroom_test-auto",
            "resources_absent": True,
            "reason": "TOKEN_EXHAUSTED",
        },
        workflow_id="wf-nd",
        node_id="cleanroom_test",
        task_id="wf-nd-cleanroom_test-auto",
        source="launch",
    )
    res = nd_store.direct_finished(
        db_path, started["id"], "owner-1", ["wf-nd-cleanroom_test-auto"], now=now + 1
    )
    assert res["status"] == "pending"
    assert res["started"] == 0
    assert res["detail"]["reason"] == "dispatch_resources_absent"
