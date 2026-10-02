"""Herdr State Machine Transition Rules & Contract.

Functional Core: Pure-logic state transition matrices, validation functions,
and classifications with zero external I/O or system side effects.
"""

from typing import Dict, Set


class InvalidTransitionError(ValueError):
    """Raised when an illegal state transition is attempted."""
    pass


# ============================================================
# Task Lifecycle State Machine
# ============================================================

TASK_TRANSITIONS: Dict[str, Set[str]] = {
    "pending": {"dispatched", "failed"},
    "dispatched": {"working", "blocked", "agent_done", "paused", "failed", "superseded", "interrupted"},
    "working": {"blocked", "agent_done", "rework", "paused", "failed", "superseded", "interrupted"},
    "blocked": {"rework", "working", "agent_done", "paused", "failed", "superseded", "interrupted"},
    "paused": {"working", "rework", "agent_done", "failed", "superseded", "interrupted"},
    "agent_done": {"completed", "rework", "failed", "superseded", "interrupted"},
    "rework": {"working", "blocked", "agent_done", "paused", "failed", "superseded", "interrupted"},
    "interrupted": {"working", "rework", "paused", "failed", "superseded"},
    "completed": {"committed", "cleanup_ready"},
    "committed": {"integrated"},
    "integrated": {"cleanup_ready"},
    "cleanup_ready": {"cleaned"},
    "cleaned": {"superseded"},
    "failed": {"superseded"},
    "superseded": set(),
}

ACTIVE_TASK_STATUSES: Set[str] = {
    "dispatched",
    "working",
    "blocked",
    "agent_done",
    "rework",
    "paused",
    "interrupted",
}

COMPLETED_TASK_STATUSES: Set[str] = {
    "completed",
    "committed",
    "integrated",
    "cleanup_ready",
    "cleaned",
}

TERMINAL_TASK_STATUSES: Set[str] = {
    *COMPLETED_TASK_STATUSES,
    "failed",
    "superseded",
}


def validate_task_transition(old_status: str, new_status: str, force: bool = False) -> bool:
    """Validate whether task transition old_status -> new_status is legal.

    If force is True, edge legality (old_status -> new_status) is bypassed,
    but new_status MUST still be a recognized, valid task status.
    Returns True if valid (including idempotent self-transitions).
    Raises InvalidTransitionError if target status is unknown or transition is illegal.
    """
    if new_status not in TASK_TRANSITIONS:
        raise InvalidTransitionError(f"Invalid target task status: '{new_status}'")

    if old_status == new_status or force:
        return True

    allowed = TASK_TRANSITIONS.get(old_status, set())
    if new_status not in allowed:
        raise InvalidTransitionError(
            f"Illegal task transition: '{old_status}' -> '{new_status}' (allowed: {sorted(allowed)})"
        )

    return True


# ============================================================
# Workflow Lifecycle State Machine
# ============================================================

WORKFLOW_TRANSITIONS: Dict[str, Set[str]] = {
    "pending": {"running", "in_progress", "paused", "failed"},
    "running": {"paused", "closing", "completed", "failed", "in_progress"},
    "in_progress": {"paused", "closing", "completed", "failed", "running"},
    "paused": {"running", "in_progress", "failed"},
    "closing": {"completed", "failed"},
    "completed": {"in_progress", "running", "closing"},
    "failed": {"in_progress", "running"},
}

ACTIVE_WORKFLOW_STATUSES: Set[str] = {
    "pending",
    "running",
    "in_progress",
    "paused",
    "closing",
}

TERMINAL_WORKFLOW_STATUSES: Set[str] = {
    "completed",
}


def validate_workflow_transition(old_status: str, new_status: str, force: bool = False) -> bool:
    """Validate whether workflow transition old_status -> new_status is legal.

    If force is True, edge legality (old_status -> new_status) is bypassed,
    but new_status MUST still be a recognized, valid workflow status.
    Returns True if valid (including idempotent self-transitions).
    Raises InvalidTransitionError if target status is unknown or transition is illegal.
    """
    if new_status not in WORKFLOW_TRANSITIONS:
        raise InvalidTransitionError(f"Invalid target workflow status: '{new_status}'")

    if old_status == new_status or force:
        return True

    allowed = WORKFLOW_TRANSITIONS.get(old_status, set())
    if new_status not in allowed:
        raise InvalidTransitionError(
            f"Illegal workflow transition: '{old_status}' -> '{new_status}' (allowed: {sorted(allowed)})"
        )

    return True
