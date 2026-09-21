"""Semantic Context Compact V1: bounded, reference-verified working memory.

Trajectory is fact history, Observation is evidence, and Finding is analysis.
This module creates a small append-only ContextPack snapshot from those layers.
The optional reducer may select semantic text, but program code owns facts and
removes every reference that cannot be resolved in the source stores.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from . import state_db
from .trajectory import TrajectoryLedger


LOGGER = logging.getLogger(__name__)
DEFAULT_CONFIG = {
    "enabled": True,
    "max_input_chars": 12000,
    "max_recent_events": 100,
    "max_findings": 10,
    "max_observations": 20,
    "max_artifacts": 20,
}
_COMPACT_LOCK = threading.RLock()


@dataclass(frozen=True)
class ContextPack:
    context_id: str
    run_id: str
    task_id: Optional[str]
    workflow_id: Optional[str]
    goal: Optional[str]
    current_state: Dict[str, Any] = field(default_factory=dict)
    completed: List[Dict[str, Any]] = field(default_factory=list)
    verified_facts: List[Dict[str, Any]] = field(default_factory=list)
    important_findings: List[Dict[str, Any]] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    artifact_refs: List[Dict[str, Any]] = field(default_factory=list)
    open_issues: List[Dict[str, Any]] = field(default_factory=list)
    next_focus: List[Dict[str, Any]] = field(default_factory=list)
    source_event_sequence: int = 0
    created_at: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_mapping(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, raw: Dict[str, Any]) -> "ContextPack":
        if not isinstance(raw, dict):
            raise TypeError("context pack must be a dict")
        return cls(
            context_id=str(raw["context_id"]),
            run_id=str(raw["run_id"]),
            task_id=raw.get("task_id"),
            workflow_id=raw.get("workflow_id"),
            goal=raw.get("goal"),
            current_state=dict(raw.get("current_state") or {}),
            completed=list(raw.get("completed") or []),
            verified_facts=list(raw.get("verified_facts") or []),
            important_findings=list(raw.get("important_findings") or []),
            evidence_refs=list(raw.get("evidence_refs") or []),
            artifact_refs=list(raw.get("artifact_refs") or []),
            open_issues=list(raw.get("open_issues") or []),
            next_focus=list(raw.get("next_focus") or []),
            source_event_sequence=int(raw.get("source_event_sequence") or 0),
            created_at=float(raw.get("created_at") or 0.0),
            metadata=dict(raw.get("metadata") or {}),
        )


def _db_path(store: Any = None) -> Optional[Path]:
    value = getattr(store, "db_path", None)
    return Path(value) if value is not None else None


def _config(config: Optional[Dict[str, Any]]) -> Dict[str, int]:
    merged = dict(DEFAULT_CONFIG)
    merged.update(config or {})
    return {
        key: int(value) if key != "enabled" else bool(value)
        for key, value in merged.items()
    }


def _event_id(event: Dict[str, Any]) -> str:
    return str(event.get("event_id") or f"evt_{event.get('id')}")


def _findings(run_id: str, db_path: Optional[Path], limit: int) -> List[Dict[str, Any]]:
    rows = state_db.list_trajectory_findings(run_id=run_id, limit=None, db_path=db_path)
    severity_rank = {"critical": 3, "warning": 2, "info": 1}
    rows.sort(key=lambda row: (severity_rank.get(row.get("severity"), 0), row.get("created_at") or 0), reverse=True)
    return rows[:limit]


def _observation_metadata(run_id: str, db_path: Optional[Path], limit: int) -> List[Dict[str, Any]]:
    rows = state_db.list_observations(run_id=run_id, db_path=db_path)
    return [
        {
            "observation_id": row["observation_id"],
            "source_type": row["source_type"],
            "source_ref": row["source_ref"],
            "sha256": row["sha256"],
            "excerpt": row.get("excerpt"),
        }
        for row in rows[-limit:]
    ]


def _artifact_refs(events: Sequence[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for event in reversed(events):
        artifact = event.get("artifact")
        if not isinstance(artifact, dict):
            continue
        ref = artifact.get("ref") or artifact.get("path")
        if not ref or str(ref) in seen:
            continue
        seen.add(str(ref))
        refs.append({
            "ref": str(ref),
            "kind": artifact.get("kind"),
            "observation_id": artifact.get("observation_id"),
        })
        if len(refs) >= limit:
            break
    refs.reverse()
    return refs


def _current_state(task: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    task = task or {}
    runtime = task.get("runtime") or {}
    return {
        key: value
        for key, value in {
            "task_status": task.get("status"),
            "node": task.get("node") or task.get("stage"),
            "stage": task.get("stage") or task.get("node"),
            "agent": task.get("agent") or runtime.get("agent"),
            "agent_name": task.get("agent_name") or runtime.get("agent_name"),
            "runtime_status": runtime.get("status"),
        }.items()
        if value is not None
    }


def _verification_facts(events: Iterable[Dict[str, Any]], task: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    facts: List[Dict[str, Any]] = []
    for event in events:
        verification = event.get("verification")
        if not isinstance(verification, dict) and event.get("event_type") != "verification_completed":
            continue
        verification = dict(verification or {})
        fact = {
            "fact_type": "verification",
            "event_id": _event_id(event),
            "passed": bool(verification.get("passed")) if "passed" in verification else False,
        }
        for key in ("passed_tests", "total_tests", "evidence_id", "observation_id", "status"):
            if key in verification:
                fact[key] = verification[key]
        facts.append(fact)
    if not facts and task and task.get("status"):
        facts.append({"fact_type": "task_status", "status": task["status"]})
    return facts


def _goal(task: Optional[Dict[str, Any]], events: Sequence[Dict[str, Any]]) -> Optional[str]:
    if task and task.get("goal"):
        return str(task["goal"])
    for event in events:
        for candidate in (event.get("goal"), (event.get("metadata") or {}).get("goal")):
            if candidate:
                return str(candidate)[:4000]
    return None


def _compact_input(
    *,
    goal: Optional[str],
    current_state: Dict[str, Any],
    previous_context: Optional[ContextPack],
    events: Sequence[Dict[str, Any]],
    findings: Sequence[Dict[str, Any]],
    observations: Sequence[Dict[str, Any]],
    artifacts: Sequence[Dict[str, Any]],
    max_chars: int,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "goal": goal,
        "current_state": current_state,
        "previous_context": previous_context.to_mapping() if previous_context else None,
        "recent_events": list(events),
        "findings": list(findings),
        "observation_metadata": list(observations),
        "artifact_refs": list(artifacts),
    }

    def size() -> int:
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    while size() > max_chars and payload["recent_events"]:
        payload["recent_events"].pop(0)
    while size() > max_chars and len(payload["findings"]) > 1:
        low = min(range(len(payload["findings"])), key=lambda i: {"critical": 3, "warning": 2, "info": 1}.get(payload["findings"][i].get("severity"), 0))
        payload["findings"].pop(low)
    while size() > max_chars and payload["observation_metadata"]:
        payload["observation_metadata"].pop(0)
    while size() > max_chars and payload["artifact_refs"]:
        payload["artifact_refs"].pop(0)
    if size() > max_chars:
        payload["recent_events"] = []
        payload["observation_metadata"] = []
        payload["artifact_refs"] = []
        payload["previous_context"] = None
    if size() > max_chars:
        payload["findings"] = payload["findings"][:1]
    if size() > max_chars:
        payload["goal"] = (goal or "")[: max(0, max_chars // 4)]
    if size() > max_chars:
        payload["current_state"] = {
            key: str(value)[:128]
            for key, value in payload["current_state"].items()
        }
    if size() > max_chars:
        payload["findings"] = [
            {key: item.get(key) for key in ("finding_id", "severity", "summary")}
            for item in payload["findings"][:1]
        ]
    if size() > max_chars:
        payload["goal"] = ""
        payload["current_state"] = {}
        payload["findings"] = []
    return payload


def _reducer_output(provider: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    if provider is None:
        return {}
    reducer = getattr(provider, "reduce", None)
    if reducer is None and callable(provider):
        reducer = provider
    if reducer is not None:
        result = reducer(payload)
    elif hasattr(provider, "choose"):
        findings = payload.get("findings") or []
        if not findings:
            return {}
        options = {
            str(item.get("finding_id")): str(item.get("summary") or item.get("finding_type") or "")
            for item in findings if item.get("finding_id")
        }
        result = provider.choose(
            {"instructions": "Select the single most important current finding.", "criteria": "Return one option key."},
            payload,
            options,
        )
        selected = getattr(result, "value", None)
        return {"selected_findings": [str(selected)]} if selected in options else {}
    else:
        return {}
    if isinstance(result, str):
        result = json.loads(result)
    return dict(result) if isinstance(result, dict) else {}


def verify_context_references(
    semantic: Dict[str, Any],
    *,
    event_ids: Set[str],
    finding_ids: Set[str],
    observation_ids: Set[str],
    artifact_refs: Set[str],
) -> Dict[str, Any]:
    valid = event_ids | finding_ids | observation_ids | artifact_refs

    def refs(item: Any) -> List[str]:
        if not isinstance(item, dict):
            return []
        return [str(ref) for ref in item.get("refs", []) if str(ref) in valid]

    def items(key: str) -> List[Dict[str, Any]]:
        result = []
        for item in semantic.get(key, []) if isinstance(semantic.get(key), list) else []:
            if not isinstance(item, dict):
                continue
            item = dict(item)
            item["refs"] = refs(item)
            if item["refs"]:
                result.append(item)
        return result

    return {
        "completed": items("completed"),
        "open_issues": items("open_issues"),
        "next_focus": items("next_focus"),
        "selected_findings": [str(ref) for ref in semantic.get("selected_findings", []) if str(ref) in finding_ids],
        "selected_observations": [str(ref) for ref in semantic.get("selected_observations", []) if str(ref) in observation_ids],
        "selected_artifacts": [str(ref) for ref in semantic.get("selected_artifacts", []) if str(ref) in artifact_refs],
    }


def _fallback_semantic(events: Sequence[Dict[str, Any]], findings: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    completed = []
    for event in events:
        if event.get("event_type") in {"task_completed", "agent_done", "verification_completed"}:
            completed.append({"text": str(event.get("event_type")), "refs": [_event_id(event)]})
    open_issues = [
        {"text": finding.get("summary") or finding.get("finding_type"), "refs": [finding["finding_id"]]}
        for finding in findings
        if finding.get("finding_id")
    ]
    return {"completed": completed, "open_issues": open_issues, "next_focus": []}


def compact_run(
    run_id: str,
    *,
    task: Optional[Dict[str, Any]] = None,
    store: Any = None,
    provider: Any = None,
    config: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
) -> ContextPack:
    """Create or return the latest bounded ContextPack for one run."""
    if not run_id:
        raise ValueError("run_id is required")
    cfg = _config(config)
    if not cfg["enabled"]:
        raise RuntimeError("context compact is disabled")
    db_path = _db_path(store)
    with _COMPACT_LOCK:
        source_sequence = state_db.latest_trajectory_sequence(run_id, db_path=db_path)
        recent_rows = state_db.list_trajectory_events(
            run_id, limit=cfg["max_recent_events"], desc=True, db_path=db_path,
        )
        verification_rows = state_db.list_trajectory_events(
            run_id, event_type="verification_completed", limit=10, desc=True, db_path=db_path,
        )
        recent_rows = [TrajectoryLedger._decode(event) for event in recent_rows]
        verification_rows = [TrajectoryLedger._decode(event) for event in verification_rows]
        by_id = {_event_id(event): event for event in recent_rows + verification_rows}
        all_events = sorted(by_id.values(), key=lambda event: int(event.get("sequence") or 0))
        latest = state_db.get_latest_context_pack(run_id, db_path=db_path)
        if latest is not None and int(latest.get("source_event_sequence") or 0) == source_sequence:
            return ContextPack.from_mapping(latest)
        previous = ContextPack.from_mapping(latest) if latest else None
        events = [event for event in all_events if event in recent_rows]
        findings = _findings(run_id, db_path, cfg["max_findings"])
        observations = _observation_metadata(run_id, db_path, cfg["max_observations"])
        artifacts = _artifact_refs(events, cfg["max_artifacts"])
        if task is None:
            task_id = next((event.get("task_id") for event in reversed(all_events) if event.get("task_id")), None)
            if task_id:
                task = state_db.get_task(str(task_id), db_path=db_path)
        goal = _goal(task, all_events)
        current_state = _current_state(task)
        compact_input = _compact_input(
            goal=goal, current_state=current_state, previous_context=previous,
            events=events, findings=findings, observations=observations,
            artifacts=artifacts, max_chars=cfg["max_input_chars"],
        )
        try:
            semantic = _reducer_output(provider, compact_input)
        except Exception as exc:
            LOGGER.warning("context reducer skipped: run=%s error=%s: %s", run_id, type(exc).__name__, exc)
            semantic = {}
        semantic = {**_fallback_semantic(events, findings), **semantic}
        verified = _verification_facts(all_events, task)
        event_ids = {_event_id(event) for event in all_events}
        finding_ids = {str(finding.get("finding_id")) for finding in findings}
        observation_ids = {str(observation["observation_id"]) for observation in observations}
        artifact_set = {str(item["ref"]) for item in artifacts}
        selected = verify_context_references(
            semantic, event_ids=event_ids, finding_ids=finding_ids,
            observation_ids=observation_ids, artifact_refs=artifact_set,
        )
        selected_findings = {item["finding_id"]: item for item in findings if item.get("finding_id")}
        important = [
            {
                "finding_id": selected_findings[key]["finding_id"],
                "finding_type": selected_findings[key].get("finding_type"),
                "severity": selected_findings[key].get("severity"),
                "summary": str(selected_findings[key].get("summary") or "")[:1000],
            }
            for key in selected["selected_findings"]
            if key in selected_findings
        ][:5]
        if not important:
            important = [
                {
                    "finding_id": finding["finding_id"],
                    "finding_type": finding.get("finding_type"),
                    "severity": finding.get("severity"),
                    "summary": str(finding.get("summary") or "")[:1000],
                }
                for finding in findings[:5]
            ]
        evidence_refs = selected["selected_observations"] or [str(item["observation_id"]) for item in observations]
        artifact_refs = [item for item in artifacts if item["ref"] in selected["selected_artifacts"] or not selected["selected_artifacts"]]
        pack = ContextPack(
            context_id=f"ctx_{uuid.uuid4().hex}", run_id=run_id,
            task_id=(task or {}).get("task_id") or (all_events[-1].get("task_id") if all_events else None),
            workflow_id=(task or {}).get("workflow_id") or (all_events[-1].get("workflow_id") if all_events else None),
            goal=goal, current_state=current_state,
            completed=selected["completed"], verified_facts=verified,
            important_findings=important, evidence_refs=evidence_refs,
            artifact_refs=artifact_refs, open_issues=selected["open_issues"],
            next_focus=selected["next_focus"][:3], source_event_sequence=source_sequence,
            created_at=float(now if now is not None else time.time()),
            metadata={"analysis": {"completed": True, "open_issues": True, "next_focus": True}, "input_chars": len(json.dumps(compact_input, ensure_ascii=False))},
        )
        stored = state_db.save_context_pack(pack.to_mapping(), db_path=db_path)
        return ContextPack.from_mapping(stored)


def get_context(context_id: str, *, store: Any = None) -> Optional[ContextPack]:
    row = state_db.get_context_pack(context_id, db_path=_db_path(store))
    return ContextPack.from_mapping(row) if row else None


def get_latest_context(run_id: str, *, store: Any = None) -> Optional[ContextPack]:
    row = state_db.get_latest_context_pack(run_id, db_path=_db_path(store))
    return ContextPack.from_mapping(row) if row else None


def list_contexts(run_id: str, *, store: Any = None) -> List[ContextPack]:
    return [ContextPack.from_mapping(row) for row in state_db.list_context_packs(run_id, db_path=_db_path(store))]


def compact_run_best_effort(run_id: str, **kwargs: Any) -> Optional[ContextPack]:
    try:
        return compact_run(run_id, **kwargs)
    except Exception as exc:  # pragma: no cover - boundary safety net
        LOGGER.warning("context compact skipped: run=%s error=%s: %s", run_id, type(exc).__name__, exc)
        return None


__all__ = [
    "ContextPack", "compact_run", "compact_run_best_effort", "get_context",
    "get_latest_context", "list_contexts", "verify_context_references",
]
