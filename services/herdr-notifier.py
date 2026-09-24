#!/opt/homebrew/bin/python3
import argparse, json, shutil, subprocess, time, urllib.parse
from pathlib import Path

HOME = Path.home()
ROOT = HOME / ".herdr-controller"
TASKS_FILE = ROOT / "tasks.json"
STATE_FILE = ROOT / "notifier-state.json"

ATTENTION = {"blocked", "failed", "human_review", "needs_action"}
STAGES = {"requirements","plan","implementation","test","review","wrapup"}

CONSOLE_BASE_URL = "http://127.0.0.1:8765"
_HINT_SHOWN = False

def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)

def esc(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"')

def build_console_url(workflow_id=None, task_id=None):
    params = []
    if workflow_id:
        params.append(f"workflow_id={urllib.parse.quote(str(workflow_id))}")
    if task_id:
        params.append(f"task_id={urllib.parse.quote(str(task_id))}")
    if params:
        return f"{CONSOLE_BASE_URL}/?{'&'.join(params)}"
    return f"{CONSOLE_BASE_URL}/"

def notify_human_upgrade(task_id, workflow_id, body, url=None):
    """Dedicated human-escalation channel (T4).

    Separate from scan() state-change dedup (:129): callers dedup by
    blocked_episode_id, so a long-blocked task escalates exactly once per
    episode. Body carries copy-paste commands only, never auto-executes.
    """
    notify(
        "Herdr Factory · 阻塞升级（需人工）",
        f"{workflow_id} · {task_id}",
        str(body),
        url=url,
    )


def notify(title, subtitle, message, url=None):
    global _HINT_SHOWN
    tn = shutil.which("terminal-notifier")
    if tn:
        cmd = [
            tn,
            "-title", str(title),
            "-subtitle", str(subtitle),
            "-message", str(message),
            "-sound", "Glass",
        ]
        if url:
            cmd += ["-open", str(url)]
        subprocess.run(cmd, capture_output=True, text=True)
        return

    if not _HINT_SHOWN:
        print(
            "[NOTIFIER HINT] terminal-notifier 未安装，通知回退为 osascript（无法点击直达页面）。建议安装: brew install terminal-notifier",
            flush=True,
        )
        _HINT_SHOWN = True

    script = (
        f'display notification "{esc(message)}" '
        f'with title "{esc(title)}" '
        f'subtitle "{esc(subtitle)}" sound name "Glass"'
    )
    subprocess.run(["osascript", "-e", script], capture_output=True, text=True)

def project(task):
    return (
        task.get("project_name")
        or task.get("project")
        or task.get("repo_name")
        or task.get("workspace_id")
        or "unknown-project"
    )

def reason(task):
    for k in ("failure_reason","blocked_reason","human_review_reason","sentinel_reason","reason"):
        if task.get(k):
            return str(task[k])
    return "需要人工查看"

def workflow_complete(tasks):
    if not tasks:
        return False
    wf_id = tasks[0].get("workflow_id")
    expected_nodes = set(STAGES)
    if wf_id:
        try:
            from herdr_projects import workflow_config_for
            cfg = workflow_config_for(wf_id)
            if cfg and cfg.get("nodes"):
                expected_nodes = {n["id"] for n in cfg["nodes"]}
        except Exception:
            pass

    seen = {t.get("node") or t.get("stage") for t in tasks}
    return expected_nodes.issubset(seen) and all(t.get("status") == "cleaned" for t in tasks)

def scan(state):
    data = load(TASKS_FILE, {"tasks":[]})
    tasks = data.get("tasks", [])

    if not state.get("initialized"):
        groups = {}
        for t in tasks:
            if t.get("workflow_id"):
                groups.setdefault(t["workflow_id"], []).append(t)
        state = {
            "initialized": True,
            "last_status": {t["task_id"]: t.get("status") for t in tasks if t.get("task_id")},
            "completed_workflows": [wf for wf, ts in groups.items() if workflow_complete(ts)],
        }
        save(STATE_FILE, state)
        return state

    last = state.setdefault("last_status", {})
    completed = set(state.setdefault("completed_workflows", []))

    for t in tasks:
        tid = t.get("task_id")
        if not tid:
            continue
        cur = t.get("status")
        prev = last.get(tid)

        if cur != prev and cur in ATTENTION:
            title = {
                "blocked": "Herdr Factory · 需要处理",
                "failed": "Herdr Factory · Task 失败",
            }.get(cur, "Herdr Factory · 需要人工审核")

            wf_id = t.get("workflow_id")
            url = build_console_url(workflow_id=wf_id, task_id=tid)

            notify(
                title,
                f"{project(t)} · {tid}",
                f"Workflow: {t.get('workflow_id','unknown')}\n"
                f"Agent: {t.get('agent','unknown')} · Pane: {t.get('pane_id','unknown')}\n"
                f"{reason(t)}",
                url=url,
            )

        last[tid] = cur

    groups = {}
    for t in tasks:
        wf = t.get("workflow_id")
        if wf:
            groups.setdefault(wf, []).append(t)

    for wf, ts in groups.items():
        if wf not in completed and workflow_complete(ts):
            url = build_console_url(workflow_id=wf)
            notify(
                "Herdr Factory · Workflow 完成",
                f"{project(ts[0])} · {wf}",
                "工作流所有节点已全部完成并通过清理验收",
                url=url,
            )
            completed.add(wf)

    state["completed_workflows"] = sorted(completed)
    save(STATE_FILE, state)
    return state

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    if args.test:
        notify(
            "Herdr Factory · 通知测试",
            "macOS Notification Center",
            "通知系统已经正常工作。以后 BLOCKED / FAILED / Workflow 完成会提醒你。",
            url=build_console_url(),
        )
        print("TEST_NOTIFICATION_SENT")
        return

    state = load(STATE_FILE, {"initialized":False,"last_status":{},"completed_workflows":[]})

    if args.once:
        scan(state)
        return

    print("[HERDR NOTIFIER] starting", flush=True)
    while True:
        try:
            state = scan(state)
        except Exception as e:
            print(f"[NOTIFIER ERROR] {e}", flush=True)
        time.sleep(5)

if __name__ == "__main__":
    main()
