"""Finite recovery budgets and verified progress, using the original operation row."""
import hashlib
import json
from pathlib import Path

IDLE_SECONDS = 3600
MAX_ATTEMPTS = 3
VERIFY_SECONDS = 600


class RecoveryBudgetExhausted(Exception):
    pass


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


def artifact_stamp(path):
    """Stability CAS for content already hashed, never independent progress proof."""
    stat = Path(path).stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def verified_progress(conn, operation, workflow, tasks, observations):
    """Prepare a resumable bounded evidence batch outside the writer transaction."""
    from .workflow_recovery import repair_coverage
    from .task_checkpoint import _verify_receipt, MAX_SEGMENTS
    detail = operation['detail']
    batch = {'progress': [], 'scan_cursors': dict(detail.get('progress_scan_cursors', {})),
             'incomplete': False, 'artifact_stamps': {}}
    if (not generation_matches(operation, workflow) or 'first_started_at' not in detail
            or repair_coverage(operation, tasks) or detail.get('execution_id') != workflow.get('execution_id')):
        return batch
    mapping = {entry['task_id']: entry for entry in detail['repair_map'].values()}
    candidates = [t for t in tasks if t['task_id'] in mapping or (
        (t.get('node') or t.get('stage')) in detail.get('gate_nodes', [])
        and workflow.get('candidate_sha') != operation['payload'].get('candidate_sha')
        and t.get('candidate_sha') == workflow.get('candidate_sha'))]
    authorized = []
    by_id = {}
    for task in candidates:
        if (task.get('workflow_id') != operation['workflow_id'] or not task.get('run_id')
                or task.get('execution_id') != workflow.get('execution_id')
                or not task.get('completion_epoch') or task.get('superseded_by')
                or task.get('status') == 'superseded'):
            continue
        entry = mapping.get(task['task_id'])
        if entry and (entry.get('run_id') != task['run_id']
                or task.get('candidate_sha') not in {operation['payload'].get('candidate_sha'), workflow.get('candidate_sha')}
                or any(not entry.get(field) or entry[field] != task.get(field)
                       for field in ('completion_epoch', 'completion_identity_path'))):
            continue
        # Consumption survives epoch rejudgment; a repeated phase/content is still old.
        scope = json.dumps([task['task_id'], task['run_id']], separators=(',', ':'))
        key = hashlib.sha256(scope.encode()).hexdigest()
        scanned_at, scanned_id = batch['scan_cursors'].get(key, [0, 0])
        authorized.append({'task_id': task['task_id'], 'run_id': task['run_id'],
                           'epoch': task['completion_epoch'], 'key': key,
                           'scanned_at': scanned_at, 'scanned_id': scanned_id})
        by_id[task['task_id']] = task
    # Source-side identity filtering and a global chronological LIMIT prevent
    # one busy target from starving another target's gap-bridging checkpoint.
    pending = conn.execute("""SELECT e.id,e.timestamp,e.payload_json,
            json_extract(a.value,'$.key') AS scope_key,e.task_id
        FROM events e JOIN json_each(?) a ON e.task_id=json_extract(a.value,'$.task_id')
            AND e.run_id=json_extract(a.value,'$.run_id')
        WHERE e.workflow_id=? AND e.source='checkpoint' AND e.event_type='task_artifact_checkpoint'
            AND e.id>? AND json_extract(e.payload_json,'$.epoch')=json_extract(a.value,'$.epoch')
            AND (e.timestamp>json_extract(a.value,'$.scanned_at') OR
                (e.timestamp=json_extract(a.value,'$.scanned_at') AND e.id>json_extract(a.value,'$.scanned_id')))
        ORDER BY e.timestamp,e.id LIMIT 65""",
        (json.dumps(authorized), operation['workflow_id'], detail['progress_event_floor'])).fetchall()
    remaining_bytes, inspected = 8 * 1024 * 1024, 0
    for row in pending:
        published_at, event_id, key = row['timestamp'], row['id'], row['scope_key']
        task = by_id[row['task_id']]
        if inspected >= 64:
            batch['incomplete'] = True
            break
        try:
            receipt = json.loads(row['payload_json'])
            cursor = detail.get('progress_cursors', {}).get(key, {'step': 0, 'hashes': [], 'completed': []})
            if (receipt.get('task_id') != task['task_id'] or receipt.get('run_id') != task['run_id']
                    or receipt.get('epoch') != task['completion_epoch']
                    or type(receipt.get('step')) is not int or not 1 <= receipt['step'] <= MAX_SEGMENTS
                    or not isinstance(receipt.get('completed_steps'), list) or not 1 <= len(receipt['completed_steps']) <= 100):
                continue
            tokens = [hashlib.sha256(value.encode()).hexdigest() for value in receipt['completed_steps']
                      if isinstance(value, str) and value.strip()]
            if (receipt['step'] <= cursor['step'] or receipt.get('sha256') in cursor['hashes']
                    or not set(tokens) - set(cursor['completed'])):
                continue
            obs = observations.get(receipt['observation_id'])
            if not obs or obs.size_bytes != receipt.get('size_bytes'):
                continue
            if obs.size_bytes > remaining_bytes:
                batch['incomplete'] = True
                break
            remaining_bytes -= obs.size_bytes
            before = artifact_stamp(obs.content_ref)
            obs = _verify_receipt(receipt, task, observations)
            if obs.metadata.get('step') != receipt['step'] or artifact_stamp(obs.content_ref) != before:
                continue
            batch['artifact_stamps'][obs.content_ref] = before
            batch['progress'].append((event_id, published_at, key, receipt['step'], obs.sha256, tokens))
        except (ValueError, OSError, KeyError, TypeError):
            continue
        finally:
            # Invalid and duplicate records advance only the scan, never the idle deadline.
            # Leave an uninspected size-limited record for the next batch.
            if not batch['incomplete']:
                inspected += 1
                batch['scan_cursors'][key] = [published_at, event_id]
    return batch


def consume_progress(detail, progress, now):
    """Consume verified new work before settling an unresolved wait; total stays fixed."""
    detail = dict(detail)
    cursors = {k: dict(v) for k, v in detail.get('progress_cursors', {}).items()}
    deadline = detail['result_deadline_at']
    for _, published_at, key, step, sha, tokens in progress:
        cursor = cursors.get(key, {'step': 0, 'hashes': [], 'completed': []})
        if (published_at < detail['first_started_at'] or published_at > now
                or step <= cursor['step'] or sha in cursor['hashes']
                or not set(tokens) - set(cursor['completed'])):
            continue
        cursors[key] = {'step': step, 'hashes': cursor['hashes'] + [sha],
                        'completed': sorted(set(cursor['completed']) | set(tokens))}
        deadline = min(detail['total_deadline_at'], max(deadline, published_at + detail['idle_seconds']))
    detail.update(progress_cursors=cursors, result_deadline_at=deadline)
    return detail
