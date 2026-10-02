import importlib
import importlib.util
from herdr.state_store import SQLiteStateStore
import pytest


def api():
    assert importlib.util.find_spec('herdr.delivery_report') is not None
    return importlib.import_module('herdr.delivery_report')


@pytest.fixture
def execution(tmp_path):
    store=SQLiteStateStore(tmp_path/'state.db')
    store.save_workflow({'workflow_id':'wf-report','status':'completed','candidate_sha':'a'*40})
    store.save_task({'task_id':'test-a','workflow_id':'wf-report','run_id':'run-a','status':'agent_done','candidate_sha':'a'*40})
    return store


def record(store, candidate='a'*40, mode='execute', passed=True, run='run-a'):
    return store.record_event('verification_completed',{'verification':{'passed':passed,'candidate_sha':candidate,
        'execution_mode':mode,'counts':{'pass':3,'fail':0,'skip':2},'command':'pytest -q'}},
        workflow_id='wf-report',task_id='test-a',run_id=run,source='trajectory')


def test_missing_evidence_is_unknown_even_completed(execution):
    report=api().build_delivery_report('wf-report',execution)
    assert report['tasks'][0]['verification']['status']=='unknown'
    assert report['production_validation']=='unknown'
    assert report['merge_status']=='unknown'


@pytest.mark.parametrize('candidate,mode,run',[('b'*40,'execute','run-a'),('a'*40,'dry-run','run-a'),('a'*40,'execute','foreign')])
def test_cross_candidate_dryrun_and_crossrun_do_not_pass(execution,candidate,mode,run):
    record(execution,candidate,mode,run=run)
    report=api().build_delivery_report('wf-report',execution)
    assert report['tasks'][0]['verification']['status']!='pass'


def test_bound_real_verification_preserves_skip_and_failed_gate(execution):
    record(execution,passed=False)
    report=api().build_delivery_report('wf-report',execution)
    fact=report['tasks'][0]['verification']
    assert fact['status']=='fail' and fact['counts']=={'pass':3,'fail':0,'skip':2}
    assert fact['command']=='pytest -q'
    assert report['all_verifications_passed'] is False


def test_cli_report_reads_real_store_without_mutation(execution,tmp_path):
    import json,os,subprocess
    from pathlib import Path
    result=subprocess.run([str(Path(__file__).resolve().parents[1]/'bin/herdr-task'),'delivery-report','wf-report'],
        env={**os.environ,'HERDR_STATE_DB':str(execution.db_path),'HERDR_CONTROLLER_DIR':str(tmp_path)},capture_output=True,text=True)
    assert result.returncode==0,result.stderr+result.stdout
    assert json.loads(result.stdout)['workflow_status']=='completed'
    assert execution.get_task('test-a')['status']=='agent_done'


def test_changed_registered_artifact_invalidates_pass(execution,tmp_path):
    from herdr.observation import ObservationStore
    evidence=ObservationStore(execution.db_path).create(
        run_id='run-a',task_id='test-a',workflow_id='wf-report',source_type='verification',source_ref='pytest',content='3 passed')
    execution.update_task_metadata('test-a',{'verified_candidate_sha':'a'*40})
    execution.record_event('verification_completed',{'verification':{'passed':True,'candidate_sha':'a'*40,
        'execution_mode':'execute','observation_id':evidence.observation_id}},workflow_id='wf-report',task_id='test-a',run_id='run-a',source='trajectory')
    assert api().build_delivery_report('wf-report',execution)['tasks'][0]['verification']['status']=='pass'
    from pathlib import Path
    Path(evidence.content_ref).write_text('changed')
    assert api().build_delivery_report('wf-report',execution)['tasks'][0]['verification']['status']=='unknown'


def test_new_epoch_does_not_reuse_old_pass(execution):
    from herdr.observation import ObservationStore
    execution.update_task_metadata('test-a',{'completion_protocol':'receipt-v1','completion_epoch':'epoch-a','verified_candidate_sha':'a'*40})
    evidence=ObservationStore(execution.db_path).create(run_id='run-a',task_id='test-a',workflow_id='wf-report',
        source_type='verification',source_ref='pytest-epoch',content='passed',metadata={'epoch':'epoch-a'})
    execution.record_event('verification_completed',{'verification':{'passed':True,'candidate_sha':'a'*40,'epoch':'epoch-a',
        'execution_mode':'execute','observation_id':evidence.observation_id}},workflow_id='wf-report',task_id='test-a',run_id='run-a',source='trajectory')
    assert api().build_delivery_report('wf-report',execution)['tasks'][0]['verification']['status']=='pass'
    execution.update_task_metadata('test-a',{'completion_epoch':'epoch-b'})
    assert api().build_delivery_report('wf-report',execution)['tasks'][0]['verification']['status']=='unknown'


def test_large_history_is_bounded_and_cannot_claim_all_passed(execution):
    from herdr import state_db
    with state_db.get_db_connection(execution.db_path) as conn:
        for number in range(1001):
            state_db.save_task({'task_id':f'history-{number:04d}','workflow_id':'wf-report',
                                'status':'cleaned'},conn=conn)
    report=api().build_delivery_report('wf-report',execution)
    assert len(report['tasks']) == 1000
    assert report['tasks_truncated'] is True
    assert report['all_verifications_passed'] is False
