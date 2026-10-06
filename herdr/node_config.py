"""Audited workflow-local configuration snapshots, preserving shared project files."""
import hashlib
import json
from pathlib import Path
import os
import tempfile

from . import state_db
from .projects import validate_required_task_scope
from .task_resources import workflow_launch_lock
from .workflow import normalize_workflow


def read_configuration(store, workflow_id):
    workflow = store.get_workflow(workflow_id)
    if not workflow:
        raise ValueError('workflow not found')
    if workflow.get('config', {}).get('nodes'):
        config = workflow['config']
        raw = (json.dumps(config, ensure_ascii=False, indent=2) + '\n').encode()
        return workflow, config, hashlib.sha256(raw).hexdigest()
    path = Path(workflow.get('workflow_file') or '').expanduser()
    if not path.is_file() or path.stat().st_size > 1024*1024:
        raise ValueError('workflow configuration unavailable or exceeds budget')
    raw = path.read_bytes()
    return workflow, json.loads(raw), hashlib.sha256(raw).hexdigest()


def update_required_tasks(store, workflow_id, node_id, required_ids, *, expected_sha, reason):
    if not reason.strip() or len(reason) > 1000:
        raise ValueError('configuration change requires a bounded reason')
    with workflow_launch_lock(store.db_path, workflow_id):
        workflow, config, before = read_configuration(store,workflow_id)
        if before != expected_sha:
            raise ValueError('configuration changed; read current configuration and retry')
        if workflow['status'] in {'completed','failed','halted','abandoned','closing'}:
            raise ValueError('terminal workflow configuration cannot be changed')
        nodes = config.get('nodes')
        if not isinstance(nodes,list):
            raise ValueError('node configuration mutation requires explicit nodes')
        matches = [n for n in nodes if isinstance(n,dict) and (n.get('id') or n.get('key')) == node_id]
        if len(matches) != 1:
            raise ValueError('node missing or ambiguous')
        previous = matches[0].get('required_task_ids')
        matches[0]['required_task_ids'] = required_ids
        normalized = normalize_workflow(config)
        validate_required_task_scope(normalized,workflow_id,store)
        data = (json.dumps(config,ensure_ascii=False,indent=2)+'\n').encode()
        after = hashlib.sha256(data).hexdigest()
        directory = Path(store.db_path).resolve().parent/'workflow-configs'
        directory.mkdir(parents=True,exist_ok=True)
        identity = hashlib.sha256(workflow_id.encode()).hexdigest()[:24]
        target = directory/f'{identity}-{after}.json'
        if target.exists():
            if target.is_symlink() or target.read_bytes() != data:
                raise ValueError('configuration snapshot identity conflict')
        else:
            fd, temp = tempfile.mkstemp(prefix='.config-',dir=directory)
            try:
                with os.fdopen(fd,'wb') as stream:
                    stream.write(data);stream.flush();os.fsync(stream.fileno())
                os.chmod(temp,0o600)
                os.replace(temp,target)
                fd = os.open(directory,os.O_RDONLY)
                try: os.fsync(fd)
                finally: os.close(fd)
            finally: Path(temp).unlink(missing_ok=True)
        # Unreferenced immutable snapshots are harmless after interruption.
        # Pointer and audit are committed together in the existing authority.
        conn = state_db.get_db_connection(store.db_path)
        try:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT status, metadata_json, config_json FROM workflows WHERE workflow_id = ?', (workflow_id,)).fetchone()
            if row is None:
                raise ValueError('workflow disappeared during configuration update')
            fresh = {**json.loads(row['metadata_json'] or '{}'), 'status':row['status']}
            if fresh.get('workflow_file') != workflow.get('workflow_file') or fresh['status'] != workflow['status']:
                raise ValueError('workflow changed during configuration update')
            if workflow.get('config', {}).get('nodes'):
                raw = (json.dumps(json.loads(row['config_json']), ensure_ascii=False, indent=2) + '\n').encode()
            else:
                raw = Path(workflow['workflow_file']).read_bytes()
            if hashlib.sha256(raw).hexdigest() != before:
                raise ValueError('configuration changed during update')
            state_db.update_workflow_metadata(workflow_id,{'workflow_file':str(target)},conn=conn)
            conn.execute('UPDATE workflows SET config_json=? WHERE workflow_id=?', (json.dumps(config), workflow_id))
            result = {'workflow_id':workflow_id,'node_id':node_id,'before_sha':before,'config_sha':after,
                      'required_task_ids':required_ids,'previous_required_task_ids':previous,'reason':reason,
                      'workflow_file':str(target)}
            state_db.record_event({'event_type':'node_config_updated','workflow_id':workflow_id,
                                  'node_id':node_id,'source':'herdr-task','payload':result},conn=conn)
            conn.execute('COMMIT')
            return result
        except Exception:
            conn.execute('ROLLBACK');raise
        finally: conn.close()


def extend_budget(store, workflow_id, node_id, *, expected_sha, additional, operator, reason):
    """One explicit bounded grant; history is retained and concurrent grants CAS."""
    if type(additional) is not int or not 1 <= additional <= 8:
        raise ValueError('budget grant must be bounded to 1..8 additional tasks')
    if not operator.strip() or not reason.strip() or len(reason) > 1000:
        raise ValueError('operator and bounded reason required')
    from . import recovery_store
    with workflow_launch_lock(store.db_path, workflow_id), recovery_store._transaction(store.db_path) as conn:
        workflow, config, _ = recovery_store._snapshot(conn, workflow_id)
        raw = (json.dumps(config, ensure_ascii=False, indent=2) + '\n').encode()
        if hashlib.sha256(raw).hexdigest() != expected_sha:
            raise ValueError('configuration changed')
        if workflow.get('status') != 'running' or not workflow.get('execution_id'):
            raise ValueError('active workflow generation required')
        matches = [n for n in config.get('nodes', []) if n.get('id') == node_id]
        if len(matches) != 1 or type(matches[0].get('max_tasks_per_node')) is not int:
            raise ValueError('explicit finite node budget required')
        node = matches[0]
        old = node['max_tasks_per_node']
        if not 1 <= old < old + additional <= 64:
            raise ValueError('total budget must be bounded to 64 tasks')
        node['max_tasks_per_node'] = old + additional
        conn.execute('UPDATE workflows SET config_json=? WHERE workflow_id=?', (json.dumps(config), workflow_id))
        result = {'workflow_id': workflow_id, 'node_id': node_id,
                  'execution_id': workflow['execution_id'], 'previous_limit': old,
                  'max_tasks_per_node': old + additional, 'operator': operator, 'reason': reason}
        state_db.record_event({'event_type': 'node_budget_extended', 'workflow_id': workflow_id,
                              'node_id': node_id, 'source': 'herdr-task', 'payload': result}, conn=conn)
        return result
