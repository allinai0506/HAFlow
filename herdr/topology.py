#!/usr/bin/env python3
import json
import os
import subprocess
from pathlib import Path

HOME = Path.home()
ROOT = HOME / ".herdr-controller"
WORKFLOWS_FILE = ROOT / "workflows.json"

DEFAULT_STAGE_LABELS = {
    "requirements": "2需求分析",
    "plan": "3计划",
    "implementation": "4实现",
    "test": "5测试",
    "review": "6评审",
    "wrapup": "7收尾",
}


def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _run_json(cmd):
    r = subprocess.run(cmd, text=True, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(
            r.stderr.strip()
            or r.stdout.strip()
            or "command failed: " + " ".join(cmd)
        )
    return json.loads(r.stdout)


def _workflow_record(workflow_id):
    try:
        from .state_store import get_state_store
        wf = get_state_store().get_workflow(workflow_id)
        if wf and not (wf.get("status") == "unknown" and not wf.get("project_id")):
            return wf
    except Exception:
        pass
    fallback = Path(os.environ.get("WORKFLOWS_FILE") or WORKFLOWS_FILE)
    data = _load(fallback, {"workflows": {}})
    record = data.get("workflows", {}).get(workflow_id)
    if not record:
        raise RuntimeError(f"未找到工作流注册记录: {workflow_id}")
    return record


def _workflow_config(workflow_id):
    record = _workflow_record(workflow_id)
    workflow_file = Path(record.get("workflow_file", "")).expanduser()
    if not workflow_file.exists():
        raise RuntimeError(f"未找到工作流配置文件: {workflow_file}")
    workflow = _load(workflow_file, None)
    if not workflow:
        raise RuntimeError(f"工作流配置文件内容无效或无法解析: {workflow_file}")
    from .projects import _with_node_runtime
    return record, workflow_file, _with_node_runtime(workflow, record)


def _tabs(workspace_id):
    d = _run_json(["herdr", "tab", "list", "--workspace", workspace_id])
    result = d.get("result", {})
    return result.get("tabs", []) if isinstance(result, dict) else []


def _panes(workspace_id):
    d = _run_json(["herdr", "pane", "list", "--workspace", workspace_id])
    result = d.get("result", {})
    return result.get("panes", []) if isinstance(result, dict) else []


def _resolve_or_create_tab(workspace_id, project_root, stage):
    tabs = _tabs(workspace_id)
    stage_name = stage.get("key") or stage.get("id")
    desired_label = (
        stage.get("label")
        or DEFAULT_STAGE_LABELS.get(stage_name)
        or stage_name
    )
    cached_tab_id = stage.get("tab_id")

    cached = next(
        (x for x in tabs if x.get("tab_id") == cached_tab_id),
        None,
    )
    if cached and cached.get("label") == desired_label:
        return cached["tab_id"], False

    matches = [x for x in tabs if x.get("label") == desired_label]

    if len(matches) == 1:
        return matches[0]["tab_id"], True

    if len(matches) > 1:
        if cached:
            return cached["tab_id"], True
        raise RuntimeError(
            f"Ambiguous stage tab: stage={stage_name} "
            f"label={desired_label} "
            f"matches={[x.get('tab_id') for x in matches]}"
        )

    created = _run_json([
        "herdr", "tab", "create",
        "--workspace", workspace_id,
        "--cwd", project_root,
        "--label", desired_label,
        "--no-focus",
    ])
    return created["result"]["tab"]["tab_id"], True


def _anchor_label(stage_key):
    return f"Herdr Anchor · {stage_key}"


def _resolve_or_create_anchor(
    workspace_id,
    tab_id,
    project_root,
    stage,
):
    panes = _panes(workspace_id)
    cached_anchor_id = stage.get("anchor_pane_id")
    cached = next(
        (x for x in panes if x.get("pane_id") == cached_anchor_id),
        None,
    )

    def safe_anchor(pane):
        return (
            pane
            and pane.get("tab_id") == tab_id
            and (pane.get("cwd") or pane.get("foreground_cwd")) == project_root
            and not pane.get("agent")
        )

    if safe_anchor(cached):
        return cached["pane_id"], False

    stage_key = stage.get("key") or stage.get("id") or ""
    label = _anchor_label(stage_key)

    labeled = [
        x for x in panes
        if x.get("tab_id") == tab_id
        and x.get("label") == label
        and safe_anchor(x)
    ]
    if len(labeled) == 1:
        return labeled[0]["pane_id"], True

    plain = [
        x for x in panes
        if x.get("tab_id") == tab_id
        and safe_anchor(x)
    ]
    if len(plain) == 1:
        pane_id = plain[0]["pane_id"]
        subprocess.run(
            ["herdr", "pane", "rename", pane_id, label],
            text=True,
            capture_output=True,
        )
        return pane_id, True

    parents = [x for x in panes if x.get("tab_id") == tab_id]
    if not parents:
        raise RuntimeError(f"Stage tab has no panes: {tab_id}")

    parent = parents[0]["pane_id"]
    created = _run_json([
        "herdr", "pane", "split",
        parent,
        "--direction", "right",
        "--ratio", "0.30",
        "--cwd", project_root,
        "--no-focus",
    ])
    result = created["result"]
    pane = result.get("pane") or result.get("new_pane")
    if not pane:
        raise RuntimeError(f"Herdr pane split returned no pane: {result}")

    pane_id = pane["pane_id"]
    subprocess.run(
        ["herdr", "pane", "rename", pane_id, label],
        text=True,
        capture_output=True,
    )
    return pane_id, True


def ensure_stage_topology(workflow_id, stage_key):
    record, workflow_file, workflow = _workflow_config(workflow_id)

    workspace_id = workflow.get("workspace_id") or record.get("workspace_id")
    project_root = os.path.realpath(
        os.path.expanduser(
            workflow.get("project_root")
            or record.get("project_root")
            or ""
        )
    )

    if not workspace_id:
        raise RuntimeError(f"工作流未绑定工作区 ID (workspace_id): {workflow_id}")
    if not project_root or project_root == "/":
        raise RuntimeError(f"工作流项目根目录无效: {workflow_id}")

    stage = next(
        (
            item
            for item in workflow.get("stages", [])
            if (item.get("key") or item.get("id")) == stage_key
        ),
        None,
    )
    if not stage:
        stage = next(
            (
                item
                for item in workflow.get("nodes", [])
                if (item.get("id") or item.get("key")) == stage_key
            ),
            None,
        )
    if not stage:
        raise RuntimeError(f"未找到工作流阶段/节点: {stage_key}")

    tab_id, tab_changed = _resolve_or_create_tab(
        workspace_id, project_root, stage
    )
    stage["tab_id"] = tab_id

    anchor_pane_id, anchor_changed = _resolve_or_create_anchor(
        workspace_id, tab_id, project_root, stage
    )
    stage["anchor_pane_id"] = anchor_pane_id

    # Keep both stages and nodes in sync
    for s in workflow.get("stages", []):
        if (s.get("key") or s.get("id")) == stage_key:
            s["tab_id"] = tab_id
            s["anchor_pane_id"] = anchor_pane_id

    for n in workflow.get("nodes", []):
        if (n.get("id") or n.get("key")) == stage_key:
            n["tab_id"] = tab_id
            n["anchor_pane_id"] = anchor_pane_id

    if tab_changed or anchor_changed:
        from .projects import save_workflow_config_for
        save_workflow_config_for(workflow_id, workflow, runtime_node_id=stage_key)
        print(
            "[TOPOLOGY HEALED] "
            f"workflow={workflow_id} "
            f"stage={stage_key} "
            f"tab={tab_id} "
            f"anchor={anchor_pane_id}"
        )

    label = (
        stage.get("label")
        or DEFAULT_STAGE_LABELS.get(stage_key)
        or stage_key
    )

    return {
        "workspace_id": workspace_id,
        "stage_key": stage.get("key") or stage.get("id") or stage_key,
        "stage_label": label,
        "node_id": stage.get("id") or stage.get("key") or stage_key,
        "node_label": label,
        "tab_id": tab_id,
        "anchor_pane_id": anchor_pane_id,
        "next": stage.get("next"),
    }


def ensure_workflow_topology(workflow_id):
    _, _, workflow = _workflow_config(workflow_id)
    out = {}
    for stage in workflow.get("stages", []):
        key = stage.get("key") or stage.get("id")
        if key:
            out[key] = ensure_stage_topology(workflow_id, key)
    return out
