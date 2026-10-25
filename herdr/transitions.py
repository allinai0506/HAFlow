"""Herdr State Machine Transition Rules & Contract.

Functional Core: Pure-logic state transition matrices, validation functions,
and classifications with zero external I/O or system side effects.
"""

from typing import Any, Dict, Set


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

# Task Lifecycle Active Statuses:
# Represents tasks actively in the lifecycle pipeline after launch and before termination.
# Includes paused and interrupted (managed tasks whose lifecycle is not complete).
# Excludes pending (unlaunched/unassigned draft tasks).
# Distinct from herdr.agent_router.ROUTER_OCCUPIED_STATUSES (SC-008), which measures
# worker queue/capacity load and therefore includes pending while excluding paused/interrupted.
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

# ============================================================
# Privileged Task Transitions (ADR-0005 / OEP-0002)
# ============================================================

PRIVILEGED_TASK_TRANSITIONS: Dict[str, Set[tuple[str, str]]] = {
    "delivery-repair": {("completed", "rework")},
    "operator-bypass": {
        ("blocked", "completed"),
        ("suspended", "completed"),
        ("failed", "completed"),
        ("paused", "working"),
    },
    # Discarding a finalize_escalated task's quarantined output. The CLI owns
    # the precondition (explicit --abandon, reason, finalize_escalated flag);
    # candidate assembly excludes escalated tasks, so no published candidate
    # can reference the discarded content.
    "escalation-abandon": {
        *((s, "superseded") for s in TASK_TRANSITIONS if s != "superseded"),
    },
    "rollback": {
        *((s, "superseded") for s in TASK_TRANSITIONS if s != "superseded"),
        ("active", "superseded"),
        ("suspended", "superseded"),
    },
    "console-force-review": {
        *((s, "agent_done") for s in TASK_TRANSITIONS if s not in TERMINAL_TASK_STATUSES),
        ("active", "agent_done"),
    },
}


def validate_task_transition(
    old_status: str,
    new_status: str,
    force: bool = False,
    intent: str | None = None,
) -> bool:
    """Validate whether task transition old_status -> new_status is legal.

    If intent is provided, the transition must match the declared privileged intent contract
    (ADR-0005 / OEP-0002).
    If force is True, edge legality (old_status -> new_status) is bypassed,
    but new_status MUST still be a recognized, valid task status.
    Returns True if valid (including idempotent self-transitions).
    Raises InvalidTransitionError if target status is unknown or transition is illegal.
    """
    if new_status not in TASK_TRANSITIONS:
        raise InvalidTransitionError(f"Invalid target task status: '{new_status}'")

    if old_status == new_status:
        return True

    if intent is not None:
        if intent not in PRIVILEGED_TASK_TRANSITIONS:
            raise InvalidTransitionError(f"Unknown privileged transition intent: '{intent}'")
        allowed_pairs = PRIVILEGED_TASK_TRANSITIONS[intent]
        if (old_status, new_status) in allowed_pairs:
            return True
        if intent == "rollback" and new_status == "superseded":
            return True
        raise InvalidTransitionError(
            f"Illegal privileged transition for intent '{intent}': '{old_status}' -> '{new_status}'"
        )

    if force:
        return True

    allowed = TASK_TRANSITIONS.get(old_status, set())
    if new_status not in allowed:
        raise InvalidTransitionError(
            f"Illegal task transition: '{old_status}' -> '{new_status}' (allowed: {sorted(allowed)})"
        )

    return True


# ============================================================
# Workflow Lifecycle State Machine (ADR-0007 / OEP-0002)
# ============================================================

INITIAL_WORKFLOW_STATUS: str = "running"
UNPROVISIONED_WORKFLOW_STATUS: str = "pending"

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


# ============================================================
# Gate Exemption & Operator Audit Contract (SC-010)
# ============================================================

class InvalidOperatorError(ValueError):
    """Raised when an operator identity violates the operator audit contract (SC-010)."""
    pass


DISALLOWED_OPERATOR_NAMES: Set[str] = {
    "",
    "none",
    "null",
    "nil",
    "undefined",
    "unknown",
    "anonymous",
}


def validate_operator(operator: Any) -> str:
    """Validate and normalize an operator identity for audit-sensitive operations (SC-010).

    Enforces minimum operator identity specifications:
    1. Must not be None, empty, or whitespace-only.
    2. Stripped length must be between 1 and 128 characters.
    3. Must not contain control characters (ASCII < 32).
    4. Must not contain path traversal / separator characters ('/', '\\', '..').
    5. Must not contain script/HTML injection tags ('<', '>').
    6. Case-insensitively rejects anonymous/placeholder tokens ('none', 'null', 'nil',
       'undefined', 'unknown', 'anonymous').

    Returns:
        Cleaned, stripped operator string.
    Raises:
        InvalidOperatorError: If operator does not satisfy requirements.
    """
    if operator is None:
        raise InvalidOperatorError("操作人标识（operator）不能为空或全为空白字符")

    if isinstance(operator, bool):
        raise InvalidOperatorError("操作人标识类型非法，禁止传入布尔值")

    if not isinstance(operator, str):
        if isinstance(operator, (dict, list, tuple, set)):
            raise InvalidOperatorError("操作人标识必须为合法字符串，禁止传入复合数据类型")
        operator = str(operator)

    for ch in operator:
        if ord(ch) < 32:
            raise InvalidOperatorError("操作人标识包含非法控制字符")

    op_clean = operator.strip()
    if not op_clean:
        raise InvalidOperatorError("操作人标识（operator）不能为空或全为空白字符")

    if len(op_clean) > 128:
        raise InvalidOperatorError(f"操作人标识过长（当前 {len(op_clean)} 字符，最大允许 128 字符）")

    if "/" in op_clean or "\\" in op_clean or ".." in op_clean:
        raise InvalidOperatorError("操作人标识包含非法路径遍历或分隔字符")

    if "<" in op_clean or ">" in op_clean:
        raise InvalidOperatorError("操作人标识包含非法字符（'<' 或 '>'）")

    if op_clean.lower() in DISALLOWED_OPERATOR_NAMES:
        raise InvalidOperatorError(f"操作人标识不能使用占位符或匿名标识: '{op_clean}'")

    return op_clean
