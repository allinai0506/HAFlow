"""Authorize forward effects against one current business-evidence cohort."""
from . import recovery_store, state_db, task_checkpoint as checkpoint
from .workflow_progress import active_tasks, _hash


def _workflow_key(workflow, config):
    return _hash({'workflow': {k: workflow.get(k) for k in
        ('workflow_id', 'execution_id', 'status', 'candidate_sha', 'candidate_episode_id', 'created_at', 'reopened_at')},
        'config': config})


def _eligible(workflow, task):
    candidate = workflow.get('candidate_sha')
    return bool(candidate and workflow.get('execution_id')
        and task.get('workflow_id') == workflow.get('workflow_id')
        and task.get('execution_id') == workflow['execution_id']
        and task.get('candidate_sha') == candidate == task.get('verified_candidate_sha')
        and task.get('status') in {'completed', 'committed', 'integrated', 'cleanup_ready', 'cleaned'}
        and not task.get('superseded_by')
        and task.get('stage_verdict') in {'pass', 'approved'}
        and task.get('candidate_episode_id', workflow.get('candidate_episode_id')) == workflow.get('candidate_episode_id'))


def business_gates_blockers(store, workflow, gates, gate_nodes=None):
    """Hash outside SQL transactions, then recheck the entire cohort together."""
    selected = [t for t in gates if t.get('completion_protocol') == 'receipt-v1']
    if not selected:
        return []
    ids = {t['task_id'] for t in selected}
    if len(gates) > 128 or not getattr(store, 'db_path', None):
        return sorted(ids)
    def cohort(wf, tasks):
        return sorted(t['task_id'] for t in active_tasks(wf, tasks)
                      if (t.get('node') or t.get('stage')) in gate_nodes) if gate_nodes else []
    reader = state_db.get_readonly_db_connection(store.db_path)
    try:
        reader.execute('BEGIN')
        initial, config, tasks = recovery_store._snapshot(reader, workflow['workflow_id'])
        by_id = {t['task_id']: t for t in tasks}
        if _workflow_key(initial, config) != _workflow_key(workflow, workflow.get('config') or config):
            return sorted(ids)
        membership = cohort(initial, tasks)
        if gate_nodes and membership != sorted(t['task_id'] for t in gates):
            return sorted(ids) or ['<unknown:gate-cohort>']
        receipts = {}
        invalid = []
        for task in selected:
            current = by_id.get(task['task_id'], {})
            receipt = checkpoint.business_acceptance_receipt(reader, current, initial.get('candidate_sha'))
            if (not _eligible(initial, current) or current.get('version') != task.get('version')
                    or checkpoint._business_scope(current, initial) != checkpoint._business_scope(task, workflow) or not receipt):
                invalid.append(task['task_id'])
            else:
                receipts[task['task_id']] = receipt['id']
        if invalid:
            return sorted(invalid)
    except (ValueError, OSError):
        return sorted(ids)
    finally:
        reader.close()
    invalid = [t['task_id'] for t in selected
               if not checkpoint.has_business_acceptance(store, t, initial['candidate_sha'])]
    if invalid:
        return sorted(invalid)
    reader = state_db.get_readonly_db_connection(store.db_path)
    try:
        reader.execute('BEGIN')
        fresh, fresh_config, fresh_tasks = recovery_store._snapshot(reader, workflow['workflow_id'])
        if (_workflow_key(fresh, fresh_config) != _workflow_key(initial, config)
                or cohort(fresh, fresh_tasks) != membership):
            return sorted(ids)
        by_id = {t['task_id']: t for t in fresh_tasks}
        for task in selected:
            current = by_id.get(task['task_id'], {})
            receipt = checkpoint.business_acceptance_receipt(reader, current, fresh.get('candidate_sha'))
            if (current.get('version') != task.get('version')
                    or checkpoint._business_scope(current, fresh) != checkpoint._business_scope(task, initial)
                    or not receipt or receipt['id'] != receipts[task['task_id']]):
                invalid.append(task['task_id'])
        return sorted(invalid)
    except (ValueError, OSError):
        return sorted(ids)
    finally:
        reader.close()


def business_gate_blockers(store, workflow, config, tasks, node_id=None):
    """Preflight downstream receipt-v1 dependencies; unknown never means rework.

    Legacy tasks without a business completion contract keep their existing
    stage policy; this does not certify their business acceptance.
    """
    nodes = {n['id']: n for n in config.get('nodes') or []}
    if not nodes and any(t.get('workflow_id') == workflow.get('workflow_id')
                         and t.get('completion_protocol') == 'receipt-v1'
                         and (t.get('node') or t.get('stage')) in {'test', 'review'} for t in tasks):
        return ['<unknown:configuration>']
    if node_id is None:
        dependencies = set(nodes)
    else:
        dependencies, pending = set(), list((nodes.get(node_id) or {}).get('depends_on') or [])
        while pending:
            dependency = pending.pop()
            if dependency in dependencies:
                continue
            dependencies.add(dependency)
            pending.extend((nodes.get(dependency) or {}).get('depends_on') or [])
    missing, gates = [], []
    current = active_tasks(workflow, tasks)
    for dependency in sorted(dependencies & {'test', 'review'}):
        verifiers = [t for t in current if (t.get('node') or t.get('stage')) == dependency]
        requires_business = any(t.get('completion_protocol') == 'receipt-v1' for t in current)
        if not verifiers and workflow.get('candidate_sha') and requires_business:
            missing.append('<missing:' + dependency + '>')
        gates.extend(verifiers)
    missing.extend(business_gates_blockers(store, workflow, gates, dependencies & {'test', 'review'}))
    return sorted(set(missing))
