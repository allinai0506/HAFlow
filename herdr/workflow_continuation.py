"""Pure obligations for an idle, unfinished planned node; no execution authority."""
import hashlib
import json

DELIVERED = frozenset({'completed', 'committed', 'integrated', 'cleanup_ready', 'cleaned'})


def pending_continuations(workflow, config, tasks):
    if workflow.get('status') != 'running' or workflow.get('startup_ready') is False:
        return []
    if workflow.get('outcome') in {'delivered', 'abandoned'} or workflow.get('completed_at'):
        return []
    wid = workflow.get('workflow_id')
    owned = [t for t in tasks if t.get('workflow_id') == wid]
    active = [t for t in owned if t.get('status') != 'superseded' and not t.get('superseded_by')]
    if not owned or any(t.get('status') not in DELIVERED or
                         t.get('stage_verdict') == 'blocked' or t.get('finalize_escalated') or
                         (t.get('integration_mode') == 'git' and t.get('status') in {'completed', 'committed'})
                         for t in active):
        return []
    pending = []
    for node in config.get('nodes') or []:
        required = node.get('required_task_ids')
        if not isinstance(required, list) or not required or len(required) > 64:
            continue
        if any(not isinstance(tid, str) or not tid.strip() for tid in required):
            continue
        node_id = node.get('id')
        node_tasks = [t for t in active if (t.get('node') or t.get('stage')) == node_id]
        node_history = [t for t in owned if (t.get('node') or t.get('stage')) == node_id]
        if not node_history:
            continue
        # A successor already ran: adoption into the original base need not be
        # meaningful (e.g. candidate branches). Its own gates own that result.
        successors = {n.get('id') for n in config.get('nodes') or []
                      if node_id in (n.get('depends_on') or [])}
        if any((t.get('node') or t.get('stage')) in successors for t in active):
            continue
        by_id = {t.get('task_id'): t for t in owned
                 if (t.get('node') or t.get('stage')) == node_id}
        missing = []
        for tid in required:
            current, seen = tid, set()
            while current not in seen:
                seen.add(current)
                task = by_id.get(current)
                if task is None:
                    missing.append(tid)
                    break
                if task.get('status') != 'superseded' and not task.get('superseded_by'):
                    break
                current = task.get('superseded_by')
            else:
                missing.append(tid)
        deliveries = [{'task_id': t['task_id'], 'sha': t.get('integrated_commit'),
                       'adoption': 'unknown'} for t in node_tasks if t.get('integration_mode') == 'git']
        pending.append({'workflow_id': wid, 'node_id': node_id, 'missing_task_ids': missing,
                'deliveries': deliveries, 'target_branch': workflow.get('candidate_branch') or workflow.get('base_branch'),
                'target_sha': None, 'last_progress_at': max(float(t.get('updated_at') or 0) for t in owned)})
    return pending


def finish_continuation(pending, target_sha, adoption):
    if pending is None:
        return None
    result = dict(pending, target_sha=target_sha)
    result['deliveries'] = [dict(d, adoption=adoption.get(d['task_id'], 'unknown'))
                            for d in pending['deliveries']]
    unresolved = [d for d in result['deliveries'] if d['adoption'] != 'adopted']
    if not result['missing_task_ids'] and not unresolved:
        return None
    identity = {k: result[k] for k in ('workflow_id', 'node_id', 'missing_task_ids',
                                      'deliveries', 'target_sha', 'target_branch', 'last_progress_at')}
    result['fingerprint'] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    missing = ', '.join(result['missing_task_ids']) or '无'
    unadopted = ', '.join(d['task_id'] for d in unresolved) or '无'
    result['message'] = f"等待协调推进：未派发 {missing}；成果已接收但未确认采用 {unadopted}"
    return result
