#!/usr/bin/env python3
"""Herdr Checkpoint Store V2: SQLite Embedded State Engine.

Provides durable, transactional state management and time-travel snapshots
using the Python standard library `sqlite3` (Zero external dependencies).

Core Capabilities:
- WAL mode (Write-Ahead Logging) for safe concurrent reads & single-writer transactions
- Single-transaction atomic snapshot capture across workflows and tasks
- Fast indexed snapshot listing without directory scanning
- Time-travel state forking (branching a new workflow from any historical checkpoint)
- Parent-child checkpoint DAG lineage tracking
- Lossless bi-directional migration between V1 JSON files and V2 SQLite DB
"""

import json
import hashlib
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from herdr.transitions import (
    ACTIVE_TASK_STATUSES,
    COMPLETED_TASK_STATUSES,
    InvalidTransitionError,
    validate_task_transition,
    validate_workflow_transition,
)


HOME = Path.home()
CONTROLLER_DIR = HOME / ".herdr-controller"


def get_default_db_path() -> Path:
    """Resolve active SQLite DB path from environment or default location."""
    env_path = os.environ.get("HERDR_STATE_DB")
    if env_path:
        return Path(env_path)
    if os.environ.get("CHECKPOINTS_DIR"):
        return Path(os.environ["CHECKPOINTS_DIR"]).parent / "state.db"
    if os.environ.get("WORKFLOWS_FILE"):
        return Path(os.environ["WORKFLOWS_FILE"]).parent / "state.db"
    if os.environ.get("TASKS_FILE"):
        p = Path(os.environ["TASKS_FILE"])
        return p.parent / "state.db" if p.name == "tasks.json" else p.with_suffix(".db")
    return CONTROLLER_DIR / "state.db"



_INITIALIZED_DBS: set = set()


def _ensure_schema(conn: sqlite3.Connection, path_key: str) -> None:
    """Execute table and index creation DDL once per database path."""
    if path_key in _INITIALIZED_DBS:
        return

    conn.execute("""
        CREATE TABLE IF NOT EXISTS workflows (
            workflow_id TEXT PRIMARY KEY,
            title TEXT,
            status TEXT,
            template_name TEXT,
            current_stage TEXT,
            config_json TEXT,
            metadata_json TEXT,
            created_at REAL,
            updated_at REAL
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            workflow_id TEXT,
            node TEXT,
            stage TEXT,
            agent TEXT,
            status TEXT,
            stage_verdict TEXT,
            stage_verdict_note TEXT,
            pane_id TEXT,
            goal TEXT,
            blocker TEXT,
            payload_json TEXT,
            created_at REAL,
            updated_at REAL,
            FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE CASCADE
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            workflow_id TEXT,
            tag TEXT,
            parent_checkpoint_id TEXT,
            created_at REAL,
            workflow_status TEXT,
            task_count INTEGER,
            snapshot_json TEXT,
            metadata_json TEXT,
            FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE CASCADE
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id TEXT,
            node_id TEXT,
            task_id TEXT,
            agent_id TEXT,
            event_type TEXT,
            payload_json TEXT,
            timestamp REAL,
            source TEXT,
            run_id TEXT,
            sequence INTEGER
        );
    """)

    # Observer analysis (NOT facts): findings are judgments derived from the
    # trajectory/events ledger and must stay queryable apart from it.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trajectory_findings (
            finding_id TEXT PRIMARY KEY,
            finding_key TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL,
            task_id TEXT,
            workflow_id TEXT,
            node TEXT,
            agent TEXT,
            agent_session_id TEXT,
            finding_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            summary TEXT,
            suspected_cause TEXT,
            recommended_action TEXT,
            confidence REAL,
            evidence_json TEXT,
            metadata_json TEXT,
            created_at REAL
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS observations (
            observation_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            task_id TEXT,
            workflow_id TEXT,
            source_type TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            content_ref TEXT NOT NULL,
            media_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            excerpt TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            UNIQUE(run_id, source_type, source_ref, sha256)
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS observation_receipts (
            observation_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            task_id TEXT,
            workflow_id TEXT,
            created_at REAL NOT NULL
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS context_packs (
            context_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            task_id TEXT,
            workflow_id TEXT,
            goal TEXT,
            current_state_json TEXT NOT NULL DEFAULT '{}',
            completed_json TEXT NOT NULL DEFAULT '[]',
            verified_facts_json TEXT NOT NULL DEFAULT '[]',
            important_findings_json TEXT NOT NULL DEFAULT '[]',
            evidence_refs_json TEXT NOT NULL DEFAULT '[]',
            artifact_refs_json TEXT NOT NULL DEFAULT '[]',
            open_issues_json TEXT NOT NULL DEFAULT '[]',
            next_focus_json TEXT NOT NULL DEFAULT '[]',
            source_event_sequence INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS steering_items (
            steer_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            workflow_id TEXT,
            instruction TEXT NOT NULL,
            operator TEXT,
            urgent INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            dispatched_at REAL,
            created_at REAL,
            payload_json TEXT
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS steering_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            task_id TEXT,
            steer_id TEXT,
            instruction TEXT,
            operator TEXT,
            urgent INTEGER DEFAULT 0,
            reason TEXT,
            timestamp REAL,
            payload_json TEXT
        );
    """)

    # Indexes for fast lookup and DAG queries
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_wf ON tasks(workflow_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cp_wf_created ON checkpoints(workflow_id, created_at DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cp_parent ON checkpoints(parent_checkpoint_id);")
    _ensure_event_columns(conn)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_wf ON events(workflow_id, timestamp);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_node ON events(node_id, timestamp);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, timestamp);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_id, timestamp);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, timestamp);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_source ON events(source, timestamp);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_run_sequence ON events(run_id, sequence, id);")
    for row in conn.execute(
        "SELECT run_id, task_id, workflow_id, timestamp, payload_json "
        "FROM events WHERE source = 'trajectory' AND event_type = 'observation_created'"
    ).fetchall():
        try:
            observation_id = (json.loads(row["payload_json"] or "{}").get("observation") or {}).get("observation_id")
        except (TypeError, json.JSONDecodeError):
            observation_id = None
        if observation_id:
            conn.execute(
                "INSERT OR IGNORE INTO observation_receipts "
                "(observation_id, run_id, task_id, workflow_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (observation_id, row["run_id"], row["task_id"], row["workflow_id"], row["timestamp"] or time.time()),
            )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_findings_run ON trajectory_findings(run_id, created_at DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_findings_type ON trajectory_findings(finding_type, created_at DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_observations_run ON observations(run_id, created_at, observation_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_observations_task ON observations(task_id, created_at, observation_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_observations_type ON observations(source_type, created_at, observation_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_observations_sha256 ON observations(sha256);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_context_packs_run_created ON context_packs(run_id, created_at DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_context_packs_task_created ON context_packs(task_id, created_at DESC);")
    # Deduplication is fingerprint-based, not sequence-only: a Finding or task
    # state can change without a new Trajectory sequence.
    conn.execute("DROP INDEX IF EXISTS ux_context_packs_run_sequence;")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_context_packs_run_sequence ON context_packs(run_id, source_event_sequence);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_steering_task ON steering_items(task_id, status);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_steering_hist_task ON steering_history(task_id, timestamp);")

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
                                    save_workflow(wf_obj, db_path=None, conn=conn)
                        elif isinstance(wfs_data, list):
                            for wf_obj in wfs_data:
                                if isinstance(wf_obj, dict) and wf_obj.get("workflow_id"):
                                    save_workflow(wf_obj, db_path=None, conn=conn)

                    if tasks_file.exists():
                        data = json.loads(tasks_file.read_text(encoding="utf-8"))
                        t_list = data.get("tasks", [])
                        if isinstance(t_list, list):
                            for t_obj in t_list:
                                if isinstance(t_obj, dict) and t_obj.get("task_id"):
                                    save_task(t_obj, db_path=None, conn=conn)

                    if st_file.exists():
                        s_data = json.loads(st_file.read_text(encoding="utf-8"))
                        q_dict = s_data.get("steering_queues", {})
                        if isinstance(q_dict, dict):
                            for tid, q in q_dict.items():
                                if isinstance(q, list):
                                    for s_item in q:
                                        s_item.setdefault("task_id", tid)
                                        save_steer(s_item, conn=conn)
                        h_list = s_data.get("history", [])
                        if isinstance(h_list, list):
                            for h in h_list:
                                record_steering_history(h, conn=conn)

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
                                    save_workflow(
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


def _ensure_event_columns(conn: sqlite3.Connection) -> None:
    """Upgrade pre-WorkflowEvent event tables in place."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(events);")}
    column_types = {
        "node_id": "TEXT",
        "agent_id": "TEXT",
        "source": "TEXT",
        "run_id": "TEXT",
        "sequence": "INTEGER",
    }
    for name in ("node_id", "agent_id", "source", "run_id", "sequence"):
        if name not in columns:
            try:
                conn.execute(f"ALTER TABLE events ADD COLUMN {name} {column_types[name]};")
            except sqlite3.OperationalError as exc:
                # Another process may have won the schema-upgrade race after
                # this connection's PRAGMA snapshot. Only accept that exact
                # race after confirming the target column now exists.
                if "duplicate column name" not in str(exc).lower():
                    raise
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(events);")}
                if name not in columns:
                    raise
            columns.add(name)


def get_db_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Create a thread-safe connection to the SQLite state database with WAL mode."""
    path = db_path or get_default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    
    conn = sqlite3.connect(
        str(path),
        timeout=10.0,
        isolation_level=None,  # autocommit mode; we manage transactions explicitly
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    
    # Configure high-concurrency PRAGMAs
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")

    path_key = str(path.resolve())
    try:
        _ensure_schema(conn, path_key)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise
    return conn


def init_db(db_path: Optional[Path] = None) -> Path:
    """Initialize database tables and indexes if they do not exist."""
    path = db_path or get_default_db_path()
    # Force initialization even if cached
    _INITIALIZED_DBS.discard(str(path.resolve()))
    conn = get_db_connection(path)
    conn.close()
    return path



def save_workflow(
    wf_dict: Dict[str, Any],
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Upsert a workflow record."""
    should_close = False
    if conn is None:
        conn = get_db_connection(db_path)
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
    conn = get_db_connection(db_path)
    try:
        cur = conn.execute("SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,))
        row = cur.fetchone()
        if not row:
            return None

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
            "updated_at": row["updated_at"],
        })
        return wf
    finally:
        conn.close()


def list_workflows(
    status: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Fetch all workflows, optionally filtered by status."""
    conn = get_db_connection(db_path)
    try:
        if status:
            cur = conn.execute("SELECT * FROM workflows WHERE status = ? ORDER BY created_at DESC", (status,))
        else:
            cur = conn.execute("SELECT * FROM workflows ORDER BY created_at DESC")
        results = []
        for row in cur.fetchall():
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
                "updated_at": row["updated_at"],
            })
            results.append(wf)
        return results
    finally:
        conn.close()


def delete_workflow(workflow_id: str, db_path: Optional[Path] = None) -> bool:
    """Delete a workflow and cascade its tasks/checkpoints."""
    conn = get_db_connection(db_path)
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
) -> None:
    """Upsert a task record."""
    should_close = False
    if conn is None:
        conn = get_db_connection(db_path)
        should_close = True

    tid = task_dict.get("task_id")
    if not tid:
        raise ValueError("task_id is required")
    wid = task_dict.get("workflow_id") or "default"

    # Auto-ensure parent workflow exists to prevent foreign key violation
    cur_wf = conn.execute("SELECT 1 FROM workflows WHERE workflow_id = ?", (wid,))
    if not cur_wf.fetchone():
        save_workflow({"workflow_id": wid, "title": wid, "status": "pending"}, db_path, conn=conn)

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
        "stage_verdict", "stage_verdict_note", "pane_id", "goal", "blocker", "created_at"
    }}
    payload_json = json.dumps(payload, ensure_ascii=False)

    try:
        conn.execute("""
            INSERT INTO tasks (task_id, workflow_id, node, stage, agent, status, stage_verdict, stage_verdict_note, pane_id, goal, blocker, payload_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                updated_at=excluded.updated_at;
        """, (tid, wid, node, stage, agent, status, verdict, verdict_note, pane_id, goal, blocker, payload_json, created_at, now))
    finally:
        if should_close:
            conn.close()


def get_task(task_id: str, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Fetch a single task by its task_id."""
    conn = get_db_connection(db_path)
    try:
        cur = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = cur.fetchone()
        if not row:
            return None
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
        })
        return t
    finally:
        conn.close()


def get_tasks(workflow_id: str, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Fetch all tasks for a workflow."""
    return list_tasks(workflow_id=workflow_id, db_path=db_path)


def list_tasks(
    workflow_id: Optional[str] = None,
    status: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Fetch tasks optionally filtered by workflow_id and/or status."""
    conn = get_db_connection(db_path)
    try:
        query = "SELECT * FROM tasks WHERE 1=1"
        params: List[Any] = []
        if workflow_id:
            query += " AND workflow_id = ?"
            params.append(workflow_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at ASC"
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
            })
            tasks.append(t)
        return tasks
    finally:
        conn.close()


def delete_task(task_id: str, db_path: Optional[Path] = None) -> bool:
    """Delete a task by its task_id."""
    conn = get_db_connection(db_path)
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


def save_steer(
    steer_dict: Dict[str, Any],
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Upsert a steering item."""
    should_close = False
    if conn is None:
        conn = get_db_connection(db_path)
        should_close = True

    sid = steer_dict.get("steer_id")
    tid = steer_dict.get("task_id")
    if not sid or not tid:
        raise ValueError("steer_id and task_id are required")

    wid = steer_dict.get("workflow_id")
    instruction = steer_dict.get("instruction", "")
    operator = steer_dict.get("operator", "human")
    urgent = 1 if steer_dict.get("urgent") else 0
    status = steer_dict.get("status", "pending")
    dispatched_at = steer_dict.get("dispatched_at")
    created_at = float(steer_dict.get("created_at") or time.time())

    payload = {k: v for k, v in steer_dict.items() if k not in {
        "steer_id", "task_id", "workflow_id", "instruction", "operator",
        "urgent", "status", "dispatched_at", "created_at"
    }}
    payload_json = json.dumps(payload, ensure_ascii=False)

    try:
        conn.execute("""
            INSERT INTO steering_items (
                steer_id, task_id, workflow_id, instruction, operator,
                urgent, status, dispatched_at, created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(steer_id) DO UPDATE SET
                task_id=excluded.task_id,
                workflow_id=excluded.workflow_id,
                instruction=excluded.instruction,
                operator=excluded.operator,
                urgent=excluded.urgent,
                status=excluded.status,
                dispatched_at=excluded.dispatched_at,
                payload_json=excluded.payload_json;
        """, (sid, tid, wid, instruction, operator, urgent, status, dispatched_at, created_at, payload_json))
    finally:
        if should_close:
            conn.close()


def get_steer(steer_id: str, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Fetch a steering item by its steer_id."""
    conn = get_db_connection(db_path)
    try:
        cur = conn.execute("SELECT * FROM steering_items WHERE steer_id = ?", (steer_id,))
        row = cur.fetchone()
        if not row:
            return None
        payload = json.loads(row["payload_json"] or "{}")
        item = dict(payload)
        item.update({
            "steer_id": row["steer_id"],
            "task_id": row["task_id"],
            "workflow_id": row["workflow_id"],
            "instruction": row["instruction"],
            "operator": row["operator"],
            "urgent": bool(row["urgent"]),
            "status": row["status"],
            "dispatched_at": row["dispatched_at"],
            "created_at": row["created_at"],
        })
        return item
    finally:
        conn.close()


def list_steers(
    task_id: Optional[str] = None,
    status: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """List steering items optionally filtered by task_id and/or status."""
    conn = get_db_connection(db_path)
    try:
        query = "SELECT * FROM steering_items WHERE 1=1"
        params: List[Any] = []
        if task_id:
            query += " AND task_id = ?"
            params.append(task_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at ASC"
        cur = conn.execute(query, tuple(params))
        items = []
        for row in cur.fetchall():
            payload = json.loads(row["payload_json"] or "{}")
            item = dict(payload)
            item.update({
                "steer_id": row["steer_id"],
                "task_id": row["task_id"],
                "workflow_id": row["workflow_id"],
                "instruction": row["instruction"],
                "operator": row["operator"],
                "urgent": bool(row["urgent"]),
                "status": row["status"],
                "dispatched_at": row["dispatched_at"],
                "created_at": row["created_at"],
            })
            items.append(item)
        return items
    finally:
        conn.close()


def update_steer_status(
    steer_id: str,
    status: str,
    dispatched_at: Optional[float] = None,
    db_path: Optional[Path] = None,
) -> bool:
    """Update status and dispatched_at for a steering item."""
    conn = get_db_connection(db_path)
    try:
        conn.execute("BEGIN TRANSACTION;")
        if dispatched_at is not None:
            cur = conn.execute(
                "UPDATE steering_items SET status = ?, dispatched_at = ? WHERE steer_id = ?",
                (status, dispatched_at, steer_id),
            )
        else:
            cur = conn.execute(
                "UPDATE steering_items SET status = ? WHERE steer_id = ?",
                (status, steer_id),
            )
        updated = cur.rowcount > 0
        conn.execute("COMMIT;")
        return updated
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    finally:
        conn.close()


def record_steering_history(
    record: Dict[str, Any],
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Record a steering audit/action entry."""
    should_close = False
    if conn is None:
        conn = get_db_connection(db_path)
        should_close = True
    try:
        if should_close:
            conn.execute("BEGIN TRANSACTION;")
        now = float(record.get("timestamp") or time.time())
        action = record.get("action", "unknown")
        task_id = record.get("task_id")
        steer_id = record.get("steer_id")
        instruction = record.get("instruction")
        operator = record.get("operator")
        urgent = 1 if record.get("urgent") else 0
        reason = record.get("reason")
        payload = {k: v for k, v in record.items() if k not in {
            "action", "task_id", "steer_id", "instruction", "operator", "urgent", "reason", "timestamp"
        }}
        payload_json = json.dumps(payload, ensure_ascii=False)

        conn.execute("""
            INSERT INTO steering_history (
                action, task_id, steer_id, instruction, operator, urgent, reason, timestamp, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (action, task_id, steer_id, instruction, operator, urgent, reason, now, payload_json))
        record_event({
            "workflow_id": record.get("workflow_id"),
            "node_id": record.get("node_id") or record.get("node"),
            "task_id": task_id,
            "agent_id": record.get("agent_id") or record.get("agent"),
            "event_type": f"steering.{action}",
            "timestamp": now,
            "payload": {
                "steer_id": steer_id,
                "instruction": instruction,
                "operator": operator,
                "urgent": bool(urgent),
                "reason": reason,
                **payload,
            },
            "source": record.get("source") or "steering",
        }, conn=conn)
        if should_close:
            conn.execute("COMMIT;")
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


def list_steering_history(
    task_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """List steering history entries ordered chronologically."""
    conn = get_db_connection(db_path)
    try:
        if task_id:
            cur = conn.execute("SELECT * FROM steering_history WHERE task_id = ? ORDER BY timestamp ASC", (task_id,))
        else:
            cur = conn.execute("SELECT * FROM steering_history ORDER BY timestamp ASC")
        results = []
        for row in cur.fetchall():
            payload = json.loads(row["payload_json"] or "{}")
            item = dict(payload)
            item.update({
                "action": row["action"],
                "task_id": row["task_id"],
                "steer_id": row["steer_id"],
                "instruction": row["instruction"],
                "operator": row["operator"],
                "urgent": bool(row["urgent"]),
                "reason": row["reason"],
                "timestamp": row["timestamp"],
            })
            results.append(item)
        return results
    finally:
        conn.close()


def record_event(
    event: Dict[str, Any],
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Append a canonical WorkflowEvent and return the normalized event."""
    should_close = False
    if conn is None:
        conn = get_db_connection(db_path)
        should_close = True

    try:
        event_type = event.get("event_type")
        if not event_type:
            raise ValueError("event_type is required")

        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dict")

        normalized = {
            "workflow_id": event.get("workflow_id"),
            "node_id": event.get("node_id") or event.get("node"),
            "task_id": event.get("task_id"),
            "agent_id": event.get("agent_id") or event.get("agent"),
            "event_type": event_type,
            "timestamp": float(event["timestamp"]) if event.get("timestamp") is not None else time.time(),
            "payload": payload,
            "source": event.get("source") or "system",
        }

        cur = conn.execute("""
            INSERT INTO events (
                workflow_id, node_id, task_id, agent_id,
                event_type, payload_json, timestamp, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            normalized["workflow_id"],
            normalized["node_id"],
            normalized["task_id"],
            normalized["agent_id"],
            normalized["event_type"],
            json.dumps(normalized["payload"], ensure_ascii=False),
            normalized["timestamp"],
            normalized["source"],
        ))
        normalized["id"] = cur.lastrowid
        return normalized
    finally:
        if should_close:
            conn.close()


def list_events(
    workflow_id: Optional[str] = None,
    node_id: Optional[str] = None,
    task_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    event_type: Optional[str] = None,
    source: Optional[str] = None,
    limit: Optional[int] = None,
    db_path: Optional[Path] = None,
    desc: bool = False,
) -> List[Dict[str, Any]]:
    """List WorkflowEvents in chronological order with optional filters.

    ``desc=True`` returns newest-first and makes ``limit`` select the newest
    N events (the default ASC + LIMIT would return the oldest N).
    """
    conn = get_db_connection(db_path)
    try:
        query = "SELECT * FROM events WHERE 1=1"
        params: List[Any] = []
        # Keep the legacy WorkflowEvent API behavior stable: trajectory rows
        # are queried through list_trajectory_events, unless explicitly asked
        # for source="trajectory".
        if source == "trajectory":
            query += " AND source = 'trajectory'"
        elif source is None:
            query += " AND (source IS NULL OR source != 'trajectory')"
        for column, value in (
            ("workflow_id", workflow_id),
            ("node_id", node_id),
            ("task_id", task_id),
            ("agent_id", agent_id),
            ("event_type", event_type),
            ("source", source),
        ):
            if value is not None:
                query += f" AND {column} = ?"
                params.append(value)
        direction = "DESC" if desc else "ASC"
        query += f" ORDER BY timestamp {direction}, id {direction}"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))

        results = []
        for row in conn.execute(query, params).fetchall():
            results.append({
                "id": row["id"],
                "workflow_id": row["workflow_id"],
                "node_id": row["node_id"],
                "task_id": row["task_id"],
                "agent_id": row["agent_id"],
                "event_type": row["event_type"],
                "timestamp": row["timestamp"],
                "payload": json.loads(row["payload_json"] or "{}"),
                "source": row["source"] or "unknown",
            })
        return results
    finally:
        conn.close()


def record_trajectory_event(
    event: Dict[str, Any],
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Append one trajectory event with a transaction-local run sequence.

    This is deliberately separate from the legacy WorkflowEvent API. Both use
    the same SQLite event store, but trajectory rows carry the run/sequence
    identity needed for deterministic replay.
    """
    run_id = event.get("run_id")
    event_type = event.get("event_type")
    if not run_id:
        raise ValueError("run_id is required")
    if not event_type:
        raise ValueError("event_type is required")

    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")

    conn = get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        sequence = conn.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1
            FROM events
            WHERE run_id = ? AND source = 'trajectory'
            """,
            (run_id,),
        ).fetchone()[0]
        timestamp = float(event["timestamp"]) if event.get("timestamp") is not None else time.time()
        cur = conn.execute(
            """
            INSERT INTO events (
                workflow_id, node_id, task_id, agent_id,
                event_type, payload_json, timestamp, source,
                run_id, sequence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'trajectory', ?, ?)
            """,
            (
                event.get("workflow_id"),
                event.get("node_id") or event.get("node"),
                event.get("task_id"),
                event.get("agent_id") or event.get("agent"),
                event_type,
                json.dumps(payload, ensure_ascii=False),
                timestamp,
                run_id,
                sequence,
            ),
        )
        conn.execute("COMMIT;")
        return {
            "id": cur.lastrowid,
            "workflow_id": event.get("workflow_id"),
            "node_id": event.get("node_id") or event.get("node"),
            "task_id": event.get("task_id"),
            "agent_id": event.get("agent_id") or event.get("agent"),
            "event_type": event_type,
            "timestamp": timestamp,
            "source": "trajectory",
            "run_id": run_id,
            "sequence": sequence,
            "payload": payload,
        }
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def record_observation_receipt(
    event: Dict[str, Any],
    observation_id: str,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Atomically append at most one trajectory receipt for an Observation."""
    if not observation_id:
        raise ValueError("observation_id is required")
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    run_id = event.get("run_id")
    if not run_id:
        raise ValueError("run_id is required")

    conn = get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        timestamp = float(event["timestamp"]) if event.get("timestamp") is not None else time.time()
        inserted = conn.execute(
            """
            INSERT OR IGNORE INTO observation_receipts
                (observation_id, run_id, task_id, workflow_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (observation_id, run_id, event.get("task_id"), event.get("workflow_id"), timestamp),
        ).rowcount
        if not inserted:
            conn.execute("COMMIT;")
            return None
        sequence = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE run_id = ? AND source = 'trajectory'",
            (run_id,),
        ).fetchone()[0]
        cur = conn.execute(
            """
            INSERT INTO events (
                workflow_id, node_id, task_id, agent_id,
                event_type, payload_json, timestamp, source, run_id, sequence
            ) VALUES (?, ?, ?, ?, 'observation_created', ?, ?, 'trajectory', ?, ?)
            """,
            (
                event.get("workflow_id"), event.get("node_id") or event.get("node"),
                event.get("task_id"), event.get("agent_id") or event.get("agent"),
                json.dumps(payload, ensure_ascii=False), timestamp, run_id, sequence,
            ),
        )
        conn.execute("COMMIT;")
        return {
            "id": cur.lastrowid,
            "workflow_id": event.get("workflow_id"),
            "node_id": event.get("node_id") or event.get("node"),
            "task_id": event.get("task_id"),
            "agent_id": event.get("agent_id") or event.get("agent"),
            "event_type": "observation_created",
            "timestamp": timestamp,
            "source": "trajectory",
            "run_id": run_id,
            "sequence": sequence,
            "payload": payload,
        }
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def list_trajectory_events(
    run_id: str,
    event_type: Optional[str] = None,
    task_id: Optional[str] = None,
    limit: Optional[int] = None,
    desc: bool = False,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Read trajectory rows with optional newest-first bounded selection."""
    conn = get_db_connection(db_path)
    try:
        query = "SELECT * FROM events WHERE run_id = ? AND source = 'trajectory'"
        params: List[Any] = [run_id]
        if event_type is not None:
            query += " AND event_type = ?"
            params.append(event_type)
        if task_id is not None:
            query += " AND task_id = ?"
            params.append(task_id)
        direction = "DESC" if desc else "ASC"
        query += f" ORDER BY sequence {direction}, id {direction}"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        results = []
        for row in conn.execute(query, params).fetchall():
            results.append({
                "id": row["id"],
                "workflow_id": row["workflow_id"],
                "node_id": row["node_id"],
                "task_id": row["task_id"],
                "agent_id": row["agent_id"],
                "event_type": row["event_type"],
                "timestamp": row["timestamp"],
                "source": row["source"],
                "run_id": row["run_id"],
                "sequence": row["sequence"],
                "payload": json.loads(row["payload_json"] or "{}"),
            })
        return results
    finally:
        conn.close()


def latest_trajectory_sequence(run_id: str, db_path: Optional[Path] = None) -> int:
    """Read only the run watermark without loading its event history."""
    conn = get_db_connection(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM events WHERE run_id = ? AND source = 'trajectory'",
            (run_id,),
        ).fetchone()
        return int(row["sequence"] or 0)
    finally:
        conn.close()


def trajectory_event_exists(event_id: str, run_id: Optional[str] = None, db_path: Optional[Path] = None) -> bool:
    """Check one event identity without loading the trajectory window."""
    try:
        row_id = int(str(event_id).removeprefix("evt_"))
    except ValueError:
        return False
    conn = get_db_connection(db_path)
    try:
        query = "SELECT 1 FROM events WHERE id = ? AND source = 'trajectory'"
        params: List[Any] = [row_id]
        if run_id is not None:
            query += " AND run_id = ?"
            params.append(run_id)
        return conn.execute(query, params).fetchone() is not None
    finally:
        conn.close()


def artifact_ref_exists(ref: str, run_id: str, db_path: Optional[Path] = None) -> bool:
    """Check an artifact reference in trajectory payloads without full history materialization."""
    conn = get_db_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT payload_json FROM events
               WHERE run_id = ? AND source = 'trajectory'
                 AND event_type = 'artifact_created'
                 AND (payload_json LIKE '%"ref"%' OR payload_json LIKE '%"path"%')""",
            (run_id,),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"] or "{}")
            artifact = payload.get("artifact") or {}
            if str(artifact.get("ref") or artifact.get("path") or "") == str(ref):
                return True
        return False
    finally:
        conn.close()


def _decode_finding_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "finding_id": row["finding_id"],
        "finding_key": row["finding_key"],
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "workflow_id": row["workflow_id"],
        "node": row["node"],
        "agent": row["agent"],
        "agent_session_id": row["agent_session_id"],
        "finding_type": row["finding_type"],
        "severity": row["severity"],
        "status": row["status"] or "open",
        "summary": row["summary"],
        "suspected_cause": row["suspected_cause"],
        "recommended_action": row["recommended_action"],
        "confidence": row["confidence"],
        "evidence": json.loads(row["evidence_json"] or "[]"),
        "metadata": json.loads(row["metadata_json"] or "{}"),
        "created_at": row["created_at"],
    }


def upsert_trajectory_finding(
    finding: Dict[str, Any],
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Record/refresh one analysis finding and return the canonical persisted row.

    Findings are deliberately separated from trajectory events: the events
    ledger records what happened, this table records what HAFlow suspects it
    means. ``finding_key`` is the dedup contract (UNIQUE):

    - first observation inserts the row;
    - later observations of the SAME episode (same key) refresh the analysis
      fields in place (severity/summary/evidence/... escalating as the chain
      grows) while ``finding_id``/``created_at`` stay canonical;
    - a concurrent observer that lost the insert race re-reads and returns the
      canonical row, so every caller converges on one ``finding_id``;
    - a lower-severity observation never downgrades a stored row.

    Returns None only when the row cannot be read back at all.
    """
    finding_key = finding.get("finding_key")
    finding_id = finding.get("finding_id")
    run_id = finding.get("run_id")
    finding_type = finding.get("finding_type")
    if not finding_key or not finding_id or not run_id or not finding_type:
        raise ValueError("finding_id, finding_key, run_id and finding_type are required")

    conn = get_db_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO trajectory_findings (
                finding_id, finding_key, run_id, task_id, workflow_id, node,
                agent, agent_session_id, finding_type, severity, status,
                summary, suspected_cause, recommended_action, confidence,
                evidence_json, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(finding_key) DO UPDATE SET
                task_id = excluded.task_id,
                workflow_id = excluded.workflow_id,
                node = excluded.node,
                agent = excluded.agent,
                agent_session_id = excluded.agent_session_id,
                severity = excluded.severity,
                status = excluded.status,
                summary = excluded.summary,
                suspected_cause = excluded.suspected_cause,
                recommended_action = excluded.recommended_action,
                confidence = excluded.confidence,
                evidence_json = excluded.evidence_json,
                metadata_json = excluded.metadata_json
            WHERE CASE excluded.severity
                      WHEN 'critical' THEN 3 WHEN 'warning' THEN 2 ELSE 1 END
                  >= CASE trajectory_findings.severity
                      WHEN 'critical' THEN 3 WHEN 'warning' THEN 2 ELSE 1 END;
            """,
            (
                finding_id,
                finding_key,
                run_id,
                finding.get("task_id"),
                finding.get("workflow_id"),
                finding.get("node"),
                finding.get("agent"),
                finding.get("agent_session_id"),
                finding_type,
                finding.get("severity") or "warning",
                finding.get("status") or "open",
                finding.get("summary"),
                finding.get("suspected_cause"),
                finding.get("recommended_action"),
                float(finding.get("confidence") or 0.0),
                json.dumps(finding.get("evidence") or [], ensure_ascii=False),
                json.dumps(finding.get("metadata") or {}, ensure_ascii=False),
                float(finding["created_at"]) if finding.get("created_at") is not None else time.time(),
            ),
        )
        # Whether this call inserted, updated, or lost a race, the canonical
        # row is the single answer every caller must return.
        row = conn.execute(
            "SELECT * FROM trajectory_findings WHERE finding_key = ?", (finding_key,)
        ).fetchone()
        return _decode_finding_row(row) if row is not None else None
    finally:
        conn.close()


def get_trajectory_finding(
    finding_key: str,
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch one finding by its dedup key (None when absent)."""
    should_close = False
    if conn is None:
        conn = get_db_connection(db_path)
        should_close = True
    try:
        row = conn.execute(
            "SELECT * FROM trajectory_findings WHERE finding_key = ?", (finding_key,)
        ).fetchone()
        return _decode_finding_row(row) if row is not None else None
    finally:
        if should_close:
            conn.close()


def get_trajectory_finding_by_id(
    finding_id: str,
    run_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch one finding by its public identity for reference verification."""
    conn = get_db_connection(db_path)
    try:
        query = "SELECT * FROM trajectory_findings WHERE finding_id = ?"
        params: List[Any] = [finding_id]
        if run_id is not None:
            query += " AND run_id = ?"
            params.append(run_id)
        row = conn.execute(query, params).fetchone()
        return _decode_finding_row(row) if row is not None else None
    finally:
        conn.close()


def list_trajectory_findings(
    run_id: Optional[str] = None,
    finding_type: Optional[str] = None,
    limit: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """List analysis findings in stable creation order."""
    conn = get_db_connection(db_path)
    try:
        query = "SELECT * FROM trajectory_findings WHERE 1=1"
        params: List[Any] = []
        if run_id is not None:
            query += " AND run_id = ?"
            params.append(run_id)
        if finding_type is not None:
            query += " AND finding_type = ?"
            params.append(finding_type)
        query += " ORDER BY created_at ASC, rowid ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        return [_decode_finding_row(row) for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def list_trajectory_findings_bounded(
    run_id: str,
    limit: int,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Return the most important recent findings with SQL-side bounding."""
    conn = get_db_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT * FROM trajectory_findings
               WHERE run_id = ?
               ORDER BY CASE severity
                          WHEN 'critical' THEN 3
                          WHEN 'warning' THEN 2
                          ELSE 1
                        END DESC,
                        created_at DESC, rowid DESC
               LIMIT ?""",
            (run_id, int(limit)),
        ).fetchall()
        return [_decode_finding_row(row) for row in rows]
    finally:
        conn.close()


def _finding_source_fingerprint(findings: List[Dict[str, Any]]) -> str:
    payload = json.dumps(findings, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _context_source_version_in_conn(
    conn: sqlite3.Connection,
    run_id: str,
    task_id: Optional[str],
    finding_limit: int,
) -> Dict[str, Any]:
    sequence_row = conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM events WHERE run_id = ? AND source = 'trajectory'",
        (run_id,),
    ).fetchone()
    task_updated_at = None
    if task_id:
        task_row = conn.execute(
            "SELECT updated_at FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if task_row is not None:
            task_updated_at = task_row["updated_at"]
    finding_rows = conn.execute(
        """SELECT * FROM trajectory_findings
           WHERE run_id = ?
           ORDER BY CASE severity
                      WHEN 'critical' THEN 3
                      WHEN 'warning' THEN 2
                      ELSE 1
                    END DESC,
                    created_at DESC, rowid DESC
           LIMIT ?""",
        (run_id, int(finding_limit)),
    ).fetchall()
    findings = [_decode_finding_row(row) for row in finding_rows]
    return {
        "task_id": task_id,
        "trajectory_sequence": int(sequence_row["sequence"] or 0),
        "task_updated_at": task_updated_at,
        "finding_fingerprint": _finding_source_fingerprint(findings),
        "finding_limit": int(finding_limit),
    }


def get_context_source_version(
    run_id: str,
    task_id: Optional[str],
    finding_limit: int,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Read the compact source watermark/revisions from one SQLite snapshot."""
    conn = get_db_connection(db_path)
    try:
        return _context_source_version_in_conn(conn, run_id, task_id, finding_limit)
    finally:
        conn.close()


def _decode_observation_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "observation_id": row["observation_id"],
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "workflow_id": row["workflow_id"],
        "source_type": row["source_type"],
        "source_ref": row["source_ref"],
        "content_ref": row["content_ref"],
        "media_type": row["media_type"],
        "size_bytes": row["size_bytes"],
        "sha256": row["sha256"],
        "excerpt": row["excerpt"],
        "metadata": json.loads(row["metadata_json"] or "{}"),
        "created_at": row["created_at"],
    }


def insert_observation(
    observation: Dict[str, Any],
    *,
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Insert immutable Observation metadata and return the inserted row."""
    should_close = conn is None
    conn = conn or get_db_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO observations (
                observation_id, run_id, task_id, workflow_id, source_type,
                source_ref, content_ref, media_type, size_bytes, sha256,
                excerpt, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation["observation_id"],
                observation["run_id"],
                observation.get("task_id"),
                observation.get("workflow_id"),
                observation["source_type"],
                observation["source_ref"],
                observation["content_ref"],
                observation["media_type"],
                int(observation["size_bytes"]),
                observation["sha256"],
                observation.get("excerpt"),
                json.dumps(observation.get("metadata") or {}, ensure_ascii=False),
                float(observation["created_at"]),
            ),
        )
        row = conn.execute(
            "SELECT * FROM observations WHERE observation_id = ?",
            (observation["observation_id"],),
        ).fetchone()
        if row is None:
            raise RuntimeError("observation insert was not readable")
        return _decode_observation_row(row)
    finally:
        if should_close:
            conn.close()


def get_observation(
    observation_id: str,
    *,
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """Read one immutable Observation metadata row."""
    should_close = conn is None
    conn = conn or get_db_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM observations WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()
        return _decode_observation_row(row) if row is not None else None
    finally:
        if should_close:
            conn.close()


def find_observation_by_dedup(
    run_id: str,
    source_type: str,
    source_ref: str,
    sha256: str,
    *,
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """Find the canonical row for an Observation deduplication key."""
    should_close = conn is None
    conn = conn or get_db_connection(db_path)
    try:
        row = conn.execute(
            """
            SELECT * FROM observations
            WHERE run_id = ? AND source_type = ? AND source_ref = ? AND sha256 = ?
            """,
            (run_id, source_type, source_ref, sha256),
        ).fetchone()
        return _decode_observation_row(row) if row is not None else None
    finally:
        if should_close:
            conn.close()


def list_observations(
    *,
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
    source_type: Optional[str] = None,
    limit: Optional[int] = None,
    desc: bool = False,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """List Observation metadata with access-pattern-aligned filters."""
    conn = get_db_connection(db_path)
    try:
        query = "SELECT * FROM observations WHERE 1=1"
        params: List[Any] = []
        for column, value in (("run_id", run_id), ("task_id", task_id), ("source_type", source_type)):
            if value is not None:
                query += f" AND {column} = ?"
                params.append(value)
        direction = "DESC" if desc else "ASC"
        query += f" ORDER BY created_at {direction}, observation_id {direction}"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        return [_decode_observation_row(row) for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def _decode_context_pack_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "context_id": row["context_id"],
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "workflow_id": row["workflow_id"],
        "goal": row["goal"],
        "current_state": json.loads(row["current_state_json"] or "{}"),
        "completed": json.loads(row["completed_json"] or "[]"),
        "verified_facts": json.loads(row["verified_facts_json"] or "[]"),
        "important_findings": json.loads(row["important_findings_json"] or "[]"),
        "evidence_refs": json.loads(row["evidence_refs_json"] or "[]"),
        "artifact_refs": json.loads(row["artifact_refs_json"] or "[]"),
        "open_issues": json.loads(row["open_issues_json"] or "[]"),
        "next_focus": json.loads(row["next_focus_json"] or "[]"),
        "source_event_sequence": int(row["source_event_sequence"] or 0),
        "metadata": json.loads(row["metadata_json"] or "{}"),
        "created_at": float(row["created_at"]),
    }


def save_context_pack(
    context_pack: Dict[str, Any],
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Append one ContextPack, returning the existing pack on exact fingerprint dedup."""
    required = ("context_id", "run_id")
    if any(not context_pack.get(key) for key in required):
        raise ValueError("context_id and run_id are required")
    conn = get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        fingerprint = (context_pack.get("metadata") or {}).get("context_source_fingerprint")
        source_version = (context_pack.get("metadata") or {}).get("context_source_version")
        source_version_valid = False
        latest = conn.execute(
            """SELECT * FROM context_packs
               WHERE run_id = ?
               ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (context_pack["run_id"],),
        ).fetchone()
        if isinstance(source_version, dict):
            current_source_version = _context_source_version_in_conn(
                conn,
                context_pack["run_id"],
                source_version.get("task_id"),
                int(source_version.get("finding_limit") or 0),
            )
            if current_source_version != source_version:
                conn.commit()
                if latest is not None:
                    return _decode_context_pack_row(latest)
                raise ValueError("stale ContextPack source version")
            source_version_valid = True
        if latest is not None:
            existing_metadata = json.loads(latest["metadata_json"] or "{}")
            if fingerprint and existing_metadata.get("context_source_fingerprint") == fingerprint:
                conn.commit()
                return _decode_context_pack_row(latest)
            # Compact requests may finish out of order.  The request start
            # timestamp is the snapshot's logical ordering key, so a late
            # older request must never become the latest snapshot.  A later
            # A->B->A request still has a newer timestamp and is appended.
            candidate_created_at = float(context_pack.get("created_at") or time.time())
            latest_created_at = float(latest["created_at"] or 0.0)
            if not source_version_valid and latest_created_at > candidate_created_at:
                conn.commit()
                return _decode_context_pack_row(latest)
        insert_created_at = float(context_pack.get("created_at") or time.time())
        if source_version_valid and latest is not None:
            # Source-version validation, rather than request start time, is
            # authoritative for a candidate that was read after waiting for
            # the writer lock.  Make that accepted snapshot the SQL latest.
            insert_created_at = max(insert_created_at, float(latest["created_at"] or 0.0) + 1e-9)
        conn.execute(
            """INSERT INTO context_packs (
                context_id, run_id, task_id, workflow_id, goal,
                current_state_json, completed_json, verified_facts_json,
                important_findings_json, evidence_refs_json, artifact_refs_json,
                open_issues_json, next_focus_json, source_event_sequence,
                metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                context_pack["context_id"], context_pack["run_id"],
                context_pack.get("task_id"), context_pack.get("workflow_id"), context_pack.get("goal"),
                json.dumps(context_pack.get("current_state") or {}, ensure_ascii=False),
                json.dumps(context_pack.get("completed") or [], ensure_ascii=False),
                json.dumps(context_pack.get("verified_facts") or [], ensure_ascii=False),
                json.dumps(context_pack.get("important_findings") or [], ensure_ascii=False),
                json.dumps(context_pack.get("evidence_refs") or [], ensure_ascii=False),
                json.dumps(context_pack.get("artifact_refs") or [], ensure_ascii=False),
                json.dumps(context_pack.get("open_issues") or [], ensure_ascii=False),
                json.dumps(context_pack.get("next_focus") or [], ensure_ascii=False),
                int(context_pack.get("source_event_sequence") or 0),
                json.dumps(context_pack.get("metadata") or {}, ensure_ascii=False),
                insert_created_at,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM context_packs WHERE context_id = ?", (context_pack["context_id"],)
        ).fetchone()
        if row is None:
            raise RuntimeError("context pack insert was not readable")
        return _decode_context_pack_row(row)
    finally:
        conn.close()


def get_context_pack(
    context_id: str,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    conn = get_db_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM context_packs WHERE context_id = ?", (context_id,)).fetchone()
        return _decode_context_pack_row(row) if row is not None else None
    finally:
        conn.close()


def get_latest_context_pack(
    run_id: str,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    conn = get_db_connection(db_path)
    try:
        row = conn.execute(
            """SELECT * FROM context_packs WHERE run_id = ?
               ORDER BY created_at DESC, rowid DESC LIMIT 1""", (run_id,)
        ).fetchone()
        return _decode_context_pack_row(row) if row is not None else None
    finally:
        conn.close()


def list_context_packs(
    run_id: str,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    conn = get_db_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT * FROM context_packs WHERE run_id = ?
               ORDER BY created_at ASC, rowid ASC""", (run_id,)
        ).fetchall()
        return [_decode_context_pack_row(row) for row in rows]
    finally:
        conn.close()


PROTECTED_TASK_METADATA_FIELDS = {
    "task_id",
    "workflow_id",
    "run_id",
    "node",
    "stage",
    "agent",
    "status",
    "created_at",
    "updated_at",
}

PROTECTED_WORKFLOW_METADATA_FIELDS = {
    "workflow_id",
    "status",
    "created_at",
    "updated_at",
}

RESERVED_EVENT_METADATA_FIELDS = {
    "from",
    "to",
    "from_status",
    "to_status",
    "reason",
    "source",
    "timestamp",
    "forced",
}


def transition_task(
    task_id: str,
    to_status: str,
    reason: str,
    source: str = "system",
    metadata: Optional[Dict[str, Any]] = None,
    force: bool = False,
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Atomically validate and transition a task status, appending a canonical WorkflowEvent."""
    should_close = False
    if conn is None:
        conn = get_db_connection(db_path)
        should_close = True

    try:
        if should_close:
            conn.execute("BEGIN IMMEDIATE;")

        cur = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Task '{task_id}' not found")

        old_status = row["status"]
        validate_task_transition(old_status, to_status, force=force)

        meta = dict(metadata or {})
        forbidden = set(meta.keys()) & PROTECTED_TASK_METADATA_FIELDS
        if forbidden:
            raise ValueError(f"Cannot overwrite protected task fields via metadata: {sorted(forbidden)}")
        forbidden_event = set(meta.keys()) & RESERVED_EVENT_METADATA_FIELDS
        if forbidden_event:
            raise ValueError(f"Cannot overwrite reserved event fields via metadata: {sorted(forbidden_event)}")

        now = time.time()
        payload = json.loads(row["payload_json"] or "{}")
        task_dict = dict(payload)
        task_dict.update({
            "task_id": row["task_id"],
            "workflow_id": row["workflow_id"],
            "node": row["node"],
            "stage": row["stage"],
            "agent": row["agent"],
            "status": old_status,
            "stage_verdict": row["stage_verdict"],
            "stage_verdict_note": row["stage_verdict_note"],
            "pane_id": row["pane_id"],
            "goal": row["goal"],
            "blocker": row["blocker"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })

        if force:
            task_dict["forced"] = True
        for k, v in meta.items():
            task_dict[k] = v

        task_dict["status"] = to_status
        task_dict["updated_at"] = now
        task_dict["last_activity_at"] = now

        if to_status in ACTIVE_TASK_STATUSES and not task_dict.get("started_at"):
            task_dict["started_at"] = now

        if to_status in COMPLETED_TASK_STATUSES and not task_dict.get("last_result"):
            task_dict["last_result"] = to_status

        # Append to status_history in payload (meta unpacked first, canonical fields last)
        status_history = list(task_dict.get("status_history") or [])
        status_history_entry = {
            **meta,
            "from": old_status,
            "to": to_status,
            "reason": reason,
            "source": source,
            "timestamp": now,
        }
        if force:
            status_history_entry["forced"] = True
        status_history.append(status_history_entry)
        task_dict["status_history"] = status_history

        save_task(task_dict, db_path=None, conn=conn)

        event_payload = {
            **meta,
            "from_status": old_status,
            "to_status": to_status,
            "reason": reason,
            "forced": force,
        }
        event = record_event(
            {
                "workflow_id": task_dict.get("workflow_id"),
                "node_id": task_dict.get("node") or task_dict.get("stage"),
                "task_id": task_id,
                "agent_id": task_dict.get("agent"),
                "event_type": "task_transition",
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
            "task_id": task_id,
            "workflow_id": task_dict.get("workflow_id"),
            "old_status": old_status,
            "new_status": to_status,
            "reason": reason,
            "source": source,
            "event_id": event.get("id"),
            "task": task_dict,
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


def transition_workflow(
    workflow_id: str,
    to_status: str,
    reason: str,
    source: str = "system",
    metadata: Optional[Dict[str, Any]] = None,
    force: bool = False,
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Atomically validate and transition a workflow status, appending a canonical WorkflowEvent."""
    should_close = False
    if conn is None:
        conn = get_db_connection(db_path)
        should_close = True

    try:
        if should_close:
            conn.execute("BEGIN IMMEDIATE;")

        cur = conn.execute("SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Workflow '{workflow_id}' not found")

        old_status = row["status"]
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

        save_workflow(wf_dict, db_path=None, conn=conn)

        event_payload = {
            **user_meta,
            "from_status": old_status,
            "to_status": to_status,
            "reason": reason,
            "forced": force,
        }
        event = record_event(
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


def update_task_metadata(
    task_id: str,
    updates: Dict[str, Any],
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Atomically update non-protected metadata fields of a task without touching status or identity."""
    forbidden = set(updates.keys()) & PROTECTED_TASK_METADATA_FIELDS
    if forbidden:
        raise ValueError(f"Cannot update protected task fields via metadata update: {sorted(forbidden)}")

    should_close = False
    if conn is None:
        conn = get_db_connection(db_path)
        should_close = True

    try:
        if should_close:
            conn.execute("BEGIN IMMEDIATE;")

        cur = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Task '{task_id}' not found")

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

        save_task(t, db_path=None, conn=conn)

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
) -> Dict[str, Any]:
    """Atomically update non-protected metadata fields of a workflow without touching status or identity."""
    forbidden = set(updates.keys()) & PROTECTED_WORKFLOW_METADATA_FIELDS
    if forbidden:
        raise ValueError(f"Cannot update protected workflow fields via metadata update: {sorted(forbidden)}")

    should_close = False
    if conn is None:
        conn = get_db_connection(db_path)
        should_close = True

    try:
        if should_close:
            conn.execute("BEGIN IMMEDIATE;")

        cur = conn.execute("SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Workflow '{workflow_id}' not found")

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

        save_workflow(wf, db_path=None, conn=conn)

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


def create_checkpoint(
    workflow_id: str,
    tag: Optional[str] = None,
    parent_checkpoint_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    db_path: Optional[Path] = None,
    checkpoint_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Capture an atomic point-in-time snapshot of a workflow and all its tasks."""
    conn = get_db_connection(db_path)
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

            record_event({
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
    conn = get_db_connection(db_path)
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
    conn = get_db_connection(db_path)
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

    conn = get_db_connection(db_path)
    try:
        conn.execute("BEGIN TRANSACTION;")
        try:
            # 1. Restore workflow record
            save_workflow(restored_wf, db_path, conn=conn)

            # 2. Delete existing tasks for this workflow and insert restored tasks
            conn.execute("DELETE FROM tasks WHERE workflow_id = ?;", (workflow_id,))
            for t in restored_tasks:
                save_task(t, db_path, conn=conn)

            # 3. Record event
            record_event({
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
    conn = get_db_connection(db_path)
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
            save_workflow(forked_wf, db_path, conn=conn)
            for t in forked_tasks:
                save_task(t, db_path, conn=conn)

            record_event({
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


def migrate_v1_to_v2(
    workflows_file: Optional[Path] = None,
    tasks_file: Optional[Path] = None,
    checkpoints_dir: Optional[Path] = None,
    steering_file: Optional[Path] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Lossless migration of V1 JSON registries and checkpoint files into SQLite."""
    wf_path = workflows_file or (CONTROLLER_DIR / "workflows.json")
    tasks_path = tasks_file or (CONTROLLER_DIR / "tasks.json")
    cp_path = checkpoints_dir or (CONTROLLER_DIR / "checkpoints")
    st_path = steering_file or (CONTROLLER_DIR / "steering.json")
    db = init_db(db_path)

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
                save_workflow(wf_obj, db)
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
                    save_task(t_obj, db)
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

                conn = get_db_connection(db)
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
                    save_steer(s_item, db)
                    migrated_steers += 1
            for h in s_data.get("history", []):
                record_steering_history(h, db)
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
