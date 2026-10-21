from console import inventory

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


def _validate_force_pass_params(payload, wid, wf=None, task=None, db_path=None):
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
    wf_tasks = inventory.tasks_for_workflow(wid)
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

    raw_op = payload.get('operator')
    if raw_op is None and 'operator' not in payload:
        raw_op = 'human'
    token = payload.get('auth_token') or payload.get('token') or payload.get('operator_token')
    credentials_path = payload.get('credentials_path') or payload.get('operator_credentials')
    from herdr.operator_auth import OperatorAuthError, authenticate_and_authorize_operator
    from herdr.transitions import InvalidOperatorError, validate_operator
    try:
        op = validate_operator(raw_op)
        principal = authenticate_and_authorize_operator(
            operator_id=op,
            action="force_pass_gate",
            token=token,
            credentials_path=credentials_path,
            task=task,
            db_path=db_path,
        )
        op = principal.operator_id
    except InvalidOperatorError as exc:
        raise RuntimeError(f"无效的操作人标识: {exc}")
    except OperatorAuthError as exc:
        raise RuntimeError(f"操作人凭据或能力鉴权失败: {exc}")
    except (ValueError, RuntimeError) as exc:
        raise RuntimeError(f"无效的操作人标识: {exc}")
    return gate, reason, op
