"""Integration tests for Console Inbox APIs."""

import pytest
from pathlib import Path
from unittest.mock import patch

from console.inbox_api import (
    api_inbox_cancel_task,
    api_inbox_confirm_task,
    api_inbox_create_task,
    api_inbox_detect,
    api_inbox_reorder_task,
    api_inbox_retry_task,
    api_inbox_set_priority,
    api_inbox_stats,
    api_inbox_task,
    api_inbox_tasks,
    get_inbox_store,
)
from herdr.inbox.store import InboxStore

MOCK_PROJECTS = {
    "version": 1,
    "projects": {
        "/Users/user/HAFlow": {
            "project_id": "haflow-1de29d2d",
            "project_name": "HAFlow",
            "project_root": "/Users/user/HAFlow",
            "workspace_id": "w1K",
        },
    },
}


@pytest.fixture(autouse=True)
def setup_inbox_test_db(tmp_path: Path):
    db_file = tmp_path / "test_api_inbox.db"
    store = InboxStore(db_file)
    with patch("console.inbox_api.get_inbox_store", return_value=store):
        yield store


def test_api_inbox_workflow():
    with patch("herdr.inbox.detector.load_projects", return_value=MOCK_PROJECTS):
        # 1. Detect scope
        detect_res = api_inbox_detect({
            "title": "Bug in HAFlow UI",
            "instruction": "Fix layout bug",
        })
        assert detect_res["ok"] is True
        assert detect_res["recommended_project_id"] == "haflow-1de29d2d"
        assert detect_res["recommended_workspace_id"] == "w1K"

        # 2. Create as draft
        draft_res = api_inbox_create_task({
            "title": "Draft task",
            "instruction": "Need review first",
            "priority": "LOW",
        })
        assert draft_res["ok"] is True
        assert draft_res["status"] == "DRAFT"
        task_id = draft_res["task_id"]

        # 3. Confirm scope
        confirm_res = api_inbox_confirm_task({
            "task_id": task_id,
            "project_id": "haflow-1de29d2d",
            "workspace_id": "w1K",
        })
        assert confirm_res["ok"] is True
        assert confirm_res["task"]["status"] == "PENDING"

        # 4. Set priority
        prio_res = api_inbox_set_priority({
            "task_id": task_id,
            "priority": "HIGH",
        })
        assert prio_res["ok"] is True
        assert prio_res["task"]["priority"] == "HIGH"

        # 5. Create another task directly to PENDING with auto_confirm
        auto_res = api_inbox_create_task({
            "title": "Urgent Hotfix",
            "instruction": "Fix critical bug",
            "project_id": "haflow-1de29d2d",
            "workspace_id": "w1K",
            "priority": "URGENT",
            "auto_confirm": True,
        })
        assert auto_res["status"] == "PENDING"

        # 6. List tasks
        list_res = api_inbox_tasks({"status": "PENDING", "workspace_id": "w1K"})
        assert list_res["ok"] is True
        assert list_res["total"] == 2
        # Urgent Hotfix should be #1, Draft task (now HIGH) should be #2
        assert list_res["tasks"][0]["title"] == "Urgent Hotfix"
        assert list_res["tasks"][0]["queue_position"] == 1
        assert list_res["tasks"][1]["title"] == "Draft task"
        assert list_res["tasks"][1]["queue_position"] == 2

        # 7. Cancel a task
        cancel_res = api_inbox_cancel_task({
            "task_id": auto_res["task_id"],
            "reason": "Test cancel",
        })
        assert cancel_res["ok"] is True
        assert cancel_res["task"]["status"] == "CANCELLED"

        # 8. Stats
        stats_res = api_inbox_stats()
        assert stats_res["ok"] is True
        assert stats_res["total"] == 2
        assert stats_res["counts"]["PENDING"] == 1
        assert stats_res["counts"]["CANCELLED"] == 1


def test_auto_confirm_source_validation_and_user_isolation(monkeypatch, setup_inbox_test_db):
    store = setup_inbox_test_db
    with patch("herdr.inbox.detector.load_projects", return_value=MOCK_PROJECTS):
        # 1. Automated confirm with low confidence must be forced to DRAFT
        low_res = api_inbox_create_task({
            "title": "Random pasta task",
            "instruction": "Cook pasta without any project references",
            "project_id": "haflow-1de29d2d",
            "workspace_id": "w1K",
            "auto_confirm": True,
            "confirmation_source": "auto",  # automated source!
        })
        assert low_res["ok"] is True
        assert low_res["status"] == "DRAFT"  # Forced to DRAFT!

        # 2. User identity scoping
        monkeypatch.setenv("HAFLOW_USER", "alice")
        alice_task = api_inbox_create_task({
            "title": "Alice task",
            "instruction": "For HAFlow project",
            "project_id": "haflow-1de29d2d",
            "workspace_id": "w1K",
            "auto_confirm": True,
            "confirmation_source": "user",
        })
        assert alice_task["task"]["user_id"] == "alice"

        monkeypatch.setenv("HAFLOW_USER", "bob")
        bob_task = api_inbox_create_task({
            "title": "Bob task",
            "instruction": "For HAFlow project",
            "project_id": "haflow-1de29d2d",
            "workspace_id": "w1K",
            "auto_confirm": True,
            "confirmation_source": "user",
        })
        assert bob_task["task"]["user_id"] == "bob"

        # Bob default listing only sees Bob's tasks
        bob_tasks = api_inbox_tasks({})
        bob_ids = [t["id"] for t in bob_tasks["tasks"]]
        assert bob_task["task_id"] in bob_ids
        assert alice_task["task_id"] not in bob_ids

        # P2-01: Non-admin caller passing user_id='all' or user_id='alice' CANNOT bypass isolation
        bob_all = api_inbox_tasks({"user_id": "all"})
        bob_all_ids = [t["id"] for t in bob_all["tasks"]]
        assert alice_task["task_id"] not in bob_all_ids

        bob_spoof = api_inbox_tasks({"user_id": "alice"})
        bob_spoof_ids = [t["id"] for t in bob_spoof["tasks"]]
        assert alice_task["task_id"] not in bob_spoof_ids

        # P2-01: Busy workspace notice masking across user boundaries
        store.update_task_status(alice_task["task_id"], "CLAIMED")
        store.update_task_status(alice_task["task_id"], "RUNNING", workflow_id="wf_secret_alice")

        bob_stats = api_inbox_stats()
        assert bob_stats["ok"] is True
        busy_w1 = bob_stats["busy_workspaces"].get("w1K")
        assert busy_w1 is not None
        assert busy_w1["status"] == "RUNNING"
        # Must be sanitized! No task_id or sensitive title leaked
        assert busy_w1["task_id"] is None
        assert busy_w1["title"] == "任务执行中 (其他用户)"


def test_high_confidence_auto_detection_cannot_bypass_user_confirmation(monkeypatch):
    """P1-01: Even if detection has 100% confidence, confirmation_source != 'user' MUST NOT set status to PENDING."""
    with patch("herdr.inbox.detector.load_projects", return_value=MOCK_PROJECTS):
        res = api_inbox_create_task({
            "title": "Fix HAFlow UI layout",
            "instruction": "Fix layout in /Users/user/HAFlow project",
            "project_id": "haflow-1de29d2d",
            "workspace_id": "w1K",
            "auto_confirm": True,
            "confirmation_source": "auto",  # automated detector
        })
        assert res["ok"] is True
        assert res["status"] == "DRAFT"  # Forced to DRAFT!
        assert res["task"]["status"] == "DRAFT"
