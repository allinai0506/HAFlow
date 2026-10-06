"""Atomically bind a delivered recovery round while retaining committed history."""
import json
import re
from . import state_db


def delivery_confirmed(task, receipts=()):
    """Recognize only the current run's durable INITIAL delivery receipt."""
    if not task.get('run_id') or not task.get('completion_identity_path') or not task.get('completion_epoch'):
        return False
    for receipt in receipts:
        payload = receipt.get('payload') or {}
        if (receipt.get('event_type') == 'initial_dispatched'
                and receipt.get('source') == 'herdr-task'
                and receipt.get('task_id') == task.get('task_id')
                and receipt.get('run_id') == task['run_id']
                and receipt.get('workflow_id') == task.get('workflow_id')
                and receipt.get('node_id') == (task.get('node') or task.get('stage'))
                and payload.get('delivery_phase') == 'dispatched'
                and payload.get('completion_epoch') == task['completion_epoch']
                and payload.get('identity_path') == task['completion_identity_path']
                and payload.get('intervention_id')):
            return True
    return False


def has_confirmed_delivery(store, task):
    conn = state_db.get_readonly_db_connection(store.db_path)
    try:
        return delivery_confirmed(task, _receipts(conn, task))
    finally:
        conn.close()



def confirmed_rework(conn, task, request_id):
    """Bind delivery to this request; a prior same-Run delivery is insufficient."""
    if (not request_id or task.get('rework_request_id') != request_id
            or task.get('rework_delivery') != 'delivered'):
        return False
    if task.get('completion_protocol') != 'receipt-v1':
        return bool(task.get('run_id'))
    row = conn.execute("SELECT payload_json FROM events WHERE task_id=? AND run_id=? AND workflow_id=? AND node_id=? AND event_type='rework_dispatched' AND source='herdr-task' AND json_extract(payload_json,'$.intervention_id')=? ORDER BY id DESC LIMIT 1",
                       (task['task_id'], task.get('run_id'), task.get('workflow_id'), task.get('node') or task.get('stage'), request_id)).fetchone()
    if not row:
        return False
    receipt = json.loads(row['payload_json'] or '{}')
    return (receipt.get('delivery_phase') == 'dispatched'
            and bool(task.get('completion_epoch'))
            and receipt.get('completion_epoch') == task['completion_epoch']
            and bool(task.get('completion_identity_path'))
            and receipt.get('identity_path') == task['completion_identity_path'])


def has_confirmed_rework(store, task, request_id):
    conn = state_db.get_readonly_db_connection(store.db_path)
    try:
        return confirmed_rework(conn, task, request_id)
    finally:
        conn.close()

def _receipts(conn, task):
    rows = conn.execute("SELECT * FROM events WHERE task_id=? AND run_id=? AND event_type='initial_dispatched' AND source='herdr-task' ORDER BY id DESC LIMIT 100",
                        (task['task_id'], task.get('run_id'))).fetchall()
    return [dict(row, payload=json.loads(row['payload_json'] or '{}')) for row in rows]


def _registered_intent(conn, predecessor, successor, candidate_sha):
    rows = conn.execute("SELECT payload_json FROM events WHERE event_type='launch_intent' AND source='launch' AND json_extract(payload_json,'$.intent_id')=? ORDER BY id DESC LIMIT 1",
                        (successor.get('launch_intent_id'),)).fetchall()
    if not rows:
        return False
    intent = json.loads(rows[0]['payload_json'] or '{}')
    return (intent.get('phase') == 'registered'
            and intent.get('task_id') == successor['task_id']
            and intent.get('supersedes') == predecessor['task_id']
            and intent.get('workflow_id') == successor.get('workflow_id')
            and intent.get('node_id') == (successor.get('node') or successor.get('stage'))
            and intent.get('role') == successor.get('dispatch_role')
            and intent.get('dispatch_round') == successor.get('dispatch_round')
            and intent.get('candidate_sha') == candidate_sha
            and (intent.get('resources') or {}).get('run_id') == successor.get('run_id'))


def link_committed_successor(store, predecessor_id, successor_id, expected_version, candidate_sha, reason):
    """Validate durable launch/delivery and write both lineage ends in one transaction."""
    if predecessor_id == successor_id or not re.fullmatch(r'[0-9a-f]{40}', candidate_sha or ''):
        raise ValueError('recovery requires distinct tasks and exact commit SHA')
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('recovery reason required')
    conn = state_db.get_db_connection(store.db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        def read(task_id):
            row=conn.execute('SELECT * FROM tasks WHERE task_id=?',(task_id,)).fetchone()
            if not row: raise ValueError('recovery task not found')
            return state_db._decode_task_row(row)
        old, new = read(predecessor_id), read(successor_id)
        workflow_row = conn.execute('SELECT metadata_json FROM workflows WHERE workflow_id=?',
                                    (old.get('workflow_id'),)).fetchone()
        workflow = json.loads(workflow_row['metadata_json'] or '{}') if workflow_row else {}
        execution_id = workflow.get('execution_id')
        if (not execution_id or new.get('execution_id') != execution_id
                or (old.get('execution_id') and old['execution_id'] != execution_id)):
            raise ValueError('recovery requires authoritative workflow execution identity')
        if old.get('status') != 'committed' or old.get('commit') != candidate_sha:
            raise ValueError('predecessor must retain exact committed candidate')
        if old.get('superseded_by') and old['superseded_by'] != successor_id:
            raise ValueError('predecessor already has another successor')
        if new.get('supersedes') and new['supersedes'] != predecessor_id:
            raise ValueError('successor already belongs to another predecessor')
        if (not old.get('workflow_id') or new.get('workflow_id') != old['workflow_id']
                or not (old.get('node') or old.get('stage'))
                or (new.get('node') or new.get('stage')) != (old.get('node') or old.get('stage'))
                or not new.get('run_id') or not old.get('run_id') or new['run_id'] == old['run_id']
                or not old.get('dispatch_role') or new.get('dispatch_role') != old['dispatch_role']
                or type(old.get('dispatch_round')) is not int
                or type(new.get('dispatch_round')) is not int
                or new['dispatch_round'] != old['dispatch_round'] + 1
                or new.get('candidate_sha') != candidate_sha
                or new.get('status') not in ('dispatched', 'working', 'rework', 'agent_done',
                                           'completed', 'committed', 'integrated', 'cleanup_ready', 'cleaned')
                or not _registered_intent(conn, old, new, candidate_sha)
                or not delivery_confirmed(new, _receipts(conn, new))):
            raise ValueError('successor identity or durable delivery unconfirmed')
        lineage = dict(predecessor_id=predecessor_id, successor_id=successor_id,
                       candidate_sha=candidate_sha, predecessor_run_id=old['run_id'],
                       successor_run_id=new['run_id'], dispatch_round=new['dispatch_round'])
        if old.get('superseded_by') == successor_id:
            if new.get('supersedes') != predecessor_id or new.get('recovery_lineage') != lineage:
                raise ValueError('incomplete or conflicting recovery lineage')
            conn.execute('COMMIT')
            return dict(ok=True, already_applied=True, **lineage)
        if old['version'] != expected_version:
            raise ValueError('predecessor version changed')
        state_db.update_task_metadata(predecessor_id, {'superseded_by':successor_id}, conn=conn)
        state_db.update_task_metadata(successor_id, {'supersedes':predecessor_id, 'recovery_lineage':lineage}, conn=conn)
        event=state_db.record_event(dict(event_type='committed_successor_linked',
            workflow_id=old['workflow_id'],node_id=old.get('node') or old.get('stage'),
            task_id=predecessor_id,run_id=old['run_id'],source='recovery',
            payload=dict(lineage,reason=reason)),conn=conn)
        conn.execute('COMMIT')
        return dict(ok=True, already_applied=False, event_id=event['id'], **lineage)
    except BaseException:
        if conn.in_transaction: conn.execute('ROLLBACK')
        raise
    finally:
        conn.close()


def choose_recovery_source(predecessor, project_root):
    """Select one clean registered repository containing the exact local commit."""
    from pathlib import Path
    import subprocess
    from .repo_hygiene import check_source_cleanliness

    candidate = predecessor.get('commit')
    if not isinstance(candidate, str) or not re.fullmatch(r'[0-9a-f]{40}', candidate):
        raise ValueError('source_candidate_unavailable')
    for source in (project_root, predecessor.get('clone_path')):
        if not source:
            continue
        try:
            path = str(Path(source).expanduser().resolve())
            resolved = subprocess.run(
                ['git', '-C', path, 'rev-parse', '--verify', f'{candidate}^{{commit}}'],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if resolved.returncode or resolved.stdout.strip() != candidate:
            continue
        clean, _ = check_source_cleanliness(path)
        if not clean:
            raise ValueError('source_wip_requires_decision')
        return path
    raise ValueError('source_candidate_unavailable')
