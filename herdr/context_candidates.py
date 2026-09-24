"""Pure candidate builders for WorkingContext.

All functions in this module are side-effect free; the compiler supplies a
validated source snapshot and the bounded item helper.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple

from .context_sources import _source_allowed, _task_value
from .context_models import (
    TERMINAL_COMPLETION_EVENTS,
    VERIFICATION_EVENTS,
    _as_list,
    _canonical_evidence_ref,
    _item,
    _relation_ids,
    _safe_float,
    _task_run,
)
from .transitions import COMPLETED_TASK_STATUSES


def _event_payload(event: Mapping[str, Any]) -> Dict[str, Any]:
    payload = event.get("payload")
    return dict(payload) if isinstance(payload, Mapping) else {}


def _finding_candidates(
    findings: Sequence[Mapping[str, Any]],
    *,
    task_by_id: Mapping[str, Mapping[str, Any]],
    allowed_runs: Set[str],
    valid_evidence_refs: Set[str],
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
    invalid: Set[str] = set()
    relations: Dict[str, Set[str]] = {}
    for finding in valid:
        finding_id = str(finding.get("finding_id") or "")
        metadata = dict(finding.get("metadata") or {}) if isinstance(finding.get("metadata"), Mapping) else {}
        for relation_key in ("supersedes", "superseded_by"):
            if finding.get(relation_key) is not None and relation_key not in metadata:
                metadata[relation_key] = finding[relation_key]
        supersedes_targets = _relation_ids(metadata, "supersedes")
        superseded_by_targets = _relation_ids(metadata, "superseded_by")
        targets = [*supersedes_targets, *superseded_by_targets]
        if targets:
            relations[finding_id] = set(targets)
        for target in supersedes_targets:
            if target not in by_id:
                invalid.add(finding_id)
            else:
                superseded.add(target)
        for target in superseded_by_targets:
            if target not in by_id:
                invalid.add(finding_id)
            else:
                superseded.add(finding_id)
    visiting: Set[str] = set()
    visited: Set[str] = set()

    def visit(finding_id: str) -> None:
        if finding_id in visiting:
            cycle = set(visiting)
            invalid.update(cycle)
            return
        if finding_id in visited:
            return
        visiting.add(finding_id)
        for target in relations.get(finding_id, set()):
            visit(target)
        visiting.remove(finding_id)
        visited.add(finding_id)

    for finding_id in relations:
        visit(finding_id)
    result: List[Dict[str, Any]] = []
    for finding in valid:
        finding_id = str(finding.get("finding_id") or "")
        if not finding_id or finding_id in superseded or finding_id in invalid:
            continue
        if str(finding.get("status") or "open") in {"superseded", "closed", "resolved"}:
            continue
        metadata = dict(finding.get("metadata") or {}) if isinstance(finding.get("metadata"), Mapping) else {}
        for relation_key in ("supersedes", "superseded_by"):
            if finding.get(relation_key) is not None and relation_key not in metadata:
                metadata[relation_key] = finding[relation_key]
        evidence_refs = []
        for raw in _as_list(finding.get("evidence")):
            ref = _canonical_evidence_ref(raw)
            if ref and ref in valid_evidence_refs and ref not in evidence_refs:
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
    valid_evidence_refs: Set[str],
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
                if ref and ref in valid_evidence_refs and ref not in evidence_refs:
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
    valid_evidence_refs: Set[str],
    workflow_id: str,
    run_scope: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return artifacts, completed, verification, decisions, and blockers."""
    artifacts: List[Dict[str, Any]] = []
    completed: List[Dict[str, Any]] = []
    verification: List[Dict[str, Any]] = []
    decisions: List[Dict[str, Any]] = []
    blockers: List[Dict[str, Any]] = []
    active_failures: Dict[Tuple[str, str], Set[str]] = {}
    recovery_types = {"task_started", "task_completed", "run_completed", "agent_done"}
    for event in sorted(events, key=lambda item: int(item.get("sequence") or 0)):
        event_key = (str(event.get("run_id") or ""), str(event.get("task_id") or ""))
        event_id = str(event.get("event_id") or "")
        event_type = str(event.get("event_type") or "")
        if event_type in {"blocker", "task_failed", "agent_failed", "run_failed"}:
            active_failures.setdefault(event_key, set()).add(event_id)
        elif event_type in recovery_types:
            active_failures.pop(event_key, None)
        elif event_type == "task_status_changed":
            status = str(_event_payload(event).get("status") or "")
            if status and status not in {"blocked", "failed"}:
                active_failures.pop(event_key, None)
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
                if ref and ref in valid_evidence_refs and ref not in evidence_refs:
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
                    "decision",
                    value,
                    event_ref,
                    metadata={"event_type": event_type, "node": event.get("node_id")},
                    **common,
                ))
        elif event_type in {"blocker", "task_failed", "agent_failed", "run_failed"}:
            event_key = (str(event.get("run_id") or ""), str(event.get("task_id") or ""))
            if event_ref.removeprefix("trajectory:") not in active_failures.get(event_key, set()):
                continue
            task = task_by_id.get(event_key[1])
            if task and str(task.get("status") or "") in COMPLETED_TASK_STATUSES:
                continue
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
            reason = (
                payload.get("reason") or payload.get("blocker")
                or metadata.get("reason") or metadata.get("blocker") or event_type
            )
            blockers.append(_item(
                "blocker",
                {"reason": reason, "event_type": event_type},
                event_ref,
                metadata={"event_type": event_type, "status": "blocked", "node": event.get("node_id")},
                **common,
            ))
    return artifacts, completed, verification, decisions, blockers


def _task_artifact_candidates(tasks: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for task in tasks:
        task_id = str(task.get("task_id") or "")
        task_ref = f"task:{task_id}"
        raw_artifacts: List[Any] = []
        for key in ("artifacts", "artifact_refs", "changed_artifacts", "deliverables"):
            raw_artifacts.extend(_as_list(task.get(key)))
        for artifact in raw_artifacts:
            if isinstance(artifact, Mapping):
                ref = artifact.get("ref") or artifact.get("path") or artifact.get("name")
                kind = artifact.get("kind")
            else:
                ref = str(artifact)
                kind = None
            if not ref:
                continue
            result.append(_item(
                "artifact",
                {"ref": str(ref), "kind": kind},
                task_ref,
                source_task=task_id,
                source_run=_task_run(task),
                created_at=task.get("updated_at"),
                metadata={"node": task.get("node") or task.get("stage"), "source_field": "task"},
            ))
    return result


def _task_candidates(
    tasks: Sequence[Mapping[str, Any]],
    *,
    dependency_ids: Sequence[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    completed: List[Dict[str, Any]] = []
    blockers: List[Dict[str, Any]] = []
    decisions: List[Dict[str, Any]] = []
    questions: List[Dict[str, Any]] = []
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
        blocker_values = []
        for key in ("blocker", "blocked_reason", "open_blockers", "blockers"):
            blocker_values.extend(_as_list(task.get(key)))
        if status == "blocked" and not blocker_values:
            blocker_values.append(status)
        for reason in blocker_values:
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
                "decision",
                value,
                task_ref,
                source_task=task_id,
                source_run=_task_run(task),
                created_at=task.get("updated_at"),
                metadata={"node": task.get("node") or task.get("stage"), "status": status},
            ))
        for key in ("open_questions", "questions", "question", "decision_question", "acceptance_gap"):
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
    return completed, blockers, decisions, questions


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

