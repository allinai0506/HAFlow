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
from herdr import workflow_graph as herdr_workflow_graph
from herdr import human_decisions as herdr_human_decisions
from herdr import projects as herdr_projects
from herdr import kernel as herdr_kernel
from herdr import steering as herdr_steering
from herdr import projection as herdr_projection
from herdr import archive as herdr_archive
from herdr import controller_actions as herdr_controller_actions
from herdr import dashboard as herdr_dashboard
from herdr import workflow_docs as herdr_workflow_docs
from herdr import delivery_record as herdr_delivery_record
from herdr.agent_binary import resolve_agent_binary
PROJECTS_FILE=ROOT/'projects.json'; WORKFLOWS_FILE=ROOT/'workflows.json'; TASKS_FILE=ROOT/'tasks.json'; POOLS_FILE=ROOT/'agent-pools.json'; SLOTS_FILE=ROOT/'pane-slots.json'; LOG_DIR=ROOT/'logs'
CONSOLE_STATIC_DIR=Path(__file__).resolve().parent/'static'
HOST='127.0.0.1'; PORT=int(os.environ.get('HERDR_CONSOLE_PORT','8765'))
PRODUCT_NAME='HAFlow'; PRODUCT_TAGLINE='让人和多个 AI Agent 一起把事情做完'
HERDR_TASK=HERDR_ROOT/'bin'/'herdr-task'
RUN_JOBS={}
RUN_JOBS_LOCK=threading.Lock()
STAGES=[('requirements','需求分析'),('plan','计划'),('implementation','实现'),('test','测试'),('review','评审'),('wrapup','收尾')]
ACTIVE={'pending','dispatched','working','blocked','agent_done','rework','completed','committed','integrated','cleanup_ready'}
AGENTS=['opencode','codex','claude','qodercli','agy','pi','grok','kimi']

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
    all_t_map = {t.get('task_id'): t for t in tasks()}
    for a in data.get('anomalies') or []:
        tid = a.get('task_id')
        if tid and tid in all_t_map:
            t = all_t_map[tid]
            if a.get('version') is None:
                a['version'] = t.get('version')
            if not a.get('pane_id'):
                a['pane_id'] = t.get('pane_id')
    return data

def projects():return list(load_json(PROJECTS_FILE,{'projects':{}}).get('projects',{}).values())
def workflows():return herdr_kernel.load_workflows_data().get('workflows',{})
def tasks():return herdr_kernel.load_tasks_data().get('tasks',[])
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

def workflow_node_types(wid,p):
    cfg=load_json(Path(p.get('workflow_file')),{}) if p and p.get('workflow_file') else {}
    return {n.get('id') or n.get('key'):n.get('node_type','agent') for n in cfg.get('nodes') or [] if n.get('id') or n.get('key')}

def workflow_definition_for(wid,p,w):
    """Resolve the real workflow definition for a running workflow (fail-soft).

    Priority: workflow_file snapshot via projects.workflow_config_for ->
    raw workflow_file JSON -> template name guess -> None (projection fail-soft).
    Never invents nodes/edges; returns None when nothing reliable exists.
    """
    try:
        cfg=herdr_projects.workflow_config_for(wid)
        if cfg and cfg.get('nodes'):
            return cfg
    except (ValueError, TypeError):
        path=(w or {}).get('workflow_file') or (p or {}).get('workflow_file')
        raw=load_json(Path(path),{}) if path else {}
        return {**raw,'configuration_error':'workflow_config_invalid'}
    except Exception:
        pass
    try:
        if p and p.get('workflow_file'):
            raw=load_json(Path(p.get('workflow_file')),{})
            if raw and (raw.get('nodes') or raw.get('stages')):
                return herdr_workflow.normalize_workflow(raw)
    except Exception:
        pass
    try:
        tmpl=(w.get('template') or w.get('template_id') or w.get('workflow_template') or '').strip() if w else ''
        if tmpl:
            return herdr_workflow.load_template(tmpl)
    except Exception:
        pass
    return None

def workflow_graph_for(wid,p,w,ts):
    """Pure projection wiring: definition + tasks + blockers -> graph. Never raises."""
    try:
        definition=workflow_definition_for(wid,p,w)
    except Exception:
        definition=None
    try:
        blockers=herdr_controller_actions.resolve_workflow_blockers(ts or [],w or {})
    except Exception:
        blockers=[]
    try:
        payload=dict(definition or {})
        if isinstance(w,dict):
            for k in ("status","gate_overrides","current_stage","workflow_id"):
                if w.get(k) is not None:
                    payload[k]=w[k]
        return herdr_workflow_graph.workflow_graph_projection(payload,ts or [],blockers,all_tasks=tasks())
    except Exception:
        return {'nodes':[],'edges':[],'context':{'required':[],'optional':[]}}

def enrich_workflow_tasks(wid,p,ts):
    node_types=workflow_node_types(wid,p)
    return [{**t,'node_type':t.get('node_type') or node_types.get(t.get('node') or t.get('stage'))} for t in ts]

def stage_summary(ts,key,workflow=None):
    xs=[t for t in ts if t.get('stage')==key]
    live=[t for t in xs if t.get('status')!='superseded' and not t.get('superseded_by')]
    wf_status=(workflow or {}).get('status') if isinstance(workflow,dict) else ''
    is_wf_completed=wf_status in {'completed','cleaned','archived'}
    gate_override = ((workflow or {}).get('gate_overrides') or {}).get(key) or {}
    gate_passed = herdr_workflow_graph.is_gate_override_valid(gate_override, live)

    if not xs:
        if is_wf_completed:
            return {'key':key,'count':0,'status':'cleaned','tasks':[]}
        return {'key':key,'count':0,'status':'waiting','tasks':[]}
    if not live: st='superseded'
    elif is_wf_completed: st='cleaned'
    elif gate_passed:
        active=any(t.get('status') in {'working','dispatched','pending','rework','agent_done'} for t in live)
        st='working' if active else 'cleaned'
    else:
        ss=[t.get('status','unknown') for t in live]
        verdicts=[str(t.get('stage_verdict') or '') for t in live]
        if any((s=='blocked' or v=='blocked') and v!='pass' for s,v in zip(ss,verdicts)): st='blocked'
        elif any(s in {'working','dispatched','pending','rework','agent_done'} for s in ss): st='working'
        elif all(s in {'completed','committed','integrated','cleanup_ready','cleaned'} or v=='pass' for s,v in zip(ss,verdicts)):
            st='cleaned'
        else:
            def _k(t):
                for k in ('created_at','updated_at','last_activity_at'):
                    v=t.get(k)
                    if isinstance(v,(int,float)): return float(v)
                    if isinstance(v,str) and v.replace('.','',1).isdigit(): return float(v)
                return 0.0
            latest=sorted(live,key=_k)[-1]
            latest_st=str(latest.get('status') or '')
            latest_v=str(latest.get('stage_verdict') or '')
            unexempted_failed = any(s == 'failed' and v != 'pass' for s, v in zip(ss, verdicts))
            if unexempted_failed:
                st = 'failed'
            elif latest_st in {'completed','committed','integrated','cleanup_ready','cleaned'}:
                st='cleaned'
            elif latest_st=='failed' or any(s=='failed' for s in ss):
                st='failed'
            elif any(s=='blocked' for s in ss):
                st='blocked'
            else:
                st='mixed'
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
    'kimi': [HOME / '.kimi-code' / 'credentials' / 'kimi-code.json', HOME / '.kimi-code' / 'config.toml'],
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

def deep_preflight(p, agent=None):
    script = HERDR_ROOT / "bin" / "herdr-deep-preflight"
    if not script.exists():
        script = HERDR_ROOT / "herdr" / "deep_preflight.py"
    if not script.exists():
        raise RuntimeError(f"Deep Preflight 未安装: {script}")

    cmd = [
        str(script),
        "--project-id", p.get("project_id"),
        "--deep",
        "--json",
    ]
    if agent:
        cmd.extend(["--agent", agent])

    r = run(cmd, 320)

    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip() or "Deep Preflight 执行失败")

    try:
        return json.loads(r.stdout)
    except Exception as e:
        raise RuntimeError("Deep Preflight JSON 解析失败") from e


def slots(p, p_panes=None):
    cfg=load_json(Path(p.get('workflow_file','')),{}) ; stage_by_tab={s.get('tab_id'):s for s in cfg.get('stages',[])}; anchors={s.get('anchor_pane_id') for s in cfg.get('stages',[])}; binds=load_json(SLOTS_FILE,{'panes':{}}).get('panes',{}); claimed={t.get('pane_id'):t.get('task_id') for t in tasks() if t.get('pane_id')}; out=[]
    all_panes = p_panes if p_panes is not None else panes(p.get('workspace_id'))
    for x in all_panes:
        pid=x.get('pane_id'); tid=x.get('tab_id')
        if not pid or tid not in stage_by_tab or pid in anchors:continue
        rt=agent_runtime(pid); s=stage_by_tab[tid]
        out.append({'pane_id':pid,'tab_id':tid,'stage':s.get('key'),'stage_label':s.get('label'),'bound_agent':binds.get(pid,{}).get('agent','auto'),'claimed_by':claimed.get(pid),'live_agent':rt.get('agent') if rt else None,'agent_status':rt.get('agent_status') if rt else None})
    return out

def service_status():
    names=['com.user.herdr-controller','com.user.herdr-notifier','com.user.herdr-sentinel','com.user.herdr-factory-console']
    out={n:'stopped' for n in names}
    try:
        r=run(['launchctl','list'],5)
        if r.returncode==0:
            for line in r.stdout.splitlines():
                parts=line.strip().split('\t')
                if len(parts)>=3 and parts[2] in names:
                    out[parts[2]]='running' if parts[0]!='-' and parts[0].isdigit() else 'stopped'
    except Exception:
        pass
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
    p_panes=panes(p.get('workspace_id'))
    try:
        s=slots(p,p_panes=p_panes)
    except TypeError:
        s=slots(p)
    return {'project':p,'workflows':ws,'latest_workflow_id':ws[0]['workflow_id'] if ws else None,'tabs':tabs(p.get('workspace_id')),'panes':p_panes,'slots':s,'agents':preflight(p)}

def workflow_stages(wid, p):
    """阶段卡片来源:优先 workflow.json 的 nodes(id/label),回退内置 STAGES。

    背景(wf-nexusarchive-0918-02):前端阶段卡片此前硬编码 software-development
    的 6 阶段,切换模板(general-task-v1 等)后界面不跟随,必须按工作流
    自己的节点定义渲染。
    """
    cfg={}
    if p and p.get('workflow_file'):
        cfg=load_json(Path(p.get('workflow_file')),{}) or {}
    out=[]
    for n in (cfg.get('nodes') or []):
        key=n.get('id') or n.get('key')
        if not key:continue
        out.append((key,n.get('label') or key))
    return out or STAGES


def workflow_detail(wid):
    w=workflows().get(wid)
    if not w:raise RuntimeError('工作流不存在')
    p=project_for_workflow(wid); ts=enrich_workflow_tasks(wid,p,tasks_for_workflow(wid)); ss=[]
    for k,l in workflow_stages(wid,p):
        x=stage_summary(ts,k,workflow=w); x['label']=l; ss.append(x)
    stall_info=herdr_projection.detect_workflow_stalls(wid,ts,workflow=w)
    graph=workflow_graph_for(wid,p,w,ts)
    derived={'current_nodes':graph.get('current_nodes',[]),'ready_nodes':graph.get('ready_nodes',[]),'derived_current_stage':graph.get('current_stage',''),'current_stage_source':graph.get('current_stage_source','derived_nodes')}
    return {'workflow':{'workflow_id':wid,**_with_subject(w),**derived},'project':p,'stages':ss,'tasks':ts,'graph':graph,'context':graph.get('context',{'required':[],'optional':[]}),'coordinator':agent_runtime(w.get('coordinator_pane_id')),'candidate_branch':w.get('candidate_branch'),'agent_override':w.get('agent_override','auto'),'stall':stall_info}


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

def update_workflow_title(wid,title):
    t=(title or '').strip()
    try:
        from herdr.state_store import get_state_store, sync_workflows_projection
        s=get_state_store()
        s.update_workflow_metadata(wid, {'title': t})
        sync_workflows_projection(store=s)
    except Exception:
        pass
    d=load_json(WORKFLOWS_FILE,{'workflows':{}}); w=d.get('workflows',{}).get(wid)
    if w:
        w['title']=t
        save_json(WORKFLOWS_FILE,d)
    return {'workflow_id':wid,'title':t}

def bind_slot(pid,agent):
    d=load_json(SLOTS_FILE,{'panes':{}}); d.setdefault('panes',{})[pid]={'agent':agent or 'auto'}; save_json(SLOTS_FILE,d); return {'pane_id':pid,'agent':agent or 'auto'}

def ask_coordinator(tid):
    t=task_detail(tid)['task']; wid=t.get('workflow_id'); w=workflows().get(wid,{}); c=w.get('coordinator_pane_id') or t.get('coordinator_pane_id')
    msg=f'''HERDR_FACTORY_CONSOLE_ACTION\n\nworkflow_id: {wid}\ntask_id: {tid}\nstatus: {t.get('status')}\nstage: {t.get('stage')}\nagent: {t.get('agent')}\npane_id: {t.get('pane_id')}\n\n用户在{PRODUCT_NAME}控制台点击“让总指挥处理”。\n请检查 Task Registry、Pane、Agent Runtime 和验收标准；可恢复则安全恢复，需要人工决策则明确说明。不得删除任何已注册 Task 的 Pane / Tab / Clone。不得处理其他工作流。'''
    r=run(['herdr','agent','prompt',c,msg,'--wait','--timeout','600000'],620)
    if r.returncode!=0:raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return {'ok':True}

def _blocked_verdict_tasks(wid):
    return [t for t in herdr_controller_actions.resolve_workflow_blockers(
        tasks_for_workflow(wid), {'workflow_id':wid, **(workflows().get(wid) or {})})
        if t.get('stage_verdict')=='blocked']

def api_workflow_recovery(wid):
    from herdr import dispatch_recovery
    db_path=Path(os.environ['HERDR_STATE_DB']) if os.environ.get('HERDR_STATE_DB') else Path(TASKS_FILE).parent/'state.db'
    return {'workflow_id':wid,'operations':dispatch_recovery.list_operations(db_path,wid)}

def api_workflow_recovery_decide(b):
    from herdr import recovery_store
    db_path=Path(os.environ['HERDR_STATE_DB']) if os.environ.get('HERDR_STATE_DB') else Path(TASKS_FILE).parent/'state.db'
    wid=str(b.get('workflow_id') or '').strip()
    operation_id=int(b['operation_id'])
    operations=recovery_store.list_operations(db_path,wid)
    if not any(op['id']==operation_id for op in operations):
        raise RuntimeError('恢复义务不属于该工作流')
    if b.get('action') == 'check_resources':
        from herdr import dispatch_recovery
        return dispatch_recovery.check_resources(db_path,wid,operation_id,int(b['expected_version']),
            b.get('operator'),b.get('reason'),candidate_sha=b.get('candidate_sha'))
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

DEFAULT_DISALLOWED_NOTES = {
    '人类在控制台强制放行并推进',
    '运维驾驶舱异常中枢一键修复放行',
    '运维驾驶舱异常中枢一键修复执行',
    'human forced pass',
    'human',
    '经人工核验，次要阻断项已评估无害，特批放行',
    '人工放行',
    'force pass',
    '人工在协同工作舱会签放行',
}

def _resolve_target_node(payload):
    """Resolve target node from node, stage, gate_node_id in payload.

    If multiple distinct non-empty values are provided, reject immediately
    to prevent 'validate node X, force pass node Y' privilege escalation.
    """
    candidates = {}
    for key in ('node', 'stage', 'gate_node_id'):
        val = str(payload.get(key) or '').strip()
        if val:
            candidates[key] = val
    unique_vals = set(candidates.values())
    if len(unique_vals) > 1:
        details = ", ".join(f"{k}={v}" for k, v in sorted(candidates.items()))
        raise RuntimeError(f"目标节点参数冲突（{details}），禁止多节点错配请求")
    return next(iter(unique_vals)) if unique_vals else ""

def _validate_force_pass_params(payload, wid, wf=None, task=None):
    """Unified validation for manual gate force-pass requests.

    Shared across /api/controller/execute-action, /api/kernel/force-pass, and /api/task/signoff.
    Returns (gate_node, reason, operator).
    """
    if not (payload.get('confirmed') is True or payload.get('confirm') is True):
        raise RuntimeError('人工强制放行必须经过显式确认（缺少 confirmation 标记）')

    reason = str(payload.get('reason') or payload.get('note') or payload.get('feedback') or '').strip()
    if not reason or reason in DEFAULT_DISALLOWED_NOTES:
        raise RuntimeError('人工强制放行必须填写非空原因，禁止使用系统默认文案')

    gate = _resolve_target_node(payload)
    if not gate:
        raise RuntimeError('缺少明确的目标门禁节点，禁止全工作流隐式放行')

    # If task is provided, ensure gate matches task node and snapshot protections are met
    if task:
        task_node = str(task.get('node') or task.get('stage') or '').strip()
        if task_node and gate != task_node:
            raise RuntimeError(f'任务节点错配: 请求目标节点为 {gate}，当前任务节点为 {task_node}，请刷新页面')

        if payload.get('expected_version') is None:
            raise RuntimeError('放行处置请求缺少 expected_version 版本保护字段，禁止无版本约束的放行处置，请刷新页面')
        cur_ver = task.get('version')
        exp_ver = payload.get('expected_version')
        if cur_ver != exp_ver:
            raise RuntimeError(f'任务版本已变化（期望版本 {exp_ver}，当前版本 {cur_ver}），请刷新页面')

        exp_pane = str(payload.get('expected_pane_id') or payload.get('pane_id') or '').strip()
        cur_pane = str(task.get('pane_id') or '').strip()
        if exp_pane and cur_pane != exp_pane:
            raise RuntimeError('任务运行实例已变化，请刷新页面')

    # Verify gate belongs to the workflow
    wf_tasks = tasks_for_workflow(wid)
    matching_tasks = [t for t in wf_tasks if gate in (t.get('node'), t.get('stage')) and t.get('status') != 'superseded']
    wf_cfg_nodes = (wf.get('config') or {}).get('nodes') or [] if isinstance(wf, dict) else []
    wf_cfg_node_ids = {n.get('id') for n in wf_cfg_nodes if isinstance(n, dict)}
    if not matching_tasks and gate not in wf_cfg_node_ids:
        raise RuntimeError(f'目标门禁节点 {gate} 不属于工作流 {wid}')

    if not task:
        if payload.get('expected_version') is not None:
            raise RuntimeError('节点级放行禁止使用单一 expected_version，必须提供 expected_task_versions 映射（无任务节点应为 {}），请刷新页面')
        exp_task_vers = payload.get('expected_task_versions')
        if not isinstance(exp_task_vers, dict):
            raise RuntimeError('节点级放行请求缺少任务版本快照保护字段（expected_task_versions），必须显式绑定任务版本快照（无任务节点必须提供空映射 {}），禁止无快照放行，请刷新页面')
        matching_tids = {t.get('task_id') for t in matching_tasks}
        provided_tids = set(exp_task_vers.keys())
        if matching_tids != provided_tids:
            raise RuntimeError(
                f'节点级放行版本映射不完整或不匹配: 当前节点有效任务为 {sorted(matching_tids)}，'
                f'提交映射为 {sorted(provided_tids)}，必须完整核验所有任务版本'
            )
        for t in matching_tasks:
            tid = t.get('task_id')
            exp_v = exp_task_vers.get(tid)
            cur_v = t.get('version')
            if cur_v != exp_v:
                raise RuntimeError(
                    f'任务 {tid} 版本已变化（期望版本 {exp_v}，当前版本 {cur_v}），请刷新页面'
                )

    op = str(payload.get('operator') or 'human').strip()
    return gate, reason, op

def api_kernel_force_pass(b):
    b = b or {}
    wid = str(b.get('workflow_id') or '').strip()
    if not wid: raise RuntimeError('workflow_id 不能为空')
    wf = workflows().get(wid)
    if not wf:
        try:
            wf = herdr_kernel._get_store().get_workflow(wid)
        except Exception:
            wf = None
    if not wf and not tasks_for_workflow(wid):
        raise RuntimeError(f'未找到工作流 {wid}')

    task_id = str(b.get('task_id') or '').strip()
    task = None
    if task_id:
        store = herdr_kernel._get_store()
        task = store.get_task(task_id) if hasattr(store, 'get_task') else None
        if not task:
            raise RuntimeError(f'未找到任务 {task_id}（工作流 {wid}）')
        if task.get('workflow_id') != wid:
            raise RuntimeError(f'任务 {task_id} 不属于工作流 {wid}（所属工作流错配）')
        if task.get('status') == 'superseded':
            raise RuntimeError(f'任务 {task_id} 已被替换或作废，请刷新页面')

    gate, reason, op = _validate_force_pass_params(b, wid, wf, task=task)

    exp_ver = b.get('expected_version')
    exp_task_vers = b.get('expected_task_versions')
    exp_pane = str(b.get('expected_pane_id') or b.get('pane_id') or '').strip()

    return herdr_kernel.force_pass_gate(
        wid,
        gate_node_id=gate,
        note=reason,
        operator=op,
        expected_version=exp_ver,
        task_id=task_id or None,
        expected_pane_id=exp_pane or None,
        expected_task_versions=exp_task_vers,
    )

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
    reason=str(b.get('reason') or '人工在控制台紧急叫停').strip()
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

def api_workflow_controller_actions(wid):
    """Blocker + pipeline actions for a workflow.

    A healthy task that still owes the git delivery pipeline its next step
    (commit / integrate / finalize) or a live pane a real re-drive is an
    actionable item too — otherwise the console shows "no blockers" while
    delivery is actually waiting on a human click.
    """
    wid = str(wid or '').strip()
    if not wid: raise RuntimeError('workflow_id 不能为空')
    wf = workflows().get(wid) or {}
    wf = {'workflow_id': wid, **wf}
    ts = tasks_for_workflow(wid)
    blockers = herdr_controller_actions.resolve_workflow_blockers(ts, wf)
    p = project_for_workflow(wid) or {}
    proj_root = p.get('project_root') or wf.get('project_root') or ''
    operations = api_workflow_recovery(wid).get('operations',[])
    managed = {tid for op in operations if op['status'] not in {'resolved','superseded'}
               for tid in op['payload'].get('task_ids') or []}
    actions_list = [
        act.to_dict()
        for act in herdr_controller_actions.collect_workflow_actions(
            ts, wf, project_root=proj_root,
            workflow_paused=str(wf.get('status') or '') == 'paused',
        )
    ]
    actions_list = [a for a in actions_list if a.get('blocker_task_id') not in managed]
    return {
        'workflow_id': wid,
        'blockers': blockers,
        'actions': actions_list,
        'recovery': operations,
    }

def pipeline_step_commands(step):
    """Resolve a pipeline step to relative argv from the core's single table.

    The console must not keep its own copy of the git delivery chain: a
    second table would drift and could execute a transition the state machine
    rejects.
    """
    for _status, name, steps, _label, _needs_git in herdr_controller_actions.GIT_PIPELINE_FORWARD:
        if name == step:
            return [[parts[0], None, *parts[1:]] for parts in steps]
    raise RuntimeError(f'未知的交付链路步骤: {step}')

ACTION_BINARIES = {
    'herdr-task': lambda: str(HERDR_TASK),
    'herdr': lambda: 'herdr',
}

def _find_action(payload, suffix):
    """Re-derive the action a request refers to, so argv stays core-authored.

    The request only names the task and the step; the executable argv always
    comes back out of ``herdr.controller_actions``, never from the body.
    """
    task_id = str(payload.get('task_id') or '').strip()
    wid = str(payload.get('workflow_id') or '').strip()
    if not task_id or not wid:
        raise RuntimeError('task_id 与 workflow_id 不能为空')
    task = next(
        (t for t in tasks_for_workflow(wid) if t.get('task_id') == task_id), None,
    )
    if task is None:
        raise RuntimeError(f'未找到任务 {task_id}（工作流 {wid}）')
    candidates = (herdr_controller_actions.generate_controller_actions(task, {'workflow_id': wid})
                  if suffix == 'rework' else
                  herdr_controller_actions.generate_progress_actions(task, {'workflow_id': wid}))
    action = next(
        (a for a in candidates
         if a.action_id.endswith(suffix)),
        None,
    )
    if action is None or not action.commands:
        raise RuntimeError(f'该任务当前没有可执行的 {suffix} 动作（状态已变化，请刷新）')
    return action

def _run_action_command(action, timeout=900):
    """Run a core-authored action: ``command_base`` + its first argv command."""
    if len(action.commands) != 1:
        raise RuntimeError(f'动作 {action.action_id} 不是单条命令，请改用 task_git_step')
    base = ACTION_BINARIES.get(action.command_base)
    if base is None:
        raise RuntimeError(f'未知的命令基座: {action.command_base}')
    argv = [base(), *[str(p) for p in action.commands[0]]]
    r = run(argv, timeout, check=True)
    return {'ok': True, 'task_id': action.blocker_task_id,
            'action_id': action.action_id, 'output': r.stdout.strip()[:2000]}

def _run_task_commands(task_id, commands, timeout=900):
    """Run relative argv in order against bin/herdr-task; stop at first failure.

    ``None`` marks the task-id slot.  Only argv shapes this module produced are
    accepted: every command is a subcommand, optionally followed by flags.
    """
    ran = []
    for command in commands:
        if not command or command[0] in ('herdr-task', 'bin/herdr-task'):
            raise RuntimeError('交付链路命令必须使用相对子命令形式')
        argv = [str(HERDR_TASK)]
        for part in command:
            if part is not None and str(part).startswith('-'):
                if argv[-1] == str(HERDR_TASK) or argv[-1].startswith('-'):
                    raise RuntimeError(f'交付链路命令缺少子命令参数: {command}')
            argv.append(str(task_id) if part is None else str(part))
        result = run(argv, timeout, check=True)
        ran.append({'command': ' '.join(argv), 'stdout': result.stdout.strip()[:2000]})
    return ran

def api_workflow_decisions(wid):
    """Open human decisions + the coordinator's latest advice for a workflow."""
    wid = str(wid or '').strip()
    if not wid: raise RuntimeError('workflow_id 不能为空')
    try:
        notes = herdr_workflow_docs.annotate_notes(
            herdr_workflow_docs.load_notes(wid), current_base_sha=None,
        )
    except Exception as exc:
        print(f"[DECISIONS WARN] {wid}: {exc}")
        notes = []
    return {
        'workflow_id': wid,
        'decisions': herdr_human_decisions.collect_open_decisions(notes, workflow_id=wid),
        'advice': herdr_human_decisions.collect_advice(notes, workflow_id=wid),
    }

def _append_decision_note(wid, fields, title, body, node=None, task_id=None):
    try:
        return herdr_workflow_docs.append_note(
            wid, kind='decision', title=title, body=body,
            node=node, task_id=task_id, source=herdr_workflow_docs.SOURCE_HUMAN,
            fields=fields,
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc

def api_workflow_decide_raise(b):
    """Open a new decision ask so the human is reminded about it."""
    wid=str(b.get('workflow_id') or '').strip()
    if not wid:raise RuntimeError('workflow_id 不能为空')
    did=str(b.get('decision_id') or '').strip()
    if not did:raise RuntimeError('decision_id 不能为空')
    question=str(b.get('question') or '').strip()
    if not question:raise RuntimeError('question 不能为空')
    options=b.get('options') or []
    if not isinstance(options,(list,tuple)):raise RuntimeError('options 必须是列表')
    try:
        fields=herdr_human_decisions.build_decision_fields(
            did, herdr_human_decisions.STATUS_OPEN, question=question,
            options=options, recommended=str(b.get('recommended') or ''),
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    record=_append_decision_note(
        wid, fields, did, question,
        node=str(b.get('node') or '').strip() or None,
        task_id=str(b.get('task_id') or '').strip() or None,
    )
    return {'ok':True,'note_id':record.get('note_id'),'decision_id':did}

def api_workflow_decide_resolve(b):
    """Record the human ruling; the ask leaves the pending list."""
    wid=str(b.get('workflow_id') or '').strip()
    if not wid:raise RuntimeError('workflow_id 不能为空')
    did=str(b.get('decision_id') or '').strip()
    if not did:raise RuntimeError('decision_id 不能为空')
    decision=str(b.get('decision') or '').strip()
    if not decision:raise RuntimeError('decision 不能为空')
    open_asks={d['decision_id']:d for d in api_workflow_decisions(wid)['decisions']}
    prior=open_asks.get(did) or {}
    fields=herdr_human_decisions.build_decision_fields(
        did, herdr_human_decisions.STATUS_RESOLVED,
        question=prior.get('question') or did, decision=decision,
    )
    record=_append_decision_note(
        wid, fields, f'{did} 裁决', decision,
        node=prior.get('node') or None, task_id=prior.get('task_id') or None,
    )
    return {'ok':True,'note_id':record.get('note_id'),'decision_id':did,
            'decision':decision}

def api_controller_execute_action(payload):
    payload = payload or {}
    act_type = str(payload.get('type') or payload.get('action_type') or '').strip()
    wid = str(payload.get('workflow_id') or '').strip()
    if not wid: raise RuntimeError('workflow_id 不能为空')
    if act_type in {'launch','force_pass_advance','rework'}:
        # Discover canonical task facts before accepting a stale cockpit action.
        tasks_for_workflow(wid)
        if any(op['status'] not in {'resolved','superseded'}
               for op in api_workflow_recovery(wid)['operations']):
            raise RuntimeError('该工作流存在持久恢复义务，请通过恢复待办裁决，禁止重复派发或绕过门禁')

    # Re-read authoritative workflow
    wf = workflows().get(wid)
    if not wf:
        try:
            wf = herdr_kernel._get_store().get_workflow(wid)
        except Exception:
            wf = None
    if not wf and not tasks_for_workflow(wid):
        raise RuntimeError(f'未找到工作流 {wid}')

    # Validate task attribution, node, version, and running instance when task_id is present
    task_id = str(payload.get('task_id') or '').strip()
    task = None
    if task_id and act_type != 'launch':
        all_t = tasks()
        task_any = next((t for t in all_t if t.get('task_id') == task_id), None)
        if task_any is None:
            try:
                task_any = herdr_kernel._get_store().get_task(task_id)
            except Exception:
                task_any = None
        if task_any is None:
            raise RuntimeError(f'未找到任务 {task_id}（工作流 {wid}）')
        if task_any.get('workflow_id') != wid:
            raise RuntimeError(f'任务 {task_id} 不属于工作流 {wid}（所属工作流错配）')
        task = task_any

        if task.get('status') == 'superseded':
            raise RuntimeError(f'任务 {task_id} 已被替换或作废，请刷新页面')

        req_node = _resolve_target_node(payload)
        task_node = str(task.get('node') or task.get('stage') or '').strip()
        if req_node and task_node and req_node != task_node:
            raise RuntimeError(f'任务节点错配: 请求节点为 {req_node}，当前任务节点为 {task_node}，请刷新页面')

        if payload.get('expected_version') is not None:
            cur_ver = task.get('version')
            exp_ver = payload.get('expected_version')
            if cur_ver != exp_ver:
                raise RuntimeError(f'任务版本已变化（期望版本 {exp_ver}，当前版本 {cur_ver}），请刷新页面')

        if payload.get('expected_pane_id') is not None:
            cur_pane = str(task.get('pane_id') or '').strip()
            exp_pane = str(payload.get('expected_pane_id') or '').strip()
            if cur_pane != exp_pane:
                raise RuntimeError(f'任务运行实例已变化，请刷新页面')
        elif payload.get('pane_id') is not None and act_type not in ('redrive',):
            req_pane = str(payload.get('pane_id') or '').strip()
            cur_pane = str(task.get('pane_id') or '').strip()
            if req_pane and cur_pane and req_pane != cur_pane:
                raise RuntimeError(f'任务运行实例已变化（请求实例 {req_pane}，当前实例 {cur_pane}），请刷新页面')

    if act_type == 'launch':
        stage = str(payload.get('stage') or 'implementation').strip()
        agent = str(payload.get('agent') or 'auto').strip()
        supersedes = str(payload.get('supersedes') or '').strip()
        prompt = str(payload.get('prompt') or f'执行 {stage} 阶段任务').strip()
        goal = str(payload.get('goal') or prompt).strip()
        if not task_id:
            task_id = f'{wid}-{stage}-{int(time.time())}'
        p = project_for_workflow(wid) or {}
        proj_root = str(payload.get('source') or p.get('project_root') or (wf and wf.get('project_root')) or '.').strip()

        cmd = [
            str(HERDR_TASK), 'launch',
            '--task-id', task_id,
            '--workflow-id', wid,
            '--stage', stage,
            '--source', proj_root,
            '--agent', agent,
            '--goal', goal,
            '--prompt', prompt,
        ]
        if supersedes:
            reason = str(payload.get('supersede_reason') or '').strip()
            if not reason: raise RuntimeError('替换任务需要 supersede_reason；优先原工位 rework')
            cmd += ['--supersedes', supersedes, '--supersede-reason', reason]
        if payload.get('ack_overflow') is True: cmd.append('--ack-overflow')
        r = run(cmd, timeout=30, check=True)
        return {'ok': True, 'task_id': task_id, 'output': r.stdout.strip()}

    elif act_type in ('force_pass', 'force_pass_advance'):
        gate, reason, op = _validate_force_pass_params(payload, wid, wf, task=task)
        exp_ver = payload.get('expected_version')
        exp_task_vers = payload.get('expected_task_versions')
        exp_pane = str(payload.get('expected_pane_id') or payload.get('pane_id') or '').strip()

        # Execution/write boundary check: pass expected_version, task_id, expected_pane_id, expected_task_versions to force_pass_gate
        gate_res = herdr_kernel.force_pass_gate(
            wid,
            gate_node_id=gate,
            note=reason,
            operator=op,
            expected_version=exp_ver,
            task_id=task_id or None,
            expected_pane_id=exp_pane or None,
            expected_task_versions=exp_task_vers,
        )

        adv_res = None
        adv_err = None
        if act_type == 'force_pass_advance':
            try:
                adv_res = manual_advance(wid)
            except Exception as e:
                adv_err = str(e)

        if adv_err:
            return {
                'ok': False,
                'partial': True,
                'gate_passed': True,
                'workflow_id': wid,
                'gate_node_id': gate,
                'gate_result': gate_res,
                'advance_error': adv_err,
                'error': f'门禁放行已生效，但推进后续阶段失败: {adv_err}',
            }

        return {
            'ok': True,
            'partial': False,
            'gate_passed': True,
            'workflow_id': wid,
            'gate_node_id': gate,
            'gate_result': gate_res,
            'advanced': adv_res,
        }

    elif act_type == 'rework':
        if task_id and payload.get('expected_version') is not None:
            s = herdr_kernel._get_store()
            fresh_task = s.get_task(task_id) if hasattr(s, 'get_task') else None
            if fresh_task and fresh_task.get('version') != payload.get('expected_version'):
                raise RuntimeError('任务状态已在执行前发生变化，请刷新页面')
        return _run_action_command(_find_action(payload, 'rework'), 30)

    elif act_type == 'advance':
        return manual_advance(wid)

    elif act_type == 'task_git_step':
        if not task_id: raise RuntimeError('task_id 不能为空')
        step = str(payload.get('step') or '').strip()
        ran = _run_task_commands(task_id, pipeline_step_commands(step), timeout=900)
        return {'ok': True, 'task_id': task_id, 'step': step, 'ran': len(ran),
                'output': ran}

    elif act_type in ('redrive', 'clear_escalation', 'supersede', 'close_workflow'):
        if act_type != 'close_workflow' and not task_id:
            raise RuntimeError('task_id 不能为空')
        if act_type == 'redrive':
            return _run_action_command(_find_action(payload, 'redrive'), 200)
        if act_type in ('clear_escalation', 'supersede'):
            return _run_action_command(_find_action(payload, act_type), 900)
        argv = [str(HERDR_TASK), 'close-workflow', wid]
        if payload.get('accept_escalated'):
            argv.append('--accept-escalated')
        r = run(argv, 900, check=True)
        return {'ok': True, 'task_id': task_id, 'workflow_id': wid,
                'output': r.stdout.strip()[:2000]}

    elif act_type == 'steer':
        return api_task_steer(payload)

    elif act_type == 'halt':
        return api_task_halt(payload)

    elif act_type in ('retry', 'ops_repair'):
        if not task_id or not task:
            raise RuntimeError('task_id 不能为空')

        # Execution boundary check: re-verify task status, version, and running instance before dispatching recovery
        s = herdr_kernel._get_store()
        fresh_task = s.get_task(task_id) if hasattr(s, 'get_task') else None
        if fresh_task:
            if payload.get('expected_version') is not None:
                cur_v = fresh_task.get('version')
                exp_v = payload.get('expected_version')
                if cur_v != exp_v:
                    raise RuntimeError(f'任务状态已在执行前发生变化（期望版本 {exp_v}，当前版本 {cur_v}），请刷新页面')
            if payload.get('expected_pane_id') is not None:
                cur_p = str(fresh_task.get('pane_id') or '').strip()
                exp_p = str(payload.get('expected_pane_id') or '').strip()
                if exp_p != cur_p:
                    raise RuntimeError('任务运行实例已变化，请刷新页面')
            elif payload.get('pane_id') is not None:
                cur_p = str(fresh_task.get('pane_id') or '').strip()
                exp_p = str(payload.get('pane_id') or '').strip()
                if exp_p and cur_p and exp_p != cur_p:
                    raise RuntimeError(f'任务运行实例已变化（请求实例 {exp_p}，当前实例 {cur_p}），请刷新页面')
            if fresh_task.get('status') == 'superseded':
                raise RuntimeError(f'任务 {task_id} 已被替换或作废，请刷新页面')
            task = fresh_task

        rework_act = None
        try:
            rework_act = _find_action(payload, 'rework')
        except Exception:
            rework_act = None

        if rework_act is not None:
            return _run_action_command(rework_act, 30)

        redrive_act = None
        try:
            redrive_act = _find_action(payload, 'redrive')
        except Exception:
            redrive_act = None

        if redrive_act is not None:
            return _run_action_command(redrive_act, 200)

        st = task.get('status') or '未知'
        raise RuntimeError(f'任务 {task_id}（当前状态: {st}）无安全可执行的自动恢复动作；请打开终端排查或通过人工通道处理')

    raise RuntimeError(f'未知的控制器动作类型: {act_type}')


def api_task_projection(tid):
    if not tid:raise RuntimeError('task_id 不能为空')
    return herdr_projection.project_task(tid)

def api_workflow_projection(wid):
    if not wid:raise RuntimeError('workflow_id 不能为空')
    return herdr_projection.project_workflow(wid)

def api_workflows(pid=None):
    all_w = workflows()
    res = []
    for wid, w in all_w.items():
        w_proj = w.get('project_id') or ''
        if pid and w_proj != pid:
            continue
        item = _with_subject(w)
        res.append({
            'workflow_id': wid,
            'project_id': w_proj,
            'title': item.get('title') or '',
            'requirement_subject': item.get('requirement_subject') or '',
            'created_at': item.get('created_at') or 0,
        })
    res.sort(key=lambda x: (x.get('created_at') or 0, x['workflow_id']), reverse=True)
    return res

def archive_query(project_id=None,workflow_id=None,agent=None,status=None,q=None,limit=50,offset=0):
    """归档查询读取 StateStore(唯一事实源)。"""
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

def _dashboard_pane_runtime(pane_id):
    """只读探针:单个工位执行者状态,3s 超时,失败返回 None(失败隔离)。"""
    if not pane_id:
        return None
    try:
        r=subprocess.run(['herdr','agent','get',pane_id],text=True,capture_output=True,timeout=3)
    except Exception:
        return None
    if r.returncode!=0:
        return None
    try:
        return json.loads(r.stdout).get('result',{}).get('agent',{})
    except Exception:
        return None

def dashboard_data(limit_tasks=50, limit_workflows=50, workflow_id=None):
    """人类仪表板聚合(只读旁路):任务+工位实时/等你决策/最新交付/卡住。

    数据权威: tasks→StateStore经kernel; 工位实时→herdr agent get(只读探针);
    阻断→controller_actions; 交付→workflow_docs+delivery_record; 停滞→projection;
    工作流标题→workflows()+_with_subject。
    任一来源失败只隔离该工作流/工位,不拖垮整页;工位探针有界(≤20)+3s 超时+并发上限 8。
    workflow_id 未知时返回空分段(fail-soft),不抛错。
    """
    now=time.time()
    try:
        _all=tasks()
    except Exception:
        _all=[]
    if not isinstance(_all,list):
        _all=[]
    scope=(workflow_id or '').strip() or None
    all_tasks=[t for t in _all if (t.get('workflow_id') or 'unknown')==scope] if scope else _all
    try:
        wf_map=workflows()
    except Exception:
        wf_map={}
    if not isinstance(wf_map,dict):
        wf_map={}

    def _upd(t):
        for k in ('last_activity_at','updated_at','created_at'):
            try:
                v=float(t.get(k))
                return v
            except (TypeError,ValueError):
                continue
        return 0.0

    active=[t for t in all_tasks if t.get('status') not in {'superseded','cleaned'} and not t.get('superseded_by')]
    by_wf={}
    for t in active:
        wid=t.get('workflow_id') or 'unknown'
        by_wf.setdefault(wid,[]).append(t)
    # 最近活跃的工作流优先,保证有界
    wf_ids=sorted(by_wf, key=lambda w:max([_upd(t) for t in by_wf[w]] or [0.0]), reverse=True)[:max(1,limit_workflows)]

    blockers=[]; actions_by_task={}; stalls={}; deliveries=[]; anomalies=[]; decisions=[]
    for wid in wf_ids:
        wts=by_wf.get(wid,[])
        wf=wf_map.get(wid) or {}
        try:
            bs=herdr_controller_actions.resolve_workflow_blockers(wts,wf)
        except Exception:
            bs=[]
        for b in bs:
            blockers.append(b)
            try:
                acts=herdr_controller_actions.generate_controller_actions(b,wf,project_root=(wf.get('project_root') or ''))
            except Exception:
                acts=[]
            if acts:
                picked=next((a for a in acts if getattr(a,'recommended',False)),acts[0])
                try:
                    ad=picked.to_dict() if hasattr(picked,'to_dict') else dict(picked)
                except Exception:
                    ad={}
                actions_by_task[b.get('task_id')]=ad
        try:
            st=herdr_projection.detect_workflow_stalls(wid,wts,workflow=(wf or None))
            if st and st.get('is_stalled'):
                stalls[wid]=st
        except Exception:
            pass
        # 轻量卡住信号:失败/阻断即卡住,不做 pane 探针
        for t in wts:
            if t.get('status') in {'failed','blocked'}:
                anomalies.append({'kind':str(t.get('status')).upper(),'task_id':t.get('task_id'),'workflow_id':wid,'last_activity_at':_upd(t) or None})
    # 工位实时:仅活跃任务的 pane,有界 ≤20,3s 超时,并发上限 8(只读旁路)
    import concurrent.futures as _fut
    _panes=sorted({t.get('pane_id') for t in active if t.get('pane_id')}, key=lambda p:max([_upd(t) for t in active if t.get('pane_id')==p] or [0.0]), reverse=True)[:20]
    runtimes={}
    if _panes:
        with _fut.ThreadPoolExecutor(max_workers=8) as _ex:
            _res=dict(zip(_panes,_ex.map(_dashboard_pane_runtime,_panes)))
        runtimes={p:v for p,v in _res.items() if isinstance(v,dict)}
    # 最新交付:最近 20 个工作流各取有效交付(失败隔离)
    recent_wf=sorted({t.get('workflow_id') for t in all_tasks if t.get('workflow_id')}, key=lambda w:max([_upd(t) for t in all_tasks if t.get('workflow_id')==w] or [0.0]), reverse=True)[:20]
    notes_by_wf={}
    for wid in recent_wf:
        try:
            notes_by_wf[wid]=herdr_workflow_docs.annotate_notes(herdr_workflow_docs.load_notes(wid),current_base_sha=None)
        except Exception:
            continue
    for wid, notes in notes_by_wf.items():
        try:
            dl_notes=[n for n in notes if isinstance(n,dict) and n.get('kind')=='delivery']
            if not dl_notes:
                continue
            eff=herdr_delivery_record.select_effective_delivery(notes,workflow_id=wid)
            if not eff:
                continue
            try:
                ts=float(eff.get('ts') or 0) or now
            except (TypeError,ValueError):
                ts=now
            deliveries.append({'workflow_id':wid,'title':str(eff.get('title') or ''),'delivery_branch':str(eff.get('delivery_branch') or eff.get('body') or ''),'candidate_sha':str(eff.get('candidate_sha') or ''),'review_task':str(eff.get('review_task') or eff.get('task_id') or ''),'test_gate':str(eff.get('test_gate') or ''),'ts':ts})
        except Exception:
            continue
    # 选择器选项:全部工作流(最近优先,≤50),标题复用 _with_subject
    _by_wf_all={}
    for t in _all:
        _by_wf_all.setdefault(t.get('workflow_id') or 'unknown',[]).append(t)
    wf_options=[]
    for wid in sorted(_by_wf_all, key=lambda w:max([_upd(t) for t in _by_wf_all[w]] or [0.0]), reverse=True)[:50]:
        wts=_by_wf_all[wid]
        subj=_with_subject(dict(wf_map.get(wid) or {}, workflow_id=wid))
        wf_options.append({'workflow_id':wid,
                           'title':subj.get('title') or subj.get('requirement_subject') or wid,
                           'active':sum(1 for t in wts if t.get('status') not in {'superseded','cleaned'} and not t.get('superseded_by')),
                           'attention':sum(1 for t in wts if not t.get('superseded_by') and (t.get('finalize_escalated') or t.get('stage_verdict')=='blocked' or t.get('status') in {'blocked','failed'}))})
    # 需人裁决项:复用上面交付物扫描已读入的同一份账本,不再二次 load_notes
    # (每工作流原本会被读+解析两次)。
    for wid, notes in notes_by_wf.items():
        try:
            decisions.extend(herdr_human_decisions.collect_open_decisions(notes,workflow_id=wid,limit=20))
        except Exception:
            continue
    return herdr_dashboard.build_dashboard(all_tasks,blockers=blockers,actions_by_task=actions_by_task,deliveries=deliveries,stalls=stalls,anomalies=anomalies,runtimes=runtimes,decisions=decisions,scope=scope,workflows=wf_options,now=now,limits={'tasks':limit_tasks,'deliveries':10,'attention':30,'stuck':30,'decisions':30})

def api_task_signoff(b):
    b = b or {}
    tid=str(b.get('task_id') or '').strip()
    wid=str(b.get('workflow_id') or '').strip()
    node=str(b.get('node') or b.get('node_id') or '').strip()
    act=str(b.get('action') or 'approve').strip().lower()
    feedback=str(b.get('feedback') or '').strip()
    retry_target=str(b.get('retry_target') or '').strip()
    operator=str(b.get('operator') or 'human_studio').strip()

    if not wid or not node:
        if tid:
            for t in tasks():
                if t.get('task_id')==tid:
                    if not wid:wid=t.get('workflow_id')
                    if not node:node=t.get('node') or t.get('stage')
                    break
    if not wid:raise RuntimeError('workflow_id 不能为空')
    if not node:raise RuntimeError('node 不能为空')
    b.setdefault('workflow_id', wid)
    b.setdefault('node', node)

    if act=='approve':
        wf = workflows().get(wid)
        if not wf:
            try:
                wf = herdr_kernel._get_store().get_workflow(wid)
            except Exception:
                wf = None
        if not wf and not tasks_for_workflow(wid):
            raise RuntimeError(f'未找到工作流 {wid}')

        task = None
        if tid:
            store = herdr_kernel._get_store()
            task = store.get_task(tid) if hasattr(store, 'get_task') else None
            if not task:
                for t in tasks():
                    if t.get('task_id') == tid:
                        task = t
                        break
            if not task:
                raise RuntimeError(f'未找到任务 {tid}（工作流 {wid}）')
            if task.get('workflow_id') != wid:
                raise RuntimeError(f'任务 {tid} 不属于工作流 {wid}（所属工作流错配）')
            if task.get('status') == 'superseded':
                raise RuntimeError(f'任务 {tid} 已被替换或作废，请刷新页面')

        if not b.get('reason') and feedback:
            b['reason'] = feedback

        gate, reason, op = _validate_force_pass_params(b, wid, wf, task=task)

        exp_ver = b.get('expected_version')
        exp_task_vers = b.get('expected_task_versions')
        exp_pane = str(b.get('expected_pane_id') or b.get('pane_id') or '').strip()

        res = herdr_kernel.force_pass_gate(
            wid,
            gate_node_id=gate,
            note=reason,
            operator=op or operator,
            expected_version=exp_ver,
            task_id=tid or None,
            expected_pane_id=exp_pane or None,
            expected_task_versions=exp_task_vers,
        )
        return {'ok': True, 'action': 'approve', 'result': res}
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

HTML_TEMPLATE=r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>__PRODUCT_NAME__</title><style>:root {
  /* Linear Clean Light Palette */
  --bg-page: #f8f9fa;
  --bg-sidebar: #fafafa;
  --bg-surface: #ffffff;
  --bg-subtle: #f4f5f7;
  --bg-hover: #f0f1f3;
  --bg-active: #eceef2;
  --bg-elevated: #ffffff;

  /* Typography */
  --text-primary: #121316;
  --text-secondary: #5f6368;
  --text-tertiary: #8c919a;
  --text-inverse: #ffffff;

  /* Borders & Dividers */
  --border-default: #e5e7eb;
  --border-subtle: #f0f1f3;
  --border-focus: #5e6ad2;

  /* Primary Brand (Linear Blue-Violet / Royal Blue) */
  --primary: #5e6ad2;
  --primary-hover: #4f5bc4;
  --primary-subtle: rgba(94, 106, 210, 0.08);

  /* Status Colors */
  --success: #16a34a;
  --success-bg: #f0fdf4;
  --warning: #d97706;
  --warning-bg: #fffbeb;
  --danger: #dc2626;
  --danger-bg: #fef2f2;

  /* Radius & Shadows */
  --radius-sm: 6px;
  --radius-md: 8px;
  --radius-lg: 10px;
  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.04);
  --shadow-md: 0 4px 12px rgba(0, 0, 0, 0.06);
  --shadow-lg: 0 12px 28px rgba(0, 0, 0, 0.09);

  /* Compatibility Aliases */
  --bg: var(--bg-page);
  --panel: var(--bg-surface);
  --panel-elevated: var(--bg-elevated);
  --card: var(--bg-surface);
  --card-hover: var(--bg-subtle);
  --line: var(--border-default);
  --line-focus: var(--border-focus);
  --text: var(--text-primary);
  --muted: var(--text-secondary);
  --subtle: var(--text-tertiary);
  --accent: var(--primary);
  --accent-glow: rgba(94, 106, 210, 0.12);
  --good: var(--success);
  --warn: var(--warning);
  --bad: var(--danger);
}

* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg-page);
  color: var(--text-primary);
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif;
  font-size: 13px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
button, input, select, textarea { font: inherit; }
button { cursor: pointer; }
*:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }

/* Layout Shell */
.shell {
  display: grid;
  grid-template-columns: 232px minmax(0, 1fr);
  min-height: 100vh;
  background: var(--bg-page);
}

/* Sidebar (Linear Style) */
.sidebar {
  border-right: 1px solid var(--border-default);
  background: var(--bg-sidebar);
  padding: 16px 12px;
  position: sticky;
  top: 0;
  height: 100vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}
.brand {
  font-size: 15px;
  font-weight: 600;
  letter-spacing: -0.2px;
  color: var(--text-primary);
  padding: 0 8px;
}
.sub {
  color: var(--text-tertiary);
  font-size: 11px;
  margin: 2px 0 12px;
  padding: 0 8px;
}
.sidebar-cta {
  width: 100%;
  margin-bottom: 12px;
  height: 32px;
  padding: 0 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  color: var(--text-primary);
  font-size: 12.5px;
  font-weight: 500;
  border-radius: var(--radius-sm);
  box-shadow: var(--shadow-sm);
  transition: all .15s ease;
}
.sidebar-cta:hover {
  background: var(--bg-subtle);
  border-color: var(--text-tertiary);
}
.sidebar-section-title {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  padding: 8px 8px 4px;
}
.projects {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.project {
  width: 100%;
  text-align: left;
  background: transparent;
  border: 1px solid transparent;
  color: var(--text-primary);
  border-radius: var(--radius-sm);
  padding: 8px;
  transition: all .12s ease;
}
.project:hover {
  background: var(--bg-hover);
}
.project.active {
  background: var(--bg-active);
  font-weight: 600;
}
.project strong {
  font-size: 13px;
  font-weight: inherit;
  color: var(--text-primary);
}
.project small {
  display: block;
  color: var(--text-tertiary);
  margin-top: 1px;
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.project small.project-path {
  white-space: normal;
  overflow-wrap: anywhere;
  overflow: visible;
  text-overflow: clip;
}
.sidebar-spacer {
  flex: 1;
}
.sidebar-footer-btn {
  width: 100%;
  margin-top: 8px;
  height: 30px;
  padding: 0 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  border: 1px solid var(--border-subtle);
  background: var(--bg-surface);
  color: var(--text-secondary);
  font-size: 12px;
  border-radius: var(--radius-sm);
  transition: all .15s ease;
}
.sidebar-footer-btn:hover {
  background: var(--bg-subtle);
  color: var(--text-primary);
  border-color: var(--border-default);
}

/* Main Area */
.main {
  padding: 20px 28px;
  min-width: 0;
  width: 100%;
}

/* Header & Top Bar */
.top {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.title {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
  margin-bottom: 2px;
}
.wf-subject {
  font-size: 20px;
  font-weight: 600;
  color: var(--text-primary);
  line-height: 1.3;
}
.wf-sub {
  font-size: 12px;
  margin-top: 2px;
  color: var(--text-secondary);
}
.actions {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.actions .btn.primary {
  margin-left: auto;
}

/* Buttons (Linear Style) */
.btn {
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  color: var(--text-primary);
  border-radius: var(--radius-sm);
  padding: 8px 12px;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  font-weight: 500;
  transition: all .12s ease;
  user-select: none;
  box-shadow: var(--shadow-sm);
}
.btn:hover {
  background: var(--bg-subtle);
  border-color: #d1d5db;
}
.btn:active {
  transform: translateY(1px);
}
.btn.primary{background:#2563eb;color:#ffffff;border:1px solid #2563eb;font-weight:600;box-shadow:0 1px 3px rgba(0,0,0,.15);}
.btn.primary:hover {
  background: #1d4ed8;
  border-color: #1d4ed8;
  color: #ffffff;
}
.btn.primary:active {
  background: #1e40af;
  transform: translateY(1px);
}
.btn.danger-btn {
  background: var(--danger);
  color: #ffffff;
  border-color: var(--danger);
  font-weight: 600;
}
.btn.danger-btn:hover {
  background: #b91c1c;
}
.btn.is-loading {
  opacity: .7;
  pointer-events: none;
  cursor: wait;
}
.btn.icon-only {
  padding: 8px;
}
.btn-group {
  display: inline-flex;
  vertical-align: middle;
  border-radius: var(--radius-sm);
}
.btn-group .btn {
  border-radius: 0;
  margin-left: -1px;
}
.btn-group .btn:first-child {
  border-top-left-radius: var(--radius-sm);
  border-bottom-left-radius: var(--radius-sm);
  margin-left: 0;
}
.btn-group .btn:last-child {
  border-top-right-radius: var(--radius-sm);
  border-bottom-right-radius: var(--radius-sm);
}
.btn-group .btn:focus-visible {
  z-index: 1;
}

/* Dropdown */
.dropdown {
  position: relative;
  display: inline-block;
}
.dropdown-menu {
  display: none;
  position: absolute;
  right: 0;
  top: calc(100% + 4px);
  background: var(--bg-surface);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  min-width: 180px;
  z-index: 80;
  box-shadow: var(--shadow-md);
  padding: 4px;
}
.dropdown.open .dropdown-menu {
  display: block;
  animation: popIn .1s ease-out;
}
.dropdown-item {
  padding: 8px 12px;
  font-size: 12.5px;
  color: var(--text-primary);
  border-radius: var(--radius-sm);
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 8px;
  background: transparent;
  border: 0;
  width: 100%;
  text-align: left;
  font: inherit;
  transition: background .1s;
}
.dropdown-item:hover {
  background: var(--bg-hover);
}
.dropdown-item.danger-text {
  color: var(--danger);
}
.dropdown-item.danger-text:hover {
  background: var(--danger-bg);
}
.dropdown-divider {
  height: 1px;
  background: var(--border-subtle);
  margin: 4px 0;
}

/* Compact Metrics (Linear Header Meta) */
.metrics {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 8px 0 12px;
  margin-bottom: 16px;
  border-bottom: 1px solid var(--border-subtle);
  flex-wrap: wrap;
}
.metric {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: transparent;
  border: none;
  padding: 0;
  font-size: 12.5px;
  color: var(--text-secondary);
}
.metric b {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-variant-numeric: tabular-nums;
}
.metric span {
  font-size: 12px;
  color: var(--text-secondary);
}
.metric:not(:last-child)::after {
  content: '·';
  margin-left: 8px;
  color: var(--border-default);
}

/* Stage Progress Bar (Linear Workflow Progress) */
.stages {
  display: flex;
  align-items: center;
  gap: 8px;
  overflow-x: auto;
  margin-bottom: 16px;
  padding: 8px 12px;
  background: var(--bg-surface);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  scroll-behavior: smooth;
}
.stage-step {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 8px;
  border-radius: var(--radius-sm);
  font-size: 12.5px;
  white-space: nowrap;
  transition: all .15s ease;
}
.stage-indicator {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  font-size: 10.5px;
  font-weight: 700;
  flex-shrink: 0;
}
.stage-step.stage-done .stage-indicator {
  color: var(--text-secondary);
  background: var(--bg-subtle);
}
.stage-step.stage-done .stage-name {
  color: var(--text-secondary);
  font-weight: 500;
}
.stage-step.stage-running {
  background: var(--primary-subtle);
}
.stage-step.stage-running .stage-indicator {
  color: #ffffff;
  background: var(--primary);
}
.stage-step.stage-running .stage-name {
  color: var(--text-primary);
  font-weight: 600;
}
.stage-step.stage-next {
  background: var(--bg-subtle);
}
.stage-step.stage-next .stage-indicator {
  color: var(--primary);
  background: transparent;
  border: 1.5px solid var(--primary);
}
.stage-step.stage-next .stage-name {
  color: var(--text-primary);
  font-weight: 600;
}
.stage-step.stage-pending .stage-indicator {
  color: var(--text-tertiary);
  background: transparent;
  border: 1px solid var(--border-default);
}
.stage-step.stage-pending .stage-name {
  color: var(--text-tertiary);
  font-weight: 400;
}
.stage-badge {
  font-size: 10.5px;
  font-weight: 600;
  padding: 1px 8px;
  border-radius: 999px;
  line-height: 1.3;
}
.stage-badge.running {
  background: var(--primary);
  color: #ffffff;
}
.stage-badge.blocked {
  background: var(--danger);
  color: #ffffff;
}
.stage-badge.next {
  background: rgba(94, 106, 210, 0.12);
  color: var(--primary);
}
.stage-meta {
  font-size: 11px;
  color: var(--text-tertiary);
}
.stage-connector {
  flex: 1;
  min-width: 14px;
  height: 1px;
  background: var(--border-default);
}

/* Badges (Linear Minimalist Pills) */
.badge {
  font-size: 11px;
  font-weight: 500;
  border-radius: 999px;
  padding: 2px 8px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  line-height: 1.4;
  border: 1px solid var(--border-default);
  background: var(--bg-subtle);
  color: var(--text-secondary);
}
.badge.cleaned, .badge.completed, .badge.committed, .badge.integrated {
  color: var(--success);
  border-color: rgba(22, 163, 74, 0.25);
  background: var(--success-bg);
}
.badge.working, .badge.dispatched, .badge.in_progress, .badge.finalizing {
  color: var(--primary);
  border-color: rgba(94, 106, 210, 0.25);
  background: var(--primary-subtle);
}
.badge.failed, .badge.blocked {
  color: var(--danger);
  border-color: rgba(220, 38, 38, 0.25);
  background: var(--danger-bg);
}
.badge.rework {
  color: var(--warning);
  border-color: rgba(217, 119, 6, 0.25);
  background: var(--warning-bg);
}
.badge.waiting, .badge.pending, .badge.superseded {
  color: var(--text-tertiary);
  background: var(--bg-subtle);
  border-color: var(--border-subtle);
}

/* Attention Banner */
.attention-banner {
  background: var(--bg-surface);
  border: 1px solid var(--border-default);
  border-left: 3px solid var(--primary);
  border-radius: var(--radius-md);
  padding: 12px 16px;
  margin-bottom: 16px;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  box-shadow: var(--shadow-sm);
}
.att-badge {
  background: var(--primary-subtle);
  color: var(--primary);
  font-weight: 600;
  font-size: 11px;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  letter-spacing: 0.3px;
  text-transform: uppercase;
}
.att-text {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary);
}
.decision-list {
  display: grid;
  gap: 8px;
  margin-top: 8px;
}
.decision-list-label {
  font-size: 11px;
  color: var(--primary);
  font-weight: 600;
  letter-spacing: .4px;
}
.decision-item {
  display: grid;
  grid-template-columns: minmax(120px,.7fr) minmax(180px,1fr) minmax(180px,1.2fr);
  gap: 8px;
  align-items: baseline;
  padding: 8px 0;
  border-top: 1px solid var(--border-subtle);
  font-size: 12px;
}
.decision-item strong {
  font-size: 12.5px;
}
.decision-item small {
  color: var(--text-tertiary);
  font-size: 11.5px;
}
.decision-more {
  color: var(--text-secondary);
  font-size: 11.5px;
}
.dash-kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));background:var(--bg-surface);border:1px solid var(--border-default);border-radius:var(--radius-md);margin-bottom:16px;overflow:hidden}
.dash-kpi{padding:12px 16px;border-left:1px solid var(--border-subtle)}
.dash-kpi:first-child{border-left:0}
.dash-kpi .k{font-size:11px;color:var(--text-secondary)}
.dash-kpi .v{font-size:18px;font-weight:650;color:var(--text-primary);font-variant-numeric:tabular-nums}
.dash-kpi .v small{font-size:12px;color:var(--text-tertiary);font-weight:500}
.dash-kpi .v.warn{color:var(--warning)}
.dash-kpi .s{font-size:11px;color:var(--text-tertiary)}
.dash-sec{background:var(--bg-surface);border:1px solid var(--border-default);border-radius:var(--radius-md);margin-bottom:16px;overflow:hidden}
.dash-sec-h{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid var(--border-default);font-size:13px;font-weight:650;color:var(--text-primary)}
.dash-sec-h .count{font-size:11.5px;color:var(--text-tertiary);font-weight:500;font-variant-numeric:tabular-nums}
.dash-task{display:grid;grid-template-columns:26px minmax(0,1fr) auto;gap:8px;align-items:start;padding:12px 16px;border-top:1px solid var(--border-subtle)}
.dash-task:first-of-type{border-top:0}
.dash-task .n{color:var(--text-tertiary);font-size:12px;padding-top:2px;font-variant-numeric:tabular-nums}
.dash-task .t{font-weight:600;font-size:13px;color:var(--text-primary);overflow-wrap:anywhere}
.dash-task .d{color:var(--text-secondary);font-size:12px;margin-top:1px}
.dash-task .m{color:var(--text-tertiary);font-size:11px;margin-top:2px}
.dash-q{padding:12px 16px;border-top:1px solid var(--border-subtle)}
.dash-q:first-of-type{border-top:0}
.dash-q .qt{font-weight:600;font-size:13px;color:var(--text-primary);display:flex;justify-content:space-between;gap:8px;align-items:flex-start}
.dash-default{font-size:11px;font-weight:600;color:var(--warning);background:var(--warning-bg);border-radius:999px;padding:2px 8px;white-space:nowrap}
.dash-q .qd{display:grid;grid-template-columns:44px minmax(0,1fr);gap:8px;margin-top:8px;font-size:12px;color:var(--text-secondary)}
.dash-q .qm{color:var(--text-tertiary);font-size:11px;margin-top:8px}
.dash-btns{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}
.dash-ok{display:flex;gap:8px;align-items:center;padding:16px;color:var(--text-secondary);font-size:13px}
.dash-layout{display:grid;grid-template-columns:minmax(0,1fr) 364px;gap:16px;align-items:start}
.dash-rail{position:sticky;top:12px;display:flex;flex-direction:column;min-width:0}
.dash-rail .dash-sec{border-top:2px solid var(--warning)}
@media(max-width:940px){.dash-layout{grid-template-columns:minmax(0,1fr)}.dash-rail{position:static}}
.dash-dot{width:8px;height:8px;border-radius:50%;flex:none}
.dash-dot.green{background:var(--success)}
.dash-dot.amber{background:var(--warning)}
.dash-dot.red{background:var(--danger)}
.dash-dot.gray{background:var(--text-tertiary)}

/* Linear Standard Select / Workflow Switcher */
.wf-switcher {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 16px;
}
.wf-switcher label {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
}
.wf-switcher select {
  background: var(--bg-surface);
  color: var(--text-primary);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  padding: 8px 12px;
  max-width: 520px;
}

.linear-select {
  position: relative;
  display: inline-block;
}
.linear-trigger {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  height: 32px;
  padding: 0 12px 0 8px;
  background: var(--bg-surface);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  box-shadow: var(--shadow-sm);
  color: var(--text-primary);
  cursor: pointer;
  transition: all .12s ease;
  max-width: 540px;
}
.linear-trigger:hover {
  background: var(--bg-hover);
  border-color: #d1d5db;
}
.linear-select.open .linear-trigger,
.linear-trigger:focus-visible {
  border-color: var(--primary);
  box-shadow: 0 0 0 2px var(--primary-subtle);
  outline: none;
}
.trigger-icon {
  color: var(--text-tertiary);
  display: flex;
  align-items: center;
}
.trigger-content {
  display: flex;
  align-items: center;
  gap: 8px;
  overflow: hidden;
  white-space: nowrap;
}
.trigger-title {
  font-weight: 500;
  font-size: 12.5px;
  color: var(--text-primary);
  text-overflow: ellipsis;
  overflow: hidden;
}
.trigger-mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 11px;
  color: var(--text-tertiary);
  background: var(--bg-subtle);
  padding: 1px 4px;
  border-radius: 4px;
  border: 1px solid var(--border-subtle);
}
.trigger-pills {
  display: flex;
  align-items: center;
  gap: 4px;
}
.trigger-chevron {
  color: var(--text-tertiary);
  margin-left: auto;
  transition: transform .15s ease;
  display: flex;
  align-items: center;
}
.linear-select.open .trigger-chevron {
  transform: rotate(180deg);
}

.linear-popover {
  display: none;
  position: absolute;
  top: calc(100% + 5px);
  left: 0;
  width: 440px;
  max-height: 440px;
  background: var(--bg-surface);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  box-shadow: 0 10px 30px -4px rgba(0, 0, 0, 0.12), 0 4px 8px -2px rgba(0, 0, 0, 0.06);
  z-index: 100;
  flex-direction: column;
  overflow: hidden;
  animation: popIn 0.1s cubic-bezier(0.16, 1, 0.3, 1);
}
.linear-select.open .linear-popover {
  display: flex;
}

.popover-search-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border-default);
  background: var(--bg-surface);
}
.popover-search-icon {
  color: var(--text-tertiary);
  flex-shrink: 0;
}
.popover-search-input {
  width: 100%;
  border: none;
  background: transparent;
  outline: none;
  font-size: 12.5px;
  color: var(--text-primary);
}
.popover-search-input::placeholder {
  color: var(--text-tertiary);
}
.popover-shortcut {
  font-size: 10px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  background: var(--bg-subtle);
  border: 1px solid var(--border-default);
  color: var(--text-tertiary);
  padding: 1px 4px;
  border-radius: 3px;
}

.popover-list {
  overflow-y: auto;
  max-height: 360px;
  padding: 4px;
}
.popover-list::-webkit-scrollbar {
  width: 5px;
}
.popover-list::-webkit-scrollbar-thumb {
  background: #e2e4e9;
  border-radius: 4px;
}
.popover-group {
  margin-bottom: 4px;
}
.popover-group-header {
  padding: 8px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-tertiary);
  display: flex;
  align-items: center;
  justify-content: space-between;
  cursor: pointer;
  border-radius: var(--radius-sm);
  user-select: none;
  transition: background .08s ease;
}
.popover-group-header:hover {
  background: var(--bg-subtle);
  color: var(--text-secondary);
}
.popover-group-title {
  display: flex;
  align-items: center;
  gap: 8px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
}
.popover-group-chevron {
  color: var(--text-tertiary);
  transition: transform .15s ease;
  display: flex;
  align-items: center;
}
.popover-group.collapsed .popover-group-chevron {
  transform: rotate(-90deg);
}
.popover-group.collapsed .popover-group-items {
  display: none;
}

.popover-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background .08s ease;
  position: relative;
}
.popover-item:hover,
.popover-item.keyboard-focus {
  background: var(--bg-hover);
}
.popover-item.selected {
  background: var(--primary-subtle);
}
.popover-item.selected .item-title {
  color: var(--primary);
}

.item-check {
  width: 14px;
  height: 14px;
  margin-top: 2px;
  color: var(--primary);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  opacity: 0;
}
.popover-item.selected .item-check {
  opacity: 1;
}
.item-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.item-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.item-title {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}
.item-badges {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
  white-space: nowrap;
}
.item-sub {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: var(--text-tertiary);
}
.item-id {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  color: var(--text-tertiary);
}
.highlight {
  background: rgba(94, 106, 210, 0.15);
  color: var(--primary);
  border-radius: 2px;
  padding: 0 1px;
  font-weight: 600;
}
.popover-empty {
  padding: 24px 16px;
  text-align: center;
  color: var(--text-tertiary);
  font-size: 12px;
}
.badge-pill {
  font-size: 11px;
  height: 19px;
  padding: 0 8px;
  border-radius: 999px;
  font-weight: 500;
  display: inline-flex;
  align-items: center;
  gap: 4.5px;
  line-height: 1;
  white-space: nowrap;
  flex-shrink: 0;
  box-sizing: border-box;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-variant-numeric: tabular-nums;
  border: 1px solid transparent;
}
.badge-pill::before {
  content: "";
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
  flex-shrink: 0;
}
.badge-pill.nodot::before {
  display: none;
}
.badge-pill.active {
  background: #ecfdf5;
  color: #047857;
  border-color: #a7f3d0;
}
.badge-pill.attention {
  background: #fffbeb;
  color: #b45309;
  border-color: #fde68a;
}
.badge-pill.completed {
  background: var(--bg-subtle);
  color: var(--text-tertiary);
  border-color: var(--border-subtle);
}
.badge-pill.completed::before {
  background: #9ca3af;
}

/* 任务看板 */
.workspace-layout {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.panel {
  background: var(--bg-surface);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  overflow: visible;
}
.panel-header {
  padding: 12px 16px;
  border-bottom: 1px solid var(--border-default);
  border-top-left-radius: var(--radius-md);
  border-top-right-radius: var(--radius-md);
}
.panel-title-area {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.panel-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0;
}
.task-filters {
  display: flex;
  gap: 4px;
  background: var(--bg-subtle);
  padding: 2px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-subtle);
}
.filter-btn {
  background: transparent;
  border: none;
  color: var(--text-secondary);
  border-radius: var(--radius-sm);
  padding: 4px 12px;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: all .12s ease;
}
.filter-btn:hover {
  color: var(--text-primary);
}
.filter-btn.active {
  background: var(--bg-surface);
  color: var(--text-primary);
  font-weight: 600;
  box-shadow: var(--shadow-sm);
}
.filter-cnt {
  background: var(--bg-subtle);
  border-radius: 999px;
  padding: 1px 8px;
  margin-left: 4px;
  font-size: 11px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-variant-numeric: tabular-nums;
}

/* 任务列表 */
.task-list {
  background: var(--bg-surface);
  overflow: visible;
}
.task {
  padding: 8px 16px;
  border-bottom: 1px solid var(--border-subtle);
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 12px;
  align-items: center;
  transition: background .12s ease;
  min-height: 44px;
}
.task-list .task {
  grid-template-columns: 20px minmax(200px, 1fr) 120px auto 28px;
  cursor: pointer;
}
.task:last-child {
  border-bottom: none;
  border-bottom-left-radius: var(--radius-md);
  border-bottom-right-radius: var(--radius-md);
}
.task:hover {
  background: var(--bg-hover);
}
.task.task-highlight {
  background: var(--primary-subtle);
  border-left: 3px solid var(--primary);
}
.task-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  font-size: 10px;
  font-weight: 700;
}
.task-icon.done {
  color: var(--success);
  background: var(--success-bg);
}
.task-icon.working {
  color: var(--primary);
  background: var(--primary-subtle);
}
.task-icon.blocked {
  color: var(--danger);
  background: var(--danger-bg);
}
.task-icon.pending {
  color: var(--text-tertiary);
  border: 1px solid var(--border-default);
}
.task-main {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}
.task-name {
  font-weight: 500;
  font-size: 13px;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.task-id {
  color: var(--text-tertiary);
  font-size: 11.5px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}
.task-agent {
  color: var(--text-secondary);
  font-size: 12px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.task-status {
  display: flex;
  align-items: center;
  justify-content: flex-end;
}
.task-menu {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: flex-end;
}
.task-menu-btn {
  width: 28px;
  height: 28px;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-sm);
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-tertiary);
  font-size: 15px;
  font-weight: 700;
  line-height: 1;
  cursor: pointer;
  transition: all .12s ease;
  letter-spacing: -1px;
}
.task-menu-btn:hover, .task-menu.open .task-menu-btn {
  background: var(--bg-hover);
  border-color: var(--border-default);
  color: var(--text-primary);
}
.task-dropdown-menu {
  display: none;
  position: absolute;
  right: 0;
  top: calc(100% + 2px);
  background: var(--bg-surface);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  min-width: 140px;
  z-index: 100;
  box-shadow: var(--shadow-md);
  padding: 4px;
}
.task-menu.open .task-dropdown-menu {
  display: block;
  animation: popIn .1s ease-out;
}
.task-dropdown-item {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border: none;
  background: transparent;
  color: var(--text-primary);
  font-size: 12px;
  font-weight: 500;
  border-radius: var(--radius-sm);
  text-align: left;
  cursor: pointer;
  white-space: nowrap;
  transition: background .12s ease;
  box-sizing: border-box;
}
.task-dropdown-item:hover {
  background: var(--bg-hover);
}
.task-dropdown-item.danger {
  color: var(--danger);
}
.task-dropdown-item.danger:hover {
  background: var(--danger-bg);
}
.task-dropdown-item.primary {
  color: var(--primary);
  font-weight: 600;
}
.task-dropdown-item.primary:hover {
  background: var(--primary-subtle);
}
.task-dropdown-divider {
  height: 1px;
  background: var(--border-subtle);
  margin: 4px 0;
}
.task-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.mini, .mini-btn {
  padding: 3px 8px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  color: var(--text-secondary);
  font-size: 11.5px;
  font-weight: 500;
  transition: all .12s ease;
  white-space: nowrap;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.mini:hover, .mini-btn:hover {
  background: var(--bg-subtle);
  border-color: #d1d5db;
  color: var(--text-primary);
}
.mini-btn.primary-subtle {
  color: var(--primary);
  border-color: rgba(94, 106, 210, 0.3);
  font-weight: 600;
}
.mini-btn.primary-subtle:hover {
  background: var(--primary-subtle);
  border-color: var(--primary);
}

/* 协同资源与工位 */
.resources-details {
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  overflow: hidden;
}
.resources-summary {
  padding: 12px 16px;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text-secondary);
  cursor: pointer;
  display: flex;
  justify-content: space-between;
  align-items: center;
  user-select: none;
  background: var(--bg-subtle);
  transition: background .12s;
}
.resources-summary:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}
.resources-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 12px;
  padding: 16px;
  border-top: 1px solid var(--border-subtle);
}
.res-card {
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  padding: 8px 12px;
}
.res-card h4 {
  margin: 0 0 8px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}
.agent-row, .slot-row, .alert-row {
  padding: 8px 0;
  border-bottom: 1px solid var(--border-subtle);
  display: flex;
  justify-content: space-between;
  gap: 8px;
  align-items: center;
  font-size: 12px;
}
.agent-row:last-child, .slot-row:last-child, .alert-row:last-child {
  border-bottom: none;
}
.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  display: inline-block;
  margin-right: 8px;
  background: var(--text-tertiary);
}
.dot.ready { background: var(--success); }
.dot.working { background: var(--primary); }
.dot.disabled, .dot.failed { background: var(--danger); }

/* Modals & Forms */
.modal {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.35);
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
  display: none;
  align-items: center;
  justify-content: center;
  padding: 16px;
  z-index: 50;
}
.modal.open {
  display: flex;
  animation: fadeIn .12s ease-out;
}
.modal-card {
  width: min(860px, 100%);
  max-height: 86vh;
  overflow: auto;
  background: var(--bg-surface);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
}
.modal-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 18px;
  border-bottom: 1px solid var(--border-default);
  position: sticky;
  top: 0;
  background: var(--bg-surface);
  z-index: 2;
}
.modal-head strong {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
}
.modal-body {
  padding: 18px;
}
.close {
  background: transparent;
  color: var(--text-secondary);
  border: 0;
  width: 28px;
  height: 28px;
  border-radius: var(--radius-sm);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all .12s;
}
.close:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}
.form {
  display: grid;
  gap: 8px;
}
.form label {
  font-size: 12px;
  color: var(--text-secondary);
  font-weight: 600;
}
.form input, .form select, .form textarea {
  width: 100%;
  background: var(--bg-surface);
  color: var(--text-primary);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  padding: 8px 12px;
  font-size: 13px;
  transition: border-color .15s;
}
.form input:focus, .form select:focus, .form textarea:focus {
  border-color: var(--primary);
}
.form textarea {
  min-height: 100px;
  line-height: 1.5;
}

/* Deep Physical Drawer */
.deep-drawer {
  position: fixed;
  bottom: 0;
  left: 232px;
  right: 0;
  background: var(--bg-surface);
  border-top: 1px solid var(--border-default);
  box-shadow: 0 -4px 16px rgba(0,0,0,0.06);
  z-index: 40;
  transition: transform .2s ease-in-out;
}
.deep-drawer.collapsed {
  transform: translateY(calc(100% - 38px));
}
.drawer-head {
  height: 38px;
  padding: 0 16px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  cursor: pointer;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border-default);
  user-select: none;
}
.drawer-head:hover {
  background: var(--bg-hover);
}
.drawer-pill {
  background: var(--primary-subtle);
  border: 1px solid rgba(94,106,210,0.25);
  color: var(--primary);
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 4px;
}
.drawer-body {
  height: 280px;
  display: flex;
  flex-direction: column;
  background: var(--bg-page);
}
.drawer-tabs {
  display: flex;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border-default);
}
.dtab {
  padding: 8px 16px;
  background: transparent;
  border: 0;
  border-bottom: 2px solid transparent;
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
}
.dtab:hover {
  color: var(--text-primary);
}
.dtab.active {
  color: var(--primary);
  border-bottom-color: var(--primary);
  font-weight: 600;
}
.drawer-view {
  flex: 1;
  overflow: auto;
  padding: 12px 16px;
  background: #fafbfc;
}

/* 任务详情抽屉 V1：右侧面板，非通用弹窗 */
.task-drawer {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  width: min(680px, 100%);
  z-index: 45;
  pointer-events: none;
}
.task-drawer[hidden] {
  display: none;
}
.task-drawer.open {
  pointer-events: auto;
}
.task-drawer-card {
  height: 100%;
  background: var(--bg-surface);
  border-left: 1px solid var(--border-default);
  box-shadow: var(--shadow-lg);
  display: flex;
  flex-direction: column;
  animation: fadeIn .14s ease-out;
}
.task-drawer-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  padding: 16px 18px 12px;
  border-bottom: 1px solid var(--border-default);
  background: var(--bg-surface);
}
.task-drawer-title {
  font-size: 16px;
  font-weight: 700;
  color: var(--text-primary);
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.task-drawer-id {
  font-size: 11.5px;
  color: var(--text-tertiary);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-variant-numeric: tabular-nums;
  margin-top: 2px;
  word-break: break-all;
}
.task-drawer-meta {
  font-size: 12px;
  color: var(--text-secondary);
  margin-top: 8px;
}
.task-drawer-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.task-drawer-menu-wrap {
  position: relative;
}
.task-drawer-menu {
  display: none;
  position: absolute;
  right: 0;
  top: 32px;
  min-width: 180px;
  background: var(--bg-surface);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-md);
  padding: 4px;
  z-index: 3;
}
.task-drawer-menu.open {
  display: block;
}
.td-tabs {
  display: flex;
  gap: 2px;
  padding: 0 18px;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border-default);
}
.td-tab {
  padding: 12px 16px;
  background: transparent;
  border: 0;
  border-bottom: 2px solid transparent;
  color: var(--text-secondary);
  font-size: 13px;
  cursor: pointer;
}
.td-tab:hover {
  color: var(--text-primary);
}
.td-tab.active {
  color: var(--primary);
  border-bottom-color: var(--primary);
  font-weight: 600;
}
.td-tab:focus-visible,
.task-drawer .close:focus-visible,
.task-drawer .mini:focus-visible,
.task-drawer .btn:focus-visible {
  outline: 2px solid var(--border-focus);
  outline-offset: 1px;
}
.task-drawer-body {
  flex: 1;
  overflow: auto;
  padding: 16px 18px 24px;
  background: var(--bg-surface);
}
.td-attrs {
  display: grid;
  grid-template-columns: 88px 1fr;
  gap: 8px 12px;
  padding: 12px 16px;
  background: var(--bg-subtle);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  margin-bottom: 16px;
  font-size: 12.5px;
}
.td-attr-k {
  color: var(--text-tertiary);
}
.td-attr-v {
  color: var(--text-primary);
  min-width: 0;
  word-break: break-word;
}
.td-sec {
  margin-bottom: 16px;
}
.td-lbl {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 8px;
}
.td-txt {
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-primary);
  white-space: pre-wrap;
  word-break: break-word;
}
.td-timeline {
  display: grid;
  gap: 0;
}
.td-ev {
  display: grid;
  grid-template-columns: 64px 18px 1fr;
  gap: 8px;
  align-items: start;
}
.td-ev-time {
  font-size: 11.5px;
  color: var(--text-tertiary);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  padding-top: 2px;
  text-align: right;
}
.td-ev-rail {
  display: flex;
  flex-direction: column;
  align-items: center;
  min-height: 100%;
}
.td-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--border-default);
  margin-top: 4px;
  flex-shrink: 0;
}
.td-dot.done {
  background: var(--success);
}
.td-dot.warn {
  background: var(--warning);
}
.td-dot.bad {
  background: var(--danger);
}
.td-dot.info {
  background: var(--primary);
}
.td-ev-line {
  width: 1px;
  flex: 1;
  min-height: 14px;
  background: var(--border-default);
}
.td-ev-body {
  padding-bottom: 16px;
  min-width: 0;
}
.td-ev-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}
.td-ev-detail {
  font-size: 12px;
  color: var(--text-secondary);
  margin-top: 2px;
  word-break: break-word;
  white-space: pre-wrap;
}
.td-art {
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  padding: 12px;
  margin-bottom: 8px;
  background: var(--bg-surface);
}
.td-art-hd {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

/* Toast */
.toast {
  position: fixed;
  right: 24px;
  bottom: 24px;
  background: var(--bg-surface);
  border: 1px solid var(--border-default);
  padding: 12px 16px;
  border-radius: var(--radius-md);
  display: none;
  max-width: 540px;
  max-height: 80vh;
  z-index: 90;
  box-shadow: var(--shadow-md);
  font-size: 13px;
  line-height: 1.4;
  color: var(--text-primary);
  user-select: text;
  -webkit-user-select: text;
}
.toast.show {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  animation: slideUp .15s ease-out;
}
.toast.danger-toast {
  border-color: var(--danger);
  box-shadow: 0 4px 16px rgba(239, 68, 68, 0.2);
}
.toast-msg {
  flex: 1;
  word-break: break-word;
  white-space: pre-wrap;
  max-height: 60vh;
  overflow-y: auto;
}
.toast-close {
  background: transparent;
  border: none;
  color: var(--text-secondary);
  cursor: pointer;
  padding: 2px 8px;
  font-size: 15px;
  font-weight: bold;
  line-height: 1;
  border-radius: 4px;
  flex-shrink: 0;
  margin-left: 4px;
}
.toast-close:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

/* Typography & Helpers */
.danger-text { color: var(--danger); }
.good-text { color: var(--success); }
.warn-text { color: var(--warning); }
.muted { color: var(--text-secondary); }
.empty { padding: 24px 16px; color: var(--text-tertiary); font-size: 13px; text-align: center; }
pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; line-height: 1.5; }

/* Animations */
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
@keyframes slideUp { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
@keyframes popIn { from { opacity: 0; transform: scale(0.97); } to { opacity: 1; transform: scale(1); } }
@keyframes spin { to { transform: rotate(360deg); } }

/* Preflight, Signoff & Ops Center */
.signoff-box { display: grid; gap: 16px; }
.signoff-head { display: flex; justify-content: space-between; align-items: center; padding-bottom: 12px; border-bottom: 1px solid var(--border-default); }
.signoff-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--border-default); }
.spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid rgba(0,0,0,0.15); border-radius: 50%; border-top-color: var(--primary); animation: spin .8s linear infinite; vertical-align: middle; }
.preflight-box { display: grid; gap: 12px; }
.preflight-head { background: var(--bg-subtle); border: 1px solid var(--border-default); border-radius: var(--radius-sm); padding: 12px 16px; }
.preflight-prog { height: 4px; background: var(--border-default); border-radius: 2px; overflow: hidden; margin-top: 8px; }
.preflight-prog-fill { height: 100%; width: 0%; background: var(--primary); transition: width .3s ease; }
.preflight-row { background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: var(--radius-sm); padding: 12px; display: flex; justify-content: space-between; gap: 8px; align-items: flex-start; }
.preflight-row.is-ready { border-color: rgba(22, 163, 74, 0.35); }
.preflight-row.is-failed { border-color: rgba(220, 38, 38, 0.35); }
.preflight-row.is-warn { border-color: rgba(217, 119, 6, 0.35); }
.fleet-table { border: 1px solid var(--border-default); border-radius: var(--radius-sm); overflow: hidden; margin-top: 8px; }
.fleet-header, .fleet-row { display: grid; grid-template-columns: 120px 80px minmax(120px, 1.2fr) 70px 80px minmax(120px, 1.5fr); gap: 8px; align-items: center; padding: 8px 12px; background: var(--bg-surface); border-bottom: 1px solid var(--border-subtle); font-size: 12px; }
.fleet-header { font-size: 11px; font-weight: 600; color: var(--text-tertiary); background: var(--bg-subtle); text-transform: uppercase; letter-spacing: 0.5px; }
.fleet-row:last-child { border-bottom: 0; }
.fleet-row:hover { background: var(--bg-hover); }
/* Ops Center Split-View Redesign */
.ops-dashboard-grid { display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(360px, 1fr); gap: 16px; align-items: start; margin-top: 8px; }
@media (max-width: 1080px) { .ops-dashboard-grid { grid-template-columns: 1fr; } }
.ops-col-left, .ops-col-right { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.ops-panel-anomaly, .ops-panel-fleet { background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: var(--radius-md); padding: 16px; box-shadow: var(--shadow-sm); }
.ops-section-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 8px; }
.ops-section-title { font-size: 13px; font-weight: 600; color: var(--text-primary); display: flex; align-items: center; gap: 8px; }
.ops-badge { font-size: 11px; font-weight: 600; color: var(--text-secondary); background: var(--bg-subtle); border: 1px solid var(--border-subtle); padding: 2px 8px; border-radius: 999px; }
.ops-badge.danger { background: rgba(220, 38, 38, 0.12); color: var(--danger); border-color: rgba(220, 38, 38, 0.3); }
.ops-filter-pills { display: inline-flex; background: var(--bg-subtle); padding: 2px; border-radius: 6px; border: 1px solid var(--border-subtle); gap: 2px; }
.ops-filter-pill { border: none; background: transparent; font-size: 11.5px; padding: 4px 8px; border-radius: 4px; cursor: pointer; color: var(--text-secondary); font-weight: 500; transition: all .15s ease; }
.ops-filter-pill:hover { color: var(--text-primary); background: var(--bg-hover); }
.ops-filter-pill.active { background: var(--bg-surface); color: var(--text-primary); font-weight: 600; box-shadow: 0 1px 2px rgba(0,0,0,0.06); }

.ops-card { background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: var(--radius-md); padding: 16px; margin-bottom: 12px; transition: border-color .15s ease, box-shadow .15s ease; box-shadow: var(--shadow-sm); }
.ops-card:hover { border-color: rgba(94, 106, 210, 0.5); }
.ops-card.is-failed { border-left: 3px solid var(--danger); }
.ops-card.is-blocked { border-left: 3px solid var(--warning); }
.ops-card.is-working { border-left: 3px solid var(--primary); }
.ops-card.is-completed { border-left: 3px solid var(--success); }

.ops-card-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 8px; }
.ops-card-title-group { display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1; }
.ops-card-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-tertiary); flex-shrink: 0; }
.ops-card-dot.is-working { background: var(--primary); }
.ops-card-dot.is-completed { background: var(--success); }
.ops-card-dot.is-blocked { background: var(--warning); }
.ops-card-dot.is-failed { background: var(--danger); }

.ops-card-name { font-size: 13.5px; font-weight: 600; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ops-card-status-badge { font-size: 10.5px; font-weight: 600; padding: 2px 8px; border-radius: 999px; }
.ops-card-status-badge.is-working { background: var(--primary-subtle); color: var(--primary); }
.ops-card-status-badge.is-completed { background: rgba(22, 163, 74, 0.12); color: var(--success); }
.ops-card-status-badge.is-blocked { background: var(--warning-bg); color: var(--warning); }
.ops-card-status-badge.is-failed { background: rgba(220, 38, 38, 0.12); color: var(--danger); }

.ops-card-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.ops-card-time { font-size: 11.5px; color: var(--text-tertiary); }
.ops-card-meta-line { font-size: 12px; color: var(--text-secondary); margin-bottom: 8px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }

.ops-card-progress-wrap { margin: 8px 0 12px; }
.ops-progress-bar { height: 6px; border-radius: 999px; background: var(--bg-subtle); border: 1px solid var(--border-subtle); overflow: hidden; }
.ops-progress-fill { height: 100%; border-radius: 999px; background: var(--success); transition: width .3s ease; }
.ops-card.is-failed .ops-progress-fill { background: var(--danger); }
.ops-card.is-blocked .ops-progress-fill { background: var(--warning); }
.ops-progress-meta { display: flex; justify-content: space-between; font-size: 11px; color: var(--text-secondary); margin-top: 4px; }

.ops-stage-callout { display: flex; justify-content: space-between; align-items: center; background: var(--bg-subtle); border: 1px solid var(--border-subtle); border-radius: 6px; padding: 8px 12px; font-size: 12px; gap: 8px; }
.ops-stage-callout.is-failed { background: rgba(220, 38, 38, 0.06); border-color: rgba(220, 38, 38, 0.25); color: var(--danger); }
.ops-stage-callout.is-blocked { background: rgba(217, 119, 6, 0.06); border-color: rgba(217, 119, 6, 0.25); color: var(--warning); }
.ops-stage-callout.is-working { background: var(--primary-subtle); border-color: rgba(94, 106, 210, 0.25); color: var(--primary); }
.ops-callout-content { display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1; }
.ops-callout-text { font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ops-toggle-details-btn { border: none; background: transparent; color: var(--text-tertiary); font-size: 11px; cursor: pointer; padding: 2px 8px; border-radius: 4px; flex-shrink: 0; }
.ops-toggle-details-btn:hover { color: var(--text-primary); background: var(--bg-hover); }

.ops-card-nodes-detail { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; padding-top: 12px; border-top: 1px dashed var(--border-subtle); }

.ops-anomaly-card { background: var(--bg-surface); border: 1px solid rgba(220, 38, 38, 0.25); border-left: 3px solid var(--danger); border-radius: var(--radius-sm); padding: 12px; margin-bottom: 8px; font-size: 12px; }
.ops-anomaly-card.is-blocked { border-color: rgba(217, 119, 6, 0.25); border-left-color: var(--warning); }
.ops-anomaly-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.ops-anomaly-event { font-size: 11px; color: var(--text-secondary); margin: 8px 0; background: var(--bg-subtle); padding: 4px 8px; border-radius: 4px; word-break: break-all; }
.ops-anomaly-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }

.ops-empty-clean { text-align: center; padding: 24px 16px; background: var(--bg-subtle); border: 1px dashed var(--border-subtle); border-radius: var(--radius-sm); }
.ops-empty-icon { font-size: 20px; margin-bottom: 4px; }
.ops-empty-title { font-size: 13px; font-weight: 600; color: var(--text-primary); }
.ops-empty-sub { font-size: 11.5px; color: var(--text-secondary); margin-top: 2px; }

/* Controller cockpit — light high-contrast redesign (Linear clean) */
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; background: var(--bg-subtle); border: 1px solid var(--border-subtle); border-radius: 4px; padding: 1px 4px; color: var(--text-primary); }
.ctl-wrap { display: grid; gap: 16px; line-height: 1.5; max-height: 75vh; overflow-y: auto; padding-right: 4px; }
.ctl-status { background: var(--bg-surface); border: 1px solid var(--border-default); border-left: 3px solid var(--success); border-radius: var(--radius-md); padding: 12px 16px; box-shadow: var(--shadow-sm); }
.ctl-status.is-stalled { border-left-color: var(--warning); background: var(--warning-bg); }
.ctl-status-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 8px; }
.ctl-status-title { font-weight: 600; font-size: 13px; color: var(--text-primary); display: inline-flex; align-items: center; gap: 8px; }
.ctl-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--success); display: inline-block; flex-shrink: 0; }
.ctl-status.is-stalled .ctl-dot { background: var(--warning); }
.ctl-status-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 16px; }
.ctl-status-item { background: var(--bg-subtle); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 8px 12px; font-size: 12px; color: var(--text-secondary); }
.ctl-status.is-stalled .ctl-status-item { background: rgba(255,255,255,.65); }
.ctl-status-item.is-full { grid-column: 1 / -1; }
.ctl-k { display: block; font-size: 11px; color: var(--text-tertiary); font-weight: 600; letter-spacing: .3px; margin-bottom: 2px; }
.ctl-status-item strong { color: var(--text-primary); font-size: 12.5px; }
.ctl-note { font-size: 12px; color: var(--text-secondary); line-height: 1.6; }
.ctl-section-title { font-weight: 600; font-size: 13px; color: var(--text-primary); margin-bottom: 4px; display: flex; align-items: center; gap: 8px; }
.ctl-count { font-size: 11px; color: var(--text-secondary); font-weight: 600; background: var(--bg-subtle); border: 1px solid var(--border-subtle); padding: 1px 8px; border-radius: 999px; }
.ctl-sub { font-size: 12px; color: var(--text-secondary); margin-bottom: 8px; line-height: 1.5; }
.ctl-actions { display: grid; gap: 8px; }
.ctl-card { background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: var(--radius-md); padding: 12px 16px; box-shadow: var(--shadow-sm); }
.ctl-card.is-recommended { border-color: rgba(94,106,210,.45); box-shadow: 0 0 0 1px rgba(94,106,210,.15), var(--shadow-sm); }
.ctl-card-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }
.ctl-index { font-size: 11px; font-weight: 700; color: var(--primary); background: var(--primary-subtle); border: 1px solid rgba(94,106,210,.25); border-radius: 6px; padding: 1px 8px; white-space: nowrap; }
.ctl-card-title { font-size: 13px; font-weight: 600; color: var(--text-primary); flex: 1; min-width: 140px; }
.ctl-badges { display: inline-flex; gap: 8px; align-items: center; }
.ctl-rec { font-size: 11px; font-weight: 700; color: #ffffff; background: var(--primary); border-radius: 999px; padding: 2px 8px; }
.ctl-card-desc { font-size: 12px; color: var(--text-secondary); margin-bottom: 8px; line-height: 1.6; }
.ctl-cmd { background: var(--bg-subtle); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 8px 12px; margin-bottom: 8px; }
.ctl-cmd-label { display: flex; justify-content: space-between; align-items: center; font-size: 11px; font-weight: 600; color: var(--text-tertiary); letter-spacing: .3px; margin-bottom: 8px; }
.ctl-cmd code { display: block; background: #ffffff; border: 1px solid var(--border-default); border-radius: 6px; padding: 8px; font-size: 11.5px; line-height: 1.6; color: var(--text-primary); white-space: pre-wrap; word-break: break-all; user-select: all; }
.ctl-card-foot { display: flex; justify-content: flex-end; gap: 8px; align-items: center; flex-wrap: wrap; }
.ctl-empty { background: var(--success-bg); border: 1px solid rgba(22,163,74,.25); border-radius: var(--radius-md); padding: 12px 16px; font-size: 12.5px; color: var(--text-primary); line-height: 1.6; }
.ctl-empty .muted { margin-top: 4px; }
.ctl-cheatsheet { background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: var(--radius-md); padding: 12px 16px; box-shadow: var(--shadow-sm); }
.ctl-cheat-list { display: grid; }
.ctl-cheat-row { display: grid; grid-template-columns: 24px minmax(0,1fr); gap: 8px; padding: 12px 0; border-top: 1px solid var(--border-subtle); }
.ctl-cheat-row:first-of-type { border-top: 0; padding-top: 8px; }
.ctl-cheat-num { width: 20px; height: 20px; border-radius: 6px; background: var(--bg-subtle); border: 1px solid var(--border-default); color: var(--text-secondary); font-size: 11px; font-weight: 700; display: inline-flex; align-items: center; justify-content: center; margin-top: 1px; }
.ctl-cheat-name { font-size: 12.5px; font-weight: 600; color: var(--text-primary); display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 8px; }
.ctl-cheat-cmd { background: var(--bg-subtle); border: 1px solid var(--border-subtle); border-radius: 6px; padding: 8px; }
.ctl-cheat-cmd code { display: block; background: #ffffff; border: 1px solid var(--border-default); border-radius: 6px; padding: 8px; white-space: pre-wrap; word-break: break-all; line-height: 1.6; user-select: all; }
.ctl-cheat-desc { font-size: 12px; color: var(--text-secondary); margin-top: 8px; line-height: 1.5; }
.ctl-mini-card { margin-top: 8px; padding: 8px 12px; background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: var(--radius-sm); }
.ctl-mini-card.is-recommended { border-color: rgba(94,106,210,.4); }
.ctl-mini-cmd { display: flex; align-items: flex-start; gap: 8px; background: var(--bg-subtle); padding: 8px; border-radius: 6px; border: 1px solid var(--border-subtle); margin: 8px 0; }
.ctl-mini-cmd code { flex: 1; background: #fff; border: 1px solid var(--border-default); border-radius: 4px; padding: 4px 8px; font-size: 11px; white-space: pre-wrap; word-break: break-all; line-height: 1.5; user-select: all; }
.ctl-pre-light { white-space: pre-wrap; max-height: 160px; overflow: auto; background: var(--bg-subtle); color: var(--text-primary); padding: 8px 12px; border-radius: 8px; margin-top: 8px; border: 1px solid var(--border-subtle); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; line-height: 1.5; }
/* Task-bound unblocking (end-user oriented, buttons first) */
.ctl-blocker { background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: var(--radius-md); padding: 12px 16px; box-shadow: var(--shadow-sm); }
.ctl-blocker-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }
.ctl-blocker-id { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; color: var(--text-secondary); margin-top: 2px; word-break: break-all; }
.ctl-blocker-title { font-size: 13px; font-weight: 600; color: var(--text-primary); }
.ctl-blocker-reason { font-size: 12px; color: var(--text-secondary); background: var(--bg-subtle); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 8px; margin: 8px 0 8px; line-height: 1.6; }
.ctl-effect { font-size: 12px; color: var(--text-primary); background: var(--primary-subtle); border: 1px solid rgba(94,106,210,.25); border-radius: var(--radius-sm); padding: 8px; margin-bottom: 8px; line-height: 1.6; }
.ctl-card.is-destructive .ctl-effect { background: var(--danger-bg); border-color: rgba(220,38,38,.25); }
.ctl-tech { margin-bottom: 8px; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-surface); }
.ctl-tech summary { cursor: pointer; font-size: 11.5px; color: var(--text-tertiary); padding: 8px 12px; user-select: none; }
.ctl-tech summary:hover { color: var(--text-secondary); }
.ctl-tech-body { padding: 0 12px 12px; }
.ctl-meta-chips { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.ctl-chip { font-size: 11px; color: var(--text-secondary); background: var(--bg-subtle); border: 1px solid var(--border-subtle); border-radius: 999px; padding: 1px 8px; }
.ctl-chip strong { color: var(--text-primary); font-weight: 600; }
/* Human decision asks: must read as "waiting on you", not as another log line */
.ctl-decision { background: var(--warning-bg); border: 1px solid rgba(217,119,6,.3); border-left: 3px solid var(--warning); border-radius: var(--radius-md); padding: 12px 16px; margin-bottom: 8px; }
.ctl-decision.is-resolved { background: var(--success-bg); border-color: rgba(22,163,74,.3); border-left-color: var(--success); }
.ctl-dec-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.ctl-dec-id { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; font-weight: 600; color: var(--warning); }
.ctl-dec-q { font-size: 13px; color: var(--text-primary); line-height: 1.6; margin-bottom: 8px; }
.ctl-dec-opts { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.ctl-dec-opt { font-size: 11.5px; color: var(--text-secondary); background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: var(--radius-sm); padding: 3px 8px; }
.ctl-dec-opt.is-rec { color: var(--text-primary); border-color: rgba(217,119,6,.5); font-weight: 600; }
.ctl-dec-advice { background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: var(--radius-md); padding: 8px 12px; margin-bottom: 8px; }
.ctl-dec-advice.is-stale { opacity: .6; }
.ctl-dec-advice-h { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; }
.ctl-dec-advice-kind { font-size: 10.5px; font-weight: 600; text-transform: uppercase; letter-spacing: .4px; color: var(--text-tertiary); border: 1px solid var(--border-subtle); border-radius: 999px; padding: 0 8px; }
.ctl-dec-advice-t { font-size: 12.5px; font-weight: 600; color: var(--text-primary); }
.ctl-dec-advice-s { font-size: 12px; color: var(--text-secondary); line-height: 1.6; }

@media (max-width: 1000px) {
  .shell { grid-template-columns: 1fr; }
  .sidebar { position: static; height: auto; border-right: 0; border-bottom: 1px solid var(--border-default); }
  .deep-drawer { left: 0; }
  .decision-item { grid-template-columns: 1fr; gap: 2px; }
  .task-list .task { grid-template-columns: 20px 1fr auto 28px; }
  .task-agent { display: none; }
  .ctl-status-grid { grid-template-columns: 1fr; }
  .ctl-card-foot { justify-content: stretch; }
  .ctl-card-foot .btn { flex: 1; justify-content: center; }
}
/* Flow Workbench v1: read-mostly runtime canvas + inspector */
.flow-view-toggle { display: inline-flex; background: var(--bg-subtle); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 2px; gap: 2px; }
.flow-view-toggle .filter-btn.active { background: var(--bg-surface); }
#flowSummary { font-size: 12px; color: var(--text-secondary); padding: 8px 16px; border-bottom: 1px solid var(--border-subtle); display: none; }
#flowWrap { display: flex; gap: 0; min-height: 420px; background: var(--bg-surface); }
#flowCanvasWrap { position: relative; flex: 1; min-width: 0; min-height: 420px; background: var(--bg-surface); }
#flowCanvas { width: 100%; height: 480px; background: var(--bg-surface); }
#flowCanvas .x6-graph-svg { background: var(--bg-surface); }
.flow-controls { position: absolute; top: 10px; right: 10px; display: flex; gap: 8px; z-index: 5; }
.flow-controls .btn { padding: 4px 12px; font-size: 12px; }
#flowError { display: none; margin: 12px 16px; padding: 12px; background: var(--warning-bg); border: 1px solid rgba(217,119,6,.3); border-radius: var(--radius-sm); font-size: 12px; color: var(--text-primary); }
#flowInspector { width: 340px; flex-shrink: 0; border-left: 1px solid var(--border-default); background: var(--bg-surface); display: flex; flex-direction: column; min-height: 420px; max-height: 640px; }
.flow-insp-head { padding: 12px 16px 8px; border-bottom: 1px solid var(--border-subtle); }
.flow-insp-title { font-size: 14px; font-weight: 600; color: var(--text-primary); }
.flow-insp-meta { font-size: 11.5px; color: var(--text-secondary); margin-top: 2px; }
.flow-insp-tabs { display: flex; gap: 2px; padding: 8px 12px 0; border-bottom: 1px solid var(--border-subtle); }
.flow-insp-tab { background: transparent; border: none; border-bottom: 2px solid transparent; padding: 8px 12px; font-size: 12px; color: var(--text-secondary); cursor: pointer; }
.flow-insp-tab.active { color: var(--text-primary); font-weight: 600; border-bottom-color: var(--primary); }
#flowInspectorBody { padding: 12px 16px; overflow-y: auto; font-size: 12.5px; line-height: 1.6; }
.flow-kv { display: grid; grid-template-columns: 72px 1fr; gap: 4px 8px; margin-bottom: 8px; }
.flow-kv dt { color: var(--text-tertiary); font-size: 11.5px; }
.flow-kv dd { margin: 0; color: var(--text-primary); word-break: break-word; }
.flow-node-card { border: 1px solid var(--border-default); border-radius: 8px; background: #fff; padding: 8px 12px; }
.flow-node-card.is-working { border-color: var(--primary); }
.flow-node-card.is-blocked, .flow-node-card.is-failed { border-color: var(--danger); }
.flow-node-card.is-rework { border-color: var(--warning); }
.flow-node-card.is-completed { border-color: rgba(22,163,74,.4); }
.flow-task-row { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 8px 0; border-bottom: 1px solid var(--border-subtle); cursor: pointer; }
.flow-task-row:hover { background: var(--bg-hover); }
@media (max-width: 1280px) { #flowInspector { width: 300px; } }
@media (max-width: 1000px) { #flowWrap { flex-direction: column; } #flowInspector { width: 100%; border-left: none; border-top: 1px solid var(--border-default); max-height: none; } }
/* Console shell v1 — approved haflow-flow-canvas-proposal.html */
body {
  background: #fff;
  padding: 0;
  font-family: "PingFang SC", "Hiragino Sans GB", "Noto Sans SC", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
.shell {
  grid-template-columns: 48px 220px minmax(0, 1fr);
  height: 100vh;
  min-height: 0;
  background: #fff;
  border: 0;
  border-radius: 0;
  box-shadow: none;
  overflow: hidden;
}
.left-rail {
  width: 48px;
  background: #ffffff;
  border-right: 1px solid #e6e8ee;
  display: flex;
  flex-direction: column;
  align-items: center;
  flex-shrink: 0;
  z-index: 25;
  height: 100vh;
  box-sizing: border-box;
}
.rail-head {
  height: 44px;
  width: 48px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-bottom: 1px solid #e6e8ee;
  flex-shrink: 0;
  box-sizing: border-box;
}
.rail-brand {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: #16171b;
  color: #ffffff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 800;
  font-size: 13px;
  cursor: pointer;
  user-select: none;
}
.rail-nav {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 8px 0;
}
.rail-item {
  width: 32px;
  height: 32px;
  margin: 4px 8px;
  border-radius: 6px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #5e636e;
  background: transparent;
  border: 0;
  cursor: pointer;
  position: relative;
  transition: all 0.12s ease;
  padding: 0;
}
.rail-item:hover {
  background: #f4f5f8;
  color: #16171b;
}
.rail-item.active, .rail-item.on {
  background: #f0f2ff;
  color: #5e6ad2;
}
.rail-item.active::before, .rail-item.on::before {
  content: "";
  position: absolute;
  left: -8px;
  top: 8px;
  bottom: 8px;
  width: 3px;
  background: #5e6ad2;
  border-radius: 0 2px 2px 0;
}
.rail-badge-dot {
  position: absolute;
  top: 8px;
  right: 8px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #ef4444;
}
.rail-foot {
  padding: 8px 0 12px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}
.rail-avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: #ece7e2;
  color: #6b4f3a;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: 12px;
  cursor: pointer;
  border: 1px solid #e6e8ee;
  user-select: none;
}
.shell.sidebar-collapsed {
  grid-template-columns: 48px 0 minmax(0, 1fr);
}
.sidebar {
  position: relative;
  top: auto;
  height: 100vh;
  overflow: hidden;
  background: #fafafa;
  border-right: 1px solid #e6e8ee;
  padding: 0;
  display: flex;
  flex-direction: column;
  width: 220px;
  flex-shrink: 0;
  transition: width 0.15s ease;
  z-index: 20;
  white-space: nowrap;
  box-sizing: border-box;
}
.sidebar.collapsed {
  width: 0;
  border-right: none;
  visibility: hidden;
}
.brand-row {
  height: 44px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 12px;
  border-bottom: 1px solid #e6e8ee;
  box-sizing: border-box;
  flex: none;
  background: #fafafa;
}
.brand {
  display: none;
}
.sidebar-head {
  height: 44px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 12px;
  border-bottom: 1px solid #e6e8ee;
  box-sizing: border-box;
  flex: none;
  background: #fafafa;
}
.sidebar-title {
  font-weight: 600;
  font-size: 12px;
  color: #16171b;
}
.btn-collapse-sidebar {
  margin-left: auto;
  width: 24px;
  height: 24px;
  border-radius: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #8b909a;
  background: transparent;
  border: 0;
  cursor: pointer;
  padding: 0;
  transition: all 0.12s ease;
}
.btn-collapse-sidebar:hover { background: #f0f1f4; color: #16171b; }
.btn-expand-sidebar {
  width: 28px;
  height: 28px;
  margin-right: 4px;
  border-radius: 4px;
  display: none;
  align-items: center;
  justify-content: center;
  color: #5e636e;
  background: transparent;
  border: 0;
  cursor: pointer;
  padding: 0;
  flex-shrink: 0;
}
.btn-expand-sidebar:hover { background: #f0f1f4; color: #16171b; }
.btn-expand-sidebar.visible { display: flex; }
.sidebar-scroll {
  padding: 12px 8px;
  overflow-y: auto;
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 16px;
  box-sizing: border-box;
}
.sidebar-zone-top {
  display: flex;
  flex-direction: column;
  gap: 8px;
  position: relative;
}
.space-pill {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 8px;
  background: #ffffff;
  border: 1px solid #e6e8ee;
  border-radius: 8px;
  box-shadow: 0 1px 2px rgba(18, 19, 22, 0.04);
  cursor: pointer;
  text-align: left;
  color: inherit;
  width: 100%;
  box-sizing: border-box;
}
.space-avatar {
  width: 24px;
  height: 24px;
  border-radius: 4px;
  background: #ece7e2;
  color: #6b4f3a;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 700;
  flex-shrink: 0;
}
.space-name { font-size: 12px; font-weight: 600; color: #16171b; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.space-subtext { font-size: 10px; color: #8b909a; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.space-chev { margin-left: auto; color: #8b909a; flex-shrink: 0; }
.space-menu {
  position: absolute;
  z-index: 30;
  left: 0;
  right: 0;
  top: calc(100% + 4px);
  background: #fff;
  border: 1px solid #e4e6ec;
  border-radius: 12px;
  box-shadow: 0 12px 32px rgba(18, 19, 22, 0.12);
  padding: 8px;
  max-height: 320px;
  overflow: auto;
}
.space-menu[hidden] { display: none; }
.space-new {
  width: 100%;
  margin-top: 4px;
  border: 0;
  background: transparent;
  text-align: left;
  padding: 8px;
  border-radius: 8px;
  color: #5e6ad2;
  font-weight: 650;
  font-size: 12.5px;
  cursor: pointer;
}
.space-new:hover { background: #f4f6fb; }
.btn-primary-launch {
  height: 32px;
  background: #5e6ad2;
  border-radius: 6px;
  border: 0;
  color: #ffffff;
  font-size: 12px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  cursor: pointer;
  transition: all 0.12s ease;
  box-shadow: 0 1px 2px rgba(94, 106, 210, 0.2);
  width: 100%;
  padding: 0 12px;
  box-sizing: border-box;
}
.btn-primary-launch:hover { background: #4f5bc4; }
.sidebar-section { display: flex; flex-direction: column; gap: 4px; }
.sidebar-section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 8px;
  font-size: 11px;
  font-weight: 600;
  color: #8b909a;
  letter-spacing: 0.04em;
}
.sidebar-subgroup-title {
  font-size: 11px;
  font-weight: 600;
  color: #5e636e;
  padding: 4px 8px 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.sidebar-subgroup-title.collapsible {
  cursor: pointer;
  user-select: none;
  border-radius: 4px;
}
.sidebar-subgroup-title.collapsible:hover {
  color: #16171b;
  background: #f3f4f6;
}
.sidebar-subgroup-left {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.sidebar-subgroup-arrow {
  display: inline-block;
  font-size: 9px;
  color: #8b909a;
  transition: transform 0.15s ease;
  line-height: 1;
}
.sidebar-subgroup-title.collapsed .sidebar-subgroup-arrow {
  transform: rotate(-90deg);
}
.subgroup-badge {
  font-size: 10px;
  padding: 0 4px;
  border-radius: 4px;
  font-weight: 600;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.subgroup-badge.warning { background: #fffbeb; color: #b45309; border: 1px solid #fde68a; }
.subgroup-badge.running { background: #ecfdf5; color: #047857; border: 1px solid #a7f3d0; }
.subgroup-badge.completed { background: #f3f4f6; color: #6b7280; border: 1px solid #e5e7eb; }
.sidebar-completed-toggle {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  padding: 4px 8px;
  margin-top: 4px;
  font-size: 11px;
  color: #5e6ad2;
  background: transparent;
  border: 1px dashed #d5d9eb;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.15s ease;
  box-sizing: border-box;
}
.sidebar-completed-toggle:hover {
  background: #f0f2ff;
  border-color: #5e6ad2;
}
.sidebar-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 8px;
  border-radius: 6px;
  font-size: 12px;
  color: #5e636e;
  cursor: pointer;
  transition: all 0.12s ease;
  position: relative;
  background: transparent;
  border: 0;
  width: 100%;
  text-align: left;
  box-sizing: border-box;
}
.sidebar-item:hover { background: #f3f4f6; color: #16171b; }
.sidebar-item.active, .sidebar-item.on { background: #f0f2ff; color: #5e6ad2; font-weight: 600; }
.sidebar-item.highlight-governance {
  background: #ffffff;
  border: 1px solid #e6e8ee;
  box-shadow: 0 1px 2px rgba(18, 19, 22, 0.04);
}
.sidebar-item.highlight-governance:hover {
  border-color: #5e6ad2;
  background: #f0f2ff;
  color: #5e6ad2;
}
.sidebar-item.highlight-governance.active, .sidebar-item.highlight-governance.on {
  background: #f0f2ff;
  border-color: #5e6ad2;
  color: #5e6ad2;
  font-weight: 600;
}
.sidebar-item.danger-item {
  color: #ef4444;
}
.sidebar-item.danger-item:hover {
  background: #fef2f2;
  color: #b91c1c;
}
.item-icon { font-size: 14px; flex-shrink: 0; display: inline-flex; align-items: center; justify-content: center; }
.item-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
#sidebarWorkflowGroups .sidebar-item {
  display: grid;
  grid-template-columns: 8px minmax(0, 1fr) auto;
  align-items: start;
}
#sidebarWorkflowGroups .status-dot { margin-top: 4px; }
#sidebarWorkflowGroups .item-text {
  white-space: normal;
  overflow-wrap: anywhere;
  overflow: visible;
  text-overflow: clip;
}
#sidebarWorkflowGroups .item-meta { white-space: nowrap; }
.warning-text { color: #b45309; font-weight: 600; }
.danger-text { color: #ef4444; font-weight: 500; }
.online-pill {
  font-size: 9px;
  background: #ecfdf5;
  color: #047857;
  padding: 0 4px;
  border-radius: 4px;
  margin-left: 4px;
  border: 1px solid #a7f3d0;
}
.resource-title { font-size: 11.5px; font-weight: 600; color: #16171b; }
.resource-sub { font-size: 10px; color: #8b909a; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pulse-indicator {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #10b981;
  box-shadow: 0 0 0 2px #ecfdf5;
  margin-left: auto;
  flex-shrink: 0;
  animation: pulseLive 2s cubic-bezier(0.4, 0, 0.6, 1) infinite;
}
@keyframes pulseLive {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: .5; transform: scale(0.9); }
}
.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.status-dot.running { background: #10b981; box-shadow: 0 0 0 2px #ecfdf5; }
.status-dot.decision { background: #f59e0b; box-shadow: 0 0 0 2px #fffbeb; }
.status-dot.blocked { background: #ef4444; box-shadow: 0 0 0 2px #fef2f2; }
.status-dot.completed { background: #9ca3af; }
.item-meta { margin-left: auto; font-size: 10px; color: #8b909a; flex-shrink: 0; }
.side-foot { margin-top: auto; padding: 12px 8px 4px; border-top: 1px solid #e6e8ee; }
.side-foot b { display: block; font-size: 11px; font-weight: 650; }
.side-foot span { display: block; margin-top: 4px; font-size: 10px; color: #8b909a; line-height: 1.4; }
.nav-count {
  margin-left: auto;
  min-width: 18px;
  text-align: center;
  font-size: 11px;
  color: #6d727c;
  background: #f0f1f4;
  border-radius: 999px;
  padding: 0 8px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-variant-numeric: tabular-nums;
}
.nav-count[hidden] { display: none; }
.main {
  padding: 0;
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: #fff;
  overflow: hidden;
}
.top {
  height: 44px;
  flex: none;
  margin: 0;
  padding: 0 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: nowrap;
  border-bottom: 1px solid #e6e8ee;
  background: #fff;
  box-sizing: border-box;
}
.workflow-tabs-bar {
  height: 44px;
  display: flex;
  align-items: center;
  gap: 4px;
  flex: 1;
  min-width: 0;
  overflow: hidden;
}
.tabs-scroll {
  display: flex;
  align-items: center;
  gap: 4px;
  flex: 1;
  min-width: 0;
  overflow-x: auto;
  overflow-y: hidden;
  height: 44px;
}
.tabs-scroll::-webkit-scrollbar { display: none; }
.wf-tab {
  height: 32px;
  padding: 0 8px;
  border-radius: 6px;
  display: flex;
  align-items: center;
  gap: 8px;
  background: transparent;
  color: #5e636e;
  font-size: 12px;
  cursor: pointer;
  position: relative;
  flex-shrink: 0;
  max-width: 220px;
  transition: all 0.12s ease;
  border: 1px solid transparent;
  user-select: none;
}
.wf-tab:hover {
  background: #f3f4f6;
  color: #16171b;
}
.wf-tab.active {
  background: #fff;
  color: #16171b;
  font-weight: 600;
  border-color: #e6e8ee;
  box-shadow: 0 1px 2px rgba(18, 19, 22, 0.04);
}
.wf-tab.active::after {
  content: "";
  position: absolute;
  bottom: -6px;
  left: 8px;
  right: 8px;
  height: 2px;
  background: #5e6ad2;
  border-radius: 2px;
}
.tab-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.tab-dot.running { background: #10b981; box-shadow: 0 0 0 2px #ecfdf5; }
.tab-dot.decision { background: #f59e0b; box-shadow: 0 0 0 2px #fffbeb; }
.tab-dot.blocked { background: #ef4444; box-shadow: 0 0 0 2px #fef2f2; }
.tab-dot.completed { background: #9ca3af; }
.tab-dot.waiting { background: #d1d5db; }
.tab-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.tab-close {
  width: 16px;
  height: 16px;
  border-radius: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #8b909a;
  opacity: 0.6;
  transition: all 0.1s ease;
  margin-left: 4px;
  border: 0;
  background: transparent;
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
  padding: 0;
}
.tab-close:hover {
  background: #e4e6ec;
  color: #16171b;
  opacity: 1;
}
.btn-new-tab {
  width: 24px;
  height: 24px;
  border-radius: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #8b909a;
  border: 1px dashed #e6e8ee;
  background: transparent;
  cursor: pointer;
  font-size: 14px;
  margin-left: 4px;
  flex-shrink: 0;
}
.btn-new-tab:hover {
  border-color: #5e6ad2;
  color: #5e6ad2;
  background: #f0f2ff;
}
.no-tabs {
  font-size: 12px;
  color: #8b909a;
  padding: 0 8px;
}
.crumb-line { font-size: 13px; color: #8b909a; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.space-name, .resource-sub, .trigger-title, .item-title, #sideFootTitle {
  white-space: normal;
  overflow-wrap: anywhere;
  overflow: visible;
  text-overflow: clip;
  min-width: 0;
}
.linear-trigger { height: auto; min-height: 32px; padding-top: 4px; padding-bottom: 4px; }
.trigger-content { white-space: normal; min-width: 0; flex-wrap: wrap; }
#workflowSubject.wf-subject { font-size: 14px; font-weight: 680; color: #16171b; }
#workflowSub { display: block; margin-top: 2px; font-size: 11.5px; color: #8b909a; }
.top .btn { height: 30px; border-radius: 8px; }
.top .btn.primary { background: #5e6ad2; border-color: #5e6ad2; color: #fff; font-weight: 650; }
.top .btn.primary:hover { background: #4f5bc4; border-color: #4f5bc4; color: #fff; }
.top .btn.icon-only { width: 30px; padding: 0; justify-content: center; }
#opsButton, #dashButton, .metrics { display: none !important; }
.visually-hidden {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0;
}
.shell[data-view="workbench"] #stages,
.shell[data-view="workbench"] #attentionBanner,
.shell[data-view="workbench"] #workflowSwitcher { display: none !important; }
.shell[data-view="dashboard"] #workflowSwitcher { margin: 12px 16px 0; }
.workspace-layout { flex: 1; min-height: 0; gap: 0; position: relative; }
.panel.main-panel {
  flex: 1; min-height: 0; display: flex; flex-direction: column;
  border: 0; border-radius: 0; background: transparent; overflow: hidden;
}
#canvasToolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  z-index: 4;
  pointer-events: none;
}
#canvasToolbar > * { pointer-events: auto; }
.shell[data-workspace="flow"] #canvasToolbar {
  position: absolute; top: 12px; left: 16px; right: 316px;
}
.shell[data-workspace="list"] #canvasToolbar { position: relative; margin: 12px 16px 0; }
.shell[data-workspace="aux"] #canvasToolbar { display: none !important; }
.toolbar-left, .canvas-tools { display: flex; align-items: center; gap: 8px; min-width: 0; }
.canvas-tools { margin-left: auto; }
.flow-view-toggle {
  background: #fff;
  border: 1px solid #e6e8ee;
  border-radius: 8px;
  padding: 2px;
  box-shadow: 0 1px 2px rgba(18,19,22,.04);
}
.flow-view-toggle .filter-btn { padding: 4px 8px; border-radius: 6px; }
.flow-view-toggle .filter-btn.active { background: #f6eee8; color: #16171b; font-weight: 650; box-shadow: none; }
.canvas-wf-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  max-width: 480px;
  background: rgba(255, 255, 255, 0.94);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  border: 1px solid #e6e8ee;
  border-radius: 8px;
  padding: 4px 8px;
  box-shadow: 0 1px 2px rgba(18, 19, 22, 0.04);
}
.canvas-wf-title-wrap {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
}
.canvas-wf-title {
  font-size: 13px;
  font-weight: 650;
  color: #16171b;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 240px;
  cursor: pointer;
  line-height: 20px;
}
.canvas-wf-title:hover {
  color: #c96442;
}
.canvas-wf-edit-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  padding: 0;
  border: 0;
  background: transparent;
  color: #8c919d;
  border-radius: 4px;
  cursor: pointer;
  opacity: 0.7;
  transition: opacity 0.15s, color 0.15s, background 0.15s;
}
.canvas-wf-edit-btn:hover {
  opacity: 1;
  color: #16171b;
  background: #f0f2f5;
}
.canvas-wf-id-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: #f7f8fa;
  border: 1px solid #e6e8ee;
  border-radius: 6px;
  padding: 0 8px;
  height: 20px;
  font-size: 11px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-variant-numeric: tabular-nums;
  color: #5e636e;
  cursor: pointer;
  user-select: none;
  transition: all 0.15s ease;
  white-space: nowrap;
}
.canvas-wf-id-pill:hover {
  background: #fff;
  border-color: #c96442;
  color: #c96442;
}
.canvas-wf-id-pill svg {
  width: 12px;
  height: 12px;
  flex-shrink: 0;
}
#flowSummary {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  border: 1px solid #e6e8ee;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.94);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  padding: 4px 12px;
  font-size: 12px;
  color: #5e636e;
  white-space: nowrap;
  box-shadow: 0 1px 2px rgba(18, 19, 22, 0.04);
}
#flowSummary b {
  color: #16171b;
  font-weight: 680;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-variant-numeric: tabular-nums;
}
.flow-status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #10b981;
  display: inline-block;
  flex-shrink: 0;
  box-shadow: 0 0 0 2px rgba(16, 185, 129, 0.2);
}
.shell[data-workspace="list"] #flowSummary,
.shell[data-workspace="list"] .flow-controls { display: none !important; }
.flow-controls { position: static; gap: 0; background: #fff; border: 1px solid #e6e8ee; border-radius: 8px; overflow: hidden; }
.flow-controls .btn { padding: 4px 8px; border: 0; border-radius: 0; border-left: 1px solid #e6e8ee; box-shadow: none; background: #fff; }
.flow-controls .btn:first-child { border-left: 0; }
#flowWrap { flex: 1; min-height: 0; background: transparent; }
#flowCanvasWrap {
  min-height: 0;
  display: flex;
  flex-direction: column;
  background-color: #f7f8fa;
  background-image: radial-gradient(rgba(18,19,22,.13) 1px, transparent 1.15px);
  background-size: 18px 18px;
  padding-top: 52px;
}
#flowCanvas { flex: 1; height: auto; min-height: 0; width: 100% !important; max-width: 100%; background: transparent; }
#flowCanvas .x6-graph-svg, #flowCanvas .x6-graph { background: transparent !important; }
#flowCanvas .x6-node text { display: none; }
#flowInspector {
  width: 300px;
  max-height: none;
  min-height: 0;
  height: auto;
  border-left: 1px solid #e6e8ee;
  background: #fff;
}
.flow-insp-head {
  height: 44px;
  padding: 0 16px;
  border-bottom: 1px solid #e6e8ee;
  display: flex;
  align-items: center;
  justify-content: space-between;
  box-sizing: border-box;
}
.flow-insp-title { font-size: 14px; font-weight: 680; }
.flow-insp-meta { font-size: 11px; color: #5e636e; }
.flow-insp-tabs { padding: 0 8px; gap: 2px; }
.flow-insp-tab { padding: 8px; font-size: 12.5px; border-bottom: 0; }
.flow-insp-tab.active { color: #16171b; font-weight: 680; box-shadow: inset 0 -2px 0 #5e6ad2; }
#flowInspectorBody { flex: 1; }
#flowCanvas .flow-node-card {
  box-sizing: border-box;
  width: 232px;
  height: 134px;
  margin: 0;
  background: #fff;
  border: 1px solid #e5e7ee;
  border-radius: 12px;
  padding: 12px;
  box-shadow: 0 1px 2px rgba(18,19,22,.04), 0 8px 20px rgba(18,19,22,.05);
  cursor: pointer;
  overflow: visible;
  position: relative;
  font-family: "PingFang SC", "Hiragino Sans GB", "Noto Sans SC", -apple-system, sans-serif;
}
#flowCanvas .node-anchor {
  position: absolute;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #ffffff;
  border: 2px solid #cbd5e1;
  pointer-events: none;
  z-index: 5;
  box-sizing: border-box;
  transition: all 0.2s ease;
}
#flowCanvas .node-anchor.left {
  left: -5px;
  top: 50%;
  transform: translateY(-50%);
}
#flowCanvas .node-anchor.right {
  right: -5px;
  top: 50%;
  transform: translateY(-50%);
}
#flowCanvas .node-anchor.inbound-active {
  border-color: #6366f1;
  background: #ffffff;
  box-shadow: 0 0 0 2px rgba(99, 102, 241, 0.25), 0 0 8px rgba(99, 102, 241, 0.6);
}
#flowCanvas .node-anchor.outbound-active {
  border-color: #10b981;
  background: #ffffff;
  box-shadow: 0 0 0 2px rgba(16, 185, 129, 0.25), 0 0 8px rgba(16, 185, 129, 0.6);
}
#flowInspector .node-anchor,
.flow-attn .node-anchor {
  display: none !important;
}
@keyframes flowEdgeDash {
  0% { stroke-dashoffset: 20; }
  100% { stroke-dashoffset: 0; }
}
.flow-gesture-hint {
  color: #8c919d;
  font-size: 11px;
  margin-left: 8px;
  padding: 2px 8px;
  background: #f0f2f5;
  border-radius: 4px;
  font-weight: 500;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.flow-gesture-hint kbd {
  font-family: inherit;
  font-size: 10px;
  background: #fff;
  border: 1px solid #dcdfe6;
  border-radius: 3px;
  padding: 0 4px;
}
#flowInspector .flow-node-card,
.flow-attn .flow-node-card {
  width: auto;
  height: auto;
  margin: 0 0 8px;
  overflow: visible;
  cursor: default;
}
#flowCanvas .flow-node-card.is-working { border-color: #c9cef3; }
#flowCanvas .flow-node-card.is-working .fn-ico { background: #eef0fb; color: #5e6ad2; }
#flowCanvas .flow-node-card.is-working .fn-pill { background: #eef0fb; color: #5e6ad2; }
#flowCanvas .flow-node-card.is-blocked,
#flowCanvas .flow-node-card.is-failed { border-color: #f0b4b4; }
#flowCanvas .flow-node-card.is-blocked .fn-pill,
#flowCanvas .flow-node-card.is-failed .fn-pill { background: #dc2626; color: #fff; }
#flowCanvas .flow-node-card.is-rework { border-color: #f3d19a; }
#flowCanvas .flow-node-card.is-rework .fn-pill { background: #fffbeb; color: #d97706; }
#flowCanvas .flow-node-card.is-completed .fn-pill { background: #f0fdf4; color: #157a3a; }
#flowCanvas .flow-node-card.is-selected { border-color: #5e6ad2; box-shadow: 0 0 0 3px rgba(94,106,210,.16), 0 10px 24px rgba(94,106,210,.12); }
.fn-top { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.fn-ico { width: 20px; height: 20px; border-radius: 6px; background: #f3f4f6; color: #6d727c; display: grid; place-items: center; flex: none; }
.fn-kind { font-size: 10px; letter-spacing: .07em; font-weight: 750; color: #8b909a; }
.fn-pill { margin-left: auto; font-size: 11px; font-weight: 650; border-radius: 999px; padding: 2px 8px; background: #f3f4f6; color: #727782; white-space: nowrap; }
.fn-title { font-size: 14px; font-weight: 680; letter-spacing: -.01em; color: #16171b; line-height: 1.3; }
.fn-purpose { margin-top: 3px; font-size: 12px; color: #5e636e; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fn-foot { margin-top: 8px; font-size: 11.5px; color: #8b909a; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.flow-reads { background: #f4f6fb; border: 1px solid #e4e8f4; border-radius: 10px; padding: 12px; font-size: 12.5px; line-height: 1.55; color: #2c3142; }
.flow-reads em { font-style: normal; display: block; margin-bottom: 4px; font-size: 11px; letter-spacing: .05em; font-weight: 700; color: #8b909a; }
.flow-sec { margin: 16px 0 8px; font-size: 11px; letter-spacing: .05em; color: #8b909a; font-weight: 700; }
.flow-check { display: flex; gap: 8px; padding: 8px 0; border-top: 1px solid #f3f4f6; font-size: 12.5px; }
.flow-check:first-of-type { border-top: 0; }
.flow-mark { width: 14px; flex: none; font-weight: 750; }
.flow-mark.ok { color: #157a3a; }
.flow-mark.wait { color: #8b909a; }
.flow-mark.bad { color: #dc2626; }
.flow-task-row b { display: block; font-size: 12px; font-weight: 650; max-width: 188px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.flow-task-row small { display: block; color: #8b909a; font-size: 11.5px; }
.flow-attn {
  margin-top: 8px;
  background: rgba(254, 252, 232, 0.95);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  border: 1px solid #fde047;
  border-radius: 8px;
  padding: 8px 12px;
  box-shadow: 0 1px 3px rgba(234, 179, 8, 0.08);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  font-size: 12px;
  color: #854d0e;
}
.flow-attn-content {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex: 1;
}
.flow-attn-tag {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: #fef08a;
  color: #713f12;
  font-weight: 600;
  font-size: 11px;
  padding: 0 8px;
  border-radius: 4px;
  height: 20px;
  flex-shrink: 0;
}
.flow-attn-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #713f12;
}
.flow-attn-action {
  flex-shrink: 0;
  background: #fff !important;
  color: #854d0e !important;
  border: 1px solid #fde047 !important;
  border-radius: 6px !important;
  padding: 4px 8px !important;
  font-size: 11.5px !important;
  font-weight: 600 !important;
  cursor: pointer;
  transition: all 0.15s ease;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
}
.flow-attn-action:hover {
  background: #fef08a !important;
  border-color: #eab308 !important;
  color: #713f12 !important;
}
.shell-page { padding: 4px 4px 28px; }
.shell-page h3 { margin: 8px 0 12px; font-size: 16px; font-weight: 680; }
.shell-row {
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  padding: 12px;
  margin: 0 0 8px;
  border: 1px solid #e6e8ee;
  border-radius: 10px;
  background: #fff;
  text-align: left;
}
.shell-row small { color: #8b909a; }
.shell[data-workspace="aux"] #tasks,
.shell[data-workspace="list"] #tasks { flex: 1; min-height: 0; overflow: auto; padding: 8px 16px 24px; }
.main {
  min-width: 0;
  min-height: 0;
  height: 100vh;
  display: flex;
  flex-direction: column;
  padding-bottom: 0;
  background: #fff;
  overflow: hidden;
}
.light-bottom-bar {
  height: 28px;
  background: #ffffff;
  border-top: 1px solid #e6e8ee;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px;
  font-size: 11px;
  color: #8b909a;
  z-index: 30;
  flex-shrink: 0;
  user-select: none;
  box-sizing: border-box;
}
.bottom-left, .bottom-right {
  display: flex;
  align-items: center;
  gap: 16px;
  min-width: 0;
}
.bottom-indicator {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  color: #16171b;
  flex-shrink: 0;
}
.indicator-pip {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #10b981;
  box-shadow: 0 0 0 2px #ecfdf5;
}
.bottom-sep {
  color: #dcdfe8;
  font-size: 12px;
}
.bottom-ticker {
  font-size: 11px;
  color: #5e636e;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bottom-stat {
  font-size: 11px;
  color: #8b909a;
  font-variant-numeric: tabular-nums;
}
.bottom-stat b {
  color: #16171b;
  font-weight: 600;
}
.btn-bottom-action {
  background: transparent;
  border: 1px solid #e6e8ee;
  border-radius: 4px;
  padding: 4px 8px;
  font-size: 11px;
  color: #5e6ad2;
  font-weight: 600;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 4px;
}
.btn-bottom-action:hover {
  background: #f0f2ff;
  border-color: #5e6ad2;
}
.deep-drawer { left: 268px; right: 0; bottom: 28px; border-radius: 0; }
.deep-drawer.collapsed { clip-path: inset(0 0 calc(100% - 38px) 0); }
@media (max-width: 1000px) {
  body { padding: 0; }
  .shell { height: 100vh; min-height: 0; border-radius: 0; grid-template-columns: 1fr; }
  .left-rail { display: none; }
  .sidebar { width: 100%; border-right: 0; border-bottom: 1px solid #e6e8ee; max-height: 220px; }
  .deep-drawer { left: 0; right: 0; bottom: 28px; border-radius: 0; }
  .deep-drawer.collapsed { clip-path: inset(0 0 calc(100% - 38px) 0); }
  .shell[data-workspace="flow"] #canvasToolbar {
    left: 16px;
    right: 16px;
    flex-wrap: wrap;
    row-gap: 8px;
    background: rgba(247,248,250,.94);
    border-radius: 8px;
    padding: 4px;
  }
  #flowCanvasWrap { min-height: 420px; flex: none; padding-top: 96px; }
  #flowWrap { flex-direction: column; }
  #flowInspector { width: 100%; max-height: none; border-left: none; border-top: 1px solid #e6e8ee; }
}
.controller-panel{flex:1;min-height:0;overflow-y:auto;padding:24px 24px 80px 24px;background:#ffffff;display:flex;flex-direction:column;gap:16px;}
.controller-panel[hidden]{display:none !important;}
.ctl-panel-header{display:flex;align-items:center;gap:8px;padding-bottom:16px;border-bottom:1px solid #e6e8ee;font-size:14px;font-weight:600;color:#16171b;}
.log-kind-selector{display:inline-flex;align-items:center;background:#f4f5f7;padding:4px;border-radius:6px;gap:4px;}
.log-kind-btn{border:none;background:transparent;padding:4px 8px;border-radius:4px;font-size:12px;color:#5f6368;cursor:pointer;}
.log-kind-btn:hover{background:#ffffff;color:#121316;}
.log-kind-btn.active{background:#ffffff;color:#5e6ad2;font-weight:600;box-shadow:0 1px 2px rgba(18,19,22,0.08);}
.log-stream-wrap{flex:1;min-height:360px;background:#f8f9fa;border:1px solid #e6e8ee;border-radius:8px;padding:12px 16px;display:flex;flex-direction:column;overflow:hidden;}
.log-content-pre{flex:1;overflow-y:auto;font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:12px;line-height:1.6;color:#121316;white-space:pre-wrap;word-break:break-all;margin:0;}
</style><style>

/* --- Obsidian Component Polish & Pixel-Perfect Line Alignment --- */
:root {
  --header-row-height: 48px;
  --line-color: #e2e8f0;
}
.left-rail {
  border-right: 1px solid var(--line-color) !important;
  background: #ffffff !important;
}
.rail-head {
  height: var(--header-row-height) !important;
  border-bottom: 1px solid var(--line-color) !important;
  box-sizing: border-box !important;
}
.sidebar {
  border-right: 1px solid var(--line-color) !important;
  background: #f8fafc !important;
  position: relative !important;
}
.sidebar-head, .sidebar-head.brand-row, .brand-row {
  height: var(--header-row-height) !important;
  min-height: var(--header-row-height) !important;
  max-height: var(--header-row-height) !important;
  border-bottom: 1px solid var(--line-color) !important;
  background: #ffffff !important;
  box-sizing: border-box !important;
  padding: 0 16px !important;
}
.sidebar-title {
  font-size: 13px !important;
  font-weight: 600 !important;
  letter-spacing: -0.01em !important;
  color: #0f172a !important;
}
.space-pill {
  padding: 8px 12px !important;
  background: #ffffff !important;
  border: 1px solid rgba(0, 0, 0, 0.08) !important;
  border-radius: 8px !important;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04) !important;
  transition: all 0.15s ease !important;
}
.space-pill:hover {
  border-color: rgba(0, 0, 0, 0.16) !important;
  box-shadow: 0 2px 4px rgba(0, 0, 0, 0.06) !important;
}
.space-avatar {
  width: 22px !important;
  height: 22px !important;
  border-radius: 6px !important;
  background: #eff6ff !important;
  color: #2563eb !important;
  font-weight: 600 !important;
  font-size: 11px !important;
}
.space-name {
  font-size: 12.5px !important;
  font-weight: 500 !important;
  color: #0f172a !important;
}
.sidebar-section-head {
  padding: 8px 8px 4px !important;
  font-size: 11px !important;
  font-weight: 600 !important;
  letter-spacing: 0.02em !important;
  color: #64748b !important;
}
.sidebar-section-head .tabular-nums {
  font-size: 11px !important;
  color: #94a3b8 !important;
  font-weight: 500 !important;
}
.sidebar-subgroup-title {
  padding: 4px 8px 2px !important;
  font-size: 11px !important;
  font-weight: 600 !important;
  color: #64748b !important;
}
.subgroup-badge {
  font-size: 10px !important;
  padding: 2px 8px !important;
  border-radius: 10px !important;
  font-weight: 500 !important;
}
.subgroup-badge.warning {
  background: #fef3c7 !important;
  color: #d97706 !important;
}
.subgroup-badge.running {
  background: #dbeafe !important;
  color: #2563eb !important;
}
.subgroup-badge.completed {
  background: #f1f5f9 !important;
  color: #64748b !important;
}
.sidebar-item {
  padding: 4px 8px !important;
  border-radius: 6px !important;
  font-size: 12.5px !important;
  color: #475569 !important;
  transition: all 0.15s ease !important;
  margin-bottom: 1px !important;
}
.sidebar-item:hover {
  background: #f1f5f9 !important;
  color: #0f172a !important;
}
.sidebar-item.active {
  background: #ffffff !important;
  color: #0f172a !important;
  font-weight: 600 !important;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08), 0 0 0 1px rgba(0, 0, 0, 0.04) !important;
}
.item-icon-svg {
  flex-shrink: 0 !important;
  margin-right: 8px !important;
  color: #64748b !important;
  transition: color 0.15s ease !important;
}
.sidebar-item:hover .item-icon-svg {
  color: #1e293b !important;
}
.sidebar-item.active .item-icon-svg {
  color: #2563eb !important;
}
.online-pill {
  font-size: 10px !important;
  padding: 2px 8px !important;
  border-radius: 10px !important;
  background: #ecfdf5 !important;
  color: #059669 !important;
  border: none !important;
  font-weight: 500 !important;
}
.resource-sub {
  font-size: 11px !important;
  color: #94a3b8 !important;
  margin-top: 1px !important;
}
.resource-title {
  font-size: 12px !important;
  font-weight: 500 !important;
  color: #1e293b !important;
}
.btn-primary-launch {
  height: 32px !important;
  background: #2563eb !important;
  border-radius: 6px !important;
  color: #ffffff !important;
  font-size: 12px !important;
  font-weight: 500 !important;
  box-shadow: 0 1px 2px rgba(37, 99, 235, 0.2), inset 0 1px 0 rgba(255, 255, 255, 0.2) !important;
  transition: all 0.15s ease !important;
}
.btn-primary-launch:hover {
  background: #1d4ed8 !important;
}
.top {
  height: var(--header-row-height) !important;
  min-height: var(--header-row-height) !important;
  max-height: var(--header-row-height) !important;
  border-bottom: 1px solid var(--line-color) !important;
  background: #ffffff !important;
  box-sizing: border-box !important;
  padding: 0 16px !important;
  margin: 0 !important;
}
.workflow-tabs-bar {
  height: var(--header-row-height) !important;
  border-bottom: none !important;
  background: transparent !important;
  padding: 0 !important;
}
.tabs-scroll {
  position: relative !important;
  padding: 4px !important;
  gap: 2px !important;
  background: rgba(0, 0, 0, 0.03) !important;
  border-radius: 8px !important;
  height: 38px !important;
  align-items: center !important;
  border: 1px solid rgba(0, 0, 0, 0.04) !important;
}
.wf-tab-sliding-pill {
  position: absolute !important;
  top: 4px !important;
  bottom: 4px !important;
  background: #ffffff !important;
  border-radius: 6px !important;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08), 0 0 0 1px rgba(0, 0, 0, 0.04) !important;
  transition: transform 0.22s cubic-bezier(0.25, 1, 0.5, 1), width 0.22s cubic-bezier(0.25, 1, 0.5, 1) !important;
  pointer-events: none !important;
  z-index: 1 !important;
}
.wf-tab {
  height: 30px !important;
  padding: 0 8px !important;
  border-radius: 6px !important;
  border: none !important;
  background: transparent !important;
  color: var(--text-secondary, #5f6368) !important;
  font-weight: 500 !important;
  position: relative !important;
  z-index: 2 !important;
  transition: color 0.15s ease !important;
  box-shadow: none !important;
  display: inline-flex !important;
  align-items: center !important;
  gap: 8px !important;
}
.wf-tab:hover {
  color: var(--text-primary, #121316) !important;
}
.wf-tab.active {
  color: var(--text-primary, #121316) !important;
  font-weight: 600 !important;
}
.wf-tab.active::after {
  display: none !important;
}
.tab-icon-svg {
  flex-shrink: 0 !important;
  color: #64748b !important;
  vertical-align: middle !important;
  transition: color 0.15s ease !important;
}
.wf-tab:hover .tab-icon-svg {
  color: #1e293b !important;
}
.wf-tab.active .tab-icon-svg {
  color: #2563eb !important;
}
.side-foot {
  height: 48px !important;
  min-height: 48px !important;
  padding: 8px 16px !important;
  border-top: 1px solid var(--line-color) !important;
  background: #ffffff !important;
  box-sizing: border-box !important;
  margin-top: auto !important;
  display: flex !important;
  flex-direction: column !important;
  justify-content: center !important;
}
.side-foot b {
  font-size: 11.5px !important;
  color: #1e293b !important;
  font-weight: 600 !important;
  line-height: 1.2 !important;
}
.side-foot span {
  font-size: 10.5px !important;
  color: #64748b !important;
  line-height: 1.2 !important;
  margin-top: 2px !important;
}
.deep-drawer {
  border-top: 1px solid var(--line-color) !important;
  background: #ffffff !important;
}
.drawer-head {
  height: 48px !important;
  border-bottom: 1px solid var(--line-color) !important;
  background: #ffffff !important;
  box-sizing: border-box !important;
  padding: 0 16px !important;
}
.deep-drawer.collapsed {
  transform: translateY(calc(100% - 48px)) !important;
  clip-path: none !important;
}
.drawer-pill {
  background: #eff6ff !important;
  border: 1px solid rgba(37, 99, 235, 0.2) !important;
  color: #2563eb !important;
  border-radius: 6px !important;
  padding: 3px 8px !important;
  font-size: 11px !important;
  font-weight: 600 !important;
}
.light-bottom-bar {
  border-top: 1px solid var(--line-color) !important;
  background: #ffffff !important;
  box-sizing: border-box !important;
}
.sidebar-resizer-handle {
  position: absolute !important;
  top: 0 !important;
  right: -3px !important;
  bottom: 0 !important;
  width: 6px !important;
  cursor: col-resize !important;
  z-index: 50 !important;
  transition: background 0.15s ease !important;
}
.sidebar-resizer-handle:hover, .sidebar-resizer-handle.is-dragging {
  background: #3b82f6 !important;
  opacity: 0.6 !important;
}

</style>
</head><body>
<div class="shell" data-view="workbench" data-workspace="flow">
  <aside class="left-rail">
    <div class="rail-head">
      <div class="rail-brand" title="__PRODUCT_NAME__" onclick="showShellView('workbench')">HF</div>
    </div>
    <nav class="rail-nav" aria-label="系统一级导航">
      <button type="button" class="rail-item active" id="railNavWorkbench" data-rail-nav="workbench" data-nav="workbench" onclick="showShellView('workbench')" title="空间工作台 (Workbench)">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>
      </button>
      <button type="button" class="rail-item" id="railNavDashboard" data-rail-nav="dashboard" data-nav="dashboard" onclick="showShellView('dashboard')" title="全局仪表板 (Dashboard)">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/><rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/></svg>
      </button>
      <button type="button" class="rail-item" id="railNavOps" data-rail-nav="ops" data-nav="ops" onclick="showShellView('ops')" title="运维驾驶舱 (Ops & Topology)">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="m10 15 5-3-5-3v6Z"/></svg>
      </button>
      <button type="button" class="rail-item" id="railNavAlerts" data-rail-nav="alerts" data-nav="alerts" onclick="showShellView('alerts')" title="告警中心 (Alerts)">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>
        <span class="rail-badge-dot" id="railAlertDot" style="display:none"></span>
      </button>
      <button type="button" class="rail-item" id="railNavTemplates" data-rail-nav="templates" data-nav="templates" onclick="showTemplateLibrary()" title="模板与规范资产库 (Templates)">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><polyline points="14 2 14 8 20 8"/></svg>
      </button>
    </nav>
    <div class="rail-foot">
      <div class="rail-avatar" id="railSpaceAvatar" title="空间" onclick="toggleSpaceMenu(event)">共</div>
    </div>
  </aside>
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-head brand-row">
      <div class="brand">__PRODUCT_NAME__</div>
      <span class="sidebar-title" id="sidebarTitle">空间工作台</span>
      <button type="button" class="btn-collapse-sidebar" id="btnCollapseSidebar" onclick="toggleSidebar()" title="折叠侧边栏 (⌘B)">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m15 18-6-6 6-6"/></svg>
      </button>
    </div>
    <div class="sidebar-scroll">
      <!-- Zone 1: Space Switcher Pill + Primary Action CTA -->
      <div class="sidebar-zone-top switcher-wrap">
        <div class="space-pill" id="spaceSwitcher" onclick="toggleSpaceMenu(event)" aria-haspopup="true" aria-label="切换工厂空间" title="切换工厂空间">
          <div class="space-avatar" id="spaceAvatar">数</div>
          <div style="min-width:0;flex:1">
            <div class="space-name" id="spaceName">选择空间</div>
            <div class="space-subtext tabular-nums" id="spaceMeta">当前工厂</div>
          </div>
          <svg class="space-chev" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 9 6 6 6-6"/></svg>
        </div>
        <div id="spaceMenu" class="space-menu" hidden>
          <div id="projects" class="projects"></div>
          <button type="button" class="space-new" onclick="closeSpaceMenu();showNewProjectModal()">新建工厂空间</button>
        </div>
        <button type="button" class="btn-primary-launch" onclick="showNewWorkflow()" title="发起新需求 / 启动工作流">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          <span>发起新需求</span>
        </button>
      </div>

      <!-- Zone 2: Space Workflow Tree (按生产态分流) -->
      <div class="sidebar-section" id="sidebarWorkflowTreeSection">
        <div class="sidebar-section-head">
          <span>空间工作流</span>
          <span class="tabular-nums" id="sidebarWfTotalCount">0</span>
        </div>
        <div id="sidebarWorkflowGroups" data-nav="workflows">
          <!-- Rendered dynamically via renderSidebarWorkflows() -->
        </div>
        <span class="nav-count" id="navWfCount" hidden>0</span>
      </div>

      <!-- Zone 3: Fleet & Slots (空间资源与工位) -->
      <div class="sidebar-section">
        <div class="sidebar-section-head">
          <span>空间资源与工位</span>
          <span class="tabular-nums" id="sidebarResourceCounts">0</span>
        </div>
        <div class="sidebar-item" id="sidebarFleetItem" onclick="showShellView('agents')" title="查看执行者阵容" data-nav="agents">
          <svg class="item-icon-svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
          <div style="min-width:0;flex:1">
            <div class="resource-title">执行者机队 <span class="online-pill" id="sidebarOnlinePill">0 在线</span></div>
            <div class="resource-sub" id="sidebarFleetNames">暂无执行者</div>
          </div>
        </div>
        <div class="sidebar-item" id="sidebarPanesItem" onclick="showShellView('slots')" title="查看常驻智能体工位" data-nav="slots">
          <svg class="item-icon-svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18"/><path d="M9 21V9"/></svg>
          <div style="min-width:0;flex:1">
            <div class="resource-title">智能体工位</div>
            <div class="resource-sub" id="sidebarPanesSummary">暂无工位</div>
          </div>
        </div>
        <span class="nav-count" id="navAgentCount" hidden>0</span>
        <span class="nav-count" id="navAlertCount" hidden>0</span>
      </div>

      <!-- Zone 4: Governance (项目治理与审计) -->
      <div class="sidebar-section">
        <div class="sidebar-section-head">
          <span>项目治理与审计</span>
        </div>
        <div class="sidebar-item highlight-governance" id="navController" onclick="openControllerCockpitModal()" title="Controller 协调器调度控制台">
          <span class="item-icon">🎮</span>
          <span class="item-text">Controller 控制台</span>
          <span class="pulse-indicator" title="协调器调度活跃"></span>
        </div>
        <div class="sidebar-item" id="navTemplates" onclick="showTemplateLibrary()" title="工作流模板规范资产库">
          <svg class="item-icon-svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
          <span class="item-text">模板规范库</span>
          <button type="button" class="visually-hidden" onclick="showTemplateLibrary()">模板库</button>
        </div>
        <div class="sidebar-item" id="sidebarLogsItem" onclick="showLogs()" title="查看调度审计日志">
          <svg class="item-icon-svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1-2.5-2.5Z"/><path d="M6 6h10"/><path d="M6 10h10"/></svg>
          <span class="item-text">调度审计日志</span>
        </div>
        <div class="sidebar-item" id="navArchive" onclick="showArchive()" title="查看任务归档库">
          <svg class="item-icon-svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><path d="M10 12h4"/></svg>
          <span class="item-text">任务归档库</span>
        </div>
        <div class="sidebar-item danger-item" onclick="showUnregisterProjectModal()" title="注销当前项目工厂">
          <svg class="item-icon-svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
          <span class="item-text">注销项目</span>
        </div>
      </div>
      <div class="side-foot">
        <b id="sideFootTitle">当前工厂</b>
        <span>日志与归档可回看</span>
      </div>
    </div>
  </aside>
  <main class="main">
    <div class="top">
      <div class="crumb visually-hidden" aria-hidden="true">
        <div class="crumb-line"><span id="crumbSection">工作台</span> / <strong id="workflowSubject" class="wf-subject">—</strong></div>
        <small id="workflowSub" class="wf-sub"></small>
        <div id="projectTitle" class="visually-hidden">选择项目</div>
      </div>
      <div class="workflow-tabs-bar" id="workflowTabsBar" role="tablist" aria-label="工作流页签">
        <button type="button" class="btn-expand-sidebar" id="btnExpandSidebar" onclick="toggleSidebar()" title="展开侧边栏 (⌘B)">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m9 18 6-6-6-6"/></svg>
        </button>
        <div class="tabs-scroll" id="workflowTabsList"></div>
        <button type="button" class="btn-new-tab" onclick="showNewWorkflow()" title="发起新工作流">+</button>
      </div>
      <div class="actions">
        <button class="btn factory-action" onclick="advanceStage()" title="推进当前阶段">进入下一阶段</button>
        <div class="dropdown factory-action" id="moreDropdown">
          <button class="btn icon-only" onclick="toggleMoreMenu(event)" aria-label="更多操作" title="更多操作">···</button>
          <div class="dropdown-menu">
            <button class="dropdown-item" onclick="closeMoreMenu();editWorkflowTitle()">修改工作流名称</button>
            <button class="dropdown-item" onclick="closeMoreMenu();openControllerCockpitModal()">Controller 控制台</button>
            <button class="dropdown-item" onclick="closeMoreMenu();createCandidate()">创建候选分支</button>
            <button class="dropdown-item" onclick="closeMoreMenu();runPreflight()">执行者自检</button>
            <button class="dropdown-item" onclick="closeMoreMenu();showArchive()">任务归档</button>
            <button class="dropdown-item" onclick="closeMoreMenu();showLogs()">查看日志</button>
            <button class="dropdown-item" onclick="closeMoreMenu();showAgentOverride()">指定执行者</button>
            <button class="dropdown-item" onclick="closeMoreMenu();toggleWorkflowPause()">暂停/恢复调度</button>
            <button class="dropdown-item" onclick="closeMoreMenu();stepWorkflow()">单步推进节点</button>
            <button class="dropdown-item" onclick="closeMoreMenu();showRollbackModal()">节点回溯 (Rollback)</button>
            <button class="dropdown-item" onclick="closeMoreMenu();showCheckpointsModal()">快照中心 (Checkpoints)</button>
            <div class="dropdown-divider"></div>
            <button class="dropdown-item danger-text" onclick="closeMoreMenu();showUnregisterProjectModal()">注销项目</button>
          </div>
        </div>
        <button class="btn primary factory-action" onclick="showNewWorkflow()"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg><span>新需求</span></button>
      </div>
    </div>
    <div class="metrics" aria-hidden="true">
      <div class="metric"><b id="mProjects">0</b><span>项目空间</span></div>
      <div class="metric"><b id="mWorkflows">0</b><span>活跃工作流</span></div>
      <div class="metric"><b id="mAgents">0</b><span>活跃执行者</span></div>
      <div class="metric"><b id="mAlerts">0</b><span>需要关注</span></div>
    </div>
    <div id="stages" class="stages"></div>
    <div id="attentionBanner" class="attention-banner" style="display:none"></div>
    <div id="workflowSwitcher" class="wf-switcher" style="display:none"></div>
    <div class="workspace-layout">
      <section class="panel main-panel">
        <div id="canvasToolbar">
          <div class="toolbar-left">
            <div id="canvasWfMeta" class="canvas-wf-meta" style="display:none"></div>
            <div class="flow-view-toggle" role="tablist" aria-label="工作流视图切换">
              <button class="filter-btn active" id="viewFlowBtn" onclick="switchWorkflowView('flow')">流程图</button>
              <button class="filter-btn" id="viewListBtn" onclick="switchWorkflowView('list')">任务列表</button>
            </div>
            <div class="task-filters" style="display:none">
              <button class="filter-btn active" id="fAll" onclick="setTaskFilter('all')">全部任务 <span class="filter-cnt" id="cntAll">0</span></button>
              <button class="filter-btn" id="fDecision" onclick="setTaskFilter('decision')">待我拍板 <span class="filter-cnt" id="cntDecision">0</span></button>
              <button class="filter-btn" id="fAttention" onclick="setTaskFilter('attention')">需关注 <span class="filter-cnt" id="cntAttention">0</span></button>
              <button class="filter-btn" id="fActive" onclick="setTaskFilter('active')">进行中 <span class="filter-cnt" id="cntActive">0</span></button>
            </div>
          </div>
          <div class="canvas-tools">
            <div id="flowSummary"></div>
            <div class="flow-controls">
              <button class="btn" onclick="flowZoomIn()" title="放大">+</button>
              <button class="btn" onclick="flowZoomOut()" title="缩小">−</button>
              <button class="btn" onclick="fitFlowGraph()" title="自适应">Fit</button>
            </div>
          </div>
        </div>
        <div id="flowWrap">
          <div id="flowCanvasWrap">
            <div id="flowCanvas" role="application" aria-label="工作流 DAG 画布"></div>
            <div id="flowError" role="alert"></div>
          </div>
          <aside id="flowInspector" aria-label="节点 Inspector">
            <div class="flow-insp-head">
              <div class="flow-insp-title" id="flowInspTitle">节点详情</div>
              <div class="flow-insp-meta" id="flowInspMeta">—</div>
            </div>
            <div class="flow-insp-tabs" role="tablist" aria-label="Inspector 页签">
              <button class="flow-insp-tab active" id="flowTabSummary" onclick="switchFlowInspectorTab('summary')">概览</button>
              <button class="flow-insp-tab" id="flowTabTasks" onclick="switchFlowInspectorTab('tasks')">任务</button>
              <button class="flow-insp-tab" id="flowTabContext" onclick="switchFlowInspectorTab('context')">上下文</button>
              <button class="flow-insp-tab" id="flowTabRuntime" onclick="switchFlowInspectorTab('runtime')">运行</button>
            </div>
            <div id="flowInspectorBody"></div>
          </aside>
        </div>
        <div id="tasks" class="task-list" style="display:none"></div>
        <div id="controllerTabView" class="controller-panel" hidden></div>
        <div id="templatesTabView" class="controller-panel" hidden></div>
        <div id="archiveTabView" class="controller-panel" hidden></div>
        <div id="logsTabView" class="controller-panel" hidden></div>
      </section>
    </div>
    <div id="resourceStore" hidden>
      <h4>执行者阵容</h4><div id="agents"></div>
      <h4>常驻智能体工位</h4><div id="slots"></div>
      <div id="alerts"></div>
    </div>
    <footer class="light-bottom-bar" id="consoleBottomBar">
      <div class="bottom-left">
        <div class="bottom-indicator">
          <span class="indicator-pip"></span>
          <span id="bottomStatusText">Controller 正常调度</span>
        </div>
        <span class="bottom-sep">·</span>
        <span class="bottom-ticker" id="bottomTicker">工作流调度就绪</span>
      </div>
      <div class="bottom-right">
        <span class="bottom-stat">活跃执行者: <b id="bottomActiveWorkers">0</b></span>
        <span class="bottom-stat">轮询延迟: <b id="bottomPollLatency">8ms</b></span>
        <button type="button" class="btn-bottom-action" onclick="toggleDeepDrawer()">
          <span id="bottomDrawerText">▲ 底层现场</span>
        </button>
      </div>
    </footer>
  </main>
</div>
<div id="modal" class="modal" role="dialog" aria-modal="true" aria-labelledby="modalTitle">
  <div class="modal-card">
    <div class="modal-head">
      <strong id="modalTitle">详情</strong>
      <button class="close" aria-label="关闭弹窗" onclick="closeModal()"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg></button>
    </div>
    <div id="modalBody" class="modal-body"></div>
  </div>
</div>
<div id="taskDrawer" class="task-drawer" role="dialog" aria-modal="false" aria-labelledby="taskDrawerTitle" hidden>
  <div class="task-drawer-card">
    <div class="task-drawer-head">
      <div style="min-width:0;flex:1">
        <div class="task-drawer-title"><span id="taskDrawerIcon">○</span><span id="taskDrawerTitle">任务详情</span></div>
        <div class="task-drawer-id" id="taskDrawerId"></div>
        <div class="task-drawer-meta" id="taskDrawerMeta"></div>
      </div>
      <div class="task-drawer-actions">
        <div class="task-drawer-menu-wrap">
          <button class="btn icon-only" id="taskDrawerMore" aria-label="任务更多操作" title="更多操作" onclick="toggleTaskDrawerMenu(event)">···</button>
          <div class="task-drawer-menu" id="taskDrawerMenu" onclick="event.stopPropagation()"></div>
        </div>
        <button class="btn primary" id="taskDrawerPrimary" style="padding:4px 12px;font-size:12px">成果会签</button>
        <button class="close" aria-label="关闭任务详情" onclick="closeTaskDrawer()"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg></button>
      </div>
    </div>
    <div class="td-tabs" role="tablist" aria-label="任务详情页签">
      <button class="td-tab" role="tab" id="tdTabOverview" aria-selected="true" onclick="switchTaskDrawerTab('overview')">概览</button>
      <button class="td-tab" role="tab" id="tdTabActivity" aria-selected="false" onclick="switchTaskDrawerTab('activity')">活动</button>
      <button class="td-tab" role="tab" id="tdTabArtifacts" aria-selected="false" onclick="switchTaskDrawerTab('artifacts')">产物</button>
      <button class="td-tab" role="tab" id="tdTabRuntime" aria-selected="false" onclick="switchTaskDrawerTab('runtime')">运行时</button>
    </div>
    <div id="taskDrawerBody" class="task-drawer-body"></div>
  </div>
</div>
<div id="toast" class="toast" role="alert" aria-live="polite"></div>
<div id="deepDrawer" class="deep-drawer collapsed">
  <div class="drawer-head" onclick="toggleDeepDrawer()">
    <div style="display:flex;align-items:center;gap:8px"><span class="drawer-pill">底层物理现场</span><span class="muted" style="font-size:12px">原生终端 · 内核日志 · 白盒遥测</span></div>
    <div style="display:flex;align-items:center;gap:8px"><button class="mini" onclick="event.stopPropagation();refreshDeepDrawer()">刷新</button><span id="drawerToggleText" style="font-size:12px;color:var(--accent);font-weight:600">▲ 展开抽屉</span></div>
  </div>
  <div class="drawer-body">
    <div class="drawer-tabs">
      <button class="dtab active" id="dtabTty" onclick="switchDrawerTab('tty')">活动工位终端 (Live TTY)</button>
      <button class="dtab" id="dtabLogs" onclick="switchDrawerTab('logs')">内核日志 (Controller Log)</button>
      <button class="dtab" id="dtabRaw" onclick="switchDrawerTab('raw')">白盒遥测原始数据 (Raw Telemetry)</button>
    </div>
    <div class="drawer-view"><pre id="drawerPre">请选择活动工位或点击展开查看底层物理输出…</pre></div>
  </div>
</div>
<script src="/static/vendor/dagre-3.1.1.min.js"></script>
<script src="/static/vendor/x6-3.1.8.min.js"></script>
<script>
let state={overview:null,project:null,workflow:null,ops:null,dash:null,projectId:null,workflowId:null,openWorkflowTabIds:[],sidebarCollapsed:false,completedExpanded:false,completedVisibleLimit:5,spaceId:null,space:null,opsMode:false,dashMode:false,dashWorkflowId:null,shellView:'workbench',taskFilter:'all',drawerTab:'tty',selectedTaskId:null,taskDrawerTab:'overview',selectedTaskDetail:null,workflowView:'flow',flowSelectedNodeId:null,flowInspectorTab:'summary',flowGraph:null,flowGraphWfId:null,flowHtmlReady:false};
const VIEW_KEY='herdrConsoleView';
function closeMoreMenu(){const dd=document.getElementById('moreDropdown');if(dd)dd.classList.remove('open')}
function toggleMoreMenu(e){e.stopPropagation();const dd=document.getElementById('moreDropdown');if(dd)dd.classList.toggle('open')}
function closeAllTaskMenus(){document.querySelectorAll('.task-menu.open').forEach(m=>m.classList.remove('open'))}
function toggleTaskMenu(e,tid){e.stopPropagation();const m=document.getElementById('taskMenu_'+tid);if(!m)return;const wasOpen=m.classList.contains('open');closeAllTaskMenus();closeMoreMenu();if(!wasOpen)m.classList.add('open')}
function onTaskRowClick(e,tid){if(e.target.closest('.task-menu'))return;if(window.getSelection&&window.getSelection().toString())return;openTaskDrawer(tid)}
document.addEventListener('click',e=>{
  const dd=document.getElementById('moreDropdown');
  if(dd&&!dd.contains(e.target))dd.classList.remove('open');
  if(!e.target.closest('.switcher-wrap'))closeSpaceMenu();
  if(!e.target.closest('.task-menu'))closeAllTaskMenus();
  if(!e.target.closest('.task-drawer-menu-wrap'))closeTaskDrawerMenu();
  if(!e.target.closest('.linear-select'))closeLinearSelectMenu();
});
document.addEventListener('keydown',e=>{
  if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='b'){
    e.preventDefault();
    if(typeof toggleSidebar==='function')toggleSidebar();
    return;
  }
  if(e.key==='Escape'){
    const modal=document.getElementById('modal');
    if(modal&&modal.classList.contains('open')){closeModal();return}
    const dm=document.getElementById('taskDrawerMenu');
    if(dm&&dm.classList.contains('open')){closeTaskDrawerMenu();return}
    const td=document.getElementById('taskDrawer');
    if(td&&!td.hidden){closeTaskDrawer();return}
    closeMoreMenu();
    closeSpaceMenu();
    closeAllTaskMenus();
    closeLinearSelectMenu();
    const d=document.getElementById('deepDrawer');
    if(d&&!d.classList.contains('collapsed')){
      d.classList.add('collapsed');
      const t=document.getElementById('drawerToggleText');
      if(t)t.textContent='▲ 展开抽屉';
    }
  }
});
function showConfirmModal({title,message,confirmText='确认',danger=false,onConfirm}){
  openModal(title,`<div style="line-height:1.6"><div style="font-size:14px;margin-bottom:18px;color:var(--text)">${esc(message)}</div><div style="display:flex;justify-content:flex-end;gap:8px"><button class="btn" onclick="closeModal()">取消</button><button id="modalConfirmBtn" class="btn ${danger?'danger-btn':'primary'}">${esc(confirmText)}</button></div></div>`);
  const btn=document.getElementById('modalConfirmBtn');
  if(btn)btn.onclick=async()=>{closeModal();if(onConfirm)await onConfirm()}
}
function showPromptModal({title,label,defaultValue='',confirmText='确定',onConfirm}){
  openModal(title,`<div class="form"><label for="promptInput">${esc(label)}</label><input id="promptInput" value="${esc(defaultValue)}" autofocus><div style="display:flex;justify-content:flex-end;gap:8px;margin-top:8px"><button class="btn" onclick="closeModal()">取消</button><button id="modalPromptBtn" class="btn primary">${esc(confirmText)}</button></div></div>`);
  const input=document.getElementById('promptInput');
  if(input){input.focus();input.select();input.addEventListener('keydown',e=>{if(e.key==='Enter')document.getElementById('modalPromptBtn')?.click()})};
  const btn=document.getElementById('modalPromptBtn');
  if(btn)btn.onclick=async()=>{const val=input?input.value.trim():'';closeModal();if(onConfirm)await onConfirm(val)}
}
function syncUrlView(){
  try{
    const u=new URL(window.location.href);
    const v=state.opsMode?'ops':state.dashMode?'dashboard':(state.shellView||'workbench');
    if(v==='workbench'){
      u.searchParams.delete('view');
      u.searchParams.delete('ops');
      if(state.workflowId&&state.workflowId!=='__templates__'&&state.workflowId!=='__ctl__'&&state.workflowId!=='__archive__'&&state.workflowId!=='__logs__'){
        u.searchParams.set('workflow_id',state.workflowId);
      }else{
        u.searchParams.delete('workflow_id');
      }
    }else{
      u.searchParams.set('view',v);
      if(v==='dashboard'&&state.dashWorkflowId){
        u.searchParams.set('workflow_id',state.dashWorkflowId);
      }else{
        u.searchParams.delete('workflow_id');
      }
      u.searchParams.delete('ops');
    }
    const newUrl=u.pathname+u.search+u.hash;
    if(window.location.pathname+window.location.search+window.location.hash!==newUrl){
      window.history.replaceState(null,'',newUrl);
    }
  }catch(e){}
}
function saveViewState(){try{localStorage.setItem(VIEW_KEY,JSON.stringify({opsMode:state.opsMode,dashMode:state.dashMode,dashWorkflowId:state.dashWorkflowId||null,spaceId:state.spaceId,workflowId:state.workflowId,shellView:state.shellView||'workbench'}));syncUrlView();}catch(e){}}
function loadViewState(){try{return JSON.parse(localStorage.getItem(VIEW_KEY)||'null')}catch(e){return null}}
async function waitForWorkflowJob(jobId){
  try{
    for(let i=1;i<=620;i++){
      const job=await api('/api/run/status?id='+encodeURIComponent(jobId));
      if(job.status==='succeeded')return job;
      if(job.status==='failed')throw new Error(job.error||'工作流启动失败');
      const wait=document.getElementById('runWaitStatus');
      if(wait)wait.textContent='深度体检与启动中… '+i+'s（后端最长约 10 分钟，请勿重复创建）';
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
  let db=document.getElementById('dashButton');
  if(!db){db=document.createElement('button');db.id='dashButton';actions.prepend(db)}
  db.textContent=state.dashMode?'← 返回工厂':'我的仪表板';
  db.className=state.dashMode?'btn primary':'btn';
  db.onclick=state.dashMode?exitDashboard:showDashboard;
  let oh=document.getElementById('opsHeaderActions');
  if(!oh){
    oh=document.createElement('div');
    oh.id='opsHeaderActions';
    oh.style.display='none';
    oh.style.alignItems='center';
    oh.style.gap='8px';
    oh.innerHTML='<span id="opsTimerBadge" class="fn-pill" style="font-size:11px;padding:2px 8px;border-radius:12px;background:var(--bg-subtle);border:1px solid var(--border-subtle);color:var(--text-secondary)">自动刷新 · 10s</span><button class="btn" id="opsRefreshBtn" onclick="triggerOpsRefresh()" style="padding:4px 12px;font-size:12px">⟳ 立即刷新</button>';
    actions.appendChild(oh);
  }
  oh.style.display=state.opsMode?'inline-flex':'none';
  closeMoreMenu();
  document.querySelectorAll('.factory-action').forEach(button=>{button.hidden=state.opsMode});
  document.querySelectorAll('.factory-action').forEach(button=>{if(state.dashMode)button.hidden=true});
}

async function api(p,o={}){
  const r=await fetch(p,{headers:{'Content-Type':'application/json'},...o});
  const d=await r.json();
  if(!r.ok||d.ok===false)throw new Error(d.error||('HTTP '+r.status));
  return d.data??d
}
function friendlyErrorChinese(raw){
  const msg=String(raw||'').trim();
  if(!msg)return '操作失败，请重试';
  if(/pre-commit script failed/i.test(msg)||/husky.*failed/i.test(msg)){
    if(/check-node-version-sources/i.test(msg)||/Node 版本/i.test(msg)){
      return '代码提交前门禁失败：检测到 Node 版本声明不一致（或存在临时测试脚本干扰）。\n\n' + msg;
    }
    if(/ArchitectureTest/i.test(msg)||/architecture check failed/i.test(msg)){
      return '代码提交前门禁失败：后端架构规范检查未通过。\n\n' + msg;
    }
    return '代码提交前预检门禁（Git Hooks）拦截：请检查提交内容是否符合仓库门禁约束。\n\n' + msg;
  }
  if(/git_index_lock/i.test(msg)||/Unable to create.*index\.lock/i.test(msg)){
    return 'Git 索引被其他操作锁定中，请稍候几秒后重试。\n\n' + msg;
  }
  if(/merge conflict|conflict/i.test(msg)){
    return '分支合并发生代码冲突，请核对分支基线。\n\n' + msg;
  }
  if(/命令超时|timeout/i.test(msg)){
    return '操作执行超时，可能由于后台进程负载过高或正在编译。\n\n' + msg;
  }
  return msg;
}
let toastTimer=null;
function toast(m,b=false){
  const e=document.getElementById('toast');
  if(!e)return;
  if(toastTimer){clearTimeout(toastTimer);toastTimer=null;}
  const displayMsg=b?friendlyErrorChinese(m):String(m||'');
  e.className='toast show '+(b?'danger-text danger-toast':'good-text');
  e.innerHTML='<div class="toast-msg">'+esc(displayMsg)+'</div>'+(b?'<button class="toast-close" title="点击关闭" onclick="closeToast()">×</button>':'');
  if(b){
    // 错误信息不自动消失，等待用户查看、复制并在点击后再消失
    e.onclick=function(evt){if(evt.target===e)closeToast();};
  }else{
    e.onclick=null;
    toastTimer=setTimeout(closeToast,4200);
  }
}
function closeToast(){
  const e=document.getElementById('toast');
  if(e){e.classList.remove('show');e.className='toast';}
  if(toastTimer){clearTimeout(toastTimer);toastTimer=null;}
}
function esc(s){
  return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))
}
// Emit a value as a single-quoted JS literal for use inside a double-quoted
// inline handler. JSON.stringify would emit double quotes and truncate the
// attribute at the first one.
function jsArg(v){
  const s=String(v??'').replace(/[\r\n]/g,'');
  return "'"+s.replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/&/g,'&amp;').replace(/"/g,'&quot;')+"'";
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
function humanNodeStatus(s){
  return s==='completed'?'已完成':humanStatus(s);
}
function badge(s){return `<span class="badge ${esc(s)}">${esc(humanStatus(s))}</span>`}
function cleanStageLabel(s){
  return String(s??'').replace(/^\d+\s*(?=[\u4e00-\u9fff])/, '')
}

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
  const tplEl=document.getElementById('templatesTabView');
  setWorkspaceMode('templates');
  if(!state.openWorkflowTabIds)state.openWorkflowTabIds=[];
  if(!state.openWorkflowTabIds.includes('__templates__'))state.openWorkflowTabIds.push('__templates__');
  state.workflowId='__templates__';
  state.opsMode=false;state.dashMode=false;state.shellView='templates';
  saveViewState();
  renderWorkflowTabs();
  paintCrumb();
  if(tplEl){
    tplEl.innerHTML=`<div class="ctl-panel-header"><div style="display:flex;justify-content:space-between;align-items:center;width:100%"><div><strong>工作流模板规范库</strong><span class="muted" style="margin-left:8px;font-weight:400">· 模板定义工作流节点与 DAG 依赖</span></div><button class="btn primary" onclick="showTemplateEditor()"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg><span>新建模板</span></button></div></div><div class="empty">正在加载模板…</div>`;
  }
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
    if(tplEl){
      tplEl.innerHTML=`<div class="ctl-panel-header"><div style="display:flex;justify-content:space-between;align-items:center;width:100%"><div><strong>工作流模板规范库</strong><span class="muted" style="margin-left:8px;font-weight:400">· 模板定义工作流节点与 DAG 依赖</span></div><button class="btn primary" onclick="showTemplateEditor()"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg><span>新建模板</span></button></div></div><div class="muted" style="margin-bottom:8px">模板定义工作流的节点与 DAG 依赖。自定义模板保存到 ~/.herdr-controller/templates/，对新启动的工作流即时生效，不影响已运行的工作流。</div>${cards}`;
    }
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
let opsTimer=null;
let opsCountdown=10;
function startOpsTimer(){
  stopOpsTimer();
  opsCountdown=10;
  updateOpsTimerBadge();
  opsTimer=setInterval(()=>{
    if(!state.opsMode){stopOpsTimer();return}
    if(document.hidden)return;
    opsCountdown--;
    updateOpsTimerBadge();
    if(opsCountdown<=0){
      opsCountdown=10;
      loadOpsCenter();
    }
  },1000);
}
function stopOpsTimer(){
  if(opsTimer){clearInterval(opsTimer);opsTimer=null}
}
function updateOpsTimerBadge(){
  const b=document.getElementById('opsTimerBadge');
  if(b)b.textContent='自动刷新 · '+opsCountdown+'s';
}
async function triggerOpsRefresh(){
  opsCountdown=10;
  updateOpsTimerBadge();
  const btn=document.getElementById('opsRefreshBtn');
  if(btn){btn.disabled=true;btn.textContent='刷新中…'}
  await loadOpsCenter();
  if(btn){btn.disabled=false;btn.textContent='⟳ 立即刷新'}
}

function showOpsCenter(){
  state.opsMode=true;state.dashMode=false;state.shellView='ops';stopDashTimer();
  saveViewState();
  syncOpsUi();
  document.getElementById('projectTitle').textContent='运维驾驶舱';
  document.getElementById('workflowSubject').textContent='四层运维视图 · 全局驾驶舱';
  document.getElementById('workflowSub').textContent='';
  document.getElementById('workflowSwitcher').style.display='none';
  document.getElementById('stages').innerHTML='';
  setWorkspaceMode('aux');
  document.getElementById('tasks').innerHTML='<div class="empty">正在加载运维驾驶舱数据…</div>';
  paintCrumb();
  loadOpsCenter();
  startOpsTimer();
}
async function exitOpsCenter(){
  stopOpsTimer();
  state.opsMode=false;
  state.shellView='workbench';
  saveViewState();
  syncOpsUi();
  await refreshAll();
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

function filterOpsWorkflows(filter){
  state.opsWfFilter=filter;
  renderOpsCenter();
}

function toggleOpsCardNodes(e,wid){
  if(e)e.stopPropagation();
  state.opsExpandedNodes=state.opsExpandedNodes||{};
  state.opsExpandedNodes[wid]=!state.opsExpandedNodes[wid];
  const el=document.getElementById('nodes-'+wid);
  const textEl=document.getElementById('toggleText-'+wid);
  if(el){
    const open=state.opsExpandedNodes[wid];
    el.style.display=open?'flex':'none';
    if(textEl)textEl.textContent=open?'收起详情 ▲':'节点明细 ▼';
  }
}

let _opsActionBusy=false;
async function runOpsAnomalyAction(actType,wid,tid,node,options){
  options=options||{};
  if(actType==='force_pass'||actType==='force_pass_advance'){
    if(!options.confirmed){
      confirmOpsForcePass(wid,tid,node,options);
      return;
    }
  }
  if(_opsActionBusy){
    toast('操作正在执行中，请勿重复点击',true);
    return;
  }
  _opsActionBusy=true;
  try{
    toast('正在执行操作: '+actType+' …');
    const payload={
      type:actType,
      action_type:actType,
      workflow_id:wid,
      task_id:tid,
      node:node,
      stage:node,
      gate_node_id:node,
      note:options.reason||options.note||'',
      reason:options.reason||options.note||'',
      confirmed:!!options.confirmed,
      operator:options.operator||'human_ops'
    };
    if(options.expected_version!==undefined&&options.expected_version!==null&&options.expected_version!==''){
      payload.expected_version=parseInt(options.expected_version,10);
    }else if(options.version!==undefined&&options.version!==null&&options.version!==''){
      payload.expected_version=parseInt(options.version,10);
    }
    if(options.expected_pane_id||options.pane_id){
      payload.expected_pane_id=options.expected_pane_id||options.pane_id;
      payload.pane_id=options.pane_id||options.expected_pane_id;
    }
    const res=await api('/api/controller/execute-action',{
      method:'POST',
      body:JSON.stringify(payload)
    });
    if(res.partial){
      toast('人工豁免已写入但推进失败: '+(res.advance_error||res.error||'后续推进未完成'),true);
    }else if(res.ok){
      if(actType==='force_pass'||actType==='force_pass_advance'){
        if(res.advanced){
          toast('门禁已豁免，工作流推进至: '+((res.advanced&&res.advanced.next_stage)||'下一阶段'));
        }else{
          toast('人工豁免记录已写入，等待调度推进');
        }
      }else if(actType==='ops_repair'||actType==='retry'||actType==='rework'||actType==='redrive'){
        toast('恢复命令已执行 ('+(res.action_id||actType)+')，已向工位派发');
      }else{
        toast('操作成功: '+(res.task_id||res.action_id||'已生效'));
      }
    }else{
      toast('操作未完成: '+(res.error||'执行未生效'),true);
    }
    await loadOpsCenter();
  }catch(e){
    toast('操作失败: '+e.message,true);
  }finally{
    _opsActionBusy=false;
  }
}

function confirmOpsForcePass(wid,tid,node,options){
  options=options||{};
  const widEsc=esc(wid||'');
  const tidEsc=esc(tid||'');
  const nodeEsc=esc(node||'');
  const expVer=(options.expected_version!==undefined&&options.expected_version!==null)?options.expected_version:(options.version!==undefined&&options.version!==null?options.version:'');
  const expPane=esc(options.expected_pane_id||options.pane_id||'');
  openModal('人工强制放行确认 (高风险)',`
    <div style="line-height:1.6">
      <div style="margin-bottom:8px"><strong>目标工作流:</strong> <code>${widEsc}</code></div>
      <div style="margin-bottom:8px"><strong>目标门禁节点:</strong> <span class="badge warning">${nodeEsc||'未指定'}</span></div>
      ${tid?`<div style="margin-bottom:8px"><strong>关联任务:</strong> <code>${tidEsc}</code></div>`:''}
      <div class="ctl-effect" style="margin:12px 0;padding:12px;background:var(--warning-bg);border:1px solid var(--warning);border-radius:8px;color:var(--text-primary)">
        <strong>⚠️ 影响范围警告:</strong><br>
        本操作为<b>人工强制豁免</b>，将直接把该节点的门禁判定标记为 <code>pass</code>，并在工作流持久化记录豁免审计凭证，随后尝试推进工作流到后续阶段。<br>
        <b>绝不能用于普通自动修复！必须由工程师人工核验产物后方可放行。</b>
      </div>
      <div style="margin-top:12px">
        <label for="forcePassReason" style="font-weight:600;font-size:12px;color:var(--text-primary);display:block;margin-bottom:4px">放行原因与核验结论（必填）:</label>
        <textarea id="forcePassReason" class="input" style="width:100%;height:72px;box-sizing:border-box" placeholder="请详细填写人工豁免原因、核验人及结论..."></textarea>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">
        <button class="btn" onclick="closeModal()">取消</button>
        <button id="btnSubmitForcePass" class="btn warning">确认强制放行</button>
      </div>
    </div>
  `);
  const btn=document.getElementById('btnSubmitForcePass');
  if(btn){
    btn.onclick=()=>{
      const el=document.getElementById('forcePassReason');
      const reason=(el?el.value:'').trim();
      if(!reason){
        toast('必须填写强制放行原因',true);
        return;
      }
      btn.disabled=true;
      closeModal();
      const opts={confirmed:true,reason:reason};
      if(expVer!==''&&expVer!=='undefined'&&expVer!=='null'){
        opts.expected_version=parseInt(expVer,10);
      }
      if(expPane){
        opts.expected_pane_id=expPane;
        opts.pane_id=expPane;
      }
      runOpsAnomalyAction('force_pass_advance',wid,tid,node,opts);
    };
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

  state.opsWfFilter=state.opsWfFilter||'all';
  let activeWfs=0,attentionWfs=0,completedWfs=0;
  cards.forEach(c=>{
    const isFailed=(c.tasks.failed>0)||(c.nodes||[]).some(n=>n.status==='failed');
    const isBlocked=(c.tasks.blocked>0)||(c.nodes||[]).some(n=>n.status==='blocked');
    const isActive=(c.tasks.active>0)||(c.nodes||[]).some(n=>n.status==='working');
    if(isActive)activeWfs++;
    if(isFailed||isBlocked)attentionWfs++;
    if(!isActive&&!isFailed&&!isBlocked)completedWfs++;
  });

  const filteredCards=cards.filter(c=>{
    const isFailed=(c.tasks.failed>0)||(c.nodes||[]).some(n=>n.status==='failed');
    const isBlocked=(c.tasks.blocked>0)||(c.nodes||[]).some(n=>n.status==='blocked');
    const isActive=(c.tasks.active>0)||(c.nodes||[]).some(n=>n.status==='working');
    if(state.opsWfFilter==='active')return isActive;
    if(state.opsWfFilter==='attention')return isFailed||isBlocked;
    if(state.opsWfFilter==='completed')return !isActive&&!isFailed&&!isBlocked;
    return true;
  });

  const wfCardsHtml=filteredCards.length?filteredCards.map(c=>{
    const nodes=c.nodes||[];
    const totalNodes=nodes.length;
    const completedNodes=nodes.filter(n=>n.status==='completed'||n.status==='superseded').length;
    const failedNode=nodes.find(n=>n.status==='failed');
    const blockedNode=nodes.find(n=>n.status==='blocked');
    const workingNode=nodes.find(n=>n.status==='working');

    let statusClass='is-idle';
    let statusText='就绪';
    let summaryIcon='○';
    let summaryText='等待调度推进';
    let summaryClass='is-idle';

    if(failedNode||c.tasks.failed>0){
      statusClass='is-failed';
      statusText='执行失败';
      summaryIcon='🚨';
      summaryClass='is-failed';
      summaryText='关键阶段失败: '+cleanStageLabel(failedNode?failedNode.node_label:'未命名')+'（任务异常中断）';
    }else if(blockedNode||c.tasks.blocked>0){
      statusClass='is-blocked';
      statusText='阻塞阻断';
      summaryIcon='⏸';
      summaryClass='is-blocked';
      summaryText='关键阶段阻塞: '+cleanStageLabel(blockedNode?blockedNode.node_label:'未命名')+'（等待人工/前置条件）';
    }else if(workingNode||c.tasks.active>0){
      statusClass='is-working';
      statusText='正在运行';
      summaryIcon='⚡';
      summaryClass='is-working';
      summaryText='正在推进阶段: '+cleanStageLabel(workingNode?workingNode.node_label:'执行中');
    }else if(totalNodes>0&&completedNodes===totalNodes){
      statusClass='is-completed';
      statusText='已完成';
      summaryIcon='✓';
      summaryClass='is-completed';
      summaryText='全部 '+totalNodes+' 个阶段已全部顺利完成';
    }

    const pct=totalNodes>0?Math.round((completedNodes/totalNodes)*100):(c.tasks.completed>0?100:0);
    const isExpanded=!!(state.opsExpandedNodes&&state.opsExpandedNodes[c.workflow_id]);

    return `
      <div class="ops-card ${statusClass}" data-wf-id="${esc(c.workflow_id)}">
        <div class="ops-card-head">
          <div class="ops-card-title-group">
            <span class="ops-card-dot ${statusClass}"></span>
            <strong class="ops-card-name" title="${esc(c.workflow_label||c.workflow_id)}">${esc(c.workflow_label||c.workflow_id)}</strong>
            <span class="ops-card-status-badge ${statusClass}">${statusText}</span>
          </div>
          <div class="ops-card-actions">
            <span class="ops-card-time">${formatElapsed(c.runtime_seconds)}</span>
            <button class="btn mini primary" onclick="event.stopPropagation();openWorkflowFromOps('${esc(c.workflow_id)}')">进入工作流 ➔</button>
          </div>
        </div>
        <div class="ops-card-meta-line">
          <code>${esc(c.workflow_id)}</code> · ${c.tasks.active} 运行 · ${c.tasks.completed} 完成 · ${c.tasks.blocked} 阻塞 · ${c.tasks.failed} 失败
        </div>
        <div class="ops-card-progress-wrap">
          <div class="ops-progress-bar">
            <div class="ops-progress-fill" style="width:${pct}%;"></div>
          </div>
          <div class="ops-progress-meta">
            <span>阶段进度: <b>${completedNodes}/${totalNodes}</b> (${pct}%)</span>
            <span>总计任务: ${c.tasks.active+c.tasks.completed+c.tasks.blocked+c.tasks.failed} 个</span>
          </div>
        </div>
        <div class="ops-stage-callout ${summaryClass}">
          <div class="ops-callout-content">
            <span class="ops-callout-icon">${summaryIcon}</span>
            <span class="ops-callout-text">${esc(summaryText)}</span>
          </div>
          <button class="ops-toggle-details-btn" onclick="toggleOpsCardNodes(event,'${esc(c.workflow_id)}')">
            <span id="toggleText-${esc(c.workflow_id)}">${isExpanded?'收起详情 ▲':('节点明细 ('+totalNodes+') ▼')}</span>
          </button>
        </div>
        <div id="nodes-${esc(c.workflow_id)}" class="ops-card-nodes-detail" style="display:${isExpanded?'flex':'none'}">
          ${nodes.map(n=>`<span class="badge ${esc(n.status)}">${esc(cleanStageLabel(n.node_label))} · ${esc(humanNodeStatus(n.status))}</span>`).join('')}
        </div>
      </div>
    `;
  }).join(''):'<div class="empty">当前筛选条件下暂无工作流</div>';

  const anomsHtml=anoms.length?anoms.map(a=>{
    const isBlocked=a.kind==='BLOCKED'||a.kind==='UPDATE_BLOCKED';
    return `
      <div class="ops-anomaly-card ${isBlocked?'is-blocked':''}">
        <div class="ops-anomaly-head">
          <div style="display:flex;align-items:center;gap:8px">
            <span class="badge ${isBlocked?'blocked':'failed'}">${esc(a.kind)}</span>
            <strong>${esc(a.workflow_id||'未知工作流')}</strong>
          </div>
          <span class="task-meta">${formatElapsed(a.last_event_seconds||0)}前</span>
        </div>
        <div class="ops-anomaly-body">
          <div class="task-meta">阶段: <b>${esc(a.node||'—')}</b> · 执行者: <b>${esc(a.agent||'—')}</b> · 任务: <code>${esc(a.task_id||'—')}</code></div>
          <div class="ops-anomaly-event">最后事件: ${esc(a.last_event||'无详细事件记录')}</div>
        </div>
        <div class="ops-anomaly-actions">
          ${a.kind==='FAILED'?`<button class="btn mini primary" onclick="runOpsAnomalyAction('ops_repair','${esc(a.workflow_id||'')}','${esc(a.task_id||'')}','${esc(a.node||'')}',{expected_version:${a.version!==undefined&&a.version!==null?a.version:'null'},expected_pane_id:'${esc(a.pane_id||'')}',pane_id:'${esc(a.pane_id||'')}'})">一键重试修复</button>`:''}
          ${isBlocked?`<button class="btn mini warning" onclick="confirmOpsForcePass('${esc(a.workflow_id||'')}','${esc(a.task_id||'')}','${esc(a.node||'')}',{expected_version:${a.version!==undefined&&a.version!==null?a.version:'null'},expected_pane_id:'${esc(a.pane_id||'')}',pane_id:'${esc(a.pane_id||'')}'})">强制放行推进</button>`:''}
          ${a.pane_id?`<button class="btn mini" onclick="showPane('${esc(a.pane_id)}')">打开终端</button>`:''}
          ${a.task_id?`<button class="btn mini" onclick="openTaskDrawer('${esc(a.task_id)}')">任务详情</button>`:''}
          <button class="btn mini" onclick="showOpsAnomaly(${JSON.stringify(a).replaceAll('"','&quot;')})">更多处理 ···</button>
        </div>
      </div>
    `;
  }).join(''):`
    <div class="ops-empty-clean">
      <div class="ops-empty-icon">🟢</div>
      <div class="ops-empty-title">当前无异常阻断</div>
      <div class="ops-empty-sub">全集群工作流与执行者健康运行中</div>
    </div>
  `;

  const fleetHtml=fleet.length?`
    <div class="fleet-table">
      <div class="fleet-header">
        <span>执行者</span>
        <span>健康状态</span>
        <span>当前任务</span>
        <span>负载</span>
        <span>耗时</span>
      </div>
      ${fleet.map(a=>`
        <div class="fleet-row">
          <strong>${esc(a.agent)}</strong>
          <span class="${opsHealthClass(a.health)}">${esc(healthLabel(a.health))}</span>
          <span class="task-id" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${a.current_task?`<a onclick="openTaskDrawer('${esc(a.current_task)}')">${esc(a.current_task)}</a>`:'—'}</span>
          <span>负载 ${a.load}</span>
          <span>${formatElapsed(a.runtime_seconds)}</span>
        </div>
      `).join('')}
    </div>
  `:'<div class="empty">暂无执行者数据</div>';

  document.getElementById('tasks').innerHTML=`
    <div class="ops-dashboard-grid">
      <div class="ops-col-left">
        <div class="ops-section-head">
          <div class="ops-section-title">
            <span>工作流 / 阶段节点</span>
            <span class="ops-badge" id="opsWfCount">${cards.length}</span>
          </div>
          <div class="ops-filter-pills" role="tablist">
            <button class="ops-filter-pill ${state.opsWfFilter==='all'?'active':''}" onclick="filterOpsWorkflows('all')">全部 (${cards.length})</button>
            <button class="ops-filter-pill ${state.opsWfFilter==='active'?'active':''}" onclick="filterOpsWorkflows('active')">运行中 (${activeWfs})</button>
            <button class="ops-filter-pill ${state.opsWfFilter==='attention'?'active':''}" onclick="filterOpsWorkflows('attention')">需关注 (${attentionWfs})</button>
            <button class="ops-filter-pill ${state.opsWfFilter==='completed'?'active':''}" onclick="filterOpsWorkflows('completed')">已完成 (${completedWfs})</button>
          </div>
        </div>
        <div id="opsWorkflowCards" class="ops-cards-container">
          ${wfCardsHtml}
        </div>
      </div>
      <div class="ops-col-right">
        <div class="ops-panel-anomaly">
          <div class="ops-section-head">
            <div class="ops-section-title">
              <span class="ops-anomaly-icon">🚨</span>
              <span>异常与阻断中枢</span>
              <span class="ops-badge ${anoms.length?'danger':''}" id="opsAnomalyCount">${anoms.length}</span>
            </div>
          </div>
          <div id="opsAnomalyList" class="ops-anomaly-container">
            ${anomsHtml}
          </div>
        </div>
        <div class="ops-panel-fleet">
          <div class="ops-section-head">
            <div class="ops-section-title">
              <span>执行者舰队</span>
              <span class="ops-badge" id="opsFleetCount">${fleet.length}</span>
            </div>
          </div>
          <div id="opsFleetList" class="fleet-table-wrap">
            ${fleetHtml}
          </div>
        </div>
      </div>
    </div>
  `;

  document.querySelectorAll('.ops-card').forEach((el)=>{
    const wid=el.dataset.wfId;
    if(wid){
      el.style.cursor='pointer';
      el.onclick=()=>openWorkflowFromOps(wid);
    }
  });

  const alertsEl=document.getElementById('alerts');
  if(alertsEl)alertsEl.innerHTML=anoms.length?anoms.map(a=>`<div class="alert-row"><div><strong>${esc(a.kind)}</strong><div class="task-meta">${esc(a.task_id||'')} · ${esc(a.agent||'')} · ${esc(a.last_event||'')}</div></div><button class="mini" onclick="showOpsAnomaly(${JSON.stringify(a).replaceAll('"','&quot;')})">处理</button></div>`).join(''):'<div class="empty">暂无异常</div>';
}

function showOpsAnomaly(a){
  const wid=a.workflow_id||'';
  const tid=a.task_id||'';
  const node=a.node||'';
  const aOpts=`{expected_version:${a.version!==undefined&&a.version!==null?a.version:'null'},expected_pane_id:'${esc(a.pane_id||'')}',pane_id:'${esc(a.pane_id||'')}'}`;
  const actionsList=(a.actions||[]).map(x=>{
    let clickFn="toast('建议操作："+esc(x)+"')";
    if(x==='重试'||x==='自动修复'){
      clickFn="closeModal();runOpsAnomalyAction('ops_repair','"+esc(wid)+"','"+esc(tid)+"','"+esc(node)+"',"+aOpts+")";
    }else if(x==='打开 Pane'&&a.pane_id){
      clickFn="closeModal();showPane('"+esc(a.pane_id)+"')";
    }else if(x==='让总指挥处理'){
      clickFn="closeModal();runOpsAnomalyAction('advance','"+esc(wid)+"','"+esc(tid)+"','"+esc(node)+"',"+aOpts+")";
    }else if(x==='强制放行'||x==='人工放行'){
      clickFn="closeModal();confirmOpsForcePass('"+esc(wid)+"','"+esc(tid)+"','"+esc(node)+"',"+aOpts+")";
    }
    return `<button class="btn mini primary" onclick="${clickFn}">${esc(x)}</button>`;
  }).join('');

  openModal(a.kind,`
    <div class="task-meta">${esc(wid)} · ${esc(tid)} · ${esc(a.agent||'')} · ${esc(node)}</div>
    <div class="task-meta" style="margin:8px 0;background:var(--bg-subtle);padding:8px;border-radius:4px">最后事件：${esc(a.last_event||'—')}</div>
    <div style="font-size:12px;font-weight:600;margin:12px 0 8px">快捷处置动作：</div>
    <div class="actions" style="display:flex;gap:8px;flex-wrap:wrap">
      ${actionsList}
      ${tid?`<button class="btn mini" onclick="closeModal();openTaskDrawer('${esc(tid)}')">查看任务详情</button>`:''}
    </div>
    <div style="font-size:12px;font-weight:600;margin:16px 0 8px">CLI 诊断与修复命令：</div>
    <pre style="margin-top:4px;font-size:11.5px;max-height:160px;overflow:auto">${esc(JSON.stringify(a.links||{},null,2))}</pre>
  `);
}
let dashTimer=null;
function stopDashTimer(){if(dashTimer){clearInterval(dashTimer);dashTimer=null}}
let dashLinearKeyboardIdx = 0;

function toggleLinearSelectMenu(e){
  if(e) e.stopPropagation();
  const sel=document.getElementById('dashLinearSelect');
  if(!sel) return;
  const isOpen = sel.classList.toggle('open');
  if(isOpen){
    state.dashLinearSearch = '';
    dashLinearKeyboardIdx = 0;
    const inp = document.getElementById('dashLinearSearch');
    if(inp){ inp.value = ''; setTimeout(() => inp.focus(), 50); }
    renderLinearPopoverList();
  }
}

function closeLinearSelectMenu(){
  const sel=document.getElementById('dashLinearSelect');
  if(sel) sel.classList.remove('open');
}

function togglePopoverGroup(key, e){
  if(e) e.stopPropagation();
  if(!state.collapsedGroups) state.collapsedGroups = new Set();
  if(state.collapsedGroups.has(key)) state.collapsedGroups.delete(key);
  else state.collapsedGroups.add(key);
  renderLinearPopoverList();
}

function handleLinearSelectSearch(val){
  state.dashLinearSearch = (val || '').trim().toLowerCase();
  dashLinearKeyboardIdx = 0;
  renderLinearPopoverList();
}

function handleLinearSelectKeydown(e){
  const listEl=document.getElementById('dashLinearList');
  if(!listEl) return;
  const items = Array.from(listEl.querySelectorAll('.popover-item')).filter(it => !it.closest('.popover-group.collapsed'));
  if(!items.length) return;
  if(e.key==='ArrowDown'){
    e.preventDefault();
    dashLinearKeyboardIdx = (dashLinearKeyboardIdx + 1) % items.length;
    items.forEach((it, idx) => it.classList.toggle('keyboard-focus', idx === dashLinearKeyboardIdx));
    items[dashLinearKeyboardIdx].scrollIntoView({ block: 'nearest' });
  } else if(e.key==='ArrowUp'){
    e.preventDefault();
    dashLinearKeyboardIdx = (dashLinearKeyboardIdx - 1 + items.length) % items.length;
    items.forEach((it, idx) => it.classList.toggle('keyboard-focus', idx === dashLinearKeyboardIdx));
    items[dashLinearKeyboardIdx].scrollIntoView({ block: 'nearest' });
  } else if(e.key==='Enter'){
    e.preventDefault();
    if(items[dashLinearKeyboardIdx]) items[dashLinearKeyboardIdx].click();
  } else if(e.key==='Escape'){
    e.preventDefault();
    closeLinearSelectMenu();
    const btn = document.getElementById('dashLinearTrigger');
    if(btn) btn.focus();
  }
}

function selectLinearWorkflow(wid){
  state.dashWorkflowId = wid || null;
  const sel=document.getElementById('dashWfSel');
  if(sel) sel.value = wid || '';
  closeLinearSelectMenu();
  saveViewState();
  loadDashboard();
}

function renderLinearPopoverList(){
  const listEl = document.getElementById('dashLinearList');
  if(!listEl) return;
  const wfs = state.dashWorkflows || [];
  const cur = state.dashWorkflowId || '';
  const kw = state.dashLinearSearch || '';
  if(!state.collapsedGroups) state.collapsedGroups = new Set();

  const filtered = wfs.filter(w => {
    if(!kw) return true;
    return (w.title && w.title.toLowerCase().includes(kw)) ||
           (w.workflow_id && w.workflow_id.toLowerCase().includes(kw));
  });

  let html = '';
  if(!kw || '全部工作流'.includes(kw)){
    const isSel = !cur;
    html += `<div class="popover-item ${isSel ? 'selected' : ''}" onclick="selectLinearWorkflow('')" role="option">
      <span class="item-check"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg></span>
      <div class="item-main">
        <div class="item-header">
          <span class="item-title">全部工作流</span>
          <span class="item-badges"><span class="badge-pill nodot" style="background:var(--bg-subtle);color:var(--text-secondary)">聚合</span></span>
        </div>
        <div class="item-sub">查看所有活跃工作流的聚合看板</div>
      </div>
    </div><div style="height:1px;background:var(--border-subtle);margin:4px 0"></div>`;
  }

  if(!filtered.length){
    listEl.innerHTML = html + `<div class="popover-empty">未找到匹配的工作流 “${esc(kw)}”</div>`;
    return;
  }

  const activeGroup = filtered.filter(w => (w.active > 0 || w.attention > 0));
  const idleGroup = filtered.filter(w => (w.active === 0 && w.attention === 0));

  function renderGroup(key, label, items){
    if(!items.length) return '';
    const isCollapsed = !kw && state.collapsedGroups.has(key);
    const chevronSvg = `<svg class="popover-group-chevron" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 9 6 6 6-6"/></svg>`;
    let gHtml = `<div class="popover-group ${isCollapsed ? 'collapsed' : ''}" data-group="${key}">
      <div class="popover-group-header" onclick="togglePopoverGroup('${key}', event)">
        <span class="popover-group-title">${label} (${items.length})</span>
        ${chevronSvg}
      </div>
      <div class="popover-group-items">`;
    items.forEach(w => {
      const isSel = (w.workflow_id === cur);
      const title = w.title || w.workflow_id;
      let pills = '';
      if(w.active > 0) pills += `<span class="badge-pill active">${w.active} 活跃</span>`;
      if(w.attention > 0) pills += `<span class="badge-pill attention">${w.attention} 需决策</span>`;
      if(!pills) pills = `<span class="badge-pill completed">已完成</span>`;

      function hl(txt){
        if(!kw || !txt) return esc(txt);
        const re = new RegExp(`(${kw.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&')})`, 'gi');
        return esc(txt).replace(re, '<span class="highlight">$1</span>');
      }

      gHtml += `<div class="popover-item ${isSel ? 'selected' : ''}" onclick="selectLinearWorkflow(${jsArg(w.workflow_id)})" role="option">
        <span class="item-check"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg></span>
        <div class="item-main">
          <div class="item-header">
            <span class="item-title">${hl(title)}</span>
            <span class="item-badges">${pills}</span>
          </div>
          <div class="item-sub">
            <span class="item-id">${hl(w.workflow_id)}</span>
          </div>
        </div>
      </div>`;
    });
    gHtml += `</div></div>`;
    return gHtml;
  }

  html += renderGroup('active', '进行中 / 需决策', activeGroup);
  html += renderGroup('idle', '已完成 / 闲置', idleGroup);
  listEl.innerHTML = html;

  const renderedItems = Array.from(listEl.querySelectorAll('.popover-item')).filter(it => !it.closest('.popover-group.collapsed'));
  renderedItems.forEach((it, idx) => it.classList.toggle('keyboard-focus', idx === dashLinearKeyboardIdx));
}

function updateLinearWorkflowTrigger(workflows, curWid){
  const contentEl = document.getElementById('dashLinearTriggerContent');
  if(!contentEl) return;
  if(!curWid){
    contentEl.innerHTML = `<span class="trigger-title">全部工作流</span>`;
    return;
  }
  const cw = (workflows || []).find(w => w.workflow_id === curWid);
  if(!cw){
    contentEl.innerHTML = `<span class="trigger-title">${esc(curWid)}（已归档或未知）</span><span class="trigger-mono">${esc(curWid)}</span>`;
    return;
  }
  const title = cw.title || cw.workflow_id;
  let pills = '';
  if(cw.active > 0) pills += `<span class="badge-pill active">${cw.active} 活跃</span>`;
  if(cw.attention > 0) pills += `<span class="badge-pill attention">${cw.attention} 需决策</span>`;

  contentEl.innerHTML = `<span class="trigger-title">${esc(title)}</span>`
    + `<span class="trigger-mono">${esc(cw.workflow_id)}</span>`
    + (pills ? `<span class="trigger-pills">${pills}</span>` : '');
}

function showDashboard(){
  setWorkspaceMode('aux');
  state.dashMode=true;state.opsMode=false;state.shellView='dashboard';saveViewState();syncOpsUi();stopDashTimer();
  document.getElementById('projectTitle').textContent='我的仪表板';
  document.getElementById('workflowSubject').textContent='任务 · 工位实时 · 等你决策 · 每 10 秒自动刷新';
  document.getElementById('workflowSub').textContent='';
  const sw=document.getElementById('workflowSwitcher');
  delete sw.dataset.sig;
  sw.style.display='flex';
  sw.innerHTML='<label>工作流</label>'
    +'<div class="linear-select" id="dashLinearSelect">'
      +'<button class="linear-trigger" id="dashLinearTrigger" onclick="toggleLinearSelectMenu(event)" aria-haspopup="listbox">'
        +'<span class="trigger-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="6" y1="3" x2="6" y2="15"></line><circle cx="18" cy="6" r="3"></circle><circle cx="6" cy="18" r="3"></circle><path d="M18 9a9 9 0 0 1-9 9"></path></svg></span>'
        +'<div class="trigger-content" id="dashLinearTriggerContent"><span class="trigger-title">全部工作流</span></div>'
        +'<span class="trigger-chevron"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 9 6 6 6-6"/></svg></span>'
      +'</button>'
      +'<div class="linear-popover" id="dashLinearPopover" onclick="event.stopPropagation()">'
        +'<div class="popover-search-wrap">'
          +'<span class="popover-search-icon"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg></span>'
          +'<input type="text" class="popover-search-input" id="dashLinearSearch" placeholder="搜索工作流名称、ID (如 0929 / 待办)..." autocomplete="off" oninput="handleLinearSelectSearch(this.value)" onkeydown="handleLinearSelectKeydown(event)" />'
          +'<span class="popover-shortcut">ESC</span>'
        +'</div>'
        +'<div class="popover-list" id="dashLinearList" role="listbox"></div>'
      +'</div>'
      +'<select id="dashWfSel" style="display:none" onchange="dashSelectWorkflow(this.value)"><option value="">全部工作流</option></select>'
    +'</div>'
    +'<button class="btn primary" onclick="dashOpenWorkflow()">进入该工作流</button>';
  document.getElementById('stages').innerHTML='';
  document.getElementById('attentionBanner').style.display='none';
  document.getElementById('tasks').innerHTML='<div class="empty">正在加载仪表板…</div>';
  paintCrumb();
  loadDashboard();
  dashTimer=setInterval(()=>{if(state.dashMode&&!document.hidden)loadDashboard()},10000);
}
async function exitDashboard(){
  state.dashMode=false;state.shellView='workbench';saveViewState();syncOpsUi();stopDashTimer();await refreshAll()
}
async function loadDashboard(){
  try{
    const qs=state.dashWorkflowId?('?workflow_id='+encodeURIComponent(state.dashWorkflowId)):'';
    const d=await api('/api/dashboard'+qs);
    if(!state.dashMode)return;
    state.dash=d;renderDashboard();
  }catch(e){
    if(state.dashMode){
      toast(e.message,true);
      const el=document.getElementById('tasks');
      if(el)el.innerHTML=`<div class="panel" style="padding:32px 20px;text-align:center;margin:16px"><div style="font-size:16px;font-weight:700;margin-bottom:8px">仪表板数据加载失败</div><div class="task-meta" style="margin-bottom:16px">${esc(e.message)}</div><button class="btn primary" onclick="loadDashboard()">点击重试</button></div>`;
    }
  }
}
function dashSelectWorkflow(v){state.dashWorkflowId=v||null;saveViewState();loadDashboard()}
async function dashOpenWorkflowOf(wid){
  if(!wid)return dashOpenWorkflow();
  state.dashWorkflowId=wid;
  await dashOpenWorkflow();
  openControllerCockpitModal();
}
async function dashOpenWorkflow(){
  const wid=state.dashWorkflowId;
  if(!wid)return toast('请先在下拉框选择一个工作流',true);
  state.workflowId=wid;state.dashMode=false;state.shellView='workbench';state.dashWorkflowId=null;stopDashTimer();saveViewState();syncOpsUi();
  try{await loadWorkflow(wid)}catch(e){toast(e.message,true)}
  await refreshAll();
}
function dashPillFor(t){
  const s=t.status||'';
  if(s==='cleaned'||s==='completed'||s==='committed'||s==='integrated')return '<span class="badge cleaned">已完成</span>';
  if(s==='working'||s==='dispatched'||s==='agent_done'||s==='rework')return '<span class="badge working">'+esc(t.status_text)+'</span>';
  if(s==='blocked'||s==='failed')return '<span class="badge blocked">'+esc(t.status_text)+'</span>';
  return '<span class="badge waiting">'+esc(t.status_text)+'</span>';
}
function renderDashboard(){
  const d=state.dash||{tasks:[],attention:[],deliveries:[],stuck:[],counts:{tasks:0,attention:0,deliveries:0,stuck:0},generated_at_text:'—',scope:'all',workflows:[]};
  const doneCount=d.tasks.filter(t=>['cleaned','completed','committed','integrated'].indexOf(t.status)>=0).length;
  const workT=d.tasks.filter(t=>['working','dispatched','agent_done','rework'].indexOf(t.status)>=0)[0];
  const bad=d.attention.length+d.stuck.length;
  document.getElementById('mProjects').textContent=d.counts.tasks;
  document.getElementById('mWorkflows').textContent=d.tasks.filter(t=>['working','dispatched','agent_done','rework','blocked','failed'].indexOf(t.status)>=0).length;
  document.getElementById('mAgents').textContent=d.attention.length;
  document.getElementById('mAlerts').textContent=d.stuck.length;
  const mSpans=document.querySelectorAll('.metrics .metric span');
  if(mSpans[0])mSpans[0].textContent='任务总数';if(mSpans[1])mSpans[1].textContent='活跃任务';if(mSpans[2])mSpans[2].textContent='等你决策';if(mSpans[3])mSpans[3].textContent='卡住';
  const sel=document.getElementById('dashWfSel');
  if(sel){
    const cur=state.dashWorkflowId||'';
    sel.innerHTML='<option value="">全部工作流</option>'+(d.workflows||[]).map(w=>'<option value="'+esc(w.workflow_id)+'"'+(w.workflow_id===cur?' selected':'')+'>'+esc(w.title||w.workflow_id)+'（活 '+w.active+' / 等 '+w.attention+'）</option>').join('');
    if(cur&&!(d.workflows||[]).some(w=>w.workflow_id===cur)){const o=document.createElement('option');o.value=cur;o.textContent=cur+'（已归档或未知）';o.selected=true;sel.appendChild(o)}
    sel.value=cur;
    state.dashWorkflows = d.workflows || [];
    updateLinearWorkflowTrigger(d.workflows || [], cur);
    renderLinearPopoverList();
  }
  const kpis='<div class="dash-kpis">'
    +'<div class="dash-kpi"><div class="k">任务完成</div><div class="v">'+doneCount+' <small>/ '+d.counts.tasks+'</small></div></div>'
    +'<div class="dash-kpi"><div class="k">进行中</div><div class="v" style="font-size:15px">'+esc(workT?workT.task_id:'—')+'</div><div class="s">'+esc(workT?(workT.agent+' · 工位 '+(workT.pane_id||'—')):'当前无执行中任务')+'</div></div>'
    +'<div class="dash-kpi"><div class="k">等你的问题</div><div class="v'+(d.attention.length?' warn':'')+'">'+d.attention.length+' <small>个</small></div><div class="s">默认动作已就绪</div></div>'
    +'<div class="dash-kpi"><div class="k">卡住</div><div class="v'+(d.stuck.length?' warn':'')+'">'+d.stuck.length+'</div><div class="s">'+(d.stuck.length?'需要处理':'Clear')+'</div></div>'
    +'<div class="dash-kpi"><div class="k">交付物</div><div class="v">'+d.deliveries.length+'</div><div class="s">最新有效交付</div></div></div>';
  const taskRows=d.tasks.length?d.tasks.map((t,i)=>'<div class="dash-task"><span class="n">'+(i+1)+'</span><div><div class="t">'+esc(t.task_id)+'</div><div class="d">'+esc(t.goal||t.node)+'</div><div class="m">工位 '+esc(t.pane_id||'—')+' · 执行者 '+esc(t.agent)+(t.runtime_status?(' · '+esc(t.runtime_status)):'')+' · 更新于 '+esc(t.updated_at_text)+'</div></div><div>'+dashPillFor(t)+'</div></div>').join(''):'<div class="empty">当前没有任务</div>';
  const attRows=d.attention.length?d.attention.map(a=>{
    if(a.decision_id)return '<div class="dash-q"><div class="qt"><span>待你裁决 · '+esc(a.decision_id)+'</span><span class="dash-default">'+esc(a.default_action)+'</span></div><div class="qd"><b>说明</b><span>'+esc(a.reason||a.default_action_text)+'</span></div><div class="qm">'+esc(a.workflow_id)+' · '+esc(a.node||'—')+' · '+esc(a.updated_at_text)+'</div><div class="dash-btns"><button class="btn primary" onclick="dashOpenWorkflowOf('+jsArg(a.workflow_id)+')">进入工作流裁决</button></div></div>';
    return '<div class="dash-q"><div class="qt"><span>'+esc(a.reason||a.task_id)+'</span><span class="dash-default">默认: '+esc(a.default_action)+'</span></div><div class="qd"><b>说明</b><span>'+esc(a.default_action_text)+'</span></div><div class="qm">'+esc(a.task_id)+' · 工位 '+esc(a.pane_id||'—')+' · '+esc(a.workflow_id)+' · '+esc(a.updated_at_text)+'</div><div class="dash-btns"><button class="btn primary" onclick="openSignoffChamber('+JSON.stringify(a.task_id)+')">会签放行</button><button class="btn danger-btn" onclick="dashSignoff('+JSON.stringify(a.task_id)+','+JSON.stringify(a.workflow_id)+','+JSON.stringify(a.node||'')+',\'reject\')">打回</button>'+(a.endpoint?'<button class="btn" onclick="dashExec('+JSON.stringify(a.endpoint)+','+JSON.stringify(a.payload||{}).replace(/</g,'\\u003c')+')">一键：'+esc(a.default_action)+'</button>':'')+'</div></div>';
  }).join(''):'<div class="dash-ok"><span class="dash-dot green"></span>没有等你的问题</div>';
  const decRows=(d.decisions||[]).length?d.decisions.map(x=>'<div class="dash-q"><div class="qt"><span>'+esc(x.decision_id)+'</span><span class="dash-default">'+esc(x.node||'—')+'</span></div><div class="qd"><b>问题</b><span>'+esc(x.question||x.title)+'</span></div>'+((x.options||[]).length?'<div class="qm">可选：'+esc((x.options||[]).join(' / '))+(x.recommended?('（建议：'+esc(x.recommended)+'）'):'')+'</div>':'')+'<div class="qm">'+esc(x.workflow_id)+' · '+esc(x.raised_at_text)+'</div><div class="dash-btns"><button class="btn primary" onclick="dashOpenWorkflowOf('+jsArg(x.workflow_id)+')">去拍板</button></div></div>').join(''):'<div class="dash-ok"><span class="dash-dot green"></span>没有待裁决项</div>';
  const dlvRows=d.deliveries.length?d.deliveries.map(x=>'<div class="dash-task"><span class="n">✓</span><div><div class="t">'+esc(x.title||x.candidate_sha)+'</div><div class="m">'+esc(x.workflow_id)+' · '+esc(x.updated_at_text)+'</div></div><div><span class="badge cleaned">交付</span></div></div>').join(''):'<div class="empty">暂无交付物</div>';
  const stuckRows=d.stuck.length?d.stuck.map(s=>'<div class="dash-task"><span class="n">!</span><div><div class="t">'+esc(s.task_id||s.workflow_id)+'</div><div class="d">'+esc(s.reason)+'</div><div class="m">'+esc(s.workflow_id)+' · '+esc(s.last_event_text||'')+'</div></div><div><span class="badge blocked">卡住</span></div></div>').join(''):'<div class="dash-ok"><span class="dash-dot green"></span>一切正常，没有卡住</div>';
  document.getElementById('tasks').innerHTML=kpis
    +'<div class="dash-layout"><div class="dash-main">'
    +'<div class="dash-sec"><div class="dash-sec-h"><span>任务及其状态</span><span class="count">'+d.tasks.length+' / '+d.counts.tasks+'</span></div>'+taskRows+'</div>'
    +'<div class="dash-sec"><div class="dash-sec-h"><span>最新交付物</span><span class="count">'+d.deliveries.length+' 个</span></div>'+dlvRows+'</div>'
    +'</div><div class="dash-rail">'
    +'<div class="dash-sec"><div class="dash-sec-h"><span>待你裁决</span><span class="count">'+(d.decisions||[]).length+' 个</span></div>'+decRows+'</div>'
    +'<div class="dash-sec"><div class="dash-sec-h"><span>等你的问题</span><span class="count">'+d.attention.length+' 个</span></div>'+attRows+'</div>'
    +'<div class="dash-sec"><div class="dash-sec-h"><span>卡住的东西</span><span class="count">'+d.stuck.length+' 项</span></div>'+stuckRows+'</div>'
    +'</div></div>';
  document.getElementById('agents').innerHTML='<div class="empty">工位实时已合并到任务列表。</div>';
  document.getElementById('slots').innerHTML='<div class="empty">点击任务可进入详情。</div>';
  document.getElementById('alerts').innerHTML='<div class="empty">卡住事项见上方。</div>';
}
async function dashSignoff(taskId,wid,node,act){if(act==='approve'){return openSignoffChamber(taskId)}try{await api('/api/task/signoff',{method:'POST',body:JSON.stringify({task_id:taskId,workflow_id:wid,node:node,action:act,operator:'仪表板'})});await loadDashboard()}catch(e){toast('操作失败：'+e.message,true)}}
async function dashExec(endpoint,payload){try{await api(endpoint||'/api/controller/execute-action',{method:'POST',body:JSON.stringify(payload||{})});await loadDashboard()}catch(e){toast('操作失败：'+e.message,true)}}
async function openWorkflowFromOps(id){
  stopOpsTimer();
  state.opsMode=false;state.dashMode=false;state.shellView='workbench';stopDashTimer();
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
    startOpsTimer();
    await loadOpsCenter()
  }
}

async function refreshAll(){
  try{
    syncOpsUi();
    if(state.dashMode){await loadDashboard();return}
    if(state.opsMode){await loadOpsCenter();return}
    state.overview=await api('/api/overview');
    if(state.opsMode){await loadOpsCenter();return}
    const ss=state.overview.spaces||[];

    if(!state.spaceId||!ss.some(s=>s.workspace_id===state.spaceId)){
      const preferred=ss.find(s=>s.relation==='current_factory')||ss[0];
      state.spaceId=preferred?preferred.workspace_id:null
    }

    renderOverview();
    if(state.spaceId)await selectSpace(state.spaceId,false);
    if(!state.opsMode&&!state.dashMode&&state.shellView&&state.shellView!=='workbench')paintShellPage(state.shellView);
    else paintCrumb();
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
      <small class="project-path">${esc(s.project_root||'未识别项目目录')}</small>
    </button>`).join(''):'<div class="empty">暂无项目空间</div>';

  document.getElementById('alerts').innerHTML=alerts.length?alerts.map(a=>`
    <div class="alert-row">
      <div>
        <div class="task-name">${esc(taskDisplayName(a))}</div>
        <div class="task-id">${esc(a.task_id||'')}</div>
        <div class="task-meta">${esc(a.agent||'')} · ${esc(a.pane_id||'')}</div>
      </div>
      ${badge(a.status)}
    </div>`).join(''):'<div class="empty">当前项目 / 工作流暂无告警</div>';
  paintCrumb();
}

async function selectSpace(workspaceId,rer=true){
  closeSpaceMenu();
  const s=(state.overview.spaces||[]).find(x=>x.workspace_id===workspaceId);
  if(!s)return;

  state.spaceId=workspaceId;
  state.space=s;

  if(s.relation==='current_factory'&&s.project_id){
    state.projectId=s.project_id;
    await loadProject(s.project_id,false);
    document.getElementById('projectTitle').textContent=s.label||s.project_name||s.workspace_id;
  }else{
    destroyFlowGraph();
    state.project=null;
    state.projectId=s.project_id||null;
    state.workflow=null;
    state.workflowId=null;
    state.flowSelectedNodeId=null;
    setWorkspaceMode('aux');

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
  paintCrumb();
  saveViewState()
}

async function loadProject(id,rer=true){
  state.projectId=id;
  state.project=await api('/api/project?id='+encodeURIComponent(id));
  if(rer&&state.overview)renderOverview();
  document.getElementById('projectTitle').textContent=state.project.project.project_name;
  const w=state.project.workflows;
  const isSysTab=state.workflowId&&(state.workflowId.startsWith('__')||['__ctl__','__templates__','__archive__','__logs__'].includes(state.workflowId));
  if(!isSysTab&&(!state.workflowId||!w.some(x=>x.workflow_id===state.workflowId)))state.workflowId=state.project.latest_workflow_id;
  if(!state.openWorkflowTabIds)state.openWorkflowTabIds=[];
  if(state.workflowId&&!state.openWorkflowTabIds.includes(state.workflowId)){
    state.openWorkflowTabIds.push(state.workflowId);
  }
  renderAgents();
  renderSlots();
  renderWorkflowSwitcher();
  renderWorkflowTabs();
  if(isSysTab){
    paintCrumb();
  }else if(state.workflowId){
    await loadWorkflow(state.workflowId);
  }else{
    clearWorkflow();
  }
}
function workflowSubject(w){return (w&&(w.title||w.requirement_subject))||''}
function workflowDisplayName(w){
  if(!w)return '';
  const subj=workflowSubject(w);
  return subj?`${subj} (${w.workflow_id})`:w.workflow_id;
}
function copyWorkflowId(wid){
  const id=wid||state.workflowId;
  if(!id||id.startsWith('__'))return;
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(id).then(()=>{
      toast('已复制工作流编号: '+id);
    }).catch(()=>{
      toast('复制失败，请手动复制: '+id, true);
    });
  }else{
    toast('工作流编号: '+id);
  }
}
async function editWorkflowTitle(wid){
  const id=wid||state.workflowId;
  if(!id||id.startsWith('__'))return toast('当前工作流不可重命名',true);
  const w=(state.workflow&&state.workflow.workflow&&state.workflow.workflow.workflow_id===id)?state.workflow.workflow:((state.project&&state.project.workflows)||[]).find(x=>x.workflow_id===id);
  const curTitle=w?(w.title||w.requirement_subject||''):'';
  showPromptModal({
    title:'修改工作流名称',
    label:'工作流名称 / 业务主题',
    defaultValue:curTitle,
    confirmText:'保存',
    onConfirm:async(newTitle)=>{
      const trimmed=(newTitle||'').trim();
      if(trimmed===curTitle)return;
      try{
        await api('/api/workflow/rename',{
          method:'POST',
          body:JSON.stringify({workflow_id:id,title:trimmed})
        });
        toast('工作流名称已更新');
        if(state.workflow&&state.workflow.workflow&&state.workflow.workflow.workflow_id===id){
          state.workflow.workflow.title=trimmed;
          state.workflow.workflow.requirement_subject=trimmed;
        }
        if(state.project&&state.project.workflows){
          const target=state.project.workflows.find(x=>x.workflow_id===id);
          if(target){
            target.title=trimmed;
            target.requirement_subject=trimmed;
          }
        }
        if(state.workflow&&state.workflow.workflow&&state.workflow.workflow.workflow_id===id){
          renderWorkflowHead(state.workflow.workflow);
        }
        renderWorkflowTabs();
        if(typeof renderSidebarWorkflows==='function')renderSidebarWorkflows();
      }catch(err){
        toast('更新工作流名称失败: '+(err.message||err),true);
      }
    }
  });
}
function renderWorkflowMetaBadge(w){
  const box=document.getElementById('canvasWfMeta');
  if(!box)return;
  if(!w||!w.workflow_id||String(w.workflow_id).startsWith('__')){
    box.style.display='none';
    box.innerHTML='';
    return;
  }
  const wid=w.workflow_id;
  const subj=workflowSubject(w)||wid;
  box.style.display='inline-flex';
  box.innerHTML=`
    <div class="canvas-wf-title-wrap">
      <span class="canvas-wf-title" onclick="editWorkflowTitle('${esc(wid)}')" title="点击修改工作流名称">${esc(subj)}</span>
      <button class="canvas-wf-edit-btn" onclick="editWorkflowTitle('${esc(wid)}')" title="修改工作流名称" aria-label="修改工作流名称">
        <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path>
          <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path>
        </svg>
      </button>
    </div>
    <span class="canvas-wf-id-pill" onclick="copyWorkflowId('${esc(wid)}')" title="点击复制工作流编号 (${esc(wid)})">
      <span>${esc(wid)}</span>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
      </svg>
    </span>
  `;
}
function renderWorkflowHead(w){
  const subj=workflowSubject(w);
  document.getElementById('workflowSubject').textContent=subj||w.workflow_id;
  const a=state.workflow.agent_override||'auto';
  const parts=[];
  parts.push('工作流 '+w.workflow_id);
  parts.push('执行者 '+(a==='auto'?'自动分配':a));
  if(w.candidate_branch)parts.push('候选分支 '+w.candidate_branch);
  document.getElementById('workflowSub').textContent=parts.join(' · ');
  renderWorkflowMetaBadge(w);
  paintCrumb();
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
async function loadWorkflow(id){
  const prevWfId=state.flowGraphWfId||state.workflowId;
  const loadSequence=state.workflowLoadSequence=(state.workflowLoadSequence||0)+1;
  const refreshingController=state.workflowId==='__ctl__'&&state.workflow?.workflow?.workflow_id===id;
  if(!refreshingController)state.workflowId=id;
  state.flowSelectedNodeId=null;
  if(!state.openWorkflowTabIds)state.openWorkflowTabIds=[];
  if(id&&!state.openWorkflowTabIds.includes(id))state.openWorkflowTabIds.push(id);
  const _fetchDecs=()=>api('/api/workflow/decisions?workflow_id='+encodeURIComponent(id));
  const [wfRes,actionsRes,decisionsRes]=await Promise.all([
    api('/api/workflow?id='+encodeURIComponent(id)),
    api('/api/workflow/controller-actions?workflow_id='+encodeURIComponent(id)).catch(()=>null),
    _fetchDecs().catch(()=>null)
  ]);
  if(loadSequence!==state.workflowLoadSequence)return;
  const keepController=state.workflowId==='__ctl__'&&state.workflow?.workflow?.workflow_id===id;
  state.workflow=wfRes;
  state.controllerActionsData=actionsRes;
  state.decisionData=decisionsRes;
  if(false){state.decisionData=await api('/api/workflow/decisions?workflow_id='+encodeURIComponent(id));}
  const w=state.workflow.workflow;
  saveViewState();
  renderWorkflowTabs();
  renderWorkflowHead(w);
  renderStages();
  renderTasks();
  if(prevWfId!==id){
    destroyFlowGraph();
  }
  if(keepController)openControllerCockpitModal();else renderFlowWorkbench();
  if(typeof renderSidebarWorkflows==='function')renderSidebarWorkflows();
}
function clearWorkflow(){
  destroyFlowGraph();
  state.workflow=null;
  state.workflowId=null;
  state.flowSelectedNodeId=null;
  saveViewState();
  renderWorkflowTabs();
  renderWorkflowMetaBadge(null);
  document.getElementById('workflowSubject').textContent='暂无工作流';
  document.getElementById('workflowSub').textContent='';
  document.getElementById('stages').innerHTML='';
  const ab=document.getElementById('attentionBanner');if(ab)ab.style.display='none';
  setWorkspaceMode('aux');
  document.getElementById('tasks').innerHTML='<div class="empty">暂无任务</div>';
  paintCrumb();
  if(typeof renderSidebarWorkflows==='function')renderSidebarWorkflows();
}
function toggleSidebar(){
  state.sidebarCollapsed = !state.sidebarCollapsed;
  const sb = document.getElementById('sidebar');
  const shell = document.querySelector('.shell');
  const expandBtn = document.getElementById('btnExpandSidebar');
  if(state.sidebarCollapsed){
    if(sb)sb.classList.add('collapsed');
    if(shell)shell.classList.add('sidebar-collapsed');
    if(expandBtn)expandBtn.classList.add('visible');
    toast('侧边栏已收起 (⌘B)');
  } else {
    if(sb)sb.classList.remove('collapsed');
    if(shell)shell.classList.remove('sidebar-collapsed');
    if(expandBtn)expandBtn.classList.remove('visible');
    toast('侧边栏已展开 (⌘B)');
  }
}
function renderSidebarWorkflows(){
  const container = document.getElementById('sidebarWorkflowGroups');
  if(!container) return;
  const ws = (state.project && state.project.workflows) || [];
  const totalEl = document.getElementById('sidebarWfTotalCount');
  if(totalEl) totalEl.textContent = String(ws.length);

  if(!ws.length){
    container.innerHTML = '<div style="padding:12px 8px;font-size:11.5px;color:#8b909a;text-align:center">当前空间暂无工作流</div>';
    container.dataset.sig = '';
    return;
  }

  const listSig = JSON.stringify([
    Boolean(state.completedExpanded),
    state.completedVisibleLimit || 5,
    ws.map(w => [w.workflow_id, w.status||'waiting', w.progress||0, workflowSubject(w)||w.workflow_id])
  ]);
  if(container.dataset.sig === listSig) {
    container.querySelectorAll('.sidebar-item[data-wf-id]').forEach(el => {
      const wid = el.getAttribute('data-wf-id');
      el.classList.toggle('active', wid === state.workflowId);
    });
    return;
  }
  container.dataset.sig = listSig;

  const blocked = [];
  const running = [];
  const completed = [];

  ws.forEach(w => {
    const st = w.status || 'waiting';
    if(st === 'blocked' || st === 'decision' || st === 'failed'){
      blocked.push(w);
    } else if(st === 'completed' || st === 'cleaned' || st === 'archived'){
      completed.push(w);
    } else {
      running.push(w);
    }
  });

  let html = '';

  if(blocked.length > 0){
    html += `<div class="sidebar-subgroup-title">待拍板 / 阻塞 <span class="subgroup-badge warning">${blocked.length}</span></div>`;
    blocked.forEach(w => {
      const isActive = w.workflow_id === state.workflowId;
      const title = (typeof workflowSubject==='function'?workflowSubject(w):'') || w.title || w.workflow_id;
      html += `
        <div class="sidebar-item ${isActive ? 'active' : ''}" data-wf-id="${esc(w.workflow_id)}" onclick="openWorkflowTab('${esc(w.workflow_id)}')" title="${esc(title)} (${esc(w.workflow_id)})">
          <span class="status-dot decision"></span>
          <span class="item-text">${esc(title)}</span>
          <span class="item-meta warning-text">需决策</span>
        </div>`;
    });
  }

  if(running.length > 0){
    html += `<div class="sidebar-subgroup-title" style="${blocked.length > 0 ? 'margin-top:8px' : ''}">进行中 <span class="subgroup-badge running">${running.length}</span></div>`;
    running.forEach(w => {
      const isActive = w.workflow_id === state.workflowId;
      const title = (typeof workflowSubject==='function'?workflowSubject(w):'') || w.title || w.workflow_id;
      const meta = w.progress ? `${w.progress}%` : (w.status === 'working' ? '进行中' : '就绪');
      html += `
        <div class="sidebar-item ${isActive ? 'active' : ''}" data-wf-id="${esc(w.workflow_id)}" onclick="openWorkflowTab('${esc(w.workflow_id)}')" title="${esc(title)} (${esc(w.workflow_id)})">
          <span class="status-dot running"></span>
          <span class="item-text">${esc(title)}</span>
          <span class="item-meta tabular-nums">${esc(meta)}</span>
        </div>`;
    });
  }

  if(completed.length > 0){
    const limit = state.completedVisibleLimit || 5;
    const isExpanded = !!state.completedExpanded;
    let visibleCompleted = completed;
    let hasHidden = false;

    if(!isExpanded && completed.length > limit){
      hasHidden = true;
      visibleCompleted = completed.slice(0, limit);
      // 若当前选中的历史任务在截断范围之外，将其加入可见列表，确保选中项高亮可定位
      if(state.workflowId){
        const activeIdx = completed.findIndex(w => w.workflow_id === state.workflowId);
        if(activeIdx >= limit){
          visibleCompleted = visibleCompleted.concat([completed[activeIdx]]);
        }
      }
    }

    const titleStyle = (blocked.length > 0 || running.length > 0) ? 'margin-top:8px' : '';
    html += `
      <div class="sidebar-subgroup-title collapsible ${!isExpanded && completed.length > limit ? '' : ''}"
           style="${titleStyle}"
           onclick="toggleCompletedExpanded()"
           title="${isExpanded ? '点击折叠收起更多历史任务' : '点击展开全部历史任务'}">
        <span class="sidebar-subgroup-left">
          <span>历史完成</span>
          <span class="sidebar-subgroup-arrow">${isExpanded ? '▼' : '▶'}</span>
        </span>
        <span class="subgroup-badge completed">${completed.length}</span>
      </div>`;

    visibleCompleted.forEach(w => {
      const isActive = w.workflow_id === state.workflowId;
      const title = (typeof workflowSubject==='function'?workflowSubject(w):'') || w.title || w.workflow_id;
      html += `
        <div class="sidebar-item ${isActive ? 'active' : ''}" data-wf-id="${esc(w.workflow_id)}" onclick="openWorkflowTab('${esc(w.workflow_id)}')" title="${esc(title)} (${esc(w.workflow_id)})">
          <span class="status-dot completed"></span>
          <span class="item-text">${esc(title)}</span>
          <span class="item-meta">已完成</span>
        </div>`;
    });

    if(completed.length > limit){
      if(!isExpanded){
        const remainingCount = completed.length - visibleCompleted.length;
        html += `
          <button type="button" class="sidebar-completed-toggle" onclick="toggleCompletedExpanded()" title="展开所有历史完成任务">
            展开更多 (${remainingCount > 0 ? '剩余 ' + remainingCount + ' 项' : '全部'})
          </button>`;
      } else {
        html += `
          <button type="button" class="sidebar-completed-toggle" onclick="toggleCompletedExpanded()" title="收起超出的历史完成任务">
            收起历史任务 (仅显示前 ${limit} 项)
          </button>`;
      }
    }
  }

  container.innerHTML = html;
}
function toggleCompletedExpanded(){
  state.completedExpanded = !state.completedExpanded;
  const container = document.getElementById('sidebarWorkflowGroups');
  if(container) container.dataset.sig = '';
  renderSidebarWorkflows();
}
function openWorkflowTab(id){
  if(!id)return;
  if(!state.openWorkflowTabIds)state.openWorkflowTabIds=[];
  if(!state.openWorkflowTabIds.includes(id))state.openWorkflowTabIds.push(id);
  state.workflowId=id;
  if(typeof renderSidebarWorkflows==='function')renderSidebarWorkflows();
  if(typeof renderWorkflowTabs==='function')renderWorkflowTabs();
  if(id==='__ctl__'){if(typeof setWorkspaceMode==='function')setWorkspaceMode('ctl');if(typeof paintCrumb==='function')paintCrumb();if(typeof openControllerCockpitModal==='function')openControllerCockpitModal();return;}
  if(id==='__templates__'){if(typeof setWorkspaceMode==='function')setWorkspaceMode('templates');if(typeof paintCrumb==='function')paintCrumb();if(typeof showTemplateLibrary==='function')showTemplateLibrary();return;}
  if(id==='__archive__'){if(typeof setWorkspaceMode==='function')setWorkspaceMode('archive');if(typeof paintCrumb==='function')paintCrumb();if(typeof showArchive==='function')showArchive();return;}
  if(id==='__logs__'){if(typeof setWorkspaceMode==='function')setWorkspaceMode('logs');if(typeof paintCrumb==='function')paintCrumb();if(typeof showLogs==='function')showLogs();return;}
  state.shellView='workbench';
  state.workflowView='flow';
  saveViewState();
  loadWorkflow(id);
}
function closeWorkflowTab(id,e){
  if(e){e.stopPropagation();if(e.preventDefault)e.preventDefault();}
  if(!state.openWorkflowTabIds)return;
  const idx=state.openWorkflowTabIds.indexOf(id);
  if(idx===-1)return;
  state.openWorkflowTabIds.splice(idx,1);
  if(id==='__ctl__'){const ctlP=document.getElementById('controllerTabView');if(ctlP)ctlP.hidden=true;}
  if(id==='__templates__'){const tplP=document.getElementById('templatesTabView');if(tplP)tplP.hidden=true;}
  if(id==='__archive__'){const arcP=document.getElementById('archiveTabView');if(arcP)arcP.hidden=true;}
  if(id==='__logs__'){const logP=document.getElementById('logsTabView');if(logP)logP.hidden=true;if(typeof logState!=='undefined'&&logState.autoRefreshTimer){clearInterval(logState.autoRefreshTimer);logState.autoRefreshTimer=null;}}
  if(state.workflowId===id){
    if(state.openWorkflowTabIds.length>0){
      const nextIdx=Math.min(idx,state.openWorkflowTabIds.length-1);
      const nextId=state.openWorkflowTabIds[nextIdx];
      openWorkflowTab(nextId);
    }else{
      state.workflowId=null;
      if(typeof setWorkspaceMode==='function')setWorkspaceMode('aux');
      clearWorkflow();
    }
  }else{
    renderWorkflowTabs();
  }
  if(typeof renderSidebarWorkflows==='function')renderSidebarWorkflows();
}
function renderWorkflowTabs(){
  const box=document.getElementById('workflowTabsList');
  if(!box)return;
  if(!state.openWorkflowTabIds)state.openWorkflowTabIds=[];
  if(state.workflowId&&!state.openWorkflowTabIds.includes(state.workflowId)){
    state.openWorkflowTabIds.push(state.workflowId);
  }
  const ws=(state.project&&state.project.workflows)||[];
  const wfMap={};
  ws.forEach(w=>{wfMap[w.workflow_id]=w;});
  if(state.workflow&&state.workflow.workflow){
    wfMap[state.workflow.workflow.workflow_id]=state.workflow.workflow;
  }
  if(state.openWorkflowTabIds.length===0){
    box.innerHTML='<span class="no-tabs">无打开的工作流</span>';
    return;
  }
  box.innerHTML=state.openWorkflowTabIds.map(wid=>{
    const isActive=wid===state.workflowId;
    if(wid==='__ctl__'){
      return `<div class="wf-tab ${isActive?'active':''}" onclick="openWorkflowTab('__ctl__')" title="Controller 调度与解卡控制台" role="tab" aria-selected="${isActive}">`+
        `<span class="tab-dot running" aria-hidden="true"></span>`+
        `<svg class="tab-icon-svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polygon points="10 8 16 12 10 16 10 8"/></svg><span class="tab-title">Controller 控制台</span>`+
        `<button type="button" class="tab-close" onclick="closeWorkflowTab('__ctl__',event)" title="关闭页签" aria-label="关闭页签">×</button>`+
        `</div>`;
    }
    if(wid==='__templates__'){
      return `<div class="wf-tab ${isActive?'active':''}" onclick="openWorkflowTab('__templates__')" title="工作流模板规范库" role="tab" aria-selected="${isActive}">`+
        `<span class="tab-dot completed" aria-hidden="true"></span>`+
        `<svg class="tab-icon-svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg><span class="tab-title">模板规范库</span>`+
        `<button type="button" class="tab-close" onclick="closeWorkflowTab('__templates__',event)" title="关闭页签" aria-label="关闭页签">×</button>`+
        `</div>`;
    }
    if(wid==='__archive__'){
      return `<div class="wf-tab ${isActive?'active':''}" onclick="openWorkflowTab('__archive__')" title="任务归档与冷存储" role="tab" aria-selected="${isActive}">`+
        `<span class="tab-dot completed" aria-hidden="true"></span>`+
        `<svg class="tab-icon-svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><path d="M10 12h4"/></svg><span class="tab-title">任务归档</span>`+
        `<button type="button" class="tab-close" onclick="closeWorkflowTab('__archive__',event)" title="关闭页签" aria-label="关闭页签">×</button>`+
        `</div>`;
    }
    if(wid==='__logs__'){
      return `<div class="wf-tab ${isActive?'active':''}" onclick="openWorkflowTab('__logs__')" title="调度审计日志" role="tab" aria-selected="${isActive}">`+
        `<span class="tab-dot running" aria-hidden="true"></span>`+
        `<svg class="tab-icon-svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1-2.5-2.5Z"/><path d="M6 6h10"/><path d="M6 10h10"/></svg><span class="tab-title">调度审计日志</span>`+
        `<button type="button" class="tab-close" onclick="closeWorkflowTab('__logs__',event)" title="关闭页签" aria-label="关闭页签">×</button>`+
        `</div>`;
    }
    const w=wfMap[wid]||{workflow_id:wid,status:'waiting'};
    const title=workflowSubject(w)||w.title||wid;
    const st=w.status||'waiting';
    let dotClass='waiting';
    if(st==='working'||st==='running'||st==='active')dotClass='running';
    else if(st==='blocked'||st==='decision')dotClass='decision';
    else if(st==='completed'||st==='cleaned'||st==='archived')dotClass='completed';
    return `<div class="wf-tab ${isActive?'active':''}" onclick="openWorkflowTab('${esc(wid)}')" title="${esc(title)} (${esc(wid)})" role="tab" aria-selected="${isActive}">`+
      `<span class="tab-dot ${dotClass}" aria-hidden="true"></span>`+
      `<span class="tab-title">${esc(title)}</span>`+
      `<button type="button" class="tab-close" onclick="closeWorkflowTab('${esc(wid)}',event)" title="关闭页签" aria-label="关闭页签">×</button>`+
      `</div>`;
  }).join('');
}
function setTaskFilter(f){state.taskFilter=f;['All','Decision','Attention','Active'].forEach(k=>{const el=document.getElementById('f'+k);if(el)el.classList.toggle('active',f.toLowerCase()===k.toLowerCase())});renderTasks()}
function isDecisionTask(t){
  if(t.status==='superseded'||t.status==='cleaned')return false;
  return t.stage_verdict==='blocked'||t.status==='blocked'||(t.node_type==='gate'&&['agent_done','completed'].includes(t.status)&&t.stage_verdict!=='pass');
}
function decisionSummary(t){
  const title=taskDisplayName(t);
  const blocker=Array.isArray(t.blocker)?t.blocker.join('; '):t.blocker;
  return {
    title,
    question:t.decision_question||`是否批准“${title}”继续推进？`,
    basis:t.stage_verdict_note||blocker||t.blocked_reason||t.goal||'请核查成果后选择通过并放行或批注打回'
  };
}
function copyCliCommand(cmd){
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(cmd).then(()=>{toast('已复制 Controller 命令到剪贴板！')}).catch(()=>{toast('复制失败',true)});
  }else{
    const ta=document.createElement('textarea');
    ta.value=cmd;
    document.body.appendChild(ta);
    ta.select();
    try{document.execCommand('copy');toast('已复制 Controller 命令到剪贴板！')}catch(e){toast('命令: '+cmd)}
    document.body.removeChild(ta);
  }
}
function copyCliCommandByActionId(actId){
  const act=(state.controllerActionsMap&&state.controllerActionsMap[actId])||{};
  const cmd=act.command_line||actId;
  copyCliCommand(cmd);
}
async function executeControllerAction(actId,wid){
  const act=(state.controllerActionsMap&&state.controllerActionsMap[actId])||{};
  const payload=Object.assign({}, act.api_payload||{type:actId,workflow_id:wid});
  const endpoint=act.api_endpoint||'/api/controller/execute-action';
  const cmdLine=act.command_line||actId;
  const effect=act.effect||act.description||'';
  const targetTask=act.blocker_task_id||act.old_task_id||(act.api_payload&&act.api_payload.task_id)||'';
  if(targetTask){
    const allTasks=(state.workflow&&state.workflow.tasks)||[];
    const matchedTask=allTasks.find(t=>String(t.task_id)===String(targetTask));
    if(matchedTask){
      if(payload.expected_version===undefined&&matchedTask.version!==undefined){
        payload.expected_version=matchedTask.version;
      }
      if(payload.expected_pane_id===undefined&&matchedTask.pane_id){
        payload.expected_pane_id=matchedTask.pane_id;
      }
      if(payload.pane_id===undefined&&matchedTask.pane_id){
        payload.pane_id=matchedTask.pane_id;
      }
    }
  }
  const isForcePass=(payload.type==='force_pass'||payload.type==='force_pass_advance');
  const reasonHtml=isForcePass?`
    <div style="margin-top:12px">
      <label for="ctlReasonInput" style="font-weight:600;font-size:12px;color:var(--text-primary);display:block;margin-bottom:4px">人工豁免原因（必填）:</label>
      <textarea id="ctlReasonInput" class="input" style="width:100%;height:64px;box-sizing:border-box" placeholder="请输入人工核验结论及放行原因..."></textarea>
    </div>
  `:'';
  const summaryHtml=`<div style="line-height:1.6"><div style="font-size:13.5px;font-weight:600;margin-bottom:8px;color:var(--text-primary)">将执行：${esc(act.title||actId)}</div>${targetTask?`<div class="muted" style="font-size:12px;margin-bottom:8px">针对卡点任务：<code>${esc(targetTask)}</code></div>`:''}${effect?`<div class="ctl-effect" style="margin-bottom:8px">${esc(effect)}</div>`:''}${act.is_destructive?'<div style="font-size:12px;color:var(--danger);font-weight:600;margin-bottom:8px">高风险豁免操作，请确认已人工核查产物。</div>':''}${reasonHtml}<details class="ctl-tech"><summary>工程师技术详情（可选展开）</summary><div class="ctl-tech-body"><div class="muted" style="font-size:11.5px;margin-bottom:4px">对应底层命令（仅审计 standby，点按钮即可执行，无需手动敲）：</div><div class="ctl-cheat-cmd"><code>${esc(cmdLine)}</code></div><div style="display:flex;justify-content:flex-end;margin-top:8px"><button class="mini" onclick="copyCliCommandByActionId('${esc(act.action_id||actId)}')">复制命令</button></div></div></details></div>`;
  openModal('执行 Controller 解卡操作',`${summaryHtml}<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px"><button class="btn" onclick="closeModal()">取消</button><button id="ctlExecConfirmBtn" class="btn ${act.is_destructive?'danger-btn':'primary'}">立即执行</button></div>`);
  const confirmBtn=document.getElementById('ctlExecConfirmBtn');
  if(confirmBtn)confirmBtn.onclick=async()=>{
    if(isForcePass){
      const rEl=document.getElementById('ctlReasonInput');
      const rVal=(rEl?rEl.value:'').trim();
      if(!rVal){toast('必须填写人工豁免原因',true);return;}
      payload.reason=rVal;
      payload.note=rVal;
      payload.confirmed=true;
    }
    closeModal();
    try{
      toast('正在调度 Controller 执行…');
      const res=await api(endpoint,{
        method:'POST',
        body:JSON.stringify(payload)
      });
      if(res.partial){
        toast('人工豁免已写入但推进失败: '+(res.advance_error||res.error||'后续推进未完成'),true);
      }else{
        toast('Controller 解卡命令已执行！正在刷新现场…');
      }
      await loadWorkflow(wid);
      if(state.spaceId)await refreshAll();
    }catch(e){
      toast('执行失败: '+e.message,true);
    }
  };
}
function controllerBlockerTask(actionId){
  const act=(state.controllerActionsMap&&state.controllerActionsMap[actionId])||{};
  return act.blocker_task_id||act.old_task_id||'';
}
function locateControllerTask(taskId){
  if(!taskId)return;
  closeModal();
  try{if(state.taskFilter&&state.taskFilter!=='all'){setTaskFilter('all')}}catch(e){}
  setTimeout(()=>{try{openTaskDrawer(taskId)}catch(e){toast('任务详情打开失败: '+e.message,true)}},60);
}
function updateAttentionHub(){
  const ts=(state.workflow&&state.workflow.tasks)||[];
  const cntAll=ts.length;
  const decisionTasks=ts.filter(isDecisionTask);
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
  const w=state.workflow&&state.workflow.workflow;
  if(stall&&stall.is_stalled){
    ab.style.background='var(--warning-bg)';
    ab.style.borderColor='rgba(217,119,6,0.25)';
    let actBtn='';
    if(stall.suggested_action==='force_review'&&stall.target_task_id){
      actBtn=`<button class="btn primary" style="background:#d97706;border-color:#b45309;padding:4px 12px;font-size:12px" onclick="forceReviewTask('${stall.target_task_id}')">🔔 立即唤醒评审</button>`;
    }else if(stall.suggested_action==='retry_advance'){
      actBtn=`<button class="btn primary" style="background:#2563eb;border-color:#1d4ed8;padding:4px 12px;font-size:12px" onclick="retryStageAdvance('${state.workflowId}')">⚡ 尝试推进阶段</button>`;
    }
    ab.innerHTML=`<div style="display:flex;align-items:center;gap:8px"><span class="att-badge" style="background:var(--warning);color:#fff">推进停滞告警</span><span class="att-text" style="color:var(--text-primary)">⚠️ ${esc(stall.message)}</span></div><div>${actBtn}</div>`;
  }else if(w&&(w.status==='completed'||w.outcome==='delivered')){
    ab.style.background='var(--success-bg)';
    ab.style.borderColor='rgba(22,163,74,0.25)';
    ab.innerHTML=`<div style="display:flex;align-items:center;gap:8px"><span class="att-badge" style="background:var(--success);color:#fff">已交付</span><span class="att-text" style="color:var(--text-primary);font-weight:500">🎉 工作流已顺利完成全流程闭环并交付归档</span></div><div></div>`;
  }else{
    ab.style.background='var(--bg-surface)';
    ab.style.borderColor='var(--border-default)';
    ab.style.borderLeft='3px solid var(--primary)';
    state.controllerActionsMap={};
    const acts=(state.controllerActionsData&&state.controllerActionsData.actions)||[];
    acts.forEach(a=>{state.controllerActionsMap[a.action_id]=a;});
    const detailRows=decisionTasks.slice(0,3).map(t=>{
      const d=decisionSummary(t);
      let actionsHtml='';
      if(acts.length){
        const mine=acts.filter(a=>((a.blocker_task_id||a.old_task_id||String(a.action_id||'').split(':')[0])===t.task_id));
        const shown=(mine.length?mine:acts).slice(0,2);
        const cardRows=shown.map(act=>`
          <div class="ctl-mini-card${act.recommended?' is-recommended':''}">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:2px">
              <strong style="color:var(--text-primary);font-size:12px">${act.recommended?'⭐ ':''}${esc(act.title)}</strong>
              <span class="ctl-badges"><span class="badge ${catMeta(act.category)[1]}" style="font-size:10px">${esc(catMeta(act.category)[0])}</span></span>
            </div>
            <div class="muted" style="margin-bottom:4px;font-size:11px;line-height:1.5">${esc(act.effect||act.description)}</div>
            <div style="display:flex;justify-content:flex-end;gap:8px;align-items:center">
              <button class="mini" style="padding:2px 8px;font-size:10.5px;flex-shrink:0" onclick="locateControllerTask('${esc(t.task_id)}')">查看任务</button>
              <button class="btn primary" style="padding:3px 8px;font-size:11.5px" onclick="executeControllerAction('${esc(act.action_id)}','${esc(state.workflowId)}')">一键执行</button>
            </div>
            <details class="ctl-tech" style="margin-top:8px"><summary>技术详情</summary><div class="ctl-tech-body"><div class="ctl-mini-cmd"><code>${esc(act.command_line)}</code><button class="mini" style="padding:2px 8px;font-size:10.5px;flex-shrink:0" onclick="copyCliCommandByActionId('${esc(act.action_id)}')">复制</button></div></div></details>
          </div>
        `).join('');
        actionsHtml=`<div class="controller-actions-wrap" style="margin-top:4px">${cardRows}</div>`;
      }
      return `<div class="decision-item"><strong>${esc(d.title)}</strong><span>${esc(d.question)}</span><small>依据：${esc(d.basis)}</small>${actionsHtml}</div>`;
    }).join('');
    const more=decisionTasks.length>3?`<div class="decision-more">还有 ${decisionTasks.length-3} 项，请查看全部决策项。</div>`:'';
    const details=decisionTasks.length?`<div class="decision-list"><div class="decision-list-label">待决策事项</div>${detailRows}${more}</div>`:'';
    ab.innerHTML=`<div style="min-width:0;flex:1"><div style="display:flex;align-items:center;gap:8px"><span class="att-badge">人机协同态势</span><span class="att-text">${activeTasks.length} 个执行者正在协同 · ${readyCnt} 个正常推进 · ${attentionTasks.length} 个需关注 · <strong style="color:${decisionTasks.length?'var(--accent)':'var(--text)'}">${decisionTasks.length} 个待你拍板</strong></span></div>${details}</div><div>${decisionTasks.length?`<button class="btn primary" style="padding:4px 12px;font-size:12px" onclick="setTaskFilter('decision')">查看决策项</button>`:''}</div>`;
  }
}
function renderStages(){
  const ss=(state.workflow&&state.workflow.stages)||[];
  const e=document.getElementById('stages');
  if(!e)return;
  if(!ss.length){e.style.display='none';e.innerHTML='';return}
  e.style.display='flex';
  const actIdx=ss.findIndex(s=>['working','finalizing','failed','blocked'].includes(s.status));
  let nextIdx=-1;
  if(actIdx===-1){
    nextIdx=ss.findIndex(s=>!['cleaned','completed','committed','integrated'].includes(s.status));
  }
  e.innerHTML=ss.map((s,i)=>{
    const done=['cleaned','completed','committed','integrated'].includes(s.status);
    const isAct=(i===actIdx);
    const isNext=(i===nextIdx);
    const stIcon=done?'✓':(isAct||isNext)?'●':'○';
    const stCls=done?'stage-done':isAct?'stage-running':isNext?'stage-next':'stage-pending';
    let badgeHtml='';
    if(isAct){
      if(s.status==='blocked')badgeHtml='<span class="stage-badge blocked">阻塞</span>';
      else if(s.status==='failed')badgeHtml='<span class="stage-badge blocked">失败</span>';
      else badgeHtml='<span class="stage-badge running">进行中</span>';
    }else if(isNext){
      badgeHtml='<span class="stage-badge next">下一阶段</span>';
    }
    return `<div class="stage-step ${stCls}">`+
      `<span class="stage-indicator">${stIcon}</span>`+
      `<span class="stage-name">${esc(cleanStageLabel(s.label))}</span>`+
      badgeHtml+
      `<span class="stage-meta">${s.count} 任务</span>`+
    `</div>`+(i<ss.length-1?'<div class="stage-connector"></div>':'');
  }).join('')
}
function renderTasks(){
  const e=document.getElementById('tasks');
  const allTs=(state.workflow&&state.workflow.tasks)||[];
  updateAttentionHub();
  let ts=allTs;
  const f=state.taskFilter||'all';
  if(f==='decision'){
    ts=ts.filter(isDecisionTask);
  }else if(f==='attention'){
    ts=ts.filter(t=>['failed','interrupted','rework'].includes(t.status)||(t.blocker&&t.blocker.length));
  }else if(f==='active'){
    ts=ts.filter(t=>['dispatched','working','rework','paused'].includes(t.status));
  }
  if(!ts.length){e.innerHTML=`<div class="empty">${f==='all'?'暂无任务':'当前筛选无匹配任务'}</div>`;return}
  e.innerHTML=ts.map(t=>{
    const isDone=['completed','committed','integrated','cleaned'].includes(t.status);
    const isAct=['working','dispatched','rework','in_progress'].includes(t.status);
    const isBlk=['blocked','failed'].includes(t.status);
    const iconCls=isDone?'done':isAct?'working':isBlk?'blocked':'pending';
    const iconChar=isDone?'✓':isAct?'●':isBlk?'!':'○';
    return `
    <div class="task" data-task-id="${esc(t.task_id)}" onclick="onTaskRowClick(event, '${esc(t.task_id)}')">
      <span class="task-icon ${iconCls}">${iconChar}</span>
      <div class="task-main">
        <span class="task-id">${esc(t.task_id)}</span>
        <span class="task-name">${esc(taskDisplayName(t))}</span>
      </div>
      <span class="task-agent"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="opacity:0.6"><circle cx="12" cy="8" r="4"/><path d="M6 20v-2a6 6 0 0 1 12 0v2"/></svg>${esc(t.agent||'未分配')}</span>
      <div class="task-status">${badge(t.status)}</div>
      <div class="task-menu" id="taskMenu_${esc(t.task_id)}">
        <button class="task-menu-btn" title="更多操作" onclick="toggleTaskMenu(event, '${esc(t.task_id)}')">···</button>
        <div class="task-dropdown-menu" onclick="event.stopPropagation()">
          <button class="task-dropdown-item primary" onclick="closeAllTaskMenus();openSignoffChamber('${esc(t.task_id)}')">成果会签</button>
          <button class="task-dropdown-item" onclick="closeAllTaskMenus();showPane('${esc(t.pane_id||'')}')">查看工位</button>
          <button class="task-dropdown-item" onclick="closeAllTaskMenus();askCoordinator('${esc(t.task_id)}')">让总指挥处理</button>
          ${['blocked','failed','rework'].includes(t.status)||t.stage_verdict==='blocked'||(t.blocker&&t.blocker.length)?`
            <button class="task-dropdown-item" style="color:var(--primary);font-weight:600" onclick="closeAllTaskMenus();openControllerCockpitModal()">⚡ 查看解卡方案</button>
          `:''}
          ${['working','dispatched','rework','blocked','paused'].includes(t.status)?`
            <div class="task-dropdown-divider"></div>
            <button class="task-dropdown-item" style="color:var(--primary)" onclick="closeAllTaskMenus();showSteerModal('${esc(t.task_id)}')">留话指导</button>
            <button class="task-dropdown-item danger" onclick="closeAllTaskMenus();haltTaskPrompt('${esc(t.task_id)}')">紧急叫停</button>
          `:''}
          ${t.status==='rework'?`
            <div class="task-dropdown-divider"></div>
            <button class="task-dropdown-item" style="color:var(--warning);font-weight:600" onclick="closeAllTaskMenus();forceReviewTask('${esc(t.task_id)}')">唤醒评审</button>
          `:''}
          ${t.stage_verdict==='blocked'?`
            <div class="task-dropdown-divider"></div>
            <button class="task-dropdown-item primary" onclick="closeAllTaskMenus();forcePassTask('${esc(t.workflow_id||state.workflowId)}','${esc(t.node||t.stage)}',{task_id:'${esc(t.task_id||'')}',expected_version:${t.version!==undefined&&t.version!==null?t.version:'null'},expected_pane_id:'${esc(t.pane_id||'')}',pane_id:'${esc(t.pane_id||'')}'})">强制放行</button>
          `:''}
        </div>
      </div>
    </div>`;
  }).join('')
}

function toggleSpaceMenu(ev){if(ev)ev.stopPropagation();const m=document.getElementById('spaceMenu');if(m)m.hidden=!m.hidden;}
function closeSpaceMenu(){const m=document.getElementById('spaceMenu');if(m)m.hidden=true;}
function paintCrumb(){
  const sec=document.getElementById('crumbSection');
  const view=state.opsMode?'ops':state.dashMode?'dashboard':(state.shellView||'workbench');
  const map={workbench:'工作台',dashboard:'仪表板',ops:'运维',alerts:'告警',agents:'执行者',slots:'工位',workflows:'工作流'};
  if(sec)sec.textContent=map[view]||'工作台';
  const tabsBar=document.getElementById('workflowTabsBar');
  const crumbEl=document.querySelector('.top .crumb');
  if(tabsBar&&tabsBar.style)tabsBar.style.display=(view==='workbench'?'flex':'none');
  if(crumbEl&&crumbEl.classList)crumbEl.classList.toggle('visually-hidden', view==='workbench');
  document.querySelectorAll('[data-nav]').forEach(el=>el.classList&&el.classList.toggle('on', el.getAttribute('data-nav')===view));
  const ctlBtn=document.getElementById('navController');
  const tplBtn=document.getElementById('navTemplates');
  const arcBtn=document.getElementById('navArchive');
  const logBtn=document.getElementById('sidebarLogsItem');
  if(ctlBtn&&ctlBtn.classList)ctlBtn.classList.toggle('on',state.workflowId==='__ctl__');
  if(tplBtn&&tplBtn.classList)tplBtn.classList.toggle('on',state.workflowId==='__templates__');
  if(arcBtn&&arcBtn.classList)arcBtn.classList.toggle('on',state.workflowId==='__archive__');
  if(logBtn&&logBtn.classList)logBtn.classList.toggle('on',state.workflowId==='__logs__');
  const railActiveId = (state.workflowId === '__templates__') ? 'templates' : view;
  document.querySelectorAll('.rail-item[data-rail-nav]').forEach(el=>el.classList&&el.classList.toggle('active', el.getAttribute('data-rail-nav')===railActiveId));
  const shell=document.querySelector('.shell');
  if(shell&&shell.dataset)shell.dataset.view=view;
  const s=state.space;
  if(s){
    const label=s.label||s.project_name||s.workspace_id||'空间';
    const name=document.getElementById('spaceName');
    const meta=document.getElementById('spaceMeta');
    const av=document.getElementById('spaceAvatar');
    const rav=document.getElementById('railSpaceAvatar');
    const foot=document.getElementById('sideFootTitle');
    if(name)name.textContent=label;
    if(meta)meta.textContent=relationText(s);
    if(av)av.textContent=String(label).slice(0,1)||'空';
    if(rav)rav.textContent=String(label).slice(0,1)||'共';
    if(foot)foot.textContent=label+' · '+relationText(s);
  }
  const o=state.overview||{};
  const setCount=(id,n)=>{const el=document.getElementById(id);if(!el)return;const v=Number(n)||0;el.textContent=String(v);el.hidden=v<=0;};
  const totalWfCount=((state.project&&state.project.workflows)||[]).length;
  setCount('navWfCount', totalWfCount);
  const wfTotalEl=document.getElementById('sidebarWfTotalCount');
  if(wfTotalEl)wfTotalEl.textContent=String(totalWfCount);
  setCount('navAgentCount', o.active_agents);
  const alertCnt=(typeof visibleAlerts==='function'?visibleAlerts():[]).length;
  setCount('navAlertCount', alertCnt);
  const railAlert=document.getElementById('railAlertDot');
  if(railAlert){if(railAlert.style)railAlert.style.display=alertCnt>0?'block':'none';railAlert.hidden=alertCnt<=0;}
  const bw=document.getElementById('bottomActiveWorkers');
  if(bw)bw.textContent=String(o.active_agents||o.active_workers||0);
  const slotsCount=(state.project&&(state.project.slots||[]).length)||18;
  const activeAgents=o.active_agents||0;
  const resCountEl=document.getElementById('sidebarResourceCounts');
  if(resCountEl)resCountEl.textContent=`${activeAgents}/${slotsCount}`;
  const onlinePill=document.getElementById('sidebarOnlinePill');
  if(onlinePill)onlinePill.textContent=`${activeAgents} 在线`;
  const fleetNames=document.getElementById('sidebarFleetNames');
  if(fleetNames){
    const agents=(state.project&&state.project.agents)||[];
    const names=agents.slice(0,4).map(a=>a.name||a.agent||a.id).filter(Boolean);
    fleetNames.textContent=names.length?names.join(', '):'Claude, OpenCode, Codex, Human';
  }
  const panesSummary=document.getElementById('sidebarPanesSummary');
  if(panesSummary){
    panesSummary.textContent=`${slotsCount} 个物理工位 (${activeAgents} 运行中)`;
  }
  if(typeof renderSidebarWorkflows==='function')renderSidebarWorkflows();
}
async function showShellView(view){
  if(view==='dashboard'){state.shellView='dashboard';return showDashboard();}
  if(view==='ops'){state.shellView='ops';return showOpsCenter();}
  const leaving=!!(state.opsMode||state.dashMode);
  state.opsMode=false;state.dashMode=false;stopDashTimer();
  state.shellView=view||'workbench';
  saveViewState();syncOpsUi();
  if(leaving){await refreshAll();return;}
  if(state.shellView==='workbench'){
    if(!state.workflow){
      if(state.space&&state.space.relation&&state.space.relation!=='current_factory'){
        await selectSpace(state.space.workspace_id,false);
        return;
      }
      setWorkspaceMode('aux');
      const empty=document.getElementById('tasks');
      if(empty)empty.innerHTML='<div class="empty">暂无任务</div>';
      paintCrumb();
      return;
    }
    setWorkspaceMode(state.workflowView||'flow');
    paintCrumb();
    if((state.workflowView||'flow')==='list')renderTasks();
    else{
      if(!state.flowGraph)renderFlowGraph();
      requestAnimationFrame(()=>{resizeFlowGraph();fitFlowGraph();});
    }
    return;
  }
  paintShellPage(state.shellView);
}
function paintShellPage(view){
  setWorkspaceMode('aux');
  paintCrumb();
  if(view==='alerts')renderShellAlerts();
  else if(view==='agents')renderShellAgents();
  else if(view==='slots')renderShellSlots();
  else if(view==='workflows')renderShellWorkflows();
}
function renderShellAlerts(){
  const el=document.getElementById('tasks');if(!el)return;
  const src=document.getElementById('alerts');
  const body=src&&src.innerHTML.trim()?src.innerHTML:'<div class="empty">当前项目 / 工作流暂无告警</div>';
  el.innerHTML='<div class="shell-page"><h3>告警</h3>'+body+'</div>';
}
function renderShellAgents(){
  const el=document.getElementById('tasks');if(!el)return;
  if(!state.project){el.innerHTML='<div class="shell-page"><h3>执行者</h3><div class="empty">当前空间暂无执行者</div></div>';return;}
  renderAgents();
  const src=document.getElementById('agents');
  el.innerHTML='<div class="shell-page"><h3>执行者</h3>'+(src?src.innerHTML:'')+'</div>';
}
function renderShellSlots(){
  const el=document.getElementById('tasks');if(!el)return;
  if(!state.project){el.innerHTML='<div class="shell-page"><h3>工位</h3><div class="empty">当前空间暂无工位</div></div>';return;}
  renderSlots();
  const src=document.getElementById('slots');
  el.innerHTML='<div class="shell-page"><h3>工位</h3>'+(src?src.innerHTML:'')+'</div>';
}
function renderShellWorkflows(){
  const el=document.getElementById('tasks');if(!el)return;
  const ws=(state.project&&state.project.workflows)||[];
  el.innerHTML='<div class="shell-page"><h3>工作流</h3>'+(ws.length?ws.map(w=>`<button class="shell-row" onclick="openShellWorkflow('${esc(w.workflow_id)}')"><strong>${esc(workflowDisplayName(w))}</strong><small>${esc(w.workflow_id)}</small></button>`).join(''):'<div class="empty">当前空间暂无工作流</div>')+'</div>';
}
async function openShellWorkflow(id){
  state.shellView='workbench';state.workflowView='flow';saveViewState();
  if(!state.openWorkflowTabIds)state.openWorkflowTabIds=[];
  if(id&&!state.openWorkflowTabIds.includes(id))state.openWorkflowTabIds.push(id);
  try{await loadWorkflow(id);}catch(e){toast(e.message,true);return;}
  paintCrumb();
  renderWorkflowTabs();
  requestAnimationFrame(()=>{resizeFlowGraph();fitFlowGraph();});
}
function flowIconSvg(id){
  const paths={
    requirements:'<path d="M13 2L4 14h7l-1 8 9-12h-7l1-8z"/>',
    plan:'<path d="M4 6h16M4 12h10M4 18h16"/>',
    implementation:'<path d="M14 4l6 6-10 10H4v-6L14 4z"/>',
    review:'<path d="M5 13l4 4L19 7"/>',
    test:'<path d="M9 3h6M10 3v6l-5 9a4 4 0 0 0 3.5 6h7A4 4 0 0 0 19 18l-5-9V3"/>',
    wrapup:'<path d="M4 7h16v12H4zM8 7V5h8v2"/>'
  };
  const p=paths[id]||paths.requirements;
  return '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'+p+'</svg>';
}
function flowCardFoot(n){
  const u=n.resource_usage;
  if(u)return '累计任务 '+u.task_count+' / '+(u.max_tasks_per_node??'未设')+' · 运行 '+u.active_task_count+' / '+(u.max_concurrency??'未设')+' · 工位引用 '+u.pane_count+(u.overflow?' · 超限':'');
  if(!n.task_count)return (n.status === 'completed' || n.status === 'cleaned') ? '已完成' : '尚未开始';
  const head=(Number(n.active_task_count)||0)+' 运行 · 完成 '+(Number(n.completed_task_count)||0);
  const agents=(n.agents||[]).filter(Boolean).slice(0,2);
  return agents.length?head+' · '+agents.join(' · '):head;
}
function flowNodeStatus(node,fallback){
  if(recoveryUnavailable())return '状态未确认';
  const op=currentRecovery(node.id)[0];
  return op?({waiting_human:'待人工确认',awaiting_result:'核验派发',waiting:'已暂缓',pending:'等待派发',running:'正在派发'}[op.status]||'需恢复'):fallback;
}
function flowCardInner(n){
  const status=n.status||'waiting';
  const kind=String(n.node_type||'agent').toUpperCase();
  const leftAnchor=n.has_upstream!==false?('<div class="node-anchor left'+(n.inbound_active?' inbound-active':'')+'"></div>'):'';
  const rightAnchor=n.has_downstream!==false?('<div class="node-anchor right'+(n.outbound_active?' outbound-active':'')+'"></div>'):'';
  return leftAnchor+rightAnchor
    +'<div class="fn-top"><span class="fn-ico">'+flowIconSvg(n.id)+'</span><span class="fn-kind">'+esc(kind)+'</span><span class="fn-pill">'+esc(flowNodeStatus(n,humanNodeStatus(status)))+'</span></div>'
    +'<div class="fn-title">'+esc(cleanStageLabel(n.label||n.id||''))+'</div>'
    +'<div class="fn-purpose">'+esc(flowPlain(n.purpose))+'</div>'
    +'<div class="fn-foot"'+(n.resource_usage?.overflow?' style="color:var(--danger);font-weight:600"':'')+'>'+esc(flowCardFoot(n))+'</div>';
}
function ensureFlowCardShape(){
  if(state.flowHtmlReady)return true;
  if(!window.X6||!X6.Shape||!X6.Shape.HTML||!X6.Shape.HTML.register)return false;
  X6.Shape.HTML.register({
    shape:'flow-card',width:232,height:134,effect:['data'],
    html(cell){
      const n=(cell&&cell.getData)?(cell.getData()||{}):{};
      const wrap=document.createElement('div');
      wrap.className='flow-node-card is-'+(n.status||'waiting')+(n.selected?' is-selected':'');
      wrap.innerHTML=flowCardInner(n);
      wrap.addEventListener('click',function(){if(n.id)selectFlowNode(n.id);});
      return wrap;
    }
  });
  state.flowHtmlReady=true;
  return true;
}
function flowPlain(s){return String(s||'').replace(/\s+/g,' ').trim();}
function flowReads(node){
  const purpose=flowPlain(node.purpose)||'该节点暂无目的说明';
  const agents=(node.agents||[]).filter(Boolean);
  const active=Number(node.active_task_count||0);
  let tail;
  if(!agents.length)tail='尚未分配执行者。';
  else if(active)tail='当前由 '+agents.join('、')+' 执行，'+active+' 个任务运行中。';
  else tail='当前由 '+agents.join('、')+' 执行。';
  const sep=/[。！？.!?]$/.test(purpose)?'':'。';
  return purpose+sep+tail;
}
function flowChecklist(node){
  const map={};(flowGraphData().nodes||[]).forEach(n=>{map[n.id]=n;});
  const labelOf=id=>cleanStageLabel((map[id]&&map[id].label)||id);
  const items=[];
  const deps=node.depends_on||[];
  if(!deps.length)items.push(['ok','起点节点，无上游依赖']);
  else{
    const waiting=deps.filter(id=>!map[id]||map[id].status!=='completed');
    if(!waiting.length)items.push(['ok','上游依赖已满足']);
    else items.push(['wait','等待上游 · '+waiting.map(labelOf).join('、')]);
  }
  const issueLabels={required_task_missing:'必需任务缺失',required_task_out_of_workflow:'必需任务属于其他工作流',required_task_out_of_node:'必需任务属于其他节点',required_task_lineage_cycle:'任务替代关系存在循环',required_task_ids_invalid:'必需任务配置无效',workflow_config_invalid:'工作流配置无效',required_task_incomplete:'必需任务尚未完成'};
  (node.completion_issues||[]).forEach(issue=>items.push(['bad',(issueLabels[issue.reason]||issue.reason)+(issue.task_id?' · '+issue.task_id:'')]));
  const agents=(node.agents||[]).filter(Boolean);
  if(!agents.length){
    if(node.status==='completed')items.push(['ok','阶段已收尾完成']);
    else items.push(['wait','尚未分配执行者']);
  }
  else items.push(['ok','执行者已分配 · '+agents.join('、')]);
  const failed=Number(node.failed_task_count||0), blocked=Number(node.blocked_task_count||0);
  if(failed||blocked){
    if(node.status==='completed')items.push(['ok','历史失败/阻塞已处理放行（历史失败 '+failed+' · 阻塞 '+blocked+'）']);
    else items.push(['bad','失败 '+failed+' · 阻塞 '+blocked]);
  }
  else if(node.status==='blocked'||(node.completion_issues||[]).length)items.push(['bad','节点仍有未解除的阻塞']);
  else if(recoveryUnavailable())items.push(['bad','恢复状态读取失败，卡点未确认']);
  else if(currentRecovery(node.id).length)items.push(['bad','存在派发恢复待办，需要核查或人工确认']);
  else items.push(['ok','无失败、无阻塞']);
  const down=node.downstream||[];
  if(!down.length)items.push(['ok','终点节点']);
  else items.push(['ok','下游 · '+down.map(labelOf).join('、')]);
  return items;
}
function flowOverviewHtml(node, tasks){
  const checks=flowChecklist(node).map(item=>{
    const kind=item[0];
    const mark=kind==='bad'?'!':kind==='wait'?'○':'✓';
    return '<div class="flow-check"><span class="flow-mark '+kind+'">'+mark+'</span><span>'+esc(item[1])+'</span></div>';
  }).join('');
  const live=(tasks||[]).filter(t=>['cleaned','completed','committed','integrated','superseded'].indexOf(t.status)<0).slice(0,6);
  const rows=live.length?live.map(t=>'<div class="flow-task-row" onclick="openTaskDrawer(\''+esc(t.task_id)+'\')"><div><b>'+esc(t.task_id)+'</b><small>'+esc(t.agent||'未分配')+'</small></div><span class="fn-pill">'+esc(humanStatus(t.status))+'</span></div>').join(''):'<div class="empty">暂无进行中的任务</div>';
  return dispatchConfirmationHtml(node.id)+currentRecovery(node.id).map(o=>recoveryCard(o,state.workflowId)).join('')+'<div class="flow-reads"><em>读作</em>'+esc(flowReads(node))+'</div><div class="flow-sec">本节点现状</div>'+checks+'<div class="flow-sec">当前任务</div>'+rows;
}

function flowGraphData(){return (state.workflow&&state.workflow.graph)||{nodes:[],edges:[]};}
function initFlowGraph(){renderFlowGraph();}
function pickDefaultFlowNodeId(nodes){if(!nodes||!nodes.length)return null;const order=['blocked','failed','working','rework'];for(const st of order){const hit=nodes.find(n=>n.status===st);if(hit)return hit.id;}const completed=new Set(nodes.filter(n=>n.status==='completed').map(n=>n.id));const waitingNext=nodes.find(n=>n.status==='waiting'&&(n.depends_on||[]).every(d=>completed.has(d)));if(waitingNext)return waitingNext.id;const waiting=nodes.find(n=>n.status==='waiting');if(waiting)return waiting.id;return nodes[0].id;}
function setWorkspaceMode(mode){const fw=document.getElementById('flowWrap');const tl=document.getElementById('tasks');const st=document.getElementById('stages');const fs=document.getElementById('flowSummary');const ctlP=document.getElementById('controllerTabView');const tplP=document.getElementById('templatesTabView');const arcP=document.getElementById('archiveTabView');const logP=document.getElementById('logsTabView');const shell=document.querySelector('.shell');if(shell)shell.dataset.workspace=mode||'flow';const isSys=(mode==='aux'||mode==='ctl'||mode==='templates'||mode==='archive'||mode==='logs');const bar=document.getElementById('canvasToolbar');if(bar)bar.style.display=isSys?'none':'flex';const tg=document.querySelector('.flow-view-toggle');if(tg)tg.style.display=isSys?'none':'inline-flex';const tf=document.querySelector('.task-filters');if(tf)tf.style.display=(isSys||mode!=='list')?'none':'flex';if(fw)fw.style.display=mode==='flow'?'flex':'none';if(fs)fs.style.display=(mode==='flow')?'block':'none';if(st)st.style.display='none';if(tl)tl.style.display=(mode==='flow'||mode==='ctl'||mode==='templates'||mode==='archive'||mode==='logs')?'none':'block';if(ctlP)ctlP.hidden=(mode!=='ctl');if(tplP)tplP.hidden=(mode!=='templates');if(arcP)arcP.hidden=(mode!=='archive');if(logP)logP.hidden=(mode!=='logs');}
function switchWorkflowView(v){state.workflowView=(v==='list'?'list':'flow');const bf=document.getElementById('viewFlowBtn');if(bf)bf.classList.toggle('active',state.workflowView==='flow');const bl=document.getElementById('viewListBtn');if(bl)bl.classList.toggle('active',state.workflowView==='list');setWorkspaceMode(state.workflowView);if(state.workflowView==='flow')requestAnimationFrame(()=>{resizeFlowGraph();fitFlowGraph();});else renderTasks();}
function switchFlowInspectorTab(t){state.flowInspectorTab=t;['Summary','Tasks','Context','Runtime'].forEach(k=>{const el=document.getElementById('flowTab'+k);if(el)el.classList.toggle('active',k.toLowerCase()===t);});renderNodeInspector();}
function selectFlowNode(id){state.flowSelectedNodeId=id;updateFlowSelection();renderNodeInspector();}
function updateFlowSelection(){try{if(!state.flowGraph||!state.flowGraph.getCellById)return;const g=flowGraphData();(g.nodes||[]).forEach(n=>{try{const cell=state.flowGraph.getCellById(n.id);if(!cell||!cell.getData||!cell.setData)return;const sel=String(state.flowSelectedNodeId||'')===String(n.id);const data=cell.getData()||{};if(!!data.selected===sel)return;cell.setData(Object.assign({},data,{selected:sel}));}catch(err){}});}catch(e){}}
function destroyFlowGraph(){try{if(state.flowGraph&&state.flowGraph.dispose)state.flowGraph.dispose();}catch(e){}state.flowGraph=null;state.flowGraphWfId=null;const c=document.getElementById('flowCanvas');if(c)c.innerHTML='';}
function resizeFlowGraph(){try{const cv=document.getElementById('flowCanvas');const host=document.getElementById('flowCanvasWrap')||cv;if(!cv||!host||!state.flowGraph)return;const w=host.clientWidth||cv.clientWidth||800;const h=Math.max(280,cv.clientHeight||host.clientHeight||480);if(state.flowGraph.resize)state.flowGraph.resize(w,h);}catch(e){}}
function fitFlowGraph(){try{if(!state.flowGraph)return;if(state.flowGraph.zoomToFit){state.flowGraph.zoomToFit({padding:24,maxScale:1});if(state.flowGraph.centerContent)state.flowGraph.centerContent();}else if(state.flowGraph.centerContent)state.flowGraph.centerContent();}catch(e){}}
function flowZoomIn(){try{if(state.flowGraph&&state.flowGraph.zoom)state.flowGraph.zoom(0.15);}catch(e){}}
function renderFlowWorkbench(){
  const g=flowGraphData();const nodes=g.nodes||[];
  setWorkspaceMode(state.workflowView);
  const fs=document.getElementById('flowSummary');
  if(fs){
    if(state.workflowView==='flow'&&nodes.length){
      const working=nodes.filter(n=>n.status==='working').length;
      fs.style.display='inline-flex';
      const dotHtml=working>0?'<span class="flow-status-dot" style="background:#5e6ad2;box-shadow:0 0 0 2px rgba(94,106,210,.25)"></span>':'<span class="flow-status-dot"></span>';
      fs.innerHTML=dotHtml+'<span><b>'+nodes.length+'</b> 个节点 · <b>'+working+'</b> 个运行中</span><span class="flow-gesture-hint">拖拽平移 · <kbd>⌘</kbd>+滚轮缩放</span>';
    }else{
      fs.style.display='none';
    }
  }
  const bf=document.getElementById('viewFlowBtn');if(bf)bf.classList.toggle('active',state.workflowView!=='list');
  const bl=document.getElementById('viewListBtn');if(bl)bl.classList.toggle('active',state.workflowView==='list');
  if(!state.flowSelectedNodeId||!nodes.some(n=>n.id===state.flowSelectedNodeId)){state.flowSelectedNodeId=pickDefaultFlowNodeId(nodes);}
  const recoveries=currentRecovery();
  const ticker=document.getElementById('bottomTicker');
  if(ticker)ticker.textContent=recoveryUnavailable()?'恢复状态读取失败，卡点未确认':recoveries.length?'有 '+recoveries.length+' 项恢复待办，请查看卡点':'工作流调度就绪';
  const oldAttn=document.getElementById('flowAttnBanner');
  if(oldAttn)oldAttn.remove();
  if(fs&&recoveryUnavailable())fs.innerHTML+=renderRecoveryPanel(state.workflow.workflow.workflow_id);
  if(fs&&recoveries.length){
    const cleanItems=recoveries.map(o=>cleanStageLabel(o.recovery?.node_label||o.payload?.node_id||'')+' · '+(o.recovery?.summary||'需处理')).filter(Boolean).join('；');
    fs.insertAdjacentHTML('afterend',`
      <div role="status" class="flow-attn" id="flowAttnBanner">
        <div class="flow-attn-content">
          <span class="flow-attn-tag">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"></path>
              <line x1="12" y1="9" x2="12" y2="13"></line>
              <line x1="12" y1="17" x2="12.01" y2="17"></line>
            </svg>
            ${recoveries.length} 项恢复待办
          </span>
          <span class="flow-attn-text" title="${esc(cleanItems)}">${esc(cleanItems)}</span>
        </div>
        <button class="flow-attn-action" onclick="openControllerCockpitModal()">查看卡点与恢复操作</button>
      </div>
    `);
  }
  renderNodeInspector();
  renderFlowGraph();
}
function dagreLayoutPositions(nodes,edges){const g=new window.dagre.graphlib.Graph();g.setGraph({rankdir:'LR',ranksep:56,nodesep:36,marginx:32,marginy:32});g.setDefaultEdgeLabel(()=>({}));const W=232,H=134;nodes.forEach(n=>g.setNode(n.id,{width:W,height:H}));edges.forEach(e=>{try{g.setEdge(e.from,e.to);}catch(err){}});window.dagre.layout(g);const pos={};nodes.forEach(n=>{const p=g.node(n.id);if(p)pos[n.id]={x:p.x-W/2,y:p.y-H/2};});return pos;}
function renderFlowGraph(){const wrap=document.getElementById('flowCanvas');const errBox=document.getElementById('flowError');if(errBox){errBox.style.display='none';errBox.textContent='';}if(!wrap)return;const g=flowGraphData();const nodes=g.nodes||[];const edges=g.edges||[];if(state.flowGraphWfId&&state.flowGraphWfId!==state.workflowId)destroyFlowGraph();if(!nodes.length){destroyFlowGraph();if(errBox){errBox.style.display='block';errBox.textContent='当前工作流暂无可用节点定义（legacy 或空定义，已 fail-soft，不伪造连线）。';}return;}if(typeof window.dagre==='undefined'||!window.dagre.graphlib||!window.dagre.layout){destroyFlowGraph();if(errBox){errBox.style.display='block';errBox.textContent='本地 Dagre 资源缺失（/static/vendor/dagre-3.1.1.min.js），请检查 Console 静态资源。页面其他功能不受影响。';}return;}if(typeof window.X6==='undefined'||!window.X6.Graph){destroyFlowGraph();if(errBox){errBox.style.display='block';errBox.textContent='本地 X6 资源缺失（/static/vendor/x6-3.1.8.min.js），请检查 Console 静态资源。页面其他功能不受影响。';}return;}if(!ensureFlowCardShape()){destroyFlowGraph();if(errBox){errBox.style.display='block';errBox.textContent='本地 X6 缺少 HTML 节点（Shape.HTML），无法绘制流程卡片。';}return;}let pos={};try{pos=dagreLayoutPositions(nodes,edges);}catch(e){destroyFlowGraph();if(errBox){errBox.style.display='block';errBox.textContent='Dagre 布局失败：'+e.message;}return;}try{destroyFlowGraph();const W=wrap.clientWidth||800;const H=480;const graph=new window.X6.Graph({container:wrap,width:W,height:H,background:{color:'transparent'},panning:{enabled:true},mousewheel:{enabled:true,modifiers:['ctrl','meta'],minScale:0.45,maxScale:2.0},interacting:{nodeMovable:false,edgeMovable:false,edgeLabelMovable:false,arrowheadMovable:false,vertexMovable:false,vertexAddable:false,vertexDeletable:false,edgeAddable:false},connecting:{allowBlank:false,allowLoop:false,allowNode:false,allowEdge:false,snap:true},highlighting:{magnetAdsorbed:{name:'stroke',args:{padding:4,attrs:{stroke:'#5e6ad2','stroke-width':2}}}}});state.flowGraph=graph;state.flowGraphWfId=state.workflowId;const nodesMap={};nodes.forEach(n=>{nodesMap[n.id]=n;});const activeIncomingNodes=new Set();const activeOutgoingNodes=new Set();edges.forEach(e=>{const fromN=nodesMap[e.from];const toN=nodesMap[e.to];if(fromN&&toN){if(toN.status==='working'&&(fromN.status==='completed'||fromN.status==='working')){activeIncomingNodes.add(toN.id);activeOutgoingNodes.add(fromN.id);}}});nodes.forEach(n=>{const p=pos[n.id]||{x:20,y:20};const nodeData=Object.assign({},n,{selected:String(state.flowSelectedNodeId||'')===String(n.id),inbound_active:activeIncomingNodes.has(n.id),outbound_active:activeOutgoingNodes.has(n.id),has_upstream:Boolean(n.depends_on&&n.depends_on.length),has_downstream:Boolean(n.downstream&&n.downstream.length)});graph.addNode({id:n.id,shape:'flow-card',x:p.x,y:p.y,width:232,height:134,data:nodeData});});edges.forEach(e=>{try{const fromN=nodesMap[e.from];const toN=nodesMap[e.to];const isAct=Boolean(fromN&&toN&&toN.status==='working'&&(fromN.status==='completed'||fromN.status==='working'));const isDone=Boolean(fromN&&toN&&fromN.status==='completed'&&toN.status==='completed');const strokeColor=isAct?'#4f46e5':isDone?'#94a3b8':'#c5c9d3';const strokeWidth=isAct?2.2:1.6;graph.addEdge({source:{cell:e.from,anchor:{name:'right',args:{dx:0}},connectionPoint:'anchor'},target:{cell:e.to,anchor:{name:'left',args:{dx:-5}},connectionPoint:'anchor'},connector:{name:'smooth'},attrs:{line:{stroke:strokeColor,'stroke-width':strokeWidth,strokeDasharray:isAct?'6 4':'none',style:isAct?{animation:'flowEdgeDash 1.2s linear infinite'}:{},targetMarker:{name:'block',size:isAct?7:6,fill:strokeColor,stroke:strokeColor}}},zIndex:isAct?10:1});}catch(err){}});graph.on('node:click',({node})=>{try{selectFlowNode(node.id);}catch(err){}});updateFlowSelection();fitFlowGraph();requestAnimationFrame(()=>{resizeFlowGraph();fitFlowGraph();});}catch(e){destroyFlowGraph();if(errBox){errBox.style.display='block';errBox.textContent='Flow Canvas 初始化失败：'+e.message;}}}
function flowNodeById(id){const g=flowGraphData();return (g.nodes||[]).find(n=>n.id===id)||null;}
function flowTasksForNode(nodeId){const ts=(state.workflow&&state.workflow.tasks)||[];return ts.filter(t=>String(t.node||t.stage||'')===String(nodeId)&&t.status!=='superseded'&&!t.superseded_by);}
function renderNodeInspector(){const body=document.getElementById('flowInspectorBody');const title=document.getElementById('flowInspTitle');const meta=document.getElementById('flowInspMeta');if(!body)return;const node=flowNodeById(state.flowSelectedNodeId);if(!node){if(title)title.textContent='节点详情';if(meta)meta.textContent='—';body.innerHTML='<div class="empty">暂无节点</div>';return;}if(title)title.textContent=cleanStageLabel(node.label||node.id);if(meta)meta.textContent=(node.node_type||'agent').toUpperCase()+' · '+flowNodeStatus(node,humanNodeStatus(node.status));const tab=state.flowInspectorTab||'summary';if(tab==='tasks'){const nts=flowTasksForNode(node.id);body.innerHTML=nts.length?nts.map(t=>`<div class="flow-task-row" onclick="openTaskDrawer('${esc(t.task_id)}')"><span><strong>${esc(t.task_id)}</strong><span class="muted"> · ${esc(t.agent||'未分配')}</span></span><span>${badge(t.status)}</span></div>`).join(''):'<div class="empty">该节点暂无任务</div>';return;}if(tab==='context'){const ctx=(state.workflow&&state.workflow.context)||{required:[],optional:[]};const req=ctx.required||[];const opt=ctx.optional||[];body.innerHTML=`<div class="proj-sec"><div class="proj-lbl">必需上下文</div>${req.length?req.map(c=>`<div>· ${esc(typeof c==='string'?c:(c.id||c))}</div>`).join(''):'<div class="muted">—</div>'}</div><div class="proj-sec"><div class="proj-lbl">可选上下文</div>${opt.length?opt.map(c=>`<div>· ${esc(typeof c==='string'?c:(c.id||c))}</div>`).join(''):'<div class="muted">—</div>'}</div><div class="muted" style="margin-top:8px">仅显示真实 Context Contract；无绑定信息时不伪造已加载。</div>`;return;}if(tab==='runtime'){const nts=flowTasksForNode(node.id);body.innerHTML=nts.length?nts.map(t=>`<div class="flow-node-card" style="margin-bottom:8px"><div><strong>${esc(t.task_id)}</strong> ${badge(t.status)}</div><dl class="flow-kv" style="margin-top:8px"><dt>执行者</dt><dd>${esc(t.agent||'—')}</dd><dt>工位</dt><dd>${esc(t.pane_id||'—')}</dd><dt>更新</dt><dd>${esc(t.updated_at||t.last_activity_at||'—')}</dd><dt>分支</dt><dd>${esc(t.candidate_sha||t.integration_branch||'—')}</dd><dt>结论</dt><dd>${esc(t.stage_verdict||'—')}</dd></dl></div>`).join(''):'<div class="empty">暂无运行时信息</div>';return;}const nts=flowTasksForNode(node.id);const acts=(state.controllerActionsData&&state.controllerActionsData.actions)||[];const mine=acts.filter(a=>((a.blocker_task_id||a.old_task_id||String(a.action_id||'').split(':')[0])===node.id)||(node.task_ids||[]).includes(a.blocker_task_id||a.old_task_id));const shown=mine.slice(0,2);const actHtml=shown.length?`<div class="flow-attn"><div class="flow-sec">需要处理</div>${shown.map(a=>`<div class="flow-node-card" style="margin-bottom:8px"><div><strong>${esc(a.title||a.action_id)}</strong></div><div class="muted" style="margin:4px 0">${esc(a.effect||a.description||'')}</div><div style="display:flex;gap:8px;justify-content:flex-end"><button class="mini" onclick="locateControllerTask('${esc(a.blocker_task_id||a.old_task_id||'')}')">查看任务</button><button class="btn primary" style="padding:3px 8px;font-size:11.5px" onclick="executeControllerAction('${esc(a.action_id)}','${esc(state.workflowId)}')">一键执行</button></div></div>`).join('')}</div>`:(node.has_attention?'<div class="muted">该节点需关注，暂无可用一键解卡建议，请查看任务详情。</div>':'');body.innerHTML=flowOverviewHtml(node, nts)+actHtml;}
window.addEventListener('resize',()=>{try{if(state.workflowView!=='list'){resizeFlowGraph();fitFlowGraph();}}catch(e){}});

function renderAgents(){const rs=state.project.agents||[];document.getElementById('agents').innerHTML=rs.length?rs.map(a=>`<div class="agent-row"><span><i class="dot ${esc(a.status)}"></i>${esc(a.agent)}</span><span class="muted">${esc(agentStatusLabel(a.status))} · 负载 ${a.load} · 认证 ${esc(authHintLabel(a.auth_hint))}</span></div>`).join(''):'<div class="empty">暂无执行者信息</div>'}function renderSlots(){const rs=state.project.slots||[];document.getElementById('slots').innerHTML=rs.length?rs.map(s=>`<div class="slot-row"><div><div>${esc(s.pane_id)} · ${esc(cleanStageLabel(s.stage_label))}</div><div class="task-meta">绑定 ${esc(s.bound_agent)} · 运行时 ${esc(s.live_agent||'空闲')} · ${esc(s.claimed_by?'被任务占用':'未占用')}</div></div><button class="mini" onclick="bindSlotPrompt('${esc(s.pane_id)}')">绑定</button></div>`).join(''):'<div class="empty">暂无用户预建智能体工位</div>'}
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
  openModal('注销工厂项目',`<div style="line-height:1.6"><div style="font-size:15px;font-weight:650;margin-bottom:8px">确定要注销项目【${esc(p.project_name||p.project_id)}】吗？</div><div class="muted" style="font-size:13px;margin-bottom:16px">项目目录：<code>${esc(p.project_root)}</code><br>当前空间：<b>${esc(p.workspace_id||'')}</b></div><div style="background:var(--bg-subtle);border:1px solid var(--border-default);border-radius:10px;padding:12px;font-size:12px;margin-bottom:16px;color:var(--text-primary)"><div style="font-weight:600;margin-bottom:4px">注销影响说明：</div><div>1. <b>本地代码仓库绝对不碰</b>，保留所有代码与 Git 提交。</div><div>2. 工厂调度器将停止对该项目的自动任务派发与状态巡检。</div><div>3. 终端空间将从【当前工厂 <span class="dot ready" style="vertical-align:middle"></span>】退回为【独立终端 <span class="dot working" style="vertical-align:middle"></span>】。</div></div><label for="unregCloseSpace" style="display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer;margin-bottom:16px"><input type="checkbox" id="unregCloseSpace"> 同时关闭终端工作区（仅关闭窗口标签页，不删除代码）</label><div style="display:flex;justify-content:flex-end;gap:8px"><button class="btn" onclick="closeModal()">取消</button><button class="btn danger-btn" onclick="submitUnregisterProject()">确认注销</button></div></div>`);
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
  openModal('新建需求',`<div class="form"><label for="newProjName">项目</label><input id="newProjName" value="${esc(state.project.project.project_name)}" disabled><label for="newTitle">本次任务名称</label><input id="newTitle" placeholder="例如：适配深色模式切换 / 修复结算页面浮点精度 Bug"><label for="newTemplate">工作流模板</label><select id="newTemplate"><option value="software-development-v1">software-development-v1（默认软件开发）</option></select><label for="newAgent">执行者策略</label><select id="newAgent"><option value="auto">auto（Router 自动）</option>${['opencode','codex','claude','qodercli','agy','pi','grok','kimi'].map(a=>`<option>${a}</option>`).join('')}</select><label for="newRequirement">自然语言需求</label><textarea id="newRequirement" onblur="autoFillWorkflowTitle()"></textarea><button class="btn primary" onclick="submitNewWorkflowAsync()">启动工作流</button><div id="runWaitStatus" class="muted" style="min-height:16px;font-size:12px"></div></div>`);
  populateTemplateSelect()
}
let preflightRunId=0;
const preflightState={total:0,completed:0,ready:0,warn:0,error:0,disabled:0,results:{}};
const preflightStatusLabel=s=>({
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
const preflightHardFailures=new Set([
  'TOKEN_EXHAUSTED','AUTH_REQUIRED','PROVIDER_ERROR','LOCAL_ERROR','TRUST_REQUIRED',
  'UPDATE_BLOCKED','TIMEOUT','ERROR','MISSING'
]);
function buildPreflightRowHtml(a){
  const agent=a.agent||a;
  const auth=authHintLabel(a.auth_hint||'unknown');
  return `<div class="preflight-row" id="pf-row-${esc(agent)}">`
    +`<div style="flex:1;min-width:0">`
    +`<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">`
    +`<i class="dot" id="pf-dot-${esc(agent)}"></i>`
    +`<strong>${esc(agent)}</strong> `
    +`<span id="pf-badge-${esc(agent)}" class="badge waiting">等待检测…</span>`
    +`<span id="pf-spin-${esc(agent)}" class="spinner" style="width:11px;height:11px;border-width:1.5px;display:none"></span>`
    +`</div>`
    +`<div class="task-meta" id="pf-note-${esc(agent)}" style="margin-top:4px">准备执行最小探针…</div>`
    +`<div id="pf-output-${esc(agent)}"></div>`
    +`</div>`
    +`<div style="display:flex;flex-direction:column;align-items:flex-end;gap:8px;flex-shrink:0">`
    +`<span class="task-meta" id="pf-auth-${esc(agent)}">${esc(auth)}</span>`
    +`<div id="pf-action-${esc(agent)}"></div>`
    +`</div>`
    +`</div>`;
}
function updateAgentPreflightResult(agent,row){
  const deep=row.deep||{};
  const final=row.final_status||row.shallow_status||'UNKNOWN';
  let dotCls='';
  let badgeCls='waiting';
  let rowCls='';
  if(final==='READY'){
    dotCls='ready';badgeCls='cleaned';rowCls='is-ready';
  }else if(preflightHardFailures.has(final)){
    dotCls='failed';badgeCls='failed';rowCls='is-failed';
  }else if(final==='WARN'){
    dotCls='working';badgeCls='working';rowCls='is-warn';
  }else if(final==='DISABLED'){
    dotCls='disabled';badgeCls='superseded';
  }
  const note=deep.note||row.version||row.binary||(final==='MISSING'?'未安装':'');
  const adapter=deep.adapter?(' · '+esc(deep.adapter)):'';
  const out=(deep.output||'').slice(-800);
  const outHtml=out?('<pre class="ctl-pre-light">'+esc(out)+'</pre>'):'';
  const rowEl=document.getElementById('pf-row-'+agent);
  if(rowEl)rowEl.className='preflight-row '+rowCls;
  const dotEl=document.getElementById('pf-dot-'+agent);
  if(dotEl)dotEl.className='dot '+dotCls;
  const badgeEl=document.getElementById('pf-badge-'+agent);
  if(badgeEl){badgeEl.className='badge '+badgeCls;badgeEl.textContent=preflightStatusLabel(final)}
  const spinEl=document.getElementById('pf-spin-'+agent);
  if(spinEl)spinEl.style.display='none';
  const noteEl=document.getElementById('pf-note-'+agent);
  if(noteEl)noteEl.innerHTML=esc(note)+adapter;
  const outEl=document.getElementById('pf-output-'+agent);
  if(outEl)outEl.innerHTML=outHtml;
  const authEl=document.getElementById('pf-auth-'+agent);
  if(authEl)authEl.textContent=authHintLabel(row.auth_hint||'unknown');
  const actEl=document.getElementById('pf-action-'+agent);
  if(actEl)actEl.innerHTML=`<button class="mini" onclick="retrySinglePreflight('${esc(agent)}')">重试</button>`;
  preflightState.results[agent]=row;
  updatePreflightSummary();
}
function updatePreflightSummary(){
  const rs=Object.values(preflightState.results);
  const completed=rs.length;
  const total=preflightState.total;
  let ready=0,warn=0,err=0,dis=0;
  for(const r of rs){
    const f=r.final_status||r.shallow_status||'UNKNOWN';
    if(f==='READY')ready++;
    else if(preflightHardFailures.has(f))err++;
    else if(f==='WARN')warn++;
    else if(f==='DISABLED')dis++;
  }
  const pct=total?Math.min(100,Math.round((completed/total)*100)):0;
  const bar=document.getElementById('preflightProgressBar');
  if(bar)bar.style.width=pct+'%';
  const cnt=document.getElementById('preflightCounter');
  if(cnt)cnt.textContent=`${completed} / ${total}`;
  const sReady=document.getElementById('preflightStatReady');
  if(sReady){sReady.style.display=ready?'inline':'none';sReady.textContent=`${ready} 就绪`}
  const sWarn=document.getElementById('preflightStatWarn');
  if(sWarn){sWarn.style.display=warn?'inline':'none';sWarn.textContent=`${warn} 警告`}
  const sErr=document.getElementById('preflightStatError');
  if(sErr){sErr.style.display=err?'inline':'none';sErr.textContent=`${err} 异常`}
  const sDis=document.getElementById('preflightStatDisabled');
  if(sDis){sDis.style.display=dis?'inline':'none';sDis.textContent=`${dis} 已禁用`}
  if(completed>=total&&total>0){
    const topSpin=document.getElementById('preflightTopSpinner');
    if(topSpin)topSpin.style.display='none';
    const titleEl=document.getElementById('preflightSummaryTitle');
    if(titleEl)titleEl.innerHTML=`<span class="good-text">✓ 全部执行者自检已完成</span>`;
    const btn=document.getElementById('preflightRerunAllBtn');
    if(btn)btn.disabled=false;
    toast('深度自检完成');
  }
}
async function executeSinglePreflight(agent,runId){
  const dotEl=document.getElementById('pf-dot-'+agent);
  if(dotEl)dotEl.className='dot working';
  const badgeEl=document.getElementById('pf-badge-'+agent);
  if(badgeEl){badgeEl.className='badge in_progress';badgeEl.textContent='检测中…'}
  const spinEl=document.getElementById('pf-spin-'+agent);
  if(spinEl)spinEl.style.display='inline-block';
  const noteEl=document.getElementById('pf-note-'+agent);
  if(noteEl)noteEl.textContent='正在发起沙盒探针与真实最小调用…';
  const actEl=document.getElementById('pf-action-'+agent);
  if(actEl)actEl.innerHTML='';
  try{
    const res=await api('/api/deep-preflight?id='+encodeURIComponent(state.projectId)+'&agent='+encodeURIComponent(agent));
    if(runId&&runId!==preflightRunId)return;
    const row=(res.agents&&res.agents[0])||{agent:agent,final_status:'UNKNOWN'};
    updateAgentPreflightResult(agent,row);
  }catch(e){
    if(runId&&runId!==preflightRunId)return;
    updateAgentPreflightResult(agent,{agent:agent,final_status:'ERROR',deep:{attempted:true,status:'ERROR',note:'探针执行异常: '+e.message,output:e.stack||''}});
  }
}
async function retrySinglePreflight(agent){
  if(!state.projectId)return toast('当前空间不参与工厂调度',true);
  toast(`正在重新自检 ${agent}…`);
  delete preflightState.results[agent];
  updatePreflightSummary();
  await executeSinglePreflight(agent,0);
  toast(`${agent} 自检完成`);
}
async function runPreflight(){
  if(!state.projectId)return toast('当前空间不参与工厂调度',true);
  preflightRunId=Date.now();
  const currentRunId=preflightRunId;
  const agentItems=(state.project&&state.project.agents&&state.project.agents.length)
    ?state.project.agents
    :['opencode','codex','claude','qodercli','agy','pi','grok','kimi'].map(a=>({agent:a,auth_hint:'unknown'}));
  const agentNames=agentItems.map(a=>a.agent);
  preflightState.total=agentNames.length;
  preflightState.completed=0;
  preflightState.ready=0;
  preflightState.warn=0;
  preflightState.error=0;
  preflightState.disabled=0;
  preflightState.results={};
  const html='<div class="preflight-box">'
    +'<div class="muted" style="font-size:12px;line-height:1.5">'
    +'真实最小调用仅用于已确认安全非交互模式的执行者；UNKNOWN 表示尚未配置安全适配器，不代表不可用。<br>'
    +'TIMEOUT 可能是慢而非不可用（claude 冷启动常需 40–60s，已自动重试 1 次），请结合下方原始输出判断，必要时点一次“执行者自检”重试。'
    +'</div>'
    +'<div class="preflight-head">'
    +'<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">'
    +'<div style="display:flex;align-items:center;gap:8px">'
    +'<span id="preflightTopSpinner" class="spinner"></span>'
    +'<strong id="preflightSummaryTitle">正在逐个自检执行者可用情况…</strong>'
    +'<span class="muted" id="preflightCounter" style="font-size:12px">0 / '+agentNames.length+'</span>'
    +'</div>'
    +'<button class="mini" id="preflightRerunAllBtn" onclick="runPreflight()" disabled>全部重新自检</button>'
    +'</div>'
    +'<div class="preflight-prog">'
    +'<div id="preflightProgressBar" class="preflight-prog-fill" style="width:0%"></div>'
    +'</div>'
    +'<div id="preflightStats" class="task-meta" style="margin-top:8px;display:flex;gap:12px;font-size:11.5px">'
    +'<span id="preflightStatReady" class="good-text" style="display:none">0 就绪</span>'
    +'<span id="preflightStatWarn" class="warn-text" style="display:none">0 警告</span>'
    +'<span id="preflightStatError" class="danger-text" style="display:none">0 异常</span>'
    +'<span id="preflightStatDisabled" class="muted" style="display:none">0 已禁用</span>'
    +'</div>'
    +'</div>'
    +'<div id="preflightRows" style="display:grid;gap:8px">'
    +agentItems.map(buildPreflightRowHtml).join('')
    +'</div>'
    +'</div>';
  openModal('执行者深度自检',html);
  agentNames.forEach((agentName,idx)=>{
    setTimeout(()=>{
      if(preflightRunId!==currentRunId)return;
      executeSinglePreflight(agentName,currentRunId);
    },idx*100);
  });
}
function showAgentOverride(){if(!state.workflowId)return toast('当前没有工作流',true);const cur=state.workflow.agent_override||'auto';openModal('指定后续任务执行者',`<div class="form"><label for="overrideAgent">执行者策略</label><select id="overrideAgent">${['auto','opencode','codex','claude','qodercli','agy','pi','grok','kimi'].map(a=>`<option ${a===cur?'selected':''}>${a}</option>`).join('')}</select><button class="btn primary" onclick="saveAgentOverride()">保存</button><div class="muted">只影响后续新建任务。</div></div>`)}async function saveAgentOverride(){try{await api('/api/workflow/agent',{method:'POST',body:JSON.stringify({workflow_id:state.workflowId,agent:document.getElementById('overrideAgent').value})});closeModal();await loadWorkflow(state.workflowId);toast('执行者策略已更新')}catch(e){toast(e.message,true)}}function taskDrawerNumTs(v){const n=parseFloat(v);return Number.isFinite(n)?n:null}
function fmtClock(ts){const n=taskDrawerNumTs(ts);if(n===null)return '';const ms=n>1e12?n:n*1000;const d=new Date(ms);if(isNaN(d.getTime()))return '';return d.toLocaleTimeString('zh-CN',{hour12:false})}
function taskExecStart(t){t=t||{};const rt=t.runtime||{};return taskDrawerNumTs(t.started_at??rt.started_at)}
function taskDisplayStart(t){const e=taskExecStart(t);if(e!==null)return {label:'开始时间',ts:e};const c=taskDrawerNumTs((t||{}).created_at);if(c!==null)return {label:'创建时间',ts:c};return null}
function taskUpdatedAt(t){t=t||{};return taskDrawerNumTs(t.updated_at??t.last_activity_at)}
function taskDurationSecs(t){const s=taskExecStart(t);const u=taskUpdatedAt(t);if(s===null||u===null)return null;return Math.max(0,Math.round(u-s))}
function canSteerTask(t){return ['working','dispatched','rework','blocked','paused'].includes((t||{}).status)}
function canForceReviewTask(t){return (t||{}).status==='rework'}
function canForcePassTask(t){return (t||{}).stage_verdict==='blocked'}
function buildTaskEvents(task,proj){
  task=task||{};proj=proj||{};
  const evs=[];
  const push=(type,timestamp,title,detail,tone)=>{evs.push({type,timestamp:timestamp??null,title:title||type,detail:detail||'',tone:tone||'default'})};
  const created=taskDrawerNumTs(task.created_at);
  if(created!==null)push('task_created',created,'任务已创建','', 'default');
  const MAP={pending:['task_created','任务已创建'],dispatched:['task_dispatched','已派发'],working:['agent_started','执行者开始执行'],paused:['task_dispatched','已暂停'],blocked:['blocked','任务已阻塞'],failed:['failed','任务失败'],rework:['rework','返工中'],agent_done:['agent_done','执行者已完成'],completed:['completed','任务完成'],committed:['commit_created','已提交'],integrated:['completed','已集成'],cleanup_ready:['completed','待归档'],cleaned:['completed','已完成']};
  const hist=Array.isArray(task.status_history)?task.status_history:[];
  for(const h of hist){
    if(!h||typeof h!=='object')continue;
    const st=h.to||h.status;
    const at=taskDrawerNumTs(h.at);
    if(!st||at===null)continue;
    const m=MAP[st]||['status_changed',humanStatus(st)];
    if(m[0]==='task_created'&&evs.some(e=>e.type==='task_created'&&e.timestamp!==null&&Math.abs(e.timestamp-at)<2))continue;
    const tone=st==='failed'?'bad':(st==='blocked'?'warn':(['completed','committed','integrated','cleaned'].includes(st)?'done':(['working','dispatched','rework'].includes(st)?'info':'default')));
    const from=h.from?('由 '+h.from+' → '+st):'';
    push(m[0],at,m[1],from,tone);
  }
  const rt=task.runtime||{};
  const rtStart=taskDrawerNumTs(rt.started_at);
  if(rtStart!==null&&!evs.some(e=>e.type==='runtime_ready'))push('runtime_ready',rtStart,'运行现场就绪',(task.pane_id?('工位 '+task.pane_id):''),'info');
  const upd=taskUpdatedAt(task);
  const blockers=Array.isArray(proj.blockers)?proj.blockers:(proj.blocker?[proj.blocker]:((task.blocker||task.blocked_reason)?[task.blocker||task.blocked_reason]:[]));
  const realBlockers=blockers.filter(b=>b&&String(b).trim());
  if(realBlockers.length&&upd!==null&&!evs.some(e=>(e.type==='blocked'||e.type==='failed')&&e.timestamp!==null&&Math.abs(e.timestamp-upd)<2))push('blocked',upd,'任务已阻塞',String(realBlockers[0]),'warn');
  let acts=proj.recent_activity;
  if(typeof acts==='string'&&acts)acts=acts.split('\n');
  if(Array.isArray(acts)){for(const a of acts.slice(0,6)){if(a&&String(a).trim())push('observation',null,String(a).trim(),'', 'default')}}
  const timed=evs.filter(e=>e.timestamp!==null).sort((a,b)=>a.timestamp-b.timestamp);
  const untimed=evs.filter(e=>e.timestamp===null);
  return timed.concat(untimed);
}
function renderTaskOverview(task,proj){
  task=task||{};proj=proj||{};
  const goal=task.goal||proj.goal||proj.intent||'';
  const result=task.last_result||task.result||'';
  const blocker=proj.blocker||task.blocker||task.blocked_reason||'';
  const verdict=task.stage_verdict||'';
  const note=task.stage_verdict_note||'';
  let h='';
  if(goal)h+=`<div class="td-sec"><div class="td-lbl">目标</div><div class="td-txt">${esc(goal)}</div></div>`;
  if(result){h+=`<div class="td-sec"><div class="td-lbl">结果</div><div class="td-txt">${esc(typeof result==='object'?JSON.stringify(result,null,2):result)}</div></div>`}
  else{h+=`<div class="td-sec"><div class="td-lbl">结果</div><div class="empty" style="padding:8px 0">当前任务尚未产生结果</div></div>`}
  h+=`<div class="td-sec"><div class="td-lbl">状态</div><div>${badge(task.status||'unknown')}</div></div>`;
  if(blocker)h+=`<div class="td-sec"><div class="td-lbl">阻塞原因</div><div class="td-txt">${esc(blocker)}</div></div>`;
  if(verdict)h+=`<div class="td-sec"><div class="td-lbl">阶段结论</div><div class="td-txt">${esc(verdict)}${note?' · '+esc(note):''}</div></div>`;
  return h;
}
function renderTaskActivity(task,proj){
  const evs=buildTaskEvents(task,proj);
  if(!evs.length)return '<div class="empty">暂无可用活动记录</div>';
  return `<div class="td-timeline">${evs.map((e,i)=>{
    const t=e.timestamp!==null?fmtClock(e.timestamp):'·';
    const dot=e.tone==='done'?'done':e.tone==='warn'?'warn':e.tone==='bad'?'bad':e.tone==='info'?'info':'';
    const line=i<evs.length-1?'<div class="td-ev-line"></div>':'';
    return `<div class="td-ev"><div class="td-ev-time">${esc(t)}</div><div class="td-ev-rail"><span class="td-dot ${dot}"></span>${line}</div><div class="td-ev-body"><div class="td-ev-title">${esc(e.title)}</div>${e.detail?`<div class="td-ev-detail">${esc(e.detail)}</div>`:''}</div></div>`;
  }).join('')}</div>`;
}
function renderTaskArtifacts(task,proj){
  task=task||{};proj=proj||{};
  const arts=Array.isArray(proj.artifacts)?proj.artifacts:[];
  const cr=task.commit_result;
  const commitSha=task.commit||task.integrated_commit||((cr&&typeof cr==='object')?(cr.commit||cr.sha||''):(typeof cr==='string'?cr:''))||'';
  const commitMsg=((cr&&typeof cr==='object')?(cr.message||cr.summary||''):'')||task.commit_basis||'';
  const branch=task.branch||task.integration_branch||'';
  if(!arts.length&&!commitSha&&!branch)return '<div class="empty">当前任务尚未产生可展示产物</div>';
  let h='';
  if(commitSha)h+=`<div class="td-sec"><div class="td-lbl">提交</div><div class="td-txt" style="font-family:ui-monospace,Menlo,monospace">${esc(String(commitSha).slice(0,12))}</div>${commitMsg?`<div class="td-ev-detail">${esc(String(commitMsg).slice(0,200))}</div>`:''}</div>`;
  if(branch)h+=`<div class="td-sec"><div class="td-lbl">分支</div><div class="td-txt" style="font-family:ui-monospace,Menlo,monospace">${esc(branch)}</div></div>`;
  for(const a of arts){
    const files=Array.isArray(a.files)&&a.files.length?`<div class="td-ev-detail">${esc(a.files.slice(0,10).join(', '))}${a.files_changed?` · 共 ${a.files_changed} 个文件`:''}</div>`:'';
    h+=`<div class="td-art"><div class="td-art-hd"><span>${esc(a.name||a.kind||'产物')}</span><span class="badge ${a.passed?'cleaned':(a.kind==='evaluation'?'failed':'waiting')}">${esc(a.kind||'')}</span></div>${a.summary?`<div class="td-ev-detail">${esc(a.summary)}</div>`:''}${a.path?`<div class="td-ev-detail" style="font-family:ui-monospace,Menlo,monospace">${esc(a.path)}</div>`:''}${files}</div>`;
  }
  return h;
}
function renderTaskRuntime(task,proj){
  task=task||{};const rt=task.runtime||{};const live=(state.selectedTaskDetail&&state.selectedTaskDetail.live)||{};
  const rows=[];
  const add=(k,v)=>{if(v!==null&&v!==undefined&&String(v).trim()!=='')rows.push([k,String(v)])};
  add('执行者',task.agent||live.agent||'');
  add('执行者会话',rt.agent_session_id||rt.agent_name||live.agent_session_id||'');
  add('项目空间',task.workspace_id||rt.workspace_id||'');
  add('工作流节点',task.tab_id||rt.tab_id||'');
  add('智能体工位',task.pane_id||rt.pane_id||'');
  add('运行 ID',task.run_id||'');
  add('分支',task.branch||'');
  add('运行状态',rt.status||live.agent_status||'');
  const ds=taskDisplayStart(task);
  if(ds)add(ds.label,fmtClock(ds.ts));
  if(task.clone_path)add('代码目录',task.clone_path);
  if(!rows.length)return '<div class="empty">暂无运行时信息</div>';
  return `<div class="td-attrs">${rows.map(r=>`<div class="td-attr-k">${esc(r[0])}</div><div class="td-attr-v">${esc(r[1])}</div>`).join('')}</div>`;
}
function taskDrawerMenuHtml(t){
  t=t||{};
  const id=esc(t.task_id||'');
  const rt=(t.runtime||{});
  const paneId=esc(t.pane_id||rt.pane_id||'');
  let h='';
  if(paneId)h+=`<button class="task-dropdown-item" onclick="closeTaskDrawerMenu();showPane('${paneId}')">查看工位</button>`;
  h+=`<button class="task-dropdown-item" onclick="closeTaskDrawerMenu();askCoordinator('${id}')">让总指挥处理</button>`;
  if(canSteerTask(t)){h+=`<div class="task-dropdown-divider"></div><button class="task-dropdown-item" style="color:var(--primary)" onclick="closeTaskDrawerMenu();showSteerModal('${id}')">留话指导</button><button class="task-dropdown-item danger" onclick="closeTaskDrawerMenu();haltTaskPrompt('${id}')">紧急叫停</button>`}
  if(canForceReviewTask(t)){h+=`<div class="task-dropdown-divider"></div><button class="task-dropdown-item" style="color:var(--warning);font-weight:600" onclick="closeTaskDrawerMenu();forceReviewTask('${id}')">唤醒评审</button>`}
  if(canForcePassTask(t)){h+=`<div class="task-dropdown-divider"></div><button class="task-dropdown-item primary" onclick="closeTaskDrawerMenu();forcePassTask('${esc(t.workflow_id||state.workflowId||'')}','${esc(t.node||t.stage||'')}',{task_id:'${esc(t.task_id||id||'')}',expected_version:${t.version!==undefined&&t.version!==null?t.version:'null'},expected_pane_id:'${esc(paneId||t.pane_id||'')}',pane_id:'${esc(paneId||t.pane_id||'')}'})">强制放行</button>`}
  return h;
}
function toggleTaskDrawerMenu(e){if(e)e.stopPropagation();const m=document.getElementById('taskDrawerMenu');if(m)m.classList.toggle('open')}
function closeTaskDrawerMenu(){const m=document.getElementById('taskDrawerMenu');if(m)m.classList.remove('open')}
function renderTaskDrawer(){
  const c=state.selectedTaskDetail;
  const drawer=document.getElementById('taskDrawer');
  if(!c||!drawer)return;
  const task=c.task||{};const proj=c.proj||{};
  const title=taskDisplayName(task);
  const dur=taskDurationSecs(task);
  document.getElementById('taskDrawerIcon').textContent=['completed','committed','integrated','cleaned'].includes(task.status)?'✓':(['blocked','failed'].includes(task.status)?'!':(['working','dispatched','rework'].includes(task.status)?'●':'○'));
  document.getElementById('taskDrawerTitle').textContent=title;
  document.getElementById('taskDrawerId').textContent=task.task_id||'';
  const parts=[];
  if(task.status)parts.push(humanStatus(task.status));
  if(task.agent)parts.push(task.agent);
  if(dur!==null)parts.push(formatElapsed(dur));
  document.getElementById('taskDrawerMeta').textContent=parts.join(' · ');
  const primary=document.getElementById('taskDrawerPrimary');
  if(primary)primary.onclick=()=>openSignoffChamber(task.task_id);
  document.getElementById('taskDrawerMenu').innerHTML=taskDrawerMenuHtml(task);
  const stage=task.stage_label||task.stage||task.node_label||task.node||'';
  const rt=task.runtime||{};
  const rtShort=[task.workspace_id||rt.workspace_id||'',task.pane_id||rt.pane_id||''].filter(Boolean).join(' / ');
  const ds=taskDisplayStart(task);
  const attrRows=[];
  if(task.agent)attrRows.push(['执行者',task.agent]);
  if(stage)attrRows.push(['阶段',stage]);
  if(task.status)attrRows.push(['状态',humanStatus(task.status)]);
  if(rtShort)attrRows.push(['运行现场',rtShort]);
  if(ds)attrRows.push([ds.label,fmtClock(ds.ts)]);
  if(dur!==null)attrRows.push(['耗时',formatElapsed(dur)]);
  const tabs=['overview','activity','artifacts','runtime'];
  const labels={overview:'tdTabOverview',activity:'tdTabActivity',artifacts:'tdTabArtifacts',runtime:'tdTabRuntime'};
  for(const k of tabs){const el=document.getElementById(labels[k]);if(el){const on=state.taskDrawerTab===k;el.classList.toggle('active',on);el.setAttribute('aria-selected',on?'true':'false')}}
  let body='';
  if(attrRows.length)body+=`<div class="td-attrs">${attrRows.map(r=>`<div class="td-attr-k">${esc(r[0])}</div><div class="td-attr-v">${esc(r[1])}</div>`).join('')}</div>`;
  if(state.taskDrawerTab==='activity')body+=renderTaskActivity(task,proj);
  else if(state.taskDrawerTab==='artifacts')body+=renderTaskArtifacts(task,proj);
  else if(state.taskDrawerTab==='runtime')body+=renderTaskRuntime(task,proj);
  else body+=renderTaskOverview(task,proj);
  document.getElementById('taskDrawerBody').innerHTML=body;
}
function resolveTaskDetail(tid,td,proj,tdError,projError,rowTask){
  if(!td&&!proj){
    const msg=(tdError&&tdError.message)||(projError&&projError.message)||'任务详情加载失败';
    return {status:'error',message:msg};
  }
  const fetched=(td&&td.task)||{};
  const task=Object.assign({},rowTask||{},fetched);
  if(!Object.keys(task).length&&proj)Object.assign(task,{task_id:proj.task_id,workflow_id:proj.workflow_id,node:proj.node,agent:proj.agent,status:proj.status,goal:proj.goal});
  return {status:'ok',task,proj:proj||{},live:((td&&td.runtime)||{})};
}
async function openTaskDrawer(tid){
  if(!tid)return;
  state.selectedTaskId=tid;state.taskDrawerTab='overview';
  const drawer=document.getElementById('taskDrawer');
  if(drawer){drawer.hidden=false;drawer.classList.add('open')}
  document.getElementById('taskDrawerTitle').textContent='加载中…';
  document.getElementById('taskDrawerId').textContent=tid;
  document.getElementById('taskDrawerMeta').textContent='';
  document.getElementById('taskDrawerBody').innerHTML='<div class="empty">正在加载任务详情…</div>';
  document.querySelectorAll('.task.task-highlight').forEach(el=>el.classList.remove('task-highlight'));
  const row=document.querySelector(`[data-task-id="${CSS.escape?CSS.escape(tid):tid}"]`);
  if(row)row.classList.add('task-highlight');
  try{
    const [tdRes,projRes]=await Promise.all([
      api('/api/task?id='+encodeURIComponent(tid)).then(d=>({ok:true,data:d}),e=>({ok:false,error:e})),
      api('/api/task/projection?id='+encodeURIComponent(tid)).then(d=>({ok:true,data:d}),e=>({ok:false,error:e}))
    ]);
    if(state.selectedTaskId!==tid)return;
    const td=tdRes.ok?tdRes.data:null;
    const proj=projRes.ok?projRes.data:null;
    let rowTask=null;
    try{rowTask=((state.workflow&&state.workflow.tasks)||[]).find(t=>t.task_id===tid)||null}catch(e){rowTask=null}
    const r=resolveTaskDetail(tid,td,proj,tdRes.ok?null:tdRes.error,projRes.ok?null:projRes.error,rowTask);
    if(r.status==='error'){
      document.getElementById('taskDrawerTitle').textContent='任务详情加载失败';
      document.getElementById('taskDrawerMeta').textContent='';
      document.getElementById('taskDrawerBody').innerHTML=`<div class="empty">加载失败：${esc(r.message)}</div>`;
      state.selectedTaskDetail=null;
      return;
    }
    state.selectedTaskDetail={task:r.task,proj:r.proj,live:r.live};
    renderTaskDrawer();
  }catch(e){document.getElementById('taskDrawerBody').innerHTML=`<div class="empty">加载失败：${esc(e.message)}</div>`}
}
function closeTaskDrawer(){
  const drawer=document.getElementById('taskDrawer');
  if(!drawer||drawer.hidden)return;
  drawer.classList.remove('open');drawer.hidden=true;
  state.selectedTaskId=null;state.selectedTaskDetail=null;closeTaskDrawerMenu();
  document.querySelectorAll('.task.task-highlight').forEach(el=>el.classList.remove('task-highlight'));
}
function switchTaskDrawerTab(tab){
  state.taskDrawerTab=tab;
  renderTaskDrawer();
  const drawer=document.getElementById('taskDrawer');
  if(drawer&&drawer.hidden){drawer.hidden=false;drawer.classList.add('open')}
}
async function showTask(id){try{const [td,proj]=await Promise.all([api('/api/task?id='+encodeURIComponent(id)).catch(()=>null),api('/api/task/projection?id='+encodeURIComponent(id)).catch(()=>null)]);const d=proj||(td&&td.task)||{};const raw=td||proj||{};const st=d.status||(td&&td.task&&td.task.status)||'unknown';const intent=d.intent||(td&&td.task&&td.task.goal)||'无明确意图描述';const blockers=Array.isArray(d.blockers)?d.blockers:(d.blocker?[d.blocker]:[]);const ms=Array.isArray(d.milestones)?d.milestones:[];const arts=Array.isArray(d.artifacts)?d.artifacts:[];const acts=Array.isArray(d.recent_activity)?d.recent_activity:(typeof d.recent_activity==='string'&&d.recent_activity?d.recent_activity.split('\n'):[]);const blkHtml=blockers.length?`<div class="proj-blk"><strong>⚠️ 卡点告警:</strong><span>${esc(blockers.join('; '))}</span></div>`:'';const msHtml=ms.length?`<div class="proj-sec"><div class="proj-lbl">动态路标</div>${ms.map(m=>`<div class="proj-ms"><span class="proj-ms-dot ${m.status}">${m.status==='completed'?'✓':(m.status==='in_progress'?'›':'·')}</span><span style="${m.status==='completed'?'color:var(--text)':(m.status==='in_progress'?'color:var(--warn);font-weight:600':'color:var(--muted)')}">${esc(m.label)}</span></div>`).join('')}</div>`:'';const artHtml=arts.length?`<div class="proj-sec"><div class="proj-lbl">核心产物</div>${arts.map(a=>`<div class="proj-art"><div class="proj-art-hd"><span>${esc(a.name||a.kind)}</span><span class="badge ${a.passed?'cleaned':(a.kind==='evaluation'?'failed':'waiting')}">${esc(a.kind)}</span></div><div class="muted">${esc(a.summary||'')}</div></div>`).join('')}</div>`:'';const actHtml=acts.length?`<div class="proj-sec"><div class="proj-lbl">近期动态提炼</div><ul class="proj-acts">${acts.map(a=>`<li>${esc(a)}</li>`).join('')}</ul></div>`:'';const body=`<div class="proj-box"><div style="display:flex;justify-content:space-between;align-items:center;padding-bottom:8px;border-bottom:1px solid var(--line)"><div><span class="badge ${st}">${esc(st)}</span><span style="margin-left:8px;font-size:12px;color:var(--muted)">执行者: <strong>${esc(d.agent||'-')}</strong></span><span style="margin-left:8px;font-size:12px;color:var(--muted)">工位: <strong>${esc(d.node||'-')}</strong></span></div><button class="mini" onclick="const el=document.getElementById('taskRawPre');if(el)el.style.display=el.style.display==='none'?'block':'none'">原始数据</button></div>${blkHtml}<div class="proj-sec"><div class="proj-lbl">当前语义意图</div><div class="proj-txt">${esc(intent)}</div></div>${msHtml}${artHtml}${actHtml}<div id="taskRawPre" style="display:none;margin-top:8px"><div class="proj-lbl">原始调试数据</div><pre>${esc(JSON.stringify(raw,null,2))}</pre></div></div>`;openModal('任务白盒简报 · '+id,body)}catch(e){toast(e.message,true)}}async function showPane(id){if(!id)return toast('没有工位',true);try{const d=await api('/api/pane/read?id='+encodeURIComponent(id));openModal('工位 '+id,`<pre>${esc(d.output)}</pre>`)}catch(e){toast(e.message,true)}}async function askCoordinator(id){try{toast('正在通知总指挥…');await api('/api/task/coordinator',{method:'POST',body:JSON.stringify({task_id:id})});toast('总指挥已处理/接收')}catch(e){toast(e.message,true)}}
async function openSignoffChamber(taskId){try{const [td,proj]=await Promise.all([api('/api/task?id='+encodeURIComponent(taskId)).catch(()=>null),api('/api/task/projection?id='+encodeURIComponent(taskId)).catch(()=>null)]);const d=proj||(td&&td.task)||{};const t=(td&&td.task)||{};window._signoffTask=t;const wid=t.workflow_id||state.workflowId;const node=t.node||t.stage||'';const arts=Array.isArray(d.artifacts)?d.artifacts:[];const isBlocked=t.stage_verdict==='blocked'||t.status==='blocked';let artCards='<div class="empty">暂无生成产物</div>';if(arts.length){artCards=arts.map(a=>`<div class="proj-sec" style="margin-bottom:8px"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px"><strong>${esc(a.name||a.kind)}</strong><span class="badge ${a.passed?'cleaned':(a.kind==='evaluation'?'failed':'waiting')}">${esc(a.kind)}</span></div><div class="task-meta" style="margin-bottom:8px">${esc(a.path||'')}</div><div class="proj-txt ctl-pre-light">${esc(a.content||a.summary||'（文件产物记录正常）')}</div></div>`).join('')}const html=`<div class="signoff-box"><div class="signoff-head"><div><div style="font-size:16px;font-weight:700">${esc(taskDisplayName(t))}</div><div class="task-meta">任务 ID: ${esc(taskId)} · 执行者: <b>${esc(t.agent||'-')}</b> · 节点: <b>${esc(node)}</b> · 版本: <b>v${esc(t.version!=null?t.version:'-')}</b></div></div><div>${badge(t.status)}</div></div>${isBlocked?'<div class="proj-blk"><strong>⚠️ 门禁会签等待:</strong> 当前节点触发门禁阻断，需要人类总指挥核查产物并决策放行或打回。</div>':''}<div class="proj-sec"><div class="proj-lbl">核心交付物与成果列表</div>${artCards}</div><div class="form"><label for="signoffFeedback">审批意见 / 批注说明（通过必填原因，打回可选）</label><input id="signoffFeedback" placeholder="例如：经核验核心成果符合要求，特批放行"></div><div class="signoff-actions"><button class="btn" onclick="closeModal()">暂不处理</button><button class="btn danger-btn" onclick="submitSignoffDecision(\'${esc(taskId)}\',\'${esc(wid)}\',\'${esc(node)}\',\'reject\')">批注打回</button><button class="btn primary" onclick="submitSignoffDecision(\'${esc(taskId)}\',\'${esc(wid)}\',\'${esc(node)}\',\'approve\')">通过并放行</button></div></div>`;openModal('成果交付会签室 (Artifact Signoff Chamber)',html)}catch(e){toast(e.message,true)}}
async function submitSignoffDecision(taskId,wid,node,act){const feedback=(document.getElementById('signoffFeedback')?.value||'').trim();if(act==='approve'&&!feedback){return toast('通过并放行必须填写审批说明原因',true)}closeModal();try{toast(act==='approve'?'正在通过并放行…':'正在批注打回…');const body={task_id:taskId,workflow_id:wid,node:node,action:act,feedback:feedback,reason:feedback,operator:'总指挥'};if(act==='approve'){body.confirmed=true;const curT=window._signoffTask||{};if(curT.task_id===taskId){if(curT.version!==undefined&&curT.version!==null)body.expected_version=curT.version;if(curT.pane_id)body.expected_pane_id=curT.pane_id}}const res=await api('/api/task/signoff',{method:'POST',body:JSON.stringify(body)});if(res.ok){await loadWorkflow(wid);toast(act==='approve'?'已通过并放行门禁！':'已完成批注打回，已回退至上游重新推进')}else{toast('操作失败: '+(res.error||'未知错误'),true)}}catch(e){toast(e.message,true)}}
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
function toggleDeepDrawer(){const d=document.getElementById('deepDrawer');if(!d)return;const isCollapsed=d.classList.contains('collapsed');d.classList.toggle('collapsed');const txt=document.getElementById('drawerToggleText');if(txt)txt.textContent=isCollapsed?'▼ 折叠收起':'▲ 展开抽屉';const btxt=document.getElementById('bottomDrawerText');if(btxt)btxt.textContent=isCollapsed?'▼ 收起现场':'▲ 底层现场';if(isCollapsed)refreshDeepDrawer()}

function switchDrawerTab(tab){state.drawerTab=tab;['tty','logs','raw'].forEach(t=>{const el=document.getElementById('dtab'+t.charAt(0).toUpperCase()+t.slice(1));if(el)el.classList.toggle('active',t===tab)});refreshDeepDrawer()}
async function refreshDeepDrawer(){const pre=document.getElementById('drawerPre');if(!pre)return;pre.textContent='正在拉取底层现场数据…';try{if(state.drawerTab==='tty'){const ts=(state.workflow&&state.workflow.tasks)||[];const activeTask=ts.find(t=>['working','dispatched','rework','paused'].includes(t.status))||ts[0];const paneId=activeTask?activeTask.pane_id:(state.project&&state.project.slots&&state.project.slots[0]&&state.project.slots[0].pane_id);if(!paneId){pre.textContent='当前暂无活动工位 TTY';return}const d=await api('/api/pane/read?id='+encodeURIComponent(paneId));pre.textContent=`[工位 ${paneId} 实时终端现场]\n`+(d.output||'（工位暂无输出）')}else if(state.drawerTab==='logs'){const d=await api('/api/logs?kind=controller');pre.textContent='[调度器内核日志 Controller Log]\n'+(d.output||'（暂无日志）')}else if(state.drawerTab==='raw'){if(!state.workflowId){pre.textContent='请先选择一个工作流';return}const d=await api('/api/workflow/projection?id='+encodeURIComponent(state.workflowId));pre.textContent=JSON.stringify(d,null,2)}}catch(e){pre.textContent='拉取失败: '+e.message}}
function showSteerModal(tid){openModal('留话指导',`<div class="form"><div class="muted" style="margin-bottom:8px">任务 ID：${esc(tid)}</div><label for="steerInput">想告诉它的话（排队等它空闲时读到，不打断当前执行）</label><textarea id="steerInput" rows="3" placeholder="例如：优先使用标准库，不要引入外部第三方包" style="width:100%;box-sizing:border-box;margin-bottom:8px"></textarea><div style="display:flex;align-items:center;gap:8px;margin-bottom:12px"><input type="checkbox" id="steerUrgent" style="width:auto"><label for="steerUrgent" style="margin:0;cursor:pointer"><strong>现在就打断它，马上送进去</strong></label></div><button class="btn primary" onclick="submitSteer('${esc(tid)}')">发送指令</button></div>`)}
async function submitSteer(tid){const inst=(document.getElementById('steerInput')?.value||'').trim();const urgent=!!document.getElementById('steerUrgent')?.checked;if(!inst)return toast('请输入指令内容',true);closeModal();try{toast('正在留言…');const res=await api('/api/task/steer',{method:'POST',body:JSON.stringify({task_id:tid,instruction:inst,urgent:urgent})});if(res.ok===false){toast(res.reason==='soft_steer_not_supported'?'当前工位不支持不打断的留言，指令已保留；可选择立即打断发送。':res.reason==='unknown_agent_no_adapter_registered'?'当前工位没有可用的指令适配能力，指令已保留，请检查工位配置。':'指令未送达，请查看任务现场后重试。',true);await loadWorkflow(state.workflowId);return}toast(res.status==='dispatched'?'已经打断它并送到手上了':'已放进留言队列，它空闲时会读到');await loadWorkflow(state.workflowId)}catch(e){toast(e.message,true)}}
function haltTaskPrompt(tid){showConfirmModal({title:'紧急叫停这个任务',message:'确定要立刻中断任务 '+tid+' 吗？正在做的事会停下，但代码和现场都会保留，之后你可以决定重新派人还是作废。',confirmText:'确认叫停',danger:true,onConfirm:async()=>{try{toast('正在叫停…');await api('/api/task/halt',{method:'POST',body:JSON.stringify({task_id:tid,reason:'人工在控制台紧急叫停'})});toast('工位已安全打断');await loadWorkflow(state.workflowId)}catch(e){toast(e.message,true)}}})}
async function toggleWorkflowPause(){if(!state.workflowId)return toast('当前没有工作流',true);const curSt=state.workflow&&state.workflow.workflow&&state.workflow.workflow.status;const isPaused=curSt==='paused';const act=isPaused?'resume':'pause';const label=isPaused?'恢复自动调度':'暂停自动调度';showConfirmModal({title:label,message:isPaused?'确认恢复该工作流的自动调度推进？':'确认暂停该工作流的自动推进？当前运行中的工位不会被强制终止。',confirmText:label,onConfirm:async()=>{try{await api('/api/kernel/'+act,{method:'POST',body:JSON.stringify({workflow_id:state.workflowId})});await loadWorkflow(state.workflowId);toast('工作流已'+(isPaused?'恢复':'暂停'))}catch(e){toast(e.message,true)}}})}
async function stepWorkflow(){if(!state.workflowId)return toast('当前没有工作流',true);try{toast('正在单步推进…');const res=await api('/api/kernel/step',{method:'POST',body:JSON.stringify({workflow_id:state.workflowId})});if(res.ok){await loadWorkflow(state.workflowId);toast('单步已推进: '+res.stepped_label+' ('+res.stepped_node+')')}else{toast('无法单步推进: '+(res.reason==='no_ready_nodes'?'当前无就绪节点':res.reason),true)}}catch(e){toast(e.message,true)}}
function showRollbackModal(){if(!state.workflowId)return toast('当前没有工作流',true);const stages=(state.workflow&&state.workflow.stages)||[];if(!stages.length)return toast('工作流暂无节点',true);const options=stages.map(s=>`<option value="${esc(s.key)}">${esc(cleanStageLabel(s.label))} (${esc(s.key)})</option>`).join('');openModal('节点回溯 (Rollback)',`<div class="form"><label for="rbTarget">回溯目标节点（该节点及所有下游任务将被重置作废）</label><select id="rbTarget">${options}</select><label for="rbReason">回溯原因</label><input id="rbReason" type="text" value="人工核验需求变更或发现重大缺陷"><button class="btn primary" style="background:#dc2626;border-color:#ef4444" onclick="executeRollback()">确认回溯</button><div class="muted">警告：此操作不可撤销，下游所有产物与任务将被标记为作废。</div></div>`)}
async function executeRollback(){const target=document.getElementById('rbTarget').value;const reason=document.getElementById('rbReason').value;closeModal();try{toast('正在执行回溯…');const res=await api('/api/kernel/rollback',{method:'POST',body:JSON.stringify({workflow_id:state.workflowId,target_node_id:target,reason:reason})});await loadWorkflow(state.workflowId);toast('已成功回溯至 '+target+'，作废 '+res.invalidated_tasks.length+' 个任务')}catch(e){toast(e.message,true)}}
function forcePassTask(wid,nodeId,options){
  options=options||{};
  const widEsc=esc(wid||'');
  const nodeEsc=esc(nodeId||'');
  const tid=options.task_id||'';
  const tidEsc=esc(tid);
  let expVer=(options.expected_version!==undefined&&options.expected_version!==null)?options.expected_version:(options.version!==undefined&&options.version!==null?options.version:'');
  let expPane=options.expected_pane_id||options.pane_id||'';
  if(tid&&(expVer===''||!expPane)){
    const allTasks=(state.workflow&&state.workflow.tasks)||[];
    const matchedTask=allTasks.find(t=>String(t.task_id)===String(tid));
    if(matchedTask){
      if(expVer===''&&matchedTask.version!==undefined){expVer=matchedTask.version;}
      if(!expPane&&matchedTask.pane_id){expPane=matchedTask.pane_id;}
    }
  }
  openModal('人工强制放行门禁确认 (高风险)',`
    <div style="line-height:1.6">
      <div style="margin-bottom:8px"><strong>目标工作流:</strong> <code>${widEsc}</code></div>
      <div style="margin-bottom:8px"><strong>目标门禁节点:</strong> <span class="badge warning">${nodeEsc||'未指定'}</span></div>
      ${tid?`<div style="margin-bottom:8px"><strong>关联任务:</strong> <code>${tidEsc}</code></div>`:''}
      <div class="ctl-effect" style="margin:12px 0;padding:12px;background:var(--warning-bg);border:1px solid var(--warning);border-radius:8px;color:var(--text-primary)">
        <strong>⚠️ 影响范围警告:</strong><br>
        本操作为<b>人工强制豁免</b>，将直接把该节点的门禁判定标记为 <code>pass</code>，并在工作流持久化记录豁免审计凭证。<br>
        <b>此入口仅执行门禁放行，不自动推进工作流。绝不能替代自动修复，必须由工程师人工核验产物后方可放行。</b>
      </div>
      <div style="margin-top:12px">
        <label for="kernelForcePassReason" style="font-weight:600;font-size:12px;color:var(--text-primary);display:block;margin-bottom:4px">放行原因与核验结论（必填）:</label>
        <textarea id="kernelForcePassReason" class="input" style="width:100%;height:72px;box-sizing:border-box" placeholder="请详细填写人工豁免原因、核验人及结论..."></textarea>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">
        <button class="btn" onclick="closeModal()">取消</button>
        <button id="btnSubmitKernelForcePass" class="btn warning">确认强制放行</button>
      </div>
    </div>
  `);
  const btn=document.getElementById('btnSubmitKernelForcePass');
  if(btn){
    btn.onclick=async()=>{
      const el=document.getElementById('kernelForcePassReason');
      const reason=(el?el.value:'').trim();
      if(!reason){
        toast('必须填写强制放行原因',true);
        return;
      }
      btn.disabled=true;
      closeModal();
      try{
        toast('正在强制放行…');
        const body={
          workflow_id:wid,
          gate_node_id:nodeId,
          node:nodeId,
          note:reason,
          reason:reason,
          confirmed:true,
          operator:'human'
        };
        if(tid){
          body.task_id=tid;
          if(expVer!==''&&expVer!==undefined&&expVer!==null){
            body.expected_version=parseInt(expVer,10);
          }
          if(expPane){
            body.expected_pane_id=expPane;
            body.pane_id=expPane;
          }
        }else{
          const allTasks=(state.workflow&&state.workflow.tasks)||[];
          const nodeTasks=allTasks.filter(t=>(t.node===nodeId||t.stage===nodeId)&&t.status!=='superseded');
          const taskMap={};
          for(const t of nodeTasks){
            if(t&&t.task_id){
              taskMap[t.task_id]=t.version!==undefined&&t.version!==null?t.version:1;
            }
          }
          body.expected_task_versions=taskMap;
        }
        await api('/api/kernel/force-pass',{
          method:'POST',
          body:JSON.stringify(body)
        });
        await loadWorkflow(wid);
        toast('门禁已强制放行');
      }catch(e){
        toast('强制放行失败: '+e.message,true);
      }
    };
  }
}
async function showCheckpointsModal(){if(!state.workflowId)return toast('当前没有工作流',true);try{const cps=await api('/api/kernel/checkpoints?workflow_id='+encodeURIComponent(state.workflowId));let listHtml='<div class="muted" style="margin-bottom:12px">暂无历史快照</div>';if(cps&&cps.length){listHtml=cps.map(c=>`<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--line)"><div><strong>${esc(c.tag||'无标签')}</strong><div class="task-meta">${esc(c.checkpoint_id)} · ${c.task_count} 任务 · ${new Date(c.created_at*1000).toLocaleString()}</div></div><button class="btn" style="padding:3px 8px;font-size:12px;color:var(--danger)" onclick="restoreWorkflowCheckpoint('${esc(c.checkpoint_id)}')">恢复此快照</button></div>`).join('')}openModal('工作流快照中心',`<div class="form"><div style="display:flex;gap:8px;margin-bottom:16px"><input id="cpTagInput" type="text" placeholder="快照标签（如：修改前基准）" style="flex:1"><button class="btn primary" onclick="createWorkflowCheckpoint()">创建快照</button></div><h4>历史快照</h4><div style="max-height:240px;overflow-y:auto">${listHtml}</div></div>`)}catch(e){toast(e.message,true)}}
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
function controllerActionCard(act,tid,index){
  const m=catMeta(act.category);
  const fx=act.effect||act.description||'';
  const target=tid||act.blocker_task_id||act.old_task_id||'';
  return `<article class="ctl-card${act.recommended?' is-recommended':''}${act.is_destructive?' is-destructive':''}"><div class="ctl-card-head"><span class="ctl-index">操作 ${index}</span><span class="ctl-card-title">${act.recommended?'⭐ ':''}${esc(act.title)}</span><span class="ctl-badges">${act.recommended?'<span class="ctl-rec">推荐</span>':''}<span class="badge ${m[1]}">${esc(m[0])}</span></span></div><p class="ctl-card-desc">${esc(act.description)}</p>${fx?`<div class="ctl-effect">点按钮后：${esc(fx)}</div>`:''}${act.new_task_id||act.new_agent?`<div class="ctl-meta-chips">${act.old_task_id?`<span class="ctl-chip">旧任务 <strong>${esc(act.old_task_id)}</strong></span>`:''}${act.new_task_id?`<span class="ctl-chip">新任务 <strong>${esc(act.new_task_id)}</strong></span>`:''}${act.new_agent?`<span class="ctl-chip">执行者 <strong>${esc(act.new_agent)}</strong></span>`:''}</div>`:''}<div class="ctl-card-foot">${target?`<button class="btn" onclick="locateControllerTask('${esc(target)}')">查看任务</button>`:''}<button class="btn primary" onclick="executeControllerAction('${esc(act.action_id)}','${esc(state.workflowId||'')}')">一键执行</button></div>${act.command_line?`<details class="ctl-tech"><summary>工程师技术详情（可选）</summary><div class="ctl-tech-body"><div class="ctl-cheat-cmd"><code>${esc(act.command_line)}</code></div><div style="display:flex;justify-content:flex-end;margin-top:8px"><button class="mini" onclick="copyCliCommandByActionId('${esc(act.action_id)}')">复制命令</button></div></div></details>`:''}</article>`;
}
// Category → [label, badge class].  Module scope: the action-card renderer is
// shared by both the blocker and pipeline sections.
const CTL_CAT_META={fix:['修复','working'],rework:['换人重派','rework'],bypass:['豁免推进','cleaned'],recovery:['恢复','waiting'],pipeline:['推进交付','working']};
function catMeta(c){return CTL_CAT_META[c]||[c||'操作','waiting'];}
// Pipeline actions must render even when there are zero blockers: a healthy task
// that still owes `integrate` is exactly the case that used to render nothing.
function renderPipelineActions(acts){
  const pipelineActs=acts.filter(a=>a.group==='pipeline');
  if(!pipelineActs.length)return '';
  const resumeAct=pipelineActs.find(a=>a.action_id.endsWith(':resume_workflow'));
  const taskActs=pipelineActs.filter(a=>a!==resumeAct);
  const byTask={};
  taskActs.forEach(a=>{const k=a.blocker_task_id||String(a.action_id).split(':')[0];(byTask[k]=byTask[k]||[]).push(a)});
  const groups=Object.keys(byTask).map(k=>{
    const items=byTask[k].slice().sort((a,b)=>(b.recommended?1:0)-(a.recommended?1:0));
    return `<div class="ctl-blocker" style="margin-bottom:8px"><div class="ctl-blocker-head"><div style="min-width:0"><div class="ctl-blocker-title">${esc(taskDisplayName({task_id:k})||k)}</div><div class="ctl-blocker-id">${esc(k)}</div></div><div style="display:flex;gap:8px;flex-shrink:0"><button class="mini" onclick="locateControllerTask('${esc(k)}')">查看任务详情</button></div></div></div><div class="ctl-actions">${items.map((a,i)=>controllerActionCard(a,k,i+1)).join('')}</div>`;
  }).join('');
  const resumeHtml=resumeAct?`<div class="ctl-actions" style="margin-bottom:12px">${controllerActionCard(resumeAct,'',1)}</div>`:'';
  return `<section><div class="ctl-section-title">继续推进 <span class="ctl-count">${pipelineActs.length}</span></div><div class="ctl-sub">这些任务本身没有报错，只是还差一步就能收尾（保存版本 / 并入目标分支 / 归档），或者工位需要人推一把。点按钮即可。</div>${resumeHtml}${groups}</section>`;
}
// Open decision asks must be impossible to miss: they block whole waves and
// previously only existed as free text inside the coordinator's terminal.
function renderDecisionPanel(){
  const data=state.decisionData||{};
  const decisions=data.decisions||[];
  const advice=data.advice||[];
  let html='';
  if(decisions.length){
    html+=`<div class="ctl-section-title">待你裁决 <span class="ctl-count">${decisions.length}</span></div><div class="ctl-sub">总指挥把这些问题交给你决定了。在你拍板前，相关阶段不会开工。点选项即记录，总指挥下一轮就能读到你的结论。</div>`;
    html+=decisions.map(d=>{
      const opts=(d.options||[]).map(o=>`<span class="ctl-dec-opt${o===d.recommended?' is-rec':''}">${esc(o)}${o===d.recommended?' · 建议':''}</span>`).join('');
      return `<div class="ctl-decision"><div class="ctl-dec-head"><span class="ctl-dec-id">${esc(d.decision_id)}</span><span class="ctl-chip">${esc(d.node||'—')}</span><span class="ctl-chip">${esc(d.raised_at_text||'')}</span></div><div class="ctl-dec-q">${esc(d.question||d.title||d.decision_id)}</div>${opts?`<div class="ctl-dec-opts">${opts}</div>`:''}<div class="ctl-card-foot"><button class="btn primary" onclick="openDecisionPanel('${esc(d.decision_id)}')">已拍板，记录结论</button></div></div>`;
    }).join('');
  }
  if(advice.length){
    html+=`<div class="ctl-section-title" style="margin-top:16px">总指挥最新建议 <span class="ctl-count">${advice.length}</span></div><div class="ctl-sub">来自工作流的共享记录（只追加，不会被改写），不是模型口头说的话。</div>`;
    html+=advice.map(a=>`<div class="ctl-dec-advice${a.stale?' is-stale':''}"><div class="ctl-dec-advice-h"><span class="ctl-dec-advice-kind">${esc(a.kind)}</span><span class="ctl-dec-advice-t">${esc(a.title||'—')}</span>${a.stale?'<span class="ctl-chip">已作废</span>':''}</div>${a.summary?`<div class="ctl-dec-advice-s">${esc(a.summary)}</div>`:''}</div>`).join('');
  }
  if(!html)return `<div class="ctl-empty">✓ 当前没有待你裁决的问题。<div class="muted">总指挥交给你的问题会出现在这里；定了之后会自动消失。</div></div>`;
  return `<section>${html}</section>`;
}
function openDecisionPanel(decisionId){
  const decisions=((state.decisionData||{}).decisions)||[];
  const d=decisions.find(x=>x.decision_id===decisionId);
  if(!d)return toast('该裁决项已不在待办列表',true);
  // Single quotes only: the handler lives inside a double-quoted HTML
  // attribute, so JSON.stringify's double quotes would truncate it.
  // esc() escapes ' as &#39;, so the browser decodes back to the raw value.
  const opts=(d.options||[]).map(o=>`<div class="ctl-dec-opt${o===d.recommended?' is-rec':''}" style="cursor:pointer;padding:8px 12px" onclick="document.getElementById('decChosen').value=String.fromCharCode(${Array.from(o).map(ch=>ch.charCodeAt(0)).join(',')})">${esc(o)}${o===d.recommended?' · 建议':''}</div>`).join('');
  openModal('记录裁决 · '+d.decision_id,`<div class="ctl-wrap"><div class="ctl-dec-q">${esc(d.question||d.title)}</div>${opts?`<div class="ctl-dec-opts" style="flex-direction:column;align-items:stretch">${opts}</div>`:''}<div class="form"><label for="decChosen">你的结论（可直接选上面的选项，也可自己写）</label><input id="decChosen" value="${esc(d.recommended||'')}" placeholder="例如：MATCH 入 V1，但 T3 段需先过 G1c 的 DB/EXPLAIN 门禁"><label for="decNote">补充说明（可选）</label><input id="decNote" placeholder="给总指挥的执行约束"></div><div style="display:flex;justify-content:flex-end;gap:8px;margin-top:12px"><button class="btn" onclick="closeModal()">取消</button><button class="btn primary" onclick="submitDecision('${esc(d.decision_id)}')">落库并通知总指挥</button></div></div>`);
}
async function submitDecision(decisionId){
  const decision=(document.getElementById('decChosen')?.value||'').trim();
  const note=(document.getElementById('decNote')?.value||'').trim();
  if(!decision)return toast('请填写你的结论',true);
  closeModal();
  try{
    toast('正在记录裁决…');
    await api('/api/workflow/decision',{method:'POST',body:JSON.stringify({workflow_id:state.workflowId,decision_id:decisionId,decision:decision+(note?('（'+note+'）'):''),operator:'human_console'})});
    toast('裁决已落库，总指挥下一轮即可读到');
    await loadWorkflow(state.workflowId);
  }catch(e){toast('记录失败: '+e.message,true)}
}
function openControllerCockpitModal(){
  const w=state.workflow&&state.workflow.workflow;
  const wid=(w&&w.workflow_id)||state.workflowId||'未选择工作流';
  const stall=state.workflow&&state.workflow.stall;
  const allActs=(state.controllerActionsData&&state.controllerActionsData.actions)||[];
  const blockers=(state.controllerActionsData&&state.controllerActionsData.blockers)||[];
  const recoveryCount=currentRecovery().length;
  const acts=allActs.filter(a=>a.group!=='pipeline');
  if(!state.controllerActionsMap)state.controllerActionsMap={};
  allActs.forEach(a=>{state.controllerActionsMap[a.action_id]=a;});
  const isWfDone=['completed','cleaned','archived'].includes(w&&w.status);
  const curStage=isWfDone?null:(state.workflow&&state.workflow.stages?state.workflow.stages.find(s=>['working','failed','blocked'].includes(s.status)):null);
  const stageName=isWfDone?'已全部完成':(curStage?(curStage.label||curStage.key):'就绪/空闲');
  const isStalled=Boolean(stall&&stall.is_stalled);

  let statusCard=`<section class="ctl-status${isStalled?' is-stalled':''}"><div class="ctl-status-head"><span class="ctl-status-title"><span class="ctl-dot"></span>调度状态与等待条件</span><span class="badge ${isStalled?'failed':'cleaned'}">${isStalled?'推进停滞':'调度运转中'}</span></div><div class="ctl-status-grid"><div class="ctl-status-item"><span class="ctl-k">当前关注阶段</span><strong>${esc(stageName)}</strong></div><div class="ctl-status-item"><span class="ctl-k">活跃卡点</span><strong>${recoveryUnavailable()?'未确认':blockers.length+recoveryCount+' 项'}</strong></div><div class="ctl-status-item is-full"><span class="ctl-k">${isStalled?'停滞原因':'轮询状态'}</span><span class="ctl-note">${isStalled?`⚠️ ${esc(stall.message)}`:'✓ Controller 后台轮询正常，正在监控 DAG 拓扑门禁'}</span></div></div></section>`;

  let unblockSection='';
  if(blockers.length||acts.length){
    const actionKey=a=>a.blocker_task_id||a.old_task_id||String(a.action_id||'').split(':')[0]||'';
    const groups=blockers.length?blockers.map(b=>({blocker:b,items:acts.filter(a=>actionKey(a)===b.task_id)})):[{blocker:null,items:acts}];
    if(!blockers.length&&acts.length)groups[0].items=acts;
    const sections=groups.map((g,gi)=>{
      const b=g.blocker||{};
      const tid=b.task_id||(g.items[0]?actionKey(g.items[0]):'')||'';
      const reason=b.stage_verdict_note||b.blocker||b.blocked_reason||'';
      const st=b.status||'';
      const ag=b.agent||'';
      const stageLabel=b.stage_label||b.stage||b.node||'';
      const head=tid?`<div class="ctl-blocker" style="margin-bottom:8px"><div class="ctl-blocker-head"><div style="min-width:0"><div class="ctl-blocker-title">卡点任务 ${gi+1} · ${esc(taskDisplayName(b)||stageLabel||'任务')}</div><div class="ctl-blocker-id">${esc(tid)}${ag?` · 执行者 ${esc(ag)}`:''}${st?` · ${esc(humanStatus(st))}`:''}${stageLabel?` · ${esc(stageLabel)}`:''}</div></div><div style="display:flex;gap:8px;flex-shrink:0"><button class="mini" onclick="locateControllerTask('${esc(tid)}')">查看任务详情</button></div></div>${reason?`<div class="ctl-blocker-reason">卡点原因：${esc(reason)}</div>`:''}</div>`:`<div class="ctl-sub">以下为当前可执行的一键解卡操作（已按推荐度排序）。</div>`;
      const cards=(g.items.length?g.items:[]).map((act,i)=>controllerActionCard(act,tid,i+1)).join('')||'<div class="muted" style="font-size:12px">该卡点暂无自动解卡按钮，请查看任务详情人工处理。</div>';
      return `<section style="margin-bottom:16px">${head}<div class="ctl-actions">${cards}</div></section>`;
    }).join('');
    unblockSection=`<section><div class="ctl-section-title">按卡点任务一键解卡 <span class="ctl-count">${blockers.length||acts.length}</span></div><div class="ctl-sub">每个卡点任务独立分组：先看“卡点原因”，再点对应按钮即可执行，无需手动敲命令。</div>${sections}</section>`;
  } else if(!recoveryCount&&!recoveryUnavailable()) {
    unblockSection=`<div class="ctl-empty">✓ 当前没有报错卡点。<div class="muted">若只是还没收尾（保存版本 / 并入目标分支 / 归档），见下方“继续推进”一节。</div></div>`;
  }
  const resources=(state.workflow?.graph?.nodes||[]).filter(n=>n.resource_usage);
  const resourceSection=resources.length?'<section><div class="ctl-section-title">节点任务与工位占用</div><div class="ctl-sub">累计包含已归档任务；工位计数为持久引用数，待回收引用单列。</div>'+resources.map(n=>{
    const u=n.resource_usage;
    return '<div class="ctl-status-item"'+(u.overflow?' style="color:var(--danger);font-weight:600"':'')+'><strong>'+esc(n.label||n.id)+'</strong><span>'+esc(flowCardFoot(n))+' · 待回收 '+u.orphan_pane_count+'</span></div>';
  }).join('')+'</section>':'';
  const pipelineSection=renderPipelineActions(allActs);
  const decisionSection=renderDecisionPanel();
  const recoverySection=renderRecoveryPanel(wid);

  const cheatRows=[
    {name:'原工位返工',cmd:`bin/herdr-task rework <task-id> --prompt '<修复指引>'`,desc:'保留 task_id 与工位，就地修复与自测'},
    {name:'换执行者重派（例外）',cmd:`bin/herdr-task launch --task-id <new-task-id> --workflow-id ${wid} --stage <stage> --source . --agent <agent> --goal <goal> --prompt <prompt> --supersedes <old-task-id> --supersede-reason '<替换理由>'`,desc:'作废指定卡点旧任务，换执行者重派新任务'},
    {name:'强制推进阶段',cmd:`bin/herdr-task advance ${wid}`,desc:'检查并强制推进工作流至下一阶段'},
    {name:'解除升级锁定',cmd:'bin/herdr-task clear-escalation <task-id>',desc:'撤销机器终化升级锁，解除阻断重新流转'},
    {name:'立刻推工位一把',cmd:`herdr agent prompt <pane-id> "提示内容" --wait --timeout 180000`,desc:'马上把指令送到工位并等回执（立即生效）'},
    {name:'留话指导',cmd:'bin/herdr-task steer <task-id> "提示内容"',desc:'把指导放进留言队列，下一轮才生效（不打断当前执行）'},
  ];
  let cheatSheet=`<details class="ctl-tech"><summary>工程师命令参考（可选展开，终端用户无需使用）</summary><div class="ctl-tech-body"><section class="ctl-cheatsheet" style="box-shadow:none"><div class="ctl-sub">以下命令仅供审计与排查；日常使用请点上面的“一键执行”按钮。</div><div class="ctl-cheat-list">${cheatRows.map((r,i)=>`<div class="ctl-cheat-row"><span class="ctl-cheat-num">${i+1}</span><div><div class="ctl-cheat-name"><span>${esc(r.name)}</span><button class="mini" onclick="copyCliCommand(document.getElementById('ctlCheat${i}').textContent)">复制</button></div><div class="ctl-cheat-cmd"><code id="ctlCheat${i}">${esc(r.cmd)}</code></div><div class="ctl-cheat-desc">${esc(r.desc)}</div></div></div>`).join('')}</div></section></div></details>`;

  const html=`<div class="ctl-wrap">${statusCard}${resourceSection}${recoverySection}${decisionSection}${unblockSection}${pipelineSection}${cheatSheet}</div>`;
  const _ctlEl=document.getElementById('controllerTabView');
  if(_ctlEl){_ctlEl.innerHTML=`<div class="ctl-panel-header"><strong>Controller 调度与解卡控制台</strong><span class="muted" style="margin-left:8px;font-weight:400">· ${esc(wid)}</span></div>`+html;_ctlEl.hidden=false;}
  setWorkspaceMode('ctl');
  if(!state.openWorkflowTabIds)state.openWorkflowTabIds=[];
  if(!state.openWorkflowTabIds.includes('__ctl__'))state.openWorkflowTabIds.push('__ctl__');
  state.workflowId='__ctl__';
  renderWorkflowTabs();
}
function currentRecovery(nodeId){
  return (state.controllerActionsData?.recovery||[]).filter(o=>!['resolved','superseded'].includes(o.status)&&(!nodeId||o.payload?.node_id===nodeId));
}
function dispatchConfirmations(nodeId){return (state.controllerActionsData?.recovery||[]).filter(o=>o.confirmation&&(!nodeId||o.payload?.node_id===nodeId));}
function dispatchConfirmationHtml(nodeId){return dispatchConfirmations(nodeId).map(o=>'<div class="flow-attn" role="status">派发已确认：'+esc(o.confirmation.task_ids.join('、'))+' · 查看任务列表可跟踪实际执行</div>').join('');}
function recoveryUnavailable(){return Boolean(state.workflow&&!state.controllerActionsData);}
function recoveryCard(o,wid){
  const r=o.recovery;
  const labels={pending:'等待派发',running:'正在处理',waiting:'已暂缓',waiting_human:'等待人工确认',awaiting_result:'等待总指挥建立任务'};
  const reasons={dispatch_task_missing:'未确认目标任务登记，请核对已有派发并补足需求',dispatch_delivery_unknown:'派发结果未知，先核验已有任务',dispatch_awaiting_task:'通知已返回，正在核验实际任务登记',legacy_dispatch_unknown:'历史通知无登记证据，正在核验',coordinator_missing:'总指挥工位不可用，等待前置问题处理',dispatch_generation_changed:'工作流代次或配置已变化，旧派发需核对',coordinator_busy:'总指挥忙碌，等待下一次检查',source_wip_requires_decision:'源仓库存在未处理改动，请先确认归属和处理方式',delivery_unknown:'交付结果未知，请核对现有后继',successor_delivery_unconfirmed:'后继交付尚未确认',candidate_unknown:'候选身份尚未确认',candidate_mismatch:'验收候选与当前候选不一致',recovery_successor_failed:'修复任务仍未通过，需要人工处理',finalize_escalated:'交付需要人工排除前置障碍',workflow_execution_unknown:'工作流执行身份尚未确认'};
  const code=o.detail?.reason||o.payload?.reason;
  const reason=r?.summary||o.detail?.decision_needed||reasons[code]||code||'等待恢复处理';
  const controls=r?(r.actions||[]).map(a=>`<button class="mini" data-recovery-action="${esc(a.action)}" onclick="openRecoveryDecision(${o.id},'${esc(a.action)}')">${esc(a.label)}</button>`).join(''):(o.status==='waiting_human'?`<button class="mini" onclick="decideRecovery(${o.id},${o.version},'${o.started?'verify':'retry'}','${esc(wid)}')">${o.started?'核对现有任务':'前置问题已处理，重试'}</button><button class="mini" onclick="decideRecovery(${o.id},${o.version},'hold','${esc(wid)}')">暂缓一小时</button>`:'');
  return `<div class="ctl-blocker"><strong>${esc(cleanStageLabel(r?.node_label||o.payload?.node_id||'恢复待办'))} · ${esc(labels[o.status]||o.status)}</strong><div>${esc(reason)}</div>${r?`<p class="muted">${esc(r.explanation)}</p><p>${esc(r.next_step)}</p><div>${(r.resource_checks||[]).map(c=>`<p>${esc(c.task_id||'旧启动')}：${esc(c.message)}</p>`).join('')}</div><div class="muted">验收版本：${esc((r.candidate_sha||'未确认').slice(0,12))}</div>`:''}<div class="muted">负责人：总指挥 · 恢复：Controller · ${o.status!=='waiting_human'&&o.next_due_at?'下次检查：'+new Date(o.next_due_at*1000).toLocaleString():'等待人工决定'}${o.detail?.deadline_at?' · 截止：'+new Date(o.detail.deadline_at*1000).toLocaleString():''}</div><div>${controls}</div></div>`;
}
function renderRecoveryPanel(wid){
  if(recoveryUnavailable())return '<section role="alert">无法读取恢复状态，尚不能确认是否有卡点。<button class="mini" onclick="loadWorkflow(state.workflow.workflow.workflow_id)">刷新恢复状态</button></section>';
  const operations=currentRecovery();
  return (operations.length?'<section><div class="ctl-section-title">恢复待办</div>'+operations.map(o=>recoveryCard(o,wid)).join('')+'</section>':'')+dispatchConfirmationHtml();
}
function openRecoveryDecision(id,action){
  const o=currentRecovery().find(o=>o.id===id);
  const choice=o?.recovery?.actions?.find(a=>a.action===action);
  if(!choice){toast('恢复条件已变化，请刷新页面',true);return;}
  const wid=o.workflow_id;
  const resend=['restore_scope','confirm_absent'].includes(action);
  const bindLineage=action==='restore_scope'&&o.recovery.lineage?.some(t=>t.requires_binding);
  openModal(choice.label,`<form id="recoveryForm" class="form"><p>${esc(o.recovery.summary)}</p><p>${esc(o.recovery.next_step)}</p><p class="muted">节点：${esc(cleanStageLabel(o.recovery.node_label||o.payload.node_id))} · 验收版本：${esc(o.recovery.candidate_sha||'未确认')}</p><label for="recoveryOperator">处理人</label><input id="recoveryOperator" required maxlength="128"><label for="recoveryReason">核查结果与处理依据</label><textarea id="recoveryReason" required maxlength="1000" rows="3"></textarea>${resend?'<label><input id="recoveryConfirmed" type="checkbox" required style="width:auto">我已查看任务列表及旧工位，确认没有旧任务执行，授权对当前版本重新派发。</label><p class="muted">这是人工核查回执，不代表业务验收通过。系统仍会复查启动记录、候选和依赖。</p>':''}${bindLineage?`<p>旧任务：${esc(o.recovery.lineage.map(t=>t.task_id+' / '+(t.run_id||'身份未确认')).join('；'))}</p><label><input id="recoveryLineage" type="checkbox" required style="width:auto">确认以上Task/Run属于本次工作流执行，授权补齐其执行归属。</label>`:''}<p id="recoveryError" role="alert"></p><button id="recoverySubmit" type="submit" class="btn primary">${esc(choice.label)}</button><button type="button" class="btn" onclick="closeModal()">取消</button></form>`);
  const form=document.getElementById('recoveryForm');
  const ownsForm=()=>form.isConnected&&document.getElementById('recoveryForm')===form&&document.getElementById('modal')?.classList.contains('open')&&(state.workflowId===wid||(state.workflowId==='__ctl__'&&state.workflow?.workflow?.workflow_id===wid));
  form.onsubmit=async(event)=>{
    event.preventDefault();const button=document.getElementById('recoverySubmit');button.disabled=true;
    const body={workflow_id:wid,operation_id:o.id,expected_version:o.version,operator:document.getElementById('recoveryOperator').value.trim(),reason:document.getElementById('recoveryReason').value.trim(),action,candidate_sha:o.recovery.candidate_sha,confirmed_absent:document.getElementById('recoveryConfirmed')?.checked===true,confirmed_lineage:document.getElementById('recoveryLineage')?.checked===true,lineage_snapshot:(o.recovery.lineage||[]).map(t=>({task_id:t.task_id,run_id:t.run_id,version:t.version}))};
    if(action==='hold')body.until=Date.now()/1000+3600;
    try{
      const result=await api('/api/workflow/recovery/decision',{method:'POST',body:JSON.stringify(body)});
      if(!ownsForm())return;
      closeModal();await loadWorkflow(body.workflow_id);
      if(state.workflowId!==wid&&!(state.workflowId==='__ctl__'&&state.workflow?.workflow?.workflow_id===wid))return;
      openControllerCockpitModal();
      if(action==='check_resources')toast(result.checks.every(c=>c.status==='resources_absent')?'已确认启动资源不存在，可继续恢复':'现场尚未确认资源不存在，仍禁止重发，请查看恢复待办');
      else toast(resend?'已建立恢复待办，Controller将派发；尚未确认任务登记':'已记录，等待核对和执行');
    }catch(error){if(ownsForm()){document.getElementById('recoveryError').textContent=error.message;button.disabled=false;}}
  };
}
function decideRecovery(id,version,action,wid){
  showPromptModal({title:'记录恢复决策',label:'本次处理人的名称',onConfirm:(operator)=>{
    if(!operator?.trim())return;
    showPromptModal({title:'记录处理依据',label:'重试前请先处理提示的前置问题',onConfirm:async(reason)=>{
      if(!reason?.trim())return;
      const body={workflow_id:wid,operation_id:id,expected_version:version,operator,reason,action};
      if(action==='hold')body.until=Date.now()/1000+3600;
      try{await api('/api/workflow/recovery/decision',{method:'POST',body:JSON.stringify(body)});toast('已记录，等待核对和执行');await loadWorkflow(wid)}catch(e){toast('处理失败: '+e.message,true)}
    }});
  }});
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
let logState = {
  kind: 'controller',
  lines: 200,
  autoRefreshTimer: null
};

function renderLogsTabSkeleton(){
  return `<div class="ctl-panel-header">`+
    `<div style="display:flex;justify-content:space-between;align-items:center;width:100%;flex-wrap:wrap;gap:8px">`+
      `<div><strong>调度审计日志</strong><span class="muted" style="margin-left:8px;font-weight:400">· 协调器事件溯源、主循环轮询流与各工位物理日志</span></div>`+
      `<div style="display:flex;gap:8px;align-items:center">`+
        `<button class="btn" onclick="refreshLogsTab()" title="刷新日志">🔄 刷新</button>`+
        `<button class="btn" onclick="copyCurrentLog()" title="复制日志到剪贴板">📋 复制</button>`+
        `<button class="btn" onclick="exportLogsFile()" title="导出日志文件">⬇️ 导出</button>`+
      `</div>`+
    `</div>`+
  `</div>`+
  `<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:8px">`+
    `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">`+
      `<div class="log-kind-selector">`+
        `<button class="btn mini log-kind-btn ${logState.kind==='controller'?'active':''}" id="logKindController" onclick="switchLogKind('controller')">协调器 (Controller)</button>`+
        `<button class="btn mini log-kind-btn ${logState.kind==='controller_err'?'active':''}" id="logKindControllerErr" onclick="switchLogKind('controller_err')">错误流 (Error)</button>`+
        `<button class="btn mini log-kind-btn ${logState.kind==='sentinel'?'active':''}" id="logKindSentinel" onclick="switchLogKind('sentinel')">巡检守卫 (Sentinel)</button>`+
        `<button class="btn mini log-kind-btn ${logState.kind==='notifier'?'active':''}" id="logKindNotifier" onclick="switchLogKind('notifier')">通知服务 (Notifier)</button>`+
      `</div>`+
      `<select id="logLineCount" onchange="switchLogLineCount(this.value)" style="padding:4px 8px;font-size:12px;border:1px solid #e6e8ee;border-radius:6px;background:#ffffff">`+
        `<option value="100"${logState.lines===100?' selected':''}>最近 100 行</option>`+
        `<option value="200"${logState.lines===200?' selected':''}>最近 200 行</option>`+
        `<option value="500"${logState.lines===500?' selected':''}>最近 500 行</option>`+
        `<option value="1000"${logState.lines===1000?' selected':''}>最近 1000 行</option>`+
      `</select>`+
    `</div>`+
    `<div style="display:flex;gap:16px;align-items:center">`+
      `<label style="display:inline-flex;align-items:center;gap:4px;font-size:12px;color:#5f6368;cursor:pointer;user-select:none">`+
        `<input type="checkbox" id="logAutoScroll" checked style="margin:0">`+
        `<span>自动滚屏</span>`+
      `</label>`+
      `<label style="display:inline-flex;align-items:center;gap:4px;font-size:12px;color:#5f6368;cursor:pointer;user-select:none">`+
        `<input type="checkbox" id="logAutoRefresh" onchange="toggleLogAutoRefresh(this.checked)" style="margin:0">`+
        `<span>自动刷新 (3s)</span>`+
      `</label>`+
    `</div>`+
  `</div>`+
  `<div class="cockpit-stat-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:8px">`+
    `<div style="background:#ffffff;border:1px solid #e6e8ee;border-radius:8px;padding:12px 16px;box-shadow:0 1px 2px rgba(18,19,22,0.04)">`+
      `<div style="font-size:11px;color:#5f6368;font-weight:500">当前日志流</div>`+
      `<div id="logStatKind" style="font-size:14px;font-weight:600;color:#121316;margin-top:4px">Controller 主循环</div>`+
    `</div>`+
    `<div style="background:#ffffff;border:1px solid #e6e8ee;border-radius:8px;padding:12px 16px;box-shadow:0 1px 2px rgba(18,19,22,0.04)">`+
      `<div style="font-size:11px;color:#5f6368;font-weight:500">日志行数</div>`+
      `<div id="logStatLines" style="font-size:14px;font-weight:600;color:#121316;margin-top:4px;font-variant-numeric:tabular-nums">0 行</div>`+
    `</div>`+
    `<div style="background:#ffffff;border:1px solid #e6e8ee;border-radius:8px;padding:12px 16px;box-shadow:0 1px 2px rgba(18,19,22,0.04)">`+
      `<div style="font-size:11px;color:#5f6368;font-weight:500">持久化模式</div>`+
      `<div style="font-size:14px;font-weight:600;color:#5e6ad2;margin-top:4px">WAL 严格追加</div>`+
    `</div>`+
    `<div style="background:#ffffff;border:1px solid #e6e8ee;border-radius:8px;padding:12px 16px;box-shadow:0 1px 2px rgba(18,19,22,0.04)">`+
      `<div style="font-size:11px;color:#5f6368;font-weight:500">更新时间</div>`+
      `<div id="logStatUpdated" style="font-size:14px;font-weight:600;color:#047857;margin-top:4px;font-variant-numeric:tabular-nums">—</div>`+
    `</div>`+
  `</div>`+
  `<div class="log-stream-wrap">`+
    `<pre id="logContentPre" class="log-content-pre">正在拉取调度审计日志…</pre>`+
  `</div>`;
}

async function showLogs(kind){
  if(kind)logState.kind=kind;
  const logEl=document.getElementById('logsTabView');
  setWorkspaceMode('logs');
  if(!state.openWorkflowTabIds)state.openWorkflowTabIds=[];
  if(!state.openWorkflowTabIds.includes('__logs__'))state.openWorkflowTabIds.push('__logs__');
  state.workflowId='__logs__';
  renderWorkflowTabs();
  paintCrumb();
  if(logEl){
    if(!document.getElementById('logContentPre')){
      logEl.innerHTML=renderLogsTabSkeleton();
    }
    logEl.hidden=false;
  }
  await loadLogContent();
}

async function loadLogContent(isAuto){
  const pre=document.getElementById('logContentPre');
  const statLines=document.getElementById('logStatLines');
  const statUpdated=document.getElementById('logStatUpdated');
  const statKind=document.getElementById('logStatKind');
  if(!pre)return;
  if(!isAuto&&(!pre.textContent||!pre.textContent.trim())){
    pre.textContent='正在拉取调度审计日志…';
  }
  try{
    const d=await api('/api/logs?kind='+encodeURIComponent(logState.kind)+'&n='+encodeURIComponent(logState.lines));
    const output=(d&&d.output)||'（暂无日志输出）';
    pre.textContent=output;
    const lineCount=output.trim()?output.split('\n').length:0;
    if(statLines)statLines.textContent=lineCount+' 行';
    if(statUpdated){
      const now=new Date();
      statUpdated.textContent=now.toTimeString().split(' ')[0];
    }
    const kindNames={
      controller:'Controller 主循环',
      controller_err:'Error 异常流',
      sentinel:'Sentinel 巡检',
      notifier:'Notifier 通知'
    };
    if(statKind)statKind.textContent=kindNames[logState.kind]||logState.kind;
    const autoScrollEl=document.getElementById('logAutoScroll');
    if(autoScrollEl&&autoScrollEl.checked){
      pre.scrollTop=pre.scrollHeight;
    }
  }catch(e){
    if(!isAuto)toast(e.message,true);
    if(pre)pre.textContent='拉取日志失败: '+e.message;
  }
}

function switchLogKind(kind){
  logState.kind=kind;
  ['controller','controller_err','sentinel','notifier'].forEach(k=>{
    const btn=document.getElementById('logKind'+(k==='controller'?'Controller':k==='controller_err'?'ControllerErr':k==='sentinel'?'Sentinel':'Notifier'));
    if(btn)btn.classList.toggle('active',k===kind);
  });
  loadLogContent();
}

function switchLogLineCount(val){
  logState.lines=parseInt(val,10)||200;
  loadLogContent();
}

function refreshLogsTab(){
  loadLogContent();
  toast('日志已刷新');
}

function copyCurrentLog(){
  const pre=document.getElementById('logContentPre');
  if(!pre||!pre.textContent){toast('暂无日志内容可复制',true);return;}
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(pre.textContent).then(()=>{toast('已复制日志到剪贴板！');}).catch(()=>{toast('复制失败',true);});
  }else{
    const ta=document.createElement('textarea');
    ta.value=pre.textContent;
    document.body.appendChild(ta);
    ta.select();
    try{document.execCommand('copy');toast('已复制日志到剪贴板！');}catch(e){toast('复制失败',true);}
    document.body.removeChild(ta);
  }
}

function exportLogsFile(){
  const pre=document.getElementById('logContentPre');
  if(!pre||!pre.textContent){toast('暂无日志内容可导出',true);return;}
  const blob=new Blob([pre.textContent],{type:'text/plain;charset=utf-8'});
  const url=URL.createObjectURL(blob);
  const a=document.createElement('a');
  a.href=url;
  a.download=`haflow-${logState.kind}-${Date.now()}.log`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  toast('已导出日志文件');
}

function toggleLogAutoRefresh(enabled){
  if(logState.autoRefreshTimer){
    clearInterval(logState.autoRefreshTimer);
    logState.autoRefreshTimer=null;
  }
  if(enabled){
    logState.autoRefreshTimer=setInterval(()=>{
      if(state.workflowId==='__logs__'&&!document.hidden){
        loadLogContent(true);
      }
    },3000);
    toast('已开启 3s 自动刷新');
  }else{
    toast('已关闭自动刷新');
  }
}
const ARCHIVE_STATUS_CHOICES=[['archived','已归档'],['all','全部状态'],['active','进行中'],['cleaned','已完成'],['superseded','已取代'],['failed','失败'],['completed','待收尾']];
let archiveQuery={page:0,size:50,project_id:'',workflow_id:'',agent:'',status:'archived',q:''};
function getKnownWorkflowsForProject(pid){
  if(!pid)return null;
  if(state.project && (state.projectId===pid || (state.project.project && state.project.project.project_id===pid))){
    return state.project.workflows||[];
  }
  return null;
}
function archiveFiltersHtml(){
  const ps=(state.overview&&state.overview.projects)||[];
  const knownWfs=getKnownWorkflowsForProject(archiveQuery.project_id);
  let wfOptions='<option value="">全部工作流</option>';
  if(knownWfs && knownWfs.length){
    let found=false;
    knownWfs.forEach(w=>{
      const isSel=(archiveQuery.workflow_id===w.workflow_id);
      if(isSel)found=true;
      wfOptions+=`<option value="${esc(w.workflow_id)}"${isSel?' selected':''}>${esc(workflowDisplayName(w))}</option>`;
    });
    if(archiveQuery.workflow_id && !found){
      wfOptions+=`<option value="${esc(archiveQuery.workflow_id)}" selected>${esc(archiveQuery.workflow_id)}</option>`;
    }
  }else if(archiveQuery.workflow_id){
    wfOptions+=`<option value="${esc(archiveQuery.workflow_id)}" selected>${esc(archiveQuery.workflow_id)}</option>`;
  }
  return `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin-bottom:8px">`
    +`<select id="arcProject" onchange="onArchiveProjectChange()"><option value="">全部项目</option>${ps.map(p=>`<option value="${esc(p.project_id)}"${archiveQuery.project_id===p.project_id?' selected':''}>${esc(p.project_name||p.project_id)}</option>`).join('')}</select>`
    +`<select id="arcWorkflow" onchange="onArchiveWorkflowChange()">${wfOptions}</select>`
    +`<select id="arcAgent" onchange="applyArchiveFilters()"><option value="">全部执行者</option>${['opencode','codex','claude','qodercli','agy','pi','grok','kimi'].map(a=>`<option${archiveQuery.agent===a?' selected':''}>${esc(a)}</option>`).join('')}</select>`
    +`<select id="arcStatus" onchange="applyArchiveFilters()">${ARCHIVE_STATUS_CHOICES.map(c=>`<option value="${c[0]}"${archiveQuery.status===c[0]?' selected':''}>${c[1]}</option>`).join('')}</select>`
    +`<input id="arcQ" placeholder="关键词：任务/目标/节点" value="${esc(archiveQuery.q)}" onkeydown="if(event.key==='Enter')applyArchiveFilters()">`
    +`</div><div style="display:flex;justify-content:flex-end;gap:8px;margin-bottom:8px"><button class="btn primary" onclick="applyArchiveFilters()">查询</button></div><div id="archiveList"><div class="empty">正在加载…</div></div>`;
}
function archiveRowsHtml(d){
  const items=d.items||[];
  if(!items.length)return '<div class="empty">没有匹配的任务</div>';
  const size=Math.max(1,d.limit||50);
  const pages=Math.max(1,Math.ceil(d.total/size));
  const page=Math.floor(d.offset/size)+1;
  const rows=items.map(t=>{
    const when=t.updated_at?new Date(t.updated_at*1000).toLocaleString():'—';
    const wfId=t.workflow_id||'';
    const wfLink=wfId?`<a href="javascript:void(0)" style="color:var(--accent);text-decoration:underline;cursor:pointer" onclick="filterArchiveByWorkflow('${esc(wfId)}','${esc(t.project_id||'')}')" title="按此工作流筛选">${esc(wfId)}</a>`:'';
    return `<div class="task"><div><div class="task-name">${esc(t.node_label||t.node||'任务')} <span class="task-id">${esc(t.task_id)}</span> ${badge(t.status)}</div>`
      +`<div class="task-id">${wfLink}${t.project_name?' · '+esc(t.project_name):''} · 执行者 ${esc(t.agent||'-')}${t.stage_verdict?' · 验收 '+esc(t.stage_verdict):''}${t.superseded_by?' · 取代者 '+esc(t.superseded_by):''}</div>`
      +`<div class="task-meta">${esc((t.goal||'').slice(0,140))}</div>`
      +`<div class="task-meta">更新于 ${esc(when)} · 历时 ${esc(formatElapsed(t.duration_seconds))}</div></div>`
      +`<div class="task-actions"><button class="mini" onclick="openTaskDrawer('${esc(t.task_id)}')">详情</button></div></div>`;
  }).join('');
  return rows+`<div class="task-meta" style="padding:12px 16px;display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap"><span>共 ${d.total} 条 · 第 ${page}/${pages} 页</span><span><button class="mini" onclick="archivePage(-1)"${page<=1?' style="opacity:.45;pointer-events:none"':''}>上一页</button> <button class="mini" onclick="archivePage(1)"${page>=pages?' style="opacity:.45;pointer-events:none"':''}>下一页</button></span></div>`;
}
async function populateArchiveWorkflowSelect(projectId,selectedWorkflowId){
  const sel=document.getElementById('arcWorkflow');
  if(!sel)return;
  const knownWfs=getKnownWorkflowsForProject(projectId);
  let wfs=knownWfs;
  if(!wfs){
    try{
      const url='/api/workflows'+(projectId?'?project_id='+encodeURIComponent(projectId):'');
      wfs=await api(url);
    }catch(e){wfs=[]}
  }
  let options='<option value="">全部工作流</option>';
  let found=false;
  (wfs||[]).forEach(w=>{
    const name=workflowDisplayName(w)||w.workflow_id;
    const isSel=(selectedWorkflowId && w.workflow_id===selectedWorkflowId);
    if(isSel)found=true;
    options+=`<option value="${esc(w.workflow_id)}"${isSel?' selected':''}>${esc(name)}</option>`;
  });
  if(selectedWorkflowId && !found){
    options+=`<option value="${esc(selectedWorkflowId)}" selected>${esc(selectedWorkflowId)}</option>`;
  }
  sel.innerHTML=options;
}
async function onArchiveProjectChange(){
  const pVal=document.getElementById('arcProject')?.value||'';
  archiveQuery.project_id=pVal;
  archiveQuery.workflow_id='';
  archiveQuery.page=0;
  await populateArchiveWorkflowSelect(pVal,'');
  loadArchive();
}
function onArchiveWorkflowChange(){
  archiveQuery.workflow_id=(document.getElementById('arcWorkflow')?.value||'').trim();
  archiveQuery.page=0;
  loadArchive();
}
async function filterArchiveByWorkflow(wid,pid){
  if(pid && archiveQuery.project_id!==pid){
    archiveQuery.project_id=pid;
    const pSel=document.getElementById('arcProject');
    if(pSel)pSel.value=pid;
    await populateArchiveWorkflowSelect(pid,wid);
  }else{
    const wSel=document.getElementById('arcWorkflow');
    if(wSel){
      if(!Array.from(wSel.options).some(o=>o.value===wid)){
        const opt=document.createElement('option');
        opt.value=wid;
        opt.textContent=wid;
        wSel.appendChild(opt);
      }
      wSel.value=wid;
    }
  }
  archiveQuery.workflow_id=wid;
  archiveQuery.page=0;
  loadArchive();
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
  if(state.projectId){
    archiveQuery.project_id=state.projectId;
  }
  if(state.workflowId&&state.workflowId!=='__ctl__'&&state.workflowId!=='__templates__'&&state.workflowId!=='__archive__'&&state.workflowId!=='__logs__'){
    archiveQuery.workflow_id=state.workflowId;
  }
  archiveQuery.page=0;
  const arcEl=document.getElementById('archiveTabView');
  if(arcEl){
    arcEl.innerHTML=`<div class="ctl-panel-header"><div style="display:flex;justify-content:space-between;align-items:center;width:100%"><div><strong>任务归档与冷存储</strong><span class="muted" style="margin-left:8px;font-weight:400">· 历史已完成或已归档工作流任务检索</span></div></div></div>`+archiveFiltersHtml();
  }
  setWorkspaceMode('archive');
  if(!state.openWorkflowTabIds)state.openWorkflowTabIds=[];
  if(!state.openWorkflowTabIds.includes('__archive__'))state.openWorkflowTabIds.push('__archive__');
  state.workflowId='__archive__';
  renderWorkflowTabs();
  paintCrumb();
  const knownWfs=getKnownWorkflowsForProject(archiveQuery.project_id);
  if(!knownWfs){
    await populateArchiveWorkflowSelect(archiveQuery.project_id,archiveQuery.workflow_id);
  }
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
setInterval(()=>{if(!document.hidden)refreshAll()},600000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshAll()});
(function(){
  const v=loadViewState();
  if(!v)return;
  state.opsMode=!!v.opsMode;
  state.dashMode=!!v.dashMode;
  state.dashWorkflowId=v.dashWorkflowId||null;
  state.spaceId=v.spaceId||null;
  state.workflowId=v.workflowId||null;
  state.shellView=v.shellView||'workbench';
})();
async function initFromUrlOrState(){
  const p=new URLSearchParams(window.location.search);
  let qWf=p.get('workflow_id');
  const qTask=p.get('task_id'),qPane=p.get('pane_id'),qOps=p.get('ops'),qView=p.get('view');
  if(!qWf&&qTask){
    try{
      const td=await api('/api/task?id='+encodeURIComponent(qTask));
      if(td&&td.task&&td.task.workflow_id)qWf=td.task.workflow_id;
    }catch(e){}
  }
  if(!qWf&&!qTask&&!qPane&&!qOps&&!qView){
    if(state.opsMode)showOpsCenter();
    else if(state.dashMode)showDashboard();
    else if(state.workflowId==='__templates__')showTemplateLibrary();
    else if(state.shellView&&state.shellView!=='workbench')showShellView(state.shellView);
    else refreshAll();
    return;
  }
  if(qOps==='1'||qOps==='true'||qView==='ops'){
    state.opsMode=true;
    state.dashMode=false;
    state.shellView='ops';
  }else if(qView==='dashboard'){
    state.dashMode=true;
    state.opsMode=false;
    state.shellView='dashboard';
    if(qWf)state.dashWorkflowId=qWf;
  }else if(qView==='templates'){
    state.opsMode=false;
    state.dashMode=false;
    state.shellView='templates';
    state.workflowId='__templates__';
  }else if(qView&&['alerts','agents','slots','workflows','workbench'].includes(qView)){
    state.opsMode=false;
    state.dashMode=false;
    state.shellView=qView;
  }
  if(qWf&&!state.dashMode&&state.workflowId!=='__templates__'){
    state.opsMode=false;
    state.dashWorkflowId=null;
    state.workflowId=qWf;
    try{
      const d=await api('/api/workflow?id='+encodeURIComponent(qWf));
      if(d&&d.project){
        if(d.project.project_id)state.projectId=d.project.project_id;
        if(d.project.workspace_id)state.spaceId=d.project.workspace_id;
      }
    }catch(e){}
  }
  if(state.dashMode){
    await showDashboard();
  }else if(state.opsMode){
    await showOpsCenter();
  }else if(state.workflowId==='__templates__'||state.shellView==='templates'){
    await showTemplateLibrary();
  }else if(state.shellView&&state.shellView!=='workbench'){
    await refreshAll();
    showShellView(state.shellView);
  }else{
    await refreshAll();
    if(qWf&&state.workflowId!==qWf){
      try{await loadWorkflow(qWf);}catch(e){}
    }
    if(qTask){
      const el=document.querySelector(`[data-task-id="${CSS.escape?CSS.escape(qTask):qTask}"]`);
      if(el){el.scrollIntoView({behavior:'smooth',block:'center'});el.classList.add('task-highlight');}
      await openTaskDrawer(qTask);
    }else if(qPane){
      await showPane(qPane);
    }
  }
}
window.addEventListener('popstate',()=>{initFromUrlOrState();});
initFromUrlOrState();
</script>
<script>
(function(){
  function setupSlidingPill() {
    const tabsList = document.getElementById('workflowTabsList');
    if (!tabsList) return;
    let pill = tabsList.querySelector('.wf-tab-sliding-pill');
    if (!pill) {
      pill = document.createElement('div');
      pill.className = 'wf-tab-sliding-pill';
      tabsList.prepend(pill);
    }
    const activeTab = tabsList.querySelector('.wf-tab.active');
    if (activeTab) {
      const parentRect = tabsList.getBoundingClientRect();
      const tabRect = activeTab.getBoundingClientRect();
      pill.style.transform = `translateX(${tabRect.left - parentRect.left + tabsList.scrollLeft}px)`;
      pill.style.width = `${tabRect.width}px`;
      pill.style.display = 'block';
    } else {
      pill.style.display = 'none';
    }
  }

  const origRenderTabs = window.renderWorkflowTabs;
  if (typeof origRenderTabs === 'function') {
    window.renderWorkflowTabs = function() {
      origRenderTabs.apply(this, arguments);
      setTimeout(setupSlidingPill, 10);
    };
  }

  function setupSidebarResizer() {
    const sidebar = document.getElementById('sidebar');
    if (!sidebar) return;
    let handle = sidebar.querySelector('.sidebar-resizer-handle');
    if (!handle) {
      handle = document.createElement('div');
      handle.className = 'sidebar-resizer-handle';
      handle.title = '按住拖动调整宽度，双击复位';
      sidebar.appendChild(handle);
    }
    let isDragging = false, startX = 0, startW = 280;
    const savedW = localStorage.getItem('haflow_custom_sidebar_w');
    if (savedW) sidebar.style.width = savedW + 'px';

    handle.addEventListener('mousedown', (e) => {
      isDragging = true;
      startX = e.clientX;
      startW = sidebar.getBoundingClientRect().width;
      handle.classList.add('is-dragging');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
    });
    window.addEventListener('mousemove', (e) => {
      if (!isDragging) return;
      const newW = Math.max(220, Math.min(460, startW + (e.clientX - startX)));
      sidebar.style.width = newW + 'px';
    });
    window.addEventListener('mouseup', () => {
      if (!isDragging) return;
      isDragging = false;
      handle.classList.remove('is-dragging');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      localStorage.setItem('haflow_custom_sidebar_w', sidebar.getBoundingClientRect().width);
    });
    handle.addEventListener('dblclick', () => {
      sidebar.style.width = '280px';
      localStorage.setItem('haflow_custom_sidebar_w', '280');
    });
  }

  window.addEventListener('DOMContentLoaded', () => {
    setupSidebarResizer();
    setupSlidingPill();
  });
})();
</script>
</body></html>'''
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
    def send_static(self,rel):
        base=CONSOLE_STATIC_DIR.resolve()
        target=(base/rel).resolve()
        if base not in target.parents and target!=base:
            return self.send_json(403,error='Forbidden')
        if not target.is_file():
            return self.send_json(404,error='Not Found')
        suffix=target.suffix.lower()
        ctype='text/html; charset=utf-8' if suffix in ('.html', '.htm') else 'application/javascript; charset=utf-8' if suffix=='.js' else 'text/css; charset=utf-8' if suffix=='.css' else 'application/octet-stream'
        raw=target.read_bytes(); self.send_response(200); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(raw))); self.send_header('Cache-Control','public, max-age=3600'); self.end_headers(); self.wfile.write(raw)
    def query(self):return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    def body(self):
        n=int(self.headers.get('Content-Length','0') or 0); raw=self.rfile.read(n) if n else b'{}'; return json.loads(raw.decode() or '{}')
    def do_GET(self):
        p=urllib.parse.urlparse(self.path).path
        try:
            if p=='/':return self.send_html(HTML)
            if p.startswith('/static/'):return self.send_static(p[len('/static/'):])
            if p=='/dashboard':
                self.send_response(302);self.send_header('Location','/?view=dashboard');self.end_headers();return
            if p=='/api/dashboard':return self.send_json(200,dashboard_data(workflow_id=self.query().get('workflow_id',[''])[0] or None))
            if p=='/api/overview':return self.send_json(200,overview())
            if p=='/api/ops-center':return self.send_json(200,ops_center(self.query().get('workflow_id',[''])[0] or None,self.query().get('include_tasks',[''])[0]=='1'))
            if p=='/api/templates':return self.send_json(200,templates_summary())
            if p=='/api/template':return self.send_json(200,template_detail(self.query().get('id',[''])[0]))
            if p=='/api/run/status':return self.send_json(200,workflow_job_status(self.query().get('id',[''])[0]))
            if p=='/api/project':return self.send_json(200,project_detail(self.query().get('id',[''])[0]))
            if p=='/api/workflow':return self.send_json(200,workflow_detail(self.query().get('id',[''])[0]))
            if p=='/api/workflows':return self.send_json(200,api_workflows(self.query().get('project_id',[''])[0] or None))
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
                agent=self.query().get('agent',[None])[0]
                return self.send_json(200,deep_preflight(x,agent=agent))
            if p=='/api/preflight':
                x=project_by_id(self.query().get('id',[''])[0]);
                if not x:raise RuntimeError('项目不存在')
                return self.send_json(200,preflight(x))
            if p=='/api/logs':
                q=self.query()
                kind=q.get('kind',['controller'])[0]
                try:
                    n_lines=int(q.get('n',['200'])[0])
                except (ValueError,TypeError):
                    n_lines=200
                return self.send_json(200,{'output':tail_log(kind,n=n_lines)})
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
            if p=='/api/workflow/controller-actions':
                wid=self.query().get('workflow_id',[''])[0] or self.query().get('id',[''])[0]
                return self.send_json(200,api_workflow_controller_actions(wid))
            if p=='/api/workflow/recovery':
                return self.send_json(200,api_workflow_recovery(self.query().get('workflow_id',[''])[0]))
            if p=='/api/workflow/decisions':
                wid=self.query().get('workflow_id',[''])[0] or self.query().get('id',[''])[0]
                return self.send_json(200,api_workflow_decisions(wid))
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
            if p=='/api/workflow/rename':return self.send_json(200,update_workflow_title(str(b['workflow_id']),str(b.get('title') or '')))
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
            if p=='/api/controller/execute-action':return self.send_json(200,api_controller_execute_action(b))
            if p=='/api/workflow/decision/raise':return self.send_json(200,api_workflow_decide_raise(b))
            if p=='/api/workflow/decision':return self.send_json(200,api_workflow_decide_resolve(b))
            if p=='/api/workflow/recovery/decision':return self.send_json(200,api_workflow_recovery_decide(b))
            return self.send_json(404,error='Not Found')
        except Exception as e:
            self.log_message('POST %s failed: %s', self.path, e)
            return self.send_json(500,error=e)

def main():
    try:
        from herdr import service_release
        import_root = Path(service_release.__file__).resolve().parent.parent
        fingerprint = service_release.runtime_fingerprint(
            import_root, [Path(__file__).resolve(), Path(service_release.__file__).resolve(),
                          import_root / "herdr" / "state_store.py", import_root / "herdr" / "kernel.py"],
        )
    except Exception:
        fingerprint = {"running_sha": "unknown", "running_import_root": "unknown", "component_versions": {}}
    print("HERDR_RUNTIME_FINGERPRINT=" + json.dumps(
        {"service": "com.user.herdr-factory-console", "pid": os.getpid(), **fingerprint}, sort_keys=True,
    ), flush=True)
    ROOT.mkdir(parents=True,exist_ok=True); print(f'{PRODUCT_NAME}控制台: http://{HOST}:{PORT}',flush=True); ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
if __name__=='__main__':main()
