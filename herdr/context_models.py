"""Immutable WorkingContext value objects and deterministic helpers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .observation import _redact_value
from .trajectory import run_id_for_task


MAX_TEXT_CHARS = 1000
MAX_METADATA_CHARS = 1000
DEFAULT_MAX_ITEMS = 40
DEFAULT_MAX_CHARS = 12_000
DEFAULT_MAX_ITEMS_PER_KIND = {
    "completed": 8,
    "artifacts": 10,
    "evidence": 10,
    "findings": 10,
    "decisions": 5,
    "blockers": 5,
    "open_questions": 5,
    "verification": 10,
    "handoffs": 3,
}
SOURCE_PREFIXES = frozenset({
    "task",
    "workflow",
    "trajectory",
    "finding",
    "observation",
    "collaboration",
    "eval",
    "policy",
    "evidence",
    "artifact",
})
ROLE_ALIASES = {
    "developer": "developer",
    "dev": "developer",
    "implementer": "developer",
    "implementation": "developer",
    "engineer": "developer",
    "reviewer": "reviewer",
    "review": "reviewer",
    "code_reviewer": "reviewer",
    "tester": "tester",
    "test": "tester",
    "qa": "tester",
    "verifier": "tester",
    "coordinator": "coordinator",
    "coord": "coordinator",
    "orchestrator": "coordinator",
}
TERMINAL_COMPLETION_EVENTS = frozenset({
    "task_completed",
    "run_completed",
})
VERIFICATION_EVENTS = frozenset({"verification_completed", "tests_completed"})
RELEVANT_EVENT_TYPES = frozenset({
    "run_started",
    "run_completed",
    "run_failed",
    "task_started",
    "task_completed",
    "task_failed",
    "task_status_changed",
    "agent_done",
    "agent_failed",
    "artifact_created",
    "verification_completed",
    "tests_completed",
    "blocker",
    "decision",
    "decision_completed",
    "review_requested",
    "verification_requested",
})

# ---------------------------------------------------------------------------
# Public value objects


@dataclass(frozen=True)
class ContextItem:
    """One bounded, provenance-bearing context item."""

    kind: str
    value: Any
    source_ref: str
    source_task: Optional[str] = None
    source_run: Optional[str] = None
    created_at: Optional[float] = None
    evidence_refs: Tuple[str, ...] = ()
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("context item kind is required")
        if not _valid_source_ref(self.source_ref):
            raise ValueError(f"invalid context source_ref: {self.source_ref!r}")
        object.__setattr__(self, "evidence_refs", tuple(str(ref) for ref in self.evidence_refs if ref))
        if not isinstance(self.metadata, dict):
            raise ValueError("context item metadata must be a dict")
        object.__setattr__(self, "value", _redact_value(self.value))
        object.__setattr__(self, "metadata", _redact_value(dict(self.metadata)))

    def to_mapping(self) -> Dict[str, Any]:
        value = {
            "kind": self.kind,
            "value": self.value,
            "source_ref": self.source_ref,
            "source_task": self.source_task,
            "source_run": self.source_run,
            "created_at": self.created_at,
            "evidence_refs": list(self.evidence_refs),
            "metadata": dict(self.metadata),
        }
        return {key: item for key, item in value.items() if item is not None and item != {} and item != []}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ContextItem":
        return cls(
            kind=str(raw.get("kind") or ""),
            value=raw.get("value"),
            source_ref=str(raw.get("source_ref") or ""),
            source_task=raw.get("source_task"),
            source_run=raw.get("source_run"),
            created_at=raw.get("created_at"),
            evidence_refs=tuple(raw.get("evidence_refs") or ()),
            metadata=dict(raw.get("metadata") or {}),
        )


@dataclass(frozen=True)
class WorkingContext:
    """Immutable execution-boundary projection for one task and role."""

    context_id: str
    run_scope: str
    run_id: Optional[str]
    workflow_id: str
    task_id: str
    node_id: Optional[str]
    agent_role: str
    goal: str
    current_state: Dict[str, Any]
    completed: List[Dict[str, Any]] = field(default_factory=list)
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    findings: List[Dict[str, Any]] = field(default_factory=list)
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    blockers: List[Dict[str, Any]] = field(default_factory=list)
    open_questions: List[Dict[str, Any]] = field(default_factory=list)
    verification: List[Dict[str, Any]] = field(default_factory=list)
    handoffs: List[Dict[str, Any]] = field(default_factory=list)
    next_action: str = ""
    source_refs: List[str] = field(default_factory=list)
    goal_source_ref: str = ""
    current_state_refs: Dict[str, str] = field(default_factory=dict)
    next_action_source_ref: str = ""
    context_fingerprint: str = ""
    source_version: str = ""
    source_watermark: int = 0
    metrics: Dict[str, Any] = field(default_factory=dict)
    compiled_at: float = 0.0

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "context_id": self.context_id,
            "run_scope": self.run_scope,
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "agent_role": self.agent_role,
            "goal": self.goal,
            "goal_source_ref": self.goal_source_ref,
            "current_state": dict(self.current_state),
            "current_state_refs": dict(self.current_state_refs),
            "completed": list(self.completed),
            "artifacts": list(self.artifacts),
            "evidence": list(self.evidence),
            "findings": list(self.findings),
            "decisions": list(self.decisions),
            "blockers": list(self.blockers),
            "open_questions": list(self.open_questions),
            "verification": list(self.verification),
            "handoffs": list(self.handoffs),
            "next_action": self.next_action,
            "next_action_source_ref": self.next_action_source_ref,
            "source_refs": list(self.source_refs),
            "context_fingerprint": self.context_fingerprint,
            "source_version": self.source_version,
            "source_watermark": self.source_watermark,
            "metrics": dict(self.metrics),
            "compiled_at": self.compiled_at,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "WorkingContext":
        return cls(
            context_id=str(raw["context_id"]),
            run_scope=str(raw["run_scope"]),
            run_id=raw.get("run_id"),
            workflow_id=str(raw["workflow_id"]),
            task_id=str(raw["task_id"]),
            node_id=raw.get("node_id"),
            agent_role=str(raw["agent_role"]),
            goal=str(raw.get("goal") or ""),
            goal_source_ref=str(raw.get("goal_source_ref") or ""),
            current_state=dict(raw.get("current_state") or {}),
            current_state_refs=dict(raw.get("current_state_refs") or {}),
            completed=list(raw.get("completed") or []),
            artifacts=list(raw.get("artifacts") or []),
            evidence=list(raw.get("evidence") or []),
            findings=list(raw.get("findings") or []),
            decisions=list(raw.get("decisions") or []),
            blockers=list(raw.get("blockers") or []),
            open_questions=list(raw.get("open_questions") or []),
            verification=list(raw.get("verification") or []),
            handoffs=list(raw.get("handoffs") or []),
            next_action=str(raw.get("next_action") or ""),
            next_action_source_ref=str(raw.get("next_action_source_ref") or ""),
            source_refs=list(raw.get("source_refs") or []),
            context_fingerprint=str(raw.get("context_fingerprint") or ""),
            source_version=str(raw.get("source_version") or ""),
            source_watermark=int(raw.get("source_watermark") or 0),
            metrics=dict(raw.get("metrics") or {}),
            compiled_at=float(raw.get("compiled_at") or 0.0),
        )


# ---------------------------------------------------------------------------
# Small deterministic helpers


def _valid_source_ref(value: Any) -> bool:
    if not isinstance(value, str) or ":" not in value:
        return False
    prefix, rest = value.split(":", 1)
    return prefix in SOURCE_PREFIXES and bool(rest.strip())


def normalize_agent_role(value: str) -> str:
    role = ROLE_ALIASES.get(str(value or "").strip().casefold())
    if role is None:
        raise ValueError(f"unsupported agent_role: {value!r}")
    return role


def infer_agent_role(task: Mapping[str, Any]) -> str:
    explicit = task.get("agent_role")
    if explicit:
        return normalize_agent_role(str(explicit))
    text = " ".join(str(task.get(key) or "") for key in ("node", "stage", "agent", "role")).casefold()
    if "review" in text:
        return "reviewer"
    if "test" in text or "qa" in text or "verif" in text:
        return "tester"
    if "coord" in text or "orchestr" in text:
        return "coordinator"
    return "developer"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _clip_text(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    text = str(_redact_value(value) or "")
    return text[: max(0, int(limit))]


def _bound_value(value: Any, limit: int = MAX_TEXT_CHARS, depth: int = 0) -> Any:
    if depth > 4:
        return "[bounded]"
    if isinstance(value, str):
        return _clip_text(value, limit)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_bound_value(item, limit, depth + 1) for item in value[:20]]
    if isinstance(value, tuple):
        return [_bound_value(item, limit, depth + 1) for item in value[:20]]
    if isinstance(value, dict):
        return {
            str(key): _bound_value(item, limit, depth + 1)
            for key, item in list(value.items())[:30]
        }
    return _clip_text(value, limit)


def _item(
    kind: str,
    value: Any,
    source_ref: str,
    *,
    source_task: Optional[str] = None,
    source_run: Optional[str] = None,
    created_at: Optional[float] = None,
    evidence_refs: Iterable[str] = (),
    metadata: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    return ContextItem(
        kind=kind,
        value=_bound_value(_redact_value(value)),
        source_ref=source_ref,
        source_task=source_task,
        source_run=source_run,
        created_at=created_at,
        evidence_refs=tuple(str(ref) for ref in evidence_refs if ref),
        metadata=_bound_value(_redact_value(dict(metadata or {})), MAX_METADATA_CHARS),
    ).to_mapping()


def _as_list(value: Any) -> List[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _task_run(task: Mapping[str, Any]) -> Optional[str]:
    try:
        return str(run_id_for_task(dict(task)))
    except (TypeError, ValueError):
        return None


def _db_path(store: Any = None) -> Optional[Path]:
    value = getattr(store, "db_path", None)
    return Path(value) if value is not None else None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _canonical_evidence_ref(raw: Any) -> Optional[str]:
    if isinstance(raw, Mapping):
        for key in ("observation_id", "evidence_id", "event_id", "ref", "source_ref"):
            if raw.get(key):
                raw = raw[key]
                break
        else:
            return None
    text = str(raw or "").strip()
    if not text:
        return None
    if _valid_source_ref(text):
        return text
    if text.startswith("obs_"):
        return f"observation:{text}"
    if text.startswith("evt_"):
        return f"trajectory:{text}"
    return f"evidence:{text}"


def _relation_ids(metadata: Mapping[str, Any], key: str) -> List[str]:
    values = metadata.get(key)
    result: List[str] = []
    for value in _as_list(values):
        if isinstance(value, Mapping):
            value = value.get("finding_id") or value.get("id")
        if value:
            result.append(str(value))
    return result

