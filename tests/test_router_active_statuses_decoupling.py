"""Tests for SC-008: Decoupling of router occupied statuses and lifecycle active statuses."""

from unittest.mock import MagicMock, patch

from herdr.agent_router import (
    ROUTER_ACTIVE_STATUSES,
    ROUTER_OCCUPIED_STATUSES,
    _active_agent_loads,
    _agent_occupied_loads,
)
from herdr.transitions import ACTIVE_TASK_STATUSES


def test_status_set_definitions_and_aliases():
    """Verify exact membership and alias equality for SC-008 status sets."""
    expected_router = {
        "pending",
        "dispatched",
        "working",
        "blocked",
        "agent_done",
        "rework",
    }
    expected_lifecycle = {
        "dispatched",
        "working",
        "blocked",
        "agent_done",
        "rework",
        "paused",
        "interrupted",
    }

    assert ROUTER_OCCUPIED_STATUSES == expected_router
    assert ROUTER_ACTIVE_STATUSES == expected_router
    assert ACTIVE_TASK_STATUSES == expected_lifecycle


def test_status_set_semantic_differences():
    """Verify orthogonal differences between routing load and lifecycle activity.

    - pending occupies router capacity/queue depth, but is not yet active in lifecycle.
    - paused/interrupted remain active in task lifecycle, but do not consume active execution capacity.
    - their intersection represents actively executing states.
    """
    assert ROUTER_OCCUPIED_STATUSES - ACTIVE_TASK_STATUSES == {"pending"}
    assert ACTIVE_TASK_STATUSES - ROUTER_OCCUPIED_STATUSES == {"paused", "interrupted"}
    assert ROUTER_OCCUPIED_STATUSES & ACTIVE_TASK_STATUSES == {
        "dispatched",
        "working",
        "blocked",
        "agent_done",
        "rework",
    }


def test_agent_loads_calculation_respects_sc008_boundary():
    """Verify that _active_agent_loads counts pending, but excludes paused and interrupted."""
    mock_store = MagicMock()
    mock_store.list_tasks.return_value = [
        {"project_id": "p1", "agent": "codex", "status": "pending"},      # counts (+1)
        {"project_id": "p1", "agent": "codex", "status": "working"},      # counts (+1)
        {"project_id": "p1", "agent": "codex", "status": "paused"},       # excludes (0)
        {"project_id": "p1", "agent": "codex", "status": "interrupted"},  # excludes (0)
        {"project_id": "p1", "agent": "codex", "status": "completed"},    # excludes (0)
        {"project_id": "p1", "agent": "claude", "status": "rework"},      # counts (+1)
        {"project_id": "p1", "agent": "claude", "status": "blocked"},     # counts (+1)
        {"project_id": "p1", "agent": "claude", "status": "agent_done"},  # counts (+1)
        {"project_id": "p1", "agent": "claude", "status": "failed"},      # excludes (0)
        {"project_id": "other", "agent": "codex", "status": "working"},   # different project (0)
    ]

    with patch("herdr.agent_router._get_store", return_value=mock_store):
        loads = _active_agent_loads("p1")
        alias_loads = _agent_occupied_loads("p1")

    assert loads == {"codex": 2, "claude": 3}
    assert alias_loads == loads
