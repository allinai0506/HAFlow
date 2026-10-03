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
