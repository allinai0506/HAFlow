"""Bounded launch serialization and identity-checked task pane teardown."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import subprocess
from pathlib import Path
import time
import uuid

from .archive import ARCHIVED_STATUSES
from .node_capacity import pane_reference
from .observer.live import probe_live_runtime


class NodeLaunchBusy(RuntimeError):
    pass


@contextmanager
def workflow_launch_lock(db_path, workflow_id, timeout=10):
    # The database and workflow, not caller source/cwd, define the quota domain.
    key = hashlib.sha256(str(workflow_id).encode()).hexdigest()
    directory = Path(db_path).resolve().parent / 'locks'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f'node-launch-{key}.lock').open('a') as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise NodeLaunchBusy('workflow launch in progress; retry after it registers')
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def owned_live_pane(task, probe=None):
    """Reuse the existing run/session probe, requiring concrete instance proof."""
    pane = pane_reference(task)
    if not pane:
        return False, 'no_pane_id'
    scoped = {**task, 'pane_id': pane}
    verdict = (probe or probe_live_runtime)(scoped)
    proven = verdict.get('status') == 'available' and verdict.get('reason') in {
        'identity_match', 'agent_identity_match'}
    return proven, verdict.get('reason') or 'unknown'


def reap_task_pane(store, task_id, *, apply=False, probe=None, close=None):
    with workflow_launch_lock(store.db_path, 'managed-dynamic-pane-lifecycle'):
        return _reap_task_pane(store, task_id, apply=apply, probe=probe, close=close)


def _reap_task_pane(store, task_id, *, apply=False, probe=None, close=None):
    """Release only archived, privately owned dynamic panes; preserve clones."""
    from . import kernel
    initial = store.get_task(task_id)
    pane_key = pane_reference(initial) if initial else task_id
    with workflow_launch_lock(store.db_path, f'pane-reap:{pane_key}'):
        task = store.get_task(task_id)
        row = {'task_id': task_id, 'action': 'retained'}
        if not task:
            return {**row, 'reason': 'task_missing'}
        pane = pane_reference(task)
        if not pane:
            return {**row, 'action': 'already_released'}
        row['pane_id'] = pane
        if task.get('status') not in ARCHIVED_STATUSES:
            return {**row, 'reason': 'task_not_archived'}
        if task.get('pane_source') != 'dynamic':
            return {**row, 'reason': 'dynamic_ownership_not_proven'}
        workflow = store.get_workflow(task.get('workflow_id')) or {}
        try:
            definition = workflow.get('config') or workflow
            if workflow.get('workflow_file'):
                definition = json.loads(Path(workflow['workflow_file']).expanduser().read_text())
        except (OSError, ValueError, TypeError):
            return {**row, 'reason': 'workflow_topology_unknown'}
        protected = {workflow.get('coordinator_pane_id'), definition.get('coordinator_pane_id')}
        for node in definition.get('nodes') or definition.get('stages') or []:
            if isinstance(node, dict):
                protected.add(node.get('anchor_pane_id'))
        if pane in protected:
            return {**row, 'reason': 'anchor_or_coordinator_pane'}
        for other in store.list_tasks():
            if (other.get('task_id') != task_id and pane_reference(other) == pane
                    and other.get('status') not in ARCHIVED_STATUSES):
                return {**row, 'reason': 'pane_claimed_by_live_task'}
        verdict = (probe or probe_live_runtime)({**task, 'pane_id': pane})
        missing = verdict.get('status') == 'unavailable' and verdict.get('reason') == 'pane_missing'
        proven = verdict.get('status') == 'available' and verdict.get('reason') in {
            'identity_match', 'agent_identity_match'}
        if not (missing or proven):
            return {**row, 'reason': verdict.get('reason') or 'unknown'}
        if not apply:
            return {**row, 'action': 'would_release', 'reason': verdict.get('reason')}
        if not missing:
            if close is None:
                raise ValueError('reap apply requires a pane-close transport')
            try:
                if not close(pane):
                    return {**row, 'reason': 'close_failed'}
            except (OSError, RuntimeError, TimeoutError, subprocess.TimeoutExpired) as exc:
                return {**row, 'reason': f'close_failed:{type(exc).__name__}'}
        runtime = dict(task.get('runtime') or {})
        runtime.update(pane_id=None, status='unavailable')
        result = kernel.transition_task(
            task_id, task['status'], reason='pane_reaped', source='herdr-task',
            store=store, expected_status=task['status'], expected_version=task.get('version'),
            metadata={'pane_id': None, 'runtime': runtime, 'pane_lifecycle': 'released',
                      'released_pane_id': pane, 'pane_retained': False},
        )
        if not result.get('accepted', True):
            return {**row, 'reason': 'metadata_changed_retry_required'}
        return {**row, 'action': 'released', 'reason': 'pane_missing' if missing else 'identity_match'}


def dispatch_identity(workflow_id, node_id, role, candidate_sha, dispatch_round):
    """Stable across caller source and generated task IDs."""
    fields = [workflow_id, node_id, role, candidate_sha or '', dispatch_round]
    if any(not isinstance(value, str) or not value or len(value) > 256
           for value in fields[:3]):
        raise ValueError('workflow/node/role must be bounded nonempty strings')
    if not isinstance(fields[3], str) or len(fields[3]) > 256:
        raise ValueError('candidate_sha must be a bounded string')
    if type(dispatch_round) is not int or dispatch_round < 1:
        raise ValueError('dispatch_round must be a positive integer')
    return 'launch:' + hashlib.sha256(json.dumps(fields).encode()).hexdigest()


def _launch_event(store, intent, now=None):
    store.record_event('launch_intent', dict(intent), workflow_id=intent['workflow_id'],
                       node_id=intent['node_id'], task_id=intent['key'],
                       source='launch', timestamp=time.time() if now is None else now)


def _latest_intent(store, key):
    events = store.list_events(task_id=key, event_type='launch_intent',
                               source='launch', limit=1, desc=True)
    return events[0]['payload'] if events else None


def find_dispatch_task(store, workflow_id, node_id, role, candidate_sha, dispatch_round):
    dispatch_identity(workflow_id, node_id, role, candidate_sha, dispatch_round)
    for task in store.list_tasks(workflow_id=workflow_id):
        # Historical records have no declared role/round. Do not invent a
        # matching identity from terminal failed/superseded task history.
        if 'dispatch_role' not in task or 'dispatch_round' not in task:
            continue
        if ((task.get('node') or task.get('stage')) == node_id
                and (task.get('dispatch_role') or 'worker') == role
                and (task.get('candidate_sha') or '') == (candidate_sha or '')
                and int(task.get('dispatch_round') or 1) == dispatch_round):
            return task
    return None


def begin_launch_intent(store, *, workflow_id, node_id, role='worker', candidate_sha='',
                        dispatch_round=1, task_id, supersedes=None, now=None,
                        lease_seconds=120):
    """Call under workflow_launch_lock BEFORE Router/Pane/Clone side effects.

    A lease is an attention deadline, never permission to repeat an uncertain
    allocation. Both automatic and manual launches must use this identity.
    """
    now = time.time() if now is None else now
    if not 1 <= lease_seconds <= 600:
        raise ValueError('launch lease must be between 1 and 600 seconds')
    key = dispatch_identity(workflow_id, node_id, role, candidate_sha, dispatch_round)
    if dispatch_round > 1 and not supersedes:
        raise ValueError('new dispatch round requires supersedes')
    if supersedes:
        old = store.get_task(supersedes)
        if (not old or old.get('workflow_id') != workflow_id
                or (old.get('node') or old.get('stage')) != node_id
                or (old.get('dispatch_role') or 'worker') != role
                or dispatch_round != int(old.get('dispatch_round') or 1) + 1):
            raise ValueError('supersedes must bind previous round of same workflow/node/role')
    task = find_dispatch_task(store, workflow_id, node_id, role, candidate_sha, dispatch_round)
    if task:
        return {'status': 'duplicate', 'task': task}
    previous = _latest_intent(store, key)
    if previous and previous['phase'] != 'resources_absent':
        status = 'in_progress' if now < previous['lease_until'] and not (previous.get('resources') or {}).get('allocation_failed') else 'recovery_required'
        return {'status': status, 'intent': previous}
    intent = {'key': key, 'intent_id': uuid.uuid4().hex,
              'workflow_id': workflow_id, 'node_id': node_id, 'role': role,
              'candidate_sha': candidate_sha or '', 'dispatch_round': dispatch_round,
              'task_id': task_id, 'supersedes': supersedes, 'phase': 'allocating',
              'lease_until': now + lease_seconds, 'resources': {}}
    _launch_event(store, intent, now)
    return {'status': 'claimed', 'intent': intent}


def record_launch_resources(store, intent, resource_identity, *, now=None):
    """Persist after each allocation; transport must tag resources with intent_id.

    An interruption before this write remains uncertain and blocks relaunch.
    """
    current = _latest_intent(store, intent['key'])
    if not current or current['intent_id'] != intent['intent_id'] or current['phase'] != 'allocating':
        raise ValueError('launch intent no longer owns allocation')
    updated = {**current, 'resources': {**current['resources'], **resource_identity}}
    if len(json.dumps(updated)) > 8192:
        raise ValueError('launch resource identity exceeds budget')
    _launch_event(store, updated, now)
    return updated


def finish_launch_intent(store, intent, *, now=None):
    """Registration precedes completion, so interrupted finish is deduplicated."""
    task = registered_launch_task(store, intent)
    if not task:
        raise ValueError('registered task does not match launch intent')
    current = _latest_intent(store, intent['key'])
    if not current or current['intent_id'] != intent['intent_id']:
        raise ValueError('launch intent no longer owns registration')
    _launch_event(store, {**current, 'phase': 'registered'}, now)


def registered_launch_task(store, intent):
    task = store.get_task(intent['task_id'])
    if not task:
        return None
    if dispatch_identity(task.get('workflow_id'), task.get('node') or task.get('stage'),
                         task.get('dispatch_role') or 'worker', task.get('candidate_sha') or '',
                         int(task.get('dispatch_round') or 1)) != intent['key']:
        return None
    run_id = (intent.get('resources') or {}).get('run_id')
    return task if not run_id or task.get('run_id') == run_id else None


def reconcile_launch_intent(store, intent, probe_resources, *, now=None):
    """Transport returns absent only after checking ALL intent-tagged resources.

    owned/foreign/unknown do not grant permission to allocate or delete anything.
    The caller can adopt verified owned resources and finish registration instead.
    Run under workflow_launch_lock; probes must impose their own finite timeout.
    """
    current = _latest_intent(store, intent['key'])
    if not current or current['intent_id'] != intent['intent_id']:
        raise ValueError('launch intent changed during recovery')
    if store.get_task(current['task_id']):
        task = registered_launch_task(store, current)
        if task:
            return {'status': 'registered', 'task': task}
        return {'status': 'recovery_required', 'resource_status': 'foreign', 'intent': current}
    verdict = probe_resources(current)
    if verdict != 'absent':
        return {'status': 'recovery_required', 'resource_status': verdict, 'intent': current}
    recovered = {**current, 'phase': 'resources_absent'}
    _launch_event(store, recovered, now)
    return {'status': 'resources_absent', 'intent': recovered}


def write_worker_launch_identity(clone, identity, *, initial=False):
    """Private durable resource tag, separate from execution success declarations."""
    import os
    import tempfile
    path = Path(clone) / '.herdr-launch-identity.json'
    if not initial:
        previous = json.loads(path.read_text())
        if any(previous.get(key) != identity.get(key) for key in ('intent_id', 'task_id', 'run_id')):
            raise ValueError('worker launch resource ownership changed')
    data = json.dumps(identity, sort_keys=True).encode()
    if len(data) > 8192:
        raise ValueError('launch identity exceeds budget')
    fd, temp = tempfile.mkstemp(prefix='.launch-identity-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temp).unlink(missing_ok=True)


def inventory_launch_resources(intent, runner=None, *, timeout=10):
    """Bounded read-only inventory; absence requires complete native + file facts.

    Native schemas lacking cwd cannot prove an unregistered Pane is absent.
    No delete/stop/adopt operation is issued for owned, foreign or unknown rows.
    """
    import os
    resources = intent.get('resources') or {}
    planned = resources.get('planned_clone_path')
    if not planned or not resources.get('run_id'):
        return {'resource_status': 'unknown', 'reason': 'planned_resource_identity_missing'}
    clone = Path(planned)
    tag = None
    if os.path.lexists(clone):
        if clone.is_symlink() or not clone.is_dir():
            return {'resource_status': 'foreign', 'reason': 'workspace_path_changed'}
        path = clone / '.herdr-launch-identity.json'
        try:
            if path.is_symlink() or path.stat().st_size > 8192:
                raise ValueError('untrusted tag')
            tag = json.loads(path.read_text())
        except (OSError, ValueError):
            return {'resource_status': 'unknown', 'reason': 'workspace_tag_missing_or_unreadable'}
        if any(tag.get(key) != value for key, value in {
            'intent_id': intent['intent_id'], 'task_id': intent['task_id'],
            'run_id': resources['run_id'],
        }.items()):
            return {'resource_status': 'foreign', 'reason': 'workspace_tag_identity_mismatch'}
    deadline = time.monotonic() + timeout
    if runner is None:
        def runner(argv, budget):
            from .bounded_tools import run_bounded
            result = run_bounded(argv, timeout=budget, output_limit=65536)
            return (result['exit_code'] if result['status'] == 'completed' else 1,
                    result['stdout'], result['stderr'])
    try:
        code, stdout, _ = runner(['herdr', 'pane', 'list'], max(0.1, deadline - time.monotonic()))
        if code or len(stdout.encode()) > 65536:
            raise ValueError('native inventory unavailable')
        payload = json.loads(stdout)
        if not isinstance(payload, dict):
            raise ValueError('native inventory schema')
        result = payload.get('result')
        panes = result.get('panes') if isinstance(result, dict) else result
        if not isinstance(panes, list) or len(panes) > 64:
            raise ValueError('native inventory budget or schema')
        known_pane = resources.get('pane_id') or (tag or {}).get('pane_id')
        for pane in panes:
            if not isinstance(pane, dict) or not pane.get('pane_id'):
                raise ValueError('native inventory incomplete')
            if known_pane and pane['pane_id'] == known_pane:
                return {'resource_status': 'unknown', 'reason': 'pane_present_needs_instance_reconciliation',
                        'pane_id': known_pane, 'clone_tag': tag}
            cwd = pane.get('cwd')
            if not cwd:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('inventory deadline')
                code, stdout, _ = runner(['herdr', 'pane', 'get', pane['pane_id']], min(2, remaining))
                if code or len(stdout.encode()) > 65536:
                    raise ValueError('pane get unavailable')
                cwd = json.loads(stdout).get('result', {}).get('pane', {}).get('cwd')
            if not isinstance(cwd, str) or not cwd:
                raise ValueError('pane cwd unknown')
            if Path(cwd).expanduser().resolve() == clone.resolve():
                return {'resource_status': 'unknown', 'reason': 'unregistered_pane_in_launch_workspace',
                        'pane_id': pane['pane_id'], 'clone_tag': tag}
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError, TimeoutError, subprocess.TimeoutExpired):
        return {'resource_status': 'unknown', 'reason': 'native_inventory_unverified', 'clone_tag': tag}
    if tag:
        return {'resource_status': 'owned', 'reason': 'private_workspace_tag_matches', 'clone_tag': tag}
    return {'resource_status': 'absent', 'reason': 'workspace_absent_complete_native_inventory'}


def recover_launch_resources(store, intent, *, runner=None, timeout=10):
    with workflow_launch_lock(store.db_path, 'managed-dynamic-pane-lifecycle'):
        return _recover_launch_resources(store, intent, runner=runner, timeout=timeout)


def _recover_launch_resources(store, intent, *, runner=None, timeout=10):
    """Reclaim only an intent-tagged workspace whose Agent never started.

    Keep the failed workspace as evidence. Recheck the private tag, native cwd
    and explicit empty agent registry immediately before closing dynamic panes.
    Caller holds the same cross-process workflow launch lock as allocation.
    """
    import os
    if runner is None:
        def runner(argv, budget):
            from .bounded_tools import run_bounded
            result = run_bounded(argv, timeout=budget, output_limit=65536)
            return result['exit_code'] if result['status'] == 'completed' else 1, result['stdout'], result['stderr']
    current = _latest_intent(store, intent['key'])
    if not current or current['intent_id'] != intent['intent_id']:
        raise ValueError('launch intent changed during recovery')
    if store.get_task(current['task_id']):
        return reconcile_launch_intent(store, current, lambda _: 'unknown')
    report = inventory_launch_resources(current, runner=runner, timeout=timeout)
    if report['resource_status'] == 'absent':
        return reconcile_launch_intent(store, current, lambda _: 'absent')
    retained = {'status': 'recovery_required', **report}
    tag = report.get('clone_tag')
    if not tag or tag.get('phase') not in {'workspace_created', 'pane_allocated'}:
        return retained
    clone = Path(current['resources']['planned_clone_path'])
    tag_path = clone / '.herdr-launch-identity.json'
    deadline = time.monotonic() + timeout
    def native(argv):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('recovery deadline')
        code, stdout, _ = runner(argv, remaining)
        if code or len(stdout.encode()) > 65536:
            raise ValueError('native recovery unavailable')
        return json.loads(stdout)['result']
    def validate():
        if clone.is_symlink() or tag_path.is_symlink() or json.loads(tag_path.read_text()) != tag:
            raise ValueError('workspace identity changed')
        pane = report.get('pane_id')
        if tag.get('pane_id') and not pane and report.get('resource_status') != 'owned':
            raise ValueError('pane absence unverified')
        if pane:
            if tag.get('pane_source') != 'dynamic' or tag.get('pane_id') != pane or not tag.get('terminal_id'):
                raise ValueError('dynamic pane ownership unknown')
            live = native(['herdr', 'pane', 'get', pane]).get('pane')
            if not isinstance(live, dict) or live.get('terminal_id') != tag.get('terminal_id') or not live.get('cwd') or Path(live['cwd']).resolve() != clone.resolve():
                raise ValueError('pane workspace identity changed')
            agent = native(['herdr', 'agent', 'get', pane])
            if 'agent' not in agent or agent['agent'] not in (None, {}):
                raise ValueError('pane agent absence unverified')
        return pane
    try:
        pane = validate()
        # Persist evidence before destructive native action; interruption remains
        # retryable from this same tag without granting a second allocation.
        if pane:
            validate()
            native(['herdr', 'pane', 'close', pane])
        after_close = inventory_launch_resources(current, runner=runner, timeout=max(0.1, deadline-time.monotonic()))
        if after_close.get('resource_status') != 'owned' or after_close.get('reason') != 'private_workspace_tag_matches':
            raise ValueError('additional launch resources remain or native inventory is incomplete')
        if clone.is_symlink() or json.loads(tag_path.read_text()) != tag:
            raise ValueError('workspace changed before archival')
        archive = clone.parent / (clone.name + '.failed-' + current['intent_id'])
        if os.path.lexists(archive):
            raise ValueError('failed workspace archive already exists')
        clone.rename(archive)
        result = reconcile_launch_intent(store, current, lambda item: inventory_launch_resources(
            item, runner=runner, timeout=max(0.1,deadline-time.monotonic()))['resource_status'])
        return {**result, 'archived_clone': str(archive)}
    except (OSError, ValueError, KeyError, TypeError, TimeoutError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return {**retained, 'reason': 'recovery_unverified:' + type(exc).__name__}


def authorize_launch_recovery(store, intent, terminal_id, reason, *, runner=None):
    """Explicit operator attestation binds a legacy unstarted allocation token.

    This does not close anything. The caller must explicitly attest that no
    Agent start was attempted; native emptiness alone is insufficient.
    """
    if not terminal_id or len(terminal_id) > 128 or not reason.strip() or len(reason.encode()) > 2048:
        raise ValueError('Recovery authorization requires terminal identity and bounded evidence reason')
    with workflow_launch_lock(store.db_path, 'managed-dynamic-pane-lifecycle'):
        current = _latest_intent(store, intent['key'])
        if not current or current['intent_id'] != intent['intent_id'] or store.get_task(current['task_id']):
            raise ValueError('Launch identity changed or registered')
        report = inventory_launch_resources(current, runner=runner)
        tag = report.get('clone_tag')
        pane = report.get('pane_id')
        if not tag or not pane or tag.get('agent_session_id') or tag.get('agent_name') or tag.get('pane_source') != 'dynamic':
            raise ValueError('Recovery requires a private dynamic workspace without started agent evidence')
        if runner is None:
            def runner(argv, budget):
                from .bounded_tools import run_bounded
                result=run_bounded(argv,timeout=budget,output_limit=65536)
                return result['exit_code'] if result['status']=='completed' else 1,result['stdout'],result['stderr']
        code, stdout, _ = runner(['herdr','pane','get',pane], 2)
        if code or json.loads(stdout).get('result',{}).get('pane',{}).get('terminal_id') != terminal_id:
            raise ValueError('Recovery terminal identity mismatch')
        code, stdout, _ = runner(['herdr','agent','get',pane], 2)
        agent=json.loads(stdout).get('result',{})
        if code or 'agent' not in agent or agent['agent'] not in (None, {}):
            raise ValueError('Recovery requires explicit empty native agent registry')
        updated={**tag,'pane_id':pane,'terminal_id':terminal_id,'phase':'pane_allocated',
                 'recovery_authorized_reason':reason}
        write_worker_launch_identity(current['resources']['planned_clone_path'],updated)
        store.record_event('launch_recovery_authorized', {'intent_id':intent['intent_id'],
            'pane_id':pane,'terminal_id':terminal_id,'reason':reason,'certifies_success':False},
            workflow_id=intent['workflow_id'],node_id=intent['node_id'],task_id=intent['task_id'],source='herdr-task')
        return {'authorized':True,'intent_id':intent['intent_id'],'terminal_id':terminal_id}
