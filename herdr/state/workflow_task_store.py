"""workflow task store persistence; extracted without changing behavior."""

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from herdr.transitions import TERMINAL_TASK_STATUSES
from ._facade import _facade


class TaskAlreadyExistsError(RuntimeError):
    """Raised when attempting an exclusive task insert for a task_id that already exists."""

    pass


def save_workflow(
    wf_dict: Dict[str, Any],
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Upsert a workflow record."""
    should_close = False
    if conn is None:
        conn = _facade().get_db_connection(db_path)
        should_close = True

    wid = wf_dict.get("workflow_id")
    if not wid:
        raise ValueError("workflow_id is required")

    now = time.time()
    title = wf_dict.get("title", "")
    status = wf_dict.get("status", "pending")
    template_name = wf_dict.get("template_name", "")
    current_stage = wf_dict.get("current_stage") or wf_dict.get("stage", "")
    config_json = json.dumps(wf_dict.get("config", {}), ensure_ascii=False)
    created_at = float(wf_dict.get("created_at") or now)

    # Exclude special keys from metadata
    meta = {k: v for k, v in wf_dict.items() if k not in {
        "workflow_id", "title", "status", "template_name", "current_stage", "stage", "config", "created_at"
    }}
    metadata_json = json.dumps(meta, ensure_ascii=False)

    try:
        conn.execute("""
            INSERT INTO workflows (workflow_id, title, status, template_name, current_stage, config_json, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workflow_id) DO UPDATE SET
                title=excluded.title,
                status=excluded.status,
                template_name=excluded.template_name,
                current_stage=excluded.current_stage,
                config_json=excluded.config_json,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at;
        """, (wid, title, status, template_name, current_stage, config_json, metadata_json, created_at, now))
    finally:
        if should_close:
            conn.close()

def get_workflow(workflow_id: str, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Fetch a workflow by its workflow_id."""
    from herdr.recovery_store import _workflow_snapshot
    try:
        conn = _facade().get_readonly_db_connection(db_path)
    except FileNotFoundError:
        return None
    try:
        conn.execute('BEGIN')
        try:
            return _workflow_snapshot(conn, workflow_id)[0]
        except ValueError as exc:
            if str(exc) == 'workflow not found':
                return None
            raise
    finally:
        conn.close()

def list_workflows(
    status: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Fetch all workflows, optionally filtered by status."""
    from herdr.recovery_store import _workflow_snapshot
    try:
        conn = _facade().get_readonly_db_connection(db_path)
    except FileNotFoundError:
        return []
    try:
        conn.execute('BEGIN')
        if status:
            rows = conn.execute("SELECT workflow_id FROM workflows WHERE status=? ORDER BY created_at DESC", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT workflow_id FROM workflows ORDER BY created_at DESC").fetchall()
        return [_workflow_snapshot(conn, row['workflow_id'])[0] for row in rows]
    finally:
        conn.close()

def delete_workflow(workflow_id: str, db_path: Optional[Path] = None) -> bool:
    """Delete a workflow and cascade its tasks/checkpoints."""
    conn = _facade().get_db_connection(db_path)
    try:
        conn.execute("BEGIN TRANSACTION;")
        cur = conn.execute("DELETE FROM workflows WHERE workflow_id = ?", (workflow_id,))
        deleted = cur.rowcount > 0
        conn.execute("COMMIT;")
        return deleted
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    finally:
        conn.close()

def save_task(
    task_dict: Dict[str, Any],
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
    fail_if_exists: bool = False,
) -> None:
    """Upsert a task record."""
    tid = task_dict.get("task_id")
    if not tid:
        raise ValueError("task_id is required")
    wid = task_dict.get("workflow_id") or "default"

    now = time.time()
    node = task_dict.get("node") or task_dict.get("stage", "")
    stage = task_dict.get("stage") or node
    agent = task_dict.get("agent", "auto")
    status = task_dict.get("status", "pending")
    verdict = task_dict.get("stage_verdict", "")
    verdict_note = task_dict.get("stage_verdict_note", "")
    pane_id = task_dict.get("pane_id", "")
    goal = task_dict.get("goal", "")
    blocker = task_dict.get("blocker", "")
    created_at = float(task_dict.get("created_at") or task_dict.get("started_at") or now)

    payload = {k: v for k, v in task_dict.items() if k not in {
        "task_id", "workflow_id", "node", "stage", "agent", "status",
        "stage_verdict", "stage_verdict_note", "pane_id", "goal", "blocker",
        "created_at", "version"
    }}
    payload_json = json.dumps(payload, ensure_ascii=False)

    should_close = conn is None
    if should_close:
        conn = _facade().get_db_connection(db_path)
    try:
        if should_close:
            conn.execute('BEGIN IMMEDIATE')
        # Parent creation and task/obligation writes share this transaction.
        if not conn.execute("SELECT 1 FROM workflows WHERE workflow_id = ?", (wid,)).fetchone():
            save_workflow({"workflow_id": wid, "title": wid, "status": "pending"}, db_path, conn=conn)
        existing = conn.execute('SELECT workflow_id,node,payload_json FROM tasks WHERE task_id=?', (tid,)).fetchone()
        if fail_if_exists and existing:
            raise TaskAlreadyExistsError(f"Task already exists: {tid}")
        if not existing or status not in {'superseded', 'cleaned', 'completed'}:
            from herdr.dispatch_recovery import require_authorization
            require_authorization(conn, wid, node, task_dict.get('dispatch_operation_id'))
        if existing:
            try:
                previous = json.loads(existing['payload_json'])
            except (TypeError, json.JSONDecodeError):
                previous = None
            if not isinstance(previous, dict):
                previous = {}
            identity_keys = ('dispatch_operation_id', 'run_id', 'execution_id', 'launch_intent_id')
            is_proven_unbound = False
            proven_intent_payload = None
            if previous.get('launch_intent_id'):
                ev = conn.execute(
                    "SELECT payload_json FROM events WHERE workflow_id=? AND event_type='launch_intent' AND source='launch' "
                    "AND json_extract(payload_json,'$.task_id')=? AND json_extract(payload_json,'$.intent_id')=? "
                    "ORDER BY timestamp DESC, id DESC LIMIT 1",
                    (existing['workflow_id'], tid, previous['launch_intent_id'])
                ).fetchone()
                if ev:
                    try:
                        proven_intent_payload = json.loads(ev['payload_json'])
                    except (TypeError, json.JSONDecodeError):
                        proven_intent_payload = None
                    if isinstance(proven_intent_payload, dict) and proven_intent_payload.get('dispatch_operation_id') is None:
                        is_proven_unbound = True

            if not is_proven_unbound:
                if any(previous.get(key) in (None, '') for key in identity_keys):
                    from herdr.node_dispatch_store import recover_registration_identity
                    durable = recover_registration_identity(conn, existing['workflow_id'], tid)
                    if durable:
                        if any(previous.get(key) not in (None, '') and previous[key] != durable[key]
                               for key in (*identity_keys, 'supersedes')):
                            raise ValueError('dispatch registration identity conflicts with durable launch intent')
                        previous.update(durable)
                    elif previous.get('dispatch_operation_id') is not None:
                        raise ValueError('dispatch registration identity is incomplete without durable launch intent')
                if previous.get('dispatch_operation_id') is not None and previous.get('supersedes') is None:
                    from herdr.node_dispatch_store import recover_registration_identity
                    durable = recover_registration_identity(conn, existing['workflow_id'], tid)
                    if durable:
                        previous['supersedes'] = durable.get('supersedes')

            if previous.get('dispatch_operation_id') != task_dict.get('dispatch_operation_id'):
                raise ValueError('cannot rebind an existing task to another dispatch operation')

            if previous.get('dispatch_operation_id') is None and (
                    existing['workflow_id'] != wid or existing['node'] != node
                    or any(previous.get(key) != task_dict.get(key) for key in ('run_id', 'execution_id'))):
                from herdr.node_dispatch_store import validate_task_registration
                validate_task_registration(conn, task_dict, now)

            if previous.get('dispatch_operation_id') is not None and (
                    existing['workflow_id'] != wid or existing['node'] != node
                    or any(previous.get(key) != task_dict.get(key) for key in ('run_id', 'execution_id', 'launch_intent_id', 'supersedes'))):
                raise ValueError('cannot change dispatch task registration identity')

            if is_proven_unbound and proven_intent_payload:
                intent_run = (proven_intent_payload.get('resources') or {}).get('run_id')
                if intent_run is None:
                    intent_run = proven_intent_payload.get('run_id')
                intent_exec = proven_intent_payload.get('execution_id')
                intent_supersedes = proven_intent_payload.get('supersedes')
                intent_wf = proven_intent_payload.get('workflow_id')
                intent_node = proven_intent_payload.get('node_id')
                intent_id = proven_intent_payload.get('intent_id')

                if intent_supersedes is not None and previous.get('supersedes') not in (None, '') and previous['supersedes'] != intent_supersedes:
                    raise ValueError('dispatch registration identity conflicts with durable launch intent')

                if (existing['workflow_id'] != wid) or (intent_wf is not None and wid != intent_wf):
                    raise ValueError('cannot change dispatch task registration identity')

                if (existing['node'] != node) or (intent_node is not None and node != intent_node):
                    raise ValueError('cannot change dispatch task registration identity')

                if (intent_id is not None and task_dict.get('launch_intent_id') != intent_id) or (
                        previous.get('launch_intent_id') is not None and task_dict.get('launch_intent_id') != previous['launch_intent_id']):
                    raise ValueError('cannot change dispatch task registration identity')

                if intent_exec is not None and task_dict.get('execution_id') != intent_exec:
                    raise ValueError('cannot change dispatch task registration identity')

                if (intent_run is not None and task_dict.get('run_id') != intent_run) or (
                        previous.get('run_id') is not None and task_dict.get('run_id') != previous['run_id']):
                    raise ValueError('cannot change dispatch task registration identity')

                if previous.get('supersedes') is not None:
                    if task_dict.get('supersedes') != previous['supersedes']:
                        raise ValueError('cannot change dispatch task registration identity')
                elif intent_supersedes is None:
                    if task_dict.get('supersedes') is not None:
                        raise ValueError('cannot change dispatch task registration identity')
                else:
                    if task_dict.get('supersedes') is not None and task_dict.get('supersedes') != intent_supersedes:
                        raise ValueError('cannot change dispatch task registration identity')
        else:
            from herdr.node_dispatch_store import validate_task_registration
            validate_task_registration(conn, task_dict, now)
        if fail_if_exists:
            try:
                conn.execute("""
                    INSERT INTO tasks (task_id, workflow_id, node, stage, agent, status, stage_verdict, stage_verdict_note, pane_id, goal, blocker, payload_json, created_at, updated_at, version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """, (tid, wid, node, stage, agent, status, verdict, verdict_note, pane_id, goal, blocker, payload_json, created_at, now))
            except sqlite3.IntegrityError as exc:
                raise TaskAlreadyExistsError(f"Task already exists: {tid}") from exc
        else:
            conn.execute("""
                INSERT INTO tasks (task_id, workflow_id, node, stage, agent, status, stage_verdict, stage_verdict_note, pane_id, goal, blocker, payload_json, created_at, updated_at, version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(task_id) DO UPDATE SET
                    workflow_id=excluded.workflow_id,
                    node=excluded.node,
                    stage=excluded.stage,
                    agent=excluded.agent,
                    status=excluded.status,
                    stage_verdict=excluded.stage_verdict,
                    stage_verdict_note=excluded.stage_verdict_note,
                    pane_id=excluded.pane_id,
                    goal=excluded.goal,
                    blocker=excluded.blocker,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at,
                    version=COALESCE(tasks.version, 0) + 1;
            """, (tid, wid, node, stage, agent, status, verdict, verdict_note, pane_id, goal, blocker, payload_json, created_at, now))
        from herdr.recovery_store import ensure_for_workflow
        ensure_for_workflow(conn, wid, now=now)
        if should_close:
            conn.execute('COMMIT')
        # Outcome choke point (write path only): a task reaching a terminal
        # status may now be settleable. Best-effort and fail-open; the
        # canonical resolver lives in herdr.execution_outcome.
        if (
            should_close
            and str(status) in TERMINAL_TASK_STATUSES
            and _facade()._outcome_autofinalize_enabled()
        ):
            try:
                from herdr.execution_outcome import try_autofinalize_for_task
            except ImportError:  # pragma: no cover - script-style fallback
                from herdr.execution_outcome import try_autofinalize_for_task
            try:
                try_autofinalize_for_task(str(tid), db_path=db_path)
            except Exception:
                pass
    except BaseException:
        if should_close and conn.in_transaction:
            conn.execute('ROLLBACK')
        raise
    finally:
        if should_close:
            conn.close()

def _decode_task_row(row: sqlite3.Row) -> Dict[str, Any]:
    payload = json.loads(row["payload_json"] or "{}")
    task = dict(payload)
    task.update({
        "task_id": row["task_id"],
        "workflow_id": row["workflow_id"],
        "node": row["node"],
        "stage": row["stage"],
        "agent": row["agent"],
        "status": row["status"],
        "stage_verdict": row["stage_verdict"],
        "stage_verdict_note": row["stage_verdict_note"],
        "pane_id": row["pane_id"],
        "goal": row["goal"],
        "blocker": row["blocker"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "version": int(row["version"] or 0) if "version" in row.keys() else 0,
    })
    return task

def get_task(task_id: str, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Fetch a single task by its task_id."""
    conn = _facade().get_db_connection(db_path)
    try:
        cur = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = cur.fetchone()
        return _facade()._decode_task_row(row) if row else None
    finally:
        conn.close()

def get_tasks(workflow_id: str, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Fetch all tasks for a workflow."""
    return list_tasks(workflow_id=workflow_id, db_path=db_path)

def list_tasks(
    workflow_id: Optional[str] = None,
    status: Optional[str] = None,
    db_path: Optional[Path] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Fetch tasks optionally filtered by workflow_id and/or status."""
    conn = _facade().get_db_connection(db_path)
    try:
        query = "SELECT * FROM tasks WHERE 1=1"
        params: List[Any] = []
        if workflow_id:
            query += " AND workflow_id = ?"
            params.append(workflow_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at ASC, task_id ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(max(0, min(int(limit), 10001)))
        cur = conn.execute(query, tuple(params))
        tasks = []
        for row in cur.fetchall():
            payload = json.loads(row["payload_json"] or "{}")
            t = dict(payload)
            t.update({
                "task_id": row["task_id"],
                "workflow_id": row["workflow_id"],
                "node": row["node"],
                "stage": row["stage"],
                "agent": row["agent"],
                "status": row["status"],
                "stage_verdict": row["stage_verdict"],
                "stage_verdict_note": row["stage_verdict_note"],
                "pane_id": row["pane_id"],
                "goal": row["goal"],
                "blocker": row["blocker"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "version": int(row["version"] or 0),
            })
            tasks.append(t)
        return tasks
    finally:
        conn.close()

def delete_task(task_id: str, db_path: Optional[Path] = None) -> bool:
    """Delete a task by its task_id."""
    conn = _facade().get_db_connection(db_path)
    try:
        conn.execute("BEGIN TRANSACTION;")
        cur = conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        deleted = cur.rowcount > 0
        conn.execute("COMMIT;")
        return deleted
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    finally:
        conn.close()
