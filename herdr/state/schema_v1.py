"""schema v1 persistence; extracted without changing behavior."""

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional
from ._facade import _facade
from .constants import _INITIALIZED_DBS


def migrate_v1_to_v2(
    workflows_file: Optional[Path] = None,
    tasks_file: Optional[Path] = None,
    checkpoints_dir: Optional[Path] = None,
    steering_file: Optional[Path] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Lossless migration of V1 JSON registries and checkpoint files into SQLite."""
    wf_path = workflows_file or (_facade().CONTROLLER_DIR / "workflows.json")
    tasks_path = tasks_file or (_facade().CONTROLLER_DIR / "tasks.json")
    cp_path = checkpoints_dir or (_facade().CONTROLLER_DIR / "checkpoints")
    st_path = steering_file or (_facade().CONTROLLER_DIR / "steering.json")
    db = _facade().init_db(db_path)

    migrated_wfs = 0
    migrated_tasks = 0
    migrated_cps = 0
    migrated_steers = 0

    # 1. Migrate workflows
    if wf_path.exists():
        try:
            with open(wf_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for wid, wf_obj in data.get("workflows", {}).items():
                wf_obj.setdefault("workflow_id", wid)
                _facade().save_workflow(wf_obj, db)
                migrated_wfs += 1
        except Exception:
            pass

    # 2. Migrate tasks
    if tasks_path.exists():
        try:
            with open(tasks_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for t_obj in data.get("tasks", []):
                if t_obj.get("task_id") and t_obj.get("workflow_id"):
                    _facade().save_task(t_obj, db)
                    migrated_tasks += 1
        except Exception:
            pass

    # 3. Migrate checkpoints
    if cp_path.exists():
        for f in cp_path.rglob("cp_*.json"):
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    snap = json.load(fp)
                cpid = snap.get("checkpoint_id")
                wid = snap.get("workflow_id")
                if not cpid or not wid:
                    continue

                conn = _facade().get_db_connection(db)
                try:
                    conn.execute("""
                        INSERT INTO checkpoints (
                            checkpoint_id, workflow_id, tag, parent_checkpoint_id,
                            created_at, workflow_status, task_count, snapshot_json, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(checkpoint_id) DO NOTHING;
                    """, (
                        cpid,
                        wid,
                        snap.get("tag", ""),
                        snap.get("parent_checkpoint_id"),
                        float(snap.get("created_at") or time.time()),
                        snap.get("workflow", {}).get("status", "pending"),
                        len(snap.get("tasks", [])),
                        json.dumps(snap, ensure_ascii=False),
                        json.dumps(snap.get("metadata", {}), ensure_ascii=False),
                    ))
                    migrated_cps += 1
                finally:
                    conn.close()
            except Exception:
                continue

    # 4. Migrate steering
    if st_path.exists():
        try:
            with open(st_path, "r", encoding="utf-8") as f:
                s_data = json.load(f)
            for tid, q in s_data.get("steering_queues", {}).items():
                for s_item in q:
                    s_item.setdefault("task_id", tid)
                    _facade().save_steer(s_item, db)
                    migrated_steers += 1
            for h in s_data.get("history", []):
                _facade().record_steering_history(h, db)
        except Exception:
            pass

    return {
        "ok": True,
        "db_path": str(db),
        "migrated_workflows": migrated_wfs,
        "migrated_tasks": migrated_tasks,
        "migrated_checkpoints": migrated_cps,
        "migrated_steers": migrated_steers,
    }

def _bootstrap_v1(conn, path_key):
    # One-time atomic bootstrap migration if initializing a DB where legacy JSON exists and not yet completed
    cur = conn.execute("SELECT value FROM schema_meta WHERE key = 'v1_migration_done';")
    if not cur.fetchone():
        # Check if the database already contains existing runtime state (e.g. upgraded from PR #21
        # where SQLite was in active use before schema_meta was introduced).
        # If runtime state already exists, SQLite is authoritative and we MUST NOT import legacy JSON,
        # otherwise stale JSON could overwrite newer SQLite state on upgrade.
        has_existing_state = bool(conn.execute("""
            SELECT (
                EXISTS(SELECT 1 FROM workflows LIMIT 1) OR
                EXISTS(SELECT 1 FROM tasks LIMIT 1) OR
                EXISTS(SELECT 1 FROM checkpoints LIMIT 1) OR
                EXISTS(SELECT 1 FROM steering_items LIMIT 1) OR
                EXISTS(SELECT 1 FROM events LIMIT 1)
            );
        """).fetchone()[0])

        if has_existing_state:
            conn.execute("""
                INSERT INTO schema_meta (key, value) VALUES ('v1_migration_done', '1')
                ON CONFLICT(key) DO UPDATE SET value = '1';
            """)
            _INITIALIZED_DBS.add(path_key)
        else:
            target_path = Path(path_key)
            target_dir = target_path.parent
            wf_file = target_dir / "workflows.json"
            st_file = target_dir / "steering.json"
            if os.environ.get("CHECKPOINTS_DIR"):
                cp_dir = Path(os.environ["CHECKPOINTS_DIR"])
            else:
                cp_dir = target_dir / "checkpoints"

            # If a dedicated companion json exists for this DB file (e.g. /tmp/xyz.db -> /tmp/xyz.json)
            if target_path.name != "state.db" and target_path.with_suffix(".json").exists():
                tasks_file = target_path.with_suffix(".json")
            else:
                tasks_file = target_dir / "tasks.json"

            has_legacy = (
                wf_file.exists()
                or tasks_file.exists()
                or st_file.exists()
                or (cp_dir.exists() and any(cp_dir.rglob("*.json")))
            )
            if not has_legacy:
                conn.execute("""
                    INSERT INTO schema_meta (key, value) VALUES ('v1_migration_done', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                """)
                _INITIALIZED_DBS.add(path_key)
            else:
                try:
                    conn.execute("BEGIN TRANSACTION;")

                    if wf_file.exists():
                        data = json.loads(wf_file.read_text(encoding="utf-8"))
                        wfs_data = data.get("workflows", {})
                        if isinstance(wfs_data, dict):
                            for wid, wf_obj in wfs_data.items():
                                if isinstance(wf_obj, dict):
                                    wf_obj.setdefault("workflow_id", wid)
                                    _facade().save_workflow(wf_obj, db_path=None, conn=conn)
                        elif isinstance(wfs_data, list):
                            for wf_obj in wfs_data:
                                if isinstance(wf_obj, dict) and wf_obj.get("workflow_id"):
                                    _facade().save_workflow(wf_obj, db_path=None, conn=conn)

                    if tasks_file.exists():
                        data = json.loads(tasks_file.read_text(encoding="utf-8"))
                        t_list = data.get("tasks", [])
                        if isinstance(t_list, list):
                            for t_obj in t_list:
                                if isinstance(t_obj, dict) and t_obj.get("task_id"):
                                    _facade().save_task(t_obj, db_path=None, conn=conn)

                    if st_file.exists():
                        s_data = json.loads(st_file.read_text(encoding="utf-8"))
                        q_dict = s_data.get("steering_queues", {})
                        if isinstance(q_dict, dict):
                            for tid, q in q_dict.items():
                                if isinstance(q, list):
                                    for s_item in q:
                                        s_item.setdefault("task_id", tid)
                                        _facade().save_steer(s_item, conn=conn)
                        h_list = s_data.get("history", [])
                        if isinstance(h_list, list):
                            for h in h_list:
                                _facade().record_steering_history(h, conn=conn)

                    if cp_dir.exists():
                        for f in sorted(cp_dir.rglob("*.json")):
                            if f.is_file():
                                snap = json.loads(f.read_text(encoding="utf-8"))
                                cpid = snap.get("checkpoint_id")
                                wid = snap.get("workflow_id")
                                if not cpid or not wid:
                                    continue
                                cur_wf = conn.execute("SELECT 1 FROM workflows WHERE workflow_id = ?", (wid,))
                                if not cur_wf.fetchone():
                                    _facade().save_workflow(
                                        snap.get("workflow") or {"workflow_id": wid, "title": wid, "status": "pending"},
                                        db_path=None,
                                        conn=conn,
                                    )
                                conn.execute("""
                                    INSERT INTO checkpoints (
                                        checkpoint_id, workflow_id, tag, parent_checkpoint_id,
                                        created_at, workflow_status, task_count, snapshot_json, metadata_json
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    ON CONFLICT(checkpoint_id) DO NOTHING;
                                """, (
                                    cpid,
                                    wid,
                                    snap.get("tag", ""),
                                    snap.get("parent_checkpoint_id"),
                                    float(snap.get("created_at") or time.time()),
                                    snap.get("workflow", {}).get("status", "pending"),
                                    len(snap.get("tasks", [])),
                                    json.dumps(snap, ensure_ascii=False),
                                    json.dumps(snap.get("metadata", {}), ensure_ascii=False),
                                ))

                    conn.execute("""
                        INSERT INTO schema_meta (key, value) VALUES ('v1_migration_done', '1')
                        ON CONFLICT(key) DO UPDATE SET value = '1';
                    """)
                    conn.execute("COMMIT;")
                    _INITIALIZED_DBS.add(path_key)
                except Exception:
                    try:
                        conn.execute("ROLLBACK;")
                    except Exception:
                        pass
                    raise
    else:
        _INITIALIZED_DBS.add(path_key)
