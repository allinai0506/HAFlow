"""Build coordinator prompts from task data and injected shared policy."""

def build_coordinator_message(task, event_type, *, discipline, read_loop_doc):
    workflow_id = task.get("workflow_id", "unknown")
    task_id = task["task_id"]

    goal = task.get("goal", "未定义")

    criteria = "\n".join(
        f"- {item}"
        for item in task.get(
            "acceptance_criteria",
            []
        )
    )

    if not criteria:
        criteria = "- 未定义"

    if event_type == "inner_loop_exhausted":
        blocker_content = (
            read_loop_doc(task, "BLOCKER.md")
            or "（BLOCKER.md 未找到）"
        )

        return f"""
HERDR_CONTROLLER_BLOCKER_EVENT

workflow_id: {workflow_id}
task_id: {task_id}
stage: {task['stage']}
pane_id: {task['pane_id']}
agent: {task['agent']}
agent_status: 已阻塞（工位内循环已耗尽全部重试次数，无法自愈，需要总指挥仲裁）

任务目标：
{goal}

⚠️ 工位内循环已耗尽全部重试次数，无法自愈，主动请求总指挥仲裁。

━━━━━━━━━━━━━━━━━━━━━
工位求助单 (BLOCKER.md)
━━━━━━━━━━━━━━━━━━━━━
{blocker_content}

━━━━━━━━━━━━━━━━━━━━━
总指挥仲裁三选一(决策必须落盘)：
━━━━━━━━━━━━━━━━━━━━━

A. 问题可解决(提供具体指导后让工位继续)：
   ~/HAFlow/bin/herdr-task set {task_id} working
   然后用 herdr agent prompt 向工位下达具体修复指令。

B. 需要换策略(放弃本轮，换 Agent 或调整范围)：
   ~/HAFlow/bin/herdr-task set {task_id} failed
   再按需重新规划/重派。

C. 目标或验收标准有歧义(调整后重新派发)：
   ~/HAFlow/bin/herdr-task set {task_id} failed
   修正任务目标/验收标准后重新下发。

仲裁前不得把 Task 置为 completed。

{discipline}
""".strip()

    if event_type == "blocked":
        return f"""
HERDR_CONTROLLER_BLOCKED_EVENT

workflow_id: {workflow_id}
task_id: {task_id}
stage: {task['stage']}
pane_id: {task['pane_id']}
agent: {task['agent']}
agent_status: blocked

任务目标：
{goal}

当前 Task 被 Agent 阻塞。

你现在只负责解除阻塞，不允许验收任务。

必须执行：

1. 使用 Herdr 读取 {task['pane_id']} 当前界面和最新输出。
2. 判断 blocked 的真实原因。
3. 如果属于低风险、当前任务范围内的正常操作，可以处理审批并让 Agent 继续。
4. 如果属于高风险操作，不得自动批准，向用户报告风险并保持 blocked。
5. blocked 阶段不得把 Task 设置为 completed。
6. blocked 阶段不得进行正式任务验收。
7. Agent 恢复执行后，Controller 会自动处理 working 状态。
8. 不要推进阶段。

blocked 只表示等待处理，不代表任务结束。

{discipline}
""".strip()

    if event_type == "attention":
        return f"""
HERDR_CONTROLLER_ATTENTION_EVENT

workflow_id: {workflow_id}
task_id: {task_id}
stage: {task['stage']}
pane_id: {task['pane_id']}
agent: {task['agent']}
task_status: {task.get('status')}

任务目标：
{goal}

该 Task 长时间停留在需要人工/总指挥裁决的中间态（interrupted / paused），
既未完成也未失败，阻塞了当前节点的推进。

现在必须立即裁决，只处理这一个 Task：

1. 使用 Herdr 读取 {task['pane_id']} 当前界面与最新输出。
2. 判断现场是否仍有未完成的有效产出：
   - 产出已就绪 → 执行验收（verify-baseline），通过则 set completed（门禁阶段带 --verdict）。
   - 实现未完成 → set rework 并用 herdr agent prompt 继续下发指令。
   - 无法恢复 → set failed。
3. 严禁让任务继续停留在 interrupted / paused。
4. 不要创建新 Task，不要推进阶段。

{discipline}
""".strip()

    if event_type == "done":
        return f"""
HERDR_CONTROLLER_DONE_EVENT

workflow_id: {workflow_id}
task_id: {task_id}
stage: {task['stage']}
pane_id: {task['pane_id']}
agent: {task['agent']}
agent_status: done

任务目标：
{goal}

验收标准：
{criteria}

Agent 本轮执行已经结束。

现在执行正式验收：

1. 使用 Herdr 读取 {task['pane_id']} 的最终输出。

2. 必须执行基线验证与量化指标核验：
   ~/HAFlow/bin/herdr-task verify-baseline {task_id}
   ~/HAFlow/bin/herdr-task verify-metrics {task_id} --if-present

3. `verify-baseline` 是判断当前 Task 文件变化的唯一事实来源：

   - `BASELINE_MATCH`
     表示 Agent 相对于 Task 创建时没有产生新的文件变化。

   - `TASK_CHANGED`
     后面列出的文件，才是当前 Task 真正产生的变化。

4. 验收决策指引（严禁混淆）：

   A. 验收通过（所有标准满足、测试绿灯）：
      ~/HAFlow/bin/herdr-task set {task_id} completed --verdict pass

   B. 发现代码缺陷需回炉（特别是 test / review 阶段查出问题）：
      **严禁对评审/测试任务执行 set rework！**
      必须以 blocked 结论闭环，Controller 会自动触发跨阶段回流并作废受影响链条：
      ~/HAFlow/bin/herdr-task set {task_id} completed --verdict blocked --note "<blocker 清单与修复指引>"

   C. 仅当当前任务自身未完成（如实现中途卡死、需在同一工位继续补全）：
      ~/HAFlow/bin/herdr-task rework {task_id} --prompt "<本任务修复指引>"
      保留同一 task_id 与原工位；不要 supersede + launch 扩增 Pane。

   D. 如果任务发生不可恢复的崩溃：
      ~/HAFlow/bin/herdr-task set {task_id} failed

阶段推进前必须执行：

~/HAFlow/bin/herdr-task list --workflow-id {workflow_id}

只能检查当前 workflow_id 下的任务。
禁止使用其他 Workflow 或历史 Task 判断当前阶段门禁。

只有当前 Workflow 当前阶段所有必要 Task 都 completed，
才能进入下一阶段。

{discipline}
""".strip()

    return None


def build_stage_advance_context(
    *, item, stage, project_ctx, project_name, project_root, base_branch,
    node, next_stage, policy, tasks, frozen_candidate,
):
    """Map scheduler inputs to the legacy stage prompt context."""
    if node:
        purpose = node.get("purpose") or policy.get("purpose", "未定义")
        integration_mode = node.get("default_integration_mode") or policy.get("default_integration_mode", "none")
        task_type = node.get("default_task_type") or policy.get("default_task_type", "feat")
        node_label = node.get("label", next_stage)
        node_type = node.get("node_type", "agent")
        agent_policy = node.get("agent_policy", {})
        req_outs = node.get("required_outputs") or policy.get("required_outputs", [])
        rules_list = node.get("rules") or policy.get("rules", [])
    else:
        purpose = policy.get("purpose", "未定义")
        integration_mode = policy.get("default_integration_mode", "none")
        task_type = policy.get("default_task_type", "test")
        node_label = item.get("stage_label", next_stage)
        node_type = "agent"
        agent_policy = {}
        req_outs = policy.get("required_outputs", [])
        rules_list = policy.get("rules", [])

    required_outputs = "\n".join(f"- {out}" for out in req_outs) or "- 未定义"
    rules = "\n".join(f"- {rule}" for rule in rules_list) or "- 未定义"
    agent_policy_text = ""
    if agent_policy:
        pref = ", ".join(agent_policy.get("preferred", [])) or "无"
        exc = ", ".join(agent_policy.get("exclude", [])) or "无"
        fix = agent_policy.get("fixed") or "无"
        agent_policy_text = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Node Agent 策略
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 优先 Agent: {pref}
- 排除 Agent: {exc}
- 固定 Agent: {fix}
""".strip()

    workflow_id = item.get("workflow_id", "unknown")
    preserved_ids = [
        task["task_id"]
        for task in tasks
        if task.get("workflow_id") == workflow_id
        and next_stage in (task.get("node"), task.get("stage"))
        and task.get("status") != "superseded"
        and task.get("stage_verdict") == "pass"
    ]
    preserved_text = ""
    if preserved_ids:
        preserved_text = (
            "\n已保留的有效任务（verdict=pass，禁止重复创建）：\n"
            + "\n".join(f"- {task_id}" for task_id in preserved_ids)
            + "\n只需补派缺失/被作废的 Task。\n"
        )

    frozen_sha, frozen_branch = frozen_candidate
    candidate_flags = ""
    candidate_block = ""
    if frozen_sha:
        onto_line = (f"   --onto {frozen_branch}\n"
                     if frozen_branch and next_stage not in ("test", "review") else "")
        candidate_flags = f"{onto_line}   --candidate-sha {frozen_sha}"
        candidate_block = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
候选身份（调度器已冻结，必须原样透传）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

candidate_sha: {frozen_sha}
candidate_branch: {frozen_branch or '（未记录）'}

创建 Task 时必须原样携带以上候选身份，
不得重新推断当前分支或版本。
验收节点（test/review）必须验证该冻结版本；
版本不符时门禁 fail-closed 拒绝合并。
""".strip()

    return {
        "workflow_id": workflow_id,
        "project_name": project_name,
        "project_root": project_root,
        "base_branch": base_branch,
        "stage": stage,
        "next_stage": next_stage,
        "node_label": node_label,
        "node_type": node_type,
        "requirement": project_ctx.get("requirement", ""),
        "preserved_text": preserved_text,
        "purpose": purpose,
        "integration_mode": integration_mode,
        "task_type": task_type,
        "required_outputs": required_outputs,
        "rules": rules,
        "agent_policy_text": agent_policy_text,
        "candidate_flags": candidate_flags,
        "candidate_block": candidate_block,
    }


def build_stage_advance_message(context, *, discipline):
    """Render the coordinator prompt for one workflow stage advancement."""
    event_header = context["event_header"]
    intake_note = context["intake_note"]
    workflow_id = context["workflow_id"]
    project_name = context["project_name"]
    project_root = context["project_root"]
    base_branch = context["base_branch"]
    stage = context["stage"]
    next_stage = context["next_stage"]
    node_label = context["node_label"]
    node_type = context["node_type"]
    project_ctx = {"requirement": context["requirement"]}
    preserved_text = context["preserved_text"]
    purpose = context["purpose"]
    integration_mode = context["integration_mode"]
    task_type = context["task_type"]
    required_outputs = context["required_outputs"]
    rules = context["rules"]
    docs_block = context["docs_block"]
    agent_policy_text = context["agent_policy_text"]
    TASK_MANAGER = context["task_manager"]
    candidate_flags = context["candidate_flags"]
    dispatch_op = context["dispatch_op"]
    candidate_block = context["candidate_block"]
    COORDINATOR_DISCIPLINE = discipline

    return f"""
{event_header}

{intake_note}workflow_id: {workflow_id}
project_name: {project_name}
project_root: {project_root}
base_branch: {base_branch}
completed_node: {stage}
next_node: {next_stage} ({node_label})
node_type: {node_type}

用户需求：
{project_ctx.get('requirement', '').strip() or '（未提供；请停止并等待需求正文）'}

当前工作流前置依赖已全部完成。
{preserved_text}
现在进入下一节点：

{next_stage} ({node_label})

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
节点职责
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{purpose}

默认配置：

integration_mode:
{integration_mode}

task_type:
{task_type}

必须产出：

{required_outputs}

执行规则：

{rules}

{docs_block}

{agent_policy_text}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
执行要求
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. 首先执行：

   "{TASK_MANAGER}" list --workflow-id {workflow_id}

   阅读当前 Workflow 已完成节点的真实成果。

2. 根据前面节点的实际成果，
   决定当前节点需要创建几个 Task。

3. 不要固定前端、后端、数据库等角色。

   Pane = Task。

   Agent 根据 Task 动态选择。

4. 每个 Task 必须明确：

   - task_id
   - goal
   - acceptance criteria
   - agent
   - task_type
   - integration_mode

5. 创建 Task 必须使用：

   "{TASK_MANAGER}" launch

   并指定：

   --workflow-id {workflow_id}
   --node {next_stage}
   --source {project_root}
{candidate_flags}
{('   --dispatch-operation-id ' + str(dispatch_op['id']) + ' --dispatch-attempt ' + str(dispatch_op['attempts'])) if dispatch_op else ''}
{('   替代派发：每个前序任务各建一个替代Task，分别携带 --supersedes ' + ', --supersedes '.join(p['task_id'] for p in dispatch_op['payload'].get('predecessors', []))) if dispatch_op and dispatch_op['payload'].get('predecessors') else ''}

6. 默认使用本节点 policy：

   task_type={task_type}
   integration_mode={integration_mode}

   只有当前 Task 的真实性质明确需要不同配置时，
   才允许调整。

7. 不允许手工创建：

   - Clone
   - Pane
   - Branch
   - Agent

8. 可以创建一个 Task，
   也可以创建多个并行 Task。

   数量由实际工作决定。

{candidate_block}

9. 当前节点所有必要 Task 派发完成后，
   结束当前回合。

10. 后续执行、验收、返工、节点推进，
继续交给 Controller。

不要等待用户提醒。

{COORDINATOR_DISCIPLINE}
""".strip()
