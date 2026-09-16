"""Telemetry Projection Engine (herdr/projection.py).

Cleanses raw terminal streams, extracts structured 4D telemetries
(Intent, Milestones, Artifacts First-Class Citizens, Blockers),
and produces high signal-to-noise white-box briefings for human operators.
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ANSI_REGEX = re.compile(
    r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\].*?(?:\x07|\x1b\\))"
)


def strip_ansi_codes(text: str) -> str:
    """Remove ANSI escape sequences, control codes, and normalize linebreaks."""
    if not text:
        return ""
    clean = ANSI_REGEX.sub("", text)
    clean = clean.replace("\r\n", "\n").replace("\r", "\n")
    # Clean terminal bell and non-printable control characters (except newline, tab)
    clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", clean)
    return clean


def _read_pane_content(pane_id: str) -> str:
    """Read visible content from Herdr pane."""
    if not pane_id:
        return ""
    try:
        r = subprocess.run(
            ["herdr", "pane", "read", pane_id, "--source", "visible"],
            text=True,
            capture_output=True,
            timeout=5,
        )
        return (r.stdout or "") + "\n" + (r.stderr or "")
    except Exception:
        return ""


def get_tasks_file() -> Path:
    p = os.environ.get("TASKS_FILE")
    if p:
        return Path(p)
    return Path.home() / ".herdr-controller" / "tasks.json"


def get_workflows_file() -> Path:
    p = os.environ.get("WORKFLOWS_FILE")
    if p:
        return Path(p)
    return Path.home() / ".herdr-controller" / "workflows.json"


def load_tasks_data() -> Dict[str, Any]:
    from .kernel import load_tasks_data as _kernel_load_tasks
    return _kernel_load_tasks()


def load_workflows_data() -> Dict[str, Any]:
    from .kernel import load_workflows_data as _kernel_load_workflows
    return _kernel_load_workflows()


def extract_task_intent(task: Dict[str, Any], terminal_text: str) -> str:
    """Extract current intent from terminal text or fallback to task goal."""
    clean = strip_ansi_codes(terminal_text)
    
    # 1. Check for explicit [HERDR_INTENT] or [INTENT] markers
    intent_match = re.search(r"\[(?:HERDR_)?INTENT\][:\s]*(.+)", clean, re.IGNORECASE)
    if intent_match:
        return intent_match.group(1).strip()

    # 2. Fallback to task goal as primary intent
    goal = task.get("goal")
    if goal:
        return str(goal).strip()

    # 3. Check for recent running actions in terminal
    action_match = re.search(r"(?:Running|Executing|Testing|Building|Compiling)\s+([^\n\r]+)", clean)
    if action_match:
        return f"正在执行: {action_match.group(0).strip()}"

    return f"{task.get('node_label') or task.get('node') or '工位执行'} 进行中"


def extract_task_milestones(task: Dict[str, Any], terminal_text: str) -> List[Dict[str, Any]]:
    """Extract ordered milestones and their statuses."""
    status = task.get("status")
    clone_path = Path(task.get("clone_path") or "")
    loop_dir = clone_path / ".herdr-loop"

    milestones = [
        {"label": "锁定验收目标与环境契约", "status": "completed" if clone_path.exists() else "pending"},
        {"label": "核心代码实现与迭代开发", "status": "pending"},
        {"label": "内循环自检与质量门禁", "status": "pending"},
        {"label": "交付产物会签与状态收尾", "status": "pending"},
    ]

    if status in {"completed", "committed", "integrated", "cleanup_ready", "cleaned"}:
        for m in milestones:
            m["status"] = "completed"
        return milestones

    if status in {"working", "dispatched", "rework", "blocked", "paused", "interrupted"}:
        milestones[0]["status"] = "completed"
        milestones[1]["status"] = "in_progress"

        if (loop_dir / "STATE.md").exists() or (loop_dir / "EVALUATION.md").exists():
            milestones[1]["status"] = "completed"
            milestones[2]["status"] = "in_progress"

        if status == "agent_done":
            milestones[2]["status"] = "completed"
            milestones[3]["status"] = "in_progress"

    return milestones


def collect_task_artifacts(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Collect artifacts produced by the task as first-class citizens."""
    artifacts: List[Dict[str, Any]] = []
    clone_path_str = task.get("clone_path")
    if not clone_path_str:
        return artifacts

    clone_path = Path(clone_path_str)
    if not clone_path.exists():
        return artifacts

    # 1. Git Changes / Diff Artifact
    try:
        r_status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(clone_path),
            text=True,
            capture_output=True,
            timeout=5,
        )
        status_lines = [l for l in (r_status.stdout or "").splitlines() if l.strip()]
        if status_lines:
            files_list = [l[3:].strip() for l in status_lines]
            r_diff = subprocess.run(
                ["git", "diff", "--stat"],
                cwd=str(clone_path),
                text=True,
                capture_output=True,
                timeout=5,
            )
            diff_summary = r_diff.stdout.strip().splitlines()[-1] if r_diff.stdout.strip() else f"{len(files_list)} 个文件变更"
            artifacts.append({
                "kind": "diff",
                "name": "Git 工作区变更",
                "summary": diff_summary,
                "files_changed": len(files_list),
                "files": files_list[:15],
            })
    except Exception:
        pass

    # 2. Evaluation Report Artifact
    eval_file = clone_path / ".herdr-loop" / "EVALUATION.md"
    if eval_file.exists():
        try:
            content = eval_file.read_text(encoding="utf-8")
            score_match = re.search(r"Overall Score[\*:\s]*([\d.]+)", content, re.IGNORECASE)
            score = float(score_match.group(1)) if score_match else None
            passed = "PASSED" in content or (score is not None and score >= 100.0)
            artifacts.append({
                "kind": "evaluation",
                "name": "工位自检评分报告",
                "score": score,
                "passed": passed,
                "path": str(eval_file),
                "summary": f"自检评分: {score or 0.0} / 100.0 ({'通过' if passed else '未达标'})",
            })
        except Exception:
            pass

    # 3. Document Artifacts (Goal & deliverables)
    goal_file = clone_path / ".herdr-loop" / "GOAL.md"
    if goal_file.exists():
        artifacts.append({
            "kind": "document",
            "name": "工位目标与验收标准契约",
            "path": str(goal_file),
            "summary": "工位量化验收契约文档 (.herdr-loop/GOAL.md)",
        })

    # Search for other markdown deliverables in docs/ or root
    doc_count = 0
    for doc_candidate in clone_path.glob("docs/**/*.md"):
        if ".herdr-loop" not in str(doc_candidate):
            rel = doc_candidate.relative_to(clone_path)
            artifacts.append({
                "kind": "document",
                "name": doc_candidate.name,
                "path": str(doc_candidate),
                "summary": f"设计与交付文档 ({rel})",
            })
            doc_count += 1
            if doc_count >= 15:
                break

    return artifacts


def extract_recent_activity(clean_text: str, max_lines: int = 5) -> List[str]:
    """Extract clean recent activity log lines from terminal buffer."""
    if not clean_text:
        return []
    lines = [l.strip() for l in clean_text.splitlines() if l.strip()]
    if not lines:
        return []
    return lines[-max_lines:]


def extract_task_blockers(task: Dict[str, Any], clean_text: str) -> List[str]:
    """Extract structured blocker alerts from task state and terminal output."""
    blockers: List[str] = []
    status = task.get("status")

    if status == "blocked":
        blockers.append(task.get("sentinel_reason") or "工位遇到阻碍，已上报仲裁")
    elif status == "interrupted":
        blockers.append(f"已制动暂停: {task.get('interrupt_reason') or '人工干预'}")

    if not clean_text:
        return blockers

    # 1. Explicit marker [HERDR_TASK_BLOCKER] or [BLOCKER]
    marker_match = re.search(r"\[(?:HERDR_TASK_)?BLOCKER\][:\s]*(.+)", clean_text, re.IGNORECASE)
    if marker_match:
        blockers.append(marker_match.group(1).strip())
    elif "HERDR_TASK_BLOCKER" in clean_text:
        blockers.append("终端输出阻碍标记 HERDR_TASK_BLOCKER")

    # 2. Fatal runtime error signatures in recent log tail
    tail_lines = [l.strip() for l in clean_text.splitlines() if l.strip()][-12:]
    tail_text = "\n".join(tail_lines)

    import_err = re.search(r"(?:ModuleNotFoundError|ImportError):\s*([^\n\r]+)", tail_text)
    if import_err:
        blockers.append(f"依赖缺失: {import_err.group(0).strip()}")

    syntax_err = re.search(r"(?:SyntaxError):\s*([^\n\r]+)", tail_text)
    if syntax_err:
        blockers.append(f"语法错误: {syntax_err.group(0).strip()}")

    # Deduplicate while preserving order
    deduped: List[str] = []
    seen = set()
    for b in blockers:
        if b not in seen:
            seen.add(b)
            deduped.append(b)
    return deduped


def project_task(task_id: str) -> Dict[str, Any]:
    """Produce the complete 4D white-box telemetry projection for a task."""
    tasks_data = load_tasks_data()
    task = next((t for t in tasks_data.get("tasks", []) if t.get("task_id") == task_id), None)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")

    pane_id = task.get("pane_id")
    raw_terminal = _read_pane_content(pane_id) if pane_id else ""
    clean_terminal = strip_ansi_codes(raw_terminal)

    intent = extract_task_intent(task, clean_terminal)
    milestones = extract_task_milestones(task, clean_terminal)
    artifacts = collect_task_artifacts(task)
    blockers = extract_task_blockers(task, clean_terminal)
    blocker = blockers[0] if blockers else None
    recent_activity = extract_recent_activity(clean_terminal, max_lines=6)

    return {
        "task_id": task_id,
        "workflow_id": task.get("workflow_id"),
        "node": task.get("node") or task.get("stage"),
        "node_label": task.get("node_label") or task.get("stage_label") or task.get("node"),
        "agent": task.get("agent"),
        "status": task.get("status"),
        "goal": task.get("goal"),
        "intent": intent,
        "milestones": milestones,
        "artifacts": artifacts,
        "blocker": blocker,
        "blockers": blockers,
        "recent_activity": recent_activity,
        "projected_at": time.time(),
    }


def detect_workflow_stalls(
    workflow_id: str,
    tasks: List[Dict[str, Any]],
    workflow: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Detect workflow deadlocks, orphan reworks, or hanging stage transitions."""
    now = time.time()

    # Resolve workflow record if not provided
    wf_entry = workflow
    if wf_entry is None and workflow_id:
        try:
            wf_data = load_workflows_data()
            wf_entry = wf_data.get("workflows", {}).get(workflow_id)
        except Exception:
            wf_entry = None

    # Closed, completed, closing, paused, or failed workflows cannot hang stage advancement
    if wf_entry:
        status = wf_entry.get("status")
        if status in {"completed", "closing", "failed", "paused"}:
            return {
                "is_stalled": False,
                "stall_type": None,
                "message": "",
                "suggested_action": None,
                "target_task_id": None,
            }
        if wf_entry.get("outcome") in {"delivered", "abandoned"} or wf_entry.get("completed_at"):
            return {
                "is_stalled": False,
                "stall_type": None,
                "message": "",
                "suggested_action": None,
                "target_task_id": None,
            }

    # 1. Detect rework orphan: task staying in 'rework' for > 45 seconds
    for t in tasks:
        if t.get("status") == "rework":
            last_ts = t.get("updated_at") or t.get("started_at") or t.get("created_at") or now
            if isinstance(last_ts, (int, float)) and (now - last_ts > 45):
                tid = t.get("task_id")
                return {
                    "is_stalled": True,
                    "stall_type": "rework_orphan",
                    "message": f"工位 {tid} 返工已停滞超 45 秒，等待协调器复验促醒",
                    "suggested_action": "force_review",
                    "target_task_id": tid,
                }

    # 2. Detect stage advance hang: all current tasks done/cleaned, but workflow still running
    terminal_statuses = {"completed", "committed", "integrated", "cleanup_ready", "cleaned"}
    if tasks and all(t.get("status") in terminal_statuses for t in tasks):
        # If terminal/wrapup stage tasks are present and complete, workflow reached end of stages
        stages_in_tasks = {t.get("stage") or t.get("node") for t in tasks}
        if "wrapup" in stages_in_tasks:
            return {
                "is_stalled": False,
                "stall_type": None,
                "message": "",
                "suggested_action": None,
                "target_task_id": None,
            }

        # Check if entire workflow DAG is complete based on workflow config
        try:
            from herdr.projects import workflow_config_for
            from herdr.workflow import is_workflow_completed
            cfg = workflow_config_for(workflow_id)
            if cfg and cfg.get("nodes"):
                completed_nodes = {
                    t.get("node") or t.get("stage")
                    for t in tasks
                    if t.get("status") in terminal_statuses
                }
                if is_workflow_completed(cfg, completed_nodes):
                    return {
                        "is_stalled": False,
                        "stall_type": None,
                        "message": "",
                        "suggested_action": None,
                        "target_task_id": None,
                    }
        except Exception:
            pass

        latest_finish = max(
            (t.get("updated_at") or t.get("created_at") or 0)
            for t in tasks
        )
        if isinstance(latest_finish, (int, float)) and latest_finish > 0 and (now - latest_finish > 45):
            return {
                "is_stalled": True,
                "stall_type": "stage_advance_hang",
                "message": "上一阶段所有任务均已完成，但后继阶段推进悬挂已超 45 秒",
                "suggested_action": "retry_advance",
                "target_task_id": None,
            }

    return {
        "is_stalled": False,
        "stall_type": None,
        "message": "",
        "suggested_action": None,
        "target_task_id": None,
    }


def project_workflow(workflow_id: str) -> Dict[str, Any]:
    """Produce aggregated white-box projection across all tasks in a workflow."""
    wf_data = load_workflows_data()
    wf_entry = wf_data.get("workflows", {}).get(workflow_id)
    if not wf_entry:
        raise ValueError(f"Workflow '{workflow_id}' not found")

    tasks_data = load_tasks_data()
    wf_tasks = [t for t in tasks_data.get("tasks", []) if t.get("workflow_id") == workflow_id]

    stall_info = detect_workflow_stalls(workflow_id, wf_tasks, workflow=wf_entry)

    task_projections = []
    completed_count = 0
    blocked_count = 0

    for t in wf_tasks:
        try:
            p = project_task(t["task_id"])
            if stall_info["is_stalled"] and stall_info.get("target_task_id") == t["task_id"]:
                p["blockers"].insert(0, stall_info["message"])
                p["blocker"] = stall_info["message"]
            task_projections.append(p)
            if p["status"] in {"completed", "committed", "integrated", "cleanup_ready", "cleaned"}:
                completed_count += 1
            elif p["status"] == "blocked":
                blocked_count += 1
        except Exception:
            pass

    return {
        "workflow_id": workflow_id,
        "status": wf_entry.get("status"),
        "progress": {
            "total_tasks": len(wf_tasks),
            "completed_tasks": completed_count,
            "blocked_tasks": blocked_count,
        },
        "stall": stall_info,
        "tasks": task_projections,
        "projected_at": time.time(),
    }

