"""HAFlow Stage Skills Assembly and Unified Dev Flow Engineering Adapter.

Responsibilities:
1. Resolve and validate stage-bound skills from local repository, universal-skills,
   or user environments. Detect missing skills or unexecutable scripts as explicit capability gaps.
2. Assemble stage execution context: identity, role, bound skills, upstream deliverable references,
   mandatory outputs, stage exit conditions, and delivery ceiling.
3. Link/stage skills into the task workstation clone so agents have immediate access.
4. Provide planning orchestrator rules ensuring plans hand off to HAFlow rather than SDD/executing-plans.
5. Provide authoritative database-backed stage identity verification for Stop Hook enforcement.
6. Validate structured deliverables for requirements, plan, implementation, and test stages.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


def _get_real_home() -> Path:
    """Resolve the real user home directory, robust against test fixture HOME overrides."""
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return Path.home()


# Pre-defined skill bindings for the fixed six nodes of software-development-v1
STAGE_SKILL_BINDINGS: Dict[str, List[str]] = {
    "requirements": ["brainstorming", "parse-prd", "domain-modeling"],
    "plan": ["writing-plans", "plan-eng-review", "architect"],
    "implementation": ["test-driven-development", "ai-slop-cleaner"],
    "test": ["verification-before-completion", "e2e-testing", "self-verifying-tests", "acceptance-evidence-gate"],
    "review": ["google-code-review", "health", "code-review"],
    "wrapup": ["six-step-finish", "knowledge-capture"],
}

DEFAULT_UNIVERSAL_SKILLS_PATH = Path(
    os.environ.get("UNIVERSAL_SKILLS_PATH") or str(_get_real_home() / "universal-skills")
)


class SkillCapabilityGap(RuntimeError):
    """Raised when a required stage skill is missing, unreadable, or its tools are unexecutable."""
    pass


def is_staged_dev_flow_workflow(workflow_config: Any) -> bool:
    """Check if the given workflow configuration belongs to software-development-v1 / staged dev flow."""
    if not isinstance(workflow_config, dict):
        return False
    name = str(workflow_config.get("name") or workflow_config.get("workflow_id") or "").strip()
    if name == "software-development-v1" or name.startswith("software-development-v1"):
        return True
    if workflow_config.get("staged_dev_flow"):
        return True
    return False


def get_default_skill_search_roots() -> List[Path]:
    """Default candidate search directories for stage skills."""
    roots: List[Path] = []

    # 1. Current repository skills
    repo_agents_skills = Path(__file__).resolve().parent.parent / ".agents" / "skills"
    if repo_agents_skills.exists():
        roots.append(repo_agents_skills)

    # 2. Universal skills categories
    if DEFAULT_UNIVERSAL_SKILLS_PATH.exists():
        cats = DEFAULT_UNIVERSAL_SKILLS_PATH / "categories"
        if cats.exists():
            for sub in cats.iterdir():
                if sub.is_dir() and not sub.name.startswith("."):
                    roots.append(sub)

    # 3. User agent skills
    homes = {Path.home(), _get_real_home()}
    for h in homes:
        for p in (
            h / ".claude" / "skills",
            h / ".gemini" / "config" / "skills",
            h / ".codex" / "skills",
            h / ".agents" / "skills",
        ):
            if p.exists() and p not in roots:
                roots.append(p)

    return roots


def resolve_stage_skill(
    skill_name: str,
    search_roots: Optional[List[Any]] = None,
    required_scripts: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Resolve a skill by name, verifying SKILL.md and executable scripts."""
    name = str(skill_name or "").strip()
    if not name:
        raise SkillCapabilityGap("Skill name cannot be empty")

    roots = [Path(r) for r in search_roots] if search_roots else get_default_skill_search_roots()
    found_dir: Optional[Path] = None

    for root in roots:
        candidate = root / name
        if candidate.is_dir() and (candidate / "SKILL.md").is_file():
            found_dir = candidate
            break

    if not found_dir:
        raise SkillCapabilityGap(
            f"Stage skill '{name}' not found across search paths: {[str(r) for r in roots]}"
        )

    skill_file = found_dir / "SKILL.md"
    try:
        content = skill_file.read_text(encoding="utf-8")
    except Exception as exc:
        raise SkillCapabilityGap(f"Stage skill '{name}' SKILL.md is unreadable: {exc}") from exc

    # Parse version / metadata if available in frontmatter
    version = ""
    description = ""
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            fm = parts[1]
            for line in fm.splitlines():
                if line.startswith("version:"):
                    version = line.split(":", 1)[1].strip().strip('"\'')
                elif line.startswith("description:"):
                    description = line.split(":", 1)[1].strip().strip('"\'')

    # Validate required scripts
    validated_scripts: List[str] = []
    scripts_to_check = list(required_scripts or [])

    # Specific known skill script requirements
    if name == "acceptance-evidence-gate" and "scripts/acceptance_gate.py" not in scripts_to_check:
        scripts_to_check.append("scripts/acceptance_gate.py")
    elif name == "six-step-finish" and "scripts/finish-task.sh" not in scripts_to_check:
        scripts_to_check.append("scripts/finish-task.sh")

    for rel_script in scripts_to_check:
        script_path = found_dir / rel_script
        if not script_path.exists():
            raise SkillCapabilityGap(
                f"Stage skill '{name}' is missing required script: {rel_script} (expected at {script_path})"
            )
        # Check executable permissions
        if not os.access(script_path, os.X_OK):
            # Attempt to check if python script can be invoked or if executable bit is set
            raise SkillCapabilityGap(
                f"Stage skill '{name}' script is not executable: {rel_script} ({script_path})"
            )
        validated_scripts.append(str(script_path))

    return {
        "name": name,
        "skill_dir": str(found_dir),
        "skill_file": str(skill_file),
        "version": version or "unversioned",
        "description": description,
        "scripts": validated_scripts,
    }


def stage_skills_for_node(node_id: str, configured_skills: Optional[List[str]] = None) -> List[str]:
    """Get the skill names for a node, using template configured skills if provided, falling back to defaults."""
    if configured_skills:
        return [str(s).strip() for s in configured_skills if str(s).strip()]
    return list(STAGE_SKILL_BINDINGS.get(node_id, []))


def stage_skills_into_workspace(clone_path: str | Path, skill_names: List[str]) -> List[Dict[str, Any]]:
    """Link or copy resolved skills into <clone_path>/.agents/skills/<name> so agents have local workspace access."""
    clone = Path(clone_path)
    if not clone.exists() or not clone.is_dir():
        return []

    target_skills_dir = clone / ".agents" / "skills"
    target_skills_dir.mkdir(parents=True, exist_ok=True)

    resolved_skills: List[Dict[str, Any]] = []
    for skill_name in skill_names:
        resolved = resolve_stage_skill(skill_name)
        resolved_skills.append(resolved)
        skill_src = Path(resolved["skill_dir"])
        skill_dst = target_skills_dir / skill_name
        if os.path.islink(skill_dst) or os.path.lexists(skill_dst):
            try:
                if skill_dst.resolve() == skill_src.resolve():
                    continue
                os.unlink(skill_dst)
            except OSError:
                pass
        if not skill_dst.exists():
            try:
                os.symlink(skill_src, skill_dst)
            except OSError:
                # Fallback to directory copy if symlinks fail (e.g. cross-volume on Windows)
                import shutil
                shutil.copytree(skill_src, skill_dst)

    return resolved_skills


def render_planning_orchestrator_rules() -> str:
    """Explicit planning orchestrator rules forbidding secondary execution loops."""
    return """\
【HAFlow 规划交接契约 - Orchestrator Handoff Protocol】
1. HAFlow 是当前流程的唯一主控系统，直接管理后续实现、测试、评审与收尾节点；
2. 计划产出后直接作为 plan 节点交付物，交回 HAFlow；
3. 禁止在计划文档头部添加 REQUIRED SUB-SKILL（如 subagent-driven-development 或 executing-plans）声明；
4. 禁止询问用户选择 subagent 还是 inline 执行器；
5. 禁止在本节点启动实现或另开工作区；
6. 计划必须明确：每项验收条目对应的实现任务、文件范围（Create/Modify/Test）、具体的验证命令及预期结果。\
"""


def assemble_stage_context(
    workflow_id: str,
    task_id: str,
    run_id: str,
    node_id: str,
    dispatch_role: str = "worker",
    candidate_sha: str = "",
    upstream_notes: Optional[List[Dict[str, Any]]] = None,
    delivery_ceiling: str = "workspace",
    configured_skills: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Assemble structured stage execution context for a software-development-v1 node."""
    skill_names = stage_skills_for_node(node_id, configured_skills)
    resolved_skills: List[Dict[str, Any]] = []
    for name in skill_names:
        try:
            resolved_skills.append(resolve_stage_skill(name))
        except SkillCapabilityGap:
            raise

    # Responsibilities and allowed operations by node
    responsibilities: Dict[str, str] = {
        "requirements": "澄清需求边界与验收标准，产出结构化需求规格（包含问题/范围/编号验收条目/证明方式/交付终点）。默认不得修改业务代码。",
        "plan": "承接需求规格，按 writing-plans 制定可执行任务拆解。executor 起草计划，challenger 对抗质询。不得修改业务代码，不得在头部声明下游启动 SDD/executing-plans。",
        "implementation": "承接计划执行真实代码修改与自测。实现前确认失败证明，完成修改后执行 ai-slop-cleaner 删除式清理并复验。交付候选代码，不执行下游独立评审与发布。",
        "test": "针对冻结 candidate_sha 执行真实验证，运行计划中的测试命令并记录完整退出码与日志。有缺陷输出明确缺陷清单，不得随意修改业务代码。",
        "review": "在 test 完成后独立评审需求、计划、当前代码差异及 test 阶段的新鲜测试证据。有阻塞缺陷输出 blocked 并给出明确修复要求，不得直接修改代码。",
        "wrapup": "汇总全流程交付成果，核对 test 与 review 对同一候选的有效结论。停在授权交付上限（workspace 交付免 PR 审核回执；PR 交付执行 PR 审核闭环；绝不自动合并或部署）。",
    }

    exit_conditions: Dict[str, str] = {
        "requirements": "需求规格与验收标准落盘并记录。退出只要求需求阶段结果，不要求下游工件。",
        "plan": "技术方案与任务拆解完备，质询通过并落盘。退出只要求计划阶段结果，直接交回 HAFlow，不启动二次执行器。",
        "implementation": "S4 计划改动实现完毕，完成 ai-slop-cleaner 清理并通过自测。退出只要求 S4 代码与自测记录，不要求下游独立评审报告（S5/S6 由下游独立节点负责）。",
        "test": "完成针对冻结 candidate_sha 的新鲜测试命令执行，记录命令/退出码/日志并写入门禁结论文件。退出不要求 S6 评审报告。",
        "review": "对需求、代码差异及本轮测试证据完成独立评审，写入门禁评审结论文件。",
        "wrapup": "完成收尾报告与六步状态表，履行授权交付上限，归档知识沉淀。",
    }

    resp = responsibilities.get(node_id, "按节点定义执行职责。")
    exit_cond = exit_conditions.get(node_id, "产物完备并落盘。")

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "【HAFlow 阶段执行契约 - Stage Execution Context】",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"流程身份: workflow={workflow_id} | run={run_id} | task={task_id} | node={node_id} | role={dispatch_role}",
        f"阶段职责: {resp}",
        f"交付授权: {delivery_ceiling}（不得超越此上限）",
    ]

    if candidate_sha:
        lines.append(f"冻结候选: {candidate_sha}（必须针对此版本进行操作或验证，禁止擅自切换）")

    # Skills block
    lines.append("\n【绑定的阶段技能】")
    for s in resolved_skills:
        lines.append(f"- {s['name']} (version: {s['version']}, path: {s['skill_file']})")
        if s.get("scripts"):
            for sc in s["scripts"]:
                lines.append(f"  * 可执行工具: {sc}")

    # Specific orchestrator rules for plan node
    if node_id == "plan":
        lines.append("\n" + render_planning_orchestrator_rules())

    # Upstream deliverables
    if upstream_notes:
        lines.append("\n【上游交付物与证据引用】")
        for note in upstream_notes:
            kind = note.get("kind", "note")
            node = note.get("node", "-")
            title = note.get("title", "(无标题)")
            task = note.get("task_id", "")
            base = note.get("base_sha", "")
            body = note.get("body", "")
            lines.append(f"- [{kind}][{node}] {title} (task={task}, base={base})")
            if body:
                preview = body.strip().splitlines()
                lines.append("  " + "\n  ".join(preview[:5]))
                if len(preview) > 5:
                    lines.append("  ...")

    # Stage exit separation
    lines.append(f"\n【本阶段退出条件】\n{exit_cond}")
    lines.append("【阶段隔离红线】本工位仅履行本阶段职责；下游节点的独立测试与评审由后续工位承接，严禁在本工位越权伪造下游结论。")

    prompt_block = "\n".join(lines)
    return {
        "workflow_id": workflow_id,
        "task_id": task_id,
        "run_id": run_id,
        "node_id": node_id,
        "dispatch_role": dispatch_role,
        "delivery_ceiling": delivery_ceiling,
        "bound_skills": resolved_skills,
        "prompt_block": prompt_block,
    }


def check_stage_stop_hook_eligibility(
    project_dir: str | Path,
    db_path: Optional[str | Path] = None,
) -> Tuple[bool, str, Optional[str]]:
    """Determine whether the current directory belongs to an active HAFlow stage task.

    Returns: (is_eligible, reason, stage_node)
    If eligible:
      - requirements / plan: only requires stage outputs
      - implementation: S4 exit, owes planned change & self-test, exempt from S5/S6
      - test: S5 exit, owes test evidence, exempt from S6
      - review: S6 exit
      - wrapup: S7/S8 exit
    If not eligible:
      - full strict Stop hook check applies.
    """
    real_dir = os.path.realpath(str(project_dir))
    resolved_db = Path(db_path) if db_path else Path.home() / ".herdr-controller" / "state.db"

    if not resolved_db.exists():
        return False, "HAFlow state database not found", None

    try:
        conn = sqlite3.connect(f"file:{resolved_db}?mode=ro", uri=True)
        cursor = conn.cursor()
        # Query active tasks
        cursor.execute(
            """
            SELECT t.task_id, t.workflow_id, t.node, t.stage, t.status, t.payload_json, w.config_json
            FROM tasks t
            JOIN workflows w ON t.workflow_id = w.workflow_id
            WHERE t.status IN ('working', 'dispatched', 'rework', 'agent_done')
            """
        )
        rows = cursor.fetchall()
        conn.close()
    except Exception as exc:
        return False, f"Failed to query state database: {exc}", None

    matched_task = None
    matched_config = None
    for task_id, wf_id, node, stage, status, payload_raw, config_raw in rows:
        try:
            payload = json.loads(payload_raw or "{}")
        except Exception:
            payload = {}
        task_clone = os.path.realpath(str(payload.get("clone_path") or ""))
        if task_clone and task_clone == real_dir:
            matched_task = {
                "task_id": task_id,
                "workflow_id": wf_id,
                "node": node or stage,
                "status": status,
            }
            try:
                matched_config = json.loads(config_raw or "{}")
            except Exception:
                matched_config = {}
            break

    if not matched_task:
        return False, f"Directory {real_dir} does not match any active HAFlow task clone", None

    if not is_staged_dev_flow_workflow(matched_config):
        return False, "Active task does not belong to a staged dev flow workflow", None

    stage_node = matched_task["node"]
    if stage_node == "implementation":
        return True, "HAFlow stage task: S4 implementation (downstream S5/S6 handled by test/review nodes)", stage_node
    elif stage_node in ("requirements", "plan"):
        return True, f"HAFlow stage task: {stage_node} (pure specification stage)", stage_node
    elif stage_node == "test":
        return True, "HAFlow stage task: S5 test (downstream S6 handled by review node)", stage_node
    elif stage_node == "review":
        return True, "HAFlow stage task: S6 review", stage_node
    elif stage_node == "wrapup":
        return True, "HAFlow stage task: S7/S8 wrapup", stage_node

    return True, f"HAFlow stage task: {stage_node}", stage_node


# Deliverable Content Validation Helpers
_REQ_GOAL_RE = re.compile(r"(用户目标|需求目标|需求背景|问题与假设|目标与范围)", re.IGNORECASE)
_REQ_SCOPE_RE = re.compile(r"(功能范围|需求范围|非目标|业务规则|边界)", re.IGNORECASE)
_REQ_AC_RE = re.compile(r"(验收标准|验收条目|AC-\d+|验收方案)", re.IGNORECASE)
_REQ_PROOF_RE = re.compile(r"(证明方式|验证计划|proof_mode|验证命令)", re.IGNORECASE)
_REQ_CEILING_RE = re.compile(r"(交付终点|交付上限|delivery_ceiling)", re.IGNORECASE)


def validate_requirements_deliverable_content(text: str) -> Tuple[bool, List[str]]:
    """Validate that requirements output contains necessary structural sections."""
    if not text or len(text.strip()) < 100:
        return False, ["内容过短或为空（至少需 100 字符）"]

    missing: List[str] = []
    if not _REQ_GOAL_RE.search(text):
        missing.append("缺少用户目标与需求背景")
    if not _REQ_SCOPE_RE.search(text):
        missing.append("缺少功能范围或非目标说明")
    if not _REQ_AC_RE.search(text):
        missing.append("缺少明确可验收标准或编号条目（如 AC-1）")
    if not _REQ_PROOF_RE.search(text):
        missing.append("缺少证明方式或验证计划")
    if not _REQ_CEILING_RE.search(text):
        missing.append("缺少交付上限说明（如 workspace / PR）")

    return (len(missing) == 0, missing)


_PLAN_TASKS_RE = re.compile(r"(任务拆分|解耦任务|Task \d+|任务清单|验收条目映射)", re.IGNORECASE)
_PLAN_FILES_RE = re.compile(r"(文件范围|修改文件|Create:|Modify:|Test:)", re.IGNORECASE)
_PLAN_VERIFY_RE = re.compile(r"(验证命令|自测命令|pytest|python3|bash|npm|pnpm)", re.IGNORECASE)
_PLAN_CHALLENGER_RE = re.compile(r"(方案对抗|质询|可行性评估|风险与验证|死锁|过度设计)", re.IGNORECASE)


def validate_plan_deliverable_content(text: str, role: str = "executor") -> Tuple[bool, List[str]]:
    """Validate that plan output contains necessary structural sections."""
    if not text or len(text.strip()) < 100:
        return False, ["内容过短或为空（至少需 100 字符）"]

    missing: List[str] = []
    if role == "challenger":
        if not _PLAN_CHALLENGER_RE.search(text):
            missing.append("缺少方案对抗性质询或可行性风险评估记录")
        return (len(missing) == 0, missing)

    if not _PLAN_TASKS_RE.search(text):
        missing.append("缺少解耦任务拆分与验收条目映射")
    if not _PLAN_FILES_RE.search(text):
        missing.append("缺少具体修改文件范围清单")
    if not _PLAN_VERIFY_RE.search(text):
        missing.append("缺少明确验证命令或测试执行方案")

    return (len(missing) == 0, missing)


def validate_test_evidence_freshness(
    candidate_sha: str,
    test_sha: str,
    verdict: str,
    has_commands_evidence: bool,
) -> Tuple[bool, str]:
    """Verify test evidence candidate SHA matches and real command evidence exists."""
    c_sha = str(candidate_sha or "").strip()
    t_sha = str(test_sha or "").strip()

    if not c_sha or not t_sha:
        return False, "Candidate SHA missing from verification assertion"

    if c_sha != t_sha:
        return False, f"Candidate SHA mismatch: expected {c_sha}, tested {t_sha}"

    if verdict not in ("pass", "blocked"):
        return False, f"Invalid test verdict: {verdict}"

    if not has_commands_evidence:
        return False, "Missing command execution records in test evidence"

    return True, "Valid test evidence"
