"""One automatic redo for a node that is not a code gate.

A settled blocked result goes back to the same node once. The next blocked
result stays blocked and can advance: the verdict is not rewritten to pass.
Code gates stay on the existing fix loop. In-flight work is left alone.

REDO_LIMIT is one round per node. Only a blocked task this execution
superseded for ``same_node_redo`` spends it. Another reason, or another
execution, does not. A failed task is not settled and is not stamped
``accepted_with_limits``. Raising the limit needs a real round counter; a
task count would treat the executor and the challenger as two rounds.
"""

import re

from herdr.transitions import COMPLETED_TASK_STATUSES

REDO_LIMIT = 1
NOTE_LIMIT = 2000
REDO_INSTRUCTION = (
    "请处理这些阻断。修不完、也不值得停下后续节点的，"
    "写成已知限制，不要为了百分之百再扩一轮。"
)

_SETTLED = COMPLETED_TASK_STATUSES
_CODE_GATE_IDS = frozenset({
    "test", "review", "wrapup", "gate",
    "cleanroom", "cleanroom_test", "cleanroom_review", "cleanroom_gate",
})
_GATE_NAME = re.compile(r"(^|[_-])(gate|cleanroom)($|[_-])")


def _declared_gate(value):
    """Return a real gate declaration. An empty object is not one.

    ``normalize_workflow`` writes ``gate: {}`` on every node. That placeholder
    must not turn requirements or plan into a code gate.
    """
    if isinstance(value, dict) and not value:
        return None
    return value


def node_uses_gate_loop(node, node_id=None):
    """True when a blocked result stays on the gate fix loop.

    ``normalize_workflow`` still writes ``gate: {}`` on nodes that never
    declared one. That placeholder is not a gate. ``gate_declared`` marks an
    author who wrote ``gate``, including an explicit empty object.
    """
    node = node or {}
    if node.get("gate") is False:
        return False
    if node.get("gate_declared") or node.get("node_type") == "gate":
        return True
    gate = _declared_gate(node.get("gate"))
    if gate is not None:
        return True
    text = str(node_id if node_id is not None else node.get("id") or "").lower()
    return text in _CODE_GATE_IDS or bool(_GATE_NAME.search(text))


def _is_code_gate(node):
    return node_uses_gate_loop(node)


def _live(task):
    return task.get("status") != "superseded" and not task.get("superseded_by")


def _blocked(task):
    return task.get("stage_verdict") == "blocked"


def _redo_marker(task):
    if task.get("same_node_redo_note"):
        return True
    for entry in task.get("status_history") or []:
        if isinstance(entry, dict) and entry.get("reason") == "same_node_redo":
            return True
    return False


def _same_execution(task, live_tasks):
    current = {
        item.get("execution_id")
        for item in live_tasks
        if item.get("execution_id")
    }
    if not current:
        return True
    return task.get("execution_id") in current


def _spent(tasks, live_tasks):
    """1 after this execution already spent its automatic same-node redo."""
    return 1 if any(
        (not _live(task))
        and _blocked(task)
        and _redo_marker(task)
        and _same_execution(task, live_tasks)
        for task in tasks
    ) else 0


def _note(blocked_tasks):
    lines = [REDO_INSTRUCTION]
    for task in sorted(blocked_tasks, key=lambda item: str(item.get("task_id") or "")):
        text = str(task.get("stage_verdict_note") or task.get("blocker") or "").strip()
        if text:
            lines.append(f"{task.get('task_id')}: {text}")
    note = "\n".join(lines)
    if len(note) <= NOTE_LIMIT:
        return note
    return note[: NOTE_LIMIT - 1] + "…"


def acceptance_actions(config, tasks, workflow_id, gate_ids=()):
    """Return rework or accept writes. Empty means the sweep should not write.

    Each item is ``{op, node_id, tasks, note}``. ``rework`` lists every live
    task on the node. ``accept`` lists only the live blocked tasks that do
    not already carry ``accepted_with_limits``.
    """
    gates = {str(node_id) for node_id in (gate_ids or ()) if node_id}
    grouped = {}
    for task in tasks or []:
        if not isinstance(task, dict) or task.get("workflow_id") != workflow_id:
            continue
        node_id = task.get("node") or task.get("stage")
        if node_id:
            grouped.setdefault(node_id, []).append(task)

    actions = []
    for node in (config or {}).get("nodes") or []:
        if not isinstance(node, dict):
            continue
        node_id = node.get("id")
        if not node_id or _is_code_gate(node) or node_id in gates:
            continue
        node_tasks = grouped.get(node_id) or []
        live = [task for task in node_tasks if _live(task)]
        if not live or any(task.get("finalize_escalated") for task in live):
            continue
        if any(task.get("status") not in _SETTLED for task in live):
            continue
        blocked = [task for task in live if _blocked(task)]
        if not blocked:
            continue
        if _spent(node_tasks, live) >= REDO_LIMIT:
            pending = [task for task in blocked if not task.get("accepted_with_limits")]
            if pending:
                actions.append({
                    "op": "accept",
                    "node_id": node_id,
                    "tasks": pending,
                    "note": "",
                })
            continue
        if all(task.get("accepted_with_limits") for task in blocked):
            continue
        actions.append({
            "op": "rework",
            "node_id": node_id,
            "tasks": list(live),
            "note": _note(blocked),
        })
    return actions
