"""Regression tests for the fix-loop implementation repair.

These tests intentionally exercise temporary SQLite/workflow-doc state only.  They
are the executable contract for the six blockers reported by test-0924.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import multiprocessing
import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from herdr import blocked_sla, delivery_record, workflow_docs
from herdr.state_store import SQLiteStateStore

ROOT = Path(__file__).resolve().parent.parent


def load_script(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(name, str(path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed_task(store: SQLiteStateStore, task_id: str = "task-cas") -> dict:
    store.save_workflow({"workflow_id": "wf-fix1", "status": "running"})
    task = {
        "task_id": task_id,
        "workflow_id": "wf-fix1",
        "node": "implementation",
        "stage": "implementation",
        "agent": "opencode",
        "status": "working",
        "started_at": 1.0,
        "created_at": 1.0,
    }
    store.save_task(task)
    return store.get_task(task_id)


def cas_child(db_path: str, task_id: str, ready, release, result_queue):
    try:
        store = SQLiteStateStore(Path(db_path))
        task = store.get_task(task_id)
        result_queue.put(("observed", task["version"]))
        release.wait(10)
        result = store.compare_and_set_task_transition(
            task_id=task_id,
            to_status="agent_done",
            reason="completion_test",
            source="test",
            expected_status="working",
            expected_version=task["version"],
        )
        result_queue.put(("accepted", bool(result.get("accepted"))))
    except (AssertionError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        result_queue.put(("error", repr(exc)))


def test_state_store_cas_rejects_same_status_reopen_with_new_version(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    observed = seed_task(store)

    # A human can rewrite blocked -> working and land on the same status value.
    # The monotonic task version must still invalidate the old observation.
    store.update_task_metadata(observed["task_id"], {"human_touch": "reopen"})

    result = store.compare_and_set_task_transition(
        task_id=observed["task_id"],
        to_status="agent_done",
        reason="completion_test",
        source="test",
        expected_status="working",
        expected_version=observed["version"],
    )

    assert result["accepted"] is False
    assert result["reason"] == "cas_mismatch"
    assert store.get_task(observed["task_id"])["status"] == "working"
    assert store.list_events(task_id=observed["task_id"], event_type="task_transition") == []


def test_state_store_cas_rejects_race_from_independent_process(tmp_path):
    db_path = str(tmp_path / "state.db")
    store = SQLiteStateStore(Path(db_path))
    observed = seed_task(store)
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    result_queue = ctx.Queue()
    process = ctx.Process(
        target=cas_child,
        args=(db_path, observed["task_id"], ready, release, result_queue),
    )
    process.start()
    assert result_queue.get(timeout=10)[0] == "observed"
    # Parent uses an independent connection to perform the human rewrite.
    store.update_task_metadata(observed["task_id"], {"human_touch": "reopen"})
    release.set()
    result = result_queue.get(timeout=10)
    assert result[0] != "error", result
    process.join(5)
    if process.is_alive():
        process.terminate()
        process.join(2)
    assert result == ("accepted", False)
    assert process.exitcode == 0
    assert store.get_task(observed["task_id"])["status"] == "working"


def test_completion_requires_two_stable_samples_after_absence(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-completion")
    first = store.observe_completion(
        task_id=task["task_id"],
        marker_present=False,
        agent_status="working",
        observed_at=100.0,
    )
    assert first["ready"] is False

    second = store.observe_completion(
        task_id=task["task_id"],
        marker_present=True,
        agent_status="working",
        observed_at=103.0,
    )
    assert second["ready"] is False
    assert second["consecutive_samples"] == 1

    third = store.observe_completion(
        task_id=task["task_id"],
        marker_present=True,
        agent_status="idle",
        observed_at=106.0,
    )
    assert third["ready"] is True
    assert third["consecutive_samples"] == 2
    assert third["observed_version"] == task["version"]


def test_completion_epoch_change_invalidates_old_marker_observation(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-epoch")
    store.observe_completion(
        task_id=task["task_id"], marker_present=False,
        agent_status="working", observed_at=100.0,
    )
    store.observe_completion(
        task_id=task["task_id"], marker_present=True,
        agent_status="working", observed_at=103.0,
    )
    store.update_task_metadata(task["task_id"], {"human_touch": "reopen"})
    fresh = store.observe_completion(
        task_id=task["task_id"], marker_present=True,
        agent_status="idle", observed_at=106.0,
    )
    assert fresh["epoch_changed"] is True
    assert fresh["ready"] is False
    assert store.get_task(task["task_id"])["status"] == "working"


def test_completion_marker_disappearance_is_uncertain_not_a_deadlock(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-uncertain")
    store.observe_completion(
        task_id=task["task_id"], marker_present=False,
        agent_status="working", observed_at=100.0,
    )
    store.observe_completion(
        task_id=task["task_id"], marker_present=True,
        agent_status="working", observed_at=103.0,
    )
    result = store.observe_completion(
        task_id=task["task_id"], marker_present=False,
        agent_status="working", observed_at=106.0,
    )
    assert result["uncertain"] is True
    assert result["ready"] is False


def test_blocked_sla_restores_one_repush_and_one_human_escalation():
    task = {"task_id": "t-blocked", "status": "blocked", "updated_at": 0}
    episode = {
        "task_id": task["task_id"],
        "entry_updated_at": 0,
        "active_seconds": blocked_sla.first_sla_seconds(),
        "repushes": 0,
        "human_escalations": 0,
    }
    decision = blocked_sla.decide_blocked_action(
        task=task, episode=episode, now=blocked_sla.first_sla_seconds(),
    )
    assert decision["action"] == "repush"
    assert decision["repush_number"] == 1

    episode["repushes"] = 1
    episode["active_seconds"] = (
        blocked_sla.first_sla_seconds() + blocked_sla.second_sla_seconds()
    )
    escalated = blocked_sla.decide_blocked_action(
        task=task, episode=episode, now=episode["active_seconds"],
    )
    assert escalated["action"] == "escalate"
    assert escalated["episode_id"].endswith(":0")

    episode["human_escalations"] = 1
    assert blocked_sla.decide_blocked_action(
        task=task, episode=episode, now=episode["active_seconds"],
    )["action"] == "suppressed_bounds"


def test_blocked_sla_excludes_jitter_and_keeps_episode_dedup():
    task = {"task_id": "t-jitter", "status": "blocked", "updated_at": 1000}
    episode = {
        "task_id": task["task_id"],
        "entry_updated_at": 1000,
        "active_seconds": 10000,
        "repushes": 0,
        "human_escalations": 0,
    }
    decision = blocked_sla.decide_blocked_action(
        task=task, episode=episode, now=1001,
    )
    assert decision["action"] == "suppressed_jitter"
    episode["entry_updated_at"] = 0
    episode["repushes"] = 1
    episode["active_seconds"] = blocked_sla.first_sla_seconds()
    assert blocked_sla.decide_blocked_action(
        task=task, episode=episode, now=blocked_sla.first_sla_seconds(),
    )["action"] != "repush"


def test_controller_records_failed_repush_and_later_human_escalation(tmp_path):
    controller = load_script("herdr_controller_sla_fix1", "services/herdr-controller.py")
    from herdr import liveness

    store = SQLiteStateStore(tmp_path / "state.db")
    store.save_workflow({"workflow_id": "wf-sla", "status": "running"})
    store.save_task({
        "task_id": "t-sla", "workflow_id": "wf-sla", "status": "blocked",
        "node": "implementation", "stage": "implementation", "pane_id": "pane-sla",
    })
    task = store.get_task("t-sla")
    episode_store = liveness.EpisodeStore(tmp_path / "attention.json")
    entry = float(task["updated_at"])
    episode_store.upsert("t-sla:blocked_sla", {
        "task_id": "t-sla", "entry_updated_at": entry,
        "entry_version": task["version"], "active_seconds": 1800,
        "last_tick_at": entry, "repushes": 0, "human_escalations": 0,
        "last_action_at": None, "episode_id": f"t-sla:{int(entry)}:{task['version']}",
    })
    with patch.object(controller, "_attention_store", episode_store), \
         patch.object(controller, "_get_store", return_value=store), \
         patch.object(controller, "_send_blocked_repush", return_value=(False, "pane gone")), \
         patch.object(controller, "enqueue_coordinator_event"):
        decision = controller.process_blocked_sla_task(
            store.get_task("t-sla"), now=entry + 1801,
        )
        assert decision["action"] == "repush"
        for _ in range(100):
            event_types = [
                event["event_type"] for event in store.list_events(task_id="t-sla")
            ]
            if (
                "prompt_delivery_failed" in event_types
                and "blocked_repush_failed" in event_types
            ):
                break
            time.sleep(0.01)
        assert "prompt_delivery_failed" in event_types
        assert "blocked_repush_failed" in event_types
    episode_store.upsert("t-sla:blocked_sla", {
        "active_seconds": 3600, "last_tick_at": entry + 1801,
        "last_action_at": entry + 1801,
    })
    with patch.object(controller, "_attention_store", episode_store), \
         patch.object(controller, "_get_store", return_value=store), \
         patch.object(controller, "_send_blocked_repush", return_value=(True, "recovered")), \
         patch.object(controller, "enqueue_coordinator_event"):
        decision = controller.process_blocked_sla_task(
            store.get_task("t-sla"), now=entry + 2401,
        )
        assert decision["action"] == "repush_recover"
        for _ in range(100):
            event_types = [
                event["event_type"] for event in store.list_events(task_id="t-sla")
            ]
            if "blocked_auto_repush_recovered" in event_types:
                break
            time.sleep(0.01)
        assert "blocked_auto_repush_recovered" in event_types
    episode_store.upsert("t-sla:blocked_sla", {
        "active_seconds": 3600, "last_tick_at": entry + 2401,
        "last_action_at": entry + 2401, "recovery_attempts": 1,
    })
    with patch.object(controller, "_attention_store", episode_store), \
         patch.object(controller, "_get_store", return_value=store), \
         patch.object(controller, "_notify_blocked_human_upgrade", return_value=True):
        decision = controller.process_blocked_sla_task(
            store.get_task("t-sla"), now=entry + 3601,
        )
    assert decision["action"] == "escalate"
    assert "blocked_human_escalated" in [
        event["event_type"] for event in store.list_events(task_id="t-sla")
    ]


def test_delivery_selector_rejects_ambiguous_active_candidates(tmp_path):
    with tempfile.TemporaryDirectory() as directory, \
            patch.dict(os.environ, {workflow_docs.DOCS_DIR_ENV: directory}):
            a = workflow_docs.append_note(
                "wf-delivery", kind="delivery", title="a", node="wrapup",
                fields={"delivery_id": "candidate-a", "candidate_sha": "a"},
            )
            b = workflow_docs.append_note(
                "wf-delivery", kind="delivery", title="b", node="wrapup",
                fields={"delivery_id": "candidate-b", "candidate_sha": "b"},
            )
            with pytest.raises(delivery_record.DeliveryAmbiguityError):
                delivery_record.select_effective_delivery([a, b])


def test_delivery_supersede_and_fix_loop_invalidation_never_fallback(tmp_path):
    with tempfile.TemporaryDirectory() as directory, \
            patch.dict(os.environ, {workflow_docs.DOCS_DIR_ENV: directory}):
            old = workflow_docs.append_note(
                "wf-delivery", kind="delivery", title="old", node="wrapup",
                fields={"delivery_id": "candidate-a", "candidate_sha": "a"},
            )
            new = workflow_docs.append_note(
                "wf-delivery", kind="delivery", title="new", node="wrapup",
                fields={
                    "delivery_id": "candidate-b",
                    "candidate_sha": "b",
                    "supersedes": old["delivery_id"],
                },
            )
            assert delivery_record.select_effective_delivery([old, new])["delivery_id"] == "candidate-b"

            invalidated = workflow_docs.append_note(
                "wf-delivery", kind="invalidation", title="fix-loop",
                node="wrapup", invalidates=["wrapup"],
                fields={"invalidated_candidates": ["candidate-b"]},
            )
            annotated = workflow_docs.annotate_notes([old, new, invalidated])
            assert delivery_record.select_effective_delivery(annotated) is None
            assert delivery_record.select_effective_delivery(
                delivery_record.apply_invalidation(annotated)
            ) is None


def test_force_is_a_direct_legacy_close_choice():
    module = load_script("herdr_task_force_fix1", "bin/herdr-task")
    tasks = [{
        "task_id": "t-force",
        "workflow_id": "wf-force",
        "status": "committed",
        "integration_mode": "git",
        "finalize_escalated": True,
    }]
    with patch.object(module, "load_tasks", return_value={"tasks": tasks}), \
         patch.object(module, "_load_workflow_entry", return_value=(None, {})), \
         patch.object(module, "_finalize_one", return_value={"task_id": "t-force", "status": "cleaned", "action": "finalized"}), \
         patch.object(module, "_workflow_stage_tabs", return_value={"tab_ids": [], "workspace_id": None, "coordinator_pane": None, "owned_pane_ids": set()}), \
         patch.object(module, "_mark_workflow_completed"), \
         patch.object(module, "stage_reset"):
        report = module.close_workflow("wf-force", force=True)
    assert report["escalated_accepted"] == [{"task_id": "t-force", "via": "force"}]


def test_router_opt_out_audit_failure_is_fail_closed(tmp_path):
    from herdr import agent_router

    class BrokenStore:
        def get_workflow(self, workflow_id):
            return {"workflow_id": workflow_id, "project_id": "p", "status": "running"}

        def list_tasks(self, workflow_id=None, status=None):
            return [{
                "workflow_id": "wf-audit",
                "node": "implementation",
                "agent": "codex",
                "status": "completed",
            }]

        def record_event(self, *args, **kwargs):
            raise OSError("audit disk full")

    with patch.object(agent_router, "_get_store", return_value=BrokenStore()), \
         patch.object(agent_router, "workflow_config_for", return_value={
             "nodes": [{
                 "id": "test",
                 "agent_policy": {
                     "allow_reuse_implementation_agents": True,
                     "reuse_reason": "controlled emergency review",
                 },
             }]
         }), \
         patch.object(agent_router, "ensure_pool_for_project", return_value={
             "allowed_agents": ["codex"], "disabled_agents": [],
             "stage_preferences": {"test": ["codex"]},
             "task_type_preferences": {"test": ["codex"]},
         }), pytest.raises(RuntimeError, match="audit"):
        agent_router.choose_agent("wf-audit", "test", "test", requested="auto")


def test_router_failure_does_not_acquire_a_pane(tmp_path, monkeypatch):
    module = load_script("herdr_task_router_failure_fix1", "bin/herdr-task")
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(db_path))
    args = SimpleNamespace(
        task_id="task-no-pane", workflow_id="wf-no-pane", node="test", stage=None,
        agent="auto", task_type="test", integration_mode="none", onto=None,
        source=str(tmp_path), goal="review", acceptance=[], prompt="review",
        test_cmd=None, lint_cmd=None, repro_cmd=None,
    )
    project = {
        "project_id": "p", "project_root": str(tmp_path), "execution": {"mode": "git"},
    }
    with patch.object(module, "project_for_workflow", return_value=project), \
         patch.object(module, "ensure_stage_topology", side_effect=AssertionError("pane topology called")), \
         patch.object(module, "choose_agent", side_effect=RuntimeError("empty review pool")), \
         patch.object(module, "acquire_pane_for_task", side_effect=AssertionError("pane acquired")), \
         pytest.raises(SystemExit) as exc:
        module._launch_task(args)
    assert exc.value.code == 2
    store = SQLiteStateStore(db_path)
    task = store.get_task("task-no-pane")
    assert task is not None
    assert task["status"] == "failed"
    assert task["pane_id"] in (None, "")
    events = store.list_events(task_id="task-no-pane", event_type="router_isolation_rejected")
    assert events and events[-1]["payload"]["pane_dispatched"] is False
    from herdr.state_store import reset_state_store
    reset_state_store()
