"""Recovery completion needs new-candidate evidence, never a sent notification."""
from copy import deepcopy

from herdr.workflow_recovery import result_status, successor_launch_command, repair_coverage

A, B = 'a' * 40, 'b' * 40
WF = {'workflow_id': 'wf', 'status': 'running', 'execution_id': 'gen', 'candidate_sha': B}
OP = {'workflow_id': 'wf', 'payload': {'candidate_sha': A, 'kind': 'fix_loop', 'affected_task_ids': ['impl']},
      'detail': {'successor_ids': ['impl-r2'], 'gate_nodes': ['test', 'review'], 'execution_id': 'gen', 'target_runs': {'impl-r2': 'new'}, 'source_runs': {'impl': 'old'}, 'repair_map': {'impl': {'kind': 'successor', 'task_id': 'impl-r2', 'run_id': 'new', 'source_run_id': 'old'}}}}


def facts():
    return [{'task_id': 'impl-r2', 'workflow_id': 'wf', 'node': 'implementation',
             'status': 'integrated', 'execution_id': 'gen', 'run_id': 'new', 'commit': B, 'supersedes': 'impl',
             'recovery_lineage': {'predecessor_id': 'impl', 'successor_id': 'impl-r2', 'candidate_sha': A,
                                  'predecessor_run_id': 'old', 'successor_run_id': 'new'}},
            {'task_id': 'impl', 'workflow_id': 'wf', 'execution_id': 'gen', 'run_id': 'old',
             'commit': A, 'status': 'committed', 'superseded_by': 'impl-r2'},
            *[{'task_id': g + '-new', 'workflow_id': 'wf', 'node': g, 'status': 'cleaned',
               'stage_verdict': 'pass', 'candidate_sha': B, 'verified_candidate_sha': B,
               'execution_id': 'gen', 'run_id': g + '-run'} for g in ['test', 'review']]]


def test_new_candidate_and_all_fresh_gates_resolve():
    assert result_status(OP, WF, facts())[0] == 'resolved'


def test_old_or_missing_verifier_does_not_resolve():
    tasks = facts()
    tasks[-1]['candidate_sha'] = A
    tasks[-1]['verified_candidate_sha'] = A
    assert result_status(OP, WF, tasks) is None
    assert result_status(OP, WF, tasks[:-1]) is None
    assert result_status(OP, dict(WF, candidate_sha=A), facts()) is None


def test_failed_successor_preserves_human_obligation():
    tasks = facts()
    tasks[0]['status'] = 'failed'
    assert result_status(OP, WF, tasks)[0] == 'waiting_human'


def test_mixed_successor_and_rework_cannot_ignore_failed_rework():
    operation = deepcopy(OP)
    operation['detail']['rework_ids'] = ['impl-rework']
    tasks = facts() + [{'task_id': 'impl-rework', 'workflow_id': 'wf', 'status': 'failed'}]
    assert result_status(operation, WF, tasks)[0] == 'waiting_human'


def test_closed_or_wrong_generation_cannot_accept_new_result():
    assert result_status(OP, dict(WF, status='completed'), facts()) is None
    assert result_status(OP, dict(WF, execution_id='other'), facts())[0] == 'waiting_human'


def test_successor_launch_is_pinned_distinct_and_formal():
    old = {'task_id': 'impl', 'node': 'implementation', 'commit': A,
           'dispatch_role': 'worker', 'dispatch_round': 1, 'goal': 'repair'}
    cmd = successor_launch_command('/cli', WF, old, 'impl-r2', '/source', 'fix only agreed defects')
    assert cmd[:2] == ['/cli', 'launch']
    assert cmd[cmd.index('--candidate-sha') + 1] == A
    assert cmd[cmd.index('--supersedes') + 1] == 'impl'
    assert '--onto' not in cmd
    assert cmd[cmd.index('--dispatch-round') + 1] == '2'
    assert cmd[cmd.index('--execution-id') + 1] == 'gen'


def test_completed_git_repair_cannot_resolve():
    operation = deepcopy(OP)
    operation['detail']['target_runs'] = {'impl-r2': 'new'}
    tasks = facts()
    tasks[0]['status'] = 'completed'
    assert result_status(operation, WF, tasks) is None


def test_missing_or_changed_target_run_waits_for_human():
    missing = deepcopy(OP)
    missing['detail'].pop('target_runs')
    assert result_status(missing, WF, facts())[0] == 'waiting_human'
    operation = deepcopy(OP)
    operation['detail']['target_runs'] = {'impl-r2': 'old'}
    assert result_status(operation, WF, facts())[0] == 'waiting_human'


def test_execution_receipt_has_bounded_result_polling(monkeypatch):
    from herdr import workflow_recovery as recovery
    operation = {'id': 1, 'version': 1, 'status': 'pending'}
    class Store:
        db_path = 'unused'
    calls = []
    monkeypatch.setattr(recovery.recovery_store, 'reconcile', lambda *a, **kw: [operation])
    monkeypatch.setattr(recovery.recovery_store, 'claim_operation', lambda *a, **kw: operation)
    monkeypatch.setattr(recovery.recovery_store, 'finish_operation', lambda *args: calls.append(args))
    monkeypatch.setattr(recovery.recovery_store, 'list_operations', lambda *a: [])
    recovery.drive_recovery(Store(), 'wf', lambda *args: ('awaiting_result', {'target_runs': {'impl': 'run'}}), now=100)
    detail = calls[0][4]
    assert detail['result_deadline_at'] == 3700
    assert detail['next_due_at'] == 130


def test_partial_original_repair_cannot_cover_second_failed_target():
    operation = deepcopy(OP)
    operation['payload']['affected_task_ids'].append('old2')
    assert repair_coverage(operation, facts()) == ['old2']
    result = result_status(operation, WF, facts())
    assert result[0] == 'waiting_human'
    assert result[1]['missing_task_ids'] == ['old2']


def test_valid_mixed_successor_and_delivered_rework_covers_every_original():
    operation = deepcopy(OP)
    operation['payload']['affected_task_ids'].append('old2')
    operation['detail']['repair_map']['old2'] = {'kind': 'rework', 'task_id': 'old2', 'run_id': 'old2-run', 'source_run_id': 'old2-run', 'request_id': 'old2-request', 'completion_epoch': None, 'completion_identity_path': None}
    operation['detail']['source_runs']['old2'] = 'old2-run'
    operation['detail']['rework_requests'] = {'old2': 'old2-request'}
    operation['detail']['rework_ids'] = ['old2']
    operation['detail']['target_runs']['old2'] = 'old2-run'
    tasks = facts() + [{'task_id': 'old2', 'workflow_id': 'wf', 'execution_id': 'gen', 'run_id': 'old2-run',
                        'status': 'integrated', 'commit': B, 'rework_delivery': 'delivered', 'rework_request_id': 'old2-request', 'completion_epoch': None, 'completion_identity_path': None}]
    assert repair_coverage(operation, tasks) == []
    assert result_status(operation, WF, tasks)[0] == 'resolved'


def test_forged_mapping_without_gateway_lineage_is_missing():
    tasks = facts()
    tasks[0].pop('recovery_lineage')
    assert repair_coverage(OP, tasks) == ['impl']


def test_source_or_target_run_rotation_invalidates_coverage():
    for index in [0, 1]:
        tasks = facts()
        tasks[index]['run_id'] = 'rotated'
        assert repair_coverage(OP, tasks) == ['impl']


def test_extra_receipt_target_cannot_escape_mapping():
    operation = deepcopy(OP)
    operation['detail']['rework_ids'] = ['extra']
    assert result_status(operation, WF, facts())[0] == 'waiting_human'


def test_unknown_empty_original_targets_fail_closed():
    operation = deepcopy(OP)
    operation['payload']['affected_task_ids'] = []
    assert repair_coverage(operation, facts())
    assert result_status(operation, WF, facts())[0] == 'waiting_human'


def test_mapping_without_pre_effect_source_run_receipt_cannot_cover():
    operation = deepcopy(OP)
    operation['detail'].pop('source_runs')
    assert repair_coverage(operation, facts()) == ['impl']


def test_finalize_mapping_covers_original_but_requires_real_delivery_to_settle():
    operation = {'workflow_id': 'wf', 'payload': {'kind': 'finalize', 'candidate_sha': A, 'task_ids': ['impl']},
                 'detail': {'execution_id': 'gen', 'rework_ids': ['impl'], 'target_runs': {'impl': 'old'},
                            'source_runs': {'impl': 'old'},
                            'repair_map': {'impl': {'kind': 'finalize', 'task_id': 'impl', 'run_id': 'old', 'source_run_id': 'old'}}}}
    tasks = [{'task_id': 'impl', 'workflow_id': 'wf', 'execution_id': 'gen', 'run_id': 'old', 'status': 'committed'}]
    assert repair_coverage(operation, tasks) == []
    assert result_status(operation, WF, tasks) is None
    tasks[0]['status'] = 'integrated'
    assert result_status(operation, WF, tasks)[0] == 'resolved'


def test_unconfirmed_rework_or_wrong_bidirectional_link_fails_coverage():
    tasks = facts()
    tasks[1]['superseded_by'] = 'other'
    assert repair_coverage(OP, tasks) == ['impl']
    operation = deepcopy(OP)
    operation['detail']['repair_map']['impl'] = {'kind': 'rework', 'task_id': 'impl', 'run_id': 'old', 'source_run_id': 'old'}
    tasks[1].pop('superseded_by')
    assert repair_coverage(operation, tasks) == ['impl']


def rework_receipt():
    operation = {'workflow_id': 'wf', 'payload': {'kind': 'fix_loop', 'candidate_sha': A, 'affected_task_ids': ['old']},
                 'detail': {'execution_id': 'gen', 'source_runs': {'old': 'old-run'},
                            'rework_requests': {'old': 'request-new'},
                            'repair_map': {'old': {'kind': 'rework', 'task_id': 'old', 'run_id': 'old-run',
                              'source_run_id': 'old-run', 'request_id': 'request-new',
                              'completion_epoch': 2, 'completion_identity_path': '/receipt/current'}}}}
    tasks = [{'task_id': 'old', 'workflow_id': 'wf', 'execution_id': 'gen', 'run_id': 'old-run',
              'rework_delivery': 'delivered', 'rework_request_id': 'request-new',
              'completion_protocol': 'receipt-v1', 'completion_epoch': 2,
              'completion_identity_path': '/receipt/current'}]
    return operation, tasks


def test_same_run_old_rework_request_cannot_cover_current_request():
    operation, tasks = rework_receipt()
    tasks[0]['rework_request_id'] = 'request-old'
    assert repair_coverage(operation, tasks) == ['old']


def test_missing_durable_rework_request_cannot_cover():
    operation, tasks = rework_receipt()
    operation['detail'].pop('rework_requests')
    assert repair_coverage(operation, tasks) == ['old']


def test_rework_completion_epoch_and_path_must_match_current_receipt():
    for key, value in [('completion_epoch', 3), ('completion_identity_path', '/receipt/other')]:
        operation, tasks = rework_receipt()
        tasks[0][key] = value
        assert repair_coverage(operation, tasks) == ['old']


def test_receipt_v1_requires_known_completion_identity():
    operation, tasks = rework_receipt()
    operation['detail']['repair_map']['old']['completion_epoch'] = None
    tasks[0]['completion_epoch'] = None
    assert repair_coverage(operation, tasks) == ['old']


def test_current_rework_receipt_and_explicit_legacy_identity_cover():
    operation, tasks = rework_receipt()
    assert repair_coverage(operation, tasks) == []
    tasks[0].pop('completion_protocol')
    for key in ['completion_epoch', 'completion_identity_path']:
        tasks[0][key] = None
        operation['detail']['repair_map']['old'][key] = None
    assert repair_coverage(operation, tasks) == []
    operation['detail']['repair_map']['old'].pop('completion_epoch')
    assert repair_coverage(operation, tasks) == ['old']
