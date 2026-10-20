"""Tests for workflow run definition snapshot fail-closed invariant (SC-006).

Verifies:
1. When definition source file exists and freeze succeeds, register_workflow freezes private snapshot.
2. When definition source file exists but freeze fails, register_workflow fails-closed (raises RuntimeError)
   and refuses to register the workflow in state.db.
3. When source file contains invalid non-object definition, register_workflow raises ValueError and aborts.
4. freeze_run_definition parameter validation (empty ID, non-dict definition).
"""

import json
from pathlib import Path
import pytest
from herdr import projects
from herdr.state_store import get_state_store


def test_snapshot_success_registers_run_with_private_snapshot(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERDR_STATE_DB", str(tmp_path / "state.db"))
    config = tmp_path / "workflow.json"
    config.write_text(json.dumps({"nodes": [{"id": "craft"}]}))

    project = {
        "project_id": "p-1",
        "project_name": "Test Project",
        "project_root": str(tmp_path),
        "base_branch": "main",
        "workspace_id": "ws-1",
        "coordinator_pane_id": "pane-1",
        "workflow_file": str(config),
    }

    projects.register_workflow("wf-success-1", project)
    wf = get_state_store().get_workflow("wf-success-1")
    assert wf is not None
    assert wf["workflow_id"] == "wf-success-1"
    # Resolved workflow_file must point to a private snapshot, not the shared file
    assert wf["workflow_file"] != str(config)
    assert Path(wf["workflow_file"]).name == "workflow.json"
    assert "wf-success-1" in wf["workflow_file"]


def test_snapshot_freeze_failure_fails_closed_refuses_registration(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERDR_STATE_DB", str(tmp_path / "state.db"))
    config = tmp_path / "workflow.json"
    config.write_text(json.dumps({"nodes": [{"id": "craft"}]}))

    project = {
        "project_id": "p-2",
        "project_name": "Test Project",
        "project_root": str(tmp_path),
        "base_branch": "main",
        "workspace_id": "ws-2",
        "coordinator_pane_id": "pane-2",
        "workflow_file": str(config),
    }

    monkeypatch.setattr(projects, "freeze_run_definition", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="Workflow definition snapshot failed; run was not registered"):
        projects.register_workflow("wf-failed-1", project)

    # State DB must NOT contain the workflow
    assert get_state_store().get_workflow("wf-failed-1") is None


def test_invalid_definition_content_aborts_registration(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERDR_STATE_DB", str(tmp_path / "state.db"))
    config = tmp_path / "workflow.json"
    config.write_text("invalid json content")

    project = {
        "project_id": "p-3",
        "project_name": "Test Project",
        "project_root": str(tmp_path),
        "base_branch": "main",
        "workspace_id": "ws-3",
        "coordinator_pane_id": "pane-3",
        "workflow_file": str(config),
    }

    with pytest.raises(ValueError, match="Workflow definition is unreadable or not an object"):
        projects.register_workflow("wf-invalid-1", project)

    assert get_state_store().get_workflow("wf-invalid-1") is None


def test_freeze_run_definition_validation():
    with pytest.raises(ValueError, match="workflow_id is required"):
        projects.freeze_run_definition("")

    with pytest.raises(ValueError, match="workflow_id is required"):
        projects.freeze_run_definition(None)

    with pytest.raises(ValueError, match="definition must be a mapping or None"):
        projects.freeze_run_definition("wf-1", definition=["not", "a", "dict"])
