"""Staged recovery contracts: evidence must fail closed and retries stay bounded."""
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.controller import attention, task_events
from services.controller import auto_acceptance as acceptance


@pytest.mark.parametrize('output,code,expected', [
    ('HERDR_BASELINE_RESULT={"changes": []}', 0, 'no_changes'),
    ('HERDR_BASELINE_RESULT={"changes": ["a"]}', 0, 'changed'),
    ('TASK_CHANGED:\n- a', 0, 'changed'),
    ('', 0, 'unavailable'),
    ('HERDR_BASELINE_RESULT=oops', 0, 'unavailable'),
    ('HERDR_BASELINE_RESULT=[]', 0, 'unavailable'),
    ('HERDR_BASELINE_RESULT={}', 0, 'unavailable'),
    ('TASK_CHANGED', 1, 'unavailable'),
])
def test_task_changes_evidence_matrix(output, code, expected):
    ports = SimpleNamespace(TASK_MANAGER='task', json=json,
        subprocess=SimpleNamespace(run=Mock(return_value=SimpleNamespace(returncode=code, stdout=output))))
    assert acceptance.task_changes_evidence(ports, {'task_id': 't'})[0] == expected
    assert ports.subprocess.run.call_count == 1


@pytest.mark.parametrize('exc', [OSError('unavailable'), subprocess.TimeoutExpired('task', 60)])
def test_verification_exception_is_unavailable(exc):
    ports = SimpleNamespace(TASK_MANAGER='task', json=json, subprocess=SimpleNamespace(run=Mock(side_effect=exc)))
    assert acceptance.task_changes_evidence(ports, {'task_id': 't'})[0] == 'unavailable'


def test_rejected_result_is_falsey():
    assert not acceptance.AutoAcceptResult(False, 'verification_unavailable')
    assert acceptance.AutoAcceptResult(True)


def test_status_history_canonical_entries_only_for_attempt_count():
    history = [dict(**{'from': a, 'to': b}, reason=r) for a, b, r in [
        ('agent_done', 'rework', 'auto_acceptance_rework:x:attempt:1'),
        ('rework', 'rework', 'rework_delivered'),
        ('agent_done', 'rework', 'manual'),
    ]]
    assert acceptance.auto_rework_attempts(None, {'status_history': history}) == 1


def test_escalated_latch_binds_episode_and_clears_on_new_version():
    ep = {'escalated': True, 'task_version': 1, 'run_id': 'r', 'execution_id': 'e'}
    ports = SimpleNamespace(attention_get=lambda key: ep, attention_clear=Mock())
    task = {'version': 1, 'run_id': 'r', 'execution_id': 'e'}
    assert attention.attention_escalated(ports, 't:done', task)
    assert not attention.attention_escalated(ports, 't:done', dict(task, version=2))
    ports.attention_clear.assert_called_once_with('t:done')


def route_ports(task, result):
    ep = {}
    ports = SimpleNamespace(
        get_task=lambda task_id: task.copy(),
        auto_rework_attempts=lambda t: acceptance.auto_rework_attempts(None, t),
        attention_get=lambda key: ep.copy(), attention_clear=Mock(),
        attention_note=lambda key, t, event, **kw: ep.update(kw),
        notify_attention=Mock(), verdict_defer_reason=lambda t: 'gate_signal_missing_or_conflict',
        TASK_MANAGER='task', time=SimpleNamespace(time=lambda: 100),
        liveness=SimpleNamespace(backoff_delay=lambda n: n * 60),
        subprocess=SimpleNamespace(run=Mock(return_value=SimpleNamespace(returncode=0))),
    )
    return ports, ep


def task_snapshot(**kw):
    return dict(task_id='t', workflow_id='wf', node='implementation', status='agent_done',
                version=1, run_id='r', execution_id='e', **kw)


def test_requirements_missing_sections_rework_same_task_and_pane():
    task = task_snapshot()
    result = acceptance.AutoAcceptResult(False, 'requirements_missing_sections', missing=('验收标准',))
    ports, ep = route_ports(task, result)
    task_events._route_staged_auto_acceptance_failure(ports, 't:done', task, result)
    cmd = ports.subprocess.run.call_args.args[0]
    assert cmd[:3] == ['task', 'rework', 't']
    assert cmd[cmd.index('--expected-version') + 1] == '1'
    assert '验收标准' in cmd[cmd.index('--prompt') + 1]
    assert not ep


@pytest.mark.parametrize('reason', ['verification_unavailable', 'gate_node'])
def test_gate_missing_verdict_bounded_retry_then_escalates(reason):
    task = task_snapshot()
    result = acceptance.AutoAcceptResult(False, reason)
    ports, ep = route_ports(task, result)
    for _ in range(3):
        task_events._route_staged_auto_acceptance_failure(ports, 't:done', task, result)
    assert ep['escalated'] is True
    assert ep['attempts'] == ep['limit'] == 3
    ports.subprocess.run.assert_not_called()


@pytest.mark.parametrize('reason', ['auto_accept_disabled', 'stage_verdict_blocked', 'review_role', '', 'unknown'])
def test_non_reworkable_escalates_without_rework(reason):
    task = task_snapshot()
    result = acceptance.AutoAcceptResult(False, reason)
    ports, ep = route_ports(task, result)
    task_events._route_staged_auto_acceptance_failure(ports, 't:done', task, result)
    assert ep['escalated'] is True
    ports.subprocess.run.assert_not_called()


def test_rework_cli_refuses_expected_snapshot_mismatch(tmp_path):
    from unittest.mock import patch

    from herdr.state_store import SQLiteStateStore
    from herdr.task_cli_rework import handle_rework
    store = SQLiteStateStore(str(tmp_path / 'state.db'))
    store.save_task(task_snapshot(pane_id='101'))
    args = SimpleNamespace(task_id='t', expected_status='agent_done', expected_version=999,
                           expected_run_id='r', expected_execution_id='e', request_id='req',
                           prompt='fix', reason='auto_acceptance_rework:x')
    transport = Mock()
    with patch('herdr.task_resources.owned_live_pane', return_value=(True, 'match')), pytest.raises(SystemExit) as exc:
        handle_rework(args, get_store=lambda: store, herdr=transport,
                      is_gate_task=lambda t: False, recover_unconsumed_gate_cleanup_id=lambda *a: None,
                      cleanup_gate_for_rework=lambda *a: None)
    assert exc.value.code == 2
    assert store.get_task('t')['status'] == 'agent_done'
    transport.assert_not_called()


@pytest.fixture
def real_chain(tmp_path, monkeypatch):
    """Actual Controller assembly, SQLite, CLI handler, kernel and attention store."""
    import argparse

    from herdr import kernel, liveness
    from herdr.state_store import SQLiteStateStore
    from herdr.task_cli_parser_execution import add_execution_parsers
    from herdr.task_cli_rework import handle_rework
    from tests.test_auto_acceptance import _load_controller
    store = SQLiteStateStore(str(tmp_path / 'state.db'))
    store.save_workflow({'workflow_id': 'wf', 'name': 'dev', 'status': 'running'})
    store.save_task(task_snapshot(pane_id='101', agent='codex'))
    ctrl = _load_controller('ctrl_recovery_real')
    monkeypatch.setattr(ctrl, '_get_store', lambda: store)
    monkeypatch.setattr(ctrl, 'get_task', store.get_task)
    monkeypatch.setattr(ctrl, '_attention_store', liveness.EpisodeStore(tmp_path / 'attention.json'))
    monkeypatch.setattr(ctrl, 'workflow_config_for', lambda wf: {'workflow_template': 'software-development-v1'})
    monkeypatch.setattr(ctrl, 'node_is_gate', lambda *a: False)
    monkeypatch.setattr(ctrl, 'try_auto_verdict', lambda *a: False)
    monkeypatch.setattr(ctrl, 'finalize_completed_task', Mock())
    monkeypatch.setattr(ctrl, 'notify_attention', Mock())
    monkeypatch.setattr('herdr.task_resources.owned_live_pane', lambda t: (True, 'match'))
    state = {'changes': [], 'sent': [], 'commands': []}
    def send(*args):
        state['sent'].append(args)
        return SimpleNamespace(returncode=0)
    parser = argparse.ArgumentParser()
    add_execution_parsers(parser.add_subparsers())
    def run(cmd, **kwargs):
        state['commands'].append(cmd)
        if cmd[1] == 'verify-baseline':
            return SimpleNamespace(returncode=0, stdout='HERDR_BASELINE_RESULT=' + json.dumps({'changes': state['changes']}))
        assert cmd[1] == 'rework'
        args = parser.parse_args(cmd[1:])
        try:
            handle_rework(args, get_store=lambda: store, herdr=send,
                          is_gate_task=lambda t: False, recover_unconsumed_gate_cleanup_id=lambda *a: None,
                          cleanup_gate_for_rework=lambda *a: None)
            return SimpleNamespace(returncode=0)
        except SystemExit as exc:
            return SimpleNamespace(returncode=exc.code)
        except RuntimeError:
            return SimpleNamespace(returncode=2)
    monkeypatch.setattr(ctrl.subprocess, 'run', run)
    def done():
        ctrl._handle_task_event({'task_id': 't', 'event_type': 'done', 'key': 't:done'})
    def complete_again():
        for status in ('working', 'agent_done'):
            assert kernel.transition_task('t', status, 'worker', store=store)['accepted']
    return ctrl, store, state, done, complete_again


def test_implementation_no_controlled_changes_rework_then_budget_exhausted(real_chain):
    ctrl, store, state, done, again = real_chain
    for _ in range(2):
        done()
        task = store.get_task('t')
        assert task['status'] == 'rework'
        assert task['pane_id'] == '101'
        assert task['run_id'] == 'r' and task['execution_id'] == 'e'
        assert task['rework_delivery'] == 'delivered'
        again()
    done()
    ep = ctrl.attention_get('t:done')
    assert ep['attempts'] == ep['limit'] == 2 and ep['escalated']
    assert ep['reason_code'] == 'implementation_no_controlled_changes'
    assert len(state['sent']) == 2
    assert store.get_task('t')['status'] == 'agent_done'
    from herdr.state_store import SQLiteStateStore
    assert ctrl.auto_rework_attempts(SQLiteStateStore(store.db_path).get_task('t')) == 2
    before = len(state['commands'])
    done()  # queued duplicate cannot rerun verification or dispatch
    assert len(state['commands']) == before
    assert not ctrl.redeliver_done_event(store.get_task('t'), now=100000000000)


def test_fixed_deliverable_reaccepted_without_second_rework(real_chain):
    ctrl, store, state, done, again = real_chain
    done()
    again()
    state['changes'] = ['implemented.py']
    done()
    task = store.get_task('t')
    assert task['status'] == 'completed'
    assert task['auto_accept_reason'] == 'controlled_changes'
    assert task['acceptance_mode'] == 'auto'
    assert not task.get('stage_verdict')
    assert len(state['sent']) == 1
    assert not ctrl.attention_get('t:done')


def test_success_path_unchanged_metadata_and_no_side_effects(real_chain, capsys):
    ctrl, store, state, done, _ = real_chain
    state['changes'] = ['a.py']
    done()
    assert '[AUTO ACCEPT] task=t node=implementation -> completed' in capsys.readouterr().out
    assert not state['sent']
    assert store.get_task('t')['acceptance_mode'] == 'auto'
    ctrl.finalize_completed_task.assert_called_once_with('t')
    assert len(state['commands']) == 1


def test_old_done_cannot_rework_new_execution(real_chain, monkeypatch):
    ctrl, store, state, done, _ = real_chain
    from herdr import kernel
    original = ctrl.try_auto_accept
    def racing(task_id):
        result = original(task_id)
        assert kernel.transition_task('t', 'rework', 'operator rework', store=store)['accepted']
        assert kernel.transition_task('t', 'working', 'new execution', store=store)['accepted']
        return result
    monkeypatch.setattr(ctrl, 'try_auto_accept', racing)
    done()
    assert store.get_task('t')['status'] == 'working'
    assert not state['sent'] and not ctrl.attention_get('t:done')


@pytest.mark.parametrize('field,value', [('expected_status', 'working'), ('expected_version', 999),
                                        ('expected_run_id', 'new'), ('expected_execution_id', 'new')])
def test_rework_cli_snapshot_fields_refused(real_chain, field, value):
    ctrl, store, state, _, _ = real_chain
    cmd = ['task', 'rework', 't', '--prompt', 'fix', '--' + field.replace('_', '-'), str(value)]
    assert ctrl.subprocess.run(cmd).returncode == 2
    assert store.get_task('t')['status'] == 'agent_done' and not state['sent']


def test_real_owned_live_pane_refusal_keeps_agent_done(real_chain, monkeypatch):
    ctrl, store, state, done, _ = real_chain
    monkeypatch.setattr('herdr.task_resources.owned_live_pane', lambda task: (False, 'unproven'))
    done()
    assert store.get_task('t')['status'] == 'agent_done'
    assert not state['sent']
    assert ctrl.attention_get('t:done')['reason_code'] == 'rework_dispatch_failed'


@pytest.mark.parametrize('phase', ['after_prepared', 'before_prompt', 'after_prompt'])
def test_receipt_v1_delivery_faults_do_not_create_new_attempt(real_chain, monkeypatch, phase):
    ctrl, store, state, done, _ = real_chain
    from herdr import liveness, supervisor_delivery
    store.update_task_metadata('t', {'completion_protocol': 'receipt-v1'})
    original = supervisor_delivery.deliver
    def fault(*args, **kwargs):
        def boundary(current):
            if current == phase:
                raise RuntimeError('injected delivery interruption')
        return original(*args, **kwargs, boundary=boundary)
    monkeypatch.setattr(supervisor_delivery, 'deliver', fault)
    done()
    task = store.get_task('t')
    assert task['status'] == 'rework' and task['rework_delivery'] == 'pending'
    assert ctrl.auto_rework_attempts(task) == 1
    assert ctrl.attention_get('t:done')['reason_code'] == 'rework_delivery_pending'
    assert ctrl.attention_get('t:done')['escalated']
    ctrl._attention_store = liveness.EpisodeStore(ctrl._attention_store.path)
    done()
    assert ctrl.auto_rework_attempts(store.get_task('t')) == 1
    assert len(state['sent']) == (1 if phase == 'after_prompt' else 0)
    assert not ctrl.redeliver_done_event(task, now=100000000000)


def test_receipt_v1_successful_rework(real_chain):
    ctrl, store, state, done, _ = real_chain
    store.update_task_metadata('t', {'completion_protocol': 'receipt-v1'})
    done()
    task = store.get_task('t')
    assert task['status'] == 'rework' and task['rework_delivery'] == 'delivered'
    assert ctrl.auto_rework_attempts(task) == 1 and len(state['sent']) == 1
    events = store.list_events(task_id='t')
    assert any(e['event_type'] == 'rework_transport_started' for e in events)


def test_concurrent_done_single_rework_and_single_prompt(tmp_path):
    """Independent processes exercise real launch locks and expected CAS (30s cap)."""
    import multiprocessing

    from herdr.state_store import SQLiteStateStore
    store = SQLiteStateStore(str(tmp_path / 'concurrent.db'))
    store.save_task(task_snapshot(pane_id='101'))
    snapshot = store.get_task('t')
    ctx = multiprocessing.get_context('fork')
    ready = ctx.Event()
    results = ctx.Queue()
    # fork retains the callable; each child opens its own SQLite connections.
    def worker():
        from unittest.mock import patch

        from herdr.task_cli_rework import handle_rework
        ready.wait(5)
        args = SimpleNamespace(task_id='t', prompt='fix', reason='auto_acceptance_rework:x:attempt:1',
                               request_id='same', expected_status='agent_done', expected_version=snapshot['version'],
                               expected_run_id='r', expected_execution_id='e')
        def send(*args):
            with (tmp_path / 'prompts').open('a') as stream:
                stream.write('sent\n')
            return SimpleNamespace(returncode=0)
        with patch('herdr.task_resources.owned_live_pane', return_value=(True, 'match')):
            try:
                handle_rework(args, get_store=lambda: SQLiteStateStore(store.db_path), herdr=send,
                              is_gate_task=lambda t: False, recover_unconsumed_gate_cleanup_id=lambda *a: None,
                              cleanup_gate_for_rework=lambda *a: None)
                results.put(0)
            except SystemExit as exc:
                results.put(exc.code)
    processes = [ctx.Process(target=worker) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        ready.set()
        for process in processes:
            process.join(10)
            assert not process.is_alive(), 'possible nested launch lock deadlock'
            assert process.exitcode == 0
        assert sorted(results.get(timeout=1) for _ in processes) == [0, 2]
        assert (tmp_path / 'prompts').read_text().splitlines() == ['sent']
        assert acceptance.auto_rework_attempts(None, store.get_task('t')) == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(2)


def test_latch_persistence_and_new_episode_recovery(real_chain):
    from herdr import kernel, liveness
    ctrl, store, state, done, _ = real_chain
    task = store.get_task('t')
    ctrl.attention_note('t:done', task, 'done', 'operator_disabled', escalated=True)
    ctrl._attention_store = liveness.EpisodeStore(ctrl._attention_store.path)
    done()
    assert not state['commands']
    assert kernel.transition_task('t', 'agent_done', 'fresh completion', store=store)['accepted']
    done()
    assert len(state['sent']) == 1
    assert not ctrl.attention_get('t:done')


@pytest.mark.parametrize('node,role', [('requirements', 'reviewer'), ('implementation', 'challenger')])
def test_staged_review_role_never_reworks(real_chain, node, role):
    ctrl, store, state, done, _ = real_chain
    store.save_task(dict(store.get_task('t'), node=node, dispatch_role=role))
    result = ctrl.try_auto_accept('t')
    assert not result and result.reason_code == 'review_role'
    done()
    assert not state['sent']


def test_plan_challenger_missing_sections_rework_prompt_is_deliverable_only(real_chain, tmp_path):
    _ctrl, store, state, done, _ = real_chain
    store.save_task(dict(store.get_task('t'), node='plan', dispatch_role='challenger', clone_path=str(tmp_path)))
    done()
    assert len(state['sent']) == 1
    prompt = state['sent'][0][3]
    assert 'plan_challenger_missing_sections' in prompt and '不得修改裁决' in prompt


@pytest.mark.parametrize('node,filename,content', [
    ('requirements', 'requirements.md', '用户目标 范围 非目标 验收标准 AC-1 验证计划 交付上限 workspace\n' + '明确业务边界及可复核的交付内容。' * 12),
    ('plan', 'plan.md', '# 实现计划\n任务拆分 Task 1\n文件范围 Modify: a.py\n验证命令 pytest -q\n' + '在授权范围完成任务并验证。' * 12),
])
def test_real_deliverable_repair_and_reaccept(real_chain, tmp_path, node, filename, content):
    _ctrl, store, state, done, again = real_chain
    store.save_task(dict(store.get_task('t'), node=node, clone_path=str(tmp_path)))
    done()
    assert store.get_task('t')['status'] == 'rework'
    (tmp_path / filename).write_text(content)
    again()
    done()
    assert store.get_task('t')['status'] == 'completed'
    assert len(state['sent']) == 1


@pytest.mark.parametrize('output,returncode', [('', 0), ('HERDR_BASELINE_RESULT=bad', 0), ('TASK_CHANGED', 1)])
def test_verification_unavailable_never_completes_or_reworks(real_chain, monkeypatch, output, returncode):
    ctrl, store, state, done, _ = real_chain
    monkeypatch.setattr(ctrl.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=returncode, stdout=output))
    for _ in range(3):
        done()
    assert store.get_task('t')['status'] == 'agent_done'
    assert not state['sent']
    ep = ctrl.attention_get('t:done')
    assert ep['escalated'] and ep['attempts'] == 3 and ep['reason_code'] == 'verification_unavailable'
