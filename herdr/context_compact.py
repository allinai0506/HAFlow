"""Semantic Context Compact V1: bounded, reference-verified working memory.

Trajectory is fact history, Observation is evidence, and Finding is analysis.
This module creates a small append-only ContextPack snapshot from those layers.
The optional reducer may select semantic text, but program code owns facts and
removes every reference that cannot be resolved in the source stores.
"""

from __future__ import annotations

import json
import hashlib
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
    return state_db.list_trajectory_findings_bounded(run_id, limit, db_path=db_path)


def _observation_metadata(run_id: str, db_path: Optional[Path], limit: int) -> List[Dict[str, Any]]:
    rows = state_db.list_observations(run_id=run_id, limit=limit, desc=True, db_path=db_path)
    result = [
        {
            "observation_id": row["observation_id"],
            "source_type": row["source_type"],
            "source_ref": row["source_ref"],
            "sha256": row["sha256"],
            "excerpt": row.get("excerpt"),
        }
        for row in rows
    ]
    result.reverse()
    return result


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
    candidates: Dict[str, List[Dict[str, Any]]],
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
        "candidate_semantics": candidates,
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
    while size() > max_chars and any(payload["candidate_semantics"].values()):
        for category in ("next_focus", "open_issues", "completed"):
            if payload["candidate_semantics"].get(category):
                payload["candidate_semantics"][category].pop(0)
                break
    if size() > max_chars:
        payload["candidate_semantics"] = {}
        payload["previous_context"] = None
    if size() > max_chars:
        payload = {"goal": "", "current_state": {}, "recent_events": [], "findings": [], "observation_metadata": [], "artifact_refs": [], "candidate_semantics": {}}
    while size() > max_chars and payload["goal"]:
        payload["goal"] = payload["goal"][:-1]
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
        candidates = payload.get("candidate_semantics") or {}
        output: Dict[str, Any] = {}
        # DecisionProvider's standard judge_many path selects semantic items;
        # the text and refs remain program-produced candidates.
        questions = {}
        candidate_index = {}
        for category in ("completed", "open_issues", "next_focus"):
            for index, item in enumerate(candidates.get(category) or []):
                key = f"{category}:{index}"
                questions[key] = {
                    "instructions": f"Is this {category} item important for the next agent?",
                    "criteria": "Return a probability from 0 to 1.",
                }
                candidate_index[key] = (category, item)
        if questions and hasattr(provider, "judge_many"):
            results = provider.judge_many(questions, payload)
            for category in ("completed", "open_issues", "next_focus"):
                selected_items = []
                for key, (item_category, item) in candidate_index.items():
                    if item_category != category:
                        continue
                    result = results.get(key)
                    value = getattr(result, "value", result)
                    try:
                        keep = float(value) >= 0.5
                    except (TypeError, ValueError):
                        keep = bool(value) is True
                    if keep:
                        selected_items.append(item)
                if selected_items:
                    output[category] = selected_items[:3] if category == "next_focus" else selected_items
        findings = payload.get("findings") or []
        options = {
            str(item.get("finding_id")): str(item.get("summary") or item.get("finding_type") or "")
            for item in findings if item.get("finding_id")
        }
        if options:
            result = provider.choose(
                {"instructions": "Select the single most important current finding.", "criteria": "Return one option key."},
                payload, options,
            )
            selected = getattr(result, "value", None)
            if selected in options:
                output["selected_findings"] = [str(selected)]
        return output
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


def _semantic_candidates(
    events: Sequence[Dict[str, Any]],
    findings: Sequence[Dict[str, Any]],
    previous: Optional[ContextPack],
) -> Dict[str, List[Dict[str, Any]]]:
    fallback = _fallback_semantic(events, findings, previous)
    return {key: list(value) for key, value in fallback.items() if key in {"completed", "open_issues", "next_focus"}}


def _fallback_semantic(events: Sequence[Dict[str, Any]], findings: Sequence[Dict[str, Any]], previous: Optional[ContextPack] = None) -> Dict[str, Any]:
    completed = list(previous.completed if previous else [])
    seen_completed = {(item.get("text"), tuple(item.get("refs") or [])) for item in completed}
    for event in events:
        if event.get("event_type") in {"task_completed", "agent_done", "verification_completed", "artifact_created"}:
            item = {"text": str(event.get("event_type")), "refs": [_event_id(event)]}
            if (item["text"], tuple(item["refs"])) not in seen_completed:
                completed.append(item)
                seen_completed.add((item["text"], tuple(item["refs"])))
    open_issues = list(previous.open_issues if previous else [])
    seen_issues = {(item.get("text"), tuple(item.get("refs") or [])) for item in open_issues}
    for finding in findings:
        item = {"text": finding.get("summary") or finding.get("finding_type"), "refs": [finding["finding_id"]]}
        if finding.get("finding_id") and (item["text"], tuple(item["refs"])) not in seen_issues:
            open_issues.append(item)
            seen_issues.add((item["text"], tuple(item["refs"])))
    next_focus = list(previous.next_focus if previous else [])
    seen_focus = {(item.get("text"), tuple(item.get("refs") or [])) for item in next_focus}
    for finding in findings:
        if finding.get("finding_id") and finding.get("recommended_action"):
            item = {"text": finding["recommended_action"], "refs": [finding["finding_id"]]}
            if (item["text"], tuple(item["refs"])) not in seen_focus:
                next_focus.append(item)
                seen_focus.add((item["text"], tuple(item["refs"])))
    return {"completed": completed, "open_issues": open_issues, "next_focus": next_focus}


def _source_fingerprint(
    run_id: str,
    source_sequence: int,
    task: Optional[Dict[str, Any]],
    current_state: Dict[str, Any],
    db_path: Optional[Path],
) -> str:
    conn = state_db.get_db_connection(db_path)
    try:
        finding = conn.execute(
            "SELECT COUNT(*) AS count, COALESCE(MAX(created_at), 0) AS latest, COALESCE(MAX(rowid), 0) AS rowid FROM trajectory_findings WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        observation = conn.execute(
            "SELECT COUNT(*) AS count, COALESCE(MAX(created_at), 0) AS latest, COALESCE(MAX(rowid), 0) AS rowid FROM observations WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()
    source = {
        "run_id": run_id,
        "trajectory_sequence": source_sequence,
        "task": {"task_id": (task or {}).get("task_id"), "workflow_id": (task or {}).get("workflow_id"), "goal": (task or {}).get("goal")},
        "current_state": current_state,
        "findings": dict(finding) if finding else {},
        "observations": dict(observation) if observation else {},
    }
    return hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _previously_verified_refs(previous: Optional[ContextPack], run_id: str, db_path: Optional[Path]) -> Dict[str, Set[str]]:
    if previous is None:
        return {"events": set(), "findings": set(), "observations": set(), "artifacts": set()}
    events: Set[str] = set()
    findings: Set[str] = {str(item.get("finding_id")) for item in previous.important_findings if item.get("finding_id")}
    observations: Set[str] = {str(value) for value in previous.evidence_refs if value}
    artifacts: Set[str] = set()
    for item in previous.artifact_refs:
        if isinstance(item, dict) and item.get("ref"):
            artifacts.add(str(item["ref"]))
        elif item:
            artifacts.add(str(item))
    for item in list(previous.completed) + list(previous.open_issues) + list(previous.next_focus):
        for ref in item.get("refs", []) if isinstance(item, dict) else []:
            value = str(ref)
            if value.startswith("evt_"):
                events.add(value)
            elif value.startswith("finding_"):
                findings.add(value)
            elif value.startswith("obs_"):
                observations.add(value)
            else:
                artifacts.add(value)
    verified = {
        "events": {ref for ref in events if state_db.trajectory_event_exists(ref, run_id, db_path=db_path)},
        "findings": {ref for ref in findings if state_db.get_trajectory_finding_by_id(ref, run_id, db_path=db_path)},
        "observations": {ref for ref in observations if state_db.get_observation(ref, db_path=db_path)},
        "artifacts": {ref for ref in artifacts if state_db.artifact_ref_exists(ref, run_id, db_path=db_path)},
    }
    return verified


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
    if task is not None and task.get("run_id") and str(task["run_id"]) != str(run_id):
        raise ValueError(f"task.run_id {task['run_id']} does not match run_id {run_id}")
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
        fingerprint = _source_fingerprint(run_id, source_sequence, task, current_state, db_path)
        if latest and (latest.get("metadata") or {}).get("context_source_fingerprint") == fingerprint:
            return ContextPack.from_mapping(latest)
        candidates = _semantic_candidates(events, findings, previous)
        compact_input = _compact_input(
            goal=goal, current_state=current_state, previous_context=previous,
            events=events, findings=findings, observations=observations,
            artifacts=artifacts, candidates=candidates, max_chars=cfg["max_input_chars"],
        )
        try:
            semantic = _reducer_output(provider, compact_input)
        except Exception as exc:
            LOGGER.warning("context reducer skipped: run=%s error=%s: %s", run_id, type(exc).__name__, exc)
            semantic = {}
        semantic = {**candidates, **semantic}
        verified = list(previous.verified_facts if previous else [])
        current_facts = _verification_facts(all_events, task)
        known_fact_keys = {json.dumps(fact, sort_keys=True, default=str) for fact in verified}
        verified.extend(fact for fact in current_facts if json.dumps(fact, sort_keys=True, default=str) not in known_fact_keys)
        previous_refs = _previously_verified_refs(previous, run_id, db_path)
        event_ids = {_event_id(event) for event in all_events} | previous_refs["events"]
        finding_ids = {str(finding.get("finding_id")) for finding in findings} | previous_refs["findings"]
        observation_ids = {str(observation["observation_id"]) for observation in observations} | previous_refs["observations"]
        artifact_set = {str(item["ref"]) for item in artifacts} | previous_refs["artifacts"]
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
        if previous:
            known = {item.get("finding_id") for item in important}
            for old in previous.important_findings:
                finding_id = old.get("finding_id") if isinstance(old, dict) else None
                if finding_id and finding_id in previous_refs["findings"] and finding_id not in known:
                    important.append(old)
                    known.add(finding_id)
                    if len(important) >= 5:
                        break
        evidence_refs = selected["selected_observations"] or [str(item["observation_id"]) for item in observations]
        evidence_refs = list(dict.fromkeys(list(previous_refs["observations"]) + evidence_refs))
        artifact_refs = [item for item in artifacts if item["ref"] in selected["selected_artifacts"] or not selected["selected_artifacts"]]
        if previous:
            old_artifacts = [item for item in previous.artifact_refs if isinstance(item, dict) and item.get("ref") in previous_refs["artifacts"]]
            artifact_refs = old_artifacts + artifact_refs
            seen_artifact_refs = set()
            artifact_refs = [item for item in artifact_refs if not (item.get("ref") in seen_artifact_refs or seen_artifact_refs.add(item.get("ref")))]
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
            metadata={"analysis": {"completed": True, "open_issues": True, "next_focus": True}, "input_chars": len(json.dumps(compact_input, ensure_ascii=False)), "context_source_fingerprint": fingerprint},
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
