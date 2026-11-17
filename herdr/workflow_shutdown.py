"""Fence new launches and verify local execution has stopped before release."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

from herdr import task_resources
from herdr.agent_adapter import get_agent_adapter
from herdr.kernel import state_db_path
from herdr.node_capacity import CONCURRENT_STATUSES, pane_reference
from herdr.observer.live import _get_pane, _run_herdr
from herdr.state_db import get_workflow, transition_task, transition_workflow
from herdr.state_store import SQLiteStateStore
from herdr.transitions import TERMINAL_WORKFLOW_STATUSES

logger = logging.getLogger(__name__)


def close_pane(pane_id: str) -> None:
    # The caller checks the same native backend before and after this request.
    _run_herdr(["herdr", "pane", "close", pane_id], 5)


def _pane(pane_id):
    outcome, pane = _get_pane(_run_herdr, pane_id, 2)
    if outcome == "failed":
        raise RuntimeError("pane probe failed")
    return pane


def _original_terminal_gone(pane, terminal_id):
    if pane is None:
        return True
    current_id = pane.get("terminal_id")
    return (isinstance(current_id, str) and bool(current_id)
            and isinstance(terminal_id, str) and bool(terminal_id)
            and current_id != terminal_id)


def _terminal_processes(session_id, previous=()):
    """Track the terminal session and observed descendants, even after reparenting."""
    result = subprocess.run(
        ["ps", "-axo", "uid=,pid=,ppid=,stat="], capture_output=True,
        text=True, timeout=2, check=True,
    )
    processes = {}
    members = set(previous)
    for line in result.stdout.splitlines():
        uid, pid, parent, status = line.split()
        pid = int(pid)
        if status.startswith("Z"):
            continue
        processes[pid] = int(parent)
        if int(uid) == os.getuid() and pid not in members:
            try:
                if os.getsid(pid) == session_id:
                    members.add(pid)
            except ProcessLookupError:
                continue
    if not result.stdout.strip():
        raise RuntimeError("empty process inventory")
    frontier = members
    while frontier:
        children = {pid for pid, parent in processes.items() if parent in frontier} - members
        members |= children
        frontier = children
    return members.intersection(processes)


def _stop_runtime(store, target, *, reason, deadline, intent=None) -> str | None:
    """Stop the original execution and return its confirmed terminal ID."""
    pane_id = pane_reference(target)
    task_id = target["task_id"] if intent is None else intent["key"]
    workflow_id = target["workflow_id"]
    identity = {
        "pane_id": pane_id, "run_id": target.get("run_id"),
        "intent_id": intent["intent_id"] if intent else None,
    }
    events = store.list_events(
        workflow_id=workflow_id, task_id=task_id, event_type="shutdown_process",
        source="workflow_shutdown", limit=1, desc=True,
    )
    receipt = events[0]["payload"] if events else None
    if receipt and receipt["identity"] != identity:
        if receipt["phase"] != "PROCESS_STOPPED":
            return None
        receipt = None

    def record(phase, terminal_id, shell, pids):
        store.record_event(
            "shutdown_process", {"identity": identity, "phase": phase,
                                 "terminal_id": terminal_id, "shell": shell, "pids": sorted(pids)},
            workflow_id=workflow_id, task_id=task_id, source="workflow_shutdown",
        )

    def owned(pane):
        if not pane or not pane.get("terminal_id"):
            return False
        workspace = target.get("workspace_id") or (target.get("runtime") or {}).get("workspace_id")
        if workspace and pane.get("workspace_id") != workspace:
            return False
        if target.get("terminal_id") and pane["terminal_id"] != target["terminal_id"]:
            return False
        if (intent and target.get("phase") in {"workspace_created", "pane_allocated"}
                and target.get("pane_source") == "dynamic" and target.get("terminal_id")):
            return (bool(pane.get("cwd")) and
                    Path(pane["cwd"]).resolve() == Path(target["planned_clone_path"]).resolve())
        return task_resources.owned_live_pane(target)[0]

    pane = _pane(pane_id)
    if receipt and receipt["phase"] == "PROCESS_STOPPED":
        # A surviving/new pane with this terminal identity contradicts the receipt.
        terminal_id = receipt["terminal_id"]
        return terminal_id if _original_terminal_gone(pane, terminal_id) else None
    if pane is None and not receipt:
        logger.warning("STOP_FAILED for %s: pane missing without a process receipt", task_id)
        return None
    if receipt and _original_terminal_gone(pane, receipt["terminal_id"]):
        if _terminal_processes(receipt["shell"], receipt["pids"]):
            return None
        record("PROCESS_STOPPED", receipt["terminal_id"], receipt["shell"], receipt["pids"])
        return receipt["terminal_id"]
    if not owned(pane):
        return None

    terminal_id = pane["terminal_id"]
    code, stdout, _ = _run_herdr(["herdr", "pane", "process-info", "--pane", pane_id], 2)
    if code:
        return None
    info = json.loads(stdout)["result"]["process_info"]
    shell = info["shell_pid"]
    if (info["pane_id"] != pane_id or type(shell) is not int or shell <= 1
            or os.getsid(shell) != shell):
        return None
    current = _pane(pane_id)
    if not current or current.get("terminal_id") != terminal_id or not owned(current):
        return None
    if receipt and (receipt["terminal_id"] != terminal_id or receipt["shell"] != shell):
        return None
    pids = _terminal_processes(shell, receipt["pids"] if receipt else ())
    if shell not in pids or not info.get("foreground_processes"):
        return None
    record("STOP_PENDING", terminal_id, shell, pids)

    runtime = target.get("runtime") or {}
    adapter = get_agent_adapter(target.get("agent") or runtime.get("agent"))
    if adapter.supports_interrupt and adapter.interrupt(pane_id, reason=reason):
        logger.info("INTERRUPT_SENT for %s", task_id)
        grace = min(deadline, time.monotonic() + 0.5)
        while time.monotonic() < grace:
            live = _terminal_processes(shell, pids)
            pids |= live
            if not live - {shell}:
                break
            time.sleep(0.05)

    current = _pane(pane_id)
    original_present = not _original_terminal_gone(current, terminal_id)
    if original_present:
        if current.get("terminal_id") != terminal_id or not owned(current):
            # The Agent can disappear after a successful interrupt. The
            # unchanged terminal and an empty execution session still allow
            # closing the original shell.
            if (current.get("terminal_id") != terminal_id
                    or task_resources.owned_live_pane(target)[1] != "agent_not_found"
                    or _terminal_processes(shell, pids) != {shell}):
                return None
    pids |= _terminal_processes(shell, pids)
    record("STOP_PENDING", terminal_id, shell, pids)
    if original_present:
        latest = _pane(pane_id)
        if not _original_terminal_gone(latest, terminal_id):
            if latest.get("terminal_id") != terminal_id or time.monotonic() >= deadline:
                return None
            close_pane(pane_id)

    settle_until = min(deadline, time.monotonic() + 1.0)
    while True:
        if (_original_terminal_gone(_pane(pane_id), terminal_id)
                and not _terminal_processes(shell, pids)):
            record("PROCESS_STOPPED", terminal_id, shell, pids)
            return terminal_id
        if time.monotonic() >= settle_until:
            return None
        time.sleep(0.05)


def _latest_events(store, workflow_id, event_type, source):
    from herdr.state_db import get_readonly_db_connection

    conn = get_readonly_db_connection(store.db_path)
    try:
        return conn.execute("""
            SELECT task_id, payload_json
            FROM (
                SELECT task_id, payload_json, ROW_NUMBER() OVER (
                    PARTITION BY task_id ORDER BY timestamp DESC, id DESC
                ) AS history_rank
                FROM events
                WHERE workflow_id = ? AND event_type = ? AND source = ?
            )
            WHERE history_rank = 1
            """, (workflow_id, event_type, source)).fetchall()
    finally:
        conn.close()


def _pending_processes_stopped(store, workflow_id):
    # Other lifecycle callers can clear a Task's pane reference while a
    # shutdown is pending. Read the process receipts independently of Tasks.
    for row in _latest_events(store, workflow_id, "shutdown_process", "workflow_shutdown"):
        receipt = json.loads(row["payload_json"])
        if receipt["phase"] == "PROCESS_STOPPED":
            continue
        pane = _pane(receipt["identity"]["pane_id"])
        if (not _original_terminal_gone(pane, receipt["terminal_id"])
                or _terminal_processes(receipt["shell"], receipt["pids"])):
            return False
        store.record_event("shutdown_process", {**receipt, "phase": "PROCESS_STOPPED"},
                           workflow_id=workflow_id, task_id=row["task_id"], source="workflow_shutdown")
    return True


def drain_unsettled_launch_intents(store, workflow_id, *, reason, deadline, check_only=False):
    """Resolve each latest intent against registered tasks or actual resources."""
    for row in _latest_events(store, workflow_id, "launch_intent", "launch"):
        intent = json.loads(row["payload_json"])
        if intent["workflow_id"] != workflow_id or intent["key"] != row["task_id"]:
            return False
        if intent["phase"] in {"resources_absent", "shutdown_stopped"}:
            continue
        if intent["phase"] == "registered":
            if not task_resources.registered_launch_task(store, intent):
                return False
            continue
        if check_only or intent["phase"] != "allocating" or time.monotonic() >= deadline:
            return False
        if task_resources.registered_launch_task(store, intent):
            task_resources.finish_launch_intent(store, intent)
            continue
        if store.get_task(intent["task_id"]):
            return False
        remaining = max(0.1, deadline - time.monotonic())
        inventory = task_resources.inventory_launch_resources(intent, timeout=remaining)
        if inventory["resource_status"] == "foreign":
            return False
        resources = intent["resources"]
        tag = inventory.get("clone_tag") or {}
        pane_id = resources.get("pane_id") or tag.get("pane_id")
        # Inventory covers panes and clone paths. An allocated pane can leave
        # detached descendants even when both resources are now absent.
        if inventory["resource_status"] == "absent" and not pane_id:
            result = task_resources.reconcile_launch_intent(store, intent, lambda _: "absent")
            if result["status"] != "resources_absent":
                return False
            continue
        if not pane_id or not resources.get("run_id"):
            logger.warning("STOP_FAILED for launch %s: %s", intent["key"], inventory["reason"])
            return False
        if tag.get("pane_id") not in (None, pane_id):
            return False
        target = {**tag, **{key: value for key, value in resources.items() if value is not None},
                  "pane_id": pane_id, "task_id": intent["task_id"], "workflow_id": workflow_id}
        stopped_terminal_id = _stop_runtime(store, target, reason=reason, deadline=deadline, intent=intent)
        if not stopped_terminal_id:
            logger.warning("STOP_FAILED for launch %s: execution unverified", intent["key"])
            return False
        inventory = task_resources.inventory_launch_resources(
            intent, timeout=max(0.1, deadline - time.monotonic()), stopped_terminal_id=stopped_terminal_id,
        )
        if inventory["resource_status"] not in {"absent", "owned"}:
            logger.warning("STOP_FAILED for launch %s after close: %s", intent["key"], inventory["reason"])
            return False
        # Retain the tagged clone as evidence and keep its allocation reserved.
        phase = "resources_absent" if inventory["resource_status"] == "absent" else "shutdown_stopped"
        task_resources._launch_event(store, {**intent, "phase": phase, "abort_reason": reason})
    return True


def shutdown_workflow(
    workflow_id: str | None,
    *,
    reason: str = "workflow shutdown requested",
    outcome: str = "abandoned",
    source: str = "workflow_shutdown",
    db_path: str | Path | None = None,
    timeout: float = 10.0,
) -> str:
    """Return STOPPED only after confirmed process exit and intent settlement."""
    if not workflow_id:
        return "ALREADY_TERMINAL"

    try:
        resolved_db = Path(db_path or state_db_path()).resolve()
        store = SQLiteStateStore(str(resolved_db))
        workflow = get_workflow(workflow_id, db_path=resolved_db)
        if not workflow:
            return "ALREADY_TERMINAL"

        def transition(status, **kwargs):
            transition_workflow(
                workflow_id=workflow_id, to_status=status, reason=reason,
                source=source, force=True, db_path=resolved_db, **kwargs,
            )

        status = workflow["status"]
        if status not in TERMINAL_WORKFLOW_STATUSES and status != "closing":
            transition("closing", expected_status=status)
        with task_resources.workflow_launch_lock(resolved_db, workflow_id, timeout=timeout):
            current = get_workflow(workflow_id, db_path=resolved_db)
            tasks = store.list_tasks(workflow_id=workflow_id)
            deadline = time.monotonic() + timeout
            if current["status"] in TERMINAL_WORKFLOW_STATUSES:
                if (not any(t["status"] in CONCURRENT_STATUSES or pane_reference(t) for t in tasks)
                        and drain_unsettled_launch_intents(store, workflow_id, reason=reason,
                                                          deadline=deadline, check_only=True)
                        and _pending_processes_stopped(store, workflow_id)):
                    return "ALREADY_TERMINAL"
                transition("closing", expected_status=current["status"])
            if not drain_unsettled_launch_intents(store, workflow_id, reason=reason, deadline=deadline):
                return "STOP_FAILED"

            # Launcher registration and all shutdown side effects share this lock.
            for task in store.list_tasks(workflow_id=workflow_id):
                pane_id = pane_reference(task)
                metadata = {}
                if pane_id:
                    if (time.monotonic() >= deadline
                            or not _stop_runtime(store, task, reason=reason, deadline=deadline)):
                        logger.warning("STOP_FAILED for task %s", task["task_id"])
                        return "STOP_FAILED"
                    runtime = {**(task.get("runtime") or {}), "pane_id": None, "status": "unavailable"}
                    metadata = {"pane_id": None, "runtime": runtime, "pane_lifecycle": "released",
                                "released_pane_id": pane_id, "pane_retained": False}
                active = task["status"] in CONCURRENT_STATUSES
                if active or metadata:
                    result = transition_task(
                        task_id=task["task_id"], to_status="failed" if active else task["status"],
                        reason=reason, source=source, db_path=resolved_db, force=True,
                        expected_version=task["version"], metadata=metadata,
                    )
                    if not result["ok"]:
                        return "STOP_FAILED"
            if any(t["status"] in CONCURRENT_STATUSES or pane_reference(t)
                   for t in store.list_tasks(workflow_id=workflow_id)):
                return "STOP_FAILED"
            if not _pending_processes_stopped(store, workflow_id):
                return "STOP_FAILED"
            transition("completed", expected_status="closing", metadata={"outcome": outcome})
            return "STOPPED"
    except Exception:
        logger.exception("STOP_FAILED for workflow %s", workflow_id)
        return "STOP_FAILED"
