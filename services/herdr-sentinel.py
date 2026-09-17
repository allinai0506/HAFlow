#!/opt/homebrew/bin/python3

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERDR_ROOT = Path(__file__).resolve().parent.parent
if str(HERDR_ROOT) not in sys.path:
    sys.path.insert(0, str(HERDR_ROOT))

from herdr import liveness  # noqa: E402  (must follow sys.path bootstrap)

HOME = Path.home()
ROOT = HOME / ".herdr-controller"
TASKS_FILE = ROOT / "tasks.json"
STATE_FILE = ROOT / "sentinel-state.json"
CONTROLLER_SERVICE = f"gui/{os.getuid()}/com.user.herdr-controller"

ACTIVE = {"dispatched", "working", "blocked", "rework"}
CRASH_PATTERNS = (
    "Bun has crashed",
    "segmentation fault",
    "panic(main thread)",
)

POLL_SECONDS = 3
NUDGE_AFTER_SECONDS = 15
# 同一任务因投递熔断被自动失败的最大次数;超过后只告警不再自动失败,
# 防止结构性损坏的 Agent/Pane 陷入"失败->重启->再失败"的无限循环。
DISPATCH_FUSE_MAX_REQUEUES = 2


def dispatch_fuse_enabled():
    value = os.environ.get("HERDR_DISPATCH_FUSE", "1")
    return value.strip().lower() not in ("0", "false", "off", "no")


def run(cmd, timeout=10):
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def pane_visible(pane_id):
    try:
        r = run(
            ["herdr", "pane", "read", pane_id, "--source", "visible"],
            timeout=10,
        )
        return (r.stdout or "") + "\n" + (r.stderr or "")
    except Exception:
        return ""


def agent_status(pane_id):
    try:
        r = run(["herdr", "agent", "get", pane_id], timeout=10)
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        return data["result"]["agent"].get("agent_status")
    except Exception:
        return None


def nudge_enter(pane_id):
    try:
        run(["herdr", "pane", "send-keys", pane_id, "enter"], timeout=10)
        return True
    except Exception:
        return False


def _get_store():
    from herdr.state_store import get_state_store
    if os.environ.get("HERDR_STATE_DB"):
        return get_state_store(Path(os.environ["HERDR_STATE_DB"]))
    t_file = globals().get("TASKS_FILE") or os.environ.get("TASKS_FILE")
    if t_file:
        p = Path(t_file)
        db_path = p.parent / "state.db" if p.name == "tasks.json" else p.with_suffix(".db")
        if db_path.parent.exists():
            return get_state_store(db_path=db_path)
    return get_state_store()


def update_statuses(changes):
    if not changes:
        return False

    store = _get_store()
    changed = False

    for task_id, (new_status, reason) in changes.items():
        authoritative = store.get_task(task_id)
        if not authoritative:
            print(f"[SENTINEL SKIP] task {task_id} missing from authoritative StateStore", file=sys.stderr, flush=True)
            continue

        old_status = authoritative.get("status")
        if old_status not in ACTIVE:
            continue

        try:
            from herdr import kernel
            kernel.transition_task(
                task_id=task_id,
                to_status=new_status,
                reason=reason,
                source="herdr-sentinel",
                metadata={
                    "sentinel_reason": reason,
                    "sentinel_updated_at": int(time.time()),
                },
                store=store,
            )
            changed = True
            print(
                f"[SENTINEL STATE] {task_id}: "
                f"{old_status} -> {new_status} ({reason})",
                flush=True,
            )
        except Exception as exc:
            print(
                f"[SENTINEL ERROR] transition failed for {task_id}: {exc}; task status preserved as {old_status}",
                file=sys.stderr,
                flush=True,
            )

    return changed


def restart_controller():
    try:
        run(
            [
                "launchctl",
                "kickstart",
                "-k",
                CONTROLLER_SERVICE,
            ],
            timeout=20,
        )
        print("[SENTINEL] Controller restarted for recovery", flush=True)
    except Exception as e:
        print(f"[SENTINEL ERROR] Controller restart: {e}", flush=True)


def notify_stall(alert):
    """Best-effort stall notification (deep-linked to the console)."""
    try:
        import importlib

        notifier = importlib.import_module("services.herdr-notifier")
        url = notifier.build_console_url(
            workflow_id=alert.get("workflow_id"),
            task_id=alert.get("task_id"),
        )
        notifier.notify(
            "Herdr Factory · 任务停滞",
            f"{alert.get('workflow_id', 'unknown')} · {alert.get('task_id')}",
            f"任务停留在 {alert.get('status')} 已 {alert.get('idle_seconds')}s，"
            "无任何状态推进，需要关注。",
            url=url,
        )
    except Exception as exc:
        print(f"[SENTINEL NOTIFY ERROR] {exc}", file=sys.stderr, flush=True)


def notify_dispatch_fuse(breach):
    """Deliverable-fuse notification: dispatched task never reached working."""
    try:
        import importlib

        notifier = importlib.import_module("services.herdr-notifier")
        url = notifier.build_console_url(
            workflow_id=breach.get("workflow_id"),
            task_id=breach.get("task_id"),
        )
        action = breach.get("action", "attention")
        if action == "failed":
            body = (
                f"任务派发后 {breach.get('waited_seconds')}s 内未进入 working，"
                f"Pane 无投递痕迹，已按投递熔断标记 failed，等待总指挥重新派发。"
            )
        else:
            body = (
                f"任务派发后 {breach.get('waited_seconds')}s 内未进入 working，"
                "Pane 仍有活动迹象或已达自动失败上限，仅告警不处置，请人工关注。"
            )
        notifier.notify(
            "Herdr Factory · 投递熔断",
            f"{breach.get('workflow_id', 'unknown')} · {breach.get('task_id')}",
            body,
            url=url,
        )
    except Exception as exc:
        print(f"[SENTINEL NOTIFY ERROR] {exc}", file=sys.stderr, flush=True)


def _pane_delivery_evidence(pane_id, task_id):
    """Evidence for whether the dispatch prompt actually landed on the pane."""
    if not pane_id:
        return {"has_marker": False, "agent_status": None}

    screen = pane_visible(pane_id)
    orchestration_marker = f"HERDR_ORCH_TASK:{task_id}"
    return {
        "has_marker": orchestration_marker in screen,
        "agent_status": agent_status(pane_id),
    }


def check_dispatch_fuse(tasks, state):
    """Fail dispatched-but-never-working tasks so the controller can requeue.

    仅当 Pane 无投递痕迹且 Agent 未在 working 时才自动失败;否则只告警。
    返回 True 表示发生了状态失败处置(调用方据此走即时保存路径)。
    """
    if not dispatch_fuse_enabled():
        return False

    episodes = state.get("dispatch_fuse") or {}
    breaches, updated = liveness.evaluate_dispatch_fuse(
        tasks, episodes, time.time()
    )

    changed = False
    for breach in breaches:
        evidence = _pane_delivery_evidence(
            breach.get("pane_id"), breach.get("task_id")
        )
        agent_state = evidence.get("agent_status")
        dead_delivery = not evidence.get("has_marker") and agent_state != "working"
        can_fail = dead_delivery and breach.get("requeues", 0) < DISPATCH_FUSE_MAX_REQUEUES

        breach["action"] = "failed" if can_fail else "attention"

        print(
            f"[SENTINEL FUSE] task={breach['task_id']} "
            f"waited={breach.get('waited_seconds')}s "
            f"marker={evidence.get('has_marker')} "
            f"agent={agent_state} action={breach['action']}",
            flush=True,
        )

        if can_fail:
            if update_statuses(
                {breach["task_id"]: ("failed", "dispatch_delivery_fuse")}
            ):
                changed = True

        updated[breach["task_id"]]["requeues"] = breach.get("requeues", 0) + 1
        updated[breach["task_id"]]["action"] = breach["action"]
        notify_dispatch_fuse(breach)

    state["dispatch_fuse"] = updated
    return changed


def check_task_stalls(tasks, state):
    """停滞检测:任务处于未终态且长时间无任何状态变迁 -> 告警一次。

    这补上了哨兵此前只扫描 pane 完成标记、对"控制面停滞"完全失明的盲区。
    """
    episodes = state.get("stalls") or {}
    alerts, updated = liveness.evaluate_task_stalls(tasks, episodes, time.time())

    for alert in alerts:
        print(
            f"[SENTINEL STALL] "
            f"task={alert['task_id']} "
            f"status={alert['status']} "
            f"idle={alert['idle_seconds']}s "
            f"workflow={alert.get('workflow_id')}",
            flush=True,
        )
        notify_stall(alert)

    state["stalls"] = updated
    return updated != episodes


def main():
    state = load_json(STATE_FILE, {"seen": {}, "nudged": {}})

    print("[HERDR SENTINEL] starting", flush=True)

    while True:
        store = _get_store()
        tasks = store.list_tasks()
        now = time.time()
        changes = {}

        for task in tasks:
            status = task.get("status")
            if status not in ACTIVE:
                continue

            task_id = task.get("task_id")
            pane_id = task.get("pane_id")

            if not task_id or not pane_id:
                continue

            state["seen"].setdefault(task_id, now)

            screen = pane_visible(pane_id)
            done_marker = f"HERDR_TASK_DONE:{task_id}"
            blocker_marker = f"HERDR_TASK_BLOCKER:{task_id}"
            orchestration_marker = f"HERDR_ORCH_TASK:{task_id}"

            if status in {"dispatched", "working"} and done_marker in screen:
                changes[task_id] = (
                    "agent_done",
                    "completion_sentinel",
                )
                continue

            # Inner loop exhausted: agent self-reported a blocker escalation.
            # Transition to 'blocked' so the Coordinator can route to human/Coordinator.
            if status in {"dispatched", "working"} and blocker_marker in screen:
                changes[task_id] = (
                    "blocked",
                    "inner_loop_exhausted",
                )
                print(
                    f"[SENTINEL BLOCKER] task={task_id} pane={pane_id} — inner loop exhausted, escalating to Coordinator",
                    flush=True,
                )
                continue

            if any(pattern in screen for pattern in CRASH_PATTERNS):
                changes[task_id] = (
                    "failed",
                    "agent_process_crash",
                )
                continue

            age = now - state["seen"][task_id]
            if (
                status == "dispatched"
                and age >= NUDGE_AFTER_SECONDS
                and task_id not in state["nudged"]
                and orchestration_marker in screen
                and agent_status(pane_id) in {"idle", "unknown", None}
            ):
                if nudge_enter(pane_id):
                    state["nudged"][task_id] = now
                    print(
                        f"[SENTINEL NUDGE] task={task_id} pane={pane_id}",
                        flush=True,
                    )

            # Check for in-flight pending steering instructions to inject on idle
            try:
                import herdr.steering as herdr_steering
                if agent_status(pane_id) in {"idle", "unknown", None} and done_marker not in screen:
                    dispatched_steer = herdr_steering.dispatch_pending_steer(task_id)
                    if dispatched_steer:
                        print(
                            f"[SENTINEL STEER] Injected pending steer {dispatched_steer['steer_id']} for task={task_id} pane={pane_id}",
                            flush=True,
                        )
            except Exception:
                pass

        fuse_changed = check_dispatch_fuse(tasks, state)

        if update_statuses(changes) or fuse_changed:
            check_task_stalls(tasks, state)
            save_json_atomic(STATE_FILE, state)
            print("[SENTINEL] State updated, controller will auto-sync via registry watcher", flush=True)
            time.sleep(1)
        else:
            check_task_stalls(tasks, state)
            save_json_atomic(STATE_FILE, state)
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
