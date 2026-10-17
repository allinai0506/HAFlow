"""Read-only metadata for Console lists; never initialize the state database."""
import json

from .state_db import get_readonly_db_connection


def project_workflows(project_id=None, db_path=None):
    conn = get_readonly_db_connection(db_path)
    try:
        where=" WHERE CASE WHEN json_valid(metadata_json) THEN json_extract(metadata_json, '$.project_id') END = ?" if project_id else ''
        rows = conn.execute('SELECT * FROM workflows'+where+
            ' ORDER BY created_at DESC, workflow_id DESC', (project_id,) if project_id else ())
        result = []
        for row in rows:
            workflow = json.loads(row['metadata_json'] or '{}')
            workflow.update({key: row[key] for key in (
                'workflow_id', 'title', 'status', 'template_name',
                'current_stage', 'created_at', 'updated_at')})
            workflow['config'] = json.loads(row['config_json'] or '{}')
            result.append(workflow)
        return result
    finally:
        conn.close()


def project_agent_loads(project_id, statuses, db_path=None):
    conn = get_readonly_db_connection(db_path)
    try:
        marks = ','.join('?' for _ in statuses)
        rows = conn.execute(f'''SELECT agent, COUNT(*) AS load FROM tasks
            WHERE json_extract(payload_json, '$.project_id') = ?
              AND status IN ({marks}) AND agent IS NOT NULL GROUP BY agent''',
            (project_id, *statuses))
        return {row['agent']: row['load'] for row in rows}
    finally:
        conn.close()


def workflow_snapshot(workflow_id, db_path=None):
    """One workflow snapshot (metadata + config + frozen candidate), or None.

    Scoped read: the requested row and its current-epoch candidate event only.
    Never initializes the database.
    """
    from .recovery_store import _workflow_snapshot
    conn = get_readonly_db_connection(db_path)
    try:
        conn.execute('BEGIN')
        row = conn.execute(
            'SELECT workflow_id FROM workflows WHERE workflow_id = ?', (workflow_id,)
        ).fetchone()
        if row is None:
            return None
        return _workflow_snapshot(conn, workflow_id)[0]
    finally:
        conn.close()


def project_workflow_snapshots(project_id, db_path=None):
    """Full snapshots for one project's workflows, filtered in SQLite first.

    ``project_workflows`` above stays the lightweight metadata-only variant.
    Corrupt metadata rows are skipped by the filter instead of failing the read.
    """
    from .recovery_store import _workflow_snapshot
    conn = get_readonly_db_connection(db_path)
    try:
        conn.execute('BEGIN')
        rows = conn.execute(
            "SELECT workflow_id FROM workflows "
            "WHERE CASE WHEN json_valid(metadata_json) "
            "THEN json_extract(metadata_json, '$.project_id') END = ? "
            "ORDER BY created_at DESC, workflow_id DESC",
            (project_id,),
        ).fetchall()
        return [_workflow_snapshot(conn, row['workflow_id'])[0] for row in rows]
    finally:
        conn.close()


def workflow_tasks(workflow_id, db_path=None):
    """Tasks of one workflow, decoded exactly like the shared task list."""
    from . import state_db
    conn = get_readonly_db_connection(db_path)
    try:
        return [state_db._decode_task_row(row) for row in conn.execute(
            'SELECT * FROM tasks WHERE workflow_id = ? ORDER BY created_at ASC, task_id ASC',
            (workflow_id,))]
    finally:
        conn.close()


def read_inventory(db_path=None):
    """Return (tasks, workflows_by_id) from one read-only SQLite snapshot.

    Frozen candidate identity is authoritative only within the workflow's current
    creation/reopen epoch, matching recovery_store._workflow_snapshot.
    """
    from . import state_db
    conn = state_db.get_readonly_db_connection(db_path)
    try:
        conn.execute('BEGIN')
        tasks = [state_db._decode_task_row(row) for row in conn.execute(
            'SELECT * FROM tasks ORDER BY created_at ASC, task_id ASC')]
        workflows = {}
        for row in conn.execute('SELECT * FROM workflows ORDER BY created_at DESC'):
            workflow = json.loads(row['metadata_json'] or '{}')
            workflow.update({key: row[key] for key in (
                'workflow_id', 'title', 'template_name', 'current_stage', 'status',
                'created_at', 'updated_at')})
            workflow['config'] = json.loads(row['config_json'] or '{}')
            workflow['candidate_sha'] = None
            workflow['candidate_episode_id'] = 0
            workflows[row['workflow_id']] = workflow
        # Filter by indexed event type before grouping; fetch at most one
        # qualifying candidate per workflow rather than all historical events.
        rows = conn.execute('''SELECT e.workflow_id, e.id, e.payload_json
            FROM events e JOIN (
                SELECT events.workflow_id, MAX(events.id) AS id
                FROM events JOIN workflows w ON w.workflow_id=events.workflow_id
                WHERE events.event_type='candidate_frozen'
                  AND events.source='critical-path-scheduler'
                  AND events.timestamp >= MAX(COALESCE(w.created_at,0),
                    CAST(COALESCE(json_extract(w.metadata_json,'$.reopened_at'),0) AS REAL))
                GROUP BY events.workflow_id
            ) latest ON latest.id=e.id''')
        for row in rows:
            workflow = workflows[row['workflow_id']]
            workflow['candidate_sha'] = json.loads(row['payload_json']).get('candidate_sha')
            workflow['candidate_episode_id'] = row['id']
        return tasks, workflows
    finally:
        conn.close()
