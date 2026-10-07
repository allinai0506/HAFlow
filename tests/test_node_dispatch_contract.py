"""A notification must retain responsibility until the real Task is registered."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from herdr import recovery_store
from herdr.state_store import get_state_store, reset_state_store

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def scene(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path / 'state.db'))
    monkeypatch.setenv('TASKS_FILE', str(tmp_path / 'tasks.json'))
    monkeypatch.setenv('WORKFLOWS_FILE', str(tmp_path / 'workflows.json'))
    monkeypatch.setenv('HERDR_COORDINATOR_INTAKE', '1')
    reset_state_store()
    store = get_state_store(tmp_path / 'state.db')
    workflow = {'workflow_id': 'wf', 'execution_id': 'execution-1', 'created_at': 100,
                'status': 'running', 'startup_ready': True, 'requirement': 'build a bounded feature',
                'config': {'nodes': [{'id': 'implementation', 'depends_on': []},
                                     {'id': 'test', 'depends_on': ['implementation']}]}}
    store.save_workflow(workflow)
    spec = importlib.util.spec_from_file_location('node_dispatch_controller', ROOT / 'services/herdr-controller.py')
    ctrl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ctrl)
    clock = [1000.0]
    monkeypatch.setattr(ctrl.time, 'time', lambda: clock[0])
    for name, function in {
        '_get_store': lambda: store, 'load_tasks': store.list_tasks,
        'workflow_closed': lambda _: False, 'project_for_workflow': lambda _: store.get_workflow('wf'),
        'workflow_config_for': lambda _: store.get_workflow('wf')['config'],
        'coordinator_pane_for_workflow': lambda _: 'external-coordinator',
        'coordinator_status': lambda _: 'idle', 'get_stage_policy': lambda _: {},
        'shared_docs_block': lambda *a, **kw: '', 'maybe_compact_coordinator': lambda *a, **kw: None,
        '_scheduler_frozen_candidate_identity': lambda *a: (None, None),
        '_scheduler_resolve_candidate_and_plan': lambda *a: (None, False),
        '_scheduler_join_gate_allows': lambda *a: True,
    }.items():
        monkeypatch.setattr(ctrl, name, function)
    ctrl.STAGE_STATE_FILE = str(tmp_path / 'stage-state.json')
    sent = []
    real_run = ctrl.subprocess.run
    def external_call(command, **kwargs):
        if len(command) > 1 and command[1] == 'supersede':
            import sys
            return real_run([sys.executable, str(ROOT / 'bin/herdr-task'), *command[1:]], **kwargs)
        assert command[:3] == ['herdr', 'agent', 'prompt']
        sent.append(command)
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(ctrl.subprocess, 'run', external_call)
    item = {'kind': 'stage_advance', 'workflow_id': 'wf', 'stage': 'start',
            'node_id': 'implementation', 'next_stage': 'implementation',
            'node': workflow['config']['nodes'][0]}
    yield SimpleNamespace(store=store, ctrl=ctrl, clock=clock, sent=sent, item=item)
    reset_state_store()


def dispatch_operations(scene):
    return [op for op in recovery_store.list_operations(scene.store.db_path, 'wf')
            if op['payload'].get('kind') == 'node_dispatch']


def test_successful_notification_without_task_has_deadline_and_responsibility(scene, capsys):
    scene.ctrl._handle_coordinator_item(scene.item)
    assert '[STAGE ADVANCED]' not in capsys.readouterr().out
    operations = dispatch_operations(scene)
    assert len(operations) == 1
    assert operations[0]['status'] == 'awaiting_result'
    assert operations[0]['detail']['deadline_at'] == 1900
    for _ in range(4):
        scene.clock[0] += 30
        scene.ctrl.check_workflow_stage_advance('wf')
        scene.ctrl.check_workflow_recovery('wf')
    assert len(scene.sent) == 1
    assert scene.store.list_tasks(workflow_id='wf') == []
    assert scene.ctrl.coordinator_queue.empty()
    assert len(dispatch_operations(scene)) == 1
    assert dispatch_operations(scene)[0]['detail']['deadline_at'] == 1900
    scene.clock[0] = 1900
    scene.ctrl.check_workflow_recovery('wf')
    op = dispatch_operations(scene)[0]
    assert op['status'] == 'waiting_human'
    assert op['detail']['reason'] == 'dispatch_task_missing'
    assert len(scene.sent) == 1


def test_zero_task_workflow_without_coordinator_is_not_an_unowned_running_state(scene, monkeypatch):
    monkeypatch.setattr(scene.ctrl, 'coordinator_pane_for_workflow', lambda _: None)
    scene.ctrl.check_workflow_stage_advance('wf')
    operations = dispatch_operations(scene)
    assert len(operations) == 1
    assert operations[0]['status'] == 'pending'
    assert operations[0]['detail']['reason'] == 'coordinator_missing'
    assert operations[0]['next_due_at'] == 1030


def started_operation(scene):
    from herdr import node_dispatch_store as nd
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    op = dispatch_operations(scene)[0]
    claimed = nd.claim(scene.store.db_path, op['id'], 'owner', now=1000)
    assert claimed is not None
    return nd.start(scene.store.db_path, op['id'], 'owner', now=1000)


def register_task(scene, op, task_id='work', role='worker', predecessor=None):
    from herdr.task_resources import begin_launch_intent
    predecessors = op['payload'].get('predecessors') or []
    supersedes = predecessor or (predecessors[0]['task_id'] if predecessors else None)
    intent = begin_launch_intent(scene.store, workflow_id='wf', node_id='implementation',
        task_id=task_id, role=role, dispatch_operation_id=op['id'], run_id='run-' + task_id,
        execution_id='execution-1', supersedes=supersedes,
        dispatch_round=int(scene.store.get_task(supersedes).get('dispatch_round') or 1) + 1 if supersedes else 1,
        now=scene.clock[0])['intent']
    task = {'task_id': task_id, 'workflow_id': 'wf', 'node': 'implementation', 'status': 'pending',
            'execution_id': 'execution-1', 'run_id': 'run-' + task_id,
            'dispatch_operation_id': op['id'], 'launch_intent_id': intent['intent_id'],
            'dispatch_role': role, 'dispatch_round': intent['dispatch_round']}
    if supersedes:
        task['supersedes'] = supersedes
    scene.store.save_task(task)
    return task


def test_registration_after_transport_resolves_without_waiting_for_intent_finish(scene):
    from herdr import node_dispatch_store as nd
    op = started_operation(scene)
    register_task(scene, op)
    result = nd.transport_finished(scene.store.db_path, op['id'], 'owner', reason='dispatch_awaiting_task', now=1001)
    assert result['status'] == 'resolved'
    assert result['detail']['registered_task_ids'] == ['work']
    assert result['detail']['registered_runs'] == {'work': 'run-work'}
    # Workflow completion is separate from dispatch completion.
    assert scene.store.get_workflow('wf')['status'] == 'running'
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1100)
    assert len(dispatch_operations(scene)) == 1


def test_required_inventory_cannot_resolve_on_partial_registration(scene):
    from herdr import node_dispatch_store as nd
    wf = scene.store.get_workflow('wf')
    wf['config']['nodes'][0]['required_task_ids'] = ['first', 'second']
    scene.store.save_workflow(wf)
    op = started_operation(scene)
    register_task(scene, op, 'first', 'first-role')
    nd.transport_finished(scene.store.db_path, op['id'], 'owner', reason='dispatch_awaiting_task', now=1000)
    assert dispatch_operations(scene)[0]['status'] == 'awaiting_result'
    register_task(scene, op, 'second', 'second-role')
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1030)
    assert dispatch_operations(scene)[0]['status'] == 'resolved'


@pytest.mark.parametrize('mutation', ['execution', 'config', 'closed', 'paused'])
def test_old_dispatch_cannot_authorize_new_launch(scene, mutation):
    op = started_operation(scene)
    wf = scene.store.get_workflow('wf')
    if mutation == 'execution':
        wf['execution_id'] = 'execution-2'
    elif mutation == 'config':
        wf['config']['nodes'][0]['purpose'] = 'changed scope'
    else:
        wf['status'] = mutation
    scene.store.save_workflow(wf)
    with pytest.raises(ValueError, match='authorize'):
        register_task(scene, op)
    assert scene.store.list_tasks(workflow_id='wf') == []
    assert scene.store.list_events(event_type='launch_intent') == []


def test_forged_task_without_matching_intent_is_rejected_atomically(scene):
    op = started_operation(scene)
    with pytest.raises(ValueError, match='matching durable launch intent'):
        scene.store.save_task({'task_id': 'forged', 'workflow_id': 'wf', 'node': 'implementation',
            'execution_id': 'execution-1', 'run_id': 'invented', 'dispatch_operation_id': op['id'],
            'launch_intent_id': 'invented'})
    assert scene.store.get_task('forged') is None


def test_task_cannot_be_rebound_or_have_run_identity_replaced(scene):
    op = started_operation(scene)
    task = register_task(scene, op)
    for changed in ({'dispatch_operation_id': None}, {'run_id': 'other'}, {'execution_id': 'other'}):
        with pytest.raises(ValueError, match='cannot'):
            scene.store.save_task({**task, **changed})
    assert scene.store.get_task('work')['run_id'] == 'run-work'


def test_two_independent_connections_can_claim_only_one_sender(scene):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from herdr import node_dispatch_store as nd
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    op = dispatch_operations(scene)[0]
    barrier = Barrier(2)
    def compete(owner):
        barrier.wait(timeout=5)
        return nd.claim(scene.store.db_path, op['id'], owner, now=1000)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(compete, owner) for owner in ('one', 'two')]
        claims = [f.result(timeout=10) for f in futures]
    assert sum(c is not None for c in claims) == 1
    assert dispatch_operations(scene)[0]['attempts'] == 1


def test_restart_after_started_lease_expiry_verifies_without_redispatch(scene):
    from herdr import node_dispatch_store as nd
    op = started_operation(scene)
    scene.clock[0] = 1661
    scene.ctrl.check_workflow_recovery('wf')
    assert dispatch_operations(scene)[0]['status'] == 'awaiting_result'
    assert nd.claim(scene.store.db_path, op['id'], 'new-controller', now=1661) is None
    register_task(scene, op)
    scene.clock[0] = 1691
    scene.ctrl.check_workflow_recovery('wf')
    assert dispatch_operations(scene)[0]['status'] == 'resolved'
    assert scene.sent == []


def test_restart_before_send_can_recover_expired_claim(scene):
    from herdr import node_dispatch_store as nd
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    op = dispatch_operations(scene)[0]
    nd.claim(scene.store.db_path, op['id'], 'dead-controller', now=1000)
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1661)
    recovered = nd.claim(scene.store.db_path, op['id'], 'new-controller', now=1661)
    assert recovered is not None
    assert not recovered['started']


@pytest.mark.parametrize('transport', ['timeout', 'nonzero'])
def test_unknown_transport_is_never_replayed(scene, monkeypatch, transport):
    import subprocess
    def unknown(command, **kwargs):
        scene.sent.append(command)
        if transport == 'timeout':
            raise subprocess.TimeoutExpired(command, 600)
        return SimpleNamespace(returncode=1, stdout='', stderr='external failure')
    monkeypatch.setattr(scene.ctrl.subprocess, 'run', unknown)
    scene.ctrl._handle_coordinator_item(scene.item)
    for tick in (1030, 1060, 1900):
        scene.clock[0] = tick
        scene.ctrl.check_workflow_stage_advance('wf')
        scene.ctrl.check_workflow_recovery('wf')
    assert len(scene.sent) == 1
    assert dispatch_operations(scene)[0]['status'] == 'waiting_human'


def test_legacy_notified_migrates_to_bounded_unknown_wait(scene):
    scene.ctrl.mark_stage_advance_notified('wf', 'implementation')
    scene.ctrl.check_workflow_stage_advance('wf')
    op = dispatch_operations(scene)[0]
    assert op['status'] == 'awaiting_result'
    assert op['detail']['origin'] == 'legacy_notified'
    assert op['detail']['deadline_at'] == 1900
    scene.clock[0] = 1900
    scene.ctrl.check_workflow_recovery('wf')
    assert dispatch_operations(scene)[0]['status'] == 'waiting_human'
    assert scene.sent == []


def test_missing_coordinator_exhausts_bounded_preflight_then_human_can_retry(scene, monkeypatch):
    monkeypatch.setattr(scene.ctrl, 'coordinator_pane_for_workflow', lambda _: None)
    for tick in (1000, 1030, 1060, 2000):
        scene.clock[0] = tick
        scene.ctrl.check_workflow_stage_advance('wf')
    op = dispatch_operations(scene)[0]
    assert op['status'] == 'waiting_human'
    assert op['attempts'] == 3
    retried = recovery_store.decide_operation(scene.store.db_path, op['id'], op['version'],
        'operator', 'retry', 'coordinator repaired', 2000)
    assert retried['status'] == 'pending'
    assert retried['attempts'] == 0


def test_zero_task_projection_exposes_wait_and_expired_human_decision(scene):
    from herdr.projection import detect_workflow_stalls
    scene.ctrl._handle_coordinator_item(scene.item)
    waiting = detect_workflow_stalls('wf', [], scene.store.get_workflow('wf'))
    assert waiting['dispatch']['responsible'] == 'coordinator'
    assert waiting['dispatch']['next_due_at'] == 1000
    assert waiting['dispatch']['deadline_at'] == 1900
    assert not waiting['is_stalled']
    scene.clock[0] = 1900
    scene.ctrl.check_workflow_recovery('wf')
    stalled = detect_workflow_stalls('wf', [], scene.store.get_workflow('wf'))
    assert stalled['is_stalled']
    assert stalled['stall_type'] == 'node_dispatch'
    assert stalled['dispatch']['decision_needed']


@pytest.mark.parametrize('replacement', [None, 'new-r2', 'replacement-work'])
def test_real_cli_launch_binds_intent_and_task_to_operation(tmp_path, monkeypatch, replacement):
    # Existing harness only substitutes native Pane/Worker/Agent resources.
    spec = importlib.util.spec_from_file_location('dispatch_cli_test_harness', ROOT / 'tests/test_recovery_entrypoints.py')
    entries = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entries)
    from herdr import node_dispatch_store as nd, state_db
    import subprocess
    real_run = subprocess.run
    cli, store, make_args, calls, _ = entries._real_committed_launch_scene(tmp_path, monkeypatch)
    native_transport = cli.subprocess.run
    monkeypatch.setattr(cli.subprocess, 'run', lambda cmd, **kw:
        real_run(cmd, **kw) if cmd[0] == 'ps' else native_transport(cmd, **kw))
    conn = state_db.get_db_connection(store.db_path)
    conn.execute("DELETE FROM tasks WHERE task_id='old'")
    conn.close()
    nd.reconcile_workflow(store.db_path, 'wf')
    op = recovery_store.list_operations(store.db_path, 'wf')[0]
    nd.claim(store.db_path, op['id'], 'controller')
    nd.start(store.db_path, op['id'], 'controller')
    args = make_args()
    args.supersedes = None
    args.dispatch_round = 1
    args.dispatch_operation_id = op['id']
    cli.launch_task(args)
    task = store.get_task('new')
    assert task['dispatch_operation_id'] == op['id']
    assert task['execution_id'] == 'execution'
    assert task['run_id'] == 'new-run'
    intents = store.list_events(event_type='launch_intent', desc=True, limit=1)
    assert intents[0]['payload']['dispatch_operation_id'] == op['id']
    assert intents[0]['payload']['intent_id'] == task['launch_intent_id']
    result = nd.transport_finished(store.db_path, op['id'], 'controller', reason='dispatch_awaiting_task')
    assert result['status'] == 'resolved'
    assert result['detail']['registered_runs'] == {'new': 'new-run'}
    if replacement:
        cli.supersede_task('new', allow_pending=True, reason='infrastructure failure')
        nd.reconcile_workflow(store.db_path, 'wf')
        retry = nd.operation_for_node(store.db_path, 'wf', 'implementation')
        assert retry['id'] != op['id']
        nd.claim(store.db_path, retry['id'], 'retry')
        nd.start(store.db_path, retry['id'], 'retry')
        args = make_args()
        args.task_id = replacement
        args.supersedes = 'new'
        args.dispatch_round = 2
        args.run_id = 'retry-run'
        args.dispatch_operation_id = retry['id']
        if replacement == 'replacement-work':
            from herdr.supervisor_delivery import DeliveryUnknown
            calls['fail_prompt'] = True
            with pytest.raises(DeliveryUnknown):
                cli._launch_task(args)
            calls['fail_prompt'] = False
        else:
            cli.launch_task(args)
        replacement_task = store.get_task(replacement)
        assert replacement_task['dispatch_operation_id'] == retry['id']
        assert replacement_task['supersedes'] == 'new'
        if replacement == 'replacement-work':
            assert store.get_task('new').get('superseded_by') is None
        else:
            assert store.get_task('new')['superseded_by'] == replacement
        for _ in range(4):
            nd.reconcile_workflow(store.db_path, 'wf')
        assert nd.operation_for_node(store.db_path, 'wf', 'implementation')['id'] == retry['id']
        assert nd.operation_for_node(store.db_path, 'wf', 'implementation')['status'] == 'resolved'

    assert calls['worker'] == calls['prompt'] == (2 if replacement else 1)
    # Repeating the CLI command uses its actual persistent identity and creates no new resources.
    cli.launch_task(args)
    assert calls['worker'] == calls['prompt'] == (2 if replacement else 1)
    reset_state_store()


def test_startup_not_ready_does_not_register_or_send(scene):
    wf = scene.store.get_workflow('wf')
    wf['startup_ready'] = False
    scene.store.save_workflow(wf)
    scene.ctrl.check_workflow_stage_advance('wf')
    assert dispatch_operations(scene) == []
    assert scene.sent == []


def test_started_unknown_does_not_permit_manual_retry_and_hold_has_fixed_expiry(scene):
    scene.ctrl._handle_coordinator_item(scene.item)
    scene.clock[0] = 1900
    scene.ctrl.check_workflow_recovery('wf')
    op = dispatch_operations(scene)[0]
    with pytest.raises(ValueError, match='verified'):
        recovery_store.decide_operation(scene.store.db_path, op['id'], op['version'],
            'human', 'retry', 'no visible task is not absence proof', 1900)
    held = recovery_store.decide_operation(scene.store.db_path, op['id'], op['version'],
        'human', 'hold', 'need requirements', 1900, until=2500)
    assert held['next_due_at'] == 2500
    scene.clock[0] = 2000
    scene.ctrl.check_workflow_recovery('wf')
    assert dispatch_operations(scene)[0]['status'] == 'waiting'
    scene.clock[0] = 2500
    scene.ctrl.check_workflow_recovery('wf')
    assert dispatch_operations(scene)[0]['status'] == 'waiting_human'
    assert len(scene.sent) == 1


def test_reopened_generation_does_not_inherit_old_task_suppression(scene):
    scene.store.save_task({'task_id': 'old', 'workflow_id': 'wf', 'node': 'implementation',
                          'execution_id': 'previous-execution', 'run_id': 'old-run', 'status': 'cleaned'})
    scene.ctrl.check_workflow_stage_advance('wf')
    assert len(dispatch_operations(scene)) == 1
    assert dispatch_operations(scene)[0]['status'] == 'running'
    assert scene.ctrl.coordinator_queue.get_nowait()['node_id'] == 'implementation'


def test_old_resolved_history_cannot_hide_current_dispatch_from_scan(scene):
    import json
    from herdr import state_db, node_dispatch_store as nd
    conn = state_db.get_db_connection(scene.store.db_path)
    conn.executemany('''INSERT INTO workflow_recovery_operations
        (identity_key,workflow_id,payload_json,status,created_at,updated_at)
        VALUES (?,'wf',?,'resolved',0,0)''', [(f'node_dispatch:old-{n}', json.dumps(
            {'kind': 'node_dispatch', 'node_id': 'implementation'})) for n in range(1001)])
    conn.close()
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    op = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    nd.claim(scene.store.db_path, op['id'], 'owner', now=1000)
    nd.start(scene.store.db_path, op['id'], 'owner', now=1000)
    register_task(scene, op)
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1030)
    current = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    assert current['status'] == 'resolved'


def test_queued_dispatch_survives_lost_in_memory_queue_with_bounded_lease(scene):
    from herdr import node_dispatch_store as nd
    scene.ctrl.check_workflow_stage_advance('wf')
    assert not scene.ctrl.coordinator_queue.empty()
    while not scene.ctrl.coordinator_queue.empty():
        scene.ctrl.coordinator_queue.get_nowait()
    # A lost queue cannot retain an unbounded queued latch. The DB reservation expires.
    scene.clock[0] = 1661
    scene.ctrl.check_workflow_stage_advance('wf')
    assert not scene.ctrl.coordinator_queue.empty()
    scene.ctrl._handle_coordinator_item(scene.ctrl.coordinator_queue.get_nowait())
    assert len(scene.sent) == 1
    assert nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')['started']


def test_stale_queue_cannot_send_deleted_node_via_untracked_fallback(scene):
    scene.ctrl.check_workflow_stage_advance('wf')
    item = scene.ctrl.coordinator_queue.get_nowait()
    wf = scene.store.get_workflow('wf')
    wf['config'] = {'nodes': [{'id': 'other', 'depends_on': []}]}
    scene.store.save_workflow(wf)
    scene.ctrl._handle_coordinator_item(item)
    assert scene.sent == []


def test_concurrent_real_controller_handlers_send_once(scene):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    barrier = Barrier(2)
    def deliver():
        barrier.wait(timeout=5)
        scene.ctrl._handle_coordinator_item(scene.item)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(deliver) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    assert len(scene.sent) == 1
    assert len(dispatch_operations(scene)) == 1


def test_console_renders_dispatch_wait_owner_deadline_and_decision(tmp_path):
    import json
    import shutil
    import subprocess
    node = shutil.which('node')
    if node is None:
        pytest.skip('node unavailable for Console runtime rendering')
    source = (ROOT / 'console/herdr_factory_console.py').read_text()
    renderer = source.split('function renderRecoveryPanel(wid){', 1)[1].split('function decideRecovery(', 1)[0]
    renderer = 'function renderRecoveryPanel(wid){' + renderer
    operations = [{'id': 1, 'version': 4, 'status': 'awaiting_result', 'started': 1,
                   'next_due_at': 1030, 'detail': {'deadline_at': 1900},
                   'payload': {'kind': 'node_dispatch', 'node_id': '<implementation>'}},
                  {'id': 2, 'version': 5, 'status': 'waiting_human', 'started': 1,
                   'detail': {'reason': 'dispatch_task_missing', 'decision_needed': '请核对需求与已有派发'},
                   'payload': {'kind': 'node_dispatch', 'node_id': 'implementation'}}]
    script = ('const state={controllerActionsData:{recovery:' + json.dumps(operations) + '}};\n'
              'function esc(x){return String(x).replaceAll("<","&lt;").replaceAll(">","&gt;");}\n'
              + renderer + '\nconsole.log(renderRecoveryPanel("wf"));')
    result = subprocess.run([node, '-e', script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert '等待总指挥建立任务' in result.stdout
    assert '负责人：总指挥' in result.stdout and '截止：' in result.stdout
    assert '&lt;implementation&gt;' in result.stdout
    assert '请核对需求与已有派发' in result.stdout
    assert '核对现有任务' in result.stdout


@pytest.mark.parametrize('damaged_payload', ['{', '', '{}', 'null', '[]', '"empty"'])
def test_corrupt_bound_task_can_be_repaired_but_cannot_erase_dispatch_identity(scene, damaged_payload):
    from herdr import state_db
    op = started_operation(scene)
    task = register_task(scene, op)
    conn = state_db.get_db_connection(scene.store.db_path)
    conn.execute("UPDATE tasks SET payload_json=? WHERE task_id='work'", (damaged_payload,))
    conn.close()
    with pytest.raises(ValueError, match='rebind'):
        scene.store.save_task({**task, 'dispatch_operation_id': None})
    scene.store.save_task({**task, 'goal': 'repair damaged payload'})
    assert scene.store.get_task('work')['dispatch_operation_id'] == op['id']
    assert scene.store.get_task('work')['goal'] == 'repair damaged payload'


@pytest.mark.parametrize('missing', [
    ('run_id', 'execution_id', 'launch_intent_id'),
    ('run_id',), ('execution_id',), ('launch_intent_id',),
])
def test_partial_bound_identity_allows_exact_repair_but_rejects_erasure(scene, missing):
    import json
    from herdr import state_db
    op = started_operation(scene)
    task = register_task(scene, op)
    damaged = {k: v for k, v in task.items() if k not in missing}
    conn = state_db.get_db_connection(scene.store.db_path)
    conn.execute('UPDATE tasks SET payload_json=? WHERE task_id=?', (json.dumps(damaged), 'work'))
    conn.close()
    with pytest.raises(ValueError, match='registration identity'):
        scene.store.save_task(damaged)
    scene.store.save_task({**task, 'goal': 'repair partial registration identity'})
    repaired = scene.store.get_task('work')
    assert repaired['goal'] == 'repair partial registration identity'
    for key in ('dispatch_operation_id', 'run_id', 'execution_id', 'launch_intent_id'):
        assert repaired[key] == task[key]


@pytest.mark.parametrize('conflict', ['dispatch_operation_id', 'run_id', 'execution_id', 'launch_intent_id'])
def test_partial_bound_identity_cannot_certify_conflicting_nonempty_fields(scene, conflict):
    import json
    from herdr import state_db
    op = started_operation(scene)
    task = register_task(scene, op)
    damaged = {k: v for k, v in task.items() if k not in ('run_id', 'execution_id', 'launch_intent_id')}
    damaged[conflict] = 999 if conflict == 'dispatch_operation_id' else 'conflicting-identity'
    conn = state_db.get_db_connection(scene.store.db_path)
    conn.execute('UPDATE tasks SET payload_json=? WHERE task_id=?', (json.dumps(damaged), 'work'))
    conn.close()
    # Neither incoming self-certification nor replacement may overwrite a known conflict.
    with pytest.raises(ValueError, match='conflict'):
        scene.store.save_task(damaged)
    with pytest.raises(ValueError, match='conflict'):
        scene.store.save_task(task)


def test_lookup_of_unregistered_workflow_has_no_dispatch_obligation(scene):
    from herdr import node_dispatch_store as nd
    assert nd.operation_for_node(scene.store.db_path, 'not-registered', 'implementation') is None
    assert scene.store.get_workflow('not-registered') is None
    assert scene.store.list_events(workflow_id='not-registered') == []


def test_resolved_intake_allows_distinct_replacement_lifecycle(scene):
    from herdr import node_dispatch_store as nd
    original = started_operation(scene)
    register_task(scene, original)
    nd.transport_finished(scene.store.db_path, original['id'], 'owner', reason='registered', now=1000)
    scene.store.transition_task('work', 'dispatched', 'dispatch', source='test')
    scene.store.transition_task('work', 'failed', 'agent_process_crash', source='test')
    assert scene.ctrl.recover_infra_failed_tasks('wf') is True
    assert scene.store.get_task('work')['replacement_pending'] is True
    for _ in range(4):
        scene.ctrl.check_workflow_stage_advance('wf')
        scene.ctrl.check_workflow_recovery('wf')
    assert scene.ctrl.coordinator_queue.qsize() == 1
    item = scene.ctrl.coordinator_queue.get_nowait()
    replacement = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    assert replacement['id'] != original['id']
    assert replacement['payload']['predecessors'] == [{'task_id': 'work', 'run_id': 'run-work'}]
    scene.ctrl._handle_coordinator_item(item)
    assert len(scene.sent) == 1
    assert '--supersedes work' in ' '.join(scene.sent[0])
    assert '--dispatch-operation-id ' + str(replacement['id']) in ' '.join(scene.sent[0])
    task = register_task(scene, replacement, 'work-r2')
    assert task['dispatch_operation_id'] == replacement['id']
    for _ in range(4):
        scene.ctrl.check_workflow_stage_advance('wf')
        scene.ctrl.check_workflow_recovery('wf')
    assert scene.ctrl.coordinator_queue.empty()
    operations = dispatch_operations(scene)
    assert len(operations) == 2
    assert all(op['status'] == 'resolved' for op in operations)
    assert operations[0]['detail']['registered_task_ids'] == ['work']
    assert operations[1]['detail']['registered_task_ids'] == ['work-r2']


@pytest.mark.parametrize('abandoned', [False, True])
def test_replacement_requires_superseded_pending_predecessor(scene, abandoned):
    from herdr import node_dispatch_store as nd
    original = started_operation(scene)
    register_task(scene, original)
    nd.transport_finished(scene.store.db_path, original['id'], 'owner', reason='registered', now=1000)
    scene.store.transition_task('work', 'failed', 'agent_process_crash')
    if abandoned:
        scene.store.transition_task('work', 'superseded', 'abandoned', metadata={'replacement_pending': False})
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    assert nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')['id'] == original['id']
    assert len(dispatch_operations(scene)) == 1


def test_replacement_rejects_old_dispatch_and_wrong_predecessor(scene):
    from herdr import node_dispatch_store as nd
    from herdr.task_resources import begin_launch_intent
    original = started_operation(scene)
    register_task(scene, original)
    nd.transport_finished(scene.store.db_path, original['id'], 'owner', reason='registered', now=1000)
    scene.store.transition_task('work', 'failed', 'agent_process_crash')
    scene.store.transition_task('work', 'superseded', 'recover')
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    replacement = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    nd.claim(scene.store.db_path, replacement['id'], 'replacement', now=1000)
    nd.start(scene.store.db_path, replacement['id'], 'replacement', now=1000)
    with pytest.raises(ValueError, match='authorize'):
        register_task(scene, original, 'stale', 'stale-role')
    with pytest.raises(ValueError, match='predecessor'):
        begin_launch_intent(scene.store, workflow_id='wf', node_id='implementation',
            task_id='unrelated', role='different-role', dispatch_operation_id=replacement['id'],
            run_id='unrelated-run', execution_id='execution-1', now=1000)


def test_replacement_inventory_requires_each_predecessor_and_can_recover_again(scene):
    from herdr import node_dispatch_store as nd
    original = started_operation(scene)
    register_task(scene, original, 'a', 'a-role')
    register_task(scene, original, 'b', 'b-role')
    nd.transport_finished(scene.store.db_path, original['id'], 'owner', reason='registered', now=1000)
    for task_id in ['a', 'b']:
        scene.store.transition_task(task_id, 'failed', 'crash')
        scene.store.transition_task(task_id, 'superseded', 'recover')
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    retry = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    nd.claim(scene.store.db_path, retry['id'], 'retry', now=1000)
    nd.start(scene.store.db_path, retry['id'], 'retry', now=1000)
    register_task(scene, retry, 'a-r2', 'a-role', predecessor='a')
    nd.transport_finished(scene.store.db_path, retry['id'], 'retry', reason='partial', now=1000)
    assert nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')['status'] == 'awaiting_result'
    register_task(scene, retry, 'b-r2', 'b-role', predecessor='b')
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    assert nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')['status'] == 'resolved'
    scene.store.transition_task('a-r2', 'failed', 'crash')
    scene.store.transition_task('a-r2', 'superseded', 'recover')
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    next_retry = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    assert next_retry['id'] not in {retry['id'], original['id']}
    assert next_retry['payload']['predecessors'] == [{'task_id': 'a-r2', 'run_id': 'run-a-r2'}]
    assert len(dispatch_operations(scene)) == 3


@pytest.mark.parametrize('mutation', ['abandoned', 'run', 'linked'])
def test_queued_replacement_revalidates_predecessor_before_send(scene, mutation):
    from herdr import node_dispatch_store as nd
    original = started_operation(scene)
    register_task(scene, original)
    nd.transport_finished(scene.store.db_path, original['id'], 'owner', reason='registered', now=1000)
    scene.store.transition_task('work', 'failed', 'crash')
    scene.store.transition_task('work', 'superseded', 'recover')
    scene.ctrl.check_workflow_stage_advance('wf')
    item = scene.ctrl.coordinator_queue.get_nowait()
    task = scene.store.get_task('work')
    task[{'abandoned': 'replacement_pending', 'run': 'run_id', 'linked': 'superseded_by'}[mutation]] = {
        'abandoned': False, 'run': 'other-run', 'linked': 'other-task'}[mutation]
    if mutation == 'run':
        # Simulate a concurrent damaged stored row; normal save preserves bound Run identity.
        import json
        from herdr import state_db
        conn = state_db.get_db_connection(scene.store.db_path)
        conn.execute('UPDATE tasks SET payload_json=? WHERE task_id=?', (json.dumps(task), 'work'))
        conn.close()
    else:
        scene.store.save_task(task)
    scene.ctrl._handle_coordinator_item(item)
    assert scene.sent == []
    assert nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')['status'] == 'superseded'


@pytest.mark.parametrize('restore', [False, True])
def test_invalid_unsent_replacement_preserves_remaining_obligations(scene, restore):
    from herdr import node_dispatch_store as nd
    original = started_operation(scene)
    register_task(scene, original, 'a', 'a-role')
    register_task(scene, original, 'b', 'b-role')
    nd.transport_finished(scene.store.db_path, original['id'], 'owner', reason='registered', now=1000)
    for task_id in ['a', 'b']:
        scene.store.transition_task(task_id, 'failed', 'crash')
        scene.store.transition_task(task_id, 'superseded', 'recover')
    scene.ctrl.check_workflow_stage_advance('wf')
    stale_item = scene.ctrl.coordinator_queue.get_nowait()
    cancelled_id = stale_item['dispatch_operation_id']
    scene.store.update_task_metadata('a', {'replacement_pending': False})
    scene.ctrl._handle_coordinator_item(stale_item)
    assert scene.sent == []
    assert nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')['status'] == 'superseded'
    if restore:
        scene.store.update_task_metadata('a', {'replacement_pending': True})
    for _ in range(4):
        scene.ctrl.check_workflow_stage_advance('wf')
        scene.ctrl.check_workflow_recovery('wf')
    assert scene.ctrl.coordinator_queue.qsize() == 1
    current_item = scene.ctrl.coordinator_queue.get_nowait()
    current = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    assert current['id'] != cancelled_id
    assert current['payload']['prior_operation_id'] == cancelled_id
    assert {p['task_id'] for p in current['payload']['predecessors']} == ({'a', 'b'} if restore else {'b'})
    scene.ctrl._handle_coordinator_item(stale_item)
    assert scene.sent == []
    scene.ctrl._handle_coordinator_item(current_item)
    assert len(scene.sent) == 1
    register_task(scene, current, 'b-r2', 'b-role', predecessor='b')
    if restore:
        register_task(scene, current, 'a-r2', 'a-role', predecessor='a')
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    assert nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')['status'] == 'resolved'


@pytest.mark.parametrize('supersedes', [None, 'unrelated'])
def test_bound_replacement_cannot_erase_or_change_predecessor(scene, supersedes):
    from herdr import node_dispatch_store as nd
    original = started_operation(scene)
    register_task(scene, original)
    nd.transport_finished(scene.store.db_path, original['id'], 'owner', reason='registered', now=1000)
    scene.store.transition_task('work', 'failed', 'crash')
    scene.store.transition_task('work', 'superseded', 'recover')
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    retry = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    nd.claim(scene.store.db_path, retry['id'], 'retry', now=1000)
    nd.start(scene.store.db_path, retry['id'], 'retry', now=1000)
    task = register_task(scene, retry, 'work-r2')
    task['supersedes'] = supersedes
    with pytest.raises(ValueError, match='registration identity'):
        scene.store.save_task(task)
    assert scene.store.get_task('work-r2')['supersedes'] == 'work'



def test_damaged_replacement_rejects_nonempty_predecessor_conflict(scene):
    from herdr import node_dispatch_store as nd, state_db
    import json
    original = started_operation(scene)
    register_task(scene, original)
    nd.transport_finished(scene.store.db_path, original['id'], 'owner', reason='registered', now=1000)
    scene.store.transition_task('work', 'failed', 'crash')
    scene.store.transition_task('work', 'superseded', 'recover')
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    retry = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    nd.claim(scene.store.db_path, retry['id'], 'retry', now=1000)
    nd.start(scene.store.db_path, retry['id'], 'retry', now=1000)
    task = register_task(scene, retry, 'work-r2')
    damaged = dict(task, supersedes='unrelated')
    damaged.pop('run_id')
    conn = state_db.get_db_connection(scene.store.db_path)
    conn.execute('UPDATE tasks SET payload_json=? WHERE task_id=?', (json.dumps(damaged), 'work-r2'))
    conn.close()
    with pytest.raises(ValueError, match='conflicts with durable'):
        scene.store.save_task(task)



def test_legacy_task_without_intake_history_has_persistent_replacement(scene):
    from herdr import node_dispatch_store as nd
    scene.store.save_task({'task_id': 'legacy', 'workflow_id': 'wf', 'node': 'implementation',
        'status': 'dispatched', 'execution_id': 'execution-1', 'run_id': 'legacy-run',
        'dispatch_role': 'worker', 'dispatch_round': 1})
    scene.store.transition_task('legacy', 'failed', 'agent_process_crash', source='test')
    assert scene.ctrl.recover_infra_failed_tasks('wf')
    for _ in range(4):
        scene.ctrl.check_workflow_stage_advance('wf')
        scene.ctrl.check_workflow_recovery('wf')
    assert scene.ctrl.coordinator_queue.qsize() == 1
    item = scene.ctrl.coordinator_queue.get_nowait()
    op = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    assert op and op['payload']['predecessors'] == [{'task_id': 'legacy', 'run_id': 'legacy-run'}]
    scene.ctrl._handle_coordinator_item(item)
    assert len(scene.sent) == 1
    register_task(scene, op, 'legacy-replacement')
    scene.ctrl.check_workflow_recovery('wf')
    assert nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')['status'] == 'resolved'
    assert scene.ctrl.coordinator_queue.empty()


def test_multiple_roots_discover_empty_node_despite_other_root_tasks(scene):
    from herdr import node_dispatch_store as nd
    workflow = scene.store.get_workflow('wf')
    workflow['config']['nodes'].insert(1, {'id': 'independent', 'depends_on': []})
    scene.store.save_workflow(workflow)
    scene.store.save_task({'task_id': 'existing', 'workflow_id': 'wf', 'node': 'implementation',
        'status': 'dispatched', 'execution_id': 'execution-1', 'run_id': 'existing-run'})
    scene.ctrl.check_workflow_stage_advance('wf')
    assert scene.ctrl.coordinator_queue.qsize() == 1
    item = scene.ctrl.coordinator_queue.get_nowait()
    assert item['node_id'] == 'independent'
    assert nd.operation_for_node(scene.store.db_path, 'wf', 'independent') is not None
    scene.ctrl._handle_coordinator_item(item)
    assert len(scene.sent) == 1


@pytest.mark.parametrize('reserved', [False, True])
def test_direct_mode_retires_unsent_intake_and_allows_completed_root(scene, monkeypatch, reserved):
    from herdr import node_dispatch_store as nd
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    op = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    if reserved:
        nd.claim(scene.store.db_path, op['id'], 'queued-owner', now=1000)
        scene.ctrl.mark_stage_advance_queued('wf', 'implementation')
    monkeypatch.setenv('HERDR_COORDINATOR_INTAKE', '0')
    scene.store.save_task({'task_id': 'direct', 'workflow_id': 'wf', 'node': 'implementation',
        'status': 'completed', 'stage_verdict': 'pass', 'execution_id': 'execution-1',
        'run_id': 'direct-run', 'integration_mode': 'none'})
    scene.clock[0] += 3 * 3600
    scene.ctrl.check_workflow_stage_advance('wf')
    assert nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')['status'] == 'superseded'
    assert scene.ctrl.coordinator_queue.qsize() == 1
    assert scene.ctrl.coordinator_queue.get_nowait()['node_id'] == 'test'


def test_registered_non_suffix_replacement_is_not_discovered_twice(scene):
    from herdr import node_dispatch_store as nd
    original = started_operation(scene)
    register_task(scene, original)
    nd.transport_finished(scene.store.db_path, original['id'], 'owner', reason='registered', now=1000)
    scene.store.transition_task('work', 'failed', 'crash')
    scene.store.transition_task('work', 'superseded', 'recover')
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    retry = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    nd.claim(scene.store.db_path, retry['id'], 'retry', now=1000)
    nd.start(scene.store.db_path, retry['id'], 'retry', now=1000)
    task = register_task(scene, retry, 'replacement-work')
    # Actual CLI registers supersedes before delivery and reverse-link success.
    assert scene.store.get_task('work').get('superseded_by') is None
    for _ in range(4):
        nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
        scene.ctrl.check_workflow_stage_advance('wf')
    assert nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')['id'] == retry['id']
    assert len(dispatch_operations(scene)) == 2
    assert scene.ctrl.coordinator_queue.empty()
    assert scene.store.get_task(task['task_id'])['supersedes'] == 'work'



def test_direct_mode_reenable_creates_new_unsent_intake_epoch(scene, monkeypatch):
    from herdr import node_dispatch_store as nd
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    original = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    monkeypatch.setenv('HERDR_COORDINATOR_INTAKE', '0')
    scene.ctrl.check_workflow_recovery('wf')
    assert nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')['status'] == 'superseded'
    monkeypatch.setenv('HERDR_COORDINATOR_INTAKE', '1')
    scene.ctrl.check_workflow_stage_advance('wf')
    current = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    assert current['id'] != original['id']
    assert current['payload']['prior_operation_id'] == original['id']
    scene.ctrl._handle_coordinator_item(scene.ctrl.coordinator_queue.get_nowait())
    assert len(scene.sent) == 1


def test_direct_mode_keeps_sent_unknown_intake_responsibility(scene, monkeypatch):
    from herdr import node_dispatch_store as nd
    scene.ctrl._handle_coordinator_item(scene.item)
    original = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    monkeypatch.setenv('HERDR_COORDINATOR_INTAKE', '0')
    for _ in range(4):
        scene.ctrl.check_workflow_stage_advance('wf')
        scene.ctrl.check_workflow_recovery('wf')
    scene.ctrl._handle_coordinator_item(scene.item)
    current = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    assert current['id'] == original['id']
    assert current['status'] == 'awaiting_result'
    assert current['detail']['deadline_at'] == original['detail']['deadline_at']
    assert scene.ctrl.coordinator_queue.empty()
    assert len(scene.sent) == 1



def test_durable_arbitrary_name_lineage_preserves_latest_recovery_and_legacy_groups():
    from herdr.direct_dispatch import lineage_redispatch_candidates
    tasks = [{'task_id': 'old-r9', 'status': 'superseded', 'replacement_pending': True},
             {'task_id': 'replacement-work', 'supersedes': 'old-r9', 'status': 'failed'}]
    assert lineage_redispatch_candidates(tasks) == []
    tasks[1].update(status='superseded', replacement_pending=True)
    assert lineage_redispatch_candidates(tasks) == [tasks[1]]
    tasks[1]['replacement_pending'] = False
    assert lineage_redispatch_candidates(tasks) == []
    tasks.append({'task_id': 'old-r10', 'status': 'dispatched'})
    tasks[1]['replacement_pending'] = True
    assert lineage_redispatch_candidates(tasks) == []


@pytest.mark.parametrize('reverse_link', [False, True])
def test_mixed_legacy_history_keeps_latest_explicit_recovery(scene, reverse_link):
    from herdr.direct_dispatch import lineage_redispatch_candidates
    from herdr import node_dispatch_store as nd
    tasks = [{'task_id': name, 'workflow_id': 'wf', 'node': 'implementation',
              'status': 'superseded', 'execution_id': 'execution-1', 'run_id': 'run-' + name,
              'replacement_pending': True} for name in ['job', 'job-r2', 'job-r3', 'new-name']]
    tasks[-1]['supersedes'] = 'job-r3'
    if reverse_link:
        tasks[1]['superseded_by'] = 'job-r3'
        tasks[1]['supersedes'] = 'job'
    assert lineage_redispatch_candidates(tasks) == [tasks[-1]]
    for task in tasks:
        scene.store.save_task(task)
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    op = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    assert op['payload']['predecessors'] == [{'task_id': 'new-name', 'run_id': 'run-new-name'}]


def test_direct_mode_does_not_bypass_manual_hold(scene, monkeypatch):
    from herdr import node_dispatch_store as nd
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    op = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    recovery_store.decide_operation(scene.store.db_path, op['id'], op['version'],
        operator='human', action='hold', reason='wait for decision', until=4600, now=1000)
    monkeypatch.setenv('HERDR_COORDINATOR_INTAKE', '0')
    for _ in range(4):
        scene.ctrl.check_workflow_stage_advance('wf')
        scene.ctrl.check_workflow_recovery('wf')
    current = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    assert current['status'] == 'waiting'
    assert current['next_due_at'] == 4600
    assert scene.ctrl.coordinator_queue.empty()
    scene.ctrl._handle_coordinator_item(scene.item)
    assert scene.sent == []


@pytest.mark.parametrize('reverse_link', [False, True])
def test_arbitrary_rename_followed_by_legacy_suffix_keeps_latest(scene, reverse_link):
    from herdr.direct_dispatch import lineage_redispatch_candidates
    from herdr import node_dispatch_store as nd
    tasks = [{'task_id': name, 'workflow_id': 'wf', 'node': 'implementation',
              'status': 'superseded', 'execution_id': 'execution-1', 'run_id': 'run-' + name,
              'replacement_pending': True} for name in ['job', 'job-r2', 'job-r3', 'new-name', 'new-name-r2']]
    tasks[-2]['supersedes'] = 'job-r3'
    if reverse_link:
        tasks[-2]['superseded_by'] = 'new-name-r2'
    assert lineage_redispatch_candidates(tasks) == [tasks[-1]]
    for task in tasks:
        scene.store.save_task(task)
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    assert nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')['payload']['predecessors'] == [
        {'task_id': 'new-name-r2', 'run_id': 'run-new-name-r2'}]


def test_explicit_link_overrides_legacy_suffix_cycle():
    from herdr.direct_dispatch import lineage_redispatch_candidates
    tasks = [{'task_id': name, 'status': 'superseded', 'replacement_pending': True}
             for name in ['job', 'job-r2', 'job-r3', 'job-r4', 'job-r5']]
    tasks[2]['supersedes'] = 'job-r5'
    from itertools import permutations
    for ordering in permutations(tasks):
        assert lineage_redispatch_candidates(ordering) == [tasks[2]]


@pytest.mark.parametrize('reserved', [False, True])
def test_pending_intake_adopts_arriving_legacy_task_before_send(scene, reserved):
    from herdr import node_dispatch_store as nd
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    original = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    if reserved:
        nd.claim(scene.store.db_path, original['id'], 'owner', now=1000)
    scene.store.save_task({'task_id': 'legacy', 'workflow_id': 'wf', 'node': 'implementation',
        'status': 'dispatched', 'execution_id': 'execution-1', 'run_id': 'legacy-run',
        'dispatch_role': 'worker', 'dispatch_round': 1})
    if reserved:
        assert nd.start(scene.store.db_path, original['id'], 'owner', now=1000) is None
    else:
        assert nd.claim(scene.store.db_path, original['id'], 'owner', now=1000) is None
    assert nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')['status'] == 'superseded'
    scene.store.transition_task('legacy', 'failed', 'agent_process_crash')
    assert scene.ctrl.recover_infra_failed_tasks('wf')
    scene.ctrl.check_workflow_stage_advance('wf')
    retry = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    assert retry['id'] != original['id']
    assert retry['payload']['predecessors'] == [{'task_id': 'legacy', 'run_id': 'legacy-run'}]
    assert scene.ctrl.coordinator_queue.qsize() == 1



def test_unsent_partial_legacy_inventory_keeps_remaining_responsibility(scene):
    from herdr import node_dispatch_store as nd
    workflow = scene.store.get_workflow('wf')
    workflow['config']['nodes'][0]['required_task_ids'] = ['a', 'b']
    scene.store.save_workflow(workflow)
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    original = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    scene.store.save_task({'task_id': 'a', 'workflow_id': 'wf', 'node': 'implementation',
        'status': 'dispatched', 'run_id': 'a-run', 'execution_id': 'execution-1'})
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    assert nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')['status'] == 'pending'
    scene.store.save_task({'task_id': 'b', 'workflow_id': 'wf', 'node': 'implementation',
        'status': 'dispatched', 'run_id': 'b-run', 'execution_id': 'execution-1'})
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    latest = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    assert latest['id'] == original['id']
    assert latest['status'] == 'superseded'
    assert latest['detail']['task_runs'] == {'a': 'a-run', 'b': 'b-run'}


def test_explicit_chain_wins_equal_rank_legacy_leaf():
    from itertools import permutations
    from herdr.task_lineage import lineage_redispatch_candidates
    tasks = [{'task_id': name, 'status': 'superseded', 'replacement_pending': True,
              'created_at': 1000}
             for name in ['job', 'job-r2', 'job-r3', 'job-r4', 'job-r5']]
    tasks[0]['supersedes'] = 'job-r2'
    tasks[1]['supersedes'] = 'job-r3'
    for ordering in permutations(tasks):
        assert lineage_redispatch_candidates(ordering) == [tasks[0]]


@pytest.mark.parametrize('reverse', [False, True])
def test_equal_weight_durable_branches_have_stable_tie_break(reverse):
    from itertools import permutations
    from herdr.task_lineage import lineage_redispatch_candidates
    tasks = [{'task_id': name, 'status': 'superseded', 'replacement_pending': True,
              'created_at': 1000} for name in ['job', 'job-r2', 'alias']]
    if reverse:
        tasks[0]['superseded_by'] = 'alias'
    else:
        tasks[2]['supersedes'] = 'job'
    tasks[1]['supersedes'] = 'job'
    # Equal graph rank, evidence depth and timestamp: ID is a stable fallback,
    # not evidence that either concurrent branch happened later.
    for ordering in permutations(tasks):
        assert lineage_redispatch_candidates(ordering) == [tasks[1]]
