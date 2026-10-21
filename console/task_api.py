import time
from herdr import kernel as herdr_kernel
from herdr import steering as herdr_steering
from herdr import interaction_loop as herdr_interaction_loop
from herdr.transitions import InvalidTransitionError
from console import common, inventory, read_api, runtime, workflow_policy

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

def _check_task_interaction(t, store=None):
    if not t:
        return None
    tid = t.get('task_id')
    pid = t.get('pane_id')
    if not pid:
        return {'detected': False, 'task_id': tid, 'reason': 'no_pane'}
    rt = runtime.agent_runtime(pid)
    if not rt:
        return {'detected': False, 'task_id': tid, 'pane_id': pid, 'reason': 'runtime_unavailable'}
    agent_status = rt.get('agent_status')
    agent_name = rt.get('agent') or t.get('agent') or 'auto'
    try:
        content = read_api.read_pane(pid)
    except Exception as e:
        return {'detected': False, 'task_id': tid, 'pane_id': pid, 'reason': f'read_pane_failed: {e}'}

    det = herdr_interaction_loop.detect_agent_interaction(content, agent_name=agent_name)
    fp = herdr_interaction_loop.compute_prompt_fingerprint(content) if content else ''

    already_responded = False
    recent_event = None
    if store and fp:
        try:
            evs = store.list_events(task_id=tid, limit=20)
            for ev in evs:
                p = ev.get('payload') or {}
                if p.get('prompt_fingerprint') == fp:
                    already_responded = True
                    recent_event = ev.get('event_type')
                    break
        except Exception:
            pass

    return {
        'detected': bool(det.get('detected')),
        'unrecognized': bool(det.get('unrecognized')),
        'task_id': tid,
        'workflow_id': t.get('workflow_id'),
        'task_title': t.get('title') or t.get('goal') or tid,
        'pane_id': pid,
        'agent': agent_name,
        'agent_status': agent_status,
        'terminal_id': rt.get('terminal_id'),
        'revision': rt.get('revision'),
        'prompt_fingerprint': fp,
        'interaction_type': det.get('interaction_type', ''),
        'raw_prompt': det.get('raw_prompt', ''),
        'options': [
            {'option_id': opt['option_id'], 'label': opt['label']}
            for opt in det.get('options', [])
        ],
        'already_responded': already_responded,
        'recent_event': recent_event,
        'task_version': t.get('version'),
    }

def api_task_interaction_get(tid='', wid=''):
    store = herdr_kernel.get_state_store() if hasattr(herdr_kernel, 'get_state_store') else None
    all_ts = inventory.tasks()
    if tid:
        t = next((x for x in all_ts if x.get('task_id') == tid), None)
        if not t and store:
            try:
                t = store.get_task(tid)
            except Exception:
                pass
        if not t:
            raise RuntimeError(f'任务 {tid} 不存在')
        res = _check_task_interaction(t, store)
        return {'ok': True, 'task_id': tid, 'interaction': res}

    target_tasks = [
        t for t in all_ts
        if (not wid or t.get('workflow_id') == wid)
        and t.get('status') in {'working', 'blocked', 'dispatched', 'paused', 'rework'}
    ]
    interactions = []
    for t in target_tasks:
        res = _check_task_interaction(t, store)
        if res and (res.get('detected') or (res.get('agent_status') == 'blocked' and res.get('raw_prompt'))):
            interactions.append(res)
    return {'ok': True, 'interactions': interactions}

def api_task_interaction_respond(b):
    b = b or {}
    tid = str(b.get('task_id') or '').strip()
    opt_id = str(b.get('option_id') or '').strip()
    exp_fp = str(b.get('expected_fingerprint') or '').strip()
    if not tid:
        raise RuntimeError('task_id 不能为空')
    if not opt_id:
        raise RuntimeError('option_id 不能为空')
    if not exp_fp:
        raise RuntimeError('expected_fingerprint 不能为空')

    store = herdr_kernel.get_state_store() if hasattr(herdr_kernel, 'get_state_store') else None
    t = None
    if store:
        try:
            t = store.get_task(tid)
        except Exception:
            pass
    if not t:
        t = next((x for x in inventory.tasks() if x.get('task_id') == tid), None)
    if not t:
        raise RuntimeError(f'任务 {tid} 不存在')

    if t.get('status') in {'superseded', 'cleaned', 'completed', 'committed', 'integrated'}:
        raise RuntimeError('任务已结束或已作废，拒绝处理交互')
    if b.get('expected_task_version') is not None and t.get('version') is not None:
        if t.get('version') != b.get('expected_task_version'):
            raise RuntimeError('任务代次已变更，拒绝处理交互')

    pid = t.get('pane_id')
    if not pid:
        raise RuntimeError('任务未绑定活动工位')
    if b.get('expected_pane_id') and b.get('expected_pane_id') != pid:
        raise RuntimeError(f"工位发生漂移: 期望 {b.get('expected_pane_id')}，实际 {pid}")

    rt = runtime.agent_runtime(pid)
    if not rt:
        raise RuntimeError('无法连接工位执行者运行时')

    content = read_api.read_pane(pid)
    current_fp = herdr_interaction_loop.compute_prompt_fingerprint(content)

    stored_identity = {
        'pane_id': b.get('expected_pane_id') or pid,
        'agent': b.get('expected_agent') or rt.get('agent'),
        'terminal_id': b.get('expected_terminal_id') or rt.get('terminal_id'),
        'prompt_fingerprint': exp_fp,
    }
    current_identity = {
        'pane_id': pid,
        'agent': rt.get('agent'),
        'terminal_id': rt.get('terminal_id'),
        'prompt_fingerprint': current_fp,
    }
    valid, reason = herdr_interaction_loop.validate_interaction_identity(stored_identity, current_identity)
    if not valid:
        raise RuntimeError(f'交互上下文已失效: {reason}')

    det = herdr_interaction_loop.detect_agent_interaction(content, agent_name=rt.get('agent'))
    if not det.get('detected'):
        raise RuntimeError('当前终端中交互提示已不存在或已变化')

    key_seq = herdr_interaction_loop.resolve_option_key(opt_id, det.get('options', []))
    if not key_seq:
        raise RuntimeError(f'非法或已过期的选项标识: {opt_id}')

    wid = t.get('workflow_id')
    interaction_key = f'{tid}:{pid}:{exp_fp}'

    from herdr import state_db
    if store:
        res = state_db.record_event_if_absent(
            {
                'event_type': 'interaction_accepted',
                'workflow_id': wid,
                'task_id': tid,
                'agent_id': rt.get('agent'),
                'source': 'interaction_loop',
                'payload': {
                    'interaction_key': interaction_key,
                    'pane_id': pid,
                    'prompt_fingerprint': exp_fp,
                    'option_id': opt_id,
                    'timestamp': time.time(),
                }
            },
            identity_field='interaction_key',
            identity_value=interaction_key,
            db_path=store.db_path,
        )
        if res.get('status') == 'exists':
            raise RuntimeError('该交互已受理，请勿重复提交')
        elif res.get('status') == 'rejected':
            raise RuntimeError(f"记录交互状态被拒绝: {res.get('reason')}")

    sent = herdr_interaction_loop.send_interaction_response(pid, key_seq)
    if not sent:
        if store:
            herdr_interaction_loop.record_interaction_event(
                store, 'interaction_uncertain',
                workflow_id=wid, task_id=tid, pane_id=pid,
                agent=rt.get('agent'), prompt_fingerprint=exp_fp,
                option_id=opt_id, result='uncertain',
                detail='herdr send-keys returned non-zero or failed'
            )
        return {
            'ok': False,
            'status': 'failed_or_uncertain',
            'reason': '发送按键失败或超时，结果未知，已记录审计并禁止自动重试',
            'task_id': tid,
            'pane_id': pid,
        }

    if store:
        herdr_interaction_loop.record_interaction_event(
            store, 'interaction_sent',
            workflow_id=wid, task_id=tid, pane_id=pid,
            agent=rt.get('agent'), prompt_fingerprint=exp_fp,
            option_id=opt_id, result='sent'
        )

    time.sleep(0.4)
    new_rt = runtime.agent_runtime(pid)
    new_content = read_api.read_pane(pid)
    new_fp = herdr_interaction_loop.compute_prompt_fingerprint(new_content)

    if new_fp != exp_fp or (new_rt and new_rt.get('agent_status') in ('working', 'idle', 'done')):
        if store:
            herdr_interaction_loop.record_interaction_event(
                store, 'interaction_resolved',
                workflow_id=wid, task_id=tid, pane_id=pid,
                agent=rt.get('agent'), prompt_fingerprint=exp_fp,
                option_id=opt_id, result='resolved'
            )
        return {
            'ok': True,
            'status': 'resolved',
            'message': '用户选项已发送，原交互问题已解除，执行者正在继续运行',
            'task_id': tid,
            'pane_id': pid,
        }
    else:
        return {
            'ok': True,
            'status': 'waiting_confirmation',
            'message': '输入已发送，等待执行者状态更新',
            'task_id': tid,
            'pane_id': pid,
        }

def api_task_force_review(b):
    b = b or {}
    tid = str(b.get('task_id') or '').strip()
    if not tid:
        raise RuntimeError('task_id 不能为空')
    raw_op = b.get('operator')
    if raw_op is None and 'operator' not in b:
        raw_op = 'human_console'
    token = b.get('auth_token') or b.get('token') or b.get('operator_token')
    credentials_path = b.get('credentials_path') or b.get('operator_credentials')
    from herdr.transitions import validate_operator, InvalidOperatorError
    from herdr.operator_auth import authenticate_and_authorize_operator, OperatorAuthError
    try:
        op = validate_operator(raw_op)
        principal = authenticate_and_authorize_operator(
            operator_id=op,
            action="force_review",
            token=token,
            credentials_path=credentials_path,
        )
        op = principal.operator_id
    except InvalidOperatorError as exc:
        raise RuntimeError(f"无效的操作人标识: {exc}")
    except OperatorAuthError as exc:
        raise RuntimeError(f"操作人凭据或能力鉴权失败: {exc}")
    except ValueError as exc:
        raise RuntimeError(f"无效的操作人标识: {exc}")
    reason = str(b.get('reason') or 'console_force_review').strip()
    exp_ver_raw = b.get('expected_version')
    exp_ver = None
    if exp_ver_raw is not None:
        try:
            exp_ver = int(exp_ver_raw)
        except (ValueError, TypeError):
            raise RuntimeError(f"无效的 expected_version: {exp_ver_raw}")
    exp_status = b.get('expected_status')
    if exp_status is not None:
        exp_status = str(exp_status).strip()

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
        if not task:
            raise RuntimeError(f'未找到任务 {tid}')
        task['status'] = 'agent_done'
        task['updated_at'] = time.time()
        herdr_kernel.save_tasks_data(tdata)
    else:
        if hasattr(store, "compare_and_set_task_transition"):
            target_expected_version = exp_ver if exp_ver is not None else task.get('version')
            target_expected_status = exp_status if exp_status is not None else task.get('status')
            try:
                res = store.compare_and_set_task_transition(
                    task_id=tid,
                    to_status="agent_done",
                    reason=reason,
                    source="console-force-review",
                    intent="console-force-review",
                    metadata={"operator": op},
                    expected_status=target_expected_status,
                    expected_version=target_expected_version,
                )
            except (InvalidTransitionError, ValueError) as exc:
                raise RuntimeError(f"任务状态流转失败: {exc}")
            if not res.get("ok", True):
                raise RuntimeError(f"任务状态流转失败: {res.get('reason')}")
            task = store.get_task(tid) or task
            if hasattr(herdr_kernel, "sync_tasks_projection"):
                herdr_kernel.sync_tasks_projection(store=store)
        else:
            task['status'] = 'agent_done'
            task['updated_at'] = time.time()
            store.save_task(task)

    wid = task.get('workflow_id')
    notified = False
    if wid:
        wf = None
        if store:
            wf = store.get_workflow(wid)
        else:
            wdata = herdr_kernel.load_workflows_data()
            wf = wdata.get('workflows', {}).get(wid)
        if wf and wf.get('coordinator_pane_id'):
            c = wf['coordinator_pane_id']
            msg = f'''HERDR_TASK_FORCE_REVIEW\n\ntask_id: {tid}\nworkflow_id: {wid}\n\n总指挥已人工唤醒评审，请立即对工位产物进行复审验收。'''
            try:
                r = common.run(['herdr', 'agent', 'prompt', c, msg, '--wait', '--timeout', '10000'], 15)
                notified = (r.returncode == 0)
            except Exception:
                pass
    return {'task_id': tid, 'status': 'agent_done', 'notified': notified}

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
            for t in inventory.tasks():
                if t.get('task_id')==tid:
                    if not wid:wid=t.get('workflow_id')
                    if not node:node=t.get('node') or t.get('stage')
                    break
    if not wid:raise RuntimeError('workflow_id 不能为空')
    if not node:raise RuntimeError('node 不能为空')
    b.setdefault('workflow_id', wid)
    b.setdefault('node', node)

    if act=='approve':
        wf = inventory.workflows().get(wid)
        if not wf:
            try:
                wf = herdr_kernel._get_store().get_workflow(wid)
            except Exception:
                wf = None
        if not wf and not inventory.tasks_for_workflow(wid):
            raise RuntimeError(f'未找到工作流 {wid}')

        task = None
        if tid:
            store = herdr_kernel._get_store()
            task = store.get_task(tid) if hasattr(store, 'get_task') else None
            if not task:
                for t in inventory.tasks():
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

        gate, reason, op = workflow_policy._validate_force_pass_params(b, wid, wf, task=task)

        exp_ver = b.get('expected_version')
        exp_task_vers = b.get('expected_task_versions')
        exp_pane = str(b.get('expected_pane_id') or b.get('pane_id') or '').strip()
        token = b.get('auth_token') or b.get('token') or b.get('operator_token')
        credentials_path = b.get('credentials_path') or b.get('operator_credentials')

        res = herdr_kernel.force_pass_gate(
            wid,
            gate_node_id=gate,
            note=reason,
            operator=op or operator,
            expected_version=exp_ver,
            task_id=tid or None,
            expected_pane_id=exp_pane or None,
            expected_task_versions=exp_task_vers,
            token=token,
            credentials_path=credentials_path,
        )
        return {'ok': True, 'action': 'approve', 'result': res}
    elif act=='reject':
        reason=feedback or "人工在协同工作舱会签打回"
        target=retry_target or node
        res=herdr_kernel.rollback_workflow(wid,target_node_id=target,reason=reason)
        return {'ok':True,'action':'reject','rollback':res,'feedback':feedback,'target_node':target}
    else:
        raise RuntimeError(f"不支持的会签动作: {act}")
