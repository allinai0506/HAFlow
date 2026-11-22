"""Apply a validated work-item observation in the existing Intake claim transaction."""

import json
import os
from uuid import uuid4

from herdr.projects import load_projects
from herdr.inbox.store import InboxStore

from .protocol import canonical_json, digest, integer, text, token
from .store import AVAILABLE, LeaseLost, ScopeDenied

EVENT_TYPE = "com.haflow.intake.work-item.observed.v1"
DATA_SCHEMA = "urn:haflow:intake:work-item:v1"
PURPOSE = "review"



def local_config():
    """Trusted instance configuration, independent of connector and request data."""
    raw = os.environ.get("HAFLOW_INTAKE_INBOX_CONFIG")
    if not raw:
        return None
    config = json.loads(raw)
    if type(config) is not dict or config.keys() != {"tenant_id", "user_id", "connections"}:
        raise ValueError("invalid Intake Inbox configuration")
    token(config["tenant_id"])
    token(config["user_id"])
    if type(config["connections"]) is not dict or len(config["connections"]) > 100:
        raise ValueError("invalid Intake Inbox connections")
    for connection_id, policy in config["connections"].items():
        token(connection_id)
        if (type(policy) is not dict
                or policy.keys() - {"auto_assign", "project_id", "workspace_id"}
                or type(policy.get("auto_assign", True)) is not bool):
            raise ValueError("invalid Intake connection Inbox policy")
        for key in ("project_id", "workspace_id"):
            if policy.get(key) is not None:
                text(policy[key])
    return config


def bind_instance(db, tenant_id, user_id):
    value = canonical_json([token(tenant_id), token(user_id)])
    db.execute("INSERT OR IGNORE INTO schema_meta VALUES ('intake_inbox_owner_v1', ?)", (value,))
    check_instance(db, tenant_id, user_id)


def check_instance(db, tenant_id, user_id):
    row = db.execute("SELECT value FROM schema_meta WHERE key = 'intake_inbox_owner_v1'").fetchone()
    if row and row[0] != canonical_json([tenant_id, user_id]):
        raise ScopeDenied("Intake Inbox instance binding changed")


def task_text(value, limit):
    if (type(value) is not str or not value.strip() or len(value.encode("utf-8")) > limit
            or any(ord(c) < 32 and c not in "\n\r\t" or ord(c) == 127 for c in value)):
        raise ValueError("invalid task content")
    return value.strip()


def _observation(event):
    data = json.loads(event["data_json"])
    fixed = {"object_type", "external_id", "version", "action", "title", "description"}
    if (event["type"] != EVENT_TYPE or event["data_schema_uri"] != DATA_SCHEMA or type(data) is not dict
            or not fixed <= data.keys() or data.keys() - (fixed | {"identities"})):
        raise ValueError("unsupported work item schema")
    token(data["object_type"])
    text(data["external_id"])
    integer(data["version"], 1, 2**53 - 1)
    if data["action"] not in ("work", "update", "closed", "metadata"):
        raise ValueError("unsupported work item action")
    data["title"] = task_text(data["title"], 240)
    data["description"] = task_text(data["description"], 8000)
    identities = data.get("identities", {})
    if type(identities) is not dict or identities.keys() - {"assignee", "creator", "approver"}:
        raise ValueError("unsupported source identity field")
    for identity in identities.values():
        text(identity)
    return data


def valid_scope(project, workspace):
    records = load_projects().get("projects", {}).values()
    matches = [record for record in records if record.get("project_id") == project]
    return len(matches) == 1 and matches[0].get("workspace_id") == workspace


def configured_scope(policy):
    project, workspace = policy.get("project_id"), policy.get("workspace_id")
    if project and workspace and valid_scope(project, workspace):
        return project, workspace
    return None, None


def create_draft(db, link_id, user_id, snapshot, project, workspace, *, priority="NORMAL"):
    return InboxStore.insert_task(
        db, snapshot["title"], snapshot["description"], project, workspace,
        user_id=user_id, priority=priority, source_type="active_intake", source_id=link_id,
    )["id"]


def materialize(store, claim, policy):
    """Apply the output, attempt completion and audit on the same FULL writer."""
    # Resolve local registry context outside SQLite's write lock.
    project, workspace = configured_scope(policy)
    user = token(policy["user_id"])
    with store._write() as db:
        now = store.now()
        event = store._processing(db, claim, now)
        bind_instance(db, store.scope.tenant_id, user)
        if event["config_revision"] != claim.revision:
            return store._finish(db, event, now, "IGNORED", "SOURCE_AUTHORIZATION_CHANGED")
        if event["data_json"] is None or event["received_at"] <= now - store.policy.retention_ms:
            return store._finish(db, event, now, "IGNORED", "NO_RETAINED_CANONICAL_DATA")
        try:
            incoming = _observation(event)
        except json.JSONDecodeError:
            raise
        except ValueError:
            return store._finish(db, event, now, "REJECTED", "INVALID_WORK_ITEM")
        if incoming["action"] == "metadata":
            return store._finish(db, event, now, "IGNORED", "METADATA_ONLY")

        outcome, reason = _apply(db, store, event, now, incoming, user,
                                 policy.get("auto_assign", True), project, workspace,
                                 policy.get("allowed_connections", (store.scope.connection_id,)))
        finish_at = store.now()
        if finish_at >= claim.lease_until:
            raise LeaseLost("materialization claim expired before completion")
        return store._finish(db, event, finish_at, outcome, reason)


def _content_digest(observation):
    content = {key: value for key, value in observation.items() if key != "action"}
    return digest(canonical_json(content).encode("utf-8"))


def _existing_content_digest(obj):
    try:
        existing = json.loads(obj["snapshot_json"])
        if isinstance(existing, dict):
            return _content_digest(existing)
    except (TypeError, ValueError):
        pass
    return obj["snapshot_digest"]


def _apply(db, store, event, now, incoming, user, auto_assign, project, workspace, allowed):
    connection = store._connection(db)
    tenant, connection_id = store.scope.keys
    key = (tenant, connection["provider"], connection["provider_instance_key"],
           incoming["object_type"], incoming["external_id"])
    obj = db.execute("""SELECT * FROM intake_objects WHERE tenant_id = ? AND provider = ?
        AND provider_instance_key = ? AND object_type = ? AND external_id = ?""", key).fetchone()
    object_id = obj["object_id"] if obj else str(uuid4())
    link = db.execute("""SELECT * FROM intake_task_links WHERE tenant_id = ?
        AND object_id = ? AND purpose_key = ?""", (tenant, object_id, PURPOSE)).fetchone()
    link_id = link["link_id"] if link else None
    if event["object_id"] not in (None, object_id) or event["link_id"] not in (None, link_id):
        raise ScopeDenied("event association cannot be redirected")

    snapshot = canonical_json(incoming)
    snapshot_digest = _content_digest(incoming)
    previous = None
    if obj:
        previous = db.execute("""SELECT c.status, c.config_revision, e.config_revision AS observed_revision
            FROM intake_connections c JOIN intake_events e
              ON e.tenant_id = c.tenant_id AND e.connection_id = c.connection_id
            WHERE c.tenant_id = ? AND c.connection_id = ? AND e.event_id = ?""",
                              (tenant, obj["observed_connection_id"], obj["last_event_id"])).fetchone()
        if (obj["observed_connection_id"] != connection_id
                and (obj["observed_connection_id"] not in allowed or not _current_source(previous))):
            return "IGNORED", "OBJECT_CONNECTION_NOT_AUTHORIZED"
    if obj and incoming["version"] <= int(obj["version"]):
        conflict = (incoming["version"] == int(obj["version"])
                    and snapshot_digest != _existing_content_digest(obj))
        if incoming["action"] == "closed" or conflict:
            status = ("CLOSED" if incoming["action"] == "closed" or obj["external_status"] == "CLOSED"
                      else "CONFLICT")
            if obj["external_status"] != status:
                db.execute("""UPDATE intake_objects SET external_status = ?, updated_at = ?
                    WHERE tenant_id = ? AND object_id = ?""", (status, now, tenant, object_id))
                if link:
                    _touch_link(db, tenant, object_id, link_id, event["event_id"], now, clear_confirmation=True)
            _attach_event(db, store, event, object_id, link_id)
            kind = "intake_object_closed" if status == "CLOSED" else "intake_object_version_conflict"
            store._audit(db, kind, now, origin="SYSTEM", object_id=object_id,
                         event_id=event["event_id"], link_id=link_id)
            return ("PROCESSED", None) if status == "CLOSED" else ("IGNORED", "OBJECT_VERSION_CONFLICT")
        if (not link and incoming["action"] == "work"
                and obj["external_status"] == "OPEN" and _current_source(previous)):
            current = json.loads(obj["snapshot_json"])
            link_id = _create_link(db, store, event, now, object_id, current, user,
                                   auto_assign, project, workspace,
                                   (obj["observed_connection_id"], previous["observed_revision"]))
            _attach_event(db, store, event, object_id, link_id)
            return "PROCESSED", None
        _attach_event(db, store, event, object_id, link_id)
        return "IGNORED", "DUPLICATE_OR_STALE_OBJECT"

    closed = incoming["action"] == "closed" or bool(obj and obj["external_status"] == "CLOSED")
    status = "CLOSED" if closed else "OPEN"
    if obj:
        db.execute("""UPDATE intake_objects SET version = ?, snapshot_digest = ?, snapshot_json = ?,
            external_status = ?, last_event_id = ?, observed_connection_id = ?, updated_at = ?
            WHERE tenant_id = ? AND object_id = ?""",
                   (str(incoming["version"]), snapshot_digest, snapshot, status, event["event_id"],
                    connection_id, now, tenant, object_id))
    else:
        db.execute("""INSERT INTO intake_objects
            (object_id, tenant_id, provider, provider_instance_key, object_type, external_id,
             observed_connection_id, version, snapshot_digest, snapshot_json, external_status,
             last_event_id, first_seen_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (object_id, *key, connection_id, str(incoming["version"]), snapshot_digest,
                    snapshot, status, event["event_id"], now, now))

    if not link and incoming["action"] == "work" and not closed:
        link_id = _create_link(db, store, event, now, object_id, incoming, user,
                               auto_assign, project, workspace, (connection_id, event["config_revision"]))
    elif link:
        _touch_link(db, tenant, object_id, link_id, event["event_id"], now, clear_confirmation=closed)
        store._audit(db, "intake_work_item_merged", now, origin="SYSTEM", object_id=object_id,
                     event_id=event["event_id"], link_id=link_id, task_id=link["task_id"])

    _attach_event(db, store, event, object_id, link_id)
    return "PROCESSED", None



def _current_source(row):
    return (row and row["status"] in AVAILABLE
            and row["config_revision"] == row["observed_revision"])


def _touch_link(db, tenant, object_id, link_id, event_id, now, *, clear_confirmation):
    db.execute("""UPDATE intake_task_links SET updated_at = ?, last_event_id = ?, revision = revision + 1,
        confirmation_json = CASE WHEN ? THEN NULL ELSE confirmation_json END
        WHERE tenant_id = ? AND object_id = ? AND link_id = ?""",
               (now, event_id, clear_confirmation, tenant, object_id, link_id))


def _create_link(db, store, event, now, object_id, incoming, user, auto_assign,
                 project, workspace, body_authority):
    tenant = store.scope.tenant_id
    link_id = str(uuid4())
    db.execute("""INSERT INTO intake_task_links
        (link_id, tenant_id, object_id, purpose_key, disposition, created_at,
         updated_at, assignment_source, project_id, workspace_id, origin_connection_id,
         authorization_revision, last_event_id)
        VALUES (?,?,?,?,'UNASSIGNED',?,?,'AUTO',?,?,?,?,?)""",
               (link_id, tenant, object_id, PURPOSE, now, now, project, workspace,
                store.scope.connection_id, event["config_revision"], event["event_id"]))
    task_id = None
    if auto_assign:
        task_id = create_draft(db, link_id, user, incoming, project, workspace)
        db.execute("""UPDATE intake_task_links SET user_id = ?, task_id = ?, disposition = 'LINKED',
            body_connection_id = ?, body_revision = ? WHERE tenant_id = ? AND object_id = ? AND link_id = ?""",
                   (user, task_id, *body_authority, tenant, object_id, link_id))
    store._audit(db, "intake_materialized", now, origin="SYSTEM", object_id=object_id,
                 event_id=event["event_id"], link_id=link_id, task_id=task_id)
    store._audit(db, "intake_assignment", now, origin="AUTO", link_id=link_id,
                 user_id=user if auto_assign else None,
                 reason="CONNECTION_POLICY" if auto_assign else "REVIEW_REQUIRED")
    return link_id


def _attach_event(db, store, event, object_id, link_id):
    db.execute("""UPDATE intake_events SET object_id = ?, link_id = ?
        WHERE tenant_id = ? AND connection_id = ? AND event_id = ? AND claim_epoch = ?""",
               (object_id, link_id, *store.scope.keys, event["event_id"], event["claim_epoch"]))
