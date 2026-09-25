"""HAFlow Collaboration Protocol V1: pure collaboration semantics.

Herdr = runtime / communication. HAFlow = collaboration semantics.
This module never touches sockets, panes, or subprocesses.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

SUPPORTED_TYPES = frozenset({
    "HANDOFF", "REQUEST", "RESULT", "BLOCKER", "REVIEW_REQUEST", "ACK",
})

STATUSES = frozenset({
    "created", "dispatched", "acknowledged", "completed", "failed",
})

_TRANSITIONS = {
    "created": frozenset({"dispatched", "failed"}),
    "dispatched": frozenset({"acknowledged", "failed"}),
    "acknowledged": frozenset({"completed", "failed"}),
    "completed": frozenset(),
    "failed": frozenset(),
}

SUMMARY_MAX = 500
PROMPT_MAX = 2000
NEXT_ACTION_MAX = 500
REFS_MAX = 10
REF_ITEM_MAX = 200

_DETERMINISTIC_ROUTES = {
    "implementation_completed": {"to_agent": "reviewer", "type": "HANDOFF"},
    "review_completed": {"to_agent": "tester", "type": "HANDOFF"},
    "blocker": {"to_agent": "coordinator", "type": "BLOCKER"},
}


def identity_key(run_id: str, from_task_id: str, to_task_id: str,
                 event_type: str, source_fact_id: str) -> str:
    if not all((run_id, from_task_id, to_task_id, event_type, source_fact_id)):
        raise ValueError("run_id, from_task_id, to_task_id, type and source_fact_id are required")
    if event_type not in SUPPORTED_TYPES:
        raise ValueError(f"unsupported collaboration type: {event_type}")
    return f"{run_id}:{from_task_id}:{to_task_id}:{event_type}:{source_fact_id}"


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text[:limit] if len(text) > limit else text


def create_handoff(*, run_id: str, workflow_id: str, from_task_id: str,
                   from_agent: str = "", to_task_id: str, to_agent: str = "",
                   summary: str = "", artifact_refs: Optional[List[str]] = None,
                   evidence_refs: Optional[List[str]] = None,
                   context_refs: Optional[List[str]] = None,
                   source_fact_id: str, event_type: str = "HANDOFF",
                   requires_response: bool = True) -> Dict[str, Any]:
    key = identity_key(run_id, from_task_id, to_task_id, event_type, source_fact_id)
    now = time.time()
    return {
        "event_id": str(uuid.uuid4()),
        "identity_key": key,
        "run_id": run_id,
        "workflow_id": workflow_id,
        "from_task_id": from_task_id,
        "from_agent": from_agent or "",
        "from_pane_id": None,
        "to_task_id": to_task_id,
        "to_agent": to_agent or "",
        "to_pane_id": None,
        "type": event_type,
        "summary": _clip(summary, SUMMARY_MAX),
        "artifact_refs": list(artifact_refs or []),
        "evidence_refs": list(evidence_refs or []),
        "context_refs": list(context_refs or []),
        "requires_response": bool(requires_response),
        "status": "created",
        "source_fact_id": source_fact_id,
        "created_at": now,
        "dispatched_at": None,
        "acknowledged_at": None,
        "completed_at": None,
        "handoff_created_at": now,
        "handoff_dispatched_at": None,
        "handoff_acknowledged_at": None,
        "handoff_completed_at": None,
    }


def build_handoff_prompt(
    event: Dict[str, Any],
    next_action: str = "",
    working_context: Any = None,
) -> str:
    summary = _clip(event.get("summary") or "", SUMMARY_MAX)
    artifacts = [_clip(a, REF_ITEM_MAX) for a in (event.get("artifact_refs") or [])[:REFS_MAX]]
    evidence = [_clip(e, REF_ITEM_MAX) for e in (event.get("evidence_refs") or [])[:REFS_MAX]]
    action = _clip(next_action, NEXT_ACTION_MAX)
    context_refs = list(event.get("context_refs") or [])
    if working_context is not None:
        context_id = getattr(working_context, "context_id", None)
        if context_id is None and isinstance(working_context, dict):
            context_id = working_context.get("context_id")
        if context_id:
            context_refs.insert(0, str(context_id))
    context_refs = list(dict.fromkeys(str(ref) for ref in context_refs if ref))[:3]
    context_refs = [_clip(ref, REF_ITEM_MAX) for ref in context_refs]
    # Tail carries correlation and the context reference: reserve them before
    # clipping the body so a large summary/ref list cannot remove context_id.
    context_tail = "\n".join(
        f"WORKING_CONTEXT_REF: {ref}" for ref in context_refs
    )
    tail = f"HANDOFF_ID: {event.get('event_id') or ''}"
    if context_tail:
        tail += "\n" + context_tail
        tail += "\nLoad the immutable WorkingContext by context_id before continuing."
        tail += "\nherdr-task working-context get --context-id " + context_refs[0]
    body = "\n".join([
        f"HANDOFF FROM: {event.get('from_agent') or ''}",
        f"TASK: {event.get('from_task_id') or ''}",
        "",
        "SUMMARY:",
        summary,
        "",
        "ARTIFACTS:",
        *["- " + str(a) for a in artifacts],
        "",
        "EVIDENCE:",
        *["- " + str(e) for e in evidence],
        "",
        "NEXT ACTION:",
        str(action or ""),
        "",
    ])
    body = _clip(body, PROMPT_MAX - len(tail) - 1)
    return body + "\n" + tail


def route_deterministic_handoff(*, trigger: str) -> Optional[Dict[str, str]]:
    route = _DETERMINISTIC_ROUTES.get(str(trigger or "").lower())
    return dict(route) if route else None


def collab_scope_for_task(task: Dict[str, Any]) -> Optional[str]:
    """Shared workflow execution scope for handoff isolation (fail-closed).

    A collaboration handoff spans tasks, but every ``herdr-task launch``
    mints its own per-task ``run_id`` — so ``task.run_id`` must NEVER be the
    isolation scope, or siblings would always mismatch. V1 scope is the
    workflow execution identity: explicit ``workflow_run_id`` /
    ``execution_id`` when present, else the shared ``workflow_id``.
    Returns None when nothing identifies the execution: callers fail closed.
    """
    if not isinstance(task, dict):
        return None
    return (
        task.get("workflow_run_id")
        or task.get("execution_id")
        or task.get("workflow_id")
        or None
    )


def infer_handoff_trigger(from_node_id: str, to_node_id: str) -> Optional[str]:
    """Map a completed→ready node edge to a deterministic trigger.

    Returns None for edges without a V1 auto-route: those keep the existing
    Coordinator path.
    """
    src = str(from_node_id or "").lower()
    if "implement" in src:
        return "implementation_completed"
    if "review" in src:
        return "review_completed"
    return None


def is_valid_transition(old: str, new: str) -> bool:
    return new in _TRANSITIONS.get(old, frozenset())


def handoff_latency(event: Dict[str, Any]) -> Dict[str, Optional[float]]:
    out: Dict[str, Optional[float]] = {
        "dispatch_latency": None, "ack_latency": None, "handoff_latency": None,
    }
    c = event.get("handoff_created_at")
    d = event.get("handoff_dispatched_at")
    a = event.get("handoff_acknowledged_at")
    f = event.get("handoff_completed_at")
    if c is not None and d is not None:
        out["dispatch_latency"] = float(d) - float(c)
    if d is not None and a is not None:
        out["ack_latency"] = float(a) - float(d)
    if c is not None and f is not None:
        out["handoff_latency"] = float(f) - float(c)
    return out
