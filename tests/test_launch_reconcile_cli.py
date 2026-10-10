import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from herdr.state_store import get_state_store
from herdr.task_resources import workflow_launch_lock, begin_launch_intent, record_launch_resources

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('resource_status', ['absent', 'owned', 'foreign', 'unknown'])
def test_real_reconcile_cli_only_proven_absence_releases(tmp_path, resource_status):
    store = get_state_store(tmp_path / 'state.db')
    clone = tmp_path / 'clones' / 't'
    with workflow_launch_lock(store.db_path, 'wf'):
        intent = begin_launch_intent(store, workflow_id='wf', node_id='n', candidate_sha='abc', task_id='t', now=100)['intent']
        record_launch_resources(store, intent, {'planned_clone_path': str(clone), 'run_id': 'r'}, now=101)
    if resource_status in ('owned', 'foreign'):
        clone.mkdir(parents=True)
        tag = {'intent_id': intent['intent_id'] if resource_status == 'owned' else 'foreign', 'task_id': 't', 'run_id': 'r', 'phase': 'agent_start_requested'}
        (clone / '.herdr-launch-identity.json').write_text(json.dumps(tag))
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    native = bindir / 'herdr'
    native.write_text('#!/usr/bin/env python3\nimport json,sys\n' + ("sys.exit(1)\n" if resource_status == 'unknown' else "print(json.dumps({'result': {'panes': []}}))\n"))
    native.chmod(0o755)
    env = {**os.environ, 'HOME': str(tmp_path), 'HERDR_STATE_DB': str(store.db_path), 'TASKS_FILE': str(tmp_path / 'tasks.json'), 'WORKFLOWS_FILE': str(tmp_path / 'workflows.json'), 'PATH': str(bindir) + os.pathsep + os.environ['PATH']}
    result = subprocess.run([sys.executable, str(ROOT / 'bin/herdr-task'), 'launch-reconcile', '--workflow-id', 'wf', '--node', 'n', '--candidate-sha', 'abc', '--apply'], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report['resource_status'] == resource_status
    latest = store.list_events(event_type='launch_intent', desc=True, limit=1)[0]['payload']
    assert (latest['phase'] == 'resources_absent') is (resource_status == 'absent')
    assert clone.exists() is (resource_status in ('owned', 'foreign'))


def test_real_worker_stable_private_intent_tag(tmp_path):
    home = tmp_path / 'home'
    bindir = home / '.local/bin'
    bindir.mkdir(parents=True)
    codex = bindir / 'codex'
    codex.write_text("#!/usr/bin/env python3\nimport sys\nif '--version' in sys.argv: print('safe-stub 1')\nelif '--help' in sys.argv: print('exec')\nelse: print('HERDR_PREFLIGHT_OK')\n")
    codex.chmod(0o755)
    native = bindir / 'herdr'
    native.write_text("#!/usr/bin/env python3\nimport json,sys\nif sys.argv[1:3] == ['pane','read']: print('')\nelse: print(json.dumps({'result': {'agent': {'name': 'owned', 'agent': 'codex', 'agent_session': 's', 'agent_status': 'idle'}}}))\n")
    native.chmod(0o755)
    env = {**os.environ, 'HOME': str(home), 'HERDR_CLONES_DIR': str(tmp_path / 'clones'), 'HERDR_STATE_DB': str(tmp_path / 'state.db'), 'PATH': str(bindir) + os.pathsep + os.environ['PATH']}
    result = subprocess.run([sys.executable, str(ROOT / 'services/herdr-worker.py'), '--task-id', 't', '--run-id', 'r', '--launch-intent-id', 'intent-unique', '--source', str(tmp_path), '--agent', 'codex', '--execution-mode', 'context', '--pane-id', 'p'], env=env, capture_output=True, text=True, timeout=12)
    assert result.returncode == 0, result.stdout + result.stderr
    tagfile = tmp_path / 'clones/t/.herdr-launch-identity.json'
    tag = json.loads(tagfile.read_text())
    assert tag['intent_id'] == 'intent-unique' and tag['run_id'] == 'r'
    assert tag['agent_session_id'] == 's' and tag['pane_id'] == 'p'
    assert tagfile.stat().st_mode & 0o777 == 0o600
    receipt = json.loads(next(line.split('=', 1)[1] for line in result.stdout.splitlines() if line.startswith('HERDR_WORKER_RESULT=')))
    assert receipt['launch_intent_id'] == 'intent-unique'


def test_reconciliation_never_adopts_task_id_reused_by_foreign_run(tmp_path):
    from herdr.task_resources import reconcile_launch_intent
    store = get_state_store(tmp_path / 'state.db')
    with workflow_launch_lock(store.db_path, 'wf'):
        intent = begin_launch_intent(store, workflow_id='wf', node_id='n', task_id='t')['intent']
        intent = record_launch_resources(store, intent, {'run_id': 'original'})
        store.save_task({'task_id': 't', 'workflow_id': 'wf', 'node': 'n', 'run_id': 'foreign', 'dispatch_role': 'worker', 'dispatch_round': 1, 'status': 'working'})
        result = reconcile_launch_intent(store, intent, lambda actual: 'absent')
        assert result['status'] == 'recovery_required'
        assert result['resource_status'] == 'foreign'
        assert store.list_events(event_type='launch_intent', limit=1, desc=True)[0]['payload']['phase'] == 'allocating'


def _partial_launch_fixture(tmp_path):
    import hashlib
    from herdr.task_resources import begin_launch_intent, record_launch_resources
    store = get_state_store(tmp_path / 'partial.db')
    clone = tmp_path / 'clones' / 'partial-task'
    clone.mkdir(parents=True)
    intent = begin_launch_intent(store, workflow_id='wf', node_id='test',
        candidate_sha='old-sha', task_id='partial-task', run_id='run-12', now=100)['intent']
    identity = {'workflow_id': 'wf', 'node_id': 'test', 'candidate_sha': 'old-sha',
        'intent_id': intent['intent_id'], 'task_id': 'partial-task', 'run_id': 'run-12',
        'pane_id': 'pane-12', 'terminal_id': 'terminal-12', 'pane_source': 'dynamic',
        'phase': 'agent_start_requested'}
    record_launch_resources(store, intent, {'planned_clone_path': str(clone),
        'run_id': 'run-12', 'pane_id': 'pane-12', 'terminal_id': 'terminal-12',
        'pane_source': 'dynamic'}, now=101)
    from herdr.task_resources import write_worker_launch_identity
    write_worker_launch_identity(clone, identity, initial=True)
    transcript = 'OpenCode startup\nWaiting for task instructions.\n'
    digest = hashlib.sha256(transcript.encode()).hexdigest()
    return store, intent, clone, transcript, digest


def test_partial_launch_retirement_archives_exact_unstarted_private_launch(tmp_path):
    from herdr.task_resources import retire_partial_launch
    store, intent, clone, transcript, digest = _partial_launch_fixture(tmp_path)
    closed = []
    def runner(argv, _timeout):
        if argv[1:3] == ['pane', 'get']:
            return 0, json.dumps({'result': {'pane': {'pane_id': 'pane-12',
                'terminal_id': 'terminal-12', 'cwd': str(clone)}}}), ''
        if argv[1:3] == ['agent', 'get']:
            return 0, json.dumps({'result': {'agent': None}}), ''
        if argv[1:3] == ['pane', 'read']:
            return 0, transcript, ''
        if argv[1:3] == ['pane', 'close']:
            closed.append(argv[3]); return 0, '{}', ''
        if argv[1:3] == ['pane', 'list']:
            panes = [] if closed else [{'pane_id': 'pane-12', 'cwd': str(clone)}]
            return 0, json.dumps({'result': {'panes': panes}}), ''
        raise AssertionError(argv)
    result = retire_partial_launch(store, intent, expected_intent_id=intent['intent_id'],
        expected_task_id='partial-task', expected_workflow_id='wf', expected_node_id='test',
        expected_candidate_sha='old-sha', expected_terminal_id='terminal-12',
        expected_pane_id='pane-12', operator='human', reason='startup only',
        transcript_sha256=digest, confirmed_startup_only=True, runner=runner, now=200)
    assert result['status'] == 'resources_absent'
    assert closed == ['pane-12']
    assert not clone.exists() and Path(result['archived_clone']).is_dir()
    latest = store.list_events(event_type='launch_intent', desc=True, limit=1)[0]['payload']
    assert latest['phase'] == 'resources_absent'
    assert latest['retirement']['transcript_sha256'] == digest


@pytest.mark.parametrize('failure', ['transcript_changed', 'agent_active'])
def test_partial_launch_retirement_refuses_changed_or_active_runtime(tmp_path, failure):
    from herdr.task_resources import retire_partial_launch
    store, intent, clone, transcript, digest = _partial_launch_fixture(tmp_path)
    closed = []
    def runner(argv, _timeout):
        if argv[1:3] == ['pane', 'get']:
            return 0, json.dumps({'result': {'pane': {'pane_id': 'pane-12',
                'terminal_id': 'terminal-12', 'cwd': str(clone)}}}), ''
        if argv[1:3] == ['agent', 'get']:
            agent = None if failure == 'transcript_changed' else {'agent_session': 'active'}
            return 0, json.dumps({'result': {'agent': agent}}), ''
        if argv[1:3] == ['pane', 'read']:
            text = transcript if failure == 'agent_active' else transcript + 'task output\n'
            return 0, text, ''
        if argv[1:3] == ['pane', 'close']:
            closed.append(argv[3]); return 0, '{}', ''
        raise AssertionError(argv)
    with pytest.raises(ValueError):
        retire_partial_launch(store, intent, expected_intent_id=intent['intent_id'],
            expected_task_id='partial-task', expected_workflow_id='wf', expected_node_id='test',
            expected_candidate_sha='old-sha', expected_terminal_id='terminal-12',
            expected_pane_id='pane-12', operator='human', reason='startup only',
            transcript_sha256=digest, confirmed_startup_only=True, runner=runner, now=200)
    assert closed == []
    assert clone.is_dir()
    latest = store.list_events(event_type='launch_intent', desc=True, limit=1)[0]['payload']
    assert latest['phase'] == 'allocating'


def test_partial_launch_retirement_recovers_after_archive_before_receipt(tmp_path, monkeypatch):
    from herdr import task_resources
    store, intent, clone, transcript, digest = _partial_launch_fixture(tmp_path)
    closed = []
    def runner(argv, _timeout):
        if argv[1:3] == ['pane', 'get']:
            if closed:
                return 1, '', 'pane absent'
            return 0, json.dumps({'result': {'pane': {'pane_id': 'pane-12',
                'terminal_id': 'terminal-12', 'cwd': str(clone)}}}), ''
        if argv[1:3] == ['agent', 'get']:
            return 0, json.dumps({'result': {'agent': None}}), ''
        if argv[1:3] == ['pane', 'read']:
            return 0, transcript, ''
        if argv[1:3] == ['pane', 'close']:
            closed.append(argv[3]); return 0, '{}', ''
        if argv[1:3] == ['pane', 'list']:
            panes = [] if closed else [{'pane_id': 'pane-12', 'cwd': str(clone)}]
            return 0, json.dumps({'result': {'panes': panes}}), ''
        raise AssertionError(argv)
    write = task_resources._launch_event
    def fail_receipt(target, value, at=None, **kwargs):
        if value.get('phase') == 'resources_absent':
            raise OSError('simulated event write failure')
        return write(target, value, at, **kwargs)
    monkeypatch.setattr(task_resources, '_launch_event', fail_receipt)
    args = dict(expected_intent_id=intent['intent_id'], expected_task_id='partial-task',
        expected_workflow_id='wf', expected_node_id='test', expected_candidate_sha='old-sha',
        expected_terminal_id='terminal-12', expected_pane_id='pane-12', operator='human',
        reason='startup only', transcript_sha256=digest, confirmed_startup_only=True,
        runner=runner, now=200)
    with pytest.raises(OSError):
        task_resources.retire_partial_launch(store, intent, **args)
    monkeypatch.setattr(task_resources, '_launch_event', write)
    result = task_resources.retire_partial_launch(store, intent, **args)
    assert result['status'] == 'resources_absent'
    assert len(closed) == 1
    assert Path(result['archived_clone']).is_dir()
