#!/opt/homebrew/bin/python3
import json
import subprocess
from pathlib import Path
try:
    from .projects import project_for_workflow
except ImportError:
    from herdr.projects import project_for_workflow

HOME = Path.home()
ROOT = HOME / ".herdr-controller"
TASKS_FILE = ROOT / "tasks.json"
BINDINGS_FILE = ROOT / "pane-slots.json"


def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _run_json(cmd):
    r = subprocess.run(cmd, text=True, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip() or "command failed")
    return json.loads(r.stdout)


def _pane_list(workspace_id):
    data = _run_json(["herdr", "pane", "list", "--workspace", workspace_id])
    result = data.get("result", data)
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ("panes", "items"):
            value = result.get(key)
            if isinstance(value, list):
                return value
    return []


def _live_agent(pane_id):
    r = subprocess.run(["herdr", "agent", "get", pane_id], text=True, capture_output=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)["result"]["agent"]
    except Exception:
        return {"agent": "unknown", "agent_status": "unknown"}


def _claimed_panes():
    tasks = _load(TASKS_FILE, {"tasks": []})
    out = {}
    for task in tasks.get("tasks", []):
        pane_id = task.get("pane_id")
        if pane_id and task.get("task_id"):
            out[pane_id] = task["task_id"]
    return out


def _bindings():
    return _load(BINDINGS_FILE, {"panes": {}})


def bind_pane(pane_id, agent="auto"):
    data = _bindings()
    data.setdefault("panes", {})[pane_id] = {"agent": agent or "auto"}
    _save(BINDINGS_FILE, data)


def unbind_pane(pane_id):
    data = _bindings()
    data.setdefault("panes", {}).pop(pane_id, None)
    _save(BINDINGS_FILE, data)


def list_slots_for_project(project):
    workflow = _load(project.get("workflow_file"), None)
    if not workflow:
        return []

    from .projects import _with_node_runtime
    workflow = _with_node_runtime(workflow, project)

    items = workflow.get("nodes") or workflow.get("stages", [])
    node_by_tab = {
        item.get("tab_id"): item
        for item in items
        if item.get("tab_id")
    }
    anchors = {
        item.get("anchor_pane_id")
        for item in items
        if item.get("anchor_pane_id")
    }
    claimed = _claimed_panes()
    bindings = _bindings().get("panes", {})

    slots = []
    for pane in _pane_list(project["workspace_id"]):
        pane_id = pane.get("pane_id")
        tab_id = pane.get("tab_id")
        if not pane_id or tab_id not in node_by_tab:
            continue
        if pane_id in anchors or pane_id == project.get("coordinator_pane_id"):
            continue

        live = _live_agent(pane_id)
        claimed_by = claimed.get(pane_id)
        node = node_by_tab[tab_id]
        node_id = node.get("id") or node.get("key")
        bound = bindings.get(pane_id, {}).get("agent", "auto")
        slots.append({
            "pane_id": pane_id,
            "tab_id": tab_id,
            "node": node_id,
            "stage": node_id,
            "node_label": node.get("label"),
            "stage_label": node.get("label"),
            "bound_agent": bound,
            "claimed_by": claimed_by,
            "live_agent": live.get("agent") if live else None,
            "agent_status": live.get("agent_status") if live else None,
            "available": claimed_by is None and live is None,
        })
    return slots


def acquire_pane_for_task(workflow_id, stage, agent):
    project = project_for_workflow(workflow_id)
    if not project:
        return None
    slots = list_slots_for_project(project)

    exact = [
        s for s in slots
        if s["available"] and (s.get("node") == stage or s.get("stage") == stage) and s["bound_agent"] == agent
    ]
    if exact:
        return exact[0]["pane_id"]

    generic = [
        s for s in slots
        if s["available"] and (s.get("node") == stage or s.get("stage") == stage) and s["bound_agent"] in ("auto", "", None)
    ]
    return generic[0]["pane_id"] if generic else None
