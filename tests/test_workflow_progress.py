from copy import deepcopy
from itertools import permutations

import pytest

from herdr.workflow_progress import active_tasks, assess_workflow, recovery_identity

SHA = 'a' * 40
WF = {'workflow_id': 'wf', 'status': 'running', 'execution_id': 'generation-1', 'candidate_sha': SHA}
CFG = {'nodes': [{'id': 'implementation'}, {'id': 'test'}, {'id': 'review'}]}


def task(name='test', **changes):
    result = {'task_id': name + '-01', 'workflow_id': 'wf', 'run_id': name + '-run', 'node': name,
              'status': 'cleaned', 'stage_verdict': 'blocked', 'stage_verdict_note': 'failure', 'candidate_sha': SHA}
    result.update(changes)
    return result


def test_cleaned_blocked_recovers_without_review():
    failed = task(execution_id='generation-1')
    result = assess_workflow(WF, CFG, [task('implementation', execution_id='generation-1', status='committed', stage_verdict='pass'), failed])
    assert result['blockers'] == [failed]
    assert result['can_advance'] is False
    assert result['obligations'][0]['status'] == 'pending'
    assert result['obligations'][0]['kind'] == 'fix_loop'


def test_parallel_failures_merge_and_order_is_stable():
    tasks = [task(execution_id='generation-1'), task('review', execution_id='generation-1'), task('implementation', execution_id='generation-1', status='committed', stage_verdict='pass')]
    results = [assess_workflow(WF, CFG, list(p))['obligations'] for p in permutations(tasks)]
    assert all(r == results[0] for r in results)
    assert len(results[0]) == 1
    assert results[0][0]['gate_nodes'] == ['review', 'test']
    assert results[0][0]['task_ids'] == ['review-01', 'test-01']
    assert results[0][0]['affected_task_ids'] == ['implementation-01']


@pytest.mark.parametrize('changes,reason', [({'candidate_sha': None}, 'candidate_unknown'),
    ({'candidate_sha': 'short'}, 'candidate_invalid'),
    ({'run_id': None}, 'identity_unknown'), ({'workflow_id': None}, 'identity_unknown')])
def test_unknown_or_stale_keeps_blocker(changes, reason):
    failed = task(**changes)
    result = assess_workflow(WF, CFG, [failed])
    assert result['blockers'] == [failed]
    assert result['obligations'][0]['status'] == 'waiting_human'
    assert result['obligations'][0]['reason'] == reason


@pytest.mark.parametrize('changes', [{'status': 'superseded'}, {'superseded_by': 'next'}, {'workflow_id': 'foreign'}])
def test_historical_and_foreign_excluded(changes):
    assert active_tasks(WF, [task(**changes)]) == []
    assert assess_workflow(WF, CFG, [task(**changes)])['can_advance']


def test_finalize_escalation_is_visible_for_committed():
    failed = task('implementation', status='committed', stage_verdict='pass', finalize_escalated=True)
    result = assess_workflow(WF, CFG, [failed])
    assert result['blockers'] == [failed]
    assert result['obligations'][0]['kind'] == 'finalize'
    assert result['obligations'][0]['status'] == 'waiting_human'


@pytest.mark.parametrize('status', ['closed', 'paused'])
def test_inactive_workflow_has_no_executable_obligations(status):
    result = assess_workflow(dict(WF, status=status), CFG, [task()])
    assert result['blockers']
    assert all(o['status'] == 'waiting_human' for o in result['obligations'])
    assert not result['can_advance']


def test_explicit_gate_false_overrides_default():
    config = {'nodes': [{'id': 'test', 'gate': False}]}
    assert assess_workflow(WF, config, [task(execution_id='generation-1')])['obligations'][0]['reason'] == 'gate_unknown'


def test_custom_gate_and_unknown_retry_node():
    config = {'nodes': [{'id': 'verify', 'gate': {'retry_node': 'build'}}, {'id': 'build'}]}
    assert assess_workflow(WF, config, [task('verify')])['obligations'][0]['retry_node'] == 'build'
    config['nodes'].pop()
    assert assess_workflow(WF, config, [task('verify')])['obligations'][0]['status'] == 'waiting_human'


def test_identity_excludes_lifecycle_activity_but_tracks_verdict_run_candidate_and_generation():
    original = task()
    identity = recovery_identity(WF, [original])
    changed = dict(original, version=20, updated_at=999, status='committed')
    assert recovery_identity(WF, [changed]) == identity
    for key, value in [('stage_verdict_note', 'new'), ('run_id', 'new-run'), ('candidate_sha', 'b' * 40), ('affected_task_ids', ['other'])]:
        assert recovery_identity(WF, [dict(original, **{key: value})]) != identity
    assert recovery_identity(dict(WF, execution_id='new'), [original]) != identity
    assert recovery_identity(dict(WF, candidate_sha='b' * 40), [original]) != identity
    assert recovery_identity(WF, [task('review'), original]) == recovery_identity(WF, [original, task('review')])


def test_no_mutations():
    before = deepcopy((WF, CFG, [task()]))
    assess_workflow(*before)
    assert before == (WF, CFG, [task()])


def test_other_execution_facts_cannot_automate_current_candidate():
    foreign = task(execution_id='other-generation')
    obligation = assess_workflow(WF, CFG, [foreign])['obligations'][0]
    assert obligation['status'] == 'waiting_human'
    assert obligation['reason'] == 'identity_unknown'


def test_explicit_affected_lineage_does_not_expand_to_unrelated_implementations():
    selected = task('implementation', task_id='selected', stage_verdict='pass', status='committed')
    preserved = task('implementation', task_id='preserved', stage_verdict='pass', status='committed')
    failed = task(stage_verdict_affected_task_ids=['selected'])
    assert assess_workflow(WF, CFG, [failed, selected, preserved])['obligations'][0]['affected_task_ids'] == ['selected']


def test_sibling_arrival_reuses_slot_but_changes_fact_identity():
    first = assess_workflow(WF, CFG, [task()])['obligations'][0]
    merged = assess_workflow(WF, CFG, [task(), task('review')])['obligations'][0]
    assert first['identity_key'] == merged['identity_key']
    assert first['identity'] != merged['identity']


def test_affected_run_identity_is_preserved():
    implementation = task('implementation', status='committed', stage_verdict='pass')
    first = assess_workflow(WF, CFG, [task(), implementation])['obligations'][0]
    changed = assess_workflow(WF, CFG, [task(), dict(implementation, run_id='replacement')])['obligations'][0]
    assert first['identity'] != changed['identity']


def test_missing_generation_waits_for_human():
    workflow = dict(WF)
    workflow.pop('execution_id')
    assert assess_workflow(workflow, CFG, [task()])['obligations'][0]['reason'] == 'identity_unknown'


@pytest.mark.parametrize('affected', ['missing', 'implementation-01'])
def test_missing_or_foreign_affected_lineage_waits(affected):
    failed = task(affected_task_ids=[affected])
    implementation = task('implementation', candidate_sha='b' * 40, stage_verdict='pass', status='committed')
    assert assess_workflow(WF, CFG, [failed, implementation])['obligations'][0]['status'] == 'waiting_human'


def test_actual_commit_and_affected_fields_are_bound():
    implementation = task('implementation', execution_id='generation-1', status='committed', stage_verdict='pass')
    implementation.pop('candidate_sha')
    implementation['commit'] = SHA
    failed = task(execution_id='generation-1', stage_verdict_affected_task_ids=['implementation-01'])
    obligation = assess_workflow(WF, CFG, [implementation, failed])['obligations'][0]
    assert obligation['affected_task_ids'] == ['implementation-01']
    assert obligation['status'] == 'pending'


def test_empty_gate_uses_default():
    assert assess_workflow(WF, {'nodes': [{'id': 'test', 'gate': {}}, {'id': 'implementation'}]}, [task(execution_id='generation-1'), task('implementation', execution_id='generation-1', stage_verdict='pass', status='committed')])['obligations'][0]['status'] == 'pending'


@pytest.mark.parametrize('status', ['completed', 'closing', 'abandoned'])
def test_non_running_workflow_waits(status):
    assert assess_workflow(dict(WF, status=status), CFG, [task()])['obligations'][0]['status'] == 'waiting_human'


def test_startup_not_ready_waits():
    assert assess_workflow(dict(WF, startup_ready=False), CFG, [task()])['obligations'][0]['status'] == 'waiting_human'


def test_recovery_payload_only_retains_semantic_fields():
    failed = task(secret_token='private', prompt='private', runtime={'credential': 'private'})
    result = assess_workflow(WF, CFG, [failed, task('implementation', status='committed', stage_verdict='pass')])
    fact = next(f for f in result['obligations'][0]['facts'] if f['task_id'] == failed['task_id'])
    assert not {'secret_token', 'prompt', 'runtime'} & fact.keys()


def test_empty_affected_fix_loop_fails_closed():
    assert assess_workflow(WF, CFG, [task(execution_id='generation-1')])['obligations'][0]['reason'] == 'affected_tasks_unknown'


def test_known_workflow_generation_cannot_adopt_legacy_old_run_same_candidate():
    failed = task(run_id='old-run')
    failed.pop('execution_id', None)
    implementation = task('implementation', status='committed', stage_verdict='pass', execution_id='generation-1')
    result = assess_workflow(WF, CFG, [failed, implementation])
    assert result['blockers'] == [failed]
    assert result['obligations'][0]['status'] == 'waiting_human'
    assert result['obligations'][0]['reason'] == 'identity_unknown'


def test_affected_implementation_missing_generation_cannot_automate():
    failed = task(execution_id='generation-1')
    implementation = task('implementation', status='committed', stage_verdict='pass', run_id='legacy-implementation')
    implementation.pop('execution_id', None)
    assert assess_workflow(WF, CFG, [failed, implementation])['obligations'][0]['reason'] == 'identity_unknown'
