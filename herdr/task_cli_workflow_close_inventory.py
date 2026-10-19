"""Workflow close inventory and target resolution helpers."""

import json
import os
import sys


def _workflow_stage_tabs(workflow_id, tasks, *, deps):
    project_for_workflow = deps["project_for_workflow"]
    os = deps["os"]
    "从项目 workflow 配置解析阶段 tab 与归属 pane 集合。\n\n    返回 dict:tab_ids / coordinator_pane / workspace_id / owned_pane_ids。\n    "
    empty = {
        "tab_ids": [],
        "coordinator_pane": None,
        "workspace_id": None,
        "owned_pane_ids": set(),
    }
    project = project_for_workflow(workflow_id) or {}
    cfg_file = project.get("workflow_file") or next(
        (t.get("workflow_config") for t in tasks if t.get("workflow_config")), None
    )
    if not cfg_file or not os.path.exists(cfg_file):
        return empty
    try:
        with open(cfg_file, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError):
        return empty
    from herdr.projects import _with_node_runtime

    cfg = _with_node_runtime(cfg, project)
    items = cfg.get("nodes") or cfg.get("stages", [])
    tab_ids = sorted({item.get("tab_id") for item in items if item.get("tab_id")})
    owned_pane_ids = {
        item.get("anchor_pane_id") for item in items if item.get("anchor_pane_id")
    }
    owned_pane_ids |= {t.get("pane_id") for t in tasks if t.get("pane_id")}
    return {
        "tab_ids": tab_ids,
        "coordinator_pane": (cfg.get("coordinator") or {}).get("pane_id"),
        "workspace_id": cfg.get("workspace_id"),
        "owned_pane_ids": owned_pane_ids,
    }


def _live_workspace_panes(workspace_id, *, deps):
    _herdr = deps["herdr"]
    "返回 workspace 存活 pane 列表;不可用时返回 None(宁可保留现场)。"
    listing = _herdr("pane", "list", "--workspace", workspace_id)
    if listing.returncode != 0:
        return None
    try:
        result = json.loads(listing.stdout).get("result", {})
        panes = result.get("panes", result) if isinstance(result, dict) else result
        return panes or []
    except (json.JSONDecodeError, TypeError):
        return None


def _foreign_panes_in_tab(live_panes, tab_id, owned_pane_ids):
    """筛选 tab 内不属于本 workflow 的存活 pane。

    阶段 tab 可能被连续多个 workflow 复用;关闭 tab 会连带销毁
    共存的其他现场,所以有外来 pane 时必须放弃关 tab。
    """
    return sorted(
        (
            pane_id
            for pane in live_panes or []
            if pane.get("tab_id") == tab_id
            and (pane_id := pane.get("pane_id"))
            and (pane_id not in owned_pane_ids)
        )
    )


def resolve_workflow_id_for_close(input_id=None, *, deps):
    load_workflows = deps["load_workflows"]
    load_tasks = deps["load_tasks"]
    detect_git_root = deps["detect_git_root"]
    project_by_root = deps["project_by_root"]
    entries = load_workflows().get("workflows", {})
    if input_id:
        if input_id in entries:
            return input_id
        tasks = load_tasks().get("tasks", [])
        if any((t.get("workflow_id") == input_id for t in tasks)):
            return input_id
        non_terminal = {
            wid: e for wid, e in entries.items() if e.get("status") not in {"completed"}
        }
        matches = [
            wid
            for wid in non_terminal
            if wid.endswith(input_id) or f"-{input_id}" in wid or input_id in wid
        ]
        if len(matches) == 1:
            print(f"[AUTO RESOLVE] 匹配到活跃工作流: {matches[0]}")
            return matches[0]
        elif len(matches) > 1:
            print(
                f"[AMBIGUOUS WORKFLOW ID] 输入 '{input_id}' 匹配到多个活跃工作流: {', '.join(matches)}"
            )
            sys.exit(1)
        all_matches = [
            wid
            for wid in entries
            if wid.endswith(input_id) or f"-{input_id}" in wid or input_id in wid
        ]
        if len(all_matches) == 1:
            print(f"[AUTO RESOLVE] 匹配到工作流: {all_matches[0]}")
            return all_matches[0]
        elif len(all_matches) > 1:
            print(
                f"[AMBIGUOUS WORKFLOW ID] 输入 '{input_id}' 匹配到多个工作流: {', '.join(all_matches)}"
            )
            sys.exit(1)
        return input_id
    try:
        root = detect_git_root()
        proj = project_by_root(root)
        if proj:
            pid = proj.get("project_id")
            active = [
                wid
                for wid, e in entries.items()
                if e.get("project_id") == pid and e.get("status") not in {"completed"}
            ]
            if len(active) == 1:
                print(
                    f"[AUTO DETECT] 检测到当前项目 {proj.get('project_name')} 唯一活跃工作流: {active[0]}"
                )
                return active[0]
            elif len(active) > 1:
                print(
                    f"当前项目存在多个活跃工作流，请显式指定 workflow_id:\n  "
                    + "\n  ".join(active)
                )
                sys.exit(1)
    except Exception:
        pass
    print("请指定 workflow_id，例如: herdr-task close-workflow <workflow_id>")
    sys.exit(1)
