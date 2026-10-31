"""规则化节点派发决策（纯函数，无 I/O）。

背景：阶段推进过去必须唤醒总指挥 LLM 才能创建下一节点的 Task——单轮回合
可达数分钟，且同一 workflow 的事件全部串行排在它后面。本模块把"常规推进会"
降级为确定性决策，只有配置不足 / 需求缺失才回落总指挥：

- 首次进入节点：按节点模板（purpose / required_outputs / rules /
  default_task_type / default_integration_mode）生成节点任务；
- fix-loop 回流：只补派被作废且无替代的 Task（受影响子集），verdict=pass 的
  任务由 controller 保留，不在此重复派发；
- 节点存在活跃任务：返回 wait（不重复创建，等状态机自然推进）。

本模块只消费标准 dict 结构，便于零成本单测；launch 子进程与状态持久化由
controller 外壳完成。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .node_roles import is_verifier_node
from .task_lineage import REPLACEMENT_SUFFIX_RE, lineage_key, lineage_redispatch_candidates

# onto 只接受合法分支名:非法值一律丢弃(fail-open 原行为)。
# 允许 slash（agent/... 功能分支常见），拒绝路径穿越/空白/shell 元字符。
_BAD_BRANCH_RE = re.compile(r"(\.\.|[\s~^:?*\[\\]|^[/.-]|/$)")


def sanitize_branch_name(value):
    """Return a safe branch name, or None when unusable."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 256:
        return None
    if _BAD_BRANCH_RE.search(text):
        return None
    return text


GIT_DELIVERED_STATUSES = frozenset(
    {"integrated", "cleanup_ready", "cleaned"}
)


# Launch refusals that a bare retry cannot clear: each needs a state change
# (rebase onto dev, a human decision, or an agent/credential fix). Re-queueing
# them silently just burns the log and hides the escalation — wf-haflow-1021-01
# spun 17.5 minutes and produced zero attention entries this way.
PERMANENT_DISPATCH_REFUSALS = (
    ("[BASELINE STALE]", "baseline_stale"),
    ("[BASELINE REJECTED]", "baseline_rejected"),
    ("[DISPATCH REJECTED]", "dispatch_rejected"),
    ("[ROUTER REJECTED]", "router_isolation_rejected"),
    ("ROUTER OPT-OUT AUDIT FAILED", "router_isolation_rejected"),
    ("[STAGE SKILLS REFUSED]", "skill_capability_gap"),
)


def permanent_dispatch_refusal(output):
    """The stable refusal code when retrying cannot help, otherwise None."""
    text = str(output or "")
    for marker, code in PERMANENT_DISPATCH_REFUSALS:
        if marker in text:
            return code
    return None


def permanent_refusal_escalation(output, task_id, workflow_id, node_id):
    """The attention episode a permanent launch refusal warrants, or None.

    Returned as ready-to-apply arguments so the Controller shell stays a thin
    imperative layer: it decides nothing here, it only performs the two effects.
    """
    code = permanent_dispatch_refusal(output)
    if not code:
        return None
    detail = str(output or "")[:500]
    return {
        "key": f"{task_id}:dispatch_refused",
        "reason": code,
        "note": {"key": f"{task_id}:dispatch_refused",
                 "task": {"task_id": task_id, "workflow_id": workflow_id},
                 "event_type": "dispatch_refused", "reason": code,
                 "attempts": 1, "detail": detail},
        "log": (f"[DIRECT DISPATCH REFUSED] workflow={workflow_id} node={node_id} "
                f"task={task_id} reason={code} -> attention; retry cannot clear this"),
    }


def candidate_branch_for_node(
    tasks, workflow_id, node_id, dep_ids, delivered_in_base=False
):
    """派发候选分支:依赖链最新分支优先,回退本节点自身。

    test 类节点必须测实现分支而非自己的旧任务分支；依赖无分支时
    返回 None（调用方保持原行为）。

    ``delivered_in_base=True`` 表示调用方已确认"git 交付物已合入 base"
    （即工作流的冻结候选就是 base 分支本身）。此时已交付（integrated /
    cleanup_ready / cleaned）依赖的**任务分支**不再作为 onto：交付物早已
    进入 base，而任务分支通常从未推送到 origin，而 `herdr-task launch` 要求
    ``--onto`` 必须存在于 ``refs/remotes/origin/``。实测事故
    (wf-project-0929-01)：test 节点反复 ``Onto branch not found on origin:
    agent/opencode/feat-impl-t7-integration-gates-r2``，任务在 pending 阶段
    即被判 router_isolation_rejected。返回 None 让任务落在 base（候选）上测。

    未交付（仍在跑 / 尚未 integrate）的依赖仍取其任务分支 —— fix-loop 续接
    依赖该行为。
    """
    best = None
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        if task.get("workflow_id") != workflow_id:
            continue
        if (dep_ids or []) and task.get("node") not in dep_ids \
                and task.get("stage") not in dep_ids:
            continue
        if not (dep_ids or []) and node_id not in (
            task.get("node"), task.get("stage")
        ):
            continue
        if task.get("status") == "superseded" or task.get("superseded_by"):
            continue
        if task.get("integration_mode") == "none" or task.get("node") in ("test", "requirements", "plan"):
            # Non-Git deliverables and verifier/docs stations never produce candidate code branches
            continue
        if delivered_in_base and task.get("integration_mode") == "git" \
                and task.get("status") in GIT_DELIVERED_STATUSES:
            # 交付物已在 base 上：onto 若指向本地任务分支，launch 会因
            # origin 无该分支而拒绝派发。跳过，让候选回退到 base。
            continue
        branch = sanitize_branch_name(task.get("branch"))
        if not branch:
            continue
        try:
            updated = float(task.get("updated_at") or 0)
        except (TypeError, ValueError):
            updated = 0.0
        if best is None or updated > best[0]:
            best = (updated, branch)
    if best is not None:
        return best[1]
    if delivered_in_base:
        # 依赖已全部交付 → 候选就是 base 本身。**不要**回退到本节点分支：
        # 本节点正在跑的任务分支（如 test 节点的 agent/pi/test-...-r6）同样
        # 从未推送，launch 仍会以 Onto branch not found on origin 拒绝派发。
        # 返回 None 让任务落在 source HEAD（即 base）上。
        return None
    if dep_ids:
        # If specified dependencies (e.g. test) produced no git code branches,
        # look for upstream code-producing dependencies (e.g. implementation).
        impl_tasks = [
            t for t in tasks or []
            if isinstance(t, dict) and t.get("workflow_id") == workflow_id
            and (t.get("node") or t.get("stage")) in ("implementation", "feat", "impl")
            and t.get("integration_mode") == "git"
            and t.get("status") not in ("superseded", "failed")
        ]
        if impl_tasks and "implementation" not in dep_ids:
            return candidate_branch_for_node(
                tasks, workflow_id, node_id, ["implementation"], delivered_in_base=delivered_in_base
            )
        return candidate_branch_for_node(
            tasks, workflow_id, node_id, [], delivered_in_base=delivered_in_base
        )
    return None

DEFAULT_TASK_TYPE = "feat"
DEFAULT_INTEGRATION_MODE = "none"

# launch --task-type 的 argparse choices（bin/herdr-task）必须与此一致。
TASK_TYPE_CHOICES = ("fix", "feat", "refactor", "docs", "test", "chore", "perf", "ci")
# 早期模板使用的词表：配置合法但 argparse 会拒绝，导致节点永远无法派发。
TASK_TYPE_ALIASES = {"plan": "docs", "explore": "docs", "code": "docs", "review": "test"}


def resolve_task_type(value, node_id=None):
    """节点声明的任务类型 -> launch choices 之一。

    技术任务类型（含早期模板的 plan/explore/code/review）是强约束；业务任务类型
    （如 default_task_type: quote，与 business_task_type 同义）不是 CLI 的枚举维度，
    不在这里拒绝，而是回落到 DEFAULT_TASK_TYPE 安全派发，原始声明保留在
    declared_task_type 供调用方与审计使用。
    """
    raw = str(value or "").strip()
    if not raw:
        return DEFAULT_TASK_TYPE
    if raw in TASK_TYPE_CHOICES:
        return raw
    if raw in TASK_TYPE_ALIASES:
        return TASK_TYPE_ALIASES[raw]
    return DEFAULT_TASK_TYPE

GENERIC_ACCEPTANCE = (
    "改动范围以 herdr-task verify-baseline 为准",
    "产物必须落盘到当前工作目录，禁止只写在回复里",
)

# 门禁结论文件默认落在 clone 外的状态目录:避免被 herdr-task commit 带进
# 交付(clone 内写 .herdr/ 虽已被内部过滤兜底,但状态目录更干净,且不依赖
# clone 存活)。可用 HERDR_GATE_VERDICT_DIR 覆盖(测试/自定义部署)。
GATE_VERDICT_DIR_ENV = "HERDR_GATE_VERDICT_DIR"
DEFAULT_GATE_VERDICT_DIR = Path.home() / ".herdr-controller" / "gate-verdicts"


def gate_verdict_dir() -> Path:
    override = os.environ.get(GATE_VERDICT_DIR_ENV)
    return Path(override).expanduser() if override else DEFAULT_GATE_VERDICT_DIR


def gate_verdict_path(task_id) -> str:
    """门禁结论文件的绝对路径(状态目录,clone 外)。"""
    return str(gate_verdict_dir() / f"{task_id}.json")


def gate_verdict_contract(task_id, inventory_block=None, execution_token=None) -> str:
    """门禁结论契约文本(含本任务的结论文件绝对路径)。

    ``inventory_block``(PR #110,可选):workflow 显式开启 selective_replan
    时由控制面注入的可归因实现 Task 清单;提供后契约追加结构化
    ``affected_task_ids`` 条款。未提供时契约文本与历史逐字节一致。
    ``execution_token``(可选):当前执行纪元令牌;提供后契约要求结论携带该令牌。
    """
    path = gate_verdict_path(task_id)
    tok_clause = f', "execution_token": "{execution_token}"' if execution_token else ""
    base = f"""\
【门禁结论契约（必须遵守，结论将被机器直接采纳）】
1. 验证完成后，写入门禁结论文件：{path}
   内容二选一：
   {{"verdict": "pass", "note": "一句话结论"{tok_clause}}}
   {{"verdict": "blocked", "note": "阻塞原因清单"{tok_clause}}}
   （若你的写权限不允许写该路径，可退回写入当前工作目录下 .herdr/gate-verdict.json，内容格式相同）
2. 同时在终端单独输出一行，便于人工对照：
   HERDR_GATE_VERDICT: <pass|blocked>
3. verdict 只能二选一：pass = 未发现必须返工的阻塞缺陷；blocked = 存在必须返工的阻塞缺陷，且必须在 note 中列出。
4. 结论一经写入即作为门禁裁决生效：pass 自动推进下一节点；blocked 自动触发回流返工。"""
    if execution_token:
        base += f"\n5. 本次门禁执行令牌为：{execution_token}，结论文件必须携带 execution_token 字段以防旧轮次未决写入被误采纳。"
    if not inventory_block:
        return base
    item_num = 6 if execution_token else 5
    return base + f"""
{item_num}. 本工作流已启用选择性返工(Selective Replan):blocked 时如能明确归因,
   请在结论 JSON 中追加结构化字段 affected_task_ids(见下方清单),
   系统将只返工被点名的实现任务;归因不明时保持缺省即整体返工。

{inventory_block}"""


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    result = []
    for item in value:
        text = str(item).strip()
        if text:
            result.append(text)
    return result


def _has_value(value):
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def merge_node_policy(node, policy):
    """节点模板与 stage policy 合并：节点字段为空时回退 policy。

    与总指挥路径的 `node.get(...) or policy.get(...)` 语义保持一致，
    兼容历史 workflow.json 中 purpose/outputs 为空的旧快照。
    """
    node = node if isinstance(node, dict) else {}
    policy = policy if isinstance(policy, dict) else {}

    def pick(key, default=None):
        if _has_value(node.get(key)):
            return node.get(key)
        if _has_value(policy.get(key)):
            return policy.get(key)
        return default

    merged = dict(node)
    merged["purpose"] = pick("purpose", "")
    merged["label"] = pick("label")
    merged["required_outputs"] = pick("required_outputs", [])
    merged["rules"] = pick("rules", [])
    merged["artifact_mode"] = pick("artifact_mode")
    merged["default_task_type"] = pick("default_task_type")
    merged["default_integration_mode"] = pick("default_integration_mode")
    merged["agent_policy"] = pick("agent_policy", {})
    return merged


def classify_dispatch(node):
    if node.get("node_type", "agent") != "agent":
        return "native"
    policy = node.get("agent_policy") or node.get("worker_policy") or {}
    if not isinstance(policy, dict):
        return "dynamic"
    roles = policy.get("roles") or []
    if isinstance(roles, list) and any(
        isinstance(role, dict) and role.get("name") for role in roles
    ):
        return "static_multi"
    if policy.get("max_concurrency", policy.get("max_agents", 1)) == 1 and not node.get("parallel", False):
        return "static_single"
    return "dynamic"


def normalize_node(node):
    """节点模板 -> 决策所需的稳定结构；缺少 id 时返回 None。"""
    if not isinstance(node, dict):
        return None

    node_id = str(node.get("id") or "").strip()
    if not node_id:
        return None

    label = str(node.get("label") or node_id).strip()
    purpose = str(node.get("purpose") or "").strip()

    agent_policy = node.get("agent_policy") or node.get("worker_policy") or {}
    roles = []
    if isinstance(agent_policy, dict):
        raw_roles = agent_policy.get("roles") or []
        if isinstance(raw_roles, list):
            roles = [r for r in raw_roles if isinstance(r, dict) and r.get("name")]

    return {
        "id": node_id,
        "label": label,
        "purpose": purpose,
        "required_outputs": _as_list(node.get("required_outputs")),
        "rules": _as_list(node.get("rules")),
        "task_type": resolve_task_type(node.get("default_task_type"), node_id),
        "declared_task_type": str(node.get("default_task_type") or "").strip(),
        "integration_mode": str(
            node.get("default_integration_mode") or DEFAULT_INTEGRATION_MODE
        ).strip(),
        "roles": roles,
        "artifact_mode": node.get("artifact_mode"),
        "skills": node.get("skills"),
        # Enterprise experience attribution (optional template keys; absent = no behavior).
        "customer": str(node.get("customer") or "").strip(),
        "business_task_type": str(node.get("business_task_type") or "").strip(),
        "mandatory_experience_rules": bool(node.get("mandatory_experience_rules")),
    }


def next_replacement_id(old_task_id, existing_ids):
    """被作废任务的补派 id：x -> x-r2；x-r2 -> x-r3；冲突则递增。"""
    match = REPLACEMENT_SUFFIX_RE.search(old_task_id)
    if match:
        base = old_task_id[: match.start()]
        index = int(match.group(1)) + 1
    else:
        base = old_task_id
        index = 2

    while True:
        candidate = f"{base}-r{index}"
        if candidate not in existing_ids:
            return candidate
        index += 1


def node_tasks_for_latch(tasks, workflow_id, node_id):
    """该 workflow 下某节点的**全部**任务（含 superseded），供闩的自愈判定使用。

    与 :func:`herdr.scheduler.node_tasks`（只取活跃任务）相反：阶段推进闩要
    回答的是"这个节点名下还有没有活着的任务"，被作废的谱系头正是活着的
    替代者缺席的证据，必须一并纳入判定。
    """
    matched = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        if task.get("workflow_id") != workflow_id:
            continue
        if node_id not in (task.get("node"), task.get("stage")):
            continue
        matched.append(task)
    return matched


def initial_task_id(workflow_id, node_id, existing_ids):
    base = f"{workflow_id}-{node_id}-auto"
    if base not in existing_ids:
        return base
    index = 2
    while f"{base}-{index}" in existing_ids:
        index += 1
    return f"{base}-{index}"


def _acceptance_lines(node, override):
    lines = _as_list(override)
    if not lines:
        lines = list(node["required_outputs"])
    for item in GENERIC_ACCEPTANCE:
        if item not in lines:
            lines.append(item)
    return lines


def _prompt(
    node,
    requirement,
    goal,
    acceptance,
    *,
    task_id=None,
    redispatch_of=None,
    last_failure_note=None,
    context_branch=None,
    candidate_sha=None,
    role_outputs=None,
    gate_contract=False,
    docs_block=None,
    gate_inventory_block=None,
    redispatch_blocker_note=None,
    plan_chunk=None,
    plan_ref=None,
):
    target_outputs = role_outputs if role_outputs is not None else node["required_outputs"]
    outputs = "\n".join(f"- {line}" for line in target_outputs) or "- 未定义"
    rules = "\n".join(f"- {line}" for line in node["rules"]) or "- 未定义"
    criteria = "\n".join(f"- {line}" for line in acceptance) or "- 未定义"

    redispatch_note = ""
    if redispatch_of:
        redispatch_note = (
            f"\n本任务是对 {redispatch_of} 的作废补派："
            "只重跑受影响子集，请聚焦失败项，不要扩大改动范围。\n"
        )
    if redispatch_blocker_note:
        redispatch_note += f"\n{str(redispatch_blocker_note).strip()}\n"
    if last_failure_note:
        redispatch_note += f"\n上次门禁失败原因：\n{last_failure_note}\n"
    if context_branch:
        redispatch_note += f"\n相关既有分支（如需核对）：{context_branch}\n"
    frozen_sha = str(candidate_sha or "").strip()
    if frozen_sha:
        redispatch_note += (
            "\n冻结候选版本（必须验证此版本，禁止切换分支或验证其他提交）："
            f"{frozen_sha}\n"
        )

    gate_note = (
        "\n\n" + gate_verdict_contract(task_id, gate_inventory_block)
        if gate_contract and task_id else ""
    )

    docs_section = f"\n{docs_block}\n" if docs_block else ""

    plan_section = ""
    if plan_chunk:
        ref_text = f"（已批准计划版本: {plan_ref}）\n" if plan_ref else ""
        plan_section = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
已审计划约束与任务规格 {ref_text}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{plan_chunk.strip()}
"""

    stage_context_section = ""
    try:
        from herdr import stage_skills
        node_id = str(node.get("id") or "").strip()
        wf_id = str(node.get("workflow_id") or "").strip()
        configured_skills = node.get("skills")
        # 默认 stage 契约只属于 software-development-v1 适配流（PR #355 评
        # 审遗留）：其他工作流命中同名节点 ID 时不得注入默认技能与限制。
        wf_cfg = {}
        try:
            from herdr.projects import workflow_config_for
            wf_cfg = workflow_config_for(wf_id) or {}
        except Exception:
            wf_cfg = {}
        adapted = stage_skills.is_staged_dev_flow_workflow(wf_cfg)
        if (configured_skills or node_id in stage_skills.STAGE_SKILL_BINDINGS) and (adapted or configured_skills):
            ctx = stage_skills.assemble_stage_context(
                workflow_id=wf_id,
                task_id=task_id or "",
                run_id="",
                node_id=node_id,
                dispatch_role=node.get("dispatch_role", "worker"),
                candidate_sha=candidate_sha or "",
                configured_skills=configured_skills,
            )
            stage_context_section = f"\n{ctx['prompt_block']}\n"
    except stage_skills.SkillCapabilityGap:
        raise
    except Exception:
        stage_context_section = ""

    return f"""HERDR_DIRECT_DISPATCH

workflow_id: {node.get("workflow_id", "")}
node: {node["id"]} ({node["label"]})
{redispatch_note}
用户需求：
{requirement}

任务目标：
{goal}
{plan_section}
{stage_context_section}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
节点职责
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{node["purpose"]}

必须产出：

{outputs}

执行规则：

{rules}
{docs_section}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
验收标准
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{criteria}

完成后确保产物已写入当前工作目录并结束回合；
不要手工创建 Clone / Pane / 分支 / Agent，工位已由 Herdr 装配；
严禁向远程执行 git push 或提 PR，Worker Agent 仅在本地工作区自测，严禁随意切换或重命名 Git 分支，所有代码交付与集成由流水线控制器统一收编。{gate_note}""".strip()


def _dispatch_spec(
    node,
    requirement,
    goal,
    acceptance,
    task_id,
    *,
    redispatch_of=None,
    last_failure_note=None,
    context_branch=None,
    integration_mode=None,
    role_outputs=None,
    gate_contract=False,
    docs_block=None,
    onto_branch=None,
    candidate_sha=None,
    gate_inventory_block=None,
    redispatch_blocker_note=None,
    plan_chunk=None,
    plan_ref=None,
):
    from .workflow import validate_artifact_contract
    validate_artifact_contract(node.get("artifact_mode"), integration_mode or node["integration_mode"], node_id=node.get("id"), task_type=node.get("task_type"))
    if is_verifier_node(node) and str(candidate_sha or "").strip():
        # Parallel verifiers own separate branches at one immutable candidate.
        onto_branch = None
    elif onto_branch is None:
        onto_branch = sanitize_branch_name(context_branch)
    spec = {
        "task_id": task_id,
        "goal": goal,
        "acceptance": acceptance,
        "prompt": _prompt(
            node,
            requirement,
            goal,
            acceptance,
            task_id=task_id,
            redispatch_of=redispatch_of,
            last_failure_note=last_failure_note,
            context_branch=context_branch,
            candidate_sha=candidate_sha,
            role_outputs=role_outputs,
            gate_contract=gate_contract,
            docs_block=docs_block,
            gate_inventory_block=gate_inventory_block,
            redispatch_blocker_note=redispatch_blocker_note,
            plan_chunk=plan_chunk,
            plan_ref=plan_ref,
        ),
        "artifact_mode": node.get("artifact_mode"),
        "dispatch_role": "worker",
        "dispatch_round": 1,
        "task_type": node["task_type"],
        "integration_mode": integration_mode or node["integration_mode"],
    }
    # Enterprise experience attribution passthrough (absent = no behavior).
    if str(node.get("customer") or "").strip():
        spec["customer"] = str(node.get("customer")).strip()
    if str(node.get("business_task_type") or "").strip():
        spec["business_task_type"] = str(node.get("business_task_type")).strip()
    if node.get("mandatory_experience_rules"):
        spec["mandatory_experience_rules"] = True
    if redispatch_of:
        spec["redispatch_of"] = redispatch_of
        spec["supersedes"] = redispatch_of
    if onto_branch:
        spec["onto_branch"] = onto_branch
    elif is_verifier_node(node) and candidate_sha and context_branch:
        spec["candidate_branch"] = sanitize_branch_name(context_branch)
    frozen_sha = str(candidate_sha or "").strip()
    if frozen_sha:
        spec["candidate_sha"] = frozen_sha
    return spec


def experience_launch_flags(spec) -> list:
    """CLI flags carrying enterprise experience attribution from a dispatch spec."""
    flags: list = []
    customer = str((spec or {}).get("customer") or "").strip()
    if customer:
        flags += ["--customer", customer]
    business_type = str((spec or {}).get("business_task_type") or "").strip()
    if business_type:
        flags += ["--business-task-type", business_type]
    if (spec or {}).get("mandatory_experience_rules"):
        flags += ["--mandatory-experience-rules"]
    return flags


def plan_stage_dispatch(
    workflow_id,
    node,
    tasks,
    requirement,
    *,
    context_branch=None,
    gate_contract=False,
    docs_block=None,
    candidate_sha=None,
    gate_inventory_block=None,
    redispatch_blocker_notes=None,
    reserved_task_ids=None,
    project_root=None,
):
    """决定 ready 节点该派发什么。

    返回 {"mode": "dispatch"|"wait"|"fallback", "reason": str, "specs": [...]}。

    ``gate_inventory_block``(PR #110,可选):selective_replan 开启时由控制面
    注入的可归因 Task 清单文本,追加到门禁契约。
    ``redispatch_blocker_notes``(PR #110,可选):{被作废 task_id: blocker 上下文},
    补派该谱系时注入 prompt;旧 Task 本身不被改写。
    """
    normalized = normalize_node(node)
    if not normalized:
        return {"mode": "fallback", "reason": "node config missing", "specs": []}

    from .workflow import validate_artifact_contract
    validate_artifact_contract(normalized.get("artifact_mode"), normalized["integration_mode"], node_id=normalized.get("id"), task_type=normalized.get("task_type"))
    dispatch_kind = classify_dispatch(node)
    if dispatch_kind == "native":
        return {"mode": "fallback", "reason": "non-agent node", "specs": []}

    if not normalized["purpose"]:
        return {
            "mode": "fallback",
            "reason": "node purpose missing",
            "specs": [],
        }

    node_id = normalized["id"]
    node_tasks = [
        task
        for task in (tasks or [])
        if task.get("workflow_id") == workflow_id
        and node_id in (task.get("node"), task.get("stage"))
    ]

    awaiting = [
        task
        for task in lineage_redispatch_candidates(node_tasks)
        if not task.get("superseded_by")
    ]
    active = [
        task for task in node_tasks if task.get("status") != "superseded"
    ]

    try:
        if awaiting:
            existing_ids = {
                str(task.get("task_id"))
                for task in (tasks or [])
                if task.get("task_id")
            }
            existing_ids.update(str(task_id) for task_id in (reserved_task_ids or ()) if task_id)
            specs = []
            for task in sorted(
                awaiting, key=lambda item: (item.get("created_at") or 0, item.get("task_id") or "")
            ):
                old_id = str(task.get("task_id"))
                goal = str(task.get("goal") or "").strip() or f"{normalized['label']}: {normalized['purpose']}"
                acceptance = _acceptance_lines(
                    normalized, task.get("acceptance_criteria")
                )
                new_id = next_replacement_id(old_id, existing_ids)
                existing_ids.add(new_id)
                specs.append(
                    _dispatch_spec(
                        normalized,
                        requirement,
                        goal,
                        acceptance,
                        new_id,
                        redispatch_of=old_id,
                        last_failure_note=str(
                            task.get("same_node_redo_note")
                            or task.get("stage_verdict_note")
                            or ""
                        ).strip()
                        or None,
                        context_branch=context_branch,
                        integration_mode=task.get("integration_mode"),
                        gate_contract=gate_contract,
                        docs_block=docs_block,
                        candidate_sha=candidate_sha,
                        gate_inventory_block=gate_inventory_block,
                        redispatch_blocker_note=(
                            redispatch_blocker_notes or {}
                        ).get(old_id),
                    )
                )
            for spec, task in zip(specs, sorted(awaiting, key=lambda item: (item.get("created_at") or 0, item.get("task_id") or ""))):
                spec["dispatch_role"] = task.get("dispatch_role") or "worker"
                spec["dispatch_round"] = int(task.get("dispatch_round") or 1) + 1
                spec["supersedes"] = task["task_id"]
            return {"mode": "dispatch", "reason": "redispatch superseded subset", "specs": specs}

        if active:
            return {
                "mode": "wait",
                "reason": "node has active tasks",
                "specs": [],
            }

        if any(t.get("replacement_pending") is False and not t.get("superseded_by") for t in node_tasks):
            return {"mode": "wait", "reason": "node tasks explicitly abandoned; confirm remaining scope",
                    "specs": []}

        if dispatch_kind == "dynamic":
            has_plan_tasks = any(
                isinstance(t, dict) and (t.get("node") == "plan" or t.get("stage") == "plan")
                and (not t.get("workflow_id") or t.get("workflow_id") == workflow_id)
                for t in (tasks or [])
            )
            # For implementation stage or nodes depending on plan, consume plan deliverables directly
            if has_plan_tasks and (node_id == "implementation" or "plan" in (node.get("depends_on") or [])):
                from herdr.plan_parser import (
                    verify_plan_and_challenger,
                    parse_plan_tasks,
                    validate_task_decoupling,
                )

                policy = node.get("agent_policy") or node.get("worker_policy") or {}
                max_concurrency = int(policy.get("max_concurrency", policy.get("max_agents", 3)) or 3)
                max_tasks_per_node = int(node.get("max_tasks_per_node", 12) or 12)

                has_challenger_role = any(
                    isinstance(t, dict) and (t.get("node") == "plan" or t.get("stage") == "plan")
                    and (not t.get("workflow_id") or t.get("workflow_id") == workflow_id)
                    and any(k in str(t.get(rk) or "").lower() for rk in ("dispatch_role", "role", "task_id") for k in ("challenger", "review", "adversarial"))
                    for t in (tasks or [])
                )
                require_challenger = bool(has_challenger_role)
                if "require_challenger" in node:
                    require_challenger = bool(node["require_challenger"])

                verif_ok, verif_reason, details = verify_plan_and_challenger(
                    workflow_id,
                    tasks=tasks,
                    project_root=project_root,
                    require_challenger=require_challenger,
                )
                if not verif_ok:
                    refusal_code = verif_reason.split(":")[0].strip()
                    return {
                        "mode": "refusal",
                        "reason": verif_reason,
                        "refusal_code": refusal_code,
                        "specs": [],
                    }

                parsed_tasks = parse_plan_tasks(
                    details.get("plan_text") or "",
                    default_name=f"{normalized['label']}主实现任务",
                )
                decoupling_ok, decoupling_reason = validate_task_decoupling(
                    parsed_tasks,
                    max_concurrency=max_concurrency,
                    max_tasks_per_node=max_tasks_per_node,
                )
                if not decoupling_ok:
                    return {
                        "mode": "refusal",
                        "reason": f"plan_decoupling_refusal: {decoupling_reason}",
                        "refusal_code": "plan_decoupling_refusal",
                        "specs": [],
                    }

                existing_ids = {
                    str(task.get("task_id"))
                    for task in (tasks or [])
                    if task.get("task_id")
                }
                existing_ids.update(str(task_id) for task_id in (reserved_task_ids or ()) if task_id)

                specs = []
                num_tasks = len(parsed_tasks)
                plan_ref = str(details.get("plan_id") or details.get("target_ref") or "").strip()
                for idx, t in enumerate(parsed_tasks):
                    t_idx = t.get("task_index", idx + 1)
                    t_name = str(t.get("name") or f"Task {t_idx}").strip()
                    t_files = t.get("files") or []
                    t_verify = str(t.get("verify_command") or "").strip()
                    t_goal_text = str(t.get("goal") or "").strip()
                    t_scope_text = str(t.get("scope") or "").strip()
                    t_ac_text = str(t.get("acceptance") or "").strip()
                    t_chunk = str(t.get("raw_chunk") or "").strip()
                    t_preamble = str(t.get("plan_preamble") or "").strip()
                    if t_preamble:
                        t_chunk = f"{t_preamble}\n\n{t_chunk}" if t_chunk else t_preamble

                    t_goal = f"{normalized['label']} ({t_name}): {t_goal_text or normalized['purpose']}"
                    if t_files:
                        t_goal += f" | 修改文件范围: {', '.join(t_files)}"

                    t_acceptance = []
                    if t_goal_text:
                        t_acceptance.append(f"满足目标：{t_goal_text}")
                    if t_scope_text:
                        t_acceptance.append(f"限定范围：{t_scope_text}")
                    if t_ac_text:
                        t_acceptance.append(f"具体验收条件：{t_ac_text}")
                    if t_files:
                        t_acceptance.append(f"限定修改文件范围：{', '.join(t_files)}")
                    if t_verify:
                        t_acceptance.append(f"通过自测与验证命令：{t_verify}")
                    t_acceptance.extend(list(normalized["required_outputs"]))
                    for item in GENERIC_ACCEPTANCE:
                        if item not in t_acceptance:
                            t_acceptance.append(item)

                    if num_tasks == 1:
                        task_id = initial_task_id(workflow_id, node_id, existing_ids)
                    else:
                        suffix = f"t{idx + 1}"
                        candidate_id = f"{workflow_id}-{node_id}-{suffix}"
                        if candidate_id in existing_ids:
                            candidate_id = initial_task_id(workflow_id, f"{node_id}-{suffix}", existing_ids)
                        task_id = candidate_id
                    existing_ids.add(task_id)

                    spec = _dispatch_spec(
                        normalized,
                        requirement.strip() if requirement else "",
                        t_goal,
                        t_acceptance,
                        task_id,
                        context_branch=context_branch,
                        gate_contract=gate_contract,
                        docs_block=docs_block,
                        candidate_sha=candidate_sha,
                        gate_inventory_block=gate_inventory_block,
                        plan_chunk=t_chunk,
                        plan_ref=plan_ref,
                    )
                    spec["dispatch_role"] = f"impl_{idx + 1}" if num_tasks > 1 else "worker"
                    spec["planned_files"] = t_files
                    spec["verify_command"] = t_verify
                    spec["plan_chunk"] = t_chunk
                    spec["plan_scope"] = t_scope_text
                    spec["plan_acceptance"] = t_ac_text
                    spec["plan_ref"] = plan_ref
                    specs.append(spec)

                return {
                    "mode": "dispatch",
                    "reason": f"plan-driven direct dispatch ({num_tasks} task{'s' if num_tasks > 1 else ''})",
                    "specs": specs,
                }

            return {"mode": "fallback", "reason": "dynamic node requires planning", "specs": []}

        if not (requirement or "").strip():
            return {
                "mode": "fallback",
                "reason": "requirement text missing",
                "specs": [],
            }

        existing_ids = {
            str(task.get("task_id"))
            for task in (tasks or [])
            if task.get("task_id")
        }
        existing_ids.update(str(task_id) for task_id in (reserved_task_ids or ()) if task_id)

        roles = normalized.get("roles") or []
        if roles:
            specs = []
            for r in roles:
                r_name = str(r.get("name") or "worker").strip()
                r_label = str(r.get("label") or r_name).strip()
                r_goal = str(r.get("goal") or "").strip()
                if not r_goal:
                    suffix = str(r.get("purpose_suffix") or "").strip()
                    r_goal = f"{normalized['label']} ({r_label}): {suffix or normalized['purpose']}"
                r_outputs = _as_list(r.get("outputs")) or list(normalized["required_outputs"])
                r_acceptance = _acceptance_lines(normalized, r_outputs)

                task_id = f"{workflow_id}-{node_id}-{r_name}"
                if task_id in existing_ids:
                    task_id = initial_task_id(workflow_id, f"{node_id}-{r_name}", existing_ids)
                existing_ids.add(task_id)

                specs.append(
                    _dispatch_spec(
                        normalized,
                        requirement.strip(),
                        r_goal,
                        r_acceptance,
                        task_id,
                        role_outputs=r_outputs,
                        context_branch=context_branch,
                        gate_contract=gate_contract,
                        docs_block=docs_block,
                        candidate_sha=candidate_sha,
                        gate_inventory_block=gate_inventory_block,
                    )
                )
            for spec, role in zip(specs, roles):
                spec["dispatch_role"] = str(role["name"]).strip()
            return {"mode": "dispatch", "reason": "initial node dispatch with roles", "specs": specs}

        acceptance = _acceptance_lines(normalized, None)
        goal = f"{normalized['label']}: {normalized['purpose']}"
        spec = _dispatch_spec(
            normalized,
            requirement.strip(),
            goal,
            acceptance,
            initial_task_id(workflow_id, node_id, existing_ids),
            context_branch=context_branch,
            gate_contract=gate_contract,
            docs_block=docs_block,
            candidate_sha=candidate_sha,
            gate_inventory_block=gate_inventory_block,
        )
        return {"mode": "dispatch", "reason": "initial node dispatch", "specs": [spec]}
    except Exception as exc:
        try:
            from herdr.stage_skills import SkillCapabilityGap
            if isinstance(exc, SkillCapabilityGap):
                return {
                    "mode": "refusal",
                    "reason": f"skill_capability_gap: {exc}",
                    "refusal_code": "skill_capability_gap",
                    "specs": [],
                }
        except ImportError:
            pass
        raise
