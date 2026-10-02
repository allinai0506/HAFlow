"""Bounded launch serialization and identity-checked task pane teardown."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import subprocess
from pathlib import Path
import time

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
