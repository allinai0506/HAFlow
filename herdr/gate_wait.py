"""Durable business-gate wait responsibility; never creates acceptance evidence."""
import json
import time

from . import recovery_store as rs
from . import state_db
from .workflow_progress import _generation, _hash


def record_wait(db_path, workflow_id, node_id, missing, *, now=None):
    now = time.time() if now is None else now
    missing = sorted({str(item)[:256] for item in missing})[:128]
    with rs._transaction(db_path) as conn:
        workflow, config = rs._workflow_snapshot(conn, workflow_id)
        facts = {'kind': 'business_gate_wait', 'workflow_id': workflow_id, 'node_id': node_id,
                 'execution_id': workflow.get('execution_id'),
                 'generation': _generation(workflow),
                 'candidate_sha': workflow.get('candidate_sha'),
                 'candidate_episode_id': workflow.get('candidate_episode_id'),
                 'missing': missing, 'responsible': 'human'}
        identity = 'business_gate_wait:' + _hash(facts)
        inactive = (workflow.get('status') != 'running'
                    or node_id not in {node.get('id') for node in config.get('nodes') or []})
        rows = conn.execute("SELECT * FROM workflow_recovery_operations WHERE workflow_id=? "
                            "AND identity_key LIKE 'business_gate_wait:%' "
                            "AND status NOT IN ('resolved','superseded')", (workflow_id,)).fetchall()
        for row in rows:
            old = rs._decode(row)
            if (old['payload'].get('node_id') == node_id
                    and (not missing or inactive or old['identity_key'] != identity)):
                state = 'superseded' if missing else 'resolved'
                conn.execute("UPDATE workflow_recovery_operations SET status=?, "
                             "version=version+1,updated_at=? WHERE id=?", (state, now, row['id']))
                rs._event(conn, rs._get(conn, row['id']), 'gate_wait_resolved', now)
        if not missing or inactive:
            return None
        detail = {'reason': 'business_acceptance_missing', 'missing': missing,
                  'decision_needed': 'Rebase/relaunch the blocked gate on the current candidate; '
                                     'the gate must publish acceptance-record with its own artifacts.'}
        conn.execute('''INSERT INTO workflow_recovery_operations
            (identity_key,workflow_id,payload_json,status,attempts,detail_json,created_at,updated_at)
            VALUES (?,?,?,'observing',0,?,?,?) ON CONFLICT(identity_key) DO NOTHING''',
            (identity, workflow_id, json.dumps(facts), json.dumps(detail), now, now))
        row = conn.execute('SELECT * FROM workflow_recovery_operations WHERE identity_key=?', (identity,)).fetchone()
        op = rs._decode(row)
        if op['status'] in {'resolved', 'superseded'}:
            conn.execute("UPDATE workflow_recovery_operations SET status='observing',attempts=0,version=version+1,updated_at=? WHERE id=?", (now, op['id']))
            op = rs._get(conn, op['id'])
        if op['status'] == 'waiting_human':
            return op
        # Count sweeps at least 30 seconds apart, not calls from concurrent readers.
        count = min(3, op['attempts'] + int(op['attempts'] == 0 or now - op['updated_at'] >= 30))
        status = 'waiting_human' if count >= 3 else 'observing'
        if count != op['attempts']:
            conn.execute('UPDATE workflow_recovery_operations SET status=?,attempts=?,version=version+1,updated_at=? WHERE id=?',
                         (status, count, now, op['id']))
        result = rs._get(conn, op['id'])
        if status == 'waiting_human':
            state_db.record_event({'workflow_id': workflow_id, 'node_id': node_id,
                'event_type': 'business_gate_stalled', 'source': 'business_gate', 'timestamp': now,
                'payload': {**facts, **detail, 'operation_id': op['id']}}, conn=conn)
        return result
