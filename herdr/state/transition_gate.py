"""transition gate persistence; extracted without changing behavior."""

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional
from herdr.transitions import InvalidTransitionError, validate_workflow_transition
from ._facade import _facade
from .constants import PROTECTED_TASK_METADATA_FIELDS, PROTECTED_WORKFLOW_METADATA_FIELDS, RESERVED_EVENT_METADATA_FIELDS


def transition_workflow(
    workflow_id: str,
    to_status: str,
    reason: str,
    source: str = "system",
    metadata: Optional[Dict[str, Any]] = None,
    force: bool = False,
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
    expected_status: Optional[str] = None,
) -> Dict[str, Any]:
    """Atomically validate and transition a workflow status, appending a canonical WorkflowEvent."""
    should_close = False
    if conn is None:
        conn = _facade().get_db_connection(db_path)
        should_close = True

    try:
        if should_close:
            conn.execute("BEGIN IMMEDIATE;")

        cur = conn.execute("SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Workflow '{workflow_id}' not found")

        old_status = row["status"]
        if expected_status is not None and old_status != expected_status:
            raise InvalidTransitionError(
                f"Workflow '{workflow_id}' changed during preflight: "
                f"expected {expected_status}, found {old_status}"
            )
        validate_workflow_transition(old_status, to_status, force=force)

        user_meta = dict(metadata or {})
        forbidden = set(user_meta.keys()) & PROTECTED_WORKFLOW_METADATA_FIELDS
        if forbidden:
            raise ValueError(f"Cannot overwrite protected workflow fields via metadata: {sorted(forbidden)}")
        forbidden_event = set(user_meta.keys()) & RESERVED_EVENT_METADATA_FIELDS
        if forbidden_event:
            raise ValueError(f"Cannot overwrite reserved event fields via metadata: {sorted(forbidden_event)}")

        now = time.time()
        meta = json.loads(row["metadata_json"] or "{}")
        cfg = json.loads(row["config_json"] or "{}")

        if to_status == "completed" and user_meta.get("outcome", meta.get("outcome")) == "delivered":
            from herdr.recovery_store import _snapshot
            from herdr.workflow import validate_delivery_inventory
            validate_delivery_inventory(*_snapshot(conn, workflow_id))

        wf_dict = dict(meta)
        wf_dict.update({
            "workflow_id": row["workflow_id"],
            "title": row["title"],
            "status": old_status,
            "template_name": row["template_name"],
            "current_stage": row["current_stage"],
            "config": cfg,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })

        if force:
            wf_dict["forced"] = True
        for k, v in user_meta.items():
            wf_dict[k] = v

        wf_dict["status"] = to_status
        wf_dict["updated_at"] = now

        if to_status == "completed":
            wf_dict.setdefault("completed_at", now)

        _facade().save_workflow(wf_dict, db_path=None, conn=conn)

        event_payload = {
            **user_meta,
            "from_status": old_status,
            "to_status": to_status,
            "reason": reason,
            "forced": force,
        }
        event = _facade().record_event(
            {
                "workflow_id": workflow_id,
                "node_id": user_meta.get("node_id") or wf_dict.get("current_stage"),
                "task_id": None,
                "agent_id": None,
                "event_type": "workflow_transition",
                "payload": event_payload,
                "source": source,
                "timestamp": now,
            },
            conn=conn,
        )

        if should_close:
            conn.execute("COMMIT;")

        return {
            "ok": True,
            "workflow_id": workflow_id,
            "old_status": old_status,
            "new_status": to_status,
            "reason": reason,
            "source": source,
            "event_id": event.get("id"),
            "workflow": wf_dict,
        }
    except Exception:
        if should_close:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
        raise
    finally:
        if should_close:
            conn.close()

def register_workflow(
    wf_dict: Dict[str, Any],
    event_payload: Optional[Dict[str, Any]] = None,
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
    save_workflow_fn: Optional[Any] = None,
) -> Dict[str, Any]:
    """Atomically upsert a workflow record and persist its authoritative workflow_created event.

    Shares a single SQLite write transaction (BEGIN IMMEDIATE) to guarantee that
    workflow record persistence and creation audit event either both commit or both roll back.
    Idempotent: If a workflow_created event already exists for this workflow_id,
    deduplicates the event to prevent multiple creation records on registration retries.
    """
    should_close = False
    if conn is None:
        conn = _facade().get_db_connection(db_path)
        should_close = True

    wid = wf_dict.get("workflow_id")
    if not wid:
        raise ValueError("workflow_id is required")

    try:
        if should_close:
            conn.execute("BEGIN IMMEDIATE;")

        row = conn.execute("SELECT status, metadata_json FROM workflows WHERE workflow_id = ?", (wid,)).fetchone()
        from_status = row["status"] if row else None
        if from_status and from_status != "running":
            validate_workflow_transition(from_status, "running")

        save_fn = save_workflow_fn or (lambda wf, **kw: _facade().save_workflow(wf, db_path=None, **kw))
        try:
            save_fn(wf_dict, conn=conn)
        except TypeError:
            save_fn(wf_dict)

        # Check for existing workflow_created event to deduplicate retries (ADR-0007 / P2)
        existing_ev = conn.execute(
            "SELECT id FROM events WHERE workflow_id = ? AND event_type = 'workflow_created' LIMIT 1",
            (wid,)
        ).fetchone()

        event_id = None
        if not existing_ev:
            payload = dict(event_payload or {})
            payload.setdefault("workflow_id", wid)
            payload.setdefault("title", wf_dict.get("title", ""))
            payload.setdefault("status", "running")
            payload.setdefault("from_status", from_status)
            ev = _facade().record_event({
                "workflow_id": wid,
                "node_id": None,
                "task_id": None,
                "agent_id": None,
                "event_type": "workflow_created",
                "payload": payload,
                "source": "register_workflow",
                "timestamp": time.time(),
            }, conn=conn)
            event_id = ev.get("id")
        else:
            event_id = existing_ev["id"]

        if should_close:
            conn.execute("COMMIT;")

        return {
            "ok": True,
            "workflow_id": wid,
            "status": "running",
            "from_status": from_status,
            "event_id": event_id,
            "deduplicated": bool(existing_ev),
        }
    except Exception:
        if should_close:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
        raise
    finally:
        if should_close:
            conn.close()

def update_task_metadata(
    task_id: str,
    updates: Dict[str, Any],
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
    expected_version: Optional[int] = None,
) -> Dict[str, Any]:
    """Atomically update non-protected metadata fields of a task without touching status or identity."""
    forbidden = set(updates.keys()) & PROTECTED_TASK_METADATA_FIELDS
    if forbidden:
        raise ValueError(f"Cannot update protected task fields via metadata update: {sorted(forbidden)}")

    should_close = False
    if conn is None:
        conn = _facade().get_db_connection(db_path)
        should_close = True

    try:
        if should_close:
            conn.execute("BEGIN IMMEDIATE;")

        cur = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Task '{task_id}' not found")

        if expected_version is not None and row["version"] != expected_version:
            raise RuntimeError(
                f"任务版本已在写入边界发生变化（期望版本 {expected_version}，当前版本 {row['version']}），写入已拒绝，请刷新页面"
            )

        now = time.time()
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
            "updated_at": now,
        })

        for k, v in updates.items():
            if v is None:
                t.pop(k, None)
            else:
                t[k] = v

        _facade().save_task(t, db_path=None, conn=conn)

        if should_close:
            conn.execute("COMMIT;")

        return t
    except Exception:
        if should_close:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
        raise
    finally:
        if should_close:
            conn.close()

def update_workflow_metadata(
    workflow_id: str,
    updates: Dict[str, Any],
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
    expected_status: str | None = None,
    save_workflow_fn: Any | None = None,
) -> Dict[str, Any]:
    """Atomically update non-protected metadata fields of a workflow without touching status or identity.

    ``expected_status`` is a compare-and-set guard: the transaction re-reads
    the authoritative row and refuses to write when the status changed.
    ``save_workflow_fn`` preserves the store-level writer (and its test
    failure-injection point) while keeping the same transaction.
    """
    forbidden = set(updates.keys()) & PROTECTED_WORKFLOW_METADATA_FIELDS
    if forbidden:
        raise ValueError(f"Cannot update protected workflow fields via metadata update: {sorted(forbidden)}")

    should_close = False
    if conn is None:
        conn = _facade().get_db_connection(db_path)
        should_close = True

    try:
        if should_close:
            conn.execute("BEGIN IMMEDIATE;")

        cur = conn.execute("SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Workflow '{workflow_id}' not found")

        if expected_status is not None and row["status"] != expected_status:
            raise InvalidTransitionError(
                f"Workflow '{workflow_id}' changed during metadata update: "
                f"expected {expected_status}, found {row['status']}"
            )

        now = time.time()
        meta = json.loads(row["metadata_json"] or "{}")
        cfg = json.loads(row["config_json"] or "{}")
        wf = dict(meta)
        wf.update({
            "workflow_id": row["workflow_id"],
            "title": row["title"],
            "status": row["status"],
            "template_name": row["template_name"],
            "current_stage": row["current_stage"],
            "config": cfg,
            "created_at": row["created_at"],
            "updated_at": now,
        })

        for k, v in updates.items():
            if v is None:
                wf.pop(k, None)
            else:
                wf[k] = v

        if save_workflow_fn is not None:
            save_workflow_fn(wf, conn=conn)
        else:
            _facade().save_workflow(wf, db_path=None, conn=conn)

        if should_close:
            conn.execute("COMMIT;")

        return wf
    except Exception:
        if should_close:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
        raise
    finally:
        if should_close:
            conn.close()

def force_pass_gate(
    workflow_id: str,
    gate_node_id: str,
    note: str = "human forced pass",
    operator: str = "human",
    db_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
    expected_version: int | None = None,
    task_id: str | None = None,
    expected_pane_id: str | None = None,
    expected_task_versions: dict[str, int] | None = None,
    token: str | None = None,
    credentials_path: str | None = None,
    strict_auth: bool | None = None,
    secret: str | None = None,
    principal: Any | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Atomically check and force-pass a gate node within a single immediate transaction."""
    should_close = False
    if conn is None:
        conn = _facade().get_db_connection(db_path)
        should_close = True

    try:
        if should_close:
            conn.execute("BEGIN IMMEDIATE;")

        cur_wf = conn.execute("SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,))
        wf_row = cur_wf.fetchone()
        if not wf_row:
            raise ValueError(f"Workflow '{workflow_id}' not found")

        cur_tasks = conn.execute(
            "SELECT * FROM tasks WHERE workflow_id = ? ORDER BY created_at ASC", (workflow_id,)
        )
        all_task_rows = cur_tasks.fetchall()
        all_tasks = [_facade()._decode_task_row(r) for r in all_task_rows]

        matching_tasks = [
            t for t in all_tasks
            if gate_node_id in (t.get("node"), t.get("stage")) and t.get("status") != "superseded"
        ]

        target_task = None
        if task_id:
            target_task = next((t for t in matching_tasks if t.get("task_id") == task_id), None)
            if not target_task:
                any_t = next((t for t in all_tasks if t.get("task_id") == task_id), None)
                if not any_t:
                    raise RuntimeError(f"未找到任务 {task_id}（工作流 {workflow_id}）")
                if any_t.get("status") == "superseded":
                    raise RuntimeError(f"任务 {task_id} 已被替换或作废，请刷新页面")
                t_node = any_t.get("node") or any_t.get("stage")
                raise RuntimeError(f"任务 {task_id} 属于节点 {t_node}，与目标门禁节点 {gate_node_id} 不匹配")
        elif matching_tasks:
            for mt in matching_tasks:
                m_agent = str(mt.get("agent") or "").strip().lower()
                op_name = operator.operator_id if hasattr(operator, "operator_id") else str(operator or "")
                if m_agent and op_name.strip().lower() == m_agent:
                    from herdr.operator_auth import OperatorImpersonationError
                    raise OperatorImpersonationError(
                        "the operator must be independent of the task worker; agent cannot bypass its own gate"
                    )

        from herdr.operator_auth import (
            OperatorPrincipal,
            authenticate_and_authorize_operator,
        )
        if principal is None and isinstance(operator, OperatorPrincipal):
            principal = operator

        if principal is None:
            principal = authenticate_and_authorize_operator(
                operator_id=operator,
                action="force_pass_gate",
                token=token,
                credentials_path=credentials_path,
                task=target_task,
                db_path=db_path,
                strict_auth=strict_auth,
                secret=secret,
            )
            operator = principal.operator_id
        else:
            operator = principal.operator_id
            if target_task:
                t_agent = str(target_task.get("agent") or "").strip().lower()
                if t_agent and operator.strip().lower() == t_agent:
                    from herdr.operator_auth import OperatorImpersonationError
                    raise OperatorImpersonationError(
                        "the operator must be independent of the task worker; agent cannot bypass its own gate"
                    )

        tasks_to_update = []
        if task_id:

            if expected_pane_id is not None:
                cur_pane = str(target_task.get("pane_id") or "").strip()
                exp_pane = str(expected_pane_id).strip()
                if cur_pane != exp_pane:
                    raise RuntimeError(
                        f"任务运行实例已变化（期望实例 {expected_pane_id}，当前实例 {cur_pane}），写入已拒绝，请刷新页面"
                    )

            if expected_version is not None:
                cur_ver = target_task.get("version")
                if cur_ver != expected_version:
                    raise RuntimeError(
                        f"任务版本已在写入边界发生变化（期望版本 {expected_version}，当前版本 {cur_ver}），写入已拒绝，请刷新页面"
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

        from herdr.task_checkpoint import reject_verdict_only_override
        for task in tasks_to_update:
            reject_verdict_only_override(task)

        now = time.time()
        updated_tasks = []
        for t in tasks_to_update:
            tid = t["task_id"]
            # Target optimistic version for task CAS write directly from caller expected parameters
            if expected_task_versions is not None:
                exp_v = expected_task_versions.get(tid)
            elif expected_version is not None:
                exp_v = expected_version
            else:
                exp_v = None
            payload = {k: v for k, v in t.items() if k not in {
                "task_id", "workflow_id", "node", "stage", "agent", "status",
                "stage_verdict", "stage_verdict_note", "pane_id", "goal", "blocker",
                "created_at", "version"
            }}
            payload_json = json.dumps(payload, ensure_ascii=False)
            verdict = "pass"
            verdict_note = f"[FORCE PASS by {operator}] {note}"

            cur = conn.execute("""
                UPDATE tasks SET
                    stage_verdict = ?,
                    stage_verdict_note = ?,
                    payload_json = ?,
                    updated_at = ?,
                    version = COALESCE(version, 0) + 1
                WHERE task_id = ? AND (? IS NULL OR version = ?);
            """, (verdict, verdict_note, payload_json, now, tid, exp_v, exp_v))

            if cur.rowcount == 0:
                raise RuntimeError(
                    f"任务 {tid} 版本已在写入边界发生变化（期望版本 {exp_v}），写入已拒绝，请刷新页面"
                )
            updated_tasks.append(tid)

        meta = json.loads(wf_row["metadata_json"] or "{}")
        cfg = json.loads(wf_row["config_json"] or "{}")
        wf_dict = dict(meta)
        wf_dict.update({
            "workflow_id": wf_row["workflow_id"],
            "title": wf_row["title"],
            "status": wf_row["status"],
            "template_name": wf_row["template_name"],
            "current_stage": wf_row["current_stage"],
            "config": cfg,
            "created_at": wf_row["created_at"],
            "updated_at": now,
        })
        overrides = dict(wf_dict.get("gate_overrides") or {})
        history_overrides = list(wf_dict.get("gate_overrides_history") or [])
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

        wf_dict["gate_overrides"] = overrides
        wf_dict["gate_overrides_history"] = history_overrides
        _facade().save_workflow(wf_dict, db_path=None, conn=conn)

        if should_close:
            conn.execute("COMMIT;")

        return {
            "ok": True,
            "workflow_id": workflow_id,
            "gate_node_id": gate_node_id,
            "updated_tasks": updated_tasks,
        }
    except Exception:
        if should_close:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
        raise
    finally:
        if should_close:
            conn.close()
