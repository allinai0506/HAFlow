import time
from herdr import human_decisions as herdr_human_decisions
from herdr import kernel as herdr_kernel
from herdr import controller_actions as herdr_controller_actions
from herdr import workflow_docs as herdr_workflow_docs
from console import common, inventory, task_api, workflow_api, workflow_policy

def api_kernel_pause(b):
    wid=str(b.get('workflow_id') or '').strip()
    if not wid:raise RuntimeError('workflow_id 不能为空')
    nid=str(b.get('node_id') or '').strip() or None
    return herdr_kernel.pause_workflow(wid,node_id=nid,expected_identity=b.get('expected_identity'))

def api_kernel_resume(b):
    wid=str(b.get('workflow_id') or '').strip()
    if not wid:raise RuntimeError('workflow_id 不能为空')
    nid=str(b.get('node_id') or '').strip() or None
    return herdr_kernel.resume_workflow(wid,node_id=nid,expected_identity=b.get('expected_identity'))

def api_kernel_step(b):
    wid=str(b.get('workflow_id') or '').strip()
    if not wid:raise RuntimeError('workflow_id 不能为空')
    return herdr_kernel.step_workflow(wid, expected_identity=b.get('expected_identity'))

def api_kernel_rollback(b):
    wid=str(b.get('workflow_id') or '').strip()
    target=str(b.get('target_node_id') or '').strip()
    if not wid:raise RuntimeError('workflow_id 不能为空')
    if not target:raise RuntimeError('target_node_id 不能为空')
    reason=str(b.get('reason') or 'manual_rollback').strip()
    return herdr_kernel.rollback_workflow(wid,target_node_id=target,reason=reason,expected_identity=b.get('expected_identity'))



def api_kernel_force_pass(b):
    b = b or {}
    wid = str(b.get('workflow_id') or '').strip()
    if not wid: raise RuntimeError('workflow_id 不能为空')
    wf = inventory.workflows().get(wid)
    if not wf:
        try:
            wf = herdr_kernel._get_store().get_workflow(wid)
        except Exception:
            wf = None
    if not wf and not inventory.tasks_for_workflow(wid):
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

    gate, reason, op = workflow_policy._validate_force_pass_params(b, wid, wf, task=task)

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
    return herdr_kernel.create_checkpoint(wid,tag=tag,expected_identity=b.get('expected_identity'))

def api_kernel_checkpoint_list(wid):
    return herdr_kernel.list_checkpoints(wid)

def api_kernel_checkpoint_restore(b):
    wid=str(b.get('workflow_id') or '').strip()
    cpid=str(b.get('checkpoint_id') or '').strip()
    if not wid:raise RuntimeError('workflow_id 不能为空')
    if not cpid:raise RuntimeError('checkpoint_id 不能为空')
    return herdr_kernel.restore_checkpoint(wid,checkpoint_id=cpid,expected_identity=b.get('expected_identity'))

def api_workflow_retry_advance(b):
    wid=str(b.get('workflow_id') or '').strip()
    if not wid:raise RuntimeError('workflow_id 不能为空')
    return workflow_api.manual_advance(wid)

def api_workflow_controller_actions(wid):
    """Blocker + pipeline actions for a workflow.

    A healthy task that still owes the git delivery pipeline its next step
    (commit / integrate / finalize) or a live pane a real re-drive is an
    actionable item too — otherwise the console shows "no blockers" while
    delivery is actually waiting on a human click.
    """
    wid = str(wid or '').strip()
    if not wid: raise RuntimeError('workflow_id 不能为空')
    from services.controller.cockpit_automation import read_cockpit
    # Preserve legacy obligation discovery, then read all facts coherently.
    workflow_api.api_workflow_recovery(wid)
    cockpit = read_cockpit(herdr_kernel._get_store(), wid, controller={'status': 'unknown'})
    operations = cockpit['recovery']
    managed = {tid for op in operations if op['status'] not in {'resolved','superseded'}
               for tid in op['payload'].get('task_ids') or []}
    actions_list = cockpit['actions']
    actions_list = [a for a in actions_list if a.get('blocker_task_id') not in managed]
    return {
        'workflow_id': wid,
        'blockers': cockpit['blockers'],
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
        (t for t in inventory.tasks_for_workflow(wid) if t.get('task_id') == task_id), None,
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
    r = common.run(argv, timeout, check=True)
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
        argv = [str(common.HERDR_TASK)]
        for part in command:
            if part is not None and str(part).startswith('-'):
                if argv[-1] == str(common.HERDR_TASK) or argv[-1].startswith('-'):
                    raise RuntimeError(f'交付链路命令缺少子命令参数: {command}')
            argv.append(str(task_id) if part is None else str(part))
        result = common.run(argv, timeout, check=True)
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

def _launch_experience_inherit(payload, old_task):
    """CLI flags carrying enterprise experience policy for a launch.

    Direct payload fields win (fix-loop style: policy rides the action);
    otherwise fall back to the superseded record (replacement style).
    Absent policy yields no flags.
    """
    payload = payload or {}
    old_task = old_task or {}

    def _pick(key):
        v = str(payload.get(key) or "").strip()
        if not v:
            v = str(old_task.get(key) or "").strip()
        return v

    flags = []
    customer = _pick("customer")
    if customer:
        flags += ["--customer", customer]
    business_type = _pick("business_task_type")
    if business_type:
        flags += ["--business-task-type", business_type]
    if payload.get("mandatory_experience_rules") or old_task.get("mandatory_experience_rules"):
        flags += ["--mandatory-experience-rules"]
    return flags

def resolve_controller_action(workflow_id, action_id, *, snapshot=None):
    """Look up only the exact current server-authored Action."""
    from console.controller_cockpit_api import get_controller_cockpit
    cockpit = snapshot if snapshot is not None else get_controller_cockpit(workflow_id)
    return next((action for action in cockpit['actions'] if action['action_id'] == action_id), None)


def api_controller_execute_action(payload):
    payload = payload or {}
    from console.controller_cockpit_api import execute_controller_action
    if (payload or {}).get('action_id'):
        return execute_controller_action(payload)
    # Historical callers retain their result/exception contract; receipts add
    # authoritative outcome facts without changing transport success semantics.
    from herdr.controller_receipts import classify_receipt
    from services.controller.cockpit_automation import (read_cockpit, persist_receipt,
        read_action_state, pending_action_receipt, checkpoint_result, _unknown_cockpit)
    store = herdr_kernel._get_store()
    wid = str((payload or {}).get('workflow_id') or '').strip()
    try:
        before = read_cockpit(store, wid, controller={'status': 'running'}) if wid else None
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    aid = str(payload.get('task_id') or wid) + ':' + str(payload.get('type') or payload.get('action_type') or '')
    action = {'action_id': aid, 'target': {'workflow_id': wid, 'task_id': payload.get('task_id')}, 'api_payload': payload}
    if not wid:
        return _execute_legacy_controller_action(payload)  # Preserve historical missing-id validation.
    initial = read_action_state(store, action)
    task = initial['action_readback']['task']
    action['target'].update({key: task[key] for key in ('run_id', 'execution_id', 'candidate_sha') if task.get(key)})
    pending = pending_action_receipt(action, before, payload, initial)
    from uuid import uuid4
    key = 'legacy:' + uuid4().hex
    try:
        _execute_legacy_controller_action(payload, validate_only=True)
    except Exception as exc:
        if wid:
            receipt = {'status': 'rejected', 'action_id': aid, 'workflow_id': wid, 'target': action['target'],
                       'outcome': 'rejected' if isinstance(exc, RuntimeError) else 'failed',
                       'reason_code': 'legacy_action_rejected', 'refresh_required': True}
            persist_receipt(store, receipt, key=key)
            exc.receipt = receipt
        raise
    persist_receipt(store, pending, key=key)
    try:
        result = _execute_legacy_controller_action(payload)
    except Exception as exc:
        receipt = {**pending, 'outcome': 'unknown', 'reason_code': 'effect_unknown', 'refresh_required': True}
        persist_receipt(store, receipt, key=key)
        exc.receipt = receipt
        raise
    pending = checkpoint_result(pending, result)
    persist_receipt(store, pending, key=key)
    context = pending['reconciliation']
    try:
        after = read_cockpit(store, wid, controller={'status': 'running'})
        after['action_readback'] = read_action_state(store, action, result, since=context['started_at'],
            intent_digest=context['intent_digest'], event_watermark=context['event_watermark'])['action_readback']
        receipt = classify_receipt(action, before, after, result)
        if receipt.get('reason_code') in {'effect_unconfirmed', 'readback_identity_changed'}:
            receipt = {**pending, 'reason_code': receipt['reason_code'], 'refresh_required': True}
    except Exception:
        receipt = {**pending, 'reason_code': 'readback_unavailable', 'refresh_required': True}
        receipt, _ = persist_receipt(store, receipt, key=key)
        return {**result, 'receipt': receipt, 'cockpit': _unknown_cockpit(wid, receipt)}
    receipt, _ = persist_receipt(store, receipt, key=key)
    return {**result, 'receipt': receipt, 'cockpit': after}


def _execute_legacy_controller_action(payload, *, validate_only=False):
    payload = payload or {}
    act_type = str(payload.get('type') or payload.get('action_type') or '').strip()
    wid = str(payload.get('workflow_id') or '').strip()
    if not wid: raise RuntimeError('workflow_id 不能为空')
    if act_type in {'launch','force_pass_advance','rework'}:
        # Discover canonical task facts before accepting a stale cockpit action.
        inventory.tasks_for_workflow(wid)
        if any(op['status'] not in {'resolved','superseded'}
               for op in workflow_api.api_workflow_recovery(wid)['operations']):
            raise RuntimeError('该工作流存在持久恢复义务，请通过恢复待办裁决，禁止重复派发或绕过门禁')

    # Re-read authoritative workflow
    wf = inventory.workflows().get(wid)
    if not wf:
        try:
            wf = herdr_kernel._get_store().get_workflow(wid)
        except Exception:
            wf = None
    if not wf and not inventory.tasks_for_workflow(wid):
        raise RuntimeError(f'未找到工作流 {wid}')

    # Validate task attribution, node, version, and running instance when task_id is present
    task_id = str(payload.get('task_id') or '').strip()
    task = None
    if task_id and act_type != 'launch':
        all_t = inventory.tasks()
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

        req_node = workflow_policy._resolve_target_node(payload)
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

    if validate_only:
        return None

    if act_type == 'launch':
        stage = str(payload.get('stage') or 'implementation').strip()
        agent = str(payload.get('agent') or 'auto').strip()
        supersedes = str(payload.get('supersedes') or '').strip()
        prompt = str(payload.get('prompt') or f'执行 {stage} 阶段任务').strip()
        goal = str(payload.get('goal') or prompt).strip()
        if not task_id:
            task_id = f'{wid}-{stage}-{int(time.time())}'
        p = inventory.project_for_workflow(wid) or {}
        proj_root = str(payload.get('source') or p.get('project_root') or (wf and wf.get('project_root')) or '.').strip()

        cmd = [
            str(common.HERDR_TASK), 'launch',
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
        # Inherit enterprise experience policy: direct payload fields first
        # (fix-loop actions carry policy without supersedes), then the
        # superseded record, so replacement launches never drop protection.
        try:
            old_task = herdr_kernel._get_store().get_task(supersedes) if supersedes else None
        except Exception:
            old_task = None
        cmd += _launch_experience_inherit(payload, old_task)
        if payload.get('ack_overflow') is True: cmd.append('--ack-overflow')
        r = common.run(cmd, timeout=30, check=True)
        return {'ok': True, 'task_id': task_id, 'output': r.stdout.strip()}

    elif act_type in ('force_pass', 'force_pass_advance'):
        gate, reason, op = workflow_policy._validate_force_pass_params(payload, wid, wf, task=task)
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
                adv_res = workflow_api.manual_advance(wid)
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
        return workflow_api.manual_advance(wid)

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
        argv = [str(common.HERDR_TASK), 'close-workflow', wid]
        if payload.get('accept_escalated'):
            argv.append('--accept-escalated')
        r = common.run(argv, 900, check=True)
        return {'ok': True, 'task_id': task_id, 'workflow_id': wid,
                'output': r.stdout.strip()[:2000]}

    elif act_type == 'steer':
        return task_api.api_task_steer(payload)

    elif act_type == 'halt':
        return task_api.api_task_halt(payload)

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


ACTION_BINARIES = {
    'herdr-task': lambda: str(common.HERDR_TASK),
    'herdr': lambda: 'herdr',
}
