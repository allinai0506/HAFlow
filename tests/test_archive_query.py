#!/usr/bin/env python3
"""Tests for the task archive query core (herdr/archive.py) and the console
/api/archive endpoint.

Contract: the archive list must read the authoritative StateStore (not the
JSON projection) and must never leak active tasks into the default view.
"""

import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

from herdr import archive as arc
from herdr.state_store import get_state_store, reset_state_store


def _task(i, **overrides):
    task = {
        "task_id": f"t-{i:03d}",
        "workflow_id": "wf-a",
        "project_id": "p1",
        "project_name": "proj",
        "node": "requirements",
        "node_label": "2需求分析",
        "agent": "codex",
        "status": "cleaned",
        "stage_verdict": "pass",
        "goal": f"goal {i}",
        "created_at": 1000 + i,
        "updated_at": 2000 + i,
    }
    task.update(overrides)
    return task


# ---------------------------------------------------------------------------
# Pure core: herdr/archive.py
# ---------------------------------------------------------------------------

def test_default_status_filter_keeps_archived_only():
    tasks = [
        _task(1, status="cleaned"),
        _task(2, status="superseded"),
        _task(3, status="failed"),
        _task(4, status="working"),
        _task(5, status="completed"),
    ]
    res = arc.query_archived_tasks(tasks)
    assert res["total"] == 3
    assert {item["status"] for item in res["items"]} == {"cleaned", "superseded", "failed"}


def test_status_all_includes_active_and_finalizing():
    tasks = [_task(1, status="cleaned"), _task(2, status="working"), _task(3, status="completed")]
    res = arc.query_archived_tasks(tasks, status="all")
    assert res["total"] == 3


def test_status_group_active_and_exact_status():
    tasks = [_task(1, status="cleaned"), _task(2, status="dispatched"), _task(3, status="blocked")]
    assert arc.query_archived_tasks(tasks, status="active")["total"] == 2
    assert arc.query_archived_tasks(tasks, status="cleaned")["total"] == 1


def test_project_workflow_and_agent_filters():
    tasks = [
        _task(1, project_id="p1", workflow_id="wf-alpha-01", agent="codex"),
        _task(2, project_id="p2", workflow_id="wf-beta-02", agent="claude"),
    ]
    assert arc.query_archived_tasks(tasks, project_id="p2")["total"] == 1
    assert arc.query_archived_tasks(tasks, workflow_id="beta")["total"] == 1
    assert arc.query_archived_tasks(tasks, workflow_id="wf-alpha")["total"] == 1
    assert arc.query_archived_tasks(tasks, agent="claude")["total"] == 1
    assert arc.query_archived_tasks(tasks, agent="claude", project_id="p1")["total"] == 0


def test_keyword_search_matches_id_goal_and_label():
    tasks = [
        _task(1, task_id="req-summary-format", goal="摘要格式化"),
        _task(2, goal="标题溯源展示"),
    ]
    assert arc.query_archived_tasks(tasks, q="summary")["total"] == 1
    assert arc.query_archived_tasks(tasks, q="溯源")["total"] == 1
    assert arc.query_archived_tasks(tasks, q="需求分析")["total"] == 2
    assert arc.query_archived_tasks(tasks, q="不存在的词")["total"] == 0


def test_sorted_by_updated_desc_with_task_id_tiebreak():
    tasks = [
        _task(1, updated_at=100),
        _task(2, updated_at=300),
        _task(3, updated_at=200),
        _task(4, updated_at=200, task_id="a-000"),
    ]
    res = arc.query_archived_tasks(tasks)
    assert [item["task_id"] for item in res["items"]] == ["t-002", "a-000", "t-003", "t-001"]


def test_pagination_reports_total_and_slices():
    tasks = [_task(i, updated_at=100 + i) for i in range(1, 8)]
    res = arc.query_archived_tasks(tasks, limit=3, offset=3)
    assert res["total"] == 7
    assert res["count"] == 3
    assert res["limit"] == 3
    assert res["offset"] == 3
    assert [item["task_id"] for item in res["items"]] == ["t-004", "t-003", "t-002"]


def test_limit_and_offset_are_clamped():
    tasks = [_task(i) for i in range(1, 5)]
    assert arc.query_archived_tasks(tasks, limit=0)["limit"] == 1
    assert arc.query_archived_tasks(tasks, limit=9999)["limit"] == arc.MAX_LIMIT
    assert arc.query_archived_tasks(tasks, offset=-5)["offset"] == 0
    assert arc.query_archived_tasks(tasks, limit="bad", offset="bad")["count"] == 4


def test_summarize_task_exposes_duration_and_fallbacks():
    item = arc.summarize_task(_task(1, created_at=100, updated_at=160, stage=None, stage_label="4实现"))
    assert item["duration_seconds"] == 60.0
    assert item["node"] == "requirements"
    assert item["node_label"] == "2需求分析"

    fallback = arc.summarize_task({
        "task_id": "x",
        "status": "superseded",
        "stage": "plan",
        "created_at": 10,
        "last_activity_at": 40,
    })
    assert fallback["node"] == "plan"
    assert fallback["node_label"] == "plan"
    assert fallback["duration_seconds"] == 30.0
    assert fallback["updated_at"] == 40


# ---------------------------------------------------------------------------
# Console shell: /api/archive wiring
# ---------------------------------------------------------------------------

@pytest.fixture
def archive_console_env(tmp_path, monkeypatch):
    from console import herdr_factory_console as console

    db_file = tmp_path / "state.db"
    wf_file = tmp_path / "workflows.json"
    tasks_file = tmp_path / "tasks.json"

    monkeypatch.setenv("HERDR_STATE_DB", str(db_file))
    monkeypatch.setenv("WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setenv("TASKS_FILE", str(tasks_file))
    monkeypatch.setattr(console, "WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setattr(console, "TASKS_FILE", str(tasks_file))

    reset_state_store()
    yield {"console": console, "db_file": db_file, "tasks_file": tasks_file}
    reset_state_store()


def test_console_archive_query_reads_statestore_not_stale_projection(archive_console_env):
    console = archive_console_env["console"]
    store = get_state_store(archive_console_env["db_file"])

    store.save_task(_task(1, project_id="p-arc", workflow_id="wf-arc-01"))
    store.save_task(_task(2, project_id="p-arc", workflow_id="wf-arc-01", status="working"))

    # Projection file is empty/stale on purpose: archive must still see SQLite.
    Path(archive_console_env["tasks_file"]).write_text(json.dumps({"tasks": []}), encoding="utf-8")

    res = console.archive_query(project_id="p-arc")
    assert res["total"] == 1
    assert res["items"][0]["task_id"] == "t-001"
    assert res["items"][0]["workflow_id"] == "wf-arc-01"


def test_console_archive_query_filters_and_pagination(archive_console_env):
    console = archive_console_env["console"]
    store = get_state_store(archive_console_env["db_file"])

    for i in range(1, 6):
        store.save_task(_task(i, project_id="p-arc", workflow_id="wf-arc-01", agent="codex"))
    store.save_task(_task(6, project_id="p-arc", workflow_id="wf-arc-02", agent="claude"))

    res = console.archive_query(project_id="p-arc", workflow_id="arc-02")
    assert res["total"] == 1 and res["items"][0]["agent"] == "claude"

    res = console.archive_query(project_id="p-arc", status="all", limit=2, offset=4)
    assert res["total"] == 6
    assert res["count"] == 2

    res = console.archive_query(project_id="p-arc", q="goal 3")
    assert res["total"] == 1


def test_console_archive_route_is_registered():
    source = (Path(__file__).resolve().parent.parent / "console" / "herdr_factory_console.py").read_text(encoding="utf-8")
    assert "'/api/archive'" in source
    assert "'/api/workflows'" in source
    assert "def archive_query(" in source
    assert "def api_workflows(" in source


def test_console_workflows_api_wiring(archive_console_env):
    console = archive_console_env["console"]
    wf_file = archive_console_env["console"].WORKFLOWS_FILE
    workflows_data = {
        "workflows": {
            "wf-proj1-01": {
                "project_id": "p1",
                "title": "功能A开发",
                "created_at": 100,
            },
            "wf-proj1-02": {
                "project_id": "p1",
                "requirement_subject": "功能B优化",
                "created_at": 200,
            },
            "wf-proj2-01": {
                "project_id": "p2",
                "title": "项目2功能",
                "created_at": 300,
            },
        }
    }
    Path(wf_file).write_text(json.dumps(workflows_data), encoding="utf-8")

    all_wfs = console.api_workflows()
    assert len(all_wfs) == 3
    assert all_wfs[0]["workflow_id"] == "wf-proj2-01"

    p1_wfs = console.api_workflows(pid="p1")
    assert len(p1_wfs) == 2
    assert [w["workflow_id"] for w in p1_wfs] == ["wf-proj1-02", "wf-proj1-01"]
    assert p1_wfs[0]["requirement_subject"] == "功能B优化"
    assert p1_wfs[1]["title"] == "功能A开发"


def test_console_frontend_exposes_archive_view():
    console_path = Path(__file__).resolve().parent.parent / "console" / "herdr_factory_console.py"
    loader = importlib.machinery.SourceFileLoader("console_archive_ui_test", str(console_path))
    spec = importlib.util.spec_from_loader("console_archive_ui_test", loader)
    console = importlib.util.module_from_spec(spec)
    loader.exec_module(console)

    html = console.HTML_TEMPLATE
    assert "showArchive()" in html
    assert "/api/archive" in html
    assert "/api/workflows" in html
    assert "任务归档" in html
    assert '<select id="arcWorkflow"' in html
    assert "onArchiveProjectChange()" in html
    assert "onArchiveWorkflowChange()" in html
    assert "filterArchiveByWorkflow(" in html

