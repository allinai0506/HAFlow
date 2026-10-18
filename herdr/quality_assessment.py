"""Deterministic execution-quality assessment (herdr/quality_assessment.py).

One ``(task_id, run_id)`` in, one report out. Settled outcome fields are
copied, not recomputed. Missing trace, attribution, verification, or exit
codes stay unknown. This module does not write the task database, dispatch
an agent, or publish an experience rule.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .execution_trace import AGENT_ACTION_KINDS, _argv, _test_runner


ASSESSOR_VERSION = "1"
_EVENT_CAP = 2000
_ACTION_CAP = 500
_ANOMALY_CAP = 20
_EVIDENCE_CAP = 40
_UNKNOWN_NOTES = frozenset({
    "trajectory_unavailable",
    "native_unavailable",
    "native_unverified",
    "native_corrupt",
    "stored_payload_corrupt",
    "historical_run_trace_limited",
    "human_signals_truncated",
})
_PARTIAL_NOTES = frozenset({"native_truncated", "native_index_budget"})


def assess_task_run(
    task_id: str,
    run_id: str,
    *,
    db_path: Optional[Path] = None,
    roots=None,
    budget_s: float = 4.0,
) -> Dict[str, Any]:
    """Assess one run from the existing database. Never writes, never raises."""
    task_id = str(task_id or "").strip()
    run_id = str(run_id or "").strip()
    if not task_id or not run_id:
        return _empty(task_id, run_id, ["identity_incomplete"])
    try:
        from .quality_sources import load_quality_inputs
        loaded = load_quality_inputs(
            task_id, run_id, db_path=db_path, roots=roots, budget_s=budget_s,
        )
        return assess_quality(task_id, run_id, **loaded)
    except Exception:
        return _empty(task_id, run_id, ["assessment_unavailable"])


def assess_quality(
    task_id: str,
    run_id: str,
    *,
    outcome: Optional[Dict[str, Any]],
    events: Sequence[Dict[str, Any]] = (),
    actions: Sequence[Dict[str, Any]] = (),
    degradation: Sequence[Dict[str, Any]] = (),
    human_signals: Optional[Dict[str, Any]] = None,
    allow_legacy_unscoped: bool = False,
    trace_truncated: bool = False,
) -> Dict[str, Any]:
    """Judge already loaded facts. No I/O."""
    task_id = str(task_id or "")
    run_id = str(run_id or "")
    codes = set()
    evidence: List[Dict[str, str]] = []
    outcome_id = None
    correctness = "unknown"
    rework = None
    blocked = None
    human_count = None
    if outcome is None:
        codes.add("outcome_absent")
    elif not isinstance(outcome, dict) or not isinstance(outcome.get("qualified_success"), bool):
        codes.add("outcome_unreadable")
    else:
        outcome_id = str(outcome.get("outcome_id") or "") or None
        correctness = "success" if outcome["qualified_success"] else "failure"
        rework = _count(outcome.get("rework_count"))
        blocked = _count(outcome.get("blocked_count"))
        human_count = _count(outcome.get("human_intervention_count"))
        if rework is None or blocked is None or human_count is None:
            codes.add("outcome_count_unreadable")
        if outcome_id:
            evidence.append({"kind": "outcome", "ref": outcome_id})

    scoped, foreign, unscoped = _scope_events(events, run_id, allow_legacy_unscoped)
    if foreign:
        codes.add("foreign_run_dropped")
    if unscoped:
        codes.add("unscoped_evidence_ignored")
    if len(scoped) > _EVENT_CAP:
        head = scoped[:_EVENT_CAP]
        late_failures = [event for event in scoped[_EVENT_CAP:] if event.get("kind") == "test_failed"]
        if late_failures:
            room = min(len(late_failures), _EVENT_CAP)
            head = head[:_EVENT_CAP - room] + late_failures[:room]
        scoped = head
        trace_truncated = True

    anomalies = _anomalies(scoped)
    if outcome is None and not _has_verification(scoped):
        codes.add("verification_missing")

    explicit_refs, implicit_refs, exit_unknown = _retries(actions[:_ACTION_CAP])
    if len(actions) > _ACTION_CAP:
        trace_truncated = True
    if exit_unknown:
        codes.add("exit_code_unknown")
    if explicit_refs:
        codes.add("explicit_retry")
        evidence.extend({"kind": "native_action", "ref": ref} for ref in explicit_refs)
    if implicit_refs:
        codes.add("implicit_retry")
        evidence.extend({"kind": "native_action", "ref": ref} for ref in implicit_refs)
    for event in scoped:
        if event.get("kind") == "retry" and event.get("id"):
            codes.add("explicit_retry")
            evidence.append({"kind": "trace_event", "ref": str(event["id"])})

    corrective, decisions, retry_refs = _humans(human_signals or {}, run_id, evidence)
    if retry_refs:
        codes.add("explicit_retry")
    if (
        isinstance(human_count, int)
        and human_count > corrective + decisions
    ):
        codes.add("human_intervention_unclassified")

    completeness = _completeness(
        scoped, degradation, trace_truncated, codes, unscoped,
    )
    if anomalies:
        codes.add("test_anomaly")
        evidence.extend(
            {"kind": "trace_event", "ref": item["evidence_ref"]}
            for item in anomalies
            if item.get("evidence_ref")
        )
    if correctness == "failure":
        codes.add("business_failure")
    if isinstance(rework, int) and rework > 0:
        codes.add("rework_observed")
    if isinstance(blocked, int) and blocked > 0:
        codes.add("blocked_observed")
    if corrective:
        codes.add("corrective_human")

    process = _process(correctness, codes, completeness)
    return {
        "assessor_version": ASSESSOR_VERSION,
        "task_id": task_id,
        "run_id": run_id,
        "outcome_id": outcome_id,
        "correctness": correctness,
        "process_quality": process,
        "rework_count": rework,
        "blocked_count": blocked,
        "human_intervention_count": human_count,
        "corrective_human_count": corrective,
        "workflow_decision_count": decisions,
        "test_anomalies": anomalies[:_ANOMALY_CAP],
        "trace_completeness": completeness,
        "problem_codes": sorted(codes),
        "evidence": _dedupe(evidence)[:_EVIDENCE_CAP],
    }


def _empty(task_id: str, run_id: str, codes: Sequence[str]) -> Dict[str, Any]:
    return {
        "assessor_version": ASSESSOR_VERSION,
        "task_id": task_id,
        "run_id": run_id,
        "outcome_id": None,
        "correctness": "unknown",
        "process_quality": "unknown",
        "rework_count": None,
        "blocked_count": None,
        "human_intervention_count": None,
        "corrective_human_count": 0,
        "workflow_decision_count": 0,
        "test_anomalies": [],
        "trace_completeness": "unknown",
        "problem_codes": sorted(set(codes)),
        "evidence": [],
    }


def _count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def _scope_events(events, run_id: str, allow_legacy: bool):
    scoped = []
    foreign = False
    unscoped = False
    for event in events:
        if not isinstance(event, dict):
            continue
        row_run = event.get("run_id")
        if row_run in (None, ""):
            if allow_legacy:
                scoped.append(event)
            else:
                unscoped = True
            continue
        if str(row_run) == run_id:
            scoped.append(event)
        else:
            foreign = True
    return scoped, foreign, unscoped


def _anomalies(events: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
    found = []
    for event in events:
        if event.get("kind") != "test_failed":
            continue
        ref = str(event.get("id") or event.get("source_ref") or "")
        if not ref:
            continue
        found.append({"code": "test_failed", "evidence_ref": ref})
    return found


def _has_verification(events: Sequence[Dict[str, Any]]) -> bool:
    return any(
        event.get("source") == "trajectory"
        and event.get("kind") in {"test_passed", "test_failed"}
        for event in events
    )


def _retries(actions: Sequence[Dict[str, Any]]):
    usable = [item for item in actions if isinstance(item, dict)]
    known = {
        str(item.get("source_ref"))
        for item in usable
        if isinstance(item.get("source_ref"), str) and item.get("source_ref")
    }
    explicit = []
    consumed = set()
    for item in usable:
        retry_of = item.get("retry_of")
        source = item.get("source_ref")
        if (
            isinstance(retry_of, str)
            and retry_of in known
            and isinstance(source, str)
            and source
            and retry_of != source
        ):
            explicit.append(source)
            consumed.add(source)
            consumed.add(retry_of)
    implicit = []
    exit_unknown = False
    # Loaders concatenate sessions, so without complete timestamps the list
    # order is a chronology only inside one session. Pair a failure with its
    # retry inside that session; a pair spanning unordered sessions stays
    # unknown instead of being invented in loader order.
    per_session = not all(_has_timestamp(item) for item in usable)
    failed: Dict[Any, str] = {}
    for item in _ordered_actions(usable):
        source = item.get("source_ref")
        command = item.get("command") if isinstance(item.get("command"), str) else ""
        command = " ".join(command.split())
        exit_code = item.get("exit_code")
        cited = isinstance(source, str) and bool(source)
        if command and _is_test_command(command) and not isinstance(exit_code, int):
            exit_unknown = True
        if not cited or not command or source in consumed:
            continue
        if not isinstance(exit_code, int):
            continue
        key = (_session_key(item), command) if per_session else command
        if key in failed:
            implicit.extend([failed.pop(key), source])
            continue
        if exit_code != 0:
            failed[key] = source
    return _unique(explicit), _unique(implicit), exit_unknown


def _session_key(item: Dict[str, Any]) -> str:
    return str(item.get("session_id") or "")


def _ordered_actions(actions: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Order retries without treating a missing timestamp as time zero."""
    indexed = list(enumerate(actions))
    if any(not _has_timestamp(item) for _, item in indexed):
        return [item for _, item in indexed]
    return [
        item for _, item in sorted(
            indexed,
            key=lambda pair: (
                pair[1]["timestamp"],
                pair[1].get("ordinal") if isinstance(pair[1].get("ordinal"), int)
                and not isinstance(pair[1].get("ordinal"), bool) else pair[0],
                pair[0],
            ),
        )
    ]


def _has_timestamp(item: Dict[str, Any]) -> bool:
    timestamp = item.get("timestamp")
    return isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool)


def _is_test_command(command: str) -> bool:
    """Recognize the same runners as the trace classifier, quoting included."""
    return _test_runner(_argv(command) or command.split()) is not None


def _humans(signals: Dict[str, Any], run_id: str, evidence: List[Dict[str, str]]):
    corrective = 0
    decisions = 0
    retries = []
    steers = signals.get("steers") if isinstance(signals.get("steers"), list) else []
    for steer in steers:
        if not isinstance(steer, dict) or str(steer.get("run_id") or "") != run_id:
            continue
        ref = str(steer.get("ref") or "")
        if str(steer.get("decision_id") or "").strip() or str(steer.get("kind") or "") == "decision":
            decisions += 1
            if ref:
                evidence.append({"kind": "steer", "ref": ref})
            continue
        operator = steer.get("operator")
        if operator in (None, "", "human"):
            corrective += 1
            if ref:
                evidence.append({"kind": "steer", "ref": ref})
    interventions = signals.get("interventions") if isinstance(signals.get("interventions"), list) else []
    for item in interventions:
        if not isinstance(item, dict) or str(item.get("run_id") or "") != run_id:
            continue
        if str(item.get("action") or "") != "RETRY":
            continue
        ref = str(item.get("ref") or "")
        if ref:
            evidence.append({"kind": "intervention", "ref": ref})
            retries.append(ref)
    return corrective, decisions, retries


def _completeness(events, degradation, truncated: bool, codes: set, unscoped: bool) -> str:
    kinds = {event.get("kind") for event in events}
    partial = truncated or any(
        isinstance(note, dict) and note.get("code") in _PARTIAL_NOTES
        for note in degradation
    )
    unknown = unscoped
    for note in degradation:
        if not isinstance(note, dict):
            continue
        code = str(note.get("code") or "")
        if code == "native_unverified":
            codes.add("native_unattributed")
            unknown = True
        elif code == "trajectory_unavailable":
            codes.add("trace_unavailable")
            unknown = True
        elif code in _UNKNOWN_NOTES:
            codes.add(code)
            unknown = True
    if partial:
        codes.add("trace_truncated")
    if unknown:
        if not events and "trace_unavailable" not in codes and "historical_run_trace_limited" not in codes:
            codes.add("trace_missing")
        return "unknown"
    if partial:
        return "partial"
    if kinds & AGENT_ACTION_KINDS:
        return "complete"
    codes.add("trace_missing" if not events else "agent_actions_absent")
    return "unknown"


def _process(correctness: str, codes: set, completeness: str) -> str:
    signals = {
        "business_failure",
        "rework_observed",
        "blocked_observed",
        "test_anomaly",
        "explicit_retry",
        "implicit_retry",
        "corrective_human",
    }
    if codes & signals:
        return "improvement_opportunity"
    blocked = {
        "exit_code_unknown",
        "human_intervention_unclassified",
        "outcome_unreadable",
        "outcome_count_unreadable",
        "outcome_absent",
        "verification_missing",
    }
    if correctness == "success" and completeness == "complete" and not (codes & blocked):
        return "acceptable"
    return "unknown"


def _unique(values: Iterable[str]) -> List[str]:
    seen = set()
    ordered = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _dedupe(evidence: Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    ordered = []
    for item in evidence:
        key = (item.get("kind"), item.get("ref"))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        ordered.append({"kind": str(item["kind"]), "ref": str(item["ref"])})
    return ordered


__all__ = ["ASSESSOR_VERSION", "assess_quality", "assess_task_run"]
