"""Project and Workspace scope validation and auto-detection."""

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from herdr.projects import load_projects


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


def detect_project_and_workspace(
    instruction: str,
    title: str = "",
) -> Dict[str, Any]:
    """Analyze prompt instruction and title to recommend matching project and workspace.

    Returns:
        dict with recommended_project_id, recommended_workspace_id, confidence, and reason.
    """
    text = f"{title or ''}\n{instruction or ''}".strip()
    if not text:
        return {
            "recommended_project_id": None,
            "recommended_workspace_id": None,
            "project_name": None,
            "confidence": 0.0,
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
            "reason": "系统中暂无已注册项目",
        }

    best_match = None
    best_score = 0.0
    best_reason = ""

    for p in projects:
        score = 0.0
        reason = ""
        root = p.get("project_root", "")
        p_name = p.get("project_name", "")
        p_id = p.get("project_id", "")

        # 1. Absolute project root path match in text
        if root and root in text:
            score = 1.0
            reason = f"命中完整项目路径: {root}"
        # 2. Exact project_id match in text
        elif p_id and p_id in text:
            score = 0.98
            reason = f"命中项目 ID: {p_id}"
        # 3. Exact project_name token match (boundary safe)
        elif p_name:
            pattern = re.compile(rf"\b{re.escape(p_name)}\b", re.IGNORECASE)
            if pattern.search(text):
                score = 0.90
                reason = f"命中项目名称: {p_name}"
            elif p_name.lower() in text.lower():
                score = 0.75
                reason = f"包含项目名称子串: {p_name}"

        if score > best_score:
            best_score = score
            best_match = p
            best_reason = reason

    if best_match and best_score >= 0.70:
        return {
            "recommended_project_id": best_match.get("project_id"),
            "recommended_workspace_id": best_match.get("workspace_id"),
            "project_name": best_match.get("project_name"),
            "confidence": round(best_score, 2),
            "reason": best_reason,
        }

    # Only single project in system -> fallback with low confidence
    if len(projects) == 1:
        single = projects[0]
        return {
            "recommended_project_id": single.get("project_id"),
            "recommended_workspace_id": single.get("workspace_id"),
            "project_name": single.get("project_name"),
            "confidence": 0.50,
            "reason": f"系统中仅有单项目: {single.get('project_name')}",
        }

    return {
        "recommended_project_id": None,
        "recommended_workspace_id": None,
        "project_name": None,
        "confidence": 0.0,
        "reason": "未找到匹配的项目或置信度不足",
    }
