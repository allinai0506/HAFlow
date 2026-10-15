"""Deliverable reads use existing SQLite facts without mutable initialization."""
import json
import sqlite3

import pytest

from console import herdr_factory_console as c
from herdr import state_db


def seed(tmp_path, monkeypatch):
    db = tmp_path / 'state.db'
    monkeypatch.setenv('HERDR_STATE_DB', str(db))
    state_db.save_workflow({'workflow_id': 'wf', 'config': {'nodes': [{'id': 'impl'}]}}, db_path=db)
    state_db.save_task({'task_id': 't', 'workflow_id': 'wf', 'node': 'impl', 'status': 'cleaned'}, db_path=db)
    state_db.record_event({'workflow_id': 'wf', 'task_id': 't', 'event_type': 'pull_request_created',
                           'source': 'herdr-task', 'timestamp': 1, 'payload': {'url': 'https://example.com/pulls/1'}}, db_path=db)
    monkeypatch.setattr(c, '_deliverable_observation_loader', c._load_deliverable_observations)
    monkeypatch.setattr(c, '_deliverable_pr_loader', c._load_deliverable_pr)
    return db


def test_catalog_and_file_read_never_initialize_or_load_global_inventory(tmp_path, monkeypatch):
    db = seed(tmp_path, monkeypatch)
    def forbidden(*args, **kwargs):
        pytest.fail('deliverable read entered mutable/global state path')
    monkeypatch.setattr(state_db, 'get_db_connection', forbidden)
    monkeypatch.setattr(c.herdr_kernel, '_get_store', forbidden)
    monkeypatch.setattr(c, 'workflows', forbidden)
    monkeypatch.setattr(c, 'tasks_for_workflow', forbidden)
    result = c.api_workflow_deliverables('wf')
    assert [row['url'] for row in result['items'] if row['kind'] == 'git_pr'] == ['https://example.com/pulls/1']
    with pytest.raises(c.herdr_deliverables.DeliverableError) as exc:
        c.api_read_deliverable('wf', 't', 'absent')
    assert exc.value.status == 404


def test_enrichment_shares_one_readonly_snapshot_and_bounds_each_task(tmp_path, monkeypatch):
    db = seed(tmp_path, monkeypatch)
    state_db.save_task({'task_id': 'other', 'workflow_id': 'wf', 'node': 'impl'}, db_path=db)
    conn = sqlite3.connect(db)
    for i in range(45):
        conn.execute('''INSERT INTO observations
            (observation_id,run_id,task_id,workflow_id,source_type,source_ref,content_ref,media_type,size_bytes,sha256,metadata_json,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (f'o{i:02}', 'run', 't', 'wf', 'artifact', f'src{i}', 'ref', 'text/plain', 1, f'{i:064x}', json.dumps({'artifact_path': f'{i}.md'}), i))
    conn.commit();conn.close()
    for workflow, task, source, timestamp, number in [('wf', 't', 'herdr-task', 2, 2), ('wf', 't', 'herdr-task', 2, 3),
                                                       ('foreign', 't', 'herdr-task', 99, 99), ('wf', 't', 'agent', 100, 100)]:
        state_db.record_event({'workflow_id': workflow, 'task_id': task, 'event_type': 'pull_request_created',
                               'source': source, 'timestamp': timestamp, 'payload': {'url': f'https://example.com/pulls/{number}'}}, db_path=db)
    original = state_db.get_readonly_db_connection
    connections = []
    statements = []
    def tracked(path):
        conn = original(path)
        connections.append(conn)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(state_db, 'get_readonly_db_connection', tracked)
    result = c._tasks_for_deliverables([{'task_id': 't', 'workflow_id': 'wf'}, {'task_id': 'other', 'workflow_id': 'wf'}])
    assert len(connections) == 1
    assert len(result[0]['observations']) == 40
    assert result[0]['observations'][0]['observation_id'] == 'o00'
    assert result[0]['observations'][-1]['observation_id'] == 'o39'
    assert result[0]['pull_request']['url'] == 'https://example.com/pulls/3'
    assert 'pull_request' not in result[1]
    assert not any(sql.lstrip().upper().startswith(('CREATE', 'UPDATE', 'INSERT', 'DELETE')) for sql in statements)
    assert any(sql == 'BEGIN' for sql in statements)


def test_enrichment_work_is_scoped_to_task_in_large_shared_tables(tmp_path, monkeypatch):
    db = seed(tmp_path, monkeypatch)
    conn = sqlite3.connect(db)
    for i in range(1000):
        conn.execute('''INSERT INTO observations
            (observation_id,run_id,task_id,workflow_id,source_type,source_ref,content_ref,media_type,size_bytes,sha256,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            (f'foreign{i}', 'foreign-run', 'foreign', 'foreign-wf', 'artifact', f'src{i}', 'ref', 'text/plain', 1, f'{i:064x}', i))
        conn.execute('''INSERT INTO events (workflow_id,task_id,event_type,source,timestamp,payload_json)
                        VALUES ('foreign-wf','foreign','pull_request_created','herdr-task',?,'{}')''', (i + 10,))
    conn.commit();conn.close()
    steps = []
    original = state_db.get_readonly_db_connection
    def tracked(path):
        conn = original(path)
        conn.set_progress_handler(lambda: steps.append(100) or 0, 100)
        return conn
    monkeypatch.setattr(state_db, 'get_readonly_db_connection', tracked)
    result = c._tasks_for_deliverables([{'task_id': 't', 'workflow_id': 'wf'}])
    assert result[0]['pull_request']['url'] == 'https://example.com/pulls/1'
    assert sum(steps) < 1000, 'unrelated workflows must not expand the enrichment query work'
