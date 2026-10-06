import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from herdr.state_store import SQLiteStateStore


def test_node_config_cli_updates_only_owned_workflow_and_records_audit(tmp_path):
    cfg=tmp_path/'workflow.json';cfg.write_text(json.dumps({'nodes':[{'id':'implementation'}]}))
    before=cfg.read_bytes();sha=hashlib.sha256(before).hexdigest()
    store=SQLiteStateStore(tmp_path/'state.db')
    for wid in ('wf','other'):
        store.save_workflow({'workflow_id':wid,'status':'running','workflow_file':str(cfg)})
    store.save_task({'task_id':'owned','workflow_id':'wf','node':'implementation','status':'integrated'})
    cli=Path(__file__).resolve().parents[1]/'bin/herdr-task'
    env={**os.environ,'HERDR_STATE_DB':str(store.db_path)}
    args=[sys.executable,str(cli),'node-config-set','wf','implementation','--expected-sha',sha,
          '--required-task-id','owned','--reason','Repair manifest']
    result=subprocess.run(args,env=env,text=True,capture_output=True)
    assert result.returncode==0,result.stdout+result.stderr
    changed=store.get_workflow('wf')['workflow_file']
    assert changed!=str(cfg) and json.loads(Path(changed).read_text())['nodes'][0]['required_task_ids']==['owned']
    assert cfg.read_bytes()==before and store.get_workflow('other')['workflow_file']==str(cfg)
    assert store.list_events(workflow_id='wf',event_type='node_config_updated')
    result=subprocess.run(args,env=env,text=True,capture_output=True)
    assert result.returncode==2 and 'changed' in result.stderr


def test_node_config_rejects_foreign_task_before_any_write(tmp_path):
    from herdr.node_config import update_required_tasks
    cfg=tmp_path/'workflow.json';cfg.write_text(json.dumps({'nodes':[{'id':'implementation'}]}))
    store=SQLiteStateStore(tmp_path/'state.db')
    store.save_workflow({'workflow_id':'wf','status':'running','workflow_file':str(cfg)})
    store.save_task({'task_id':'foreign','workflow_id':'other','node':'implementation','status':'cleaned'})
    with pytest.raises(ValueError,match='out_of_workflow'):
        update_required_tasks(store,'wf','implementation',['foreign'],expected_sha=hashlib.sha256(cfg.read_bytes()).hexdigest(),reason='Repair')
    assert store.get_workflow('wf')['workflow_file']==str(cfg)
    assert not store.list_events(workflow_id='wf',event_type='node_config_updated')


def test_graph_does_not_hide_required_task_failure_or_claim_single_parallel_stage():
    from herdr.workflow_graph import workflow_graph_projection
    wf={'workflow_id':'wf','nodes':[{'id':'impl','required_task_ids':['foreign']},
        {'id':'review','depends_on':[]},{'id':'test','depends_on':[]}]}
    tasks=[{'task_id':'owned','workflow_id':'wf','node':'impl','status':'integrated'},
           {'task_id':'r','workflow_id':'wf','node':'review','status':'working'},
           {'task_id':'t','workflow_id':'wf','node':'test','status':'working'}]
    graph=workflow_graph_projection(wf,tasks,all_tasks=tasks+[
        {'task_id':'foreign','workflow_id':'other','node':'impl','status':'cleaned'}])
    impl=next(n for n in graph['nodes'] if n['id']=='impl')
    assert impl['status']!='completed'
    assert impl['completion_issues'][0]['reason']=='required_task_out_of_workflow'
    assert set(graph['current_nodes'])=={'review','test'}
    assert graph['current_stage']=='' and graph['current_stage_source']=='derived_nodes'


def test_independent_cli_updates_are_serialized(tmp_path):
    cfg=tmp_path/'workflow.json';cfg.write_text(json.dumps({'nodes':[{'id':'implementation'}]}))
    store=SQLiteStateStore(tmp_path/'state.db');store.save_workflow({'workflow_id':'wf','status':'running','workflow_file':str(cfg)})
    args=[sys.executable,str(Path(__file__).resolve().parents[1]/'bin/herdr-task'),'node-config-set','wf','implementation',
          '--expected-sha',hashlib.sha256(cfg.read_bytes()).hexdigest(),'--required-task-id','future','--reason','planned future task']
    env={**os.environ,'HERDR_STATE_DB':str(store.db_path)}
    processes=[subprocess.Popen(args,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
    results=[(p.communicate(timeout=30),p.returncode) for p in processes]
    assert sorted(code for _,code in results)==[0,2]
    assert len(store.list_events(workflow_id='wf',event_type='node_config_updated'))==1


def test_browser_checklist_does_not_claim_unblocked_when_manifest_blocks():
    """Execute the real render helper, preserving its reason and severity."""
    source=(Path(__file__).resolve().parents[1]/'console/herdr_factory_console.py').read_text()
    start=source.index('function flowChecklist(node){')
    end=source.index('function flowOverviewHtml(',start)
    script="const flowGraphData=()=>({nodes:[]});const cleanStageLabel=x=>x;\n"+source[start:end]
    script+="\nconsole.log(JSON.stringify(flowChecklist({status:'blocked',completion_issues:[{reason:'required_task_out_of_workflow',task_id:'foreign'}]})));"
    result=subprocess.run(['node','-e',script],capture_output=True,text=True,check=True)
    items=json.loads(result.stdout)
    assert any(item[0]=='bad' and 'foreign' in item[1] for item in items)
    assert not any(item[0]=='ok' and item[1]=='无失败、无阻塞' for item in items)


def test_real_runtime_repair_preserves_content_addressed_config_and_audit(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from herdr import projects
    from herdr.node_config import update_required_tasks,read_configuration
    cfg=tmp_path/'shared.json'
    cfg.write_text(json.dumps({'workspace_id':'workspace','project_root':str(tmp_path),
                              'nodes':[{'id':'implementation','tab_id':'old-tab','anchor_pane_id':'old-pane'}]}))
    shared=cfg.read_bytes();store=SQLiteStateStore(tmp_path/'state.db')
    for wid in ['wf','other']:
        store.save_workflow({'workflow_id':wid,'status':'running','workflow_file':str(cfg)})
    monkeypatch.setattr(projects,'_get_store',lambda:store)
    update=update_required_tasks(store,'wf','implementation',['future'],expected_sha=hashlib.sha256(shared).hexdigest(),reason='required output')
    path=Path(update['workflow_file']);snapshot=path.read_bytes()
    monkeypatch.setattr(projects,'_workspace_alive',lambda _:True)
    monkeypatch.setattr(projects,'_run',lambda *a,**kw: SimpleNamespace(returncode=1))
    monkeypatch.setattr(projects,'_run_json',lambda argv: {'result':{'tab':{'tab_id':'new-tab'},'root_pane':{'pane_id':'new-pane'}}})
    repaired=projects.ensure_node_runtime('wf','implementation')
    assert repaired['tab_id']=='new-tab'
    assert path.read_bytes()==snapshot,'runtime repair overwrote immutable audited configuration'
    assert read_configuration(store,'wf')[2]==update['config_sha']
    assert cfg.read_bytes()==shared and store.get_workflow('other')['workflow_file']==str(cfg)
    view=projects.workflow_config_for('wf')
    node=next(n for n in view['nodes'] if n['id']=='implementation')
    assert node['tab_id']=='new-tab' and node['anchor_pane_id']=='new-pane'
    assert node['required_task_ids']==['future']
    assert store.get_workflow('wf')['node_runtime']['implementation']['tab_id']=='new-tab'
    assert len(store.list_events(event_type='node_config_updated',workflow_id='wf'))==1
    assert store.list_events(event_type='node_runtime_updated',workflow_id='wf')
    monkeypatch.setattr(projects,'_run',lambda *a,**kw: SimpleNamespace(returncode=0))
    again=projects.ensure_node_runtime('wf','implementation')
    assert again['tab_id']=='new-tab' and again['anchor_pane_id']=='new-pane'
    assert len(store.list_events(event_type='node_runtime_updated',workflow_id='wf'))==1
    second=update_required_tasks(store,'wf','implementation',['next-future'],expected_sha=update['config_sha'],reason='next required output')
    assert second['before_sha']==update['config_sha']
    assert path.read_bytes()==snapshot
    assert projects.workflow_config_for('wf')['nodes'][0]['tab_id']=='new-tab'


from tests.test_dispatch_idempotency import launch_transport_scene


def test_actual_launch_topology_preserves_snapshot_and_projects_runtime_readers(launch_transport_scene,tmp_path,monkeypatch):
    from types import SimpleNamespace
    from herdr import projects,topology,pane_pool,task_resources
    from herdr.node_config import update_required_tasks
    cli,store,args=launch_transport_scene
    cfg=tmp_path/'workflow.json';cfg.write_text(json.dumps({'workspace_id':'w','project_root':str(tmp_path),
        'nodes':[{'id':'n','label':'Node','tab_id':'old-tab','anchor_pane_id':'old-anchor','default_integration_mode':'none'}]}))
    record=store.get_workflow('wf');record['workflow_file']=str(cfg);record['workspace_id']='w';record['config']={};store.save_workflow(record)
    update=update_required_tasks(store,'wf','n',[],expected_sha=hashlib.sha256(cfg.read_bytes()).hexdigest(),reason='configuration baseline')
    snapshot=Path(update['workflow_file']);before=snapshot.read_bytes()
    monkeypatch.setattr(cli,'ensure_stage_topology',topology.ensure_stage_topology)
    calls=[]
    real_run=cli.subprocess.run
    def transport(argv,**kwargs):
        calls.append(argv)
        if 'herdr-worker.py' in str(argv[0]):
            return SimpleNamespace(returncode=1,stdout='',stderr='controlled worker stop before Agent launch')
        if argv[0] != 'herdr':return real_run(argv,**kwargs)
        key=argv[1:3]
        if key==['tab','list']:result={'tabs':[]}
        elif key==['tab','create']:result={'tab':{'tab_id':'new-tab'}}
        elif key==['pane','list']:result={'panes':[{'pane_id':'new-anchor','tab_id':'new-tab','cwd':str(tmp_path),'label':'Herdr Anchor · n'}]}
        elif key==['pane','rename']:result={}
        else:raise AssertionError(argv)
        return SimpleNamespace(returncode=0,stdout=json.dumps({'result':result}),stderr='')
    monkeypatch.setattr(cli.subprocess,'run',transport)
    monkeypatch.setattr(sys,'argv',['herdr-task','launch','--task-id','actual','--workflow-id','wf','--node','n','--source',str(tmp_path),'--task-type','docs','--integration-mode','none','--goal','report','--prompt','report'])
    with pytest.raises(SystemExit) as exc:cli.main()
    assert exc.value.code==1
    assert any('herdr-worker.py' in str(cmd[0]) for cmd in calls)
    assert snapshot.read_bytes()==before,'actual launch topology overwrote immutable configuration'
    assert hashlib.sha256(before).hexdigest()==update['config_sha']
    fresh=store.get_workflow('wf');assert fresh['node_runtime']['n']['tab_id']=='new-tab'
    store.save_workflow({'workflow_id':'other','project_id':'p','status':'running','workflow_file':str(cfg),'config':json.loads(cfg.read_text()),
                         'node_runtime':{'n':{'tab_id':'foreign-tab','anchor_pane_id':'foreign-anchor'}}})
    assert projects.workflow_config_for('other')['nodes'][0]['tab_id']=='foreign-tab'
    assert projects.workflow_config_for('wf')['nodes'][0]['anchor_pane_id']=='new-anchor'
    assert topology._workflow_config('wf')[2]['nodes'][0]['tab_id']=='new-tab'
    assert cli.load_workflow('wf')['nodes'][0]['anchor_pane_id']=='new-anchor'
    close_topology=cli._workflow_stage_tabs('wf',[])
    assert close_topology['tab_ids']==['new-tab'] and 'new-anchor' in close_topology['owned_pane_ids']
    monkeypatch.setattr(pane_pool,'_pane_list',lambda _:[{'pane_id':'new-anchor','tab_id':'new-tab'},{'pane_id':'available','tab_id':'new-tab'}])
    monkeypatch.setattr(pane_pool,'_claimed_panes',lambda:{})
    monkeypatch.setattr(pane_pool,'_bindings',lambda:{})
    monkeypatch.setattr(pane_pool,'_live_agent',lambda _:None)
    slots=pane_pool.list_slots_for_project(fresh)
    assert [slot['pane_id'] for slot in slots]==['available']
    store.save_task({'task_id':'anchor-task','workflow_id':'wf','node':'n','status':'superseded','pane_id':'new-anchor','pane_source':'dynamic'})
    verdict=task_resources.reap_task_pane(store,'anchor-task',probe=lambda _:pytest.fail('anchor must be protected before transport'))
    assert verdict['reason']=='anchor_or_coordinator_pane'
