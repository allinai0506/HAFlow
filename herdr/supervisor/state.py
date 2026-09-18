#!/usr/bin/env python3
"""SupervisorState - bounded context snapshot (herdr/supervisor/state.py).

Functional Core: builds the one-snapshot context a supervision judgment
needs from deterministic task/runtime facts plus event summaries.

Guarantees:
- bounded: strings truncated, lists capped, serialized size <= max_context_size;
- redacted: credential-shaped values never leave the fact layer;
- summarized: recent events become {type, ago_seconds} rows, not raw logs.

Raw stdout, full diffs, full files and env dumps are deliberately excluded.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

MAX_GOAL_CHARS = 600
MAX_SUMMARY_CHARS = 400
MAX_EVENT_ROWS = 15
MAX_RECENT_ROW_CHARS = 120
MAX_FACT_DICT_KEYS = 12
MAX_CRITERIA_ITEMS = 6

# Credential-shaped content: cloud keys, provider keys, bearer tokens,
# inline key=value assignments. Patterns are conservative; a match means the
# whole value is replaced by a redaction marker.
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd|authorization)\b\s*[:=]\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_\-\*]{8,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bbearer\s+[A-Za-z0-9\-._~\+\/]{8,}", re.IGNORECASE),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
)

_PLACEHOLDER = "[redacted]"


def redact_text(value: str) -> str:
    text = value
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_PLACEHOLDER, text)
    return text


def _truncate(text: Optional[str], limit: int) -> Optional[str]:
    if text is None:
        return None
    cleaned = redact_text(str(text)).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)] + "…"


def _summarize_events(events: List[dict], now: float, limit: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for event in sorted(events, key=lambda e: float(e.get("timestamp") or 0.0), reverse=True)[:limit]:
        if not isinstance(event, dict):
            continue
        timestamp = float(event.get("timestamp") or now)
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        note = payload.get("reason") or payload.get("note") or ""
        row = {
            "type": str(event.get("event_type") or "unknown")[:60],
            "ago_seconds": max(0, round(now - timestamp, 1)),
            "source": str(event.get("source") or "system")[:30],
        }
        if note:
            row["note"] = _truncate(str(note), MAX_RECENT_ROW_CHARS)
        rows.append(row)
    return rows


def _previous_signals(previous: Optional[dict]) -> Optional[Dict[str, float]]:
    if not isinstance(previous, dict):
        return None
    signals = previous.get("signals")
    if not isinstance(signals, dict):
        return None
    return {
        str(name): float(value)
        for name, value in signals.items()
        if isinstance(value, (int, float))
    }


def build_supervisor_state(
    task: dict,
    *,
    now: float,
    events: Optional[List[dict]] = None,
    facts: Optional[dict] = None,
    previous_evaluation: Optional[dict] = None,
    max_context_size: int = 8000,
    recent_events_limit: int = MAX_EVENT_ROWS,
) -> Dict[str, Any]:
    """Assemble the bounded judgment snapshot from a task record + facts.

    ``facts`` carries what only the caller's live environment knows:
    test summary, git diff summary, verification/retry counts, attempt count,
    elapsed seconds. Everything is optional; absent facts stay absent rather
    than being guessed.
    """
    facts = facts or {}
    runtime = task.get("runtime") if isinstance(task.get("runtime"), dict) else {}

    state: Dict[str, Any] = {
        "task_id": _truncate(task.get("task_id"), MAX_RECENT_ROW_CHARS),
        "workflow_id": _truncate(task.get("workflow_id"), MAX_RECENT_ROW_CHARS),
        "goal": _truncate(task.get("goal"), MAX_GOAL_CHARS),
        "blocker": _truncate(task.get("blocker"), MAX_SUMMARY_CHARS),
        "node": _truncate(task.get("node") or task.get("stage"), MAX_RECENT_ROW_CHARS),
        "task_status": _truncate(task.get("status"), 40),
        "agent": _truncate(runtime.get("agent") or task.get("agent"), MAX_RECENT_ROW_CHARS),
        "agent_name": _truncate(runtime.get("agent_name"), MAX_RECENT_ROW_CHARS),
        "runtime_status": _truncate(runtime.get("status"), 40),
        "attempt_count": facts.get("attempt_count"),
        "elapsed_seconds": facts.get("elapsed_seconds"),
        "verification_count": facts.get("verification_count"),
        "stage_verdict": _truncate(task.get("stage_verdict"), MAX_RECENT_ROW_CHARS),
    }

    criteria = task.get("acceptance_criteria")
    if isinstance(criteria, str) and criteria.strip():
        state["acceptance_criteria"] = [_truncate(criteria, MAX_SUMMARY_CHARS)]
    elif isinstance(criteria, list):
        rows = [
            _truncate(str(item), MAX_RECENT_ROW_CHARS)
            for item in criteria[:MAX_CRITERIA_ITEMS]
            if str(item).strip()
        ]
        if rows:
            state["acceptance_criteria"] = rows

    tests = facts.get("tests")
    if isinstance(tests, dict):
        state["tests"] = {
            str(key): (
                _truncate(str(value), MAX_RECENT_ROW_CHARS)
                if isinstance(value, str)
                else value
            )
            for key, value in list(tests.items())[:MAX_FACT_DICT_KEYS]
        }
    diff_summary = facts.get("diff_summary")
    if isinstance(diff_summary, dict):
        state["diff_summary"] = {
            str(key): (
                _truncate(str(value), MAX_RECENT_ROW_CHARS)
                if isinstance(value, str)
                else value
            )
            for key, value in list(diff_summary.items())[:MAX_FACT_DICT_KEYS]
        }
    output_summary = facts.get("output_summary")
    if output_summary:
        state["recent_output_summary"] = _truncate(str(output_summary), MAX_SUMMARY_CHARS)

    state["recent_events"] = _summarize_events(
        events or [], now, min(recent_events_limit, MAX_EVENT_ROWS)
    )
    previous = _previous_signals(previous_evaluation)
    if previous:
        state["previous_signals"] = previous

    return _fit_budget(state, max_context_size)


def _fit_budget(state: Dict[str, Any], max_context_size: int) -> Dict[str, Any]:
    """Shrink the snapshot until its serialized form fits the byte budget."""
    budget = max(500, int(max_context_size))
    if _size(state) <= budget:
        return state
    # Drop order: history first, then summaries; identity + goal survive.
    for key in ("recent_events", "previous_signals", "diff_summary", "tests",
                "recent_output_summary", "blocker", "acceptance_criteria"):
        state.pop(key, None)
        if _size(state) <= budget:
            return state
    # Last resort: hard-clamp every remaining string so the budget is absolute.
    for _ in range(10):
        if _size(state) <= budget:
            break
        for key, value in list(state.items()):
            if isinstance(value, str) and len(value) > 8:
                state[key] = value[: max(8, len(value) // 2)]
    return state


def _size(state: Dict[str, Any]) -> int:
    return len(json.dumps(state, ensure_ascii=False))
