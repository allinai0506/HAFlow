#!/opt/homebrew/bin/python3

import hashlib
import json
import os
import sqlite3
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
    """Return one visible-screen capture; "" when the read failed.

    A failed read must look exactly like the exception path: an error
    payload on stdout (rc != 0) is not a screen, and treating it as one
    would fabricate the absent half of the blocker presence cycle (C03c).
    """
    try:
        r = run(
            ["herdr", "pane", "read", pane_id, "--source", "visible"],
            timeout=10,
        )
        if r.returncode != 0:
            return ""
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


def update_statuses(changes, expected=None, epochs=None, versions=None):
    """Apply legacy Sentinel changes through one atomic status/version CAS.

    The read above the call is only used to build an audit snapshot.  The
    authoritative compare and transition happen in the StateStore transaction;
    a concurrent human/Controller write therefore rejects the candidate rather
    than overwriting it.
    """
    if not changes:
        return False

    store = _get_store()
    changed = False
    expected = expected or {}
    epochs = epochs or {}
    versions = versions or {}

    for task_id, (new_status, reason) in changes.items():
        authoritative = store.get_task(task_id)
        if not authoritative:
            print(
                f"[SENTINEL SKIP] task {task_id} missing from authoritative StateStore",
                file=sys.stderr,
                flush=True,
            )
            continue

        old_status = authoritative.get("status")
        if old_status not in ACTIVE:
            continue
        observed_status = expected.get(task_id, old_status)
        observed_version = versions.get(task_id, authoritative.get("version"))
        epoch_pair = epochs.get(task_id)
        observed_updated = None
        if epoch_pair is not None:
            observed_updated = epoch_pair[1]
        if reason == "completion_sentinel":
            try:
                completion = store.compare_and_set_completion_transition(
                    task_id,
                    reason=reason,
                    source="herdr-sentinel",
                    expected_status=observed_status,
                    expected_version=observed_version,
                    expected_updated_at=observed_updated,
                    now=time.time(),
                )
            except (AttributeError, NotImplementedError, TypeError, ValueError, RuntimeError):
                completion = {"accepted": False, "reason": "atomic_completion_unavailable"}
            if completion.get("accepted", False):
                changed = True
            continue
        try:
            result = store.compare_and_set_task_transition(
                task_id=task_id,
                to_status=new_status,
                reason=reason,
                source="herdr-sentinel",
                metadata={
                    "sentinel_reason": reason,
                    "sentinel_updated_at": int(time.time()),
                },
                expected_status=observed_status,
                expected_version=observed_version,
                expected_updated_at=observed_updated,
            )
        except (AttributeError, TypeError):
            # Small test doubles from older integrations may not expose CAS;
            # production StateStore always does.  Keep the compatibility path
            # explicit rather than silently falling back in the real store.
            from herdr import kernel
            try:
                result = kernel.transition_task(
                    task_id=task_id,
                    to_status=new_status,
                    reason=reason,
                    source="herdr-sentinel",
                    metadata={"sentinel_reason": reason},
                    store=store,
                )
            except Exception as exc:
                print(
                    f"[SENTINEL ERROR] transition failed for {task_id}: {exc}; "
                    "task status preserved",
                    file=sys.stderr,
                    flush=True,
                )
                continue
        except Exception as exc:
            print(
                f"[SENTINEL ERROR] transition failed for {task_id}: {exc}; "
                "task status preserved",
                file=sys.stderr,
                flush=True,
            )
            continue
        if not result.get("accepted", True):
            _record_sentinel_event(
                store,
                authoritative,
                "completion_sentinel_cas_rejected",
                {
                    "expected_status": observed_status,
                    "expected_version": observed_version,
                    "authoritative_status": old_status,
                    "authoritative_version": authoritative.get("version"),
                    "reason": reason,
                },
            )
            print(
                f"[SENTINEL CAS REJECTED] task={task_id} "
                f"expected={observed_status}@{observed_version} "
                f"authoritative={old_status}@{authoritative.get('version')}",
                flush=True,
            )
            continue
        changed = True
        print(
            f"[SENTINEL STATE] {task_id}: "
            f"{old_status} -> {new_status} ({reason})",
            flush=True,
        )

    return changed


RECEIPT_V1_PROTOCOL = "receipt-v1"


def receipt_completion_hint(task):
    """Name the channel that can settle a receipt-v1 task (never the marker).

    Screen-marker CAS is structurally impossible for ``receipt-v1`` tasks:
    ``Controller.process_completion_observation`` returns early for that
    protocol, so the sample is never consumed.  Point at the structured
    receipt and the identity file instead.
    """
    task = task or {}
    task_id = task.get("task_id") or "unknown"
    identity_path = task.get("completion_identity_path")
    lines = [
        f"Task {task_id} uses the receipt-v1 completion protocol: a "
        f"HERDR_TASK_DONE screen marker alone can never settle it.",
        f"  herdr-task report-completion {task_id} --identity-file "
        f"{identity_path or '<completion_identity_path missing>'}",
        "If the credential expired: herdr-task renew-completion "
        f"{task_id} --operation-id <new-op-id>, then retry report-completion.",
        f"  herdr-task supersede {task_id} --abandon "
        f'--reason "receipt-v1 completion not submitted"  # drop the obligation',
    ]
    return "\n".join(lines)


def stall_alert_body(task, idle_seconds):
    """Actionable stall-alert body, protocol-aware.

    A stalled ``receipt-v1`` task gets the receipt-recovery commands; all
    other tasks get abandon/supersede.  Text carries copy-paste commands
    only and never auto-executes (same rule as the Controller's
    human-upgrade channel).

    Every emitted command must run as printed: the reason token carries a
    concrete value instead of an ellipsis, so pasting it cannot silently
    record ``"..."`` as the audit reason.
    """
    task = task or {}
    task_id = task.get("task_id") or "unknown"
    workflow_id = task.get("workflow_id") or "unknown"
    idle = int(idle_seconds or 0)
    if task.get("completion_protocol") == RECEIPT_V1_PROTOCOL:
        return (
            f"Task {task_id} stalled {idle}s in "
            f"{task.get('status') or 'unknown'}; "
            f"workflow={workflow_id}. "
            "No automatic recovery was run.\n"
            + receipt_completion_hint(task)
        )
    return (
        f"Task {task_id} stalled {idle}s in {task.get('status') or 'unknown'}; "
        f"workflow={workflow_id}. No automatic recovery was run.\n"
        f'  herdr-task supersede {task_id} --abandon --reason "stalled {idle}s '
        f'without progress"  # discard branch commits\n'
        f"  herdr-task close-workflow {workflow_id} --force  # direct force close"
    )


def observe_crash_pattern(task, *, changes, expected, versions):
    """Record and enqueue an infrastructure crash transition atomically later."""
    task_id = (task or {}).get("task_id")
    if not task_id:
        return False
    _record_sentinel_event(
        _get_store(),
        task,
        "agent_process_crash_observed",
        {
            "next_status": "failed",
            "reason": "agent_process_crash",
            "observed_status": task.get("status"),
            "observed_version": task.get("version"),
        },
    )
    changes[task_id] = ("failed", "agent_process_crash")
    expected[task_id] = task.get("status")
    versions[task_id] = task.get("version")
    return True


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
    except (OSError, ValueError, RuntimeError, AttributeError, sqlite3.Error) as exc:
        print(f"[SENTINEL EVENT WARN] {event_type}: {exc}", file=sys.stderr, flush=True)


def observe_completion_sample(task, store, *, now, state=None, screen=None):
    """Record one durable completion sample for a dispatched/working task.

    Extracted from the main sweep loop so the protocol routing (legacy FACT
    screen-marker path vs ``receipt-v1`` structured-receipt path) is unit
    testable.  FR-1 unchanged: Sentinel records the sample, never promotes.
    For ``receipt-v1`` tasks the screen-marker CAS announcement is
    deliberately skipped — the Controller cannot consume that sample — and
    the operator is pointed at the receipt channel exactly once per version.

    ``screen`` is the pane capture the caller already paid for; passing it in
    keeps one ``herdr pane read`` subprocess per task per sweep (the original
    inline loop's cost).  ``None`` means the capture still has to be taken.
    """
    task = task or {}
    store = store if store is not None else _get_store()
    task_id = task.get("task_id")
    pane_id = task.get("pane_id")
    if not task_id or not pane_id:
        return False
    state = state if state is not None else {}
    if screen is None:
        screen = pane_visible(pane_id)
    from herdr import completion as _comp

    done_marker_present = _comp.marker_present(screen, task_id)
    marker_present = done_marker_present
    agent_state = agent_status(pane_id)
    try:
        observation = store.observe_completion(
            task_id,
            marker_present=marker_present,
            agent_status=agent_state,
            observed_at=now,
        )
    except (AttributeError, OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        _record_sentinel_event(
            store,
            task,
            "completion_observation_failed",
            {"reason": str(exc)[:300]},
        )
        observation = {}
    if observation.get("epoch_changed"):
        _record_sentinel_event(
            store,
            task,
            "completion_observation_reset",
            {"reason": "task_epoch_changed", "version": task.get("version")},
        )
    if observation.get("uncertain"):
        _record_sentinel_event(
            store,
            task,
            "completion_uncertain",
            {"reason": "marker_vanished", "task_id": task_id},
        )
    signal = _comp.classify_signal(marker_present, agent_state)
    if signal == "unknown" and marker_present:
        _record_sentinel_event(
            store,
            task,
            "agent_status_unknown",
            {"task_id": task_id, "reason": "agent_status_unreadable"},
        )
    elif signal == "early":
        early_state = state.setdefault("completion", {}).setdefault(task_id, {})
        early_n = int(early_state.get("early_count") or 0) + 1
        early_state["early_count"] = early_n
        _record_sentinel_event(
            store,
            task,
            "early_done_signal",
            {"task_id": task_id, "count": early_n, "agent_status": agent_state},
        )
        if early_n >= 3:
            print(
                f"[SENTINEL EARLY] task={task_id} marker present while busy; "
                "Controller will arbitrate, no Sentinel flip",
                flush=True,
            )
    if observation.get("ready"):
        if task.get("completion_protocol") == RECEIPT_V1_PROTOCOL:
            announced = state.setdefault("completion", {}).setdefault(task_id, {})
            version = observation.get("observed_version")
            if announced.get("receipt_hint_version") != version:
                announced["receipt_hint_version"] = version
                _record_sentinel_event(
                    store,
                    task,
                    "receipt_v1_marker_ignored",
                    {
                        "task_id": task_id,
                        "observed_version": version,
                        "reason": "receipt_v1_requires_structured_receipt",
                    },
                )
                print(
                    f"[SENTINEL RECEIPT-V1] task={task_id} "
                    f"version={version}; screen-marker CAS is impossible for "
                    "receipt-v1 — submit the structured receipt instead "
                    "(see receipt_completion_hint)",
                    flush=True,
                )
        else:
            print(
                f"[SENTINEL COMPLETION READY] task={task_id} "
                f"version={observation.get('observed_version')}; "
                "Controller CAS pending",
                flush=True,
            )
    return True


def _screen_fingerprint(screen):
    """Content hash of one pane capture; forensic evidence, not an epoch."""
    return hashlib.sha256(screen.encode("utf-8", "replace")).hexdigest()


def _observe_blocker_presence(store, task_id, *, marker_present, event_payload=None):
    """Best-effort durable BLOCKER presence tracking (never blocks the sweep).

    For a present marker the observer also appends the
    ``blocked_marker_observed`` sample atomically on a ``record`` verdict;
    the return value is the sighting classification (or ``"skip"`` when the
    observation could not be made).
    """
    try:
        result = store.observe_blocker_marker(
            task_id,
            marker_present=marker_present,
            event_payload=event_payload,
        )
        return str((result or {}).get("action") or "skip")
    except (OSError, ValueError, RuntimeError, AttributeError, sqlite3.Error) as exc:
        print(
            f"[SENTINEL BLOCKER WARN] task={task_id} presence observe: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return "skip"


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


def notify_stall(alert, task=None):
    """Best-effort stall notification (deep-linked to the console).

    The body is protocol-aware so the operator receives a runnable recovery
    command instead of a bare notice: a ``receipt-v1`` task names the
    structured-receipt channel, all other tasks name abandon/supersede.
    Emitted text carries copy-paste commands only and never auto-executes
    (same rule as the Controller's human-upgrade channel).
    """
    try:
        import importlib

        notifier = importlib.import_module("services.herdr-notifier")
        url = notifier.build_console_url(
            workflow_id=alert.get("workflow_id"),
            task_id=alert.get("task_id"),
        )
        idle = alert.get("idle_seconds")
        try:
            idle = int(idle)
        except (TypeError, ValueError):
            idle = 0
        lines = [
            f"任务停留在 {alert.get('status')} 已 {idle}s，"
            "无任何状态推进，需要关注。",
        ]
        hint = stall_alert_body(
            dict(task or {}, task_id=alert.get("task_id"),
                 workflow_id=alert.get("workflow_id"), status=alert.get("status")),
            idle,
        )
        notifier.notify(
            "Herdr Factory · 任务停滞",
            f"{alert.get('workflow_id', 'unknown')} · {alert.get('task_id')}",
            "\n".join(lines) + "\n" + _truncate(hint, 1400),
            url=url,
        )
    except Exception as exc:
        print(f"[SENTINEL NOTIFY ERROR] {exc}", file=sys.stderr, flush=True)


def _truncate(text, limit):
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


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

    from herdr.completion import ORCH_MARKER_PREFIX, marker_present

    screen = pane_visible(pane_id)
    return {
        "has_marker": marker_present(screen, task_id, ORCH_MARKER_PREFIX),
        "agent_status": agent_status(pane_id),
    }


def reap_terminal_completion_samples(db_module=None):
    """Delete durable completion samples of tasks that reached a terminal status.

    A ``completed``/``cleaned``/``failed``/``superseded`` task can never consume
    its sample again, and its stale ``observed_version`` misleads future reads
    (observed version 9 vs actual task version 12 on a cleaned task, seen
    2026-10-09).  CAS acceptance already deletes the consumed row atomically;
    this covers every other path to a terminal status.  Returns the number of
    rows removed.

    Bounded by construction: one statement over one connection, so the cost is
    flat in the number of tasks (常驻工程约束 #8).  Filtering per task row in
    Python would open one SQLite connection per task ever created — 770 ms per
    sweep at 300 terminal tasks, growing without bound, and it would delay the
    stall alert this sweep exists to deliver.

    The status set is read from ``herdr.transitions`` (single authority) and
    handed to the data layer, which never redefines it.  No task list is
    needed, so the caller passes nothing but the injectable module.
    """
    from herdr.transitions import TERMINAL_TASK_STATUSES

    if db_module is None:
        from herdr import state_db as db_module

    try:
        return int(db_module.clear_completion_observations_for_statuses(
            sorted(TERMINAL_TASK_STATUSES)
        ) or 0)
    except (AttributeError, OSError, RuntimeError, ValueError):
        return 0


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


def check_task_stalls(tasks, state, notify_fn=None, now=None):
    """停滞检测:任务处于未终态且长时间无任何状态变迁 -> 告警一次。

    这补上了哨兵此前只扫描 pane 完成标记、对"控制面停滞"完全失明的盲区。

    The single notification entry point is :func:`notify_stall`, whose body
    is protocol-aware: a ``receipt-v1`` task gets the structured-receipt
    recovery commands, other tasks get supersede/force.  ``notify_fn`` is an
    injection seam for tests only; production routes through ``notify_stall``
    so the liveness-guard contract (one call, dedupe, episode clear) never
    diverges between test and production.
    ``now`` is injectable for deterministic tests; production passes None.
    """
    now = time.time() if now is None else float(now)
    episodes = state.get("stalls") or {}
    alerts, updated = liveness.evaluate_task_stalls(tasks, episodes, now)

    by_id = {t.get("task_id"): t for t in tasks or [] if t.get("task_id")}

    for alert in alerts:
        task = by_id.get(alert["task_id"]) or {}
        print(
            f"[SENTINEL STALL] "
            f"task={alert['task_id']} "
            f"status={alert['status']} "
            f"idle={alert['idle_seconds']}s "
            f"workflow={alert.get('workflow_id')}",
            flush=True,
        )
        if notify_fn is not None:
            notify_fn(
                alert.get("task_id"),
                alert.get("workflow_id"),
                stall_alert_body(task, alert.get("idle_seconds", 0)),
            )
        else:
            notify_stall(alert, task)

    state["stalls"] = updated
    return updated != episodes


def main():
    try:
        from herdr import service_release
        import_root = Path(service_release.__file__).resolve().parent.parent
        fingerprint = service_release.runtime_fingerprint(
            import_root, [Path(__file__).resolve(), Path(service_release.__file__).resolve(),
                          import_root / "herdr" / "state_store.py", import_root / "herdr" / "kernel.py"],
        )
    except Exception:
        fingerprint = {"running_sha": "unknown", "running_import_root": "unknown", "component_versions": {}}
    print("HERDR_RUNTIME_FINGERPRINT=" + json.dumps(
        {"service": "com.user.herdr-sentinel", "pid": os.getpid(), **fingerprint}, sort_keys=True,
    ), flush=True)
    state = load_json(STATE_FILE, {"seen": {}, "nudged": {}})

    print("[HERDR SENTINEL] starting", flush=True)

    while True:
        store = _get_store()
        try:
            tasks = store.list_tasks()
        except sqlite3.Error as exc:
            print(
                f"[SENTINEL DB WARN] list_tasks failed; preserving the loop: {exc}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(POLL_SECONDS)
            continue
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

            # FR-1: Sentinel is an observer.  It records a durable sample and
            # never promotes a task; Controller consumes the sample and owns
            # the final CAS-backed transition.  The extraction keeps the
            # protocol routing (legacy FACT vs receipt-v1) unit testable.
            if status in {"dispatched", "working"}:
                observe_completion_sample(
                    task, store, now=now, state=state, screen=screen
                )
            from herdr import completion as _comp

            done_marker_present = _comp.marker_present(screen, task_id)

            # Infrastructure crash first: a crashed process outranks blocker
            # sampling, and a screen that carries crash text must not feed
            # the blocker fingerprint (PR #161 review).
            if any(pattern in screen for pattern in CRASH_PATTERNS):
                observe_crash_pattern(
                    task,
                    changes=changes,
                    expected=state.setdefault("crash_expected", {}),
                    versions=state.setdefault("crash_versions", {}),
                )
                continue

            # Inner loop exhausted: record the observation only.  The
            # Controller transitions the task to blocked and owns arbitration.
            # C03c: the marker on screen may be residue from a pre-reopen
            # attempt, which a version-matching Controller cannot tell from a
            # fresh exhaustion.  Only a first sighting, an absent -> present
            # cycle, or a version-invalidated unconsumed sample is recorded;
            # unrelated screen changes do not prove a new occurrence.
            blocker_present = _comp.marker_present(
                screen, task_id, _comp.BLOCKER_MARKER_PREFIX
            )
            # Preserve observed absence during blocked/rework too, so a
            # later resume can recognize the next occurrence.  Failed pane
            # reads return an empty string and must not fabricate absence.
            if screen and not blocker_present:
                _observe_blocker_presence(store, task_id, marker_present=False)
            if status in {"dispatched", "working"} and blocker_present:
                blocker_action = _observe_blocker_presence(
                    store,
                    task_id,
                    marker_present=True,
                    event_payload={
                        "next_status": "blocked",
                        "reason": "inner_loop_exhausted",
                        "observed_status": status,
                        "observed_version": task.get("version"),
                        "observed_updated_at": task.get("updated_at"),
                        "screen_sha256": _screen_fingerprint(screen),
                    },
                )
                if blocker_action == "record":
                    print(
                        f"[SENTINEL BLOCKER] task={task_id} pane={pane_id} — inner loop exhausted, observation sent to Controller",
                        flush=True,
                    )
                    continue
                # Residue suppresses only the duplicate blocker sample:
                # pending-steer delivery below is an independent check
                # and must still run (PR #161 review).

            age = now - state["seen"][task_id]
            if (
                status == "dispatched"
                and age >= NUDGE_AFTER_SECONDS
                and task_id not in state["nudged"]
                and _comp.marker_present(
                    screen, task_id, _comp.ORCH_MARKER_PREFIX
                )
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
                if agent_status(pane_id) in {"idle", "unknown", None} and not done_marker_present:
                    dispatched_steer = herdr_steering.dispatch_pending_steer(task_id)
                    if dispatched_steer and dispatched_steer.get("ok"):
                        print(
                            f"[SENTINEL STEER] Injected pending steer {dispatched_steer['steer_id']} for task={task_id} pane={pane_id}",
                            flush=True,
                        )
            except Exception:
                pass

        fuse_changed = check_dispatch_fuse(tasks, state)

        expected = state.get("completion_expected") or {}
        epochs = state.get("completion_epochs") or {}
        versions = state.get("crash_versions") or {}
        elapsed_map = state.get("completion_elapsed") or {}
        # Only completion_sentinel writes carry CAS context; other reasons
        # (blocker/crash/fuse) keep the legacy authoritative re-check.
        cas_expected = {
            tid: expected.get(tid)
            for tid, (_st, reason) in changes.items()
            if tid in expected
        }
        cas_versions = {
            tid: versions.get(tid)
            for tid, (_st, reason) in changes.items()
            if tid in versions
        }
        cas_epochs = {
            tid: epochs.get(tid)
            for tid, (_st, reason) in changes.items()
            if reason == "completion_sentinel" and tid in epochs
        }
        wrote = update_statuses(
            changes,
            expected=cas_expected,
            epochs=cas_epochs,
            versions=cas_versions,
        )
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
            for key in (
                "completion_expected",
                "completion_epochs",
                "completion_elapsed",
                "crash_expected",
                "crash_versions",
            ):
                state.pop(key, None)
        try:
            reaped = reap_terminal_completion_samples()
        except Exception as exc:
            print(f"[SENTINEL REAP WARN] {exc}", file=sys.stderr, flush=True)
            reaped = 0
        if reaped:
            print(
                f"[SENTINEL REAP] removed {reaped} stale completion samples",
                flush=True,
            )
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
