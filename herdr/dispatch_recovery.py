"""Audited user recovery decisions over the existing dispatch responsibility."""
import json
import time
from pathlib import Path
from . import node_dispatch as core, node_dispatch_store as nd, recovery_store as rs


def _intents(conn, workflow, node_id):
    rows = conn.execute("""SELECT payload_json FROM events WHERE workflow_id=?
        AND event_type='launch_intent' AND source='launch'
        AND (node_id=? OR json_extract(payload_json,'$.node_id')=?)
        ORDER BY id DESC LIMIT 10001""", (workflow['workflow_id'], node_id, node_id)).fetchall()
    if len(rows) > 10000:
        raise ValueError('启动记录超过核查预算，请先归档历史记录')
    latest = {}
    for row in rows:
        item = json.loads(row['payload_json'])
        if item.get('execution_id') and item['execution_id'] != workflow.get('execution_id'):
            continue
        latest.setdefault(item.get('key') or item.get('intent_id'), item)
    return list(latest.values())


def _context(conn, op, snapshot=None):
    workflow, config, tasks = snapshot if snapshot is not None else nd._snapshot(conn, op['workflow_id'])
    inventory = [t for t in tasks if (t.get('node') or t.get('stage')) == op['payload']['node_id']
                 and (not t.get('execution_id') or t['execution_id'] == workflow.get('execution_id'))]
    intents = _intents(conn, workflow, op['payload']['node_id'])
    return workflow, config, tasks, inventory, intents


def _pending_intents(intents, inventory):
    tasks = {t['task_id']: t for t in inventory}
    pending = []
    for intent in intents:
        task = tasks.get(intent.get('task_id'))
        run_id = (intent.get('resources') or {}).get('run_id')
        closed_registration = (intent.get('phase') == 'registered' and task
            and task.get('launch_intent_id') == intent.get('intent_id')
            and run_id and run_id == task.get('run_id')
            and task.get('status') in {'superseded', 'cleaned', 'completed'})
        if intent.get('phase') != 'resources_absent' and not closed_registration:
            pending.append(intent)
    return pending


def _stale_partial_launch(conn, op, context):
    workflow, _, tasks, _, intents = context
    if op['detail'].get('reason') != 'dispatch_prior_delivery_unknown':
        return None
    prior_id = op['detail'].get('prior_operation_id')
    if not isinstance(prior_id, int) or prior_id == op['id']:
        return None
    try:
        prior = rs._get(conn, prior_id)
    except ValueError:
        return None
    if (prior['workflow_id'] != op['workflow_id'] or not prior['started']
            or prior['status'] != 'waiting_human'
            or prior['payload'].get('node_id') != op['payload'].get('node_id')
            or prior['payload'].get('candidate_sha') == workflow.get('candidate_sha')):
        return None
    task_ids = {t.get('task_id') for t in tasks}
    def eligible_phase(intent):
        if intent.get('phase') not in {'registered', 'resources_absent'}:
            return True
        if intent.get('phase') != 'resources_absent':
            return False
        receipt = intent.get('retirement') or {}
        resources = intent.get('resources') or {}
        return bool(receipt.get('archived_clone')
            and resources.get('retirement_archive') == receipt.get('archived_clone')
            and receipt.get('intent_id') == intent.get('intent_id')
            and receipt.get('task_id') == intent.get('task_id')
            and receipt.get('workflow_id') == workflow.get('workflow_id')
            and receipt.get('node_id') == op['payload'].get('node_id')
            and receipt.get('candidate_sha') == prior['payload'].get('candidate_sha')
            and receipt.get('pane_id') == (resources.get('pane_id') or '')
            and receipt.get('terminal_id') == (resources.get('terminal_id') or '')
            and len(str(receipt.get('transcript_sha256') or '')) == 64)
    candidates = [i for i in intents if i.get('dispatch_operation_id') == prior_id
        and i.get('candidate_sha') == prior['payload'].get('candidate_sha')
        and i.get('task_id') not in task_ids and eligible_phase(i)
        and (i.get('resources') or {}).get('planned_clone_path')
        and (i.get('resources') or {}).get('pane_id')
        and (i.get('resources') or {}).get('terminal_id')]
    if len(candidates) != 1:
        return None
    intent = candidates[0]
    return {'prior_operation_id': prior['id'], 'prior_version': prior['version'],
        'intent_id': intent['intent_id'], 'task_id': intent['task_id'],
        'pane_id': intent['resources']['pane_id'],
        'terminal_id': intent['resources']['terminal_id'],
        'prior_candidate_sha': prior['payload'].get('candidate_sha')}


def _view(conn, op, context):
    from . import scheduler
    workflow, config, tasks, inventory, intents = context
    pending_intents = _pending_intents(intents, inventory)
    blocked = None
    node = next((n for n in config.get('nodes') or [] if n.get('id') == op['payload']['node_id']), None)
    latest = nd._node_operation(conn, workflow, config, node) if node else None
    valid = core.active(workflow) and core.current(op, workflow, config) and latest and latest['id'] == op['id']
    if not valid:
        blocked = '工作流已暂停或候选、配置已变化，请刷新后查看当前恢复待办。'
    elif not workflow.get('execution_id') or not workflow.get('candidate_sha'):
        blocked = '当前执行或冻结候选身份不完整，尚不能授权补派。'
    elif any(not t.get('run_id') or t.get('execution_id') != workflow['execution_id']
             for dep in node.get('depends_on') or []
             for t in scheduler.node_tasks(tasks, workflow['workflow_id'], dep)
             if not t.get('execution_id') or t['execution_id'] == workflow['execution_id']):
        blocked = '上游任务执行身份不完整，请先核实上游任务的执行与Run归属。'
    elif not core.dependencies_ready(workflow, config,
            node, tasks):
        blocked = '上游依赖未满足，请先处理上游任务。'
    elif any(t.get('status') != 'superseded' and not t.get('superseded_by') for t in inventory):
        blocked = '节点已有登记任务，请查看任务详情，不能重复派发。'
    elif nd._prior_delivery_unknown(conn, op):
        blocked = '同节点旧派发仍未结案，请先核对旧恢复待办。'
    elif pending_intents:
        blocked = '存在未结案启动记录；先核查启动现场，不能直接重新派发。'
    cancelled = core.cancelled_inventory(inventory)
    heads = sorted((t for t in inventory if t.get('status') == 'superseded' and not t.get('superseded_by') and t.get('replacement_pending') is False), key=lambda t: t['task_id'])
    if cancelled and any(not t.get('run_id') for t in heads):
        blocked = '旧任务Run身份缺失，不能建立替代谱系；请先核实旧任务记录。'
    summary = '测试或验收范围已取消补派' if cancelled else '旧派发尚未确认任务登记' if op['started'] else '节点正在等待派发'
    actions = []
    if valid and op['status'] not in core.TERMINAL and op['status'] != 'running':
        if op['started'] and op['status'] in {'awaiting_result', 'waiting_human'}:
            actions.append({'action': 'verify', 'label': '核对现有任务'})
        if pending_intents:
            actions.append({'action': 'check_resources', 'label': '核查启动现场'})
        partial = _stale_partial_launch(conn, op, context)
        if partial:
            actions.append({'action': 'abandon_partial_launch', 'label': '归档并废弃旧代部分启动',
                            **partial})
        if not blocked:
            if cancelled:
                actions.append({'action': 'restore_scope', 'label': '恢复验收范围并重新派发'})
            elif op['started']:
                actions.append({'action': 'confirm_absent', 'label': '确认旧任务未运行并重新派发'})
        actions.append({'action': 'hold', 'label': '暂缓一小时'})
    return {'summary': summary, 'node_label': (node or {}).get('label') or op['payload']['node_id'],
            'explanation': '取消范围需要人工重新授权；未知交付需要核实旧任务，系统不会自动重复发送。',
            'next_step': blocked or '检查旧工位及任务列表，确认没有旧任务执行后，填写处理人与依据并授权恢复。',
            'candidate_sha': workflow.get('candidate_sha'), 'actions': actions,
            'resource_checks': op['detail'].get('resource_checks') or [],
            'lineage': [{'task_id': t['task_id'], 'run_id': t.get('run_id'), 'version': t['version'],
                         'requires_binding': not t.get('execution_id')} for t in heads]}


def list_operations(db_path, workflow_id):
    from .state_db import get_readonly_db_connection
    if not Path(db_path).is_file():
        return []
    conn = get_readonly_db_connection(db_path)
    try:
        conn.execute('BEGIN')
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='workflow_recovery_operations'").fetchone():
            return []
        operations = [rs._decode(row) for row in conn.execute(
            "SELECT * FROM workflow_recovery_operations WHERE workflow_id=? ORDER BY CASE WHEN status IN ('resolved','superseded') THEN 1 ELSE 0 END,next_due_at,id LIMIT 1001", (workflow_id,))]
        if len(operations) > 1000:
            raise ValueError('恢复记录超过展示预算')
        snapshot = nd._snapshot(conn, workflow_id) if any(o['payload'].get('kind') == 'node_dispatch' and o['status'] != 'superseded' for o in operations) else None
        contexts = {}
        for op in operations:
            if op['payload'].get('kind') == 'node_dispatch' and op['status'] != 'superseded':
                node_id = op['payload']['node_id']
                if node_id not in contexts:
                    contexts[node_id] = _context(conn, op, snapshot)
                if op['status'] == 'resolved':
                    workflow, config, tasks, _, _ = contexts[node_id]
                    registered = core.registered_tasks(op, workflow, tasks) if core.current(op, workflow, config) else []
                    if registered:
                        op['confirmation'] = {'task_ids': sorted(t['task_id'] for t in registered)}
                else:
                    op['recovery'] = _view(conn, op, contexts[node_id])
        return operations
    finally:
        conn.close()


def decide(db_path, workflow_id, operation_id, expected_version, operator, action, reason,
           *, confirmed_absent=False, confirmed_lineage=False, lineage_snapshot=None, candidate_sha=None, now=None):
    """Retire an old authorization and create a new epoch in one transaction."""
    from . import state_db
    now = time.time() if now is None else now
    if action not in {'restore_scope', 'confirm_absent'} or confirmed_absent is not True:
        raise ValueError('必须明确确认旧任务未执行并选择恢复范围或未交付确认')
    if not isinstance(operator, str) or not operator.strip() or len(operator) > 128 or not isinstance(reason, str) or not reason.strip() or len(reason.encode()) > 2048:
        raise ValueError('请填写处理人及有界处理依据')
    from .task_resources import workflow_launch_lock
    with workflow_launch_lock(db_path, workflow_id), rs._transaction(db_path) as conn:
        op = rs._get(conn, operation_id)
        if op['workflow_id'] != workflow_id or op['payload'].get('kind') != 'node_dispatch' or op['version'] != expected_version:
            raise ValueError('恢复记录或版本已变化，请刷新页面')
        context = _context(conn, op)
        workflow, config, tasks, inventory, _ = context
        if candidate_sha != workflow.get('candidate_sha'):
            raise ValueError('当前候选已变化，请刷新页面')
        choices = _view(conn, op, context)
        if action not in {a['action'] for a in choices['actions']}:
            raise ValueError(choices['next_step'])
        bindings = []
        if action == 'restore_scope':
            expected_lineage = [{k: t[k] for k in ('task_id', 'run_id', 'version')} for t in choices['lineage']]
            if lineage_snapshot != expected_lineage:
                raise ValueError('旧任务Run或版本已变化，请刷新并重新确认归属')
            if any(t['requires_binding'] for t in choices['lineage']) and confirmed_lineage is not True:
                raise ValueError('请明确确认旧Task/Run归属当前执行，不能猜测执行身份')
            for task in inventory:
                if task.get('status') == 'superseded' and not task.get('superseded_by') and task.get('replacement_pending') is False:
                    updated = {**task, 'replacement_pending': True}
                    if not task.get('execution_id'):
                        updated['execution_id'] = workflow['execution_id']
                        bindings.append(task['task_id'])
                    state_db.save_task(updated, conn=conn)
        _, _, tasks = nd._snapshot(conn, workflow_id)
        from .task_lineage import lineage_redispatch_candidates
        candidates = lineage_redispatch_candidates([t for t in tasks if (t.get('node') or t.get('stage')) == op['payload']['node_id']
            and (not t.get('execution_id') or t['execution_id'] == workflow.get('execution_id'))])
        predecessors = sorted(({'task_id': t['task_id'], 'run_id': t.get('run_id')} for t in candidates), key=lambda t: t['task_id']) or None
        receipt = {'operator': operator.strip(), 'reason': reason.strip(), 'action': action,
                   'confirmed_absent': True, 'candidate_sha': candidate_sha, 'at': now,
                   'bound_task_ids': bindings, 'lineage_snapshot': lineage_snapshot}
        nd._write(conn, op, 'superseded', {'human_decision': receipt,
            'reason': 'dispatch_human_reauthorized'}, now)
        node = next(n for n in config['nodes'] if n['id'] == op['payload']['node_id'])
        facts = core.payload(workflow, config, node, predecessors, op['id'])
        cur = conn.execute('''INSERT INTO workflow_recovery_operations
            (identity_key,workflow_id,payload_json,status,next_due_at,detail_json,created_at,updated_at)
            VALUES (?,?,?,'pending',?,?,?,?)''',
            (facts['identity_key'], workflow_id, json.dumps(facts), now,
             json.dumps({'human_decision': receipt, 'reason': 'dispatch_human_reauthorized'}), now, now))
        new = rs._get(conn, cur.lastrowid)
        rs._event(conn, new, 'human_reauthorized', now, receipt)
        return {'ok': True, 'operation': new}


def check_resources(db_path, workflow_id, operation_id, expected_version, operator, reason, *, candidate_sha=None, now=None):
    """Bounded native inventory; only actual absence can finish a launch receipt."""
    from . import task_resources
    from .state_db import get_readonly_db_connection
    from .state_store import get_state_store
    now = time.time() if now is None else now
    if not isinstance(operator, str) or not operator.strip() or len(operator) > 128 or not isinstance(reason, str) or not reason.strip() or len(reason.encode()) > 2048:
        raise ValueError('请填写处理人和核查依据')
    with task_resources.workflow_launch_lock(db_path, workflow_id):
        conn = get_readonly_db_connection(db_path)
        try:
            conn.execute('BEGIN')
            op = rs._get(conn, operation_id)
            if op['workflow_id'] != workflow_id or op['version'] != expected_version or op['payload'].get('kind') != 'node_dispatch':
                raise ValueError('恢复记录已变化，请刷新页面')
            context = _context(conn, op)
            if candidate_sha != context[0].get('candidate_sha'):
                raise ValueError('当前候选已变化，请刷新页面')
            view = _view(conn, op, context)
            if 'check_resources' not in {a['action'] for a in view['actions']}:
                raise ValueError('当前没有可核查的启动记录')
            _, _, _, inventory, intents = context
        finally:
            conn.close()
        pending = _pending_intents(intents, inventory)
        if len(pending) > 8:
            raise ValueError('未结案启动记录超过单次核查预算')
        store = get_state_store(db_path)
        deadline = time.monotonic() + 12
        reports = []
        for intent in pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError('现场核查超时，请刷新后重试')
            reports.append(task_resources.inventory_launch_resources(intent, timeout=remaining))
        checks = []
        with rs._transaction(db_path) as conn:
            current = rs._get(conn, operation_id)
            context = _context(conn, current)
            if candidate_sha != context[0].get('candidate_sha'):
                raise ValueError('核查期间候选已变化，请刷新页面')
            if current['version'] != expected_version or current['status'] in core.TERMINAL:
                raise ValueError('核查期间恢复记录已变化，请刷新页面')
            view = _view(conn, current, context)
            if 'check_resources' not in {a['action'] for a in view['actions']}:
                raise ValueError('核查期间恢复条件已变化，请刷新页面')
            current_pending = _pending_intents(context[4], context[3])
            if sorted(current_pending, key=lambda i:i['intent_id']) != sorted(pending, key=lambda i:i['intent_id']):
                raise ValueError('核查期间启动记录已变化，请刷新页面')
            for intent, report in zip(pending, reports):
                status = report.get('resource_status', 'unknown')
                if status == 'absent':
                    status = task_resources.reconcile_launch_intent(store, intent, lambda _: 'absent', now=now, conn=conn)['status']
                    if status == 'registered' and (intent.get('resources') or {}).get('run_id'):
                        task_resources.finish_launch_intent(store, intent, now=now, conn=conn)
                messages = {'resources_absent': '已确认没有启动资源', 'registered': '已找到登记任务，请处理已有任务',
                            'recovery_required': '资源或登记归属尚未确认，仍禁止重发',
                            'owned': '发现旧工作区或工位，需核实旧启动现场',
                            'foreign': '现场身份不属于旧启动，不能回收或重发',
                            'unknown': '无法完整确认旧工位或工作区，暂不能证明旧任务未执行'}
                if report.get('reason') == 'planned_resource_identity_missing':
                    messages['unknown'] = '旧启动记录缺少工位或工作区身份，无法证明旧任务未执行，不能重发。'
                checks.append({'task_id': intent.get('task_id'), 'status': status,
                               'message': messages.get(status, '现场核查未完成，仍禁止重发')})
            nd._write(conn, current, current['status'], {'resource_checks': checks}, now,
                      next_due=current['next_due_at'], owner=current['owner'], lease=current['lease_until'])
            from .state_db import record_event
            record_event({'event_type':'dispatch_resources_checked', 'payload':{'operation_id': operation_id,
                'operator': operator.strip(), 'reason': reason.strip(), 'checks': checks},
                'workflow_id':workflow_id, 'node_id':op['payload']['node_id'],
                'source':'console-recovery', 'timestamp':now}, conn=conn)
        return {'ok': True, 'checks': checks}


def abandon_partial_launch(db_path, workflow_id, operation_id, expected_version,
                           prior_operation_id, expected_prior_version, *, operator, reason,
                           candidate_sha, intent_id, task_id, pane_id, terminal_id,
                           transcript_sha256, confirmed_startup_only, now=None, runner=None):
    """Archive a stale generation's partial launch, then release current dispatch by CAS."""
    from . import task_resources, state_db
    now = time.time() if now is None else now
    with task_resources.workflow_launch_lock(db_path, workflow_id):
        conn = state_db.get_readonly_db_connection(db_path)
        try:
            conn.execute('BEGIN')
            op = rs._get(conn, operation_id)
            prior = rs._get(conn, prior_operation_id)
            context = _context(conn, op)
            workflow, _, tasks, _, intents = context
            partial = _stale_partial_launch(conn, op, context)
            if (op['version'] != expected_version or op['status'] != 'waiting_human'
                    or prior['version'] != expected_prior_version
                    or prior['status'] != 'waiting_human'
                    or prior['id'] != op['detail'].get('prior_operation_id')
                    or workflow_id != op['workflow_id']
                    or candidate_sha != workflow.get('candidate_sha')
                    or not partial or partial['intent_id'] != intent_id
                    or partial['task_id'] != task_id or partial['pane_id'] != pane_id
                    or partial['terminal_id'] != terminal_id):
                raise ValueError('恢复身份、候选或 operation 版本已变化')
            if any(t.get('task_id') == task_id for t in tasks):
                raise ValueError('旧启动已登记 Task，禁止废弃')
            intent = next(i for i in intents if i.get('intent_id') == intent_id)
            if prior['payload'].get('candidate_sha') == candidate_sha:
                raise ValueError('旧启动属于当前候选，不能作为 stale partial launch 处置')
        finally:
            conn.close()
        resource = task_resources.retire_partial_launch(
            __import__('herdr.state_store', fromlist=['get_state_store']).get_state_store(db_path), intent,
            expected_intent_id=intent_id, expected_task_id=task_id,
            expected_workflow_id=workflow_id, expected_node_id=op['payload']['node_id'],
            expected_candidate_sha=partial['prior_candidate_sha'],
            expected_terminal_id=terminal_id, expected_pane_id=pane_id,
            operator=operator, reason=reason, transcript_sha256=transcript_sha256,
            confirmed_startup_only=confirmed_startup_only, runner=runner, now=now)
        if resource.get('status') != 'resources_absent':
            raise ValueError('旧启动资源尚未完整归档，当前派发仍保持阻断')
        with rs._transaction(db_path) as conn:
            current = rs._get(conn, operation_id)
            old = rs._get(conn, prior_operation_id)
            context = _context(conn, current)
            workflow, _, tasks, _, latest_intents = context
            latest = next((item for item in latest_intents
                           if item.get('intent_id') == intent_id), None)
            retirement = (latest or {}).get('retirement') or {}
            resource_receipt = resource.get('retirement') or {}
            if (current['version'] != expected_version or current['status'] != 'waiting_human'
                    or old['version'] != expected_prior_version or old['status'] != 'waiting_human'
                    or old['id'] != current['detail'].get('prior_operation_id')
                    or candidate_sha != workflow.get('candidate_sha')
                    or any(t.get('task_id') == task_id for t in tasks)
                    or not latest or latest.get('phase') != 'resources_absent'
                    or latest.get('dispatch_operation_id') != prior_operation_id
                    or latest.get('task_id') != task_id
                    or latest.get('candidate_sha') != prior['payload'].get('candidate_sha')
                    or retirement.get('transcript_sha256') != transcript_sha256.lower()
                    or retirement.get('pane_id') != pane_id
                    or retirement.get('terminal_id') != terminal_id
                    or retirement.get('workflow_id') != workflow_id
                    or retirement.get('node_id') != current['payload']['node_id']
                    or retirement.get('candidate_sha') != prior['payload'].get('candidate_sha')
                    or retirement.get('task_id') != task_id
                    or retirement.get('intent_id') != intent_id
                    or retirement.get('archived_clone') != resource.get('archived_clone')
                    or resource_receipt.get('archived_clone') != resource.get('archived_clone')):
                raise ValueError('归档后 CAS 复核失败；保留归档，刷新恢复待办再重试')
            receipt = {'operator': operator.strip(), 'reason': reason.strip(),
                'action': 'abandon_partial_launch', 'candidate_sha': candidate_sha,
                'prior_candidate_sha': partial['prior_candidate_sha'], 'prior_operation_id': prior_operation_id,
                'prior_version': expected_prior_version, 'intent_id': intent_id, 'task_id': task_id,
                'pane_id': pane_id, 'terminal_id': terminal_id,
                'archived_clone': resource['archived_clone'],
                'transcript_sha256': transcript_sha256, 'at': now}
            nd._write(conn, old, 'superseded', {'reason': 'dispatch_partial_launch_retired',
                'partial_launch_retirement': receipt}, now)
            nd._write(conn, current, 'pending', {'reason': 'dispatch_partial_launch_retired',
                'prior_operation_id': None, 'partial_launch_retirement': receipt}, now,
                next_due=now, started=0, owner=None, lease=None)
            from .state_db import record_event
            record_event({'event_type': 'dispatch_partial_launch_retired', 'payload': receipt,
                'workflow_id': workflow_id, 'node_id': current['payload']['node_id'],
                'source': 'console-recovery', 'timestamp': now}, conn=conn)
            return {'ok': True, 'operation': rs._get(conn, operation_id),
                    'retired_operation': rs._get(conn, prior_operation_id), 'receipt': receipt,
                    'resource_receipt': resource}


def require_authorization(conn, workflow_id, node_id, operation_id=None):
    """An explicit human epoch cannot be captured by an unbound old caller.

    This guard deliberately does not decode Task payloads: save_task also repairs
    damaged legacy payloads, before they can participate in a full snapshot.
    """
    if not conn.execute('SELECT 1 FROM workflows WHERE workflow_id=?', (workflow_id,)).fetchone():
        return
    workflow, _ = rs._workflow_snapshot(conn, workflow_id)
    scope = (workflow_id, node_id, workflow.get('execution_id'), workflow.get('created_at'), workflow.get('reopened_at'))
    where = """workflow_id=? AND json_extract(payload_json,'$.node_id')=?
        AND json_extract(payload_json,'$.generation[0]') IS ?
        AND json_extract(payload_json,'$.generation[1]') IS ?
        AND json_extract(payload_json,'$.generation[2]') IS ?"""
    human = conn.execute('SELECT 1 FROM workflow_recovery_operations WHERE '+where+
        " AND json_extract(detail_json,'$.human_decision') IS NOT NULL LIMIT 1", scope).fetchone()
    if human:
        latest = conn.execute('SELECT id FROM workflow_recovery_operations WHERE '+where+' ORDER BY id DESC LIMIT 1', scope).fetchone()
        if latest and operation_id != latest['id']:
            raise ValueError('此节点已人工重新授权，旧无派发授权请求不能启动或登记任务')
