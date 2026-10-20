"""Read shared workflow documents and render coordinator/fix-loop notifications.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def shared_docs_block(ports, workflow_id, node_id, related_nodes=(), project_ctx=None):
    """跨节点共享文档区注入块:只读上下文 + 追加写入指引(best-effort)。"""
    try:
        from herdr import workflow_docs as wd
        notes = wd.load_notes(workflow_id)
    except Exception as exc:
        print(f"[SHARED DOCS WARN] load {workflow_id}: {exc}")
        return ""

    current_base_sha = None
    ctx = project_ctx or {}
    project_root = ctx.get("project_root") or ""
    base_branch = ctx.get("base_branch") or ""
    if project_root and base_branch:
        try:
            result = ports.subprocess.run(
                ["git", "-C", project_root, "rev-parse", "--short", base_branch],
                text=True,
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                current_base_sha = result.stdout.strip() or None
        except Exception:
            current_base_sha = None

    try:
        return wd.render_context_block(
            workflow_id,
            notes,
            node_id=node_id,
            related_nodes=related_nodes,
            current_base_sha=current_base_sha,
        )
    except Exception as exc:
        print(f"[SHARED DOCS WARN] render {workflow_id}: {exc}")
        return ""

def build_coordinator_message(ports, task, event_type):
    from herdr.coordinator_messages import (
        build_coordinator_message as build_message,
    )

    return build_message(
        task,
        event_type,
        discipline=ports.COORDINATOR_DISCIPLINE,
        read_loop_doc=ports._read_loop_doc,
    )

def build_stage_advance_message(ports, context):
    from herdr.coordinator_messages import build_stage_advance_message as build_message

    return build_message(context, discipline=ports.COORDINATOR_DISCIPLINE)

def _read_loop_doc(ports, task, filename, max_lines=None):
    """读取工位内环目录(<clone>/.herdr-loop/)下的报告文档,失败返回空串。"""
    clone_path = task.get("clone_path") or ""
    if not clone_path:
        return ""
    path = ports.Path(clone_path) / ".herdr-loop" / filename
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        return ""
    if max_lines:
        lines = lines[:max_lines]
    return "\n".join(lines)

def _build_selective_replan_message(ports, item, project_name="unknown"):
    """选择性返工的事件消息:告知归因结果,禁止总指挥重做整个阶段。"""
    workflow_id = item["workflow_id"]
    gate_stage = item["gate_stage"]
    retry_node = item["retry_node"]
    loop_count = item["loop_count"]
    max_loops = item["max_loops"]
    invalidated = item.get("invalidated") or []
    suggested_branch = item.get("suggested_branch")
    roots = [str(r).strip() for r in (item.get("target_lineage_roots") or [])]
    roots_text = ", ".join(roots) if roots else "(未记录)"

    blockers_text = "\n".join(
        f"- {b.get('task_id')}: {b.get('note') or '(未记录说明)'}"
        for b in item.get("blockers") or []
    ) or "- (未记录 blocker 说明,请读取 gate 阶段任务输出)"

    if loop_count >= max_loops:
        escalation = (
            f"\n注意:已达 fix-loop 上限({loop_count}/{max_loops})。"
            "先向用户请示(继续修 / 换方案 / 放弃),未经用户确认不得派发。\n"
        )
    else:
        escalation = ""

    branch_line = suggested_branch or "(未找到,请确认 retry_node 最近 committed 任务的分支)"

    docs_block = ports.shared_docs_block(
        workflow_id,
        retry_node,
        related_nodes=(gate_stage,),
        project_ctx=ports.project_for_workflow(workflow_id) or {},
    )

    return f"""
HERDR_CONTROLLER_SELECTIVE_REPLAN_EVENT

workflow_id: {workflow_id}
project_name: {project_name}
gate_stage: {gate_stage} — 验收结论 blocked(已显式归因)
retry_node: {retry_node}
suggested_branch: {branch_line}
loop_count: {loop_count}/{max_loops}
被点名的实现任务谱系(受影响,需重做): {roots_text}
{escalation}
门禁在结构化结论里明确指出了受影响的实现 Task,因此本次**不是**整体返工:
Controller 已只作废被点名的谱系(共 {len(invalidated)} 个 Task,见 Task Registry),
实现节点内其余任务保持原样、不重派。

可继续的任务由 Controller 就地 rework，复用 Task 和 Pane；
已终结任务的替代任务(如 <task>-r2)通过既有补派管线创建，记录理由并计入累计配额,
你**不需要**派发任何 fix task。

⛔ 禁止:对 --stage {retry_node} 派发全量 fix task。
那会与 Controller 正在执行的替代任务重复,并把改动范围扩大到被明确
保留的任务上,直接破坏本次归因的前提。

你现在只需确认流程继续:
- 替代任务已派发 → 无需动作,等 fix 完成,下游 test → review → wrapup 自动推进;
- 长时间没有出现替代任务(例如路由/准入阻断)→ 排查派发链路本身
  (`./bin/herdr-deep-preflight --deep` 看 Agent 准入),必要时向用户请示,
  仍**不要**改用全量 fix task 兜底。

Blocker 清单(blocked 结论与修复指引):
{blockers_text}

如需再次修复,由后续门禁结论重新归因;不要绕过 Controller 自行扩大作废范围。
派发/排查完成后结束当前回合,后续推进交给 Controller。

{docs_block}

{ports.COORDINATOR_DISCIPLINE}
""".strip()

def build_fix_loop_message(ports, item, project_name="unknown"):
    """fix_loop 事件消息;派发命令是建议骨架,裁量在总指挥。

    mode=selective(PR #110)时归因已明确到具体实现任务谱系,替代任务由
    Controller 的既有补派管线自动创建——此时绝不能发 legacy 的全量
    fix task 指引,否则总指挥会与 Controller 同时在同一分支上重做整个
    阶段,既重复又破坏「只重做被点名范围」的保证。
    """
    if item.get("mode") == "selective":
        return ports._build_selective_replan_message(item, project_name)

    workflow_id = item["workflow_id"]
    gate_stage = item["gate_stage"]
    retry_node = item["retry_node"]
    loop_count = item["loop_count"]
    max_loops = item["max_loops"]
    invalidated = item.get("invalidated") or []
    suggested_branch = item.get("suggested_branch")

    blockers_text = "\n".join(
        f"- {b.get('task_id')}: {b.get('note') or '(未记录说明)'}"
        for b in item.get("blockers") or []
    ) or "- (未记录 blocker 说明,请读取 gate 阶段任务输出)"

    escalation = ""
    if loop_count >= max_loops:
        escalation = (
            f"\n注意:已达 fix-loop 上限({loop_count}/{max_loops})。"
            "先向用户请示(继续修 / 换方案 / 放弃),"
            "未经用户确认不得派发。\n"
        )

    if item.get("exhausted"):
        escalation = (
            f"\n⛔ 回流预算已耗尽(loops={loop_count}/{max_loops},"
            f"原因={item.get('escalation_reason', 'unknown')})。"
            "Controller 已停止自动作废/重派。"
            "只允许三选一并落盘:接受现状推进 / 缩小范围重派 / 关闭工作流。"
            "禁止再次派发同范围 fix task。\n"
        )

    if suggested_branch:
        onto_flag = f"--onto {suggested_branch} "
        branch_line = suggested_branch
    else:
        onto_flag = ""
        branch_line = "(未找到,请自行确认 retry_node 最近 committed 任务的分支)"

    docs_block = ports.shared_docs_block(
        workflow_id,
        retry_node,
        related_nodes=(gate_stage,),
        project_ctx=ports.project_for_workflow(workflow_id) or {},
    )

    return f"""
HERDR_CONTROLLER_FIX_LOOP_EVENT

workflow_id: {workflow_id}
project_name: {project_name}
gate_stage: {gate_stage} — 验收结论 blocked
retry_node: {retry_node}
suggested_branch: {branch_line}
loop_count: {loop_count}/{max_loops}
{escalation}
Controller 已自动作废受影响的 gate 与下游 Task(共 {len(invalidated)} 个,见 Task Registry);
fix 完成后 DAG 将自动按 test → review → wrapup 顺序重新推进,旧 verdict 一并作废。

Blocker 清单(blocked 结论与修复指引):
{blockers_text}

默认继续 retry_node 的原 Task：Controller 已对可继续的任务执行 rework，复用原 Pane。
请先检查 Task Registry；已有 rework 时等待修复完成，不再 launch。
只有原任务已经终结、无法继续时，才按明确理由派发替代任务（计入累计配额）：

~/HAFlow/bin/herdr-task launch --workflow-id {workflow_id} --stage {retry_node} \\
  {onto_flag}--agent auto --task-type fix --integration-mode git \\
  --execution-id {workflow_id} \\
  --goal "修复 gate {gate_stage} 的阻断项" \\
  --acceptance "<逐条对应 Blocker 清单>" \\
  --prompt "<blocker 详情、修复范围与验证方式>"

fix 产出必须落分支("--integration-mode git"),否则测试仍测旧候选而恒 blocked。
如需再次修复，优先 herdr-task rework <task_id> --prompt "<阻断项>"。
终结任务例外替换需 --supersedes <task_id> --supersede-reason "<无法继续的具体理由>"。
派发完成后结束当前回合,后续推进交给 Controller。

{docs_block}

{ports.COORDINATOR_DISCIPLINE}
""".strip()


DEPENDENCIES = {'shared_docs_block': ('subprocess',),
 'build_coordinator_message': ('COORDINATOR_DISCIPLINE', '_read_loop_doc'),
 'build_stage_advance_message': ('COORDINATOR_DISCIPLINE',),
 '_read_loop_doc': ('Path',),
 '_build_selective_replan_message': ('COORDINATOR_DISCIPLINE', 'project_for_workflow',
                                     'shared_docs_block'),
 'build_fix_loop_message': ('COORDINATOR_DISCIPLINE', '_build_selective_replan_message',
                            'project_for_workflow', 'shared_docs_block')}

__all__ = tuple(DEPENDENCIES)
