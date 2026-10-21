import json
from pathlib import Path
from herdr.reason_labels import human_reason
from herdr.agent_binary import resolve_agent_binary
from console import common, inventory

def panes(workspace):
    try:
        d=common.run_json(['herdr','pane','list','--workspace',workspace]).get('result',{})
        return d.get('panes',[]) if isinstance(d,dict) else d if isinstance(d,list) else []
    except Exception:return []

def tabs(workspace):
    try:
        d=common.run_json(['herdr','tab','list','--workspace',workspace]).get('result',{})
        return d.get('tabs',[]) if isinstance(d,dict) else d if isinstance(d,list) else []
    except Exception:return []

def agent_runtime(pane):
    if not pane:return None
    r=common.run(['herdr','agent','get',pane],8)
    if r.returncode!=0:return None
    try:return json.loads(r.stdout)['result']['agent']
    except Exception:return None

def pool(pid):return common.load_json(common.POOLS_FILE,{'projects':{}}).get('projects',{}).get(pid,{})

def agent_loads(pid):
    d={a:0 for a in AGENTS}
    for t in inventory.tasks():
        if t.get('project_id')==pid and t.get('status') in IN_FLIGHT_STATUSES and t.get('agent'):d[t['agent']]=d.get(t['agent'],0)+1
    return d

def preflight(p, loads=None):
    po=pool(p.get('project_id')); allowed=po.get('allowed_agents',AGENTS); disabled=set(po.get('disabled_agents',[])); loads=agent_loads(p.get('project_id')) if loads is None else loads
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
    script = common.HERDR_ROOT / "bin" / "herdr-deep-preflight"
    if not script.exists():
        script = common.HERDR_ROOT / "herdr" / "deep_preflight.py"
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

    r = common.run(cmd, 320)

    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip() or "Deep Preflight 执行失败")

    try:
        return json.loads(r.stdout)
    except Exception as e:
        raise RuntimeError("Deep Preflight JSON 解析失败") from e

def slots(p, p_panes=None, claimed=None):
    cfg=common.load_json(Path(p.get('workflow_file','')),{}) ; stage_by_tab={s.get('tab_id'):s for s in cfg.get('stages',[])}; anchors={s.get('anchor_pane_id') for s in cfg.get('stages',[])}; binds=common.load_json(common.SLOTS_FILE,{'panes':{}}).get('panes',{}); out=[]
    if claimed is None:claimed={t.get('pane_id'):t.get('task_id') for t in inventory.tasks() if t.get('pane_id')}
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
        r=common.run(['launchctl','list'],5)
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
        d = common.run_json(['herdr', 'workspace', 'list'], 10).get('result', {})
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

    registered_roots = {p.get('project_root') for p in inventory.projects() if p.get('project_root')}

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
    ps = inventory.projects()
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

TERMINAL_WORKFLOW_STATUSES = {
    'completed',
    'failed',
    'halted',
    'closed',
    'abandoned',
    'archived',
    'cancelled',
}


def overview():
    ts = inventory.tasks()
    wfs = inventory.workflows()
    superseded_ids = {t['supersedes'] for t in ts if t.get('supersedes')}
    terminal_wf_statuses = TERMINAL_WORKFLOW_STATUSES
    active_wf_ids = {
        t.get('workflow_id')
        for t in ts
        if t.get('status') in common.ACTIVE and t.get('workflow_id')
    }

    alerts = []
    for t in ts:
        if t.get('status') not in {'blocked', 'failed'}:
            continue
        if t.get('status') == 'superseded' or t.get('superseded_by') or t.get('task_id') in superseded_ids:
            continue
        wid = t.get('workflow_id')
        wf = wfs.get(wid) if wid else None
        if wid:
            if wf and wf.get('status') in terminal_wf_statuses:
                continue
            if not wf and wid not in active_wf_ids:
                continue
        pid = t.get('project_id') or (wf.get('project_id') if wf else None)
        pname = t.get('project_name') or (wf.get('project_name') if wf else None)
        alerts.append({
            'task_id': t.get('task_id'),
            'workflow_id': t.get('workflow_id'),
            'project_id': pid,
            'project_name': pname,
            'status': t.get('status'),
            'agent': t.get('agent'),
            'pane_id': t.get('pane_id'),
            'stage': t.get('stage') or t.get('node'),
            'stage_label': t.get('stage_label') or t.get('node_label') or t.get('stage'),
            'created_at': t.get('created_at'),
            'updated_at': t.get('updated_at'),
            'reason': human_reason(
                t.get('failure_reason')
                or t.get('blocked_reason')
                or t.get('sentinel_reason')
                or ''
            ),
        })
    ss = spaces()
    return {
        'projects': inventory.projects(),
        'spaces': ss,
        'space_count': len(ss),
        'active_workflows': len({
            t.get('workflow_id')
            for t in ts
            if t.get('status') in common.ACTIVE
        }),
        'active_agents': sum(
            1 for t in ts
            if t.get('status') in {'working', 'blocked', 'dispatched'}
        ),
        'alerts': alerts[-50:],
        'services': service_status(),
    }

AUTH_HINTS = {
    'codex': [common.HOME / '.codex' / 'auth.json'],
    'claude': [common.HOME / '.claude.json'],
    'pi': [common.HOME / '.pi' / 'agent' / 'auth.json'],
    'opencode': [common.HOME / '.config' / 'opencode'],
    'qodercli': [common.HOME / '.qoder-cn'],
    'agy': [common.HOME / '.agy'],
    'grok': [common.HOME / '.grok' / 'auth.json'],
    'kimi': [common.HOME / '.kimi-code' / 'credentials' / 'kimi-code.json', common.HOME / '.kimi-code' / 'config.toml'],
}

IN_FLIGHT_STATUSES = {'pending', 'dispatched', 'working', 'blocked', 'agent_done', 'rework'}

AGENTS=['opencode','codex','claude','qodercli','agy','pi','grok','kimi']
