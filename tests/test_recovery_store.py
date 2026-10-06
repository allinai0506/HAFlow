import multiprocessing
import sqlite3

import pytest
from herdr import recovery_store as rs
from herdr import state_db


def _seed(path):
    conn = state_db.get_db_connection(path)
    rs.ensure_schema(conn)
    with conn:
        state_db.save_workflow({'workflow_id': 'w', 'status': 'running', 'execution_id': 'gen', 'candidate_sha': 'A', 'config': {'nodes': {}}}, conn=conn)
    conn.close()


def _facts(identity='i', candidate='A'):
    return {'identity_key': identity, 'status': 'pending', 'candidate_sha': candidate}


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / 'state.db'
    _seed(p)
    from herdr import workflow_progress
    monkeypatch.setattr(workflow_progress, 'assess_workflow', lambda *args: {'obligations': [_facts()]})
    conn = state_db.get_db_connection(p)
    with conn:
        rs.ensure_for_workflow(conn, 'w', 10)
    conn.close()
    return p


def test_read_only_absent(tmp_path):
    p = tmp_path / 'absent.db'
    assert rs.list_operations(p, 'w') == []
    assert not p.exists()
    sqlite3.connect(p).close()
    assert rs.list_operations(p, 'w') == []
    assert sqlite3.connect(p).execute('SELECT name FROM sqlite_master').fetchall() == []


def test_owner_lease_and_delivery_unknown(db):
    op = rs.list_operations(db, 'w')[0]
    assert rs.claim_operation(db, op['id'], 'a', 9) is None
    assert rs.claim_operation(db, op['id'], 'a', 10)['attempts'] == 1
    assert rs.claim_operation(db, op['id'], 'b', 10) is None
    with pytest.raises(ValueError):
        rs.finish_operation(db, op['id'], 'b', 'resolved', {}, 11)
    assert rs.mark_started(db, op['id'], 'a', 'delivery', 11)['started']
    with pytest.raises(ValueError):
        rs.finish_operation(db, op['id'], 'a', 'resolved', {}, 71)
    result = rs.reconcile(db, 'w', 71)[0]
    assert result['status'] == 'waiting_human'
    assert result['detail']['reason'] == 'delivery_unknown'
    assert rs.claim_operation(db, op['id'], 'b', 72) is None
    with pytest.raises(ValueError):
        rs.decide_operation(db, op['id'], result['version'], 'human', 'retry', 'retry', 72)


def test_transaction_rollback(db):
    from herdr import workflow_progress
    conn = state_db.get_db_connection(db)
    before = rs.list_operations(db, 'w')
    with pytest.raises(RuntimeError):
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("UPDATE workflows SET status='paused' WHERE workflow_id='w'")
            rs.ensure_obligations(conn, {'workflow_id': 'w'}, {}, [], now=12)
            raise RuntimeError('injected before commit')
    assert rs.list_operations(db, 'w') == before
    assert conn.execute("SELECT status FROM workflows WHERE workflow_id='w'").fetchone()[0] == 'running'
    conn.close()


def test_version_fresh_candidate_and_hold(db, monkeypatch):
    from herdr import workflow_progress
    op = rs.list_operations(db, 'w')[0]
    held = rs.decide_operation(db, op['id'], op['version'], 'alice', 'hold', 'investigate', 11, until=20)
    assert held['status'] == 'waiting'
    assert rs.claim_operation(db, op['id'], 'worker', 12) is None
    expired = rs.reconcile(db, 'w', 20)[0]
    assert expired['status'] == 'waiting_human'
    with pytest.raises(ValueError):
        rs.decide_operation(db, op['id'], held['version'], 'alice', 'retry', 'done', 21)
    monkeypatch.setattr(workflow_progress, 'assess_workflow', lambda *args: {'obligations': [_facts('new', 'B')]})
    with pytest.raises(ValueError):
        rs.decide_operation(db, op['id'], expired['version'], 'alice', 'retry', 'done', 21)
    rows = rs.reconcile(db, 'w', 21)
    assert rows[0]['status'] == 'superseded'
    assert rows[1]['payload']['candidate_sha'] == 'B'


def test_stable_facts_and_bounded_attempts(db):
    op = rs.list_operations(db, 'w')[0]
    assert rs.reconcile(db, 'w', 11)[0]['version'] == op['version']
    for index in range(3):
        assert rs.claim_operation(db, op['id'], 'worker', 10 + index * 61)['attempts'] == index + 1
    assert rs.claim_operation(db, op['id'], 'worker', 200) is None
    assert rs.list_operations(db, 'w')[0]['status'] == 'waiting_human'


def _race(path, barrier, queue, claim=False):
    from herdr import workflow_progress
    workflow_progress.assess_workflow = lambda *args: {'obligations': [_facts()]}
    barrier.wait()
    if claim:
        queue.put(rs.claim_operation(path, 1, str(multiprocessing.current_process().pid), 10) is not None)
    else:
        conn = state_db.get_db_connection(path)
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            rs.ensure_for_workflow(conn, 'w', 10)
        conn.close()
        queue.put(True)


@pytest.mark.parametrize('claim', [False, True])
def test_independent_process_uniqueness(tmp_path, claim):
    path = tmp_path / 'race.db'
    _seed(path)
    if claim:
        conn = state_db.get_db_connection(path)
        with conn:
            rs.ensure_obligations(conn, {'workflow_id': 'w'}, {}, [], 10)
        conn.close()
        # Seed an explicit stable slot, independent of pure assessor input.
        conn = sqlite3.connect(path)
        conn.execute("DELETE FROM workflow_recovery_operations")
        conn.execute("INSERT INTO workflow_recovery_operations(id,identity_key,workflow_id,payload_json,status,next_due_at,created_at,updated_at) VALUES(1,'i','w',?,'pending',10,10,10)", (__import__('json').dumps(_facts(), sort_keys=True),))
        conn.commit()
        conn.close()
    context = multiprocessing.get_context('spawn')
    barrier = context.Barrier(2)
    queue = context.Queue()
    processes = [context.Process(target=_race, args=(path, barrier, queue, claim)) for _ in range(2)]
    for process in processes:
        process.start()
    outcomes = [queue.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    assert sum(outcomes) == (1 if claim else 2)
    assert len(rs.list_operations(path, 'w')) == 1


def test_new_gate_revokes_claim_same_slot(db, monkeypatch):
    from herdr import workflow_progress
    op = rs.claim_operation(db, 1, 'worker', 10)
    merged = {**_facts(), 'task_ids': ['test', 'review']}
    monkeypatch.setattr(workflow_progress, 'assess_workflow', lambda *args: {'obligations': [merged]})
    with pytest.raises(ValueError):
        rs.mark_started(db, op['id'], 'worker', 'delivery', 11)
    current = rs.reconcile(db, 'w', 12)[0]
    assert current['status'] == 'pending'
    assert current['owner'] is None
    assert len(rs.list_operations(db, 'w')) == 1
    assert current['version'] > op['version']


def test_facts_and_new_operation_rollback(db, monkeypatch):
    from herdr import workflow_progress
    monkeypatch.setattr(workflow_progress, 'assess_workflow', lambda *args: {'obligations': [_facts('new')]})
    conn = state_db.get_db_connection(db)
    before = rs.list_operations(db, 'w')
    with pytest.raises(RuntimeError):
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            state_db.save_task({'task_id': 'gate', 'workflow_id': 'w', 'status': 'blocked', 'stage_verdict': 'blocked'}, conn=conn)
            rs.ensure_for_workflow(conn, 'w', 12)
            assert conn.execute('SELECT COUNT(*) FROM workflow_recovery_operations').fetchone()[0] == 2
            raise RuntimeError('injected before commit')
    assert rs.list_operations(db, 'w') == before
    assert conn.execute("SELECT task_id FROM tasks WHERE task_id='gate'").fetchone() is None
    conn.close()


def test_finalize_retry_and_receipt_survives_source_disappearance(db, monkeypatch):
    from herdr import workflow_progress
    facts = {**_facts(), 'status': 'waiting_human', 'reason': 'finalize_escalated', 'kind': 'finalize'}
    monkeypatch.setattr(workflow_progress, 'assess_workflow', lambda *args: {'obligations': [facts]})
    current = rs.reconcile(db, 'w', 11)[0]
    retry = rs.decide_operation(db, current['id'], current['version'], 'operator', 'retry', 'resolved integration conflict', 12)
    claim = rs.claim_operation(db, retry['id'], 'worker', 13)
    assert claim is not None
    rs.record_step(db, claim['id'], 'worker', 'successor_registered', {'successor_ids': ['next'], 'gate_nodes': ['test']}, 14)
    monkeypatch.setattr(workflow_progress, 'assess_workflow', lambda *args: {'obligations': []})
    rs.reconcile(db, 'w', 15)
    receipt = rs.finish_operation(db, claim['id'], 'worker', 'awaiting_result', {'delivery_confirmed': True}, 16)
    assert receipt['detail']['successor_ids'] == ['next']
    assert rs.reconcile(db, 'w', 17)[0]['status'] == 'awaiting_result'


def test_real_assessment_fact_hook_transaction(tmp_path):
    path = tmp_path / 'real.db'
    sha = 'a' * 40
    config = {'nodes': [{'id': 'implementation'}, {'id': 'test', 'gate': {'retry_node': 'implementation'}}]}
    conn = state_db.get_db_connection(path)
    rs.ensure_schema(conn)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        state_db.save_workflow({'workflow_id': 'w', 'status': 'running', 'execution_id': 'generation', 'candidate_sha': sha, 'config': config}, conn=conn)
        state_db.save_task({'task_id': 'impl', 'workflow_id': 'w', 'node': 'implementation', 'status': 'committed', 'run_id': 'impl-run', 'execution_id': 'generation', 'candidate_sha': sha}, conn=conn)
        state_db.save_task({'task_id': 'test', 'workflow_id': 'w', 'node': 'test', 'status': 'cleaned', 'stage_verdict': 'blocked', 'run_id': 'test-run', 'execution_id': 'generation', 'candidate_sha': sha}, conn=conn)
        rs.ensure_for_workflow(conn, 'w', 10)
    rows = rs.list_operations(path, 'w')
    assert len(rows) == 1
    assert rows[0]['payload']['task_ids'] == ['test']
    assert rows[0]['payload']['affected_task_ids'] == ['impl']
    assert rows[0]['status'] == 'pending'
    assert rs.claim_operation(path, rows[0]['id'], 'worker', rows[0]['next_due_at']) is not None
    conn.close()


def test_snapshot_derives_candidate_event_and_bounded_config(tmp_path):
    import json
    path = tmp_path / 'snapshot.db'
    config_path = tmp_path / 'workflow.json'
    config_path.write_text(json.dumps({'nodes': [{'id': 'implementation'}, {'id': 'test', 'gate': {'retry_node': 'implementation'}}]}))
    conn = state_db.get_db_connection(path)
    rs.ensure_schema(conn)
    sha = 'c' * 40
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        state_db.save_workflow({'workflow_id': 'w', 'status': 'running', 'created_at': 10, 'execution_id': 'generation', 'workflow_file': str(config_path)}, conn=conn)
        state_db.record_event({'workflow_id': 'w', 'event_type': 'candidate_frozen', 'source': 'critical-path-scheduler', 'timestamp': 11, 'payload': {'candidate_sha': sha}}, conn=conn)
        state_db.save_task({'task_id': 'impl', 'workflow_id': 'w', 'node': 'implementation', 'status': 'committed', 'run_id': 'impl-run', 'execution_id': 'generation', 'candidate_sha': sha}, conn=conn)
        state_db.save_task({'task_id': 'test', 'workflow_id': 'w', 'node': 'test', 'status': 'cleaned', 'stage_verdict': 'blocked', 'run_id': 'run', 'execution_id': 'generation', 'candidate_sha': sha}, conn=conn)
        rs.ensure_for_workflow(conn, 'w', 12)
    row = rs.list_operations(path, 'w')[0]
    assert row['status'] == 'pending', (row['payload']['reason'], row['detail'])
    assert row['payload']['candidate_sha'] == sha
    assert 'candidate_sha' not in json.loads(conn.execute("SELECT metadata_json FROM workflows WHERE workflow_id='w'").fetchone()[0])
    conn.close()


def test_active_source_limit_does_not_starve(db):
    conn = state_db.get_db_connection(db)
    with conn:
        conn.execute('UPDATE workflow_recovery_operations SET id=100 WHERE id=1')
        for number in range(40):
            conn.execute("INSERT INTO workflow_recovery_operations(id,identity_key,workflow_id,payload_json,status,next_due_at,created_at,updated_at) VALUES (?,?,'w','{}','resolved',0,0,0)", (number + 1, str(number)))
    conn.close()
    assert rs.reconcile(db, 'w', 11, active_only=True, limit=1)[0]['id'] == 100


def test_verify_unknown_receipt_claim_preserves_cursor(db):
    op = rs.claim_operation(db, 1, 'old', 10)
    rs.record_step(db, op['id'], 'old', 'successor_registered', {'successor_ids': ['next']}, 11)
    expired = rs.reconcile(db, 'w', 71)[0]
    verified = rs.decide_operation(db, 1, expired['version'], 'operator', 'verify', 'inspect delivery', 72)
    assert verified['detail']['successor_ids'] == ['next']
    assert verified['detail']['action'] == 'verify'
    claim = rs.claim_operation(db, 1, 'new', 73)
    assert claim['detail']['step'] == 'successor_registered'
    assert claim['detail']['action'] == 'verify'


def test_settle_result_uses_fresh_canonical_targets(db):
    op = rs.claim_operation(db, 1, 'worker', 10)
    rs.record_step(db, 1, 'worker', 'registered', {'successor_ids': ['next'], 'execution_id': 'gen'}, 11)
    receipt = rs.finish_operation(db, 1, 'worker', 'awaiting_result', {}, 12)
    assert rs.settle_result(db, 1, receipt['version'], 13)['status'] == 'waiting_human'
    with pytest.raises(ValueError):
        rs.settle_result(db, 1, receipt['version'], 14)


@pytest.mark.parametrize('change', ['pause', 'candidate'])
def test_pre_effect_validation_rejects_fresh_changed_workflow(db, change):
    op = rs.claim_operation(db, 1, 'worker', 10)
    conn = state_db.get_db_connection(db)
    with conn:
        if change == 'pause':
            conn.execute("UPDATE workflows SET status='paused' WHERE workflow_id='w'")
        else:
            state_db.record_event({'workflow_id': 'w', 'event_type': 'candidate_frozen', 'source': 'critical-path-scheduler', 'timestamp': 99999999999, 'payload': {'candidate_sha': 'b' * 40}}, conn=conn)
    conn.close()
    with pytest.raises(ValueError):
        rs.record_step(db, op['id'], 'worker', 'external_launch', {}, 11)
    assert rs.list_operations(db, 'w')[0]['started'] == 0


def test_pre_effect_new_gate_revokes_safety_without_rewriting_started_payload(db, monkeypatch):
    from herdr import workflow_progress
    op = rs.claim_operation(db, 1, 'worker', 10)
    rs.record_step(db, 1, 'worker', 'successor_registered', {'successor_ids': ['next']}, 11)
    changed = {**_facts(), 'task_ids': ['late-review'], 'affected_task_ids': ['new-impl']}
    monkeypatch.setattr(workflow_progress, 'assess_workflow', lambda *args: {'obligations': [changed]})
    assert rs.reconcile(db, 'w', 12)[0]['payload'] == op['payload']
    with pytest.raises(ValueError):
        rs.record_step(db, 1, 'worker', 'invalidate_gates', {}, 13)
    receipt = rs.record_step(db, 1, 'worker', 'delivery_confirmed', {'delivery_confirmed': True}, 14, validate=False)
    assert receipt['detail']['successor_ids'] == ['next']


def test_renew_owner_and_inactive_claim(db):
    claim = rs.claim_operation(db, 1, 'worker', 10)
    renewed = rs.renew_owner(db, 1, 'worker', 11, lease_seconds=120)
    assert renewed['lease_until'] == 131
    with pytest.raises(ValueError):
        rs.renew_owner(db, 1, 'foreign', 12)
    with pytest.raises(ValueError):
        rs.renew_owner(db, 1, 'worker', 131)
    conn = state_db.get_db_connection(db)
    with conn:
        conn.execute("UPDATE workflows SET status='pending' WHERE workflow_id='w'")
    conn.close()
    assert rs.claim_operation(db, 1, 'other', 200) is None


def test_verify_rotated_candidate_checks_original_generation(db):
    claim = rs.claim_operation(db, 1, 'worker', 10)
    rs.record_step(db, 1, 'worker', 'delivered', {'successor_ids': ['next']}, 11)
    conn = state_db.get_db_connection(db)
    with conn:
        state_db.record_event({'workflow_id': 'w', 'event_type': 'candidate_frozen', 'source': 'critical-path-scheduler', 'timestamp': 99999999999, 'payload': {'candidate_sha': 'b' * 40}}, conn=conn)
    conn.close()
    expired = rs.reconcile(db, 'w', 71)[0]
    decision = rs.decide_operation(db, 1, expired['version'], 'operator', 'verify', 'inspect new candidate receipts', 72)
    assert decision['detail']['action'] == 'verify'


def test_read_view_active_before_terminal_history(db):
    conn = state_db.get_db_connection(db)
    with conn:
        conn.execute('UPDATE workflow_recovery_operations SET id=100 WHERE id=1')
        for n in range(40):
            conn.execute("INSERT INTO workflow_recovery_operations(id,identity_key,workflow_id,payload_json,status,next_due_at,created_at,updated_at) VALUES (?,?,'w','{}','resolved',0,0,0)", (n + 1, str(n)))
    conn.close()
    assert rs.list_operations(db, 'w', limit=1)[0]['id'] == 100


def _lineage_db(tmp_path, impl_verdict=None, impl_status='committed'):
    path = tmp_path / 'lineage.db'
    sha = 'a' * 40
    conn = state_db.get_db_connection(path)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        state_db.save_workflow({'workflow_id': 'w', 'status': 'running', 'execution_id': 'gen', 'candidate_sha': sha, 'config': {'nodes': [{'id': 'implementation'}, {'id': 'test', 'gate': {'retry_node': 'implementation'}}, {'id': 'review', 'gate': {'retry_node': 'implementation'}}]}}, conn=conn)
        state_db.save_task({'task_id': 'impl', 'workflow_id': 'w', 'node': 'implementation', 'status': impl_status, 'run_id': 'impl-run', 'execution_id': 'gen', 'candidate_sha': sha, 'commit': sha, 'stage_verdict': impl_verdict, 'stage_verdict_note': 'original defect' if impl_verdict else None}, conn=conn)
        for gate in ('test', 'review'):
            state_db.save_task({'task_id': gate, 'workflow_id': 'w', 'node': gate, 'status': 'cleaned', 'stage_verdict': 'blocked', 'run_id': gate + '-run', 'execution_id': 'gen', 'candidate_sha': sha, 'stage_verdict_affected_task_ids': ['impl']}, conn=conn)
    conn.close()
    op = next(op for op in rs.list_operations(path, 'w') if op['status'] == 'pending')
    return path, rs.claim_operation(path, op['id'], 'worker', op['next_due_at']), sha


def test_confirmed_successor_and_partial_owned_gate_invalidation(tmp_path):
    path, op, sha = _lineage_db(tmp_path)
    now = op['updated_at'] + 1
    rs.record_step(path, op['id'], 'worker', 'launch', {'successor_ids': ['next']}, now)
    conn = state_db.get_db_connection(path)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        state_db.save_task({'task_id': 'next', 'workflow_id': 'w', 'node': 'implementation', 'status': 'running', 'run_id': 'next-run', 'execution_id': 'gen', 'candidate_sha': sha, 'supersedes': 'impl', 'recovery_lineage': {'predecessor_id':'impl', 'successor_id':'next', 'predecessor_run_id':'impl-run', 'successor_run_id':'next-run', 'candidate_sha':sha}}, conn=conn)
        state_db.update_task_metadata('impl', {'superseded_by': 'next'}, conn=conn)
    receipt = {'execution_id':'gen', 'source_runs':{'impl':'impl-run'},
               'repair_map':{'impl':{'kind':'successor','task_id':'next','run_id':'next-run','source_run_id':'impl-run'}}}
    rs.record_step(path, op['id'], 'worker', 'gates_invalidating', receipt, now + 1)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("UPDATE tasks SET status='superseded' WHERE task_id='test'")
    rs.record_step(path, op['id'], 'worker', 'gates_invalidating', {}, now + 2)
    assert rs.renew_owner(path, op['id'], 'worker', now + 3, lease_seconds=120)['lease_until'] == now + 123
    conn.close()


def test_delivered_same_run_rework_verdict_clear(tmp_path):
    path, op, sha = _lineage_db(tmp_path, impl_verdict='blocked', impl_status='working')
    now = op['updated_at'] + 1
    rs.record_step(path, op['id'], 'worker', 'rework_started', {'rework_ids': ['impl']}, now)
    conn = state_db.get_db_connection(path)
    with conn:
        state_db.update_task_metadata('impl', {'rework_delivery': 'delivered', 'rework_request_id':'current-request', 'stage_verdict': None, 'stage_verdict_note': None}, conn=conn)
    receipt = {'execution_id':'gen', 'source_runs':{'impl':'impl-run'}, 'rework_requests':{'impl':'current-request'},
               'repair_map':{'impl':{'kind':'rework','task_id':'impl','run_id':'impl-run','source_run_id':'impl-run',
                                   'request_id':'current-request','completion_epoch':None,'completion_identity_path':None}}}
    rs.record_step(path, op['id'], 'worker', 'gates_invalidating', receipt, now + 1)
    with conn:
        raw = state_db._decode_task_row(conn.execute("SELECT * FROM tasks WHERE task_id='impl'").fetchone())
        state_db.save_task(dict(raw, run_id='another-run'), conn=conn)
    with pytest.raises(ValueError):
        rs.record_step(path, op['id'], 'worker', 'gates_invalidating', {}, now + 2)
    conn.close()


def test_verification_claim_survives_completed_invalidation(tmp_path):
    path, op, sha = _lineage_db(tmp_path, impl_status='working')
    now = op['updated_at'] + 1
    conn = state_db.get_db_connection(path)
    with conn:
        state_db.update_task_metadata('impl', {'rework_delivery':'delivered', 'rework_request_id':'current-request'}, conn=conn)
    receipt = {'execution_id':'gen', 'source_runs':{'impl':'impl-run'}, 'rework_requests':{'impl':'current-request'},
               'repair_map':{'impl':{'kind':'rework','task_id':'impl','run_id':'impl-run','source_run_id':'impl-run',
                                   'request_id':'current-request','completion_epoch':None,'completion_identity_path':None}}}
    rs.record_step(path, op['id'], 'worker', 'gates_invalidating', receipt, now)
    with conn:
        conn.execute("UPDATE tasks SET status='superseded' WHERE task_id IN ('test','review')")
    conn.close()
    rs.reconcile(path, 'w', now + 70)
    expired = next(x for x in rs.list_operations(path, 'w') if x['id'] == op['id'])
    decided = rs.decide_operation(path, op['id'], expired['version'], 'operator', 'verify', 'inspect invalidation receipt', now + 71)
    claimed = rs.claim_operation(path, op['id'], 'verifier', now + 72)
    assert decided['status'] == 'pending'
    assert claimed is not None
    rs.record_step(path, op['id'], 'verifier', 'existing_delivery_verified', {}, now + 73, validate=False)
    rs.renew_owner(path, op['id'], 'verifier', now + 74)
    rs.record_step(path, op['id'], 'verifier', 'gates_invalidating', {}, now + 75)


def test_gate_invalidation_requires_complete_repair_receipt_in_transaction(tmp_path):
    path, op, sha = _lineage_db(tmp_path)
    now = op['updated_at'] + 1
    with pytest.raises(ValueError, match='repair coverage'):
        rs.record_step(path, op['id'], 'worker', 'gates_invalidating', {}, now)
    fresh = next(x for x in rs.list_operations(path, 'w') if x['id'] == op['id'])
    assert fresh['started'] == 0
    assert not fresh['detail'].get('gate_invalidation_started')
