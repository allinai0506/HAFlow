"""Tests for workflow completion experience generation and collector fixes."""

import os
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from herdr import state_db
from herdr.experience.client import ExperienceClient
from herdr.experience.collector import ExperienceCollector


@pytest.fixture
def temp_db(tmp_path: Path):
    db_file = tmp_path / "state.db"
    state_db.init_db(db_file)
    return db_file


def test_collect_workflow_tasks_succeeds_on_standard_tasks_schema(temp_db: Path, tmp_path: Path):
    collector_db = tmp_path / "collector.db"
    collector = ExperienceCollector(db_path=collector_db)

    # Insert a workflow and completed task
    state_db.save_workflow({"workflow_id": "wf_test_01", "status": "completed"}, db_path=temp_db)
    with state_db.get_db_connection(temp_db) as conn:
        conn.execute(
            """
            INSERT INTO tasks (task_id, workflow_id, node, stage, agent, status, payload_json, created_at, updated_at)
            VALUES (?, ?, 'impl', 'implementation', 'codex', 'completed', '{"run_id": "run_001"}', ?, ?)
            """,
            ("task_test_01", "wf_test_01", time.time(), time.time()),
        )
        conn.commit()

    res = collector.collect_workflow_tasks("wf_test_01", state_db_path=temp_db)
    assert res.get("ok") is True, f"Failed with: {res}"
    assert res.get("enqueued") == 1


def test_experience_client_fallback_to_local_credentials(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HERDR_HUB_TOKEN", raising=False)

    cred_dir = tmp_path / "credentials"
    cred_dir.mkdir(parents=True)
    admin_token_file = cred_dir / "experience-hub-admin.token"
    admin_token_file.write_text("secret_admin_token_12345\n", encoding="utf-8")

    monkeypatch.setenv("HERDR_CREDENTIALS_DIR", str(cred_dir))

    client = ExperienceClient(base_url="http://127.0.0.1:8780")
    assert client.token == "secret_admin_token_12345"


def test_close_workflow_owned_triggers_experience_collector(tmp_path: Path):
    from herdr.state_db import ACTIVE_TASK_STATUSES
    from herdr.state_store import SQLiteStateStore
    from herdr.task_cli_teardown import _teardown_output_guard
    from herdr.task_cli_workflow_close import close_workflow_owned

    store = SQLiteStateStore(tmp_path / "state.db")
    store.save_workflow({
        "workflow_id": "wf-close-exp",
        "status": "running",
        "execution_id": "exec-exp",
        "project_root": str(tmp_path),
        "base_branch": "main",
        "config": {"nodes": [{"id": "wrapup"}]},
    })
    store.save_task({
        "task_id": "wrap",
        "workflow_id": "wf-close-exp",
        "node": "wrapup",
        "stage": "wrapup",
        "status": "cleaned",
        "run_id": "run-exp",
        "execution_id": "exec-exp",
        "integration_mode": "none",
    })

    sync_calls = []

    class MockCollector:
        def collect_and_sync_lessons(self):
            sync_calls.append("lessons")

        def collect_workflow_tasks(self, wid):
            sync_calls.append(f"tasks:{wid}")

        def recover_and_sync(self):
            sync_calls.append("recover")

    with patch("herdr.experience.collector.ExperienceCollector", return_value=MockCollector()):
        close_workflow_owned(
            "wf-close-exp",
            load_tasks=lambda: {"tasks": [store.get_task("wrap")]},
            load_workflow_entry=lambda wid: (store.get_workflow(wid), None),
            get_store=lambda: store,
            finalize_one=lambda task, **opts: {"task_id": task["task_id"], "action": "cleaned", "status": "cleaned"},
            close_pane=lambda pane: None,
            stage_reset=lambda wid: None,
            mark_workflow_completed=lambda wid, **kw: None,
            workflow_stage_tabs=lambda wid, tasks: {"workspace_id": None, "tab_ids": []},
            live_workspace_panes=lambda: {},
            foreign_panes_in_tab=lambda tab: [],
            close_owned_pane=lambda pane, ident: None,
            teardown_output_guard=_teardown_output_guard,
            record_output_retention=lambda task, out: None,
            clone_root=str(tmp_path / "clones"),
            teardown_blocking_statuses=ACTIVE_TASK_STATUSES,
        )

    assert "lessons" in sync_calls
    assert "tasks:wf-close-exp" in sync_calls
    assert "recover" in sync_calls

