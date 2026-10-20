"""Unit and regression tests for ADR-0007 / OEP-0002 D3 Workflow Initial State Convergence.

Tests verify:
1. Pure transition rules: INITIAL_WORKFLOW_STATUS is 'running', UNPROVISIONED_WORKFLOW_STATUS is 'pending'.
2. Dual-initial state semantics: pending -> running is a legal transition edge.
3. register_workflow provisions workflows with status 'running' and emits authoritative 'workflow_created' events.
4. Placeholder workflows (seeded with 'pending') transition safely to 'running' upon register_workflow.
5. Active workflow queries (active_workflows_for_project, non_terminal_workflow_ids) recognize 'running' workflows.
"""

import json
from pathlib import Path
import pytest

from herdr import transitions
from herdr.transitions import (
    INITIAL_WORKFLOW_STATUS,
    UNPROVISIONED_WORKFLOW_STATUS,
    WORKFLOW_TRANSITIONS,
    InvalidTransitionError,
    validate_workflow_transition,
)
from herdr import projects
from herdr.state_store import get_state_store, reset_state_store


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    db_file = tmp_path / "state.db"
    wf_file = tmp_path / "workflows.json"
    tasks_file = tmp_path / "tasks.json"

    monkeypatch.setenv("HERDR_STATE_DB", str(db_file))
    monkeypatch.setenv("WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setenv("TASKS_FILE", str(tasks_file))

    reset_state_store()
    yield {"db_file": db_file, "wf_file": wf_file, "tmp": tmp_path}
    reset_state_store()


class TestD3WorkflowTransitionsPure:
    def test_canonical_initial_status_constants(self):
        assert INITIAL_WORKFLOW_STATUS == "running"
        assert UNPROVISIONED_WORKFLOW_STATUS == "pending"

    def test_workflow_transitions_matrix_contains_dual_initial_paths(self):
        # 'pending' has legal edges to active statuses
        assert "running" in WORKFLOW_TRANSITIONS["pending"]
        assert "in_progress" in WORKFLOW_TRANSITIONS["pending"]
        assert "failed" in WORKFLOW_TRANSITIONS["pending"]

        # 'running' is active and can progress or complete
        assert "in_progress" in WORKFLOW_TRANSITIONS["running"]
        assert "closing" in WORKFLOW_TRANSITIONS["running"]
        assert "completed" in WORKFLOW_TRANSITIONS["running"]
        assert "paused" in WORKFLOW_TRANSITIONS["running"]
        assert "failed" in WORKFLOW_TRANSITIONS["running"]

    def test_validate_workflow_transition_rules(self):
        # Legal transitions
        assert validate_workflow_transition("pending", "running") is True
        assert validate_workflow_transition("running", "in_progress") is True
        assert validate_workflow_transition("running", "closing") is True
        assert validate_workflow_transition("running", "running") is True  # idempotent

        # Illegal transitions
        with pytest.raises(InvalidTransitionError, match="Illegal workflow transition"):
            validate_workflow_transition("pending", "completed")

        with pytest.raises(InvalidTransitionError, match="Invalid target workflow status"):
            validate_workflow_transition("running", "non_existent_status")


class TestD3WorkflowRegistrationConvergence:
    def _create_dummy_project(self, tmp_path, project_id="proj-test"):
        wf_config = tmp_path / "test_wf.json"
        wf_config.write_text(json.dumps({
            "name": "test-workflow",
            "nodes": [
                {"id": "plan", "role": "planner"},
                {"id": "impl", "role": "worker"},
            ]
        }))
        return {
            "project_id": project_id,
            "project_name": "Test Project",
            "project_root": str(tmp_path),
            "base_branch": "main",
            "workspace_id": "ws-test",
            "coordinator_pane_id": "pane-coord",
            "workflow_file": str(wf_config),
        }

    def test_register_workflow_sets_running_and_emits_event(self, store_env):
        proj = self._create_dummy_project(store_env["tmp"], "proj-d3-new")
        wid = "wf-d3-new-01"

        projects.register_workflow(
            wid,
            proj,
            requirement="Implement feature XYZ",
            title="D3 Feature Run",
        )

        store = get_state_store()
        record = store.get_workflow(wid)
        assert record is not None
        assert record["status"] == "running"
        assert record["title"] == "D3 Feature Run"
        assert record["project_id"] == "proj-d3-new"

        # Verify authoritative workflow_created event
        events = store.list_events(workflow_id=wid)
        created_events = [e for e in events if e.get("event_type") == "workflow_created"]
        assert len(created_events) >= 1

        ev = created_events[0]
        payload = ev.get("payload") or {}
        assert payload.get("status") == "running"
        assert payload.get("from_status") is None
        assert payload.get("workflow_id") == wid
        assert payload.get("project_id") == "proj-d3-new"
        assert ev.get("source") == "register_workflow"

    def test_register_workflow_provisions_pending_placeholder(self, store_env):
        proj = self._create_dummy_project(store_env["tmp"], "proj-d3-placeholder")
        wid = "wf-d3-placeholder-01"
        store = get_state_store()

        # Simulate a task arriving before workflow registration, seeding a pending placeholder
        store.save_workflow({
            "workflow_id": wid,
            "title": wid,
            "status": "pending",
        })

        initial_record = store.get_workflow(wid)
        assert initial_record["status"] == "pending"

        # Formally register the workflow
        projects.register_workflow(
            wid,
            proj,
            requirement="Provisioning placeholder workflow",
            title="Provisioned Workflow",
        )

        provisioned_record = store.get_workflow(wid)
        assert provisioned_record["status"] == "running"
        assert provisioned_record["title"] == "Provisioned Workflow"

        # Verify workflow_created event recorded from_status == 'pending'
        events = store.list_events(workflow_id=wid)
        created_events = [e for e in events if e.get("event_type") == "workflow_created"]
        assert len(created_events) >= 1

        ev = created_events[0]
        payload = ev.get("payload") or {}
        assert payload.get("status") == "running"
        assert payload.get("from_status") == "pending"

    def test_register_workflow_atomic_rollback_on_event_failure(self, store_env, monkeypatch):
        proj = self._create_dummy_project(store_env["tmp"], "proj-d3-atomic")
        wid = "wf-d3-atomic-fail"
        store = get_state_store()

        from herdr import state_db
        real_record_event = state_db.record_event

        def mock_failing_record_event(event, db_path=None, conn=None):
            if event.get("event_type") == "workflow_created":
                raise IOError("Simulated SQLite disk full during workflow_created audit event")
            return real_record_event(event, db_path=db_path, conn=conn)

        monkeypatch.setattr(state_db, "record_event", mock_failing_record_event)

        with pytest.raises(IOError, match="Simulated SQLite disk full"):
            projects.register_workflow(
                wid,
                proj,
                requirement="Should fail atomically",
                title="Atomic Failure Test",
            )

        # Invariant: Workflow MUST NOT be left in storage if creation audit record fails!
        assert store.get_workflow(wid) is None

    def test_register_workflow_idempotent_deduplicates_creation_event(self, store_env):
        proj = self._create_dummy_project(store_env["tmp"], "proj-d3-idempotent")
        wid = "wf-d3-retry-wid"
        store = get_state_store()

        # First registration
        projects.register_workflow(
            wid,
            proj,
            requirement="Initial registration",
            title="Initial Title",
        )

        record1 = store.get_workflow(wid)
        assert record1["status"] == "running"
        events1 = [e for e in store.list_events(workflow_id=wid) if e.get("event_type") == "workflow_created"]
        assert len(events1) == 1

        # Second registration (retry or update)
        projects.register_workflow(
            wid,
            proj,
            requirement="Retried registration",
            title="Updated Title",
        )

        record2 = store.get_workflow(wid)
        assert record2["status"] == "running"
        assert record2["title"] == "Updated Title"

        # P2 guarantee: exactly ONE workflow_created event, deduplicated on retry!
        events2 = [e for e in store.list_events(workflow_id=wid) if e.get("event_type") == "workflow_created"]
        assert len(events2) == 1


class TestD3ActiveWorkflowQueries:
    def test_active_queries_recognize_running_workflow(self, store_env):
        proj = {
            "project_id": "proj-queries",
            "project_name": "Query Project",
            "project_root": str(store_env["tmp"]),
            "base_branch": "main",
            "workspace_id": "ws-query",
            "coordinator_pane_id": "pane-query",
            "workflow_file": str(store_env["tmp"] / "wf.json"),
        }
        Path(proj["workflow_file"]).write_text(json.dumps({"name": "q", "nodes": []}))
        wid = "wf-query-01"

        projects.register_workflow(wid, proj, requirement="test query")

        active = projects.active_workflows_for_project("proj-queries")
        assert len(active) == 1
        assert active[0]["workflow_id"] == wid
        assert active[0]["status"] == "running"

        assert wid in projects.non_terminal_workflow_ids()
        assert projects.workflow_closed(wid) is False
        assert projects.workflow_registered(wid) is True
