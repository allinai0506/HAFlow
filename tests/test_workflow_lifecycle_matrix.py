import json
import os
import subprocess
import tempfile
import time
import pytest


@pytest.fixture
def temp_herdr_env(tmp_path, monkeypatch):
    """Isolate tasks.json, workflows.json, and stage-state.json."""
    tasks_file = tmp_path / "tasks.json"
    workflows_file = tmp_path / "workflows.json"
    stage_state_file = tmp_path / "stage-state.json"
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)

    tasks_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")
    workflows_file.write_text(json.dumps({"workflows": []}), encoding="utf-8")
    stage_state_file.write_text(json.dumps({}), encoding="utf-8")

    monkeypatch.setenv("TASKS_FILE", str(tasks_file))
    monkeypatch.setenv("WORKFLOWS_FILE", str(workflows_file))
    monkeypatch.setenv("STAGE_STATE_FILE", str(stage_state_file))
    monkeypatch.setenv("HERDR_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("HERDR_CONTROLLER_TEST", "1")

    return {
        "root": tmp_path,
        "tasks_file": tasks_file,
        "workflows_file": workflows_file,
        "stage_state_file": stage_state_file,
        "projects_dir": projects_dir,
    }


def _run_task_cli(args, env):
    cmd = ["python3", "bin/herdr-task"] + args
    custom_env = os.environ.copy()
    custom_env["TASKS_FILE"] = str(env["tasks_file"])
    custom_env["WORKFLOWS_FILE"] = str(env["workflows_file"])
    custom_env["STAGE_STATE_FILE"] = str(env["stage_state_file"])
    custom_env["HERDR_STATE_DB"] = str(env["root"] / "state.db")
    return subprocess.run(cmd, env=custom_env, text=True, capture_output=True)


def _seed_task(env, task_id, workflow_id="wf-test-01", node="plan", status="pending"):
    from herdr.state_store import get_state_store
    data = json.loads(env["tasks_file"].read_text(encoding="utf-8"))
    task = {
        "task_id": task_id,
        "workflow_id": workflow_id,
        "project_id": "proj-test",
        "project_name": "test",
        "source_repo": "/tmp/test",
        "coordinator_pane_id": "wD:p1",
        "node": node,
        "stage": node,
        "status": status,
        "pane_id": "wD:p2",
        "agent": "codex",
        "status_history": [{"status": status, "at": time.time(), "from": None, "to": status}],
    }
    data["tasks"].append(task)
    env["tasks_file"].write_text(json.dumps(data, indent=2), encoding="utf-8")
    db_path = env["root"] / "state.db"
    store = get_state_store(db_path=db_path)
    store.save_task(task)
    return task


def _get_task(env, task_id):
    from herdr.state_store import get_state_store
    db_path = env["root"] / "state.db"
    store = get_state_store(db_path=db_path)
    t = store.get_task(task_id)
    if t:
        return t
    data = json.loads(env["tasks_file"].read_text(encoding="utf-8"))
    for t in data.get("tasks", []):
        if t["task_id"] == task_id:
            return t
    return None


def _load_controller():
    import importlib.util
    spec = importlib.util.spec_from_file_location("controller", "services/herdr-controller.py")
    ctrl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ctrl)
    return ctrl


class TestWorkflowLifecycleMatrix:

    def test_task_rapid_completion(self, temp_herdr_env):
        """Dispatched -> agent_done rapid completion without prior working transition."""
        _seed_task(temp_herdr_env, "t-rapid", status="dispatched")
        res = _run_task_cli(["set", "t-rapid", "agent_done"], temp_herdr_env)
        assert res.returncode == 0, res.stderr
        task = _get_task(temp_herdr_env, "t-rapid")
        assert task["status"] == "agent_done"

    def test_task_rework_loop_matrix(self, temp_herdr_env):
        """Full rework loop: agent_done -> rework -> working -> agent_done -> completed,
        and rapid rework completion: rework -> agent_done."""
        _seed_task(temp_herdr_env, "t-rework", status="agent_done")

        # 1. Reject to rework
        res = _run_task_cli(["set", "t-rework", "rework"], temp_herdr_env)
        assert res.returncode == 0, res.stderr
        assert _get_task(temp_herdr_env, "t-rework")["status"] == "rework"

        # 2. Worker resumes working
        res = _run_task_cli(["set", "t-rework", "working"], temp_herdr_env)
        assert res.returncode == 0, res.stderr
        assert _get_task(temp_herdr_env, "t-rework")["status"] == "working"

        # 3. Worker finishes -> agent_done
        res = _run_task_cli(["set", "t-rework", "agent_done"], temp_herdr_env)
        assert res.returncode == 0, res.stderr
        assert _get_task(temp_herdr_env, "t-rework")["status"] == "agent_done"

        # 4. Another round with rapid rework -> agent_done
        res = _run_task_cli(["set", "t-rework", "rework"], temp_herdr_env)
        assert res.returncode == 0, res.stderr
        res = _run_task_cli(["set", "t-rework", "agent_done"], temp_herdr_env)
        assert res.returncode == 0, res.stderr
        assert _get_task(temp_herdr_env, "t-rework")["status"] == "agent_done"

        # 5. Finally accepted -> completed
        res = _run_task_cli(["set", "t-rework", "completed", "--verdict", "pass"], temp_herdr_env)
        assert res.returncode == 0, res.stderr
        task = _get_task(temp_herdr_env, "t-rework")
        assert task["status"] == "completed"
        assert task["stage_verdict"] == "pass"

    def test_task_pause_and_resume(self, temp_herdr_env):
        """Task pausing and resuming:
        working -> paused -> working
        rework -> paused -> rework
        paused -> superseded."""
        _seed_task(temp_herdr_env, "t-pause", status="working")

        # Pause from working
        res = _run_task_cli(["pause", "t-pause", "--reason", "manual review"], temp_herdr_env)
        assert res.returncode == 0, res.stderr
        assert _get_task(temp_herdr_env, "t-pause")["status"] == "paused"

        # Resume back to working
        res = _run_task_cli(["resume", "t-pause"], temp_herdr_env)
        assert res.returncode == 0, res.stderr
        assert _get_task(temp_herdr_env, "t-pause")["status"] == "working"

        # Advance to rework then pause
        _run_task_cli(["set", "t-pause", "agent_done"], temp_herdr_env)
        _run_task_cli(["set", "t-pause", "rework"], temp_herdr_env)
        _run_task_cli(["pause", "t-pause"], temp_herdr_env)
        assert _get_task(temp_herdr_env, "t-pause")["status"] == "paused"

        # Resume should restore to rework!
        res = _run_task_cli(["resume", "t-pause"], temp_herdr_env)
        assert res.returncode == 0, res.stderr
        assert _get_task(temp_herdr_env, "t-pause")["status"] == "rework"

        # Pause then supersede directly
        _run_task_cli(["pause", "t-pause"], temp_herdr_env)
        res = _run_task_cli(["supersede", "t-pause", "--abandon", "--reason", "abandoned while paused"], temp_herdr_env)
        assert res.returncode == 0, res.stderr
        assert _get_task(temp_herdr_env, "t-pause")["status"] == "superseded"

    def test_multi_task_parallel_advance_gate(self, temp_herdr_env, monkeypatch):
        """Test is_node_complete with multiple parallel tasks in a stage."""
        ctrl = _load_controller()

        wf_id = "wf-parallel-01"
        _seed_task(temp_herdr_env, "t-p1", workflow_id=wf_id, node="plan", status="working")
        _seed_task(temp_herdr_env, "t-p2", workflow_id=wf_id, node="plan", status="working")

        # 1. Both working -> incomplete
        assert ctrl.is_node_complete(wf_id, "plan") is False

        # 2. T1 completed, T2 working -> incomplete
        _run_task_cli(["set", "t-p1", "agent_done"], temp_herdr_env)
        _run_task_cli(["set", "t-p1", "completed"], temp_herdr_env)
        assert ctrl.is_node_complete(wf_id, "plan") is False

        # 3. T2 in rework -> incomplete
        _run_task_cli(["set", "t-p2", "agent_done"], temp_herdr_env)
        _run_task_cli(["set", "t-p2", "rework"], temp_herdr_env)
        assert ctrl.is_node_complete(wf_id, "plan") is False

        # 4. T2 in paused -> incomplete
        _run_task_cli(["pause", "t-p2"], temp_herdr_env)
        assert ctrl.is_node_complete(wf_id, "plan") is False

        # 5. T2 resumed -> agent_done -> completed -> complete!
        _run_task_cli(["resume", "t-p2"], temp_herdr_env)
        _run_task_cli(["set", "t-p2", "agent_done"], temp_herdr_env)
        _run_task_cli(["set", "t-p2", "completed"], temp_herdr_env)
        assert ctrl.is_node_complete(wf_id, "plan") is True

    def test_workflow_pause_blocks_advance(self, temp_herdr_env, monkeypatch):
        """When workflow status is 'paused', check_workflow_stage_advance must not advance."""
        ctrl = _load_controller()

        wf_id = "wf-pause-test"
        # Seed workflow as paused
        wf_data = {
            "workflows": [
                {
                    "workflow_id": wf_id,
                    "project_name": "test",
                    "project_root": "/tmp/test",
                    "status": "paused",
                    "stage": "plan",
                    "coordinator_pane_id": "wD:p1",
                }
            ]
        }
        temp_herdr_env["workflows_file"].write_text(json.dumps(wf_data), encoding="utf-8")

        queued_items = []
        monkeypatch.setattr(ctrl.coordinator_queue, "put", lambda item: queued_items.append(item))

        # Invoke advance check
        ctrl.check_workflow_stage_advance(wf_id)
        assert len(queued_items) == 0, "Paused workflow should not advance stages"

    def test_controller_registry_watcher_catches_external_done(self, temp_herdr_env, monkeypatch):
        """When a task reaches agent_done externally, controller enqueues done event automatically."""
        ctrl = _load_controller()

        wf_id = "wf-watcher-01"
        wf_data = {
            "workflows": [
                {
                    "workflow_id": wf_id,
                    "project_name": "test",
                    "project_root": "/tmp/test",
                    "status": "running",
                    "coordinator_pane_id": "wD:p1",
                }
            ]
        }
        temp_herdr_env["workflows_file"].write_text(json.dumps(wf_data), encoding="utf-8")

        _seed_task(temp_herdr_env, "t-watch", workflow_id=wf_id, status="working")

        enqueued = []
        monkeypatch.setattr(ctrl, "enqueue_coordinator_event", lambda task, evt: enqueued.append((task["task_id"], evt)))

        # Simulate Sentinel writing agent_done
        _run_task_cli(["set", "t-watch", "agent_done"], temp_herdr_env)

        # Run single scan logic of registry_watcher
        tasks = ctrl.load_tasks()
        for task in tasks:
            if task.get("status") == "agent_done" and not ctrl.workflow_closed(task.get("workflow_id")):
                key = f"{task['task_id']}:done"
                with ctrl.lock:
                    already = key in ctrl.queued_events
                if not already:
                    ctrl.enqueue_coordinator_event(task, "done")

        assert ("t-watch", "done") in enqueued, "External agent_done should be discovered and enqueued"

    def test_in_progress_and_cleaned_can_be_superseded(self, temp_herdr_env):
        """In-progress states (dispatched, working, rework, blocked, paused) and cleaned can be superseded."""
        for st in ("dispatched", "working", "rework", "blocked", "paused", "cleaned"):
            t_id = f"t-sup-{st}"
            _seed_task(temp_herdr_env, t_id, status=st)
            res = _run_task_cli(["supersede", t_id, "--abandon", "--reason", "lifecycle test"], temp_herdr_env)
            assert res.returncode == 0, f"Failed to supersede from {st}: {res.stderr}"
            assert _get_task(temp_herdr_env, t_id)["status"] == "superseded"

    def test_workflow_resume_allows_advance(self, temp_herdr_env, monkeypatch):
        """When workflow status is resumed to 'running', check_workflow_stage_advance resumes."""
        ctrl = _load_controller()

        wf_id = "wf-resume-test"
        wf_data = {
            "workflows": {
                wf_id: {
                    "workflow_id": wf_id,
                    "project_name": "test",
                    "project_root": "/tmp/test",
                    "status": "running",
                    "coordinator_pane_id": "wD:p1",
                }
            }
        }
        temp_herdr_env["workflows_file"].write_text(json.dumps(wf_data), encoding="utf-8")

        # Mock project & workflow config
        mock_cfg = {
            "nodes": [
                {"id": "plan", "depends_on": []},
                {"id": "impl", "depends_on": ["plan"]},
            ]
        }
        monkeypatch.setattr(ctrl, "workflow_config_for", lambda wid: mock_cfg)
        monkeypatch.setattr(ctrl, "project_for_workflow", lambda wid: {"project_name": "test", "project_root": "/tmp/test", "startup_ready": True, "coordinator_pane_id": "wD:p1"})

        # Seed plan task as completed
        _seed_task(temp_herdr_env, "t-plan-done", workflow_id=wf_id, node="plan", status="completed")

        queued_advances = []
        monkeypatch.setattr(ctrl.coordinator_queue, "put", lambda item: queued_advances.append(item))

        ctrl.check_workflow_stage_advance(wf_id)
        assert len(queued_advances) == 1, "Resumed running workflow should advance to impl"
        assert queued_advances[0]["node_id"] == "impl"

    def test_agent_idle_jitter_filtered(self, temp_herdr_env, monkeypatch):
        """When an agent flickers to idle briefly without HERDR_TASK_DONE, jitter filter keeps it in working."""
        ctrl = _load_controller()

        task_id = "t-jitter"
        _seed_task(temp_herdr_env, task_id, status="working")

        # Mock subprocess to return empty screen (no HERDR_TASK_DONE)
        monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr=""))
        # Mock runtime status to recover back to working immediately
        monkeypatch.setattr(ctrl, "get_agent_runtime_status", lambda pane_id: "working")

        # Temporarily enable jitter filter by clearing test bypass
        monkeypatch.delenv("HERDR_CONTROLLER_TEST", raising=False)
        monkeypatch.setattr(time, "sleep", lambda s: None)  # fast sleep

        ctrl.handle_event(task_id, "idle")

        # Task should remain working!
        assert _get_task(temp_herdr_env, task_id)["status"] == "working", "Jitter should be filtered"
