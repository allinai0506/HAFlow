"""Regression evidence for the 2026-10-20 handoff, using isolated SQLite."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from herdr import evaluator, kernel, state_db
from herdr.state_store import SQLiteStateStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERDR_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("WORKFLOWS_FILE", str(tmp_path / "workflows.json"))
    monkeypatch.setenv("TASKS_FILE", str(tmp_path / "tasks.json"))
    monkeypatch.setenv("HERDR_WORKFLOW_DOCS_DIR", str(tmp_path / "docs"))
    result = SQLiteStateStore(tmp_path / 'state.db')
    result.save_workflow({'workflow_id': 'wf', 'execution_id': 'exec', 'status': 'running',
                          'config': {'nodes': [{'id': 'test', 'depends_on': []},
                                               {'id': 'wrapup', 'depends_on': ['test']}]}})
    result.save_task({'task_id': 't', 'workflow_id': 'wf', 'execution_id': 'exec',
                     'run_id': 'run', 'node': 'test', 'status': 'completed',
                     'stage_verdict': 'blocked', 'completion_protocol': 'receipt-v1'})
    return result


@pytest.mark.parametrize('entry', ['kernel-force', 'db-force', 'bypass'])
def test_receipt_gate_refuses_verdict_only_override_without_writes(store, entry):
    before = (store.get_task('t'), store.get_workflow('wf'), store.list_events(workflow_id='wf'))
    with pytest.raises(ValueError, match='receipt-v1.*acceptance-record'):
        if entry == 'kernel-force':
            kernel.force_pass_gate('wf', 'test', store=store)
        elif entry == 'db-force':
            state_db.force_pass_gate('wf', 'test', db_path=store.db_path)
        else:
            kernel.bypass_task_gate('t', 'audited', store=store)
    assert before == (store.get_task('t'), store.get_workflow('wf'), store.list_events(workflow_id='wf'))


@pytest.mark.parametrize('output,code', [('internal crash', 2), ('error: invalid option', 2), ('', 1)])
def test_unparsed_lint_is_unavailable_even_with_large_baseline(output, code):
    metrics = evaluator.calculate_metrics('1 passed', 0, output, code, baseline_lint_errors=100)
    assert metrics.details['lint_status'] == 'unavailable'
    assert not evaluator.is_converged(metrics)
    assert metrics.composite_score < 100


def test_ruff_structured_diagnostics_counted():
    output = json.dumps([{'code': 'F401', 'message': 'unused import',
                          'filename': '/repo/a.py', 'location': {'row': 1, 'column': 1}}] * 3)
    assert evaluator.parse_lint_output(output, 1) == 3


def test_rebaseline_reason_history_and_sha(tmp_path):
    import subprocess
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subprocess.run(['git', '-C', str(tmp_path), '-c', 'user.name=T', '-c', 'user.email=t@t',
                    '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-qm', 'base'], check=True)
    (tmp_path / 'dirty').touch()
    command = "if [ -f dirty ]; then printf 'Found 7 errors.'; exit 1; fi"
    loop = evaluator.init_loop(tmp_path, 'fixture', test_cmd='true', lint_cmd=command, capture_baseline=True)
    (tmp_path / 'dirty').unlink()
    old = json.loads((loop / 'BASELINE_LINT.json').read_text())
    evaluator.rebaseline_lint(loop, command, tmp_path, reason='authorized intake completed')
    current = json.loads((loop / 'BASELINE_LINT.json').read_text())
    assert current['lint_errors'] == 0
    assert len(current['baseline_of_sha']) == 40
    assert current['history'] == [old]
    assert current['reason'] == 'authorized intake completed'
    before = (loop / 'BASELINE_LINT.json').read_bytes()
    with pytest.raises(ValueError):
        evaluator.rebaseline_lint(loop, command, tmp_path, reason='')
    assert (loop / 'BASELINE_LINT.json').read_bytes() == before


def test_business_wait_escalates_once_and_resolves(store):
    from herdr import gate_wait, recovery_store
    for now in (10, 40):
        op = gate_wait.record_wait(store.db_path, 'wf', 'wrapup', ['t'], now=now)
        assert op['status'] == 'observing'
    op = gate_wait.record_wait(store.db_path, 'wf', 'wrapup', ['t'], now=70)
    assert op['status'] == 'waiting_human'
    assert 'acceptance-record' in op['detail']['decision_needed']
    for now in (100, 130):
        gate_wait.record_wait(store.db_path, 'wf', 'wrapup', ['t'], now=now)
        recovery_store.reconcile(store.db_path, 'wf', now=now)
    assert len(store.list_events(workflow_id='wf', event_type='business_gate_stalled')) == 1
    gate_wait.record_wait(store.db_path, 'wf', 'wrapup', [], now=160)
    assert next(op for op in recovery_store.list_operations(store.db_path, 'wf')
                if op['payload'].get('kind') == 'business_gate_wait')['status'] == 'resolved'


def test_rejected_launch_is_actionable_and_redispatchable(store):
    from herdr.task_cli_launch import record_router_failure_task
    from herdr.task_lineage import lineage_redispatch_candidates
    args = SimpleNamespace(task_id='refused', workflow_id='wf', run_id='r2')
    record_router_failure_task(args, 'test', 'stale candidate', get_store=lambda: store,
                               new_run_id=lambda: 'r2', now=lambda: 1,
                               release_agent_reservation=lambda _: None,
                               reason='baseline_stale', remediation='rebase current candidate')
    task = store.get_task('refused')
    assert 'stale candidate' in task['blocker']
    assert 'rebase current candidate' in task['blocker']
    assert task['launch_not_started'] is True
    assert task['status'] == 'superseded'
    assert lineage_redispatch_candidates([task]) == [task]


def test_project_create_unavailable_workspace_is_503_without_project(tmp_path, monkeypatch):
    import contextlib
    import importlib.util
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    from pathlib import Path

    from console import herdr_factory_console as console
    from herdr import projects
    spec = importlib.util.spec_from_file_location('handoff_sandbox', Path(__file__).parent / 'support/simple_workbench_sandbox.py')
    sandbox = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sandbox)
    with contextlib.ExitStack() as stack:
        sandbox.configure(tmp_path, stack)
        def unavailable(command):
            raise RuntimeError('cli:workspace:create server_not_running')
        monkeypatch.setattr(projects, '_run_json', unavailable)
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), console.Handler)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        try:
            request = urllib.request.Request(f'http://127.0.0.1:{httpd.server_port}/api/project/create',
                data=json.dumps({'project_root': str(tmp_path), 'template': 'business-task-v1'}).encode(),
                headers={'Content-Type': 'application/json', 'X-HAFlow-Console-Request': '1'})
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=5)
            assert error.value.code == 503
            payload = json.loads(error.value.read())
            assert 'server_not_running' in payload['error']
            assert 'retry' in payload['error']
            assert not (tmp_path / 'projects.json').exists()
        finally:
            httpd.shutdown()
            httpd.server_close()
            worker.join(timeout=5)


def test_fix_loop_human_wait_has_explicit_replacement_guidance(store):
    from herdr import recovery_store
    task = store.get_task('t')
    store.save_task({**task, 'status': 'cleaned'})
    recovery_store.reconcile(store.db_path, 'wf', now=10)
    ops = recovery_store.list_operations(store.db_path, 'wf')
    op = next(op for op in ops if op['payload'].get('kind') == 'fix_loop')
    assert op['status'] == 'waiting_human'
    assert op['detail']['target_requires_explicit_replacement'] is True
    assert op['detail']['replacement']['operation_type'] == 'fix_loop'
    assert 'recovery-decide' in op['detail']['decision_needed']


def test_reconcile_reports_completed_close_receipt_with_paused_workflow(store):
    from herdr.reconciliation import reconcile_workflow_state
    from herdr.workflow_close import workflow_close_claim
    with workflow_close_claim(store, 'wf') as claim:
        claim.complete({'workflow_id': 'wf'})
    store.save_workflow({**store.get_workflow('wf'), 'status': 'paused'})
    result = reconcile_workflow_state('wf', store=store)
    assert result['status'] == 'paused'
    assert result['inconsistencies'][0]['reason'] == 'close_receipt_status_mismatch'
    assert result['inconsistencies'][0]['action_required']


def test_reconcile_reads_legacy_close_receipt_without_generation_column(store):
    from herdr.reconciliation import reconcile_workflow_state

    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            'CREATE TABLE workflow_close_operations ('
            'workflow_id TEXT PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL, '
            'state TEXT NOT NULL, options TEXT NOT NULL, receipt TEXT)'
        )
        conn.execute(
            'INSERT INTO workflow_close_operations VALUES (?,?,?,?,?)',
            ('wf', 'legacy-close', 'completed', '{}', '{"workflow_id":"wf"}'),
        )
    store.save_workflow({**store.get_workflow('wf'), 'status': 'paused'})

    result = reconcile_workflow_state('wf', store=store)

    assert result['status'] == 'paused'
    assert result['inconsistencies'][0]['operation_id'] == 'legacy-close'


def test_controller_join_wait_persists_stall_without_acceptance(store, monkeypatch):
    from herdr import gate_wait
    from services.controller.join_gates import _scheduler_join_gate_allows
    store.save_workflow({**store.get_workflow('wf'), 'config': {'nodes': [
        {'id': 'test'}, {'id': 'wrapup', 'depends_on': ['test']}]}})
    ports = SimpleNamespace(_get_store=lambda: store)
    clock = [10]
    monkeypatch.setattr(gate_wait.time, 'time', lambda: clock[0])
    for now in (10, 40, 70, 100):
        clock[0] = now
        assert not _scheduler_join_gate_allows(ports, 'wf', {'id': 'wrapup'}, store.list_tasks(workflow_id='wf'))
    assert len(store.list_events(workflow_id='wf', event_type='business_gate_stalled')) == 1
    assert store.list_events(workflow_id='wf', event_type='business_acceptance_recorded') == []


def test_preflight_refusal_aborts_real_intent_and_registers_replacement(store):
    from herdr import node_dispatch_store, recovery_store, task_resources
    from herdr.task_cli_launch import record_router_failure_task
    store.save_workflow({'workflow_id': 'new', 'execution_id': 'exec2', 'status': 'running',
                         'config': {'nodes': [{'id': 'implementation'}]}})
    intent = task_resources.begin_launch_intent(store, workflow_id='new', node_id='implementation',
        task_id='refused', run_id='r', execution_id='exec2', now=10)['intent']
    args = SimpleNamespace(task_id='refused', workflow_id='new', run_id='r', _launch_intent=intent)
    record_router_failure_task(args, 'implementation', 'stale', get_store=lambda: store,
        new_run_id=lambda: 'r', now=lambda: 11, release_agent_reservation=lambda _: None,
        reason='baseline_stale', remediation='rebase')
    assert task_resources._latest_intent(store, intent['key'])['phase'] == 'resources_absent'
    node_dispatch_store.reconcile_workflow(store.db_path, 'new', now=20)
    op = next(op for op in recovery_store.list_operations(store.db_path, 'new')
              if op['payload'].get('kind') == 'node_dispatch')
    assert op['status'] == 'pending'
    assert op['payload']['predecessors'] == [{'task_id': 'refused', 'run_id': 'r'}]


def test_checkpoint_epoch_budget_is_independent(store, monkeypatch):
    from herdr import task_checkpoint as checkpoint
    monkeypatch.setattr(checkpoint, 'MAX_SEGMENTS', 1)
    store.save_task({**store.get_task('t'), 'completion_epoch': 'one'})
    checkpoint.publish_task_checkpoint('t', 'run', 'one', 'first', 1, store=store)
    store.save_task({**store.get_task('t'), 'completion_epoch': 'two'})
    second = checkpoint.publish_task_checkpoint('t', 'run', 'two', 'second', 1, store=store)
    assert checkpoint.read_task_checkpoints('t', 'run', 'two', store=store)['segments'] == [second]


def test_close_cannot_issue_completed_receipt_before_workflow_completion(store):
    from herdr.task_cli_workflow_close import close_workflow
    from herdr.workflow_close import workflow_close_claim
    with pytest.raises(ValueError, match='workflow.*completed'):
        close_workflow('wf', abandon=True, get_store=lambda: store,
            workflow_close_claim=workflow_close_claim,
            business_gate_blockers=lambda *args: [], validate_delivery_inventory=lambda *args: None,
            close_workflow_owned=lambda *args, **kwargs: {'workflow_id': 'wf'},
            build_delivery_report=lambda *args: {})
    conn = state_db.get_readonly_db_connection(store.db_path)
    try:
        assert conn.execute("SELECT count(*) FROM workflow_close_operations WHERE state='completed'").fetchone()[0] == 0
    finally:
        conn.close()


def test_initial_capture_identifies_baseline_commit(tmp_path):
    import subprocess
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subprocess.run(['git', '-C', str(tmp_path), '-c', 'user.name=T', '-c', 'user.email=t@t',
                    '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-qm', 'base'], check=True)
    loop = evaluator.init_loop(tmp_path, 'baseline identity', test_cmd='true', lint_cmd="printf 'Found 2 errors.'; exit 1", capture_baseline=True)
    assert len(json.loads((loop / 'BASELINE_LINT.json').read_text())['baseline_of_sha']) == 40


@pytest.mark.parametrize('output', ['Found 0 errors.', '0 problems (0 errors, 0 warnings)'])
def test_nonzero_exit_without_diagnostics_remains_unavailable(output):
    metrics = evaluator.calculate_metrics('1 passed', 0, output, 2, baseline_lint_errors=100)
    assert not evaluator.is_converged(metrics)
    assert metrics.details['lint_status'] == 'unavailable'


@pytest.mark.parametrize('change', ['completed', 'execution', 'removed-node'])
def test_obsolete_gate_wait_is_superseded_during_reconcile(store, change):
    from herdr import gate_wait, recovery_store
    store.save_workflow({**store.get_workflow('wf'), 'config': {'nodes': [
        {'id': 'test'}, {'id': 'wrapup', 'depends_on': ['test']}]}})
    for now in (10, 40, 70):
        op = gate_wait.record_wait(store.db_path, 'wf', 'wrapup', ['t'], now=now)
    workflow = store.get_workflow('wf')
    if change == 'completed':
        workflow['status'] = 'completed'
    elif change == 'execution':
        workflow['execution_id'] = 'new-execution'
    else:
        workflow['config']['nodes'] = [{'id': 'test'}]
    store.save_workflow(workflow)
    recovery_store.reconcile(store.db_path, 'wf', now=100)
    assert next(item for item in recovery_store.list_operations(store.db_path, 'wf')
                if item['id'] == op['id'])['status'] == 'superseded'


def test_started_fix_loop_finish_also_exposes_replacement_guidance(store):
    from herdr import recovery_store
    with recovery_store._transaction(store.db_path) as conn:
        opid = conn.execute('''INSERT INTO workflow_recovery_operations
            (identity_key,workflow_id,payload_json,status,owner,lease_until,started,detail_json,created_at,updated_at)
            VALUES ('started-fix','wf',?,'running','owner',100,1,'{}',10,10)''',
            (json.dumps({'kind': 'fix_loop', 'retry_node': 'implementation', 'affected_task_ids': ['impl']}),)).lastrowid
    op = recovery_store.finish_operation(store.db_path, opid, 'owner', 'waiting_human',
                                         {'reason': 'replacement_missing'}, 20)
    assert op['detail']['target_requires_explicit_replacement'] is True
    assert op['detail']['replacement']['affected_task_ids'] == ['impl']


def test_recurring_blocker_set_restarts_bounded_wait(store):
    from herdr import gate_wait, recovery_store
    for now, missing in [(10, ['a']), (40, ['a']), (70, ['a']), (100, ['b']),
                         (130, ['a']), (160, ['a']), (190, ['a']), (220, ['a'])]:
        op = gate_wait.record_wait(store.db_path, 'wf', 'wrapup', missing, now=now)
    assert op['status'] == 'waiting_human'
    active = [item for item in recovery_store.list_operations(store.db_path, 'wf')
              if item['payload'].get('kind') == 'business_gate_wait'
              and item['status'] not in {'resolved', 'superseded'}]
    assert len(active) == 1
    assert len(store.list_events(workflow_id='wf', event_type='business_gate_stalled')) == 2


def test_console_business_wait_projection_does_not_advertise_unsupported_actions(store):
    from herdr import dispatch_recovery, gate_wait
    for now in (10, 40, 70):
        gate_wait.record_wait(store.db_path, 'wf', 'wrapup', ['t'], now=now)
    op = next(op for op in dispatch_recovery.list_operations(store.db_path, 'wf')
              if op['payload'].get('kind') == 'business_gate_wait')
    assert op['recovery']['actions'] == []
    assert 'acceptance-record' in op['recovery']['next_step']


def test_rebaseline_rejects_new_command_and_modified_evaluator(tmp_path):
    import subprocess
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subprocess.run(['git', '-C', str(tmp_path), '-c', 'user.name=T', '-c', 'user.email=t@t',
                    '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-qm', 'base'], check=True)
    command = "printf 'Found 2 errors.'; exit 1"
    loop = evaluator.init_loop(tmp_path, 'bound baseline', test_cmd='true', lint_cmd=command, capture_baseline=True)
    before = (loop / 'BASELINE_LINT.json').read_bytes()
    with pytest.raises(ValueError, match='contract'):
        evaluator.rebaseline_lint(loop, "printf 'Found 9000 errors.'; exit 1", tmp_path, reason='intake')
    assert (loop / 'BASELINE_LINT.json').read_bytes() == before
    (loop / 'EVALUATOR.sh').write_text('#!/bin/sh\nexit 0\n')
    with pytest.raises(ValueError, match='contract'):
        evaluator.rebaseline_lint(loop, command, tmp_path, reason='intake')
    assert (loop / 'BASELINE_LINT.json').read_bytes() == before


def test_unavailable_lint_is_not_presented_as_zero_or_green_quality():
    metrics = evaluator.calculate_metrics('1 passed', 0, 'tool crashed', 2, baseline_lint_errors=100)
    scorecard = evaluator.render_metrics_markdown(metrics, 1, 5)
    quality_row = next(line for line in scorecard.splitlines() if '代码质量 (Quality)' in line)
    assert 'unavailable' in quality_row and '✅' not in quality_row
    feedback = evaluator.render_evaluation_markdown(metrics, 1, 5)
    assert 'Lint: unavailable' in feedback
    assert 'Lint 错误数: `0`' not in feedback


def test_native_eval_rejects_baseline_from_changed_evaluator(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    loop = evaluator.init_loop(tmp_path, 'stale contract', test_cmd="printf '1 passed\\n'",
        lint_cmd="printf 'Found 100 errors.'; exit 1", capture_baseline=True)
    script = loop / 'EVALUATOR.sh'
    script.write_text(script.read_text().replace('Found 100 errors.', 'Found 4 errors.'))
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'bin/herdr-loop'),
        'eval', '--dir', str(tmp_path)], capture_output=True, text=True, check=False, timeout=10)
    assert result.returncode != 0
    metrics = json.loads((loop / 'METRICS.json').read_text())
    assert 'lint_baseline_contract_unavailable' in metrics['details']['evaluation_errors']
    assert not json.loads((loop / 'EVAL_DONE.json').read_text())['converged']


def test_reopened_workflow_gets_a_new_wait_generation(store):
    import time

    from herdr import gate_wait, recovery_store
    start = time.time()
    for offset in (0, 30, 60):
        previous = gate_wait.record_wait(store.db_path, 'wf', 'wrapup', ['t'], now=start + offset)
    kernel.transition_workflow('wf', 'completed', reason='fixture closed',
        metadata={'outcome': 'delivered'}, store=store)
    kernel.transition_workflow('wf', 'in_progress', reason='workflow_reopened',
        metadata={'reopened_at': start + 100, 'suppress_auto_close': True}, store=store)
    kernel.transition_workflow('wf', 'running', reason='fixture resumed', store=store)
    current = gate_wait.record_wait(store.db_path, 'wf', 'wrapup', ['t'], now=start + 130)
    assert store.get_workflow('wf')['completed_at']
    assert store.get_workflow('wf')['outcome'] == 'delivered'
    assert current['id'] != previous['id']
    assert current['status'] == 'observing' and current['attempts'] == 1
    for offset in (130, 160, 190):
        current = gate_wait.record_wait(store.db_path, 'wf', 'wrapup', ['t'], now=start + offset)
        recovery_store.reconcile(store.db_path, 'wf', now=start + offset)
    result = next(op for op in recovery_store.list_operations(store.db_path, 'wf') if op['id'] == current['id'])
    assert result['status'] == 'waiting_human'
    old = next(op for op in recovery_store.list_operations(store.db_path, 'wf') if op['id'] == previous['id'])
    assert old['status'] == 'superseded'
    assert old['payload']['generation'] != result['payload']['generation']
