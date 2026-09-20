import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
import pytest

from herdr.transitions import (
    TASK_TRANSITIONS,
    WORKFLOW_TRANSITIONS,
    ACTIVE_TASK_STATUSES,
    COMPLETED_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    InvalidTransitionError,
    validate_task_transition,
    validate_workflow_transition,
)
from herdr.state_store import get_state_store, reset_state_store, SQLiteStateStore
from herdr import state_db
from herdr import kernel
from herdr.trajectory import TrajectoryLedger


@pytest.fixture
def clean_store(tmp_path, monkeypatch):
    """Provide an isolated SQLite database and reset global StateStore."""
    db_path = tmp_path / "test_state.db"
    workflows_file = tmp_path / "workflows.json"
    tasks_file = tmp_path / "tasks.json"

    workflows_file.write_text(json.dumps({"workflows": {}}), encoding="utf-8")
    tasks_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")

    monkeypatch.setenv("HERDR_STATE_DB", str(db_path))
    monkeypatch.setenv("WORKFLOWS_FILE", str(workflows_file))
    monkeypatch.setenv("TASKS_FILE", str(tasks_file))

    reset_state_store()
    store = get_state_store(db_path)

    yield store, db_path, tmp_path

    reset_state_store()


class TestStateTransitionsRules:
    """Test pure functional state transition rules."""

    def test_validate_task_transitions(self):
        assert validate_task_transition("pending", "dispatched") is True
        assert validate_task_transition("dispatched", "working") is True
        assert validate_task_transition("working", "agent_done") is True
        assert validate_task_transition("agent_done", "completed") is True
        assert validate_task_transition("completed", "cleanup_ready") is True
        assert validate_task_transition("cleanup_ready", "cleaned") is True
        assert validate_task_transition("cleaned", "superseded") is True

        # Idempotent
        assert validate_task_transition("working", "working") is True

        # Illegal
        with pytest.raises(InvalidTransitionError):
            validate_task_transition("pending", "agent_done")

        with pytest.raises(InvalidTransitionError):
            validate_task_transition("completed", "working")

        with pytest.raises(InvalidTransitionError):
            validate_task_transition("superseded", "working")

    def test_validate_workflow_transitions(self):
        assert validate_workflow_transition("pending", "running") is True
        assert validate_workflow_transition("running", "paused") is True
        assert validate_workflow_transition("paused", "running") is True
        assert validate_workflow_transition("running", "completed") is True

        # Reopen
        assert validate_workflow_transition("completed", "in_progress") is True
        assert validate_workflow_transition("in_progress", "running") is True

        # Idempotent
        assert validate_workflow_transition("running", "running") is True

        # Illegal
        with pytest.raises(InvalidTransitionError):
            validate_workflow_transition("paused", "completed")

        with pytest.raises(InvalidTransitionError):
            validate_workflow_transition("pending", "non_existent_status")


class TestStateTransitionGateway:
    """Test State Transition Gateway execution, atomicity, and event capture."""

    def test_transition_task_success_and_event_recorded(self, clean_store):
        store, db_path, _ = clean_store

        # 1. Seed a task
        task = {
            "task_id": "t-gw-01",
            "workflow_id": "wf-gw-01",
            "node": "plan",
            "stage": "plan",
            "agent": "codex",
            "status": "pending",
        }
        store.save_task(task)

        # 2. Transition pending -> dispatched
        res = kernel.transition_task(
            task_id="t-gw-01",
            to_status="dispatched",
            reason="coordinator dispatched task",
            source="herdr-controller",
            metadata={"pane_id": "p-100"},
        )

        assert res["ok"] is True
        assert res["old_status"] == "pending"
        assert res["new_status"] == "dispatched"
        assert res["task_id"] == "t-gw-01"

        # Verify task in StateStore
        updated = store.get_task("t-gw-01")
        assert updated["status"] == "dispatched"
        assert updated.get("pane_id") == "p-100"

        # Verify canonical WorkflowEvent in StateStore
        events = store.list_events(task_id="t-gw-01", event_type="task_transition")
        assert len(events) == 1
        ev = events[0]
        assert ev["event_type"] == "task_transition"
        assert ev["workflow_id"] == "wf-gw-01"
        assert ev["node_id"] == "plan"
        assert ev["agent_id"] == "codex"
        assert ev["source"] == "herdr-controller"
        assert ev["payload"]["from_status"] == "pending"
        assert ev["payload"]["to_status"] == "dispatched"
        assert ev["payload"]["reason"] == "coordinator dispatched task"
        assert ev["payload"]["pane_id"] == "p-100"

    def test_transition_task_appends_ordered_trajectory_facts(self, clean_store):
        store, db_path, _ = clean_store
        store.save_task({
            "task_id": "t-ledger-01",
            "workflow_id": "wf-ledger-01",
            "node": "implementation",
            "stage": "implementation",
            "agent": "claude",
            "run_id": "run-ledger-01",
            "runtime": {
                "agent_session_id": "session-ledger-01",
                "agent_name": "implementation-agent",
                "workspace_id": "workspace-ledger-01",
                "tab_id": "tab-ledger-01",
                "pane_id": "pane-ledger-01",
            },
            "status": "pending",
        })

        for status in ("dispatched", "working", "agent_done", "completed"):
            kernel.transition_task(
                task_id="t-ledger-01",
                to_status=status,
                reason="ledger integration test",
                store=store,
            )

        events = TrajectoryLedger(db_path).list_events("run-ledger-01")

        assert [event["event_type"] for event in events] == [
            "task_status_changed",
            "task_status_changed",
            "task_status_changed",
            "task_status_changed",
            "task_completed",
            "run_completed",
        ]
        assert [event["sequence"] for event in events] == list(range(1, 7))
        assert events[-1]["status"] == "completed"
        assert events[0]["agent_session_id"] == "session-ledger-01"

    def test_legacy_task_uses_stable_run_id_and_records_failure(self, clean_store):
        store, db_path, _ = clean_store
        store.save_task({
            "task_id": "legacy-failure",
            "workflow_id": "wf-legacy",
            "node": "implementation",
            "status": "pending",
        })

        kernel.transition_task("legacy-failure", "dispatched", "dispatch", store=store)
        kernel.transition_task("legacy-failure", "failed", "agent crashed", store=store)

        events = TrajectoryLedger(db_path).list_events("run_legacy-failure")

        assert [event["event_type"] for event in events] == [
            "task_status_changed",
            "task_status_changed",
            "task_failed",
            "run_failed",
        ]
        assert events[-1]["metadata"]["reason"] == "agent crashed"

    def test_transition_task_illegal_rejected_and_no_event(self, clean_store):
        store, db_path, _ = clean_store

        task = {
            "task_id": "t-gw-02",
            "workflow_id": "wf-gw-01",
            "node": "code",
            "stage": "code",
            "agent": "claude",
            "status": "pending",
        }
        store.save_task(task)

        # Attempt illegal transition: pending -> completed
        with pytest.raises(InvalidTransitionError):
            kernel.transition_task(
                task_id="t-gw-02",
                to_status="completed",
                reason="illegal jump",
                source="rogue-caller",
            )

        # Verify task unchanged
        t = store.get_task("t-gw-02")
        assert t["status"] == "pending"

        # Verify no event created
        events = store.list_events(task_id="t-gw-02")
        assert len(events) == 0

    def test_transition_workflow_success_and_event_recorded(self, clean_store):
        store, db_path, _ = clean_store

        wf = {
            "workflow_id": "wf-gw-02",
            "title": "Gateway Test",
            "status": "running",
        }
        store.save_workflow(wf)

        # Transition running -> paused
        res = kernel.transition_workflow(
            workflow_id="wf-gw-02",
            to_status="paused",
            reason="manual pause by operator",
            source="user-cli",
            metadata={"operator": "alice"},
        )

        assert res["ok"] is True
        assert res["old_status"] == "running"
        assert res["new_status"] == "paused"

        updated = store.get_workflow("wf-gw-02")
        assert updated["status"] == "paused"

        events = store.list_events(workflow_id="wf-gw-02", event_type="workflow_transition")
        assert len(events) == 1
        ev = events[0]
        assert ev["event_type"] == "workflow_transition"
        assert ev["source"] == "user-cli"
        assert ev["payload"]["from_status"] == "running"
        assert ev["payload"]["to_status"] == "paused"
        assert ev["payload"]["reason"] == "manual pause by operator"
        assert ev["payload"]["operator"] == "alice"

    def test_transition_workflow_illegal_rejected(self, clean_store):
        store, db_path, _ = clean_store

        wf = {
            "workflow_id": "wf-gw-03",
            "title": "Gateway Test 3",
            "status": "paused",
        }
        store.save_workflow(wf)

        with pytest.raises(InvalidTransitionError):
            kernel.transition_workflow(
                workflow_id="wf-gw-03",
                to_status="completed",
                reason="cannot complete while paused",
                source="tester",
            )

        updated = store.get_workflow("wf-gw-03")
        assert updated["status"] == "paused"

        events = store.list_events(workflow_id="wf-gw-03", event_type="workflow_transition")
        assert len(events) == 0

    def test_transition_task_nonexistent_raises_value_error(self, clean_store):
        with pytest.raises(ValueError, match="not found"):
            kernel.transition_task(
                task_id="t-non-existent",
                to_status="working",
                reason="none",
            )

    def test_transition_workflow_nonexistent_raises_value_error(self, clean_store):
        with pytest.raises(ValueError, match="not found"):
            kernel.transition_workflow(
                workflow_id="wf-non-existent",
                to_status="running",
                reason="none",
            )

    def test_transition_task_atomicity_rollback_on_failure(self, clean_store, monkeypatch):
        store, db_path, _ = clean_store

        task = {
            "task_id": "t-gw-fail",
            "workflow_id": "wf-gw-01",
            "node": "plan",
            "status": "pending",
        }
        store.save_task(task)

        # Monkeypatch record_event to raise an exception simulating write failure
        original_record_event = state_db.record_event

        def failing_record_event(*args, **kwargs):
            raise sqlite3.OperationalError("Simulated disk error during event recording")

        monkeypatch.setattr(state_db, "record_event", failing_record_event)

        with pytest.raises(sqlite3.OperationalError, match="Simulated disk error"):
            kernel.transition_task(
                task_id="t-gw-fail",
                to_status="dispatched",
                reason="should fail and rollback",
            )

        # Restore
        monkeypatch.setattr(state_db, "record_event", original_record_event)

        # Verify task is STILL pending!
        t = store.get_task("t-gw-fail")
        assert t["status"] == "pending"

        # Verify zero events created
        events = store.list_events(task_id="t-gw-fail")
        assert len(events) == 0

    def test_transition_task_force_admin_override(self, clean_store):
        store, db_path, _ = clean_store

        task = {
            "task_id": "t-gw-force",
            "workflow_id": "wf-gw-01",
            "node": "code",
            "status": "pending",
        }
        store.save_task(task)

        # Directly force to superseded (normally illegal from pending)
        res = kernel.transition_task(
            task_id="t-gw-force",
            to_status="superseded",
            reason="admin cancellation",
            source="admin_override",
            force=True,
        )

        assert res["ok"] is True
        assert res["new_status"] == "superseded"

        t = store.get_task("t-gw-force")
        assert t["status"] == "superseded"

        events = store.list_events(task_id="t-gw-force", event_type="task_transition")
        assert len(events) == 1
        assert events[0]["payload"]["forced"] is True
        assert events[0]["payload"]["to_status"] == "superseded"

    def test_kernel_primitives_emit_canonical_events(self, clean_store):
        store, db_path, tmp_path = clean_store

        # 1. Pause & Resume workflow
        wf = {
            "workflow_id": "wf-kernel-prim",
            "title": "Kernel Primitives Test",
            "status": "running",
            "config": {
                "nodes": [
                    {"id": "req", "label": "Requirements"},
                    {"id": "impl", "label": "Implementation", "depends_on": ["req"]},
                ]
            },
        }
        store.save_workflow(wf)

        res_pause = kernel.pause_workflow("wf-kernel-prim")
        assert res_pause["status"] == "paused"
        events_pause = store.list_events(workflow_id="wf-kernel-prim", event_type="workflow_transition")
        assert len(events_pause) == 1
        assert events_pause[0]["payload"]["to_status"] == "paused"

        res_resume = kernel.resume_workflow("wf-kernel-prim")
        assert res_resume["status"] == "running"
        events_resume = store.list_events(workflow_id="wf-kernel-prim", event_type="workflow_transition")
        assert len(events_resume) == 2
        assert events_resume[1]["payload"]["to_status"] == "running"

        # 2. Rollback workflow
        t_req = {
            "task_id": "t-prim-req",
            "workflow_id": "wf-kernel-prim",
            "node": "req",
            "status": "completed",
        }
        t_impl = {
            "task_id": "t-prim-impl",
            "workflow_id": "wf-kernel-prim",
            "node": "impl",
            "status": "working",
        }
        store.save_task(t_req)
        store.save_task(t_impl)

        # Roll back to "impl"
        res_rb = kernel.rollback_workflow("wf-kernel-prim", target_node_id="impl", reason="test_rollback")
        assert res_rb["ok"] is True
        assert "t-prim-impl" in res_rb["invalidated_tasks"]

        # Verify task transition event emitted for invalidated task
        t_events = store.list_events(task_id="t-prim-impl", event_type="task_transition")
        assert len(t_events) == 1
        assert t_events[0]["payload"]["to_status"] == "superseded"
        assert "rollback to impl" in t_events[0]["payload"]["reason"]

    def test_cli_set_status_emits_workflow_event(self, clean_store):
        store, db_path, tmp_path = clean_store

        task = {
            "task_id": "t-cli-01",
            "workflow_id": "wf-cli-01",
            "node": "plan",
            "stage": "plan",
            "agent": "codex",
            "status": "dispatched",
        }
        store.save_task(task)

        import os
        import subprocess
        bin_path = Path(__file__).resolve().parent.parent / "bin" / "herdr-task"
        env = os.environ.copy()
        env["HERDR_STATE_DB"] = str(db_path)
        env["TASKS_FILE"] = str(tmp_path / "tasks.json")
        env["WORKFLOWS_FILE"] = str(tmp_path / "workflows.json")
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)

        cmd = ["python3", str(bin_path), "set", "t-cli-01", "working"]
        res = subprocess.run(cmd, env=env, text=True, capture_output=True)
        assert res.returncode == 0, res.stderr

        # Verify task status in store
        t = store.get_task("t-cli-01")
        assert t["status"] == "working"

        # Verify WorkflowEvent was produced!
        events = store.list_events(task_id="t-cli-01", event_type="task_transition")
        assert len(events) == 1
        assert events[0]["payload"]["from_status"] == "dispatched"
        assert events[0]["payload"]["to_status"] == "working"
        assert events[0]["source"] == "herdr-task"

    def test_cli_supersede_emits_workflow_event(self, clean_store):
        store, db_path, tmp_path = clean_store

        task = {
            "task_id": "t-cli-02",
            "workflow_id": "wf-cli-02",
            "node": "dev",
            "stage": "dev",
            "agent": "claude",
            "status": "working",
        }
        store.save_task(task)

        import os
        import subprocess
        bin_path = Path(__file__).resolve().parent.parent / "bin" / "herdr-task"
        env = os.environ.copy()
        env["HERDR_STATE_DB"] = str(db_path)
        env["TASKS_FILE"] = str(tmp_path / "tasks.json")
        env["WORKFLOWS_FILE"] = str(tmp_path / "workflows.json")
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)

        cmd = ["python3", str(bin_path), "supersede", "t-cli-02", "--reason", "abandoned by user"]
        res = subprocess.run(cmd, env=env, text=True, capture_output=True)
        assert res.returncode == 0, res.stderr

        t = store.get_task("t-cli-02")
        assert t["status"] == "superseded"

        events = store.list_events(task_id="t-cli-02", event_type="task_transition")
        assert len(events) == 1
        assert events[0]["payload"]["from_status"] == "working"
        assert events[0]["payload"]["to_status"] == "superseded"
        assert events[0]["payload"]["reason"] == "abandoned by user"

    def test_workflow_close_and_reopen_emit_events(self, clean_store):
        store, db_path, tmp_path = clean_store

        wf = {
            "workflow_id": "wf-close-01",
            "title": "Close and Reopen Test",
            "status": "in_progress",
            "coordinator_pane_id": "pane-coord-1",
        }
        store.save_workflow(wf)

        # 1. Close workflow
        res_close = kernel.transition_workflow(
            workflow_id="wf-close-01",
            to_status="completed",
            reason="workflow_completed: delivered",
            source="herdr-task",
            metadata={"outcome": "delivered"},
        )
        assert res_close["ok"] is True
        assert res_close["new_status"] == "completed"

        events = store.list_events(workflow_id="wf-close-01", event_type="workflow_transition")
        assert len(events) == 1
        assert events[0]["payload"]["from_status"] == "in_progress"
        assert events[0]["payload"]["to_status"] == "completed"
        assert events[0]["payload"]["outcome"] == "delivered"

        # 2. Reopen workflow
        res_reopen = kernel.transition_workflow(
            workflow_id="wf-close-01",
            to_status="in_progress",
            reason="workflow_reopened",
            source="herdr-task",
            metadata={"suppress_auto_close": True},
        )
        assert res_reopen["ok"] is True
        assert res_reopen["new_status"] == "in_progress"

        events2 = store.list_events(workflow_id="wf-close-01", event_type="workflow_transition")
        assert len(events2) == 2
        assert events2[1]["payload"]["from_status"] == "completed"
        assert events2[1]["payload"]["to_status"] == "in_progress"
        assert events2[1]["payload"]["suppress_auto_close"] is True

    def test_force_with_unknown_status_rejected(self, clean_store):
        store, db_path, _ = clean_store

        task = {
            "task_id": "t-gw-force-unk",
            "workflow_id": "wf-gw-01",
            "node": "code",
            "status": "pending",
        }
        store.save_task(task)

        # Unknown task status with force=True MUST be rejected
        with pytest.raises(InvalidTransitionError, match="Invalid target task status"):
            kernel.transition_task(
                task_id="t-gw-force-unk",
                to_status="banana",
                reason="illegal unknown status",
                force=True,
            )

        wf = {
            "workflow_id": "wf-gw-force-unk",
            "title": "Force Unknown Test",
            "status": "pending",
        }
        store.save_workflow(wf)

        # Unknown workflow status with force=True MUST be rejected
        with pytest.raises(InvalidTransitionError, match="Invalid target workflow status"):
            kernel.transition_workflow(
                workflow_id="wf-gw-force-unk",
                to_status="banana",
                reason="illegal unknown status",
                force=True,
            )

    def test_force_with_illegal_edge_allowed(self, clean_store):
        store, db_path, _ = clean_store

        # 1. Task: pending -> superseded normally illegal, but allowed with force=True
        task = {
            "task_id": "t-gw-force-edge",
            "workflow_id": "wf-gw-01",
            "node": "code",
            "status": "pending",
        }
        store.save_task(task)

        res_task = kernel.transition_task(
            task_id="t-gw-force-edge",
            to_status="superseded",
            reason="admin override edge",
            force=True,
        )
        assert res_task["ok"] is True
        assert res_task["new_status"] == "superseded"
        t = store.get_task("t-gw-force-edge")
        assert t["status"] == "superseded"
        t_events = store.list_events(task_id="t-gw-force-edge")
        assert t_events[0]["payload"]["forced"] is True

        # 2. Workflow: paused -> completed normally illegal, but allowed with force=True
        wf = {
            "workflow_id": "wf-gw-force-edge",
            "title": "Force Edge Test",
            "status": "paused",
        }
        store.save_workflow(wf)

        res_wf = kernel.transition_workflow(
            workflow_id="wf-gw-force-edge",
            to_status="completed",
            reason="admin override edge",
            force=True,
        )
        assert res_wf["ok"] is True
        assert res_wf["new_status"] == "completed"
        w = store.get_workflow("wf-gw-force-edge")
        assert w["status"] == "completed"
        w_events = store.list_events(workflow_id="wf-gw-force-edge")
        assert w_events[0]["payload"]["forced"] is True

    def test_metadata_cannot_modify_protected_fields(self, clean_store):
        store, db_path, _ = clean_store

        task = {
            "task_id": "t-gw-prot",
            "workflow_id": "wf-gw-01",
            "node": "code",
            "status": "pending",
        }
        store.save_task(task)

        for protected_field in ["task_id", "workflow_id", "run_id", "status", "created_at", "updated_at"]:
            with pytest.raises(ValueError, match="Cannot overwrite protected task fields via metadata"):
                kernel.transition_task(
                    task_id="t-gw-prot",
                    to_status="dispatched",
                    reason="metadata exploit attempt",
                    metadata={protected_field: "malicious_override"},
                )

        wf = {
            "workflow_id": "wf-gw-prot",
            "title": "Protected Fields Test",
            "status": "pending",
        }
        store.save_workflow(wf)

        for protected_field in ["workflow_id", "status", "created_at", "updated_at"]:
            with pytest.raises(ValueError, match="Cannot overwrite protected workflow fields via metadata"):
                kernel.transition_workflow(
                    workflow_id="wf-gw-prot",
                    to_status="running",
                    reason="metadata exploit attempt",
                    metadata={protected_field: "malicious_override"},
                )

    def test_rollback_skips_already_superseded_task(self, clean_store):
        store, db_path, _ = clean_store

        wf = {
            "workflow_id": "wf-rb-skip",
            "title": "Rollback Skip Test",
            "status": "running",
            "config": {
                "nodes": [
                    {"id": "step1", "label": "Step 1"},
                    {"id": "step2", "label": "Step 2", "depends_on": ["step1"]},
                ]
            },
        }
        store.save_workflow(wf)

        t1 = {
            "task_id": "t-rb-s1",
            "workflow_id": "wf-rb-skip",
            "node": "step2",
            "status": "working",
        }
        store.save_task(t1)

        # First rollback: supersedes task
        res1 = kernel.rollback_workflow("wf-rb-skip", target_node_id="step2", reason="rb 1")
        assert "t-rb-s1" in res1["invalidated_tasks"]
        events1 = store.list_events(task_id="t-rb-s1")
        assert len(events1) == 1

        # Second rollback: task is already superseded, must be SKIPPED!
        res2 = kernel.rollback_workflow("wf-rb-skip", target_node_id="step2", reason="rb 2")
        assert "t-rb-s1" not in res2["invalidated_tasks"]
        events2 = store.list_events(task_id="t-rb-s1")
        assert len(events2) == 1  # No duplicate event!

    def test_caller_fail_closed_when_gateway_fails(self, clean_store, monkeypatch):
        store, db_path, tmp_path = clean_store

        task = {
            "task_id": "t-fc-01",
            "workflow_id": "wf-fc-01",
            "node": "step1",
            "stage": "step1",
            "pane_id": "pane-101",
            "status": "dispatched",
        }
        store.save_task(task)

        # 1. Simulate disk / SQLite failure during record_event
        original_record_event = state_db.record_event

        def failing_record_event(*args, **kwargs):
            raise sqlite3.OperationalError("Simulated disk I/O error during event append")

        monkeypatch.setattr(state_db, "record_event", failing_record_event)

        # A. CLI set_status must exit non-zero (code 2) and NOT mutate status
        import importlib.machinery
        import importlib.util

        def _load_src_module(name, path):
            loader = importlib.machinery.SourceFileLoader(name, str(path))
            spec = importlib.util.spec_from_loader(name, loader)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

        ht_path = Path(__file__).resolve().parent.parent / "bin" / "herdr-task"
        ht_mod = _load_src_module("herdr_task_fc_test", ht_path)
        ht_mod.TASKS_FILE = str(tmp_path / "tasks.json")
        ht_mod.WORKFLOWS_FILE = str(tmp_path / "workflows.json")

        with pytest.raises(SystemExit) as exc_info:
            ht_mod.set_status("t-fc-01", "working")
        assert exc_info.value.code == 2, f"Expected exit code 2 on failure, got {exc_info.value.code}"
        # Task in DB MUST remain dispatched (NOT changed to working!)
        t = store.get_task("t-fc-01")
        assert t["status"] == "dispatched"

        # B. Steering halt_task must return ok=False and NOT mutate status
        from herdr import steering
        # Mock adapter to succeed physically so we test Gateway failure
        from unittest.mock import patch, MagicMock
        mock_adapter = MagicMock()
        mock_adapter.protocol_level = "prototype"
        mock_adapter.name = "mock"
        mock_adapter.interrupt.return_value = {"ok": True}
        with patch.object(steering, "get_agent_adapter", return_value=mock_adapter):
            halt_res = steering.halt_task("t-fc-01", reason="test abort")
            assert halt_res["ok"] is False
            assert "transition_task_failed" in halt_res["error"]
            t_after_halt = store.get_task("t-fc-01")
            assert t_after_halt["status"] == "dispatched"

        # C. Sentinel update_statuses must NOT mutate status
        sentinel_path = Path(__file__).resolve().parent.parent / "services" / "herdr-sentinel.py"
        sentinel_mod = _load_src_module("herdr_sentinel_test", sentinel_path)
        sentinel_mod.TASKS_FILE = str(tmp_path / "tasks.json")

        sentinel_changed = sentinel_mod.update_statuses({"t-fc-01": ("failed", "sentinel timeout")})
        assert sentinel_changed is False
        t_after_sentinel = store.get_task("t-fc-01")
        assert t_after_sentinel["status"] == "dispatched"

    def test_close_workflow_fails_closed_on_pending_or_paused_without_force(self, clean_store):
        store, db_path, tmp_path = clean_store
        import importlib.machinery
        import importlib.util
        from herdr.transitions import InvalidTransitionError

        def _load_src_module(name, path):
            loader = importlib.machinery.SourceFileLoader(name, str(path))
            spec = importlib.util.spec_from_loader(name, loader)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

        ht_path = Path(__file__).resolve().parent.parent / "bin" / "herdr-task"
        ht_mod = _load_src_module("herdr_task_close_test", ht_path)
        ht_mod.TASKS_FILE = str(tmp_path / "tasks.json")
        ht_mod.WORKFLOWS_FILE = str(tmp_path / "workflows.json")

        wf = {
            "workflow_id": "wf-close-gate",
            "project_id": "p-1",
            "status": "pending",
        }
        store.save_workflow(wf)

        # 1. Calling close_workflow without force on pending workflow MUST fail closed
        with pytest.raises(InvalidTransitionError):
            ht_mod.close_workflow("wf-close-gate", force=False)

        assert store.get_workflow("wf-close-gate")["status"] == "pending"

        # 2. Calling with force=True succeeds and marks completed
        report = ht_mod.close_workflow("wf-close-gate", force=True)
        assert report["workflow_id"] == "wf-close-gate"
        assert store.get_workflow("wf-close-gate")["status"] == "completed"

        events = store.list_events(workflow_id="wf-close-gate", event_type="workflow_transition")
        assert len(events) == 1
        assert events[0]["payload"]["forced"] is True
        assert events[0]["payload"]["to_status"] == "completed"

    def test_load_tasks_never_resurrects_deleted_tasks_from_json(self, clean_store):
        store, db_path, tmp_path = clean_store
        import importlib.machinery
        import importlib.util

        def _load_src_module(name, path):
            loader = importlib.machinery.SourceFileLoader(name, str(path))
            spec = importlib.util.spec_from_loader(name, loader)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

        ht_path = Path(__file__).resolve().parent.parent / "bin" / "herdr-task"
        ht_mod = _load_src_module("herdr_task_resurrect_test", ht_path)
        ht_mod.TASKS_FILE = str(tmp_path / "tasks.json")
        ht_mod.WORKFLOWS_FILE = str(tmp_path / "workflows.json")

        task = {
            "task_id": "t-ghost",
            "workflow_id": "wf-ghost",
            "status": "completed",
        }
        store.save_task(task)

        # Sync to json
        ht_mod.save_tasks({"tasks": [task]})
        assert (tmp_path / "tasks.json").exists()

        # Delete from SQLite directly
        store.delete_task("t-ghost")
        assert store.get_task("t-ghost") is None

        # load_tasks() MUST NOT resurrect t-ghost back into SQLite!
        loaded = ht_mod.load_tasks()
        assert loaded["tasks"] == []
        assert store.get_task("t-ghost") is None

    def test_projection_sync_locked_reexport(self, clean_store):
        store, db_path, tmp_path = clean_store
        from herdr.state_store import sync_tasks_projection, sync_workflows_projection

        t_file = tmp_path / "tasks.json"
        w_file = tmp_path / "workflows.json"

        # Initially write items
        store.save_task({"task_id": "t-lock-1", "workflow_id": "wf-lock", "status": "dispatched"})
        store.save_workflow({"workflow_id": "wf-lock", "status": "running"})

        sync_tasks_projection(store=store, tasks_file=t_file)
        sync_workflows_projection(store=store, wf_file=w_file)

        assert t_file.exists()
        assert w_file.exists()
        lock_t = tmp_path / ".tasks.json.lock"
        lock_w = tmp_path / ".workflows.json.lock"
        assert lock_t.exists()
        assert lock_w.exists()

        with open(t_file, "r", encoding="utf-8") as f:
            t_data = json.load(f)
        assert len(t_data["tasks"]) == 1
        assert t_data["tasks"][0]["task_id"] == "t-lock-1"

    def test_sentinel_never_resurrects_deleted_tasks(self, clean_store):
        store, db_path, tmp_path = clean_store
        import importlib.machinery
        import importlib.util

        def _load_src_module(name, path):
            loader = importlib.machinery.SourceFileLoader(name, str(path))
            spec = importlib.util.spec_from_loader(name, loader)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

        sentinel_path = Path(__file__).resolve().parent.parent / "services" / "herdr-sentinel.py"
        sentinel_mod = _load_src_module("herdr_sentinel_resurrect_test", sentinel_path)
        sentinel_mod.TASKS_FILE = str(tmp_path / "tasks.json")

        task = {
            "task_id": "t-ghost-sentinel",
            "workflow_id": "wf-ghost",
            "status": "working",
        }
        store.save_task(task)
        assert store.get_task("t-ghost-sentinel") is not None

        # Delete from SQLite directly
        store.delete_task("t-ghost-sentinel")
        assert store.get_task("t-ghost-sentinel") is None

        # Call update_statuses with candidate change
        changed = sentinel_mod.update_statuses({"t-ghost-sentinel": ("agent_done", "completion_sentinel")})
        assert changed is False

        # Verify task is STILL None in authoritative StateStore (never resurrected!)
        assert store.get_task("t-ghost-sentinel") is None

    def test_close_workflow_preflight_gate_blocks_before_side_effects(self, clean_store, monkeypatch):
        store, db_path, tmp_path = clean_store
        import importlib.machinery
        import importlib.util
        from herdr.transitions import InvalidTransitionError
        from unittest.mock import MagicMock

        def _load_src_module(name, path):
            loader = importlib.machinery.SourceFileLoader(name, str(path))
            spec = importlib.util.spec_from_loader(name, loader)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

        ht_path = Path(__file__).resolve().parent.parent / "bin" / "herdr-task"
        ht_mod = _load_src_module("herdr_task_preflight_side_effects_test", ht_path)
        ht_mod.TASKS_FILE = str(tmp_path / "tasks.json")
        ht_mod.WORKFLOWS_FILE = str(tmp_path / "workflows.json")

        # 1. Setup workflow in 'paused' state
        wf = {
            "workflow_id": "wf-paused-gate",
            "project_id": "p-gate",
            "status": "paused",
        }
        store.save_workflow(wf)

        # 2. Setup settled task
        task = {
            "task_id": "t-settled-gate",
            "workflow_id": "wf-paused-gate",
            "status": "cleaned",
        }
        store.save_task(task)

        # 3. Mock _finalize_one and _herdr tab close to record calls
        mock_finalize = MagicMock()
        monkeypatch.setattr(ht_mod, "_finalize_one", mock_finalize)
        mock_herdr = MagicMock(return_value=MagicMock(returncode=0))
        monkeypatch.setattr(ht_mod, "_herdr", mock_herdr)

        # 4. Attempt to close workflow without force -> MUST fail closed on preflight gate
        with pytest.raises(InvalidTransitionError):
            ht_mod.close_workflow("wf-paused-gate", force=False)

        # 5. Assert: zero physical side effects were executed!
        assert mock_finalize.call_count == 0, "_finalize_one MUST NOT be called when preflight gate fails"
        assert mock_herdr.call_count == 0, "_herdr tab close MUST NOT be called when preflight gate fails"

        # 6. Assert: task and workflow statuses in SQLite remain completely untouched
        assert store.get_workflow("wf-paused-gate")["status"] == "paused"
        assert store.get_task("t-settled-gate")["status"] == "cleaned"

    def test_metadata_update_never_regresses_concurrent_task_status(self, clean_store):
        """Regression test: Metadata update must never clobber concurrent status transition."""
        store, db_path, tmp_path = clean_store
        from herdr import kernel

        # 1. Setup task in 'working' status
        task = {
            "task_id": "t-concurrent-meta",
            "workflow_id": "wf-meta-test",
            "node": "node_impl",
            "status": "working",
        }
        store.save_task(task)

        # 2. Simulate process A taking a stale snapshot
        stale_snapshot = dict(store.get_task("t-concurrent-meta"))
        assert stale_snapshot["status"] == "working"

        # 3. Process B transitions task via State Transition Gateway to 'agent_done'
        kernel.transition_task(
            task_id="t-concurrent-meta",
            to_status="agent_done",
            reason="agent finished",
            source="agent_runtime",
            store=store,
        )
        assert store.get_task("t-concurrent-meta")["status"] == "agent_done"

        # 4. Attempting to tamper status via metadata update must fail closed
        with pytest.raises(ValueError, match="Cannot update protected task fields"):
            kernel.update_task_metadata(
                task_id="t-concurrent-meta",
                updates={"status": "working"},
                store=store,
            )

        # 5. Process A updates task metadata (e.g. stage_verdict from evaluator / gate)
        kernel.update_task_metadata(
            task_id="t-concurrent-meta",
            updates={
                "stage_verdict": "pass",
                "stage_verdict_note": "evaluator green",
            },
            store=store,
        )

        # 6. Assert: status REMAINS 'agent_done', metadata was cleanly applied!
        fresh_task = store.get_task("t-concurrent-meta")
        assert fresh_task["status"] == "agent_done"
        assert fresh_task["stage_verdict"] == "pass"
        assert fresh_task["stage_verdict_note"] == "evaluator green"

        # 7. Assert events only contain the legitimate transition
        events = store.list_events(workflow_id="wf-meta-test", event_type="task_transition")
        assert len(events) == 1
        assert events[0]["payload"]["from_status"] == "working"
        assert events[0]["payload"]["to_status"] == "agent_done"

    def test_metadata_update_never_regresses_concurrent_workflow_status(self, clean_store):
        """Regression test: Workflow metadata update must never clobber concurrent status transition."""
        store, db_path, tmp_path = clean_store
        from herdr import kernel

        # 1. Setup workflow in 'running' status
        wf = {
            "workflow_id": "wf-concurrent-meta",
            "title": "Concurrent Workflow Test",
            "status": "running",
        }
        store.save_workflow(wf)

        # 2. Simulate process A taking a stale snapshot
        stale_snapshot = dict(store.get_workflow("wf-concurrent-meta"))
        assert stale_snapshot["status"] == "running"

        # 3. Process B transitions workflow to 'completed'
        kernel.transition_workflow(
            workflow_id="wf-concurrent-meta",
            to_status="completed",
            reason="all nodes completed",
            source="controller",
            store=store,
        )
        assert store.get_workflow("wf-concurrent-meta")["status"] == "completed"

        # 4. Attempting to tamper status via metadata update must fail closed
        with pytest.raises(ValueError, match="Cannot update protected workflow fields"):
            kernel.update_workflow_metadata(
                workflow_id="wf-concurrent-meta",
                updates={"status": "running"},
                store=store,
            )

        # 5. Process A updates workflow metadata (e.g. pause/unpause a node)
        kernel.update_workflow_metadata(
            workflow_id="wf-concurrent-meta",
            updates={"paused_nodes": ["node_x"]},
            store=store,
        )

        # 6. Assert: status REMAINS 'completed', paused_nodes was cleanly applied!
        fresh_wf = store.get_workflow("wf-concurrent-meta")
        assert fresh_wf["status"] == "completed"
        assert fresh_wf.get("paused_nodes") == ["node_x"]

        # 7. Assert events only contain the legitimate transition
        events = store.list_events(workflow_id="wf-concurrent-meta", event_type="workflow_transition")
        assert len(events) == 1
        assert events[0]["payload"]["from_status"] == "running"
        assert events[0]["payload"]["to_status"] == "completed"

    def test_close_workflow_acquires_closing_state_and_blocks_concurrent_pause(self, clean_store, monkeypatch):
        """P1 verification: close_workflow transitions running -> closing before teardown,
        blocking concurrent pause, and finishes with closing -> completed."""
        store, db_path, tmp_path = clean_store
        import importlib.machinery
        import importlib.util
        from unittest.mock import MagicMock
        from herdr import kernel
        from herdr.transitions import InvalidTransitionError

        def _load_src_module(name, path):
            loader = importlib.machinery.SourceFileLoader(name, str(path))
            spec = importlib.util.spec_from_loader(name, loader)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

        ht_path = Path(__file__).resolve().parent.parent / "bin" / "herdr-task"
        ht_mod = _load_src_module("herdr_task_closing_race_test", ht_path)
        ht_mod.TASKS_FILE = str(tmp_path / "tasks.json")
        ht_mod.WORKFLOWS_FILE = str(tmp_path / "workflows.json")

        wf = {
            "workflow_id": "wf-closing-race",
            "project_id": "p-closing",
            "status": "running",
        }
        store.save_workflow(wf)

        task = {
            "task_id": "t-closing-settled",
            "workflow_id": "wf-closing-race",
            "status": "cleaned",
        }
        store.save_task(task)

        # Hook into _finalize_one to simulate a concurrent actor trying to pause during teardown
        pause_attempted_result = {}

        def _mock_finalize_interceptor(t, purge_clones=False, dry_run=False):
            # Mid-teardown: workflow MUST already be in 'closing' state
            current_status = store.get_workflow("wf-closing-race")["status"]
            assert current_status == "closing", f"Expected workflow to be 'closing' during teardown, got {current_status}"

            # Concurrent actor tries to pause the workflow
            try:
                kernel.pause_workflow("wf-closing-race", store=store)
                pause_attempted_result["success"] = True
            except InvalidTransitionError as exc:
                pause_attempted_result["error"] = exc

            return {
                "task_id": t["task_id"],
                "status": "completed",
                "action": "purged",
            }

        monkeypatch.setattr(ht_mod, "_finalize_one", _mock_finalize_interceptor)
        monkeypatch.setattr(ht_mod, "_herdr", MagicMock(return_value=MagicMock(returncode=0)))

        report = ht_mod.close_workflow("wf-closing-race", force=False)
        assert report["workflow_id"] == "wf-closing-race"

        # Assert concurrent pause was strictly blocked
        assert "error" in pause_attempted_result
        assert "Illegal workflow transition: 'closing' -> 'paused'" in str(pause_attempted_result["error"])

        # Final state must be completed
        final_wf = store.get_workflow("wf-closing-race")
        assert final_wf["status"] == "completed"

        # Check sequence of workflow_transition events: running -> closing -> completed
        events = store.list_events(workflow_id="wf-closing-race", event_type="workflow_transition")
        assert len(events) == 2
        assert events[0]["payload"]["from_status"] == "running"
        assert events[0]["payload"]["to_status"] == "closing"
        assert events[1]["payload"]["from_status"] == "closing"
        assert events[1]["payload"]["to_status"] == "completed"

    def test_projection_sync_respects_explicit_env_var_over_db_sibling(self, tmp_path, monkeypatch):
        """P2 verification: Projection sync precedence is:
        explicit argument -> os.environ["TASKS_FILE"] -> store.db_path.parent -> default CONTROLLER_DIR."""
        from herdr.state_store import (
            get_state_store,
            resolve_tasks_projection_file,
            resolve_workflows_projection_file,
            sync_tasks_projection,
        )
        from herdr import kernel

        db_dir = tmp_path / "runtime_db"
        db_dir.mkdir(parents=True, exist_ok=True)
        db_path = db_dir / "state.db"
        store = get_state_store(db_path=db_path)

        custom_dir = tmp_path / "custom_projection"
        custom_dir.mkdir(parents=True, exist_ok=True)
        env_tasks_file = custom_dir / "tasks.json"
        env_wf_file = custom_dir / "workflows.json"

        # 1. When env vars are set, resolve_* MUST pick env vars over store.db_path.parent
        monkeypatch.setenv("TASKS_FILE", str(env_tasks_file))
        monkeypatch.setenv("WORKFLOWS_FILE", str(env_wf_file))

        assert resolve_tasks_projection_file(store=store) == env_tasks_file
        assert resolve_workflows_projection_file(store=store) == env_wf_file

        # 2. Kernel transition_task and transition_workflow sync to env_tasks_file, NOT db_dir / "tasks.json"
        wf = {"workflow_id": "wf-proj-test", "status": "pending"}
        store.save_workflow(wf)
        task = {"task_id": "t-proj-test", "workflow_id": "wf-proj-test", "status": "dispatched"}
        store.save_task(task)

        kernel.transition_task(
            task_id="t-proj-test",
            to_status="working",
            reason="started",
            source="worker",
            store=store,
        )

        # Assert custom_projection has tasks.json and was updated
        assert env_tasks_file.exists(), "tasks.json should have been written to TASKS_FILE path"
        assert not (db_dir / "tasks.json").exists(), "tasks.json should NOT be written to db_path.parent when TASKS_FILE is set"
        with open(env_tasks_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert any(t["task_id"] == "t-proj-test" and t["status"] == "working" for t in data["tasks"])

        # 3. Explicit argument overrides env var
        arg_file = tmp_path / "explicit" / "override_tasks.json"
        assert resolve_tasks_projection_file(store=store, tasks_file=arg_file) == arg_file
        sync_tasks_projection(store=store, tasks_file=arg_file)
        assert arg_file.exists()

        # 4. When env var is cleared, store.db_path.parent is preferred over default
        monkeypatch.delenv("TASKS_FILE", raising=False)
        monkeypatch.delenv("WORKFLOWS_FILE", raising=False)
        assert resolve_tasks_projection_file(store=store) == db_dir / "tasks.json"
        assert resolve_workflows_projection_file(store=store) == db_dir / "workflows.json"

    def test_metadata_cannot_spoof_canonical_event_fields(self, clean_store):
        """P2 verification: User metadata cannot spoof reserved event or history fields,
        both via validation rejection and structural write-order guarantee."""
        store, db_path, tmp_path = clean_store
        from herdr import kernel

        wf = {
            "workflow_id": "wf-spoof-test",
            "project_id": "p-spoof",
            "status": "pending",
        }
        store.save_workflow(wf)

        task = {
            "task_id": "t-spoof-test",
            "workflow_id": "wf-spoof-test",
            "status": "dispatched",
        }
        store.save_task(task)

        # 1. Attempting to spoof reserved fields in transition_task metadata must fail closed
        for reserved_field in ["from", "to", "from_status", "to_status", "reason", "source", "timestamp", "forced"]:
            with pytest.raises(ValueError, match="Cannot overwrite reserved event fields via metadata"):
                kernel.transition_task(
                    task_id="t-spoof-test",
                    to_status="working",
                    reason="legit",
                    metadata={reserved_field: "spoofed"},
                    store=store,
                )

        # 2. Attempting to spoof reserved fields in transition_workflow metadata must fail closed
        for reserved_field in ["from", "to", "from_status", "to_status", "reason", "source", "timestamp", "forced"]:
            with pytest.raises(ValueError, match="Cannot overwrite reserved event fields via metadata"):
                kernel.transition_workflow(
                    workflow_id="wf-spoof-test",
                    to_status="running",
                    reason="legit",
                    metadata={reserved_field: "spoofed"},
                    store=store,
                )

        # 3. Legitimate metadata passes cleanly and canonical event fields are preserved structurally
        res = kernel.transition_task(
            task_id="t-spoof-test",
            to_status="working",
            reason="worker picked up",
            source="worker-1",
            metadata={"iteration": 1, "worker_ip": "10.0.0.1"},
            store=store,
        )
        assert res["ok"] is True

        events = store.list_events(task_id="t-spoof-test", event_type="task_transition")
        assert len(events) == 1
        ev = events[0]
        assert ev["payload"]["from_status"] == "dispatched"
        assert ev["payload"]["to_status"] == "working"
        assert ev["payload"]["reason"] == "worker picked up"
        assert ev["payload"]["forced"] is False
        assert ev["payload"]["iteration"] == 1
        assert ev["payload"]["worker_ip"] == "10.0.0.1"

        saved_task = store.get_task("t-spoof-test")
        assert saved_task["status"] == "working"
        assert saved_task["iteration"] == 1
        assert saved_task["worker_ip"] == "10.0.0.1"
        history = saved_task["status_history"]
        assert len(history) == 1
        assert history[0]["from"] == "dispatched"
        assert history[0]["to"] == "working"
        assert history[0]["source"] == "worker-1"
        assert history[0]["iteration"] == 1

    def test_sentinel_directly_bootstraps_and_imports_herdr_without_pythonpath(self, tmp_path):
        """P1 verification: services/herdr-sentinel.py inserts HERDR_ROOT into sys.path,
        so it can be invoked directly from any cwd without PYTHONPATH."""
        import subprocess
        sentinel_script = Path(__file__).resolve().parent.parent / "services" / "herdr-sentinel.py"
        code = (
            f"import runpy\n"
            f"mod = runpy.run_path('{sentinel_script}')\n"
            f"store = mod['_get_store']()\n"
            f"assert store is not None\n"
        )
        res = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(tmp_path),
            env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, f"Sentinel bootstrap failed: {res.stderr}"

    def test_set_verdict_note_on_same_status_updates_metadata_without_fake_transition(self, clean_store):
        """P2 verification: Setting verdict/note on a task with the same status (e.g. completed)
        updates task metadata without emitting a spurious lifecycle transition event or history entry."""
        store, db_path, tmp_path = clean_store
        import subprocess

        bin_path = Path(__file__).resolve().parent.parent / "bin" / "herdr-task"
        env = os.environ.copy()
        env["HERDR_STATE_DB"] = str(db_path)
        env["TASKS_FILE"] = str(tmp_path / "tasks.json")
        env["WORKFLOWS_FILE"] = str(tmp_path / "workflows.json")
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)

        task = {
            "task_id": "t-completed-meta",
            "workflow_id": "wf-meta",
            "status": "completed",
            "stage_verdict": "pending",
        }
        store.save_task(task)

        # Baseline: zero transition events
        assert len(store.list_events(task_id="t-completed-meta", event_type="task_transition")) == 0

        # Invoke herdr-task set with same status and new verdict/note
        cmd = [
            "python3", str(bin_path), "set", "t-completed-meta", "completed",
            "--verdict", "pass", "--note", "verified by human",
        ]
        res = subprocess.run(cmd, env=env, text=True, capture_output=True)
        assert res.returncode == 0, res.stderr
        assert "already completed; verdict/note updated" in res.stdout

        # Metadata MUST be updated
        updated = store.get_task("t-completed-meta")
        assert updated["status"] == "completed"
        assert updated["stage_verdict"] == "pass"
        assert updated["stage_verdict_note"] == "verified by human"

        # MUST NOT create a completed -> completed transition event or duplicate history entry
        events = store.list_events(task_id="t-completed-meta", event_type="task_transition")
        assert len(events) == 0, "No lifecycle transition event should be emitted when updating verdict/note on same status"
        history = updated.get("status_history") or []
        assert len(history) == 0, "No status_history entry should be appended on metadata-only update"

    def test_save_task_auto_creates_parent_workflow_as_pending_not_unknown(self, clean_store):
        """P2 verification: save_task auto-creates parent workflow with status 'pending'
        instead of illegal 'unknown', allowing normal lifecycle progression through Gateway."""
        store, db_path, tmp_path = clean_store
        from herdr import kernel

        # 1. Save task under non-existent workflow
        task = {
            "task_id": "t-new-task",
            "workflow_id": "wf-auto-created",
            "status": "dispatched",
        }
        store.save_task(task)

        # 2. Parent workflow MUST exist with status 'pending', NOT 'unknown'
        wf = store.get_workflow("wf-auto-created")
        assert wf is not None
        assert wf["status"] == "pending", f"Expected auto-created workflow status to be 'pending', got {wf['status']}"

        # 3. Legally transition workflow from 'pending' to 'running' via Gateway without errors
        res = kernel.transition_workflow(
            workflow_id="wf-auto-created",
            to_status="running",
            reason="workflow launched",
            source="controller",
            store=store,
        )
        assert res["ok"] is True
        assert store.get_workflow("wf-auto-created")["status"] == "running"


