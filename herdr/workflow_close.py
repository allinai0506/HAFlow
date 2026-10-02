"""Workflow-scoped close ownership and crash-resumable receipts in StateStore DB.

Callers keep existing preflight gates. UI transports must enforce bounded timeouts
and verify immutable run/instance ownership immediately before deletion.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid


class CloseClaim:
    def __init__(self, connection, workflow_id, state, operation_id=None, receipt=None):
        self.connection = connection
        self.workflow_id = workflow_id
        self.state = state
        self.operation_id = operation_id
        self.receipt = receipt

    def action(self, key, identity, owns, execute):
        """Journal intent before I/O; resume only after rechecking original identity.

        owns(identity) must validate an immutable runtime instance, never Pane ID
        alone. A false result safely retains unknown or reassigned resources.
        """
        if self.state != 'owner': raise ValueError('close ownership required')
        row = self.connection.execute('SELECT identity,status,result FROM workflow_close_actions WHERE operation_id=? AND action_key=?', (self.operation_id,key)).fetchone()
        if row and row[1] != 'started': return json.loads(row[2])
        if row: identity = json.loads(row[0])
        else:
            with self.connection:
                self.connection.execute('INSERT INTO workflow_close_actions VALUES (?,?,?,?,?)', (self.operation_id,key,json.dumps(identity,sort_keys=True),'started','{}'))
        if not owns(identity): result = {'status':'skipped_foreign'}
        else: result = {'status':'completed', 'result':execute()}
        with self.connection:
            self.connection.execute('UPDATE workflow_close_actions SET status=?,result=? WHERE operation_id=? AND action_key=?', (result['status'],json.dumps(result),self.operation_id,key))
        return result

    def complete(self, report):
        if self.state != 'owner': raise ValueError('close ownership required')
        receipt = dict(report, operation_id=self.operation_id, close_state='completed')
        with self.connection:
            self.connection.execute('UPDATE workflow_close_operations SET state=?,receipt=? WHERE operation_id=?', ('completed',json.dumps(receipt),self.operation_id))
        self.receipt = receipt
        self.state = 'completed'
        return receipt


@contextmanager
def workflow_lifecycle_lock(store, workflow_id):
    """Serialize close and reopen without manufacturing an operation receipt."""
    if not workflow_id or len(workflow_id) > 128: raise ValueError('invalid workflow id')
    db = Path(store.db_path).resolve(); db.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(workflow_id.encode()).hexdigest()
    lockpath = db.parent / (db.name + '.close-' + digest + '.lock')
    with lockpath.open('a+b') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False; return
        try:
            yield True
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN)


@contextmanager
def workflow_close_claim(store, workflow_id, *, dry_run=False, options=None):
    """Nonblocking process lock; process death releases ownership automatically."""
    if not workflow_id or len(workflow_id) > 128: raise ValueError('invalid workflow id')
    if dry_run:
        yield CloseClaim(None,workflow_id,'dry_run'); return
    db = Path(store.db_path).resolve()
    with workflow_lifecycle_lock(store, workflow_id) as acquired:
        if not acquired:
            yield CloseClaim(None,workflow_id,'in_progress'); return
        connection = sqlite3.connect(db,timeout=5)
        try:
            with connection:
                columns = {row[1] for row in connection.execute('PRAGMA table_info(workflow_close_operations)')}
                if columns and 'generation' not in columns:
                    connection.execute('ALTER TABLE workflow_close_operations RENAME TO workflow_close_operations_legacy')
                connection.execute('CREATE TABLE IF NOT EXISTS workflow_close_operations (workflow_id TEXT NOT NULL, generation TEXT NOT NULL, operation_id TEXT UNIQUE NOT NULL, state TEXT NOT NULL, options TEXT NOT NULL, receipt TEXT, PRIMARY KEY(workflow_id,generation))')
                if columns and 'generation' not in columns:
                    connection.execute('INSERT INTO workflow_close_operations SELECT workflow_id,?,operation_id,state,options,receipt FROM workflow_close_operations_legacy', (json.dumps([None,None]),))
                connection.execute('CREATE TABLE IF NOT EXISTS workflow_close_actions (operation_id TEXT NOT NULL, action_key TEXT NOT NULL, identity TEXT NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(operation_id,action_key))')
            workflow = store.get_workflow(workflow_id) if hasattr(store, 'get_workflow') else {}
            generation = json.dumps([(workflow or {}).get('execution_id'), (workflow or {}).get('reopened_at')], sort_keys=True)
            row = connection.execute('SELECT operation_id,state,options,receipt FROM workflow_close_operations WHERE workflow_id=? AND generation=?',(workflow_id,generation)).fetchone()
            encoded = json.dumps(options or {},sort_keys=True)
            if row:
                if row[1] == 'completed':
                    yield CloseClaim(connection,workflow_id,'completed',row[0],json.loads(row[3])); return
                if row[2] != encoded: raise ValueError('close recovery options differ from original operation')
                operation_id = row[0]
            else:
                operation_id = uuid.uuid4().hex
                with connection: connection.execute('INSERT INTO workflow_close_operations VALUES (?,?,?,?,?,NULL)',(workflow_id,generation,operation_id,'in_progress',encoded))
            try:
                yield CloseClaim(connection,workflow_id,'owner',operation_id)
            except BaseException:
                # CLI preflight rejection has no teardown semantics. Do not
                # freeze rejected flags and prevent a later authorized retry.
                actions = connection.execute(
                    'SELECT 1 FROM workflow_close_actions WHERE operation_id=? LIMIT 1',
                    (operation_id,),
                ).fetchone()
                workflow = store.get_workflow(workflow_id) if hasattr(store, 'get_workflow') else None
                rejected_preflight = hasattr(store, 'get_workflow') and (workflow or {}).get('status') not in {'closing','completed'}
                if not actions and rejected_preflight:
                    with connection:
                        connection.execute(
                            'DELETE FROM workflow_close_operations WHERE operation_id=? AND state!=?',
                            (operation_id,'completed'),
                        )
                raise
        finally:
            connection.close()
