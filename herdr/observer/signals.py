#!/usr/bin/env python3
"""Deterministic Observer signals (herdr/observer/signals.py).

Functional Core: pure detectors that turn real trajectory/runtime/log facts
into candidate findings. Each signal carries its own evidence references and a
stable dedup anchor. Nothing here calls a model, writes state, or guesses:
if the reliable facts are not present, no signal is produced.

The provider (``herdr/decision``) later confirms or suppresses these signals;
signals with ``requires_confirmation=False`` are evidence-backed facts that
survive even when no provider is available. Time alone never yields a
critical stall (see the task's Scenario guidance).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..supervisor.state import redact_text

TERMINAL_EVENT_TYPES = frozenset({
    "run_completed", "run_failed", "task_completed", "task_failed",
})

TERMINAL_TASK_STATUSES = frozenset({
    "completed", "failed", "superseded", "integrated", "cleanup_ready",
    "cleaned", "committed",
})

DONE_CLAIM_STATUSES = frozenset({
    "agent_done", "completed", "integrated", "cleanup_ready", "cleaned", "committed",
})

_ACTION_KEYS = ("command", "cmd", "tool", "name", "action")
_ERROR_MARKERS = re.compile(
    r"(?i)\b(error|exception|traceback|failed|failure|denied|unauthorized|forbidden|"
    r"not found|panic|fatal|timeout|refused)\b"
)
_DIGITS = re.compile(r"\d+")
_WHITESPACE = re.compile(r"\s+")


@dataclass
class Signal:
    """One deterministic candidate finding with evidence and dedup anchor."""

    finding_type: str
    severity: str
    summary: str
    suspected_cause: str
    recommended_action: str
    confidence: float
    evidence: List[Dict[str, Any]]
    anchor: str
    requires_confirmation: bool
    facts: Dict[str, Any] = field(default_factory=dict)


def event_ref(event: Dict[str, Any]) -> Dict[str, Any]:
    """Minimal trajectory reference (never a payload copy)."""
    ref = {
        "type": "trajectory",
        "event_id": event.get("event_id"),
        "sequence": event.get("sequence"),
        "event_type": event.get("event_type"),
    }
    return {key: value for key, value in ref.items() if value is not None}


def verification_ref(event: Dict[str, Any]) -> Dict[str, Any]:
    verification = event.get("verification") or {}
    ref = {
        "type": "verification",
        "event_id": event.get("event_id"),
        "sequence": event.get("sequence"),
        "passed": verification.get("passed"),
        "evidence_id": verification.get("evidence_id"),
    }
    failing = verification.get("failing_count")
    if failing is not None:
        ref["failing_count"] = failing
    return {key: value for key, value in ref.items() if value is not None}


def run_is_terminal(events: List[Dict[str, Any]]) -> bool:
    return any(event.get("event_type") in TERMINAL_EVENT_TYPES for event in events)


def _completed_run(events: List[Dict[str, Any]]) -> bool:
    return any(
        event.get("event_type") in ("run_completed", "task_completed") for event in events
    )


def _done_claim(events: List[Dict[str, Any]], task: Optional[Dict[str, Any]]) -> bool:
    return _completed_run(events) or (task or {}).get("status") in DONE_CLAIM_STATUSES


def _is_active(events: List[Dict[str, Any]], task: Optional[Dict[str, Any]]) -> bool:
    if run_is_terminal(events):
        return False
    if isinstance(task, dict):
        status = task.get("status")
        if status in TERMINAL_TASK_STATUSES:
            return False
    return True


def _event_time(event: Dict[str, Any]) -> float:
    try:
        return float(event.get("timestamp") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _last_event(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    return max(events, key=lambda event: (_event_time(event), event.get("sequence") or 0))


def _verification_passed(event: Dict[str, Any]) -> bool:
    return bool((event.get("verification") or {}).get("passed"))


def _trailing_verification_failures(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Consecutive failed verification events at the end of the ledger."""
    failures: List[Dict[str, Any]] = []
    for event in reversed(events):
        if event.get("event_type") != "verification_completed":
            continue
        if _verification_passed(event):
            break
        failures.append(event)
    failures.reverse()
    return failures


def _latest_verification(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for event in reversed(events):
        if event.get("event_type") == "verification_completed":
            return event
    return None


def _rework_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    reworks = []
    for event in events:
        if event.get("event_type") != "task_status_changed":
            continue
        metadata = event.get("metadata") or {}
        if event.get("status") == "rework" or metadata.get("to_status") == "rework":
            reworks.append(event)
    return reworks


def _action_signature(action: Dict[str, Any]) -> str:
    for key in _ACTION_KEYS:
        value = action.get(key)
        if isinstance(value, str) and value.strip():
            return _WHITESPACE.sub(" ", value.strip())[:200]
    return json.dumps(action, sort_keys=True, ensure_ascii=False, default=str)[:200]


def _is_failed_action(event: Dict[str, Any]) -> bool:
    action = event.get("action")
    if not isinstance(action, dict):
        return False
    if action.get("success") is False:
        return True
    if str(action.get("status") or "").lower() in ("failed", "error", "failure"):
        return True
    return str(event.get("event_type") or "").endswith("_failed")


def _trailing_action_failures(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Consecutive same-signature failing action events (interleaved facts skipped)."""
    failures: List[Dict[str, Any]] = []
    signature: Optional[str] = None
    for scanned, event in enumerate(reversed(events), start=1):
        if scanned > 100:
            break
        action = event.get("action")
        if not isinstance(action, dict):
            continue  # interleaved non-action facts do not invalidate the chain
        if not _is_failed_action(event):
            break  # a successful action ends the failure chain
        current = _action_signature(action)
        if signature is None:
            signature = current
        elif current != signature:
            break
        failures.append(event)
    failures.reverse()
    return failures


def _signature_hash(signature: str) -> str:
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]


def _normalize_log_line(line: str) -> str:
    cleaned = _WHITESPACE.sub(" ", line.strip().lower())
    cleaned = _DIGITS.sub("#", cleaned)
    return cleaned[:160]


def detect_log_repeat(
    log_tail: Optional[Dict[str, Any]],
    config: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Largest group of identical error-shaped log lines in the bounded tail."""
    if not isinstance(log_tail, dict):
        return None
    excerpt = str(log_tail.get("excerpt") or "")
    if not excerpt.strip():
        return None
    groups: Dict[str, List[int]] = {}
    for index, line in enumerate(excerpt.splitlines()):
        if not line.strip() or not _ERROR_MARKERS.search(line):
            continue
        groups.setdefault(_normalize_log_line(line), []).append(index)
    minimum = int(config.get("log_repeat_min", 3))
    signature, occurrences = max(
        ((key, value) for key, value in groups.items()),
        key=lambda item: len(item[1]),
        default=("", []),
    )
    if len(occurrences) < minimum:
        return None
    lines = excerpt.splitlines()
    sample = " | ".join(lines[index].strip() for index in occurrences[:3])
    return {
        "signature": signature,
        "occurrences": len(occurrences),
        "excerpt": sample[:300],
        "line_range": f"tail:{occurrences[0] + 1}-{occurrences[-1] + 1}",
    }


def question_for(signal: Signal) -> Dict[str, Any]:
    """Provider question (noul) for one candidate signal.

    The summary is redacted before it leaves the process: summaries can embed
    action commands or log lines, which the provider must never receive raw.
    """
    return {
        "instructions": (
            f"观察到的候选问题（{signal.finding_type}）：{redact_text(signal.summary)} "
            "请仅依据提供的事实与证据，判断该问题是否存在明确证据。"
        ),
        "criteria": "证据不足或证据不支持时给出低概率；不要猜测提供的事实之外的情况。",
    }


def _runtime_fact(runtime: Dict[str, Any]) -> Dict[str, Any]:
    fact = {"type": "runtime", "status": runtime.get("status")}
    for key in ("agent", "agent_name", "agent_session_id", "pane_id", "cwd"):
        if runtime.get(key):
            fact[key] = runtime[key]
    return fact


def _live_runtime_fact(live_runtime: Dict[str, Any]) -> Dict[str, Any]:
    fact = {"type": "runtime_live", "status": live_runtime.get("status")}
    for key in ("reason", "pane_id", "agent_status", "agent_session_id"):
        if live_runtime.get(key) is not None:
            fact[key] = live_runtime[key]
    if live_runtime.get("workspace_mismatch"):
        fact["workspace_mismatch"] = True
    return fact


def _detect_runtime_unavailable(
    events: List[Dict[str, Any]],
    task: Optional[Dict[str, Any]],
    runtime: Dict[str, Any],
    live_runtime: Optional[Dict[str, Any]],
) -> Optional[Signal]:
    persisted_unavailable = runtime.get("status") == "unavailable"
    live_unavailable = (live_runtime or {}).get("status") == "unavailable"
    if not (persisted_unavailable or live_unavailable):
        return None
    if not _is_active(events, task):
        return None
    last = _last_event(events) if events else {}
    evidence = []
    if persisted_unavailable:
        evidence.append(_runtime_fact(runtime))
    if isinstance(live_runtime, dict) and live_runtime:
        evidence.append(_live_runtime_fact(live_runtime))
    if last:
        evidence.append(event_ref(last))
    identity = (
        runtime.get("agent_session_id") or runtime.get("pane_id")
        or (live_runtime or {}).get("pane_id") or "runtime"
    )
    if persisted_unavailable and live_unavailable:
        source = "persisted+live"
    elif live_unavailable:
        source = "live"
    else:
        source = "persisted"
    return Signal(
        finding_type="runtime_unavailable",
        severity="critical",
        summary=(
            f"Runtime 不可用（来源：{source}，persisted.status={runtime.get('status') or 'unknown'}，"
            f"live.status={(live_runtime or {}).get('status') or 'unknown'}），Run 仍未终结；"
            "执行环境很可能已不可用。"
        ),
        suspected_cause="Pane 已消失或 Agent 会话不可用，而任务尚未进入终态。",
        recommended_action="request_human",
        confidence=0.9,
        evidence=evidence,
        anchor=f"runtime:{identity}",
        requires_confirmation=False,
        facts={
            "runtime_status": runtime.get("status"),
            "live_status": (live_runtime or {}).get("status"),
            "live_reason": (live_runtime or {}).get("reason"),
        },
    )


def _detect_verification_failure(
    events: List[Dict[str, Any]], task: Optional[Dict[str, Any]],
) -> Optional[Signal]:
    latest = _latest_verification(events)
    if latest is None or _verification_passed(latest):
        return None
    if not _done_claim(events, task):
        return None
    task_status = (task or {}).get("status")
    critical = _completed_run(events) or (
        task_status in DONE_CLAIM_STATUSES and task_status != "agent_done"
    )
    evidence = [verification_ref(latest)]
    for event in reversed(events):
        if event.get("event_type") in TERMINAL_EVENT_TYPES or (
            event.get("event_type") == "task_status_changed"
            and event.get("status") in DONE_CLAIM_STATUSES
        ):
            evidence.append(event_ref(event))
            break
    verification = latest.get("verification") or {}
    return Signal(
        finding_type="verification_failure",
        severity="critical" if critical else "warning",
        summary=(
            "最新一次验证未通过"
            f"（evidence_id={verification.get('evidence_id') or 'unknown'}，"
            f"failed={verification.get('failing_count', '?')}），"
            f"但 Run 已宣告完成或任务状态为 {task_status or 'terminal'}。"
        ),
        suspected_cause="完成判定与验证证据不一致：验证失败后没有新的通过记录。",
        recommended_action="request_human" if critical else "inspect",
        confidence=0.9,
        evidence=evidence,
        anchor=f"verification:{verification.get('evidence_id') or latest.get('event_id')}",
        requires_confirmation=False,
        facts={"failed_evidence_id": verification.get("evidence_id")},
    )


def _detect_repeated_failure(
    events: List[Dict[str, Any]], task: Optional[Dict[str, Any]], config: Dict[str, Any],
) -> Optional[Signal]:
    failures = _trailing_verification_failures(events)
    minimum = int(config.get("repeated_failure_min", 2))
    if len(failures) < minimum:
        return None
    if _done_claim(events, task):
        return None  # closure-time mismatches are verification_failure's job
    count = len(failures)
    critical = count >= 4
    return Signal(
        finding_type="repeated_failure",
        severity="critical" if critical else "warning",
        summary=(
            f"连续 {count} 次 verification_completed 未通过（passed=false），"
            f"最近一次 evidence_id={((failures[-1].get('verification') or {}).get('evidence_id')) or 'unknown'}。"
        ),
        suspected_cause="验证连续失败且没有产生通过记录，当前实现路径很可能无法收敛。",
        recommended_action="request_human" if critical else "replan",
        confidence=0.85,
        evidence=[verification_ref(event) for event in failures[-5:]],
        anchor=str(failures[0].get("event_id") or f"seq:{failures[0].get('sequence')}"),
        requires_confirmation=False,
        facts={"consecutive_failures": count},
    )


def _detect_repeated_action(events: List[Dict[str, Any]], config: Dict[str, Any]) -> Optional[Signal]:
    failures = _trailing_action_failures(events)
    if not failures:
        return None
    signature = _action_signature(failures[-1].get("action") or {})
    same = [event for event in failures if _action_signature(event.get("action") or {}) == signature]
    minimum = int(config.get("repeated_action_min", 3))
    if len(same) < minimum:
        return None
    return Signal(
        finding_type="repeated_action",
        severity="warning",
        summary=f"相同动作签名连续失败 {len(same)} 次：{signature[:120]}",
        suspected_cause="相同动作重复失败，通常意味着环境、权限或前置条件问题未被解决。",
        recommended_action="replan",
        confidence=0.8,
        evidence=[event_ref(event) for event in same[-5:]],
        anchor=f"action:{_signature_hash(signature)}:{same[0].get('event_id')}",
        requires_confirmation=False,
        facts={"consecutive_actions": len(same), "signature": signature[:120]},
    )


def _detect_no_progress(
    events: List[Dict[str, Any]], task: Optional[Dict[str, Any]],
    runtime: Dict[str, Any], now: float, config: Dict[str, Any],
) -> Optional[Signal]:
    if not _is_active(events, task) or runtime.get("status") == "unavailable":
        return None
    reworks = _rework_events(events)
    minimum = int(config.get("no_progress_min_reworks", 3))
    if len(reworks) < minimum:
        return None
    first = reworks[0]
    first_sequence = first.get("sequence") or 0
    for event in events:
        if (event.get("sequence") or 0) <= first_sequence:
            continue
        if (event.get("event_type") == "verification_completed"
                and _verification_passed(event)):
            return None  # a later passing check shows progress resumed
        if isinstance(event.get("artifact"), dict) and event.get("artifact"):
            return None  # new artifacts show progress even without verification
    last = _last_event(events)
    idle = max(0.0, now - _event_time(last))
    if idle >= float(config.get("stall_after_seconds", 1800)):
        return None  # stalls are the dominant diagnosis; avoid double reporting
    return Signal(
        finding_type="no_progress",
        severity="warning",
        summary=(
            f"任务已回流 rework {len(reworks)} 次，首次回流后既没有新的验证通过记录，"
            "也没有新的产物事件。"
        ),
        suspected_cause="反复回流但未产生可验证的进展，可能缺少明确的收敛路径。",
        recommended_action="inspect",
        confidence=0.6,
        evidence=[event_ref(first), event_ref(last)],
        anchor=f"first_rework:{first.get('event_id')}",
        requires_confirmation=True,
        facts={"rework_count": len(reworks), "idle_seconds": round(idle, 1)},
    )


def _detect_stalled(
    events: List[Dict[str, Any]], task: Optional[Dict[str, Any]],
    runtime: Dict[str, Any], now: float, config: Dict[str, Any],
) -> Optional[Signal]:
    if not events or not _is_active(events, task) or runtime.get("status") == "unavailable":
        return None
    last = _last_event(events)
    idle = max(0.0, now - _event_time(last))
    threshold = float(config.get("stall_after_seconds", 1800))
    if idle < threshold:
        return None
    failures = _trailing_verification_failures(events)
    critical = len(failures) >= 3
    node = (task or {}).get("node") or (task or {}).get("stage") or last.get("node") or "unknown"
    evidence = [event_ref(last)]
    if len(events) > 1:
        evidence.insert(0, event_ref(events[-2]))
    if runtime:
        evidence.append(_runtime_fact(runtime))
    return Signal(
        finding_type="stalled_execution",
        severity="critical" if critical else "warning",
        summary=(
            f"Agent 在 {node} 节点已连续 {round(idle / 60.0)} 分钟没有产生新的 trajectory 事件"
            f"（最近事件 #{last.get('sequence')} {last.get('event_type')}）。"
            + (f" 同时存在连续 {len(failures)} 次验证失败。" if critical else "")
        ),
        suspected_cause=(
            "长时间无新事件：可能在长时间思考、卡在交互确认，或已失去运行环境；"
            "需结合 Runtime 状态与日志确认。"
        ),
        recommended_action="request_human" if critical else "inspect",
        confidence=0.5,
        evidence=evidence,
        anchor=f"stall_after:{last.get('event_id')}",
        requires_confirmation=True,
        facts={
            "idle_seconds": round(idle, 1),
            "last_event_type": last.get("event_type"),
            "consecutive_failures": len(failures),
        },
    )


def _detect_context_problem(
    log_tail: Optional[Dict[str, Any]], config: Dict[str, Any],
) -> Optional[Signal]:
    repeated = detect_log_repeat(log_tail, config)
    if repeated is None:
        return None
    ref = str(log_tail.get("ref") or "")
    return Signal(
        finding_type="possible_context_problem",
        severity="warning",
        summary=(
            f"日志尾部同一错误签名重复出现 {repeated['occurrences']} 次"
            f"（{repeated['excerpt'][:80]}）；可能存在上下文/环境问题。"
        ),
        suspected_cause="同一错误反复出现，通常意味着缺失上下文、权限或环境依赖未满足。",
        recommended_action="inspect",
        confidence=0.55,
        evidence=[{
            "type": "log",
            "ref": ref,
            "signature": repeated["signature"][:120],
            "occurrences": repeated["occurrences"],
            "line_range": repeated["line_range"],
            "excerpt": repeated["excerpt"],
        }],
        anchor=f"log:{ref}:{_signature_hash(repeated['signature'])}",
        requires_confirmation=True,
        facts={"log_occurrences": repeated["occurrences"]},
    )


def detect_signals(
    *,
    run_id: str,
    events: List[Dict[str, Any]],
    task: Optional[Dict[str, Any]] = None,
    runtime: Optional[Dict[str, Any]] = None,
    live_runtime: Optional[Dict[str, Any]] = None,
    log_tail: Optional[Dict[str, Any]] = None,
    now: float,
    config: Dict[str, Any],
) -> List[Signal]:
    """Run every deterministic detector over one run's facts."""
    runtime = runtime or {}
    events = list(events or [])
    detectors = (
        _detect_runtime_unavailable(events, task, runtime, live_runtime),
        _detect_verification_failure(events, task),
        _detect_repeated_failure(events, task, config),
        _detect_repeated_action(events, config),
        _detect_no_progress(events, task, runtime, now, config),
        _detect_stalled(events, task, runtime, now, config),
        _detect_context_problem(log_tail, config),
    )
    signals: List[Signal] = []
    seen = set()
    for signal in detectors:
        if signal is None or signal.finding_type in seen:
            continue
        seen.add(signal.finding_type)
        signals.append(signal)
    return signals
