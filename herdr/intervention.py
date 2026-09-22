"""Durable Supervisor-to-Controller Action Protocol V1."""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Dict, Iterable, List, Optional

ACTION_VERIFY = "VERIFY"
ACTION_RETRY = "RETRY"
SUPPORTED_ACTIONS = frozenset({ACTION_VERIFY, ACTION_RETRY})

STATUS_REQUESTED = "requested"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_SUPERSEDED = "superseded"
STATUSES = frozenset({
    STATUS_REQUESTED, STATUS_RUNNING, STATUS_COMPLETED,
    STATUS_FAILED, STATUS_SUPERSEDED,
})


def identity_key(run_id: str, task_id: str, decision_id: str, action: str) -> str:
    if not all((run_id, task_id, decision_id, action)):
        raise ValueError("run_id, task_id, decision_id and action are required")
    if action not in SUPPORTED_ACTIONS:
        raise ValueError(f"unsupported intervention action: {action}")
    return f"{run_id}:{task_id}:{decision_id}:{action}"


@dataclass(frozen=True)
class Intervention:
    intervention_id: str
    identity_key: str
    run_id: str
    workflow_id: Optional[str]
    task_id: str
    evaluation_id: str
    decision_id: str
    action: str
    reason: str = ""
    finding_refs: List[str] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    status: str = STATUS_REQUESTED
    attempt: int = 0
    max_attempts: int = 0
    requested_at: float = 0.0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    execution_owner: Optional[str] = None
    lease_until: Optional[float] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None

    @classmethod
    def from_mapping(cls, raw: Dict[str, Any]) -> "Intervention":
        action = str(raw.get("action") or "")
        status = str(raw.get("status") or STATUS_REQUESTED)
        if action not in SUPPORTED_ACTIONS:
            raise ValueError(f"unsupported intervention action: {action}")
        if status not in STATUSES:
            raise ValueError(f"unsupported intervention status: {status}")
        run_id = str(raw.get("run_id") or "")
        task_id = str(raw.get("task_id") or "")
        decision_id = str(raw.get("decision_id") or "")
        if not run_id or not task_id or not decision_id:
            raise ValueError("run_id, task_id and decision_id are required")
        expected_key = identity_key(run_id, task_id, decision_id, action)
        key = str(raw.get("identity_key") or expected_key)
        if key != expected_key:
            raise ValueError("identity_key does not match intervention identity")
        return cls(
            intervention_id=str(raw.get("intervention_id") or ""),
            identity_key=key,
            run_id=run_id,
            workflow_id=raw.get("workflow_id"),
            task_id=task_id,
            evaluation_id=str(raw.get("evaluation_id") or decision_id),
            decision_id=decision_id,
            action=action,
            reason=str(raw.get("reason") or ""),
            finding_refs=[str(value) for value in (raw.get("finding_refs") or [])],
            evidence_refs=[str(value) for value in (raw.get("evidence_refs") or [])],
            status=status,
            attempt=int(raw.get("attempt") or 0),
            max_attempts=int(raw.get("max_attempts") or 0),
            requested_at=float(raw.get("requested_at") or 0.0),
            started_at=raw.get("started_at"),
            finished_at=raw.get("finished_at"),
            execution_owner=raw.get("execution_owner"),
            lease_until=raw.get("lease_until"),
            result=dict(raw["result"]) if isinstance(raw.get("result"), dict) else raw.get("result"),
            error=dict(raw["error"]) if isinstance(raw.get("error"), dict) else raw.get("error"),
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "intervention_id": self.intervention_id,
            "identity_key": self.identity_key,
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "task_id": self.task_id,
            "evaluation_id": self.evaluation_id,
            "decision_id": self.decision_id,
            "action": self.action,
            "reason": self.reason,
            "finding_refs": list(self.finding_refs),
            "evidence_refs": list(self.evidence_refs),
            "status": self.status,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "requested_at": self.requested_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "execution_owner": self.execution_owner,
            "lease_until": self.lease_until,
            "result": self.result,
            "error": self.error,
        }


def build_request(
    *, run_id: str, workflow_id: Optional[str], task_id: str,
    evaluation_id: str, decision_id: str, action: str, reason: str = "",
    finding_refs: Optional[Iterable[str]] = None,
    evidence_refs: Optional[Iterable[str]] = None,
    attempt: int = 0, max_attempts: int = 0, now: Optional[float] = None,
) -> Dict[str, Any]:
    timestamp = time.time() if now is None else float(now)
    return Intervention(
        intervention_id="",
        identity_key=identity_key(run_id, task_id, decision_id, action),
        run_id=run_id,
        workflow_id=workflow_id,
        task_id=task_id,
        evaluation_id=evaluation_id,
        decision_id=decision_id,
        action=action,
        reason=reason,
        finding_refs=list(finding_refs or []),
        evidence_refs=list(evidence_refs or []),
        attempt=int(attempt),
        max_attempts=int(max_attempts),
        requested_at=timestamp,
    ).to_mapping()


def attempt_count_for_task(task: Dict[str, Any]) -> int:
    """Read the existing retry count fields without creating a new counter."""
    fix_loop = task.get("fix_loop") if isinstance(task.get("fix_loop"), dict) else {}
    explicit = task.get("attempt_count") or fix_loop.get("loop_count")
    if explicit is not None:
        return int(explicit)
    history = task.get("status_history")
    if isinstance(history, list):
        return sum(1 for entry in history if isinstance(entry, dict) and entry.get("to") == "rework")
    return 0


def request_intervention(store: Any, task: Dict[str, Any], evaluation: Dict[str, Any],
                         decision: Dict[str, Any], config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Persist one enforced VERIFY/RETRY request without executing it."""
    if not config.get("enabled", False) or not config.get("enforce", False):
        return None
    action = str(decision.get("action") or "")
    if action not in SUPPORTED_ACTIONS:
        return None
    from .trajectory import run_id_for_task

    run_id = str(run_id_for_task(task))
    decision_run_id = decision.get("run_id")
    if decision_run_id and str(decision_run_id) != run_id:
        raise ValueError("intervention decision run_id does not match task run")
    decision_id = str(decision.get("decision_id") or decision.get("evaluation_id") or "")
    evaluation_id = str(evaluation.get("evaluation_id") or decision.get("evaluation_id") or decision_id)
    metadata = evaluation.get("metadata") if isinstance(evaluation.get("metadata"), dict) else {}
    policy = config.get("policy") if isinstance(config.get("policy"), dict) else {}
    task_attempt = attempt_count_for_task(task)
    max_attempts = int(policy.get("max_attempts", 0))
    requested = store.create_intervention(build_request(
        run_id=run_id,
        workflow_id=task.get("workflow_id"),
        task_id=str(task.get("task_id") or ""),
        evaluation_id=evaluation_id,
        decision_id=decision_id,
        action=action,
        reason=str(decision.get("reason") or "; ".join(decision.get("reasons") or [])),
        finding_refs=metadata.get("finding_refs") or decision.get("finding_refs") or [],
        evidence_refs=metadata.get("evidence_refs") or decision.get("evidence_refs") or [],
        attempt=int(task_attempt),
        max_attempts=max_attempts,
    ))
    if (
        action == ACTION_RETRY
        and requested.get("status") == STATUS_REQUESTED
        and max_attempts > 0
        and int(task_attempt) >= max_attempts
    ):
        return store.fail_intervention(
            requested["intervention_id"],
            {
                "code": "retry_budget_exhausted",
                "attempt_count": int(task_attempt),
                "max_attempts": max_attempts,
            },
        )
    return requested


__all__ = [
    "ACTION_RETRY", "ACTION_VERIFY", "SUPPORTED_ACTIONS", "STATUSES",
    "STATUS_COMPLETED", "STATUS_FAILED", "STATUS_REQUESTED", "STATUS_RUNNING",
    "STATUS_SUPERSEDED", "Intervention", "build_request", "identity_key",
    "request_intervention", "attempt_count_for_task",
]
