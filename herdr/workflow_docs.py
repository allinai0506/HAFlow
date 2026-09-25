"""Workflow-scoped shared document area.

Code stays physically isolated in per-task CoW clones; documents and machine
evidence are shared per workflow through an append-only ledger stored outside
clones:

    ~/.herdr-controller/workflows/<workflow_id>/shared/notes.jsonl

The authority hierarchy, stated to every node in its prompt:

    git commits / verify-baseline > controller machine evidence > notes

Notes are append-only. Staleness is computed at read time: base-sha drift
voids machine-verifiable evidence kinds, and controller invalidation notes
void earlier notes of the affected nodes (fix-loop 改动即失效).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import time
from pathlib import Path

DOCS_DIR_ENV = "HERDR_WORKFLOW_DOCS_DIR"
DEFAULT_ROOT = Path.home() / ".herdr-controller" / "workflows"

NOTE_KINDS = (
    "requirement",
    "spec",
    "plan",
    "decision",
    "evidence",
    "gate",
    "delivery",
    "invalidation",
    "note",
    "wrapup",
)

SOURCE_AGENT = "agent"
SOURCE_CONTROLLER = "controller"
SOURCE_HUMAN = "human"
SOURCES = (SOURCE_AGENT, SOURCE_CONTROLLER, SOURCE_HUMAN)

EVIDENCE_KINDS = frozenset({"evidence", "gate", "delivery"})
CONTEXT_KINDS = frozenset(
    {"requirement", "spec", "plan", "decision", "invalidation", "wrapup"}
)

MAX_TITLE = 200
MAX_BODY = 20000
TRUNCATION_MARK = "\n…(truncated)"

WORKFLOW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")
SPACES_RE = re.compile(r"\s{2,}")


def _clean_text(value, limit=MAX_TITLE) -> str:
    """Single-line, control-char-free text for prompt rendering."""
    text = CONTROL_CHARS_RE.sub(" ", str(value or ""))
    text = SPACES_RE.sub(" ", text).strip()
    return text[:limit]


def _safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def docs_root() -> Path:
    override = os.environ.get(DOCS_DIR_ENV)
    return Path(override).expanduser() if override else DEFAULT_ROOT


def validate_workflow_id(workflow_id) -> str:
    text = str(workflow_id or "").strip()
    if not WORKFLOW_ID_RE.match(text):
        raise ValueError(f"invalid workflow id: {workflow_id!r}")
    return text


def workflow_docs_dir(workflow_id) -> Path:
    return docs_root() / validate_workflow_id(workflow_id) / "shared"


def notes_path(workflow_id) -> Path:
    return workflow_docs_dir(workflow_id) / "notes.jsonl"


def cli_path() -> Path:
    return Path(__file__).resolve().parent.parent / "bin" / "herdr-task"


def _clean_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    elif not isinstance(value, (list, tuple, set)):
        value = [value]
    result = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _read_notes_unlocked(path: Path) -> list:
    if not path.exists():
        return []
    notes = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(item, dict) and item.get("note_id"):
            notes.append(item)
    return notes


def load_notes(workflow_id) -> list:
    path = notes_path(workflow_id)
    if not path.exists():
        return []
    lock_path = path.with_name(f".{path.name}.lock")
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        try:
            return _read_notes_unlocked(path)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def append_note(
    workflow_id,
    *,
    kind,
    title,
    body="",
    node=None,
    task_id=None,
    agent=None,
    source=SOURCE_AGENT,
    base_sha=None,
    round=1,
    invalidates=None,
    fields=None,
    precondition=None,
) -> dict:
    wf = validate_workflow_id(workflow_id)

    kind_text = str(kind or "").strip()
    if kind_text not in NOTE_KINDS:
        raise ValueError(f"invalid note kind: {kind!r}")

    title_text = _clean_text(title)
    if not title_text:
        raise ValueError("note title must not be empty")

    source_text = str(source or SOURCE_AGENT).strip()
    if source_text not in SOURCES:
        raise ValueError(f"invalid note source: {source!r}")

    body_text = str(body or "")
    if len(body_text) > MAX_BODY:
        body_text = body_text[:MAX_BODY] + TRUNCATION_MARK

    invalidated = _clean_list(invalidates)
    if invalidated and kind_text != "invalidation":
        raise ValueError("invalidates only applies to invalidation notes")

    extra_fields = dict(fields or {})
    reserved_fields = {
        "note_id", "ts", "workflow_id", "kind", "title", "body",
        "node", "task_id", "agent", "source", "base_sha", "round",
        "invalidates", "stale", "stale_reason",
    }
    blocked_fields = reserved_fields & set(extra_fields)
    if blocked_fields:
        raise ValueError(f"fields cannot overwrite note metadata: {sorted(blocked_fields)}")
    for key, value in extra_fields.items():
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$", str(key)):
            raise ValueError(f"invalid note field name: {key!r}")

    try:
        round_number = max(1, int(round))
    except (TypeError, ValueError):
        round_number = 1

    ts = time.time()
    record = {
        "note_id": f"n-{int(ts * 1000)}-{os.urandom(2).hex()}",
        "ts": ts,
        "workflow_id": wf,
        "kind": kind_text,
        "title": title_text,
        "body": body_text,
        "node": str(node).strip() if node else "",
        "task_id": str(task_id).strip() if task_id else "",
        "agent": str(agent).strip() if agent else "",
        "source": source_text,
        "base_sha": str(base_sha).strip() if base_sha else "",
        "round": round_number,
    }
    if invalidated:
        record["invalidates"] = invalidated
    record.update(extra_fields)

    path = notes_path(wf)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if precondition is not None and not precondition(
                _read_notes_unlocked(path), record
            ):
                raise ValueError("workflow note precondition rejected append")
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return record


def annotate_notes(notes, current_base_sha=None) -> list:
    """Return copies annotated with scoped base and invalidation state.

    An invalidation with explicit candidate identities only affects those
    identities.  A node-scoped invalidation affects older evidence in that
    node, but never uses an equal base SHA as a blanket invalidation key.  The
    invalidation record itself is context, not an invalidation target.
    """
    notes = list(notes or [])
    invalidations = [
        note for note in notes
        if isinstance(note, dict) and note.get("kind") == "invalidation"
    ]

    def _field_values(note, names):
        values = set()
        for name in names:
            values.update(_clean_list(note.get(name)))
        return values

    candidate_names = (
        "delivery_id", "delivery_ids", "candidate_id", "candidate_ids",
        "candidate_sha", "candidate_shas",
    )
    invalidated_names = (
        "invalidated_candidates", "invalidates_candidates", "delivery_ids",
        "candidate_ids", "candidate_sha", "candidate_shas",
        "invalidated_candidate_shas", "invalidated_delivery_ids",
    )

    annotated = []
    for note in notes:
        if not isinstance(note, dict):
            continue
        item = dict(note)
        stale = False
        reason = ""
        kind = item.get("kind")
        note_base = str(item.get("base_sha") or "")
        if (
            kind in EVIDENCE_KINDS
            and note_base
            and current_base_sha
            and note_base != current_base_sha
        ):
            stale = True
            reason = "base sha changed"
        if not stale and kind != "invalidation":
            note_ts = _safe_float(item.get("ts"))
            note_node = str(item.get("node") or "")
            note_candidates = _field_values(item, candidate_names)
            for invalidation in invalidations:
                inv_wf = str(invalidation.get("workflow_id") or "")
                note_wf = str(item.get("workflow_id") or "")
                if inv_wf and note_wf and inv_wf != note_wf:
                    continue
                inv_ts = _safe_float(invalidation.get("ts"))
                invalidated_nodes = set(_clean_list(invalidation.get("invalidates")))
                invalidated_candidates = _field_values(invalidation, invalidated_names)
                if invalidated_candidates:
                    if note_candidates & invalidated_candidates:
                        stale = True
                        reason = "fix-loop candidate invalidation"
                        break
                    # An explicitly candidate-scoped invalidation must not
                    # degrade into a node-wide invalidation.
                    continue
                if (
                    invalidated_nodes
                    and note_ts < inv_ts
                    and note_node in invalidated_nodes
                ):
                    stale = True
                    reason = "fix-loop invalidation"
                    break
        item["stale"] = stale
        if stale:
            item["stale_reason"] = reason
        annotated.append(item)
    return annotated


def summarize_notes(notes, *, node_id=None, related_nodes=(), limit=12) -> list:
    related = set(_clean_list(related_nodes))
    scored = []
    for note in notes:
        score = 0
        note_node = str(note.get("node") or "")
        kind = note.get("kind")
        if node_id and note_node == node_id:
            score += 2
        if note_node and note_node in related:
            score += 1
        if kind in CONTEXT_KINDS:
            score += 1
        if score > 0:
            scored.append((score, _safe_float(note.get("ts")), note))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    try:
        cap = max(1, int(limit))
    except (TypeError, ValueError):
        cap = 12
    return [item[2] for item in scored[:cap]]


def _provenance_text(note) -> str:
    parts = []
    if note.get("task_id"):
        parts.append(f"task={_clean_text(note['task_id'], 80)}")
    if note.get("agent"):
        parts.append(f"agent={_clean_text(note['agent'], 40)}")
    source = note.get("source")
    if source and source != SOURCE_AGENT:
        parts.append(_clean_text(source, 20))
    if note.get("base_sha"):
        parts.append(f"base={_clean_text(note['base_sha'], 40)}")
    return " ".join(parts)


def render_context_block(
    workflow_id,
    notes,
    *,
    node_id=None,
    related_nodes=(),
    current_base_sha=None,
    limit=12,
) -> str:
    wf = validate_workflow_id(workflow_id)
    node_arg = shlex.quote(_clean_text(node_id, 80)) if node_id else "<本节点>"
    selected = summarize_notes(
        annotate_notes(notes, current_base_sha=current_base_sha),
        node_id=node_id,
        related_nodes=related_nodes,
        limit=limit,
    )

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "Workflow 共享文档区（跨阶段上下文；代码物理隔离，文档受控共享）",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"目录: {workflow_docs_dir(wf)}",
        "权威层级: git commits / verify-baseline > controller 机器证据 > "
        "本区文档（仅供上下文，不得当作事实或跳过验证）",
        "追加写入: "
        f"{cli_path()} note-add --workflow-id {wf} --node {node_arg} "
        '--kind spec --title "..." --text "..."',
    ]

    if not selected:
        lines.append("已有条目: （本工作流暂无共享文档条目）")
        return "\n".join(lines)

    lines.append("已有条目（按相关度排序）:")
    if current_base_sha:
        lines.append(f"当前 base: {current_base_sha}")
    for note in selected:
        marker = ""
        if note.get("stale"):
            marker = f"[STALE: {_clean_text(note.get('stale_reason'), 60) or 'stale'}] "
        kind = _clean_text(note.get("kind"), 20) or "note"
        node = _clean_text(note.get("node"), 80) or "-"
        title = _clean_text(note.get("title")) or "(无标题)"
        provenance = _provenance_text(note)
        suffix = f" — {provenance}" if provenance else ""
        lines.append(f"- {marker}[{kind}][{node}] {title}{suffix}")
    return "\n".join(lines)


def provenance_from_task(task) -> dict:
    task = task if isinstance(task, dict) else {}
    return {
        "node": task.get("node") or task.get("stage") or "",
        "task_id": task.get("task_id") or "",
        "agent": task.get("agent") or "",
        "base_sha": task.get("base_sha") or "",
    }
