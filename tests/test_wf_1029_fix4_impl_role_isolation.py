"""Regression test for wf-haflow-1029-02 Fix 4:
Intra-stage role isolation does not exclude agents in implementation stage across different sub-tasks,
while preserving adversarial role isolation in adversarial nodes (plan/review).
"""

import time
import pytest
from herdr import agent_router as ar, deep_preflight as dp
from herdr.state_store import get_state_store


@pytest.fixture
def routing_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setattr(dp, "preflight_identity", lambda *a, **kw: {"verifiable": True, "fingerprint": "x"})
    for name, filename in [("POOLS_FILE", "pools.json"), ("RESERVATIONS_FILE", "reservations.json"), ("ROUTER_LOCK_FILE", "router.lock")]:
        monkeypatch.setattr(ar, name, tmp_path / filename)

    store = get_state_store(tmp_path / "state.db")
    store.save_workflow({
        "workflow_id": "wf-impl-test",
        "project_id": "p-impl",
        "project_root": str(tmp_path),
        "status": "active",
        "healthy_agents": ["grok", "opencode"],
        "unhealthy_agents": {},
        "preflight_checked_at": time.time(),
    })
    ar.save_pools({
        "projects": {
            "p-impl": {
                "allowed_agents": ["grok", "opencode"],
                "stage_preferences": {
                    "implementation": ["grok", "opencode"],
                    "plan": ["grok", "opencode"],
                },
            }
        }
    })
    return store


def test_implementation_stage_allows_remediation_with_custom_role(routing_env, monkeypatch):
    monkeypatch.setattr(ar, "preflight_snapshot_fresh", lambda *a, **kw: True)
    monkeypatch.setattr(ar, "workflow_config_for", lambda _: {
        "nodes": [
            {
                "id": "implementation",
                "parallel": True,
                "agent_policy": {"preferred": ["grok", "opencode"]},
            }
        ]
    })

    # Task 1 in implementation had dispatch_role="console-visibility" executed by grok
    routing_env.save_task({
        "task_id": "impl-console",
        "workflow_id": "wf-impl-test",
        "project_id": "p-impl",
        "node": "implementation",
        "dispatch_role": "console-visibility",
        "agent": "grok",
        "status": "completed",
    })

    # Task 2 is a remediation task with dispatch_role="worker" or None, requesting grok
    selected = ar.choose_agent(
        "wf-impl-test",
        "implementation",
        "feat",
        requested="grok",
        dispatch_role="worker",
        reservation_key="impl-remediation",
    )
    assert selected == "grok"


def test_adversarial_stage_still_isolates_conflicting_roles(routing_env, monkeypatch):
    monkeypatch.setattr(ar, "preflight_snapshot_fresh", lambda *a, **kw: True)
    monkeypatch.setattr(ar, "workflow_config_for", lambda _: {
        "nodes": [
            {
                "id": "plan",
                "parallel": True,
                "agent_policy": {
                    "preferred": ["grok", "opencode"],
                    "roles": [{"name": "executor"}, {"name": "challenger"}],
                },
            }
        ]
    })

    # Task 1 in plan was executor executed by grok
    routing_env.save_task({
        "task_id": "plan-exec",
        "workflow_id": "wf-impl-test",
        "project_id": "p-impl",
        "node": "plan",
        "dispatch_role": "executor",
        "agent": "grok",
        "status": "completed",
    })

    # Task 2 is challenger role: must NOT choose grok, must choose opencode
    selected = ar.choose_agent(
        "wf-impl-test",
        "plan",
        "docs",
        requested="auto",
        dispatch_role="challenger",
        reservation_key="plan-chal",
    )
    assert selected == "opencode"
