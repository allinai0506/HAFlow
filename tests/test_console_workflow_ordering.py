#!/usr/bin/env python3
"""Project workflow switcher ordering: newest-first by created_at.

Regression context: workflow IDs mix two naming schemes
(`wf-proj-<hash>-<timestamp>` and `wf-proj-<MMDD>-<seq>`), so lexicographic
sorting put an older hashed ID above a newer date-sequenced one. The console
then defaulted to a stale workflow and buried the running one last in the
switcher — users reported "启动后看不到任务".
"""

import json
import os
import tempfile
from pathlib import Path

from console import herdr_factory_console as c
from herdr import state_db


def _wf(wid, created_at, project_id="p1", status="completed"):
    return {
        "workflow_id": wid,
        "project_id": project_id,
        "status": status,
        "created_at": created_at,
        "requirement_subject": wid,
    }


def _seed(monkeypatch, tmp_path):
    db = tmp_path / "state.db"
    state_db.init_db(db)
    monkeypatch.setenv("HERDR_STATE_DB", str(db))
    wfs = {
        "wf-proj-a380753e-20260912-155001": _wf("wf-proj-a380753e-20260912-155001", 100),
        "wf-proj-0914-02": _wf("wf-proj-0914-02", 200),
        "wf-proj-0915-01": _wf("wf-proj-0915-01", 300, status="running"),
        "wf-other-01": _wf("wf-other-01", 400, project_id="p2"),
    }
    for wid, w in wfs.items():
        state_db.save_workflow({"workflow_id": wid, "project_id": w["project_id"],
                                "status": w["status"], "created_at": w["created_at"],
                                "config": {}}, db_path=db)
    return wfs


def _seed_with_missing_ts(monkeypatch, tmp_path):
    db = tmp_path / "state.db"
    state_db.init_db(db)
    monkeypatch.setenv("HERDR_STATE_DB", str(db))
    conn = state_db.get_db_connection(db)
    try:
        conn.execute(
            "INSERT INTO workflows (workflow_id, status, metadata_json, config_json, created_at, updated_at) "
            "VALUES (?, 'completed', ?, '{}', NULL, 1)",
            ("wf-legacy-no-ts", '{"project_id": "p1"}'),
        )
        conn.commit()
    finally:
        conn.close()
    state_db.save_workflow({"workflow_id": "wf-proj-0915-01", "project_id": "p1",
                            "status": "running", "created_at": 300, "config": {}}, db_path=db)


import json


def test_workflows_for_project_sorted_by_created_at_desc(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    out = c.workflows_for_project("p1")
    assert [w["workflow_id"] for w in out] == [
        "wf-proj-0915-01",
        "wf-proj-0914-02",
        "wf-proj-a380753e-20260912-155001",
    ]


def test_project_detail_latest_workflow_is_newest_not_lexicographic(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    monkeypatch.setattr(c.inventory, "project_by_id", lambda pid: {"project_id": pid, "workspace_id": "w1"})
    monkeypatch.setattr(c.runtime, "tabs", lambda ws: [])
    monkeypatch.setattr(c.runtime, "panes", lambda ws: [])
    monkeypatch.setattr(c.runtime, "slots", lambda p: [])
    monkeypatch.setattr(c.runtime, "preflight", lambda p: [])

    detail = c.project_detail("p1")
    assert detail["latest_workflow_id"] == "wf-proj-0915-01"


def test_project_detail_handles_missing_created_at(monkeypatch, tmp_path):
    _seed_with_missing_ts(monkeypatch, tmp_path)
    out = c.workflows_for_project("p1")
    assert [w["workflow_id"] for w in out] == ["wf-proj-0915-01", "wf-legacy-no-ts"]