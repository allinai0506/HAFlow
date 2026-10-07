"""Uncommitted delivery repair never substitutes for a frozen candidate."""
import importlib.util
import json
from pathlib import Path
import subprocess
import time

import pytest
from herdr import state_db, recovery_store
from herdr.workflow_progress import assess_workflow
from herdr.workflow import normalize_workflow
from tests.test_task_delivery import delivery_env, complete_doc, git, ROOT


def test_normalization_preserves_declared_delivery_scope():
    contract = {'version': 1, 'allowed_paths': ['app.py'], 'required_files': [], 'checks': []}
    node = normalize_workflow({'nodes': [{'id': 'implementation', 'delivery_contract': contract}]})['nodes'][0]
    assert node.get('delivery_contract') == contract


def mark_delivery_failure(store):
    from herdr.task_delivery import check_delivery
    receipt = check_delivery('t', store=store)
    store.save_task({**store.get_task('t'), 'status': 'completed', 'finalize_escalated': True,
                     'finalize_escalate_reason': 'delivery_incomplete', 'delivery_failure': receipt})
    return receipt


def test_no_candidate_delivery_has_own_recovery_operation(delivery_env):
    _, store = delivery_env
    mark_delivery_failure(store)
    wf, config, tasks = recovery_store.read_snapshot(store.db_path, 'wf')
    assessment = assess_workflow(wf, config, tasks)
    assert len(assessment['obligations']) == 1
    obligation = assessment['obligations'][0]
    assert obligation['kind'] == 'delivery'
    assert obligation['candidate_sha'] is None
    assert obligation['status'] == 'pending'
    assert obligation['task_ids'] == ['t']


def test_scope_conflict_is_human_decision_not_automatic_repair(delivery_env):
    _, store = delivery_env
    contract = store.get_task('t')['delivery_contract']; contract['allowed_paths'] = ['app.py']
    store.update_task_metadata('t', {'delivery_contract': contract})
    mark_delivery_failure(store)
    wf, config, tasks = recovery_store.read_snapshot(store.db_path, 'wf')
    obligation = assess_workflow(wf, config, tasks)['obligations'][0]
    assert obligation['status'] == 'waiting_human'
    assert obligation['reason'] == 'delivery_scope_conflict'


def test_generic_completed_to_rework_is_still_illegal(delivery_env):
    _, store = delivery_env
    mark_delivery_failure(store)
    with pytest.raises(ValueError):
        store.transition_task('t', 'rework', source='operator', reason='skip delivery guard')
    assert store.get_task('t')['status'] == 'completed'


def test_owning_worker_gets_one_run_bound_repair_and_new_epoch(delivery_env):
    from herdr.delivery_rework import repair_delivery
    _, store = delivery_env
    mark_delivery_failure(store)
    sent = []
    task = store.get_task('t')
    result = repair_delivery('t', task['run_id'], 'repair-1', store=store,
                             probe=lambda t: (True, 'identity_match'), send=lambda p, text: sent.append(text))
    assert result['rework_dispatched']
    fresh = store.get_task('t')
    assert fresh['status'] == 'rework'
    assert fresh['run_id'] == 'run'
    assert fresh['delivery_rework_attempts'] == 1
    assert fresh['rework_delivery'] == 'delivered'
    assert 'docs/postmortem.md' in sent[0]
    result = repair_delivery('t', 'run', 'repair-1', store=store,
                             probe=lambda t: (True, 'identity_match'), send=lambda *a: pytest.fail('duplicate send'))
    assert result['already_applied']
    assert len(store.list_events(task_id='t', event_type='rework_dispatched')) == 1


def test_changed_run_or_unowned_pane_never_receives_repair(delivery_env):
    from herdr.delivery_rework import repair_delivery
    _, store = delivery_env
    mark_delivery_failure(store)
    for run, probe in [('other', lambda t: (True, 'match')), ('run', lambda t: (False, 'unknown'))]:
        with pytest.raises(ValueError):
            repair_delivery('t', run, 'request', store=store, probe=probe,
                             send=lambda *a: pytest.fail('unauthorized native effect'))
    assert store.get_task('t')['status'] == 'completed'
    assert not store.list_events(task_id='t', event_type='rework_dispatch_prepared')


def test_transport_unknown_cannot_be_retried_even_with_new_request(delivery_env):
    from herdr.delivery_rework import repair_delivery
    from herdr.supervisor_delivery import DeliveryUnknown
    _, store = delivery_env
    mark_delivery_failure(store)
    def fail(*args):
        raise RuntimeError('native transport outcome unknown')
    with pytest.raises(DeliveryUnknown):
        repair_delivery('t', 'run', 'repair-1', store=store, probe=lambda t: (True, 'match'), send=fail)
    for request in ('repair-1', 'repair-2'):
        with pytest.raises((ValueError, DeliveryUnknown)):
            repair_delivery('t', 'run', request, store=store, probe=lambda t: (True, 'match'),
                             send=lambda *a: pytest.fail('unknown prompt resent'))
    assert store.get_task('t')['rework_delivery'] == 'pending'


def test_probe_race_cannot_rework_new_run(delivery_env):
    from herdr.delivery_rework import repair_delivery
    _, store = delivery_env
    mark_delivery_failure(store)
    other = type(store)(store.db_path)
    def raced(task):
        other.save_task({**other.get_task('t'), 'run_id': 'new-run'})
        return True, 'match'
    with pytest.raises(ValueError):
        repair_delivery('t', 'run', 'repair-1', store=store, probe=raced,
                         send=lambda *a: pytest.fail('stale run transport'))
    assert store.get_task('t')['status'] == 'completed'
    assert not store.list_events(task_id='t', event_type='rework_dispatch_prepared')


def test_three_round_budget_survives_restart(delivery_env):
    from herdr.delivery_rework import repair_delivery
    _, store = delivery_env
    mark_delivery_failure(store)
    store.update_task_metadata('t', {'delivery_rework_attempts': 3})
    with pytest.raises(ValueError, match='budget'):
        repair_delivery('t', 'run', 'repair-4', store=type(store)(store.db_path),
                         probe=lambda t: (True, 'match'), send=lambda *a: pytest.fail('fourth repair'))


def test_native_hook_error_tail_is_preserved_and_unknown_remains_retryable(delivery_env, monkeypatch):
    _, store = delivery_env
    store.save_task({**store.get_task('t'), 'status': 'completed'})
    spec = importlib.util.spec_from_file_location('delivery_controller_test', ROOT / 'services/herdr-controller.py')
    ctl = importlib.util.module_from_spec(spec); spec.loader.exec_module(ctl)
    monkeypatch.setattr(ctl, '_get_store', lambda: store)
    monkeypatch.setattr(ctl, 'get_task', store.get_task)
    monkeypatch.setattr(ctl, 'maybe_complete_on_task_done', lambda _: [])
    monkeypatch.setattr(ctl, 'ensure_no_git_processes', lambda _: None)
    native = subprocess.CompletedProcess([], 1, 'OK\n' * 700 + 'Missing postmortem', '')
    monkeypatch.setattr(ctl.subprocess, 'run', lambda *a, **k: native)
    result = ctl.finalize_completed_task('t')
    assert result['retryable'] is True
    event = store.list_events(task_id='t', event_type='finalize_commit_error')[0]['payload']
    assert 'Missing postmortem' in event['detail']


def test_structured_delivery_rejection_stops_blind_commit_retries(delivery_env, monkeypatch):
    _, store = delivery_env
    receipt = mark_delivery_failure(store)
    store.update_task_metadata('t', {'finalize_escalated': False})
    from herdr.task_delivery import check_delivery
    receipt = check_delivery('t', store=store)
    spec = importlib.util.spec_from_file_location('delivery_rejection_controller_test', ROOT / 'services/herdr-controller.py')
    ctl = importlib.util.module_from_spec(spec); spec.loader.exec_module(ctl)
    monkeypatch.setattr(ctl, '_get_store', lambda: store)
    monkeypatch.setattr(ctl, 'get_task', store.get_task)
    monkeypatch.setattr(ctl, 'maybe_complete_on_task_done', lambda _: [])
    monkeypatch.setattr(ctl, 'ensure_no_git_processes', lambda _: None)
    payload = {'task_id': 't', 'result': 'delivery_blocked', 'delivery': receipt}
    native = subprocess.CompletedProcess([], 7, 'HERDR_COMMIT_RESULT=' + json.dumps(payload), '')
    monkeypatch.setattr(ctl.subprocess, 'run', lambda *a, **k: native)
    result = ctl.finalize_completed_task('t')
    assert result['retryable'] is False
    assert result['kind'] == 'delivery'
    task = store.get_task('t')
    assert task['finalize_escalate_reason'] == 'delivery_incomplete'
    assert task['delivery_failure']['run_id'] == 'run'


def test_actual_cli_commit_runs_native_hook_and_preserves_failure(delivery_env):
    import os
    from herdr.task_delivery import check_delivery
    repo, store = delivery_env
    complete_doc(repo)
    hook = repo / '.git/hooks/pre-commit'
    hook.write_text('#!/bin/sh\necho "Repository gate refuses candidate" >&2\nexit 1\n'); hook.chmod(0o755)
    store.save_task({**store.get_task('t'), 'status': 'completed'})
    env = {**os.environ, 'HERDR_STATE_DB': str(store.db_path), 'HERDR_CONTROLLER_DIR': str(store.db_path.parent),
           'TASKS_FILE': str(store.db_path.parent / 'tasks.json'), 'WORKFLOWS_FILE': str(store.db_path.parent / 'workflows.json')}
    old_head = git(repo, 'rev-parse', 'HEAD')
    result = subprocess.run([str(ROOT / 'bin/herdr-task'), 'commit', 't'], env=env, capture_output=True, text=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'Repository gate refuses candidate' in result.stderr
    assert git(repo, 'rev-parse', 'HEAD') == old_head
    assert store.get_task('t')['status'] == 'completed'
    hook.unlink()
    result = subprocess.run([str(ROOT / 'bin/herdr-task'), 'commit', 't'], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert store.get_task('t')['status'] == 'committed'
    assert git(repo, 'rev-parse', 'HEAD') == store.get_task('t')['commit']


def test_controller_to_claimed_repair_to_real_commit_then_result(delivery_env, monkeypatch):
    from herdr import delivery_rework as repair
    from herdr.workflow_recovery import drive_recovery
    repo, store = delivery_env
    mark_delivery_failure(store)
    monkeypatch.setattr(repair, 'owned_live_pane', lambda task: (True, 'identity_match'))
    # Only the native transport is replaced: contract, transaction, operation and completion receipts remain real.
    real_repair = repair.repair_delivery
    sends = []
    monkeypatch.setattr(repair, 'repair_delivery', lambda tid, run, req, **kw: real_repair(tid, run, req, **kw, send=lambda p, text: sends.append(text)))
    drive_recovery(store, 'wf', lambda operation, owner: repair.execute_delivery_recovery(store, operation, owner))
    operations = recovery_store.list_operations(store.db_path, 'wf')
    assert len(operations) == 1 and operations[0]['status'] == 'awaiting_result'
    assert store.get_task('t')['status'] == 'rework' and len(sends) == 1
    # Another drive must wait for results instead of issuing another prompt.
    drive_recovery(store, 'wf', lambda *args: pytest.fail('duplicate repair'))
    assert recovery_store.list_operations(store.db_path, 'wf')[0]['status'] == 'awaiting_result'
    assert len(sends) == 1


def test_owner_probe_changed_request_before_send_retains_unknown(delivery_env):
    from herdr.delivery_rework import repair_delivery
    from herdr.supervisor_delivery import DeliveryUnknown
    _, store = delivery_env
    mark_delivery_failure(store)
    calls = []
    other = type(store)(store.db_path)
    def probe(task):
        calls.append(task['status'])
        if task['status'] == 'rework':
            other.update_task_metadata('t', {'rework_request_id': 'replacement'})
        return True, 'match'
    with pytest.raises(DeliveryUnknown):
        repair_delivery('t', 'run', 'request', store=store, probe=probe,
                         send=lambda *a: pytest.fail('changed ownership sent'))
    assert store.get_task('t')['rework_request_id'] == 'replacement'


def test_unregistered_commit_is_not_reopened_as_delivery_rework(delivery_env):
    from herdr.delivery_rework import repair_delivery
    repo, store = delivery_env
    mark_delivery_failure(store)
    git(repo, 'add', 'app.py'); git(repo, 'commit', '-qm', 'unregistered')
    with pytest.raises(ValueError, match='unregistered'):
        repair_delivery('t', 'run', 'request', store=store, probe=lambda t: (True, 'match'), send=lambda *a: pytest.fail('reopened committed work'))


def test_full_repair_commit_integrate_then_operation_resolves(delivery_env, monkeypatch, tmp_path):
    import os
    from herdr import delivery_rework as repair
    from herdr.completion_receipt import report_completion, consume_completion_receipt
    from herdr.task_delivery import check_delivery
    from herdr.workflow_recovery import drive_recovery
    repo, store = delivery_env
    remote = tmp_path / 'origin.git'
    subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
    git(repo, 'remote', 'add', 'origin', str(remote))
    git(repo, 'push', '-q', 'origin', 'HEAD:main')
    source = tmp_path / 'source'
    subprocess.run(['git', 'clone', '-q', '-b', 'main', str(remote), str(source)], check=True)
    store.update_task_metadata('t', {'source_repo': str(source), 'base_branch': 'main'})
    mark_delivery_failure(store)
    monkeypatch.setattr(repair, 'owned_live_pane', lambda task: (True, 'match'))
    real_repair = repair.repair_delivery
    monkeypatch.setattr(repair, 'repair_delivery', lambda tid, run, req, **kw: real_repair(tid, run, req, **kw, send=lambda *a: None))
    drive_recovery(store, 'wf', lambda operation, owner: repair.execute_delivery_recovery(store, operation, owner))
    complete_doc(repo)
    check_delivery('t', store=store)
    task = store.get_task('t'); identity = json.loads(Path(task['completion_identity_path']).read_text())
    report_completion('t', identity, [], store)
    result = consume_completion_receipt('t', store, now=time.time() + 100)
    assert result['accepted']
    store.transition_task('t', 'completed', source='test', reason='verified business work')
    env = {**os.environ, 'HERDR_STATE_DB': str(store.db_path), 'HERDR_CONTROLLER_DIR': str(store.db_path.parent),
           'TASKS_FILE': str(store.db_path.parent / 'tasks.json'), 'WORKFLOWS_FILE': str(store.db_path.parent / 'workflows.json'),
           'HERDR_GIT_LOCK_ROOT': str(tmp_path / 'git-locks')}
    for command in ('commit', 'integrate'):
        result = subprocess.run([str(ROOT / 'bin/herdr-task'), command, 't'], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
    task = store.get_task('t')
    assert task['status'] == 'integrated'
    assert git(source, 'rev-parse', 'refs/herdr/tasks/t') == task['commit']
    operations = recovery_store.list_operations(store.db_path, 'wf')
    op = next(o for o in operations if o['payload']['kind'] == 'delivery')
    settled = recovery_store.settle_result(store.db_path, op['id'], op['version'], time.time())
    assert settled['status'] == 'resolved'
    assert settled['detail']['reason'] == 'delivery_finalized'


def test_attributable_direct_commit_cannot_skip_delivery(delivery_env):
    import os
    repo, store = delivery_env
    store.save_task({**store.get_task('t'), 'status': 'completed'})
    git(repo, 'add', 'app.py'); git(repo, 'commit', '-qm', 'worker direct commit')
    env = {**os.environ, 'HERDR_STATE_DB': str(store.db_path), 'HERDR_CONTROLLER_DIR': str(store.db_path.parent),
           'TASKS_FILE': str(store.db_path.parent / 'tasks.json'), 'WORKFLOWS_FILE': str(store.db_path.parent / 'workflows.json')}
    result = subprocess.run([str(ROOT / 'bin/herdr-task'), 'commit', 't'], env=env, capture_output=True, text=True)
    assert result.returncode == 7, result.stdout + result.stderr
    assert 'delivery_blocked' in result.stdout
    assert store.get_task('t')['status'] == 'completed'
    assert not store.get_task('t').get('commit')


def test_stale_subprocess_delivery_failure_cannot_escalate_replacement(delivery_env, monkeypatch):
    _, store = delivery_env
    receipt = mark_delivery_failure(store)
    store.update_task_metadata('t', {'finalize_escalated': False, 'delivery_failure': None})
    spec = importlib.util.spec_from_file_location('late_delivery_controller', ROOT / 'services/herdr-controller.py')
    ctl = importlib.util.module_from_spec(spec); spec.loader.exec_module(ctl)
    monkeypatch.setattr(ctl, '_get_store', lambda: store); monkeypatch.setattr(ctl, 'get_task', store.get_task)
    monkeypatch.setattr(ctl, 'maybe_complete_on_task_done', lambda _: []); monkeypatch.setattr(ctl, 'ensure_no_git_processes', lambda _: None)
    def native(*args, **kwargs):
        store.save_task({**store.get_task('t'), 'run_id': 'replacement-run'})
        return subprocess.CompletedProcess([], 7, 'HERDR_COMMIT_RESULT=' + json.dumps({'task_id': 't', 'result': 'delivery_blocked', 'delivery': receipt}), '')
    monkeypatch.setattr(ctl.subprocess, 'run', native)
    result = ctl.finalize_completed_task('t')
    assert result['kind'] == 'stale_delivery_result'
    fresh = store.get_task('t')
    assert fresh['run_id'] == 'replacement-run'
    assert not fresh.get('delivery_failure') and not fresh.get('finalize_escalated')


def test_durable_dispatch_receipt_repairs_post_send_crash_without_resend(delivery_env, monkeypatch):
    from herdr import delivery_rework as repair
    _, store = delivery_env
    mark_delivery_failure(store)
    original = repair.deliver
    sends = []
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('crash after durable send receipt')
    monkeypatch.setattr(repair, 'deliver', crash)
    with pytest.raises(RuntimeError):
        repair.repair_delivery('t', 'run', 'repair-1', store=store, probe=lambda t: (True, 'match'), send=lambda *a: sends.append(a))
    assert store.get_task('t')['rework_delivery'] == 'pending'
    assert len(store.list_events(task_id='t', event_type='rework_dispatched')) == 1
    monkeypatch.setattr(repair, 'deliver', original)
    result = repair.repair_delivery('t', 'run', 'repair-1', store=type(store)(store.db_path),
                                    probe=lambda t: (True, 'match'), send=lambda *a: pytest.fail('already delivered prompt resent'))
    assert result['already_applied']
    assert store.get_task('t')['rework_delivery'] == 'delivered'
    assert len(sends) == 1


def test_legacy_rework_receives_pinned_delivery_requirements(delivery_env, monkeypatch):
    import importlib.machinery
    from types import SimpleNamespace
    from herdr import task_resources, kernel
    _, store = delivery_env
    loader = importlib.machinery.SourceFileLoader('legacy_delivery_cli', str(ROOT / 'bin/herdr-task'))
    spec = importlib.util.spec_from_loader(loader.name, loader); cli = importlib.util.module_from_spec(spec); loader.exec_module(cli)
    monkeypatch.setattr(cli, '_get_store', lambda: store)
    monkeypatch.setattr(task_resources, 'owned_live_pane', lambda task: (True, 'match'))
    monkeypatch.setattr(kernel, 'sync_tasks_projection', lambda **kw: None)
    sent = []
    monkeypatch.setattr(cli, '_herdr', lambda *args: (sent.append(args[-1]) or SimpleNamespace(returncode=0)))
    cli.cmd_rework(SimpleNamespace(task_id='t', request_id='legacy-1', reason='repair', prompt='do work'))
    assert len(sent) == 1 and 'docs/postmortem.md' in sent[0]
    assert 'delivery-check' in sent[0]


def test_existing_recovery_identities_survive_delivery_extension():
    from herdr.workflow_progress import recovery_identity
    # Recorded using the baseline implementation: legacy operations must not change identity on upgrade.
    assert recovery_identity({'workflow_id': 'wf', 'execution_id': 'gen'}, [{'task_id': 't', 'run_id': 'r'}]) == 'e8d72fa91fa7b1a201efd3232683b8286a424614f911653125fad0261e072e97'
