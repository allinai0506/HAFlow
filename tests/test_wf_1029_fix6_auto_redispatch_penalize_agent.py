"""Regression test for wf-haflow-1029-02 Fix 6:
Auto-recover redispatch deprioritizes the failed predecessor agent so it doesn't repeatedly assign
the exact same agent that just timed out or hung.
"""

import time
from unittest.mock import MagicMock
import pytest

from herdr import agent_router as ar, deep_preflight as dp
from herdr.state_store import get_state_store
from herdr.task_cli_launch import resolve_launch_agent


@pytest.fixture
def routing_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setattr(dp, "preflight_identity", lambda *a, **kw: {"verifiable": True, "fingerprint": "x"})
    for name, filename in [("POOLS_FILE", "pools.json"), ("RESERVATIONS_FILE", "reservations.json"), ("ROUTER_LOCK_FILE", "router.lock")]:
        monkeypatch.setattr(ar, name, tmp_path / filename)

    store = get_state_store(tmp_path / "state.db")
    store.save_workflow({
        "workflow_id": "wf-penalize-test",
        "project_id": "p-penalize",
        "project_root": str(tmp_path),
        "status": "active",
        "healthy_agents": ["opencode", "codex", "claude"],
        "unhealthy_agents": {},
        "preflight_checked_at": time.time(),
    })
    ar.save_pools({
        "projects": {
            "p-penalize": {
                "allowed_agents": ["opencode", "codex", "claude"],
                "stage_preferences": {
                    "wrapup": ["opencode", "codex", "claude"],
                },
            }
        }
    })
    return store


def test_penalize_agent_deprioritizes_failed_agent(routing_env, monkeypatch):
    monkeypatch.setattr(ar, "preflight_snapshot_fresh", lambda *a, **kw: True)
    monkeypatch.setattr(ar, "workflow_config_for", lambda _: {
        "nodes": [
            {
                "id": "wrapup",
                "agent_policy": {"preferred": ["opencode", "codex", "claude"]},
            }
        ]
    })

    # Without penalty: opencode is preferred and chosen first
    pick1 = ar.choose_agent("wf-penalize-test", "wrapup", "docs", requested="auto", reservation_key="t1")
    assert pick1 == "opencode"

    # With penalty on opencode: codex is chosen instead
    pick2 = ar.choose_agent(
        "wf-penalize-test",
        "wrapup",
        "docs",
        requested="auto",
        penalize_agents=["opencode"],
        reservation_key="t2",
    )
    assert pick2 == "codex"


def test_penalize_agent_ranks_before_load_based_ranking(routing_env, monkeypatch):
    monkeypatch.setattr(ar, "preflight_snapshot_fresh", lambda *a, **kw: True)
    monkeypatch.setattr(ar, "workflow_config_for", lambda _: {
        "nodes": [
            {
                "id": "wrapup",
                "agent_policy": {"preferred": ["opencode", "codex", "claude"]},
            }
        ]
    })
    # Mock active loads: opencode has 0 load (failed), codex and claude each have 1 load
    monkeypatch.setattr(ar, "_active_agent_loads", lambda pid: {"codex": 1, "claude": 1, "opencode": 0})

    # Even though opencode has lower load (0 vs 1), penalized opencode must NOT be chosen!
    pick = ar.choose_agent(
        "wf-penalize-test",
        "wrapup",
        "docs",
        requested="auto",
        penalize_agents=["opencode"],
        reservation_key="t-load-test",
    )
    assert pick == "codex"


def test_penalize_agent_preserves_explicit_request(routing_env, monkeypatch):
    monkeypatch.setattr(ar, "preflight_snapshot_fresh", lambda *a, **kw: True)
    monkeypatch.setattr(ar, "workflow_config_for", lambda _: {
        "nodes": [
            {
                "id": "wrapup",
                "agent_policy": {"preferred": ["opencode", "codex", "claude"]},
            }
        ]
    })

    # Explicit request for opencode overrides penalty
    pick = ar.choose_agent(
        "wf-penalize-test",
        "wrapup",
        "docs",
        requested="opencode",
        penalize_agents=["opencode"],
        reservation_key="t3",
    )
    assert pick == "opencode"


def test_resolve_launch_agent_extracts_predecessor_failed_agent(routing_env):
    # Old task failed with opencode
    routing_env.save_task({
        "task_id": "old-hung-task",
        "workflow_id": "wf-penalize-test",
        "status": "failed",
        "agent": "opencode",
    })

    args = MagicMock()
    args.workflow_id = "wf-penalize-test"
    args.agent = "auto"
    args.supersedes = "old-hung-task"
    args.task_type = "docs"
    args.task_id = "new-retry-task"
    args.run_id = "run-2"
    args.dispatch_role = "worker"
    args.allow_reuse = False

    mock_choose = MagicMock(return_value="codex")
    res = resolve_launch_agent(
        args,
        "wrapup",
        routing_env,
        choose_agent=mock_choose,
        workflow_config_for=lambda wid: {},
        is_isolation_rejection=lambda exc: False,
        record_router_failure_task=lambda *a, **k: None,
        release_agent_reservation=lambda *a, **k: None,
    )
    assert res == ("codex", "auto")
    mock_choose.assert_called_once()
    assert mock_choose.call_args[1]["penalize_agents"] == ["opencode"]
