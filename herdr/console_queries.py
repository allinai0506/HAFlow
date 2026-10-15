"""Read-only metadata for Console lists; never initialize the state database."""
import json

from .state_db import get_readonly_db_connection


def project_workflows(project_id=None, db_path=None):
    conn = get_readonly_db_connection(db_path)
    try:
        where=" WHERE json_extract(metadata_json, '$.project_id') = ?" if project_id else ''
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
