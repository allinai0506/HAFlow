"""Regression tests for the second fix-loop repair.

Each test drives the real StateStore, Controller, Router, or shared-note
boundary.  External process/agent transports are replaced only at the final
seam, and all state is rooted below pytest's temporary directory.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from herdr import agent_router, delivery_record, liveness, repo_hygiene, workflow_docs
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


def seed_task(store: SQLiteStateStore, task_id: str, **extra) -> dict:
    store.save_workflow({"workflow_id": "wf-fix4", "status": "running"})
    task = {
        "task_id": task_id,
        "workflow_id": "wf-fix4",
        "node": "implementation",
        "stage": "implementation",
        "agent": "opencode",
        "status": "working",
        "started_at": -1000.0,
        "created_at": -1000.0,
    }
    task.update(extra)
    store.save_task(task)
    return store.get_task(task_id)


def test_fr1_epoch_change_restarts_double_sampling_and_controller_commits_once(tmp_path):
    """A human/version rewrite must not leave the first sample permanently NULL."""
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-epoch")
    store.observe_completion(
        task_id=task["task_id"],
        marker_present=False,
        agent_status="working",
        observed_at=100.0,
    )
    store.observe_completion(
        task_id=task["task_id"],
        marker_present=True,
        agent_status="working",
        observed_at=103.0,
    )
    store.update_task_metadata(task["task_id"], {"human_touch": "reopen"})

    first = store.observe_completion(
        task_id=task["task_id"],
        marker_present=True,
        agent_status="idle",
        observed_at=106.0,
    )
    assert first["epoch_changed"] is True
    assert first["ready"] is False

    second = store.observe_completion(
        task_id=task["task_id"],
        marker_present=True,
        agent_status="idle",
        observed_at=109.0,
    )
    assert second["ready"] is True
    assert second["consecutive_samples"] == 2

    controller = load_script("herdr_controller_fr1_fix4", "services/herdr-controller.py")
    with patch.object(controller, "_get_store", return_value=store), patch.object(
        controller, "enqueue_coordinator_event"
    ):
        assert controller.process_completion_observation(
            store.get_task(task["task_id"]), now=109.0
        )
        # A repeated Controller pass cannot commit the same observation twice.
        assert not controller.process_completion_observation(
            store.get_task(task["task_id"]), now=110.0
        )

    transitions = store.list_events(task_id=task["task_id"], event_type="task_transition")
    assert len(transitions) == 1
    assert store.get_task(task["task_id"])["status"] == "agent_done"


def test_fr1_conditional_clear_cannot_delete_a_new_epoch_observation(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-clear-race")
    store.observe_completion(
        task_id=task["task_id"], marker_present=False,
        agent_status="working", observed_at=100.0,
    )
    old_version = task["version"]
    store.update_task_metadata(task["task_id"], {"human_touch": "reopen"})
    store.observe_completion(
        task_id=task["task_id"], marker_present=True,
        agent_status="idle", observed_at=103.0,
    )
    assert store.clear_completion_observation(
        task["task_id"],
        expected_status="working",
        expected_version=old_version,
    ) is False
    current = store.get_completion_observation(task["task_id"])
    assert current["observed_version"] == store.get_task(task["task_id"])["version"]


def test_fr1_requires_a_full_poll_interval_between_stable_samples(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-interval")
    store.observe_completion(
        task_id=task["task_id"], marker_present=False,
        agent_status="working", observed_at=100.0,
    )
    store.observe_completion(
        task_id=task["task_id"], marker_present=True,
        agent_status="working", observed_at=103.0,
    )
    too_soon = store.observe_completion(
        task_id=task["task_id"], marker_present=True,
        agent_status="idle", observed_at=104.0,
    )
    assert too_soon["ready"] is False
    spaced = store.observe_completion(
        task_id=task["task_id"], marker_present=True,
        agent_status="idle", observed_at=106.0,
    )
    assert spaced["ready"] is True


def test_fr2_concurrent_controller_sweeps_claim_one_repush(tmp_path):
    """Two Controller workers crossing the SLA boundary send only one prompt."""
    controller = load_script("herdr_controller_fr2_fix4", "services/herdr-controller.py")
    store = SQLiteStateStore(tmp_path / "state.db")
    store.save_workflow({"workflow_id": "wf-fix4", "status": "running"})
    store.save_task({
        "task_id": "task-blocked",
        "workflow_id": "wf-fix4",
        "status": "blocked",
        "node": "implementation",
        "stage": "implementation",
        "agent": "opencode",
        "pane_id": "pane-blocked",
        "sentinel_reason": "inner_loop_exhausted",
    })
    task = store.get_task("task-blocked")
    episode_path = tmp_path / "attention.json"
    episode_store = liveness.EpisodeStore(episode_path)
    entry = float(task["updated_at"])
    episode_store.upsert("task-blocked:blocked_sla", {
        "task_id": task["task_id"],
        "entry_updated_at": entry,
        "entry_version": task["version"],
        "active_seconds": 1800.0,
        "last_tick_at": entry,
        "repushes": 0,
        "human_escalations": 0,
        "last_action_at": None,
        "episode_id": f"task-blocked:{int(entry)}:{task['version']}",
    })

    step_barrier = threading.Barrier(2)
    original_step = controller._blocked_sla_step

    def synchronized_step(current, now, poll_seconds=3.0):
        decision = original_step(current, now, poll_seconds=poll_seconds)
        try:
            step_barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return decision

    calls = []
    calls_lock = threading.Lock()

    def send_prompt(_task, _decision):
        with calls_lock:
            calls.append(_decision)
        return True, "delivered"

    results = []

    def run_one():
        try:
            results.append(controller.process_blocked_sla_task(
                task, now=entry + 1801.0, send_prompt=send_prompt,
            ))
        except (OSError, RuntimeError, ValueError, AssertionError) as exc:
            results.append(exc)

    with patch.object(controller, "_attention_store", episode_store), patch.object(
        controller, "_get_store", return_value=store
    ), patch.object(controller, "_blocked_sla_step", synchronized_step), patch.object(
        controller, "enqueue_coordinator_event"
    ):
        workers = [threading.Thread(target=run_one) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=8)
        assert all(not worker.is_alive() for worker in workers)

    assert not any(isinstance(result, BaseException) for result in results)
    assert len(calls) == 1
    assert episode_store.get("task-blocked:blocked_sla")["delivery_attempts"] == 1
    repush_events = [
        event for event in store.list_events(task_id=task["task_id"])
        if event["event_type"] == "blocked_auto_repush"
    ]
    assert len(repush_events) == 1


def test_crash_observation_reaches_controller_auto_recovery_path(tmp_path):
    controller = load_script("herdr_controller_crash_fix4", "services/herdr-controller.py")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-crash")
    store.record_event(
        "agent_process_crash_observed",
        {
            "next_status": "failed",
            "observed_status": task["status"],
            "observed_version": task["version"],
        },
        workflow_id=task["workflow_id"],
        node_id=task["node"],
        task_id=task["task_id"],
        source="herdr-sentinel",
    )
    with patch.object(controller, "_get_store", return_value=store):
        assert controller.process_crash_observations() == 1
    failed = store.get_task(task["task_id"])
    assert failed["status"] == "failed"
    reasons = [
        entry.get("reason")
        for entry in failed.get("status_history", [])
        if entry.get("to") == "failed"
    ]
    assert "agent_process_crash" in reasons
    assert task["task_id"] in {
        item["task_id"]
        for item in liveness.select_infra_failures_for_recovery(
            [failed], {"agent_process_crash"}, max_attempts=2
        )
    }


def test_fr2_new_task_epoch_discards_an_old_action_lease(tmp_path, monkeypatch):
    controller = load_script("herdr_controller_fr2_epoch_fix4", "services/herdr-controller.py")
    store = SQLiteStateStore(tmp_path / "state.db")
    store.save_workflow({"workflow_id": "wf-fix4", "status": "running"})
    store.save_task({
        "task_id": "task-lease",
        "workflow_id": "wf-fix4",
        "status": "blocked",
        "node": "implementation",
        "stage": "implementation",
        "pane_id": "pane-lease",
        "sentinel_reason": "inner_loop_exhausted",
    })
    task = store.get_task("task-lease")
    old_entry = float(task["updated_at"])
    attention = liveness.EpisodeStore(tmp_path / "attention.json")
    attention.upsert("task-lease:blocked_sla", {
        "task_id": task["task_id"], "entry_updated_at": old_entry,
        "entry_version": task["version"], "active_seconds": 1800,
        "last_tick_at": old_entry, "repushes": 0, "human_escalations": 0,
        "last_action_at": None, "episode_id": "old-episode",
        "action_claim": {"claim_id": "old", "action": "repush", "claimed_at": old_entry,
                          "lease_until": old_entry + 10000},
    })
    store.update_task_metadata(task["task_id"], {"human_touch": "reopen"})
    monkeypatch.setenv("HERDR_BLOCKED_FIRST_SLA", "3")
    monkeypatch.setenv("HERDR_BLOCKED_JITTER_WINDOW", "1")
    current = store.get_task(task["task_id"])
    with patch.object(controller, "_attention_store", attention), patch.object(
        controller, "_get_store", return_value=store
    ), patch.object(controller, "_send_blocked_repush", return_value=(True, "new")) as sender, patch.object(
        controller, "enqueue_coordinator_event"
    ):
        assert controller.process_blocked_sla_task(
            current, now=float(current["updated_at"]) + 1,
        )["action"] == "none"
        decision = controller.process_blocked_sla_task(
            current, now=float(current["updated_at"]) + 4,
        )
    assert decision["action"] == "repush"
    sender.assert_called_once()
    assert "action_claim" not in attention.get("task-lease:blocked_sla")


def test_fr2_repush_failure_recovers_once_after_controller_restart_and_escalates(tmp_path):
    controller = load_script("herdr_controller_fr2_restart_fix4", "services/herdr-controller.py")
    store = SQLiteStateStore(tmp_path / "state.db")
    store.save_workflow({"workflow_id": "wf-fix4", "status": "running"})
    store.save_task({
        "task_id": "task-restart",
        "workflow_id": "wf-fix4",
        "status": "blocked",
        "node": "implementation",
        "stage": "implementation",
        "pane_id": "pane-restart",
        "sentinel_reason": "inner_loop_exhausted",
    })
    task = store.get_task("task-restart")
    entry = float(task["updated_at"])
    attention_path = tmp_path / "attention.json"
    first_process = liveness.EpisodeStore(attention_path)
    first_process.upsert("task-restart:blocked_sla", {
        "task_id": task["task_id"],
        "entry_updated_at": entry,
        "entry_version": task["version"],
        "active_seconds": 1800.0,
        "last_tick_at": entry,
        "repushes": 0,
        "human_escalations": 0,
        "last_action_at": None,
        "episode_id": f"task-restart:{int(entry)}:{task['version']}",
    })

    with patch.object(controller, "_attention_store", first_process), patch.object(
        controller, "_get_store", return_value=store
    ), patch.object(
        controller, "_send_blocked_repush", return_value=(False, "transport down")
    ), patch.object(controller, "enqueue_coordinator_event"):
        assert controller.process_blocked_sla_task(task, now=entry + 1801)["action"] == "repush"

    # A new EpisodeStore instance reads the same durable episode, as a restarted
    # Controller would; the failed delivery gets one transport recovery.
    restarted_process = liveness.EpisodeStore(attention_path)
    with patch.object(controller, "_attention_store", restarted_process), patch.object(
        controller, "_get_store", return_value=store
    ), patch.object(
        controller, "_send_blocked_repush", return_value=(True, "recovered")
    ), patch.object(controller, "enqueue_coordinator_event"):
        assert controller.process_blocked_sla_task(
            store.get_task(task["task_id"]), now=entry + 2401
        )["action"] == "repush_recover"

    restarted_process.upsert("task-restart:blocked_sla", {
        "active_seconds": 3600.0,
        "last_tick_at": entry + 2401,
        "last_action_at": entry + 2401,
        "recovery_attempts": 1,
    })
    with patch.object(controller, "_attention_store", restarted_process), patch.object(
        controller, "_get_store", return_value=store
    ), patch.object(controller, "_notify_blocked_human_upgrade", return_value=True):
        assert controller.process_blocked_sla_task(
            store.get_task(task["task_id"]), now=entry + 3601
        )["action"] == "escalate"

    event_types = [event["event_type"] for event in store.list_events(task_id=task["task_id"])]
    assert event_types.count("prompt_delivery_failed") == 1
    assert event_types.count("blocked_auto_repush_recovered") == 1
    assert event_types.count("blocked_human_escalated") == 1


def test_fr4_rejects_identity_payload_conflicts_and_unknown_supersede():
    conflict = [
        {
            "kind": "delivery", "note_id": "n1", "ts": 1,
            "delivery_id": "candidate-x", "candidate_sha": "sha-a",
            "delivery_branch": "branch", "review_task": "review", "test_gate": "test",
        },
        {
            "kind": "delivery", "note_id": "n2", "ts": 2,
            "delivery_id": "candidate-x", "candidate_sha": "sha-b",
            "delivery_branch": "branch", "review_task": "review", "test_gate": "test",
        },
    ]
    with pytest.raises(ValueError):
        delivery_record.select_effective_delivery(conflict)

    unknown_replacement = [{
        "kind": "delivery", "note_id": "n3", "ts": 3,
        "delivery_id": "candidate-y", "candidate_sha": "sha-y",
        "delivery_branch": "branch", "review_task": "review", "test_gate": "test",
        "supersedes": "candidate-does-not-exist",
    }]
    with pytest.raises(ValueError):
        delivery_record.select_effective_delivery(unknown_replacement)


def test_fr4_cli_rejects_unknown_supersede_target(tmp_path):
    module = load_script("herdr_task_delivery_fix4", "bin/herdr-task")
    docs_dir = tmp_path / "workflow-docs"
    with patch.dict(os.environ, {workflow_docs.DOCS_DIR_ENV: str(docs_dir)}):
        workflow_docs.append_note(
            "wf-delivery-cli",
            kind="delivery",
            title="old",
            node="wrapup",
            fields={
                "delivery_id": "candidate-old",
                "candidate_sha": "sha-old",
                "delivery_branch": "branch",
                "review_task": "review",
                "test_gate": "test",
            },
        )
        with pytest.raises(SystemExit) as exc:
            module.record_delivery_note(
                "wf-delivery-cli",
                "branch",
                "sha-new",
                "review",
                "test",
                supersedes="candidate-missing",
            )
        assert exc.value.code == 2
        assert len(workflow_docs.load_notes("wf-delivery-cli")) == 1


def test_fr4_invalidation_is_scoped_to_its_node_and_candidate():
    notes = [
        {
            "kind": "delivery", "note_id": "wrapup-delivery", "ts": 10,
            "node": "wrapup", "base_sha": "base-1", "delivery_id": "wrapup-id",
            "candidate_sha": "wrapup-sha", "delivery_branch": "branch",
            "review_task": "review", "test_gate": "test",
        },
        {
            "kind": "delivery", "note_id": "review-delivery", "ts": 11,
            "node": "review", "base_sha": "base-1", "delivery_id": "review-id",
            "candidate_sha": "review-sha", "delivery_branch": "branch",
            "review_task": "review", "test_gate": "test",
        },
        {
            "kind": "invalidation", "note_id": "invalidate-review", "ts": 12,
            "node": "review", "base_sha": "base-1", "invalidates": ["review"],
        },
    ]
    annotated = workflow_docs.annotate_notes(notes)
    assert annotated[0]["stale"] is False
    assert annotated[1]["stale"] is True
    assert delivery_record.select_effective_delivery(annotated)["delivery_id"] == "wrapup-id"


def test_fr4_concurrent_shared_note_appends_remain_fail_closed(tmp_path):
    docs_dir = tmp_path / "workflow-docs"
    with patch.dict(os.environ, {workflow_docs.DOCS_DIR_ENV: str(docs_dir)}):
        errors = []

        def append(index):
            try:
                workflow_docs.append_note(
                    "wf-concurrent-delivery",
                    kind="delivery",
                    title=f"candidate-{index}",
                    node="wrapup",
                    fields={
                        "delivery_id": f"candidate-{index}",
                        "candidate_sha": f"sha-{index}",
                        "delivery_branch": "branch",
                        "review_task": "review",
                        "test_gate": "test",
                    },
                )
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append(exc)

        workers = [threading.Thread(target=append, args=(index,)) for index in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        assert not errors
        notes = workflow_docs.load_notes("wf-concurrent-delivery")
        assert len(notes) == 2
        with pytest.raises(delivery_record.DeliveryAmbiguityError):
            delivery_record.select_effective_delivery(notes)


def test_fr3_porcelain_parser_preserves_paths_with_spaces():
    porcelain = " M src/a file.txt\n?? ignored.txt\nR  old name.py -> new name.py\n"
    assert repo_hygiene.parse_porcelain_paths(porcelain) == [
        "src/a file.txt",
        "new name.py",
    ]


def test_fr5_legacy_force_remains_a_direct_cli_choice():
    module = load_script("herdr_task_force_fix4", "bin/herdr-task")
    tasks = [{
        "task_id": "task-force",
        "workflow_id": "wf-force",
        "status": "committed",
        "integration_mode": "git",
        "finalize_escalated": True,
    }]
    with patch.object(module, "load_tasks", return_value={"tasks": tasks}), patch.object(
        module, "_load_workflow_entry", return_value=(None, {})
    ), patch.object(
        module, "_finalize_one",
        return_value={"task_id": "task-force", "status": "cleaned", "action": "finalized"},
    ), patch.object(
        module, "_workflow_stage_tabs",
        return_value={"tab_ids": [], "workspace_id": None, "coordinator_pane": None,
                      "owned_pane_ids": set()},
    ), patch.object(module, "_mark_workflow_completed"), patch.object(
        module, "stage_reset"
    ):
        report = module.close_workflow("wf-force", force=True, confirm_force=False)
    assert report["escalated_accepted"] == [{"task_id": "task-force", "via": "force"}]


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


def _launch_args(tmp_path, task_id):
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


def _empty_review_launch_context(tmp_path, module, store, *, opt_out=False, audit_store=None):
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
    router_store = audit_store or store
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
    return project, pool, config, router_store


def test_fr6_opt_out_audit_failure_is_fail_closed_before_topology(tmp_path, monkeypatch):
    module = load_script("herdr_task_audit_failure_fix4", "bin/herdr-task")
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(db_path))
    store = SQLiteStateStore(db_path)
    project, pool, config, audit_store = _empty_review_launch_context(
        tmp_path, module, store, opt_out=True, audit_store=_AuditFailingStore(store),
    )
    args = _launch_args(tmp_path, "task-audit-failure")

    with patch.object(module, "project_for_workflow", return_value=project), patch.object(
        module, "ensure_stage_topology", side_effect=AssertionError("topology called")
    ), patch.object(module, "acquire_pane_for_task", side_effect=AssertionError("pane called")), patch.object(
        module, "release_agent_reservation"
    ), patch.object(agent_router, "_get_store", return_value=audit_store), patch.object(
        agent_router, "workflow_config_for", return_value=config
    ), patch.object(agent_router, "ensure_pool_for_project", return_value=pool), pytest.raises(SystemExit) as exc:
        module._launch_task(args)
    assert exc.value.code == 2
    failed = store.get_task("task-audit-failure")
    assert failed["status"] == "failed"
    assert not failed.get("pane_id")
    events = store.list_events(task_id="task-audit-failure", event_type="router_isolation_rejected")
    assert events and events[-1]["payload"]["pane_dispatched"] is False
    assert store.get_workflow("wf-empty-review")["last_dispatch_failure"]["result"] == "failed"
    reset_state_store()


def test_fr6_empty_review_pool_has_real_cli_to_store_lifecycle(tmp_path, monkeypatch):
    module = load_script("herdr_task_empty_pool_fix4", "bin/herdr-task")
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(db_path))
    store = SQLiteStateStore(db_path)
    project, pool, config, _ = _empty_review_launch_context(tmp_path, module, store)
    args = _launch_args(tmp_path, "task-empty-review")

    with patch.object(module, "project_for_workflow", return_value=project), patch.object(
        module, "ensure_stage_topology", side_effect=AssertionError("topology called")
    ), patch.object(module, "acquire_pane_for_task", side_effect=AssertionError("pane called")), patch.object(
        module, "release_agent_reservation"
    ), patch.object(agent_router, "_get_store", return_value=store), patch.object(
        agent_router, "workflow_config_for", return_value=config
    ), patch.object(agent_router, "ensure_pool_for_project", return_value=pool), pytest.raises(SystemExit) as exc:
        module._launch_task(args)
    assert exc.value.code == 2
    failed = store.get_task("task-empty-review")
    assert failed["status"] == "failed"
    assert failed["failure_reason"] == "router_isolation_rejected"
    assert not failed.get("pane_id")
    events = store.list_events(task_id="task-empty-review", event_type="router_isolation_rejected")
    assert events and events[-1]["payload"]["pane_dispatched"] is False
    workflow = store.get_workflow("wf-empty-review")
    assert workflow["last_dispatch_failure"].get("result") == "failed"
    assert workflow["last_dispatch_failure"].get("task_id") == "task-empty-review"
    reset_state_store()
