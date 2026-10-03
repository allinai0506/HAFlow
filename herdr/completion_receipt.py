"""Run-scoped completion declarations and transactional consumption."""
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import shlex
import tempfile
import time
from . import state_db
from .completion import min_completion_seconds
from .workflow_docs import cli_path
from .observation import ObservationStore

COMPLETION_CREDENTIAL_TTL_SECONDS = 24 * 60 * 60


def _connection(store):
    conn = state_db.get_db_connection(store.db_path)
    conn.execute('''CREATE TABLE IF NOT EXISTS completion_receipts (
        receipt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
        epoch TEXT NOT NULL, payload TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0,
        UNIQUE(task_id,run_id,epoch))''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_completion_pending ON completion_receipts(consumed,task_id,run_id,epoch)")
    return conn


def _task(conn, task_id):
    row = conn.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown task')
    return state_db._decode_task_row(row)


def _workflow_open(conn, task):
    row = conn.execute('SELECT status FROM workflows WHERE workflow_id=?', (task.get('workflow_id'),)).fetchone()
    return row is not None and row[0] not in {'closing', 'completed', 'failed', 'halted', 'abandoned'}


def _timestamp(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _expiry_error(task, now):
    issued = _timestamp(task.get('completion_issued_at'))
    expires = _timestamp(task.get('completion_expires_at'))
    current = _timestamp(now)
    if (issued is None or expires is None or current is None
            or not 0 < expires - issued <= COMPLETION_CREDENTIAL_TTL_SECONDS):
        return 'completion_expiry_unknown'
    if current < issued:
        return 'completion_expiry_unknown'
    return 'completion_expired' if current >= expires else None


def _execution_start(task):
    return _timestamp(task.get('started_at')) or _timestamp(task.get('created_at')) or time.time()

def pending_completion_task_ids(store, limit=100, now=None, *, after_rowid=0, with_rows=False):
    now = time.time() if now is None else now
    conn = _connection(store)
    try:
        rows = conn.execute("""SELECT r.task_id,r.rowid FROM completion_receipts r
            JOIN tasks t ON t.task_id=r.task_id
            JOIN workflows w ON w.workflow_id=t.workflow_id
            WHERE w.status NOT IN ('closing','completed','failed','halted','abandoned')
              AND r.rowid > ? AND r.consumed=0 AND t.status IN ('working','dispatched','rework')
              AND json_extract(CASE WHEN json_valid(t.payload_json) THEN t.payload_json ELSE '{}' END,'$.run_id')=r.run_id
              AND json_extract(CASE WHEN json_valid(t.payload_json) THEN t.payload_json ELSE '{}' END,'$.completion_epoch')=r.epoch
              AND CAST(json_extract(CASE WHEN json_valid(t.payload_json) THEN t.payload_json ELSE '{}' END,'$.completion_issued_at') AS REAL) > 0
              AND CAST(json_extract(CASE WHEN json_valid(t.payload_json) THEN t.payload_json ELSE '{}' END,'$.completion_issued_at') AS REAL) <= ?
              AND CAST(json_extract(CASE WHEN json_valid(t.payload_json) THEN t.payload_json ELSE '{}' END,'$.completion_expires_at') AS REAL) > ?
              AND (CAST(json_extract(CASE WHEN json_valid(t.payload_json) THEN t.payload_json ELSE '{}' END,'$.completion_expires_at') AS REAL) - CAST(json_extract(CASE WHEN json_valid(t.payload_json) THEN t.payload_json ELSE '{}' END,'$.completion_issued_at') AS REAL)) BETWEEN 0.000001 AND ?
              AND CAST(json_extract(CASE WHEN json_valid(t.payload_json) THEN t.payload_json ELSE '{}' END,
                                    '$.completion_epoch_started_at') AS REAL) <= ?
            ORDER BY r.rowid LIMIT ?""", (after_rowid, now, now,
                                         COMPLETION_CREDENTIAL_TTL_SECONDS,
                                         now - min_completion_seconds(),
                                         max(1, min(int(limit), 100))),).fetchall()
        return [(row[0], row[1]) for row in rows] if with_rows else [row[0] for row in rows]
    finally:
        conn.close()


def issue_completion_contract(task_id, store, *, prepare=None, renew=False):
    token = secrets.token_urlsafe(32)
    epoch = secrets.token_hex(16)
    conn = _connection(store)
    path = None
    try:
        conn.execute('BEGIN IMMEDIATE')
        task = _task(conn, task_id)
        if not _workflow_open(conn, task):
            raise ValueError('Workflow is closed')
        if not task.get('run_id'):
            raise ValueError('Task has no run identity')
        if renew:
            if task.get('completion_protocol') != 'receipt-v1' or not task.get('completion_epoch') or task['status'] not in ('dispatched','working','rework'):
                raise ValueError('Renewal requires an executing receipt-v1 task')
            epoch = task['completion_epoch']
        issued_at = time.time()
        directory = Path(store.db_path).parent / 'completion-credentials'
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        identity = {'task_id': task_id, 'run_id': task['run_id'], 'epoch': epoch, 'token': token}
        fd, path = tempfile.mkstemp(prefix='receipt-', suffix='.json', dir=directory)
        with os.fdopen(fd, 'w') as stream:
            json.dump(identity, stream)
            stream.flush()
            os.fsync(stream.fileno())
        contract = {'path': path, 'epoch': epoch}
        if prepare is not None:
            prepare(conn, contract)
        # Persist through the existing task metadata authority, in this transaction.
        state_db.update_task_metadata(task_id, {
            'completion_protocol': 'receipt-v1', 'completion_epoch': epoch,
            'completion_identity_path': path,
            'completion_issued_at': issued_at,
            'completion_expires_at': issued_at + COMPLETION_CREDENTIAL_TTL_SECONDS,
            'completion_token_hash': hashlib.sha256(token.encode()).hexdigest(),
            'completion_epoch_started_at': (task.get('completion_epoch_started_at') if renew else time.time() if task.get('completion_protocol') == 'receipt-v1' or task['status'] == 'rework'
                                            else _execution_start(task))
        }, conn=conn)
        conn.commit()
        return contract
    except Exception:
        conn.rollback()
        if path is not None:
            Path(path).unlink(missing_ok=True)
        raise
    finally:
        conn.close()


def completion_instruction(task_id, store):
    contract = issue_completion_contract(task_id, store)
    return ('\nWhen finished, submit the structured completion declaration:\n'
            + shlex.quote(str(cli_path())) + ' report-completion '
            + shlex.quote(task_id) + ' --identity-file ' + shlex.quote(contract['path'])
            + '\nThe server credential expires after 24 hours. If expired, ask the controller to run '
            + shlex.quote(str(cli_path())) + ' renew-completion with a new --operation-id.\nThis declaration does not certify acceptance, merge, or deployment.\n')

def report_completion(task_id, identity, artifacts, store):
    if not isinstance(artifacts, list) or len(artifacts) > 20:
        raise ValueError('Artifact references exceed budget')
    observations = ObservationStore(store.db_path)
    conn = _connection(store)
    try:
        conn.execute('BEGIN IMMEDIATE')
        task = _task(conn, task_id)
        if not _workflow_open(conn, task):
            raise ValueError('Workflow is closed')
        valid = (identity.get('task_id') == task_id and identity.get('run_id') == task.get('run_id')
                 and identity.get('epoch') == task.get('completion_epoch')
                 and hmac.compare_digest(hashlib.sha256(str(identity.get('token', '')).encode()).hexdigest(),
                                         task.get('completion_token_hash', '')))
        if not valid:
            raise ValueError('Completion identity mismatch')
        expiry_error = _expiry_error(task, time.time())
        if expiry_error:
            raise ValueError('Completion expiry rejected: ' + expiry_error)
        from .task_checkpoint import validate_checkpoint_artifact
        for reference in artifacts:
            validate_checkpoint_artifact(task, reference, store=store, observations=observations)
        row = conn.execute('SELECT payload FROM completion_receipts WHERE task_id=? AND run_id=? AND epoch=?',
                           (task_id, task['run_id'], identity['epoch'])).fetchone()
        if row:
            conn.commit()
            original = json.loads(row[0])
            if original['artifacts'] != artifacts:
                raise ValueError('Completion receipt conflict')
            return original
        if task['status'] not in ('pending', 'dispatched', 'working', 'rework'):
            raise ValueError('Task is not executing')
        receipt = {'receipt_id': secrets.token_hex(16), 'task_id': task_id, 'run_id': task['run_id'],
                   'epoch': identity['epoch'], 'artifacts': artifacts, 'received_at': time.time()}
        conn.execute('INSERT INTO completion_receipts(receipt_id,task_id,run_id,epoch,payload) VALUES(?,?,?,?,?)',
                     (receipt['receipt_id'], task_id, task['run_id'], identity['epoch'], json.dumps(receipt)))
        conn.commit()
        return receipt
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def consume_completion_receipt(task_id, store, now=None):
    observations = ObservationStore(store.db_path)
    conn = _connection(store)
    try:
        conn.execute('BEGIN IMMEDIATE')
        task = _task(conn, task_id)
        row = conn.execute('SELECT receipt_id,payload FROM completion_receipts WHERE task_id=? AND run_id=? AND epoch=? AND consumed=0',
                           (task_id, task.get('run_id'), task.get('completion_epoch'))).fetchone()
        effective_now = _timestamp(time.time() if now is None else now)
        expiry_error = _expiry_error(task, effective_now)
        if expiry_error:
            conn.rollback()
            return {'accepted': False, 'reason': expiry_error}
        epoch_start = _timestamp(task.get('completion_epoch_started_at'))
        elapsed = (effective_now - max(_execution_start(task), epoch_start)) if effective_now and epoch_start else None
        if not _workflow_open(conn, task) or not row or task['status'] not in ('dispatched', 'working', 'rework') or elapsed is None or elapsed < min_completion_seconds():
            conn.rollback()
            return {'accepted': False}
        from .completion import verification_execution_complete_for_rework, retry_execution_complete_for_rework
        if not (verification_execution_complete_for_rework(task, store)
                and retry_execution_complete_for_rework(task, store)):
            conn.rollback()
            return {'accepted': False, 'reason': 'intervention_execution_pending'}
        from .task_checkpoint import validate_checkpoint_artifact
        try:
            for reference in json.loads(row[1])['artifacts']:
                validate_checkpoint_artifact(task, reference, store=store, observations=observations)
        except (ValueError, OSError):
            # Keep execution state untouched; quarantine a broken declaration
            # so a poison receipt cannot starve the bounded recovery batch.
            conn.execute('UPDATE completion_receipts SET consumed=2 WHERE receipt_id=?', (row[0],))
            state_db.record_event({'event_type': 'completion_receipt_rejected', 'task_id': task_id,
                'workflow_id': task.get('workflow_id'), 'run_id': task.get('run_id'),
                'source': 'controller', 'payload': {'receipt_id': row[0], 'epoch': task.get('completion_epoch'),
                                                   'reason': 'artifact_integrity_unknown'}}, conn=conn)
            conn.commit()
            return {'accepted': False, 'reason': 'artifact_integrity_unknown'}
        result = state_db.transition_task(task_id, 'agent_done', reason='structured_completion_receipt',
                                         source='controller', conn=conn, expected_status=task['status'],
                                         expected_version=task['version'])
        if result.get('accepted'):
            conn.execute('UPDATE completion_receipts SET consumed=1 WHERE receipt_id=?', (row[0],))
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def authorize_legacy_completion(task_id, run_id, expected_version, reason, store):
    """Operator authorizes a receipt credential, never certifies completion.

    Explicit run/version binds an inspected legacy execution. No screen, idle
    state or artifact can trigger this operation automatically.
    """
    if not isinstance(reason, str) or not reason.strip() or len(reason.encode()) > 2048:
        raise ValueError('Completion authorization requires a bounded evidence reason')
    def prepare(conn, contract):
        task = _task(conn, task_id)
        if (task.get('run_id') != run_id or not run_id or task.get('version') != expected_version
                or task.get('status') not in {'working','dispatched','rework'}
                or task.get('completion_protocol') == 'receipt-v1'):
            raise ValueError('Legacy execution identity/version changed or already authorized')
        state_db.record_event({'event_type':'completion_authorized','workflow_id':task.get('workflow_id'),
            'task_id':task_id,'run_id':run_id,'source':'herdr-task','timestamp':time.time(),
            'payload':{'run_id':run_id,'expected_version':expected_version,'reason':reason,
                       'epoch':contract['epoch'],'certifies_success':False}}, conn=conn)
    return issue_completion_contract(task_id, store, prepare=prepare)
