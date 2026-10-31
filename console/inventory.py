from pathlib import Path
from herdr import workflow as herdr_workflow
from herdr import workflow_graph as herdr_workflow_graph
from herdr import projects as herdr_projects
from herdr import kernel as herdr_kernel
from herdr.kernel import state_db_path
from herdr import controller_actions as herdr_controller_actions
from console import common


def ops_center(workflow_id=None,include_tasks=False):
    from herdr.ops_center import build_ops_center_payload
    payload = build_ops_center_payload(workflow_id, include_tasks, db_path=state_db_path())
    cards = payload.get("workflow_cards") if isinstance(payload, dict) else None
    if not isinstance(cards, list):
        return payload
    # startup 字段（含 failed_stage）由 ops_center 投影经同一 read_inventory
    # 携带；逐卡片重开 recovery snapshot 会因超大 config 使整页请求失败
    # (PR #364 评审遗留)。
    return payload

def projects():return list(common.load_json(common.PROJECTS_FILE,{'projects':{}}).get('projects',{}).values())

def workflows():return herdr_kernel.load_workflows_data().get('workflows',{})

def tasks():return herdr_kernel.load_tasks_data().get('tasks',[])

def project_by_id(pid):return next((p for p in projects() if p.get('project_id')==pid),None)

def workflow_by_id(wid):
    from herdr.console_queries import workflow_snapshot
    return workflow_snapshot(wid,db_path=state_db_path())

def project_for_workflow(wid):
    w=workflow_by_id(wid); return (project_by_id(w.get('project_id')) if w else None) or w

def _with_subject(w):
    w=dict(w)
    t = (w.get('title') or '').strip()
    s = (w.get('requirement_subject') or '').strip()
    if t:
        w['requirement_subject']=t
    elif not s:
        w['requirement_subject']=herdr_projects.requirement_subject(w.get('requirement',''))
    w['startup_ready'] = w.get('startup_ready') is not False
    w['outcome'] = str(w.get('outcome') or '')
    w['failed_stage'] = str(w.get('failed_stage') or '')
    w['startup_failure_reason'] = str(w.get('startup_failure_reason') or '')[:2048]
    return w

def workflows_for_project(pid):
    from herdr.console_queries import project_workflow_snapshots
    out=[{'workflow_id':w['workflow_id'],**_with_subject(w)}
         for w in project_workflow_snapshots(pid,db_path=state_db_path())]
    # 工作流 ID 混用两套命名(wf-proj-<hash>-<时间戳> 与 wf-proj-<MMDD>-<序号>)，
    # 字典序不再等于时间序；SQL 已按 created_at 倒序，保证默认选中最新的工作流。
    return out

def tasks_for_workflow(wid):
    from herdr.console_queries import workflow_tasks
    return workflow_tasks(wid,db_path=state_db_path())

def workflow_node_types(wid,p):
    cfg=common.load_json(Path(p.get('workflow_file')),{}) if p and p.get('workflow_file') else {}
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
        raw=common.load_json(Path(path),{}) if path else {}
        return {**raw,'configuration_error':'workflow_config_invalid'}
    except Exception:
        pass
    try:
        if p and p.get('workflow_file'):
            raw=common.load_json(Path(p.get('workflow_file')),{})
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

def workflow_graph_for(wid,p,w,ts, *, definition=None, all_tasks=None):
    """Pure projection wiring: definition + tasks + blockers -> graph. Never raises."""
    try:
        if definition is None:
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
            for k in ("status","gate_overrides","current_stage","workflow_id","outcome","startup_ready","failed_stage"):
                if w.get(k) is not None:
                    payload[k]=w[k]
        return herdr_workflow_graph.workflow_graph_projection(payload,ts or [],blockers,all_tasks=tasks() if all_tasks is None else all_tasks)
    except Exception:
        return {'nodes':[],'edges':[],'context':{'required':[],'optional':[]}}

def enrich_workflow_tasks(wid,p,ts):
    node_types=workflow_node_types(wid,p)
    return [{**t,'node_type':t.get('node_type') or node_types.get(t.get('node') or t.get('stage'))} for t in ts]

def _workflow_counts_as_finished(workflow):
    if not isinstance(workflow, dict):
        return False
    if str(workflow.get("outcome") or "") in {"startup_failed", "abandoned"}:
        return False
    if workflow.get("startup_ready") is False:
        return False
    return workflow.get("status") in {"completed", "cleaned", "archived"}

def stage_summary(ts,key,workflow=None):
    xs=[t for t in ts if t.get('stage')==key]
    live=[t for t in xs if t.get('status')!='superseded' and not t.get('superseded_by')]
    is_wf_completed=_workflow_counts_as_finished(workflow)
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

STAGES=[('requirements','需求分析'),('plan','计划'),('implementation','实现'),('test','测试'),('review','评审'),('wrapup','收尾')]
