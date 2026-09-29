#!/opt/homebrew/bin/python3
"""Selective Replan v1 (HAFlow PR #110) — 纯决策核心,零 I/O。

当 test/review 门禁 blocked 时,legacy fix-loop 无法区分 implementation 内
哪些 Task 真正需要返工。本模块把「Verifier 在结构化门禁结论里显式给出的
affected_task_ids」变成一次可持久化、可重放、可审计的选择性返工决策:

    Gate blocked
      → 结构化 affected_task_ids(只允许来自 Gate Verdict JSON 字段)
      → 机器校验(存在性/归属/谱系头/状态)
      → Plan(mode=selective) 或 Plan(mode=legacy_fallback)
      → 由 controller 持久化为 immutable fact 后再做 targeted invalidation

第一原则:Explicit attribution first. Unknown means legacy fallback.
任何一步无法证明(缺 targets、ID 非法、候选身份不明、版本不明)都返回
fallback 决策,绝不部分接受(两个合法 ID + 一个非法 ID = 整体拒绝)。

本模块只做纯函数决策:不读文件、不碰数据库、不打印。I/O 编排属于
services/herdr-controller.py,事实持久化属于 herdr/scheduler_facts.py。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from .direct_dispatch import lineage_key
except ImportError:  # pragma: no cover - script-style fallback
    from herdr.direct_dispatch import lineage_key

POLICY_KEY = "selective_replan"
POLICY_VERSION = "selective-replan-v1"
MODE_EXPLICIT_TASK_TARGETS = "explicit_task_targets"

MODE_SELECTIVE = "selective"
MODE_LEGACY_FALLBACK = "legacy_fallback"

# --- decision reasons (audit-stable tokens) ---
REASON_OK = "explicit_validated_task_targets"
REASON_POLICY_DISABLED = "policy_disabled"
REASON_NO_GATE_TASK = "blocked_gate_task_unavailable"
REASON_MULTIPLE_GATE_TASKS = "multiple_blocked_gate_tasks"
REASON_GATE_VERSION_UNPROVEN = "gate_task_version_unproven"
REASON_GATE_IDENTITY_UNPROVEN = "gate_candidate_identity_unproven"
REASON_TARGETS_MISSING = "affected_task_ids_missing"
REASON_TARGETS_EMPTY = "affected_task_ids_empty"
REASON_TARGETS_MALFORMED = "affected_task_ids_malformed"
REASON_TARGET_UNKNOWN = "target_task_unknown"
REASON_TARGET_FOREIGN_WORKFLOW = "target_foreign_workflow"
REASON_TARGET_WRONG_NODE = "target_outside_retry_node"
REASON_TARGET_NOT_CURRENT_LINEAGE = "target_not_current_lineage"
REASON_TARGET_STATUS_NOT_REPLACEABLE = "target_status_not_replaceable"

#: 可直接进入 replacement pipeline 的「先 finalize 再 supersede」状态。
#: 与 controller 的 FIX_LOOP_SUPERSEDEABLE 互补:前者由调用方传入,
#: 本集合是 invalidate 路径里 completed/cleanup_ready 的 finalize 前置。
FINALIZABLE_STATUSES = frozenset({"completed", "cleanup_ready"})

_NOTE_BUDGET = 300
_IDENTITY_PREFIX = "srd"


# ============================================================
# 策略
# ============================================================

def policy_from_workflow(
    workflow_cfg: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """从 workflow 配置解析 selective_replan 策略;未声明或形态非法 → None。

    None 表示「完全保持 legacy fix-loop」,调用方不得进入任何 selective
    路径(不写 fact、不改 invalidation、不改 latch)。未知 mode/version
    同样返回 None(fail-closed),而不是报错让 workflow 卡死。
    """
    block = (workflow_cfg or {}).get(POLICY_KEY)
    if not isinstance(block, dict):
        return None
    version = block.get("version")
    version = str(version).strip() if isinstance(version, str) else ""
    if not version:
        version = POLICY_VERSION
    if version != POLICY_VERSION:
        return None
    mode = str(block.get("mode") or "").strip()
    if mode != MODE_EXPLICIT_TASK_TARGETS:
        return None
    retry_node = str(block.get("retry_node") or "").strip()
    if not retry_node:
        return None
    return {"version": version, "mode": mode, "retry_node": retry_node}


def policy_identity(policy: Optional[Dict[str, Any]]) -> str:
    """解析后策略的确定性指纹(版本标签不参与身份之外的语义)。"""
    resolved = policy if isinstance(policy, dict) else {}
    canonical = json.dumps(
        {
            "mode": str(resolved.get("mode") or ""),
            "retry_node": str(resolved.get("retry_node") or ""),
            "version": str(resolved.get("version") or ""),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "srp-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ============================================================
# affected_task_ids 结构化解析
# ============================================================

def parse_affected_task_ids(raw: Any) -> Optional[List[str]]:
    """把原始 JSON 值解析为去重后的 task_id 列表;结构非法 → None。

    - 缺失(None 输入)→ None(调用方按 missing 处理);
    - 空列表 → [](Verifier 显式表示无法归因,与缺失同为 fallback,
      但审计上可区分);
    - 非列表/含非字符串/含空白字符串 → None(malformed)。
    """
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        return None
    ids: List[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            return None
        text = item.strip()
        if text not in ids:
            ids.append(text)
    return ids


# ============================================================
# 目标校验(全部通过才允许 selective;任一失败整体拒绝)
# ============================================================

def _node_of(task: Dict[str, Any]) -> str:
    return str(task.get("node") or task.get("stage") or "")


def _lineage_members(
    tasks: Sequence[Dict[str, Any]], workflow_id: str, node_id: str, root: str
) -> List[Dict[str, Any]]:
    members = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        if str(task.get("workflow_id") or "") != workflow_id:
            continue
        if _node_of(task) != node_id:
            continue
        if lineage_key(task.get("task_id"))[0] == root:
            members.append(task)
    return members


def current_lineage_head(
    tasks: Sequence[Dict[str, Any]], workflow_id: str, node_id: str, root: str
) -> Optional[Dict[str, Any]]:
    """谱系的当前权威成员:序号最大的非 superseded 成员;没有 → None。"""
    active = [
        task
        for task in _lineage_members(tasks, workflow_id, node_id, root)
        if task.get("status") != "superseded" and not task.get("superseded_by")
    ]
    if not active:
        return None
    return max(
        active,
        key=lambda t: (
            lineage_key(t.get("task_id"))[1],
            float(t.get("created_at") or 0),
        ),
    )


def validate_replan_targets(
    tasks: Optional[List[Dict[str, Any]]],
    *,
    workflow_id: str,
    retry_node: str,
    requested_ids: Sequence[str],
    supersedeable_statuses: Any,
) -> Tuple[Optional[List[str]], Optional[List[str]], str]:
    """校验每个 requested id;任一失败 → (None, None, reason) 整体拒绝。

    通过条件(全部满足):
    - Task 存在;
    - Task.workflow_id == 当前 workflow;
    - Task 属于 retry_node;
    - Task 是其谱系当前 head(历史已作废 ID 不自动映射到最新 lineage);
    - Task 未 superseded 且无 superseded_by;
    - 状态可安全进入 replacement pipeline(supersedeable 或可 finalize)。
    """
    by_id: Dict[str, Dict[str, Any]] = {}
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("task_id") or "")
        if task_id:
            by_id[task_id] = task

    supersedeable = set(supersedeable_statuses or ()) | set(FINALIZABLE_STATUSES)
    targets: List[str] = []
    for raw in requested_ids or []:
        task_id = str(raw or "").strip()
        task = by_id.get(task_id)
        if task is None:
            return None, None, f"{REASON_TARGET_UNKNOWN}:{task_id}"
        if str(task.get("workflow_id") or "") != str(workflow_id or ""):
            return None, None, f"{REASON_TARGET_FOREIGN_WORKFLOW}:{task_id}"
        if _node_of(task) != str(retry_node or ""):
            return None, None, f"{REASON_TARGET_WRONG_NODE}:{task_id}"
        root = lineage_key(task_id)[0]
        head = current_lineage_head(tasks or [], str(workflow_id or ""),
                                    str(retry_node or ""), root)
        if head is None or str(head.get("task_id")) != task_id:
            return None, None, f"{REASON_TARGET_NOT_CURRENT_LINEAGE}:{task_id}"
        if str(task.get("status") or "") not in supersedeable:
            return None, None, (
                f"{REASON_TARGET_STATUS_NOT_REPLACEABLE}:{task_id}"
            )
        targets.append(task_id)

    preserved = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        if str(task.get("workflow_id") or "") != str(workflow_id or ""):
            continue
        if _node_of(task) != str(retry_node or ""):
            continue
        if task.get("status") == "superseded" or task.get("superseded_by"):
            continue
        task_id = str(task.get("task_id") or "")
        if task_id and task_id not in targets:
            preserved.append(task_id)

    return targets, sorted(preserved), REASON_OK


# ============================================================
# Episode identity 与 Plan 构建
# ============================================================

def replan_identity(
    workflow_id: Any,
    gate_task_id: Any,
    gate_task_version: Any,
    gate_verified_candidate_sha: Any,
    retry_node: Any,
    policy_fingerprint: Any,
) -> str:
    """一次 selective replan episode 的确定性身份(SHA256)。

    绑定的是「这一次门禁 blocked episode」:workflow + gate task + gate task
    version + 门禁实际验证的候选 + retry 节点 + 解析后策略指纹。
    刻意不含 target_task_ids:同一 episode 两次给出不同 targets 必须命中
    同一 identity 并被 compare_fields 判为 identity_content_mismatch,
    而不是生成两条互相矛盾的 fact。
    """
    canonical = "\n".join(
        [
            str(workflow_id or ""),
            str(gate_task_id or ""),
            str(gate_task_version or ""),
            str(gate_verified_candidate_sha or ""),
            str(retry_node or ""),
            str(policy_fingerprint or ""),
        ]
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{_IDENTITY_PREFIX}-{digest[:32]}"


def _shas_identical(left: Any, right: Any) -> bool:
    try:
        from .scheduler import shas_identical
    except ImportError:  # pragma: no cover - script-style fallback
        try:
            from herdr.scheduler import shas_identical
        except ImportError:
            return str(left or "").strip() == str(right or "").strip()
    try:
        return bool(shas_identical(left, right))
    except Exception:
        return False


def _gate_version(gate_task: Dict[str, Any]) -> Optional[int]:
    try:
        return int(gate_task.get("version"))
    except (TypeError, ValueError):
        return None


def _truncate(text: Any, budget: int = _NOTE_BUDGET) -> str:
    value = str(text or "")
    return value if len(value) <= budget else value[: budget - 1] + "…"


def build_selective_replan_plan(
    *,
    workflow_id: str,
    gate_node: str,
    gate_tasks: Optional[List[Dict[str, Any]]],
    policy: Optional[Dict[str, Any]],
    requested_raw: Any,
    tasks: Optional[List[Dict[str, Any]]],
    frozen_candidate_sha: str,
    supersedeable_statuses: Any,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """构建一次 replan 决策(mode=selective 或 legacy_fallback)。

    无论哪种模式都返回完整 plan 骨架,由调用方持久化为 immutable fact;
    只有 mode=selective 的 plan 允许驱动 targeted invalidation。
    """
    created_at = float(now if now is not None else time.time())
    policy_fp = policy_identity(policy)
    retry_node = str((policy or {}).get("retry_node") or "")

    plan: Dict[str, Any] = {
        "workflow_id": str(workflow_id or ""),
        "replan_id": "",
        "policy_version": str((policy or {}).get("version") or ""),
        "policy_identity": policy_fp,
        "gate_node": str(gate_node or ""),
        "gate_task_id": "",
        "gate_task_version": 0,
        "gate_candidate_sha": "",
        "gate_note": "",
        "retry_node": retry_node,
        "mode": MODE_LEGACY_FALLBACK,
        "requested_task_ids": [],
        "target_task_ids": [],
        "target_lineage_roots": [],
        "preserved_task_ids": [],
        "reason": "",
        "created_at": created_at,
    }

    def _finalize(reason: str) -> Dict[str, Any]:
        plan["reason"] = reason
        if plan["gate_task_id"] and plan["gate_task_version"]:
            plan["replan_id"] = replan_identity(
                plan["workflow_id"],
                plan["gate_task_id"],
                plan["gate_task_version"],
                plan["gate_candidate_sha"],
                plan["retry_node"],
                plan["policy_identity"],
            )
        return plan

    if not policy:
        return _finalize(REASON_POLICY_DISABLED)

    blocked = [
        task
        for task in (gate_tasks or [])
        if isinstance(task, dict) and task.get("stage_verdict") == "blocked"
    ]
    if not blocked:
        return _finalize(REASON_NO_GATE_TASK)
    if len(blocked) > 1:
        return _finalize(REASON_MULTIPLE_GATE_TASKS)
    gate_task = blocked[0]
    plan["gate_task_id"] = str(gate_task.get("task_id") or "")
    plan["gate_note"] = _truncate(gate_task.get("stage_verdict_note"))

    version = _gate_version(gate_task)
    if version is None:
        return _finalize(REASON_GATE_VERSION_UNPROVEN)
    plan["gate_task_version"] = version

    # 候选身份链(#107):dispatch claim == verdict-time evidence == 当前冻结候选。
    claimed = str(gate_task.get("candidate_sha") or "").strip()
    verified = str(gate_task.get("verified_candidate_sha") or "").strip()
    frozen = str(frozen_candidate_sha or "").strip()
    if (
        not claimed
        or not verified
        or not frozen
        or not _shas_identical(claimed, verified)
        or not _shas_identical(verified, frozen)
    ):
        return _finalize(REASON_GATE_IDENTITY_UNPROVEN)
    plan["gate_candidate_sha"] = verified

    requested = parse_affected_task_ids(requested_raw)
    if requested is None:
        if requested_raw is None:
            return _finalize(REASON_TARGETS_MISSING)
        return _finalize(REASON_TARGETS_MALFORMED)
    plan["requested_task_ids"] = list(requested)
    if not requested:
        return _finalize(REASON_TARGETS_EMPTY)

    targets, preserved, reason = validate_replan_targets(
        tasks,
        workflow_id=plan["workflow_id"],
        retry_node=retry_node,
        requested_ids=requested,
        supersedeable_statuses=supersedeable_statuses,
    )
    if targets is None:
        return _finalize(reason)

    plan["mode"] = MODE_SELECTIVE
    plan["target_task_ids"] = list(targets)
    plan["target_lineage_roots"] = sorted(
        {lineage_key(task_id)[0] for task_id in targets}
    )
    plan["preserved_task_ids"] = list(preserved or [])
    return _finalize(REASON_OK)


# ============================================================
# Verifier Prompt 注入:可归因 Task Inventory
# ============================================================

def build_task_inventory(
    tasks: Optional[List[Dict[str, Any]]],
    *,
    workflow_id: str,
    retry_node: str,
) -> List[Dict[str, str]]:
    """当前有效(非 superseded)的 retry_node 谱系头清单,供门禁 Prompt 使用。

    只暴露当前 authoritative Task:历史 -rN 旧版本绝不入清单,避免
    Verifier 把 blocker 绑定到已作废的任务上。谱系头由
    :func:`current_lineage_head` 定义(序号最大的存活成员),与目标校验
    使用完全相同的口径。
    """
    roots: List[str] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        if str(task.get("workflow_id") or "") != str(workflow_id or ""):
            continue
        if _node_of(task) != str(retry_node or ""):
            continue
        root = lineage_key(task.get("task_id"))[0]
        if root not in roots:
            roots.append(root)

    inventory: List[Dict[str, str]] = []
    for root in roots:
        head = current_lineage_head(tasks or [], workflow_id, retry_node, root)
        if head is None:
            continue
        inventory.append(
            {
                "task_id": str(head.get("task_id") or ""),
                "goal": _truncate(head.get("goal"), 120),
            }
        )
    inventory.sort(key=lambda item: item["task_id"])
    return inventory


def render_inventory_block(inventory: Sequence[Dict[str, str]]) -> str:
    """门禁 Prompt 的「可归因 Task」章节(中文,与门禁契约文本一致)。"""
    lines = ["【可归因的实现 Task(affected_task_ids 只能从此列表选择)】", ""]
    for item in inventory or []:
        lines.append(f"- task_id: {item.get('task_id', '')}")
        goal = str(item.get("goal") or "").strip()
        if goal:
            lines.append(f"  goal: {goal}")
    lines.extend(
        [
            "",
            "归因规则:",
            "- 仅当结论为 blocked 且你能把阻塞明确归因到某个实现 Task 时,",
            '  在结论 JSON 中追加 "affected_task_ids": ["<task_id>", ...];',
            "- affected_task_ids 必须原样取自上方列表,禁止编造、推测或引用"
            "历史已作废任务;",
            '- 无法准确归因时写 "affected_task_ids": [] 或不写该字段,',
            "  系统将回退为整体返工(legacy fix-loop)。",
        ]
    )
    return "\n".join(lines)


def render_replacement_blocker_note(
    plan: Dict[str, Any], target_task_id: str
) -> str:
    """补派 replacement 的 blocker 上下文(注入 Prompt,不修改旧 Task)。"""
    preserved = [p for p in (plan or {}).get("preserved_task_ids") or []]
    preserved_text = ", ".join(preserved) if preserved else "(无)"
    note = str((plan or {}).get("gate_note") or "").strip() or "(门禁未附说明)"
    return (
        "本次为选择性返工(Selective Replan):门禁阻塞已明确归因到本任务谱系。\n"
        f"source_task_id: {target_task_id}\n"
        f"gate: {(plan or {}).get('gate_node', '')} "
        f"(task: {(plan or {}).get('gate_task_id', '')})\n"
        f"blocker: {note}\n"
        f"同一实现节点中已被保留(无需重做)的任务: {preserved_text}\n"
        "禁止扩大修改范围:不要顺手重构上述保留任务负责的内容,"
        "你的修改只应针对 blocker 指向的范围;验收仍以 verify-baseline 为准。"
    )


# ============================================================
# 指标(纯函数,Dashboard 之外的最小可算集)
# ============================================================

def replan_metrics(plan: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """从一条 replan fact 计算最小指标集。"""
    plan = plan if isinstance(plan, dict) else {}
    targeted = len(plan.get("target_task_ids") or [])
    preserved = len(plan.get("preserved_task_ids") or [])
    total = targeted + preserved
    return {
        "implementation_task_count": total,
        "targeted_task_count": targeted,
        "preserved_task_count": preserved,
        "replan_ratio": (targeted / total) if total else 0.0,
        "mode": str(plan.get("mode") or ""),
        "reason": str(plan.get("reason") or ""),
    }


# ============================================================
# P1-1/P1-2/P1-3 纯决策 helpers(PR #110 review round-2)
# ============================================================

def merge_affected_task_ids(
    lists: Optional[Sequence[Any]],
) -> List[str]:
    """多门禁 affected_task_ids 的确定性合并(排序去重,忽略空白)。

    P1-2: test ∥ review 并行 blocked 时,同一 retry_node 的多个门禁
    必须先冻结为一个确定的当前轮处理集合,再进入 invalidation;
    否则先处理的 gate 会把另一个 blocked gate Task 一并作废,
    后者的结构化 blocker 事实永远丢失。
    """
    merged: List[str] = []
    for raw_list in lists or []:
        for raw in raw_list or []:
            text = str(raw or "").strip()
            if text and text not in merged:
                merged.append(text)
    return sorted(merged)


def selective_invalidation_outcome(
    target_task_ids: Optional[Sequence[Any]],
    applied_task_ids: Optional[Sequence[Any]],
    failed_task_ids: Optional[Any] = None,
) -> Dict[str, Any]:
    """选择性作废结果分类(纯函数)。

    - applied: 本轮已确认作废(含崩溃前已 superseded 的目标);
    - pending: 目标中尚未作废的(需重试,不得写 latch/通知);
    - failed: pending 中明确报错的子集(诊断用);
    - all_targets_applied: 是否全部落定,调用方 fail-closed 依据。
    """
    targets = [str(t).strip() for t in (target_task_ids or []) if str(t).strip()]
    applied_set = {str(t).strip() for t in (applied_task_ids or []) if str(t).strip()}
    if isinstance(failed_task_ids, dict):
        failed = {str(k).strip(): v for k, v in failed_task_ids.items() if str(k).strip()}
    else:
        failed = {str(t).strip(): "" for t in (failed_task_ids or []) if str(t).strip()}
    applied = [t for t in targets if t in applied_set]
    pending = [t for t in targets if t not in applied_set]
    return {
        "targets": list(targets),
        "applied": applied,
        "pending": pending,
        "failed": {k: v for k, v in failed.items() if k in pending},
        "all_targets_applied": not pending,
    }


def find_reusable_selective_fact(
    facts: Optional[Sequence[Dict[str, Any]]],
    *,
    retry_node: str,
    gate_task_id: str,
    gate_task_version: Any,
    frozen_candidate_sha: str,
    policy_identity: str,
) -> Optional[Dict[str, Any]]:
    """在已持久化 selective 事实中找当前 gate episode 可复用的权威。

    P1-1 Resolve-once-persist-once-read-many:崩溃重启后 Task 状态已变
    (B 已 supersede),绝不能基于当前 Task 状态重新决定 targets;
    已有当前 gate episode 的 Fact 时直接作为 authority 逐个 reconcile。

    匹配条件(全部满足,任一不满足即不是同一 episode):
    - mode == selective;
    - retry_node 相同;
    - gate_task_id 相同;
    - gate_task_version 相同(int 比较);
    - gate_candidate_sha 与当前冻结候选 identical(缩写/大小写由调度器判定);
    - policy_identity 相同。
    返回最新的匹配事实,无匹配 → None。
    """
    node = str(retry_node or "").strip()
    gid = str(gate_task_id or "").strip()
    try:
        want_version = int(gate_task_version)
    except (TypeError, ValueError):
        return None
    frozen = str(frozen_candidate_sha or "").strip()
    policy_fp = str(policy_identity or "").strip()
    if not node or not gid or not frozen or not policy_fp:
        return None
    matched: Optional[Dict[str, Any]] = None
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        payload = fact.get("payload") if isinstance(fact.get("payload"), dict) else fact
        if str(payload.get("mode") or "") != MODE_SELECTIVE:
            continue
        if str(payload.get("retry_node") or "").strip() != node:
            continue
        if str(payload.get("gate_task_id") or "").strip() != gid:
            continue
        try:
            fact_version = int(payload.get("gate_task_version"))
        except (TypeError, ValueError):
            continue
        if fact_version != want_version:
            continue
        if not _shas_identical(
            str(payload.get("gate_candidate_sha") or ""), frozen
        ):
            continue
        if str(payload.get("policy_identity") or "").strip() != policy_fp:
            continue
        matched = fact if isinstance(fact.get("payload"), dict) else dict(fact)
        if isinstance(fact.get("payload"), dict):
            matched = {"event_id": fact.get("id"), **payload}
    return matched


def selective_replacement_baseline(
    fact: Optional[Dict[str, Any]],
    frozen_candidate_sha: Any,
    frozen_branch: Any,
) -> Optional[Dict[str, str]]:
    """Selective replacement 的 fail-closed 基线(纯函数)。

    P1-3: replacement 必须建立在当前冻结 Candidate 之上,
    不能从普通 Task branch 猜。基线可证明当且仅当:
    - fact 为 selective 且带非空 targets;
    - 当前冻结 sha 非空且与 fact 的 gate_candidate_sha identical
      (轮换后旧 fact 不得继续派发,避免在过期树上重做);
    - 当前冻结 branch 非空(可证明的 delivery/candidate branch)。
    任一不满足 → None(调用方回落总指挥,绝不猜 branch)。
    """
    fact = fact if isinstance(fact, dict) else {}
    payload = fact.get("payload") if isinstance(fact.get("payload"), dict) else fact
    if str(payload.get("mode") or "") != MODE_SELECTIVE:
        return None
    targets = [str(t).strip() for t in (payload.get("target_task_ids") or []) if str(t).strip()]
    if not targets:
        return None
    frozen_sha = str(frozen_candidate_sha or "").strip()
    branch = str(frozen_branch or "").strip()
    if not frozen_sha or not branch:
        return None
    if not _shas_identical(str(payload.get("gate_candidate_sha") or ""), frozen_sha):
        return None
    return {"onto_branch": branch, "candidate_sha": frozen_sha}
