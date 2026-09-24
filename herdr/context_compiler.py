"""Deterministic, state-aware WorkingContext compiler for HAFlow.

WorkingContext is an immutable projection.  It reads existing HAFlow facts and
never writes back to Task, Workflow, Trajectory, Observation, Finding, Eval, or
CollaborationEvent stores.  The module keeps selection and diff logic
 deterministic and delegates persistence to :mod:`herdr.state_db`.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from . import state_db
from .collaboration import collab_scope_for_task
from .observation import _redact_value
from .trajectory import run_id_for_task
from .transitions import COMPLETED_TASK_STATUSES


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
DEPENDENCY_STATUSES = frozenset({
    "pending",
    "dispatched",
    "working",
    "blocked",
    "agent_done",
    "rework",
    "completed",
    "committed",
    "integrated",
    "failed",
    "superseded",
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
        metadata=dict(metadata or {}),
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


# ---------------------------------------------------------------------------
# Bounded, same-connection source snapshot


def _decode_workflow_row(row: Any) -> Dict[str, Any]:
    metadata = json.loads(row["metadata_json"] or "{}")
    config = json.loads(row["config_json"] or "{}")
    result = dict(metadata)
    result.update({
        "workflow_id": row["workflow_id"],
        "title": row["title"],
        "status": row["status"],
        "template_name": row["template_name"],
        "current_stage": row["current_stage"],
        "config": config,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    })
    return result


def _decode_event_row(row: Any) -> Dict[str, Any]:
    payload = json.loads(row["payload_json"] or "{}")
    return {
        "event_id": f"evt_{row['id']}",
        "run_id": row["run_id"],
        "workflow_id": row["workflow_id"],
        "task_id": row["task_id"],
        "node_id": row["node_id"],
        "event_type": row["event_type"],
        "sequence": row["sequence"],
        "timestamp": row["timestamp"],
        "payload": payload,
    }


def _decode_eval_row(row: Any) -> Dict[str, Any]:
    names = set(row.keys())
    def decode(name: str, default: Any) -> Any:
        if name not in names or row[name] is None:
            return default
        try:
            return json.loads(row[name])
        except (TypeError, ValueError):
            return default
    return {
        "eval_id": row["eval_id"],
        "run_id": row["run_id"],
        "revision": int(row["revision"] or 0),
        "task_id": row["task_id"] if "task_id" in names else None,
        "workflow_id": row["workflow_id"] if "workflow_id" in names else None,
        "verification_passed": row["verification_passed"] if "verification_passed" in names else None,
        "requirements_satisfied": row["requirements_satisfied"] if "requirements_satisfied" in names else None,
        "final_status": row["final_status"] if "final_status" in names else None,
        "evidence": decode("evidence_json", None),
        "warnings": decode("warnings_json", []),
        "created_at": row["created_at"],
    }


def _read_source_snapshot(
    *,
    workflow_id: str,
    task_id: str,
    store: Any = None,
    db_path: Optional[Path] = None,
    explicit_task: Optional[Mapping[str, Any]] = None,
    explicit_workflow: Optional[Mapping[str, Any]] = None,
    max_events: int = 300,
    max_findings: int = 500,
    max_observations: int = 300,
    max_collaborations: int = 50,
    max_evals: int = 100,
) -> Dict[str, Any]:
    conn = state_db.get_db_connection(db_path or _db_path(store))
    try:
        conn.execute("BEGIN;")
        task_row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (str(task_id),)).fetchone()
        task = state_db._decode_task_row(task_row) if task_row is not None else dict(explicit_task or {})
        if not task:
            raise ValueError(f"task not found: {task_id}")
        if str(task.get("workflow_id") or "") != str(workflow_id):
            raise ValueError("task does not belong to requested workflow")

        workflow_row = conn.execute(
            "SELECT * FROM workflows WHERE workflow_id = ?", (str(workflow_id),)
        ).fetchone()
        workflow = _decode_workflow_row(workflow_row) if workflow_row is not None else dict(explicit_workflow or {})
        workflow.setdefault("workflow_id", workflow_id)

        task_rows = conn.execute(
            "SELECT * FROM tasks WHERE workflow_id = ? ORDER BY created_at ASC, task_id ASC",
            (str(workflow_id),),
        ).fetchall()
        tasks = [state_db._decode_task_row(row) for row in task_rows]
        if not any(str(item.get("task_id")) == str(task_id) for item in tasks):
            tasks.append(dict(task))
        run_scope = collab_scope_for_task(task) or _task_run(task)
        if not run_scope:
            raise ValueError("task has no resolvable workflow execution scope")
        scoped_tasks = [
            item for item in tasks
            if (collab_scope_for_task(item) or _task_run(item)) == run_scope
        ]
        explicit_execution_scope = bool(task.get("workflow_run_id") or task.get("execution_id"))
        if explicit_execution_scope:
            allowed_runs = {_task_run(item) for item in scoped_tasks if _task_run(item)}
        else:
            # Legacy tasks may share a workflow_id while belonging to distinct
            # execution runs.  Without an explicit workflow execution id,
            # only the target run is authoritative until a collaboration event
            # proves a sibling handoff link.
            target_run = _task_run(task)
            allowed_runs = {target_run} if target_run else set()
        allowed_runs.discard(None)
        if not allowed_runs:
            raise ValueError("workflow execution scope has no run identity")
        task_by_id = {str(item.get("task_id")): item for item in scoped_tasks}

        placeholders = ",".join("?" for _ in allowed_runs)
        run_values = list(allowed_runs)
        event_rows = conn.execute(
            f"""SELECT * FROM events
                WHERE source = 'trajectory'
                  AND run_id IN ({placeholders})
                  AND event_type IN ({",".join("?" for _ in RELEVANT_EVENT_TYPES)})
                ORDER BY sequence DESC, id DESC LIMIT ?""",
            (*run_values, *sorted(RELEVANT_EVENT_TYPES), int(max_events)),
        ).fetchall()
        events = [_decode_event_row(row) for row in event_rows]

        finding_rows = conn.execute(
            f"""SELECT * FROM trajectory_findings
                WHERE run_id IN ({placeholders})
                ORDER BY created_at DESC, rowid DESC LIMIT ?""",
            (*run_values, int(max_findings)),
        ).fetchall()
        findings = [state_db._decode_finding_row(row) for row in finding_rows]

        observation_rows = conn.execute(
            f"""SELECT * FROM observations
                WHERE run_id IN ({placeholders})
                ORDER BY created_at DESC, observation_id DESC LIMIT ?""",
            (*run_values, int(max_observations)),
        ).fetchall()
        observations = []
        for row in observation_rows:
            decoded = state_db._decode_observation_row(row)
            observations.append({
                "observation_id": decoded.get("observation_id"),
                "run_id": decoded.get("run_id"),
                "task_id": decoded.get("task_id"),
                "workflow_id": decoded.get("workflow_id"),
                "source_type": decoded.get("source_type"),
                "source_ref": decoded.get("source_ref"),
                "sha256": decoded.get("sha256"),
                "excerpt": decoded.get("excerpt"),
                "created_at": decoded.get("created_at"),
            })

        task_ids = list(task_by_id)
        task_placeholders = ",".join("?" for _ in task_ids) or "NULL"
        collab_rows = conn.execute(
            f"""SELECT * FROM collaboration_events
                WHERE run_id = ? AND workflow_id = ?
                  AND (from_task_id IN ({task_placeholders}) OR to_task_id IN ({task_placeholders}))
                ORDER BY created_at DESC, event_id DESC LIMIT ?""",
            (run_scope, str(workflow_id), *task_ids, *task_ids, int(max_collaborations)),
        ).fetchall()
        collaborations = [state_db._decode_collaboration_row(row) for row in collab_rows]
        if not explicit_execution_scope:
            linked_task_ids = {
                str(event.get("to_task_id") or "") for event in collaborations
            } | {
                str(event.get("from_task_id") or "") for event in collaborations
            }
            for linked_id in linked_task_ids:
                linked_task = task_by_id.get(linked_id)
                linked_run = _task_run(linked_task) if linked_task else None
                if linked_run:
                    allowed_runs.add(linked_run)
            if len(allowed_runs) > 1:
                scoped_tasks = [
                    item for item in scoped_tasks if _task_run(item) in allowed_runs
                ]
                task_by_id = {str(item.get("task_id")): item for item in scoped_tasks}
                placeholders = ",".join("?" for _ in allowed_runs)
                run_values = list(allowed_runs)
                events = [
                    _decode_event_row(row) for row in conn.execute(
                        f"""SELECT * FROM events
                            WHERE source = 'trajectory'
                              AND run_id IN ({placeholders})
                              AND event_type IN ({",".join("?" for _ in RELEVANT_EVENT_TYPES)})
                            ORDER BY sequence DESC, id DESC LIMIT ?""",
                        (*run_values, *sorted(RELEVANT_EVENT_TYPES), int(max_events)),
                    ).fetchall()
                ]
                findings = [
                    state_db._decode_finding_row(row) for row in conn.execute(
                        f"""SELECT * FROM trajectory_findings
                            WHERE run_id IN ({placeholders})
                            ORDER BY created_at DESC, rowid DESC LIMIT ?""",
                        (*run_values, int(max_findings)),
                    ).fetchall()
                ]
                observations = []
                for row in conn.execute(
                    f"""SELECT * FROM observations
                        WHERE run_id IN ({placeholders})
                        ORDER BY created_at DESC, observation_id DESC LIMIT ?""",
                    (*run_values, int(max_observations)),
                ).fetchall():
                    decoded = state_db._decode_observation_row(row)
                    observations.append({
                        "observation_id": decoded.get("observation_id"),
                        "run_id": decoded.get("run_id"),
                        "task_id": decoded.get("task_id"),
                        "workflow_id": decoded.get("workflow_id"),
                        "source_type": decoded.get("source_type"),
                        "source_ref": decoded.get("source_ref"),
                        "sha256": decoded.get("sha256"),
                        "excerpt": decoded.get("excerpt"),
                        "created_at": decoded.get("created_at"),
                    })

        eval_rows = conn.execute(
            f"""SELECT * FROM eval_results
                WHERE run_id IN ({placeholders})
                ORDER BY run_id ASC, revision DESC, rowid DESC LIMIT ?""",
            (*run_values, int(max_evals)),
        ).fetchall()
        evals_by_run: Dict[str, Dict[str, Any]] = {}
        for row in eval_rows:
            decoded = _decode_eval_row(row)
            run_id = str(decoded.get("run_id") or "")
            if run_id and run_id not in evals_by_run:
                evals_by_run[run_id] = decoded
        evals = list(evals_by_run.values())
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()

    snapshot = {
        "task": task,
        "workflow": workflow,
        "tasks": scoped_tasks,
        "task_by_id": task_by_id,
        "allowed_runs": allowed_runs,
        "run_scope": run_scope,
        "events": events,
        "findings": findings,
        "observations": observations,
        "collaborations": collaborations,
        "evals": evals,
    }
    snapshot["source_version"] = _hash(_source_projection(snapshot))
    return snapshot


def _source_projection(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    """Keep the source-version hash bounded to fields that affect compilation."""
    return {
        "task": {
            key: snapshot["task"].get(key)
            for key in (
                "task_id", "workflow_id", "run_id", "workflow_run_id", "execution_id",
                "node", "stage", "agent", "agent_role", "status", "goal", "blocker",
                "acceptance_criteria", "stage_verdict", "stage_verdict_note", "updated_at",
            )
        },
        "workflow": {
            key: snapshot["workflow"].get(key)
            for key in ("workflow_id", "status", "current_stage", "updated_at", "config")
        },
        "tasks": [
            {key: item.get(key) for key in (
                "task_id", "run_id", "workflow_run_id", "execution_id", "node", "stage",
                "agent", "agent_role", "status", "goal", "blocker", "acceptance_criteria",
                "stage_verdict", "stage_verdict_note", "updated_at",
            )}
            for item in snapshot.get("tasks", [])
        ],
        "events": snapshot.get("events", []),
        "findings": snapshot.get("findings", []),
        "observations": snapshot.get("observations", []),
        "collaborations": snapshot.get("collaborations", []),
        "evals": snapshot.get("evals", []),
    }


# ---------------------------------------------------------------------------
# Provenance and validity


def _source_allowed(
    record: Mapping[str, Any],
    *,
    task_by_id: Mapping[str, Mapping[str, Any]],
    allowed_runs: Set[str],
    workflow_id: str,
    run_scope: str,
) -> bool:
    task_id = record.get("task_id")
    run_id = record.get("run_id")
    if task_id:
        task = task_by_id.get(str(task_id))
        if task is None:
            return False
        if str(task.get("workflow_id") or "") != str(workflow_id):
            return False
        if (collab_scope_for_task(task) or _task_run(task)) != run_scope:
            return False
        if run_id and _task_run(task) != str(run_id):
            return False
        return True
    if record.get("workflow_id") and str(record["workflow_id"]) != str(workflow_id):
        return False
    return bool(run_id and str(run_id) in allowed_runs)


def _task_value(task: Mapping[str, Any], *, include_goal: bool = True) -> Dict[str, Any]:
    value = {
        "task_id": task.get("task_id"),
        "node": task.get("node") or task.get("stage"),
        "status": task.get("status"),
    }
    if include_goal and task.get("goal"):
        value["goal"] = task.get("goal")
    if task.get("stage_verdict"):
        value["stage_verdict"] = task.get("stage_verdict")
    return value


def _workflow_node(workflow: Mapping[str, Any], node_id: Optional[str]) -> Dict[str, Any]:
    config = workflow.get("config") or {}
    nodes = config.get("nodes") if isinstance(config, dict) else None
    for node in nodes or []:
        if isinstance(node, Mapping) and str(node.get("id")) == str(node_id):
            return dict(node)
    return {}


def _dependency_ids(task: Mapping[str, Any], workflow: Mapping[str, Any]) -> List[str]:
    node = _workflow_node(workflow, task.get("node") or task.get("stage"))
    values = node.get("depends_on") or task.get("depends_on") or []
    return [str(value) for value in values if str(value).strip()]


def _scope_task_for_node(
    node_id: str,
    tasks: Sequence[Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    candidates = [
        task for task in tasks
        if str(task.get("node") or task.get("stage") or "") == str(node_id)
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: _safe_float(item.get("updated_at")))[-1]


def _requirements(task: Mapping[str, Any], node: Mapping[str, Any]) -> List[str]:
    values: List[str] = []
    for value in _as_list(task.get("acceptance_criteria")):
        values.append(str(value))
    for key in ("purpose", "rules"):
        for value in _as_list(node.get(key)):
            values.append(str(value))
    return list(dict.fromkeys(value for value in values if value.strip()))


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
        "developer": {"completed", "artifacts", "findings", "blockers", "open_questions", "verification", "handoffs"},
        "reviewer": {"artifacts", "findings", "evidence", "verification", "handoffs", "blockers", "open_questions"},
        "tester": {"artifacts", "evidence", "findings", "verification", "blockers", "handoffs"},
        "coordinator": {"completed", "decisions", "blockers", "open_questions", "handoffs", "findings", "verification"},
    }[role]
    role_score = (2.0 if kind in role_kinds else 0.0)
    if role == "reviewer" and kind in {"artifacts", "findings", "verification"}:
        role_score += 0.5
    if role == "tester" and kind in {"evidence", "verification"}:
        role_score += 0.5
    if role == "coordinator" and kind in {"blockers", "handoffs", "decisions"}:
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
        if kind == "findings" and severity != "critical" and status not in {"blocked", "failed"}:
            return False
        if kind in {"evidence", "artifacts"} and len(dependency_ids) > 2:
            return False
    if role == "tester":
        if kind == "findings" and not item.get("evidence_refs") and metadata.get("finding_type") not in {
            "verification_failure", "repeated_failure", "runtime_unavailable",
        }:
            return False
        if kind == "completed":
            return False
    if role == "reviewer":
        if kind == "findings" and source_task == target_task_id and metadata.get("node") == "implementation":
            return False
    if role == "developer":
        if kind == "decisions" and source_task != target_task_id:
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


# ---------------------------------------------------------------------------
# Candidate construction


def _event_payload(event: Mapping[str, Any]) -> Dict[str, Any]:
    payload = event.get("payload")
    return dict(payload) if isinstance(payload, Mapping) else {}


def _finding_candidates(
    findings: Sequence[Mapping[str, Any]],
    *,
    task_by_id: Mapping[str, Mapping[str, Any]],
    allowed_runs: Set[str],
    workflow_id: str,
    run_scope: str,
) -> List[Dict[str, Any]]:
    valid = [
        finding for finding in findings
        if _source_allowed(
            finding,
            task_by_id=task_by_id,
            allowed_runs=allowed_runs,
            workflow_id=workflow_id,
            run_scope=run_scope,
        )
    ]
    by_id = {str(item.get("finding_id")): item for item in valid if item.get("finding_id")}
    superseded: Set[str] = set()
    for finding in valid:
        metadata = finding.get("metadata") if isinstance(finding.get("metadata"), Mapping) else {}
        for target in _relation_ids(metadata, "supersedes"):
            target_finding = by_id.get(target)
            if target_finding is not None:
                superseded.add(target)
        for target in _relation_ids(metadata, "superseded_by"):
            if target in by_id:
                superseded.add(str(finding.get("finding_id")))
    result: List[Dict[str, Any]] = []
    for finding in valid:
        finding_id = str(finding.get("finding_id") or "")
        if not finding_id or finding_id in superseded:
            continue
        if str(finding.get("status") or "open") in {"superseded", "closed", "resolved"}:
            continue
        metadata = finding.get("metadata") if isinstance(finding.get("metadata"), Mapping) else {}
        evidence_refs = []
        for raw in _as_list(finding.get("evidence")):
            ref = _canonical_evidence_ref(raw)
            if ref and ref not in evidence_refs:
                evidence_refs.append(ref)
        value = {
            "summary": finding.get("summary") or finding.get("finding_type") or "",
            "finding_type": finding.get("finding_type"),
            "severity": finding.get("severity"),
            "status": finding.get("status") or "open",
            "recommended_action": finding.get("recommended_action"),
        }
        item_metadata = {
            "finding_key": finding.get("finding_key"),
            "node": finding.get("node"),
            "severity": finding.get("severity"),
            "status": finding.get("status") or "open",
            "finding_type": finding.get("finding_type"),
        }
        for key in ("supersedes", "superseded_by"):
            if metadata.get(key):
                item_metadata[key] = _relation_ids(metadata, key)
        result.append(_item(
            "finding",
            value,
            f"finding:{finding_id}",
            source_task=str(finding.get("task_id") or "") or None,
            source_run=str(finding.get("run_id") or "") or None,
            created_at=finding.get("created_at"),
            evidence_refs=evidence_refs,
            metadata=item_metadata,
        ))
    return result


def _observation_candidates(
    observations: Sequence[Mapping[str, Any]],
    *,
    task_by_id: Mapping[str, Mapping[str, Any]],
    allowed_runs: Set[str],
    workflow_id: str,
    run_scope: str,
) -> List[Dict[str, Any]]:
    result = []
    for observation in observations:
        if not _source_allowed(
            observation,
            task_by_id=task_by_id,
            allowed_runs=allowed_runs,
            workflow_id=workflow_id,
            run_scope=run_scope,
        ):
            continue
        observation_id = str(observation.get("observation_id") or "")
        if not observation_id:
            continue
        result.append(_item(
            "evidence",
            {
                "source_type": observation.get("source_type"),
                "source_ref": observation.get("source_ref"),
                "sha256": observation.get("sha256"),
                "excerpt": observation.get("excerpt"),
            },
            f"observation:{observation_id}",
            source_task=str(observation.get("task_id") or "") or None,
            source_run=str(observation.get("run_id") or "") or None,
            created_at=observation.get("created_at"),
            metadata={"observation_id": observation_id},
        ))
    return result


def _eval_candidates(
    evals: Sequence[Mapping[str, Any]],
    *,
    task_by_id: Mapping[str, Mapping[str, Any]],
    allowed_runs: Set[str],
    workflow_id: str,
    run_scope: str,
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for evaluation in evals:
        record = {
            "run_id": evaluation.get("run_id"),
            "task_id": evaluation.get("task_id"),
            "workflow_id": evaluation.get("workflow_id"),
        }
        if not _source_allowed(
            record,
            task_by_id=task_by_id,
            allowed_runs=allowed_runs,
            workflow_id=workflow_id,
            run_scope=run_scope,
        ):
            continue
        passed = evaluation.get("verification_passed")
        requirements = evaluation.get("requirements_satisfied")
        value = {
            "verification_passed": bool(passed) if passed is not None else None,
            "requirements_satisfied": bool(requirements) if requirements is not None else None,
            "final_status": evaluation.get("final_status"),
        }
        if value["verification_passed"] is None and value["requirements_satisfied"] is None:
            continue
        evidence_refs: List[str] = []
        raw_evidence = evaluation.get("evidence")
        for raw in _as_list(raw_evidence):
            if isinstance(raw, Mapping):
                raw_values = [raw.get(key) for key in ("observation_id", "evidence_id", "event_id", "ref")]
            else:
                raw_values = [raw]
            for raw_value in raw_values:
                ref = _canonical_evidence_ref(raw_value)
                if ref and ref not in evidence_refs:
                    evidence_refs.append(ref)
        result.append(_item(
            "verification",
            value,
            f"eval:{evaluation.get('eval_id')}",
            source_task=str(evaluation.get("task_id") or "") or None,
            source_run=str(evaluation.get("run_id") or "") or None,
            created_at=evaluation.get("created_at"),
            evidence_refs=evidence_refs,
            metadata={"source_kind": "eval", "revision": evaluation.get("revision")},
        ))
    return result


def _event_candidates(
    events: Sequence[Mapping[str, Any]],
    *,
    task_by_id: Mapping[str, Mapping[str, Any]],
    allowed_runs: Set[str],
    workflow_id: str,
    run_scope: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return artifacts, completed, verification, decisions, and blockers."""
    artifacts: List[Dict[str, Any]] = []
    completed: List[Dict[str, Any]] = []
    verification: List[Dict[str, Any]] = []
    decisions: List[Dict[str, Any]] = []
    blockers: List[Dict[str, Any]] = []
    for event in events:
        if not _source_allowed(
            event,
            task_by_id=task_by_id,
            allowed_runs=allowed_runs,
            workflow_id=workflow_id,
            run_scope=run_scope,
        ):
            continue
        event_type = str(event.get("event_type") or "")
        payload = _event_payload(event)
        event_ref = f"trajectory:{event.get('event_id')}"
        source_task = str(event.get("task_id") or "") or None
        common = {
            "source_task": source_task,
            "source_run": str(event.get("run_id") or "") or None,
            "created_at": event.get("timestamp"),
        }
        if event_type == "artifact_created":
            artifact = payload.get("artifact") if isinstance(payload.get("artifact"), Mapping) else {}
            ref = artifact.get("ref") or artifact.get("path")
            if ref:
                artifacts.append(_item(
                    "artifact",
                    {"ref": ref, "kind": artifact.get("kind")},
                    event_ref,
                    metadata={"event_type": event_type, "node": event.get("node_id")},
                    **common,
                ))
        elif event_type in TERMINAL_COMPLETION_EVENTS:
            completed.append(_item(
                "completed",
                {"event_type": event_type, "node": event.get("node_id")},
                event_ref,
                metadata={"event_type": event_type, "node": event.get("node_id")},
                **common,
            ))
        elif event_type in VERIFICATION_EVENTS:
            raw_verification = payload.get("verification")
            verification = dict(raw_verification) if isinstance(raw_verification, Mapping) else payload
            if not verification:
                continue
            value = {key: verification.get(key) for key in (
                "passed", "passed_tests", "total_tests", "failing_count", "lint_errors",
                "type_errors", "evidence_id", "observation_id", "status",
            ) if key in verification}
            if "passed" in value and not isinstance(value["passed"], bool):
                value.pop("passed", None)
            evidence_refs = []
            for key in ("observation_id", "evidence_id"):
                ref = _canonical_evidence_ref(verification.get(key))
                if ref and ref not in evidence_refs:
                    evidence_refs.append(ref)
            verification.append(_item(
                "verification",
                value,
                event_ref,
                evidence_refs=evidence_refs,
                metadata={"event_type": event_type, "node": event.get("node_id")},
                **common,
            ))
        elif event_type in {"decision", "decision_completed", "review_requested", "verification_requested"}:
            value = {
                key: payload.get(key)
                for key in ("decision", "verdict", "question", "status", "reason")
                if payload.get(key) is not None
            }
            if value:
                decisions.append(_item(
                    "decisions",
                    value,
                    event_ref,
                    metadata={"event_type": event_type, "node": event.get("node_id")},
                    **common,
                ))
        elif event_type in {"blocker", "task_failed", "agent_failed", "run_failed"}:
            reason = payload.get("reason") or payload.get("blocker") or event_type
            blockers.append(_item(
                "blocker",
                {"reason": reason, "event_type": event_type},
                event_ref,
                metadata={"event_type": event_type, "status": "blocked", "node": event.get("node_id")},
                **common,
            ))
    return artifacts, completed, verification, decisions, blockers


def _task_candidates(
    tasks: Sequence[Mapping[str, Any]],
    *,
    target_task: Mapping[str, Any],
    task_by_id: Mapping[str, Mapping[str, Any]],
    dependency_ids: Sequence[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    completed: List[Dict[str, Any]] = []
    blockers: List[Dict[str, Any]] = []
    decisions: List[Dict[str, Any]] = []
    questions: List[Dict[str, Any]] = []
    dependency_items: List[Dict[str, Any]] = []
    for task in tasks:
        task_id = str(task.get("task_id") or "")
        task_ref = f"task:{task_id}"
        status = str(task.get("status") or "")
        if status in COMPLETED_TASK_STATUSES:
            completed.append(_item(
                "completed",
                _task_value(task, include_goal=False),
                task_ref,
                source_task=task_id,
                source_run=_task_run(task),
                created_at=task.get("updated_at"),
                metadata={"node": task.get("node") or task.get("stage"), "status": status},
            ))
        if status == "blocked" or task.get("blocker") or task.get("blocked_reason"):
            reason = task.get("blocker") or task.get("blocked_reason") or status
            blockers.append(_item(
                "blocker",
                str(reason),
                task_ref,
                source_task=task_id,
                source_run=_task_run(task),
                created_at=task.get("updated_at"),
                metadata={"node": task.get("node") or task.get("stage"), "status": status},
            ))
        if task.get("stage_verdict") or task.get("decision"):
            value = {
                "verdict": task.get("stage_verdict") or task.get("decision"),
                "note": task.get("stage_verdict_note"),
            }
            decisions.append(_item(
                "decisions",
                value,
                task_ref,
                source_task=task_id,
                source_run=_task_run(task),
                created_at=task.get("updated_at"),
                metadata={"node": task.get("node") or task.get("stage"), "status": status},
            ))
        for key in ("open_questions", "question", "decision_question", "acceptance_gap"):
            for value in _as_list(task.get(key)):
                questions.append(_item(
                    "open_question",
                    str(value),
                    task_ref,
                    source_task=task_id,
                    source_run=_task_run(task),
                    created_at=task.get("updated_at"),
                    metadata={"field": key, "node": task.get("node") or task.get("stage")},
                ))
        node = str(task.get("node") or task.get("stage") or "")
        if node in set(str(value) for value in dependency_ids):
            dependency_items.append(_item(
                "dependency",
                {"node": node, "status": status, "task_id": task_id},
                task_ref,
                source_task=task_id,
                source_run=_task_run(task),
                created_at=task.get("updated_at"),
                metadata={"node": node, "status": status, "dependency_relevant": True},
            ))
    return completed, blockers, decisions, questions, dependency_items


def _handoff_candidates(
    events: Sequence[Mapping[str, Any]],
    *,
    target_task_id: str,
    task_by_id: Mapping[str, Mapping[str, Any]],
    workflow_id: str,
    run_scope: str,
) -> List[Dict[str, Any]]:
    candidates = []
    for event in events:
        if not event.get("event_id"):
            continue
        if str(event.get("workflow_id") or "") != str(workflow_id):
            continue
        if str(event.get("run_id") or "") != str(run_scope):
            continue
        from_id = str(event.get("from_task_id") or "")
        to_id = str(event.get("to_task_id") or "")
        if from_id not in task_by_id or to_id not in task_by_id:
            continue
        value = {
            "type": event.get("type"),
            "status": event.get("status"),
            "summary": event.get("summary"),
            "from_task_id": from_id,
            "to_task_id": to_id,
            "artifact_refs": list(event.get("artifact_refs") or []),
            "evidence_refs": list(event.get("evidence_refs") or []),
        }
        candidates.append(_item(
            "handoff",
            value,
            f"collaboration:{event.get('event_id')}",
            source_task=to_id,
            source_run=run_scope,
            created_at=event.get("created_at"),
            metadata={
                "type": event.get("type"),
                "status": event.get("status"),
                "incoming": to_id == target_task_id,
                "failed": event.get("status") == "failed",
            },
        ))
    candidates.sort(key=lambda item: (not item.get("metadata", {}).get("incoming"), -_safe_float(item.get("created_at")), item.get("source_ref", "")))
    return candidates


def _next_action(role: str, task: Mapping[str, Any], blockers: Sequence[Mapping[str, Any]]) -> str:
    status = str(task.get("status") or "")
    if blockers or status in {"blocked", "rework", "failed"}:
        return "Resolve current blockers and re-run the required verification."
    if role == "coordinator":
        return "Resolve dependency state and dispatch the next ready node."
    if role == "reviewer":
        return "Review the selected artifacts against the implementation evidence."
    if role == "tester":
        return "Run or inspect the required verification targets and record evidence."
    return "Implement the next unresolved requirement and verify the result."


# ---------------------------------------------------------------------------
# Public compiler and storage facade


def _config(value: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    max_chars = int((value or {}).get("max_chars", DEFAULT_MAX_CHARS))
    max_items = int((value or {}).get("max_items", DEFAULT_MAX_ITEMS))
    if max_chars < 500:
        raise ValueError("max_chars must be at least 500")
    if max_items < 1:
        raise ValueError("max_items must be positive")
    caps = dict(DEFAULT_MAX_ITEMS_PER_KIND)
    supplied = (value or {}).get("max_items_per_kind") or {}
    for key, cap in supplied.items():
        if key in caps:
            cap = int(cap)
            if cap < 0:
                raise ValueError(f"max_items_per_kind[{key}] must be non-negative")
            caps[key] = cap
    return {"max_chars": max_chars, "max_items": max_items, "max_items_per_kind": caps}


def _selected_refs(context: WorkingContext) -> List[str]:
    refs = [context.goal_source_ref, context.next_action_source_ref]
    refs.extend(context.current_state_refs.values())
    for field_name in (
        "completed", "artifacts", "evidence", "findings", "decisions", "blockers",
        "open_questions", "verification", "handoffs",
    ):
        for item in getattr(context, field_name):
            ref = item.get("source_ref")
            if ref:
                refs.append(str(ref))
                refs.extend(str(value) for value in item.get("evidence_refs") or [])
    return list(dict.fromkeys(ref for ref in refs if ref))


def _fingerprint_payload(context: WorkingContext, config: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "run_scope": context.run_scope,
        "run_id": context.run_id,
        "workflow_id": context.workflow_id,
        "task_id": context.task_id,
        "node_id": context.node_id,
        "agent_role": context.agent_role,
        "goal": context.goal,
        "current_state": context.current_state,
        "current_state_refs": context.current_state_refs,
        "completed": context.completed,
        "artifacts": context.artifacts,
        "evidence": context.evidence,
        "findings": context.findings,
        "decisions": context.decisions,
        "blockers": context.blockers,
        "open_questions": context.open_questions,
        "verification": context.verification,
        "handoffs": context.handoffs,
        "next_action": context.next_action,
        "source_refs": context.source_refs,
        "source_version": context.source_version,
        "config": dict(config),
    }


def _fit_budget(context: WorkingContext, config: Mapping[str, Any]) -> WorkingContext:
    max_chars = int(config["max_chars"])
    max_items = int(config["max_items"])
    caps = config["max_items_per_kind"]
    # Per-kind caps first, then global priority trimming.
    values = {}
    for field_name in (
        "completed", "artifacts", "evidence", "findings", "decisions", "blockers",
        "open_questions", "verification", "handoffs",
    ):
        values[field_name] = list(getattr(context, field_name))[: int(caps.get(field_name, 0))]
    context = WorkingContext(**{**context.to_mapping(), **values})

    priority = [
        "completed", "handoffs", "artifacts", "findings", "evidence", "decisions",
        "verification", "open_questions", "blockers",
    ]
    while sum(len(getattr(context, field)) for field in priority) > max_items:
        for field_name in reversed(priority):
            if getattr(context, field_name):
                values[field_name] = list(getattr(context, field_name))[:-1]
                context = WorkingContext(**{**context.to_mapping(), **values})
                break
        else:
            break

    def size(candidate: WorkingContext) -> int:
        return len(json.dumps(candidate.to_mapping(), ensure_ascii=False, separators=(",", ":")))

    # Reduce bulky values and low-priority history while retaining identity/state.
    for field_name in reversed(priority):
        if size(context) <= max_chars:
            break
        while getattr(context, field_name) and size(context) > max_chars:
            values[field_name] = list(getattr(context, field_name))[:-1]
            context = WorkingContext(**{**context.to_mapping(), **values})
    if size(context) > max_chars:
        context = WorkingContext(**{
            **context.to_mapping(),
            "goal": _clip_text(context.goal, 512),
            "current_state": _bound_value(context.current_state, 256),
            "next_action": _clip_text(context.next_action, 256),
        })
    if size(context) > max_chars:
        context = WorkingContext(**{
            **context.to_mapping(),
            "goal": "",
            "next_action": _clip_text(context.next_action, 128),
            "completed": [],
            "artifacts": [],
            "evidence": [],
            "findings": [],
            "decisions": [],
            "open_questions": [],
            "verification": [],
            "handoffs": [],
            "blockers": list(context.blockers[:1]),
        })
    return context


def compile_working_context(
    *,
    workflow_id: str,
    task_id: str,
    agent_role: str,
    store: Any = None,
    task: Optional[Mapping[str, Any]] = None,
    workflow: Optional[Mapping[str, Any]] = None,
    db_path: Optional[Path] = None,
    boundary: str = "execution",
    config: Optional[Mapping[str, Any]] = None,
    now: Optional[float] = None,
    _retry: int = 0,
) -> WorkingContext:
    """Compile one deterministic, role-aware execution-boundary snapshot."""
    started = time.perf_counter()
    role = normalize_agent_role(agent_role)
    cfg = _config(config)
    snapshot = _read_source_snapshot(
        workflow_id=str(workflow_id),
        task_id=str(task_id),
        store=store,
        db_path=db_path,
        explicit_task=task,
        explicit_workflow=workflow,
    )
    target = snapshot["task"]
    current_task_id = str(target.get("task_id") or task_id)
    node_id = str(target.get("node") or target.get("stage") or "") or None
    current_state: Dict[str, Any] = {
        "task_id": current_task_id,
        "task_status": target.get("status"),
        "current_node": node_id,
        "workflow_status": snapshot["workflow"].get("status"),
        "workflow_current_stage": snapshot["workflow"].get("current_stage"),
    }
    runtime = target.get("runtime") if isinstance(target.get("runtime"), Mapping) else {}
    if runtime.get("status"):
        current_state["runtime_status"] = runtime.get("status")
    current_state = {key: value for key, value in current_state.items() if value is not None}
    current_state_refs = {
        "task_id": f"task:{current_task_id}",
        "task_status": f"task:{current_task_id}",
        "current_node": f"task:{current_task_id}",
    }
    if snapshot["workflow"].get("status") is not None:
        current_state_refs["workflow_status"] = f"workflow:{workflow_id}"
    if snapshot["workflow"].get("current_stage") is not None:
        current_state_refs["workflow_current_stage"] = f"workflow:{workflow_id}"
    if runtime.get("status"):
        current_state_refs["runtime_status"] = f"task:{current_task_id}"

    node = _workflow_node(snapshot["workflow"], node_id)
    dependency_ids = _dependency_ids(target, snapshot["workflow"])
    dependency_state: Dict[str, str] = {}
    for dependency_id in dependency_ids:
        dependency_task = _scope_task_for_node(dependency_id, snapshot["tasks"])
        dependency_state[dependency_id] = str(dependency_task.get("status")) if dependency_task else "unknown"
    current_state["dependency_state"] = dependency_state
    current_state_refs["dependency_state"] = f"workflow:{workflow_id}"
    requirements = _requirements(target, node)
    if role == "developer":
        current_state["requirements"] = requirements
        current_state_refs["requirements"] = f"task:{current_task_id}"
    elif role == "reviewer":
        current_state["review_scope"] = {
            "node": node_id,
            "dependency_nodes": dependency_ids,
            "rules": list(node.get("rules") or []),
        }
        current_state_refs["review_scope"] = f"workflow:{workflow_id}"
    elif role == "tester":
        current_state["acceptance_criteria"] = requirements
        current_state["verification_targets"] = list(node.get("rules") or [])
        current_state_refs["acceptance_criteria"] = f"task:{current_task_id}"
        current_state_refs["verification_targets"] = f"workflow:{workflow_id}"
    else:
        current_state["dependency_state"] = dependency_state
        current_state["workflow_stage"] = snapshot["workflow"].get("current_stage")
        current_state_refs["workflow_stage"] = f"workflow:{workflow_id}"

    completed_tasks, task_blockers, task_decisions, questions, dependency_items = _task_candidates(
        snapshot["tasks"],
        target_task=target,
        task_by_id=snapshot["task_by_id"],
        dependency_ids=dependency_ids,
    )
    event_artifacts, event_completed, event_verification, event_decisions, event_blockers = _event_candidates(
        snapshot["events"],
        task_by_id=snapshot["task_by_id"],
        allowed_runs=snapshot["allowed_runs"],
        workflow_id=str(workflow_id),
        run_scope=snapshot["run_scope"],
    )
    eval_verification = _eval_candidates(
        snapshot["evals"],
        task_by_id=snapshot["task_by_id"],
        allowed_runs=snapshot["allowed_runs"],
        workflow_id=str(workflow_id),
        run_scope=snapshot["run_scope"],
    )
    findings = _finding_candidates(
        snapshot["findings"],
        task_by_id=snapshot["task_by_id"],
        allowed_runs=snapshot["allowed_runs"],
        workflow_id=str(workflow_id),
        run_scope=snapshot["run_scope"],
    )
    evidence = _observation_candidates(
        snapshot["observations"],
        task_by_id=snapshot["task_by_id"],
        allowed_runs=snapshot["allowed_runs"],
        workflow_id=str(workflow_id),
        run_scope=snapshot["run_scope"],
    )
    handoffs = _handoff_candidates(
        snapshot["collaborations"],
        target_task_id=current_task_id,
        task_by_id=snapshot["task_by_id"],
        workflow_id=str(workflow_id),
        run_scope=snapshot["run_scope"],
    )
    blockers = [*task_blockers, *event_blockers]
    # Explicit current task blocker is always retained, including when it is
    # not represented by a status transition event.
    if target.get("blocker") and not any(
        item.get("source_ref") == f"task:{current_task_id}" for item in blockers
    ):
        blockers.append(_item(
            "blocker",
            {"reason": target.get("blocker"), "task_id": current_task_id},
            f"task:{current_task_id}",
            source_task=current_task_id,
            source_run=_task_run(target),
            created_at=target.get("updated_at"),
            metadata={"status": str(target.get("status") or ""), "node": node_id},
        ))
    for finding in findings:
        metadata = finding.get("metadata") or {}
        if metadata.get("severity") == "critical" or metadata.get("finding_type") in {"verification_failure", "repeated_failure"}:
            blockers.append(_item(
                "blocker",
                {"reason": finding.get("value"), "finding_id": finding.get("source_ref")},
                str(finding.get("source_ref")),
                source_task=finding.get("source_task"),
                source_run=finding.get("source_run"),
                created_at=finding.get("created_at"),
                metadata={"status": "blocked", "derived_from": "finding"},
            ))
    for handoff in handoffs:
        if (handoff.get("metadata") or {}).get("failed"):
            blockers.append(_item(
                "blocker",
                {"reason": handoff.get("value"), "handoff_id": handoff.get("source_ref")},
                str(handoff.get("source_ref")),
                source_task=handoff.get("source_task"),
                source_run=handoff.get("source_run"),
                created_at=handoff.get("created_at"),
                metadata={"status": "failed", "derived_from": "handoff"},
            ))

    decisions = [*task_decisions, *event_decisions]
    completed = [*completed_tasks, *event_completed]
    all_verification = [*event_verification, *eval_verification]
    all_candidates = [
        *completed, *event_artifacts, *evidence, *findings, *decisions,
        *blockers, *questions, *all_verification, *handoffs, *dependency_items,
    ]
    raw_candidate_items = len(all_candidates)
    now_value = float(now if now is not None else time.time())
    selected: Dict[str, List[Dict[str, Any]]] = {}
    for field_name, kind in (
        ("completed", "completed"),
        ("artifacts", "artifact"),
        ("evidence", "evidence"),
        ("findings", "finding"),
        ("decisions", "decisions"),
        ("blockers", "blocker"),
        ("open_questions", "open_question"),
        ("verification", "verification"),
        ("handoffs", "handoff"),
    ):
        source_items = {
            "completed": completed,
            "artifacts": event_artifacts,
            "evidence": evidence,
            "findings": findings,
            "decisions": decisions,
            "blockers": blockers,
            "open_questions": questions,
            "verification": all_verification,
            "handoffs": handoffs,
        }[field_name]
        selected[field_name] = _select_items(
            source_items,
            kind=kind,
            role=role,
            current_state=current_state,
            target_task_id=current_task_id,
            dependency_ids=dependency_ids,
            limit=int(cfg["max_items_per_kind"].get(field_name, 0)),
            now=now_value,
        )

    # Verification is latest-per-task/source, not a historical pass/fail dump.
    latest_verification: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for item in all_verification:
        key = (str(item.get("source_run") or ""), str(item.get("source_task") or ""))
        old = latest_verification.get(key)
        if old is None or _safe_float(item.get("created_at")) >= _safe_float(old.get("created_at")):
            latest_verification[key] = item
    selected["verification"] = sorted(
        latest_verification.values(),
        key=lambda item: str(item.get("source_ref") or ""),
    )[: int(cfg["max_items_per_kind"]["verification"])]

    # Explicitly keep the latest incoming handoff first, then bounded history.
    selected["handoffs"] = sorted(
        selected["handoffs"],
        key=lambda item: (
            not bool((item.get("metadata") or {}).get("incoming")),
            -_safe_float(item.get("created_at")),
            str(item.get("source_ref") or ""),
        ),
    )[: int(cfg["max_items_per_kind"]["handoffs"])]

    next_action = _next_action(role, target, selected["blockers"])
    goal = str(target.get("goal") or snapshot["workflow"].get("title") or node.get("purpose") or "")
    context = WorkingContext(
        context_id=f"wc_{uuid.uuid4().hex}",
        run_scope=str(snapshot["run_scope"]),
        run_id=_task_run(target),
        workflow_id=str(workflow_id),
        task_id=current_task_id,
        node_id=node_id,
        agent_role=role,
        goal=_clip_text(goal),
        goal_source_ref=f"task:{current_task_id}" if target.get("goal") else f"workflow:{workflow_id}",
        current_state=_bound_value(current_state, 512),
        current_state_refs=current_state_refs,
        next_action=_clip_text(next_action, 512),
        next_action_source_ref=f"policy:context_compiler_v1:{role}",
        source_refs=[],
        source_version=str(snapshot["source_version"]),
        compiled_at=now_value,
        **selected,
    )
    context = WorkingContext(**{
        **context.to_mapping(),
        "source_refs": _selected_refs(context),
    })
    fingerprint = _hash(_fingerprint_payload(context, cfg))
    context = WorkingContext(**{**context.to_mapping(), "context_fingerprint": fingerprint})
    context = _fit_budget(context, cfg)
    fingerprint = _hash(_fingerprint_payload(context, cfg))
    context = WorkingContext(**{
        **context.to_mapping(),
        "context_fingerprint": fingerprint,
        "source_refs": _selected_refs(context),
    })
    metrics = {
        "raw_candidate_items": raw_candidate_items,
        "selected_items": sum(len(value) for value in selected.values()),
        "context_chars": 0,
        "compile_latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "context_reuse": False,
        "context_changed": True,
        "boundary": str(boundary),
    }
    context = WorkingContext(**{**context.to_mapping(), "metrics": metrics})
    metrics["context_chars"] = len(json.dumps(context.to_mapping(), ensure_ascii=False, separators=(",", ":")))
    context = WorkingContext(**{**context.to_mapping(), "metrics": metrics})
    if _retry < 1:
        fresh_snapshot = _read_source_snapshot(
            workflow_id=str(workflow_id),
            task_id=str(task_id),
            store=store,
            db_path=db_path,
            explicit_task=task,
            explicit_workflow=workflow,
        )
        if fresh_snapshot.get("source_version") != snapshot.get("source_version"):
            return compile_working_context(
                workflow_id=workflow_id,
                task_id=task_id,
                agent_role=role,
                store=store,
                task=task,
                workflow=workflow,
                db_path=db_path,
                boundary=boundary,
                config=config,
                now=now,
                _retry=_retry + 1,
            )
    stored = state_db.save_working_context(
        context.to_mapping(), db_path=db_path or _db_path(store),
    )
    reused = str(stored.get("context_id")) != context.context_id
    metrics["context_reuse"] = reused
    metrics["context_changed"] = not reused
    result_mapping = dict(stored)
    result_mapping["metrics"] = metrics
    result_mapping["context_fingerprint"] = stored.get("context_fingerprint", context.context_fingerprint)
    return WorkingContext.from_mapping(result_mapping)


def get_working_context(
    context_id: str,
    *,
    store: Any = None,
    db_path: Optional[Path] = None,
) -> Optional[WorkingContext]:
    row = state_db.get_working_context(
        context_id,
        db_path=Path(db_path) if db_path is not None else _db_path(store),
    )
    return WorkingContext.from_mapping(row) if row is not None else None


def get_latest_working_context(
    task_id: str,
    *,
    agent_role: Optional[str] = None,
    store: Any = None,
    db_path: Optional[Path] = None,
) -> Optional[WorkingContext]:
    row = state_db.get_latest_working_context(
        task_id,
        agent_role=normalize_agent_role(agent_role) if agent_role else None,
        db_path=Path(db_path) if db_path is not None else _db_path(store),
    )
    return WorkingContext.from_mapping(row) if row is not None else None


def list_working_contexts(
    task_id: str,
    *,
    agent_role: Optional[str] = None,
    store: Any = None,
    db_path: Optional[Path] = None,
) -> List[WorkingContext]:
    rows = state_db.list_working_contexts(
        task_id,
        agent_role=normalize_agent_role(agent_role) if agent_role else None,
        db_path=Path(db_path) if db_path is not None else _db_path(store),
    )
    return [WorkingContext.from_mapping(row) for row in rows]


# ---------------------------------------------------------------------------
# Diff


def _as_context_mapping(value: Any) -> Dict[str, Any]:
    if isinstance(value, WorkingContext):
        return value.to_mapping()
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("working context must be WorkingContext or mapping")


def _item_maps(context: Mapping[str, Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    result: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for field_name in (
        "completed", "artifacts", "evidence", "findings", "decisions", "blockers",
        "open_questions", "verification", "handoffs",
    ):
        for raw in context.get(field_name) or []:
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            kind = str(item.get("kind") or field_name)
            ref = str(item.get("source_ref") or "")
            if ref:
                result[(kind, ref)] = item
    return result


def _relation_refs(item: Mapping[str, Any], key: str) -> Set[str]:
    values = item.get("metadata", {}).get(key) if isinstance(item.get("metadata"), Mapping) else None
    refs: Set[str] = set()
    for value in _as_list(values):
        text = str(value)
        refs.add(text if _valid_source_ref(text) else f"finding:{text}")
    return refs


def diff_working_context(
    old: WorkingContext | Mapping[str, Any],
    new: WorkingContext | Mapping[str, Any],
) -> Dict[str, List[Dict[str, Any]]]:
    """Return deterministic added/removed/superseded/changed item diff."""
    old_map = _as_context_mapping(old)
    new_map = _as_context_mapping(new)
    old_items = _item_maps(old_map)
    new_items = _item_maps(new_map)
    added = [item for key, item in new_items.items() if key not in old_items]
    removed = [item for key, item in old_items.items() if key not in new_items]
    changed: List[Dict[str, Any]] = []
    superseded: List[Dict[str, Any]] = []
    superseded_old_keys: Set[Tuple[str, str]] = set()
    for key, new_item in new_items.items():
        old_item = old_items.get(key)
        if old_item is not None and _canonical_json(old_item.get("value")) != _canonical_json(new_item.get("value")):
            changed.append({
                "kind": key[0],
                "source_ref": key[1],
                "old": old_item,
                "new": new_item,
            })
        if key[0] == "finding":
            old_candidates = _relation_refs(new_item, "supersedes")
            for old_ref in old_candidates:
                old_key = ("finding", old_ref)
                old_candidate = old_items.get(old_key)
                if old_candidate is not None:
                    superseded.append({
                        "kind": "finding",
                        "source_ref": old_ref,
                        "old": old_candidate,
                        "new": new_item,
                    })
                    superseded_old_keys.add(old_key)
            for old_ref in _relation_refs(new_item, "superseded_by"):
                old_key = ("finding", old_ref)
                if old_key in old_items:
                    superseded.append({
                        "kind": "finding",
                        "source_ref": old_ref,
                        "old": old_items[old_key],
                        "new": new_item,
                    })
                    superseded_old_keys.add(old_key)
    removed = [item for item in removed if (str(item.get("kind")), str(item.get("source_ref"))) not in superseded_old_keys]
    return {
        "added": sorted(added, key=lambda item: (item.get("kind", ""), item.get("source_ref", ""))),
        "removed": sorted(removed, key=lambda item: (item.get("kind", ""), item.get("source_ref", ""))),
        "superseded": sorted(superseded, key=lambda item: (item.get("kind", ""), item.get("source_ref", ""))),
        "changed": sorted(changed, key=lambda item: (item.get("kind", ""), item.get("source_ref", ""))),
    }


__all__ = [
    "ContextItem",
    "WorkingContext",
    "compile_working_context",
    "context_relevance",
    "diff_working_context",
    "get_latest_working_context",
    "get_working_context",
    "infer_agent_role",
    "list_working_contexts",
    "normalize_agent_role",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_ITEMS",
    "DEFAULT_MAX_ITEMS_PER_KIND",
]
