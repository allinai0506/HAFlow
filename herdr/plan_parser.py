"""解析计划阶段成果并转化为结构化实现任务规格。

核心原则：
1. 语义规划留在计划节点：由 executor 起草技术方案与任务拆解，challenger 进行破防质询与可行性评估；
2. 审查闭环验证：核实 challenger 审查的是本轮有效计划，且无未解决的阻塞性缺陷（未通过/blocked）；
3. 严格任务解耦：支持单任务与符合解耦条件的多任务（无文件重叠、无不支持的内部依赖、受并发与预算约束）；
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
_VERIFY_CMD_RE = re.compile(
    r"(?:(?:验证命令|自测命令|Test command|Verify)[:：]?\s*[`'\"]?([^\n`'\"]+)[`'\"]?|```(?:bash|sh|shell)?\n\s*([^\n]+(?:pytest|python3|npm|pnpm|cargo|go|make)[^\n]*)\n```)",
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
_CHALLENGER_BLOCKED_RE = re.compile(
    r"(?:HERDR_GATE_VERDICT:\s*blocked|VERDICT:\s*BLOCKED|结论[:：]\s*(?:不通过|阻断|blocked)|【阻塞】|发现阻塞性缺陷)",
    re.IGNORECASE,
)


def clean_file_path(raw: str) -> str:
    """清理文件路径中的行号后缀、注释或引号。"""
    cleaned = str(raw or "").strip().strip("`'\"")
    # 去除尾部可能带有的行号范围如 :123-145
    cleaned = re.sub(r":\d+(?:-\d+)?$", "", cleaned).strip()
    return cleaned


def find_plan_deliverables(
    workflow_id: str,
    tasks: Optional[List[Dict[str, Any]]] = None,
    project_root: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """寻找本轮计划节点产出的 plan（executor）文本与 challenge（challenger）审查文本。

    返回: (plan_text, challenger_text, executor_task, challenger_task)
    """
    plan_text = ""
    challenger_text = ""
    executor_task = None
    challenger_task = None

    plan_tasks = [
        t for t in (tasks or [])
        if isinstance(t, dict)
        and (t.get("node") == "plan" or t.get("stage") == "plan")
        and t.get("workflow_id") == workflow_id
    ]

    for t in plan_tasks:
        role = " ".join(str(t.get(k) or "").lower() for k in ("dispatch_role", "agent_role", "role", "task_id"))
        if re.search(r"(?:^|[^a-z])(review(?:er)?|adversarial|challenger)(?:$|[^a-z])", role):
            challenger_task = t
        else:
            executor_task = t

    # 1. 优先读取任务所属 Clone 目录
    if executor_task and executor_task.get("clone_path"):
        cpath = executor_task["clone_path"]
        for fname in ("plan.md", "docs/plan.md", "docs/superpowers/plans"):
            fpath = os.path.join(cpath, fname)
            if os.path.isdir(fpath):
                for pf in sorted(os.listdir(fpath)):
                    if pf.endswith(".md"):
                        try:
                            with open(os.path.join(fpath, pf), "r", encoding="utf-8") as f:
                                plan_text += "\n" + f.read()
                        except Exception:
                            pass
            elif os.path.exists(fpath) and os.path.getsize(fpath) > 0:
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        plan_text = f.read()
                    break
                except Exception:
                    pass

    if challenger_task and challenger_task.get("clone_path"):
        cpath = challenger_task["clone_path"]
        for fname in ("challenger-review.md", "challenge.md", "plan-review.md"):
            fpath = os.path.join(cpath, fname)
            if os.path.exists(fpath) and os.path.getsize(fpath) > 0:
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        challenger_text = f.read()
                    break
                except Exception:
                    pass

    # 2. 从共享 notes 中读取
    try:
        notes = workflow_docs.load_notes(workflow_id)
        for n in notes:
            if n.get("stale"):
                continue
            n_node = n.get("node")
            n_kind = n.get("kind")
            n_title = str(n.get("title") or "")
            n_body = str(n.get("body") or "")
            combined = f"{n_title}\n{n_body}"

            if not plan_text:
                if n_node == "plan" and n_kind in ("plan", "spec"):
                    plan_text = combined
                elif n_kind == "plan" or ("计划" in n_title and "质询" not in n_title and "审查" not in n_title):
                    plan_text = combined

            if not challenger_text:
                if n_node == "plan" and n_kind in ("challenge", "review"):
                    challenger_text = combined
                elif n_kind == "challenge" or ("质询" in n_title or "对抗" in n_title or "可行性评估" in n_title):
                    challenger_text = combined
    except Exception:
        pass

    # 3. 回退 project_root 下的标准路径
    if not plan_text and project_root and os.path.exists(project_root):
        for fname in ("docs/plan.md", "plan.md", "docs/superpowers/plans"):
            fpath = os.path.join(project_root, fname)
            if os.path.isdir(fpath):
                for pf in sorted(os.listdir(fpath)):
                    if pf.endswith(".md"):
                        try:
                            with open(os.path.join(fpath, pf), "r", encoding="utf-8") as f:
                                plan_text += "\n" + f.read()
                        except Exception:
                            pass
            elif os.path.exists(fpath) and os.path.getsize(fpath) > 0:
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        plan_text = f.read()
                    break
                except Exception:
                    pass

    return plan_text.strip(), challenger_text.strip(), executor_task, challenger_task


def verify_plan_and_challenger(
    workflow_id: str,
    tasks: Optional[List[Dict[str, Any]]] = None,
    project_root: Optional[str] = None,
    require_challenger: bool = True,
) -> Tuple[bool, str, Dict[str, Any]]:
    """核验计划阶段交付物及对抗审查闭环。

    返回: (ok, reason, details_dict)
    """
    plan_text, challenger_text, exec_task, chal_task = find_plan_deliverables(
        workflow_id, tasks, project_root
    )

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
        if chal_task and chal_task.get("stage_verdict") == "blocked":
            return (
                False,
                f"challenger_blocked: challenger task {chal_task.get('task_id')} stage_verdict is blocked: {chal_task.get('stage_verdict_note')}",
                {"challenger_task": chal_task.get("task_id"), "verdict": "blocked"},
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

        # 检查 challenger 文本中是否包含未解决的阻断结论
        if _CHALLENGER_BLOCKED_RE.search(challenger_text):
            return (
                False,
                "challenger_blocked: challenger review explicitly concluded with blocked verdict or unaddressed blocking issues",
                {"challenger_preview": challenger_text[:300]},
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
    for idx, line in enumerate(lines):
        m = _TASK_HEADER_RE.match(line.strip())
        if m:
            task_num = int(m.group(1))
            task_title = m.group(2).strip()
            header_indices.append((idx, task_num, task_title))

    if header_indices:
        # 多任务或显式编号任务
        for i, (start_idx, t_num, t_title) in enumerate(header_indices):
            end_idx = header_indices[i + 1][0] if i + 1 < len(header_indices) else len(lines)
            chunk = "\n".join(lines[start_idx:end_idx])

            files: Set[str] = set()
            for fm in _FILE_LINE_RE.finditer(chunk):
                fpath = clean_file_path(fm.group(1))
                if fpath and not fpath.startswith("http") and "." in fpath:
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

            verify_cmd = ""
            vm = _VERIFY_CMD_RE.search(chunk)
            if vm:
                verify_cmd = (vm.group(1) or vm.group(2) or "").strip()

            deps: List[str] = []
            for dm in _DEP_LINE_RE.finditer(chunk):
                dep_text = dm.group(1).strip()
                # 检查是否依赖了同阶段其他任务
                for other_idx, other_num, other_title in header_indices:
                    if other_num != t_num:
                        pat = rf"(?:Task|任务)\s*{other_num}\b"
                        if re.search(pat, dep_text, re.IGNORECASE):
                            deps.append(f"Task {other_num}")

            tasks.append({
                "task_index": t_num,
                "name": t_title,
                "goal": goal,
                "scope": scope,
                "files": sorted(files),
                "verify_command": verify_cmd,
                "dependencies": deps,
                "raw_chunk": chunk,
            })
    else:
        # 单任务计划（无显式 ### Task N 标题，解析全篇）
        files = set()
        for fm in _FILE_LINE_RE.finditer(plan_text):
            fpath = clean_file_path(fm.group(1))
            if fpath and not fpath.startswith("http") and "." in fpath:
                files.add(fpath)

        verify_cmd = ""
        vm = _VERIFY_CMD_RE.search(plan_text)
        if vm:
            verify_cmd = (vm.group(1) or vm.group(2) or "").strip()

        # 尝试提取标题
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
    3. 多任务并发时：各任务必须完全解耦，文件范围无交集；
    4. 严禁节点内未支持的串行 DAG 依赖（如 Task 2 依赖 Task 1 产出）；
    5. 每个任务必须包含明确的文件或验证命令。
    """
    if not tasks:
        return False, "plan contains no parsed implementation tasks"

    if len(tasks) > max_tasks_per_node:
        return False, f"parsed task count {len(tasks)} exceeds max_tasks_per_node budget {max_tasks_per_node}"

    if len(tasks) > max_concurrency:
        return False, f"parallel task count {len(tasks)} exceeds max_concurrency limit {max_concurrency}"

    # 单任务天然无内部依赖和文件冲突
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

        # 检查内部依赖声明
        deps = t.get("dependencies") or []
        if deps:
            return (
                False,
                f"unsupported intra-node dependency declared in {t_name}: depends on {', '.join(deps)}. "
                "Intra-node DAG is not supported; tasks must be independent and decoupled, or unified into a single task."
            )

        # 检查文件重叠
        for f in t.get("files", []):
            if f in seen_files:
                prior_task = seen_files[f]
                return (
                    False,
                    f"overlapping file '{f}' detected between Task {prior_task} and Task {t_num}. "
                    "Decoupled concurrent tasks must have disjoint file sets to prevent Git merge conflicts."
                )
            seen_files[f] = t_num

    return True, "decoupled tasks valid"
