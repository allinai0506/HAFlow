"""Additive Intake migration in the existing State DB file."""

import sqlite3
from contextlib import closing
from pathlib import Path

from herdr import state_db
from herdr.state.schema import _run_exclusive, _schema_initialization_lock

BUSY_TIMEOUT_MS = 250
MIGRATION_KEY = "active_intake_v1"

DDL = (
    """CREATE TABLE intake_connections (
        connection_id TEXT PRIMARY KEY NOT NULL,
        tenant_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        provider_instance_key TEXT NOT NULL,
        display_name TEXT NOT NULL,
        auth_ref TEXT NOT NULL,
        configuration TEXT NOT NULL DEFAULT '{}',
        connector_version TEXT NOT NULL,
        config_revision INTEGER NOT NULL CHECK (config_revision > 0),
        status TEXT NOT NULL CHECK (status IN ('DISABLED','ACTIVE','DEGRADED','AUTH_REQUIRED')),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        last_health_at INTEGER,
        last_error TEXT,
        UNIQUE (tenant_id, connection_id)
    )""",
    """CREATE TABLE intake_objects (
        object_id TEXT PRIMARY KEY NOT NULL,
        tenant_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        provider_instance_key TEXT NOT NULL,
        object_type TEXT NOT NULL,
        external_id TEXT NOT NULL,
        observed_connection_id TEXT NOT NULL,
        version TEXT,
        snapshot_digest TEXT NOT NULL,
        url TEXT,
        snapshot_json TEXT NOT NULL,
        external_status TEXT,
        last_event_id TEXT NOT NULL,
        first_seen_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        UNIQUE (tenant_id, object_id),
        UNIQUE (tenant_id, provider, provider_instance_key, object_type, external_id),
        FOREIGN KEY (tenant_id, observed_connection_id)
            REFERENCES intake_connections(tenant_id, connection_id)
    )""",
    """CREATE TABLE intake_task_links (
        link_id TEXT PRIMARY KEY NOT NULL,
        tenant_id TEXT NOT NULL,
        object_id TEXT NOT NULL,
        purpose_key TEXT NOT NULL,
        user_id TEXT,
        task_id TEXT UNIQUE,
        disposition TEXT NOT NULL CHECK (disposition IN ('UNASSIGNED','LINKED','IGNORED')),
        reason_code TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        confirmation_json TEXT,
        UNIQUE (tenant_id, object_id, link_id),
        UNIQUE (tenant_id, object_id, purpose_key),
        FOREIGN KEY (tenant_id, object_id) REFERENCES intake_objects(tenant_id, object_id)
    )""",
    """CREATE TABLE intake_events (
        event_id TEXT PRIMARY KEY NOT NULL,
        tenant_id TEXT NOT NULL,
        connection_id TEXT NOT NULL,
        provider_event_id TEXT,
        source TEXT NOT NULL CHECK (source <> ''),
        id TEXT NOT NULL CHECK (id <> ''),
        type TEXT NOT NULL CHECK (type <> ''),
        data_schema_uri TEXT,
        data_json TEXT,
        subject TEXT,
        occurred_at INTEGER,
        occurred_time TEXT,
        received_at INTEGER NOT NULL,
        payload_json TEXT,
        payload_expires_at INTEGER,
        raw_digest TEXT NOT NULL,
        fact_digest TEXT NOT NULL,
        signature_verified INTEGER CHECK (signature_verified = 1),
        verified_via TEXT NOT NULL CHECK (verified_via IN
            ('SIGNATURE','AUTHENTICATED_READ','AUTHENTICATED_STREAM','LOCAL_JOB')),
        status TEXT NOT NULL CHECK (status IN
            ('VERIFIED','PROCESSING','PROCESSED','RETRY_WAIT','IGNORED','REJECTED','DEAD_LETTER')),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        retry_budget_used INTEGER NOT NULL DEFAULT 0 CHECK (retry_budget_used >= 0),
        next_retry_at INTEGER,
        last_error TEXT,
        correlation_id TEXT NOT NULL,
        object_id TEXT,
        link_id TEXT,
        lease_until INTEGER,
        claim_epoch INTEGER NOT NULL DEFAULT 0 CHECK (claim_epoch >= 0),
        replay_actor TEXT,
        replay_reason TEXT,
        connector_version TEXT NOT NULL,
        config_revision INTEGER NOT NULL,
        policy_revision INTEGER NOT NULL,
        CHECK ((verified_via = 'SIGNATURE' AND signature_verified IS 1)
            OR (verified_via <> 'SIGNATURE' AND signature_verified IS NULL)),
        CHECK ((status = 'PROCESSING') = (lease_until IS NOT NULL)),
        CHECK ((status = 'RETRY_WAIT') = (next_retry_at IS NOT NULL)),
        CHECK (link_id IS NULL OR object_id IS NOT NULL),
        UNIQUE (tenant_id, source, id),
        UNIQUE (tenant_id, event_id),
        UNIQUE (tenant_id, connection_id, event_id),
        FOREIGN KEY (tenant_id, connection_id) REFERENCES intake_connections(tenant_id, connection_id),
        FOREIGN KEY (tenant_id, object_id) REFERENCES intake_objects(tenant_id, object_id),
        FOREIGN KEY (tenant_id, object_id, link_id)
            REFERENCES intake_task_links(tenant_id, object_id, link_id)
    )""",
    """CREATE TABLE intake_cursors (
        tenant_id TEXT NOT NULL,
        connection_id TEXT NOT NULL,
        stream_key TEXT NOT NULL,
        source_capability TEXT NOT NULL CHECK (source_capability <> ''),
        cursor TEXT NOT NULL,
        last_sync_at INTEGER,
        sync_status TEXT NOT NULL DEFAULT 'IDLE'
            CHECK (sync_status IN ('IDLE','RUNNING','RETRY_WAIT','PAUSED')),
        last_error TEXT,
        next_poll_at INTEGER NOT NULL,
        lease_until INTEGER,
        claim_epoch INTEGER NOT NULL DEFAULT 0 CHECK (claim_epoch >= 0),
        config_revision INTEGER NOT NULL,
        ownership_digest TEXT,
        CHECK ((sync_status = 'RUNNING') = (lease_until IS NOT NULL)),
        CHECK ((sync_status = 'RUNNING') = (ownership_digest IS NOT NULL)),
        PRIMARY KEY (tenant_id, connection_id, stream_key),
        FOREIGN KEY (tenant_id, connection_id) REFERENCES intake_connections(tenant_id, connection_id)
    )""",
    """CREATE TABLE intake_attempts (
        attempt_id TEXT PRIMARY KEY NOT NULL,
        tenant_id TEXT NOT NULL,
        connection_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
        started_at INTEGER NOT NULL,
        finished_at INTEGER,
        outcome TEXT CHECK (outcome IN ('PROCESSED','IGNORED','REJECTED','RETRY_WAIT','DEAD_LETTER')),
        error TEXT,
        next_retry_at INTEGER,
        claim_epoch INTEGER NOT NULL,
        ownership_digest TEXT NOT NULL,
        config_revision INTEGER NOT NULL,
        trigger TEXT NOT NULL CHECK (trigger IN ('INITIAL','RETRY','REPLAY')),
        actor_ref TEXT,
        replay_reason TEXT,
        UNIQUE (tenant_id, event_id, attempt_number),
        CHECK ((finished_at IS NULL) = (outcome IS NULL)),
        CHECK ((trigger = 'REPLAY' AND actor_ref IS NOT NULL AND replay_reason IS NOT NULL)
            OR (trigger <> 'REPLAY' AND actor_ref IS NULL AND replay_reason IS NULL)),
        FOREIGN KEY (tenant_id, connection_id, event_id)
            REFERENCES intake_events(tenant_id, connection_id, event_id)
    )""",
    """CREATE TABLE intake_delivery_receipts (
        receipt_id TEXT PRIMARY KEY NOT NULL,
        tenant_id TEXT NOT NULL,
        connection_id TEXT NOT NULL,
        received_at INTEGER NOT NULL,
        outcome TEXT NOT NULL CHECK (outcome IN ('ACCEPTED','DUPLICATE','REJECTED')),
        canonical_event_id TEXT,
        reason_code TEXT,
        raw_digest TEXT,
        transport_id_digest TEXT,
        batch_id TEXT,
        CHECK ((outcome = 'REJECTED') = (canonical_event_id IS NULL)),
        CHECK (outcome <> 'REJECTED' OR reason_code IS NOT NULL),
        FOREIGN KEY (tenant_id, connection_id) REFERENCES intake_connections(tenant_id, connection_id),
        FOREIGN KEY (tenant_id, canonical_event_id) REFERENCES intake_events(tenant_id, event_id)
    )""",
    """CREATE INDEX intake_events_claim ON intake_events
        (tenant_id, connection_id, connector_version, policy_revision, status,
         next_retry_at, received_at, event_id)""",
    """CREATE INDEX intake_events_lease ON intake_events
        (tenant_id, connection_id, lease_until, event_id) WHERE status = 'PROCESSING'""",
    """CREATE INDEX intake_events_list ON intake_events
        (tenant_id, connection_id, received_at, event_id)""",
    """CREATE INDEX intake_events_retention ON intake_events
        (tenant_id, connection_id, received_at, event_id) WHERE data_json IS NOT NULL""",
    """CREATE INDEX intake_receipts_received ON intake_delivery_receipts
        (tenant_id, connection_id, received_at, receipt_id)""",
    """CREATE INDEX intake_admin_audit_scope ON events (
        CASE WHEN json_valid(payload_json) THEN json_extract(payload_json, '$.tenant_id') END,
        CASE WHEN json_valid(payload_json) THEN json_extract(payload_json, '$.connection_id') END,
        id) WHERE source = 'active_intake'""",
    """CREATE TRIGGER intake_connection_identity_immutable BEFORE UPDATE OF
        tenant_id, connection_id, provider, provider_instance_key ON intake_connections
        BEGIN SELECT RAISE(ABORT, 'immutable intake connection scope'); END""",
    """CREATE TRIGGER intake_event_identity_immutable BEFORE UPDATE OF
        event_id, tenant_id, connection_id, provider_event_id, source, id, type, subject,
        occurred_at, occurred_time, received_at, data_schema_uri, raw_digest, fact_digest, signature_verified,
        verified_via, connector_version, config_revision, policy_revision, correlation_id
        ON intake_events BEGIN SELECT RAISE(ABORT, 'immutable intake identity'); END""",
    """CREATE TRIGGER intake_event_initial BEFORE INSERT ON intake_events
        WHEN NEW.status <> 'VERIFIED' OR NEW.claim_epoch <> 0 OR NEW.attempt_count <> 0
        BEGIN SELECT RAISE(ABORT, 'invalid initial intake status'); END""",
    """CREATE TRIGGER intake_event_edges BEFORE UPDATE OF status ON intake_events
        WHEN NOT (
            (OLD.status IN ('VERIFIED','RETRY_WAIT') AND NEW.status = 'PROCESSING'
                AND NEW.claim_epoch = OLD.claim_epoch + 1
                AND NEW.attempt_count = OLD.attempt_count + 1
                AND EXISTS (SELECT 1 FROM intake_attempts a WHERE a.tenant_id = NEW.tenant_id
                    AND a.connection_id = NEW.connection_id AND a.event_id = NEW.event_id
                    AND a.attempt_number = NEW.attempt_count AND a.claim_epoch = NEW.claim_epoch
                    AND a.finished_at IS NULL))
            OR (OLD.status = 'PROCESSING' AND NEW.status IN
                ('PROCESSED','IGNORED','REJECTED','RETRY_WAIT','DEAD_LETTER')
                AND NEW.claim_epoch = OLD.claim_epoch AND NEW.attempt_count = OLD.attempt_count
                AND EXISTS (SELECT 1 FROM intake_attempts a WHERE a.tenant_id = NEW.tenant_id
                    AND a.connection_id = NEW.connection_id AND a.event_id = NEW.event_id
                    AND a.attempt_number = NEW.attempt_count AND a.claim_epoch = NEW.claim_epoch
                    AND a.finished_at IS NOT NULL AND a.outcome = NEW.status))
            OR (OLD.status = 'DEAD_LETTER' AND NEW.status = 'VERIFIED'
                AND NEW.claim_epoch = OLD.claim_epoch AND NEW.attempt_count = OLD.attempt_count
                AND NEW.retry_budget_used = 0 AND NEW.replay_actor IS NOT NULL
                AND NEW.replay_reason IS NOT NULL)
        ) BEGIN SELECT RAISE(ABORT, 'invalid intake state transition'); END""",
    """CREATE TRIGGER intake_event_fence_immutable BEFORE UPDATE OF
        claim_epoch, lease_until, attempt_count, retry_budget_used, next_retry_at,
        replay_actor, replay_reason ON intake_events WHEN OLD.status = NEW.status
        BEGIN SELECT RAISE(ABORT, 'intake claim changes require a transition'); END""",
    """CREATE TRIGGER intake_event_data_immutable BEFORE UPDATE OF data_json ON intake_events
        WHEN NEW.data_json IS NOT NULL AND NEW.data_json IS NOT OLD.data_json
        BEGIN SELECT RAISE(ABORT, 'immutable intake data'); END""",
    """CREATE TRIGGER intake_finished_attempt_immutable BEFORE UPDATE ON intake_attempts
        WHEN OLD.finished_at IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'finished intake attempt'); END""",
)


MATERIALIZATION_KEY = 'active_intake_inbox_v1'
DISPATCH_KEY = 'active_intake_dispatch_v1'

MATERIALIZATION_DDL = (
    "ALTER TABLE intake_task_links ADD COLUMN revision INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE intake_task_links ADD COLUMN origin_connection_id TEXT",
    "ALTER TABLE intake_task_links ADD COLUMN authorization_revision INTEGER",
    "ALTER TABLE intake_task_links ADD COLUMN last_event_id TEXT",
    "ALTER TABLE intake_task_links ADD COLUMN body_connection_id TEXT",
    "ALTER TABLE intake_task_links ADD COLUMN body_revision INTEGER",
    "ALTER TABLE intake_task_links ADD COLUMN assignment_source TEXT",
    "ALTER TABLE intake_task_links ADD COLUMN project_id TEXT",
    "ALTER TABLE intake_task_links ADD COLUMN workspace_id TEXT",
    """CREATE INDEX intake_link_listing ON intake_task_links
       (tenant_id, updated_at DESC, link_id DESC)""",
    """CREATE INDEX intake_receipt_event ON intake_delivery_receipts
       (tenant_id, connection_id, canonical_event_id, received_at, receipt_id)""",
    """CREATE TRIGGER intake_inbox_draft_only BEFORE UPDATE ON user_inbox_tasks
       WHEN (NEW.user_id IS NOT OLD.user_id OR NEW.id IS NOT OLD.id)
         OR (
           OLD.status IN ('DRAFT', 'CANCELLED')
           AND EXISTS (SELECT 1 FROM intake_task_links WHERE task_id = OLD.id AND confirmation_json IS NULL)
           AND (NEW.status NOT IN ('DRAFT', 'CANCELLED') OR NEW.workflow_id IS NOT NULL OR NEW.worker_id IS NOT NULL)
         )
       BEGIN SELECT RAISE(ABORT, 'active intake task may not be dispatched or transferred'); END""",
    """CREATE TRIGGER intake_inbox_no_replace BEFORE INSERT ON user_inbox_tasks
       WHEN EXISTS (SELECT 1 FROM intake_task_links WHERE task_id = NEW.id)
       BEGIN SELECT RAISE(ABORT, 'active intake history must be retained'); END""",
    """CREATE TRIGGER intake_link_task_guard BEFORE UPDATE OF task_id, user_id, disposition
       ON intake_task_links WHEN
          (OLD.task_id IS NOT NULL AND NEW.task_id IS NOT OLD.task_id)
          OR (NEW.disposition = 'LINKED' AND (NEW.user_id IS NULL OR NEW.task_id IS NULL))
          OR (NEW.disposition <> 'LINKED' AND NEW.user_id IS NOT NULL)
          OR (NEW.task_id IS NOT NULL AND NOT EXISTS (
              SELECT 1 FROM user_inbox_tasks t WHERE t.id = NEW.task_id
              AND (NEW.user_id IS NULL OR t.user_id = NEW.user_id)))
       BEGIN SELECT RAISE(ABORT, 'invalid active intake task link'); END""",
    """CREATE TRIGGER intake_inbox_no_delete BEFORE DELETE ON user_inbox_tasks
       WHEN EXISTS (SELECT 1 FROM intake_task_links WHERE task_id = OLD.id)
       BEGIN SELECT RAISE(ABORT, 'active intake history must be retained'); END""",
)

DISPATCH_DDL = (
    "DROP TRIGGER IF EXISTS intake_inbox_draft_only",
    """CREATE TRIGGER intake_inbox_draft_only BEFORE UPDATE ON user_inbox_tasks
       WHEN (NEW.user_id IS NOT OLD.user_id OR NEW.id IS NOT OLD.id)
         OR (
           OLD.status IN ('DRAFT', 'CANCELLED')
           AND EXISTS (SELECT 1 FROM intake_task_links WHERE task_id = OLD.id AND confirmation_json IS NULL)
           AND (NEW.status NOT IN ('DRAFT', 'CANCELLED') OR NEW.workflow_id IS NOT NULL OR NEW.worker_id IS NOT NULL)
         )
       BEGIN SELECT RAISE(ABORT, 'active intake task may not be dispatched or transferred'); END""",
    "DROP TRIGGER IF EXISTS intake_link_task_guard",
    """CREATE TRIGGER intake_link_task_guard BEFORE UPDATE OF task_id, user_id, disposition
       ON intake_task_links WHEN
          (OLD.task_id IS NOT NULL AND NEW.task_id IS NOT OLD.task_id)
          OR (NEW.disposition = 'LINKED' AND (NEW.user_id IS NULL OR NEW.task_id IS NULL))
          OR (NEW.disposition <> 'LINKED' AND NEW.user_id IS NOT NULL)
          OR (NEW.task_id IS NOT NULL AND NOT EXISTS (
              SELECT 1 FROM user_inbox_tasks t WHERE t.id = NEW.task_id
              AND (NEW.user_id IS NULL OR t.user_id = NEW.user_id)))
       BEGIN SELECT RAISE(ABORT, 'invalid active intake task link'); END""",
)


def open_db(path: Path) -> sqlite3.Connection:
    """Open a dedicated writer. Existing State DB users retain their PRAGMAs."""
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True,
                         isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
            raise RuntimeError("Intake requires WAL on the State DB")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        db.row_factory = sqlite3.Row
        return db
    except BaseException:
        db.close()
        raise


def initialize(path: Path | None = None) -> Path:
    """Explicit startup/upgrade through State DB's schema lock and DDL runner."""
    path = Path(path or state_db.get_default_db_path()).resolve()
    state_db.init_db(path)
    with _schema_initialization_lock(path), closing(open_db(path)) as db:
        if not db.execute("SELECT 1 FROM schema_meta WHERE key = ?", (MIGRATION_KEY,)).fetchone():
            _run_exclusive(db, (*DDL, "INSERT INTO schema_meta VALUES ('active_intake_v1','1')"))
    return path


def initialize_materialization(path: Path) -> None:
    """Install the write guard before the first linked Inbox task can exist."""
    from herdr.inbox.schema import ensure_inbox_schema

    exists = "SELECT 1 FROM schema_meta WHERE key = ?"
    with closing(open_db(path)) as db:
        has_mat = db.execute(exists, (MATERIALIZATION_KEY,)).fetchone()
        has_disp = db.execute(exists, (DISPATCH_KEY,)).fetchone()
        if has_mat and has_disp:
            return
    with _schema_initialization_lock(path), closing(open_db(path)) as db:
        if not db.execute(exists, (MATERIALIZATION_KEY,)).fetchone():
            ensure_inbox_schema(db)
            _run_exclusive(db, (*MATERIALIZATION_DDL,
                                f"INSERT INTO schema_meta VALUES ('{MATERIALIZATION_KEY}','1')"))
        if not db.execute(exists, (DISPATCH_KEY,)).fetchone():
            _run_exclusive(db, (*DISPATCH_DDL,
                                f"INSERT INTO schema_meta VALUES ('{DISPATCH_KEY}','1')"))
