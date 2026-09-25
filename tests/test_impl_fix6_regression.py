"""Current-HEAD regressions for review-r1 blockers P1-1 and P2-1..P2-5."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from herdr import agent_router, workflow_docs
from herdr.state_store import SQLiteStateStore, reset_state_store

ROOT = Path(__file__).resolve().parent.parent


def load_script(name: str, relative: str):
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, str(ROOT / relative))
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_review_without_delivery_is_rejected_and_audited_before_resources(
    tmp_path, monkeypatch, capsys,
):
    task_bin = load_script("herdr_task_fix6_p1", "bin/herdr-task")
    monkeypatch.setenv("HERDR_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv(workflow_docs.DOCS_DIR_ENV, str(tmp_path / "shared"))
    store = SQLiteStateStore(tmp_path / "state.db")
    store.save_workflow({"workflow_id": "wf-r1-p1", "status": "running"})
    with patch.object(task_bin, "_get_store", return_value=store), pytest.raises(SystemExit) as exc:
        task_bin._preflight_delivery_identity("wf-r1-p1", "review", "review-no-delivery")
    assert exc.value.code == 2
    assert "delivery" in capsys.readouterr().out.lower()
    events = store.list_events(task_id="review-no-delivery", event_type="test_baseline_rejected")
    assert len(events) == 1
    assert events[0]["payload"]["failure_code"] == "delivery_missing"
    reset_state_store()


def test_unrelated_review_baseline_exits_two_and_records_actionable_event(
    tmp_path, monkeypatch, capsys,
):
    task_bin = load_script("herdr_task_fix6_bad_baseline", "bin/herdr-task")
    monkeypatch.setenv("HERDR_STATE_DB", str(tmp_path / "state.db"))
    store = SQLiteStateStore(tmp_path / "state.db")
    args = SimpleNamespace(workflow_id="wf-r1-bad-baseline", task_id="review-bad",
                           node="review", stage=None)
    with patch.object(task_bin, "_get_store", return_value=store), \
         patch.object(task_bin.subprocess, "run", return_value=SimpleNamespace(returncode=1)), \
         pytest.raises(SystemExit) as exc:
        task_bin._validate_test_delivery_baseline(
            args, "candidate-sha", "unrelated-head", str(tmp_path)
        )
    assert exc.value.code == 2
    assert "FR-6.2 fail-closed" in capsys.readouterr().out
    events = store.list_events(task_id="review-bad", event_type="test_baseline_rejected")
    assert len(events) == 1
    assert events[0]["payload"]["failure_code"] == "baseline_unproven"
    assert events[0]["payload"]["actionable"] is True
    assert "rebase" in events[0]["payload"]["remediation"]
    reset_state_store()


def test_check_delivery_cli_uses_pr_92_merged_metadata_and_git_trees(
    tmp_path, monkeypatch, capsys,
):
    task_bin = load_script("herdr_task_fix6_p2_1", "bin/herdr-task")
    monkeypatch.setenv(workflow_docs.DOCS_DIR_ENV, str(tmp_path / "shared"))
    workflow_docs.append_note(
        "wf-r1-p2-1", kind="delivery", title="delivery branch@candidate",
        node="wrapup", base_sha="base", fields={
            "delivery_id": "delivery-92", "delivery_branch": "agent/fix-92",
            "candidate_sha": "candidate-sha", "review_task": "review-92",
            "test_gate": "test-92",
        },
    )
    calls = []

    def transport(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{
                "number": 92, "headRefName": "agent/fix-92",
                "headRefOid": "old-merged-sha", "state": "MERGED",
            }]), stderr="")
        if cmd[:4] == ["git", "-C", str(tmp_path), "rev-parse"]:
            return SimpleNamespace(returncode=0, stdout="same-tree\n", stderr="")
        raise AssertionError(f"unexpected transport command: {cmd}")

    monkeypatch.setattr(task_bin.subprocess, "run", transport)
    monkeypatch.setattr(sys, "argv", ["herdr-task", "check-delivery",
        "--workflow-id", "wf-r1-p2-1", "--head-sha", "head-sha",
        "--head-ref", "agent/fix-92", "--repo-path", str(tmp_path)])
    task_bin.main()
    output = capsys.readouterr().out
    assert "same_branch_warn=True" in output
    assert "92" in output and "review required" in output
    assert any(cmd[:3] == ["gh", "pr", "list"] for cmd in calls)
    assert sum(cmd[:4] == ["git", "-C", str(tmp_path), "rev-parse"] for cmd in calls) >= 2


def test_accept_escalated_report_names_closed_pane_and_retained_unintegrated_clone(
    tmp_path, capsys,
):
    task_bin = load_script("herdr_task_fix6_p2_2", "bin/herdr-task")
    task = {"task_id": "escalated-r1", "workflow_id": "wf-r1-p2-2",
        "status": "committed", "integration_mode": "git", "finalize_escalated": True,
        "pane_id": "pane-test", "clone_path": str(tmp_path / "clone")}
    with patch.object(task_bin, "load_tasks", return_value={"tasks": [task]}), \
         patch.object(task_bin, "_load_workflow_entry", return_value=(None, {})), \
         patch.object(task_bin, "_finalize_one", return_value={
             "task_id": "escalated-r1", "status": "committed", "action": "finalized",
             "pane_closed": True, "clone_deleted": False,
             "clone_retained_reason": "committed but not integrated",
         }), patch.object(task_bin, "_workflow_stage_tabs", return_value={
             "tab_ids": [], "workspace_id": None, "coordinator_pane": None,
             "owned_pane_ids": set()}), patch.object(task_bin, "_mark_workflow_completed"), \
         patch.object(task_bin, "stage_reset"):
        report = task_bin.close_workflow("wf-r1-p2-2", accept_escalated=True)
    rendered = capsys.readouterr().out
    assert report["outcome"] == "escalated_accepted"
    assert report["tasks"][0]["pane_closed"] is True
    assert report["tasks"][0]["clone_deleted"] is False
    assert report["tasks"][0]["status"] == "committed"
    assert "not integrated" in rendered
    assert "WORKFLOW CLOSED" in rendered


def test_opt_out_event_records_the_actual_selected_agent(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    store.save_workflow({"workflow_id": "wf-r1-p2-4", "project_id": "p",
        "status": "running", "healthy_agents": [], "unhealthy_agents": {}})
    store.save_task({"task_id": "impl-r1", "workflow_id": "wf-r1-p2-4",
        "node": "implementation", "agent": "codex", "status": "completed"})
    config = {"nodes": [{"id": "review", "agent_policy": {
        "allow_reuse_implementation_agents": True, "reuse_reason": "recovery review"}}]}
    pool = {"allowed_agents": ["codex"], "disabled_agents": [],
        "stage_preferences": {"review": ["codex"]}, "task_type_preferences": {"test": ["codex"]}}
    with patch.object(agent_router, "_get_store", return_value=store), \
         patch.object(agent_router, "workflow_config_for", return_value=config), \
         patch.object(agent_router, "ensure_pool_for_project", return_value=pool), \
         patch.object(agent_router, "POOLS_FILE", tmp_path / "pools.json"), \
         patch.object(agent_router, "RESERVATIONS_FILE", tmp_path / "reservations.json"), \
         patch.object(agent_router, "ROUTER_LOCK_FILE", tmp_path / "router.lock"):
        selected = agent_router.choose_agent("wf-r1-p2-4", "review", "test")
    audit = store.list_events(workflow_id="wf-r1-p2-4", event_type="router_opt_out_used")
    assert selected == "codex"
    assert len(audit) == 1
    assert audit[0]["payload"]["selected"] == selected


def test_sentinel_survives_one_sqlite_failure_and_runs_next_cycle(tmp_path):
    sentinel = load_script("herdr_sentinel_fix6_p2_5", "services/herdr-sentinel.py")
    attempts = []
    class Store:
        def list_tasks(self):
            attempts.append("list")
            if len(attempts) == 1:
                raise sqlite3.OperationalError("temporary database lock")
            return []
    sentinel.STATE_FILE = tmp_path / "sentinel.json"
    sleeps = []
    def stop_after_recovery(_seconds):
        sleeps.append(_seconds)
        if len(sleeps) == 2:
            raise StopSentinelLoop
    with (
        patch.object(sentinel, "_get_store", return_value=Store()),
        patch.object(sentinel, "load_json", return_value={"seen": {}, "nudged": {}}),
        patch.object(sentinel.time, "sleep", side_effect=stop_after_recovery),
        pytest.raises(StopSentinelLoop),
    ):
        sentinel.main()
    assert len(attempts) == 2


def test_sentinel_observation_sqlite_error_is_best_effort_and_continues(tmp_path):
    sentinel = load_script("herdr_sentinel_fix6_observation", "services/herdr-sentinel.py")
    task = {"task_id": "task-db-flake", "workflow_id": "wf-db-flake",
            "status": "working", "pane_id": "mock-pane", "version": 1}
    attempts = []
    events = []

    class Store:
        def list_tasks(self):
            attempts.append("list")
            return [task] if len(attempts) == 1 else []
        def observe_completion(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("temporary database lock")
        def record_event(self, event_type, payload, **_kwargs):
            events.append((event_type, payload))
            raise sqlite3.OperationalError("audit writer also unavailable")

    sentinel.STATE_FILE = tmp_path / "sentinel-state.json"
    sleeps = []
    def stop_after_recovery(_seconds):
        sleeps.append(_seconds)
        if len(sleeps) == 2:
            raise StopSentinelLoop

    with (
        patch.object(sentinel, "_get_store", return_value=Store()),
        patch.object(sentinel, "load_json", return_value={"seen": {}, "nudged": {}}),
        patch.object(sentinel, "pane_visible", return_value=""),
        patch.object(sentinel, "agent_status", return_value="working"),
        patch.object(sentinel, "dispatch_fuse_enabled", return_value=False),
        patch.object(sentinel, "save_json_atomic"),
        patch.object(sentinel.time, "sleep", side_effect=stop_after_recovery),
        pytest.raises(StopSentinelLoop),
    ):
        sentinel.main()
    assert len(attempts) == 2
    assert events and events[0][0] == "completion_observation_failed"


class StopSentinelLoop(Exception):
    pass
