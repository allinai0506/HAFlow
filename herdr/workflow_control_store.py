"""SQLite identity fences for Workflow controls and checkpoint intents."""
import hashlib
import json
import time
from contextlib import contextmanager

from . import recovery_store, state_db


class WorkflowIdentityChanged(ValueError):
    """A stale control was rejected before any state mutation."""


def fingerprint(conn, workflow_id):
    """Hash exactly the persisted Workflow and Task state used by checkpoints."""
    row = conn.execute('SELECT * FROM workflows WHERE workflow_id=?', (workflow_id,)).fetchone()
    tasks = [dict(r) for r in conn.execute('SELECT * FROM tasks WHERE workflow_id=? ORDER BY task_id', (workflow_id,))]
    return hashlib.sha256(json.dumps({'workflow': dict(row) if row else None, 'tasks': tasks},
                                     sort_keys=True).encode()).hexdigest()


def validate_identity(conn, workflow_id, expected):
    workflow, config, tasks = recovery_store._snapshot(conn, workflow_id)
    if expected is not None and (
            not expected.get('workflow_fingerprint')
            or expected['workflow_fingerprint'] != fingerprint(conn, workflow_id)
            or expected.get('execution_id') != workflow.get('execution_id')
            or expected.get('candidate_sha') != workflow.get('candidate_sha')):
        raise WorkflowIdentityChanged('workflow identity changed at write boundary')
    return workflow, config, tasks


@contextmanager
def control_transaction(db_path, workflow_id, expected):
    conn = state_db.get_db_connection(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        yield conn, validate_identity(conn, workflow_id, expected)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rollback(db_path, workflow_id, target_node_id, reason, expected):
    from .kernel import collect_downstream_nodes
    from .workflow import normalize_workflow
    with control_transaction(db_path, workflow_id, expected) as (conn, (wf, config, tasks)):
        nodes = {n['id']: n for n in normalize_workflow(config)['nodes']}
        if target_node_id not in nodes:
            raise ValueError(f"Target node '{target_node_id}' not in workflow nodes ({list(nodes)})")
        affected = collect_downstream_nodes(nodes, target_node_id)
        invalidated = []
        for task in tasks:
            if ((task.get('node') or task.get('stage')) not in affected or task.get('status') == 'superseded'
                    or (task.get('execution_id') and task['execution_id'] != wf.get('execution_id'))):
                continue
            state_db.transition_task(task['task_id'], 'superseded', f'rollback to {target_node_id}: {reason}',
                source='kernel_rollback', metadata={'superseded_reason': f'rollback to {target_node_id}: {reason}'},
                force=True, intent='rollback', conn=conn)
            invalidated.append(task['task_id'])
        advances = {key: value for key, value in (wf.get('stage_advancing') or {}).items() if key not in affected}
        overrides = dict(wf.get('gate_overrides') or {})
        override_history = list(wf.get('gate_overrides_history') or [])
        invalidated_overrides = []
        now = time.time()
        for node_id in sorted(affected):
            old = overrides.pop(node_id, None)
            if old:
                invalidated_overrides.append(node_id)
                override_history.append({'node_id': node_id, 'action': 'invalidated_by_rollback',
                    'rollback_target': target_node_id, 'reason': reason, 'timestamp': now, 'prior_override': old})
        result = {'ok': True, 'workflow_id': workflow_id, 'target_node_id': target_node_id,
                  'affected_nodes': sorted(affected), 'invalidated_tasks': invalidated,
                  'invalidated_gate_overrides': invalidated_overrides}
        history = [*wf.get('history', []), {'action': 'rollback', 'reason': reason, 'timestamp': now,
            **{key: result[key] for key in ('target_node_id', 'affected_nodes', 'invalidated_tasks', 'invalidated_gate_overrides')}}]
        state_db.update_workflow_metadata(workflow_id, {'stage_advancing': advances, 'gate_overrides': overrides,
            'gate_overrides_history': override_history, 'history': history}, conn=conn)
        return result
