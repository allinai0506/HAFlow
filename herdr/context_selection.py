"""Deterministic role-aware relevance and candidate selection."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from .context_models import _safe_float, normalize_agent_role


# ---------------------------------------------------------------------------
# Pure relevance and selection


def context_relevance(
    item: Mapping[str, Any],
    *,
    agent_role: str,
    current_state: Mapping[str, Any],
    dependency_ids: Sequence[str] = (),
    current_node_id: str = "",
    now: Optional[float] = None,
) -> float:
    """Return a deterministic relevance score for one candidate item.

    Validity and state relevance dominate role/dependency relevance; recency is
    only the final tie-breaker.  The function performs no I/O and creates no
    model judgment.
    """
    role = normalize_agent_role(agent_role)
    kind = str(item.get("kind") or "")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    validity = 0.0 if metadata.get("valid") is False else 1.0
    state = 0.0
    if kind in {"blocker", "open_question"}:
        state += 5.0
    if kind == "verification":
        state += 4.0
    if kind == "finding":
        severity = str(item.get("severity") or metadata.get("severity") or "info")
        state += {"critical": 3.0, "warning": 2.0, "info": 1.0}.get(severity, 0.5)
    if str(metadata.get("status") or "") in {"blocked", "failed"}:
        state += 2.0
    if str(item.get("source_task") or "") == str(current_state.get("task_id") or ""):
        state += 1.0
    if current_node_id and str(metadata.get("node") or item.get("node") or "") == current_node_id:
        state += 0.5

    role_kinds = {
        "developer": {"completed", "artifact", "evidence", "finding", "blocker", "open_question", "verification", "handoff"},
        "reviewer": {"artifact", "finding", "evidence", "verification", "handoff", "blocker", "open_question"},
        "tester": {"artifact", "evidence", "finding", "verification", "blocker", "handoff"},
        "coordinator": {"completed", "decision", "blocker", "open_question", "handoff", "finding", "verification"},
    }[role]
    role_score = (2.0 if kind in role_kinds else 0.0)
    if role == "reviewer" and kind in {"artifact", "finding", "verification"}:
        role_score += 0.5
    if role == "tester" and kind in {"evidence", "verification"}:
        role_score += 0.5
    if role == "coordinator" and kind in {"blocker", "handoff", "decision"}:
        role_score += 0.5

    dependency_score = 0.0
    source_task = str(item.get("source_task") or "")
    if source_task and source_task in set(str(value) for value in dependency_ids):
        dependency_score += 2.0
    if metadata.get("dependency_relevant"):
        dependency_score += 1.0

    created_at = _safe_float(item.get("created_at"), 0.0)
    recency = 0.0
    if created_at and now is not None:
        age = max(0.0, float(now) - created_at)
        recency = max(0.0, 2.0 - age / 86400.0)
    return validity * 1000.0 + state * 100.0 + role_score * 10.0 + dependency_score * 5.0 + recency


def _role_allows(
    kind: str,
    item: Mapping[str, Any],
    *,
    role: str,
    target_task_id: str,
    dependency_ids: Sequence[str],
) -> bool:
    source_task = str(item.get("source_task") or "")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    severity = str(metadata.get("severity") or item.get("severity") or "info")
    status = str(metadata.get("status") or item.get("status") or "")
    if role == "coordinator":
        if kind == "finding" and severity != "critical" and status not in {"blocked", "failed"}:
            return False
        if kind in {"evidence", "artifact"} and len(dependency_ids) > 2:
            return False
    if role == "tester":
        if kind == "finding" and not item.get("evidence_refs") and metadata.get("finding_type") not in {
            "verification_failure", "repeated_failure", "runtime_unavailable",
        }:
            return False
        if kind == "completed":
            return False
    if role == "reviewer":
        if kind == "finding" and source_task == target_task_id and metadata.get("node") == "implementation":
            return False
    if role == "developer":
        if kind == "decision" and source_task != target_task_id:
            return False
    return True


def _select_items(
    items: Sequence[Mapping[str, Any]],
    *,
    kind: str,
    role: str,
    current_state: Mapping[str, Any],
    target_task_id: str,
    dependency_ids: Sequence[str],
    limit: int,
    now: float,
) -> List[Dict[str, Any]]:
    filtered = [
        dict(item) for item in items
        if item.get("kind") == kind
        and _role_allows(
            kind,
            item,
            role=role,
            target_task_id=target_task_id,
            dependency_ids=dependency_ids,
        )
    ]
    filtered.sort(
        key=lambda item: (
            -context_relevance(
                item,
                agent_role=role,
                current_state=current_state,
                dependency_ids=dependency_ids,
                current_node_id=str(current_state.get("current_node") or ""),
                now=now,
            ),
            str(item.get("source_ref") or ""),
        )
    )
    return filtered[: max(0, int(limit))]


