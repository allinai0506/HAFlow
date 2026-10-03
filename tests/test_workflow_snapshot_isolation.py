import json
from pathlib import Path
import pytest

from herdr import projects
from herdr.state_store import SQLiteStateStore


def test_register_workflow_creates_snapshot_and_resets_dynamic_fields(tmp_path: Path, monkeypatch):
    from herdr import workflow_docs as wd
    monkeypatch.setenv(wd.DOCS_DIR_ENV, str(tmp_path / "workflows"))

    db_path = tmp_path / "state.db"
    store = SQLiteStateStore(db_path=db_path)
    monkeypatch.setattr(projects, "_get_store", lambda: store)

    shared_wf = tmp_path / "projects" / "p1" / "workflow.json"
    shared_wf.parent.mkdir(parents=True, exist_ok=True)
    shared_wf.write_text(json.dumps({
        "workflow_template": "software-development-v1",
        "nodes": [
            {
                "id": "implementation",
                "label": "4实现",
                "required_task_ids": ["impl-download-guard-compliance-truth"],
                "status": "completed",
            },
            {
                "id": "test",
                "label": "5测试",
            }
        ]
    }), encoding="utf-8")

    project = {
        "project_id": "p1",
        "project_name": "Project 1",
        "project_root": str(tmp_path),
        "base_branch": "main",
        "workspace_id": "ws-1",
        "coordinator_pane_id": "pane-1",
        "workflow_file": str(shared_wf),
    }

    wid = "wf-snap-isolated-01"
    projects.register_workflow(wid, project, requirement="build feature")

    record = store.get_workflow(wid)
    assert record is not None
    snapshot_path = Path(record["workflow_file"])
    # 1. 验证创建了独立的 Workflow Run 快照文件
    assert snapshot_path.exists()
    assert snapshot_path != shared_wf
    assert wid in str(snapshot_path)

    # 2. 验证后续对项目共享文件的改动不会污染已启动的工作流
    shared_wf.write_text(json.dumps({
        "workflow_template": "software-development-v1",
        "nodes": [
            {
                "id": "implementation",
                "label": "4实现",
                "required_task_ids": ["foreign-polluting-task"],
            }
        ]
    }), encoding="utf-8")

    cfg = projects.workflow_config_for(wid)
    cfg_impl = next(n for n in cfg["nodes"] if n["id"] == "implementation")
    assert cfg_impl.get("required_task_ids") == ["impl-download-guard-compliance-truth"]


def test_clean_workflow_definition_resets_dynamic_fields():
    dirty = {
        "workflow_template": "tpl",
        "nodes": [
            {
                "id": "impl",
                "required_task_ids": ["task-1"],
                "task_ids": ["task-1"],
                "active_task_ids": ["task-1"],
                "status": "in_progress",
                "purpose": "do work",
            }
        ],
        "stages": [
            {
                "key": "impl",
                "required_task_ids": ["task-1"],
                "status": "running",
            }
        ]
    }
    cleaned = projects._clean_workflow_definition_for_new_run(dirty)
    assert "required_task_ids" not in cleaned["nodes"][0]
    assert "task_ids" not in cleaned["nodes"][0]
    assert "active_task_ids" not in cleaned["nodes"][0]
    assert "status" not in cleaned["nodes"][0]
    assert cleaned["nodes"][0]["purpose"] == "do work"
    assert "required_task_ids" not in cleaned["stages"][0]
    assert "status" not in cleaned["stages"][0]
