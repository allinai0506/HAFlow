"""Tests for the Worker Intervention & Steering Mesh (herdr/steering.py)."""

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from herdr import steering


@pytest.fixture
def steering_env(tmp_path, monkeypatch):
    tasks_file = tmp_path / "tasks.json"
    steering_file = tmp_path / "steering.json"
    workflows_file = tmp_path / "workflows.json"

    monkeypatch.setenv("TASKS_FILE", str(tasks_file))
    monkeypatch.setenv("STEERING_FILE", str(steering_file))
    monkeypatch.setenv("WORKFLOWS_FILE", str(workflows_file))

    tasks_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")
    steering_file.write_text(json.dumps({"steering_queues": {}, "history": []}), encoding="utf-8")
    workflows_file.write_text(json.dumps({"workflows": {}}), encoding="utf-8")

    return {
        "tasks_file": tasks_file,
        "steering_file": steering_file,
        "workflows_file": workflows_file,
    }


def _seed_task(env, task_id, status="working", pane_id="pane-101", workflow_id="wf-test"):
    from herdr.state_store import get_state_store
    t_data = json.loads(env["tasks_file"].read_text(encoding="utf-8"))
    task = {
        "task_id": task_id,
        "workflow_id": workflow_id,
        "status": status,
        "pane_id": pane_id,
        "node": "dev",
        "agent": "codex",
        "status_history": [{"from": None, "to": status, "timestamp": time.time()}],
    }
    t_data["tasks"].append(task)
    env["tasks_file"].write_text(json.dumps(t_data), encoding="utf-8")
    store = get_state_store(db_path=env["tasks_file"].parent / "state.db")
    store.save_task(task)
    return task


def test_format_steer_prompt():
    prompt = steering.format_steer_prompt("Please stick to standard library", operator="commander")
    assert "【总指挥实时插话纠偏指令 - STEERING INSTRUCTION】" in prompt
    assert "commander" in prompt
    assert "Please stick to standard library" in prompt


def test_queue_steer_normal(steering_env):
    task_id = "task-001"
    _seed_task(steering_env, task_id, status="working")

    res = steering.queue_steer(task_id, "Avoid external dependencies", operator="human", urgent=False)
    assert res["ok"] is True
    assert res["status"] == "queued"
    assert res["urgent"] is False
    steer_id = res["steer_id"]

    # Verify steering.json state
    s_data = steering.load_steering_data()
    q = s_data["steering_queues"].get(task_id, [])
    assert len(q) == 1
    assert q[0]["steer_id"] == steer_id
    assert q[0]["instruction"] == "Avoid external dependencies"
    assert q[0]["status"] == "pending"


def test_urgent_steer_immediate_dispatch(steering_env):
    task_id = "task-002"
    _seed_task(steering_env, task_id, status="working", pane_id="pane-999")

    mock_run = MagicMock()
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = ""
    mock_run.return_value.stderr = ""

    with patch("subprocess.run", mock_run):
        res = steering.queue_steer(task_id, "Stop! Fix syntax error immediately", operator="lead", urgent=True)

    assert res["ok"] is True
    assert res["status"] == "dispatched"
    assert res["urgent"] is True

    # Verify subprocess calls: ctrl-c, send-text, enter
    calls = mock_run.call_args_list
    assert len(calls) >= 2

    # Check first call sends ctrl-c
    first_cmd = calls[0][0][0]
    assert "herdr" in first_cmd and "send-keys" in first_cmd and "ctrl-c" in first_cmd

    # Check subsequent call sends text
    text_calls = [c for c in calls if "send-text" in c[0][0]]
    assert len(text_calls) >= 1
    cmd = text_calls[0][0][0]
    assert "Stop! Fix syntax error immediately" in cmd[4]

    # Verify steering.json state
    s_data = steering.load_steering_data()
    q = s_data["steering_queues"].get(task_id, [])
    assert len(q) == 1
    assert q[0]["status"] == "dispatched"
    assert q[0]["dispatched_at"] is not None

    # Verify tasks.json updated with steering history
    t_data = json.loads(steering_env["tasks_file"].read_text(encoding="utf-8"))
    t = next(x for x in t_data["tasks"] if x["task_id"] == task_id)
    assert len(t.get("steering_history", [])) == 1
    assert t["steering_history"][0]["instruction"] == "Stop! Fix syntax error immediately"


def test_repeated_steering_appends_one_history_and_event_per_action(steering_env):
    from herdr.state_store import get_state_store

    task_id = "task-event-steering"
    _seed_task(
        steering_env,
        task_id,
        status="working",
        pane_id="pane-event",
        workflow_id="wf-event-steering",
    )

    mock_run = MagicMock()
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = ""
    mock_run.return_value.stderr = ""

    with patch("subprocess.run", mock_run):
        for n in range(3):
            steering.queue_steer(
                task_id,
                f"Steering instruction {n}",
                operator="lead",
                urgent=True,
            )

    store = get_state_store(db_path=steering_env["tasks_file"].parent / "state.db")
    history = store.list_steering_history(task_id=task_id)
    events = store.list_events(task_id=task_id, event_type="steering.steer_dispatched")

    assert len(history) == 3
    assert len(events) == 3
    assert [h["instruction"] for h in history] == [
        "Steering instruction 0",
        "Steering instruction 1",
        "Steering instruction 2",
    ]
    assert [e["payload"]["instruction"] for e in events] == [
        "Steering instruction 0",
        "Steering instruction 1",
        "Steering instruction 2",
    ]
    assert all(e["workflow_id"] == "wf-event-steering" for e in events)
    assert all(e["node_id"] == "dev" for e in events)
    assert all(e["task_id"] == task_id for e in events)
    assert all(e["agent_id"] == "codex" for e in events)
    assert all(e["source"] == "steering" for e in events)


def test_halt_task_lifecycle(steering_env):
    from herdr.state_store import get_state_store

    task_id = "task-003"
    _seed_task(
        steering_env,
        task_id,
        status="working",
        pane_id="pane-888",
        workflow_id="wf-halt-context",
    )

    mock_run = MagicMock()
    mock_run.return_value.returncode = 0

    with patch("subprocess.run", mock_run):
        res = steering.halt_task(task_id, reason="Severe logic flaw detected", operator="lead")

    assert res["ok"] is True
    assert res["status"] == "interrupted"

    # Verify ctrl-c sent
    assert any("ctrl-c" in c[0][0] for c in mock_run.call_args_list)

    # Verify task status in tasks.json
    t_data = json.loads(steering_env["tasks_file"].read_text(encoding="utf-8"))
    t = next(x for x in t_data["tasks"] if x["task_id"] == task_id)
    assert t["status"] == "interrupted"
    assert t["interrupt_reason"] == "Severe logic flaw detected"
    assert t["status_history"][-1]["to"] == "interrupted"

    store = get_state_store(db_path=steering_env["tasks_file"].parent / "state.db")
    events = store.list_events(task_id=task_id, event_type="steering.task_halted")
    assert len(events) == 1
    assert events[0]["workflow_id"] == "wf-halt-context"
    assert events[0]["node_id"] == "dev"
    assert events[0]["agent_id"] == "codex"
    assert events[0]["source"] == "steering"


def test_drain_pending_steer(steering_env):
    task_id = "task-004"
    _seed_task(steering_env, task_id, status="working", pane_id="pane-777")

    # Queue a normal steer
    res_q = steering.queue_steer(task_id, "Use atomic write for json files", urgent=False)
    steer_id = res_q["steer_id"]

    mock_run = MagicMock()
    mock_run.return_value.returncode = 0

    with patch("subprocess.run", mock_run):
        res_drain = steering.dispatch_pending_steer(task_id)

    assert res_drain is not None
    assert res_drain["ok"] is True
    assert res_drain["steer_id"] == steer_id

    # Check text sent
    text_calls = [c for c in mock_run.call_args_list if "send-text" in c[0][0]]
    assert len(text_calls) >= 1
    cmd = text_calls[0][0][0]
    assert "Use atomic write for json files" in cmd[4]

    # Check queue status updated
    s_data = steering.load_steering_data()
    q = s_data["steering_queues"][task_id]
    assert q[0]["status"] == "dispatched"

    # Draining again returns None
    assert steering.dispatch_pending_steer(task_id) is None


def test_steer_nonexistent_or_inactive_task(steering_env):
    with pytest.raises(ValueError, match="Task 'unknown' not found"):
        steering.queue_steer("unknown", "Do something")

    _seed_task(steering_env, "done-task", status="completed")
    with pytest.raises(ValueError, match="inactive status"):
        steering.queue_steer("done-task", "Do something")

    with pytest.raises(ValueError, match="Task 'unknown' not found"):
        steering.halt_task("unknown")


def test_steer_with_agent_adapter_protocol_metadata(steering_env):
    from herdr.state_store import get_state_store

    # Seed task with claude agent
    t_data = json.loads(steering_env["tasks_file"].read_text(encoding="utf-8"))
    task = {
        "task_id": "task-claude-01",
        "workflow_id": "wf-test",
        "status": "working",
        "pane_id": "pane-claude-1",
        "node": "dev",
        "agent": "claude",
        "status_history": [{"from": None, "to": "working", "timestamp": time.time()}],
    }
    t_data["tasks"].append(task)
    steering_env["tasks_file"].write_text(json.dumps(t_data), encoding="utf-8")
    store = get_state_store(db_path=steering_env["tasks_file"].parent / "state.db")
    store.save_task(task)

    # Test get_task_adapter
    adapter = steering.get_task_adapter("task-claude-01")
    assert adapter.name == "claude"
    assert adapter.protocol_level == "tty_prototype"
    assert adapter.supports_interrupt is True

    # Urgent steer
    mock_run = MagicMock()
    mock_run.return_value.returncode = 0
    with patch("subprocess.run", mock_run):
        res = steering.queue_steer("task-claude-01", "Focus on simplicity", urgent=True)

    assert res["ok"] is True
    assert res["status"] == "dispatched"
    assert res["protocol"] == "tty_prototype"
    assert res["adapter"] == "claude"

    # Verify steering data contains protocol and adapter
    s_data = steering.load_steering_data()
    q = s_data["steering_queues"]["task-claude-01"]
    assert q[0]["protocol"] == "tty_prototype"
    assert q[0]["adapter"] == "claude"

    # Verify task entity in tasks.json contains protocol and adapter
    t_updated = json.loads(steering_env["tasks_file"].read_text(encoding="utf-8"))
    t = next(x for x in t_updated["tasks"] if x["task_id"] == "task-claude-01")
    assert t["steering_history"][-1]["protocol"] == "tty_prototype"
    assert t["steering_history"][-1]["adapter"] == "claude"

    # Soft halt
    with patch("subprocess.run", mock_run):
        halt_res = steering.halt_task("task-claude-01", reason="Manual stop")
    assert halt_res["protocol"] == "tty_prototype"
    assert halt_res["adapter"] == "claude"


def test_steer_no_pane_id_delivery_failure_keeps_pending(steering_env):
    """Test 3: Without pane_id, pane_delivery_ok must NOT be True, and item must stay pending."""
    task_id = "task-no-pane-01"
    _seed_task(steering_env, task_id, status="working", pane_id=None)

    res = steering.queue_steer(task_id, "Directive without pane", urgent=True)

    # Delivery failed: ok must be False, pane_delivery_ok must NOT be True
    assert res["ok"] is False
    assert res["pane_delivery_ok"] is not True
    assert res["pane_delivery_ok"] is False
    assert res["delivery_attempted"] is False
    assert res["status"] == "pending"

    # In steering queue, status must remain pending
    s_data = steering.load_steering_data()
    q = s_data["steering_queues"][task_id]
    assert len(q) == 1
    assert q[0]["status"] == "pending"
    assert q[0]["last_delivery_error"] == "no_pane_id"

    # Task entity must NOT record this in steering_history
    t_data = json.loads(steering_env["tasks_file"].read_text(encoding="utf-8"))
    t = next(x for x in t_data["tasks"] if x["task_id"] == task_id)
    assert len(t.get("steering_history", [])) == 0


def test_steer_physical_delivery_failure_keeps_pending(steering_env):
    """If TTY injection fails, steer item must stay pending and NOT be marked dispatched."""
    task_id = "task-fail-pane-01"
    _seed_task(steering_env, task_id, status="working", pane_id="pane-dead")

    mock_run = MagicMock()
    mock_run.return_value.returncode = 1  # Subprocess fails

    with patch("subprocess.run", mock_run):
        res = steering.queue_steer(task_id, "Will fail delivery", urgent=True)

    assert res["ok"] is False
    assert res["pane_delivery_ok"] is False
    assert res["delivery_attempted"] is True
    assert res["status"] == "pending"

    s_data = steering.load_steering_data()
    q = s_data["steering_queues"][task_id]
    assert q[0]["status"] == "pending"
    assert q[0].get("last_delivery_error") is not None


def test_halt_task_interrupt_failure_preserves_task_status(steering_env):
    """Test 4: When interrupt fails, task status MUST NOT transition to 'interrupted'."""
    task_id = "task-halt-fail-01"
    _seed_task(steering_env, task_id, status="working", pane_id="pane-cannot-interrupt")

    mock_run = MagicMock()
    mock_run.return_value.returncode = 1  # ctrl-c failed

    with patch("subprocess.run", mock_run):
        res = steering.halt_task(task_id, reason="Emergency audit")

    assert res["ok"] is False
    assert res["status"] == "working"
    assert res["error"] == "interrupt_signal_failed"

    # CRITICAL: Task in tasks.json MUST remain in "working", NOT "interrupted"
    t_data = json.loads(steering_env["tasks_file"].read_text(encoding="utf-8"))
    t = next(x for x in t_data["tasks"] if x["task_id"] == task_id)
    assert t["status"] == "working"
    assert t["status"] != "interrupted"


def test_dispatch_pending_steer_opencode_soft_steer_refused(steering_env):
    """OpenCode does not support soft steer; new non-urgent queue must preserve a permanent refusal without retry."""
    task_id = "task-opencode-01"
    from herdr.state_store import get_state_store
    t_data = json.loads(steering_env["tasks_file"].read_text(encoding="utf-8"))
    task = {
        "task_id": task_id,
        "workflow_id": "wf-test",
        "status": "working",
        "pane_id": "pane-oc-1",
        "node": "dev",
        "agent": "opencode",
        "status_history": [{"from": None, "to": "working", "timestamp": time.time()}],
    }
    t_data["tasks"].append(task)
    steering_env["tasks_file"].write_text(json.dumps(t_data), encoding="utf-8")
    store = get_state_store(db_path=steering_env["tasks_file"].parent / "state.db")
    store.save_task(task)

    # Queue a non-urgent steer
    res_q = steering.queue_steer(task_id, "Soft directive for opencode", urgent=False)
    steer_id = res_q["steer_id"]

    mock_run = MagicMock()
    with patch("subprocess.run", mock_run):
        res_drain = steering.dispatch_pending_steer(task_id)

    # Must refuse
    assert res_q["ok"] is False
    assert res_q["status"] == "blocked"
    assert res_q["reason"] == "soft_steer_not_supported"
    assert res_drain is None

    # ZERO subprocess calls: no TTY injection!
    assert mock_run.call_count == 0

    # Original instruction remains visible in blocked queue
    s_data = steering.load_steering_data()
    q = s_data["steering_queues"][task_id]
    assert q[0]["status"] == "blocked"
    assert q[0]["last_delivery_error"] == "soft_steer_not_supported"


def test_steer_urgent_interrupt_success_inject_failure_sets_task_interrupted(steering_env):
    """Blocker 1: When ctrl-c succeeds but prompt injection fails, task must transition to
    'interrupted' (with requires_attention=True) to prevent runtime fact drift, while steer
    item remains 'pending' with last_delivery_error='inject_prompt_failed'.
    """
    task_id = "task-partial-urgent-fail-01"
    _seed_task(steering_env, task_id, status="working", pane_id="pane-partial-01", workflow_id="wf-test")

    # Mock subprocess.run:
    # 1st call (send-keys ctrl-c): returncode = 0 (success)
    # 2nd call (send-text prompt): returncode = 1 (failure)
    def side_effect(cmd, *args, **kwargs):
        mock_res = MagicMock()
        if "send-keys" in cmd and "ctrl-c" in cmd:
            mock_res.returncode = 0
        else:
            mock_res.returncode = 1
        return mock_res

    with patch("subprocess.run", side_effect=side_effect):
        res = steering.queue_steer(task_id, "Directive to inject", urgent=True)

    # 1. Steering item outcome
    assert res["ok"] is False
    assert res["status"] == "pending"
    assert res["interrupted"] is True
    assert res["injected"] is False
    assert res["reason"] == "inject_prompt_failed"

    # 2. Queue state in steering: pending with error recorded
    s_data = steering.load_steering_data()
    q = s_data["steering_queues"][task_id]
    assert len(q) == 1
    assert q[0]["status"] == "pending"
    assert q[0]["last_delivery_error"] == "inject_prompt_failed"

    # 3. Task state in tasks.json: MUST be interrupted with requires_attention=True!
    t_data = json.loads(steering_env["tasks_file"].read_text(encoding="utf-8"))
    t = next(x for x in t_data["tasks"] if x["task_id"] == task_id)
    assert t["status"] == "interrupted"
    assert t["interrupt_reason"] == "urgent_steer_injection_failed"
    assert t.get("requires_attention") is True


def test_steering_history_append_only_no_duplicate_inflation(steering_env):
    """Blocker 3: Multiple steering dispatches must append to history without duplicate inflation."""
    from herdr.state_store import get_state_store
    task_id = "task-anti-inflate-01"
    _seed_task(steering_env, task_id, status="working", pane_id="pane-inflate-01")

    mock_run = MagicMock()
    mock_run.return_value.returncode = 0

    with patch("subprocess.run", mock_run):
        # Dispatch 3 distinct steers sequentially
        steering.queue_steer(task_id, "Instruction 1", urgent=True)
        steering.queue_steer(task_id, "Instruction 2", urgent=True)
        steering.queue_steer(task_id, "Instruction 3", urgent=True)

    store = get_state_store(db_path=steering_env["tasks_file"].parent / "state.db")
    hist = store.list_steering_history(task_id=task_id)

    # Exactly 3 entries, not 1 + 2 + 3 = 6 or more!
    assert len(hist) == 3
    instructions = [h.get("instruction") for h in hist]
    assert instructions == ["Instruction 1", "Instruction 2", "Instruction 3"]



