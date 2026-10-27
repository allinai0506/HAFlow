"""解析计划阶段成果并转化为结构化实现任务规格。

核心原则：
1. 语义规划留在计划节点：由 executor 起草技术方案与任务拆解，challenger 进行破防质询与可行性评估；
2. 审查闭环验证：核实 challenger 审查的是本轮有效计划，且给出明确通过结论，无未解决的阻塞缺陷（未通过/blocked）；
3. 严格任务解耦：支持单任务与符合解耦条件的多任务（无文件重叠、规范化路径匹配、无未解依赖、受并发与预算约束）；
4. 纯代码确定性转换：将经过验证的计划直接转化为 TaskSpecs 经由现有 launch 入口直接派发，
   不回退总指挥，不引入第二套规划 LLM。
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from herdr import workflow_docs
from herdr.stage_skills import (
    validate_plan_deliverable_content,
    is_staged_dev_flow_workflow,
)


_TASK_HEADER_RE = re.compile(
    r"^(?:#{2,4}\s+)?(?:Task|任务)\s*(\d+)[:：]\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)
_FILE_LINE_RE = re.compile(
    r"[-*]\s*(?:Create|Modify|Test|新增|修改|测试)[:：]?\s*[`'\"]?([^`'\"\n\r\t]+)[`'\"]?",
    re.IGNORECASE,
)
_DEP_LINE_RE = re.compile(
    r"[-*]?\s*(?:\*\*)?(?:依赖|Depends on|Dependencies|Consumes)(?:\*\*)?[:：]?\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)
_GOAL_LINE_RE = re.compile(
    r"[-*]?\s*(?:\*\*)?(?:目标|Goal|任务目标)(?:\*\*)?[:：]?\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)
_SCOPE_LINE_RE = re.compile(
    r"[-*]?\s*(?:\*\*)?(?:范围|Scope|验收标准|验收条目|AC)(?:\*\*)?[:：]?\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)

# 阻断性裁决正则：包含 [BLOCKED]、[REJECTED]、显式阻断结论或非否定形态的阻塞缺陷报告
_CHALLENGER_BLOCKED_RE = re.compile(
    r"(?:\[(?:BLOCKED|REJECT(?:ED)?|FAILED)\]"
    r"|(?:HERDR_GATE_VERDICT|VERDICT|结论|裁决|审查结论)[:：]\s*(?:blocked|reject(?:ed)?|fail(?:ed)?|不通过|阻断|存在阻塞|驳回)"
    r"|【阻塞】"
    r"|(?<![未无不])(?:发现|存在)阻塞性缺陷)",
    re.IGNORECASE,
)

# 显式通过裁决正则：包含 [PASS]、[APPROVED]、显式通过结论或无阻塞性缺陷声明
_CHALLENGER_PASS_RE = re.compile(
    r"(?:\[(?:PASS(?:ED)?|APPROVED?)\]"
    r"|(?:HERDR_GATE_VERDICT|VERDICT|结论|裁决|审查结论)[:：]\s*(?:pass(?:ed)?|approved?|通过|无阻塞|建议通过)"
    r"|(?:确认|建议|审查|评估|判定|验收)通过"
    r"|(?<![未无不])通过"
    r"|(?<![有并])未发现阻塞性缺陷"
    r"|无阻塞性缺陷)",
    re.IGNORECASE,
)


def is_valid_file_path(path: str) -> bool:
    """检查是否为合法的代码/配置文件路径，支持无后缀根文件如 Dockerfile、Makefile。"""
    if not path or path.startswith("http://") or path.startswith("https://"):
        return False
    # 路径中包含目录分隔符（如 bin/herdr-task, herdr/plan_parser.py）
    if "/" in path:
        return True
    # 包含扩展名（如 a.py, config.yaml）
    if "." in path:
        return True
    # 常见的无扩展名根文件名
    base_lower = os.path.basename(path).lower()
    if base_lower in (
        "dockerfile", "makefile", "license", "containerfile",
        "procfile", "gemfile", "rakefile", "vagrantfile", "jenkinsfile"
    ):
        return True
    return False


def clean_file_path(raw: str) -> str:
    """清理文件路径中的行号后缀、注释或引号，并规范化相对路径。"""
    cleaned = str(raw or "").strip().strip("`'\"")
    # 去除尾部可能带有的行号范围如 :123-145
    cleaned = re.sub(r":\d+(?:-\d+)?$", "", cleaned).strip()
    if cleaned:
        cleaned = os.path.normpath(cleaned).replace("\\", "/")
        if cleaned.startswith("./"):
            cleaned = cleaned[2:]
    return cleaned


def extract_verify_command(chunk: str) -> str:
    """从计划任务块中提取自测/验证命令，严格避免解析为 markdown 符号。"""
    lines = chunk.splitlines()
    for idx, line in enumerate(lines):
        m = re.search(
            r"[-*]?\s*(?:\*\*)?(?:验证命令|自测命令|Test command|Verify|Verification)(?:\*\*)?[:：]?(.*)$",
            line,
            re.IGNORECASE,
        )
        if m:
            rest = m.group(1).strip()
            rest_clean = re.sub(r"^\*+|\*+$", "", rest).strip().strip("`'\"")
            if rest_clean and rest_clean != "**":
                return rest_clean
            # 若命令在下一行
            if idx + 1 < len(lines):
                next_line = lines[idx + 1].strip()
                if next_line.startswith("```"):
                    if idx + 2 < len(lines) and not lines[idx + 2].strip().startswith("```"):
                        return lines[idx + 2].strip()
                elif next_line and not next_line.startswith("-") and not next_line.startswith("#"):
                    next_clean = next_line.strip("`'\"")
                    if next_clean and next_clean != "**":
                        return next_clean

    cb_m = re.search(
        r"```(?:bash|sh|shell)?\n\s*([^\n]+(?:pytest|python3|npm|pnpm|cargo|go|make)[^\n]*)\n```",
        chunk,
        re.IGNORECASE,
    )
    if cb_m:
        return cb_m.group(1).strip()
    return ""


def read_plan_file_from_dir(base_dir: str) -> str:
    """从指定目录查找计划文件（支持 plan.md, docs/plan.md 及 docs/superpowers/plans 下单一最新计划）。

    选择单一最新计划文件，绝不跨文件全量拼接，确保与自动验收逻辑完全一致。
    """
    if not base_dir or not os.path.exists(base_dir):
        return ""
    for fname in ("plan.md", "docs/plan.md"):
        fpath = os.path.join(base_dir, fname)
        if os.path.isfile(fpath) and os.path.getsize(fpath) > 0:
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                pass
    plans_dir = os.path.join(base_dir, "docs/superpowers/plans")
    if os.path.isdir(plans_dir):
        md_files = [
            os.path.join(plans_dir, f) for f in os.listdir(plans_dir)
            if f.endswith(".md") and os.path.isfile(os.path.join(plans_dir, f))
        ]
        if md_files:
            newest = max(md_files, key=os.path.getmtime)
            try:
                with open(newest, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                pass
    return ""


class PlanDeliverablesResult(tuple):
    """4-element tuple for backward compatibility with p, c, e, ch unpacking, carrying meta."""
    def __new__(cls, plan_text, challenger_text, executor_task, challenger_task, meta=None):
        return super().__new__(cls, (plan_text, challenger_text, executor_task, challenger_task))

    def __init__(self, plan_text, challenger_text, executor_task, challenger_task, meta=None):
        self.plan_text = plan_text
        self.challenger_text = challenger_text
        self.executor_task = executor_task
        self.challenger_task = challenger_task
        self.meta = meta or {}


def extract_challenger_verdict(
    challenger_text: str,
    chal_task: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """提取 challenger 对抗审查结论。

    返回: (verdict, reason)
    verdict 取值为: "pass" | "blocked" | "missing"
    """
    if chal_task and chal_task.get("stage_verdict"):
        v = str(chal_task.get("stage_verdict")).lower().strip()
        if v in ("blocked", "failed", "reject", "rejected"):
            note = chal_task.get("stage_verdict_note") or "task stage_verdict is blocked"
            return "blocked", f"challenger task stage_verdict is blocked: {note}"
        elif v in ("pass", "passed", "approved"):
            return "pass", "challenger task stage_verdict is pass"

    if not challenger_text or not challenger_text.strip():
        return "missing", "challenger review text is empty"

    text = challenger_text.strip()

    has_explicit_blocked = False
    blocked_reasons: List[str] = []
    has_explicit_pass = False
    pass_reasons: List[str] = []

    # 1. 显式方括号标记优先判决
    if re.search(r"\[(?:BLOCKED|REJECT(?:ED)?|FAILED)\]", text, re.IGNORECASE):
        has_explicit_blocked = True
        blocked_reasons.append("explicit [BLOCKED] marker found")
    if re.search(r"\[(?:PASS(?:ED)?|APPROVED?)\]", text, re.IGNORECASE):
        has_explicit_pass = True
        pass_reasons.append("explicit [PASS] marker found")

    # 2. 严格行首字段边界匹配裁决行（避免误匹配正文中间出现的 '测试结论' 等非裁决字段）
    verdict_lines = re.finditer(
        r"^[ \t]*(?:[-*][ \t]*)?(?:HERDR_GATE_VERDICT|GATE_VERDICT|VERDICT|最终裁决|审查裁决|评审裁决|最终结论|方案审查结论|对抗审查结论|可行性评估结论|结论)[:：][ \t]*(.+)$",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    for vm in verdict_lines:
        line_val = vm.group(1).strip()
        # 阻断检查
        if re.search(r"\b(?:blocked|reject(?:ed)?|fail(?:ed)?)\b|不通过|未通过|阻断|存在阻塞|驳回|暂不予通过|不予通过|不能通过|暂不通过|拒绝", line_val, re.IGNORECASE):
            has_explicit_blocked = True
            blocked_reasons.append(f"verdict line indicates blocked: {line_val}")
        # 条件性通过判定为阻断/未完成
        elif re.search(r"(?:方可通过|方可批准|方可实施|修[复改].*后.*通过|须.*通过|待.*通过)", line_val):
            has_explicit_blocked = True
            blocked_reasons.append(f"conditional approval rejected as blocked: {line_val}")
        # 肯定通过检查 (必须无否定前缀)
        elif re.search(r"\b(?:pass(?:ed)?|approved?)\b|建议通过|确认通过|准予通过|同意实施|通过", line_val, re.IGNORECASE):
            if not re.search(r"(?:不|未|暂不|不能|无|非|不予)通过", line_val):
                has_explicit_pass = True
                pass_reasons.append(f"verdict line indicates pass: {line_val}")

    # 3. 显式短语判决（必须是完整明确短语，严禁在正文模糊匹配单个“通过”）
    # 阻断短语
    for bm in re.finditer(r"(?:方案对抗质询[:：]\s*(?:目前)?(?:不能|不予|暂不)通过|【阻塞】|发现阻塞性缺陷|存在阻塞性缺陷)", text):
        start_pos = bm.start()
        prefix = text[max(0, start_pos - 10):start_pos]
        if not re.search(r"(?:未|无|没有|不曾)\s*$", prefix):
            has_explicit_blocked = True
            blocked_reasons.append("blocking defect phrase detected in body")

    # 通过短语（严格限定完整肯定短语）
    for pm in re.finditer(r"(?:建议通过|确认通过|同意实施|准予通过|审查通过|评估通过|可行性评估通过|未发现阻塞性缺陷|无阻塞性缺陷|(?:没有|不曾)发现阻塞性缺陷)", text):
        start_pos = pm.start()
        prefix = text[max(0, start_pos - 10):start_pos]
        if not re.search(r"(?:不|未|不能|暂不|不予)\s*$", prefix):
            # 排除条件性短语（如 '修复问题后建议通过'）
            surrounding = text[start_pos:start_pos + 30]
            if not re.search(r"(?:方可通过|方可批准|方可实施|修[复改].*后.*通过)", surrounding):
                has_explicit_pass = True
                pass_reasons.append("approval phrase detected in body")

    # 4. 裁决仲裁：阻断信号绝对优先（fail-closed）
    if has_explicit_blocked:
        return "blocked", "; ".join(blocked_reasons)

    if has_explicit_pass:
        return "pass", "; ".join(pass_reasons)

    return "missing", "challenger review missing explicit verdict declaration (PASS/BLOCKED/通过/阻断)"


def find_plan_deliverables(
    workflow_id: str,
    tasks: Optional[List[Dict[str, Any]]] = None,
    project_root: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """寻找本轮计划节点产出的 plan（executor）文本与 challenge（challenger）审查文本。

    具备失效版本过滤（通过 workflow_docs.annotate_notes 排除 stale 产物）和任务身份精确绑定。
    返回 PlanDeliverablesResult，兼容 4 元组解包同时携带 meta 字典。
    """
    plan_text = ""
    challenger_text = ""
    executor_task = None
    challenger_task = None
    selected_plan_note = None
    selected_chal_note = None
    plan_source = "none"

    plan_tasks = [
        t for t in (tasks or [])
        if isinstance(t, dict)
        and (t.get("node") == "plan" or t.get("stage") == "plan")
        and t.get("workflow_id") == workflow_id
    ]

    # 优先筛选当前未被作废的活跃计划任务
    active_plan_tasks = [
        t for t in plan_tasks
        if not t.get("superseded_by") and t.get("status") not in ("superseded", "cancelled", "failed")
    ]
    candidate_tasks = active_plan_tasks if active_plan_tasks else plan_tasks

    for t in candidate_tasks:
        role = " ".join(str(t.get(k) or "").lower() for k in ("dispatch_role", "agent_role", "role", "task_id"))
        if re.search(r"(?:^|[^a-z])(review(?:er)?|adversarial|challenger)(?:$|[^a-z])", role):
            challenger_task = t
        else:
            executor_task = t

    exec_task_id = executor_task.get("task_id") if executor_task else None
    chal_task_id = challenger_task.get("task_id") if challenger_task else None

    # 1. 优先从共享 notes 中读取（权威事实源，应用失效标注 annotate_notes 排除 stale，严格按任务 ID 匹配）
    try:
        raw_notes = workflow_docs.load_notes(workflow_id)
        annotated_notes = workflow_docs.annotate_notes(raw_notes)
        valid_notes = [n for n in annotated_notes if not n.get("stale")]

        # 按时间倒序查找最新有效记录
        for n in reversed(valid_notes):
            n_node = n.get("node")
            n_kind = n.get("kind")
            n_task = n.get("task_id") or n.get("author")
            n_title = str(n.get("title") or "")
            n_body = str(n.get("body") or "")
            combined = f"{n_title}\n{n_body}"

            if not plan_text:
                matches_exec = (n_task == exec_task_id) if exec_task_id else (n_task is None or not exec_task_id)
                if not matches_exec and not n_task and executor_task and executor_task.get("agent") and n.get("agent") == executor_task.get("agent"):
                    matches_exec = True
                if matches_exec:
                    if n_node == "plan" and n_kind in ("plan", "spec"):
                        plan_text = combined
                        selected_plan_note = n
                        plan_source = "notes"
                    elif n_kind == "plan" or ("计划" in n_title and "质询" not in n_title and "审查" not in n_title):
                        plan_text = combined
                        selected_plan_note = n
                        plan_source = "notes"

            if not challenger_text:
                matches_chal = (n_task == chal_task_id) if chal_task_id else (n_task is None or not chal_task_id)
                if not matches_chal and not n_task and challenger_task and challenger_task.get("agent") and n.get("agent") == challenger_task.get("agent"):
                    matches_chal = True
                if matches_chal:
                    if n_node == "plan" and n_kind in ("challenge", "review"):
                        challenger_text = combined
                        selected_chal_note = n
                    elif n_kind == "challenge" or ("质询" in n_title or "对抗" in n_title or "审查" in n_title or "可行性评估" in n_title):
                        challenger_text = combined
                        selected_chal_note = n
    except Exception:
        pass

    # 2. 仅当 notes 缺失时，回退读取任务所属 Clone 目录中绑定的单文件（绝不拼接历史多文件）
    if not plan_text and executor_task and executor_task.get("clone_path"):
        cpath = executor_task["clone_path"]
        fcontent = read_plan_file_from_dir(cpath)
        if fcontent:
            plan_text = fcontent
            plan_source = "clone_file"

    if not challenger_text and challenger_task and challenger_task.get("clone_path"):
        cpath = challenger_task["clone_path"]
        for fname in ("challenger-review.md", "challenge.md", "plan-review.md"):
            fpath = os.path.join(cpath, fname)
            if os.path.isfile(fpath) and os.path.getsize(fpath) > 0:
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        challenger_text = f.read()
                    break
                except Exception:
                    pass

    # 3. 仅当上述均缺失时，回退 project_root 下单一规范文件
    if not plan_text and project_root and os.path.exists(project_root):
        fcontent = read_plan_file_from_dir(project_root)
        if fcontent:
            plan_text = fcontent
            plan_source = "project_root"

    meta = {
        "plan_note": selected_plan_note,
        "challenger_note": selected_chal_note,
        "plan_source": plan_source,
    }
    return PlanDeliverablesResult(plan_text.strip(), challenger_text.strip(), executor_task, challenger_task, meta=meta)


def verify_plan_and_challenger(
    workflow_id: str,
    tasks: Optional[List[Dict[str, Any]]] = None,
    project_root: Optional[str] = None,
    require_challenger: bool = True,
) -> Tuple[bool, str, Dict[str, Any]]:
    """核验计划阶段交付物及对抗审查闭环。

    返回: (ok, reason, details_dict)
    """
    deliverables = find_plan_deliverables(
        workflow_id, tasks, project_root
    )
    plan_text, challenger_text, exec_task, chal_task = deliverables
    meta = getattr(deliverables, "meta", {})

    if not plan_text:
        return (
            False,
            "plan_missing: plan deliverable text is empty or missing from plan node",
            {"missing": "plan_text"},
        )

    # 校验 executor 方案结构完整性
    plan_ok, missing_sections = validate_plan_deliverable_content(plan_text, role="executor")
    if not plan_ok:
        return (
            False,
            f"plan_invalid: plan content missing required sections: {', '.join(missing_sections)}",
            {"missing_sections": missing_sections, "plan_preview": plan_text[:200]},
        )

    # 校验 challenger 审查闭环
    if require_challenger:
        # 优先检查显式阻断（task stage_verdict 或 文本包含 blocked 裁决）
        verdict, verdict_reason = extract_challenger_verdict(challenger_text or "", chal_task)
        if verdict == "blocked":
            return (
                False,
                f"challenger_blocked: {verdict_reason}",
                {"challenger_preview": (challenger_text or "")[:300]},
            )

        if not challenger_text:
            return (
                False,
                "challenger_missing: challenger review deliverable missing; adversarial review closed loop not satisfied",
                {"missing": "challenger_text"},
            )

        chal_ok, chal_missing = validate_plan_deliverable_content(challenger_text, role="challenger")
        if not chal_ok:
            return (
                False,
                f"challenger_invalid: challenger review missing required sections: {', '.join(chal_missing)}",
                {"missing_sections": chal_missing},
            )

        if verdict != "pass":
            return (
                False,
                f"challenger_verdict_missing: {verdict_reason}",
                {"challenger_preview": challenger_text[:300]},
            )

        # 校验审查与计划版本对应关系（防止旧审查放行新修改的计划）
        plan_note = meta.get("plan_note")
        chal_note = meta.get("challenger_note")
        if plan_note and chal_note:
            plan_nid = str(plan_note.get("note_id") or plan_note.get("id") or "").strip()
            plan_ts = float(plan_note.get("ts") or 0.0)
            chal_ts = float(chal_note.get("ts") or 0.0)

            target_ref = str(
                chal_note.get("target_note_id")
                or chal_note.get("plan_note_id")
                or ""
            ).strip()
            if not target_ref:
                ref_m = re.search(
                    r"(?:plan[_-]note[_-]id|plan[_-]ref|target[_-]note[_-]id|被审计划|审查目标|计划版本)[:：]\s*([a-zA-Z0-9_\-]+)",
                    challenger_text or "",
                    re.IGNORECASE,
                )
                if ref_m:
                    target_ref = ref_m.group(1).strip()

            if target_ref and plan_nid and target_ref != plan_nid:
                return (
                    False,
                    f"challenger_plan_version_mismatch: challenger review targets {target_ref} but selected plan is {plan_nid}",
                    {"target_plan": target_ref, "selected_plan": plan_nid},
                )

            if plan_ts > chal_ts > 0.0:
                return (
                    False,
                    f"challenger_outdated: plan was updated (ts={plan_ts}) after challenger review was published (ts={chal_ts}); new plan requires re-review",
                    {"plan_ts": plan_ts, "challenger_ts": chal_ts, "plan_id": plan_nid},
                )

    return (
        True,
        "plan_and_challenger_verified",
        {
            "plan_text": plan_text,
            "challenger_text": challenger_text,
            "executor_task": exec_task,
            "challenger_task": chal_task,
        },
    )


def parse_plan_tasks(
    plan_text: str,
    default_name: str = "实现与自测",
) -> List[Dict[str, Any]]:
    """从计划文档解析具体实现任务条目。"""
    if not plan_text or not plan_text.strip():
        return []

    lines = plan_text.splitlines()
    tasks: List[Dict[str, Any]] = []

    # 查找任务头
    header_indices = []
    seen_task_nums = set()
    has_duplicate_index = False
    duplicate_num = 0

    for idx, line in enumerate(lines):
        m = _TASK_HEADER_RE.match(line.strip())
        if m:
            task_num = int(m.group(1))
            task_title = m.group(2).strip()
            if task_num in seen_task_nums:
                has_duplicate_index = True
                duplicate_num = task_num
            seen_task_nums.add(task_num)
            header_indices.append((idx, task_num, task_title))

    if header_indices:
        # 多任务或显式编号任务
        for i, (start_idx, t_num, t_title) in enumerate(header_indices):
            end_idx = header_indices[i + 1][0] if i + 1 < len(header_indices) else len(lines)
            chunk = "\n".join(lines[start_idx:end_idx])

            files: Set[str] = set()
            for fm in _FILE_LINE_RE.finditer(chunk):
                fpath = clean_file_path(fm.group(1))
                if fpath and is_valid_file_path(fpath):
                    files.add(fpath)

            goal = ""
            gm = _GOAL_LINE_RE.search(chunk)
            if gm:
                goal = gm.group(1).strip()
            else:
                goal = f"完成 {t_title} 实现与验证"

            scope = ""
            sm = _SCOPE_LINE_RE.search(chunk)
            if sm:
                scope = sm.group(1).strip()

            verify_cmd = extract_verify_command(chunk)

            deps: List[str] = []
            for dm in _DEP_LINE_RE.finditer(chunk):
                raw_dep = dm.group(1).strip()
                cleaned_dep = re.sub(r"[`'\"*]", "", raw_dep).strip()
                if cleaned_dep.lower() in ("", "none", "n/a", "na", "null", "nil", "无", "无依赖", "无内部依赖", "-"):
                    continue

                matched_other = False
                for other_idx, other_num, other_title in header_indices:
                    if other_num != t_num:
                        pat = rf"(?:Task|任务)\s*{other_num}\b"
                        if re.search(pat, raw_dep, re.IGNORECASE):
                            deps.append(f"Task {other_num}")
                            matched_other = True

                # 若非空且未匹配到任务编号，仍作为未识别依赖记录，由解耦校验器判定
                if not matched_other and cleaned_dep:
                    deps.append(cleaned_dep)

            tasks.append({
                "task_index": t_num,
                "name": t_title,
                "goal": goal,
                "scope": scope,
                "files": sorted(files),
                "verify_command": verify_cmd,
                "dependencies": deps,
                "raw_chunk": chunk,
                "duplicate_index": has_duplicate_index and t_num == duplicate_num,
            })
    else:
        # 单任务计划（无显式 ### Task N 标题，解析全篇）
        files = set()
        for fm in _FILE_LINE_RE.finditer(plan_text):
            fpath = clean_file_path(fm.group(1))
            if fpath and is_valid_file_path(fpath):
                files.add(fpath)

        verify_cmd = extract_verify_command(plan_text)

        title = default_name
        for line in lines:
            line_str = line.strip()
            if line_str.startswith("# ") and not line_str.lower().startswith("# implementation plan"):
                title = line_str.lstrip("#").strip()
                break

        tasks.append({
            "task_index": 1,
            "name": title,
            "goal": f"按计划完成 {title} 代码修改与自测",
            "scope": "满足计划定义的验收条目与文件范围",
            "files": sorted(files),
            "verify_command": verify_cmd,
            "dependencies": [],
            "raw_chunk": plan_text,
            "duplicate_index": False,
        })

    return tasks


def validate_task_decoupling(
    tasks: List[Dict[str, Any]],
    max_concurrency: int = 3,
    max_tasks_per_node: int = 12,
) -> Tuple[bool, str]:
    """校验任务的并发解耦条件与预算约束。

    规则：
    1. 任务数必须 >= 1且 <= max_tasks_per_node；
    2. 并发数 <= max_concurrency；
    3. 严禁重复任务编号（如多个 Task 1）；
    4. 每个任务必须包含明确的文件范围和验证命令；
    5. 多任务并发时：各任务必须完全解耦，文件范围无交集（规范化路径判定）；
    6. 严禁节点内未支持的串行 DAG 依赖或未识别的前置依赖声明。
    """
    if not tasks:
        return False, "plan contains no parsed implementation tasks"

    if len(tasks) > max_tasks_per_node:
        return False, f"parsed task count {len(tasks)} exceeds max_tasks_per_node budget {max_tasks_per_node}"

    if len(tasks) > max_concurrency:
        return False, f"parallel task count {len(tasks)} exceeds max_concurrency limit {max_concurrency}"

    # 检查重复任务编号
    seen_task_nums: Set[int] = set()
    for t in tasks:
        t_num = t.get("task_index", 0)
        if t.get("duplicate_index") or t_num in seen_task_nums:
            return False, f"duplicate task index 'Task {t_num}' detected in plan"
        seen_task_nums.add(t_num)

    # 单任务校验
    if len(tasks) == 1:
        t = tasks[0]
        if not t.get("files") and not t.get("verify_command"):
            return False, "single implementation task has no specified files or verification command"
        return True, "single task valid"

    # 多任务并发解耦核验
    seen_files: Dict[str, int] = {}
    for t in tasks:
        t_num = t.get("task_index", 0)
        t_name = t.get("name", f"Task {t_num}")

        # 逐任务检查必要字段（多任务每个任务必须有明确文件和验证命令）
        if not t.get("files"):
            return False, f"task {t_name} has no specified file scope (Create/Modify/Test files required)"
        if not t.get("verify_command"):
            return False, f"task {t_name} has no specified verification command"

        # 检查内部或非空依赖声明
        deps = t.get("dependencies") or []
        if deps:
            return (
                False,
                f"unsupported intra-node dependency declared in {t_name}: depends on {', '.join(deps)}. "
                "Intra-node DAG is not supported; tasks must be independent and decoupled, or unified into a single task."
            )

        # 检查文件重叠（经过 clean_file_path 规范化处理）
        for f in t.get("files", []):
            norm_f = clean_file_path(f)
            if norm_f in seen_files:
                prior_task = seen_files[norm_f]
                return (
                    False,
                    f"overlapping file '{norm_f}' detected between Task {prior_task} and Task {t_num}. "
                    "Decoupled concurrent tasks must have disjoint file sets to prevent Git merge conflicts."
                )
            seen_files[norm_f] = t_num

    return True, "decoupled tasks valid"
