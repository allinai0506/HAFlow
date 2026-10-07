"""Delivery is proven by the repository, not a completion assertion."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from herdr.state_store import SQLiteStateStore
from herdr.completion_receipt import issue_completion_contract, report_completion

ROOT = Path(__file__).resolve().parents[1]


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


@pytest.fixture
def delivery_env(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q')
    git(repo, 'config', 'user.email', 'test@example.invalid')
    git(repo, 'config', 'user.name', 'Test')
    (repo / 'app.py').write_text('value = 1\n')
    (repo / 'rules.md').write_text('Repository delivery rules\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'baseline')
    git(repo, 'checkout', '-qb', 'agent/impl-atomic-fix')
    store = SQLiteStateStore(tmp_path / 'state.db')
    store.save_workflow({'workflow_id': 'wf', 'status': 'running', 'execution_id': 'exec',
                         'project_root': str(repo), 'current_stage': 'implementation',
                         'config': {'nodes': [{'id': 'implementation'}, {'id': 'test', 'depends_on': ['implementation']}]}})
    contract = {'version': 1, 'allowed_paths': ['app.py', 'tests/*', 'docs/*'],
                'required_files': [{'path': 'docs/postmortem.md', 'headings': ['## Root Cause', '## Prevention', '## Validation']}],
                'checks': [], 'auto_rework': True}
    store.save_task({'task_id': 't', 'workflow_id': 'wf', 'node': 'implementation', 'run_id': 'run',
                     'execution_id': 'exec', 'status': 'working', 'integration_mode': 'git',
                     'clone_path': str(repo), 'branch': git(repo, 'branch', '--show-current'),
                     'baseline_commit': git(repo, 'rev-parse', 'HEAD'), 'baseline_untracked': [],
                     'baseline_fingerprint': {'tracked': {}, 'untracked': {}},
                     'delivery_contract': contract, 'pane_id': 'p', 'started_at': time.time() - 100})
    (repo / 'app.py').write_text('value = 2\n')
    return repo, store


def complete_doc(repo):
    (repo / 'docs').mkdir(exist_ok=True)
    (repo / 'docs/postmortem.md').write_text('## Root Cause\nMissing authorization\n## Prevention\nRegression\n## Validation\nTests passed\n')


def test_missing_doc_prevents_completion_receipt(delivery_env):
    repo, store = delivery_env
    issued = issue_completion_contract('t', store)
    identity = json.loads(Path(issued['path']).read_text())
    with pytest.raises(ValueError, match='delivery'):
        report_completion('t', identity, [], store)
    assert store.get_task('t')['status'] == 'working'


def test_missing_required_output_has_machine_reason(delivery_env):
    from herdr.task_delivery import check_delivery
    _, store = delivery_env
    receipt = check_delivery('t', store=store)
    assert receipt['status'] == 'blocked'
    assert receipt['issues'] == [{'code': 'required_file_missing', 'path': 'docs/postmortem.md'}]
    events = store.list_events(task_id='t', event_type='delivery_checked')
    assert len(events) == 1 and events[0]['payload']['status'] == 'blocked'


def test_three_file_scope_cannot_silently_include_doc(delivery_env):
    from herdr.task_delivery import check_delivery
    _, store = delivery_env
    contract = store.get_task('t')['delivery_contract']
    contract['allowed_paths'] = ['app.py', 'tests/*']
    store.update_task_metadata('t', {'delivery_contract': contract})
    receipt = check_delivery('t', store=store)
    assert receipt['status'] == 'blocked'
    assert any(issue['code'] == 'scope_conflict' and issue['path'] == 'docs/postmortem.md' for issue in receipt['issues'])


def test_heading_only_doc_is_not_complete(delivery_env):
    from herdr.task_delivery import check_delivery
    repo, store = delivery_env
    complete_doc(repo)
    (repo / 'docs/postmortem.md').write_text('## Root Cause\n## Prevention\n## Validation\n')
    assert any(issue['code'] == 'required_section_empty' for issue in check_delivery('t', store=store)['issues'])


def test_check_failure_retains_tail_and_does_not_claim_ready(delivery_env):
    from herdr.task_delivery import check_delivery
    repo, store = delivery_env
    complete_doc(repo)
    contract = store.get_task('t')['delivery_contract']
    contract['checks'] = [{'id': 'native-gate', 'argv': [sys.executable, '-c', "print('OK\\n'*700); print('Missing postmortem'); raise SystemExit(1)"], 'timeout': 5}]
    store.update_task_metadata('t', {'delivery_contract': contract})
    result = check_delivery('t', store=store)
    assert result['status'] == 'blocked'
    assert result['checks'][0]['exit_code'] == 1
    assert 'Missing postmortem' in result['checks'][0]['detail']


def test_fresh_proof_allows_completion_and_edit_invalidates_it(delivery_env):
    from herdr.task_delivery import check_delivery
    repo, store = delivery_env
    complete_doc(repo)
    issued = issue_completion_contract('t', store)
    assert check_delivery('t', store=store)['status'] == 'ready'
    (repo / 'app.py').write_text('value = 3\n')
    with pytest.raises(ValueError, match='delivery'):
        report_completion('t', json.loads(Path(issued['path']).read_text()), [], store)
    assert check_delivery('t', store=store)['status'] == 'ready'
    receipt = report_completion('t', json.loads(Path(issued['path']).read_text()), [], store)
    assert receipt['run_id'] == 'run'


def test_changes_outside_scope_block(delivery_env):
    from herdr.task_delivery import check_delivery
    repo, store = delivery_env
    complete_doc(repo)
    (repo / 'unrelated.py').write_text('not authorized\n')
    result = check_delivery('t', store=store)
    assert any(i == {'code': 'outside_scope', 'path': 'unrelated.py'} for i in result['issues'])


def test_readonly_task_cannot_execute_delivery_command(delivery_env):
    from herdr.task_delivery import check_delivery
    repo, store = delivery_env
    marker = repo / 'executed'
    contract = store.get_task('t')['delivery_contract']
    contract['checks'] = [{'id': 'write', 'argv': [sys.executable, '-c', f"open({str(marker)!r},'w').write('bad')"], 'timeout': 5}]
    store.update_task_metadata('t', {'dispatch_role': 'reviewer', 'delivery_contract': contract})
    with pytest.raises(ValueError, match='read.only'):
        check_delivery('t', store=store)
    assert not marker.exists()


def test_required_output_symlink_cannot_escape_clone(delivery_env, tmp_path):
    from herdr.task_delivery import check_delivery
    repo, store = delivery_env
    external = tmp_path / 'external.md'; external.write_text('private\n')
    (repo / 'docs').mkdir(); (repo / 'docs/postmortem.md').symlink_to(external)
    assert any(i['code'] == 'unsafe_path' for i in check_delivery('t', store=store)['issues'])


def test_mutating_check_is_never_a_fresh_success(delivery_env):
    from herdr.task_delivery import check_delivery
    repo, store = delivery_env
    complete_doc(repo)
    contract = store.get_task('t')['delivery_contract']
    contract['checks'] = [{'id': 'mutating', 'argv': [sys.executable, '-c', "open('app.py','w').write('changed')"], 'timeout': 5}]
    store.update_task_metadata('t', {'delivery_contract': contract})
    assert check_delivery('t', store=store)['status'] == 'unknown'


def test_cli_check_then_completion_uses_real_sqlite(delivery_env):
    repo, store = delivery_env
    complete_doc(repo)
    issued = issue_completion_contract('t', store)
    env = {**os.environ, 'HERDR_STATE_DB': str(store.db_path), 'HERDR_CONTROLLER_DIR': str(store.db_path.parent),
           'TASKS_FILE': str(store.db_path.parent / 'tasks.json'), 'WORKFLOWS_FILE': str(store.db_path.parent / 'workflows.json')}
    result = subprocess.run([str(ROOT / 'bin/herdr-task'), 'delivery-check', 't'], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['status'] == 'ready'
    result = subprocess.run([str(ROOT / 'bin/herdr-task'), 'report-completion', 't', '--identity-file', issued['path']], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['run_id'] == 'run'
    assert store.list_events(task_id='t', event_type='delivery_checked')[0]['payload']['status'] == 'ready'


def test_configured_scope_conflict_is_rejected_before_workflow_launch():
    from herdr.workflow import normalize_workflow
    contract = {'version': 1, 'allowed_paths': ['app.py'], 'required_files': [{'path': 'docs/postmortem.md'}], 'checks': []}
    with pytest.raises(ValueError, match='scope'):
        normalize_workflow({'nodes': [{'id': 'implementation', 'delivery_contract': contract}]})


def test_direct_and_rework_delivery_persist_same_requirements(delivery_env, monkeypatch):
    from herdr.supervisor_delivery import deliver
    repo, store = delivery_env
    sent = []
    deliver(store.get_task('t'), store, 'INITIAL', {'intervention_id': 'initial-run'}, 'business goal', lambda p, s: sent.append(s))
    assert 'docs/postmortem.md' in sent[0] and 'authorized' in sent[0]
    initial = store.list_events(task_id='t', event_type='initial_dispatch_prepared')[0]['payload']['prompt']
    assert initial == sent[0]
    deliver(store.get_task('t'), store, 'REWORK', {'intervention_id': 'rework-run'}, 'fix required', lambda p, s: sent.append(s))
    assert 'docs/postmortem.md' in sent[1]


def test_branch_and_staged_changes_invalidate_ready_receipt(delivery_env):
    from herdr.task_delivery import check_delivery, require_delivery
    repo, store = delivery_env
    complete_doc(repo)
    assert check_delivery('t', store=store)['status'] == 'ready'
    git(repo, 'add', 'app.py')
    with pytest.raises(ValueError, match='delivery'):
        require_delivery(store.get_task('t'), store)
    assert check_delivery('t', store=store)['status'] == 'ready'
    git(repo, 'checkout', '-qb', 'other')
    with pytest.raises(ValueError, match='delivery'):
        require_delivery(store.get_task('t'), store)


def test_output_cap_is_unknown_not_a_pass(delivery_env):
    from herdr.task_delivery import check_delivery
    repo, store = delivery_env
    complete_doc(repo)
    contract = store.get_task('t')['delivery_contract']
    contract['checks'] = [{'id': 'overflow', 'argv': [sys.executable, '-c', "print('a'*100000)"], 'timeout': 5}]
    store.update_task_metadata('t', {'delivery_contract': contract})
    receipt = check_delivery('t', store=store)
    assert receipt['status'] == 'unknown'
    assert receipt['checks'][0]['status'] == 'output_limit'


def test_validation_redacts_tail_before_persistence(delivery_env):
    from herdr.task_delivery import check_delivery
    repo, store = delivery_env
    complete_doc(repo)
    contract = store.get_task('t')['delivery_contract']
    contract['checks'] = [{'id': 'failure', 'argv': [sys.executable, '-c', "print('API_KEY=fictional-secret-value'); raise SystemExit(1)"], 'timeout': 5}]
    store.update_task_metadata('t', {'delivery_contract': contract})
    receipt = check_delivery('t', store=store)
    assert 'fictional-secret-value' not in receipt['checks'][0]['detail']
    # Configured argv is an authority input, not diagnostic output; no secret argv should be logged in receipts.
    assert 'fictional-secret-value' not in json.dumps(store.list_events(task_id='t', event_type='delivery_checked'))


def test_edit_after_declaration_cannot_be_consumed(delivery_env):
    from herdr.task_delivery import check_delivery
    from herdr.completion_receipt import consume_completion_receipt
    repo, store = delivery_env
    complete_doc(repo)
    issued = issue_completion_contract('t', store)
    check_delivery('t', store=store)
    report_completion('t', json.loads(Path(issued['path']).read_text()), [], store)
    (repo / 'app.py').write_text('changed after declaration\n')
    result = consume_completion_receipt('t', store, now=time.time() + 100)
    assert result['accepted'] is False
    assert result['reason'] == 'delivery_not_ready'
    assert store.get_task('t')['status'] == 'working'


@pytest.mark.parametrize('output', ['{"password": "fictional-json-secret"}', "CLIENT_SECRET='fictional quoted secret'", 'API_KEY="fictional spaced secret"'])
def test_sensitive_structured_output_never_enters_delivery_event(delivery_env, output):
    from herdr.task_delivery import check_delivery
    repo, store = delivery_env
    complete_doc(repo)
    contract = store.get_task('t')['delivery_contract']
    contract['checks'] = [{'id': 'sensitive', 'argv': [sys.executable, '-c', f'print({output!r}); raise SystemExit(1)'], 'timeout': 5}]
    store.update_task_metadata('t', {'delivery_contract': contract})
    check_delivery('t', store=store)
    text = json.dumps(store.list_events(task_id='t', event_type='delivery_checked'))
    assert 'fictional' not in text
    assert 'quoted secret' not in text and 'spaced secret' not in text


def test_allowed_nonrequired_symlink_does_not_receive_ready(delivery_env, tmp_path):
    from herdr.task_delivery import check_delivery
    repo, store = delivery_env
    complete_doc(repo)
    external = tmp_path / 'private'; external.write_text('external\n')
    (repo / 'tests').mkdir(); (repo / 'tests/allowed').symlink_to(external)
    receipt = check_delivery('t', store=store)
    assert receipt['status'] == 'blocked'
    assert {'code': 'unsafe_path', 'path': 'tests/allowed'} in receipt['issues']


def test_check_subprocess_does_not_inherit_controller_state_or_model_keys(delivery_env, monkeypatch):
    from herdr.task_delivery import check_delivery
    repo, store = delivery_env
    complete_doc(repo)
    monkeypatch.setenv('HERDR_STATE_DB', str(store.db_path))
    monkeypatch.setenv('JEV_API_KEY', 'fictional-isolation-key')
    contract = store.get_task('t')['delivery_contract']
    contract['checks'] = [{'id': 'isolation', 'argv': [sys.executable, '-c', "import os; assert not os.environ.get('HERDR_STATE_DB'); assert not os.environ.get('JEV_API_KEY')"], 'timeout': 5}]
    store.update_task_metadata('t', {'delivery_contract': contract})
    assert check_delivery('t', store=store)['status'] == 'ready'


def test_interrupted_check_is_not_implicitly_reexecuted(delivery_env, monkeypatch):
    from herdr import task_delivery as delivery
    repo, store = delivery_env
    complete_doc(repo)
    contract = store.get_task('t')['delivery_contract']
    contract['checks'] = [{'id': 'bounded', 'argv': [sys.executable, '-c', 'print("ok")'], 'timeout': 5}]
    store.update_task_metadata('t', {'delivery_contract': contract})
    real = delivery.run_bounded
    def crash(argv, **kw):
        if argv[0] == sys.executable:
            raise RuntimeError('process crash before result receipt')
        return real(argv, **kw)
    monkeypatch.setattr(delivery, 'run_bounded', crash)
    with pytest.raises(RuntimeError):
        delivery.check_delivery('t', store=store)
    monkeypatch.setattr(delivery, 'run_bounded', real)
    with pytest.raises(ValueError, match='unknown'):
        delivery.check_delivery('t', store=type(store)(store.db_path))


def test_started_recheck_invalidates_previous_ready_completion(delivery_env):
    from herdr.task_delivery import check_delivery
    repo, store = delivery_env
    complete_doc(repo)
    issued = issue_completion_contract('t', store)
    ready = check_delivery('t', store=store)
    store.record_event('delivery_check_started', {**ready, 'status': 'started', 'operation_id': 'new-attempt'},
                       task_id='t', workflow_id='wf', run_id='run', source='delivery-check')
    with pytest.raises(ValueError, match='delivery'):
        report_completion('t', json.loads(Path(issued['path']).read_text()), [], store)


def test_sensitive_shaped_git_filename_still_invalidates_proof(delivery_env):
    from herdr.task_delivery import check_delivery, require_delivery
    repo, store = delivery_env
    complete_doc(repo)
    contract = store.get_task('t')['delivery_contract']; contract['allowed_paths'] = ['*']
    store.update_task_metadata('t', {'delivery_contract': contract})
    file = repo / 'password=fictional-name'; file.write_text('first\n')
    assert check_delivery('t', store=store)['status'] == 'ready'
    file.write_text('second\n')
    with pytest.raises(ValueError, match='delivery'):
        require_delivery(store.get_task('t'), store)


def test_non_utf8_git_metadata_is_rejected_before_proof(delivery_env, monkeypatch):
    from herdr import task_delivery as delivery
    repo, store = delivery_env
    complete_doc(repo)
    real = delivery.run_bounded
    def external_git(argv, **kwargs):
        if argv[0] == 'git' and 'ls-files' in argv:
            return real([sys.executable, '-c', "import sys; sys.stdout.buffer.write(bytes([255, 0]))"], **kwargs)
        return real(argv, **kwargs)
    monkeypatch.setattr(delivery, 'run_bounded', external_git)
    with pytest.raises(ValueError, match='UTF-8'):
        delivery.check_delivery('t', store=store)
    assert not store.list_events(task_id='t', event_type='delivery_checked')
