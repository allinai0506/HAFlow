#!/usr/bin/env python3
"""Tests for StateStore Interface and SQLiteStateStore Implementation."""

import json
import os
import tempfile
import time
from pathlib import Path
import pytest

from herdr.state_store import StateStore, SQLiteStateStore, get_state_store, set_state_store, reset_state_store


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    db_file = tmp_path / "state.db"
    wf_file = tmp_path / "workflows.json"
    tasks_file = tmp_path / "tasks.json"
    steering_file = tmp_path / "steering.json"
    cp_dir = tmp_path / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HERDR_STATE_DB", str(db_file))
    monkeypatch.setenv("WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setenv("TASKS_FILE", str(tasks_file))
    monkeypatch.setenv("STEERING_FILE", str(steering_file))
    monkeypatch.setenv("CHECKPOINTS_DIR", str(cp_dir))

    reset_state_store()

    yield {
        "db_file": db_file,
        "wf_file": wf_file,
        "tasks_file": tasks_file,
        "steering_file": steering_file,
        "cp_dir": cp_dir,
    }

    reset_state_store()


def test_sqlite_state_store_is_instance_of_state_store(store_env):
    store = SQLiteStateStore(db_path=store_env["db_file"])
    assert isinstance(store, StateStore)


def test_sqlite_state_store_saves_ordinary_working_context_mapping(store_env):
    from herdr.context_compiler import compile_working_context
    from herdr.observation import ObservationStore
    from herdr.state_db import save_task, save_workflow

    db_file = store_env["db_file"]
    save_workflow(
        {
            "workflow_id": "wf-context-store",
            "title": "Context Store",
            "status": "running",
            "config": {"nodes": [{"id": "review", "depends_on": []}]},
        },
        db_path=db_file,
    )
    task = {
        "task_id": "task-context-store",
        "workflow_id": "wf-context-store",
        "run_id": "run-context-store",
        "workflow_run_id": "scope-context-store",
        "node": "review",
        "stage": "review",
        "agent": "developer",
        "agent_role": "developer",
        "status": "working",
        "goal": "Store a context",
    }
    save_task(task, db_path=db_file)
    context = compile_working_context(
        workflow_id=task["workflow_id"],
        task_id=task["task_id"],
        agent_role="developer",
        store=ObservationStore(db_file),
        db_path=db_file,
    )
    store = SQLiteStateStore(db_path=db_file)
    saved = store.save_working_context(context.to_mapping())
    assert saved["context_id"] == context.context_id


def test_workflow_crud_operations(store_env):
    store = SQLiteStateStore(db_path=store_env["db_file"])

    # 1. Save workflow
    wf = {
        "workflow_id": "wf-001",
        "title": "测试工作流 1",
        "status": "running",
        "template_name": "universal_sdlc",
        "current_stage": "dev",
        "config": {"nodes": [{"id": "dev"}]},
        "paused_nodes": ["test"],
    }
    store.save_workflow(wf)

    # 2. Get workflow
    fetched = store.get_workflow("wf-001")
    assert fetched is not None
    assert fetched["workflow_id"] == "wf-001"
    assert fetched["title"] == "测试工作流 1"
    assert fetched["status"] == "running"
    assert fetched["paused_nodes"] == ["test"]

    # 3. List workflows
    store.save_workflow({"workflow_id": "wf-002", "title": "测试工作流 2", "status": "completed"})
    all_wfs = store.list_workflows()
    assert len(all_wfs) == 2

    completed_wfs = store.list_workflows(status="completed")
    assert len(completed_wfs) == 1
    assert completed_wfs[0]["workflow_id"] == "wf-002"

    # 4. Delete workflow
    deleted = store.delete_workflow("wf-002")
    assert deleted is True
    assert store.get_workflow("wf-002") is None
    assert len(store.list_workflows()) == 1


def test_task_crud_operations(store_env):
    store = SQLiteStateStore(db_path=store_env["db_file"])

    t = {
        "task_id": "task-100",
        "workflow_id": "wf-100",
        "node": "dev",
        "stage": "dev",
        "agent": "codex",
        "status": "working",
        "pane_id": "pane-1",
        "goal": "完成模块开发",
        "custom_attr": "extra_value",
    }
    store.save_task(t)

    fetched = store.get_task("task-100")
    assert fetched is not None
    assert fetched["task_id"] == "task-100"
    assert fetched["workflow_id"] == "wf-100"
    assert fetched["agent"] == "codex"
    assert fetched["custom_attr"] == "extra_value"

    tasks = store.list_tasks(workflow_id="wf-100")
    assert len(tasks) == 1
    assert tasks[0]["task_id"] == "task-100"

    deleted = store.delete_task("task-100")
    assert deleted is True
    assert store.get_task("task-100") is None


def test_steering_operations(store_env):
    store = SQLiteStateStore(db_path=store_env["db_file"])

    steer = {
        "steer_id": "str-999",
        "task_id": "task-200",
        "workflow_id": "wf-200",
        "instruction": "请先修复测试失败",
        "operator": "commander",
        "urgent": True,
        "status": "pending",
    }
    store.save_steer(steer)

    fetched = store.get_steer("str-999")
    assert fetched is not None
    assert fetched["instruction"] == "请先修复测试失败"
    assert fetched["urgent"] is True
    assert fetched["status"] == "pending"

    # List steers
    steers = store.list_steers(task_id="task-200", status="pending")
    assert len(steers) == 1

    # Update status
    now = time.time()
    store.update_steer_status("str-999", status="dispatched", dispatched_at=now)
    fetched_after = store.get_steer("str-999")
    assert fetched_after["status"] == "dispatched"
    assert fetched_after["dispatched_at"] == now

    # Record steering history
    store.record_steering_history({
        "action": "steer_dispatched",
        "task_id": "task-200",
        "steer_id": "str-999",
        "instruction": "请先修复测试失败",
        "operator": "commander",
        "timestamp": now,
    })
    hist = store.list_steering_history(task_id="task-200")
    assert len(hist) == 1
    assert hist[0]["action"] == "steer_dispatched"


def test_state_store_records_and_lists_workflow_events(store_env):
    store = SQLiteStateStore(db_path=store_env["db_file"])

    store.record_event(
        "task.blocked",
        {"reason": "needs review"},
        workflow_id="wf-store-event",
        node_id="review",
        task_id="task-store-event",
        agent_id="claude",
        source="sentinel",
        timestamp=1773472000.25,
    )

    events = store.list_events(workflow_id="wf-store-event")

    assert len(events) == 1
    assert events[0]["workflow_id"] == "wf-store-event"
    assert events[0]["node_id"] == "review"
    assert events[0]["task_id"] == "task-store-event"
    assert events[0]["agent_id"] == "claude"
    assert events[0]["event_type"] == "task.blocked"
    assert events[0]["timestamp"] == 1773472000.25
    assert events[0]["payload"] == {"reason": "needs review"}
    assert events[0]["source"] == "sentinel"


def test_checkpoint_lifecycle_via_store(store_env):
    store = SQLiteStateStore(db_path=store_env["db_file"])

    wid = "wf-cp-store"
    store.save_workflow({"workflow_id": wid, "title": "快照测试", "status": "running"})
    store.save_task({"task_id": "t-cp-1", "workflow_id": wid, "node": "dev", "status": "completed"})

    cp = store.create_checkpoint(wid, tag="v1.0")
    assert cp["ok"] is True
    cpid = cp["checkpoint_id"]

    cps = store.list_checkpoints(wid)
    assert len(cps) == 1
    assert cps[0]["checkpoint_id"] == cpid

    snap = store.get_checkpoint(wid, cpid)
    assert snap["checkpoint_id"] == cpid
    assert snap["workflow"]["title"] == "快照测试"

    # Modify and restore
    store.save_workflow({"workflow_id": wid, "title": "被污染的标题", "status": "failed"})
    res = store.restore_checkpoint(wid, cpid)
    assert res["ok"] is True
    restored_wf = store.get_workflow(wid)
    assert restored_wf["title"] == "快照测试"


def test_json_export_and_migration(store_env):
    store = SQLiteStateStore(db_path=store_env["db_file"], auto_migrate_json=False)

    wid = "wf-export"
    store.save_workflow({"workflow_id": wid, "title": "导出测试", "status": "running"})
    store.save_task({"task_id": "t-exp", "workflow_id": wid, "node": "dev", "status": "working"})
    store.save_steer({
        "steer_id": "s-exp",
        "task_id": "t-exp",
        "instruction": "测试指令",
        "status": "pending",
    })

    # Test export
    wfs_json = store.export_workflows_json()
    assert wid in wfs_json["workflows"]

    tasks_json = store.export_tasks_json()
    assert len(tasks_json["tasks"]) == 1
    assert tasks_json["tasks"][0]["task_id"] == "t-exp"

    steering_json = store.export_steering_json()
    assert "t-exp" in steering_json["steering_queues"]
    assert steering_json["steering_queues"]["t-exp"][0]["steer_id"] == "s-exp"

    # Export all
    export_dir = store_env["cp_dir"].parent / "exported_json"
    exported = store.export_all_json(export_dir)
    assert exported["ok"] is True
    assert (export_dir / "workflows.json").exists()
    assert (export_dir / "tasks.json").exists()
    assert (export_dir / "steering.json").exists()


def test_global_state_store_singleton(store_env):
    s1 = get_state_store()
    s2 = get_state_store()
    assert s1 is s2

    custom_db = store_env["cp_dir"] / "custom.db"
    s_custom = get_state_store(custom_db)
    assert s_custom.db_path == custom_db


def test_cross_module_single_source_of_truth_anti_skew(store_env):
    """Verify that kernel and steering mutations write to StateStore/SQLite as sole truth."""
    from unittest.mock import patch
    from herdr import kernel, steering

    store = get_state_store()

    wid = "wf-anti-skew-001"
    store.save_workflow({
        "workflow_id": wid,
        "title": "Anti Skew Verification",
        "status": "running",
        "config": {"nodes": [{"id": "dev"}]},
    })

    tid = "task-anti-skew-001"
    store.save_task({
        "task_id": tid,
        "workflow_id": wid,
        "node": "dev",
        "agent": "codex",
        "status": "working",
        "pane_id": "pane-mock-99",
    })

    # 1. Kernel pause mutation: verify SQLite reflects status immediately
    k_res = kernel.pause_workflow(wid)
    assert k_res["ok"] is True
    wf_in_sqlite = store.get_workflow(wid)
    assert wf_in_sqlite["status"] == "paused"

    # 2. Kernel resume mutation: verify SQLite reflects status immediately
    k_res2 = kernel.resume_workflow(wid)
    assert k_res2["ok"] is True
    wf_in_sqlite = store.get_workflow(wid)
    assert wf_in_sqlite["status"] == "running"

    # 3. Steering queue mutation: verify SQLite reflects steer item immediately
    st_res = steering.queue_steer(tid, "Hold execution until code review", operator="lead", execute_dispatch=False)
    assert st_res["ok"] is True
    steer_id = st_res["steer_id"]
    steer_in_sqlite = store.get_steer(steer_id)
    assert steer_in_sqlite is not None
    assert steer_in_sqlite["instruction"] == "Hold execution until code review"
    assert steer_in_sqlite["status"] == "pending"

    # 4. Steering dispatch mutation: verify SQLite reflects dispatched status and task history
    from unittest.mock import MagicMock
    with patch("subprocess.run", return_value=MagicMock(returncode=0)):
        d_res = steering.dispatch_steer_now(tid, steer_id)
        assert d_res["ok"] is True
        steer_dispatched = store.get_steer(steer_id)
        assert steer_dispatched["status"] == "dispatched"

        task_in_sqlite = store.get_task(tid)
        assert task_in_sqlite["last_steered_at"] is not None
        assert len(task_in_sqlite["steering_history"]) == 1
        assert task_in_sqlite["steering_history"][0]["steer_id"] == steer_id

    # 5. Steering halt task: verify SQLite reflects interrupted status
    with patch("subprocess.run", return_value=MagicMock(returncode=0)):
        h_res = steering.halt_task(tid, reason="manual pause for audit", operator="lead", execute_kill=True)
        assert h_res["ok"] is True
        task_halted = store.get_task(tid)
        assert task_halted["status"] == "interrupted"
        assert task_halted["interrupt_reason"] == "manual pause for audit"

    # 6. Verify compatibility files are completely consistent with SQLite
    wfs_exported = store.export_workflows_json()
    assert wfs_exported["workflows"][wid]["status"] == "running"
    tasks_exported = store.export_tasks_json()
    assert tasks_exported["tasks"][0]["status"] == "interrupted"


def test_anti_split_brain_json_cannot_override_sqlite(store_env):
    """Verify that JSON file tampering NEVER overrides authoritative SQLite state."""
    import importlib.util
    from herdr import kernel

    store = get_state_store()

    wid = "wf-anti-sb-01"
    tid = "task-anti-sb-01"

    # 1. Authoritative SQLite state: task completed, workflow completed
    store.save_workflow({
        "workflow_id": wid,
        "title": "Anti Split Brain Workflow",
        "status": "completed",
        "config": {"nodes": [{"id": "dev"}]},
    })
    store.save_task({
        "task_id": tid,
        "workflow_id": wid,
        "node": "dev",
        "stage": "dev",
        "status": "completed",
        "stage_verdict": "pass",
    })

    # 2. Deliberately tamper with tasks.json and workflows.json on disk with stale/conflicting data
    tampered_tasks = {
        "tasks": [
            {
                "task_id": tid,
                "workflow_id": wid,
                "node": "dev",
                "stage": "dev",
                "status": "working",  # Conflicting stale status!
                "stage_verdict": None,
            }
        ]
    }
    tampered_wfs = {
        "workflows": {
            wid: {
                "workflow_id": wid,
                "title": "Tampered Title",
                "status": "running",  # Conflicting stale status!
            }
        }
    }
    store_env["tasks_file"].write_text(json.dumps(tampered_tasks), encoding="utf-8")
    store_env["wf_file"].write_text(json.dumps(tampered_wfs), encoding="utf-8")

    # 3. Access state via kernel: must strictly return SQLite authoritative state
    k_tasks = kernel.load_tasks_data()
    assert len(k_tasks["tasks"]) == 1
    assert k_tasks["tasks"][0]["status"] == "completed"

    k_wfs = kernel.load_workflows_data()
    assert k_wfs["workflows"][wid]["status"] == "completed"

    # 4. Access state via services/herdr-controller.py
    ctrl_spec = importlib.util.spec_from_file_location("controller_mod", "services/herdr-controller.py")
    ctrl = importlib.util.module_from_spec(ctrl_spec)
    ctrl_spec.loader.exec_module(ctrl)

    ctrl_tasks = ctrl.load_tasks()
    assert len(ctrl_tasks) == 1
    assert ctrl_tasks[0]["status"] == "completed"
    ctrl_t = ctrl.get_task(tid)
    assert ctrl_t is not None
    assert ctrl_t["status"] == "completed"

    # 5. Access state via bin/herdr-task
    import importlib.machinery
    loader = importlib.machinery.SourceFileLoader("herdr_task_mod", str(Path("bin/herdr-task").resolve()))
    task_spec = importlib.util.spec_from_loader("herdr_task_mod", loader)
    task_bin = importlib.util.module_from_spec(task_spec)
    loader.exec_module(task_bin)

    bin_tasks = task_bin.load_tasks()
    assert bin_tasks.get("tasks", [])[0]["status"] == "completed"

    # 6. Verify SQLite itself remained 100% untainted
    fresh_task = store.get_task(tid)
    assert fresh_task["status"] == "completed"
    assert fresh_task["stage_verdict"] == "pass"

    fresh_wf = store.get_workflow(wid)
    assert fresh_wf["status"] == "completed"

    # 7. Verify JSON tampering never resurrects or imports new tasks into SQLite
    new_tid = "task-cold-boot-99"
    tampered_tasks["tasks"].append({
        "task_id": new_tid,
        "workflow_id": wid,
        "node": "test",
        "stage": "test",
        "status": "pending",
    })
    store_env["tasks_file"].write_text(json.dumps(tampered_tasks), encoding="utf-8")

    # Before and after load, SQLite remains authoritative and does NOT import from JSON
    assert store.get_task(new_tid) is None
    kernel.load_tasks_data()
    assert store.get_task(new_tid) is None


def test_herdr_task_cli_writes_directly_to_sqlite(store_env):
    """Verify that herdr-task CLI writes directly to SQLite as authoritative storage."""
    import subprocess

    store = get_state_store()

    wid = "wf-cli-01"
    tid = "task-cli-01"

    store.save_workflow({
        "workflow_id": wid,
        "title": "CLI Direct Write Workflow",
        "status": "running",
        "config": {"nodes": [{"id": "dev"}]},
    })
    store.save_task({
        "task_id": tid,
        "workflow_id": wid,
        "node": "dev",
        "stage": "dev",
        "status": "dispatched",
    })

    env = os.environ.copy()
    env["HERDR_STATE_DB"] = str(store_env["db_file"])
    env["TASKS_FILE"] = str(store_env["tasks_file"])
    env["WORKFLOWS_FILE"] = str(store_env["wf_file"])

    # 1. Transition task: dispatched -> working
    proc = subprocess.run(
        ["python3", "bin/herdr-task", "set", tid, "working"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"STDOUT: {proc.stdout}, STDERR: {proc.stderr}"
    assert f"{tid}: dispatched -> working" in proc.stdout

    # Assert SQLite database directly reflects 'working'
    t_after = store.get_task(tid)
    assert t_after is not None
    assert t_after["status"] == "working"
    assert t_after["started_at"] is not None
    assert len(t_after.get("status_history", [])) >= 1
    assert t_after["status_history"][-1]["to"] == "working"

    # 2. Transition task: working -> agent_done -> completed with verdict
    proc_done = subprocess.run(
        ["python3", "bin/herdr-task", "set", tid, "agent_done"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc_done.returncode == 0, f"STDOUT: {proc_done.stdout}, STDERR: {proc_done.stderr}"

    proc2 = subprocess.run(
        ["python3", "bin/herdr-task", "set", tid, "completed", "--verdict", "pass"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc2.returncode == 0, f"STDOUT: {proc2.stdout}, STDERR: {proc2.stderr}"

    # Assert SQLite database directly reflects 'completed' and 'stage_verdict'
    t_completed = store.get_task(tid)
    assert t_completed is not None
    assert t_completed["status"] == "completed"
    assert t_completed["stage_verdict"] == "pass"


def test_end_to_end_single_source_of_truth_without_workflows_json(store_env, monkeypatch):
    """End-to-end verification:
    1. Register workflow and mark startup ready via projects.py / herdr-factory APIs.
    2. Set agent override.
    3. Query SQLite directly (not workflows.json) and verify all fields are persisted.
    4. Physically DELETE workflows.json.
    5. agent_router continues to select agents correctly without workflows.json.
    6. controller continues to advance workflows without workflows.json.
    7. projects.py continues to query active workflows without workflows.json.
    """
    import importlib.util
    from herdr import projects, agent_router

    # 模块级 WORKFLOWS_FILE/TASKS_FILE 必须同步隔离，否则写入会穿透
    # 到线上 ~/.herdr-controller/workflows.json（曾覆盖 42 条真实工作流）。
    monkeypatch.setattr(projects, "WORKFLOWS_FILE", store_env["wf_file"])
    monkeypatch.setattr(agent_router, "WORKFLOWS_FILE", store_env["wf_file"])
    monkeypatch.setattr(agent_router, "TASKS_FILE", store_env["tasks_file"])

    store = get_state_store()

    # 真实工作流定义必须落在非夹具路径上（pytest-*/tmp 定义会被 Liveness Guard
    # 判定为夹具残留并排除出调度，这正是 73k 次空转事故的护栏）。
    live_dir = tempfile.TemporaryDirectory(prefix="herdr-e2e-live-")
    live_workflow_file = Path(live_dir.name) / "dummy_workflow.json"

    proj = {
        "project_id": "proj-e2e-test",
        "project_name": "e2e-project",
        "project_root": "/tmp/e2e-root",
        "base_branch": "main",
        "workspace_id": "ws-e2e-1",
        "coordinator_pane_id": "pane-coord-1",
        "workflow_file": str(live_workflow_file),
    }
    live_workflow_file.write_text(json.dumps({
        "workflow_template": "universal_sdlc",
        "nodes": [
            {"id": "requirements", "label": "2需求分析"},
            {"id": "plan", "label": "3计划", "depends_on": ["requirements"]},
        ]
    }), encoding="utf-8")

    wid = "wf-e2e-no-json-01"

    # 1. Register workflow via projects.py
    projects.register_workflow(wid, proj, requirement="测试纯 SQLite 唯一事实源", title="E2E No JSON Test")

    # 2. Mark startup ready with healthy agents
    projects.mark_workflow_startup_ready(
        wid,
        healthy_agents=["codex", "claude"],
        unhealthy_agents={"pi": "TIMEOUT"}
    )

    # 3. Set workflow agent override via agent_router
    agent_router.set_workflow_agent_override(wid, "codex")

    # 4. Directly query SQLite StateStore WITHOUT reading workflows.json
    wf_in_sqlite = store.get_workflow(wid)
    assert wf_in_sqlite is not None
    assert wf_in_sqlite["workflow_id"] == wid
    assert wf_in_sqlite["project_id"] == "proj-e2e-test"
    assert wf_in_sqlite["startup_ready"] is True
    assert wf_in_sqlite["healthy_agents"] == ["codex", "claude"]
    assert wf_in_sqlite["unhealthy_agents"] == {"pi": "TIMEOUT"}
    assert wf_in_sqlite["agent_override"] == "codex"

    # 5. Physically delete workflows.json from disk!
    if store_env["wf_file"].exists():
        store_env["wf_file"].unlink()
    assert not store_env["wf_file"].exists()

    # 6. agent_router continues to work normally without workflows.json
    chosen = agent_router.choose_agent(wid, "requirements", "feat")
    assert chosen == "codex"  # Due to agent_override="codex" in SQLite

    # Change agent override in SQLite and choose again
    agent_router.set_workflow_agent_override(wid, "claude")
    if store_env["wf_file"].exists():
        store_env["wf_file"].unlink()
    assert not store_env["wf_file"].exists()

    chosen2 = agent_router.choose_agent(wid, "requirements", "feat")
    assert chosen2 == "claude"

    # 7. projects.py queries continue to work normally without workflows.json
    proj_rec = projects.project_for_workflow(wid)
    assert proj_rec is not None
    assert proj_rec["workflow_id"] == wid
    assert proj_rec["project_id"] == "proj-e2e-test"

    active_wfs = projects.active_workflows_for_project("proj-e2e-test")
    assert len(active_wfs) == 1
    assert active_wfs[0]["workflow_id"] == wid

    # 8. services/herdr-controller.py continues to work normally without workflows.json
    ctrl_spec = importlib.util.spec_from_file_location("controller_e2e_mod", "services/herdr-controller.py")
    ctrl = importlib.util.module_from_spec(ctrl_spec)
    ctrl_spec.loader.exec_module(ctrl)
    active_reg = ctrl.active_registered_workflows()
    assert wid in active_reg

    # 8b. 夹具残留(pytest-* 路径)必须被排除出调度 sweep（Liveness Guard 护栏）。
    residue_proj = dict(proj)
    residue_proj["project_id"] = "proj-e2e-residue"
    residue_proj["workflow_file"] = str(store_env["cp_dir"] / "dummy_workflow.json")
    Path(residue_proj["workflow_file"]).write_text("{}", encoding="utf-8")
    projects.register_workflow(
        "wf-pytest-residue-01", residue_proj,
        requirement="夹具残留", title="Residue",
    )
    assert "wf-pytest-residue-01" not in ctrl.active_registered_workflows()

    wf_entry = ctrl._workflow_entry(wid)
    assert wf_entry.get("workflow_id") == wid
    assert wf_entry.get("agent_override") == "claude"

    # 9. bin/herdr-factory functions work directly with StateStore
    import importlib.machinery
    factory_loader = importlib.machinery.SourceFileLoader("factory_e2e_mod", str(Path("bin/herdr-factory").resolve()))
    factory_spec = importlib.util.spec_from_loader("factory_e2e_mod", factory_loader)
    factory = importlib.util.module_from_spec(factory_spec)
    factory_loader.exec_module(factory)

    store.save_task({
        "task_id": "task-factory-e2e",
        "workflow_id": wid,
        "status": "working",
    })
    wf_tasks = factory.tasks_for(wid)
    assert len(wf_tasks) == 1
    assert wf_tasks[0]["task_id"] == "task-factory-e2e"

    factory.pause_workflow(wid)
    assert store.get_workflow(wid)["status"] == "paused"
    factory.resume_workflow(wid)
    assert store.get_workflow(wid)["status"] == "running"

    live_dir.cleanup()


def test_workflow_writes_never_touch_real_projection_without_global_patch(store_env):
    """回归：仅靠环境变量隔离时，写操作不得穿透到线上 workflows.json。

    曾发生真实事故：e2e 测试只设了 WORKFLOWS_FILE 环境变量、
    未 patch 模块全局量，projects.register_workflow 直写
    ~/.herdr-controller/workflows.json，把 42 条真实工作流覆盖成 1 条。
    修复后所有写操作必须经 sync_workflows_projection（环境变量感知）。
    """
    from pathlib import Path as _Path
    from herdr import projects, agent_router

    # 刻意不 patch 模块全局量，只依赖环境变量（store_env 已设置）。
    assert _Path(os.environ["WORKFLOWS_FILE"]) == store_env["wf_file"]

    real_path = _Path.home() / ".herdr-controller" / "workflows.json"
    real_before = real_path.read_bytes() if real_path.exists() else None

    proj = {
        "project_id": "proj-isolation-probe",
        "project_name": "probe",
        "project_root": "/tmp/probe",
        "base_branch": "main",
        "workspace_id": "ws-probe",
        "coordinator_pane_id": "ws-probe:p1",
        "workflow_file": str(store_env["cp_dir"] / "probe_workflow.json"),
    }
    _Path(proj["workflow_file"]).write_text(json.dumps({"nodes": []}), encoding="utf-8")

    projects.register_workflow("wf-isolation-probe", proj, requirement="probe", title="probe")
    projects.mark_workflow_startup_ready("wf-isolation-probe", healthy_agents=["codex"])
    agent_router.set_workflow_agent_override("wf-isolation-probe", "codex")

    # 线上文件必须字节级不变。
    if real_before is None:
        assert not real_path.exists()
    else:
        assert real_path.read_bytes() == real_before
        assert b"wf-isolation-probe" not in real_path.read_bytes()

    # 沙盒投影必须包含新工作流。
    sandbox = json.loads(store_env["wf_file"].read_text(encoding="utf-8"))
    assert "wf-isolation-probe" in sandbox.get("workflows", {})


def test_fail_closed_prevents_silent_write_loss_when_statestore_fails(tmp_path, monkeypatch):
    """Runtime write operations must fail closed if StateStore fails.

    Under NO circumstances should a failed StateStore write silently proceed to
    write JSON mirror, which would cause Silent Write Loss.
    """
    import sqlite3
    from unittest.mock import patch
    from herdr.state_store import get_state_store
    from herdr import projects
    from herdr import agent_router

    db_path = tmp_path / "state.db"
    wf_file = tmp_path / "workflows.json"
    tasks_file = tmp_path / "tasks.json"

    monkeypatch.setenv("HERDR_STATE_DB", str(db_path))
    monkeypatch.setenv("WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setenv("TASKS_FILE", str(tasks_file))
    monkeypatch.setattr(projects, "WORKFLOWS_FILE", wf_file)
    monkeypatch.setattr(agent_router, "WORKFLOWS_FILE", wf_file)
    monkeypatch.setattr(agent_router, "TASKS_FILE", tasks_file)

    store = get_state_store(db_path=db_path)
    wid = "wf-test-fail-closed"
    initial_wf = {
        "workflow_id": wid,
        "title": "Fail Closed Test",
        "status": "running",
        "project_id": "p1",
        "agent_override": "codex",
    }
    store.save_workflow(initial_wf)
    wf_file.write_text(json.dumps({"workflows": {wid: initial_wf}}), encoding="utf-8")

    simulated_err = sqlite3.OperationalError("database is locked / disk I/O error")

    # 1. projects.save_workflows must fail closed
    with patch.object(store, "save_workflow", side_effect=simulated_err):
        with pytest.raises(sqlite3.OperationalError):
            projects.save_workflows({"workflows": {wid: {"workflow_id": wid, "status": "corrupted"}}})
        disk_data = json.loads(wf_file.read_text(encoding="utf-8"))
        assert disk_data["workflows"][wid]["status"] == "running"

    # 2. projects.register_workflow must fail closed
    with patch.object(store, "save_workflow", side_effect=simulated_err):
        dummy_proj = {
            "project_id": "p1",
            "project_name": "p1",
            "project_root": "/tmp/p1",
            "workspace_id": "w1",
            "coordinator_pane_id": "w1:p1",
            "workflow_file": "workflow.yaml",
        }
        with pytest.raises(sqlite3.OperationalError):
            projects.register_workflow("wf-uncommitted", dummy_proj)
        disk_data = json.loads(wf_file.read_text(encoding="utf-8"))
        assert "wf-uncommitted" not in disk_data["workflows"]

    # 3. agent_router.set_workflow_agent_override must fail closed
    with patch.object(store, "save_workflow", side_effect=simulated_err):
        with pytest.raises(sqlite3.OperationalError):
            agent_router.set_workflow_agent_override(wid, "claude")
        disk_data = json.loads(wf_file.read_text(encoding="utf-8"))
        assert disk_data["workflows"][wid]["agent_override"] == "codex"

    # 4. bin/herdr-factory _update_workflow_status must fail closed
    import importlib.machinery
    factory_loader = importlib.machinery.SourceFileLoader("factory_fail_closed_mod", str(Path("bin/herdr-factory").resolve()))
    factory_spec = importlib.util.spec_from_loader("factory_fail_closed_mod", factory_loader)
    factory = importlib.util.module_from_spec(factory_spec)
    factory_loader.exec_module(factory)
    monkeypatch.setattr(factory, "WORKFLOWS_FILE", wf_file)

    with patch.object(store, "transition_workflow", side_effect=simulated_err), \
         patch.object(store, "save_workflow", side_effect=simulated_err):
        with pytest.raises(sqlite3.OperationalError):
            factory._update_workflow_status(wid, "paused", "paused")
        disk_data = json.loads(wf_file.read_text(encoding="utf-8"))
        assert disk_data["workflows"][wid]["status"] == "running"

    # 5. projects.mark_workflow_startup_ready must fail closed
    with patch.object(store, "save_workflow", side_effect=simulated_err):
        with pytest.raises(sqlite3.OperationalError):
            projects.mark_workflow_startup_ready(wid, healthy_agents=["codex"])
        disk_data = json.loads(wf_file.read_text(encoding="utf-8"))
        assert disk_data["workflows"][wid].get("startup_ready") is not True

    # 6. bin/herdr-factory run_workflow_preflight must fail closed
    dummy_proc = type("Proc", (), {
        "returncode": 0,
        "stdout": json.dumps({"agents": [{"agent": "codex", "final_status": "READY"}]}),
        "stderr": "",
    })()
    with patch("subprocess.run", return_value=dummy_proc):
        with patch.object(store, "save_workflow", side_effect=simulated_err):
            with pytest.raises(sqlite3.OperationalError):
                factory.run_workflow_preflight({"project_id": "p1"}, wid)
            disk_data = json.loads(wf_file.read_text(encoding="utf-8"))
            assert "preflight_checked_at" not in disk_data["workflows"][wid]



