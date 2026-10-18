#!/usr/bin/env python3
"""Git delivery pipeline projection: commit -> integrate -> archive.

Pure core.  The console renders these dicts instead of re-deriving the
delivery stage from the raw task status — which is how a scheduler-driven
integration used to render as the terminal-looking badge "已提交" while a
competing manual "并入目标分支" button was offered next to it (issue #253).

Every field comes from an existing authority; nothing is duplicated here:

- ``task.status`` / ``integration_mode`` / ``commit`` / ``integration_branch``
  / ``candidate_sha``: the Task Store, written by ``herdr-task`` commit /
  integrate / finalize.
- ``workflow.status`` and the pinned node definition: the Workflow Store.
- gate verdicts: ``stage_verdict`` on the gate node's own tasks.
- ``candidate_published``: the append-only event ledger, supplied by the
  caller as ``published_at`` (this module performs no I/O).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

#: ``task.status`` -> ``(stage key, plain-Chinese label)``.  Ordered like
#: herdr/transitions.py so "the next step" always reads forward.
PIPELINE_STAGES: Tuple[Tuple[str, str, str], ...] = (
    ("completed", "awaiting_commit", "等待保存版本"),
    ("committed", "awaiting_integration", "等待集成"),
    ("integrated", "awaiting_finalize", "等待收尾"),
    ("cleanup_ready", "awaiting_archive", "等待归档"),
    ("cleaned", "archived", "已归档"),
)

#: Statuses whose whole git chain (commit -> integrate -> cleanup) the
#: Controller's registry watcher drives by itself through
#: ``_check_finalize_retry`` -> ``finalize_completed_task``.
SCHEDULER_DRIVEN_STATUSES = frozenset({"completed", "committed"})

#: ``stage_verdict`` values that settle a gate for its candidate.
PASSED_VERDICTS = frozenset({"pass", "approved"})

OWNER_SCHEDULER = "scheduler"
OWNER_HUMAN = "human"

_OWNER_NOTES = {
    "awaiting_commit": "由调度器自动保存版本，无需人工操作",
    "awaiting_integration": "由调度器自动集成，无需人工操作",
    "awaiting_finalize": "由调度器自动收尾，无需人工操作",
    "awaiting_archive": "由调度器自动归档，无需人工操作",
}
_ESCALATED_NOTE = "自动处理已停止，需要人工处理"
_HUMAN_NOTE = "由人工决定下一步"


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _timestamp_or_none(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _sha(task: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _text(task.get(key))
        if value:
            return value
    return ""


def stage_of(status: Any) -> Optional[Dict[str, str]]:
    """``{key, label}`` for a git-delivery status, or None outside the pipeline."""
    wanted = _text(status)
    for value, key, label in PIPELINE_STAGES:
        if value == wanted:
            return {"key": key, "label": label}
    return None


def scheduler_owns_next_step(task: Any, workflow: Any) -> bool:
    """True when the Controller — not a human — drives this task's next git step.

    Mirrors the registry watcher's own guards so the console and the scheduler
    agree on who is holding the task: a running workflow, a non-escalated git
    task, and a status the watcher still retries.  Missing or unknown workflow
    facts are not evidence of liveness, so they return False.
    """
    task = task if isinstance(task, dict) else {}
    workflow = workflow if isinstance(workflow, dict) else {}
    if _text(task.get("status")) not in SCHEDULER_DRIVEN_STATUSES:
        return False
    if _text(task.get("integration_mode") or "none") != "git":
        return False
    # ``herdr-task commit/integrate`` exit 2 without a git clone.
    if _text(task.get("execution_mode")) == "context":
        return False
    if task.get("finalize_escalated"):
        return False
    # ``workflow_closed()``: the watcher stops driving once the workflow is
    # completed and releases the task to the close/repair path.
    return _text(workflow.get("status")) == "running"


def _config_nodes(config: Any) -> List[Dict[str, Any]]:
    nodes = (config or {}).get("nodes") if isinstance(config, dict) else None
    return [n for n in nodes or [] if isinstance(n, dict) and _text(n.get("id"))]


def _node_of(task: Dict[str, Any]) -> str:
    return _text(task.get("node") or task.get("stage"))


def _node_tasks(tasks: Iterable[Any], node_id: str) -> List[Dict[str, Any]]:
    live = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        if task.get("status") == "superseded" or task.get("superseded_by"):
            continue
        if _node_of(task) == node_id:
            live.append(task)
    return live


def _gate_settled(live, gate_override) -> bool:
    if any(_text(t.get("stage_verdict")) in PASSED_VERDICTS for t in live):
        return True
    if not gate_override:
        return False
    from .workflow_graph import is_gate_override_valid
    return is_gate_override_valid(gate_override, live)


def pending_gate_nodes(config: Any, task: Any, tasks: Iterable[Any] = (),
                       workflow: Any = None) -> List[Dict[str, str]]:
    """Downstream gate nodes whose verdict this node's delivery still owes.

    Which node ids count as gates comes from ``workflow_progress`` (the single
    definition, explicit contract plus its default fallback) — never restated
    here.  A gate is settled by a pass verdict (or a still-valid manual
    override) on its own live tasks; anything else — including a gate that
    never ran — keeps the delivery waiting, and must stay visible as such.
    """
    from .workflow_progress import gate_retry_node
    task = task if isinstance(task, dict) else {}
    node_id = _node_of(task)
    if not node_id:
        return []
    overrides = (workflow or {}).get("gate_overrides") if isinstance(workflow, dict) else None
    overrides = overrides if isinstance(overrides, dict) else {}
    out = []
    for node in _config_nodes(config):
        gate_id = _text(node.get("id"))
        if node_id not in [_text(dep) for dep in (node.get("depends_on") or [])]:
            continue
        if not gate_retry_node(config, gate_id):
            continue
        live = _node_tasks(tasks, gate_id)
        if _gate_settled(live, overrides.get(gate_id)):
            continue
        out.append({"id": gate_id, "label": _text(node.get("label")) or gate_id})
    return out


def delivery_projection(task: Any, workflow: Any = None, tasks: Iterable[Any] = (),
                        config: Any = None, published_at: Any = None) -> Dict[str, Any]:
    """One dict describing where a task sits in the git delivery pipeline."""
    task = task if isinstance(task, dict) else {}
    workflow = workflow if isinstance(workflow, dict) else {}
    if config is None:
        config = workflow.get("config") if isinstance(workflow.get("config"), dict) else None
    status = _text(task.get("status"))
    stage = stage_of(status)
    owned = scheduler_owns_next_step(task, workflow)
    gates = pending_gate_nodes(config, task, tasks, workflow=workflow) if stage else []
    if gates:
        wait_kind = "gate"
    elif owned:
        wait_kind = "owner"
    else:
        wait_kind = ""
    return {
        "task_id": _text(task.get("task_id")),
        "node": _node_of(task),
        "status": status,
        "stage": stage["key"] if stage else status,
        "stage_label": stage["label"] if stage else "",
        "owner": OWNER_SCHEDULER if owned else OWNER_HUMAN,
        "scheduler_owned": owned,
        "owner_note": _owner_note(stage, owned, task),
        "wait_kind": wait_kind,
        "gates": gates,
        "commit": _sha(task, "commit"),
        "commit_short": _sha(task, "commit")[:8],
        "integration_mode": _text(task.get("integration_mode") or "none"),
        "integration_branch": _text(task.get("integration_branch")),
        "candidate_sha": _sha(task, "candidate_sha"),
        "candidate_short": _sha(task, "candidate_sha")[:8],
        "candidate_published_at": _timestamp_or_none(published_at),
    }


def _owner_note(stage: Optional[Dict[str, str]], owned: bool, task: Dict[str, Any]) -> str:
    if owned and stage:
        return _OWNER_NOTES.get(stage["key"], _HUMAN_NOTE)
    if task.get("finalize_escalated"):
        return _ESCALATED_NOTE
    return _HUMAN_NOTE
