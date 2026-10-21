"""connection persistence; extracted without changing behavior."""

import os
import re
import sqlite3
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from ._facade import _facade
from .constants import SHADOW_READ_CAPABILITY, _INITIALIZED_DBS, _OUTCOME_COLUMN_NAMES


def _db_path_for_tasks_file(value: str) -> Path:
    """Derive DB path from a TASKS_FILE value (mirrors ``bin/herdr-task``)."""
    p = Path(value)
    return p.parent / "state.db" if p.name == "tasks.json" else p.with_suffix(".db")

def _db_path_for_workflow_file(value: str) -> Path:
    """Derive DB path from a WORKFLOW(S)_FILE value (mirrors ``bin/herdr-task``)."""
    p = Path(value)
    return (
        p.parent / "state.db"
        if p.name in ("workflows.json", "workflow.json")
        else p.with_suffix(".db")
    )

def get_default_db_path() -> Path:
    """Resolve active SQLite DB path from environment or default location.

    Production global: order is frozen (HERDR_STATE_DB >
    CHECKPOINTS_DIR > WORKFLOWS_FILE > TASKS_FILE > default). Shadow
    Evaluation compatibility (singular ``WORKFLOW_FILE``) lives only in
    ``resolve_state_db_path`` and must never reorder this function:
    ``get_state_store`` and every other production caller depend on it.
    """
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
    return _facade().CONTROLLER_DIR / "state.db"

def get_db_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Create a thread-safe connection to the SQLite state database with WAL mode."""
    path = db_path or _facade().get_default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        str(path),
        timeout=10.0,
        isolation_level=None,  # autocommit mode; we manage transactions explicitly
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row

    path_key = str(path.resolve())
    schema_lock = (
        nullcontext()
        if path_key in _INITIALIZED_DBS
        else _facade()._schema_initialization_lock(Path(path_key))
    )
    try:
        with schema_lock:
            # Configure high-concurrency PRAGMAs while first-time schema
            # initialization is serialized across processes.
            conn.execute("PRAGMA busy_timeout=10000;")
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            _facade()._ensure_schema(conn, path_key)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise
    return conn

def init_db(db_path: Optional[Path] = None) -> Path:
    """Initialize database tables and indexes if they do not exist."""
    path = db_path or _facade().get_default_db_path()
    # Force initialization even if cached
    _INITIALIZED_DBS.discard(str(path.resolve()))
    conn = _facade().get_db_connection(path)
    conn.close()
    return path

class ReadonlySchemaError(RuntimeError):
    """The existing database cannot serve a read-only diagnostic read.

    Raised instead of migrating: a read-only tool observes state but
    never repairs it, so a missing table/column fails cleanly instead of
    triggering ``_ensure_schema`` DDL.
    """

def resolve_state_db_path(db_path: Optional[Path] = None) -> Path:
    """Resolve the state DB path for Shadow Evaluation (read-only).

    Shadow-only resolver: location semantics replicate
    ``bin/herdr-task._get_store`` (HERDR_STATE_DB > TASKS_FILE >
    WORKFLOW_FILE/WORKFLOWS_FILE > CHECKPOINTS_DIR > default) as pure
    path arithmetic -- no ``get_state_store()``, no ``init_db``, no
    mkdir, no file creation. Production callers keep using
    ``get_default_db_path`` with its frozen order.
    """
    if db_path is not None:
        return Path(db_path)
    env_path = os.environ.get("HERDR_STATE_DB")
    if env_path:
        return Path(env_path)
    tasks_file = os.environ.get("TASKS_FILE")
    if tasks_file:
        return _db_path_for_tasks_file(tasks_file)
    workflow_file = os.environ.get("WORKFLOW_FILE") or os.environ.get("WORKFLOWS_FILE")
    if workflow_file:
        return _db_path_for_workflow_file(workflow_file)
    if os.environ.get("CHECKPOINTS_DIR"):
        return Path(os.environ["CHECKPOINTS_DIR"]).parent / "state.db"
    return _facade().CONTROLLER_DIR / "state.db"

def get_readonly_db_connection(
    db_path: Optional[Path] = None,
) -> sqlite3.Connection:
    """Open an existing state DB strictly read-only (diagnostics only).

    Independent entry point: it deliberately does not delegate to
    ``get_db_connection``, which mkdir's the parent, creates the file,
    switches the journal to WAL and runs ``_ensure_schema`` DDL /
    migrations before returning. "No business writes" is not the same
    as "no side effects".

    Read-only means exactly: no HAFlow persistent state changes. The
    file must already exist (``mode=ro`` never creates it), plus
    ``PRAGMA query_only=ON`` so a future statement cannot write through
    this connection either. No WAL reconfiguration, no ``.schema.lock``
    sidecar, no schema init.

    SQLite's own ``-wal``/``-shm`` coordination files are deliberately
    out of scope: opening a WAL-mode DB whose sidecars are absent lets
    SQLite materialize them for the connection's lifetime (removed again
    on a clean last-connection close). They are transient OS-level WAL
    coordination, not application data, and any pre-open existence check
    would be a TOCTOU race anyway -- so this contract tolerates them by
    design instead of attempting to govern them.
    """
    path = resolve_state_db_path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"state database not found: {path}")
    conn = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=10.0,
        isolation_level=None,  # autocommit mode; reads need no transactions
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    try:
        # mode=ro is the file-level guarantee; query_only adds the
        # statement-level guard against future accidental writes.
        conn.execute("PRAGMA query_only=ON;")
        conn.execute("PRAGMA busy_timeout=10000;")
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise
    return conn


#: Read-only capability contract for Shadow Evaluation: every table and
#: column the evaluation's bounded reads bind against. Checked with pure
#: SELECT/PRAGMA introspection only -- never with DDL.
#:
#: NOTE: outcome SELECT column names live in ``_OUTCOME_COLUMN_NAMES``
#: (derived from ``_OUTCOME_COLUMNS``) further below; resolution happens
#: at use time in ``_resolve_shadow_read_capability`` so the check can
#: never drift from what shadow reads actually bind against.

def _resolve_shadow_read_capability() -> Dict[str, Tuple[str, ...]]:
    """Capability contract with outcome columns bound to the SELECT list.

    ``_OUTCOME_COLUMN_NAMES`` is defined further below in this module,
    so the names resolve on first use after import completes.
    """
    capability = dict(SHADOW_READ_CAPABILITY)
    capability["agent_execution_outcomes"] = _OUTCOME_COLUMN_NAMES
    return capability

def assert_shadow_read_capability(conn: sqlite3.Connection) -> None:
    """Verify the open DB can serve shadow evaluation, without migrating.

    SELECT-only (``sqlite_master`` + ``PRAGMA table_info``): a legacy DB
    missing a table or column raises ``ReadonlySchemaError`` so the CLI
    fails cleanly instead of auto-creating or ALTER-ing anything.
    """
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    missing: List[str] = []
    capability = _resolve_shadow_read_capability()
    for table, columns in capability.items():
        if table not in tables:
            missing.append(table)
            continue
        present = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        missing.extend(f"{table}.{name}" for name in columns if name not in present)
    if missing:
        raise ReadonlySchemaError(
            "database schema is not compatible with shadow evaluation: "
            f"missing {', '.join(missing)} "
            "(read-only mode never migrates or repairs the database)"
        )

def _open_shadow_read_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open the single read-only connection Shadow Evaluation reads through.

    ``mode=ro`` + ``query_only`` plus the capability check above: this
    path never writes application data and never touches schema (see
    ``get_readonly_db_connection`` for the SQLite coordination-file
    boundary). There is exactly one such entry point so the full
    CLI -> core -> DB chain can be audited by grep.
    """
    conn = _facade().get_readonly_db_connection(db_path)
    try:
        assert_shadow_read_capability(conn)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise
    return conn

def _get_credentials_dir() -> Path:
    """Return protected credentials directory with strict mode 0700 permissions."""
    cred_dir = Path.home() / ".herdr-controller" / "credentials"
    cred_dir.mkdir(parents=True, exist_ok=True)
    try:
        cred_dir.chmod(0o700)
    except OSError:
        pass
    return cred_dir

def save_user_token(
    user_id: str,
    token_type: str,
    token_value: str,
    db_path: Optional[Path] = None,
) -> None:
    """Save or update a persistent user token in the protected credentials store (0600) and track metadata in DB."""
    resolved_path = Path(db_path) if db_path else _facade().get_default_db_path()
    now = time.time()

    cred_dir = _facade()._get_credentials_dir()
    safe_uid = re.sub(r"[^a-zA-Z0-9_\-]", "_", user_id)
    safe_type = re.sub(r"[^a-zA-Z0-9_\-]", "_", token_type)
    token_file = cred_dir / f"{safe_uid}_{safe_type}.token"

    if token_value:
        token_file.write_text(token_value, encoding="utf-8")
        try:
            token_file.chmod(0o600)
        except OSError:
            pass
    elif token_file.exists():
        token_file.unlink()

    with _facade().get_db_connection(resolved_path) as conn:
        conn.execute(
            """
            INSERT INTO user_tokens (user_id, token_type, has_token, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, token_type) DO UPDATE SET
                has_token = excluded.has_token,
                updated_at = excluded.updated_at
            """,
            (user_id, token_type, 1 if token_value else 0, now),
        )
        conn.commit()

def get_user_token(
    user_id: str,
    token_type: str,
    db_path: Optional[Path] = None,
) -> Optional[str]:
    """Retrieve a persistent user token from the protected credentials store, defaulting if unseeded."""
    cred_dir = _facade()._get_credentials_dir()
    safe_uid = re.sub(r"[^a-zA-Z0-9_\-]", "_", user_id)
    safe_type = re.sub(r"[^a-zA-Z0-9_\-]", "_", token_type)
    token_file = cred_dir / f"{safe_uid}_{safe_type}.token"

    if token_file.is_file():
        try:
            val = token_file.read_text(encoding="utf-8").strip()
            if val:
                return val
        except Exception:
            pass

    # Fallback seeding for default local reviewer token if not yet seeded
    if token_type == "experience_hub" and user_id in ("local-console-reviewer", "default"):
        cred_path = cred_dir / "experience-console-reviewer.token"
        if cred_path.is_file():
            try:
                token_val = cred_path.read_text(encoding="utf-8").strip()
                if token_val:
                    return token_val
            except Exception:
                pass
    return None

def delete_user_token(
    user_id: str,
    token_type: str,
    db_path: Optional[Path] = None,
) -> bool:
    """Delete a persistent user token from credentials store and state database."""
    resolved_path = Path(db_path) if db_path else _facade().get_default_db_path()
    cred_dir = _facade()._get_credentials_dir()
    safe_uid = re.sub(r"[^a-zA-Z0-9_\-]", "_", user_id)
    safe_type = re.sub(r"[^a-zA-Z0-9_\-]", "_", token_type)
    token_file = cred_dir / f"{safe_uid}_{safe_type}.token"
    deleted = False
    if token_file.exists():
        token_file.unlink()
        deleted = True
    with _facade().get_db_connection(resolved_path) as conn:
        cur = conn.execute(
            "DELETE FROM user_tokens WHERE user_id = ? AND token_type = ?",
            (user_id, token_type),
        )
        conn.commit()
        if cur.rowcount > 0:
            deleted = True
    return deleted
