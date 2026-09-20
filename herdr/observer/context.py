#!/usr/bin/env python3
"""Bounded ObservationContext (herdr/observer/context.py).

Functional Core: assemble the one bounded snapshot the Observer judgment needs
from the Trajectory Ledger plus bounded log evidence. Nothing raw or unbounded
ever reaches a provider or a finding:

- the trajectory window is capped (recent N + verification + terminal rows);
- the log is read as a bounded tail (bytes -> lines -> chars);
- every string is truncated and credential-redacted (reusing the supervisor's
  redact_text);
- the serialized snapshot must fit ``max_context_size`` or it is shrunk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..projection import strip_ansi_codes
from ..supervisor.state import redact_text

MAX_EVENT_NOTE_CHARS = 120
MAX_TERMINAL_EVENTS = 6
MAX_ARTIFACTS = 10
MAX_SIGNAL_REFS = 4
DEFAULT_LOG_ROOT = os.path.expanduser("~/.herdr-controller/logs/tasks")


def _bounded(value: Any, limit: int) -> str:
    text = redact_text(str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _bounded_log_excerpt(
    log_tail: Optional[Dict[str, Any]], max_chars: int,
) -> Optional[Dict[str, Any]]:
    if not isinstance(log_tail, dict):
        return None
    excerpt = str(log_tail.get("excerpt") or "")
    if not excerpt.strip():
        return None
    bounded = {
        "ref": str(log_tail.get("ref") or log_tail.get("path") or ""),
        "excerpt": excerpt[-max(100, int(max_chars)):],
        "truncated": bool(log_tail.get("truncated")),
    }
    if log_tail.get("line_range"):
        bounded["line_range"] = str(log_tail["line_range"])[:40]
    return bounded


def bound_transcript(
    text: Any, *, ref: str, config: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Bound + redact transcript text (bytes -> lines -> chars).

    Shared by the file-tail reader and the live pane reader so both obey the
    same limits. Returns None when nothing readable remains.
    """
    raw = str(text or "")
    if not raw.strip():
        return None
    max_bytes = int(config.get("log_tail_bytes", 16384))
    max_lines = int(config.get("log_tail_lines", 200))
    max_chars = int(config.get("log_tail_chars", 4000))
    size_bytes = len(raw.encode("utf-8", "replace"))
    encoded = raw.encode("utf-8", "replace")
    truncated = size_bytes > max_bytes
    if truncated:
        encoded = encoded[-max_bytes:]
    decoded = redact_text(strip_ansi_codes(encoded.decode("utf-8", "ignore")))
    lines = decoded.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True
    tail = "\n".join(lines)
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
        truncated = True
    excerpt = tail.strip()
    if not excerpt:
        return None
    return {
        "ref": str(ref),
        "size_bytes": size_bytes,
        "truncated": truncated,
        "excerpt": excerpt,
    }


def read_log_tail(task: Optional[Dict[str, Any]], config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Read a bounded tail of the task's existing agent log evidence.

    Prefers the task's existing evidence path; falls back to the controller's
    standard per-task terminal transcript. Returns None when no readable log
    exists; never raises.
    """
    if not isinstance(task, dict):
        return None
    candidates: List[Path] = []
    evidence = task.get("evidence")
    if evidence:
        candidates.append(Path(str(evidence)))
    task_id = task.get("task_id")
    if task_id:
        candidates.append(Path(DEFAULT_LOG_ROOT) / str(task_id) / "terminal.log")

    max_bytes = int(config.get("log_tail_bytes", 16384))
    for path in candidates:
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
            with path.open("rb") as handle:
                if size > max_bytes:
                    handle.seek(-max_bytes, os.SEEK_END)
                raw = handle.read()
            bounded = bound_transcript(
                raw.decode("utf-8", "replace"), ref=str(path), config=config,
            )
            if bounded is None:
                continue
            bounded["path"] = str(path)
            if size > max_bytes:
                bounded["truncated"] = True
            return bounded
        except OSError:
            continue
    return None


def _summarize_event(event: Dict[str, Any], now: float) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "event_id": event.get("event_id"),
        "sequence": event.get("sequence"),
        "event_type": event.get("event_type"),
    }
    timestamp = event.get("timestamp")
    try:
        row["ago_seconds"] = max(0, round(now - float(timestamp or 0.0), 1))
    except (TypeError, ValueError):
        pass
    if event.get("status"):
        row["status"] = _bounded(event.get("status"), 40)
    verification = event.get("verification")
    if isinstance(verification, dict):
        row["passed"] = bool(verification.get("passed"))
        if verification.get("evidence_id"):
            row["evidence_id"] = _bounded(verification.get("evidence_id"), 60)
    metadata = event.get("metadata") or {}
    note = metadata.get("reason") or metadata.get("note")
    if note:
        row["note"] = _bounded(note, MAX_EVENT_NOTE_CHARS)
    action = event.get("action")
    if isinstance(action, dict):
        signature = action.get("command") or action.get("cmd") or action.get("name")
        if signature:
            row["action"] = _bounded(signature, MAX_EVENT_NOTE_CHARS)
    return {key: value for key, value in row.items() if value is not None}


def _terminal_events(events: List[Dict[str, Any]], now: float) -> List[Dict[str, Any]]:
    picked: List[Dict[str, Any]] = []
    for event in events:
        if event.get("event_type") == "run_started":
            picked.append(event)
            break
    for event in events:
        if event.get("event_type") in (
            "run_completed", "run_failed", "task_completed", "task_failed",
        ):
            picked.append(event)
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for event in picked:
        key = event.get("event_id") or id(event)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)
    return [_summarize_event(event, now) for event in deduped[-MAX_TERMINAL_EVENTS:]]


def _artifact_refs(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    for event in events:
        artifact = event.get("artifact")
        if not isinstance(artifact, dict) or not artifact:
            continue
        row = {
            "event_id": event.get("event_id"),
            "sequence": event.get("sequence"),
        }
        if artifact.get("ref") or artifact.get("path"):
            row["ref"] = _bounded(artifact.get("ref") or artifact.get("path"), 160)
        if artifact.get("kind"):
            row["kind"] = _bounded(artifact.get("kind"), 40)
        refs.append(row)
    return refs[-MAX_ARTIFACTS:]


def _signal_summary(signal) -> Dict[str, Any]:
    refs: List[Any] = []
    for item in signal.evidence:
        value = item.get("event_id") or item.get("evidence_id") or item.get("ref")
        if value:
            refs.append(value)
    return {
        "finding_type": signal.finding_type,
        "severity": signal.severity,
        "requires_confirmation": signal.requires_confirmation,
        "summary": _bounded(signal.summary, 300),
        "facts": {
            key: (_bounded(value, 120) if isinstance(value, str) else value)
            for key, value in list(signal.facts.items())[:8]
        },
        "evidence_refs": refs[:MAX_SIGNAL_REFS],
    }


def _runtime_facts(runtime: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: _bounded(runtime[key], 160)
        for key in ("agent", "agent_name", "agent_session_id", "workspace_id",
                    "tab_id", "pane_id", "cwd", "status")
        if runtime.get(key)
    }


def _live_runtime_facts(live_runtime: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(live_runtime, dict) or not live_runtime:
        return {"status": "unknown", "reason": "not_probed"}
    return {
        key: _bounded(live_runtime[key], 160)
        for key in ("status", "reason", "pane_id", "agent_status", "agent_session_id")
        if live_runtime.get(key) is not None
    } | ({"workspace_mismatch": True} if live_runtime.get("workspace_mismatch") else {})


def build_observation_context(
    *,
    run_id: str,
    task: Optional[Dict[str, Any]],
    events: List[Dict[str, Any]],
    runtime: Optional[Dict[str, Any]],
    log_tail: Optional[Dict[str, Any]],
    signals: List[Any],
    config: Dict[str, Any],
    now: float,
    live_runtime: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the bounded provider state for one observation."""
    task = task if isinstance(task, dict) else {}
    runtime = runtime if isinstance(runtime, dict) else {}
    events = list(events or [])
    last = events[-1] if events else {}
    first = events[0] if events else {}

    recent_limit = max(1, int(config.get("recent_events", 50)))
    verification_limit = max(1, int(config.get("verification_events", 5)))
    recent = events[-recent_limit:]
    verification_rows = [
        _summarize_event(event, now)
        for event in events
        if event.get("event_type") == "verification_completed"
    ][-verification_limit:]

    context: Dict[str, Any] = {
        "schema": "observation_context/v1",
        "run": {
            "run_id": _bounded(run_id, 128),
            "task_id": _bounded(task.get("task_id") or last.get("task_id"), 128),
            "workflow_id": _bounded(task.get("workflow_id") or last.get("workflow_id"), 128),
            "node": _bounded(task.get("node") or task.get("stage") or last.get("node"), 128),
            "agent": _bounded(task.get("agent") or last.get("agent"), 128),
            "agent_name": _bounded(
                runtime.get("agent_name") or last.get("agent_name"), 128,
            ),
            "agent_session_id": _bounded(
                runtime.get("agent_session_id") or last.get("agent_session_id"), 128,
            ),
            "task_status": _bounded(task.get("status") or last.get("status"), 40),
            "runtime_status": _bounded(runtime.get("status"), 40),
        },
        "runtime": {
            "persisted": _runtime_facts(runtime),
            "live": _live_runtime_facts(live_runtime),
        },
        "window": {
            "total_events": len(events),
            "recent_returned": len(recent),
            "first_sequence": first.get("sequence"),
            "last_sequence": last.get("sequence"),
            "truncated": len(events) > len(recent),
        },
        "recent_events": [_summarize_event(event, now) for event in recent],
        "verification": verification_rows,
        "terminal_events": _terminal_events(events, now),
        "artifacts": _artifact_refs(events),
        "logs": [
            row for row in [
                _bounded_log_excerpt(log_tail, int(config.get("log_tail_chars", 4000)))
            ] if row
        ],
        "signals": [_signal_summary(signal) for signal in signals],
    }
    if events:
        try:
            started = float(first.get("timestamp") or now)
            context["elapsed_seconds"] = max(0.0, round(now - started, 1))
        except (TypeError, ValueError):
            pass
    return _fit_budget(context, int(config.get("max_context_size", 8000)), run_id)


def _size(context: Dict[str, Any]) -> int:
    return len(json.dumps(context, ensure_ascii=False))


def _clamp_strings(node: Any, cap: int) -> Any:
    """Recursively clamp every string in a nested structure to ``cap`` chars."""
    if isinstance(node, str):
        return node[:cap]
    if isinstance(node, dict):
        return {key: _clamp_strings(value, cap) for key, value in node.items()}
    if isinstance(node, list):
        return [_clamp_strings(value, cap) for value in node]
    return node


def _fit_budget(
    context: Dict[str, Any], max_context_size: int, run_id: str = "",
) -> Dict[str, Any]:
    """Shrink the snapshot until its serialized form fits the byte budget.

    Hard guarantee: the returned context never serializes above
    ``max(500, max_context_size)``. Identity (run_id + signal types/refs) is
    preserved as long as possible; the last resort keeps run_id and signal
    types only.
    """
    budget = max(500, int(max_context_size))
    if _size(context) <= budget:
        return context
    # Drop order: bulk first, identity + signals last.
    for key in ("logs", "artifacts", "verification", "terminal_events"):
        context.pop(key, None)
        if _size(context) <= budget:
            return context
    window = context.get("recent_events") or []
    while window and _size(context) > budget:
        window = window[1:]
        context["recent_events"] = window
        if isinstance(context.get("window"), dict):
            context["window"]["recent_returned"] = len(window)
    if _size(context) <= budget:
        return context
    signals = context.get("signals") or []
    while len(signals) > 1 and _size(context) > budget:
        signals = signals[:-1]
        context["signals"] = signals
    if _size(context) <= budget:
        return context
    # Recursive string clamp, halving the cap until it fits.
    cap = 256
    while _size(context) > budget and cap >= 4:
        context = _clamp_strings(context, cap)
        if _size(context) <= budget:
            return context
        cap //= 2
    # Last resort: minimal identity (run_id + signal types), still clamped.
    minimal: Dict[str, Any] = {
        "schema": "observation_context/v1",
        "run": {"run_id": _bounded(run_id, 128)},
        "signals": [
            {"finding_type": str(signal.get("finding_type") or "")[:32]}
            for signal in signals
            if isinstance(signal, dict)
        ],
    }
    while minimal["signals"] and _size(minimal) > budget:
        minimal["signals"] = minimal["signals"][:-1]
    return _clamp_strings(minimal, max(8, budget // 4))


__all__ = [
    "DEFAULT_LOG_ROOT",
    "bound_transcript",
    "build_observation_context",
    "read_log_tail",
]
