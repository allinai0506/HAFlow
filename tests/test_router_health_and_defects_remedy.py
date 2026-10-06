"""Tests for remaining defects:
- #4: kind=sign-off support in workflow_docs and CLI
- #7: Router health rejection does not persist failed task or lock node capacity
- #3: CLI router health error does not print misleading isolation opt-out message
- #6: DISPATCH DUPLICATE reports both proposed task_id and existing task_id
- #1: freeze-candidate CLI command for candidate re-freezing
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest

from herdr.state_store import SQLiteStateStore, get_state_store, reset_state_store
from herdr.workflow_docs import NOTE_KINDS, CONTEXT_KINDS, append_note, load_notes, summarize_notes
from herdr.agent_router import (
    RouterRejectionError,
    RouterIsolationRejection,
    RouterHealthRejection,
    RouterPolicyRejection,
)
from herdr.node_capacity import node_usage


def _load_script(name, relative_path):
    import importlib.util
    import importlib.machinery
    root = Path(__file__).resolve().parents[1]
    path = root / relative_path
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(name, str(path)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Defect #4: kind=sign-off
# ---------------------------------------------------------------------------
def test_sign_off_in_note_kinds():
    assert "sign-off" in NOTE_KINDS
    assert "sign-off" in CONTEXT_KINDS


def test_sign_off_note_append_and_context_inclusion(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_WORKFLOW_DOCS_DIR", str(tmp_path))
    wf_id = "wf-sign-off-001"
    note = append_note(
        wf_id,
        kind="sign-off",
        title="Review Sign-Off for Release",
        body="All 3 verifiers green; approved for merge.",
        node="review",
        task_id="task-review-1",
        agent="codex",
    )
    assert note["kind"] == "sign-off"
    assert note["title"] == "Review Sign-Off for Release"

    notes = load_notes(wf_id)
    assert len(notes) == 1
    assert notes[0]["kind"] == "sign-off"

    # Context pack should score and include sign-off notes
    ctx = summarize_notes(notes, node_id="wrapup")
    assert len(ctx) == 1
    assert ctx[0]["kind"] == "sign-off"


def test_cli_note_add_accepts_sign_off(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    root = Path(__file__).resolve().parents[1]

    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "HERDR_STATE_DB": str(db_path),
        "HERDR_WORKFLOW_DOCS_DIR": str(docs_dir),
        "PYTHONPATH": str(root),
    }

    result = subprocess.run(
        [
            sys.executable,
            str(root / "bin/herdr-task"),
            "note-add",
            "--workflow-id", "wf-cli-signoff",
            "--kind", "sign-off",
            "--title", "Lead Sign-off Approval",
            "--text", "Deliverables verified against spec.",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "[NOTE_ADDED]" in result.stdout

    monkeypatch.setenv("HERDR_WORKFLOW_DOCS_DIR", str(docs_dir))
    notes = load_notes("wf-cli-signoff")
    assert len(notes) == 1
    assert notes[0]["kind"] == "sign-off"
    assert notes[0]["title"] == "Lead Sign-off Approval"


# ---------------------------------------------------------------------------
# Defects #7 & #3: Router Health Rejection vs Isolation Rejection
# ---------------------------------------------------------------------------
def test_router_health_rejection_does_not_persist_failed_task(tmp_path, monkeypatch):
    """When router rejects due to health (e.g. Deep Preflight failure):
    1. Do NOT persist a failed task to tasks.json (does not lock capacity/slot).
    2. Do NOT print misleading isolation opt-out instructions.
    3. Abort launch intent and release reservation cleanly.
    """
    cli = _load_script("herdr_task_health_reject_test", "bin/herdr-task")
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(db_path))
    store = SQLiteStateStore(db_path)

    wf_def = {
        "workflow_id": "wf-health-test",
        "project_id": "proj-health",
        "status": "running",
        "project_root": str(tmp_path),
        "config": {
            "nodes": [
                {
                    "id": "review",
                    "max_tasks_per_node": 1,
                    "artifact_mode": "repository_changes",
                    "default_integration_mode": "none",
                    "agent_policy": {"roles": ["reviewer"]},
                }
            ]
        },
    }
    store.save_workflow(wf_def)

    args = SimpleNamespace(
        task_id="task-health-fail",
        workflow_id="wf-health-test",
        node="review",
        stage=None,
        task_type="review",
        agent="auto",
        dispatch_role="reviewer",
        dispatch_round=1,
        candidate_sha="abc123456789",
        source=str(tmp_path),
        prompt="Review changes",
        goal="Review",
        acceptance=[],
        integration_mode="none",
        artifact_mode=None,
        supersedes=None,
        allow_reuse=False,
    )

    # Simulate router failing due to health
    health_err = RouterHealthRejection("Agent 'codex' failed Workflow Deep Preflight: NOT_READY")

    with patch.object(cli, "_get_store", return_value=store), \
         patch.object(cli, "load_tasks", return_value={"tasks": []}), \
         patch.object(cli, "project_for_workflow", return_value=wf_def), \
         patch.object(cli, "choose_agent", side_effect=health_err), \
         patch.object(cli, "ensure_stage_topology", side_effect=AssertionError("topology should not be called")), \
         patch.object(cli, "acquire_pane_for_task", side_effect=AssertionError("pane should not be called")), \
         patch.object(cli, "release_agent_reservation"), \
         pytest.raises(SystemExit) as exc:
        cli._launch_task(args)

    assert exc.value.code == 2

    # Assert #7: NO failed task was saved in tasks.json / state store!
    assert store.get_task("task-health-fail") is None
    all_tasks = store.list_tasks(workflow_id="wf-health-test")
    assert len(all_tasks) == 0

    # Node capacity should still be 0, not locked!
    node_cfg = wf_def["config"]["nodes"][0]
    usage = node_usage(node_cfg, all_tasks, "wf-health-test")
    assert usage["task_count"] == 0
    assert usage["overflow"] is False


def test_router_isolation_rejection_still_records_failure_for_fr6(tmp_path, monkeypatch):
    """When router rejects due to cross-stage isolation:
    1. Still records failed task for FR-6 audit compliance.
    2. Prints isolation fail-closed opt-out instructions.
    """
    cli = _load_script("herdr_task_isolation_reject_test", "bin/herdr-task")
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(db_path))
    store = SQLiteStateStore(db_path)

    wf_def = {
        "workflow_id": "wf-iso-test",
        "project_id": "proj-iso",
        "status": "running",
        "project_root": str(tmp_path),
        "config": {
            "nodes": [
                {
                    "id": "review",
                    "max_tasks_per_node": 2,
                    "artifact_mode": "repository_changes",
                    "default_integration_mode": "none",
                    "agent_policy": {"roles": ["reviewer"]},
                }
            ]
        },
    }
    store.save_workflow(wf_def)

    args = SimpleNamespace(
        task_id="task-iso-fail",
        workflow_id="wf-iso-test",
        node="review",
        stage=None,
        task_type="review",
        agent="auto",
        dispatch_role="reviewer",
        dispatch_round=1,
        candidate_sha="abc123456789",
        source=str(tmp_path),
        prompt="Review changes",
        goal="Review",
        acceptance=[],
        integration_mode="none",
        artifact_mode=None,
        supersedes=None,
        allow_reuse=False,
    )

    iso_err = RouterIsolationRejection(
        "No available Agent for stage 'review': all candidates ['codex'] "
        "were used in stage(s) implement. Isolation is fail-closed; "
        "provide explicit opt-out to bypass."
    )

    with patch.object(cli, "_get_store", return_value=store), \
         patch.object(cli, "load_tasks", return_value={"tasks": []}), \
         patch.object(cli, "project_for_workflow", return_value=wf_def), \
         patch.object(cli, "choose_agent", side_effect=iso_err), \
         patch.object(cli, "release_agent_reservation"), \
         pytest.raises(SystemExit) as exc:
        cli._launch_task(args)

    assert exc.value.code == 2

    # In isolation rejection, failed task IS recorded for audit
    failed_task = store.get_task("task-iso-fail")
    assert failed_task is not None
    assert failed_task["status"] == "failed"
    assert failed_task["failure_reason"] == "router_isolation_rejected"


# ---------------------------------------------------------------------------
# Defect #6: DISPATCH DUPLICATE output semantics
# ---------------------------------------------------------------------------
def test_dispatch_duplicate_output_clarity(tmp_path):
    root = Path(__file__).resolve().parents[1]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    db = tmp_path / "state.db"
    store = get_state_store(db)
    store.save_workflow({
        "workflow_id": "wf-dup",
        "project_id": "p",
        "status": "running",
        "project_root": str(tmp_path),
        "config": {
            "nodes": [
                {
                    "id": "n",
                    "max_tasks_per_node": 2,
                    "artifact_mode": "repository_changes",
                    "default_integration_mode": "none",
                }
            ]
        },
    })
    store.save_task({
        "task_id": "existing-task-001",
        "workflow_id": "wf-dup",
        "node": "n",
        "dispatch_role": "worker",
        "dispatch_round": 1,
        "candidate_sha": "abc111",
        "status": "pending",
    })
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "HERDR_STATE_DB": str(db),
        "TASKS_FILE": str(tmp_path / "tasks.json"),
        "WORKFLOWS_FILE": str(tmp_path / "workflows.json"),
        "PYTHONPATH": str(root),
    }
    result = subprocess.run(
        [
            sys.executable,
            str(root / "bin/herdr-task"),
            "launch",
            "--task-id", "new-attempt-002",
            "--workflow-id", "wf-dup",
            "--node", "n",
            "--source", str(tmp_path),
            "--prompt", "same prompt",
            "--goal", "same goal",
            "--candidate-sha", "abc111",
            "--dispatch-role", "worker",
            "--dispatch-round", "1",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    # Must clearly indicate BOTH proposed task_id and existing task_id
    assert "proposed=new-attempt-002" in result.stdout
    assert "existing=existing-task-001" in result.stdout


# ---------------------------------------------------------------------------
# Defect #1: freeze-candidate CLI
# ---------------------------------------------------------------------------
def test_freeze_candidate_cli(tmp_path):
    root = Path(__file__).resolve().parents[1]
    db = tmp_path / "state.db"
    from herdr.state_store import SQLiteStateStore
    SQLiteStateStore(db).save_workflow({'workflow_id': 'wf-freeze-test', 'status': 'running'})
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "HERDR_STATE_DB": str(db),
        "PYTHONPATH": str(root),
    }
    res = subprocess.run(
        [
            sys.executable,
            str(root / "bin/herdr-task"),
            "freeze-candidate",
            "wf-freeze-test",
            "--candidate-sha", "c" * 40,
            "--delivery-branch", "feature/test",
            "--source-node", "impl",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert res.returncode == 0, res.stderr
    assert "[CANDIDATE FROZEN]" in res.stdout
    assert "c" * 40 in res.stdout

    # Verify state store has the frozen event
    from herdr.scheduler_facts import latest_frozen_candidate_sha
    assert latest_frozen_candidate_sha("wf-freeze-test", db_path=db) == "c" * 40
