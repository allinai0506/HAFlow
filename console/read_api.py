import json, sqlite3, subprocess, time
from pathlib import Path
from herdr import human_decisions as herdr_human_decisions
from herdr.kernel import state_db_path
from herdr import projection as herdr_projection
from herdr import deliverables as herdr_deliverables
from herdr import archive as herdr_archive
from herdr import controller_actions as herdr_controller_actions
from herdr import delivery_pipeline as herdr_delivery_pipeline
from herdr import dashboard as herdr_dashboard
from herdr import workflow_docs as herdr_workflow_docs
from herdr import delivery_record as herdr_delivery_record
from console import common, inventory as inventory_module, runtime

def _project_workflow_template(p):
    """项目当前生效的模板名(读项目 workflow.json;缺失返回空串)。"""
    if not p or not p.get('workflow_file'):return ''
    cfg=common.load_json(Path(p.get('workflow_file')),{}) or {}
    return str(cfg.get('workflow_template') or '')

def project_detail(pid, lightweight=False):
    p=inventory_module.project_by_id(pid)
    if not p:raise RuntimeError('项目不存在')
    p={**p,'workflow_template':_project_workflow_template(p)}
    if lightweight:
        from herdr.console_queries import project_workflows, project_agent_loads
        db_path=state_db_path()
        ws=[inventory_module._with_subject(w) for w in project_workflows(pid, db_path)]
        loads=project_agent_loads(pid, sorted(runtime.IN_FLIGHT_STATUSES), db_path)
        return {'project':p,'workflows':ws,'latest_workflow_id':ws[0]['workflow_id'] if ws else None,
                'tabs':[],'panes':[],'slots':[],'agents':runtime.preflight(p,loads=loads),'runtime_pending':True}
    ws=inventory_module.workflows_for_project(pid)
    p_panes=runtime.panes(p.get('workspace_id'))
    try:
        s=runtime.slots(p,p_panes=p_panes)
    except TypeError:
        s=runtime.slots(p)
    return {'project':p,'workflows':ws,'latest_workflow_id':ws[0]['workflow_id'] if ws else None,'tabs':runtime.tabs(p.get('workspace_id')),'panes':p_panes,'slots':s,'agents':runtime.preflight(p)}

def project_runtime(pid):
    p=inventory_module.project_by_id(pid)
    if not p:raise RuntimeError('项目不存在')
    p_panes=runtime.panes(p.get('workspace_id'))
    from herdr.state_db import get_readonly_db_connection
    conn=get_readonly_db_connection(state_db_path())
    try:
        pane_ids=[row['pane_id'] for row in p_panes if row.get('pane_id')]
        marks=','.join('?' for _ in pane_ids)
        claimed={row['pane_id']:row['task_id'] for row in conn.execute(
            'SELECT pane_id,task_id FROM tasks WHERE pane_id IN ('+marks+') ORDER BY created_at ASC',pane_ids)} if pane_ids else {}
    finally:conn.close()
    return {'tabs':runtime.tabs(p.get('workspace_id')),'panes':p_panes,'slots':runtime.slots(p,p_panes=p_panes,claimed=claimed)}

def workflow_stages(wid, p, w=None, definition=None):
    """阶段卡片来源:优先该次运行的冻结定义,次选项目 workflow.json 的 nodes(id/label),回退内置 STAGES。

    背景(wf-nexusarchive-0918-02):前端阶段卡片此前硬编码 software-development
    的 6 阶段,切换模板(general-task-v1 等)后界面不跟随,必须按工作流
    自己的节点定义渲染。
    隔离回归(工作空间模板切换):项目 reprovision 后不得改写历史运行的
    阶段展示,运行冻结定义(definition/config)优先于项目当前模板。
    """
    cfg={}
    if isinstance(definition,dict) and definition.get('nodes'):
        cfg=definition
    if not cfg.get('nodes') and isinstance(w,dict) and isinstance(w.get('config'),dict):
        cfg=w['config']
    if not cfg.get('nodes') and p and p.get('workflow_file'):
        cfg=common.load_json(Path(p.get('workflow_file')),{}) or {}
    out=[]
    for n in (cfg.get('nodes') or []):
        if not isinstance(n, dict): continue
        key=n.get('id') or n.get('key')
        if not key:continue
        out.append((key,n.get('label') or key))
    return out or inventory_module.STAGES

def _referenced_task_inventory(wid,p,ts,w):
    """Tasks of the workflow plus explicitly referenced task ids (bounded, fail-soft).

    The graph projection only looks up referenced ids by task id, so they are
    fetched in one bounded read-only query instead of one connection per id
    (which would also bypass the read-only guarantee).
    """
    definition = w.get('config') if isinstance(w, dict) and isinstance(w.get('config'), dict) and w.get('config') else None
    if not definition:
        definition = inventory_module.workflow_definition_for(wid, p, w)
    inventory = list(ts)
    owned_ids = {t.get('task_id') for t in ts}
    required_ids = []
    for node in (definition.get('nodes') if isinstance(definition, dict) and isinstance(definition.get('nodes'), list) else []):
        if not isinstance(node, dict):
            continue
        required = node.get('required_task_ids')
        if isinstance(required, list) and len(required) <= 64:
            for task_id in required:
                if isinstance(task_id, str) and task_id not in owned_ids:
                    required_ids.append(task_id)
                    owned_ids.add(task_id)
    if required_ids:
        # workflow_tasks is scoped to one workflow_id, so fetch by id here via
        # the read-only connection in one bounded IN query.
        from herdr.state_db import get_readonly_db_connection
        conn=get_readonly_db_connection(state_db_path())
        try:
            marks=','.join('?' for _ in required_ids)
            rows=conn.execute(
                f'SELECT * FROM tasks WHERE task_id IN ({marks})',
                tuple(required_ids),
            ).fetchall()
            from herdr.state_db import _decode_task_row
            inventory.extend(_decode_task_row(r) for r in rows)
        finally:
            conn.close()
    return definition,inventory

def candidate_publication_times(wid):
    """Latest ``candidate_published`` event time per task (one bounded query).

    Read-only and fail-soft: a delivery badge is a display projection, so a
    missing or unreadable ledger must not take the whole workbench down.
    """
    wid=str(wid or '').strip()
    if not wid:return {}
    from herdr.state_db import get_readonly_db_connection
    try:
        conn=get_readonly_db_connection(state_db_path())
        try:
            rows=conn.execute(
                "SELECT task_id,MAX(timestamp) AS published_at FROM events "
                "WHERE workflow_id=? AND event_type='candidate_published' "
                "AND source='herdr-task' GROUP BY task_id",(wid,)).fetchall()
        finally:conn.close()
    except (sqlite3.Error,OSError):
        return {}
    return {str(r['task_id']):r['published_at'] for r in rows if r['task_id'] and r['published_at'] is not None}

def attach_delivery(tasks,workflow,definition=None):
    """Attach one delivery-pipeline projection per task (issue #253).

    The projection itself is pure core (`herdr.delivery_pipeline`); this shell
    only supplies the two I/O facts it cannot read: the pinned node definition
    and the candidate publication event.
    """
    tasks=list(tasks or [])
    if not any(herdr_delivery_pipeline.stage_of(t.get('status')) for t in tasks):
        return tasks
    published=candidate_publication_times((workflow or {}).get('workflow_id'))
    return [{**t,'delivery':herdr_delivery_pipeline.delivery_projection(
                t,workflow,tasks,config=definition,
                published_at=published.get(str(t.get('task_id') or '')))}
            for t in tasks]

def workflow_detail(wid, *, lightweight=False):
    if lightweight:
        from herdr.recovery_store import read_snapshot
        w, _, ts = read_snapshot(state_db_path(), wid)
        p = inventory_module.project_by_id(w.get('project_id')) or w
    else:
        w=inventory_module.workflow_by_id(wid)
        if not w:raise RuntimeError('工作流不存在')
        p=inventory_module.project_for_workflow(wid)
        ts=inventory_module.tasks_for_workflow(wid)
    ts=inventory_module.enrich_workflow_tasks(wid,p,ts)
    definition,inventory=_referenced_task_inventory(wid,p,ts,w)
    ts=attach_delivery(ts,w,definition=definition)
    ss=[]
    for k,l in workflow_stages(wid,p,w,definition=definition):
        x=inventory_module.stage_summary(ts,k,workflow=w); x['label']=l; ss.append(x)
    stall_info=herdr_projection.detect_workflow_stalls(wid,ts,workflow=w)
    graph=inventory_module.workflow_graph_for(wid,p,w,ts,definition=definition,all_tasks=inventory)
    derived={'current_nodes':graph.get('current_nodes',[]),'ready_nodes':graph.get('ready_nodes',[]),'derived_current_stage':graph.get('current_stage',''),'current_stage_source':graph.get('current_stage_source','derived_nodes')}
    if lightweight:
        deliverables={**herdr_deliverables.pack([]), 'pending': True}
    else:
        try:
            deliverables=api_workflow_deliverables(wid)
        except Exception:
            deliverables=herdr_deliverables.pack([], error='成果目录读取失败')
    return {'workflow':{'workflow_id':wid,**inventory_module._with_subject(w),**derived},'project':p,'stages':ss,'tasks':ts,'graph':graph,'context':graph.get('context',{'required':[],'optional':[]}),'coordinator':None if lightweight else runtime.agent_runtime(w.get('coordinator_pane_id')), 'coordinator_pending':lightweight,'candidate_branch':w.get('candidate_branch'),'agent_override':w.get('agent_override','auto'),'stall':stall_info,'deliverables':deliverables}

def task_detail(tid):
    t=next((x for x in inventory_module.tasks() if x.get('task_id')==tid),None)
    if not t:raise RuntimeError('Task 不存在')
    return {'task':t,'runtime':runtime.agent_runtime(t.get('pane_id'))}

def read_pane(pid):
    r=common.run(['herdr','pane','read',pid,'--source','visible'],12)
    if r.returncode!=0:raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout

def _workflow_nodes(wid, w=None):
    w = w if w is not None else (inventory_module.workflows().get(wid) or {})
    cfg = w.get('config') if isinstance(w, dict) else None
    if isinstance(cfg, dict) and isinstance(cfg.get('nodes'), list) and cfg.get('nodes'):
        return cfg['nodes']
    try:
        definition = inventory_module.workflow_definition_for(wid, inventory_module.project_for_workflow(wid), w) or {}
    except Exception:
        definition = {}
    nodes = definition.get('nodes') if isinstance(definition, dict) else None
    return nodes or []

def _load_deliverable_observations(task_id):
    if not task_id:
        return []
    from herdr.deliverable_store import read_enrichment
    try:
        facts = read_enrichment([{'task_id': task_id}], state_db_path(), pull_requests=False)
        return facts.get((None, task_id), {}).get('observations', [])
    except Exception:
        return []

def _load_deliverable_pr(task):
    if not task.get('task_id') or not task.get('workflow_id'):
        return None
    from herdr.deliverable_store import read_enrichment
    facts = read_enrichment([task], state_db_path(), observations=False)
    return facts.get((task['workflow_id'], task['task_id']), {}).get('pull_request')

def _tasks_for_deliverables(tasks):
    tasks = [task for task in (tasks or []) if isinstance(task, dict)]
    loader = _deliverable_observation_loader
    pr_loader = _deliverable_pr_loader
    default_observations = loader is _load_deliverable_observations
    default_prs = pr_loader is _load_deliverable_pr
    facts = {}
    if tasks and (default_observations or default_prs):
        from herdr.deliverable_store import read_enrichment
        try:
            facts = read_enrichment(tasks, state_db_path(),
                                    observations=default_observations, pull_requests=default_prs)
        except Exception:
            print('[DELIVERABLES WARN] persisted receipts could not be read', flush=True)
    rows = []
    for task in tasks:
        row = dict(task)
        extra_facts = facts.get((row.get('workflow_id'), row.get('task_id')), {})
        try:
            extra = extra_facts.get('observations', []) if default_observations else ((loader(row.get('task_id')) or []) if loader else [])
        except Exception:
            extra = []
        if extra:
            row['observations'] = list(row.get('observations') or []) + list(extra)
        if pr_loader:
            try:
                receipt = extra_facts.get('pull_request') if default_prs else pr_loader(row)
                if receipt:
                    row['pull_request'] = receipt
            except Exception:
                print('[DELIVERABLES WARN] PR receipt could not be read', flush=True)
        rows.append(row)
    return rows

def api_workflow_deliverables(wid):
    if not wid:
        raise RuntimeError('workflow_id 不能为空')
    from herdr.recovery_store import read_snapshot
    w, _, ts = read_snapshot(state_db_path(), wid)
    return herdr_deliverables.project_for_tasks(
        wid, _tasks_for_deliverables(ts), _workflow_nodes(wid, w),
    )

def api_read_deliverable(wid, tid, did, download=False):
    if not wid or not tid or not did:
        raise herdr_deliverables.DeliverableError('not_found', 404, '找不到这项成果')
    from herdr.recovery_store import read_snapshot
    try:
        w, _, ts = read_snapshot(state_db_path(), wid)
    except (ValueError, FileNotFoundError):
        raise herdr_deliverables.DeliverableError('not_found', 404, '找不到这项成果')
    own = [task for task in ts if task.get('task_id') == tid]
    return herdr_deliverables.read_deliverable(
        wid, _tasks_for_deliverables(own), _workflow_nodes(wid, w), tid, did, download=download,
    )

class TraceQueryError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = int(status)
        self.message = str(message)

def api_task_trajectory(tid, limit=100, cursor=None):
    """Read one page of a task execution trace. This does not dispatch or write."""
    tid = str(tid or '').strip()
    if not tid:
        raise TraceQueryError(400, 'task_id 不能为空')
    if limit in (None, ''):
        limit = 100
    try:
        page_limit = int(limit)
    except (TypeError, ValueError):
        raise TraceQueryError(400, 'limit 无效')
    if page_limit < 1 or page_limit > 200:
        raise TraceQueryError(400, 'limit 无效')
    from herdr.execution_trace import query_task_trace, read_trace_tasks
    db_path = state_db_path()
    try:
        found = read_trace_tasks(db_path, tid)
    except FileNotFoundError as exc:
        raise TraceQueryError(503, '任务轨迹数据库暂不可用。') from exc
    except sqlite3.Error as exc:
        raise TraceQueryError(503, '任务轨迹数据库暂不可用。') from exc
    task = found[0] if found else None
    if task is None:
        raise TraceQueryError(404, '任务不存在')
    try:
        return query_task_trace(task, found, db_path=db_path, limit=page_limit, cursor=cursor or None)
    except ValueError as exc:
        raise TraceQueryError(400, str(exc)) from exc

def api_task_projection(tid):
    if not tid:raise RuntimeError('task_id 不能为空')
    proj = herdr_projection.project_task(tid)
    wid = proj.get('workflow_id')
    try:
        if wid:
            from herdr.recovery_store import read_snapshot
            workflow,_,workflow_tasks=read_snapshot(state_db_path(),wid)
            own = _tasks_for_deliverables([task for task in workflow_tasks if task.get('task_id') == tid])
            proj['deliverables'] = herdr_deliverables.project_for_tasks(
                wid, own, _workflow_nodes(wid,workflow), include_expectations=False,
            )
            # Delivery pipeline row (#253): read from the same frozen definition
            # and sibling tasks the graph uses, so the drawer cannot disagree
            # with the flow view about where the task is.  Isolated from the
            # artifacts read: a projection failure is not an artifacts failure.
            task = next((t for t in workflow_tasks if t.get('task_id') == tid), None)
            if task:
                try:
                    published=candidate_publication_times(wid).get(str(tid))
                    proj['delivery']=herdr_delivery_pipeline.delivery_projection(
                        task,workflow,workflow_tasks,
                        config=workflow.get('config') if isinstance(workflow.get('config'),dict) else None,
                        published_at=published)
                except (KeyError,TypeError,ValueError):
                    pass
        else:
            proj['deliverables'] = herdr_deliverables.pack([])
    except Exception:
        proj['deliverables'] = herdr_deliverables.pack([], error='成果目录读取失败')
    return proj

def api_workflow_projection(wid):
    if not wid:raise RuntimeError('workflow_id 不能为空')
    return herdr_projection.project_workflow(wid)

def api_workflows(pid=None):
    from herdr.console_queries import project_workflows
    all_w = {w['workflow_id']:w for w in project_workflows(pid,state_db_path())}
    res = []
    for wid, w in all_w.items():
        w_proj = w.get('project_id') or ''
        if pid and w_proj != pid:
            continue
        item = inventory_module._with_subject(w)
        res.append({
            'workflow_id': wid,
            'project_id': w_proj,
            'status': w.get('status') or 'unknown',
            'title': item.get('title') or '',
            'requirement_subject': item.get('requirement_subject') or '',
            'created_at': item.get('created_at') or 0,
        })
    res.sort(key=lambda x: (x.get('created_at') or 0, x['workflow_id']), reverse=True)
    return res

def archive_query(project_id=None,workflow_id=None,agent=None,status=None,q=None,limit=50,offset=0):
    """归档查询读取 StateStore(唯一事实源)。"""
    return herdr_archive.query_archived_tasks_sqlite(
        state_db_path(),
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

def dashboard_inventory():
    from herdr.console_queries import read_inventory
    return read_inventory(state_db_path())

def dashboard_data(limit_tasks=50, limit_workflows=50, workflow_id=None):
    """人类仪表板聚合(只读旁路):任务+工位实时/等你决策/最新交付/卡住。

    数据权威: tasks→StateStore经kernel; 工位实时→herdr agent get(只读探针);
    阻断→controller_actions; 交付→workflow_docs+delivery_record; 停滞→projection;
    工作流标题→workflows()+_with_subject。
    任一来源失败只隔离该工作流/工位,不拖垮整页;工位探针有界(≤20)+3s 超时+并发上限 8。
    workflow_id 未知时返回空分段(fail-soft),不抛错。
    """
    now=time.time()
    _all,wf_map=dashboard_inventory()
    if not isinstance(_all,list):
        _all=[]
    scope=(workflow_id or '').strip() or None
    all_tasks=[t for t in _all if (t.get('workflow_id') or 'unknown')==scope] if scope else _all
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

    all_by_wf={};latest_by_wf={};pane_latest={}
    for t in _all:
        wid=t.get('workflow_id') or 'unknown'
        all_by_wf.setdefault(wid,[]).append(t)
        latest_by_wf[wid]=max(latest_by_wf.get(wid,0),_upd(t))
    active=[t for t in all_tasks if t.get('status') not in {'superseded','cleaned'} and not t.get('superseded_by')]
    by_wf={}
    for t in active:
        wid=t.get('workflow_id') or 'unknown'
        by_wf.setdefault(wid,[]).append(t)
        if t.get('pane_id'):pane_latest[t['pane_id']]=max(pane_latest.get(t['pane_id'],0),_upd(t))
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
    _panes=sorted(pane_latest,key=pane_latest.get,reverse=True)[:20]
    runtimes={}
    if _panes:
        with _fut.ThreadPoolExecutor(max_workers=8) as _ex:
            _res=dict(zip(_panes,_ex.map(_dashboard_pane_runtime,_panes)))
        runtimes={p:v for p,v in _res.items() if isinstance(v,dict)}
    # 最新交付:最近 20 个工作流各取有效交付(失败隔离)
    recent_wf=sorted({t.get('workflow_id') for t in all_tasks if t.get('workflow_id')},key=latest_by_wf.get,reverse=True)[:20]
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
    _by_wf_all=all_by_wf
    wf_options=[]
    for wid in sorted(_by_wf_all, key=latest_by_wf.get, reverse=True)[:50]:
        wts=_by_wf_all[wid]
        subj=inventory_module._with_subject(dict(wf_map.get(wid) or {}, workflow_id=wid))
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

_deliverable_observation_loader=None
_deliverable_pr_loader=None
