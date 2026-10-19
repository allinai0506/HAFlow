"""Tests for Console Interaction API (GET /api/task/interaction and POST /api/task/interaction/respond)."""

import json
from unittest.mock import MagicMock, patch
import pytest

from console import herdr_factory_console as c
import herdr.interaction_loop as il


SAMPLE_PROMPT = (
    "Allow operation?\n"
    "  1. Allow\n"
    "  2. Deny\n"
    "  [1 for allow, 2 for deny]: "
)


from herdr.state_db import init_db


@pytest.fixture
def mock_tasks(monkeypatch, tmp_path):
    db_file = tmp_path / "test_state.db"
    init_db(db_file)
    monkeypatch.setenv("HERDR_STATE_DB", str(db_file))
    t_active = {
        "task_id": "task-test-1",
        "workflow_id": "wf-test-1",
        "pane_id": "w1:p1",
        "agent": "claude",
        "status": "blocked",
        "goal": "执行确认",
        "version": 1,
    }
    t_done = {
        "task_id": "task-done-1",
        "workflow_id": "wf-test-1",
        "pane_id": "w1:p2",
        "agent": "claude",
        "status": "completed",
        "version": 2,
    }
    task_list = [t_active, t_done]
    monkeypatch.setattr(c.inventory, "tasks", lambda: list(task_list))
    return task_list



def test_api_task_interaction_get_detected(mock_tasks, monkeypatch):
    monkeypatch.setattr(c.read_api, "read_pane", lambda pid: SAMPLE_PROMPT)
    monkeypatch.setattr(c.runtime, "agent_runtime", lambda pid: {
        "agent": "claude",
        "agent_status": "blocked",
        "terminal_id": "term_123",
        "revision": 5,
    })

    res = c.api_task_interaction_get(tid="task-test-1")
    assert res["ok"] is True
    inter = res["interaction"]
    assert inter["detected"] is True
    assert inter["task_id"] == "task-test-1"
    assert inter["pane_id"] == "w1:p1"
    assert inter["agent"] == "claude"
    assert len(inter["options"]) == 2
    assert "option_id" in inter["options"][0]
    assert "key_sequence" not in inter["options"][0]  # Opaque to client


def test_api_task_interaction_get_not_detected(mock_tasks, monkeypatch):
    monkeypatch.setattr(c.read_api, "read_pane", lambda pid: "Compiling binary...\nDone in 0.5s\n")
    monkeypatch.setattr(c.runtime, "agent_runtime", lambda pid: {
        "agent": "claude",
        "agent_status": "working",
        "terminal_id": "term_123",
        "revision": 6,
    })

    res = c.api_task_interaction_get(tid="task-test-1")
    assert res["ok"] is True
    assert res["interaction"]["detected"] is False


def test_api_task_interaction_get_nonexistent_task(mock_tasks):
    with pytest.raises(RuntimeError, match="任务 nonexistent 不存在"):
        c.api_task_interaction_get(tid="nonexistent")


def test_api_task_interaction_get_workflow_scan(mock_tasks, monkeypatch):
    monkeypatch.setattr(c.read_api, "read_pane", lambda pid: SAMPLE_PROMPT)
    monkeypatch.setattr(c.runtime, "agent_runtime", lambda pid: {
        "agent": "claude",
        "agent_status": "blocked",
        "terminal_id": "term_123",
        "revision": 5,
    })

    res = c.api_task_interaction_get(wid="wf-test-1")
    assert res["ok"] is True
    assert len(res["interactions"]) == 1
    assert res["interactions"][0]["task_id"] == "task-test-1"


def test_api_task_interaction_respond_success(mock_tasks, monkeypatch):
    fp = il.compute_prompt_fingerprint(SAMPLE_PROMPT)
    monkeypatch.setattr(c.read_api, "read_pane", lambda pid: SAMPLE_PROMPT)
    monkeypatch.setattr(c.runtime, "agent_runtime", lambda pid: {
        "agent": "claude",
        "agent_status": "blocked",
        "terminal_id": "term_123",
        "revision": 5,
    })

    # First get options
    get_res = c.api_task_interaction_get(tid="task-test-1")
    opt = get_res["interaction"]["options"][0]

    with patch.object(il, "_send_keys_impl", return_value=True) as mock_send:
        # After send, simulate prompt cleared
        def updated_read(pid):
            return "Operation allowed.\nProceeding...\n"
        def updated_rt(pid):
            return {"agent": "claude", "agent_status": "working", "terminal_id": "term_123", "revision": 6}

        with patch.object(c.read_api, "read_pane", side_effect=[SAMPLE_PROMPT, "Proceeding...\n"]):
            with patch.object(c.runtime, "agent_runtime", side_effect=[
                {"agent": "claude", "agent_status": "blocked", "terminal_id": "term_123", "revision": 5},
                {"agent": "claude", "agent_status": "working", "terminal_id": "term_123", "revision": 6},
            ]):
                resp = c.api_task_interaction_respond({
                    "task_id": "task-test-1",
                    "option_id": opt["option_id"],
                    "expected_fingerprint": fp,
                    "expected_pane_id": "w1:p1",
                    "expected_agent": "claude",
                    "expected_terminal_id": "term_123",
                })
                assert resp["ok"] is True
                assert resp["status"] == "resolved"
                assert mock_send.call_count == 2
                assert mock_send.call_args_list[0][0] == ("w1:p1", "1")
                assert mock_send.call_args_list[1][0] == ("w1:p1", "enter")


def test_api_task_interaction_respond_fingerprint_mismatch(mock_tasks, monkeypatch):
    monkeypatch.setattr(c.read_api, "read_pane", lambda pid: SAMPLE_PROMPT)
    monkeypatch.setattr(c.runtime, "agent_runtime", lambda pid: {
        "agent": "claude",
        "agent_status": "blocked",
        "terminal_id": "term_123",
        "revision": 5,
    })

    with pytest.raises(RuntimeError, match="交互上下文已失效"):
        c.api_task_interaction_respond({
            "task_id": "task-test-1",
            "option_id": "any_opt",
            "expected_fingerprint": "wrong_fingerprint_value",
            "expected_pane_id": "w1:p1",
            "expected_agent": "claude",
            "expected_terminal_id": "term_123",
        })


def test_api_task_interaction_respond_pane_drift(mock_tasks, monkeypatch):
    fp = il.compute_prompt_fingerprint(SAMPLE_PROMPT)
    monkeypatch.setattr(c.read_api, "read_pane", lambda pid: SAMPLE_PROMPT)
    monkeypatch.setattr(c.runtime, "agent_runtime", lambda pid: {
        "agent": "claude",
        "agent_status": "blocked",
        "terminal_id": "term_123",
    })

    with pytest.raises(RuntimeError, match="工位发生漂移"):
        c.api_task_interaction_respond({
            "task_id": "task-test-1",
            "option_id": "any_opt",
            "expected_fingerprint": fp,
            "expected_pane_id": "w99:p99",  # Drifted
        })


def test_api_task_interaction_respond_completed_task_rejected(mock_tasks):
    with pytest.raises(RuntimeError, match="任务已结束或已作废"):
        c.api_task_interaction_respond({
            "task_id": "task-done-1",
            "option_id": "opt1",
            "expected_fingerprint": "fp1",
        })


def test_api_task_interaction_respond_duplicate_rejection(mock_tasks, monkeypatch):
    fp = il.compute_prompt_fingerprint(SAMPLE_PROMPT)
    monkeypatch.setattr(c.read_api, "read_pane", lambda pid: SAMPLE_PROMPT)
    monkeypatch.setattr(c.runtime, "agent_runtime", lambda pid: {
        "agent": "claude",
        "agent_status": "blocked",
        "terminal_id": "term_123",
    })

    get_res = c.api_task_interaction_get(tid="task-test-1")
    opt = get_res["interaction"]["options"][0]

    from herdr import state_db
    with patch.object(state_db, "record_event_if_absent", return_value={"status": "exists"}):
        with pytest.raises(RuntimeError, match="该交互已受理，请勿重复提交"):
            c.api_task_interaction_respond({
                "task_id": "task-test-1",
                "option_id": opt["option_id"],
                "expected_fingerprint": fp,
                "expected_pane_id": "w1:p1",
            })
