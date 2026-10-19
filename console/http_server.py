import json, urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from herdr import projects as herdr_projects
from herdr.kernel import state_db_path
from herdr import deliverables as herdr_deliverables
from console import common, control_api, inventory, read_api, runtime, task_api, workflow_api
from console.page import HTML

def _get_console_hub_client(token=None):
    from herdr.experience.client import ExperienceClient
    return ExperienceClient(token=(token or "").strip(), allow_env_token=False)

def _hub_error_status(res):
    """Normalize Hub failure codes to a valid HTTP status (502 on transport failure)."""
    try:
        code = int(res.get('status_code') or 0)
    except (TypeError, ValueError):
        code = 0
    return code if 100 <= code <= 599 else 502

def api_experience_candidates(q=None, token=None):
    q = q or {}
    client = _get_console_hub_client(token=token)
    status = q.get('status', [None])[0] if isinstance(q.get('status'), list) else q.get('status')
    cust = q.get('customer', [None])[0] if isinstance(q.get('customer'), list) else q.get('customer')
    try:
        cands = client.list_candidates(status=status, customer=cust, raise_for_status=True)
        return {'ok': True, 'count': len(cands), 'candidates': [c.to_dict() for c in cands]}
    except Exception as exc:
        status_code = getattr(exc, 'status_code', 500)
        return {'ok': False, 'error': str(exc), 'status_code': status_code, 'candidates': []}

def api_experience_candidate(exp_id, token=None):
    client = _get_console_hub_client(token=token)
    try:
        exp = client.get_experience(exp_id, raise_for_status=True)
        if not exp:
            return {'ok': False, 'error': 'not_found', 'status_code': 404}
        return {'ok': True, 'experience': exp.to_dict()}
    except Exception as exc:
        status_code = getattr(exc, 'status_code', 500)
        return {'ok': False, 'error': str(exc), 'status_code': status_code}

def api_experience_rules(q=None, token=None):
    q = q or {}
    client = _get_console_hub_client(token=token)
    cust = q.get('customer', [None])[0] if isinstance(q.get('customer'), list) else q.get('customer')
    scope = q.get('scope', [None])[0] if isinstance(q.get('scope'), list) else q.get('scope')
    try:
        rules = client.query_rules(customer=cust, raise_for_status=True, scope=scope)
        return {'ok': True, 'count': len(rules), 'rules': [r.to_dict() for r in rules]}
    except Exception as exc:
        status_code = getattr(exc, 'status_code', 500)
        return {'ok': False, 'error': str(exc), 'status_code': status_code, 'rules': []}

def api_experience_review(b, token=None):
    b = b or {}
    client = _get_console_hub_client(token=token)
    exp_id = str(b.get('experience_id') or '').strip()
    action = str(b.get('action') or 'approve').strip()
    comment = str(b.get('comment') or '').strip()
    return client.review_candidate(exp_id, action=action, comment=comment)

def api_experience_revoke(b, token=None):
    b = b or {}
    client = _get_console_hub_client(token=token)
    exp_id = str(b.get('experience_id') or '').strip()
    reason = str(b.get('reason') or '').strip()
    return client.revoke_rule(exp_id, reason=reason)

class Handler(BaseHTTPRequestHandler):
    server_version='HerdrFactoryConsole/1.2'
    def log_message(self,fmt,*args):print(f'[{datetime.now().isoformat(timespec="seconds")}] '+(fmt%args),flush=True)
    def get_auth_token(self):
        auth = self.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            return auth[7:].strip()
        header_tok = self.headers.get('X-HAFlow-Token')
        if header_tok:
            return header_tok.strip()
        from herdr.state_db import get_user_token
        return get_user_token('local-console-reviewer', 'experience_hub')
    def send_json(self,status,data=None,error=None):
        p={'ok':error is None};
        if data is not None:p['data']=data
        if error is not None:p['error']=str(error)
        raw=json.dumps(p,ensure_ascii=False).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(raw))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(raw)
    def send_bytes(self,status,body,headers):
        payload=body if isinstance(body,(bytes,bytearray)) else bytes(body or b'')
        self.send_response(status)
        for key,value in (headers or {}).items():
            self.send_header(key,value)
        self.send_header('Content-Length',str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def send_html(self,text):
        raw=text.encode(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(raw))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(raw)
    def send_static(self,rel):
        base=common.CONSOLE_STATIC_DIR.resolve()
        target=(base/rel).resolve()
        if base not in target.parents and target!=base:
            return self.send_json(403,error='Forbidden')
        if not target.is_file():
            return self.send_json(404,error='Not Found')
        suffix=target.suffix.lower()
        ctype='text/html; charset=utf-8' if suffix in ('.html', '.htm') else 'application/javascript; charset=utf-8' if suffix=='.js' else 'text/css; charset=utf-8' if suffix=='.css' else 'application/octet-stream'
        raw=target.read_bytes(); self.send_response(200); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(raw))); self.send_header('Cache-Control','no-cache' if target.name.startswith('simple.') or target.name=='console-tokens.css' else 'public, max-age=3600'); self.end_headers(); self.wfile.write(raw)
    def reject_untrusted(self,method):
        reason=common.console_request_rejection(method,self.headers,self.server.server_port)
        if reason:
            self.log_message('%s %s rejected: %s',method,self.path,reason)
            self.send_json(403,error=reason)
        return bool(reason)
    def query(self):return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    def body(self):
        n=int(self.headers.get('Content-Length','0') or 0); raw=self.rfile.read(n) if n else b'{}'; return json.loads(raw.decode() or '{}')
    def do_GET(self):
        if self.reject_untrusted('GET'):return
        p=urllib.parse.urlparse(self.path).path
        try:
            if p=='/':return self.send_html(HTML)
            if p in {'/simple','/simple/'}:return self.send_html((common.CONSOLE_STATIC_DIR/'simple.html').read_text(encoding='utf-8').replace('__PRODUCT_NAME__',common.PRODUCT_NAME).replace('__PRODUCT_TAGLINE__',common.PRODUCT_TAGLINE))
            if p=='/api/projects':return self.send_json(200,inventory.projects())
            if p.startswith('/static/'):return self.send_static(p[len('/static/'):])
            if p=='/dashboard':
                self.send_response(302);self.send_header('Location','/?view=dashboard');self.end_headers();return
            if p=='/api/dashboard':return self.send_json(200,read_api.dashboard_data(workflow_id=self.query().get('workflow_id',[''])[0] or None))
            if p=='/api/overview':return self.send_json(200,runtime.overview())
            if p=='/api/ops-center':return self.send_json(200,inventory.ops_center(self.query().get('workflow_id',[''])[0] or None,self.query().get('include_tasks',[''])[0]=='1'))
            if p=='/api/templates':return self.send_json(200,workflow_api.templates_summary())
            if p=='/api/template':return self.send_json(200,workflow_api.template_detail(self.query().get('id',[''])[0]))
            if p=='/api/run/status':return self.send_json(200,workflow_api.workflow_job_status(self.query().get('id',[''])[0]))
            if p=='/api/project':return self.send_json(200,read_api.project_detail(self.query().get('id',[''])[0], lightweight=self.query().get('lightweight',[''])[0]=='1'))
            if p=='/api/project/runtime':return self.send_json(200,read_api.project_runtime(self.query().get('id',[''])[0]))
            if p=='/api/workflow':return self.send_json(200,read_api.workflow_detail(self.query().get('id',[''])[0], lightweight=self.query().get('lightweight',[''])[0]=='1'))
            if p=='/api/workflow/runtime':
                from herdr.recovery_store import read_snapshot
                wid=self.query().get('id',[''])[0]
                w,_,_=read_snapshot(state_db_path(),wid)
                return self.send_json(200,{'workflow_id':wid,'coordinator':runtime.agent_runtime(w.get('coordinator_pane_id'))})
            if p=='/api/workflow/deliverables':
                wid=self.query().get('workflow_id',[''])[0] or self.query().get('id',[''])[0]
                return self.send_json(200,read_api.api_workflow_deliverables(wid))
            if p=='/api/workflow/deliverable':
                q=self.query()
                wid=q.get('workflow_id',[''])[0]
                tid=q.get('task_id',[''])[0]
                did=q.get('id',[''])[0] or q.get('deliverable_id',[''])[0]
                download=q.get('download',[''])[0] in {'1','true','yes'}
                result=read_api.api_read_deliverable(wid,tid,did,download=download)
                if download:
                    return self.send_bytes(200,result.get('body') or b'',result.get('headers') or {})
                return self.send_json(200,{k:v for k,v in result.items() if k!='body'})
            if p=='/api/workflows':return self.send_json(200,read_api.api_workflows(self.query().get('project_id',[''])[0] or None))
            if p=='/api/task':return self.send_json(200,read_api.task_detail(self.query().get('id',[''])[0]))
            if p=='/api/archive':
                qp=self.query()
                def _q(name,default=''):return qp.get(name,[default])[0]
                def _qi(name,default):
                    try:return int(_q(name,default))
                    except Exception:return default
                return self.send_json(200,read_api.archive_query(_q('project_id'),_q('workflow_id'),_q('agent'),_q('status','archived'),_q('q'),_qi('limit',50),_qi('offset',0)))
            if p=='/api/pane/read':return self.send_json(200,{'output':read_api.read_pane(self.query().get('id',[''])[0])})
            if p=='/api/deep-preflight':
                x=inventory.project_by_id(self.query().get('id',[''])[0])
                if not x:
                    raise RuntimeError('项目不存在')
                agent=self.query().get('agent',[None])[0]
                return self.send_json(200,runtime.deep_preflight(x,agent=agent))
            if p=='/api/preflight':
                x=inventory.project_by_id(self.query().get('id',[''])[0]);
                if not x:raise RuntimeError('项目不存在')
                return self.send_json(200,runtime.preflight(x))
            if p=='/api/logs':
                q=self.query()
                kind=q.get('kind',['controller'])[0]
                try:
                    n_lines=int(q.get('n',['200'])[0])
                except (ValueError,TypeError):
                    n_lines=200
                return self.send_json(200,{'output':common.tail_log(kind,n=n_lines)})
            if p=='/api/kernel/checkpoints':
                wid=self.query().get('workflow_id',[''])[0]
                if not wid:raise RuntimeError('workflow_id 不能为空')
                return self.send_json(200,control_api.api_kernel_checkpoint_list(wid))
            if p=='/api/task/steer/queue':
                tid=self.query().get('task_id',[''])[0]
                return self.send_json(200,task_api.api_task_steer_queue(tid))
            if p=='/api/agent/adapters':
                return self.send_json(200,task_api.api_agent_adapters())
            if p=='/api/task/trajectory':
                q=self.query()
                try:
                    return self.send_json(200, read_api.api_task_trajectory(
                        q.get('id',[''])[0] or q.get('task_id',[''])[0],
                        q.get('limit',['100'])[0],
                        q.get('cursor',[''])[0] or None,
                    ))
                except read_api.TraceQueryError as e:
                    return self.send_json(e.status, error=e.message)
            if p=='/api/task/projection':
                tid=self.query().get('id',[''])[0] or self.query().get('task_id',[''])[0]
                if not tid:raise RuntimeError('task_id 不能为空')
                return self.send_json(200,read_api.api_task_projection(tid))
            if p=='/api/workflow/projection':
                wid=self.query().get('id',[''])[0] or self.query().get('workflow_id',[''])[0]
                if not wid:raise RuntimeError('workflow_id 不能为空')
                return self.send_json(200,read_api.api_workflow_projection(wid))
            if p=='/api/workflow/controller-actions':
                wid=self.query().get('workflow_id',[''])[0] or self.query().get('id',[''])[0]
                return self.send_json(200,control_api.api_workflow_controller_actions(wid))
            if p=='/api/workflow/recovery':
                return self.send_json(200,workflow_api.api_workflow_recovery(self.query().get('workflow_id',[''])[0]))
            if p=='/api/workflow/decisions':
                wid=self.query().get('workflow_id',[''])[0] or self.query().get('id',[''])[0]
                return self.send_json(200,control_api.api_workflow_decisions(wid))
            if p=='/api/task/interaction':
                q=self.query()
                tid=q.get('task_id',[''])[0] or q.get('id',[''])[0]
                wid=q.get('workflow_id',[''])[0]
                return self.send_json(200,task_api.api_task_interaction_get(tid=tid,wid=wid))
            if p=='/api/experience/candidates':
                res = api_experience_candidates(self.query(), token=self.get_auth_token())
                if not res.get('ok') and 'error' in res:
                    return self.send_json(_hub_error_status(res), error=res['error'])
                return self.send_json(200, res)
            if p=='/api/experience/candidate':
                res = api_experience_candidate(self.query().get('id',[''])[0], token=self.get_auth_token())
                if not res.get('ok') and 'error' in res:
                    return self.send_json(_hub_error_status(res), error=res['error'])
                return self.send_json(200, res)
            if p=='/api/experience/rules':
                res = api_experience_rules(self.query(), token=self.get_auth_token())
                if not res.get('ok') and 'error' in res:
                    return self.send_json(_hub_error_status(res), error=res['error'])
                return self.send_json(200, res)
            if p=='/api/experience/token':
                q = self.query()
                uid = q.get('user', ['local-console-reviewer'])[0] or 'local-console-reviewer'
                from herdr.state_db import get_user_token
                tok = get_user_token(uid, 'experience_hub')
                return self.send_json(200, {'ok': True, 'has_token': bool(tok)})
            return self.send_json(404,error='Not Found')
        except herdr_deliverables.DeliverableError as e:
            return self.send_json(e.status,error=e.message)
        except Exception as e:
            self.log_message('GET %s failed: %s', self.path, e)
            return self.send_json(500,error=e)
    def do_POST(self):
        if self.reject_untrusted('POST'):return
        p=urllib.parse.urlparse(self.path).path
        try:
            b=self.body()
            if p=='/api/run':
                if not str(b.get('project_root') or '').strip():raise RuntimeError('项目目录不能为空')
                return self.send_json(202,workflow_api.start_workflow_job(str(Path(b.get('project_root','')).expanduser().resolve()),str(b.get('requirement','')).strip(),str(b.get('agent') or 'auto'),str(b.get('template') or 'software-development-v1'),str(b.get('title') or '').strip(),context=b.get('context')))
            if p=='/api/project/create':
                root=str(Path(b.get('project_root','')).expanduser().resolve())
                name=str(b.get('project_name') or '').strip() or None
                if not str(b.get('project_root') or '').strip():raise RuntimeError('项目目录不能为空')
                tmpl=str(b.get('template') or 'software-development-v1').strip()
                # Initialize the selected state store on this write path, never on GET.
                from herdr.state_store import get_state_store
                get_state_store(state_db_path())
                project=herdr_projects.create_project(root,project_name=name,template_name=tmpl)
                return self.send_json(200,project)
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
            if p=='/api/template':return self.send_json(200,workflow_api.save_template(str(b.get('name') or ''),str(b.get('yaml') or '')))
            if p=='/api/workflow/rename':return self.send_json(200,workflow_api.update_workflow_title(str(b['workflow_id']),str(b.get('title') or '')))
            if p=='/api/workflow/agent':return self.send_json(200,workflow_api.set_agent_override(str(b['workflow_id']),str(b.get('agent') or 'auto')))
            if p=='/api/workflow/candidate':return self.send_json(200,workflow_api.create_candidate(str(b['workflow_id'])))
            if p=='/api/workflow/advance':return self.send_json(200,workflow_api.manual_advance(str(b['workflow_id'])))
            if p=='/api/task/force-review':return self.send_json(200,task_api.api_task_force_review(b))
            if p=='/api/workflow/retry-advance':return self.send_json(200,control_api.api_workflow_retry_advance(b))
            if p=='/api/task/coordinator':return self.send_json(200,workflow_api.ask_coordinator(str(b['task_id'])))
            if p=='/api/task/steer':return self.send_json(200,task_api.api_task_steer(b))
            if p=='/api/task/halt':return self.send_json(200,task_api.api_task_halt(b))
            if p=='/api/task/signoff':return self.send_json(200,task_api.api_task_signoff(b))
            if p=='/api/slot/bind':return self.send_json(200,workflow_api.bind_slot(str(b['pane_id']),str(b.get('agent') or 'auto')))
            if p=='/api/kernel/pause':return self.send_json(200,control_api.api_kernel_pause(b))
            if p=='/api/kernel/resume':return self.send_json(200,control_api.api_kernel_resume(b))
            if p=='/api/kernel/step':return self.send_json(200,control_api.api_kernel_step(b))
            if p=='/api/kernel/rollback':return self.send_json(200,control_api.api_kernel_rollback(b))
            if p=='/api/kernel/force-pass':return self.send_json(200,control_api.api_kernel_force_pass(b))
            if p=='/api/kernel/checkpoint':return self.send_json(200,control_api.api_kernel_checkpoint_create(b))
            if p=='/api/kernel/checkpoint/restore':return self.send_json(200,control_api.api_kernel_checkpoint_restore(b))
            if p=='/api/controller/execute-action':return self.send_json(200,control_api.api_controller_execute_action(b))
            if p=='/api/workflow/decision/raise':return self.send_json(200,control_api.api_workflow_decide_raise(b))
            if p=='/api/workflow/decision':return self.send_json(200,control_api.api_workflow_decide_resolve(b))
            if p=='/api/workflow/recovery/decision':return self.send_json(200,workflow_api.api_workflow_recovery_decide(b))
            if p=='/api/task/interaction/respond':return self.send_json(200,task_api.api_task_interaction_respond(b))
            if p=='/api/experience/review':
                res = api_experience_review(b, token=self.get_auth_token())
                if not res.get('ok') and 'error' in res:
                    return self.send_json(400, error=res.get('error'))
                return self.send_json(200, res)
            if p=='/api/experience/revoke':
                res = api_experience_revoke(b, token=self.get_auth_token())
                if not res.get('ok') and 'error' in res:
                    return self.send_json(400, error=res.get('error'))
                return self.send_json(200, res)
            if p=='/api/experience/token':
                tok = str(b.get('token', '')).strip()
                uid = str(b.get('user', 'local-console-reviewer')).strip() or 'local-console-reviewer'
                from herdr.state_db import save_user_token
                save_user_token(uid, 'experience_hub', tok)
                return self.send_json(200, {'ok': True, 'saved': bool(tok)})
            return self.send_json(404,error='Not Found')
        except Exception as e:
            self.log_message('POST %s failed: %s', self.path, e)
            return self.send_json(500,error=e)
