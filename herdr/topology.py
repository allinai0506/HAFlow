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


def _lifecycle_lock():
    """Same lock the dynamic-pane reaper holds while it closes a pane."""
    from herdr.state_store import get_state_store
    from herdr.task_resources import workflow_launch_lock
    return workflow_launch_lock(
        get_state_store().db_path, "managed-dynamic-pane-lifecycle",
    )


def _confirmed_agent_absent(pane_id):
    """True only when a successful probe says this pane has no agent."""
    try:
        result = subprocess.run(
            ["herdr", "agent", "get", pane_id],
            text=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0 or not (result.stdout or "").strip():
        return False
    from herdr.observer.live import _agent_payload
    valid, _agent, explicit_empty = _agent_payload(result.stdout)
    return bool(valid and explicit_empty)


def _is_idle_root_shell(pane, project_root):
    """A project-root pane is idle only after a live probe confirms no agent.

    A missing agent field on the pane list is not proof. A failed, empty, or
    unreadable probe is occupied, so it does not authorize a close.
    """
    if pane.get("agent"):
        return False
    if not project_root or project_root == "/" or not pane.get("pane_id"):
        return False
    raw = pane.get("cwd") or pane.get("foreground_cwd") or ""
    if not raw:
        return False
    try:
        same_root = (
            os.path.realpath(os.path.expanduser(raw))
            == os.path.realpath(os.path.expanduser(project_root))
        )
    except OSError:
        return False
    if not same_root:
        return False
    return _confirmed_agent_absent(pane["pane_id"])


def _choose_anchor(pool, cached_id):
    chosen = next(
        (pane for pane in pool if pane.get("pane_id") == cached_id),
        None,
    )
    if chosen is None:
        if not pool:
            return None, False
        return pool[0]["pane_id"], True
    return chosen["pane_id"], False


def _close_idle_panes(workspace_id, tab_id, project_root, victims):
    """Re-list under the lifecycle lock. Never close the last pane."""
    with _lifecycle_lock():
        fresh = [
            pane for pane in _panes(workspace_id)
            if pane.get("tab_id") == tab_id and pane.get("pane_id")
        ]
        if len(fresh) <= 1:
            return fresh, False
        victim_ids = {pane.get("pane_id") for pane in victims}
        still = [
            pane for pane in fresh
            if pane.get("pane_id") in victim_ids
            and _is_idle_root_shell(pane, project_root)
        ]
        survivor_ids = {
            pane.get("pane_id") for pane in fresh
            if pane.get("pane_id") not in {item.get("pane_id") for item in still}
        }
        if not survivor_ids:
            return fresh, False
        pool = [pane for pane in fresh if pane.get("pane_id") in survivor_ids]
        changed = False
        for pane in still:
            result = subprocess.run(
                ["herdr", "pane", "close", pane["pane_id"]],
                text=True,
                capture_output=True,
            )
            if result.returncode == 0:
                changed = True
            else:
                pool.append(pane)
        return pool, changed


def _resolve_or_create_anchor(
    workspace_id,
    tab_id,
    project_root,
    stage,
):
    """Pick a living pane in the tab. Do not add or label an idle shell.

    A tab cannot exist without a pane. The recorded id is only the parent
    used for the next split. When a task pane already exists, project-root
    shells whose probe confirms no agent are closed. One pane is never
    probed and never closed.
    """
    panes = [
        pane for pane in _panes(workspace_id)
        if pane.get("tab_id") == tab_id and pane.get("pane_id")
    ]
    if not panes:
        raise RuntimeError(f"Stage tab has no panes: {tab_id}")

    cached_id = stage.get("anchor_pane_id")
    if len(panes) == 1:
        pane_id, changed = _choose_anchor(panes, cached_id)
        return pane_id, changed

    idle = [pane for pane in panes if _is_idle_root_shell(pane, project_root)]
    busy = [pane for pane in panes if pane not in idle]
    if busy:
        victims, pool = idle, list(busy)
    elif len(idle) > 1:
        kept = next(
            (pane for pane in idle if pane.get("pane_id") == cached_id),
            idle[0],
        )
        victims = [
            pane for pane in idle if pane.get("pane_id") != kept.get("pane_id")
        ]
        pool = [kept]
    else:
        victims, pool = [], list(panes)

    changed = False
    if victims:
        pool, changed = _close_idle_panes(
            workspace_id, tab_id, project_root, victims,
        )
    pane_id, choose_changed = _choose_anchor(pool, cached_id)
    if pane_id is None:
        raise RuntimeError(f"Stage tab has no panes: {tab_id}")
    return pane_id, changed or choose_changed


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
