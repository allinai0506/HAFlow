import os,shlex,subprocess,time
from pathlib import Path
from herdr.state_store import SQLiteStateStore
from herdr.completion_receipt import completion_instruction
from herdr.task_checkpoint import checkpoint_instruction_block

def test_prompt_commands_use_own_artifact_despite_old_path_shadow(tmp_path):
    store=SQLiteStateStore(tmp_path/'state.db');store.save_workflow({'workflow_id':'wf','status':'running'})
    store.save_task({'task_id':'t','workflow_id':'wf','run_id':'r','status':'working','started_at':time.time()-100})
    old=tmp_path/'old-bin';old.mkdir();shadow=old/'herdr-task';shadow.write_text('#!/bin/sh\necho old-cli >&2\nexit 2\n');shadow.chmod(0o755)
    env={**os.environ,'PATH':str(old)+os.pathsep+os.environ['PATH'],'HERDR_STATE_DB':str(store.db_path),'HERDR_CONTROLLER_DIR':str(tmp_path),'TASKS_FILE':str(tmp_path/'tasks.json'),'WORKFLOWS_FILE':str(tmp_path/'workflows.json')}
    assert subprocess.run(['herdr-task','report-completion','--help'],env=env,capture_output=True).returncode==2
    prompt=completion_instruction('t',store)
    command=next(line for line in prompt.splitlines() if ' report-completion ' in line)
    argv=shlex.split(command);assert Path(argv[0]).is_absolute()
    result=subprocess.run(argv,env=env,text=True,capture_output=True);assert result.returncode==0,result.stdout+result.stderr
    task=store.get_task('t');block=checkpoint_instruction_block(task,task['completion_epoch'])
    read=next(line.split('Resume by reading: ',1)[1] for line in block.splitlines() if line.startswith('Resume by reading: '))
    argv=shlex.split(read);assert Path(argv[0]).is_absolute()
    result=subprocess.run(argv,env=env,text=True,capture_output=True);assert result.returncode==0,result.stdout+result.stderr

def test_durable_prepared_prompt_uses_same_artifact_cli(tmp_path):
    from herdr.supervisor_delivery import deliver
    store=SQLiteStateStore(tmp_path/'state.db');store.save_workflow({'workflow_id':'wf','status':'running'})
    store.save_task({'task_id':'t','workflow_id':'wf','run_id':'r','status':'working','pane_id':'p','started_at':time.time()-100})
    prompts=[]
    deliver(store.get_task('t'),store,'RETRY',{'intervention_id':'retry'},'static',lambda pane,prompt:prompts.append(prompt))
    command=next(line for line in prompts[0].splitlines() if ' report-completion ' in line)
    assert Path(shlex.split(command)[0])==Path(__file__).resolve().parents[1]/'bin/herdr-task'
    receipt=store.list_events(task_id='t',event_type='retry_dispatch_prepared')[0]['payload']
    assert receipt['prompt']==prompts[0]

def test_initial_loop_and_context_commands_bind_artifact(tmp_path,monkeypatch):
    import importlib.machinery,importlib.util
    from types import SimpleNamespace
    from herdr import task_resources
    root=Path(__file__).resolve().parents[1]
    loader=importlib.machinery.SourceFileLoader('binding_cli',str(root/'bin/herdr-task'));spec=importlib.util.spec_from_loader(loader.name,loader);cli=importlib.util.module_from_spec(spec);loader.exec_module(cli)
    store=SQLiteStateStore(tmp_path/'state.db');store.save_workflow({'workflow_id':'wf','status':'running'})
    store.save_task({'task_id':'t','workflow_id':'wf','run_id':'r','status':'pending','pane_id':'p','clone_path':str(tmp_path)})
    (tmp_path/'.herdr-loop').mkdir()
    monkeypatch.setattr(cli,'_get_store',lambda:store);monkeypatch.setattr(cli,'load_tasks',lambda:{'tasks':store.list_tasks()})
    monkeypatch.setattr(cli,'_compile_working_context_ref',lambda task:'context-reference')
    monkeypatch.setattr(task_resources,'owned_live_pane',lambda task:(True,'identity_match'))
    prompts=[]
    monkeypatch.setattr(cli.subprocess,'run',lambda argv,**kwargs:(prompts.append(argv[-1]) or SimpleNamespace(returncode=0)))
    cli.dispatch_task('t','work')
    assert str(root/'bin/herdr-loop')+' eval' in prompts[0]
    assert str(root/'bin/herdr-task')+' working-context get' in prompts[0]
    assert '~/HAFlow/bin/herdr-loop' not in prompts[0]
