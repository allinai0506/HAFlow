#!/usr/bin/env python3
import json, os, re, shutil, subprocess, sys, threading, time, urllib.parse, uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME=Path.home()
def _resolve_herdr_root():
    env = os.environ.get('HERDR_ROOT')
    if env and Path(env).exists():
        return Path(env)
    candidate = Path(__file__).resolve().parent.parent
    if (candidate / "herdr" / "__init__.py").exists() and (candidate / "bin").is_dir():
        return candidate
    if (HOME / "HAFlow" / "herdr" / "__init__.py").exists() and (HOME / "HAFlow" / "bin").is_dir():
        return HOME / "HAFlow"
    return candidate
HERDR_ROOT=_resolve_herdr_root(); ROOT=HOME/'.herdr-controller'
sys.path.insert(0, str(HERDR_ROOT))
from herdr import workflow as herdr_workflow
from herdr import projects as herdr_projects
from herdr import kernel as herdr_kernel
from herdr import steering as herdr_steering
from herdr import projection as herdr_projection
from herdr import archive as herdr_archive
from herdr.agent_binary import resolve_agent_binary
PROJECTS_FILE=ROOT/'projects.json'; WORKFLOWS_FILE=ROOT/'workflows.json'; TASKS_FILE=ROOT/'tasks.json'; POOLS_FILE=ROOT/'agent-pools.json'; SLOTS_FILE=ROOT/'pane-slots.json'; LOG_DIR=ROOT/'logs'
HOST='127.0.0.1'; PORT=int(os.environ.get('HERDR_CONSOLE_PORT','8765'))
PRODUCT_NAME='HAFlow'; PRODUCT_TAGLINE='让人和多个 AI Agent 一起把事情做完'
HERDR_TASK=HERDR_ROOT/'bin'/'herdr-task'
RUN_JOBS={}
RUN_JOBS_LOCK=threading.Lock()
STAGES=[('requirements','需求分析'),('plan','计划'),('implementation','实现'),('test','测试'),('review','评审'),('wrapup','收尾')]
ACTIVE={'pending','dispatched','working','blocked','agent_done','rework','completed','committed','integrated','cleanup_ready'}
AGENTS=['opencode','codex','claude','qodercli','agy','pi','grok']

def load_json(path,default):
    try:return json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception:return default

def save_json(path,data):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(path.suffix+'.tmp'); tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); tmp.replace(path)

def run(cmd,timeout=20,check=False):
    try:r=subprocess.run(cmd,text=True,capture_output=True,timeout=timeout)
    except subprocess.TimeoutExpired as e: raise RuntimeError('命令超时: '+' '.join(cmd)) from e
    if check and r.returncode!=0: raise RuntimeError(r.stderr.strip() or r.stdout.strip() or '命令失败')
    return r

def run_json(cmd,timeout=20):
    r=run(cmd,timeout,True)
    try:return json.loads(r.stdout)
    except Exception as e: raise RuntimeError('无法解析 Herdr JSON') from e

def ops_center(workflow_id=None,include_tasks=False):
    cmd=[str(HERDR_TASK),'ops-center']
    if workflow_id:cmd += ['--workflow-id',workflow_id]
    if include_tasks:cmd.append('--include-tasks')
    data=run_json(cmd,20)
    subjects={wid:(w.get('title') or w.get('requirement_subject') or herdr_projects.requirement_subject(w.get('requirement',''))) for wid,w in workflows().items()}
    for c in data.get('workflow_cards') or []:
        s=subjects.get(c.get('workflow_id'))
        if s:c['workflow_label']=s
    return data

def projects():return list(load_json(PROJECTS_FILE,{'projects':{}}).get('projects',{}).values())
def workflows():return load_json(WORKFLOWS_FILE,{'workflows':{}}).get('workflows',{})
def tasks():return load_json(TASKS_FILE,{'tasks':[]}).get('tasks',[])
def project_by_id(pid):return next((p for p in projects() if p.get('project_id')==pid),None)
def project_for_workflow(wid):
    w=workflows().get(wid); return (project_by_id(w.get('project_id')) if w else None) or w

def _with_subject(w):
    w=dict(w)
    t = (w.get('title') or '').strip()
    s = (w.get('requirement_subject') or '').strip()
    if t:
        w['requirement_subject']=t
    elif not s:
        w['requirement_subject']=herdr_projects.requirement_subject(w.get('requirement',''))
    return w

def workflows_for_project(pid):
    out=[]
    for wid,w in workflows().items():
        if w.get('project_id')==pid: out.append({'workflow_id':wid,**_with_subject(w)})
    # 工作流 ID 混用两套命名(wf-proj-<hash>-<时间戳> 与 wf-proj-<MMDD>-<序号>)，
    # 字典序不再等于时间序；必须按 created_at 倒序，保证默认选中最新的工作流。
    return sorted(out,key=lambda x:(x.get('created_at') or 0,x['workflow_id']),reverse=True)

def tasks_for_workflow(wid):return [t for t in tasks() if t.get('workflow_id')==wid]

def stage_summary(ts,key):
    xs=[t for t in ts if t.get('stage')==key]
    if not xs:return {'key':key,'count':0,'status':'waiting','tasks':[]}
    live=[t for t in xs if t.get('status')!='superseded' and not t.get('superseded_by')]
    if not live: st='superseded'
    else:
        ss=[t.get('status','unknown') for t in live]
        if all(s=='cleaned' for s in ss): st='cleaned'
        elif any(s=='failed' for s in ss): st='failed'
        elif any(s=='blocked' for s in ss): st='blocked'
        elif any(s in {'working','dispatched','pending','rework','agent_done'} for s in ss): st='working'
        elif all(s in {'completed','committed','integrated','cleanup_ready','cleaned'} for s in ss): st='finalizing'
        else: st='mixed'
    return {'key':key,'count':len(live),'status':st,'tasks':xs}

def panes(workspace):
    try:
        d=run_json(['herdr','pane','list','--workspace',workspace]).get('result',{})
        return d.get('panes',[]) if isinstance(d,dict) else d if isinstance(d,list) else []
    except Exception:return []

def tabs(workspace):
    try:
        d=run_json(['herdr','tab','list','--workspace',workspace]).get('result',{})
        return d.get('tabs',[]) if isinstance(d,dict) else d if isinstance(d,list) else []
    except Exception:return []

def agent_runtime(pane):
    if not pane:return None
    r=run(['herdr','agent','get',pane],8)
    if r.returncode!=0:return None
    try:return json.loads(r.stdout)['result']['agent']
    except Exception:return None

def pool(pid):return load_json(POOLS_FILE,{'projects':{}}).get('projects',{}).get(pid,{})

AUTH_HINTS = {
    'codex': [HOME / '.codex' / 'auth.json'],
    'claude': [HOME / '.claude.json'],
    'pi': [HOME / '.pi' / 'agent' / 'auth.json'],
    'opencode': [HOME / '.config' / 'opencode'],
    'qodercli': [HOME / '.qoder-cn'],
    'agy': [HOME / '.agy'],
    'grok': [HOME / '.grok' / 'auth.json'],
}

IN_FLIGHT_STATUSES = {'pending', 'dispatched', 'working', 'blocked', 'agent_done', 'rework'}

def agent_loads(pid):
    d={a:0 for a in AGENTS}
    for t in tasks():
        if t.get('project_id')==pid and t.get('status') in IN_FLIGHT_STATUSES and t.get('agent'):d[t['agent']]=d.get(t['agent'],0)+1
    return d

def preflight(p):
    po=pool(p.get('project_id')); allowed=po.get('allowed_agents',AGENTS); disabled=set(po.get('disabled_agents',[])); loads=agent_loads(p.get('project_id'))
    out=[]
    for a in allowed:
        b=resolve_agent_binary(a)
        hs=AUTH_HINTS.get(a,[])
        if hs:
            auth_state='present' if any(h.exists() for h in hs) else 'missing'
        else:
            auth_state='unknown'
        out.append({'agent':a,'installed':bool(b),'binary':b,'disabled':a in disabled,'load':loads.get(a,0),'auth_hint':auth_state,'status':'disabled' if a in disabled else 'ready' if b else 'missing'})
    return out

def deep_preflight(p):
    script = HERDR_ROOT / "bin" / "herdr-deep-preflight"
    if not script.exists():
        script = HERDR_ROOT / "herdr" / "deep_preflight.py"
    if not script.exists():
        raise RuntimeError(f"Deep Preflight 未安装: {script}")

    r = run([
        str(script),
        "--project-id", p.get("project_id"),
        "--deep",
        "--json",
    ], 320)

    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip() or "Deep Preflight 执行失败")

    try:
        return json.loads(r.stdout)
    except Exception as e:
        raise RuntimeError("Deep Preflight JSON 解析失败") from e


def slots(p):
    cfg=load_json(Path(p.get('workflow_file','')),{}) ; stage_by_tab={s.get('tab_id'):s for s in cfg.get('stages',[])}; anchors={s.get('anchor_pane_id') for s in cfg.get('stages',[])}; binds=load_json(SLOTS_FILE,{'panes':{}}).get('panes',{}); claimed={t.get('pane_id'):t.get('task_id') for t in tasks() if t.get('pane_id')}; out=[]
    for x in panes(p.get('workspace_id')):
        pid=x.get('pane_id'); tid=x.get('tab_id')
        if not pid or tid not in stage_by_tab or pid in anchors:continue
        rt=agent_runtime(pid); s=stage_by_tab[tid]
        out.append({'pane_id':pid,'tab_id':tid,'stage':s.get('key'),'stage_label':s.get('label'),'bound_agent':binds.get(pid,{}).get('agent','auto'),'claimed_by':claimed.get(pid),'live_agent':rt.get('agent') if rt else None,'agent_status':rt.get('agent_status') if rt else None})
    return out

def service_status():
    uid=os.getuid(); names=['com.user.herdr-controller','com.user.herdr-notifier','com.user.herdr-sentinel','com.user.herdr-factory-console']; out={}
    for n in names:
        r=run(['launchctl','print',f'gui/{uid}/{n}'],5); out[n]='running' if r.returncode==0 and 'state = running' in r.stdout else 'stopped'
    return out


def herdr_workspaces():
    """Herdr is the source of truth for Spaces/Workspaces."""
    try:
        d = run_json(['herdr', 'workspace', 'list'], 10).get('result', {})
        return d.get('workspaces', []) if isinstance(d, dict) else []
    except Exception:
        return []

def infer_space_root(workspace_id):
    """Infer a project root, ignoring task clones/sandboxes."""
    xs = panes(workspace_id)
    if not xs:
        return None

    # Strongest signal: coordinator pane.
    for x in xs:
        if x.get('label') == '总指挥':
            cwd = x.get('cwd') or x.get('foreground_cwd')
            if cwd:
                return cwd

    registered_roots = {p.get('project_root') for p in projects() if p.get('project_root')}

    # If a pane sits exactly at a registered root, prefer it.
    for x in xs:
        cwd = x.get('cwd') or x.get('foreground_cwd')
        if cwd in registered_roots:
            return cwd

    # Otherwise infer only from non-task/non-sandbox panes.
    candidates = []
    for x in xs:
        cwd = x.get('cwd') or x.get('foreground_cwd')
        if not cwd:
            continue
        if '/.herdr-controller/clones/' in cwd:
            continue
        if '/.nexusarchive-sandboxes/' in cwd:
            continue
        candidates.append(cwd)

    if not candidates:
        return None

    from collections import Counter
    return Counter(candidates).most_common(1)[0][0]

def spaces():
    """Join live Herdr Spaces with the Factory project registry."""
    ps = projects()
    by_workspace = {p.get('workspace_id'): p for p in ps if p.get('workspace_id')}
    out = []

    for ws in herdr_workspaces():
        wid = ws.get('workspace_id')
        p = by_workspace.get(wid)
        root = p.get('project_root') if p else infer_space_root(wid)

        if p:
            relation = 'current_factory'
        else:
            same_project = next((x for x in ps if root and x.get('project_root') == root), None)
            if same_project:
                p = same_project
                relation = 'historical'
            else:
                relation = 'unregistered'

        item = dict(ws)
        item.update({
            'relation': relation,
            'project_root': root,
            'project_id': p.get('project_id') if p else None,
            'project_name': p.get('project_name') if p else None,
            'factory_workspace_id': p.get('workspace_id') if p else None,
        })
        out.append(item)

    rank = {'current_factory': 0, 'historical': 1, 'unregistered': 2}
    return sorted(out, key=lambda x: (
        rank.get(x.get('relation'), 9),
        x.get('number', 9999),
        x.get('workspace_id', ''),
    ))

def overview():
    ts = tasks()
    alerts = [
        {
            'task_id': t.get('task_id'),
            'workflow_id': t.get('workflow_id'),
            'project_id': t.get('project_id'),
            'project_name': t.get('project_name'),
            'status': t.get('status'),
            'agent': t.get('agent'),
            'pane_id': t.get('pane_id'),
            'reason': (
                t.get('failure_reason')
                or t.get('blocked_reason')
                or t.get('sentinel_reason')
                or ''
            ),
        }
        for t in ts
        if t.get('status') in {'blocked', 'failed'}
    ]
    ss = spaces()
    return {
        'projects': projects(),
        'spaces': ss,
        'space_count': len(ss),
        'active_workflows': len({
            t.get('workflow_id')
            for t in ts
            if t.get('status') in ACTIVE
        }),
        'active_agents': sum(
            1 for t in ts
            if t.get('status') in {'working', 'blocked', 'dispatched'}
        ),
        'alerts': alerts[-50:],
        'services': service_status(),
    }

def project_detail(pid):
    p=project_by_id(pid)
    if not p:raise RuntimeError('项目不存在')
    ws=workflows_for_project(pid)
    return {'project':p,'workflows':ws,'latest_workflow_id':ws[0]['workflow_id'] if ws else None,'tabs':tabs(p.get('workspace_id')),'panes':panes(p.get('workspace_id')),'slots':slots(p),'agents':preflight(p)}

def workflow_detail(wid):
    w=workflows().get(wid)
    if not w:raise RuntimeError('工作流不存在')
    p=project_for_workflow(wid); ts=tasks_for_workflow(wid); ss=[]
    for k,l in STAGES:
        x=stage_summary(ts,k); x['label']=l; ss.append(x)
    stall_info=herdr_projection.detect_workflow_stalls(wid,ts)
    return {'workflow':{'workflow_id':wid,**_with_subject(w)},'project':p,'stages':ss,'tasks':ts,'coordinator':agent_runtime(w.get('coordinator_pane_id')),'candidate_branch':w.get('candidate_branch'),'agent_override':w.get('agent_override','auto'),'stall':stall_info}


def task_detail(tid):
    t=next((x for x in tasks() if x.get('task_id')==tid),None)
    if not t:raise RuntimeError('Task 不存在')
    return {'task':t,'runtime':agent_runtime(t.get('pane_id'))}

def read_pane(pid):
    r=run(['herdr','pane','read',pid,'--source','visible'],12)
    if r.returncode!=0:raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout

def run_workflow(root,req,agent='auto',template='software-development-v1',title=''):
    cmd=[str(HERDR_ROOT/'bin'/'herdr-factory'),'run','--project',root,'--agent',agent or 'auto','--template',template or 'software-development-v1']
    if title:
        cmd+=['--title',title]
    cmd.append(req)
    r=run(cmd,600)
    if r.returncode!=0:raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout.strip()

TEMPLATE_NAME_RE=re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')

def _is_builtin_path(path):
    return Path(path).resolve().parent==herdr_workflow.BUNDLED_TEMPLATES_DIR.resolve()

def templates_summary():
    out=[]
    for name,info in sorted(herdr_workflow.list_templates().items()):
        out.append({'id':name,**info,'is_builtin':_is_builtin_path(info['path'])})
    return {'templates':out}

def template_detail(tid):
    info=herdr_workflow.list_templates().get(tid)
    if not info:raise RuntimeError('模板不存在: '+tid)
    wf=herdr_workflow.load_template(tid)
    nodes=[{'id':n.get('id'),'label':n.get('label') or n.get('id'),'node_type':n.get('node_type','agent'),'depends_on':n.get('depends_on',[]),'purpose':n.get('purpose','')} for n in wf.get('nodes',[])]
    return {'template':{'id':tid,**info},'nodes':nodes,'is_builtin':_is_builtin_path(info['path']),'yaml':Path(info['path']).read_text(encoding='utf-8')}

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

def _run_workflow_job(job_id,root,req,agent,template,title=''):
    try:
        output=run_workflow(root,req,agent,template,title) if title else run_workflow(root,req,agent,template)
        with RUN_JOBS_LOCK:
            RUN_JOBS[job_id].update({'status':'succeeded','output':output,'finished_at':time.time()})
    except Exception as e:
        with RUN_JOBS_LOCK:
            RUN_JOBS[job_id].update({'status':'failed','error':str(e),'finished_at':time.time()})

def start_workflow_job(root,req,agent='auto',template='software-development-v1',title=''):
    if not root or not req:
        raise RuntimeError('项目目录和自然语言需求不能为空')
    job_id=uuid.uuid4().hex[:12]
    with RUN_JOBS_LOCK:
        RUN_JOBS[job_id]={'job_id':job_id,'status':'running','started_at':time.time()}
    threading.Thread(target=_run_workflow_job,args=(job_id,root,req,agent,template,title),daemon=True).start()
    return RUN_JOBS[job_id].copy()

def workflow_job_status(job_id):
    with RUN_JOBS_LOCK:
        job=RUN_JOBS.get(job_id)
        if not job:raise RuntimeError('启动任务不存在或已过期')
        return job.copy()

def set_agent_override(wid,agent):
    d=load_json(WORKFLOWS_FILE,{'workflows':{}}); w=d.get('workflows',{}).get(wid)
    if not w:raise RuntimeError('工作流不存在')
    w['agent_override']=agent or 'auto'; save_json(WORKFLOWS_FILE,d); return {'workflow_id':wid,'agent_override':w['agent_override']}

def bind_slot(pid,agent):
    d=load_json(SLOTS_FILE,{'panes':{}}); d.setdefault('panes',{})[pid]={'agent':agent or 'auto'}; save_json(SLOTS_FILE,d); return {'pane_id':pid,'agent':agent or 'auto'}

def ask_coordinator(tid):
    t=task_detail(tid)['task']; wid=t.get('workflow_id'); w=workflows().get(wid,{}); c=w.get('coordinator_pane_id') or t.get('coordinator_pane_id')
    msg=f'''HERDR_FACTORY_CONSOLE_ACTION\n\nworkflow_id: {wid}\ntask_id: {tid}\nstatus: {t.get('status')}\nstage: {t.get('stage')}\nagent: {t.get('agent')}\npane_id: {t.get('pane_id')}\n\n用户在{PRODUCT_NAME}控制台点击“让总指挥处理”。\n请检查 Task Registry、Pane、Agent Runtime 和验收标准；可恢复则安全恢复，需要人工决策则明确说明。不得删除任何已注册 Task 的 Pane / Tab / Clone。不得处理其他工作流。'''
    r=run(['herdr','agent','prompt',c,msg,'--wait','--timeout','600000'],620)
    if r.returncode!=0:raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return {'ok':True}

def _blocked_verdict_tasks(wid):
    return [t for t in tasks_for_workflow(wid)
            if t.get('status')!='superseded' and t.get('stage_verdict')=='blocked']

def manual_advance(wid):
    d=workflow_detail(wid); done=None; nxt=None
    bl=_blocked_verdict_tasks(wid)
    if bl:raise RuntimeError('存在 blocked 验收结论('+', '.join(t['task_id'] for t in bl)+'),禁止手工推进;请先走 fix-loop(修复→重测→复审)或作废过期结论')
    for i,s in enumerate(d['stages']):
        if s['status'] in {'cleaned','finalizing'}:
            done=s['key']; nxt=d['stages'][i+1]['key'] if i+1<len(d['stages']) else None
        else:break
    if not done or not nxt:raise RuntimeError('当前没有可手工推进的下一阶段')
    w=d['workflow']; p=d['project']; c=w.get('coordinator_pane_id')
    msg=f'''HERDR_FACTORY_CONSOLE_STAGE_ADVANCE\n\nworkflow_id: {wid}\nproject_name: {p.get('project_name')}\nproject_root: {p.get('project_root')}\ncompleted_stage: {done}\nnext_stage: {nxt}\nbase_branch: {w.get('base_branch',p.get('base_branch',''))}\n\n用户点击“进入下一阶段”。请先检查门禁；满足后用 ~/HAFlow/bin/herdr-task launch 创建 {nxt} Task，参数必须包含 --workflow-id {wid} --stage {nxt} --source {p.get('project_root')} --agent auto。优先复用 Persistent Pane；不要删除 Tab、Pane、Clone。'''
    r=run(['herdr','agent','prompt',c,msg,'--wait','--timeout','600000'],620)
    if r.returncode!=0:raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return {'completed_stage':done,'next_stage':nxt}

def api_kernel_pause(b):
    wid=str(b.get('workflow_id') or '').strip()
    if not wid:raise RuntimeError('workflow_id 不能为空')
    nid=str(b.get('node_id') or '').strip() or None
    return herdr_kernel.pause_workflow(wid,node_id=nid)

def api_kernel_resume(b):
    wid=str(b.get('workflow_id') or '').strip()
    if not wid:raise RuntimeError('workflow_id 不能为空')
    nid=str(b.get('node_id') or '').strip() or None
    return herdr_kernel.resume_workflow(wid,node_id=nid)

def api_kernel_step(b):
    wid=str(b.get('workflow_id') or '').strip()
    if not wid:raise RuntimeError('workflow_id 不能为空')
    return herdr_kernel.step_workflow(wid)

def api_kernel_rollback(b):
    wid=str(b.get('workflow_id') or '').strip()
    target=str(b.get('target_node_id') or '').strip()
    if not wid:raise RuntimeError('workflow_id 不能为空')
    if not target:raise RuntimeError('target_node_id 不能为空')
    reason=str(b.get('reason') or 'manual_rollback').strip()
    return herdr_kernel.rollback_workflow(wid,target_node_id=target,reason=reason)

def api_kernel_force_pass(b):
    wid=str(b.get('workflow_id') or '').strip()
    gate=str(b.get('gate_node_id') or '').strip()
    if not wid:raise RuntimeError('workflow_id 不能为空')
    if not gate:raise RuntimeError('gate_node_id 不能为空')
    note=str(b.get('note') or 'human forced pass').strip()
    op=str(b.get('operator') or 'human').strip()
    return herdr_kernel.force_pass_gate(wid,gate_node_id=gate,note=note,operator=op)

def api_kernel_checkpoint_create(b):
    wid=str(b.get('workflow_id') or '').strip()
    if not wid:raise RuntimeError('workflow_id 不能为空')
    tag=str(b.get('tag') or '').strip() or None
    return herdr_kernel.create_checkpoint(wid,tag=tag)

def api_kernel_checkpoint_list(wid):
    return herdr_kernel.list_checkpoints(wid)

def api_kernel_checkpoint_restore(b):
    wid=str(b.get('workflow_id') or '').strip()
    cpid=str(b.get('checkpoint_id') or '').strip()
    if not wid:raise RuntimeError('workflow_id 不能为空')
    if not cpid:raise RuntimeError('checkpoint_id 不能为空')
    return herdr_kernel.restore_checkpoint(wid,checkpoint_id=cpid)

def api_task_steer(b):
    tid=str(b.get('task_id') or '').strip()
    if not tid:raise RuntimeError('task_id 不能为空')
    inst=str(b.get('instruction') or '').strip()
    if not inst:raise RuntimeError('instruction 不能为空')
    op=str(b.get('operator') or 'human').strip()
    urgent=bool(b.get('urgent', False))
    return herdr_steering.queue_steer(tid, instruction=inst, operator=op, urgent=urgent)

def api_task_halt(b):
    tid=str(b.get('task_id') or '').strip()
    if not tid:raise RuntimeError('task_id 不能为空')
    reason=str(b.get('reason') or '人工在控制台紧急制动').strip()
    op=str(b.get('operator') or 'human').strip()
    return herdr_steering.halt_task(tid, reason=reason, operator=op)

def api_task_steer_queue(tid):
    if not tid:return []
    return herdr_steering.list_task_steers(tid)

def api_agent_adapters():
    from herdr.agent_adapter import list_agent_adapters
    return list_agent_adapters()


def api_task_force_review(b):
    tid=str(b.get('task_id') or '').strip()
    if not tid:raise RuntimeError('task_id 不能为空')
    task = None
    store = None
    if hasattr(herdr_kernel, "get_state_store"):
        try:
            store = herdr_kernel.get_state_store()
            task = store.get_task(tid)
        except Exception:
            store = None
    if task is None:
        tdata = herdr_kernel.load_tasks_data()
        for t in tdata.get('tasks', []):
            if t.get('task_id') == tid:
                task = t
                break
        if not task: raise RuntimeError(f'未找到任务 {tid}')
        task['status'] = 'agent_done'
        task['updated_at'] = time.time()
        herdr_kernel.save_tasks_data(tdata)
    else:
        task['status'] = 'agent_done'
        task['updated_at'] = time.time()
        store.save_task(task)

    wid=task.get('workflow_id')
    notified=False
    if wid:
        wf = None
        if store:
            wf = store.get_workflow(wid)
        else:
            wdata = herdr_kernel.load_workflows_data()
            wf = wdata.get('workflows', {}).get(wid)
        if wf and wf.get('coordinator_pane_id'):
            c=wf['coordinator_pane_id']
            msg=f'''HERDR_TASK_FORCE_REVIEW\n\ntask_id: {tid}\nworkflow_id: {wid}\n\n总指挥已人工唤醒评审，请立即对工位产物进行复审验收。'''
            try:
                r=run(['herdr','agent','prompt',c,msg,'--wait','--timeout','10000'],15)
                notified=(r.returncode==0)
            except Exception:
                pass
    return {'task_id':tid,'status':'agent_done','notified':notified}

def api_workflow_retry_advance(b):
    wid=str(b.get('workflow_id') or '').strip()
    if not wid:raise RuntimeError('workflow_id 不能为空')
    return manual_advance(wid)


def api_task_projection(tid):
    if not tid:raise RuntimeError('task_id 不能为空')
    return herdr_projection.project_task(tid)

def api_workflow_projection(wid):
    if not wid:raise RuntimeError('workflow_id 不能为空')
    return herdr_projection.project_workflow(wid)

def archive_query(project_id=None,workflow_id=None,agent=None,status=None,q=None,limit=50,offset=0):
    """归档查询读取优先走 StateStore(唯一事实源),投影文件仅作降级兜底。"""
    try:
        all_tasks=herdr_kernel.load_tasks_data().get('tasks',[])
    except Exception:
        all_tasks=tasks()
    return herdr_archive.query_archived_tasks(
        all_tasks,
        project_id=project_id or None,
        workflow_id=workflow_id or None,
        agent=agent or None,
        status=status or None,
        q=q or None,
        limit=limit,
        offset=offset,
    )

def api_task_signoff(b):
    tid=str(b.get('task_id') or '').strip()
    wid=str(b.get('workflow_id') or '').strip()
    node=str(b.get('node') or b.get('node_id') or '').strip()
    act=str(b.get('action') or 'approve').strip().lower()
    feedback=str(b.get('feedback') or '').strip()
    retry_target=str(b.get('retry_target') or '').strip()
    operator=str(b.get('operator') or 'human_studio').strip()

    if not wid or not node:
        if tid:
            tasks_data=load_json(TASKS_FILE,{'tasks':[]})
            for t in tasks_data.get('tasks',[]):
                if t.get('task_id')==tid:
                    if not wid:wid=t.get('workflow_id')
                    if not node:node=t.get('node') or t.get('stage')
                    break
    if not wid:raise RuntimeError('workflow_id 不能为空')
    if not node:raise RuntimeError('node 不能为空')

    if act=='approve':
        note=feedback or "人工在协同工作舱会签放行"
        res=herdr_kernel.force_pass_gate(wid,gate_node_id=node,note=note,operator=operator)
        return {'ok':True,'action':'approve','result':res}
    elif act=='reject':
        reason=feedback or "人工在协同工作舱会签打回"
        target=retry_target or node
        res=herdr_kernel.rollback_workflow(wid,target_node_id=target,reason=reason)
        return {'ok':True,'action':'reject','rollback':res,'feedback':feedback,'target_node':target}
    else:
        raise RuntimeError(f"不支持的会签动作: {act}")

def create_candidate(wid):
    allw=load_json(WORKFLOWS_FILE,{'workflows':{}}); w=allw.get('workflows',{}).get(wid)
    if not w:raise RuntimeError('工作流不存在')
    bl=_blocked_verdict_tasks(wid)
    if bl:raise RuntimeError('存在 blocked 验收结论('+', '.join(t['task_id'] for t in bl)+'),拒绝创建候选分支;请先完成 fix-loop 闭环或显式处理阻断,避免把未修复的交付合入候选分支')
    p=project_for_workflow(wid); repo=p.get('project_root'); base=w.get('original_base_branch') or p.get('base_branch'); cand=w.get('candidate_branch') or f'herdr/workflow-{wid}'
    tracked=run(['git','-C',repo,'status','--porcelain','--untracked-files=no'],check=True).stdout.strip()
    if tracked:raise RuntimeError('主仓库存在 tracked 修改，拒绝创建候选分支:\n'+tracked)
    impl=[t for t in tasks_for_workflow(wid) if t.get('stage')=='implementation' and t.get('integration_branch')]
    if not impl:raise RuntimeError('没有可汇总的 implementation Integration Branch')
    current=run(['git','-C',repo,'branch','--show-current'],check=True).stdout.strip(); exists=run(['git','-C',repo,'show-ref','--verify','--quiet',f'refs/heads/{cand}']).returncode==0
    try:
        if not exists:run(['git','-C',repo,'branch',cand,base],check=True)
        run(['git','-C',repo,'switch',cand],check=True)
        for t in impl:
            b=t['integration_branch']; m=run(['git','-C',repo,'merge','--no-edit',b],120)
            if m.returncode!=0:
                run(['git','-C',repo,'merge','--abort'],20); raise RuntimeError(f'合并 {b} 失败:\n'+(m.stderr.strip() or m.stdout.strip()))
    finally:run(['git','-C',repo,'switch',current],30)
    w['original_base_branch']=base; w['candidate_branch']=cand; w['base_branch']=cand; save_json(WORKFLOWS_FILE,allw); return {'workflow_id':wid,'candidate_branch':cand,'merged':[t['integration_branch'] for t in impl]}

def tail_log(kind='controller',n=180):
    mp={'controller':LOG_DIR/'controller.out.log','controller_err':LOG_DIR/'controller.err.log','notifier':LOG_DIR/'notifier.out.log','sentinel':LOG_DIR/'sentinel.out.log'}; p=mp.get(kind)
    if not p or not p.exists():return ''
    return '\n'.join(p.read_text(errors='ignore').splitlines()[-max(10,min(n,1000)):])

HTML_TEMPLATE=r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>__PRODUCT_NAME__</title><style>
:root{--bg:#0b0f14;--panel:#121821;--panel-elevated:#172230;--card:#17202b;--card-hover:#1c2838;--line:#293342;--line-focus:#455973;--text:#edf2f7;--muted:#8fa0b5;--subtle:#9cb2cd;--accent:#67a4ff;--accent-glow:rgba(103,164,255,0.2);--good:#42c58a;--warn:#f3b950;--bad:#f36b6b}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",sans-serif;line-height:1.5}button,input,select,textarea{font:inherit}button{cursor:pointer}*:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.shell{display:grid;grid-template-columns:250px minmax(0,1fr);min-height:100vh}.sidebar{border-right:1px solid var(--line);background:#0f141b;padding:16px;position:sticky;top:0;height:100vh;overflow:auto}.brand{font-size:20px;font-weight:750;letter-spacing:-0.3px}.sub{color:var(--muted);font-size:12px;margin:4px 0 16px}.project{width:100%;text-align:left;background:transparent;border:1px solid var(--line);color:var(--text);border-radius:12px;padding:14px;margin-bottom:8px;transition:all .15s ease}.project:hover{border-color:var(--line-focus);background:rgba(255,255,255,0.02)}.project.active{border-color:var(--accent);background:#14243a;box-shadow:0 0 12px var(--accent-glow)}.project small{display:block;color:var(--subtle);margin-top:4px;font-size:11.5px}.main{padding:24px;min-width:0}.top{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:18px}.title{font-size:22px;font-weight:760}.muted{color:var(--muted)}.actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.btn{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:10px;padding:8px 14px;display:inline-flex;align-items:center;gap:6px;font-weight:550;transition:all .15s ease;user-select:none}.btn:hover{background:var(--card-hover);border-color:var(--line-focus)}.btn:active{transform:translateY(1px)}.btn.primary{background:#2563eb;color:#ffffff;border:1px solid #3b82f6;font-weight:600;box-shadow:0 1px 3px rgba(0,0,0,.25)}.btn.primary:hover{background:#1d4ed8;border-color:#60a5fa;color:#ffffff}.btn.primary:active{background:#1e40af;transform:translateY(1px)}.btn.danger-btn{background:var(--bad);color:#fff;border-color:var(--bad);font-weight:700}.btn.danger-btn:hover{background:#ff7d7d}.btn.is-loading{opacity:.75;pointer-events:none;cursor:wait}.btn.icon-only{padding:8px 10px;font-weight:700}.actions .btn.primary{margin-left:auto}.btn-group{display:inline-flex;vertical-align:middle;border-radius:10px}.btn-group .btn{border-radius:0;margin-left:-1px}.btn-group .btn:first-child{border-top-left-radius:10px;border-bottom-left-radius:10px;margin-left:0}.btn-group .btn:last-child{border-top-right-radius:10px;border-bottom-right-radius:10px}.btn-group .btn:focus-visible{z-index:1}.dropdown{position:relative;display:inline-block}.dropdown-menu{display:none;position:absolute;right:0;top:calc(100% + 6px);background:var(--panel-elevated);border:1px solid var(--line);border-radius:12px;min-width:180px;z-index:80;box-shadow:0 12px 28px rgba(0,0,0,.6);padding:6px}.dropdown.open .dropdown-menu{display:block;animation:popIn .12s ease-out}.dropdown-item{padding:8px 12px;font-size:13px;color:var(--text);border-radius:8px;cursor:pointer;display:flex;align-items:center;gap:8px;background:transparent;border:0;width:100%;text-align:left;font:inherit;transition:background .12s}.dropdown-item:hover{background:#202e40}.dropdown-item.danger-text{color:var(--bad)}.dropdown-item.danger-text:hover{background:rgba(243,107,107,.15)}.dropdown-divider{height:1px;background:var(--line);margin:6px 0}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:18px}.metric,.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px}.metric{padding:16px}.metric b{font-size:24px;display:block;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}.metric span{font-size:12px;color:var(--muted)}.stages{display:flex;gap:10px;overflow-x:auto;margin-bottom:18px;padding:2px 2px 6px;scroll-behavior:smooth}.stage{flex:1;min-width:136px;padding:14px 16px;background:var(--panel);border:1px solid var(--line);border-radius:14px;position:relative;transition:all .2s ease}.stage:hover{border-color:var(--line-focus)}.stage.stage-active{border-color:var(--accent);box-shadow:0 0 14px var(--accent-glow)}.stage:not(:last-child)::after{content:'›';position:absolute;right:-7px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:18px;font-weight:700;z-index:2;pointer-events:none}.stage-head{font-size:11px;color:var(--accent);font-weight:700;margin-bottom:4px;letter-spacing:0.5px}.stage strong{display:block;margin-bottom:8px;font-size:14px}.badge{font-size:12px;border-radius:999px;padding:4px 8px;display:inline-block;border:1px solid var(--line)}.badge.cleaned{color:var(--good);border-color:rgba(66,197,138,.3)}.badge.working,.badge.finalizing{color:var(--warn);border-color:rgba(243,185,80,.3)}.badge.failed,.badge.blocked{color:var(--bad);border-color:rgba(243,107,107,.3)}.badge.waiting{color:var(--muted)}.badge.superseded{color:var(--muted)}.badge.in_progress{color:var(--warn);border-color:rgba(243,185,80,.3)}.grid{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(280px,.8fr);gap:16px}.panel{overflow:hidden}.panel h3{font-size:14px;margin:0;padding:14px 16px;border-bottom:1px solid var(--line);background:rgba(255,255,255,0.015)}.task{padding:16px;border-bottom:1px solid var(--line);display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:center;transition:background .15s}.task:hover{background:rgba(255,255,255,0.01)}.task.task-highlight{background:rgba(103,164,255,.14);border-left:3px solid var(--accent)}.task-name{font-weight:650;font-size:14px}.task-id{color:var(--subtle);font-size:11.5px;margin-top:4px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.task-meta{color:var(--subtle);font-size:12px;margin-top:4px}.task-actions{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}.mini{padding:5px 10px;border-radius:8px;border:1px solid var(--line);background:#101720;color:var(--text);font-size:12px;transition:all .15s}.mini:hover{background:#1a2636;border-color:var(--line-focus)}.agent-row,.slot-row,.alert-row{padding:10px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:8px;align-items:center}.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:8px;background:var(--muted)}.dot.ready{background:var(--good);box-shadow:0 0 6px rgba(66,197,138,.4)}.dot.working{background:var(--warn);box-shadow:0 0 6px rgba(243,185,80,.4)}.dot.disabled,.dot.failed{background:var(--bad);box-shadow:0 0 6px rgba(243,107,107,.4)}.section-gap{margin-top:16px}.empty{padding:20px 16px;color:var(--muted);font-size:13px;text-align:center}pre{margin:0;white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;line-height:1.55}.modal{position:fixed;inset:0;background:rgba(0,0,0,.68);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);display:none;align-items:center;justify-content:center;padding:16px;z-index:50}.modal.open{display:flex;animation:fadeIn .15s ease-out}.modal-card{width:min(920px,100%);max-height:86vh;overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:16px;box-shadow:0 24px 60px rgba(0,0,0,.7)}.modal-head{display:flex;justify-content:space-between;align-items:center;padding:16px 20px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--panel);z-index:2}.modal-body{padding:20px}.close{background:transparent;color:var(--muted);border:0;width:32px;height:32px;border-radius:8px;display:inline-flex;align-items:center;justify-content:center;cursor:pointer;transition:all .15s}.close:hover{background:rgba(255,255,255,.08);color:var(--text)}.form{display:grid;gap:10px}.form label{font-size:12px;color:var(--muted);font-weight:600}.form input,.form select,.form textarea{width:100%;background:#0d131a;color:var(--text);border:1px solid var(--line);border-radius:9px;padding:10px;transition:border-color .15s}.form input:focus,.form select:focus,.form textarea:focus{border-color:var(--accent)}.form textarea{min-height:120px;line-height:1.5}.toast{position:fixed;right:24px;bottom:24px;background:#111923;border:1px solid var(--line);padding:10px 18px;border-radius:12px;display:none;max-width:420px;z-index:90;box-shadow:0 10px 30px rgba(0,0,0,.5);font-size:13px;line-height:1.4}.toast.show{display:flex;align-items:center;gap:8px;animation:slideUp .18s ease-out}.danger-text{color:var(--bad)}.good-text{color:var(--good)}.warn-text{color:var(--warn)}.wf-subject{font-size:16px;font-weight:700;line-height:1.35}.wf-sub{font-size:12px;margin-top:4px;color:var(--subtle)}.wf-switcher{display:flex;align-items:center;gap:8px;margin:0 0 16px}.wf-switcher label{font-size:12px;color:var(--muted)}.wf-switcher select{background:#0d131a;color:var(--text);border:1px solid var(--line);border-radius:9px;padding:8px;max-width:520px}.fleet-table{border:1px solid var(--line);border-radius:12px;overflow:hidden;margin-top:8px}.fleet-header,.fleet-row{display:grid;grid-template-columns:120px 80px minmax(120px,1.2fr) 70px 80px minmax(120px,1.5fr);gap:8px;align-items:center;padding:10px 14px;background:var(--panel);border-bottom:1px solid var(--line);font-size:12px}.fleet-header{font-size:11px;font-weight:700;color:var(--muted);background:#0d131a;text-transform:uppercase;letter-spacing:0.5px}.fleet-row:last-child{border-bottom:0}.fleet-row:hover{background:var(--card)}.proj-box{display:grid;gap:12px}.proj-sec{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}.proj-lbl{font-size:11px;font-weight:700;color:var(--accent);letter-spacing:0.5px;margin-bottom:6px}.proj-txt{font-size:13px;line-height:1.5;color:var(--text)}.proj-blk{background:rgba(243,107,107,0.12);border:1px solid rgba(243,107,107,0.3);border-radius:10px;padding:10px 12px;color:var(--bad);font-size:13px;display:flex;align-items:center;gap:8px}.proj-ms{display:flex;align-items:center;gap:8px;font-size:12.5px;padding:6px 8px;border-radius:8px;background:rgba(255,255,255,0.02);margin-bottom:4px}.proj-ms-dot{width:16px;height:16px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:10px;font-weight:700}.proj-ms-dot.completed{background:rgba(66,197,138,0.2);color:var(--good)}.proj-ms-dot.in_progress{background:rgba(243,185,80,0.2);color:var(--warn)}.proj-ms-dot.pending{background:rgba(255,255,255,0.05);color:var(--muted)}.proj-art{background:rgba(0,0,0,0.2);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:12px;margin-bottom:6px}.proj-art-hd{display:flex;justify-content:space-between;align-items:center;font-weight:600;margin-bottom:2px}.proj-acts{margin:0;padding-left:16px;font-size:12px;color:var(--subtle);line-height:1.5}@keyframes fadeIn{from{opacity:0}to{opacity:1}}@keyframes slideUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}@keyframes popIn{from{opacity:0;transform:scale(0.96)}to{opacity:1;transform:scale(1)}}@media(max-width:1000px){.shell{grid-template-columns:1fr}.sidebar{position:static;height:auto;border-right:0;border-bottom:1px solid var(--line)}.projects{display:flex;gap:8px;overflow:auto}.project{min-width:180px}.grid{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,1fr)}}
.attention-banner{background:linear-gradient(90deg,#14243a 0%,#111a26 100%);border:1px solid var(--accent);border-radius:12px;padding:12px 18px;margin-bottom:16px;display:flex;align-items:center;justify-content:space-between;box-shadow:0 0 16px var(--accent-glow);gap:12px}.att-badge{background:var(--accent);color:#06111f;font-weight:700;font-size:11px;padding:3px 8px;border-radius:999px;letter-spacing:0.5px;text-transform:uppercase}.att-text{font-size:13.5px;font-weight:600;color:var(--text)}.task-filters{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}.filter-btn{background:var(--panel);border:1px solid var(--line);color:var(--muted);border-radius:8px;padding:6px 12px;font-size:12px;cursor:pointer;transition:all .15s ease}.filter-btn:hover{color:var(--text);border-color:var(--line-focus)}.filter-btn.active{background:#14243a;border-color:var(--accent);color:var(--text);font-weight:600}.filter-cnt{background:rgba(255,255,255,0.08);border-radius:999px;padding:1px 6px;margin-left:4px;font-size:11px}.deep-drawer{position:fixed;bottom:0;left:250px;right:0;background:var(--panel);border-top:1px solid var(--line);box-shadow:0 -8px 24px rgba(0,0,0,0.5);z-index:40;transition:transform .2s ease-in-out}.deep-drawer.collapsed{transform:translateY(calc(100% - 40px))}.drawer-head{height:40px;padding:0 18px;display:flex;justify-content:space-between;align-items:center;cursor:pointer;background:#0d131a;border-bottom:1px solid var(--line);user-select:none}.drawer-head:hover{background:#121a24}.drawer-pill{background:#1a2736;border:1px solid var(--accent);color:var(--accent);font-size:11px;font-weight:700;padding:2px 8px;border-radius:6px}.drawer-body{height:280px;display:flex;flex-direction:column;background:var(--bg)}.drawer-tabs{display:flex;background:#0d131a;border-bottom:1px solid var(--line)}.dtab{padding:8px 16px;background:transparent;border:0;border-bottom:2px solid transparent;color:var(--muted);font-size:12px;cursor:pointer}.dtab:hover{color:var(--text)}.dtab.active{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}.drawer-view{flex:1;overflow:auto;padding:12px 18px;background:#080b0f}.signoff-box{display:grid;gap:14px}.signoff-head{display:flex;justify-content:space-between;align-items:center;padding-bottom:12px;border-bottom:1px solid var(--line)}.signoff-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:14px;padding-top:14px;border-top:1px solid var(--line)}@media(max-width:1000px){.deep-drawer{left:0}}
</style></head><body><div class="shell"><aside class="sidebar"><div class="brand">__PRODUCT_NAME__</div><div class="sub">__PRODUCT_TAGLINE__ · 控制台</div><button class="btn primary" style="width:100%;margin-bottom:12px;display:flex;align-items:center;justify-content:center;gap:6px" onclick="showNewProjectModal()"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg><span>新建工厂空间</span></button><div id="projects" class="projects"></div><button class="btn" style="width:100%;margin-top:8px" onclick="refreshAll()"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.3"/></svg><span>刷新</span></button></aside><main class="main"><div class="top"><div><div class="title" id="projectTitle">选择项目</div><div id="workflowTitle"><div class="wf-subject" id="workflowSubject">—</div><div class="muted wf-sub" id="workflowSub"></div></div></div><div class="actions"><div class="btn-group factory-action"><button class="btn" onclick="advanceStage()" title="推进当前阶段">进入下一阶段</button><button class="btn" onclick="createCandidate()" title="汇总至候选分支">创建候选分支</button></div><button class="btn factory-action" onclick="runPreflight()">执行者自检</button><button class="btn factory-action" onclick="showTemplateLibrary()">模板库</button><button class="btn factory-action" onclick="showArchive()">任务归档</button><div class="dropdown factory-action" id="moreDropdown"><button class="btn icon-only" onclick="toggleMoreMenu(event)" aria-label="更多操作" title="更多操作">···</button><div class="dropdown-menu"><button class="dropdown-item" onclick="closeMoreMenu();showLogs()">查看日志</button><button class="dropdown-item" onclick="closeMoreMenu();showAgentOverride()">指定执行者</button><button class="dropdown-item" onclick="closeMoreMenu();toggleWorkflowPause()">暂停/恢复调度</button><button class="dropdown-item" onclick="closeMoreMenu();stepWorkflow()">单步推进节点</button><button class="dropdown-item" onclick="closeMoreMenu();showRollbackModal()">节点回溯 (Rollback)</button><button class="dropdown-item" onclick="closeMoreMenu();showCheckpointsModal()">快照中心 (Checkpoints)</button><div class="dropdown-divider"></div><button class="dropdown-item danger-text" onclick="closeMoreMenu();showUnregisterProjectModal()">注销项目</button></div></div><button class="btn primary factory-action" onclick="showNewWorkflow()"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg><span>新需求</span></button></div></div><div class="metrics"><div class="metric"><b id="mProjects">0</b><span>项目空间</span></div><div class="metric"><b id="mWorkflows">0</b><span>活跃工作流</span></div><div class="metric"><b id="mAgents">0</b><span>活跃执行者</span></div><div class="metric"><b id="mAlerts">0</b><span>需要关注</span></div></div><div id="stages" class="stages"></div><div id="attentionBanner" class="attention-banner" style="display:none"></div><div id="workflowSwitcher" class="wf-switcher" style="display:none"></div><div class="grid"><section class="panel"><h3>执行者与任务实时看板</h3><div class="task-filters" style="padding:10px 16px 0"><button class="filter-btn active" id="fAll" onclick="setTaskFilter('all')">全部任务 <span class="filter-cnt" id="cntAll">0</span></button><button class="filter-btn" id="fDecision" onclick="setTaskFilter('decision')">待我拍板 <span class="filter-cnt" id="cntDecision">0</span></button><button class="filter-btn" id="fAttention" onclick="setTaskFilter('attention')">需关注 <span class="filter-cnt" id="cntAttention">0</span></button><button class="filter-btn" id="fActive" onclick="setTaskFilter('active')">进行中 <span class="filter-cnt" id="cntActive">0</span></button></div><div id="tasks"></div></section><section><div class="panel"><h3>执行者阵容</h3><div id="agents"></div></div><div class="panel section-gap"><h3>常驻智能体工位</h3><div id="slots"></div></div><div class="panel section-gap"><h3>告警中心</h3><div id="alerts"></div></div></section></div></main></div><div id="modal" class="modal" role="dialog" aria-modal="true" aria-labelledby="modalTitle"><div class="modal-card"><div class="modal-head"><strong id="modalTitle">详情</strong><button class="close" aria-label="关闭弹窗" onclick="closeModal()"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg></button></div><div id="modalBody" class="modal-body"></div></div></div><div id="toast" class="toast" role="alert" aria-live="polite"></div><div id="deepDrawer" class="deep-drawer collapsed"><div class="drawer-head" onclick="toggleDeepDrawer()"><div style="display:flex;align-items:center;gap:8px"><span class="drawer-pill">底层物理现场</span><span class="muted" style="font-size:12px">原生终端 · 内核日志 · 白盒遥测</span></div><div style="display:flex;align-items:center;gap:10px"><button class="mini" onclick="event.stopPropagation();refreshDeepDrawer()">刷新</button><span id="drawerToggleText" style="font-size:12px;color:var(--accent);font-weight:600">▲ 展开抽屉</span></div></div><div class="drawer-body"><div class="drawer-tabs"><button class="dtab active" id="dtabTty" onclick="switchDrawerTab('tty')">活动工位终端 (Live TTY)</button><button class="dtab" id="dtabLogs" onclick="switchDrawerTab('logs')">内核日志 (Controller Log)</button><button class="dtab" id="dtabRaw" onclick="switchDrawerTab('raw')">白盒遥测原始数据 (Raw Telemetry)</button></div><div class="drawer-view"><pre id="drawerPre">请选择活动工位或点击展开查看底层物理输出…</pre></div></div></div><script>
let state={overview:null,project:null,workflow:null,ops:null,projectId:null,workflowId:null,spaceId:null,space:null,opsMode:false,taskFilter:'all',drawerTab:'tty'};
const VIEW_KEY='herdrConsoleView';
function closeMoreMenu(){const dd=document.getElementById('moreDropdown');if(dd)dd.classList.remove('open')}
function toggleMoreMenu(e){e.stopPropagation();const dd=document.getElementById('moreDropdown');if(dd)dd.classList.toggle('open')}
document.addEventListener('click',e=>{const dd=document.getElementById('moreDropdown');if(dd&&!dd.contains(e.target))dd.classList.remove('open')});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeModal();closeMoreMenu();const d=document.getElementById('deepDrawer');if(d&&!d.classList.contains('collapsed')){d.classList.add('collapsed');const t=document.getElementById('drawerToggleText');if(t)t.textContent='▲ 展开抽屉'}}});
function showConfirmModal({title,message,confirmText='确认',danger=false,onConfirm}){
  openModal(title,`<div style="line-height:1.6"><div style="font-size:14px;margin-bottom:18px;color:var(--text)">${esc(message)}</div><div style="display:flex;justify-content:flex-end;gap:10px"><button class="btn" onclick="closeModal()">取消</button><button id="modalConfirmBtn" class="btn ${danger?'danger-btn':'primary'}">${esc(confirmText)}</button></div></div>`);
  const btn=document.getElementById('modalConfirmBtn');
  if(btn)btn.onclick=async()=>{closeModal();if(onConfirm)await onConfirm()}
}
function showPromptModal({title,label,defaultValue='',confirmText='确定',onConfirm}){
  openModal(title,`<div class="form"><label for="promptInput">${esc(label)}</label><input id="promptInput" value="${esc(defaultValue)}" autofocus><div style="display:flex;justify-content:flex-end;gap:10px;margin-top:10px"><button class="btn" onclick="closeModal()">取消</button><button id="modalPromptBtn" class="btn primary">${esc(confirmText)}</button></div></div>`);
  const input=document.getElementById('promptInput');
  if(input){input.focus();input.select();input.addEventListener('keydown',e=>{if(e.key==='Enter')document.getElementById('modalPromptBtn')?.click()})};
  const btn=document.getElementById('modalPromptBtn');
  if(btn)btn.onclick=async()=>{const val=input?input.value.trim():'';closeModal();if(onConfirm)await onConfirm(val)}
}
function saveViewState(){try{localStorage.setItem(VIEW_KEY,JSON.stringify({opsMode:state.opsMode,spaceId:state.spaceId,workflowId:state.workflowId}))}catch(e){}}
function loadViewState(){try{return JSON.parse(localStorage.getItem(VIEW_KEY)||'null')}catch(e){return null}}
async function waitForWorkflowJob(jobId){
  try{
    for(let i=1;i<=180;i++){
      const job=await api('/api/run/status?id='+encodeURIComponent(jobId));
      if(job.status==='succeeded')return job;
      if(job.status==='failed')throw new Error(job.error||'工作流启动失败');
      const wait=document.getElementById('runWaitStatus');
      if(wait)wait.textContent='深度体检与启动中… '+i+'s（深度体检约 1–2 分钟，请勿重复创建）';
      await new Promise(resolve=>setTimeout(resolve,1000));
    }
    throw new Error('工作流启动超时，请到运维驾驶舱查看状态');
  }finally{
    const wait=document.getElementById('runWaitStatus');
    if(wait)wait.textContent='';
  }
}
async function submitNewWorkflowAsync(){
  const button=[...document.querySelectorAll('#modal button')].find(x=>x.textContent.includes('启动工作流'));
  const title=(document.getElementById('newTitle')?.value||'').trim();
  const q=document.getElementById('newRequirement')?.value.trim();
  const a=document.getElementById('newAgent')?.value;
  const t=document.getElementById('newTemplate')?.value||'software-development-v1';
  if(!q)return toast('请输入需求',true);
  if(button){button.disabled=true;button.classList.add('is-loading');button.textContent='启动中…'}
  try{
    toast('正在创建工作流…');
    const job=await api('/api/run',{method:'POST',body:JSON.stringify({project_root:state.project.project.project_root,title:title,requirement:q,agent:a,template:t})});
    const result=await waitForWorkflowJob(job.job_id);
    const m=/(?:^|[\r\n])WORKFLOW_ID=([^\s]+)/.exec(result.output||'');
    if(m){state.workflowId=m[1];saveViewState()}
    closeModal();await refreshAll();
    toast('工作流已启动：'+(m?m[1]:(result.output||'已提交')));
  }catch(e){
    if(button){button.disabled=false;button.classList.remove('is-loading');button.textContent='重试启动工作流'}
    const body=document.getElementById('modalBody');
    if(body){let error=body.querySelector('.run-error');if(!error){error=document.createElement('div');error.className='run-error danger-text';body.prepend(error)}error.textContent='启动失败：'+e.message}
    toast('工作流启动失败：'+e.message,true);
  }
}
const submitNewWorkflow=submitNewWorkflowAsync;
function syncOpsUi(){
  const actions=document.querySelector('.actions');
  if(!actions)return;
  let b=document.getElementById('opsButton');
  if(!b){b=document.createElement('button');b.id='opsButton';actions.prepend(b)}
  b.textContent=state.opsMode?'← 返回工厂':'进入运维驾驶舱';
  b.className=state.opsMode?'btn primary':'btn';
  b.onclick=state.opsMode?exitOpsCenter:showOpsCenter;
  closeMoreMenu();
  document.querySelectorAll('.factory-action').forEach(button=>{button.hidden=state.opsMode})
}

async function api(p,o={}){
  const r=await fetch(p,{headers:{'Content-Type':'application/json'},...o});
  const d=await r.json();
  if(!r.ok||d.ok===false)throw new Error(d.error||('HTTP '+r.status));
  return d.data??d
}
function toast(m,b=false){
  const e=document.getElementById('toast');
  e.textContent=m;
  e.className='toast show '+(b?'danger-text':'good-text');
  setTimeout(()=>e.classList.remove('show'),4200)
}
function esc(s){
  return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))
}
function humanStatus(s){
  return ({
    waiting:'等待',pending:'待派发',dispatched:'已派发',working:'运行中',
    blocked:'已阻塞',agent_done:'执行者已完成',completed:'待收尾',
    committed:'已提交',integrated:'已集成',cleanup_ready:'待归档',
    cleaned:'已完成',failed:'失败',rework:'返工中',
    superseded:'已取代',in_progress:'运行中',empty:'无任务',
    finalizing:'收尾中',mixed:'处理中',active:'运行中'
  })[s]||s||'未知'
}
function badge(s){return `<span class="badge ${esc(s)}">${esc(humanStatus(s))}</span>`}

function relationText(s){
  return s.relation==='current_factory'?'当前工厂':
         s.relation==='historical'?'历史空间':'独立终端'
}
function relationClass(s){
  return s.relation==='current_factory'?'good-text':
         s.relation==='historical'?'muted':'warn-text'
}
function taskSequence(t){
  const m=String(t.task_id||'').match(/-(?:req|plan|impl|test|fix|rev|wrap)-([0-9]+)$/i);
  return m?parseInt(m[1],10):null
}
function taskKind(t){
  const id=String(t.task_id||'').toLowerCase();
  const st=String(t.stage||'').toLowerCase();
  if(id.includes('-fix-'))return '修复';
  if(id.includes('-test-')){
    const n=taskSequence(t);
    return n&&n>1?'回归测试':'测试'
  }
  if(id.includes('-req-')||st==='requirements')return '需求分析';
  if(id.includes('-plan-')||st==='plan')return '计划';
  if(id.includes('-impl-')||st==='implementation')return '实现';
  if(id.includes('-rev-')||st==='review')return '评审';
  if(id.includes('-wrap-')||st==='wrapup')return '收尾';
  return t.stage_label||t.stage||'任务'
}
function taskDisplayName(t){
  const n=taskSequence(t);
  return n?`${taskKind(t)} #${n}`:taskKind(t)
}
function visibleAlerts(){
  const all=(state.overview&&state.overview.alerts)||[];
  if(!state.projectId)return [];
  let rows=all.filter(a=>a.project_id===state.projectId);
  if(state.workflowId){
    const exact=rows.filter(a=>a.workflow_id===state.workflowId);
    if(exact.length)rows=exact
  }
  return rows
}

function formatElapsed(seconds){
  if(seconds==null)return '—';
  const n=Math.max(0,Math.round(seconds));
  if(n<60)return n+'s';
  const m=Math.floor(n/60),s=n%60;
  if(m<60)return m+'m '+String(s).padStart(2,'0')+'s';
  return Math.floor(m/60)+'h '+String(m%60).padStart(2,'0')+'m';
}
function opsHealthClass(s){return ['BLOCKED','FAILED','STALE'].includes(s)?'danger-text':s==='BUSY'?'warn-text':'good-text'}
function healthLabel(s){
  return ({BUSY:'忙碌',IDLE:'空闲',BLOCKED:'阻塞',FAILED:'失败',STALE:'失联',UNKNOWN:'未知'})[s]||s||'未知'
}
function agentStatusLabel(s){
  return ({ready:'可用',disabled:'已禁用',missing:'未安装',working:'运行中'})[s]||s||'未知'
}
function authHintLabel(s){
  return ({present:'已配置',missing:'未配置',unknown:'未知'})[s]||s||'未知'
}
async function fetchTemplates(){
  const d=await api('/api/templates');
  state.templates=d.templates||[];
  return state.templates
}
async function showTemplateLibrary(){
  openModal('工作流模板库','<div class="empty">正在加载模板…</div>');
  try{
    const ts=await fetchTemplates();
    const cards=ts.length?ts.map(t=>`
      <div class="task">
        <div>
          <div class="task-name">${esc(t.label||t.id)} <span class="badge ${t.is_builtin?'waiting':'good-text'}">${t.is_builtin?'内置':'自定义'}</span></div>
          <div class="task-id">${esc(t.id)} · v${esc(t.version)} · ${t.node_count} 节点</div>
          <div class="task-meta">${esc(t.description||'')}</div>
        </div>
        <div class="task-actions">
          <button class="mini" onclick="showTemplateDAG('${esc(t.id)}')">节点依赖</button>
          <button class="mini" onclick="showTemplateEditor('${esc(t.id)}')">${t.is_builtin?'查看 YAML':'编辑'}</button>
        </div>
      </div>`).join(''):'<div class="empty">暂无模板</div>';
    openModal('工作流模板库',`<div class="muted" style="margin-bottom:8px">模板定义工作流的节点与 DAG 依赖。自定义模板保存到 ~/.herdr-controller/templates/，对新启动的工作流即时生效，不影响已运行的工作流。</div>${cards}<div style="margin-top:8px"><button class="btn primary" onclick="showTemplateEditor()"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg><span>新建模板</span></button></div>`)
  }catch(e){toast(e.message,true)}
}
async function showTemplateDAG(id){
  try{
    const d=await api('/api/template?id='+encodeURIComponent(id));
    const rows=(d.nodes||[]).map(n=>`
      <div class="agent-row">
        <div>
          <div class="task-name">${esc(n.label||n.id)} <span class="task-id">${esc(n.id)} · ${esc(n.node_type||'agent')}</span></div>
          <div class="task-meta">${esc(n.purpose||'')}${(n.gate&&n.gate.type)?' · 门禁: '+esc(n.gate.type):''}${(n.worker_policy&&n.worker_policy.permissions)?' · 权限: '+esc(n.worker_policy.permissions.join('/')):''}</div>
        </div>
        <div class="task-meta">${(n.depends_on||[]).length?'← '+(n.depends_on||[]).map(esc).join('、'):'起始节点'}</div>
      </div>`).join('');
    openModal('节点依赖 · '+((d.template&&d.template.label)||id),rows||'<div class="empty">无节点</div>')
  }catch(e){toast(e.message,true)}
}
const TEMPLATE_SCAFFOLD=`name: my-workflow
label: 我的工作流
version: "1.0"
description: 在这里描述业务流程

nodes:
  - id: analyze
    label: 分析
    node_type: agent
    purpose: 第一步做什么

  - id: deliver
    label: 交付
    node_type: agent
    depends_on: [analyze]
    purpose: 汇总并产出结果
`;
async function showTemplateEditor(id){
  const info=(state.templates||[]).find(t=>t.id===id);
  const builtin=!!(info&&info.is_builtin);
  let yaml=TEMPLATE_SCAFFOLD;
  if(id){
    try{
      const d=await api('/api/template?id='+encodeURIComponent(id));
      yaml=d.yaml
    }catch(e){return toast(e.message,true)}
  }
  openModal(id?(builtin?'内置模板（只读）':'编辑模板 · '+id):'新建模板',`
    <div class="form">
      <label>模板名（小写字母/数字/-/_，作为启动时的 --template 参数${id?'，不可修改':''}）</label>
      <input id="tplName" value="${esc(id||'')}" ${id?'disabled':''}>
      <label>YAML 定义</label>
      <textarea id="tplYaml" style="min-height:320px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace" ${builtin?'disabled':''}>${esc(yaml)}</textarea>
      <div class="muted">保存时服务端会做 DAG 校验（未知依赖 / 循环依赖会被拒绝）。内置模板只读；自定义模板保存后可在“新建需求”中选用。</div>
      ${builtin?'':`<button class="btn primary" onclick="saveTemplate()">保存模板</button>`}
    </div>`)
}
async function saveTemplate(){
  const name=document.getElementById('tplName').value.trim();
  const yaml=document.getElementById('tplYaml').value;
  if(!name)return toast('请填写模板名',true);
  try{
    const d=await api('/api/template',{method:'POST',body:JSON.stringify({name:name,yaml:yaml})});
    closeModal();
    await fetchTemplates();
    toast('模板已保存：'+d.name+'（'+d.node_count+' 节点）')
  }catch(e){toast(e.message,true)}
}
async function populateTemplateSelect(selId='newTemplate'){
  try{
    const ts=await fetchTemplates();
    const sel=document.getElementById(selId);
    if(!sel||!ts.length)return;
    sel.innerHTML=ts.map(t=>`<option value="${esc(t.id)}"${t.id==='software-development-v1'?' selected':''}>${esc(t.id)} · ${esc(t.label||'')}（${t.node_count} 节点）</option>`).join('')
  }catch(e){}
}
function showOpsCenter(){
  state.opsMode=true;
  saveViewState();
  syncOpsUi();
  document.getElementById('projectTitle').textContent='运维驾驶舱';
  document.getElementById('workflowSubject').textContent='四层运维视图 · 每 10 分钟自动刷新';
  document.getElementById('workflowSub').textContent='';
  document.getElementById('workflowSwitcher').style.display='none';
  document.getElementById('stages').innerHTML='';
  document.getElementById('tasks').innerHTML='<div class="empty">正在加载运维数据…</div>';
  loadOpsCenter()
}
async function exitOpsCenter(){
  state.opsMode=false;
  saveViewState();
  syncOpsUi();
  await refreshAll()
}
async function loadOpsCenter(){
  try{
    const ops=await api('/api/ops-center?include_tasks=1');
    if(!state.opsMode)return;
    state.ops=ops;
    renderOpsCenter();
  }catch(e){
    if(state.opsMode){
      toast(e.message,true);
      const tasksEl=document.getElementById('tasks');
      if(tasksEl){
        tasksEl.innerHTML=`<div class="panel" style="padding:32px 20px;text-align:center;margin:16px"><div style="font-size:16px;font-weight:700;color:var(--bad);margin-bottom:8px">运维驾驶舱数据加载失败</div><div class="task-meta" style="margin-bottom:16px">${esc(e.message)}</div><button class="btn primary" onclick="loadOpsCenter()">点击重试</button></div>`;
      }
    }
  }
}
function renderOpsCenter(){
  const d=state.ops||{},b=d.boss||{},cards=d.workflow_cards||[],fleet=d.agent_fleet||[],anoms=d.anomalies||[];
  document.getElementById('mProjects').textContent=b.running_workflows||0;
  document.getElementById('mWorkflows').textContent=b.working_agents||0;
  document.getElementById('mAgents').textContent=b.attention_required||0;
  document.getElementById('mAlerts').textContent=anoms.length;
  const mSpans=document.querySelectorAll('.metrics .metric span');
  if(mSpans[0])mSpans[0].textContent='运行工作流';if(mSpans[1])mSpans[1].textContent='活跃执行者';if(mSpans[2])mSpans[2].textContent='需要关注';if(mSpans[3])mSpans[3].textContent='异常告警';
  document.getElementById('projects').innerHTML='<button class="project active" onclick="showOpsCenter()"><strong>运维驾驶舱</strong><small>老板视角 · 执行者舰队 · 异常中心</small></button>';
  document.getElementById('tasks').innerHTML=`<div class="ops-section"><h3>工作流 / 阶段节点</h3>${cards.length?cards.map(c=>`<div class="ops-card"><div class="ops-card-head"><strong>${esc(c.workflow_label||c.workflow_id)}</strong><span>${formatElapsed(c.runtime_seconds)}</span></div><div class="task-meta">${esc(c.workflow_id)} · ${c.tasks.active} 运行 · ${c.tasks.completed} 完成 · ${c.tasks.blocked} 阻塞 · ${c.tasks.failed} 失败</div><div class="ops-nodes">${c.nodes.map(n=>`<span class="badge ${esc(n.status)}">${esc(n.node_label)} · ${esc(humanStatus(n.status))}</span>`).join('')}</div></div>`).join(''):'<div class="empty">暂无运行中的工作流</div>'}</div><div class="ops-section"><h3>执行者舰队</h3><div class="fleet-table"><div class="fleet-header"><span>执行者</span><span>健康状态</span><span>当前任务</span><span>负载</span><span>运行耗时</span><span>最后结果</span></div>${fleet.length?fleet.map(a=>`<div class="fleet-row"><strong>${esc(a.agent)}</strong><span class="${opsHealthClass(a.health)}">${esc(healthLabel(a.health))}</span><span class="task-id" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(a.current_task||'—')}</span><span>负载 ${a.load}</span><span>${formatElapsed(a.runtime_seconds)}</span><span class="task-meta" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(a.last_result||'—')}</span></div>`).join(''):'<div class="empty" style="grid-column:span 6">暂无执行者数据</div>'}</div></div>`;
  document.querySelectorAll('.ops-card').forEach((el,i)=>{el.style.cursor='pointer';el.onclick=()=>openWorkflowFromOps(cards[i].workflow_id)});
  document.getElementById('agents').innerHTML='<div class="empty">运行时状态、任务注册表状态和最后事件已合并到执行者舰队。</div>';
  document.getElementById('slots').innerHTML='<div class="empty">点击现有工作流页面可进入工位 / 任务 详情。</div>';
  document.getElementById('alerts').innerHTML=anoms.length?anoms.map(a=>`<div class="alert-row"><div><strong>${esc(a.kind)}</strong><div class="task-meta">${esc(a.task_id||'')} · ${esc(a.agent||'')} · ${esc(a.last_event||'')}</div></div><button class="mini" onclick="showOpsAnomaly(${JSON.stringify(a).replaceAll('"','&quot;')})">处理</button></div>`).join(''):'<div class="empty">暂无异常</div>';
}
function showOpsAnomaly(a){openModal(a.kind,`<div class="task-meta">${esc(a.task_id||'')} · ${esc(a.agent||'')} · ${esc(a.node||'')}</div><div class="task-meta" style="margin:8px 0">最后事件：${esc(a.last_event||'—')}</div><div class="actions">${(a.actions||[]).map(x=>`<button class="mini" onclick="toast('建议操作：${esc(x)}')">${esc(x)}</button>`).join('')}</div><pre style="margin-top:16px">${esc(JSON.stringify(a.links||{},null,2))}</pre>`)}
async function openWorkflowFromOps(id){
  state.opsMode=false;
  syncOpsUi();
  state.workflowId=id;
  saveViewState();
  try{
    await loadWorkflow(id);
    const project=(state.workflow&&state.workflow.project)||{};
    if(project.project_id)state.projectId=project.project_id;
    if(project.workspace_id)state.spaceId=project.workspace_id;
    await refreshAll()
  }catch(e){
    toast(e.message,true);
    state.opsMode=true;
    saveViewState();
    syncOpsUi();
    await loadOpsCenter()
  }
}

async function refreshAll(){
  try{
    syncOpsUi();
    if(state.opsMode){await loadOpsCenter();return}
    state.overview=await api('/api/overview');
    if(state.opsMode){await loadOpsCenter();return}
    const ss=state.overview.spaces||[];

    if(!state.spaceId||!ss.some(s=>s.workspace_id===state.spaceId)){
      const preferred=ss.find(s=>s.relation==='current_factory')||ss[0];
      state.spaceId=preferred?preferred.workspace_id:null
    }

    renderOverview();
    if(state.spaceId)await selectSpace(state.spaceId,false)
  }catch(e){toast(e.message,true)}
}

function renderOverview(){
  const o=state.overview;
  const ss=o.spaces||[];
  const alerts=visibleAlerts();

  document.getElementById('mProjects').textContent=ss.length;
  document.getElementById('mWorkflows').textContent=o.active_workflows;
  document.getElementById('mAgents').textContent=o.active_agents;
  document.getElementById('mAlerts').textContent=alerts.length;
  const mSpans=document.querySelectorAll('.metrics .metric span');
  if(mSpans[0])mSpans[0].textContent='项目空间';if(mSpans[1])mSpans[1].textContent='活跃工作流';if(mSpans[2])mSpans[2].textContent='活跃执行者';if(mSpans[3])mSpans[3].textContent='需要关注';

  document.getElementById('projects').innerHTML=ss.length?ss.map(s=>`
    <button class="project ${s.workspace_id===state.spaceId?'active':''}" onclick="selectSpace('${esc(s.workspace_id)}')">
      <div style="display:flex;justify-content:space-between;gap:8px;align-items:center">
        <strong>${esc(s.label||s.workspace_id)}</strong>
        <span class="${relationClass(s)}" style="font-size:11px">${esc(relationText(s))}</span>
      </div>
      <small>${esc(s.workspace_id)} · ${s.tab_count||0} 个工作流节点 · ${s.pane_count||0} 个智能体工位</small>
      <small>${esc(s.project_root||'未识别项目目录')}</small>
    </button>`).join(''):'<div class="empty">暂无项目空间</div>';

  document.getElementById('alerts').innerHTML=alerts.length?alerts.map(a=>`
    <div class="alert-row">
      <div>
        <div class="task-name">${esc(taskDisplayName(a))}</div>
        <div class="task-id">${esc(a.task_id||'')}</div>
        <div class="task-meta">${esc(a.agent||'')} · ${esc(a.pane_id||'')}</div>
      </div>
      ${badge(a.status)}
    </div>`).join(''):'<div class="empty">当前项目 / 工作流暂无告警</div>'
}

async function selectSpace(workspaceId,rer=true){
  const s=(state.overview.spaces||[]).find(x=>x.workspace_id===workspaceId);
  if(!s)return;

  state.spaceId=workspaceId;
  state.space=s;

  if(s.relation==='current_factory'&&s.project_id){
    state.projectId=s.project_id;
    await loadProject(s.project_id,false);
    document.getElementById('projectTitle').textContent=s.label||s.project_name||s.workspace_id;
  }else{
    state.project=null;
    state.projectId=s.project_id||null;
    state.workflow=null;
    state.workflowId=null;

    document.getElementById('projectTitle').textContent=s.label||s.workspace_id;
    document.getElementById('workflowSubject').textContent=relationText(s);
    document.getElementById('workflowSub').textContent=
      s.workspace_id+
      (s.factory_workspace_id?` · 当前工厂空间：${s.factory_workspace_id}`:'');
    document.getElementById('workflowSwitcher').style.display='none';

    document.getElementById('stages').innerHTML='';
    if(s.relation==='unregistered'){
      document.getElementById('tasks').innerHTML=`
        <div style="padding:20px;background:var(--card);border:1px solid var(--line);border-radius:14px;margin:16px">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color:var(--accent)"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"></path></svg>
            <strong style="font-size:16px">按模板装配为工厂空间</strong>
          </div>
          <div class="muted" style="font-size:13px;line-height:1.6;margin-bottom:16px">
            当前终端空间（${esc(s.workspace_id)}）为独立终端，尚未接入工厂自动化流水线。<br>
            点击下方按钮，工厂将就地为您装配 <b>1总指挥</b> 与各阶段标准工位（阶段标签页与锚点工位），将其注册为该项目的当前工厂车间。
          </div>
          <div class="form" style="max-width:540px">
            <label for="adoptPath">项目代码根目录</label>
            <input id="adoptPath" value="${esc(s.project_root||'')}" placeholder="/path/to/project">
            <label for="adoptTemplate">装配流水线模板</label>
            <select id="adoptTemplate"></select>
            <button class="btn primary" style="margin-top:8px" onclick="submitAdoptSpace('${esc(s.workspace_id)}')">一键按模板装配为工厂空间</button>
          </div>
        </div>`;
      populateTemplateSelect('adoptTemplate');
      document.getElementById('agents').innerHTML='<div class="empty">装配接入工厂后，即可配置并查看执行者阵容。</div>';
      document.getElementById('slots').innerHTML='<div class="empty">装配接入工厂后，即可调度常驻智能体工位。</div>';
    }else{
      document.getElementById('tasks').innerHTML=`
        <div class="empty">
          <div style="font-weight:650;margin-bottom:8px">${esc(relationText(s))}</div>
          <div>项目目录：${esc(s.project_root||'未识别')}</div>
          <div style="margin-top:4px">${s.tab_count||0} 个工作流节点 · ${s.pane_count||0} 个智能体工位</div>
          ${s.factory_workspace_id?`<div style="margin-top:8px">该项目当前工厂空间：${esc(s.factory_workspace_id)}</div>`:''}
          <div style="margin-top:8px">该空间仅展示，不参与当前自动工作流调度。</div>
        </div>`;
      document.getElementById('agents').innerHTML='<div class="empty">历史空间不参与当前工厂执行者阵容。</div>';
      document.getElementById('slots').innerHTML='<div class="empty">只有当前工厂空间才显示可调度的常驻智能体工位。</div>';
    }
  }

  if(rer)renderOverview();
  saveViewState()
}

async function loadProject(id,rer=true){state.projectId=id;state.project=await api('/api/project?id='+encodeURIComponent(id));if(rer&&state.overview)renderOverview();document.getElementById('projectTitle').textContent=state.project.project.project_name;const w=state.project.workflows;if(!state.workflowId||!w.some(x=>x.workflow_id===state.workflowId))state.workflowId=state.project.latest_workflow_id;renderAgents();renderSlots();renderWorkflowSwitcher();if(state.workflowId)await loadWorkflow(state.workflowId);else clearWorkflow()}
function workflowSubject(w){return (w&&(w.title||w.requirement_subject))||''}
function workflowDisplayName(w){
  if(!w)return '';
  const subj=workflowSubject(w);
  return subj?`${subj} (${w.workflow_id})`:w.workflow_id;
}
function renderWorkflowHead(w){
  const subj=workflowSubject(w);
  document.getElementById('workflowSubject').textContent=subj||w.workflow_id;
  const a=state.workflow.agent_override||'auto';
  const parts=[];
  parts.push('工作流 '+w.workflow_id);
  parts.push('执行者 '+(a==='auto'?'自动分配':a));
  if(w.candidate_branch)parts.push('候选分支 '+w.candidate_branch);
  document.getElementById('workflowSub').textContent=parts.join(' · ')
}
function renderWorkflowSwitcher(){
  const box=document.getElementById('workflowSwitcher'),ws=(state.project&&state.project.workflows)||[];
  const sig=state.workflowId+'#'+ws.map(x=>x.workflow_id+':'+workflowDisplayName(x)).join('|');
  if(box.dataset.sig===sig){box.style.display=ws.length<2?'none':'flex';return}
  box.dataset.sig=sig;
  if(ws.length<2){box.style.display='none';box.innerHTML='';return}
  box.style.display='flex';
  box.innerHTML='<label>工作流</label><select id="wfSelect" onchange="state.workflowId=this.value;loadWorkflow(this.value)">'+ws.map(x=>`<option value="${esc(x.workflow_id)}"${x.workflow_id===state.workflowId?' selected':''}>${esc(workflowDisplayName(x))}</option>`).join('')+'</select>'
}
async function loadWorkflow(id){state.workflowId=id;state.workflow=await api('/api/workflow?id='+encodeURIComponent(id));const w=state.workflow.workflow;saveViewState();renderWorkflowHead(w);renderStages();renderTasks()}
function clearWorkflow(){state.workflow=null;state.workflowId=null;saveViewState();document.getElementById('workflowSubject').textContent='暂无工作流';document.getElementById('workflowSub').textContent='';document.getElementById('stages').innerHTML='';const ab=document.getElementById('attentionBanner');if(ab)ab.style.display='none';document.getElementById('tasks').innerHTML='<div class="empty">暂无任务</div>'}
function setTaskFilter(f){state.taskFilter=f;['All','Decision','Attention','Active'].forEach(k=>{const el=document.getElementById('f'+k);if(el)el.classList.toggle('active',f.toLowerCase()===k.toLowerCase())});renderTasks()}
function updateAttentionHub(){
  const ts=(state.workflow&&state.workflow.tasks)||[];
  const cntAll=ts.length;
  const decisionTasks=ts.filter(t=>t.stage_verdict==='blocked'||t.status==='blocked'||t.node_type==='gate');
  const attentionTasks=ts.filter(t=>['failed','interrupted','rework'].includes(t.status)||(t.blocker&&t.blocker.length));
  const activeTasks=ts.filter(t=>['dispatched','working','rework','paused'].includes(t.status));
  const elAll=document.getElementById('cntAll');if(elAll)elAll.textContent=cntAll;
  const elDec=document.getElementById('cntDecision');if(elDec)elDec.textContent=decisionTasks.length;
  const elAtt=document.getElementById('cntAttention');if(elAtt)elAtt.textContent=attentionTasks.length;
  const elAct=document.getElementById('cntActive');if(elAct)elAct.textContent=activeTasks.length;
  const ab=document.getElementById('attentionBanner');
  if(!ab)return;
  if(!state.workflowId||state.opsMode){ab.style.display='none';return}
  ab.style.display='flex';
  const readyCnt=Math.max(0,cntAll-decisionTasks.length-attentionTasks.length);
  const stall=state.workflow&&state.workflow.stall;
  if(stall&&stall.is_stalled){
    ab.style.background='linear-gradient(90deg, #3d1c06 0%, #1f140a 100%)';
    ab.style.borderColor='var(--warn)';
    let actBtn='';
    if(stall.suggested_action==='force_review'&&stall.target_task_id){
      actBtn=`<button class="btn primary" style="background:#d97706;border-color:#b45309;padding:4px 10px;font-size:12px" onclick="forceReviewTask('${stall.target_task_id}')">🔔 立即唤醒评审</button>`;
    }else if(stall.suggested_action==='retry_advance'){
      actBtn=`<button class="btn primary" style="background:#2563eb;border-color:#1d4ed8;padding:4px 10px;font-size:12px" onclick="retryStageAdvance('${state.workflowId}')">⚡ 尝试推进阶段</button>`;
    }
    ab.innerHTML=`<div style="display:flex;align-items:center;gap:10px"><span class="att-badge" style="background:var(--warn);color:#000">推进停滞告警</span><span class="att-text" style="color:#fef08a">⚠️ ${esc(stall.message)}</span></div><div>${actBtn}</div>`;
  }else{
    ab.style.background='linear-gradient(90deg,#14243a 0%,#111a26 100%)';
    ab.style.borderColor='var(--accent)';
    ab.innerHTML=`<div style="display:flex;align-items:center;gap:10px"><span class="att-badge">人机协同态势</span><span class="att-text">${activeTasks.length} 个执行者正在协同 · ${readyCnt} 个正常推进 · ${attentionTasks.length} 个需关注 · <strong style="color:${decisionTasks.length?'var(--accent)':'var(--text)'}">${decisionTasks.length} 个待你拍板</strong></span></div><div>${decisionTasks.length?`<button class="btn primary" style="padding:4px 10px;font-size:12px" onclick="setTaskFilter('decision')">立即拍板</button>`:''}</div>`;
  }
}
function renderStages(){
  const ss=(state.workflow&&state.workflow.stages)||[];
  document.getElementById('stages').innerHTML=ss.map((s,i)=>{
    const act=['working','finalizing','failed','blocked'].includes(s.status);
    return `
    <div class="stage ${act?'stage-active':''}">
      <div class="stage-head">阶段 ${i+1}</div>
      <strong>${esc(s.label)}</strong>
      ${badge(s.status)}
      <div class="task-meta">${s.count} 个任务</div>
    </div>`;
  }).join('')
}
function renderTasks(){
  const e=document.getElementById('tasks');
  const allTs=(state.workflow&&state.workflow.tasks)||[];
  updateAttentionHub();
  let ts=allTs;
  const f=state.taskFilter||'all';
  if(f==='decision'){
    ts=ts.filter(t=>t.stage_verdict==='blocked'||t.status==='blocked'||t.node_type==='gate');
  }else if(f==='attention'){
    ts=ts.filter(t=>['failed','interrupted','rework'].includes(t.status)||(t.blocker&&t.blocker.length));
  }else if(f==='active'){
    ts=ts.filter(t=>['dispatched','working','rework','paused'].includes(t.status));
  }
  if(!ts.length){e.innerHTML=`<div class="empty">${f==='all'?'暂无任务':'当前筛选无匹配任务'}</div>`;return}
  e.innerHTML=ts.map(t=>`
    <div class="task" data-task-id="${esc(t.task_id)}">
      <div>
        <div class="task-name">${esc(taskDisplayName(t))}</div>
        <div class="task-id">任务 ID：${esc(t.task_id)}</div>
        <div class="task-meta">${esc(t.agent||'未分配执行者')} · ${esc(t.pane_id||'未分配工位')} · ${esc(t.pane_source||'')}</div>
      </div>
      <div class="task-actions">
        ${badge(t.status)}
        <button class="mini" onclick="showTask('${esc(t.task_id)}')">简报</button>
        <button class="mini" style="color:var(--accent);font-weight:600" onclick="openSignoffChamber('${esc(t.task_id)}')">成果会签</button>
        <button class="mini" onclick="showPane('${esc(t.pane_id||'')}')">工位</button>
        ${['working','dispatched','rework','blocked','paused'].includes(t.status)?`<button class="mini" style="color:var(--accent)" onclick="showSteerModal('${esc(t.task_id)}')">插话</button><button class="mini" style="color:var(--bad)" onclick="haltTaskPrompt('${esc(t.task_id)}')">制动</button>`:''}
        ${t.status==='rework'?`<button class="mini" style="color:var(--warn);font-weight:600" onclick="forceReviewTask('${esc(t.task_id)}')">唤醒评审</button>`:''}
        <button class="mini" onclick="askCoordinator('${esc(t.task_id)}')">让总指挥处理</button>
        ${t.stage_verdict==='blocked'?`<button class="mini" style="color:var(--accent)" onclick="forcePassTask('${esc(t.workflow_id||state.workflowId)}','${esc(t.node||t.stage)}')">强制放行</button>`:''}
      </div>
    </div>`).join('')
}
function renderAgents(){const rs=state.project.agents||[];document.getElementById('agents').innerHTML=rs.length?rs.map(a=>`<div class="agent-row"><span><i class="dot ${esc(a.status)}"></i>${esc(a.agent)}</span><span class="muted">${esc(agentStatusLabel(a.status))} · 负载 ${a.load} · 认证 ${esc(authHintLabel(a.auth_hint))}</span></div>`).join(''):'<div class="empty">暂无执行者信息</div>'}function renderSlots(){const rs=state.project.slots||[];document.getElementById('slots').innerHTML=rs.length?rs.map(s=>`<div class="slot-row"><div><div>${esc(s.pane_id)} · ${esc(s.stage_label)}</div><div class="task-meta">绑定 ${esc(s.bound_agent)} · 运行时 ${esc(s.live_agent||'空闲')} · ${esc(s.claimed_by?'被任务占用':'未占用')}</div></div><button class="mini" onclick="bindSlotPrompt('${esc(s.pane_id)}')">绑定</button></div>`).join(''):'<div class="empty">暂无用户预建智能体工位</div>'}
function openModal(t,h){document.getElementById('modalTitle').textContent=t;document.getElementById('modalBody').innerHTML=h;document.getElementById('modal').classList.add('open')}function closeModal(){document.getElementById('modal').classList.remove('open')}
function autoFillWorkflowTitle(){
  const ti=document.getElementById('newTitle');
  if(!ti||ti.value.trim())return;
  const req=(document.getElementById('newRequirement')?.value||'').trim();
  if(!req)return;
  for(const line of req.split(/[\r\n]+/)){
    const clean=line.replace(/^(#{1,6}\s+|[-*+]+\s+|\d+[.、)]\s*|\[[ xX]\]\s*)+/,'').trim();
    if(clean&&!clean.startsWith('## 需求')&&clean!=='需求说明'&&clean!=='需求'){
      ti.value=clean.slice(0,50);
      break;
    }
  }
}
function showNewProjectModal(){
  openModal('新建工厂空间',`<div class="form"><label for="newProjPath">本地 Git 仓库路径</label><input id="newProjPath" placeholder="例如：/Users/user/my-app" oninput="autoFillProjectName()"><label for="newProjName">项目名称（可选）</label><input id="newProjName" placeholder="默认与文件夹同名"><label for="newProjTemplate">选用流水线模板</label><select id="newProjTemplate"></select><button class="btn primary" style="margin-top:8px" onclick="submitNewProject()">创建并装配工厂空间</button></div>`);
  populateTemplateSelect('newProjTemplate');
}
function autoFillProjectName(){
  const p=(document.getElementById('newProjPath')?.value||'').trim().replace(/[/\\]+$/,'');
  const nameInput=document.getElementById('newProjName');
  if(!nameInput)return;
  if(p){
    const segs=p.split(/[/\\]/);
    nameInput.value=segs[segs.length-1]||'';
  }
}
async function submitNewProject(){
  const path=(document.getElementById('newProjPath')?.value||'').trim();
  const name=(document.getElementById('newProjName')?.value||'').trim();
  const tmpl=document.getElementById('newProjTemplate')?.value||'software-development-v1';
  if(!path)return toast('请输入项目 Git 路径',true);
  try{
    toast('正在开辟并装配工厂空间…');
    const res=await api('/api/project/create',{method:'POST',body:JSON.stringify({project_root:path,project_name:name,template:tmpl})});
    closeModal();
    toast(res.already_registered?'项目已存在，已为您定位到该工厂空间':'工厂空间装配完成！');
    if(res.workspace_id){
      state.spaceId=res.workspace_id;
      saveViewState();
    }
    await refreshAll();
  }catch(e){
    toast('创建失败：'+e.message,true);
  }
}
async function submitAdoptSpace(wid){
  const path=(document.getElementById('adoptPath')?.value||'').trim();
  const tmpl=document.getElementById('adoptTemplate')?.value||'software-development-v1';
  try{
    toast('正在装配此空间为工厂车间…');
    const res=await api('/api/project/adopt',{method:'POST',body:JSON.stringify({workspace_id:wid,project_root:path,template:tmpl})});
    toast('空间装配完成，已接入工厂！');
    if(res.workspace_id){
      state.spaceId=res.workspace_id;
      saveViewState();
    }
    await refreshAll();
  }catch(e){
    toast('装配失败：'+e.message,true);
  }
}
function showUnregisterProjectModal(){
  if(!state.project||!state.projectId)return toast('请先选择当前工厂项目',true);
  const p=state.project.project;
  openModal('注销工厂项目',`<div style="line-height:1.6"><div style="font-size:15px;font-weight:650;margin-bottom:8px">确定要注销项目【${esc(p.project_name||p.project_id)}】吗？</div><div class="muted" style="font-size:13px;margin-bottom:14px">项目目录：<code>${esc(p.project_root)}</code><br>当前空间：<b>${esc(p.workspace_id||'')}</b></div><div style="background:#131a23;border:1px solid var(--line);border-radius:10px;padding:12px;font-size:12px;margin-bottom:14px"><div style="font-weight:600;margin-bottom:4px">注销影响说明：</div><div>1. <b>本地代码仓库绝对不碰</b>，保留所有代码与 Git 提交。</div><div>2. 工厂调度器将停止对该项目的自动任务派发与状态巡检。</div><div>3. 终端空间将从【当前工厂 <span class="dot ready" style="vertical-align:middle"></span>】退回为【独立终端 <span class="dot working" style="vertical-align:middle"></span>】。</div></div><label for="unregCloseSpace" style="display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer;margin-bottom:16px"><input type="checkbox" id="unregCloseSpace"> 同时关闭终端工作区（仅关闭窗口标签页，不删除代码）</label><div style="display:flex;justify-content:flex-end;gap:8px"><button class="btn" onclick="closeModal()">取消</button><button class="btn danger-btn" onclick="submitUnregisterProject()">确认注销</button></div></div>`);
}
async function submitUnregisterProject(){
  if(!state.project||!state.projectId)return;
  const closeSpace=Boolean(document.getElementById('unregCloseSpace')?.checked);
  try{
    toast('正在注销工厂项目…');
    await api('/api/project/unregister',{method:'POST',body:JSON.stringify({project_id:state.projectId,close_workspace:closeSpace})});
    closeModal();
    toast('项目已注销成功');
    state.projectId=null;
    state.project=null;
    state.spaceId=null;
    saveViewState();
    await refreshAll();
  }catch(e){
    toast('注销失败：'+e.message,true);
  }
}
function showNewWorkflow(){
  if(!state.project||!state.space||state.space.relation!=='current_factory')return toast('请先选择当前工厂空间',true);
  openModal('新建需求',`<div class="form"><label for="newProjName">项目</label><input id="newProjName" value="${esc(state.project.project.project_name)}" disabled><label for="newTitle">本次任务名称</label><input id="newTitle" placeholder="例如：适配深色模式切换 / 修复结算页面浮点精度 Bug"><label for="newTemplate">工作流模板</label><select id="newTemplate"><option value="software-development-v1">software-development-v1（默认软件开发）</option></select><label for="newAgent">执行者策略</label><select id="newAgent"><option value="auto">auto（Router 自动）</option>${['opencode','codex','claude','qodercli','agy','pi','grok'].map(a=>`<option>${a}</option>`).join('')}</select><label for="newRequirement">自然语言需求</label><textarea id="newRequirement" onblur="autoFillWorkflowTitle()"></textarea><button class="btn primary" onclick="submitNewWorkflowAsync()">启动工作流</button><div id="runWaitStatus" class="muted" style="min-height:16px;font-size:12px"></div></div>`);
  populateTemplateSelect()
}
async function runPreflight(){
  if(!state.projectId)return toast('当前空间不参与工厂调度',true);
  try{
    toast('正在执行深度自检…');
    const d=await api('/api/deep-preflight?id='+encodeURIComponent(state.projectId));
    const rows=d.agents||[];

    const statusLabel=s=>({
      READY:'就绪',
      DISABLED:'已禁用',
      UNKNOWN:'未知',
      MISSING:'未安装',
      WARN:'警告',
      TOKEN_EXHAUSTED:'额度耗尽',
      AUTH_REQUIRED:'需要认证',
      PROVIDER_ERROR:'服务端繁忙',
      LOCAL_ERROR:'本地异常',
      TRUST_REQUIRED:'需要信任',
      UPDATE_BLOCKED:'更新受阻',
      TIMEOUT:'超时',
      ERROR:'错误'
    })[s]||s;

    const hard=new Set([
      'TOKEN_EXHAUSTED','AUTH_REQUIRED','PROVIDER_ERROR','LOCAL_ERROR','TRUST_REQUIRED',
      'UPDATE_BLOCKED','TIMEOUT','ERROR','MISSING'
    ]);

    const html='<div class="muted" style="margin-bottom:8px">'
      +'真实最小调用仅用于已确认安全非交互模式的执行者；'
      +'UNKNOWN 表示尚未配置安全适配器，不代表不可用。'
      +'TIMEOUT 可能是慢而非不可用（claude 冷启动常需 40–60s，已自动重试 1 次），请结合下方原始输出判断，必要时点一次“执行者自检”重试。'
      +'</div><div>'
      +rows.map(a=>{
        const deep=a.deep||{};
        const final=a.final_status||a.shallow_status||'UNKNOWN';
        const cls=final==='READY'?'good-text':(hard.has(final)?'danger-text':'muted');
        const note=deep.note||a.version||a.binary||'';
        const adapter=deep.adapter?(' · '+esc(deep.adapter)):'';
        const out=(deep.output||'').slice(-800);
        const outHtml=out?('<pre class="task-meta" style="white-space:pre-wrap;max-height:120px;overflow:auto;background:#080b0f;padding:6px 8px;border-radius:8px;margin-top:4px">'+esc(out)+'</pre>'):'';
        return '<div class="agent-row">'
          +'<div><div><strong>'+esc(a.agent)+'</strong> '
          +'<span class="'+cls+'">'+esc(statusLabel(final))+'</span></div>'
          +'<div class="task-meta">'+esc(note)+adapter+'</div>'+outHtml+'</div>'
          +'<div class="task-meta">'+esc(authHintLabel(a.auth_hint||'unknown'))+'</div>'
          +'</div>';
      }).join('')
      +'</div>';

    openModal('执行者深度自检',html);
    toast('深度自检完成');
  }catch(e){
    toast(e.message,true);
  }
}
function showAgentOverride(){if(!state.workflowId)return toast('当前没有工作流',true);const cur=state.workflow.agent_override||'auto';openModal('指定后续任务执行者',`<div class="form"><label for="overrideAgent">执行者策略</label><select id="overrideAgent">${['auto','opencode','codex','claude','qodercli','agy','pi','grok'].map(a=>`<option ${a===cur?'selected':''}>${a}</option>`).join('')}</select><button class="btn primary" onclick="saveAgentOverride()">保存</button><div class="muted">只影响后续新建任务。</div></div>`)}async function saveAgentOverride(){try{await api('/api/workflow/agent',{method:'POST',body:JSON.stringify({workflow_id:state.workflowId,agent:document.getElementById('overrideAgent').value})});closeModal();await loadWorkflow(state.workflowId);toast('执行者策略已更新')}catch(e){toast(e.message,true)}}async function showTask(id){try{const [td,proj]=await Promise.all([api('/api/task?id='+encodeURIComponent(id)).catch(()=>null),api('/api/task/projection?id='+encodeURIComponent(id)).catch(()=>null)]);const d=proj||(td&&td.task)||{};const raw=td||proj||{};const st=d.status||(td&&td.task&&td.task.status)||'unknown';const intent=d.intent||(td&&td.task&&td.task.goal)||'无明确意图描述';const blockers=Array.isArray(d.blockers)?d.blockers:(d.blocker?[d.blocker]:[]);const ms=Array.isArray(d.milestones)?d.milestones:[];const arts=Array.isArray(d.artifacts)?d.artifacts:[];const acts=Array.isArray(d.recent_activity)?d.recent_activity:(typeof d.recent_activity==='string'&&d.recent_activity?d.recent_activity.split('\n'):[]);const blkHtml=blockers.length?`<div class="proj-blk"><strong>⚠️ 卡点告警:</strong><span>${esc(blockers.join('; '))}</span></div>`:'';const msHtml=ms.length?`<div class="proj-sec"><div class="proj-lbl">动态路标</div>${ms.map(m=>`<div class="proj-ms"><span class="proj-ms-dot ${m.status}">${m.status==='completed'?'✓':(m.status==='in_progress'?'›':'·')}</span><span style="${m.status==='completed'?'color:var(--text)':(m.status==='in_progress'?'color:var(--warn);font-weight:600':'color:var(--muted)')}">${esc(m.label)}</span></div>`).join('')}</div>`:'';const artHtml=arts.length?`<div class="proj-sec"><div class="proj-lbl">核心产物</div>${arts.map(a=>`<div class="proj-art"><div class="proj-art-hd"><span>${esc(a.name||a.kind)}</span><span class="badge ${a.passed?'cleaned':(a.kind==='evaluation'?'failed':'waiting')}">${esc(a.kind)}</span></div><div class="muted">${esc(a.summary||'')}</div></div>`).join('')}</div>`:'';const actHtml=acts.length?`<div class="proj-sec"><div class="proj-lbl">近期动态提炼</div><ul class="proj-acts">${acts.map(a=>`<li>${esc(a)}</li>`).join('')}</ul></div>`:'';const body=`<div class="proj-box"><div style="display:flex;justify-content:space-between;align-items:center;padding-bottom:8px;border-bottom:1px solid var(--line)"><div><span class="badge ${st}">${esc(st)}</span><span style="margin-left:8px;font-size:12px;color:var(--muted)">执行者: <strong>${esc(d.agent||'-')}</strong></span><span style="margin-left:8px;font-size:12px;color:var(--muted)">工位: <strong>${esc(d.node||'-')}</strong></span></div><button class="mini" onclick="const el=document.getElementById('taskRawPre');if(el)el.style.display=el.style.display==='none'?'block':'none'">原始数据</button></div>${blkHtml}<div class="proj-sec"><div class="proj-lbl">当前语义意图</div><div class="proj-txt">${esc(intent)}</div></div>${msHtml}${artHtml}${actHtml}<div id="taskRawPre" style="display:none;margin-top:10px"><div class="proj-lbl">原始调试数据</div><pre>${esc(JSON.stringify(raw,null,2))}</pre></div></div>`;openModal('任务白盒简报 · '+id,body)}catch(e){toast(e.message,true)}}async function showPane(id){if(!id)return toast('没有工位',true);try{const d=await api('/api/pane/read?id='+encodeURIComponent(id));openModal('工位 '+id,`<pre>${esc(d.output)}</pre>`)}catch(e){toast(e.message,true)}}async function askCoordinator(id){try{toast('正在通知总指挥…');await api('/api/task/coordinator',{method:'POST',body:JSON.stringify({task_id:id})});toast('总指挥已处理/接收')}catch(e){toast(e.message,true)}}
async function openSignoffChamber(taskId){try{const [td,proj]=await Promise.all([api('/api/task?id='+encodeURIComponent(taskId)).catch(()=>null),api('/api/task/projection?id='+encodeURIComponent(taskId)).catch(()=>null)]);const d=proj||(td&&td.task)||{};const t=(td&&td.task)||{};const wid=t.workflow_id||state.workflowId;const node=t.node||t.stage||'';const arts=Array.isArray(d.artifacts)?d.artifacts:[];const isBlocked=t.stage_verdict==='blocked'||t.status==='blocked';let artCards='<div class="empty">暂无生成产物</div>';if(arts.length){artCards=arts.map(a=>`<div class="proj-sec" style="margin-bottom:8px"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px"><strong>${esc(a.name||a.kind)}</strong><span class="badge ${a.passed?'cleaned':(a.kind==='evaluation'?'failed':'waiting')}">${esc(a.kind)}</span></div><div class="task-meta" style="margin-bottom:6px">${esc(a.path||'')}</div><div class="proj-txt" style="background:#080b0f;padding:8px 10px;border-radius:8px;font-family:ui-monospace,Menlo,monospace;font-size:12px;max-height:160px;overflow:auto">${esc(a.content||a.summary||'（文件产物记录正常）')}</div></div>`).join('')}const html=`<div class="signoff-box"><div class="signoff-head"><div><div style="font-size:16px;font-weight:700">${esc(taskDisplayName(t))}</div><div class="task-meta">任务 ID: ${esc(taskId)} · 执行者: <b>${esc(t.agent||'-')}</b> · 节点: <b>${esc(node)}</b></div></div><div>${badge(t.status)}</div></div>${isBlocked?'<div class="proj-blk"><strong>⚠️ 门禁会签等待:</strong> 当前节点触发门禁阻断，需要人类总指挥核查产物并决策放行或打回。</div>':''}<div class="proj-sec"><div class="proj-lbl">核心交付物与成果列表</div>${artCards}</div><div class="form"><label for="signoffFeedback">审批意见 / 批注说明（可选）</label><input id="signoffFeedback" placeholder="例如：数据核准，批准通过；或：海外收入拆解不全，请补充"></div><div class="signoff-actions"><button class="btn" onclick="closeModal()">暂不处理</button><button class="btn danger-btn" onclick="submitSignoffDecision(\'${esc(taskId)}\',\'${esc(wid)}\',\'${esc(node)}\',\'reject\')">批注打回</button><button class="btn primary" onclick="submitSignoffDecision(\'${esc(taskId)}\',\'${esc(wid)}\',\'${esc(node)}\',\'approve\')">通过并放行</button></div></div>`;openModal('成果交付会签室 (Artifact Signoff Chamber)',html)}catch(e){toast(e.message,true)}}
async function submitSignoffDecision(taskId,wid,node,act){const feedback=(document.getElementById('signoffFeedback')?.value||'').trim();closeModal();try{toast(act==='approve'?'正在通过并放行…':'正在批注打回…');const res=await api('/api/task/signoff',{method:'POST',body:JSON.stringify({task_id:taskId,workflow_id:wid,node:node,action:act,feedback:feedback,operator:'总指挥'})});if(res.ok){await loadWorkflow(wid);toast(act==='approve'?'已通过并放行门禁！':'已完成批注打回，已回退至上游重新推进')}else{toast('操作失败: '+(res.error||'未知错误'),true)}}catch(e){toast(e.message,true)}}
async function forceReviewTask(tid){
  try{
    toast('正在唤醒评审…');
    await api('/api/task/force-review',{method:'POST',body:JSON.stringify({task_id:tid})});
    toast('已将工位推进至 agent_done 并促醒协调器验收');
    refreshAll();
  }catch(e){toast('唤醒评审失败: '+e.message,true)}
}
async function retryStageAdvance(wid){
  try{
    toast('正在尝试推进阶段…');
    await api('/api/workflow/retry-advance',{method:'POST',body:JSON.stringify({workflow_id:wid||state.workflowId})});
    toast('已触发阶段推进');
    refreshAll();
  }catch(e){toast('推进失败: '+e.message,true)}
}
function toggleDeepDrawer(){const d=document.getElementById('deepDrawer');if(!d)return;const isCollapsed=d.classList.contains('collapsed');d.classList.toggle('collapsed');const txt=document.getElementById('drawerToggleText');if(txt)txt.textContent=isCollapsed?'▼ 折叠收起':'▲ 展开抽屉';if(isCollapsed)refreshDeepDrawer()}

function switchDrawerTab(tab){state.drawerTab=tab;['tty','logs','raw'].forEach(t=>{const el=document.getElementById('dtab'+t.charAt(0).toUpperCase()+t.slice(1));if(el)el.classList.toggle('active',t===tab)});refreshDeepDrawer()}
async function refreshDeepDrawer(){const pre=document.getElementById('drawerPre');if(!pre)return;pre.textContent='正在拉取底层现场数据…';try{if(state.drawerTab==='tty'){const ts=(state.workflow&&state.workflow.tasks)||[];const activeTask=ts.find(t=>['working','dispatched','rework','paused'].includes(t.status))||ts[0];const paneId=activeTask?activeTask.pane_id:(state.project&&state.project.slots&&state.project.slots[0]&&state.project.slots[0].pane_id);if(!paneId){pre.textContent='当前暂无活动工位 TTY';return}const d=await api('/api/pane/read?id='+encodeURIComponent(paneId));pre.textContent=`[工位 ${paneId} 实时终端现场]\n`+(d.output||'（工位暂无输出）')}else if(state.drawerTab==='logs'){const d=await api('/api/logs?kind=controller');pre.textContent='[调度器内核日志 Controller Log]\n'+(d.output||'（暂无日志）')}else if(state.drawerTab==='raw'){if(!state.workflowId){pre.textContent='请先选择一个工作流';return}const d=await api('/api/workflow/projection?id='+encodeURIComponent(state.workflowId));pre.textContent=JSON.stringify(d,null,2)}}catch(e){pre.textContent='拉取失败: '+e.message}}
function showSteerModal(tid){openModal('总指挥实时插话纠偏',`<div class="form"><div class="muted" style="margin-bottom:8px">任务 ID：${esc(tid)}</div><label for="steerInput">纠偏或引导指令（将直接注入工位执行者）</label><textarea id="steerInput" rows="3" placeholder="例如：优先使用标准库，不要引入外部第三方包" style="width:100%;box-sizing:border-box;margin-bottom:10px"></textarea><div style="display:flex;align-items:center;gap:8px;margin-bottom:12px"><input type="checkbox" id="steerUrgent" style="width:auto"><label for="steerUrgent" style="margin:0;cursor:pointer"><strong>紧急插话 (立即软打断工位执行者并注入指令)</strong></label></div><button class="btn primary" onclick="submitSteer('${esc(tid)}')">发送指令</button></div>`)}
async function submitSteer(tid){const inst=(document.getElementById('steerInput')?.value||'').trim();const urgent=!!document.getElementById('steerUrgent')?.checked;if(!inst)return toast('请输入指令内容',true);closeModal();try{toast('正在发送干预指令…');const res=await api('/api/task/steer',{method:'POST',body:JSON.stringify({task_id:tid,instruction:inst,urgent:urgent})});toast(res.status==='dispatched'?'指令已立即注入工位！':'指令已排入工位队列，将在间歇注入');await loadWorkflow(state.workflowId)}catch(e){toast(e.message,true)}}
function haltTaskPrompt(tid){showConfirmModal({title:'工位紧急制动 (Halt)',message:'确定要立即打断任务 '+tid+' 的执行吗？系统将向工位发送软中断 (SIGINT)，保留现场并转为受控待命状态。',confirmText:'紧急制动',danger:true,onConfirm:async()=>{try{toast('正在执行紧急制动…');await api('/api/task/halt',{method:'POST',body:JSON.stringify({task_id:tid,reason:'人工在控制台紧急制动'})});toast('工位已安全打断');await loadWorkflow(state.workflowId)}catch(e){toast(e.message,true)}}})}
async function toggleWorkflowPause(){if(!state.workflowId)return toast('当前没有工作流',true);const curSt=state.workflow&&state.workflow.workflow&&state.workflow.workflow.status;const isPaused=curSt==='paused';const act=isPaused?'resume':'pause';const label=isPaused?'恢复自动调度':'暂停自动调度';showConfirmModal({title:label,message:isPaused?'确认恢复该工作流的自动调度推进？':'确认暂停该工作流的自动推进？当前运行中的工位不会被强制终止。',confirmText:label,onConfirm:async()=>{try{await api('/api/kernel/'+act,{method:'POST',body:JSON.stringify({workflow_id:state.workflowId})});await loadWorkflow(state.workflowId);toast('工作流已'+(isPaused?'恢复':'暂停'))}catch(e){toast(e.message,true)}}})}
async function stepWorkflow(){if(!state.workflowId)return toast('当前没有工作流',true);try{toast('正在单步推进…');const res=await api('/api/kernel/step',{method:'POST',body:JSON.stringify({workflow_id:state.workflowId})});if(res.ok){await loadWorkflow(state.workflowId);toast('单步已推进: '+res.stepped_label+' ('+res.stepped_node+')')}else{toast('无法单步推进: '+(res.reason==='no_ready_nodes'?'当前无就绪节点':res.reason),true)}}catch(e){toast(e.message,true)}}
function showRollbackModal(){if(!state.workflowId)return toast('当前没有工作流',true);const stages=(state.workflow&&state.workflow.stages)||[];if(!stages.length)return toast('工作流暂无节点',true);const options=stages.map(s=>`<option value="${esc(s.key)}">${esc(s.label)} (${esc(s.key)})</option>`).join('');openModal('节点回溯 (Rollback)',`<div class="form"><label for="rbTarget">回溯目标节点（该节点及所有下游任务将被重置作废）</label><select id="rbTarget">${options}</select><label for="rbReason">回溯原因</label><input id="rbReason" type="text" value="人工核验需求变更或发现重大缺陷"><button class="btn primary" style="background:#dc2626;border-color:#ef4444" onclick="executeRollback()">确认回溯</button><div class="muted">警告：此操作不可撤销，下游所有产物与任务将被标记为作废。</div></div>`)}
async function executeRollback(){const target=document.getElementById('rbTarget').value;const reason=document.getElementById('rbReason').value;closeModal();try{toast('正在执行回溯…');const res=await api('/api/kernel/rollback',{method:'POST',body:JSON.stringify({workflow_id:state.workflowId,target_node_id:target,reason:reason})});await loadWorkflow(state.workflowId);toast('已成功回溯至 '+target+'，作废 '+res.invalidated_tasks.length+' 个任务')}catch(e){toast(e.message,true)}}
async function forcePassTask(wid,nodeId){showPromptModal({title:'人工强制放行门禁',label:'请输入强制放行的审计说明与依据：',defaultValue:'经人工核验，次要阻断项已评估无害，特批放行',onConfirm:async(note)=>{try{toast('正在强制放行…');await api('/api/kernel/force-pass',{method:'POST',body:JSON.stringify({workflow_id:wid,gate_node_id:nodeId,note:note})});await loadWorkflow(wid);toast('门禁已强制放行')}catch(e){toast(e.message,true)}}})}
async function showCheckpointsModal(){if(!state.workflowId)return toast('当前没有工作流',true);try{const cps=await api('/api/kernel/checkpoints?workflow_id='+encodeURIComponent(state.workflowId));let listHtml='<div class="muted" style="margin-bottom:12px">暂无历史快照</div>';if(cps&&cps.length){listHtml=cps.map(c=>`<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--line)"><div><strong>${esc(c.tag||'无标签')}</strong><div class="task-meta">${esc(c.checkpoint_id)} · ${c.task_count} 任务 · ${new Date(c.created_at*1000).toLocaleString()}</div></div><button class="btn" style="padding:3px 8px;font-size:12px;color:var(--danger)" onclick="restoreWorkflowCheckpoint('${esc(c.checkpoint_id)}')">恢复此快照</button></div>`).join('')}openModal('工作流快照中心',`<div class="form"><div style="display:flex;gap:8px;margin-bottom:14px"><input id="cpTagInput" type="text" placeholder="快照标签（如：修改前基准）" style="flex:1"><button class="btn primary" onclick="createWorkflowCheckpoint()">创建快照</button></div><h4>历史快照</h4><div style="max-height:240px;overflow-y:auto">${listHtml}</div></div>`)}catch(e){toast(e.message,true)}}
async function createWorkflowCheckpoint(){const tag=document.getElementById('cpTagInput')?.value||'';try{toast('正在保存快照…');await api('/api/kernel/checkpoint',{method:'POST',body:JSON.stringify({workflow_id:state.workflowId,tag:tag})});toast('快照已保存');showCheckpointsModal()}catch(e){toast(e.message,true)}}
async function restoreWorkflowCheckpoint(cpid){showConfirmModal({title:'恢复工作流快照',message:'确定要将工作流恢复到快照 '+cpid+' 吗？当前未保存的节点状态将被覆盖。',confirmText:'确认恢复',danger:true,onConfirm:async()=>{try{toast('正在恢复快照…');await api('/api/kernel/checkpoint/restore',{method:'POST',body:JSON.stringify({workflow_id:state.workflowId,checkpoint_id:cpid})});closeModal();await loadWorkflow(state.workflowId);toast('已恢复至快照 '+cpid)}catch(e){toast(e.message,true)}}})}
async function createCandidate(){
  showConfirmModal({
    title:'创建工作流候选分支',
    message:'确定要创建/更新工作流候选分支吗？此操作将汇总实现阶段已通过验收的交付代码，但绝不会直接合入 main。',
    confirmText:'开始构建',
    onConfirm:async()=>{
      try{
        toast('正在构建候选分支…');
        const d=await api('/api/workflow/candidate',{method:'POST',body:JSON.stringify({workflow_id:state.workflowId})});
        await loadWorkflow(state.workflowId);
        toast('候选分支：'+d.candidate_branch);
      }catch(e){toast(e.message,true)}
    }
  });
}
async function advanceStage(){
  showConfirmModal({
    title:'进入下一阶段',
    message:'让总指挥检查当前阶段门禁并尝试推进至下一阶段？',
    confirmText:'确认推进',
    onConfirm:async()=>{
      try{
        toast('正在请求阶段推进…');
        await api('/api/workflow/advance',{method:'POST',body:JSON.stringify({workflow_id:state.workflowId})});
        toast('阶段推进请求已完成');
      }catch(e){toast(e.message,true)}
    }
  });
}
async function showLogs(){try{const d=await api('/api/logs?kind=controller');openModal('控制器日志',`<pre>${esc(d.output)}</pre>`)}catch(e){toast(e.message,true)}}
const ARCHIVE_STATUS_CHOICES=[['archived','已归档'],['all','全部状态'],['active','进行中'],['cleaned','已完成'],['superseded','已取代'],['failed','失败'],['completed','待收尾']];
let archiveQuery={page:0,size:50,project_id:'',workflow_id:'',agent:'',status:'archived',q:''};
function archiveFiltersHtml(){
  const ps=(state.overview&&state.overview.projects)||[];
  return `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin-bottom:10px">`
    +`<select id="arcProject"><option value="">全部项目</option>${ps.map(p=>`<option value="${esc(p.project_id)}"${archiveQuery.project_id===p.project_id?' selected':''}>${esc(p.project_name||p.project_id)}</option>`).join('')}</select>`
    +`<input id="arcWorkflow" placeholder="工作流 ID 片段" value="${esc(archiveQuery.workflow_id)}">`
    +`<select id="arcAgent"><option value="">全部执行者</option>${['opencode','codex','claude','qodercli','agy','pi','grok'].map(a=>`<option${archiveQuery.agent===a?' selected':''}>${esc(a)}</option>`).join('')}</select>`
    +`<select id="arcStatus">${ARCHIVE_STATUS_CHOICES.map(c=>`<option value="${c[0]}"${archiveQuery.status===c[0]?' selected':''}>${c[1]}</option>`).join('')}</select>`
    +`<input id="arcQ" placeholder="关键词：任务/目标/节点" value="${esc(archiveQuery.q)}" onkeydown="if(event.key==='Enter')applyArchiveFilters()">`
    +`</div><div style="display:flex;justify-content:flex-end;gap:8px;margin-bottom:10px"><button class="btn primary" onclick="applyArchiveFilters()">查询</button></div><div id="archiveList"><div class="empty">正在加载…</div></div>`;
}
function archiveRowsHtml(d){
  const items=d.items||[];
  if(!items.length)return '<div class="empty">没有匹配的任务</div>';
  const size=Math.max(1,d.limit||50);
  const pages=Math.max(1,Math.ceil(d.total/size));
  const page=Math.floor(d.offset/size)+1;
  const rows=items.map(t=>{
    const when=t.updated_at?new Date(t.updated_at*1000).toLocaleString():'—';
    return `<div class="task"><div><div class="task-name">${esc(t.node_label||t.node||'任务')} <span class="task-id">${esc(t.task_id)}</span> ${badge(t.status)}</div>`
      +`<div class="task-id">${esc(t.workflow_id||'')}${t.project_name?' · '+esc(t.project_name):''} · 执行者 ${esc(t.agent||'-')}${t.stage_verdict?' · 验收 '+esc(t.stage_verdict):''}${t.superseded_by?' · 取代者 '+esc(t.superseded_by):''}</div>`
      +`<div class="task-meta">${esc((t.goal||'').slice(0,140))}</div>`
      +`<div class="task-meta">更新于 ${esc(when)} · 历时 ${esc(formatElapsed(t.duration_seconds))}</div></div>`
      +`<div class="task-actions"><button class="mini" onclick="showTask('${esc(t.task_id)}')">详情</button></div></div>`;
  }).join('');
  return rows+`<div class="task-meta" style="padding:10px 16px;display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap"><span>共 ${d.total} 条 · 第 ${page}/${pages} 页</span><span><button class="mini" onclick="archivePage(-1)"${page<=1?' style="opacity:.45;pointer-events:none"':''}>上一页</button> <button class="mini" onclick="archivePage(1)"${page>=pages?' style="opacity:.45;pointer-events:none"':''}>下一页</button></span></div>`;
}
async function loadArchive(){
  const list=document.getElementById('archiveList');
  if(list)list.innerHTML='<div class="empty">正在加载…</div>';
  try{
    const qs=new URLSearchParams({status:archiveQuery.status,limit:String(archiveQuery.size),offset:String(archiveQuery.page*archiveQuery.size)});
    if(archiveQuery.project_id)qs.set('project_id',archiveQuery.project_id);
    if(archiveQuery.workflow_id)qs.set('workflow_id',archiveQuery.workflow_id);
    if(archiveQuery.agent)qs.set('agent',archiveQuery.agent);
    if(archiveQuery.q)qs.set('q',archiveQuery.q);
    const d=await api('/api/archive?'+qs.toString());
    const el=document.getElementById('archiveList');
    if(el)el.innerHTML=archiveRowsHtml(d);
  }catch(e){
    const el=document.getElementById('archiveList');
    if(el)el.innerHTML='<div class="empty">加载失败：'+esc(e.message)+'</div>';
  }
}
function applyArchiveFilters(){
  archiveQuery.project_id=document.getElementById('arcProject')?.value||'';
  archiveQuery.workflow_id=(document.getElementById('arcWorkflow')?.value||'').trim();
  archiveQuery.agent=document.getElementById('arcAgent')?.value||'';
  archiveQuery.status=document.getElementById('arcStatus')?.value||'archived';
  archiveQuery.q=(document.getElementById('arcQ')?.value||'').trim();
  archiveQuery.page=0;
  loadArchive();
}
function archivePage(delta){archiveQuery.page=Math.max(0,archiveQuery.page+delta);loadArchive()}
async function showArchive(){
  if(!state.overview){try{state.overview=await api('/api/overview')}catch(e){}}
  openModal('任务归档查询',archiveFiltersHtml());
  await loadArchive();
}
async function bindSlotPrompt(p){
  showPromptModal({
    title:'绑定智能体工位',
    label:'执行者名称（如 auto、claude、codex、opencode 等）',
    defaultValue:'auto',
    confirmText:'确认绑定',
    onConfirm:async(a)=>{
      if(!a)return;
      try{
        await api('/api/slot/bind',{method:'POST',body:JSON.stringify({pane_id:p,agent:a})});
        await loadProject(state.projectId,false);
        toast('工位已绑定 '+a);
      }catch(e){toast(e.message,true)}
    }
  });
}
setInterval(()=>{if(!document.hidden)refreshAll()},600000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshAll()});(function(){const v=loadViewState();if(!v)return;state.opsMode=!!v.opsMode;state.spaceId=v.spaceId||null;state.workflowId=v.workflowId||null})();async function initFromUrlOrState(){const p=new URLSearchParams(window.location.search);let qWf=p.get('workflow_id');const qTask=p.get('task_id'),qPane=p.get('pane_id'),qOps=p.get('ops');if(!qWf&&qTask){try{const td=await api('/api/task?id='+encodeURIComponent(qTask));if(td&&td.task&&td.task.workflow_id)qWf=td.task.workflow_id}catch(e){}}if(!qWf&&!qTask&&!qPane&&!qOps){state.opsMode?showOpsCenter():refreshAll();return}if(qOps==='1'||qOps==='true')state.opsMode=true;if(qWf){state.opsMode=false;state.workflowId=qWf;try{const d=await api('/api/workflow?id='+encodeURIComponent(qWf));if(d&&d.project){if(d.project.project_id)state.projectId=d.project.project_id;if(d.project.workspace_id)state.spaceId=d.project.workspace_id}}catch(e){}}if(state.opsMode){await showOpsCenter()}else{await refreshAll();if(qWf&&state.workflowId!==qWf){try{await loadWorkflow(qWf)}catch(e){}}if(qTask){const el=document.querySelector(`[data-task-id="${CSS.escape?CSS.escape(qTask):qTask}"]`);if(el){el.scrollIntoView({behavior:'smooth',block:'center'});el.classList.add('task-highlight')}await showTask(qTask)}else if(qPane){await showPane(qPane)}}}initFromUrlOrState();
</script></body></html>'''
HTML=HTML_TEMPLATE.replace('__PRODUCT_NAME__',PRODUCT_NAME).replace('__PRODUCT_TAGLINE__',PRODUCT_TAGLINE)

class Handler(BaseHTTPRequestHandler):
    server_version='HerdrFactoryConsole/1.2'
    def log_message(self,fmt,*args):print(f'[{datetime.now().isoformat(timespec="seconds")}] '+(fmt%args),flush=True)
    def send_json(self,status,data=None,error=None):
        p={'ok':error is None};
        if data is not None:p['data']=data
        if error is not None:p['error']=str(error)
        raw=json.dumps(p,ensure_ascii=False).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(raw))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(raw)
    def send_html(self,text):
        raw=text.encode(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(raw))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(raw)
    def query(self):return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    def body(self):
        n=int(self.headers.get('Content-Length','0') or 0); raw=self.rfile.read(n) if n else b'{}'; return json.loads(raw.decode() or '{}')
    def do_GET(self):
        p=urllib.parse.urlparse(self.path).path
        try:
            if p=='/':return self.send_html(HTML)
            if p=='/api/overview':return self.send_json(200,overview())
            if p=='/api/ops-center':return self.send_json(200,ops_center(self.query().get('workflow_id',[''])[0] or None,self.query().get('include_tasks',[''])[0]=='1'))
            if p=='/api/templates':return self.send_json(200,templates_summary())
            if p=='/api/template':return self.send_json(200,template_detail(self.query().get('id',[''])[0]))
            if p=='/api/run/status':return self.send_json(200,workflow_job_status(self.query().get('id',[''])[0]))
            if p=='/api/project':return self.send_json(200,project_detail(self.query().get('id',[''])[0]))
            if p=='/api/workflow':return self.send_json(200,workflow_detail(self.query().get('id',[''])[0]))
            if p=='/api/task':return self.send_json(200,task_detail(self.query().get('id',[''])[0]))
            if p=='/api/archive':
                qp=self.query()
                def _q(name,default=''):return qp.get(name,[default])[0]
                def _qi(name,default):
                    try:return int(_q(name,default))
                    except Exception:return default
                return self.send_json(200,archive_query(_q('project_id'),_q('workflow_id'),_q('agent'),_q('status','archived'),_q('q'),_qi('limit',50),_qi('offset',0)))
            if p=='/api/pane/read':return self.send_json(200,{'output':read_pane(self.query().get('id',[''])[0])})
            if p=='/api/deep-preflight':
                x=project_by_id(self.query().get('id',[''])[0])
                if not x:
                    raise RuntimeError('项目不存在')
                return self.send_json(200,deep_preflight(x))
            if p=='/api/preflight':
                x=project_by_id(self.query().get('id',[''])[0]);
                if not x:raise RuntimeError('项目不存在')
                return self.send_json(200,preflight(x))
            if p=='/api/logs':return self.send_json(200,{'output':tail_log(self.query().get('kind',['controller'])[0])})
            if p=='/api/kernel/checkpoints':
                wid=self.query().get('workflow_id',[''])[0]
                if not wid:raise RuntimeError('workflow_id 不能为空')
                return self.send_json(200,api_kernel_checkpoint_list(wid))
            if p=='/api/task/steer/queue':
                tid=self.query().get('task_id',[''])[0]
                return self.send_json(200,api_task_steer_queue(tid))
            if p=='/api/agent/adapters':
                return self.send_json(200,api_agent_adapters())
            if p=='/api/task/projection':
                tid=self.query().get('id',[''])[0] or self.query().get('task_id',[''])[0]
                if not tid:raise RuntimeError('task_id 不能为空')
                return self.send_json(200,api_task_projection(tid))
            if p=='/api/workflow/projection':
                wid=self.query().get('id',[''])[0] or self.query().get('workflow_id',[''])[0]
                if not wid:raise RuntimeError('workflow_id 不能为空')
                return self.send_json(200,api_workflow_projection(wid))
            return self.send_json(404,error='Not Found')
        except Exception as e:
            self.log_message('GET %s failed: %s', self.path, e)
            return self.send_json(500,error=e)
    def do_POST(self):
        p=urllib.parse.urlparse(self.path).path
        try:
            b=self.body()
            if p=='/api/run':return self.send_json(202,start_workflow_job(str(Path(b.get('project_root','')).expanduser().resolve()),str(b.get('requirement','')).strip(),str(b.get('agent') or 'auto'),str(b.get('template') or 'software-development-v1'),str(b.get('title') or '').strip()))
            if p=='/api/project/create':
                root=str(Path(b.get('project_root','')).expanduser().resolve())
                name=str(b.get('project_name') or '').strip() or None
                tmpl=str(b.get('template') or 'software-development-v1').strip()
                return self.send_json(200,herdr_projects.create_project(root,project_name=name,template_name=tmpl))
            if p=='/api/project/adopt':
                wid=str(b.get('workspace_id','')).strip()
                if not wid:raise RuntimeError('workspace_id 不能为空')
                root=str(Path(b.get('project_root','')).expanduser().resolve()) if b.get('project_root') else None
                tmpl=str(b.get('template') or 'software-development-v1').strip()
                name=str(b.get('project_name') or '').strip() or None
                return self.send_json(200,herdr_projects.adopt_workspace_as_project(wid,root=root,template_name=tmpl,project_name=name))
            if p=='/api/project/unregister':
                pid=str(b.get('project_id') or b.get('project_root') or '').strip()
                if not pid:raise RuntimeError('项目标识不能为空')
                close_ws=bool(b.get('close_workspace',False))
                force=bool(b.get('force',False))
                return self.send_json(200,herdr_projects.unregister_project(pid,close_workspace=close_ws,force=force))
            if p=='/api/template':return self.send_json(200,save_template(str(b.get('name') or ''),str(b.get('yaml') or '')))
            if p=='/api/workflow/agent':return self.send_json(200,set_agent_override(str(b['workflow_id']),str(b.get('agent') or 'auto')))
            if p=='/api/workflow/candidate':return self.send_json(200,create_candidate(str(b['workflow_id'])))
            if p=='/api/workflow/advance':return self.send_json(200,manual_advance(str(b['workflow_id'])))
            if p=='/api/task/force-review':return self.send_json(200,api_task_force_review(b))
            if p=='/api/workflow/retry-advance':return self.send_json(200,api_workflow_retry_advance(b))
            if p=='/api/task/coordinator':return self.send_json(200,ask_coordinator(str(b['task_id'])))
            if p=='/api/task/steer':return self.send_json(200,api_task_steer(b))
            if p=='/api/task/halt':return self.send_json(200,api_task_halt(b))
            if p=='/api/task/signoff':return self.send_json(200,api_task_signoff(b))
            if p=='/api/slot/bind':return self.send_json(200,bind_slot(str(b['pane_id']),str(b.get('agent') or 'auto')))
            if p=='/api/kernel/pause':return self.send_json(200,api_kernel_pause(b))
            if p=='/api/kernel/resume':return self.send_json(200,api_kernel_resume(b))
            if p=='/api/kernel/step':return self.send_json(200,api_kernel_step(b))
            if p=='/api/kernel/rollback':return self.send_json(200,api_kernel_rollback(b))
            if p=='/api/kernel/force-pass':return self.send_json(200,api_kernel_force_pass(b))
            if p=='/api/kernel/checkpoint':return self.send_json(200,api_kernel_checkpoint_create(b))
            if p=='/api/kernel/checkpoint/restore':return self.send_json(200,api_kernel_checkpoint_restore(b))
            return self.send_json(404,error='Not Found')
        except Exception as e:
            self.log_message('POST %s failed: %s', self.path, e)
            return self.send_json(500,error=e)

def main():
    ROOT.mkdir(parents=True,exist_ok=True); print(f'{PRODUCT_NAME}控制台: http://{HOST}:{PORT}',flush=True); ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
if __name__=='__main__':main()
