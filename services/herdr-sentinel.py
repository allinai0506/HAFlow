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


def update_statuses(changes, expected=None, epochs=None):
    if not changes:
        return False

    store = _get_store()
    changed = False
    expected = expected or {}
    epochs = epochs or {}

    for task_id, (new_status, reason) in changes.items():
        authoritative = store.get_task(task_id)
        if not authoritative:
            print(f"[SENTINEL SKIP] task {task_id} missing from authoritative StateStore", file=sys.stderr, flush=True)
            continue

        old_status = authoritative.get("status")
        if old_status not in ACTIVE:
            continue

        # CAS: observation-epoch guard (B-1b). Same status value after a
        # human re-open still bumps updated_at, so the stale marker epoch
        # fails closed instead of re-accepting an old marker.
        exp_status = expected.get(task_id)
        if exp_status is not None and old_status != exp_status:
            _record_sentinel_event(
                store,
                authoritative,
                "completion_sentinel_cas_rejected",
                {"expected": exp_status, "authoritative": old_status, "reason": reason},
            )
            print(
                f"[SENTINEL CAS REJECTED] task {task_id}: "
                f"expected {exp_status} but authoritative is {old_status}",
                flush=True,
            )
            continue
        epoch_pair = epochs.get(task_id)
        if epoch_pair is not None:
            epoch_updated, observed_updated = epoch_pair
            try:
                current_updated = float(authoritative.get("updated_at") or 0)
            except (TypeError, ValueError):
                current_updated = 0.0
            try:
                obs_updated = float(observed_updated or 0)
            except (TypeError, ValueError):
                obs_updated = 0.0
            if obs_updated and current_updated != obs_updated:
                _record_sentinel_event(
                    store,
                    authoritative,
                    "completion_sentinel_cas_rejected",
                    {"epoch_updated": epoch_updated, "observed_updated": obs_updated,
                     "current_updated": current_updated, "reason": reason},
                )
                print(
                    f"[SENTINEL CAS REJECTED] task {task_id}: "
                    f"task rewritten after observation "
                    f"(observed updated_at={obs_updated}, now {current_updated})",
                    flush=True,
                )
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


def _record_sentinel_event(store, task, event_type, payload):
    """Best-effort observation event (never blocks the sweep)."""
    try:
        task = task or {}
        store.record_event(
            event_type,
            dict(payload or {}),
            workflow_id=task.get("workflow_id"),
            node_id=task.get("node") or task.get("stage"),
            task_id=task.get("task_id"),
            agent_id=task.get("agent"),
            source="herdr-sentinel",
        )
    except (OSError, ValueError, RuntimeError, AttributeError) as exc:
        print(f"[SENTINEL EVENT WARN] {event_type}: {exc}", file=sys.stderr, flush=True)


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

            # FR-1 triple condition (herdr/completion.py, pure).
            # Sentinel owns pane-marker detection; Controller owns final
            # arbitration of blocked upgrades (A1). Completion writes stay
            # here but are gated by absent->present + idle + 60s + epoch CAS.
            if status in {"dispatched", "working"}:
                from herdr import completion as _comp

                marker_present = done_marker in screen
                comp_state = state.setdefault("completion", {}).setdefault(
                    task_id, {}
                )
                current_updated = task.get("updated_at") or 0
                if "was_present" not in comp_state:
                    # First sight: a marker already on screen is pane-reuse
                    # residue, never a fresh completion signal.
                    comp_state["was_present"] = bool(marker_present)
                    comp_state["first_seen_at"] = None
                    comp_state["epoch_updated_at"] = (
                        current_updated if marker_present else None
                    )
                    comp_state["early_count"] = 0
                was_present = bool(comp_state.get("was_present"))
                if marker_present and not was_present:
                    comp_state["was_present"] = True
                    comp_state["first_seen_at"] = now
                    comp_state["epoch_updated_at"] = current_updated
                    comp_state["early_count"] = 0
                elif not marker_present and was_present:
                    comp_state["was_present"] = False
                    comp_state["first_seen_at"] = None
                    comp_state["epoch_updated_at"] = None
                    comp_state["early_count"] = 0
                    _record_sentinel_event(
                        store,
                        task,
                        "completion_uncertain",
                        {"reason": "marker_vanished", "task_id": task_id},
                    )
                    print(
                        f"[SENTINEL UNCERTAIN] task={task_id} marker vanished; "
                        "attention registered, no deadlock",
                        flush=True,
                    )
                    continue
                if marker_present:
                    epoch_updated = comp_state.get("epoch_updated_at")
                    try:
                        cur_upd = float(current_updated or 0)
                        epoch_upd = float(epoch_updated) if epoch_updated is not None else None
                    except (TypeError, ValueError):
                        cur_upd = 0.0
                        epoch_upd = None
                    if epoch_upd is not None and cur_upd != epoch_upd:
                        # B-1b: human re-opened the task after the marker
                        # epoch; the old marker is stale evidence.
                        comp_state["first_seen_at"] = None
                        comp_state["epoch_updated_at"] = cur_upd
                        comp_state["was_present"] = True
                        print(
                            f"[SENTINEL STALE] task={task_id} marker epoch moved "
                            f"(human re-open); old marker ignored",
                            flush=True,
                        )
                        continue
                    agent_state = agent_status(pane_id)
                    signal = _comp.classify_signal(marker_present, agent_state)
                    if signal == "unknown":
                        _record_sentinel_event(
                            store,
                            task,
                            "agent_status_unknown",
                            {"task_id": task_id, "reason": "agent_status_unreadable"},
                        )
                        continue
                    if signal == "early":
                        early_n = int(comp_state.get("early_count") or 0) + 1
                        comp_state["early_count"] = early_n
                        _record_sentinel_event(
                            store,
                            task,
                            "early_done_signal",
                            {"task_id": task_id, "count": early_n, "agent_status": agent_state},
                        )
                        if early_n == 3:
                            print(
                                f"[SENTINEL EARLY] task={task_id} marker present "
                                "while agent busy x3; coordinator hinted, no flip",
                                flush=True,
                            )
                        continue
                    # Idle path: enforce the 60s delay floor.
                    try:
                        started = float(
                            task.get("started_at")
                            or task.get("created_at")
                            or state["seen"][task_id]
                        )
                    except (TypeError, ValueError):
                        started = now
                    elapsed = now - started
                    first_seen = comp_state.get("first_seen_at")
                    if first_seen is None:
                        # Present since first sight (residue): require a fresh
                        # absent->present cycle before any accept.
                        continue
                    if _comp.should_accept(
                        marker_present=True,
                        agent_status=agent_state,
                        elapsed_seconds=elapsed,
                        is_new_or_tracked=True,
                        stale_epoch=False,
                    ):
                        changes[task_id] = ("agent_done", "completion_sentinel")
                        # Attach CAS context for update_statuses.
                        state.setdefault("completion_expected", {})[task_id] = status
                        state.setdefault("completion_epochs", {})[task_id] = (
                            comp_state.get("epoch_updated_at"),
                            current_updated,
                        )
                        state.setdefault("completion_elapsed", {})[task_id] = elapsed
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

        expected = state.get("completion_expected") or {}
        epochs = state.get("completion_epochs") or {}
        elapsed_map = state.get("completion_elapsed") or {}
        # Only completion_sentinel writes carry CAS context; other reasons
        # (blocker/crash/fuse) keep the legacy authoritative re-check.
        cas_expected = {
            tid: expected.get(tid)
            for tid, (_st, reason) in changes.items()
            if reason == "completion_sentinel" and tid in expected
        }
        cas_epochs = {
            tid: epochs.get(tid)
            for tid, (_st, reason) in changes.items()
            if reason == "completion_sentinel" and tid in epochs
        }
        wrote = update_statuses(changes, expected=cas_expected, epochs=cas_epochs)
        if wrote:
            for tid, (_st, reason) in changes.items():
                if reason != "completion_sentinel":
                    continue
                elapsed = elapsed_map.get(tid)
                task_row = store.get_task(tid)
                if task_row is not None:
                    _record_sentinel_event(
                        store,
                        task_row,
                        "completion_sentinel_accepted",
                        {"task_id": tid, "elapsed_seconds": elapsed,
                         "reason": "triple_condition_met"},
                    )
                comp_bucket = state.get("completion", {}).get(tid)
                if isinstance(comp_bucket, dict):
                    comp_bucket["was_present"] = False
                    comp_bucket["first_seen_at"] = None
                    comp_bucket["epoch_updated_at"] = None
                    comp_bucket["early_count"] = 0
            for key in ("completion_expected", "completion_epochs", "completion_elapsed"):
                state.pop(key, None)
        if wrote or fuse_changed:
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
