"""Regression: cleaned deliveries must not hide missing planned work."""
import importlib
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from herdr import liveness


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


@pytest.fixture
def scene(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q')
    git(repo, 'config', 'user.email', 'test@example.invalid')
    git(repo, 'config', 'user.name', 'Test')
    (repo / 'base').write_text('base')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'base')
    git(repo, 'branch', 'baseline')
    (repo / 'provider').write_text('provider')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'delivery')
    delivery = git(repo, 'rev-parse', 'HEAD')
    wf = {'workflow_id': 'wf', 'status': 'running', 'project_root': str(repo),
          'base_branch': 'baseline', 'startup_ready': True}
    cfg = {'nodes': [{'id': 'implementation', 'depends_on': [],
                      'required_task_ids': ['t3', 't4b', 't7']}]}
    tasks = [{'workflow_id': 'wf', 'task_id': 't3', 'node': 'implementation',
              'status': 'cleaned', 'integration_mode': 'git',
              'integrated_commit': delivery, 'updated_at': 100.0}]
    return wf, cfg, tasks


def inspect(scene):
    from herdr.projects import inspect_continuation
    return inspect_continuation(*scene)


def test_cleaned_received_delivery_and_missing_tasks_remain_obligations(scene):
    result = inspect(scene)
    assert result['missing_task_ids'] == ['t4b', 't7']
    assert result['deliveries'][0]['adoption'] == 'not_adopted'
    assert result['deliveries'][0]['task_id'] == 't3'
    assert result['target_sha'] == git(scene[0]['project_root'], 'rev-parse', 'baseline')


def test_actual_merge_resolves_adoption_without_resolving_missing_work(scene):
    git(scene[0]['project_root'], 'branch', '-f', 'baseline', 'HEAD')
    result = inspect(scene)
    assert result['deliveries'][0]['adoption'] == 'adopted'
    assert result['missing_task_ids'] == ['t4b', 't7']


@pytest.mark.parametrize('status', ['paused', 'failed', 'completed', 'closing'])
def test_explicit_workflow_hold_never_recovers(scene, status):
    scene[0]['status'] = status
    assert inspect(scene) is None


@pytest.mark.parametrize('status', ['working', 'dispatched', 'blocked', 'paused', 'rework'])
def test_active_or_explicitly_blocked_task_prevents_recovery(scene, status):
    scene[2][0]['status'] = status
    assert inspect(scene) is None


def test_foreign_workflow_task_does_not_block_or_supply_evidence(scene):
    scene[2].append({'workflow_id': 'other', 'task_id': 't4b', 'status': 'working'})
    assert inspect(scene)['missing_task_ids'] == ['t4b', 't7']


def test_successor_in_progress_prevents_recovery(scene):
    scene[2].append({'workflow_id': 'wf', 'task_id': 'test', 'node': 'test', 'status': 'working'})
    assert inspect(scene) is None


def test_git_failure_is_unknown_not_adopted(scene):
    scene[0]['base_branch'] = 'missing-branch'
    result = inspect(scene)
    assert result['target_sha'] is None
    assert result['deliveries'][0]['adoption'] == 'unknown'


def test_replacement_is_followed_and_all_obligations_resolve(scene):
    wf, cfg, tasks = scene
    tasks[0].update(status='superseded', superseded_by='t3-r2')
    tasks.append(dict(tasks[0], task_id='t3-r2', status='cleaned', superseded_by=None))
    tasks.extend(dict(tasks[-1], task_id=tid) for tid in ['t4b', 't7'])
    git(wf['project_root'], 'branch', '-f', 'baseline', 'HEAD')
    assert inspect(scene) is None


def test_controller_persists_obligation_and_reloads_without_duplicate_prompt(scene, tmp_path):
    controller = importlib.import_module('services.herdr-controller')
    wf, cfg, tasks = scene
    ledger = tmp_path / 'attention.json'
    queued = []
    with patch.object(controller, '_attention_store', liveness.EpisodeStore(ledger)), \
         patch.object(controller, 'project_for_workflow', return_value=wf), \
         patch.object(controller, 'workflow_config_for', return_value=cfg), \
         patch.object(controller, 'load_tasks', return_value=tasks), \
         patch.object(controller.coordinator_queue, 'put', side_effect=queued.append):
        controller.check_workflow_continuation('wf', now=1000)
        assert len(queued) == 1
        assert queued[0]['kind'] == 'workflow_continuation'
        controller.check_workflow_continuation('wf', now=1001)
        assert len(queued) == 1
        # A new process sees the durable claim, not only an in-memory latch.
        with patch.object(controller, '_attention_store', liveness.EpisodeStore(ledger)):
            controller.check_workflow_continuation('wf', now=1002)
        assert len(queued) == 1
    ep = json.loads(ledger.read_text())['episodes']['wf:continuation']
    assert ep['missing_task_ids'] == ['t4b', 't7']
    assert ep['claims'] == 1
    assert ep['attempts'] == 0


def test_projection_displays_specific_obligations_even_with_superseded_tasks(scene):
    from herdr.projection import detect_workflow_stalls
    wf, cfg, tasks = scene
    tasks.append({'workflow_id': 'wf', 'task_id': 'old', 'status': 'superseded'})
    with patch('herdr.projects.workflow_config_for', return_value=cfg):
        result = detect_workflow_stalls('wf', tasks, workflow=wf)
    assert result['is_stalled']
    assert result['stall_type'] == 'workflow_continuation'
    assert 't4b' in result['message']
    assert 't3' in result['message']


def wiring(controller, scene, ledger, queued):
    from contextlib import ExitStack
    stack = ExitStack()
    wf, cfg, tasks = scene
    for name, value in [('project_for_workflow', wf), ('workflow_config_for', cfg), ('load_tasks', tasks)]:
        stack.enter_context(patch.object(controller, name, return_value=value))
    stack.enter_context(patch.object(controller, '_attention_store', liveness.EpisodeStore(ledger)))
    stack.enter_context(patch.object(controller.coordinator_queue, 'put', side_effect=queued.append))
    return stack


def test_prompt_ack_does_not_close_obligation_and_budget_escalates(scene, tmp_path):
    controller = importlib.import_module('services.herdr-controller')
    queued = []
    real_run = subprocess.run
    with wiring(controller, scene, tmp_path / 'attention.json', queued), \
         patch.object(controller, 'continuation_coordinator', return_value='test-coordinator'), \
         patch.object(controller.subprocess, 'run', wraps=subprocess.run) as run, \
         patch.object(controller, 'notify_attention') as notify:
        controller.check_workflow_continuation('wf', now=1000)
        # Replace only the external Herdr boundary; Git remains real.
        def external(cmd, **kwargs):
            if cmd[0] == 'herdr':
                return subprocess.CompletedProcess(cmd, 0, '', '')
            return real_run(cmd, **kwargs)
        run.side_effect = external
        controller._handle_coordinator_item(queued[0])
        assert controller.attention_get('wf:continuation')['attempts'] == 1
        controller._handle_coordinator_item(queued[0])
        assert controller.attention_get('wf:continuation')['attempts'] == 1
        controller.check_workflow_continuation('wf', now=1601)
        controller.check_workflow_continuation('wf', now=2202)
        controller.check_workflow_continuation('wf', now=2803)
        assert len(queued) == 2
        assert controller.attention_get('wf:continuation')['escalated']
        notify.assert_called_once()


def test_stale_queue_never_prompts_after_task_dispatch(scene, tmp_path):
    controller = importlib.import_module('services.herdr-controller')
    queued = []
    with wiring(controller, scene, tmp_path / 'attention.json', queued), \
         patch.object(controller, 'continuation_coordinator') as runtime:
        controller.check_workflow_continuation('wf', now=1000)
        scene[2].append({'workflow_id': 'wf', 'task_id': 't4b', 'status': 'dispatched'})
        controller._handle_coordinator_item(queued[0])
        runtime.assert_not_called()
        controller.check_workflow_continuation('wf', now=1001)
        assert controller.attention_get('wf:continuation') is None


def test_concurrent_processes_claim_one_durable_recovery(scene, tmp_path):
    import multiprocessing
    # Independent processes / EpisodeStore connections, with simultaneous reads.
    ctx = multiprocessing.get_context('fork')
    barrier = ctx.Barrier(2)
    output = ctx.Queue()
    ledger = tmp_path / 'attention.json'
    def claim():
        controller = importlib.import_module('services.herdr-controller')
        queued = []
        with wiring(controller, scene, ledger, queued):
            barrier.wait(timeout=5)
            controller.check_workflow_continuation('wf', now=1000)
            output.put(len(queued))
    workers = [ctx.Process(target=claim) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0
    assert sum(output.get(timeout=2) for _ in workers) == 1


def test_real_sqlite_controller_and_projection_chain(scene, tmp_path):
    from herdr.state_store import SQLiteStateStore
    from herdr import projects, projection
    controller = importlib.import_module('services.herdr-controller')
    wf, cfg, tasks = scene
    definition = tmp_path / 'workflow.json'
    definition.write_text(json.dumps(cfg))
    wf['workflow_file'] = str(definition)
    store = SQLiteStateStore(tmp_path / 'state.db', auto_migrate_json=False)
    store.save_workflow(wf)
    store.save_task(tasks[0])
    queued = []
    with patch.object(projects, '_get_store', return_value=store), \
         patch.object(controller, 'load_tasks', side_effect=lambda: store.list_tasks(workflow_id='wf')), \
         patch.object(controller, '_attention_store', liveness.EpisodeStore(tmp_path / 'attention.json')), \
         patch.object(controller.coordinator_queue, 'put', side_effect=queued.append):
        import time
        with patch('herdr.projection.time.time', return_value=time.time() + 1000):
            controller.check_workflow_continuation('wf', now=time.time())
            view = projection.detect_workflow_stalls('wf', store.list_tasks(workflow_id='wf'),
                                                     workflow=store.get_workflow('wf'))
        assert len(queued) == 1
        persisted = controller.attention_get('wf:continuation')
        assert persisted['missing_task_ids'] == ['t4b', 't7']
        assert view['continuation']['target_sha'] == persisted['target_sha']
        assert view['continuation']['deliveries'][0]['adoption'] == 'not_adopted'
        # No task state or production state is modified by the recovery check.
        assert store.get_task('t3')['status'] == 'cleaned'


def test_git_timeout_keeps_adoption_unknown(scene):
    from herdr.projects import inspect_continuation
    with patch('herdr.projects.subprocess.run', side_effect=subprocess.TimeoutExpired('git', 3)):
        result = inspect_continuation(*scene)
    assert result['deliveries'][0]['adoption'] == 'unknown'


def test_satisfied_first_node_does_not_hide_later_parallel_obligation(scene):
    wf, cfg, tasks = scene
    cfg['nodes'].insert(0, {'id': 'earlier', 'required_task_ids': ['a']})
    tasks.insert(0, {'workflow_id': 'wf', 'task_id': 'a', 'node': 'earlier',
                     'status': 'cleaned', 'integration_mode': 'none', 'updated_at': 100})
    assert inspect(scene)['missing_task_ids'] == ['t4b', 't7']


def test_failed_upgrade_notification_is_retried_after_reload(scene, tmp_path):
    controller = importlib.import_module('services.herdr-controller')
    ledger = tmp_path / 'attention.json'
    queued = []
    with wiring(controller, scene, ledger, queued), \
         patch.object(controller, 'notify_attention', side_effect=[False, True]) as notify:
        controller.check_workflow_continuation('wf', now=1000)
        # Simulate an already exhausted delivery budget.
        with controller._attention_store.transaction() as episodes:
            episodes['wf:continuation'].update(attempts=2, next_retry_at=1001)
        controller.check_workflow_continuation('wf', now=1002)
        with patch.object(controller, '_attention_store', liveness.EpisodeStore(ledger)):
            controller.check_workflow_continuation('wf', now=1603)
        assert notify.call_count == 2
        assert not controller.attention_get('wf:continuation')['notification_pending']


def test_busy_coordinator_does_not_spend_prompt_budget(scene, tmp_path):
    controller = importlib.import_module('services.herdr-controller')
    queued = []
    with wiring(controller, scene, tmp_path / 'attention.json', queued), \
         patch.object(controller, 'continuation_coordinator', return_value=None):
        controller.check_workflow_continuation('wf', now=1000)
        controller._handle_coordinator_item(queued[-1])
        controller.check_workflow_continuation('wf', now=1601)
        controller._handle_coordinator_item(queued[-1])
        assert controller.attention_get('wf:continuation')['attempts'] == 0


def test_wrong_coordinator_identity_never_receives_prompt(scene):
    controller = importlib.import_module('services.herdr-controller')
    scene[0]['project_id'] = 'p'
    wrong = json.dumps({'result': {'agent': {'name': 'other-coordinator', 'agent_status': 'idle'}}})
    with patch.object(controller.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, wrong, '')):
        assert controller.continuation_coordinator(scene[0]) is None


def test_coordinator_probe_timeout_returns_unknown(scene):
    controller = importlib.import_module('services.herdr-controller')
    scene[0]['project_id'] = 'p'
    with patch.object(controller.subprocess, 'run', side_effect=subprocess.TimeoutExpired('herdr', 5)):
        assert controller.continuation_coordinator(scene[0]) is None


def test_slow_continuation_inspection_does_not_block_stage_sweep(scene):
    import threading
    controller = importlib.import_module('services.herdr-controller')
    entered, release = threading.Event(), threading.Event()
    def slow(wid, now=None):
        entered.set()
        release.wait(timeout=5)
    try:
        with patch.object(controller, 'active_registered_workflows', return_value={'wf'}), \
             patch.object(controller, 'redeliver_pending_fix_loop'), \
             patch.object(controller, 'check_workflow_stage_advance') as advance, \
             patch.object(controller, 'check_workflow_continuation', side_effect=slow), \
             patch.object(controller, '_continuation_scan_at', 0):
            import time
            started = time.monotonic()
            controller.check_all_workflows_stage_advance()
            assert time.monotonic() - started < 0.5
            assert entered.wait(timeout=2)
            advance.assert_called_once_with('wf')
    finally:
        release.set()


def test_notifier_nonzero_exit_preserves_failed_receipt():
    controller = importlib.import_module('services.herdr-controller')
    notifier = importlib.import_module('services.herdr-notifier')
    with patch.object(notifier, 'notify', return_value=False), \
         patch.object(notifier, 'build_console_url', return_value='http://test.invalid'):
        assert controller.notify_attention('title', {'workflow_id': 'wf'}, 'message', 'reason') is False


@pytest.mark.parametrize('status', ['completed', 'committed'])
def test_git_finalization_in_flight_is_not_a_continuation_gap(scene, status):
    scene[2][0]['status'] = status
    assert inspect(scene) is None


def test_required_id_on_other_node_does_not_satisfy_plan(scene):
    scene[2].extend({'workflow_id': 'wf', 'task_id': tid, 'node': 'other',
                     'status': 'cleaned', 'integration_mode': 'none', 'updated_at': 100}
                    for tid in ['t4b', 't7'])
    assert inspect(scene)['missing_task_ids'] == ['t4b', 't7']
