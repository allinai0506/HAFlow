"""Read-side latency contracts, exercised against authoritative SQLite."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from herdr import archive, projection, state_db
from herdr.state_store import SQLiteStateStore


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    monkeypatch.setenv('HERDR_STATE_DB', str(path))
    store = SQLiteStateStore(path, auto_migrate_json=False)
    store.save_workflow({'workflow_id': 'wf', 'status': 'running', 'config': {}})
    for i in range(12):
        store.save_task({'task_id': f't-{i:02}', 'workflow_id': 'wf', 'status': 'cleaned' if i % 2 else 'working',
                         'goal': 'ΑΒΓ STRASSE % _ hello', 'project_id': 'P', 'project_name': '开发',
                         'node_label': '  Mixed LABEL ', 'created_at': 10+i, 'last_activity_at': 90+i})
    return path


def test_archive_sqlite_parity_and_only_decodes_page(db, monkeypatch):
    tasks = state_db.list_tasks(db_path=db)
    cases = [{}, {'status': 'all'}, {'status': 'active', 'offset': 2, 'limit': 2},
             {'q': 'mixed label'}, {'q': '开发'}, {'q': '% _'}, {'workflow_id': ' WF '},
             {'project_id': ' P ', 'agent': 'missing'}, {'status': ' FAILED '},
             {'status': 'all', 'limit': 'bad', 'offset': 'bad'}, {'limit': 999, 'offset': -10}]
    original = state_db._decode_task_row
    decoded = []
    def decode(row):
        decoded.append(row['task_id'])
        return original(row)
    monkeypatch.setattr(state_db, '_decode_task_row', decode)
    monkeypatch.setattr(state_db, 'get_db_connection', lambda *a, **k: pytest.fail('writable connection'))
    for filters in cases:
        decoded.clear()
        expected = archive.query_archived_tasks(tasks, **filters)
        actual = archive.query_archived_tasks_sqlite(db, **filters)
        assert actual == expected
        assert len(decoded) == actual['count']


def test_projection_reads_only_target_snapshot(db, monkeypatch):
    monkeypatch.setattr(projection, 'load_tasks_data', lambda: pytest.fail('full task inventory'))
    monkeypatch.setattr(projection, 'load_workflows_data', lambda: pytest.fail('full workflow inventory'))
    monkeypatch.setattr(state_db, 'get_db_connection', lambda *a, **k: pytest.fail('writable connection'))
    monkeypatch.setattr(projection, 'collect_task_artifacts', lambda t: [])
    monkeypatch.setattr(projection, 'detect_workflow_stalls', lambda *a, **k: {'is_stalled': False})
    import herdr.deliverables
    monkeypatch.setattr(herdr.deliverables, 'project_for_tasks', lambda *a: {})
    assert projection.project_task('t-01')['task_id'] == 't-01'
    result = projection.project_workflow('wf')
    assert [t['task_id'] for t in result['tasks']] == [f't-{i:02}' for i in range(12)]
    with pytest.raises(ValueError, match='not found'):
        projection.project_task('absent')


@pytest.mark.parametrize('updated,activity,created', [
    (None, 50, 10), (0, 70, 10), ('invalid', 90, 10), ('0', 90, 10), (20, 90, 10),
])
def test_archive_timestamp_fallback_and_unicode_search(db, updated, activity, created):
    conn = state_db.get_db_connection(db)
    try:
        row = conn.execute("SELECT payload_json FROM tasks WHERE task_id='t-01'").fetchone()
        payload = json.loads(row[0])
        payload.update(last_activity_at=activity, node_label='  ΑΒΓ İ Straße ', project_name=True)
        conn.execute("UPDATE tasks SET updated_at=?,created_at=?,payload_json=? WHERE task_id='t-01'",
                     (updated, created, json.dumps(payload)))
    finally:
        conn.close()
    tasks = state_db.list_tasks(db_path=db)
    for filters in ({'limit': 1}, {'q': 'αβγ'}, {'q': 'i̇'}, {'q': 'true'}, {'q': 'strasse'},
                    {'q': ' wf '}, {'project_id': 'P', 'offset': 500}):
        assert archive.query_archived_tasks_sqlite(db, **filters) == archive.query_archived_tasks(tasks, **filters)


def test_projection_shared_probe_budget_across_requests(db, monkeypatch):
    import threading
    lock = threading.Lock()
    four_started = threading.Event()
    release = threading.Event()
    state = {'active': 0, 'maximum': 0}
    original = projection.project_task
    def probe(task_id, *, task=None):
        with lock:
            state['active'] += 1
            state['maximum'] = max(state['maximum'], state['active'])
            if state['active'] == 4:
                four_started.set()
        try:
            assert release.wait(5), 'probe test release timed out'
            return original(task_id, task=task)
        finally:
            with lock:
                state['active'] -= 1
    monkeypatch.setattr(projection, 'project_task', probe)
    monkeypatch.setattr(projection, 'collect_task_artifacts', lambda t: [])
    monkeypatch.setattr(projection, 'detect_workflow_stalls', lambda *a, **k: {'is_stalled': False})
    import herdr.deliverables
    monkeypatch.setattr(herdr.deliverables, 'project_for_tasks', lambda *a: {})
    with ThreadPoolExecutor(max_workers=2) as requests:
        futures = [requests.submit(projection.project_workflow, 'wf') for _ in range(2)]
        try:
            assert four_started.wait(5), 'workflow probes were serial'
            assert not any(future.done() for future in futures)
        finally:
            release.set()
        results = [future.result(timeout=5) for future in futures]
    assert state['maximum'] == 4
    assert all(len(result['tasks']) == 12 for result in results)


def test_stall_detection_reuses_config_without_store_initialization(db, monkeypatch):
    from herdr import projects
    monkeypatch.setattr(projection, 'load_workflows_data', lambda: pytest.fail('global workflows'))
    monkeypatch.setattr(projects, '_get_store', lambda: pytest.fail('StateStore initialization'))
    monkeypatch.setattr(projects, 'workflow_config_for', lambda *a: pytest.fail('config reread'))
    workflow = {'workflow_id': 'wf', 'status': 'running', 'startup_ready': False}
    tasks = [{'task_id': 'done', 'node': 'finish', 'status': 'completed', 'updated_at': 1}]
    config = {'nodes': [{'id': 'finish', 'depends_on': []}]}
    result = projection.detect_workflow_stalls('wf', tasks, workflow=workflow, config=config)
    assert not result['is_stalled']
    # Explicit absent/legacy config must not fall back to any mutable file/store.
    result = projection.detect_workflow_stalls('wf', tasks, workflow=workflow, config=None)
    assert result['is_stalled']
    # Standalone lookup reads only the workflow and never exports its inventory.
    assert not projection.detect_workflow_stalls('wf', [], config=None)['is_stalled']


def test_archive_corrupt_nonfinite_times_have_deterministic_order(db):
    conn = state_db.get_db_connection(db)
    try:
        for task_id, timestamp in [('t-01', 'NaN'), ('t-03', 'inf'), ('t-05', '-inf')]:
            conn.execute('UPDATE tasks SET updated_at=? WHERE task_id=?', (timestamp, task_id))
    finally:
        conn.close()
    tasks = state_db.list_tasks(db_path=db)
    expected = archive.query_archived_tasks(tasks)
    actual = archive.query_archived_tasks_sqlite(db)
    assert actual == expected
    ids = [item['task_id'] for item in actual['items']]
    assert ids[-3:] == ['t-01', 't-03', 't-05']


def test_stall_config_preserves_required_task_scope_and_runtime(db, monkeypatch):
    from herdr import projects
    monkeypatch.setattr(projects, '_get_store', lambda: pytest.fail('StateStore initialization'))
    config = {'nodes': [{'id': 'finish', 'tab_id': 'old', 'required_task_ids': ['future-task']}]}
    projected = projection._stall_config(config, 'wf', {'node_runtime': {'finish': {'tab_id': 'live'}}})
    assert projected['nodes'][0]['tab_id'] == 'live'
    assert config['nodes'][0]['tab_id'] == 'old'
    # A known task from the wrong node must still invalidate the definition.
    config['nodes'][0]['required_task_ids'] = ['t-01']
    with pytest.raises(ValueError, match='required_task_out_of_node'):
        projection._stall_config(config, 'wf', {})
    with pytest.raises(ValueError, match='required_task_out_of_workflow'):
        projection._stall_config(config, 'foreign-workflow', {})
