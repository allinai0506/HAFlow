#!/usr/bin/env python3
"""Herdr Kernel Control Primitives (Phase 1).

Provides first-class runtime control primitives for human and automated steering:
- Pause / Resume (workflow-level and node-level)
- Step execution (single-step progression while paused)
- Rollback (DAG topological downstream invalidation & state reset)
- Force Pass (gate override with human audit trail)
- Checkpoint Snapshots (durable point-in-time state capture & restoration)
"""

import json
import os
import time
from pathlib import Path
from typing import Any

from . import runtime_state, workflow
from .state_store import (
    StateStore,
    get_state_store,
    sync_tasks_projection,
    sync_workflows_projection,
)
from .trajectory import TrajectoryLedger, record_trajectory_event_best_effort

HOME = Path.home()
CONTROLLER_DIR = HOME / ".herdr-controller"


def get_workflows_file(store: StateStore | None = None) -> Path:
    from .state_store import resolve_workflows_projection_file
    return resolve_workflows_projection_file(store=store)


def get_tasks_file(store: StateStore | None = None) -> Path:
    from .state_store import resolve_tasks_projection_file
    return resolve_tasks_projection_file(store=store)


def get_checkpoints_dir() -> Path:
    return Path(os.environ.get("CHECKPOINTS_DIR") or (CONTROLLER_DIR / "checkpoints"))


def get_stage_state_file() -> Path:
    return Path(os.environ.get("STAGE_STATE_FILE") or (CONTROLLER_DIR / "stage-state.json"))


def _atomic_write_json(file_path: Path, data: Any) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = file_path.with_suffix(f".tmp.{os.getpid()}.{time.time_ns()}")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, file_path)


def state_db_path() -> Path:
    """Resolve the Kernel's existing store selection without opening a database."""
    from .state_db import get_default_db_path
    if os.environ.get("HERDR_STATE_DB"):
        return Path(os.environ["HERDR_STATE_DB"])
    for key, canonical in (("TASKS_FILE", "tasks.json"), ("WORKFLOWS_FILE", "workflows.json")):
        value = os.environ.get(key)
        if value:
            path = Path(value)
            db_path = path.parent / "state.db" if path.name == canonical else path.with_suffix(".db")
            if db_path.parent.exists():
                return db_path
    return get_default_db_path()


def _get_store(store: StateStore | None = None) -> StateStore:
    if store is not None:
        return store
    return get_state_store(db_path=state_db_path())


def load_workflows_data(store: StateStore | None = None) -> dict[str, Any]:
    """Load workflows via StateStore (single source of truth)."""
    s = _get_store(store)
    return s.export_workflows_json()


def save_workflows_data(data: dict[str, Any], store: StateStore | None = None) -> None:
    """Save workflows into StateStore and sync compatibility JSON."""
    s = _get_store(store)
    for wid, wf in (data.get("workflows") or {}).items():
        wf.setdefault("workflow_id", wid)
        s.save_workflow(wf)
    sync_workflows_projection(store=s)


def load_tasks_data(store: StateStore | None = None) -> dict[str, Any]:
    """Load tasks via StateStore (single source of truth)."""
    s = _get_store(store)
    return s.export_tasks_json()


def save_tasks_data(data: dict[str, Any], store: StateStore | None = None) -> None:
    """Save tasks into StateStore and sync compatibility JSON."""
    s = _get_store(store)
    for t in data.get("tasks", []):
        if t.get("task_id") and t.get("workflow_id"):
            s.save_task(t)
    sync_tasks_projection(store=s)



def collect_downstream_nodes(nodes_by_id: dict[str, dict[str, Any]], root_id: str) -> set[str]:
    """Calculate the downstream closure containing root_id and all its reachable successors."""
    dependents: dict[str, set[str]] = {}
    for node in nodes_by_id.values():
        for dep in node.get("depends_on", []):
            dependents.setdefault(dep, set()).add(node["id"])

    seen = {root_id}
    frontier = [root_id]
    while frontier:
        curr = frontier.pop()
        for nxt in dependents.get(curr, ()):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return seen


# ============================================================
# 0. State Transition Gateway
# ============================================================

def transition_task(
    task_id: str,
    to_status: str,
    reason: str,
    source: str = "system",
    metadata: dict[str, Any] | None = None,
    force: bool = False,
    store: StateStore | None = None,
    expected_status: str | None = None,
    expected_version: int | None = None,
    expected_updated_at: float | None = None,
    intent: str | None = None,
) -> dict[str, Any]:
    """State Transition Gateway: Atomically transition task status and append WorkflowEvent."""
    s = _get_store(store)
    meta = dict(metadata or {})
    try:
        current = s.get_task(task_id)
    except Exception:
        current = None
    # Wrapup completion carries the machine-checked review-closure invariant.
    # This gateway is the single funnel for every completion source (CLI set,
    # the controller's CAS auto-accept path and repair flows); prompt-text
    # requirements alone are bypassable (Codex review P1, wf-haflow-1015-01).
    if (to_status == "completed" and current is not None
            and (current.get("node") or current.get("stage")) == "wrapup"):
        from . import review_closure
        closure_ok, closure_why = review_closure.check_completion_allowed(s, current)
        if not closure_ok:
            raise ValueError(f"[REVIEW CLOSURE REQUIRED] task={task_id}: {closure_why}")
    # RuntimeState follows the task transition inside the same atomic write:
    # record-only, never influences the transition outcome itself.
    from .archive import ARCHIVED_STATUSES
    from .node_capacity import pane_reference
    if current and to_status in ARCHIVED_STATUSES and pane_reference(current):
        meta.setdefault("pane_lifecycle", "orphaned")
        meta.setdefault("pane_orphan_reason", to_status)
    if "runtime" not in meta:
        if current is not None:
            updated_runtime = runtime_state.transition_runtime(current, to_status)
            if updated_runtime is not None:
                meta["runtime"] = updated_runtime
    if (
        expected_status is not None
        or expected_version is not None
        or expected_updated_at is not None
    ):
        res = s.compare_and_set_task_transition(
            task_id=task_id,
            to_status=to_status,
            reason=reason,
            source=source,
            metadata=meta,
            force=force,
            expected_status=expected_status,
            expected_version=expected_version,
            expected_updated_at=expected_updated_at,
            intent=intent,
        )
    else:
        res = s.transition_task(
            task_id=task_id,
            to_status=to_status,
            reason=reason,
            source=source,
            metadata=meta,
            force=force,
            intent=intent,
        )
    if not res.get("accepted", True):
        return res
    sync_tasks_projection(store=s)

    # Historical trajectory is best-effort and strictly after the existing
    # transition. It must never change the state-machine result.
    if current is not None and res.get("old_status") != res.get("new_status"):
        trajectory_task = dict(current)
        trajectory_task.update(meta)
        trajectory_task["status"] = to_status
        ledger = TrajectoryLedger(getattr(s, "db_path", None))
        trajectory_metadata = {
            "from_status": res.get("old_status"),
            "to_status": res.get("new_status"),
            "reason": reason,
            "source": source,
        }
        record_trajectory_event_best_effort(
            trajectory_task,
            "task_status_changed",
            ledger=ledger,
            status=to_status,
            metadata=trajectory_metadata,
        )
        if to_status == "completed":
            record_trajectory_event_best_effort(
                trajectory_task,
                "task_completed",
                ledger=ledger,
                status=to_status,
                metadata=trajectory_metadata,
            )
            record_trajectory_event_best_effort(
                trajectory_task,
                "run_completed",
                ledger=ledger,
                status=to_status,
                metadata=trajectory_metadata,
            )
        elif to_status == "failed":
            record_trajectory_event_best_effort(
                trajectory_task,
                "task_failed",
                ledger=ledger,
                status=to_status,
                metadata=trajectory_metadata,
            )
            record_trajectory_event_best_effort(
                trajectory_task,
                "run_failed",
                ledger=ledger,
                status=to_status,
                metadata=trajectory_metadata,
            )
    return res


def transition_workflow(
    workflow_id: str,
    to_status: str,
    reason: str,
    source: str = "system",
    metadata: dict[str, Any] | None = None,
    force: bool = False,
    store: StateStore | None = None,
    expected_status: str | None = None,
) -> dict[str, Any]:
    """State Transition Gateway: Atomically transition workflow status and append WorkflowEvent."""
    s = _get_store(store)
    res = s.transition_workflow(
        workflow_id=workflow_id,
        to_status=to_status,
        reason=reason,
        source=source,
        metadata=metadata,
        force=force,
        **({"expected_status": expected_status} if expected_status is not None else {}),
    )
    sync_workflows_projection(store=s)
    return res


def update_task_metadata(
    task_id: str,
    updates: dict[str, Any],
    store: StateStore | None = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Atomically update non-protected metadata fields of a task without touching status."""
    s = _get_store(store)
    res = s.update_task_metadata(task_id, updates, expected_version=expected_version)
    sync_tasks_projection(store=s)
    return res



def update_workflow_metadata(
    workflow_id: str,
    updates: dict[str, Any],
    store: StateStore | None = None,
    expected_status: str | None = None,
) -> dict[str, Any]:
    """Atomically update non-protected metadata fields of a workflow without touching status."""
    s = _get_store(store)
    res = s.update_workflow_metadata(
        workflow_id, updates, expected_status=expected_status)
    sync_workflows_projection(store=s)
    return res


# ============================================================
# 1. Pause & Resume Primitives
# ============================================================

def _set_workflow_pause(workflow_id, node_id, store, expected_identity, paused):
    from . import state_db
    from .workflow_control_store import control_transaction
    s = _get_store(store)
    status = 'paused' if paused else 'running'
    with control_transaction(s.db_path, workflow_id, expected_identity) as (conn, (target, _, _)):
        if node_id:
            paused_nodes = list(target.get('paused_nodes') or [])
            if paused and node_id not in paused_nodes:
                paused_nodes.append(node_id)
            elif not paused and node_id in paused_nodes:
                paused_nodes.remove(node_id)
            state_db.update_workflow_metadata(workflow_id, {'paused_nodes': paused_nodes}, conn=conn)
            result = {'ok': True, 'workflow_id': workflow_id, 'status': target['status'],
                      'node_id': node_id, 'paused_nodes': paused_nodes}
        else:
            state_db.transition_workflow(workflow_id, status,
                reason='pause_workflow' if paused else 'resume_workflow', source='kernel', conn=conn)
            result = {'ok': True, 'workflow_id': workflow_id, 'status': status}
    sync_workflows_projection(store=s)
    return result


def pause_workflow(workflow_id: str, node_id: str | None = None, store: StateStore | None = None,
                   expected_identity=None) -> dict[str, Any]:
    """Pause progression globally or hold a node under the same identity fence."""
    return _set_workflow_pause(workflow_id, node_id, store, expected_identity, True)


def resume_workflow(workflow_id: str, node_id: str | None = None, store: StateStore | None = None,
                    expected_identity=None) -> dict[str, Any]:
    """Resume progression globally or release a node under the same identity fence."""
    return _set_workflow_pause(workflow_id, node_id, store, expected_identity, False)



# ============================================================
# 2. Force Pass Gate Primitive
# ============================================================

def force_pass_gate(
    workflow_id: str,
    gate_node_id: str,
    note: str = "human forced pass",
    operator: str = "human",
    store: StateStore | None = None,
    expected_version: int | None = None,
    task_id: str | None = None,
    expected_pane_id: str | None = None,
    expected_task_versions: dict[str, int] | None = None,
    token: str | None = None,
    credentials_path: str | None = None,
    strict_auth: bool | None = None,
    secret: str | None = None,
) -> dict[str, Any]:
    """Forcibly mark a gate node verdict as passed/approved with an audit note."""
    s = _get_store(store)
    from .operator_auth import OperatorPrincipal, authenticate_and_authorize_operator
    target_task = None
    if task_id and hasattr(s, "get_task"):
        target_task = s.get_task(task_id)

    if isinstance(operator, OperatorPrincipal):
        principal = operator
        operator = principal.operator_id
    else:
        principal = authenticate_and_authorize_operator(
            operator_id=operator,
            action="force_pass_gate",
            token=token,
            credentials_path=credentials_path,
            task=target_task,
            store=s,
            strict_auth=strict_auth,
            secret=secret,
        )
        operator = principal.operator_id

    if hasattr(s, "force_pass_gate"):
        res = s.force_pass_gate(
            workflow_id=workflow_id,
            gate_node_id=gate_node_id,
            note=note,
            operator=operator,
            expected_version=expected_version,
            task_id=task_id,
            expected_pane_id=expected_pane_id,
            expected_task_versions=expected_task_versions,
            token=token,
            credentials_path=credentials_path,
            strict_auth=strict_auth,
            secret=secret,
            principal=principal,
        )
    else:
        tasks = s.list_tasks(workflow_id=workflow_id)
        matching_tasks = [
            t for t in tasks
            if gate_node_id in (t.get("node"), t.get("stage")) and t.get("status") != "superseded"
        ]

        tasks_to_update = []
        if task_id:
            target_task = next((t for t in matching_tasks if t.get("task_id") == task_id), None)
            if not target_task:
                raise RuntimeError(f"未找到任务 {task_id}（工作流 {workflow_id}）")
            if expected_version is not None and target_task.get("version") != expected_version:
                raise RuntimeError(
                    f"任务版本已在写入边界发生变化（期望版本 {expected_version}，当前版本 {target_task.get('version')}），写入已拒绝，请刷新页面"
                )
            tasks_to_update = [target_task]
        else:
            if expected_version is not None:
                raise RuntimeError(
                    f"节点级放行禁止使用单一 expected_version 进行版本保护，当前节点有效任务为 {[t.get('task_id') for t in matching_tasks]}，"
                    f"必须使用 expected_task_versions 映射进行完整任务集合校验，或指定具体的 task_id 进行单任务放行"
                )
            if expected_task_versions is not None:
                matching_tids = {t.get("task_id") for t in matching_tasks}
                provided_tids = set(expected_task_versions.keys())
                if matching_tids != provided_tids:
                    raise RuntimeError(
                        f"节点级放行版本映射不完整或不匹配: 当前节点有效任务为 {sorted(matching_tids)}，"
                        f"提交映射为 {sorted(provided_tids)}，必须完整核验所有任务版本"
                    )
                for t in matching_tasks:
                    tid = t.get("task_id")
                    exp_v = expected_task_versions[tid]
                    cur_v = t.get("version")
                    if cur_v != exp_v:
                        raise RuntimeError(
                            f"任务 {tid} 版本已在写入边界发生变化（期望版本 {exp_v}，当前版本 {cur_v}），写入已拒绝，请刷新页面"
                        )
            tasks_to_update = matching_tasks

        from .task_checkpoint import reject_verdict_only_override
        for task in tasks_to_update:
            reject_verdict_only_override(task)
        updated_tasks = []
        for task in tasks_to_update:
            tid = task.get("task_id")
            update_task_metadata(
                task_id=tid,
                updates={
                    "stage_verdict": "pass",
                    "stage_verdict_note": f"[FORCE PASS by {operator}] {note}",
                },
                store=s,
            )
            updated_tasks.append(tid)

        wf_entry = s.get_workflow(workflow_id)
        if wf_entry:
            now = time.time()
            overrides = dict(wf_entry.get("gate_overrides") or {})
            history_overrides = list(wf_entry.get("gate_overrides_history") or [])
            override_record = {
                "verdict": "pass",
                "note": note,
                "operator": operator,
                "role": principal.role,
                "authenticated": principal.authenticated,
                "auth_method": principal.auth_method,
                "timestamp": now,
            }
            if task_id:
                override_record["task_id"] = task_id
                target_post_ver = (target_task.get("version") or 0) + 1
                override_record["task_version"] = target_post_ver
                override_record["task_versions"] = {task_id: target_post_ver}
                override_record["task_ids"] = [task_id]
            else:
                snapshot_versions = {
                    t["task_id"]: (t.get("version") or 0) + 1
                    for t in tasks_to_update
                }
                override_record["task_versions"] = snapshot_versions
                override_record["task_ids"] = sorted(list(snapshot_versions.keys()))

            overrides[gate_node_id] = override_record
            history_entry = dict(override_record)
            history_entry["node_id"] = gate_node_id
            history_entry["action"] = "force_pass"
            history_overrides.append(history_entry)

            update_workflow_metadata(
                workflow_id=workflow_id,
                updates={
                    "gate_overrides": overrides,
                    "gate_overrides_history": history_overrides,
                },
                store=s,
            )

        res = {
            "ok": True,
            "workflow_id": workflow_id,
            "gate_node_id": gate_node_id,
            "updated_tasks": updated_tasks,
        }

    sync_tasks_projection(store=s)
    sync_workflows_projection(store=s)
    return res


# ============================================================
# 3. Rollback Primitive
# ============================================================

def rollback_workflow(
    workflow_id: str,
    target_node_id: str,
    reason: str = "manual_rollback",
    store: StateStore | None = None,
    expected_identity=None,
) -> dict[str, Any]:
    """Atomically invalidate current execution tasks, overrides, and advance locks."""
    from .workflow_control_store import rollback
    s = _get_store(store)
    result = rollback(s.db_path, workflow_id, target_node_id, reason, expected_identity)
    sync_workflows_projection(store=s)
    sync_tasks_projection(store=s)

    s_file = get_stage_state_file()
    if s_file.exists():
        try:
            with open(s_file, "r", encoding="utf-8") as fp:
                s_data = json.load(fp)
            if isinstance(s_data, dict):
                changed = False
                for n_id in result['affected_nodes']:
                    key = f"{workflow_id}:{n_id}"
                    if key in s_data:
                        del s_data[key]
                        changed = True
                if changed:
                    _atomic_write_json(s_file, s_data)
        except Exception:
            pass
    return result


# ============================================================
# 4. Step Primitive
# ============================================================

def _is_task_completed(task: dict[str, Any]) -> bool:
    status = task.get("status")
    if status in ("completed", "cleaned", "cleanup_ready"):
        verdict = task.get("stage_verdict")
        return verdict != "blocked"
    return False


def step_workflow(workflow_id: str, expected_identity=None) -> dict[str, Any]:
    """Execute a single step: compute ready nodes and dispatch or advance exactly one ready node."""
    store = _get_store()
    from . import state_db
    from .workflow_control_store import (
        control_transaction,
        fingerprint,
        validate_identity,
    )
    reader = state_db.get_readonly_db_connection(store.db_path)
    try:
        reader.execute('BEGIN')
        snapshot = validate_identity(reader, workflow_id, expected_identity)
        expected_identity = expected_identity or {'execution_id': snapshot[0].get('execution_id'),
                                                  'candidate_sha': snapshot[0].get('candidate_sha'),
                                                  'workflow_fingerprint': fingerprint(reader, workflow_id)}
    finally:
        reader.close()
    # Business evidence may hash artifacts. Keep that I/O outside the write lock.
    result = _prepare_step(store, *snapshot)
    with control_transaction(store.db_path, workflow_id, expected_identity) as (conn, _):
        if result['ok']:
            state_db.transition_workflow(workflow_id, 'paused', reason='step_workflow', source='kernel_step', conn=conn)
    sync_workflows_projection(store=store)
    return result


def _prepare_step(store, wf_entry, norm_cfg, wf_tasks):
    workflow_id = wf_entry['workflow_id']
    nodes = norm_cfg.get('nodes', [])
    if not nodes:
        raise ValueError(f"Workflow '{workflow_id}' has no pinned nodes in config")

    from .workflow_progress import assess_workflow
    assessment = assess_workflow(wf_entry, norm_cfg, wf_tasks)
    if not assessment['can_advance']:
        return {'ok': False, 'workflow_id': workflow_id, 'reason': 'workflow_blocked',
                'blocker_task_ids': [t['task_id'] for t in assessment['blockers']]}

    completed_nodes: set[str] = set()
    for n in nodes:
        n_id = n["id"]
        n_tasks = [
            t for t in wf_tasks
            if t.get("status") != "superseded" and (t.get("node") == n_id or t.get("stage") == n_id)
        ]
        if n_tasks and any(_is_task_completed(t) for t in n_tasks):
            completed_nodes.add(n_id)

    ready_nodes = workflow.get_ready_nodes(norm_cfg, completed_nodes)
    paused_nodes = set(wf_entry.get("paused_nodes") or [])
    eligible_ready = [n for n in ready_nodes if n["id"] not in paused_nodes]

    if not eligible_ready:
        return {
            "ok": False,
            "workflow_id": workflow_id,
            "reason": "no_ready_nodes",
            "ready_nodes": [],
        }

    stepped = eligible_ready[0]
    stepped_id = stepped["id"]
    from .business_gate import business_gate_blockers
    missing_business = business_gate_blockers(store, wf_entry, norm_cfg, wf_tasks, stepped_id)
    if missing_business:
        return {'ok': False, 'workflow_id': workflow_id, 'reason': 'business_acceptance_unavailable',
                'blocker_task_ids': missing_business}

    return {
        "ok": True,
        "workflow_id": workflow_id,
        "stepped_node": stepped_id,
        "stepped_label": stepped.get("label", stepped_id),
        "remaining_ready": [n["id"] for n in eligible_ready[1:]],
    }


# ============================================================
# 5. Checkpoint Snapshot Primitives
# ============================================================

def create_checkpoint(
    workflow_id: str,
    tag: str | None = None,
    parent_checkpoint_id: str | None = None,
    expected_identity=None,
) -> dict[str, Any]:
    """Capture a durable point-in-time snapshot of the workflow and its tasks via StateStore."""
    store = _get_store()

    wf_entry = store.get_workflow(workflow_id)
    if not wf_entry:
        raise ValueError(f"Workflow '{workflow_id}' not found")

    res = store.create_checkpoint(
        workflow_id=workflow_id,
        tag=tag,
        parent_checkpoint_id=parent_checkpoint_id,
        expected_identity=expected_identity,
    )
    cp_id = res["checkpoint_id"]

    # Write compatibility JSON file if checkpoints_dir is accessible
    try:
        cp_dir = get_checkpoints_dir() / workflow_id
        cp_dir.mkdir(parents=True, exist_ok=True)
        cp_file = cp_dir / f"{cp_id}.json"
        snapshot = store.get_checkpoint(workflow_id, cp_id)
        _atomic_write_json(cp_file, snapshot)
        res["path"] = str(cp_file)
    except Exception:
        pass

    return res


def list_checkpoints(workflow_id: str) -> list[dict[str, Any]]:
    """List all available checkpoints for workflow_id via StateStore."""
    store = get_state_store()
    return store.list_checkpoints(workflow_id)


def get_checkpoint(workflow_id: str, checkpoint_id: str) -> dict[str, Any]:
    """Retrieve full snapshot payload for a checkpoint via StateStore."""
    store = get_state_store()
    return store.get_checkpoint(workflow_id, checkpoint_id)


def restore_checkpoint(workflow_id: str, checkpoint_id: str, expected_identity=None) -> dict[str, Any]:
    """Restore workflow state and tasks from a checkpoint snapshot via StateStore."""
    store = _get_store()
    res = store.restore_checkpoint(workflow_id, checkpoint_id, expected_identity=expected_identity)

    # Sync compatibility files
    sync_workflows_projection(store=store)
    sync_tasks_projection(store=store)

    # Clean up any transient stage advance locks in stage-state.json
    s_file = get_stage_state_file()
    if s_file.exists():
        try:
            with open(s_file, "r", encoding="utf-8") as fp:
                s_data = json.load(fp)
            if isinstance(s_data, dict):
                to_del = [k for k in s_data if k.startswith(f"{workflow_id}:")]
                if to_del:
                    for k in to_del:
                        del s_data[k]
                    _atomic_write_json(s_file, s_data)
        except Exception:
            pass

    return res


def fork_workflow_from_checkpoint(
    checkpoint_id: str,
    new_workflow_id: str,
    new_title: str | None = None,
) -> dict[str, Any]:
    """Time-travel branching: Fork a new workflow instance via StateStore."""
    store = get_state_store()
    res = store.fork_workflow_from_checkpoint(
        checkpoint_id=checkpoint_id,
        new_workflow_id=new_workflow_id,
        new_title=new_title,
    )
    sync_workflows_projection(store=store)
    sync_tasks_projection(store=store)
    return res


def reconcile_workflow(
    workflow_id: str,
    store: StateStore | None = None,
) -> dict[str, Any]:
    """Platform-level self-healing: Reconcile DAG nodes and delivery ledger state."""
    from .reconciliation import reconcile_workflow_state
    s = _get_store(store)
    res = reconcile_workflow_state(workflow_id, store=s)
    sync_workflows_projection(store=s)
    sync_tasks_projection(store=s)
    return res


def bypass_task_gate(
    task_id: str,
    reason: str,
    operator: str = "human",
    note: str | None = None,
    store: StateStore | None = None,
    expected_version: int | None = None,
    token: str | None = None,
    credentials_path: str | None = None,
    strict_auth: bool | None = None,
    secret: str | None = None,
) -> dict[str, Any]:
    """Universal human-in-the-loop gate bypass.

    Creates a structured bypass receipt, marks stage_verdict as 'pass',
    clears blockers, unblocks suspended/blocked tasks, writes gate verdict file,
    records trajectory audit event, and appends a shared workflow note.
    """
    import hashlib
    import uuid
    from datetime import datetime, timezone

    from . import workflow_docs
    from .direct_dispatch import gate_verdict_path

    cleaned_reason = str(reason or "").strip()
    if not cleaned_reason:
        raise ValueError("Gate bypass reason must be provided")

    s = _get_store(store)
    task = s.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")

    from .operator_auth import OperatorPrincipal, authenticate_and_authorize_operator
    if isinstance(operator, OperatorPrincipal):
        principal = operator
        operator = principal.operator_id
    else:
        principal = authenticate_and_authorize_operator(
            operator_id=operator,
            action="bypass_gate",
            token=token,
            credentials_path=credentials_path,
            task=task,
            store=s,
            strict_auth=strict_auth,
            secret=secret,
        )
        operator = principal.operator_id

    if expected_version is not None and task.get("version") != expected_version:
        raise ValueError(
            f"任务版本已在写入边界发生变化（期望版本 {expected_version}，当前版本 {task.get('version')}），写入已拒绝"
        )

    from .task_checkpoint import reject_verdict_only_override
    reject_verdict_only_override(task)

    now = time.time()
    bypass_id = f"bypass_{uuid.uuid4().hex[:12]}"
    previous_status = task.get("status")
    previous_verdict = task.get("stage_verdict")
    previous_note = task.get("stage_verdict_note")

    receipt = {
        "schema_version": 1,
        "bypass_id": bypass_id,
        "task_id": task_id,
        "run_id": task.get("run_id"),
        "workflow_id": task.get("workflow_id"),
        "node_id": task.get("node") or task.get("stage"),
        "previous_status": previous_status,
        "previous_verdict": previous_verdict,
        "previous_note": previous_note,
        "reason": cleaned_reason,
        "operator": operator,
        "role": principal.role,
        "authenticated": principal.authenticated,
        "auth_method": principal.auth_method,
        "note": note,
        "timestamp": now,
        "bypassed_at": datetime.now(timezone.utc).isoformat(),
    }
    if task.get("execution_token"):
        receipt["execution_token"] = task.get("execution_token")
    if task.get("rework_request_id"):
        receipt["rework_request_id"] = task.get("rework_request_id")
    if task.get("rework_token"):
        receipt["rework_token"] = task.get("rework_token")

    # Determine status transition: unblock if blocked or paused; complete if failed gate
    new_status = previous_status
    if previous_status in ("blocked", "suspended", "failed"):
        # For verification/gate nodes, completion is the target
        new_status = "completed"
    elif previous_status == "paused":
        new_status = "working"

    verdict_note = f"[GATE BYPASS by {operator}] {cleaned_reason}"
    if note:
        verdict_note += f" ({note})"

    v_data = {
        "verdict": "pass",
        "note": verdict_note,
        "bypass": receipt,
    }
    if task.get("execution_token"):
        v_data["execution_token"] = task.get("execution_token")
    if task.get("rework_request_id"):
        v_data["rework_request_id"] = task.get("rework_request_id")
    if task.get("rework_token"):
        v_data["rework_token"] = task.get("rework_token")

    v_bytes = json.dumps(v_data, indent=2, ensure_ascii=False).encode("utf-8")
    snapshot_hash = hashlib.sha256(v_bytes).hexdigest()

    metadata = {
        "stage_verdict": "pass",
        "stage_verdict_note": verdict_note,
        "blocker": None,
        "gate_bypassed": True,
        "gate_bypass": receipt,
        "evidence_snapshot_hash": snapshot_hash,
    }
    if new_status == "completed":
        metadata["failure_reason"] = None

    def _apply_bypass():
        target_expected_version = expected_version if expected_version is not None else task.get("version")
        if hasattr(s, "compare_and_set_task_transition"):
            res = s.compare_and_set_task_transition(
                task_id=task_id,
                to_status=new_status,
                reason=f"gate_bypassed: {cleaned_reason}",
                source="operator-bypass",
                metadata=metadata,
                intent="operator-bypass",
                expected_status=previous_status,
                expected_version=target_expected_version,
            )
            if not res.get("ok", True):
                raise ValueError(
                    f"[GATE BYPASS CAS FAILED] task={task_id}: {res.get('reason')} "
                    f"(expected status={previous_status}, version={target_expected_version})"
                )
            return res.get("task") or s.get_task(task_id)
        else:
            if expected_version is not None and task.get("version") != expected_version:
                raise ValueError(
                    f"任务版本已在写入边界发生变化（期望版本 {expected_version}，当前版本 {task.get('version')}），写入已拒绝"
                )
            task["status"] = new_status
            task.update(metadata)
            task["updated_at"] = now
            task["version"] = (task.get("version") or 0) + 1
            s.save_task(task)
            return task

    # This bypass persists via StateStore transition gateway with privileged intent
    # 'operator-bypass'. For wrapup node, shared review-closure invariant is enforced
    # explicitly under the clone lock (Codex review P1, wf-haflow-1015-01 & PR #270).
    if new_status == "completed" and (task.get("node") or task.get("stage")) == "wrapup":
        from . import review_closure
        c_path = task.get("clone_path")
        with review_closure.clone_lock(c_path):
            closure_ok, closure_why = review_closure.check_completion_allowed(s, task)
            if not closure_ok:
                raise ValueError(f"[REVIEW CLOSURE REQUIRED] task={task_id}: {closure_why}")
            task = _apply_bypass()
    else:
        task = _apply_bypass()

    sync_tasks_projection(store=s)

    # Write gate verdict file so external watchers immediately see the pass
    try:
        v_path = Path(gate_verdict_path(task_id))
        v_path.parent.mkdir(parents=True, exist_ok=True)
        v_path.write_bytes(v_bytes)
        clone_path = task.get("clone_path")
        if clone_path:
            try:
                c_path = Path(clone_path) / ".herdr" / "gate-verdict.json"
                c_path.parent.mkdir(parents=True, exist_ok=True)
                c_path.write_bytes(v_bytes)
            except Exception:
                pass
    except Exception:
        pass

    # Trajectory audit event
    record_trajectory_event_best_effort(
        task,
        "gate_bypassed",
        payload={"bypass": receipt},
        ledger=TrajectoryLedger(s.db_path) if hasattr(s, "db_path") else None,
    )

    # Append note to shared workflow ledger if workflow_id is present
    wf_id = task.get("workflow_id")
    if wf_id:
        try:
            workflow_docs.append_note(
                workflow_id=wf_id,
                kind="gate",
                title=f"Gate bypassed for {task_id}",
                body=f"Bypass ID: {bypass_id}\nOperator: {operator}\nReason: {cleaned_reason}",
                node=task.get("node") or task.get("stage"),
                task_id=task_id,
                source=workflow_docs.SOURCE_HUMAN,
                fields={
                    "bypass_id": bypass_id,
                    "operator": operator,
                    "reason": cleaned_reason,
                    "verdict": "pass",
                },
            )
        except Exception:
            pass

    return {
        "task_id": task_id,
        "bypass_id": bypass_id,
        "status": new_status,
        "previous_status": previous_status,
        "stage_verdict": "pass",
        "previous_verdict": previous_verdict,
        "reason": cleaned_reason,
        "operator": operator,
        "receipt": receipt,
    }
