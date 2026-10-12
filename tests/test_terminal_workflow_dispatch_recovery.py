"""Regression tests for terminal workflows and completed node dispatches."""
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from herdr import dispatch_recovery, node_dispatch_store as nd, recovery_store
from herdr.state_store import get_state_store, reset_state_store

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def scene(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path / 'state.db'))
    monkeypatch.setenv('TASKS_FILE', str(tmp_path / 'tasks.json'))
    monkeypatch.setenv('WORKFLOWS_FILE', str(tmp_path / 'workflows.json'))
    reset_state_store()
    store = get_state_store(tmp_path / 'state.db')
    workflow = {
        'workflow_id': 'wf',
        'execution_id': 'execution-1',
        'created_at': 100,
        'status': 'running',
        'startup_ready': True,
        'config': {
            'nodes': [
                {'id': 'requirements', 'depends_on': []},
                {'id': 'plan', 'depends_on': ['requirements']},
                {'id': 'implementation', 'depends_on': ['plan']},
            ]
        },
    }
    store.save_workflow(workflow)
    clock = [1000.0]
    yield SimpleNamespace(store=store, clock=clock, tmp_path=tmp_path)
    reset_state_store()


def test_terminal_workflow_retires_unconfirmed_dispatch(scene):
    # Requirements task registered
    scene.store.save_task({
        'task_id': 'req-1', 'workflow_id': 'wf', 'node': 'requirements',
        'execution_id': 'execution-1', 'run_id': 'run-req-1', 'status': 'completed',
        'stage_verdict': 'pass',
    })
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=scene.clock[0])
    plan_op = nd.operation_for_node(scene.store.db_path, 'wf', 'plan')
    assert plan_op is not None

    # Simulate dispatch was started, then generation changed to waiting_human
    with recovery_store._transaction(scene.store.db_path) as conn:
        conn.execute(
            "UPDATE workflow_recovery_operations SET status='waiting_human', started=1, "
            "detail_json=? WHERE id=?",
            (json.dumps({'reason': 'dispatch_generation_changed'}), plan_op['id'])
        )

    # Now mark the workflow completed / delivered
    wf = scene.store.get_workflow('wf')
    wf['status'] = 'completed'
    wf['outcome'] = 'delivered'
    wf['completed_at'] = scene.clock[0] + 500
    scene.store.save_workflow(wf)

    # Reconcile should automatically supersede the dangling operation
    nd.reconcile_workflow(scene.store.db_path, 'wf', discover=False, now=scene.clock[0] + 600)

    history = [o for o in recovery_store.list_operations(scene.store.db_path, 'wf')
               if o['id'] == plan_op['id']]
    assert len(history) == 1
    assert history[0]['status'] == 'superseded'
    assert history[0]['detail']['reason'] == 'workflow_completed'

    # dispatch_recovery.list_operations should have no active recovery
    ops = dispatch_recovery.list_operations(scene.store.db_path, 'wf')
    target = next((o for o in ops if o['id'] == plan_op['id']), None)
    assert target is not None
    assert target['status'] == 'superseded'
    assert 'recovery' not in target


def test_node_tasks_pass_supersedes_stale_started_dispatch(scene):
    # Requirements task registered
    scene.store.save_task({
        'task_id': 'req-1', 'workflow_id': 'wf', 'node': 'requirements',
        'execution_id': 'execution-1', 'run_id': 'run-req-1', 'status': 'completed',
        'stage_verdict': 'pass',
    })
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=scene.clock[0])
    plan_op = nd.operation_for_node(scene.store.db_path, 'wf', 'plan')
    assert plan_op is not None

    # Simulate dispatch was started in old generation
    with recovery_store._transaction(scene.store.db_path) as conn:
        conn.execute(
            "UPDATE workflow_recovery_operations SET status='waiting_human', started=1, "
            "detail_json=? WHERE id=?",
            (json.dumps({'reason': 'dispatch_generation_changed'}), plan_op['id'])
        )

    # A second requirements task is added, causing generation change
    scene.store.save_task({
        'task_id': 'req-2', 'workflow_id': 'wf', 'node': 'requirements',
        'execution_id': 'execution-1', 'run_id': 'run-req-2', 'status': 'completed',
        'stage_verdict': 'pass',
    })

    # Meanwhile, a plan task was completed and passed
    scene.store.save_task({
        'task_id': 'plan-design', 'workflow_id': 'wf', 'node': 'plan',
        'execution_id': 'execution-1', 'run_id': 'run-plan', 'status': 'cleaned',
        'stage_verdict': 'pass',
    })

    # Reconcile while workflow is still running
    nd.reconcile_workflow(scene.store.db_path, 'wf', discover=False, now=scene.clock[0] + 100)

    history = [o for o in recovery_store.list_operations(scene.store.db_path, 'wf')
               if o['id'] == plan_op['id']]
    assert len(history) == 1
    assert history[0]['status'] == 'superseded'
    assert history[0]['detail']['reason'] == 'dispatch_existing_tasks'


def test_console_completed_workflow_no_active_recovery_and_completed_node_protected():
    node = shutil.which('node')
    if node is None:
        pytest.skip('node unavailable for Console runtime rendering')
    source = (ROOT / 'console/herdr_factory_console.py').read_text()
    
    renderer = source.split('function currentRecovery(nodeId){', 1)[1].split('function decideRecovery(', 1)[0]
    renderer = 'function currentRecovery(nodeId){' + renderer

    status_renderer = source.split('function flowNodeStatus(node,fallback){', 1)[1].split('function flowCardInner(', 1)[0]
    status_renderer = 'function flowNodeStatus(node,fallback){' + status_renderer

    operations = [
        {'id': 1, 'version': 1, 'status': 'waiting_human', 'started': 1,
         'payload': {'kind': 'node_dispatch', 'node_id': 'plan'}},
        {'id': 2, 'version': 1, 'status': 'resolved',
         'confirmation': {'task_ids': ['task-123']},
         'payload': {'kind': 'node_dispatch', 'node_id': 'plan'}},
    ]

    script = f"""
    const state = {{
        controllerActionsData: {{ recovery: {json.dumps(operations)} }},
        workflow: {{ workflow: {{ status: 'completed', outcome: 'delivered' }} }}
    }};
    function recoveryUnavailable() {{ return false; }}
    function esc(s) {{ return String(s); }}
    {renderer}
    {status_renderer}

    // 1. In completed workflow, currentRecovery & dispatchConfirmations return empty
    const recs = currentRecovery();
    if (recs.length !== 0) throw new Error('currentRecovery should be empty for completed workflow, got: ' + JSON.stringify(recs));
    const confs = dispatchConfirmations();
    if (confs.length !== 0) throw new Error('dispatchConfirmations should be empty for completed workflow, got: ' + JSON.stringify(confs));
    const confHtml = dispatchConfirmationHtml();
    if (confHtml !== '') throw new Error('dispatchConfirmationHtml should be empty for completed workflow, got: ' + confHtml);

    // 2. In running workflow, a node with status completed must suppress confirmation banner
    state.workflow.workflow.status = 'running';
    delete state.workflow.workflow.outcome;
    state.workflow.graph = {{ nodes: [{{ id: 'plan', status: 'completed' }}] }};
    const nodeStatus = flowNodeStatus({{ id: 'plan', status: 'completed' }}, '已完成');
    if (nodeStatus !== '已完成') throw new Error('Completed node status must not be overridden by recovery, got: ' + nodeStatus);
    const nodeConfs = dispatchConfirmations('plan');
    if (nodeConfs.length !== 0) throw new Error('dispatchConfirmations should be empty for completed node, got: ' + JSON.stringify(nodeConfs));

    // 3. In running workflow, a node with status working must retain confirmation banner
    state.workflow.graph = {{ nodes: [{{ id: 'plan', status: 'working' }}] }};
    const workingConfs = dispatchConfirmations('plan');
    if (workingConfs.length !== 1) throw new Error('dispatchConfirmations should return active confirmation for working node');

    console.log('OK');
    """
    result = subprocess.run([node, '-e', script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'OK' in result.stdout
