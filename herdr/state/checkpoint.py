"""checkpoint persistence; extracted without changing behavior."""

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from ._facade import _facade


def create_checkpoint(
    workflow_id: str,
    tag: Optional[str] = None,
    parent_checkpoint_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    db_path: Optional[Path] = None,
    checkpoint_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Capture an atomic point-in-time snapshot of a workflow and all its tasks."""
    conn = _facade().get_db_connection(db_path)
    try:
        cur_wf = conn.execute("SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,))
        wf_row = cur_wf.fetchone()
        if not wf_row:
            raise ValueError(f"Workflow '{workflow_id}' not found in state DB")

        cur_tasks = conn.execute("SELECT * FROM tasks WHERE workflow_id = ? ORDER BY created_at ASC", (workflow_id,))
        task_rows = cur_tasks.fetchall()

        now = time.time()
        ts_str = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
        short_uuid = uuid.uuid4().hex[:6]
        cp_id = checkpoint_id or f"cp_{workflow_id}_{ts_str}_{short_uuid}"

        # Reconstruct structured objects
        wf_dict = json.loads(wf_row["metadata_json"] or "{}")
        wf_dict.update({
            "workflow_id": wf_row["workflow_id"],
            "title": wf_row["title"],
            "status": wf_row["status"],
            "template_name": wf_row["template_name"],
            "current_stage": wf_row["current_stage"],
            "config": json.loads(wf_row["config_json"] or "{}"),
            "created_at": wf_row["created_at"],
            "updated_at": wf_row["updated_at"],
        })

        tasks_list = []
        for r in task_rows:
            t_obj = json.loads(r["payload_json"] or "{}")
            t_obj.update({
                "task_id": r["task_id"],
                "workflow_id": r["workflow_id"],
                "node": r["node"],
                "stage": r["stage"],
                "agent": r["agent"],
                "status": r["status"],
                "stage_verdict": r["stage_verdict"],
                "stage_verdict_note": r["stage_verdict_note"],
                "pane_id": r["pane_id"],
                "goal": r["goal"],
                "blocker": r["blocker"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            })
            tasks_list.append(t_obj)

        snapshot_payload = {
            "checkpoint_id": cp_id,
            "workflow_id": workflow_id,
            "tag": tag or "",
            "parent_checkpoint_id": parent_checkpoint_id,
            "created_at": now,
            "workflow": wf_dict,
            "tasks": tasks_list,
            "metadata": metadata or {},
        }

        # Atomic insertion within a single transaction
        conn.execute("BEGIN TRANSACTION;")
        try:
            conn.execute("""
                INSERT INTO checkpoints (
                    checkpoint_id, workflow_id, tag, parent_checkpoint_id,
                    created_at, workflow_status, task_count, snapshot_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                cp_id,
                workflow_id,
                tag or "",
                parent_checkpoint_id,
                now,
                wf_row["status"],
                len(tasks_list),
                json.dumps(snapshot_payload, ensure_ascii=False),
                json.dumps(metadata or {}, ensure_ascii=False),
            ))

            _facade().record_event({
                "workflow_id": workflow_id,
                "event_type": "checkpoint_created",
                "timestamp": now,
                "payload": {"checkpoint_id": cp_id, "tag": tag},
                "source": "checkpoint_store",
            }, conn=conn)
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise

        return {
            "ok": True,
            "workflow_id": workflow_id,
            "checkpoint_id": cp_id,
            "tag": tag or "",
            "parent_checkpoint_id": parent_checkpoint_id,
            "created_at": now,
            "task_count": len(tasks_list),
            "workflow_status": wf_row["status"],
        }
    finally:
        conn.close()

def list_checkpoints(workflow_id: str, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """List checkpoints for a workflow sorted newest first."""
    conn = _facade().get_db_connection(db_path)
    try:
        cur = conn.execute("""
            SELECT checkpoint_id, workflow_id, tag, parent_checkpoint_id,
                   created_at, workflow_status, task_count, metadata_json
            FROM checkpoints
            WHERE workflow_id = ?
            ORDER BY created_at DESC;
        """, (workflow_id,))

        results = []
        for row in cur.fetchall():
            results.append({
                "checkpoint_id": row["checkpoint_id"],
                "workflow_id": row["workflow_id"],
                "tag": row["tag"],
                "parent_checkpoint_id": row["parent_checkpoint_id"],
                "created_at": row["created_at"],
                "workflow_status": row["workflow_status"],
                "task_count": row["task_count"],
                "metadata": json.loads(row["metadata_json"] or "{}"),
            })
        return results
    finally:
        conn.close()

def get_checkpoint(workflow_id: str, checkpoint_id: str, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Retrieve full snapshot payload from database."""
    conn = _facade().get_db_connection(db_path)
    try:
        cur = conn.execute("""
            SELECT snapshot_json FROM checkpoints
            WHERE workflow_id = ? AND checkpoint_id = ?;
        """, (workflow_id, checkpoint_id))
        row = cur.fetchone()
        if not row:
            raise FileNotFoundError(f"Checkpoint '{checkpoint_id}' not found for workflow '{workflow_id}'")
        return json.loads(row["snapshot_json"])
    finally:
        conn.close()

def restore_checkpoint(
    workflow_id: str,
    checkpoint_id: str,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Restore workflow state and tasks atomically from a checkpoint snapshot in SQLite."""
    snapshot = get_checkpoint(workflow_id, checkpoint_id, db_path)
    restored_wf = snapshot.get("workflow")
    restored_tasks = snapshot.get("tasks", [])

    if not restored_wf:
        raise ValueError(f"Checkpoint '{checkpoint_id}' contains no workflow data")

    conn = _facade().get_db_connection(db_path)
    try:
        conn.execute("BEGIN TRANSACTION;")
        try:
            # 1. Restore workflow record
            _facade().save_workflow(restored_wf, db_path, conn=conn)

            # 2. Delete existing tasks for this workflow and insert restored tasks
            conn.execute("DELETE FROM tasks WHERE workflow_id = ?;", (workflow_id,))
            for t in restored_tasks:
                _facade().save_task(t, db_path, conn=conn)

            # 3. Record event
            _facade().record_event({
                "workflow_id": workflow_id,
                "event_type": "checkpoint_restored",
                "payload": {"checkpoint_id": checkpoint_id},
                "source": "checkpoint_store",
            }, conn=conn)

            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise

        return {
            "ok": True,
            "workflow_id": workflow_id,
            "checkpoint_id": checkpoint_id,
            "restored_tasks": len(restored_tasks),
            "status": restored_wf.get("status"),
        }
    finally:
        conn.close()

def fork_workflow_from_checkpoint(
    checkpoint_id: str,
    new_workflow_id: str,
    new_title: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Time-travel branching: Fork a new workflow instance from a historical checkpoint."""
    conn = _facade().get_db_connection(db_path)
    try:
        cur = conn.execute("SELECT * FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,))
        cp_row = cur.fetchone()
        if not cp_row:
            raise FileNotFoundError(f"Source checkpoint '{checkpoint_id}' not found")

        snapshot = json.loads(cp_row["snapshot_json"])
        source_wf = snapshot.get("workflow", {})
        source_tasks = snapshot.get("tasks", [])

        now = time.time()
        forked_wf = dict(source_wf)
        forked_wf["workflow_id"] = new_workflow_id
        forked_wf["title"] = new_title or f"{source_wf.get('title', 'Workflow')} (Forked from {checkpoint_id[:12]})"
        forked_wf["created_at"] = now
        forked_wf["updated_at"] = now
        forked_wf["forked_from"] = {
            "source_workflow_id": cp_row["workflow_id"],
            "source_checkpoint_id": checkpoint_id,
            "forked_at": now,
        }

        # Clone and remap tasks
        forked_tasks = []
        for t in source_tasks:
            t_clone = dict(t)
            orig_tid = t.get("task_id", "")
            t_clone["task_id"] = f"{orig_tid}-fork-{uuid.uuid4().hex[:6]}"
            t_clone["workflow_id"] = new_workflow_id
            t_clone["created_at"] = now
            forked_tasks.append(t_clone)

        conn.execute("BEGIN TRANSACTION;")
        try:
            _facade().save_workflow(forked_wf, db_path, conn=conn)
            for t in forked_tasks:
                _facade().save_task(t, db_path, conn=conn)

            _facade().record_event({
                "workflow_id": new_workflow_id,
                "event_type": "workflow_forked",
                "timestamp": now,
                "payload": {
                    "source_checkpoint_id": checkpoint_id,
                    "source_workflow_id": cp_row["workflow_id"],
                },
                "source": "checkpoint_store",
            }, conn=conn)

            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise

        return {
            "ok": True,
            "new_workflow_id": new_workflow_id,
            "new_title": forked_wf["title"],
            "source_checkpoint_id": checkpoint_id,
            "source_workflow_id": cp_row["workflow_id"],
            "cloned_tasks": len(forked_tasks),
        }
    finally:
        conn.close()

def get_checkpoint_lineage(workflow_id: str, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Retrieve the DAG lineage of checkpoints for a workflow."""
    cps = list_checkpoints(workflow_id, db_path)
    by_id = {c["checkpoint_id"]: c for c in cps}

    lineage_tree = []
    for c in cps:
        parent_id = c.get("parent_checkpoint_id")
        lineage_tree.append({
            "checkpoint_id": c["checkpoint_id"],
            "tag": c.get("tag"),
            "created_at": c["created_at"],
            "parent_id": parent_id,
            "has_parent": bool(parent_id and parent_id in by_id),
            "task_count": c["task_count"],
        })
    return lineage_tree
