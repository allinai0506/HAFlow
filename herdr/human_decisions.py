"""Human decision intake (herdr/human_decisions.py).

Functional Core: folds the append-only ``workflow_docs`` ledger into
    1. the open "waiting on a human ruling" list, and
    2. the coordinator's latest advice timeline.

No I/O, no clock reads, no mutation of the input list.

Decision contract (flat, single-line ``note.fields`` entries so the same
record can be written by ``bin/herdr-task note-add --field`` and by the
console HTTP API):

    decision_id      required; without it a ``kind=decision`` note is a
                     *record* of a decision already taken, not a new ask
    decision_status  "open" (default) | "resolved"
    question         what the human must rule on
    options          JSON list of candidate answers
    recommended      which option the coordinator suggests
    decision         the answer once resolved

Resolution is append-only: a newer note with the same ``decision_id`` wins,
so a resolved ask disappears from the list and can be reopened by a later
note.  A ``stale`` (fix-loop invalidated) ask is not a live ask.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

from . import workflow_docs

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"
DECISION_STATUSES = (STATUS_OPEN, STATUS_RESOLVED)

#: Which note kinds can carry coordinator advice worth showing verbatim.
#: Aliased from the authoritative ledger: a second hand-maintained copy would
#: silently hide a newly added note kind from the console's advice timeline.
ADVICE_KINDS = tuple(workflow_docs.NOTE_KINDS)

#: Whitespace-collapsed body excerpt length rendered in the console.
ADVICE_SUMMARY_LIMIT = 320

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")
_SPACES_RE = re.compile(r"\s+")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False)
    return _SPACES_RE.sub(" ", _CONTROL_CHARS_RE.sub(" ", str(value))).strip()


def _field(note: Mapping[str, Any], name: str, default: str = "") -> str:
    value = note.get(name)
    text = _text(value)
    return text or default


def _epoch(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _options(raw: Any) -> List[str]:
    """Accept a real list or the JSON string the flat note field must carry."""
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        text = _text(raw)
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return []
        items = parsed if isinstance(parsed, (list, tuple)) else []
    out: List[str] = []
    for item in items:
        text = _text(item)
        if text and text not in out:
            out.append(text)
    return out


def _excerpt(body: Any, limit: int = ADVICE_SUMMARY_LIMIT) -> str:
    text = _text(body)
    return text[:limit]


def _as_note(item: Any) -> Optional[Dict[str, Any]]:
    return dict(item) if isinstance(item, Mapping) else None


def collect_open_decisions(
    notes: Iterable[Mapping[str, Any]],
    *,
    limit: int = 30,
    workflow_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Newest note per ``decision_id`` wins; resolved or stale asks drop out."""
    latest: Dict[str, Dict[str, Any]] = {}
    for raw in notes or []:
        note = _as_note(raw)
        if note is None:
            continue
        decision_id = _field(note, "decision_id")
        if not decision_id:
            continue
        if workflow_id and _field(note, "workflow_id") and \
                _field(note, "workflow_id") != workflow_id:
            continue
        previous = latest.get(decision_id)
        if previous is None or _epoch(note.get("ts")) >= _epoch(previous.get("ts")):
            latest[decision_id] = note

    items: List[Dict[str, Any]] = []
    for decision_id, note in latest.items():
        if note.get("stale"):
            continue
        status = _field(note, "decision_status", STATUS_OPEN)
        if status not in DECISION_STATUSES:
            status = STATUS_OPEN
        if status == STATUS_RESOLVED:
            continue
        question = _field(note, "question") or _excerpt(note.get("body"))
        items.append({
            "decision_id": decision_id,
            "status": status,
            "title": _field(note, "title"),
            "question": question,
            "options": _options(note.get("options")),
            "recommended": _field(note, "recommended"),
            "workflow_id": _field(note, "workflow_id"),
            "node": _field(note, "node"),
            "task_id": _field(note, "task_id"),
            "agent": _field(note, "agent"),
            "source": _field(note, "source"),
            "note_id": _field(note, "note_id"),
            "raised_at": _epoch(note.get("ts")),
            "detail": question,
        })

    items.sort(key=lambda item: -item["raised_at"])
    try:
        cap = max(1, int(limit))
    except (TypeError, ValueError):
        cap = 30
    return items[:cap]


def collect_advice(
    notes: Iterable[Mapping[str, Any]],
    *,
    limit: int = 12,
    workflow_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Newest-first coordinator advice, newest wins per note_id (append-only)."""
    seen = set()
    items: List[Dict[str, Any]] = []
    for raw in notes or []:
        note = _as_note(raw)
        if note is None:
            continue
        kind = _field(note, "kind")
        if kind not in ADVICE_KINDS:
            continue
        if workflow_id and _field(note, "workflow_id") and \
                _field(note, "workflow_id") != workflow_id:
            continue
        note_id = _field(note, "note_id")
        identity = note_id or f"{kind}:{_field(note, 'ts')}:{_field(note, 'title')}"
        if identity in seen:
            continue
        seen.add(identity)
        items.append({
            "note_id": note_id,
            "kind": kind,
            "title": _field(note, "title"),
            "summary": _excerpt(note.get("body")),
            "node": _field(note, "node"),
            "task_id": _field(note, "task_id"),
            "agent": _field(note, "agent"),
            "source": _field(note, "source"),
            "workflow_id": _field(note, "workflow_id"),
            "raised_at": _epoch(note.get("ts")),
            "stale": bool(note.get("stale")),
            "is_decision": bool(_field(note, "decision_id")),
        })
    items.sort(key=lambda item: -item["raised_at"])
    try:
        cap = max(1, int(limit))
    except (TypeError, ValueError):
        cap = 12
    return items[:cap]


def build_decision_fields(
    decision_id: str,
    status: str,
    *,
    question: str = "",
    options: Optional[Iterable[str]] = None,
    recommended: str = "",
    decision: str = "",
) -> Dict[str, str]:
    """Build the flat note ``fields`` payload shared by CLI and console writers."""
    decision_id = _text(decision_id)
    if not decision_id:
        raise ValueError("decision_id must not be empty")
    status_text = _text(status) or STATUS_OPEN
    if status_text not in DECISION_STATUSES:
        raise ValueError(f"invalid decision_status: {status!r}")

    fields = {
        "decision_id": decision_id,
        "decision_status": status_text,
    }
    if question:
        fields["question"] = _text(question)
    choices = _options(list(options or []))
    if choices:
        fields["options"] = json.dumps(choices, ensure_ascii=False)
    if recommended:
        fields["recommended"] = _text(recommended)
    if decision:
        fields["decision"] = _text(decision)
    return fields
