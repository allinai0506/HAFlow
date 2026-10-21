"""Real temporary-storage paths supplement the call-site binding matrix."""
from herdr import state_db


def test_default_paths_tokens_and_default_migration(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    for key in ('HERDR_STATE_DB', 'CHECKPOINTS_DIR', 'WORKFLOWS_FILE', 'TASKS_FILE', 'WORKFLOW_FILE'):
        monkeypatch.delenv(key, raising=False)
    # Preserve a test spy Path subclass when the hit-collection plugin is active.
    controller = type(state_db.CONTROLLER_DIR)(tmp_path / 'controller')
    controller.mkdir()
    monkeypatch.setattr(state_db, 'CONTROLLER_DIR', controller)
    expected = tmp_path / 'controller/state.db'
    assert state_db.resolve_state_db_path() == expected
    assert state_db.init_db() == expected
    with state_db.get_db_connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM workflows').fetchone()[0] == 0
    state_db.save_user_token('fixture-user', 'fixture-type', 'synthetic-fixture-token')
    assert state_db.get_user_token('fixture-user', 'fixture-type') == 'synthetic-fixture-token'
    assert state_db.delete_user_token('fixture-user', 'fixture-type')
    assert state_db.get_user_token('fixture-user', 'fixture-type') is None
    result = state_db.migrate_v1_to_v2()
    assert isinstance(result, dict)
    assert expected.is_file()


def test_empty_reads_and_idempotent_event_use_real_connections(tmp_path):
    import pytest
    db = tmp_path / 'reads.db'
    state_db.init_db(db)
    assert state_db.get_intervention('missing', db_path=db) is None
    with pytest.raises(ValueError, match='handoff or working context does not exist'):
        state_db.attach_working_context_ref('missing', 'missing', db_path=db)
    assert state_db.read_execution_outcome('missing', 'run-missing', db_path=db) is None
    assert state_db.latest_trajectory_sequence('run-missing', db_path=db) == 0
    assert state_db.list_trajectory_findings_bounded('run-missing', 10, db_path=db) == []
    version = state_db.get_context_source_version('run-missing', None, 10, db_path=db)
    assert version['trajectory_sequence'] == 0 and version['task_id'] is None
    event = {'workflow_id': 'wf-event', 'event_type': 'probe', 'source': 'test',
             'payload': {'probe_id': 'probe-once'}}
    first = state_db.record_event_if_absent(event, identity_field='probe_id',
                                           identity_value='probe-once', db_path=db)
    second = state_db.record_event_if_absent(event, identity_field='probe_id',
                                            identity_value='probe-once', db_path=db)
    assert first['status'] == 'created' and second['status'] == 'exists'
    assert len(state_db.list_events(workflow_id='wf-event', db_path=db)) == 1


def test_factory_pause_and_task_cli_read_back_real_sqlite(tmp_path):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    db = tmp_path / 'cli.db'
    state_db.save_workflow({'workflow_id': 'wf-cli', 'status': 'running'}, db_path=db)
    state_db.save_task({'task_id': 't-cli', 'workflow_id': 'wf-cli', 'status': 'working'}, db_path=db)
    env = dict(os.environ, HERDR_STATE_DB=str(db),
               TASKS_FILE=str(tmp_path / 'tasks.json'), WORKFLOWS_FILE=str(tmp_path / 'workflows.json'))
    paused = subprocess.run([sys.executable, str(root / 'bin/herdr-factory'), 'pause', 'wf-cli'],
                            env=env, capture_output=True, text=True, timeout=10, check=True)
    assert paused.returncode == 0
    assert state_db.get_workflow('wf-cli', db_path=db)['status'] == 'paused'
    events = state_db.list_events(workflow_id='wf-cli', event_type='workflow_transition', db_path=db)
    assert len(events) == 1 and events[0]['payload']['to_status'] == 'paused'
    listed = subprocess.run([sys.executable, str(root / 'bin/herdr-task'), 'list', '--workflow-id', 'wf-cli'],
                            env=env, capture_output=True, text=True, timeout=10, check=True)
    data = json.loads(listed.stdout)
    assert 't-cli' in json.dumps(data)


def test_register_port_pending_invalid_retry_and_rollback(tmp_path, monkeypatch):
    import pytest

    from herdr.state import transition_gate
    from herdr.transitions import InvalidTransitionError
    db = tmp_path / 'register.db'
    seen = []
    validate = transition_gate.validate_workflow_transition
    def traced(old, new):
        seen.append((old, new))
        return validate(old, new)
    monkeypatch.setattr(transition_gate, 'validate_workflow_transition', traced)
    state_db.save_workflow({'workflow_id': 'pending', 'status': 'pending'}, db_path=db)
    wf = {'workflow_id': 'pending', 'status': 'running'}
    first = state_db.register_workflow(wf, db_path=db)
    second = state_db.register_workflow(wf, db_path=db)
    assert seen == [('pending', 'running')]
    assert first['event_id'] == second['event_id'] and second['deduplicated']
    assert state_db.get_workflow('pending', db_path=db)['status'] == 'running'
    assert len(state_db.list_events(workflow_id='pending', db_path=db)) == 1
    state_db.save_workflow({'workflow_id': 'invalid', 'status': 'unknown-status'}, db_path=db)
    with pytest.raises(InvalidTransitionError):
        state_db.register_workflow({'workflow_id': 'invalid', 'status': 'running'}, db_path=db)
    assert state_db.get_workflow('invalid', db_path=db)['status'] == 'unknown-status'
    assert state_db.list_events(workflow_id='invalid', db_path=db) == []
    original = state_db.record_event
    def fail(event, **kwargs):
        raise OSError('event failed')
    monkeypatch.setattr(state_db, 'record_event', fail)
    with pytest.raises(OSError, match='event failed'):
        state_db.register_workflow({'workflow_id': 'rollback', 'status': 'running'}, db_path=db)
    assert state_db.get_workflow('rollback', db_path=db) is None
    monkeypatch.setattr(state_db, 'record_event', original)
    state_db.register_workflow({'workflow_id': 'rollback', 'status': 'running'}, db_path=db)
    assert len(state_db.list_events(workflow_id='rollback', db_path=db)) == 1


def test_register_port_external_connection_transaction_owned_by_caller(tmp_path, monkeypatch):
    db = tmp_path / 'external.db'
    conn = state_db.get_db_connection(db)
    statements = []
    conn.execute('BEGIN IMMEDIATE')
    conn.set_trace_callback(statements.append)
    connections = []
    save = state_db.save_workflow
    event = state_db.record_event
    def traced_save(wf, **kwargs):
        connections.append(kwargs['conn'])
        return save(wf, **kwargs)
    def traced_event(ev, **kwargs):
        connections.append(kwargs['conn'])
        return event(ev, **kwargs)
    monkeypatch.setattr(state_db, 'record_event', traced_event)
    state_db.register_workflow({'workflow_id': 'external', 'status': 'running'},
                               conn=conn, save_workflow_fn=traced_save)
    assert connections == [conn, conn]
    assert conn.in_transaction
    assert not any(s.strip().split()[0].upper() in {'BEGIN', 'COMMIT', 'ROLLBACK'} for s in statements)
    conn.rollback()
    assert state_db.get_workflow('external', db_path=db) is None
    conn.execute('SELECT 1')
    conn.close()
