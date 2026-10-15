"""Bounded read-only enrichment of task deliverables from authoritative facts."""
import json
import sqlite3

from . import state_db


def read_enrichment(tasks, db_path, *, observations=True, pull_requests=True):
    """Share one snapshot; cap artifact and PR reads for every owned task.

    These records enrich the catalog but cannot change task execution state.
    Missing optional tables/invalid records retain the catalog's soft-failure
    behavior. Workspace availability is still checked live by deliverables.
    """
    result = {}
    conn = state_db.get_readonly_db_connection(db_path)
    try:
        conn.execute('BEGIN')
        for task in tasks:
            tid, wid = task.get('task_id'), task.get('workflow_id')
            if not tid or (wid, tid) in result:
                continue
            extra = result[(wid, tid)] = {}
            # Pin existing task indexes: source/type indexes can otherwise scan
            # every workflow even with LIMIT 1/40 when this task has no rows.
            if observations:
                try:
                    rows = conn.execute(
                        "SELECT * FROM observations INDEXED BY idx_observations_task WHERE task_id=? AND source_type='artifact' "
                        "ORDER BY created_at, observation_id LIMIT 40", (str(tid),),
                    )
                    extra['observations'] = [state_db._decode_observation_row(row) for row in rows]
                except (sqlite3.Error, ValueError, TypeError):
                    extra['observations'] = []
            if pull_requests and wid:
                try:
                    row = conn.execute(
                        "SELECT payload_json FROM events INDEXED BY idx_events_task WHERE workflow_id=? AND task_id=? "
                        "AND event_type='pull_request_created' AND source='herdr-task' "
                        "ORDER BY timestamp DESC, id DESC LIMIT 1", (wid, tid),
                    ).fetchone()
                    if row:
                        extra['pull_request'] = json.loads(row['payload_json'] or '{}')
                except (sqlite3.Error, ValueError, TypeError):
                    print('[DELIVERABLES WARN] PR receipt could not be read', flush=True)
        return result
    finally:
        conn.close()
