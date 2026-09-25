"""Regression coverage for the test-r4 blocked findings."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from herdr import agent_router, delivery_record, workflow_docs
from herdr.state_store import SQLiteStateStore, reset_state_store

ROOT = Path(__file__).resolve().parent.parent


def load_script(name: str, relative: str):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(name, str(ROOT / relative)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_completion_requires_two_marker_samples_one_sentinel_cycle_apart(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    store.save_workflow({"workflow_id": "wf-fix5", "status": "running"})
    store.save_task({
        "task_id": "task-fix5-completion",
        "workflow_id": "wf-fix5",
        "node": "implementation",
        "status": "working",
        "started_at": 1.0,
    })

    store.observe_completion(
        "task-fix5-completion", marker_present=False,
        agent_status="working", observed_at=100.0,
    )
    first = store.observe_completion(
        "task-fix5-completion", marker_present=True,
        agent_status="idle", observed_at=103.0,
    )
    too_soon = store.observe_completion(
        "task-fix5-completion", marker_present=True,
        agent_status="idle", observed_at=104.5,
    )

    assert first["consecutive_samples"] == 1
    assert first["ready"] is False
    assert too_soon["consecutive_samples"] == 1
    assert too_soon["ready"] is False

    confirmed = store.observe_completion(
        "task-fix5-completion", marker_present=True,
        agent_status="idle", observed_at=106.0,
    )
    assert confirmed["consecutive_samples"] == 2
    assert confirmed["ready"] is True


class _AuditFailingStore:
    def __init__(self, delegate):
        self.delegate = delegate

    def get_workflow(self, workflow_id):
        return self.delegate.get_workflow(workflow_id)

    def list_tasks(self, **kwargs):
        return self.delegate.list_tasks(**kwargs)

    def record_event(self, event_type, *args, **kwargs):
        if event_type == "router_opt_out_used":
            raise sqlite3.OperationalError("audit disk unavailable")
        return self.delegate.record_event(event_type, *args, **kwargs)


def _empty_review_context(tmp_path, store, *, opt_out=False):
    store.save_workflow({
        "workflow_id": "wf-empty-review",
        "project_id": "project-empty-review",
        "status": "running",
        "healthy_agents": [],
        "unhealthy_agents": {},
    })
    store.save_task({
        "task_id": "implementation-task",
        "workflow_id": "wf-empty-review",
        "node": "implementation",
        "stage": "implementation",
        "agent": "codex",
        "status": "completed",
    })
    project = {
        "project_id": "project-empty-review",
        "project_root": str(tmp_path),
        "execution": {"mode": "git"},
    }
    pool = {
        "allowed_agents": ["codex"],
        "disabled_agents": [],
        "stage_preferences": {"review": ["codex"]},
        "task_type_preferences": {"test": ["codex"]},
    }
    config = {"nodes": [{"id": "review", "agent_policy": {}}]}
    if opt_out:
        config["nodes"][0]["agent_policy"] = {
            "allow_reuse_implementation_agents": True,
            "reuse_reason": "controlled recovery review",
        }
    return project, pool, config


def _empty_review_args(tmp_path, task_id):
    return SimpleNamespace(
        task_id=task_id,
        workflow_id="wf-empty-review",
        node="review",
        stage=None,
        agent="auto",
        task_type="test",
        integration_mode="git",
        onto=None,
        source=str(tmp_path),
        goal="review",
        acceptance=[],
        prompt="review",
        test_cmd=None,
        lint_cmd=None,
        repro_cmd=None,
        allow_reuse=False,
        reuse_reason="",
        agent_policy=None,
        run_id=None,
        supersedes=None,
    )


def _patch_empty_review_router(tmp_path, module, store, pool, config):
    project = {
        "project_id": "project-empty-review",
        "project_root": str(tmp_path),
        "execution": {"mode": "git"},
    }
    return (
        patch.object(module, "project_for_workflow", return_value=project),
        patch.object(agent_router, "_get_store", return_value=store),
        patch.object(agent_router, "workflow_config_for", return_value=config),
        patch.object(agent_router, "ensure_pool_for_project", return_value=pool),
        patch.object(agent_router, "POOLS_FILE", tmp_path / "pools.json"),
        patch.object(agent_router, "RESERVATIONS_FILE", tmp_path / "reservations.json"),
        patch.object(agent_router, "ROUTER_LOCK_FILE", tmp_path / "router.lock"),
    )


def test_empty_review_pool_cli_fails_closed_and_persists_no_pane(tmp_path, monkeypatch):
    module = load_script("herdr_task_empty_review_fix5", "bin/herdr-task")
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(db_path))
    store = SQLiteStateStore(db_path)
    _project, pool, config = _empty_review_context(tmp_path, store)
    args = _empty_review_args(tmp_path, "task-empty-review")
    patches = _patch_empty_review_router(tmp_path, module, store, pool, config)

    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], \
         patch.object(module, "ensure_stage_topology", side_effect=AssertionError("topology called")), \
         patch.object(module, "acquire_pane_for_task", side_effect=AssertionError("pane called")), \
         patch.object(module, "release_agent_reservation"), pytest.raises(SystemExit) as exc:
        module._launch_task(args)

    assert exc.value.code == 2
    failed = store.get_task("task-empty-review")
    assert failed["status"] == "failed"
    assert failed["failure_reason"] == "router_isolation_rejected"
    assert not failed.get("pane_id")
    events = store.list_events(
        task_id="task-empty-review", event_type="router_isolation_rejected",
    )
    assert events and events[-1]["payload"]["pane_dispatched"] is False
    failure = store.get_workflow("wf-empty-review")["last_dispatch_failure"]
    assert failure["task_id"] == "task-empty-review"
    assert failure["result"] == "failed"
    reset_state_store()


def test_opt_out_audit_failure_persists_failed_cli_task_before_topology(tmp_path, monkeypatch):
    module = load_script("herdr_task_audit_failure_fix5", "bin/herdr-task")
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(db_path))
    store = SQLiteStateStore(db_path)
    _project, pool, config = _empty_review_context(tmp_path, store, opt_out=True)
    audit_store = _AuditFailingStore(store)
    args = _empty_review_args(tmp_path, "task-audit-failure")
    patches = _patch_empty_review_router(tmp_path, module, audit_store, pool, config)

    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], \
         patch.object(module, "ensure_stage_topology", side_effect=AssertionError("topology called")), \
         patch.object(module, "acquire_pane_for_task", side_effect=AssertionError("pane called")), \
         patch.object(module, "release_agent_reservation"), pytest.raises(SystemExit) as exc:
        module._launch_task(args)

    assert exc.value.code == 2
    failed = store.get_task("task-audit-failure")
    assert failed["status"] == "failed"
    assert failed["failure_reason"] == "router_isolation_rejected"
    assert not failed.get("pane_id")
    assert store.get_workflow("wf-empty-review")["last_dispatch_failure"]["result"] == "failed"
    reset_state_store()


def test_explicit_opt_out_selects_reused_agent_and_audit_survives_lifecycle(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    _empty_review_context(tmp_path, store, opt_out=True)
    config = {"nodes": [{"id": "review", "agent_policy": {
        "allow_reuse_implementation_agents": True,
        "reuse_reason": "controlled recovery review",
    }}]}
    pool = {
        "allowed_agents": ["codex"], "disabled_agents": [],
        "stage_preferences": {"review": ["codex"]},
        "task_type_preferences": {"test": ["codex"]},
    }

    with patch.object(agent_router, "_get_store", return_value=store), \
         patch.object(agent_router, "workflow_config_for", return_value=config), \
         patch.object(agent_router, "ensure_pool_for_project", return_value=pool), \
         patch.object(agent_router, "POOLS_FILE", tmp_path / "pools.json"), \
         patch.object(agent_router, "RESERVATIONS_FILE", tmp_path / "reservations.json"), \
         patch.object(agent_router, "ROUTER_LOCK_FILE", tmp_path / "router.lock"):
        selected = agent_router.choose_agent(
            "wf-empty-review", "review", "test", requested="auto",
        )
    assert selected == "codex"
    audit = store.list_events(
        workflow_id="wf-empty-review", event_type="router_opt_out_used",
    )
    assert len(audit) == 1
    assert audit[0]["payload"]["reason"] == "controlled recovery review"

    store.save_task({
        "task_id": "task-review-lifecycle", "workflow_id": "wf-empty-review",
        "node": "review", "stage": "review", "agent": selected,
        "status": "blocked",
    })
    store.transition_task(
        "task-review-lifecycle", "working", "operator_recovered", source="test",
    )
    store.transition_task(
        "task-review-lifecycle", "agent_done", "review_completed", source="test",
    )
    assert store.get_task("task-review-lifecycle")["status"] == "agent_done"
    assert len(store.list_events(
        workflow_id="wf-empty-review", event_type="router_opt_out_used",
    )) == 1


def test_concurrent_delivery_candidates_fail_closed_and_duplicate_replay_is_idempotent(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv(workflow_docs.DOCS_DIR_ENV, str(tmp_path / "shared"))
    workflow_id = "wf-fix5-delivery"
    barrier = threading.Barrier(2)

    def append_candidate(candidate_id, sha):
        barrier.wait(timeout=5)
        return workflow_docs.append_note(
            workflow_id,
            kind="delivery",
            title=f"delivery {candidate_id}",
            node="wrapup",
            base_sha="base-fix5",
            fields={
                "delivery_id": candidate_id,
                "delivery_branch": "agent/fix5",
                "candidate_sha": sha,
                "review_task": f"review-{candidate_id}",
                "test_gate": f"gate-{candidate_id}",
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        candidates = list(pool.map(
            lambda pair: append_candidate(*pair),
            [("candidate-a", "sha-a"), ("candidate-b", "sha-b")],
        ))
    notes = workflow_docs.load_notes(workflow_id)
    assert {note["delivery_id"] for note in notes} == {
        "candidate-a", "candidate-b",
    }
    with pytest.raises(delivery_record.DeliveryAmbiguityError):
        delivery_record.select_effective_delivery(notes)

    replay = workflow_docs.append_note(
        workflow_id,
        kind="delivery",
        title="delivery candidate-a replay",
        node="wrapup",
        base_sha="base-fix5",
        fields={
            "delivery_id": "candidate-a",
            "delivery_branch": "agent/fix5",
            "candidate_sha": "sha-a",
            "review_task": "review-candidate-a",
            "test_gate": "gate-candidate-a",
        },
    )
    assert replay["delivery_id"] == candidates[0]["delivery_id"]
    replayed_candidate = [
        note for note in workflow_docs.load_notes(workflow_id)
        if note.get("delivery_id") == "candidate-a"
    ]
    effective = delivery_record.select_effective_delivery(replayed_candidate)
    assert effective["delivery_id"] == "candidate-a"
    with pytest.raises(delivery_record.DeliveryAmbiguityError):
        delivery_record.select_effective_delivery(workflow_docs.load_notes(workflow_id))
