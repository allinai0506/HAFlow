"""Project and Workspace scope validation and auto-detection."""

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from herdr.projects import load_projects

# Minimum confidence required for automatic recommendation / confirmation
CONFIDENCE_THRESHOLD = 0.80


def validate_project_and_workspace(
    project_id: str,
    workspace_id: str,
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
    """Verify that project_id exists and workspace_id matches its registered workspace.

    Returns:
        (is_valid, error_message, project_record)
    """
    if not project_id:
        return False, "project_id is required", None
    if not workspace_id:
        return False, "workspace_id is required", None

    projects_data = load_projects()
    projects = projects_data.get("projects", {})

    record = None
    for p in projects.values():
        if p.get("project_id") == project_id:
            record = p
            break

    if not record:
        return False, f"Project '{project_id}' is not registered in HAFlow", None

    expected_ws = record.get("workspace_id")
    if not expected_ws:
        return False, f"Project '{project_id}' does not have a bound workspace_id", None

    if expected_ws != workspace_id:
        return (
            False,
            f"workspace_id '{workspace_id}' does not match project's bound workspace '{expected_ws}'",
            record,
        )

    return True, None, record


def _match_token(token: str, text: str) -> bool:
    """Boundary-safe token match supporting ASCII, CJK, and mixed alphanumeric tokens.

    Prevents substring false positives (e.g. 'ha' in 'shadow') while correctly
    matching alphanumeric tokens inside Chinese text without spaces
    (e.g. 'HAFlow' in '请修复HAFlow中的收件箱问题').
    """
    if not token or not text:
        return False
    lead = r"(?<![a-zA-Z0-9_])" if re.match(r"^[a-zA-Z0-9_]", token) else ""
    trail = r"(?![a-zA-Z0-9_])" if re.search(r"[a-zA-Z0-9_]$", token) else ""
    pattern = re.compile(rf"{lead}{re.escape(token)}{trail}", re.IGNORECASE)
    return bool(pattern.search(text))


def detect_project_and_workspace(
    instruction: str,
    title: str = "",
) -> Dict[str, Any]:
    """Analyze prompt instruction and title to recommend matching project and workspace.

    Enforces strict confidence rules:
    - If multiple projects tie for the top score, no arbitrary guess is made.
    - If confidence < CONFIDENCE_THRESHOLD (0.80), is_confident is False,
      preventing automatic queue dispatching without explicit human confirmation.
    """
    text = f"{title or ''}\n{instruction or ''}".strip()
    if not text:
        return {
            "recommended_project_id": None,
            "recommended_workspace_id": None,
            "project_name": None,
            "confidence": 0.0,
            "is_confident": False,
            "reason": "输入内容为空",
        }

    projects_data = load_projects()
    projects = list(projects_data.get("projects", {}).values())
    if not projects:
        return {
            "recommended_project_id": None,
            "recommended_workspace_id": None,
            "project_name": None,
            "confidence": 0.0,
            "is_confident": False,
            "reason": "系统中暂无已注册项目",
        }

    scored_projects: List[Tuple[float, Dict[str, Any], str]] = []

    for p in projects:
        score = 0.0
        reason = ""
        root = p.get("project_root", "") or p.get("root", "")
        p_name = p.get("project_name", "")
        p_id = p.get("project_id", "")

        # 1. Absolute project root path match in text
        if root and root in text:
            score = 1.0
            reason = f"命中完整项目路径: {root}"
        # 2. Exact project_id match in text (boundary safe)
        elif p_id and _match_token(p_id, text):
            score = 0.98
            reason = f"命中项目 ID: {p_id}"
        # 3. Exact project_name token match (boundary safe, CJK friendly)
        elif p_name and _match_token(p_name, text):
            score = 0.90
            reason = f"命中项目名称: {p_name}"
        elif p_name and p_name.lower() in text.lower():
            score = 0.60
            reason = f"包含项目名称模糊子串: {p_name}"

        if score > 0:
            scored_projects.append((score, p, reason))

    if scored_projects:
        # Sort descending by score
        scored_projects.sort(key=lambda x: x[0], reverse=True)
        top_score = scored_projects[0][0]
        # Check for ties at the top score
        top_tier = [entry for entry in scored_projects if entry[0] == top_score]

        if len(top_tier) > 1:
            # Ambiguous match between multiple projects: never guess!
            names = [entry[1].get("project_name", entry[1].get("project_id", "未知")) for entry in top_tier]
            return {
                "recommended_project_id": None,
                "recommended_workspace_id": None,
                "project_name": None,
                "confidence": 0.0,
                "is_confident": False,
                "reason": f"检测到多个匹配度相同的项目 ({' / '.join(names)})，禁止随意猜测，请手动选择",
            }

        best_score, best_match, best_reason = top_tier[0]
        is_confident = (best_score >= CONFIDENCE_THRESHOLD)
        return {
            "recommended_project_id": best_match.get("project_id"),
            "recommended_workspace_id": best_match.get("workspace_id"),
            "project_name": best_match.get("project_name"),
            "confidence": round(best_score, 2),
            "is_confident": is_confident,
            "reason": best_reason if is_confident else f"{best_reason}（置信度不足 {int(CONFIDENCE_THRESHOLD*100)}%，需人工确认）",
        }

    # Only single project in system -> fallback with low confidence, strictly marked NOT confident
    if len(projects) == 1:
        single = projects[0]
        return {
            "recommended_project_id": single.get("project_id"),
            "recommended_workspace_id": single.get("workspace_id"),
            "project_name": single.get("project_name"),
            "confidence": 0.50,
            "is_confident": False,
            "reason": f"系统中仅有单项目【{single.get('project_name')}】（置信度不足，严禁自动执行，须人工确认）",
        }

    return {
        "recommended_project_id": None,
        "recommended_workspace_id": None,
        "project_name": None,
        "confidence": 0.0,
        "is_confident": False,
        "reason": "未找到匹配的项目或置信度不足",
    }
