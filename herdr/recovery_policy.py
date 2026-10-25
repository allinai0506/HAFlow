"""Finite recovery budgets and verified progress, using the original operation row."""
import hashlib
import json

IDLE_SECONDS = 3600
MAX_ATTEMPTS = 3
VERIFY_SECONDS = 600


class PreflightTemporaryFailure(Exception):
    """Only raised at the Controller's read-only source cleanliness probe."""


def initial_budget(config, now, event_floor):
    config = config.get('recovery_budget', {})
    if not isinstance(config, dict):
        raise ValueError('recovery_budget must be an object')
    idle = config.get('idle_seconds', IDLE_SECONDS)
    total = config.get('total_seconds', IDLE_SECONDS * MAX_ATTEMPTS)
    if (type(idle) is not int or type(total) is not int
            or not 60 <= idle <= 3600 or not idle <= total <= 86400):
        raise ValueError('recovery_budget requires idle 60..3600 and total idle..86400 seconds')
    return {'first_started_at': now, 'idle_seconds': idle,
            'total_deadline_at': now + total, 'result_deadline_at': now + idle,
            'progress_event_floor': event_floor, 'progress_cursors': {},
            'effect_phase': 'preflight'}


def generation_matches(operation, workflow):
    from .workflow_progress import recovery_identity
    payload = operation['payload']
    return (not payload.get('identity') or recovery_identity(
        dict(workflow, candidate_sha=payload.get('candidate_sha')),
        payload.get('facts') or []) == payload['identity'])


def verified_progress(conn, operation, workflow, tasks, observations):
    """Hash bounded checkpoint content outside the writer; never trust Task timestamps."""
    from .workflow_recovery import repair_coverage
    from .task_checkpoint import _verify_receipt, MAX_SEGMENTS
    detail = operation['detail']
    if (not generation_matches(operation, workflow)
            or 'first_started_at' not in detail or repair_coverage(operation, tasks)
            or detail.get('execution_id') != workflow.get('execution_id')):
        return []
    mapping = {entry['task_id']: entry for entry in detail['repair_map'].values()}
    candidates = [t for t in tasks if t['task_id'] in mapping or (
        (t.get('node') or t.get('stage')) in detail.get('gate_nodes', [])
        and workflow.get('candidate_sha') != operation['payload'].get('candidate_sha')
        and t.get('candidate_sha') == workflow.get('candidate_sha'))]
    if len(candidates) > 64:
        return []
    progress = []
    remaining_bytes = 8 * 1024 * 1024
    verified_count = 0
    for task in candidates:
        if (task.get('workflow_id') != operation['workflow_id'] or not task.get('run_id')
                or task.get('execution_id') != workflow.get('execution_id')
                or not task.get('completion_epoch') or task.get('superseded_by')
                or task.get('status') == 'superseded'):
            continue
        entry = mapping.get(task['task_id'])
        if entry and task.get('candidate_sha') not in {operation['payload'].get('candidate_sha'), workflow.get('candidate_sha')}:
            continue
        if entry and (entry.get('run_id') != task['run_id'] or (
                entry.get('kind') == 'rework' and entry.get('completion_epoch') != task['completion_epoch'])):
            continue
        scope = json.dumps([task['task_id'], task['run_id'], task['completion_epoch'],
                            task.get('candidate_sha')], separators=(',', ':'))
        key = hashlib.sha256(scope.encode()).hexdigest()
        cursor = detail.get('progress_cursors', {}).get(key, {'step': 0, 'hashes': [], 'completed': []})
        rows = conn.execute("""SELECT id,timestamp,payload_json FROM events
            WHERE workflow_id=? AND task_id=? AND run_id=? AND source='checkpoint'
            AND event_type='task_artifact_checkpoint' AND id>?
            AND json_extract(payload_json,'$.epoch')=? AND json_extract(payload_json,'$.step')>? ORDER BY id LIMIT ?""",
            (operation['workflow_id'], task['task_id'], task['run_id'],
             detail['progress_event_floor'], task['completion_epoch'], cursor['step'], MAX_SEGMENTS + 1)).fetchall()
        if len(rows) > MAX_SEGMENTS:
            continue
        for row in rows:
            try:
                receipt = json.loads(row['payload_json'])
                if (receipt.get('task_id') != task['task_id'] or receipt.get('run_id') != task['run_id']
                        or receipt.get('epoch') != task['completion_epoch']
                        or type(receipt.get('step')) is not int or not 1 <= receipt['step'] <= MAX_SEGMENTS
                        or not receipt.get('completed_steps')):
                    continue
                completed = receipt['completed_steps']
                if not isinstance(completed, list) or len(completed) > 100:
                    continue
                tokens = [hashlib.sha256(s.encode()).hexdigest() for s in completed if isinstance(s, str) and s.strip()]
                if receipt['sha256'] in cursor['hashes'] or not set(tokens) - set(cursor['completed']):
                    continue
                size = receipt.get('size_bytes')
                if type(size) is not int or not 0 <= size <= remaining_bytes :
                    continue
                if verified_count >= 64:
                    return sorted(progress)
                verified_count += 1
                obs = observations.get(receipt['observation_id'])
                if not obs or obs.size_bytes != size:
                    continue
                remaining_bytes -= size
                obs = _verify_receipt(receipt, task, observations)
                if obs.metadata.get('step') != receipt['step'] or obs.size_bytes != size:
                    continue
                progress.append((row['id'], row['timestamp'], key, receipt['step'], obs.sha256, tokens))
            except (ValueError, OSError, KeyError, TypeError):
                continue
    return sorted(progress)


def consume_progress(detail, progress, now):
    """Consume each completed step/content once; publication cannot revive an expired gap."""
    detail = dict(detail)
    cursors = {k: dict(v) for k, v in detail.get('progress_cursors', {}).items()}
    deadline = detail['result_deadline_at']
    for _, published_at, key, step, sha, tokens in progress:
        cursor = cursors.get(key, {'step': 0, 'hashes': [], 'completed': []})
        if (published_at < detail['first_started_at'] or published_at > min(now, deadline)
                or step <= cursor['step'] or sha in cursor['hashes']
                or not set(tokens) - set(cursor['completed'])):
            continue
        cursors[key] = {'step': step, 'hashes': cursor['hashes'] + [sha],
                        'completed': sorted(set(cursor['completed']) | set(tokens))}
        deadline = min(detail['total_deadline_at'], max(deadline, published_at + detail['idle_seconds']))
    detail.update(progress_cursors=cursors, result_deadline_at=deadline)
    return detail
