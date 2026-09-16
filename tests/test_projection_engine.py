"""Tests for the Telemetry Projection Engine (herdr/projection.py)."""

import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from herdr import projection

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def projection_env(tmp_path, monkeypatch):
    tasks_file = tmp_path / "tasks.json"
    workflows_file = tmp_path / "workflows.json"
    clone_dir = tmp_path / "clone_1"
    clone_dir.mkdir(parents=True, exist_ok=True)

    # Initialize a mock git repo in clone_dir
    subprocess.run(["git", "init"], cwd=str(clone_dir), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(clone_dir), check=True)
    subprocess.run(["git", "config", "user.email", "tester@test.com"], cwd=str(clone_dir), check=True)
    (clone_dir / "README.md").write_text("# Initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(clone_dir), check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(clone_dir), check=True)

    monkeypatch.setenv("TASKS_FILE", str(tasks_file))
    monkeypatch.setenv("WORKFLOWS_FILE", str(workflows_file))

    tasks_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")
    workflows_file.write_text(json.dumps({"workflows": {}}), encoding="utf-8")

    return {
        "tasks_file": tasks_file,
        "workflows_file": workflows_file,
        "clone_dir": clone_dir,
    }


def _seed_task_with_clone(env, task_id, status="working", goal="Implement cache expiration", pane_id="pane-proj-1"):
    t_data = json.loads(env["tasks_file"].read_text(encoding="utf-8"))
    task = {
        "task_id": task_id,
        "workflow_id": "wf-proj-1",
        "status": status,
        "goal": goal,
        "pane_id": pane_id,
        "clone_path": str(env["clone_dir"]),
        "agent": "codex",
        "node": "impl",
    }
    t_data["tasks"].append(task)
    env["tasks_file"].write_text(json.dumps(t_data), encoding="utf-8")
    return task


def test_strip_ansi_codes():
    raw = "\x1b[31;1mError:\x1b[0m File not found\r\n\x1b[2K\x1b[1A\x1b]0;Terminal Title\x07Done"
    clean = projection.strip_ansi_codes(raw)
    assert "\x1b" not in clean
    assert "Error: File not found" in clean
    assert "Done" in clean


def test_extract_task_intent():
    task = {
        "goal": "Refactor database migrations to support zero downtime",
        "status": "working",
    }
    # Case 1: with explicit terminal intent marker
    terminal_with_marker = "Some logs...\n[HERDR_INTENT] Testing rolling schema changes\nMore logs..."
    intent = projection.extract_task_intent(task, terminal_with_marker)
    assert intent == "Testing rolling schema changes"

    # Case 2: fallback to task goal
    terminal_plain = "Compiling assets...\nDone in 2.3s\n"
    intent_fallback = projection.extract_task_intent(task, terminal_plain)
    assert intent_fallback == "Refactor database migrations to support zero downtime"


def test_extract_task_milestones(projection_env):
    task = _seed_task_with_clone(projection_env, "t_milestones", status="working")
    clone_dir = projection_env["clone_dir"]
    loop_dir = clone_dir / ".herdr-loop"
    loop_dir.mkdir(exist_ok=True)

    # Mock .herdr-loop/STATE.md
    state_md = (
        "# Loop State\n"
        "iteration: 2\n"
        "status: working\n"
        "last_eval_score: 80.0\n"
    )
    (loop_dir / "STATE.md").write_text(state_md, encoding="utf-8")

    milestones = projection.extract_task_milestones(task, "Running tests...")
    assert len(milestones) >= 3
    labels = [m["label"] for m in milestones]
    assert any("锁定验收目标" in l for l in labels)
    assert any("核心代码实现" in l for l in labels)


def test_collect_task_artifacts(projection_env):
    clone_dir = projection_env["clone_dir"]
    task = _seed_task_with_clone(projection_env, "t_artifacts", status="working")

    # Mutate files in clone
    (clone_dir / "app.py").write_text("print('hello world')\n", encoding="utf-8")
    (clone_dir / "README.md").write_text("# Updated Title\nNew features here.\n", encoding="utf-8")

    # Add .herdr-loop/EVALUATION.md
    loop_dir = clone_dir / ".herdr-loop"
    loop_dir.mkdir(exist_ok=True)
    eval_content = (
        "# Evaluation Report\n\n"
        "**Overall Score:** 92.5 / 100.0\n"
        "**Status:** PASSED\n"
        "Summary: All unit tests succeeded.\n"
    )
    (loop_dir / "EVALUATION.md").write_text(eval_content, encoding="utf-8")

    artifacts = projection.collect_task_artifacts(task)
    assert len(artifacts) >= 2

    # Check git diff artifact
    diff_art = next((a for a in artifacts if a["kind"] == "diff"), None)
    assert diff_art is not None
    assert "app.py" in diff_art["files"]
    assert diff_art["files_changed"] >= 1

    # Check evaluation report artifact
    eval_art = next((a for a in artifacts if a["kind"] == "evaluation"), None)
    assert eval_art is not None
    assert eval_art["score"] == 92.5
    assert eval_art["passed"] is True


def test_project_task(projection_env):
    task_id = "t_project_full"
    task = _seed_task_with_clone(projection_env, task_id, status="working")

    mock_read = MagicMock(return_value="[HERDR_INTENT] Implementing cache engine\nWriting tests...\n")
    with patch("herdr.projection._read_pane_content", mock_read):
        proj = projection.project_task(task_id)

    assert proj["task_id"] == task_id
    assert proj["status"] == "working"
    assert proj["intent"] == "Implementing cache engine"
    assert "milestones" in proj
    assert "artifacts" in proj
    assert proj["blocker"] is None
    assert proj["blockers"] == []
    assert isinstance(proj["recent_activity"], list)
    assert len(proj["recent_activity"]) >= 1


def test_extract_task_blockers():
    # 1. State-based blocker
    task_blocked = {"status": "blocked", "sentinel_reason": "No disk space"}
    b1 = projection.extract_task_blockers(task_blocked, "")
    assert "No disk space" in b1

    # 2. Terminal marker blocker
    task_normal = {"status": "working"}
    term_marker = "Log 1\n[BLOCKER] Missing AWS credentials\nLog 2"
    b2 = projection.extract_task_blockers(task_normal, term_marker)
    assert "Missing AWS credentials" in b2

    # 3. Crash signature blocker
    term_crash = "Traceback (most recent call last):\n  File 'a.py'\nModuleNotFoundError: No module named 'foobar'"
    b3 = projection.extract_task_blockers(task_normal, term_crash)
    assert any("依赖缺失: ModuleNotFoundError" in b for b in b3)


def test_project_workflow(projection_env):
    wid = "wf-proj-1"
    w_data = json.loads(projection_env["workflows_file"].read_text(encoding="utf-8"))
    w_data["workflows"][wid] = {
        "workflow_id": wid,
        "status": "running",
        "config": {"nodes": [{"id": "impl", "label": "开发实现"}]},
    }
    projection_env["workflows_file"].write_text(json.dumps(w_data), encoding="utf-8")

    _seed_task_with_clone(projection_env, "t_wf_1", status="completed")
    _seed_task_with_clone(projection_env, "t_wf_2", status="working")

    wf_proj = projection.project_workflow(wid)
    assert wf_proj["workflow_id"] == wid
    assert wf_proj["status"] == "running"
    assert len(wf_proj["tasks"]) == 2
    assert wf_proj["progress"]["total_tasks"] == 2
    assert wf_proj["progress"]["completed_tasks"] == 1


def test_cli_project_and_artifacts(projection_env):
    task_id = "t_cli_test"
    _seed_task_with_clone(projection_env, task_id, status="working")

    env = os.environ.copy()
    env["TASKS_FILE"] = str(projection_env["tasks_file"])
    env["WORKFLOW_FILE"] = str(projection_env["workflows_file"])

    cmd_proj = [str(ROOT / "bin" / "herdr-task"), "project", task_id, "--json"]
    r = subprocess.run(cmd_proj, env=env, capture_output=True, text=True)
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert data["task_id"] == task_id
    assert "milestones" in data

    cmd_art = [str(ROOT / "bin" / "herdr-task"), "artifacts", task_id]
    r_art = subprocess.run(cmd_art, env=env, capture_output=True, text=True)
    assert r_art.returncode == 0
    assert "任务产物清单" in r_art.stdout


def test_detect_workflow_stalls_rework_orphan(projection_env):
    now = time.time()
    tasks = [
        {
            "task_id": "t_rework_stall",
            "workflow_id": "wf_stall_1",
            "status": "rework",
            "updated_at": now - 60,
        }
    ]
    stall = projection.detect_workflow_stalls("wf_stall_1", tasks)
    assert stall["is_stalled"] is True
    assert stall["stall_type"] == "rework_orphan"
    assert stall["target_task_id"] == "t_rework_stall"
    assert stall["suggested_action"] == "force_review"
    assert "t_rework_stall" in stall["message"]


def test_detect_workflow_stalls_stage_advance_hang(projection_env):
    now = time.time()
    tasks = [
        {
            "task_id": "t_done_1",
            "workflow_id": "wf_hang_1",
            "status": "cleaned",
            "updated_at": now - 70,
        }
    ]
    stall = projection.detect_workflow_stalls("wf_hang_1", tasks)
    assert stall["is_stalled"] is True
    assert stall["stall_type"] == "stage_advance_hang"
    assert stall["suggested_action"] == "retry_advance"
    assert stall["target_task_id"] is None


def test_detect_workflow_stalls_normal_working(projection_env):
    now = time.time()
    tasks = [
        {
            "task_id": "t_working_normal",
            "workflow_id": "wf_normal_1",
            "status": "working",
            "updated_at": now - 10,
        }
    ]
    stall = projection.detect_workflow_stalls("wf_normal_1", tasks)
    assert stall["is_stalled"] is False
    assert stall["stall_type"] is None


def test_detect_workflow_stalls_completed_workflow(projection_env):
    now = time.time()
    tasks = [
        {
            "task_id": "t_done_1",
            "workflow_id": "wf_completed_1",
            "stage": "requirements",
            "status": "cleaned",
            "updated_at": now - 120,
        }
    ]
    # Explicit workflow dict
    stall = projection.detect_workflow_stalls(
        "wf_completed_1",
        tasks,
        workflow={"workflow_id": "wf_completed_1", "status": "completed", "outcome": "delivered"},
    )
    assert stall["is_stalled"] is False
    assert stall["stall_type"] is None


def test_detect_workflow_stalls_delivered_outcome(projection_env):
    now = time.time()
    tasks = [
        {
            "task_id": "t_done_2",
            "workflow_id": "wf_delivered_1",
            "stage": "plan",
            "status": "cleaned",
            "updated_at": now - 200,
        }
    ]
    stall = projection.detect_workflow_stalls(
        "wf_delivered_1",
        tasks,
        workflow={"workflow_id": "wf_delivered_1", "outcome": "delivered"},
    )
    assert stall["is_stalled"] is False
    assert stall["stall_type"] is None


def test_detect_workflow_stalls_paused_workflow(projection_env):
    now = time.time()
    tasks = [
        {
            "task_id": "t_done_3",
            "workflow_id": "wf_paused_1",
            "stage": "implementation",
            "status": "cleaned",
            "updated_at": now - 100,
        }
    ]
    stall = projection.detect_workflow_stalls(
        "wf_paused_1",
        tasks,
        workflow={"workflow_id": "wf_paused_1", "status": "paused"},
    )
    assert stall["is_stalled"] is False
    assert stall["stall_type"] is None


def test_detect_workflow_stalls_wrapup_stage_completed(projection_env):
    now = time.time()
    tasks = [
        {
            "task_id": "t_wrapup_1",
            "workflow_id": "wf_wrapup_1",
            "stage": "wrapup",
            "status": "cleaned",
            "updated_at": now - 300,
        }
    ]
    # Even without explicit workflow metadata, wrapup stage completion indicates workflow finished
    stall = projection.detect_workflow_stalls("wf_wrapup_1", tasks)
    assert stall["is_stalled"] is False
    assert stall["stall_type"] is None


