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

REPLACEMENT_SUFFIX_RE = re.compile(r"-r(\d+)$")

DEFAULT_TASK_TYPE = "feat"
DEFAULT_INTEGRATION_MODE = "none"

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


def gate_verdict_contract(task_id) -> str:
    """门禁结论契约文本(含本任务的结论文件绝对路径)。"""
    path = gate_verdict_path(task_id)
    return f"""\
【门禁结论契约（必须遵守，结论将被机器直接采纳）】
1. 验证完成后，写入门禁结论文件：{path}
   内容二选一：
   {{"verdict": "pass", "note": "一句话结论"}}
   {{"verdict": "blocked", "note": "阻塞原因清单"}}
   （若你的写权限不允许写该路径，可退回写入当前工作目录下 .herdr/gate-verdict.json，内容格式相同）
2. 同时在终端单独输出一行，便于人工对照：
   HERDR_GATE_VERDICT: pass   或   HERDR_GATE_VERDICT: blocked
3. verdict 只能二选一：pass = 未发现必须返工的阻塞缺陷；blocked = 存在必须返工的阻塞缺陷，且必须在 note 中列出。
4. 结论一经写入即作为门禁裁决生效：pass 自动推进下一节点；blocked 自动触发回流返工。"""


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
    if policy.get("max_agents", 1) == 1 and not node.get("parallel", False):
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
        "task_type": str(
            node.get("default_task_type") or DEFAULT_TASK_TYPE
        ).strip(),
        "integration_mode": str(
            node.get("default_integration_mode") or DEFAULT_INTEGRATION_MODE
        ).strip(),
        "roles": roles,
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


def lineage_key(task_id):
    """替换谱系键：(谱系根, 序号)。x -> (x, 1)；x-r2 -> (x, 2)。"""
    text = str(task_id or "")
    match = REPLACEMENT_SUFFIX_RE.search(text)
    if match:
        return text[: match.start()], int(match.group(1))
    return text, 1


def lineage_redispatch_candidates(node_tasks):
    """每条替换谱系里"需要补派"的最新一发（没有则不含该谱系）。

    规则：谱系内只要还有任一非 superseded 成员（在跑/已落定），该谱系就
    已有代表，不再补派；只有当整个谱系都已作废时，才取序号最新的一发
    作为补派对象（且它必须还没有替代者）。

    补派必须按谱系去重：历史被作废任务若被反复补派，会随 fix-loop 轮次
    指数放大（2 -> 4 -> 8 个并发重复任务，实测事故见 lessons §61）。
    """
    groups = {}
    for task in node_tasks:
        root, index = lineage_key(task.get("task_id"))
        groups.setdefault(root, []).append(
            (index, float(task.get("created_at") or 0), task)
        )

    candidates = []
    for members in groups.values():
        if any(
            task.get("status") != "superseded" for _, _, task in members
        ):
            continue
        _, _, head = max(members, key=lambda item: (item[0], item[1]))
        if not head.get("superseded_by"):
            candidates.append(head)
    return candidates


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
    role_outputs=None,
    gate_contract=False,
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
    if last_failure_note:
        redispatch_note += f"\n上次门禁失败原因：\n{last_failure_note}\n"
    if context_branch:
        redispatch_note += f"\n相关既有分支（如需核对）：{context_branch}\n"

    gate_note = (
        "\n\n" + gate_verdict_contract(task_id) if gate_contract and task_id else ""
    )

    return f"""HERDR_DIRECT_DISPATCH

workflow_id: {node.get("workflow_id", "")}
node: {node["id"]} ({node["label"]})
{redispatch_note}
用户需求：
{requirement}

任务目标：
{goal}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
节点职责
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{node["purpose"]}

必须产出：

{outputs}

执行规则：

{rules}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
验收标准
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{criteria}

完成后确保产物已写入当前工作目录并结束回合；
不要手工创建 Clone / Pane / 分支 / Agent，工位已由 Herdr 装配。{gate_note}""".strip()


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
):
    return {
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
            role_outputs=role_outputs,
            gate_contract=gate_contract,
        ),
        "task_type": node["task_type"],
        "integration_mode": integration_mode or node["integration_mode"],
    }


def plan_stage_dispatch(
    workflow_id,
    node,
    tasks,
    requirement,
    *,
    context_branch=None,
    gate_contract=False,
):
    """决定 ready 节点该派发什么。

    返回 {"mode": "dispatch"|"wait"|"fallback", "reason": str, "specs": [...]}。
    """
    normalized = normalize_node(node)
    if not normalized:
        return {"mode": "fallback", "reason": "node config missing", "specs": []}

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

    if awaiting:
        existing_ids = {
            str(task.get("task_id"))
            for task in (tasks or [])
            if task.get("task_id")
        }
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
                        task.get("stage_verdict_note") or ""
                    ).strip()
                    or None,
                    context_branch=context_branch,
                    integration_mode=task.get("integration_mode"),
                    gate_contract=gate_contract,
                )
            )
        return {"mode": "dispatch", "reason": "redispatch superseded subset", "specs": specs}

    if active:
        return {
            "mode": "wait",
            "reason": "node has active tasks",
            "specs": [],
        }

    if dispatch_kind == "dynamic":
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
                    gate_contract=gate_contract,
                )
            )
        return {"mode": "dispatch", "reason": "initial node dispatch with roles", "specs": specs}

    acceptance = _acceptance_lines(normalized, None)
    goal = f"{normalized['label']}: {normalized['purpose']}"
    spec = _dispatch_spec(
        normalized,
        requirement.strip(),
        goal,
        acceptance,
        initial_task_id(workflow_id, node_id, existing_ids),
        gate_contract=gate_contract,
    )
    return {"mode": "dispatch", "reason": "initial node dispatch", "specs": [spec]}
