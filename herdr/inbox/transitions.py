"""Inbox task status constants, priorities, and transition validation."""

PRIORITY_WEIGHTS = {
    "URGENT": 4,
    "HIGH": 3,
    "NORMAL": 2,
    "LOW": 1,
}

PRIORITY_DEFAULT = "NORMAL"
VALID_PRIORITIES = frozenset(PRIORITY_WEIGHTS.keys())

ALL_INBOX_STATUSES = frozenset({
    "DRAFT",
    "PENDING",
    "CLAIMED",
    "RUNNING",
    "WAITING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
})

ACTIVE_INBOX_STATUSES = frozenset({
    "PENDING",
    "CLAIMED",
    "RUNNING",
    "WAITING",
})

# Statuses where a task is occupying or about to occupy a workspace
WORKSPACE_HOLDING_STATUSES = frozenset({
    "CLAIMED",
    "RUNNING",
})

TERMINAL_INBOX_STATUSES = frozenset({
    "COMPLETED",
    "FAILED",
    "CANCELLED",
})

INBOX_TRANSITIONS = {
    "DRAFT": {"PENDING", "CANCELLED"},
    "PENDING": {"CLAIMED", "CANCELLED"},
    "CLAIMED": {"RUNNING", "PENDING", "COMPLETED", "FAILED", "CANCELLED"},
    "RUNNING": {"WAITING", "COMPLETED", "FAILED", "CANCELLED"},
    "WAITING": {"RUNNING", "COMPLETED", "FAILED", "CANCELLED"},
    "COMPLETED": set(),
    "FAILED": {"PENDING"},  # retry
    "CANCELLED": set(),
}


class InvalidInboxTransitionError(ValueError):
    """Raised when an illegal status transition is attempted on an inbox task."""
    pass


def validate_inbox_transition(current_status: str, new_status: str) -> None:
    """Validate that moving from current_status to new_status is legal.
    
    Raises:
        InvalidInboxTransitionError if the transition is disallowed.
    """
    cur = str(current_status).upper()
    nxt = str(new_status).upper()
    if cur not in ALL_INBOX_STATUSES:
        raise InvalidInboxTransitionError(f"Unknown current status: {cur}")
    if nxt not in ALL_INBOX_STATUSES:
        raise InvalidInboxTransitionError(f"Unknown target status: {nxt}")
    if cur == nxt:
        return
    allowed = INBOX_TRANSITIONS.get(cur, set())
    if nxt not in allowed:
        raise InvalidInboxTransitionError(
            f"Illegal inbox task transition: cannot move from '{cur}' to '{nxt}'. Allowed: {sorted(allowed)}"
        )
