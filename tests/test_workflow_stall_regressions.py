"""Real Git and dispatch regressions from wf-project-1002-01."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from herdr.completion import marker_present, sanitize_prompt
from herdr.direct_dispatch import candidate_branch_for_node
from herdr.scheduler import node_is_complete
from tests.test_worker_readiness_contract import load


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize('onto', [False, True])
@pytest.mark.parametrize('fail_after_pane', [False, True])
def test_worker_git_launch_intent_survives_branch_preparation(tmp_path, monkeypatch, capsys, onto, fail_after_pane):
    worker = load('services/herdr-worker.py', 'worker_git_launch_identity')
    source = tmp_path / 'source'
    source.mkdir()
    git(source, 'init', '-b', 'main')
    git(source, 'config', 'user.name', 'Test')
    git(source, 'config', 'user.email', 'test@example.invalid')
    git(source, 'config', 'core.hooksPath', '/dev/null')
    (source / 'base.txt').write_text('base\n')
    git(source, 'add', 'base.txt')
    git(source, 'commit', '-m', 'base')
    origin = tmp_path / 'origin.git'
    subprocess.run(['git', 'clone', '--bare', str(source), str(origin)], check=True, capture_output=True)
    git(source, 'remote', 'add', 'origin', str(origin))
    (source / 'discard.txt').write_text('untracked source WIP')
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setattr(worker.Path, 'home', lambda: home)
    monkeypatch.setattr(worker, 'CLONE_ROOT', tmp_path / 'clones')
    monkeypatch.setattr(worker, '_registered_tasks', lambda: [])
    monkeypatch.setattr(worker, 'is_task_active_in_registry', lambda tid: False)
    monkeypatch.setattr(worker, 'verify_request_preflight', lambda *a: {'request_verified': True})
    monkeypatch.setattr(worker, 'create_pane', lambda *a: 'test-pane')
    monkeypatch.setattr(worker, 'start_agent', lambda *a: {
        'agent': 'codex', 'name': 'owned', 'agent_session': 'owned-session'})
    monkeypatch.setattr(worker, 'wait_startup_ready', lambda *a: {
        'status': 'READY', 'interactive_ready': True})
    monkeypatch.setattr(worker, 'measure_complexity_baseline', lambda *a: 'disabled')
    argv = ['worker', '--task-id', 'git-launch', '--source', str(source),
            '--agent', 'codex', '--base-branch', 'main', '--parent-pane', 'parent',
            '--launch-intent-id', 'intent-owned', '--run-id', 'run-owned']
    if onto:
        argv += ['--onto', 'main']
    monkeypatch.setattr(sys, 'argv', argv)
    if fail_after_pane:
        from herdr import task_resources
        real_write = task_resources.write_worker_launch_identity
        def fail_update(clone, identity, **kwargs):
            if identity.get('phase') == 'agent_start_requested':
                raise OSError('identity update unavailable after Pane creation')
            return real_write(clone, identity, **kwargs)
        monkeypatch.setattr(task_resources, 'write_worker_launch_identity', fail_update)
        monkeypatch.setattr(worker, 'start_agent', lambda *a: pytest.fail('must not start Agent'))
        with pytest.raises(OSError, match='identity update unavailable'):
            worker.main()
        failure = json.loads(next(line.split('=', 1)[1] for line in capsys.readouterr().err.splitlines()
                                  if line.startswith('HERDR_WORKER_FAILURE=')))
        assert (tmp_path / 'clones' / 'git-launch').exists()
        assert failure['recovery_required'] is True
        assert failure['agent_started'] is False
        assert failure['pane_id'] == 'test-pane'
        return
    worker.main()
    result = json.loads(next(line.split('=', 1)[1] for line in capsys.readouterr().out.splitlines()
                             if line.startswith('HERDR_WORKER_RESULT=')))
    clone = Path(result['clone'])
    identity = json.loads((clone / '.herdr-launch-identity.json').read_text())
    assert identity['intent_id'] == 'intent-owned'
    assert identity['run_id'] == 'run-owned'
    assert identity['phase'] == 'interactive_ready'
    assert identity['pane_id'] == 'test-pane'
    assert not (clone / 'discard.txt').exists()
    assert (source / 'discard.txt').exists()
    assert result['baseline_fingerprint'] == {'tracked': {}, 'untracked': {}}
    assert result['baseline_commit'] == git(clone, 'rev-parse', 'HEAD')


@pytest.mark.parametrize('space', [' ', '\t', '  \t'])
def test_legacy_spaced_completion_and_input_echo_hygiene(space):
    task_id = 'requirements-adversarial'
    text = f'HERDR_TASK_DONE:{space}{task_id}'
    assert marker_present(text, task_id)
    assert not marker_present(text + '-r2', task_id)
    assert not marker_present(text, 'other-task')
    cleaned, count = sanitize_prompt(text, task_id)
    assert count == 1
    assert not marker_present(cleaned, task_id)


@pytest.mark.parametrize('space', ['', ' '])
def test_marker_suffix_after_soft_wrap_is_another_task(space):
    text = f'HERDR_TASK_DONE:{space}requirements-adversarial\n    -r2'
    assert not marker_present(text, 'requirements-adversarial')
    assert marker_present(text, 'requirements-adversarial-r2')
    cleaned, _ = sanitize_prompt(text, 'requirements-adversarial-r2')
    assert not marker_present(cleaned, 'requirements-adversarial-r2')


def test_prompt_hygiene_does_not_rewrite_another_wrapped_task_id():
    text = 'HERDR_TASK_DONE: requirements-adversarial\n    -r2'
    cleaned, count = sanitize_prompt(text, 'requirements-adversarial')
    assert count == 0
    assert cleaned == text


def test_worker_clean_without_launch_intent_removes_copied_foreign_tag(tmp_path):
    worker = load('services/herdr-worker.py', 'worker_clean_foreign_tag')
    git(tmp_path, 'init', '-b', 'main')
    git(tmp_path, 'config', 'user.name', 'Test')
    git(tmp_path, 'config', 'user.email', 'test@example.invalid')
    git(tmp_path, 'config', 'core.hooksPath', '/dev/null')
    git(tmp_path, 'commit', '--allow-empty', '-m', 'base')
    tag = tmp_path / '.herdr-launch-identity.json'
    tag.write_text(json.dumps({'intent_id': 'foreign', 'task_id': 'foreign', 'run_id': 'foreign'}))
    worker.sanitize_clone_sandbox(tmp_path)
    assert not tag.exists()


@pytest.fixture
def frozen_worker(tmp_path, monkeypatch):
    worker = load('services/herdr-worker.py', 'worker_frozen_commit_launch')
    source = tmp_path / 'source'
    source.mkdir()
    git(source, 'init', '-b', 'main')
    git(source, 'config', 'user.name', 'Test')
    git(source, 'config', 'user.email', 'test@example.invalid')
    git(source, 'config', 'core.hooksPath', '/dev/null')
    (source / 'f').write_text('base')
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'base')
    git(source, 'checkout', '-b', 'implementation')
    (source / 'f').write_text('candidate')
    git(source, 'commit', '-am', 'candidate')
    frozen = git(source, 'rev-parse', 'HEAD')
    git(source, 'checkout', 'main')
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setattr(worker.Path, 'home', lambda: home)
    monkeypatch.setattr(worker, 'CLONE_ROOT', tmp_path / 'clones')
    monkeypatch.setattr(worker, '_registered_tasks', lambda: [{
        'task_id': 'impl', 'status': 'integrated', 'branch': 'implementation'}])
    monkeypatch.setattr(worker, 'is_task_active_in_registry', lambda tid: False)
    monkeypatch.setattr(worker, 'verify_request_preflight', lambda *a: {'request_verified': True})
    monkeypatch.setattr(worker, 'create_pane', lambda *a: 'test-pane')
    monkeypatch.setattr(worker, 'start_agent', lambda *a: {
        'agent': 'codex', 'name': 'owned', 'agent_session': 'owned-session'})
    monkeypatch.setattr(worker, 'wait_startup_ready', lambda *a: {
        'status': 'READY', 'interactive_ready': True})
    monkeypatch.setattr(worker, 'measure_complexity_baseline', lambda *a: 'disabled')
    argv = ['worker', '--task-id', 'verify-frozen', '--source', str(source),
            '--agent', 'codex', '--task-type', 'test', '--base-branch', 'main',
            '--parent-pane', 'parent', '--launch-intent-id', 'intent-owned', '--run-id', 'run-owned']
    return worker, source, frozen, argv


def test_worker_new_branch_without_onto_starts_at_explicit_frozen_commit(frozen_worker, monkeypatch, capsys):
    worker, source, frozen, argv = frozen_worker
    monkeypatch.setattr(sys, 'argv', argv + ['--candidate-sha', frozen])
    worker.main()
    result = json.loads(next(line.split('=', 1)[1] for line in capsys.readouterr().out.splitlines()
                             if line.startswith('HERDR_WORKER_RESULT=')))
    clone = Path(result['clone'])
    assert result['baseline_commit'] == frozen == git(clone, 'rev-parse', 'HEAD')
    assert git(source, 'rev-parse', 'HEAD') != frozen
    assert result['branch'] == 'agent/codex/test-verify-frozen'
    assert git(clone, 'branch', '--show-current') != 'implementation'
    assert result['baseline_fingerprint'] == {'tracked': {}, 'untracked': {}}


@pytest.mark.parametrize('invalid', ['short', 'missing', 'blob'])
def test_worker_pinned_new_branch_rejects_unproven_commit_before_pane(frozen_worker, monkeypatch, invalid):
    worker, source, frozen, argv = frozen_worker
    value = frozen[:12] if invalid == 'short' else 'a' * 40
    if invalid == 'blob':
        value = git(source, 'rev-parse', 'HEAD:f')
    monkeypatch.setattr(sys, 'argv', argv + ['--candidate-sha', value])
    monkeypatch.setattr(worker, 'create_pane', lambda *a: pytest.fail('unproven commit must not create Pane'))
    with pytest.raises(RuntimeError):
        worker.main()


@pytest.mark.parametrize('source_head', ['behind', 'ahead'])
def test_real_cli_worker_store_preserve_off_base_frozen_candidate(frozen_worker, tmp_path, monkeypatch, source_head):
    import contextlib
    import io
    from types import SimpleNamespace
    from herdr import scheduler_facts
    from herdr.state_store import get_state_store
    worker, source, frozen, argv = frozen_worker
    if source_head == 'ahead':
        git(source, 'checkout', '--detach', frozen)
        (source / 'f').write_text('source advanced after freeze')
        git(source, 'commit', '-am', 'advance source only')
    store = get_state_store(tmp_path / 'state.db')
    monkeypatch.setenv('HERDR_STATE_DB', str(store.db_path))
    monkeypatch.setenv('TASKS_FILE', str(tmp_path / 'tasks.json'))
    monkeypatch.setenv('WORKFLOWS_FILE', str(tmp_path / 'workflows.json'))
    monkeypatch.setenv('HERDR_WORKFLOW_DOCS_DIR', str(tmp_path / 'workflow-docs'))
    store.save_workflow({'workflow_id': 'wf-pin', 'status': 'running',
        'project_root': str(source), 'base_branch': 'main',
        'config': {'nodes': [{'id': 'test', 'default_task_type': 'test',
                            'default_integration_mode': 'none'}]}})
    scheduler_facts.record_candidate_frozen('wf-pin', frozen, db_path=store.db_path)
    cli = load('bin/herdr-task', 'cli_frozen_commit_launch')
    monkeypatch.setattr(cli, '_get_store', lambda: store)
    monkeypatch.setattr(cli, 'project_for_workflow', store.get_workflow)
    monkeypatch.setattr(cli, 'choose_agent', lambda *a, **k: 'codex')
    monkeypatch.setattr(cli, 'ensure_stage_topology', lambda *a: {
        'workspace_id': 'temporary', 'anchor_pane_id': 'parent', 'tab_id': 'temporary'})
    monkeypatch.setattr(cli, 'acquire_pane_for_task', lambda *a: None)
    monkeypatch.setattr(cli, 'auto_init_task_loop', lambda *a, **k: None)
    monkeypatch.setattr(cli, 'release_agent_reservation', lambda *a: None)
    monkeypatch.setattr(cli, 'dispatch_task', lambda *a: None)
    monkeypatch.setattr(cli, 'close_pane', lambda *a, **k: None)
    real_run = subprocess.run
    def controlled_transport(cmd, **kwargs):
        if str(cmd[0]).endswith('/herdr-worker.py'):
            stdout, stderr = io.StringIO(), io.StringIO()
            with monkeypatch.context() as child, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                child.setattr(sys, 'argv', cmd)
                worker.main()
            return subprocess.CompletedProcess(cmd, 0, stdout.getvalue(), stderr.getvalue())
        return real_run(cmd, **kwargs)
    monkeypatch.setattr(cli.subprocess, 'run', controlled_transport)
    args = SimpleNamespace(task_id='test-frozen', workflow_id='wf-pin', node='test',
        source=str(source), agent='auto', task_type='test', integration_mode='none',
        candidate_sha=frozen, onto=None, goal='verify frozen candidate',
        prompt='verify the current frozen candidate', acceptance=[])
    cli.launch_task(args)
    task = store.get_task('test-frozen')
    assert task['candidate_sha'] == task['baseline_commit'] == frozen
    assert task['branch'] == 'agent/codex/test-test-frozen'
    assert git(Path(task['clone_path']), 'rev-parse', 'HEAD') == frozen
    assert git(source, 'rev-parse', 'HEAD') != frozen
    intent = store.list_events(event_type='launch_intent', limit=1, desc=True)[0]['payload']
    assert intent['candidate_sha'] == frozen and intent['phase'] == 'registered'
    assert not store.list_events(event_type='test_baseline_rejected')


def test_finished_non_git_document_branch_is_not_remote_onto():
    tasks = [{'task_id': 'requirements-spec', 'workflow_id': 'wf',
              'node': 'requirements', 'status': 'cleaned', 'integration_mode': 'none',
              'branch': 'agent/opencode/docs-requirements-spec'}]
    assert candidate_branch_for_node(tasks, 'wf', 'plan', ['requirements'],
                                     delivered_in_base=True) is None


@pytest.mark.parametrize('replacement', [None, 'missing', 'cycle'])
def test_pending_replacement_cannot_disappear_from_completion(replacement):
    old = {'task_id': 'review', 'status': 'superseded', 'replacement_pending': True}
    if replacement:
        old['superseded_by'] = 'review' if replacement == 'cycle' else replacement
    assert not node_is_complete([{'task_id': 'spec', 'status': 'cleaned'}, old])


def test_real_cli_supersede_retains_obligation_until_linked_replacement(tmp_path, monkeypatch):
    from herdr.state_store import get_state_store
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path / 'state.db'))
    monkeypatch.setenv('TASKS_FILE', str(tmp_path / 'tasks.json'))
    store = get_state_store()
    for tid, status in [('spec', 'cleaned'), ('review', 'failed')]:
        store.save_task({'task_id': tid, 'workflow_id': 'wf', 'node': 'requirements', 'status': status})
    cli = load('bin/herdr-task', 'cli_supersede_obligation')
    cli.supersede_task('review', reason='retry with new worker', allow_pending=True)
    assert store.get_task('review').get('replacement_pending') is True
    ctrl = load('services/herdr-controller.py', 'ctrl_supersede_obligation')
    monkeypatch.setattr(ctrl, 'load_tasks', lambda: store.list_tasks(workflow_id='wf'))
    monkeypatch.setattr(ctrl, 'workflow_config_for', lambda wid: {'nodes': [{'id': 'requirements'}]})
    assert not ctrl.is_node_complete('wf', 'requirements')
    store.save_task({'task_id': 'review-r2', 'workflow_id': 'wf', 'node': 'requirements', 'status': 'cleaned'})
    cli.supersede_task('review', new_task_id='review-r2')
    assert ctrl.is_node_complete('wf', 'requirements')


def test_cli_rejects_foreign_node_replacement_before_any_write(tmp_path, monkeypatch):
    from herdr.state_store import get_state_store
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path / 'state.db'))
    monkeypatch.setenv('TASKS_FILE', str(tmp_path / 'tasks.json'))
    store = get_state_store()
    store.save_task({'task_id': 'review', 'workflow_id': 'wf', 'node': 'requirements',
                     'run_id': 'same-run', 'status': 'failed'})
    store.save_task({'task_id': 'impl', 'workflow_id': 'wf', 'node': 'implementation',
                     'run_id': 'same-run', 'status': 'cleaned'})
    before = store.get_task('review')
    cli = load('bin/herdr-task', 'cli_foreign_replacement_node')
    with pytest.raises(SystemExit) as exc:
        cli.supersede_task('review', new_task_id='impl')
    assert exc.value.code == 2
    assert store.get_task('review') == before
    assert store.list_events(event_type='task_transition', task_id='review') == []


def test_explicit_task_abandon_is_not_automatically_redispatched(tmp_path, monkeypatch):
    from herdr.state_store import get_state_store
    from herdr.direct_dispatch import lineage_redispatch_candidates, plan_stage_dispatch
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path / 'state.db'))
    monkeypatch.setenv('TASKS_FILE', str(tmp_path / 'tasks.json'))
    store = get_state_store()
    store.save_task({'task_id': 'old', 'workflow_id': 'wf', 'node': 'requirements', 'status': 'failed'})
    cli = load('bin/herdr-task', 'cli_abandon_obligation')
    cli.supersede_task('old', reason='explicit scope removal', abandon=True)
    abandoned = store.get_task('old')
    assert abandoned['replacement_pending'] is False
    assert lineage_redispatch_candidates([abandoned]) == []
    node = {'id': 'requirements', 'label': 'Requirements', 'purpose': 'analyze',
            'required_outputs': ['spec'], 'rules': [],
            'agent_policy': {'max_concurrency': 1}}
    plan = plan_stage_dispatch('wf', node, [abandoned], 'analyze')
    assert plan['mode'] == 'wait'
    assert plan['specs'] == []


def test_abandoned_replacement_resolves_automatic_but_not_explicit_obligation():
    tasks = [{'task_id': 'spec', 'status': 'cleaned'},
             {'task_id': 'review', 'status': 'superseded', 'replacement_pending': True,
              'superseded_by': 'review-r2'},
             {'task_id': 'review-r2', 'status': 'superseded', 'replacement_pending': False}]
    assert node_is_complete(tasks)
    assert not node_is_complete(tasks, required_task_ids=['review'])
    assert not node_is_complete(tasks, required_task_ids=['review-r2'])


def test_pending_replacement_cannot_be_satisfied_by_unknown_verifier_reuse(tmp_path, monkeypatch):
    from herdr.state_store import get_state_store
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path / 'state.db'))
    get_state_store()
    ctrl = load('services/herdr-controller.py', 'ctrl_pending_reuse')
    monkeypatch.setattr(ctrl, 'load_tasks', lambda: [
        {'task_id': 'review', 'workflow_id': 'wf', 'node': 'test',
         'status': 'superseded', 'replacement_pending': True}])
    monkeypatch.setattr(ctrl, 'workflow_config_for', lambda wid: {'nodes': [{'id': 'test'}]})
    assert not ctrl.is_node_complete('wf', 'test')


def test_controller_optional_core_failure_does_not_erase_pending_obligation(monkeypatch):
    ctrl = load('services/herdr-controller.py', 'ctrl_pending_fallback')
    monkeypatch.setattr(ctrl, 'scheduler_core', None)
    monkeypatch.setattr(ctrl, 'workflow_config_for', lambda wid: {'nodes': [{'id': 'requirements'}]})
    monkeypatch.setattr(ctrl, 'load_tasks', lambda: [
        {'task_id': 'spec', 'workflow_id': 'wf', 'node': 'requirements', 'status': 'cleaned'},
        {'task_id': 'review', 'workflow_id': 'wf', 'node': 'requirements',
         'status': 'superseded', 'replacement_pending': True}])
    assert not ctrl.is_node_complete('wf', 'requirements')


@pytest.mark.parametrize('gateway', ['kernel', 'store', 'cas'])
def test_every_new_supersede_retains_atomic_replacement_obligation(tmp_path, monkeypatch, gateway):
    from herdr import kernel
    from herdr.state_store import get_state_store
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path / 'state.db'))
    monkeypatch.setenv('TASKS_FILE', str(tmp_path / 'tasks.json'))
    store = get_state_store()
    for tid, status in [('spec', 'cleaned'), ('review', 'failed')]:
        store.save_task({'task_id': tid, 'workflow_id': 'wf', 'node': 'requirements', 'status': status})
    if gateway == 'kernel':
        kernel.transition_task('review', 'superseded', 'rollback to retry', store=store)
    elif gateway == 'store':
        store.transition_task('review', 'superseded', 'retry')
    else:
        store.compare_and_set_task_transition('review', 'superseded', 'retry', expected_status='failed')
    assert not node_is_complete(store.list_tasks(workflow_id='wf'))
    task = store.get_task('review')
    assert task['replacement_pending'] is True
    events = store.list_events(event_type='task_transition', task_id='review')
    assert len(events) == 1
    assert events[0]['payload']['replacement_pending'] is True


def test_git_run_private_definition_does_not_follow_shared_edits(tmp_path, monkeypatch):
    from herdr import projects
    from herdr.state_store import get_state_store
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path / 'state.db'))
    monkeypatch.setenv('HERDR_WORKFLOW_DOCS_DIR', str(tmp_path / 'workflows'))
    config = tmp_path / 'workflow.json'
    config.write_text(json.dumps({'name': 'test', 'nodes': [{'id': 'requirements'}]}))
    project = {'project_id': 'p', 'project_name': 'test', 'project_root': str(tmp_path),
               'base_branch': 'main', 'workspace_id': 'w', 'coordinator_pane_id': 'c',
               'workflow_file': str(config)}
    projects.register_workflow('wf-new', project, requirement='test')
    record = get_state_store().get_workflow('wf-new')
    assert record['workflow_file'] != str(config)
    config.write_text(json.dumps({'name': 'test', 'nodes': [{'id': 'other'}]}))
    assert json.loads(Path(record['workflow_file']).read_text())['nodes'][0]['id'] == 'requirements'


def test_new_run_rejects_known_foreign_required_task_binding(tmp_path, monkeypatch):
    from herdr import projects
    from herdr.state_store import get_state_store
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path / 'state.db'))
    monkeypatch.setenv('HERDR_WORKFLOW_DOCS_DIR', str(tmp_path / 'workflows'))
    store = get_state_store()
    store.save_task({'task_id': 'old-impl', 'workflow_id': 'wf-old', 'node': 'implementation', 'status': 'cleaned'})
    config = tmp_path / 'workflow.json'
    config.write_text(json.dumps({'nodes': [{'id': 'implementation', 'required_task_ids': ['old-impl']}]}))
    project = {'project_id': 'p', 'project_name': 'test', 'project_root': str(tmp_path),
               'base_branch': 'main', 'workspace_id': 'w', 'coordinator_pane_id': 'c',
               'workflow_file': str(config)}
    with pytest.raises(ValueError, match='another workflow'):
        projects.register_workflow('wf-new', project)
    assert store.get_workflow('wf-new') is None


def test_run_freezes_exact_validated_definition_when_source_changes(tmp_path, monkeypatch):
    from herdr import projects
    from herdr.state_store import get_state_store
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path / 'state.db'))
    monkeypatch.setenv('HERDR_WORKFLOW_DOCS_DIR', str(tmp_path / 'workflows'))
    store = get_state_store()
    store.save_task({'task_id': 'foreign', 'workflow_id': 'wf-old', 'node': 'implementation', 'status': 'cleaned'})
    config = tmp_path / 'workflow.json'
    original = {'nodes': [{'id': 'implementation', 'required_task_ids': ['future-current']}],
                'custom': 'keep-me'}
    config.write_text(json.dumps(original))
    project = {'project_id': 'p', 'project_name': 'test', 'project_root': str(tmp_path),
               'base_branch': 'main', 'workspace_id': 'w', 'coordinator_pane_id': 'c',
               'workflow_file': str(config)}
    real_load = projects._load
    def change_after_read(path, default):
        result = real_load(path, default)
        if Path(path) == config:
            config.write_text(json.dumps({'nodes': [{'id': 'implementation', 'required_task_ids': ['foreign']}]}))
        return result
    monkeypatch.setattr(projects, '_load', change_after_read)
    projects.register_workflow('wf-new', project)
    record = store.get_workflow('wf-new')
    assert json.loads(Path(record['workflow_file']).read_text()) == original


def test_failed_private_snapshot_does_not_register_readable_run(tmp_path, monkeypatch):
    from herdr import projects
    from herdr.state_store import get_state_store
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path / 'state.db'))
    config = tmp_path / 'workflow.json'
    config.write_text(json.dumps({'nodes': [{'id': 'requirements'}]}))
    project = {'project_id': 'p', 'project_name': 'test', 'project_root': str(tmp_path),
               'base_branch': 'main', 'workspace_id': 'w', 'coordinator_pane_id': 'c',
               'workflow_file': str(config)}
    monkeypatch.setattr(projects, 'freeze_run_definition', lambda *a, **k: None)
    with pytest.raises(RuntimeError, match='not registered'):
        projects.register_workflow('wf-new', project)
    assert get_state_store().get_workflow('wf-new') is None


def test_dispatch_prompt_resolves_business_paths_in_clone(tmp_path, monkeypatch):
    from herdr.state_store import get_state_store
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path / 'state.db'))
    monkeypatch.setenv('TASKS_FILE', str(tmp_path / 'tasks.json'))
    store = get_state_store()
    clone = tmp_path / 'clone'
    clone.mkdir()
    store.save_task({'task_id': 'docs', 'workflow_id': 'wf', 'node': 'requirements',
                     'status': 'pending', 'pane_id': 'test-pane', 'agent': 'opencode',
                     'clone_path': str(clone), 'goal': 'write doc'})
    cli = load('bin/herdr-task', 'cli_clone_write_root')
    monkeypatch.setattr(cli, '_compile_working_context_ref', lambda task: None)
    sent = []
    def transport(cmd, **kwargs):
        if cmd[:3] == ['herdr', 'agent', 'prompt']:
            sent.append(cmd[4])
        return subprocess.CompletedProcess(cmd, 0, json.dumps({'result': {'agent': {'agent_status': 'working'}}}), '')
    monkeypatch.setattr(cli.subprocess, 'run', transport)
    cli.dispatch_task('docs', '仓库根目录 /source/project；写 docs/specs/spec.md')
    assert f'WORKER_WRITE_ROOT: {clone}' in sent[0]
    assert '业务相对路径以此目录为根' in sent[0]
