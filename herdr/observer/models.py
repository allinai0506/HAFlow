"""Trajectory Finding domain model (herdr/observer/models.py).

Findings are *analysis*, never facts: the facts live in the Trajectory Ledger
(``herdr/trajectory.py``). The model keeps the two worlds apart by using a
dedicated structure and a dedicated store, and by giving every finding a
stable ``finding_key`` so repeated observations never duplicate one issue.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

FINDING_TYPES = (
    "stalled_execution",
    "repeated_failure",
    "repeated_action",
    "no_progress",
    "verification_failure",
    "runtime_unavailable",
    "possible_context_problem",
    "other",
)

SEVERITIES = ("info", "warning", "critical")

RECOMMENDED_ACTIONS = (
    "continue",
    "inspect",
    "replan",
    "retry",
    "change_agent",
    "request_human",
    "interrupt",
)

FINDING_STATUSES = ("open",)


def new_finding_id() -> str:
    return f"fnd_{uuid.uuid4().hex[:16]}"


def finding_key_for(
    run_id: str,
    finding_type: str,
    node: Optional[str] = None,
    agent_session_id: Optional[str] = None,
    anchor: str = "",
) -> str:
    """Stable dedup key for one finding episode.

    ``anchor`` identifies the concrete evidence episode (the last event before
    a stall, the first event of a failure chain, ...) so the same problem seen
    again by a later observation maps onto the same key, while a genuinely new
    episode produces a new one.
    """
    payload = "|".join([
        str(run_id or ""),
        str(finding_type or ""),
        str(node or ""),
        str(agent_session_id or ""),
        str(anchor or ""),
    ])
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"fk_{digest}"


@dataclass
class TrajectoryFinding:
    """One structured, evidence-backed observation result."""

    run_id: str
    finding_type: str
    severity: str
    summary: str
    finding_id: str = ""
    finding_key: str = ""
    task_id: Optional[str] = None
    workflow_id: Optional[str] = None
    node: Optional[str] = None
    agent: Optional[str] = None
    agent_session_id: Optional[str] = None
    created_at: float = 0.0
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    suspected_cause: Optional[str] = None
    recommended_action: str = "inspect"
    confidence: float = 0.0
    status: str = "open"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.finding_type not in FINDING_TYPES:
            raise ValueError(f"unknown finding_type: {self.finding_type!r}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown severity: {self.severity!r}")
        if self.recommended_action not in RECOMMENDED_ACTIONS:
            raise ValueError(f"unknown recommended_action: {self.recommended_action!r}")
        if self.status not in FINDING_STATUSES:
            raise ValueError(f"unknown status: {self.status!r}")
        if not self.finding_id:
            self.finding_id = new_finding_id()
        if not self.finding_key:
            self.finding_key = finding_key_for(
                self.run_id, self.finding_type, self.node, self.agent_session_id,
                self.metadata.get("anchor", ""),
            )
        self.confidence = max(0.0, min(1.0, float(self.confidence or 0.0)))

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "finding_key": self.finding_key,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "workflow_id": self.workflow_id,
            "node": self.node,
            "agent": self.agent,
            "agent_session_id": self.agent_session_id,
            "created_at": self.created_at,
            "finding_type": self.finding_type,
            "severity": self.severity,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "suspected_cause": self.suspected_cause,
            "recommended_action": self.recommended_action,
            "confidence": self.confidence,
            "status": self.status,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(cls, raw: Dict[str, Any]) -> "TrajectoryFinding":
        if not isinstance(raw, dict):
            raise TypeError("finding must be a dict")
        known = {
            "finding_id", "finding_key", "run_id", "task_id", "workflow_id", "node",
            "agent", "agent_session_id", "created_at", "finding_type", "severity",
            "summary", "evidence", "suspected_cause", "recommended_action",
            "confidence", "status", "metadata",
        }
        values = {key: raw[key] for key in known if key in raw}
        values.setdefault("evidence", [])
        values.setdefault("metadata", {})
        return cls(**values)
