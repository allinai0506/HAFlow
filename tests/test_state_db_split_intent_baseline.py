"""Incremental a6b315a intent contract: real temporary SQLite and event readback."""
import pytest
from herdr import state_db


@pytest.fixture
def db(tmp_path):
    path = tmp_path / 'intent.db'
    state_db.save_task({'task_id': 'intent-task', 'workflow_id': 'intent-wf',
                        'status': 'blocked', 'run_id': 'intent-run'}, db_path=path)
    return path


def test_cas_forwards_intent_and_persists_canonical_event(db):
    task = state_db.get_task('intent-task', db_path=db)
    result = state_db.compare_and_set_task_transition(
        'intent-task', 'completed', reason='authorized bypass', source='operator',
        expected_status='blocked', expected_version=task['version'],
        intent='operator-bypass', db_path=db)
    assert result['ok']
    reopened = state_db.get_task('intent-task', db_path=db)
    assert reopened['status'] == 'completed'
    assert reopened['intent'] == 'operator-bypass'
    assert reopened['status_history'][-1]['intent'] == 'operator-bypass'
    events = state_db.list_events(workflow_id='intent-wf', event_type='task_transition', db_path=db)
    assert len(events) == 1 and events[0]['payload']['intent'] == 'operator-bypass'


@pytest.mark.parametrize('source,intent,force', [
    ('delivery-repair', None, False),
    ('operator', 'delivery-repair', False),
    ('delivery-repair', 'delivery-repair', True),
])
def test_delivery_repair_intent_never_bypasses_real_authorization(db, source, intent, force):
    before = state_db.get_task('intent-task', db_path=db)
    with pytest.raises(ValueError, match='delivery repair transition refused'):
        state_db.transition_task('intent-task', 'rework', reason='unauthorized',
                                 source=source, intent=intent, force=force, db_path=db)
    after = state_db.get_task('intent-task', db_path=db)
    assert after == before
    assert state_db.list_events(workflow_id='intent-wf', event_type='task_transition', db_path=db) == []
