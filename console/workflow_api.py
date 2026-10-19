import hashlib, re, threading, time, uuid
from pathlib import Path
from herdr import workflow as herdr_workflow
from herdr.kernel import state_db_path
from herdr import controller_actions as herdr_controller_actions
from console import common, inventory, read_api

def run_workflow(root,req,agent='auto',template='software-development-v1',title='',context=None):
    cmd=[str(common.HERDR_ROOT/'bin'/'herdr-factory'),'run','--project',root,'--agent',agent or 'auto','--template',template or 'software-development-v1']
    if title:
        cmd+=['--title',title]
    for key, value in (context or {}).items():
        cmd += ['--context', f'{key}={value}']
    cmd.append(req)
    r=common.run(cmd,600)
    if r.returncode!=0:raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout.strip()

def _is_builtin_path(path):
    return Path(path).resolve().parent==herdr_workflow.BUNDLED_TEMPLATES_DIR.resolve()

def _derive_template_category(tid: str, data: dict):
    cat_raw = str((data or {}).get('category') or (data or {}).get('category_name') or '').strip()
    cat_lower = cat_raw.lower()
    if cat_lower in ('eng', 'software', 'dev') or '研发' in cat_lower or '开发' in cat_lower:
        return 'eng', '💻 软件研发'
    if cat_lower in ('biz', 'business') or '商务' in cat_lower or '咨询' in cat_lower:
        return 'biz', '📊 商务咨询'
    if cat_lower in ('ops', 'operation') or '运营' in cat_lower or '服务' in cat_lower:
        return 'ops', '🚀 运营服务'
    if cat_lower in ('sys', 'test') or '测试' in cat_lower or '系统' in cat_lower:
        return 'sys', '🧪 系统测试'
    if cat_raw:
        slug = re.sub(r'[^a-zA-Z0-9_-]', '', cat_lower)
        if not slug or len(slug) < 2:
            slug = 'cat_' + hashlib.md5(cat_raw.encode('utf-8')).hexdigest()[:8]
        return slug, cat_raw
    if tid in ('software-development-v1', 'code-craft-v1'):
        return 'eng', '💻 软件研发'
    if tid in ('bidding', 'business-research-v1'):
        return 'biz', '📊 商务咨询'
    if tid in ('customer-service', 'seo-audit-v1', 'general-task-v1'):
        return 'ops', '🚀 运营服务'
    if tid in ('context-smoke-test',):
        return 'sys', '🧪 系统测试'
    desc = str((data or {}).get('description', '')) + ' ' + str((data or {}).get('label', ''))
    if any(k in desc for k in ('代码', '开发', '架构', '重构', '研发')):
        return 'eng', '💻 软件研发'
    if any(k in desc for k in ('标书', '竞标', '商务', '调研', '咨询')):
        return 'biz', '📊 商务咨询'
    if any(k in desc for k in ('运营', '客服', 'SEO', '推广')):
        return 'ops', '🚀 运营服务'
    if any(k in desc for k in ('测试', '冒烟', '验证')):
        return 'sys', '🧪 系统测试'
    return 'biz', '📊 商务咨询'

def templates_summary():
    out=[]
    for name,info in sorted(herdr_workflow.list_templates().items()):
        raw = {}
        try:
            raw = herdr_workflow._load_file(Path(info['path']))
        except Exception:
            pass
        cat_id, cat_name = _derive_template_category(name, raw)
        nodes = raw.get('nodes', [])
        nodes_preview = [{'id': n.get('id'), 'label': n.get('label') or n.get('id'), 'depends_on': n.get('depends_on', [])} for n in nodes if isinstance(n, dict)]
        out.append({
            'id': name,
            **info,
            'category': cat_id,
            'category_name': cat_name,
            'nodes_preview': nodes_preview,
            'execution_mode': herdr_workflow.execution_mode(raw),
            'is_builtin': _is_builtin_path(info['path']),
        })
    return {'templates':out}

def template_detail(tid):
    info=herdr_workflow.list_templates().get(tid)
    if not info:raise RuntimeError('模板不存在: '+tid)
    wf=herdr_workflow.load_template(tid)
    cat_id, cat_name = _derive_template_category(tid, wf)
    nodes=[]
    for n in wf.get('nodes',[]):
        if not isinstance(n, dict): continue
        agents = []
        policy = n.get('agent_policy') or {}
        if isinstance(policy, dict):
            agents = policy.get('preferred') or []
        gate_info = n.get('gate') or {}
        gate_str = ''
        if isinstance(gate_info, dict):
            gate_str = gate_info.get('type') or gate_info.get('auto_criteria') or ('门禁校验' if gate_info else '')
        # Controller resolve_gate_config applies these defaults without a YAML gate.
        if not gate_str and n.get('id') in {'test', 'review', 'wrapup'}:
            gate_str = '内置默认门禁（需验证结论）'
        nodes.append({
            'id': n.get('id'),
            'label': n.get('label') or n.get('id'),
            'node_type': n.get('node_type','agent'),
            'depends_on': n.get('depends_on',[]),
            'purpose': n.get('purpose',''),
            'agents': agents,
            'gate': gate_str,
        })
    return {
        'template': {'id': tid, **info, 'category': cat_id, 'category_name': cat_name},
        'nodes': nodes,
        'is_builtin': _is_builtin_path(info['path']),
        'execution_mode': herdr_workflow.execution_mode(wf),
        'context': wf.get('context') or {},
        'yaml': Path(info['path']).read_text(encoding='utf-8'),
    }

def save_template(name,content):
    name=(name or '').strip()
    if not TEMPLATE_NAME_RE.match(name):raise RuntimeError('模板名只能用小写字母/数字/-/_，且以字母或数字开头')
    info=herdr_workflow.list_templates().get(name)
    if info and _is_builtin_path(info['path']):raise RuntimeError('内置模板只读，请换一个名字保存为自定义模板')
    import yaml
    text=(content or '').replace('\r\n','\n')
    if not text.strip():raise RuntimeError('模板内容不能为空')
    try:data=yaml.safe_load(text)
    except yaml.YAMLError as e:raise RuntimeError('YAML 解析失败:\n'+str(e))
    if not isinstance(data,dict):raise RuntimeError('模板顶层必须是 YAML 映射')
    if data.get('name') and data.get('name')!=name:raise RuntimeError(f"YAML 中的 name '{data.get('name')}' 与模板名 '{name}' 不一致")
    nodes=data.get('nodes')
    if not isinstance(nodes,list) or not nodes:raise RuntimeError('模板必须包含非空 nodes 列表')
    for n in nodes:
        if not isinstance(n,dict) or not n.get('id'):raise RuntimeError('每个节点都必须是包含 id 的映射')
    try:herdr_workflow.validate_workflow_dag(nodes)
    except ValueError as e:raise RuntimeError(str(e))
    path=herdr_workflow.USER_TEMPLATES_DIR/f'{name}.yaml'; path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(text,encoding='utf-8')
    return {'name':name,'path':str(path),'node_count':len(nodes)}

def _run_workflow_job(job_id,root,req,agent,template,title='',context=None):
    try:
        if context:
            output=run_workflow(root,req,agent,template,title,context=context)
        else:
            output=run_workflow(root,req,agent,template,title) if title else run_workflow(root,req,agent,template)
        with RUN_JOBS_LOCK:
            RUN_JOBS[job_id].update({'status':'succeeded','output':output,'finished_at':time.time()})
    except Exception as e:
        with RUN_JOBS_LOCK:
            RUN_JOBS[job_id].update({'status':'failed','error':str(e),'finished_at':time.time()})

def start_workflow_job(root,req,agent='auto',template='software-development-v1',title='',context=None):
    if not root or not req:
        raise RuntimeError('项目目录和自然语言需求不能为空')
    if context is not None and (not isinstance(context,dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in context.items())):
        raise RuntimeError('context 必须是名称到路径的映射')
    job_id=uuid.uuid4().hex[:12]
    with RUN_JOBS_LOCK:
        RUN_JOBS[job_id]={'job_id':job_id,'status':'running','started_at':time.time()}
    threading.Thread(target=_run_workflow_job,args=(job_id,root,req,agent,template,title,context),daemon=True).start()
    return RUN_JOBS[job_id].copy()

def workflow_job_status(job_id):
    with RUN_JOBS_LOCK:
        job=RUN_JOBS.get(job_id)
        if not job:raise RuntimeError('启动任务不存在或已过期')
        return job.copy()

def set_agent_override(wid,agent):
    d=common.load_json(common.WORKFLOWS_FILE,{'workflows':{}}); w=d.get('workflows',{}).get(wid)
    if not w:raise RuntimeError('工作流不存在')
    w['agent_override']=agent or 'auto'; common.save_json(common.WORKFLOWS_FILE,d); return {'workflow_id':wid,'agent_override':w['agent_override']}

def update_workflow_title(wid,title):
    t=(title or '').strip()
    try:
        from herdr.state_store import get_state_store, sync_workflows_projection
        s=get_state_store()
        s.update_workflow_metadata(wid, {'title': t})
        sync_workflows_projection(store=s)
    except Exception:
        pass
    d=common.load_json(common.WORKFLOWS_FILE,{'workflows':{}}); w=d.get('workflows',{}).get(wid)
    if w:
        w['title']=t
        common.save_json(common.WORKFLOWS_FILE,d)
    return {'workflow_id':wid,'title':t}

def bind_slot(pid,agent):
    d=common.load_json(common.SLOTS_FILE,{'panes':{}}); d.setdefault('panes',{})[pid]={'agent':agent or 'auto'}; common.save_json(common.SLOTS_FILE,d); return {'pane_id':pid,'agent':agent or 'auto'}

def ask_coordinator(tid):
    t=read_api.task_detail(tid)['task']; wid=t.get('workflow_id'); w=inventory.workflows().get(wid,{}); c=w.get('coordinator_pane_id') or t.get('coordinator_pane_id')
    msg=f'''HERDR_FACTORY_CONSOLE_ACTION\n\nworkflow_id: {wid}\ntask_id: {tid}\nstatus: {t.get('status')}\nstage: {t.get('stage')}\nagent: {t.get('agent')}\npane_id: {t.get('pane_id')}\n\n用户在{common.PRODUCT_NAME}控制台点击“让总指挥处理”。\n请检查 Task Registry、Pane、Agent Runtime 和验收标准；可恢复则安全恢复，需要人工决策则明确说明。不得删除任何已注册 Task 的 Pane / Tab / Clone。不得处理其他工作流。'''
    r=common.run(['herdr','agent','prompt',c,msg,'--wait','--timeout','600000'],620)
    if r.returncode!=0:raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return {'ok':True}

def _blocked_verdict_tasks(wid):
    return [t for t in herdr_controller_actions.resolve_workflow_blockers(
        inventory.tasks_for_workflow(wid), {'workflow_id':wid, **(inventory.workflows().get(wid) or {})})
        if t.get('stage_verdict')=='blocked']

def api_workflow_recovery(wid):
    from herdr import dispatch_recovery, node_dispatch_store
    db_path=state_db_path()
    try:
        node_dispatch_store.reconcile_workflow(db_path, wid, discover=False)
    except Exception:
        pass
    return {'workflow_id':wid,'operations':dispatch_recovery.list_operations(db_path,wid)}

def api_workflow_recovery_decide(b):
    from herdr import recovery_store
    db_path=state_db_path()
    wid=str(b.get('workflow_id') or '').strip()
    operation_id=int(b['operation_id'])
    operations=recovery_store.list_operations(db_path,wid)
    if not any(op['id']==operation_id for op in operations):
        raise RuntimeError('恢复义务不属于该工作流')
    if b.get('action') == 'check_resources':
        from herdr import dispatch_recovery
        return dispatch_recovery.check_resources(db_path,wid,operation_id,int(b['expected_version']),
            b.get('operator'),b.get('reason'),candidate_sha=b.get('candidate_sha'))
    if b.get('action') == 'abandon_partial_launch':
        from herdr import dispatch_recovery
        return dispatch_recovery.abandon_partial_launch(db_path, wid, operation_id,
            int(b['expected_version']), int(b['prior_operation_id']), int(b['expected_prior_version']),
            operator=b.get('operator'), reason=b.get('reason'), candidate_sha=b.get('candidate_sha'),
            intent_id=b.get('intent_id'), task_id=b.get('task_id'), pane_id=b.get('pane_id'),
            terminal_id=b.get('terminal_id'), transcript_sha256=b.get('transcript_sha256'),
            confirmed_startup_only=b.get('confirmed_startup_only'))
    if b.get('action') in {'restore_scope', 'confirm_absent'}:
        from herdr import dispatch_recovery
        return dispatch_recovery.decide(db_path,wid,operation_id,int(b['expected_version']),
            b.get('operator'),b['action'],b.get('reason'),confirmed_absent=b.get('confirmed_absent'),
            candidate_sha=b.get('candidate_sha'),confirmed_lineage=b.get('confirmed_lineage'),lineage_snapshot=b.get('lineage_snapshot'))
    result=recovery_store.decide_operation(db_path,operation_id,int(b['expected_version']),
        str(b.get('operator') or ''),str(b.get('action') or ''),str(b.get('reason') or ''),
        time.time(),until=b.get('until'))
    return {'ok':True,'operation':result}

def manual_advance(wid):
    d=read_api.workflow_detail(wid); done=None; nxt=None
    bl=_blocked_verdict_tasks(wid)
    if bl:raise RuntimeError('存在 blocked 验收结论('+', '.join(t['task_id'] for t in bl)+'),禁止手工推进;请先走 fix-loop(修复→重测→复审)或作废过期结论')
    for i,s in enumerate(d['stages']):
        if s['status'] in {'cleaned','finalizing'}:
            done=s['key']; nxt=d['stages'][i+1]['key'] if i+1<len(d['stages']) else None
        else:break
    if not done or not nxt:raise RuntimeError('当前没有可手工推进的下一阶段')
    w=d['workflow']; p=d['project']; c=w.get('coordinator_pane_id')
    msg=f'''HERDR_FACTORY_CONSOLE_STAGE_ADVANCE\n\nworkflow_id: {wid}\nproject_name: {p.get('project_name')}\nproject_root: {p.get('project_root')}\ncompleted_stage: {done}\nnext_stage: {nxt}\nbase_branch: {w.get('base_branch',p.get('base_branch',''))}\n\n用户点击“进入下一阶段”。请先检查门禁；满足后用 ~/HAFlow/bin/herdr-task launch 创建 {nxt} Task，参数必须包含 --workflow-id {wid} --stage {nxt} --source {p.get('project_root')} --agent auto。优先复用 Persistent Pane；不要删除 Tab、Pane、Clone。'''
    r=common.run(['herdr','agent','prompt',c,msg,'--wait','--timeout','600000'],620)
    if r.returncode!=0:raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return {'completed_stage':done,'next_stage':nxt}

def create_candidate(wid):
    allw=common.load_json(common.WORKFLOWS_FILE,{'workflows':{}}); w=allw.get('workflows',{}).get(wid)
    if not w:raise RuntimeError('工作流不存在')
    bl=_blocked_verdict_tasks(wid)
    if bl:raise RuntimeError('存在 blocked 验收结论('+', '.join(t['task_id'] for t in bl)+'),拒绝创建候选分支;请先完成 fix-loop 闭环或显式处理阻断,避免把未修复的交付合入候选分支')
    p=inventory.project_for_workflow(wid); repo=p.get('project_root'); base=w.get('original_base_branch') or p.get('base_branch'); cand=w.get('candidate_branch') or f'herdr/workflow-{wid}'
    tracked=common.run(['git','-C',repo,'status','--porcelain','--untracked-files=no'],check=True).stdout.strip()
    if tracked:raise RuntimeError('主仓库存在 tracked 修改，拒绝创建候选分支:\n'+tracked)
    impl=[t for t in inventory.tasks_for_workflow(wid) if t.get('stage')=='implementation' and t.get('integration_branch')]
    if not impl:raise RuntimeError('没有可汇总的 implementation Integration Branch')
    current=common.run(['git','-C',repo,'branch','--show-current'],check=True).stdout.strip(); exists=common.run(['git','-C',repo,'show-ref','--verify','--quiet',f'refs/heads/{cand}']).returncode==0
    try:
        if not exists:common.run(['git','-C',repo,'branch',cand,base],check=True)
        common.run(['git','-C',repo,'switch',cand],check=True)
        for t in impl:
            b=t['integration_branch']; m=common.run(['git','-C',repo,'merge','--no-edit',b],120)
            if m.returncode!=0:
                common.run(['git','-C',repo,'merge','--abort'],20); raise RuntimeError(f'合并 {b} 失败:\n'+(m.stderr.strip() or m.stdout.strip()))
    finally:common.run(['git','-C',repo,'switch',current],30)
    w['original_base_branch']=base; w['candidate_branch']=cand; w['base_branch']=cand; common.save_json(common.WORKFLOWS_FILE,allw); return {'workflow_id':wid,'candidate_branch':cand,'merged':[t['integration_branch'] for t in impl]}

TEMPLATE_NAME_RE=re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
RUN_JOBS={}
RUN_JOBS_LOCK=threading.Lock()
