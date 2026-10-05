import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from herdr.state_store import get_state_store
from herdr.task_resources import begin_launch_intent

SHA = 'a' * 40

@pytest.fixture
def scene(tmp_path):
    store = get_state_store(tmp_path / 'state.db')
    store.save_workflow(dict(workflow_id='wf', status='active', execution_id='execution'))
    store.save_task(dict(task_id='old', workflow_id='wf', node='n', status='committed',
                         run_id='old-run', commit=SHA, dispatch_role='worker', dispatch_round=1))
    def successor(task_id='new', **changes):
        task = dict(task_id=task_id, workflow_id='wf', node='n', status='working',
                    run_id=task_id+'-run', execution_id='execution',
                    dispatch_role='worker', dispatch_round=2, candidate_sha=SHA,
                    completion_epoch=1, completion_identity_path='/receipt/'+task_id)
        claim = begin_launch_intent(store, workflow_id='wf', node_id='n', role='worker',
                                    candidate_sha=SHA, dispatch_round=2, task_id=task_id, supersedes='old')
        # Competing task records can arise before recovery; each still needs a valid intent.
        intent = claim.get('intent')
        if not intent:
            intent = dict(key='launch:'+task_id, intent_id=task_id+'-intent', workflow_id='wf',
                          node_id='n', role='worker', candidate_sha=SHA, dispatch_round=2,
                          task_id=task_id, supersedes='old', resources={})
        task['launch_intent_id']=intent['intent_id']
        task.update(changes)
        store.save_task(task)
        store.record_event('launch_intent',dict(intent,phase='registered',resources={'run_id':task['run_id']}),
                           workflow_id='wf',node_id='n',task_id=intent['key'],source='launch')
        store.record_event('initial_dispatched',dict(delivery_phase='dispatched',completion_epoch=1,
                            identity_path='/receipt/'+task_id, intervention_id='initial:'+task_id),
                           workflow_id='wf',node_id='n',task_id=task_id,run_id=task['run_id'],source='herdr-task')
        return task
    return store, successor

def link(store, successor='new', version=None):
    from herdr.recovery_successor import link_committed_successor
    return link_committed_successor(store,'old',successor,
                                    store.get_task('old')['version'] if version is None else version,SHA,'recovery')

def test_preserves_history_and_idempotent(scene):
    store, successor=scene; successor()
    version=store.get_task('old')['version']
    assert link(store)['ok']
    assert link(store,version=version)['already_applied']
    old=store.get_task('old'); new=store.get_task('new')
    assert old['status']=='committed' and old['commit']==SHA
    assert old['superseded_by']=='new' and new['supersedes']=='old'
    assert new['recovery_lineage']['candidate_sha']==SHA
    assert len(store.list_events(event_type='committed_successor_linked'))==1

@pytest.mark.parametrize('changes',[{'dispatch_role':'reviewer'},{'dispatch_round':3},{'candidate_sha':'b'*40},
                                    {'workflow_id':'other'},{'run_id':'old-run'},{'execution_id':''}])
def test_identity_rejections(scene,changes):
    store, successor=scene; successor(**changes)
    with pytest.raises(ValueError): link(store)
    assert not store.get_task('old').get('superseded_by')

@pytest.mark.parametrize('remove', ['initial_dispatched','launch_intent'])
def test_missing_evidence_rejected(scene,remove):
    store, successor=scene; successor()
    from herdr import state_db
    conn=state_db.get_db_connection(store.db_path)
    conn.execute('DELETE FROM events WHERE event_type=?',(remove,)); conn.commit(); conn.close()
    with pytest.raises(ValueError): link(store)

def test_stale_version_rejected(scene):
    store, successor=scene; successor()
    with pytest.raises(ValueError): link(store,version=-1)

def test_event_failure_rolls_back_both_metadata(scene,monkeypatch):
    store, successor=scene; successor()
    from herdr import state_db
    original=state_db.record_event
    def fail(event,**kwargs):
        if event['event_type']=='committed_successor_linked': raise RuntimeError('disk full')
        return original(event,**kwargs)
    monkeypatch.setattr(state_db,'record_event',fail)
    with pytest.raises(RuntimeError): link(store)
    assert not store.get_task('old').get('superseded_by')
    assert not store.get_task('new').get('supersedes')

def test_competing_connections_only_one_links(scene):
    store, successor=scene; successor('new'); successor('other')
    barrier=threading.Barrier(2); version=store.get_task('old')['version']
    def attempt(task_id):
        barrier.wait()
        try: return link(store,task_id,version)['ok']
        except ValueError: return False
    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(attempt,['new','other']))==1
    assert len(store.list_events(event_type='committed_successor_linked'))==1

@pytest.mark.parametrize('event_changes,payload_changes', [
    ({'run_id':'foreign'}, {}), ({'source':'supervisor'}, {}),
    ({'workflow_id':'foreign'}, {}), ({'node_id':'foreign'}, {}),
    ({}, {'delivery_phase':'transport_started'}), ({}, {'completion_epoch':2}),
    ({}, {'identity_path':'/wrong'}), ({}, {'intervention_id':None}),
])
def test_delivery_receipt_identity_is_exact(scene,event_changes,payload_changes):
    store, successor=scene; task=successor()
    from herdr.recovery_successor import delivery_confirmed
    receipt=store.list_events(task_id='new',event_type='initial_dispatched')[0]
    assert delivery_confirmed(task,[receipt])
    receipt.update(event_changes); receipt['payload'].update(payload_changes)
    assert not delivery_confirmed(task,[receipt])


def test_conflicting_successor_cannot_overwrite(scene):
    store, successor=scene; successor('new'); successor('other')
    link(store,'new')
    with pytest.raises(ValueError): link(store,'other')
    assert store.get_task('old')['superseded_by']=='new'
    assert not store.get_task('other').get('supersedes')


def test_registered_task_without_dispatch_is_not_delivery(scene):
    store, successor=scene; task=successor()
    from herdr.recovery_successor import delivery_confirmed
    assert not delivery_confirmed(task)
    assert not delivery_confirmed(task,store.list_events(event_type='launch_intent'))

@pytest.mark.parametrize('target',['new','old','workflow'])
def test_execution_identity_is_workflow_authority(scene,target):
    store, successor=scene; successor()
    if target=='workflow':
        wf=store.get_workflow('wf'); wf['execution_id']='other'; store.save_workflow(wf)
    else:
        store.update_task_metadata(target,{'execution_id':'other'})
    with pytest.raises(ValueError,match='execution identity'): link(store)


def _cli_module():
    import importlib.machinery
    import importlib.util
    from pathlib import Path
    loader=importlib.machinery.SourceFileLoader('recovery_launch_cli',str(Path(__file__).parents[1]/'bin/herdr-task'))
    spec=importlib.util.spec_from_loader(loader.name,loader)
    module=importlib.util.module_from_spec(spec); loader.exec_module(module)
    return module


def test_real_launch_finish_helper_requires_delivery_and_replays(scene):
    store, successor=scene; task=successor()
    old=store.get_task('old')
    store.update_task_metadata('new',dict(recovery_predecessor='old',expected_predecessor_version=old['version'],
                                         expected_predecessor_sha=SHA))
    cli=_cli_module()
    assert cli._finish_committed_recovery_launch(store,store.get_task('new'),old,'recover')['ok']
    assert cli._finish_committed_recovery_launch(store,store.get_task('new'),store.get_task('old'),'recover')['already_applied']


def test_real_launch_finish_helper_registered_only_exits_two(scene):
    store, successor=scene; task=successor()
    from herdr import state_db
    conn=state_db.get_db_connection(store.db_path)
    conn.execute("DELETE FROM events WHERE event_type='initial_dispatched'"); conn.commit(); conn.close()
    with pytest.raises(SystemExit) as error:
        _cli_module()._finish_committed_recovery_launch(store,task,store.get_task('old'),'recover')
    assert error.value.code==2
    assert not store.get_task('old').get('superseded_by')

@pytest.mark.parametrize('status',['agent_done','completed','committed','integrated','cleanup_ready','cleaned'])
def test_delivered_successor_may_finish_before_lineage_link(scene,status):
    store, successor=scene; successor(status=status)
    assert link(store)['ok']
    assert link(store)['already_applied']
    assert store.get_task('old')['status']=='committed'
    assert store.get_task('new')['status']==status

@pytest.mark.parametrize('status',['failed','blocked','superseded'])
def test_delivery_receipt_does_not_authorize_failed_successor(scene,status):
    store, successor=scene; successor(status=status)
    with pytest.raises(ValueError): link(store)
    assert not store.get_task('old').get('superseded_by')


@pytest.mark.parametrize('change', [None, 'request', 'epoch', 'path', 'run', 'missing'])
def test_current_rework_receipt_identity(scene, change):
    from herdr.recovery_successor import has_confirmed_rework
    store, successor = scene
    task = successor(completion_protocol='receipt-v1', rework_delivery='delivered', rework_request_id='current-request')
    body = dict(delivery_phase='dispatched', intervention_id='current-request',
                completion_epoch=1, identity_path='/receipt/new')
    if change == 'request': body['intervention_id'] = 'previous-request'
    if change == 'epoch': body['completion_epoch'] = 0
    if change == 'path': body['identity_path'] = '/previous'
    if change != 'missing':
        store.record_event('rework_dispatched', body, workflow_id='wf', node_id='n',
                           task_id='new', run_id='previous-run' if change == 'run' else task['run_id'], source='herdr-task')
    assert has_confirmed_rework(store, task, 'current-request') is (change is None)
    assert not has_confirmed_rework(store, task, 'previous-request')


def _git(repo, *args):
    import subprocess
    return subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True,text=True,timeout=10).stdout.strip()


@pytest.fixture
def local_candidate(tmp_path):
    import subprocess
    project=tmp_path/'project'; project.mkdir()
    _git(project,'init','-b','main'); _git(project,'config','user.email','test@example.com'); _git(project,'config','user.name','Test')
    (project/'code.txt').write_text('C\n'); _git(project,'add','code.txt'); _git(project,'commit','-m','C')
    initial=_git(project,'rev-parse','HEAD')
    predecessor=tmp_path/'predecessor'
    subprocess.run(['git','clone','--no-hardlinks',str(project),str(predecessor)],check=True,capture_output=True,timeout=10)
    _git(predecessor,'config','user.email','test@example.com'); _git(predecessor,'config','user.name','Test')
    (predecessor/'code.txt').write_text('A\n'); _git(predecessor,'add','code.txt'); _git(predecessor,'commit','-m','local A')
    candidate=_git(predecessor,'rev-parse','HEAD')
    return project, predecessor, initial, candidate


def test_local_unpushed_candidate_uses_registered_source_and_real_worker(local_candidate,tmp_path,monkeypatch):
    import importlib
    from herdr.recovery_successor import choose_recovery_source
    project, predecessor, initial, candidate=local_candidate
    worker=importlib.import_module('services.herdr-worker')
    monkeypatch.setattr(worker,'CLONE_ROOT',tmp_path/'clones')
    monkeypatch.setattr(worker,'_registered_tasks',lambda: [])
    wrong=worker.create_clone(project,'wrong-source')
    with pytest.raises(RuntimeError,match='unavailable'):
        worker.create_task_branch(wrong,'wrong-source','test','impl','main',candidate_sha=candidate)
    chosen=choose_recovery_source({'commit':candidate,'clone_path':str(predecessor)},str(project))
    assert chosen==str(predecessor.resolve())
    clone=worker.create_clone(chosen,'successor')
    worker.create_task_branch(clone,'successor','test','impl','main',candidate_sha=candidate)
    assert _git(clone,'rev-parse','HEAD')==candidate
    assert (clone/'.git').is_dir()
    assert _git(clone,'rev-parse','--absolute-git-dir')!=_git(predecessor,'rev-parse','--absolute-git-dir')
    assert _git(predecessor,'rev-parse','HEAD')==candidate
    assert _git(project,'rev-parse','HEAD')==initial
    assert _git(project,'status','--porcelain')==_git(predecessor,'status','--porcelain')==''


def test_available_project_candidate_is_preferred(local_candidate):
    from herdr.recovery_successor import choose_recovery_source
    _, predecessor, _, candidate=local_candidate
    assert choose_recovery_source({'commit':candidate,'clone_path':'/missing'},predecessor)==str(predecessor.resolve())


def test_source_wip_requires_decision(local_candidate):
    from herdr.recovery_successor import choose_recovery_source
    project, predecessor, _, candidate=local_candidate
    (predecessor/'code.txt').write_text('WIP\n')
    with pytest.raises(ValueError,match='source_wip_requires_decision'):
        choose_recovery_source({'commit':candidate,'clone_path':str(predecessor)},project)
    assert (predecessor/'code.txt').read_text()=='WIP\n'

@pytest.mark.parametrize('commit',['short','b'*40])
def test_source_candidate_unavailable(local_candidate,commit):
    from herdr.recovery_successor import choose_recovery_source
    project, predecessor, _, _=local_candidate
    with pytest.raises(ValueError,match='source_candidate_unavailable'):
        choose_recovery_source({'commit':commit,'clone_path':str(predecessor)},project)

@pytest.mark.parametrize('failure',['timeout','oserror'])
def test_recovery_source_probe_is_bounded_and_refuses_unknown(tmp_path,monkeypatch,failure):
    import subprocess
    from herdr.recovery_successor import choose_recovery_source
    calls=[]
    def unavailable(command,**kwargs):
        calls.append(command)
        assert kwargs['timeout']==10
        if failure=='timeout': raise subprocess.TimeoutExpired(command,10)
        raise OSError('git unavailable')
    monkeypatch.setattr(subprocess,'run',unavailable)
    with pytest.raises(ValueError,match='source_candidate_unavailable'):
        choose_recovery_source({'commit':SHA,'clone_path':str(tmp_path/'old')},tmp_path/'project')
    assert len(calls)==2
