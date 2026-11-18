"""Tenant-scoped projection and operator actions for the existing User Inbox."""

import json
from contextlib import closing

from herdr.inbox.transitions import PRIORITY_WEIGHTS

from .materializer import PURPOSE, check_instance, create_draft, task_text, valid_scope
from .protocol import Scope, canonical_json, digest, integer, text
from .schema import MATERIALIZATION_KEY, open_db
from .store import EventStore, StateConflict

SELECT = """
    SELECT l.*, o.provider, o.external_id, o.object_type, o.external_status, o.snapshot_json,
        o.observed_connection_id AS snapshot_connection_id, c.config_revision,
        cause.connection_id, trigger_conn.display_name, cause.event_id, cause.occurred_time, cause.received_at,
        (SELECT receipt_id FROM intake_delivery_receipts r WHERE r.tenant_id = cause.tenant_id
          AND r.connection_id = cause.connection_id AND r.canonical_event_id = cause.event_id
          AND r.outcome = 'ACCEPTED' ORDER BY r.received_at, r.receipt_id LIMIT 1) AS receipt_id,
        t.title, t.instruction, t.priority, t.status AS task_status
    FROM intake_task_links l
    JOIN intake_objects o ON o.tenant_id = l.tenant_id AND o.object_id = l.object_id
    JOIN intake_connections c ON c.tenant_id = o.tenant_id AND c.connection_id = o.observed_connection_id
    JOIN intake_connections origin ON origin.tenant_id = l.tenant_id
      AND origin.connection_id = l.origin_connection_id AND origin.config_revision = l.authorization_revision
    LEFT JOIN intake_connections body ON body.tenant_id = l.tenant_id
      AND body.connection_id = l.body_connection_id AND body.config_revision = l.body_revision
    JOIN intake_events e ON e.tenant_id = o.tenant_id AND e.connection_id = c.connection_id
      AND e.event_id = o.last_event_id AND e.object_id = o.object_id
    JOIN intake_events cause ON cause.tenant_id = l.tenant_id AND cause.object_id = l.object_id
      AND cause.event_id = l.last_event_id AND cause.link_id = l.link_id
    JOIN intake_connections trigger_conn ON trigger_conn.tenant_id = cause.tenant_id
      AND trigger_conn.connection_id = cause.connection_id AND trigger_conn.config_revision = cause.config_revision
    LEFT JOIN user_inbox_tasks t ON t.id = l.task_id
    WHERE l.tenant_id = ? AND l.purpose_key = ? AND c.status IN ('ACTIVE','DEGRADED')
        AND origin.status IN ('ACTIVE','DEGRADED') AND c.config_revision = e.config_revision
        AND trigger_conn.status IN ('ACTIVE','DEGRADED')
        AND (l.user_id IS NULL OR (l.user_id = ? AND t.user_id = l.user_id))
        AND (l.task_id IS NULL OR (t.user_id = ? AND body.status IN ('ACTIVE','DEGRADED')))
"""


class IntakeInbox:
    """The caller authenticates the configured single operator before accessing this view."""

    def __init__(self, path, config):
        self.path = path
        self.tenant = config["tenant_id"]
        self.user = config["user_id"]
        self.connections = tuple(config["connections"])

    def _query(self, db):
        check_instance(db, self.tenant, self.user)
        placeholders = ", ".join("?" for _ in self.connections)
        query = SELECT + (f" AND o.observed_connection_id IN ({placeholders})"
                          f" AND l.origin_connection_id IN ({placeholders})"
                          f" AND cause.connection_id IN ({placeholders})"
                          f" AND (l.task_id IS NULL OR l.body_connection_id IN ({placeholders}))")
        return query, (self.tenant, PURPOSE, self.user, self.user,
                       *self.connections, *self.connections, *self.connections, *self.connections)

    def list_items(self, *, view="ALL", limit=100, offset=0, project=None, priority=None):
        limits = (integer(limit, 1, 100), integer(offset))
        filters = {
            "ALL": "",
            "MINE": " AND l.disposition = 'LINKED'",
            "PENDING_ASSIGNMENT": " AND l.disposition = 'UNASSIGNED' AND o.external_status = 'OPEN'",
            "DRAFT": """ AND l.disposition = 'LINKED' AND t.status = 'DRAFT'
                AND l.confirmation_json IS NULL AND o.external_status = 'OPEN'""",
        }
        if view not in filters:
            raise ValueError("invalid Inbox view")
        with closing(open_db(self.path)) as db:
            if not db.execute("SELECT 1 FROM schema_meta WHERE key = ?", (MATERIALIZATION_KEY,)).fetchone():
                return []
            query, keys = self._query(db)
            query += filters[view]
            if project is not None:
                query += " AND l.project_id = ?"
                keys += (text(project),)
            if priority is not None:
                if priority not in PRIORITY_WEIGHTS:
                    raise ValueError("invalid priority filter")
                query += " AND COALESCE(t.priority, 'NORMAL') = ?"
                keys += (priority,)
            rows = db.execute(query +
                              " ORDER BY l.updated_at DESC, l.link_id DESC LIMIT ? OFFSET ?",
                              (*keys, *limits))
            return [_project(row) for row in rows]

    def _get(self, db, link_id):
        query, keys = self._query(db)
        row = db.execute(query + " AND l.link_id = ?", (*keys, link_id)).fetchone()
        if row is None:
            raise KeyError("Intake task not found")
        return row

    def change(self, link_id, action, revision, changes=None):
        text(link_id, 64)
        integer(revision, 1)
        if action not in ("assign", "unassign", "edit", "confirm", "ignore", "reactivate"):
            raise ValueError("invalid Intake action")
        if action == "edit":
            changes = _validate_edit(changes)
        elif changes is not None:
            raise ValueError("fields are only accepted by edit")

        with closing(open_db(self.path)) as db:
            db.execute("BEGIN IMMEDIATE")
            with db:
                item = self._get(db, link_id)
                if item["revision"] != revision:
                    raise StateConflict("work item changed; refresh before editing")
                if item["task_status"] not in (None, "DRAFT"):
                    raise StateConflict("terminal task")
                if item["external_status"] != "OPEN" and action not in ("unassign", "ignore"):
                    raise StateConflict("external work item is closed or has conflicting source evidence")
                store = EventStore(self.path, Scope(self.tenant, item["connection_id"]))
                self._apply(db, store, item, action, changes)
                return _project(self._get(db, link_id))

    def _apply(self, db, store, item, action, changes):
        now = store.now()
        task_id = item["task_id"]
        owner = item["user_id"]
        state = item["disposition"]
        confirmation = item["confirmation_json"]
        body_authority = item["body_connection_id"], item["body_revision"]
        assignment = item["assignment_source"]
        project, workspace = item["project_id"], item["workspace_id"]
        event = None
        evidence = {}
        if action == "assign" and state == "UNASSIGNED":
            if task_id is None:
                task_id = create_draft(db, item["link_id"], self.user,
                                       json.loads(item["snapshot_json"]), project, workspace)
                body_authority = item["snapshot_connection_id"], item["config_revision"]
            state, owner, assignment = "LINKED", self.user, "MANUAL"
            event = "intake_assignment"
        elif action == "unassign" and state == "LINKED":
            state, owner, confirmation, assignment = "UNASSIGNED", None, None, "MANUAL"
            event = "intake_assignment"
        elif action == "ignore" and state != "IGNORED":
            state, owner, confirmation = "IGNORED", None, None
            event = "intake_task_ignored"
        elif action == "reactivate" and state == "IGNORED":
            state, owner, assignment = "UNASSIGNED", None, "MANUAL"
            event = "intake_task_reactivated"
        elif action == "edit" and state == "LINKED":
            project, workspace = changes["project_id"], changes["workspace_id"]
            db.execute("""UPDATE user_inbox_tasks SET title = ?, instruction = ?, project_id = ?,
                workspace_id = ?, priority = ?, priority_weight = ?, updated_at = ?
                WHERE id = ? AND user_id = ? AND status = 'DRAFT'""",
                       (changes["title"], changes["description"], project, workspace,
                        changes["priority"], PRIORITY_WEIGHTS[changes["priority"]],
                        now / 1000, task_id, self.user))
            confirmation = None
            evidence = {
                "old_project_id": item["project_id"], "new_project_id": project,
                "old_workspace_id": item["workspace_id"], "new_workspace_id": workspace,
                "old_priority": item["priority"], "new_priority": changes["priority"],
                "title_changed": item["title"] != changes["title"],
                "description_changed": item["instruction"] != changes["description"],
            }
            event = "intake_task_edited"
        elif action == "confirm" and state == "LINKED":
            if project and not valid_scope(project, workspace):
                raise StateConflict("project/workspace binding changed")
            content = [item["title"], item["instruction"], item["priority"], project, workspace]
            confirmation = canonical_json({
                "user_id": self.user, "task_id": task_id, "confirmed_at": now,
                "config_revision": item["config_revision"], "project_id": project,
                "workspace_id": workspace,
                "content_digest": digest(canonical_json(content).encode("utf-8")),
            })
            event = "intake_task_confirmed"
        if event is None:
            raise StateConflict("action does not apply to current work item")
        db.execute("""UPDATE intake_task_links SET disposition = ?, task_id = ?, user_id = ?,
            assignment_source = ?, confirmation_json = ?, project_id = ?, workspace_id = ?,
            body_connection_id = ?, body_revision = ?, updated_at = ?, revision = revision + 1
            WHERE tenant_id = ? AND object_id = ? AND link_id = ?""",
                   (state, task_id, owner, assignment, confirmation, project, workspace, *body_authority, now,
                    self.tenant, item["object_id"], item["link_id"]))
        store._audit(db, event, now, origin="MANUAL", actor_ref=self.user, user_id=owner,
                     previous_user_id=item["user_id"], link_id=item["link_id"],
                     task_id=task_id, project_id=project, workspace_id=workspace, **evidence)


def _validate_edit(changes):
    keys = {"title", "description", "project_id", "workspace_id", "priority"}
    if type(changes) is not dict or changes.keys() != keys:
        raise ValueError("invalid editable Inbox fields")
    data = dict(changes)
    data["title"] = task_text(data["title"], 240)
    data["description"] = task_text(data["description"], 8000)
    if type(data["priority"]) is not str or data["priority"] not in PRIORITY_WEIGHTS:
        raise ValueError("invalid priority")
    project, workspace = data["project_id"], data["workspace_id"]
    if project is not None or workspace is not None:
        if not valid_scope(text(project), text(workspace)):
            raise ValueError("project/workspace mapping is invalid")
    return data


def _project(row):
    snapshot = json.loads(row["snapshot_json"])
    disposition = row["disposition"]
    if disposition == "IGNORED":
        status = "IGNORED"
    elif row["external_status"] != "OPEN":
        status = "SOURCE_CLOSED" if row["external_status"] == "CLOSED" else "SOURCE_CONFLICT"
    elif disposition == "UNASSIGNED":
        status = "PENDING_ASSIGNMENT"
    elif row["task_status"] != "DRAFT":
        status = row["task_status"]
    else:
        status = "CONFIRMED" if row["confirmation_json"] else "DRAFT"
    result = {key: row[key] for key in (
        "link_id", "task_id", "provider", "external_id", "object_type", "connection_id",
        "display_name", "project_id", "workspace_id", "assignment_source", "created_at", "updated_at",
        "revision", "event_id", "occurred_time", "received_at", "receipt_id",
    )}
    return {
        **result, "status": status, "disposition": disposition,
        "title": row["title"] or snapshot["title"],
        "description": row["instruction"] or snapshot["description"],
        "priority": row["priority"] or "NORMAL",
        "source_title": snapshot["title"], "source_description": snapshot["description"],
        "identities": snapshot.get("identities", {}),
    }
