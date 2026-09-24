"""Worker Intervention & Steering Mesh (herdr/steering.py).

NOTE ON PROTOCOL STATUS:
This module currently operates as a TTY-level steering prototype via AgentAdapter,
NOT a universal agent steering protocol. Heterogeneous agents (Claude, Codex,
OpenCode, Qoder, Agy, Pi) differ substantially in their handling of:
- Ctrl-C interrupt signals
- Interactive prompt injection & stdin consumption
- Multi-turn session state preservation
- Contextual session resumption

All agent-specific runtime differences, capabilities, and transport mechanisms
are formalized through herdr.agent_adapter.AgentAdapter.
"""

from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Dict, List, Optional
import uuid

from .agent_adapter import (
    AgentAdapter,
    AgentCapability,
    get_agent_adapter,
    list_agent_adapters,
)
from .state_store import get_state_store, StateStore, sync_tasks_projection

ACTIVE_STATUSES = {"dispatched", "working", "rework", "blocked", "paused", "interrupted"}


def get_tasks_file() -> Path:
    p = os.environ.get("TASKS_FILE")
    if p:
        return Path(p)
    return Path.home() / ".herdr-controller" / "tasks.json"


def get_steering_file() -> Path:
    p = os.environ.get("STEERING_FILE")
    if p:
        return Path(p)
    return Path.home() / ".herdr-controller" / "steering.json"


def _atomic_write_json(file_path: Path, data: Any) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(file_path.parent),
        prefix=f".tmp_{file_path.name}_",
    )
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, file_path)


def _sync_tasks_file(store: StateStore) -> None:
    tasks_file = get_tasks_file()
    if tasks_file.parent.exists():
        _atomic_write_json(tasks_file, store.export_tasks_json())


def _sync_steering_file(store: StateStore) -> None:
    st_file = get_steering_file()
    if st_file.parent.exists():
        _atomic_write_json(st_file, store.export_steering_json())


def load_tasks_data() -> Dict[str, Any]:
    store = get_state_store()
    return store.export_tasks_json()


def save_tasks_data(data: Dict[str, Any]) -> None:
    store = get_state_store()
    for t in data.get("tasks", []):
        if t.get("task_id") and t.get("workflow_id"):
            store.save_task(t)
    tasks_file = get_tasks_file()
    if tasks_file.parent.exists():
        _atomic_write_json(tasks_file, data)


def load_steering_data() -> Dict[str, Any]:
    store = get_state_store()
    return store.export_steering_json()


def save_steering_data(data: Dict[str, Any]) -> None:
    """Save/upsert steering queue state into StateStore and sync disk.

    History entries are strictly append-only and recorded individually via
    store.record_steering_history() to prevent duplicate exponential inflation.
    """
    store = get_state_store()
    for tid, q in data.get("steering_queues", {}).items():
        for item in q:
            item.setdefault("task_id", tid)
            store.save_steer(item)
    st_file = get_steering_file()
    if st_file.parent.exists():
        _atomic_write_json(st_file, store.export_steering_json())


def _task_event_context(task: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not task:
        return {}
    return {
        "workflow_id": task.get("workflow_id"),
        "node_id": task.get("node") or task.get("stage"),
        "agent_id": task.get("agent"),
    }


def format_steer_prompt(instruction: str, operator: str = "human") -> str:
    """Format structured high-priority intervention prompt for Agent."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "【总指挥实时插话纠偏指令 - STEERING INSTRUCTION】\n"
        f"发起人：{operator} | 时间：{now_str}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "总指挥向你发送了高优先级干预指令，请立即优先吸收并按此调整后续动作：\n"
        f"> {instruction}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )


def queue_steer(
    task_id: str,
    instruction: str,
    operator: str = "human",
    urgent: bool = False,
    execute_dispatch: bool = True,
) -> Dict[str, Any]:
    """Queue a steering instruction for a task. If urgent, dispatch immediately."""
    instruction = (instruction or "").strip()
    if not instruction:
        raise ValueError("Instruction cannot be empty")

    # T3b hygiene: steering is a second prompt sink; strip task-id markers.
    sanitized_markers = 0
    try:
        from herdr import completion as _completion

        instruction, sanitized_markers = _completion.sanitize_completion_marker(
            instruction, task_id
        )
    except (ImportError, AttributeError, ValueError):
        sanitized_markers = 0

    tasks_data = load_tasks_data()
    task = next((t for t in tasks_data.get("tasks", []) if t.get("task_id") == task_id), None)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")

    status = task.get("status")
    if status not in ACTIVE_STATUSES:
        raise ValueError(f"Task '{task_id}' is in inactive status '{status}' and cannot receive steering instructions")

    steer_id = f"str-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    steer_item = {
        "steer_id": steer_id,
        "task_id": task_id,
        "instruction": instruction,
        "operator": operator,
        "urgent": urgent,
        "created_at": time.time(),
        "status": "pending",
        "dispatched_at": None,
    }
    if sanitized_markers:
        steer_item["sanitized_markers"] = sanitized_markers

    s_data = load_steering_data()
    q = s_data.setdefault("steering_queues", {}).setdefault(task_id, [])
    q.append(steer_item)
    save_steering_data(s_data)

    if urgent and execute_dispatch:
        return dispatch_steer_now(task_id, steer_id)

    return {
        "ok": True,
        "steer_id": steer_id,
        "task_id": task_id,
        "urgent": urgent,
        "status": "queued",
    }


def get_task_adapter(task_id: str) -> AgentAdapter:
    """Resolve the AgentAdapter for a given task, falling back to unknown (fail-closed)."""
    tasks_data = load_tasks_data()
    task = next((t for t in tasks_data.get("tasks", []) if t.get("task_id") == task_id), None)
    agent_name = task.get("agent") if task else None
    return get_agent_adapter(agent_name)


def dispatch_steer_now(task_id: str, steer_id: str) -> Dict[str, Any]:
    """Immediately dispatch a specific steer item via the task's AgentAdapter.

    Status semantics:
      "dispatched" — delivery was attempted AND succeeded (pane_delivery_ok=True).
      "pending"    — no pane, or delivery failed; item stays pending for retry.
                     last_delivery_error is recorded for observability.

    Anti-drift safety invariant:
      If urgent steering succeeds in interrupting (Ctrl-C) but fails to inject
      prompt, the physical Agent is already stopped. The task MUST transition to
      "interrupted" with requires_attention=True to prevent fact drift against SQLite.
    """
    s_data = load_steering_data()
    q = s_data.get("steering_queues", {}).get(task_id, [])
    item = next((x for x in q if x.get("steer_id") == steer_id), None)
    if not item:
        raise ValueError(f"Steer item '{steer_id}' not found for task '{task_id}'")

    tasks_data = load_tasks_data()
    task = next((t for t in tasks_data.get("tasks", []) if t.get("task_id") == task_id), None)
    pane_id = task.get("pane_id") if task else None
    agent_name = task.get("agent") if task else None
    adapter = get_agent_adapter(agent_name)

    now = time.time()
    old_status = task.get("status") if task else None

    # 1. Attempt physical delivery via AgentAdapter
    if pane_id:
        steer_result = adapter.steer_urgent(
            pane_id,
            instruction=item["instruction"],
            operator=item.get("operator", "human"),
            wait_after_interrupt=0.1,
        )
        delivery_attempted = True
        delivery_ok = bool(steer_result.get("ok", False))
        pane_delivery_ok = delivery_ok
    else:
        steer_result = {
            "ok": False,
            "interrupted": False,
            "injected": False,
            "reason": "no_pane_id",
            "detail": f"Task '{task_id}' has no assigned pane_id",
        }
        delivery_attempted = False
        delivery_ok = False
        pane_delivery_ok = False

    # 2. Update steer item status based on delivery outcome
    item["protocol"] = adapter.protocol_level
    item["adapter"] = adapter.name
    item["last_delivery_attempt"] = now
    item["delivery_attempt_count"] = item.get("delivery_attempt_count", 0) + 1

    if delivery_ok:
        item["status"] = "dispatched"
        item["dispatched_at"] = now
        item.pop("last_delivery_error", None)
    else:
        # Keep pending; record failure reason for retry / observability
        error_reason = (
            steer_result.get("reason", "delivery_failed") if delivery_attempted
            else "no_pane_id"
        )
        item["status"] = "pending"
        item["last_delivery_error"] = error_reason

    # 3. Handle partial urgent failure: interrupt succeeded, but prompt injection failed!
    # Agent was physically halted by Ctrl-C; advance task to interrupted to prevent fact drift.
    if not delivery_ok and steer_result.get("interrupted"):
        if task:
            tid = task.get("task_id")
            meta = {
                "interrupt_reason": "urgent_steer_injection_failed",
                "interrupted_by": item.get("operator", "human"),
                "interrupted_at": now,
                "requires_attention": True,
                "protocol": adapter.protocol_level,
                "adapter": adapter.name,
            }
            try:
                from herdr import kernel
                store = get_state_store()
                kernel.transition_task(
                    task_id=tid,
                    to_status="interrupted",
                    reason="urgent_steer_injection_failed",
                    source="steering",
                    metadata=meta,
                    store=store,
                )
            except Exception as exc:
                item["transition_error"] = str(exc)
                # Fail-closed: do NOT directly mutate task status or call save_tasks_data

    # 4. Append-only record to StateStore audit history (zero duplication)
    store = get_state_store()
    history_entry = {
        "action": "steer_dispatched" if delivery_ok else "steer_delivery_failed",
        "steer_id": steer_id,
        "task_id": task_id,
        **_task_event_context(task),
        "instruction": item["instruction"],
        "operator": item.get("operator"),
        "urgent": item.get("urgent", False),
        "protocol": adapter.protocol_level,
        "adapter": adapter.name,
        "delivery_ok": delivery_ok,
        "delivery_attempted": delivery_attempted,
        "interrupted": steer_result.get("interrupted", False),
        "injected": steer_result.get("injected", False),
        "error": steer_result.get("reason"),
        "timestamp": now,
    }
    store.record_steering_history(history_entry)
    save_steering_data(s_data)

    # 5. Record on task entity steering_history only when successfully delivered
    if task and delivery_ok:
        steering_history = list(task.get("steering_history") or [])
        steering_history.append({
            "steer_id": steer_id,
            "instruction": item["instruction"],
            "operator": item.get("operator"),
            "urgent": item.get("urgent", False),
            "protocol": adapter.protocol_level,
            "adapter": adapter.name,
            "dispatched_at": now,
        })
        store.update_task_metadata(task_id, {
            "last_steered_at": now,
            "steering_history": steering_history,
        })
        tasks_file = get_tasks_file()
        if tasks_file.parent.exists():
            sync_tasks_projection(store=store, tasks_file=tasks_file)

    return {
        "ok": delivery_ok,
        "steer_id": steer_id,
        "task_id": task_id,
        "urgent": item.get("urgent", False),
        "status": "dispatched" if delivery_ok else "pending",
        "protocol": adapter.protocol_level,
        "adapter": adapter.name,
        "pane_delivery_ok": pane_delivery_ok,
        "delivery_attempted": delivery_attempted,
        "interrupted": steer_result.get("interrupted", False),
        "injected": steer_result.get("injected", False),
        "reason": steer_result.get("reason"),
        "dispatched_at": item.get("dispatched_at"),
    }


def dispatch_pending_steer(task_id: str, steer_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Dispatch the next pending steer item via task's AgentAdapter (soft non-interrupting).

    Status semantics:
      "dispatched" — delivery was attempted AND succeeded (pane_delivery_ok=True).
      "pending"    — no pane, delivery failed, or soft steer not supported;
                     item stays pending for retry or escalation.
    """
    s_data = load_steering_data()
    q = s_data.get("steering_queues", {}).get(task_id, [])
    if not q:
        return None

    target_item = None
    if steer_id:
        target_item = next((x for x in q if x.get("steer_id") == steer_id and x.get("status") == "pending"), None)
    else:
        target_item = next((x for x in q if x.get("status") == "pending"), None)

    if not target_item:
        return None

    tasks_data = load_tasks_data()
    task = next((t for t in tasks_data.get("tasks", []) if t.get("task_id") == task_id), None)
    pane_id = task.get("pane_id") if task else None
    agent_name = task.get("agent") if task else None
    adapter = get_agent_adapter(agent_name)

    now = time.time()
    if pane_id:
        steer_result = adapter.steer_soft(
            pane_id,
            instruction=target_item["instruction"],
            operator=target_item.get("operator", "human"),
        )
        delivery_attempted = True
        delivery_ok = bool(steer_result.get("ok", False))
        pane_delivery_ok = delivery_ok
    else:
        steer_result = {
            "ok": False,
            "interrupted": False,
            "injected": False,
            "reason": "no_pane_id",
            "detail": f"Task '{task_id}' has no assigned pane_id",
        }
        delivery_attempted = False
        delivery_ok = False
        pane_delivery_ok = False

    target_item["protocol"] = adapter.protocol_level
    target_item["adapter"] = adapter.name
    target_item["last_delivery_attempt"] = now
    target_item["delivery_attempt_count"] = target_item.get("delivery_attempt_count", 0) + 1

    if delivery_ok:
        target_item["status"] = "dispatched"
        target_item["dispatched_at"] = now
        target_item.pop("last_delivery_error", None)
    else:
        target_item["status"] = "pending"
        target_item["last_delivery_error"] = steer_result.get("reason", "delivery_failed")

    # Append-only record to StateStore audit history (zero duplication)
    store = get_state_store()
    history_entry = {
        "action": "steer_dispatched" if delivery_ok else "steer_delivery_failed",
        "steer_id": target_item["steer_id"],
        "task_id": task_id,
        **_task_event_context(task),
        "instruction": target_item["instruction"],
        "operator": target_item.get("operator"),
        "urgent": target_item.get("urgent", False),
        "protocol": adapter.protocol_level,
        "adapter": adapter.name,
        "delivery_ok": delivery_ok,
        "delivery_attempted": delivery_attempted,
        "error": steer_result.get("reason"),
        "timestamp": now,
    }
    store.record_steering_history(history_entry)
    save_steering_data(s_data)

    if task and delivery_ok:
        steering_history = list(task.get("steering_history") or [])
        steering_history.append({
            "steer_id": target_item["steer_id"],
            "instruction": target_item["instruction"],
            "operator": target_item.get("operator"),
            "urgent": target_item.get("urgent", False),
            "protocol": adapter.protocol_level,
            "adapter": adapter.name,
            "dispatched_at": now,
        })
        store.update_task_metadata(task_id, {
            "last_steered_at": now,
            "steering_history": steering_history,
        })
        tasks_file = get_tasks_file()
        if tasks_file.parent.exists():
            sync_tasks_projection(store=store, tasks_file=tasks_file)

    return {
        "ok": delivery_ok,
        "steer_id": target_item["steer_id"],
        "task_id": task_id,
        "status": "dispatched" if delivery_ok else "pending",
        "protocol": adapter.protocol_level,
        "adapter": adapter.name,
        "pane_delivery_ok": pane_delivery_ok,
        "delivery_attempted": delivery_attempted,
        "reason": steer_result.get("reason"),
        "dispatched_at": target_item.get("dispatched_at"),
    }


def halt_task(
    task_id: str,
    reason: str = "human interrupt",
    operator: str = "human",
    execute_kill: bool = True,
) -> Dict[str, Any]:
    """Perform a graceful soft halt (SIGINT / ctrl-c) on a task via its AgentAdapter.

    Safety contract:
      If interrupt fails (pane not found, agent unsupported, or signal failure),
      the task status MUST NOT transition to "interrupted". Task remains in its
      current status to prevent runtime fact drift.
    """
    tasks_data = load_tasks_data()
    task = next((t for t in tasks_data.get("tasks", []) if t.get("task_id") == task_id), None)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")

    pane_id = task.get("pane_id")
    agent_name = task.get("agent")
    adapter = get_agent_adapter(agent_name)

    now = time.time()
    old_status = task.get("status")

    if execute_kill:
        if not pane_id:
            interrupt_ok = False
            error_reason = "no_pane_id"
        elif not adapter.supports_interrupt:
            interrupt_ok = False
            error_reason = f"agent_{adapter.name}_does_not_support_interrupt"
        else:
            interrupt_ok = bool(adapter.interrupt(pane_id, reason=reason))
            error_reason = None if interrupt_ok else "interrupt_signal_failed"
    else:
        # State-only transition without physical kill
        interrupt_ok = True
        error_reason = None

    s_data = load_steering_data()
    store = get_state_store()

    if not interrupt_ok:
        # Interrupt FAILED: Task status MUST NOT transition to interrupted!
        history_entry = {
            "action": "task_halt_failed",
            "task_id": task_id,
            **_task_event_context(task),
            "reason": reason,
            "error": error_reason,
            "operator": operator,
            "protocol": adapter.protocol_level,
            "adapter": adapter.name,
            "timestamp": now,
        }
        store.record_steering_history(history_entry)
        save_steering_data(s_data)

        return {
            "ok": False,
            "task_id": task_id,
            "status": old_status,
            "error": error_reason,
            "reason": reason,
            "operator": operator,
            "protocol": adapter.protocol_level,
            "adapter": adapter.name,
            "timestamp": now,
        }

    # Interrupt succeeded: proceed to update task status
    meta = {
        "interrupt_reason": reason,
        "interrupted_by": operator,
        "interrupted_at": now,
        "protocol": adapter.protocol_level,
        "adapter": adapter.name,
    }
    try:
        from herdr import kernel
        store = get_state_store()
        kernel.transition_task(
            task_id=task_id,
            to_status="interrupted",
            reason=reason,
            source="steering",
            metadata=meta,
            store=store,
        )
    except Exception as exc:
        return {
            "ok": False,
            "task_id": task_id,
            "status": old_status,
            "error": f"transition_task_failed: {exc}",
            "reason": reason,
            "operator": operator,
            "protocol": adapter.protocol_level,
            "adapter": adapter.name,
            "timestamp": now,
        }

    history_entry = {
        "action": "task_halted",
        "task_id": task_id,
        **_task_event_context(task),
        "reason": reason,
        "operator": operator,
        "protocol": adapter.protocol_level,
        "adapter": adapter.name,
        "timestamp": now,
    }
    store.record_steering_history(history_entry)
    save_steering_data(s_data)

    return {
        "ok": True,
        "task_id": task_id,
        "status": "interrupted",
        "reason": reason,
        "operator": operator,
        "protocol": adapter.protocol_level,
        "adapter": adapter.name,
        "timestamp": now,
    }


def list_task_steers(task_id: str) -> List[Dict[str, Any]]:
    """List all steer instructions for a given task."""
    s_data = load_steering_data()
    return s_data.get("steering_queues", {}).get(task_id, [])
