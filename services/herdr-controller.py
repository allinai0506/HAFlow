#!/usr/bin/env python3

import json
import hashlib
import re
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import threading
import time
import uuid

import sys
HERDR_ROOT = Path(__file__).resolve().parent.parent
if str(HERDR_ROOT) not in sys.path:
    sys.path.insert(0, str(HERDR_ROOT))

SOCKET_PATH = os.path.expanduser("~/.config/herdr/herdr.sock")
TASKS_FILE = os.environ.get("TASKS_FILE") or os.path.expanduser("~/.herdr-controller/tasks.json")
_task_bin = HERDR_ROOT / "bin" / "herdr-task"
TASK_MANAGER = str(_task_bin) if _task_bin.exists() else os.path.expanduser("~/HAFlow/bin/herdr-task")
try:
    from herdr.projects import (
        project_for_workflow,
        workflow_config_for,
    )
    from herdr.workflow import find_node, get_ready_nodes, is_workflow_completed
    from herdr.state_store import get_state_store
    from herdr.observation import ObservationStore, create_verification_observation_with_status
    from herdr.trajectory import (
        TrajectoryLedger,
        record_observation_created,
        record_trajectory_event,
    )
    from herdr import liveness
except ImportError:
    from herdr_projects import (
        project_for_workflow,
        workflow_config_for,
    )
    from herdr_workflow import find_node, get_ready_nodes, is_workflow_completed
    from herdr_state_store import get_state_store
    from herdr_observation import ObservationStore, create_verification_observation_with_status
    from herdr.trajectory import (
        TrajectoryLedger,
        record_observation_created,
        record_trajectory_event,
    )
    from herdr import liveness

try:
    from herdr import direct_dispatch as direct_dispatch_planner
except Exception:
    # 纯函数决策模块缺失时退回总指挥路径,绝不阻塞控制面。
    direct_dispatch_planner = None

reverification_mod = None

try:
    from herdr import scheduler as scheduler_core
    from herdr import scheduler_facts as scheduler_facts_store
    from herdr import delivery_record as delivery_record_mod
    from herdr import workflow_docs as workflow_docs_mod
except Exception:
    # Scheduler v1 可选:缺失时退回原推进语义,绝不阻塞控制面。
    scheduler_core = None
    scheduler_facts_store = None
    delivery_record_mod = None
    workflow_docs_mod = None

try:
    from herdr import reverification as reverification_mod
except Exception:
    # Selective Reverification (PR #108) is optional on top of the scheduler:
    # without it every verifier re-runs, which is exactly the pre-#108
    # behaviour. A separate import block so a missing planner cannot also
    # disable the #107 candidate / join-gate machinery.
    reverification_mod = None

try:
    from herdr import selective_replan as selective_replan_core
except Exception:
    # Selective Replan (PR #110) is optional: without it every blocked gate
    # falls back to the legacy whole-node fix-loop, exactly the pre-#110
    # behaviour. A separate import block so a missing module cannot also
    # disable the #107/#108 machinery.
    selective_replan_core = None

try:
    from herdr.git_coordination import ensure_no_git_processes
except Exception:
    ensure_no_git_processes = None

try:
    from herdr.supervisor import harness as supervisor_harness
except Exception:
    # Semantic Supervisor 是可选观察层;缺失或异常时原有流程完全不变。
    supervisor_harness = None

try:
    from herdr.observer import harness as observer_harness
except Exception:
    # Trajectory Observer 是可选旁路观察层;缺失或异常时原有流程完全不变。
    observer_harness = None

STAGE_STATE_FILE = os.environ.get("STAGE_STATE_FILE") or os.path.expanduser(
    "~/.herdr-controller/stage-state.json"
)

STAGE_POLICIES_FILE = os.path.expanduser(
    "~/.herdr-controller/stage-policies.json"
)

COORDINATOR_PANE = "w6:p1H"

WORKFLOWS_FILE = os.environ.get("WORKFLOWS_FILE") or os.path.expanduser("~/.herdr-controller/workflows.json")

# ============================================================
# Liveness guard (SLA / attention episodes / hygiene)
# ============================================================

ATTENTION_FILE = os.environ.get("HERDR_ATTENTION_FILE") or os.path.expanduser(
    "~/.herdr-controller/attention.json"
)

_attention_store = liveness.EpisodeStore(ATTENTION_FILE)

# 夹具/临时 workflow 只提示一次，避免每 2s sweep 刷屏。
_foreign_workflows_logged = set()
# 完成日志闩：close 失败也不得每 sweep 重刷 [WORKFLOW COMPLETE]。
_workflow_complete_logged = set()
# 监听订阅退避：task_id -> (attempt, next_allowed_at, status_signature)
_listener_backoff = {}
_listener_giveup_logged = set()

# 终化重试耗尽的任务:只告警一次,避免每轮 sweep 刷屏。
_finalize_retry_exhausted_logged = set()

# Blocked 观测的 CAS 风暴护栏：task_id -> 该样本的 observed_version。
# stale 只提示一次(等 Sentinel 补新样本)，非 stale 拒绝每样本只记一条事件。
_blocked_observation_stale = {}
_blocked_observation_rejected = {}

# git 终化未收敛而推迟 close 的 workflow:只提示一次,避免每 sweep 刷屏。
_close_deferred_logged = set()

# 已触发过 close-workflow 的 workflow,防止轮询期间重复派发。
_workflow_close_inflight = set()

# Blocked prompt delivery can wait on an external Agent transport.  Keep it
# off the registry watcher thread so one stalled pane cannot freeze completion,
# retry, escalation, and Observer sweeps for every other task.
_BLOCKED_SLA_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="blocked-sla",
)
_blocked_sla_scheduled = set()
_blocked_sla_schedule_lock = threading.Lock()


def schedule_blocked_sla_task(task, now=None):
    """Schedule one bounded blocked-SLA step without blocking the watcher."""
    task_id = str((task or {}).get("task_id") or "")
    if not task_id:
        return False
    with _blocked_sla_schedule_lock:
        if task_id in _blocked_sla_scheduled:
            return False
        _blocked_sla_scheduled.add(task_id)

    snapshot = dict(task or {})

    def _run():
        try:
            process_blocked_sla_task(snapshot, now=now)
        except (OSError, RuntimeError, ValueError, AttributeError) as exc:
            print(f"[BLOCKED SLA ASYNC ERROR] task={task_id}: {exc}")
        finally:
            with _blocked_sla_schedule_lock:
                _blocked_sla_scheduled.discard(task_id)

    try:
        _BLOCKED_SLA_EXECUTOR.submit(_run)
    except (RuntimeError, OSError):
        with _blocked_sla_schedule_lock:
            _blocked_sla_scheduled.discard(task_id)
        return False
    return True


def attention_get(key):
    return _attention_store.get(key)


def attention_note(key, task, event_type, reason, attempts=None, next_retry_at=None, detail=None):
    """Record/refresh an attention episode for a stalled or undeliverable event."""
    episode = _attention_store.get(key) or {}
    now = time.time()
    fields = {
        "task_id": task.get("task_id"),
        "workflow_id": task.get("workflow_id"),
        "event_type": event_type,
        "reason": reason,
        "last_attempt_at": now,
    }
    if attempts is not None:
        fields["attempts"] = int(attempts)
    if next_retry_at is not None:
        fields["next_retry_at"] = float(next_retry_at)
    if detail:
        fields["detail"] = detail
    if not episode.get("first_seen_at"):
        fields["first_seen_at"] = now
    return _attention_store.upsert(key, fields)


def attention_clear(key):
    return _attention_store.clear(key)


def attention_blocks_retry(key, now=None):
    return liveness.blocks_retry(_attention_store, key, now)


def attention_throttle(key, interval=None, now=None):
    liveness.throttle_retry(_attention_store, key, interval=interval, now=now)


def notify_attention(title, task, message, reason):
    """Best-effort macOS notification; never let notifier failure break control flow."""
    try:
        import importlib

        notifier = importlib.import_module("services.herdr-notifier")
        url = notifier.build_console_url(
            workflow_id=task.get("workflow_id"), task_id=task.get("task_id")
        )
        return bool(notifier.notify(title, f"{task.get('workflow_id', 'unknown')} · {reason}", message, url=url))
    except Exception as exc:
        print(f"[ATTENTION NOTIFY ERROR] {exc}")
        return False


def _blocked_sla_key(task_id):
    return f"{task_id}:blocked_sla"


def _notify_blocked_human_upgrade(task, episode_id, active_seconds):
    """Send the independent human escalation notification.

    The dedicated channel is not the notifier's task-status change throttle.
    Return ``True`` only when the channel accepted the message; the caller
    records a durable failure event when it did not.
    """
    try:
        import importlib

        notifier = importlib.import_module("services.herdr-notifier")
        task_id = task.get("task_id", "unknown")
        workflow_id = task.get("workflow_id", "unknown")
        url = notifier.build_console_url(workflow_id=workflow_id, task_id=task_id)
        body = (
            f"Task {task_id} blocked {int(active_seconds)}s "
            f"(episode {episode_id}); the one automatic re-push budget is "
            "exhausted.\n"
            "Human confirmation required; no destructive command was run.\n"
            f"  herdr-task set {task_id} working  # after human guidance\n"
            f"  herdr-task supersede {task_id} --reason ...  # discard branch commits\n"
            f"  herdr-task close-workflow {workflow_id} --force  # direct force close"
        )
        notify_fn = getattr(notifier, "notify_human_upgrade", None)
        if callable(notify_fn):
            delivered = notify_fn(task_id, workflow_id, body, url=url)
        else:
            delivered = notifier.notify(
                "Herdr Factory · 阻塞升级（需人工）",
                f"{workflow_id} · {task_id}",
                body,
                url=url,
            )
        return delivered is not False
    except (OSError, ValueError, RuntimeError, AttributeError) as exc:
        print(f"[BLOCKED SLA NOTIFY ERROR] {exc}")
        return False


def _send_blocked_repush(task, decision):
    """Send the one permitted automatic prompt re-push."""
    pane_id = task.get("pane_id")
    if not pane_id:
        return False, "pane_id_missing"
    number = int(decision.get("repush_number") or 1)
    message = (
        f"第 {number} 次自动重推（blocked episode "
        f"{decision.get('episode_id', '')}）。\n"
        "这是本 episode 唯一一次自动重推；请先阅读 BLOCKER.md，"
        "若仍无法恢复，等待第二 SLA 的人类升级，不要创建新 Task。"
    )
    try:
        result = subprocess.run(
            [
                "herdr", "agent", "prompt", pane_id, message,
                "--wait", "--timeout", "120000",
            ],
            text=True,
            capture_output=True,
            timeout=130,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        return False, str(exc)[:500]
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip() or
                  f"exit={result.returncode}")[:500]
        return False, detail
    return True, (result.stdout.strip() or "delivered")[:500]


def _blocked_sla_step(task, now, poll_seconds=3.0):
    """Compatibility read/advance helper; production claims use the atomic path."""
    from herdr import blocked_sla as _sla

    task = task or {}
    key = _blocked_sla_key(str(task.get("task_id") or ""))
    with _attention_store.transaction() as episodes:
        episode = dict(episodes.get(key) or {})
        if not _blocked_episode_matches_task(episode, task):
            episode = _sla.new_episode(task, now)
        else:
            episode["run_id"] = task.get("run_id")
            episode["workflow_id"] = task.get("workflow_id")
            try:
                last_tick = float(episode.get("last_tick_at") or now)
                active = float(episode.get("active_seconds") or 0)
            except (TypeError, ValueError):
                last_tick = now
                active = 0.0
            episode["active_seconds"] = active + _sla.active_tick_increment(
                max(0.0, now - last_tick), poll_seconds=poll_seconds
            )
            episode["last_tick_at"] = now
        decision = _sla.decide_blocked_action(
            task=task, episode=episode, now=now
        )
        decision["claimed"] = False
        episodes[key] = episode
        return decision


def _blocked_episode_matches_task(episode, task):
    """Return whether a persisted SLA episode belongs to this task epoch."""
    if not isinstance(episode, dict) or not episode:
        return False
    try:
        same_entry = float(episode.get("entry_updated_at")) == float(task.get("updated_at"))
    except (TypeError, ValueError):
        same_entry = False
    try:
        same_version = int(episode.get("entry_version")) == int(task.get("version"))
    except (TypeError, ValueError):
        same_version = episode.get("entry_version") is None and task.get("version") is None
    episode_run = episode.get("run_id")
    task_run = task.get("run_id")
    same_run = not episode_run or str(episode_run) == str(task_run)
    episode_workflow = episode.get("workflow_id")
    task_workflow = task.get("workflow_id")
    same_workflow = not episode_workflow or str(episode_workflow) == str(task_workflow)
    return same_entry and same_version and same_run and same_workflow


def _claim_blocked_sla_action(task, now, poll_seconds=3.0):
    """Atomically advance and claim one SLA side effect for an episode.

    The episode file is the cross-process authority.  Selection, budget
    reservation, and the claim lease happen under one EpisodeStore transaction
    so two Controller instances cannot both send the one permitted prompt.
    """
    from herdr import blocked_sla as _sla

    task = task or {}
    task_id = str(task.get("task_id") or "")
    key = _blocked_sla_key(task_id)
    now = float(now)
    claim_id = uuid.uuid4().hex
    with _attention_store.transaction() as episodes:
        episode = dict(episodes.get(key) or {})
        if not _blocked_episode_matches_task(episode, task):
            episode = _sla.new_episode(task, now)
        else:
            episode["run_id"] = task.get("run_id")
            episode["workflow_id"] = task.get("workflow_id")
            try:
                last_tick = float(episode.get("last_tick_at") or now)
            except (TypeError, ValueError):
                last_tick = now
            episode["active_seconds"] = float(episode.get("active_seconds") or 0) + _sla.active_tick_increment(
                max(0.0, now - last_tick), poll_seconds=poll_seconds
            )
            episode["last_tick_at"] = now

        decision = _sla.decide_blocked_action(
            task=task, episode=episode, now=now
        )
        action = decision.get("action")
        decision["claimed"] = False

        # A live claim blocks every competing side effect for the episode,
        # including a later SLA escalation.
        active_repush_claim = episode.get("repush_claim")
        active_human_claim = episode.get("human_escalation_claim")
        if (
            _sla.claim_is_active(
                active_repush_claim,
                now,
                lease_seconds=_sla.REPUSH_CLAIM_LEASE_SECONDS,
            )
            or _sla.claim_is_active(
                active_human_claim,
                now,
                lease_seconds=_sla.HUMAN_ESCALATION_CLAIM_LEASE_SECONDS,
            )
        ):
            decision["action"] = "suppressed_inflight"
            decision["reason"] = "durable_sla_claim_inflight"
            episodes[key] = episode
            return episode, decision

        if action in {"repush", "repush_recover"}:
            previous_state = str(episode.get("repush_state") or "pending")
            recovery = (
                action == "repush_recover"
                or previous_state == "failed"
                or previous_state == "in_flight"
            )
            attempts = int(episode.get("recovery_attempts") or 0)
            if recovery and attempts >= _sla.MAX_REPUSH_DELIVERY_RETRIES:
                decision["action"] = "suppressed_bounds"
                decision["reason"] = "repush_recovery_budget_exhausted"
            else:
                if recovery:
                    episode["recovery_attempts"] = attempts + 1
                episode["repush_claim"] = _sla.new_claim(
                    claim_id,
                    now,
                    lease_seconds=_sla.REPUSH_CLAIM_LEASE_SECONDS,
                )
                episode["repush_state"] = "in_flight"
                episode["repush_inflight_at"] = now
                episode["last_action_at"] = now
                episode["last_repush_at"] = now
                decision["claim_id"] = claim_id
                decision["recovery"] = recovery
                if recovery:
                    decision["action"] = "repush_recover"
                decision["claimed"] = True
        elif action == "escalate":
            attempts = int(episode.get("human_escalation_attempts") or 0)
            if attempts > _sla.MAX_HUMAN_ESCALATION_DELIVERY_RETRIES:
                decision["action"] = "suppressed_bounds"
                decision["reason"] = "human_escalation_delivery_budget_exhausted"
            else:
                episode["human_escalation_attempts"] = attempts + 1
                episode["human_escalation_claim"] = _sla.new_claim(
                    claim_id,
                    now,
                    lease_seconds=_sla.HUMAN_ESCALATION_CLAIM_LEASE_SECONDS,
                )
                episode["human_escalation_state"] = "in_flight"
                episode["last_action_at"] = now
                decision["claim_id"] = claim_id
                decision["claimed"] = True

        episodes[key] = episode
        return episode, decision


def _blocked_task_claim_is_current(task, decision):
    """Revalidate task/run/pane identity immediately before external I/O."""
    if not decision.get("claimed"):
        return True, "not_claimed"
    task_id = str(task.get("task_id") or "")
    try:
        authoritative = _get_store().get_task(task_id)
    except (AttributeError, OSError, RuntimeError, ValueError):
        return False, "task_read_failed"
    if not authoritative:
        return False, "task_missing"
    if authoritative.get("status") != "blocked":
        return False, "task_status_changed"
    if _task_version(authoritative) != _task_version(task):
        return False, "task_version_changed"
    if authoritative.get("pane_id") != task.get("pane_id"):
        return False, "task_pane_changed"
    if task.get("run_id") and authoritative.get("run_id") != task.get("run_id"):
        return False, "task_run_changed"
    if task.get("workflow_id") and authoritative.get("workflow_id") != task.get("workflow_id"):
        return False, "task_workflow_changed"
    return True, "current"


def _release_stale_blocked_claim(task, decision, reason):
    """Release a claim without charging its one-shot budget."""
    key = _blocked_sla_key(str(task.get("task_id") or ""))
    claim_id = decision.get("claim_id")
    with _attention_store.transaction() as episodes:
        episode = dict(episodes.get(key) or {})
        claim = episode.get("repush_claim") or episode.get("human_escalation_claim") or {}
        if claim_id and claim.get("claim_id") == claim_id:
            if "repush_claim" in episode:
                episode["repush_claim"] = None
                episode["repush_state"] = "pending"
                episode["repush_inflight_at"] = None
            if "human_escalation_claim" in episode:
                episode["human_escalation_claim"] = None
                episode["human_escalation_state"] = "pending"
            episodes[key] = episode
    _record_blocked_event(
        task,
        "blocked_sla_claim_aborted",
        {"episode_id": decision.get("episode_id", ""), "claim_id": claim_id, "reason": reason},
    )
    decision["action"] = "suppressed_stale_task"
    decision["reason"] = reason
    decision["claimed"] = False
    return decision


def _record_blocked_event(task, event_type, payload):
    event_payload = dict(payload or {})
    event_payload.setdefault("task_id", task.get("task_id"))
    event_payload.setdefault("run_id", task.get("run_id"))
    try:
        _get_store().record_event(
            event_type,
            event_payload,
            workflow_id=task.get("workflow_id"),
            node_id=task.get("node") or task.get("stage"),
            task_id=task.get("task_id"),
            agent_id=task.get("agent"),
            run_id=task.get("run_id"),
            source="herdr-controller",
        )
        return True
    except (OSError, ValueError, RuntimeError, AttributeError) as exc:
        print(f"[BLOCKED SLA EVENT WARN] {event_type}: {exc}")
        return False


def _blocked_sla_record_action(task, decision, now):
    """Persist a coordinator notice or the second-SLA human escalation."""
    task_id = task.get("task_id", "")
    key = _blocked_sla_key(task_id)
    episode = attention_get(key) or {}
    action = decision.get("action")
    episode_id = decision.get("episode_id", "")
    if action == "notice":
        count = int(episode.get("coordinator_notices") or 0) + 1
        episode["coordinator_notices"] = count
        episode["last_action_at"] = now
        _attention_store.upsert(key, episode)
        _record_blocked_event(
            task,
            "blocked_coordinator_notice",
            {"episode_id": episode_id, "count": count,
             "active_seconds": episode.get("active_seconds")},
        )
    elif action == "escalate":
        count = int(episode.get("human_escalations") or 0) + 1
        episode["human_escalations"] = count
        episode["last_action_at"] = now
        _attention_store.upsert(key, episode)
        try:
            active = float(episode.get("active_seconds") or 0)
        except (TypeError, ValueError):
            active = 0.0
        delivered = _notify_blocked_human_upgrade(task, episode_id, active)
        _record_blocked_event(
            task,
            "blocked_human_escalated" if delivered else "blocked_human_escalation_failed",
            {"episode_id": episode_id, "count": count,
             "active_seconds": active, "delivered": delivered},
        )
    elif action == "suppressed_human":
        _record_blocked_event(
            task,
            "auto_action_suppressed_human_present",
            {"episode_id": episode_id},
        )


def _blocked_sla_record_repush(task, decision, success, detail, now):
    """Finalize only the claim that owns this prompt attempt."""
    task_id = task.get("task_id", "")
    key = _blocked_sla_key(task_id)
    claim_id = decision.get("claim_id")
    stale = False
    updated = {}
    with _attention_store.transaction() as episodes:
        episode = dict(episodes.get(key) or {})
        claim = episode.get("repush_claim") or {}
        if claim_id and claim.get("claim_id") != claim_id:
            stale = True
        else:
            episode["repushes"] = 1
            episode["delivery_attempts"] = int(episode.get("delivery_attempts") or 0) + 1
            episode["repush_state"] = "delivered" if success else "failed"
            episode["repush_inflight_at"] = None
            episode["repush_claim"] = None
            episode["last_action_at"] = now
            episode["last_repush_at"] = now
            if detail:
                key_name = "last_repush_receipt" if success else "last_repush_error"
                episode[key_name] = str(detail)[:500]
            episodes[key] = episode
            updated = dict(episode)

    if stale:
        _record_blocked_event(
            task,
            "blocked_repush_stale_result",
            {"episode_id": decision.get("episode_id", ""), "claim_id": claim_id},
        )
        return {}

    payload = {
        "episode_id": decision.get("episode_id", ""),
        "claim_id": decision.get("claim_id", ""),
        "repush_number": decision.get("repush_number", 1),
        "count": updated.get("repushes", 1),
        "delivered": bool(success),
        "detail": str(detail or "")[:500],
    }
    if success:
        event_type = (
            "blocked_auto_repush_recovered"
            if decision.get("action") == "repush_recover"
            else "blocked_auto_repush"
        )
    else:
        event_type = "prompt_delivery_failed"
    _record_blocked_event(task, event_type, payload)
    if not success:
        _record_blocked_event(task, "blocked_repush_failed", payload)
    return updated


def _blocked_sla_record_escalation(task, decision, delivered, now):
    """Finalize a claimed human escalation without duplicate notification."""
    key = _blocked_sla_key(task.get("task_id", ""))
    claim_id = decision.get("claim_id")
    stale = False
    with _attention_store.transaction() as episodes:
        episode = dict(episodes.get(key) or {})
        claim = episode.get("human_escalation_claim") or {}
        if claim_id and claim.get("claim_id") != claim_id:
            stale = True
        else:
            episode["human_escalation_state"] = "delivered" if delivered else "failed"
            episode["human_escalation_claim"] = None
            episode["last_action_at"] = now
            if delivered:
                episode["human_escalations"] = max(
                    1, int(episode.get("human_escalations") or 0)
                )
            episodes[key] = episode
    if stale:
        _record_blocked_event(
            task,
            "blocked_human_escalation_stale_result",
            {"episode_id": decision.get("episode_id", ""), "claim_id": claim_id},
        )
        return False
    _record_blocked_event(
        task,
        "blocked_human_escalated" if delivered else "blocked_human_escalation_failed",
        {
            "episode_id": decision.get("episode_id", ""),
            "claim_id": decision.get("claim_id", ""),
            "count": int(episode.get("human_escalations") or 1),
            "active_seconds": episode.get("active_seconds"),
            "delivered": bool(delivered),
        },
    )
    return bool(delivered)


def process_blocked_sla_task(task, now=None, send_prompt=None):
    """Run one Controller-owned blocked SLA step, including prompt delivery."""
    now = time.time() if now is None else float(now)
    _episode, decision = _claim_blocked_sla_action(task, now, poll_seconds=3.0)
    if decision.get("claimed"):
        current, reason = _blocked_task_claim_is_current(task, decision)
        if not current:
            return _release_stale_blocked_claim(task, decision, reason)
    if decision.get("claimed") and decision.get("action") in {"repush", "repush_recover"}:
        sender = send_prompt or _send_blocked_repush
        try:
            success, detail = sender(task, decision)
        except Exception as exc:  # delivery boundary is observable, not fatal
            success, detail = False, str(exc)[:500]
        current, reason = _blocked_task_claim_is_current(task, decision)
        if not current:
            return _release_stale_blocked_claim(task, decision, reason)
        _blocked_sla_record_repush(
            task, decision, bool(success), detail, now,
        )
        if success:
            # The existing coordinator card remains useful, but it is not the
            # SLA decision and does not change blocked status.
            try:
                enqueue_coordinator_event(task, "inner_loop_exhausted")
            except (OSError, RuntimeError, ValueError, AttributeError):
                pass
    elif decision.get("claimed") and decision.get("action") == "escalate":
        try:
            active = float(_episode.get("active_seconds") or 0)
        except (TypeError, ValueError):
            active = 0.0
        delivered = _notify_blocked_human_upgrade(
            task, decision.get("episode_id", ""), active
        )
        current, reason = _blocked_task_claim_is_current(task, decision)
        if not current:
            return _release_stale_blocked_claim(task, decision, reason)
        _blocked_sla_record_escalation(task, decision, delivered, now)
    elif decision.get("action") in {"notice", "suppressed_human"}:
        _blocked_sla_record_action(task, decision, now)
    return decision


def _get_store():
    if os.environ.get("HERDR_STATE_DB"):
        return get_state_store(Path(os.environ["HERDR_STATE_DB"]))
    t_file = globals().get("TASKS_FILE") or os.environ.get("TASKS_FILE")
    if t_file:
        p = Path(t_file).parent / "state.db"
        if p.parent.exists():
            return get_state_store(db_path=p)
    w_file = globals().get("WORKFLOWS_FILE") or os.environ.get("WORKFLOWS_FILE")
    if w_file:
        p = Path(w_file).parent / "state.db"
        if p.parent.exists():
            return get_state_store(db_path=p)
    return get_state_store()


def _workflow_entry(workflow_id):
    store = _get_store()
    wf = store.get_workflow(workflow_id)
    if wf:
        return wf
    return {}


def workflow_closed(workflow_id):
    """Registry entry reached a terminal (completed) status."""
    return _workflow_entry(workflow_id).get("status") == "completed"


def git_finalize_pending_tasks(workflow_id):
    """completed/committed 且 git 集成的任务仍在 commit/integrate 终化管线中。

    自动 close 若抢先推进到 cleaned,在跑的 `herdr-task commit` 子进程随后
    撞 'cleaned -> committed' 非法转移,交付分支落不进集成链路
    (2026-09-17 wf-nexusarchive-0917-01 wrapup 实测)。终化由其有界重试
    自会收敛(耗尽则升级总指挥);收敛后下一轮 sweep 再 close。
    """
    try:
        tasks = load_tasks()
    except Exception:
        return []

    return [
        t.get("task_id")
        for t in (tasks or [])
        if t.get("workflow_id") == workflow_id
        and t.get("status") in ("completed", "committed")
        and (t.get("integration_mode") or "none") == "git"
        and not t.get("finalize_escalated")
    ]


def git_escalated_tasks(workflow_id):
    """H-1: machine-escalated git tasks still holding undelivered commits.

    Escalation is observability + retry-stop, never a closability proof.
    Auto-close must defer on these; manual close requires explicit
    ``--accept-escalated``/``--force``/``--abandon`` or ``supersede``.
    """
    try:
        tasks = load_tasks()
    except (OSError, ValueError, RuntimeError, AttributeError,
            subprocess.SubprocessError):
        return []
    return [
        t.get("task_id")
        for t in (tasks or [])
        if t.get("workflow_id") == workflow_id
        and t.get("status") in ("completed", "committed")
        and (t.get("integration_mode") or "none") == "git"
        and t.get("finalize_escalated")
    ]


def maybe_close_completed_workflow(workflow_id):
    """Workflow 全部节点完成后,自动执行物理收尾(关 pane/删 clone/归档)。

    close-workflow 自带幂等与终态闸门;这里只负责防重入派发。
    """
    if not workflow_id or workflow_id in _workflow_close_inflight:
        return

    entry = _workflow_entry(workflow_id)

    # 夹具/临时 workflow 严禁物理收尾(会去操作不存在的 clone/pane)。
    if liveness.workflow_is_foreign(entry):
        return

    if entry.get("status") == "completed":
        return
    # Fix-loop reopen 闩:重开后的 workflow 在首个任务进入 ACTIVE
    # 之前,旧任务仍全为完成系,必须挡住 sweep 的自消除 close。
    if entry.get("suppress_auto_close"):
        return

    # git 终化在途:必须等 commit/integrate 收敛,close 不得抢先物理收尾。
    pending_git = git_finalize_pending_tasks(workflow_id)
    if pending_git:
        if workflow_id not in _close_deferred_logged:
            _close_deferred_logged.add(workflow_id)
            print(
                f"[CLOSE DEFERRED] workflow={workflow_id} "
                f"waiting git finalize: {','.join(pending_git)}"
            )
        return

    # H-1: auto-close never carries human confirmation, so escalated git
    # tasks (retry-exhausted / refused / main-dirty) must also defer.
    # Silent delivered with a never-integrated committed task is the exact
    # failure this blocks.
    escalated_git = git_escalated_tasks(workflow_id)
    if escalated_git:
        if workflow_id not in _close_deferred_logged:
            _close_deferred_logged.add(workflow_id)
            print(
                f"[CLOSE DEFERRED] workflow={workflow_id} "
                f"waiting human confirm for escalated: "
                f"{','.join(escalated_git)}"
            )
        return

    _workflow_close_inflight.add(workflow_id)

    def _run():
        try:
            result = subprocess.run(
                [TASK_MANAGER, "close-workflow", workflow_id],
                text=True,
                capture_output=True,
                timeout=900,
            )
            if result.stdout.strip():
                print(result.stdout.strip())
            if result.returncode != 0:
                print(
                    f"[WORKFLOW CLOSE ERROR] "
                    f"workflow={workflow_id}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
        finally:
            _workflow_close_inflight.discard(workflow_id)

    threading.Thread(
        target=_run,
        daemon=True,
        name=f"wf-close-{workflow_id}",
    ).start()


# ============================================================
# Gate verdicts & fix-loop
# ============================================================

# 常见门禁阶段的内置默认;显式配置(workflow 节点 gate / stage-policies.json)
# 优先于这里。verdict 缺失(lenient)时门禁不生效,存量 workflow 行为不变。
GATE_DEFAULTS = {
    "test": {"retry_node": "implementation"},
    "review": {"retry_node": "implementation"},
    "wrapup": {"retry_node": "implementation"},
}

FIX_LOOP_MAX = int(os.environ.get("HERDR_FIX_LOOP_MAX", "3"))

# 可作废状态集合,必须与 bin/herdr-task TRANSITIONS 中
# 允许 → superseded 的状态保持一致(pending/committed/integrated 除外)。
FIX_LOOP_SUPERSEDEABLE = {
    "dispatched",
    "working",
    "blocked",
    "agent_done",
    "rework",
    "cleaned",
    "failed",
}


def resolve_gate_config(node, node_id):
    gate = (node or {}).get("gate") or get_stage_policy(node_id).get("gate")

    if gate is None:
        gate = GATE_DEFAULTS.get(node_id)

    if not gate:
        return None

    return {
        "retry_node": gate.get("retry_node", "implementation"),
        "max_loops": int(gate.get("max_loops", FIX_LOOP_MAX)),
    }


def gate_verdict(workflow_id, node_id):
    """Fail-safe 门禁结论:任一未作废任务的 blocked 结论即 blocked。"""
    verdict = None

    for task in load_tasks():
        if task.get("workflow_id") != workflow_id:
            continue
        if node_id not in (task.get("node"), task.get("stage")):
            continue
        if task.get("status") == "superseded":
            continue

        task_verdict = task.get("stage_verdict")

        if task_verdict == "blocked":
            return "blocked"
        if task_verdict == "pass":
            verdict = "pass"

    return verdict


def latest_branch_for_node(workflow_id, node_id):
    best = None

    for task in load_tasks():
        if task.get("workflow_id") != workflow_id:
            continue
        if node_id not in (task.get("node"), task.get("stage")):
            continue
        if not task.get("branch"):
            continue
        if best is None or task.get("updated_at", 0) > best.get("updated_at", 0):
            best = task

    return best.get("branch") if best else None


def shared_docs_block(workflow_id, node_id, related_nodes=(), project_ctx=None):
    """跨节点共享文档区注入块:只读上下文 + 追加写入指引(best-effort)。"""
    try:
        from herdr import workflow_docs as wd
        notes = wd.load_notes(workflow_id)
    except Exception as exc:
        print(f"[SHARED DOCS WARN] load {workflow_id}: {exc}")
        return ""

    current_base_sha = None
    ctx = project_ctx or {}
    project_root = ctx.get("project_root") or ""
    base_branch = ctx.get("base_branch") or ""
    if project_root and base_branch:
        try:
            result = subprocess.run(
                ["git", "-C", project_root, "rev-parse", "--short", base_branch],
                text=True,
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                current_base_sha = result.stdout.strip() or None
        except Exception:
            current_base_sha = None

    try:
        return wd.render_context_block(
            workflow_id,
            notes,
            node_id=node_id,
            related_nodes=related_nodes,
            current_base_sha=current_base_sha,
        )
    except Exception as exc:
        print(f"[SHARED DOCS WARN] render {workflow_id}: {exc}")
        return ""


def _record_invalidation_note(workflow_id, gate_node_id, retry_node, nodes, invalidated):
    """fix-loop 作废时写下 controller 机器证据,供 stale 计算与下游阅读。"""
    try:
        from herdr import workflow_docs as wd
        wd.append_note(
            workflow_id,
            kind="invalidation",
            title=(
                f"fix-loop 作废: gate={gate_node_id} "
                f"retry={retry_node or gate_node_id}"
            ),
            body="被作废任务: " + ", ".join(sorted(invalidated)),
            node=gate_node_id,
            source=wd.SOURCE_CONTROLLER,
            invalidates=sorted(node for node in nodes if node),
        )
    except Exception as exc:
        print(f"[FIX LOOP NOTE WARN] {exc}")


def _collect_downstream_nodes(nodes_by_id, root_id):
    """root 节点自身 + 传递闭包的全部下游节点。"""
    dependents = {}

    for node in nodes_by_id.values():
        for dep in node.get("depends_on", []):
            dependents.setdefault(dep, set()).add(node["id"])

    seen = {root_id}
    frontier = [root_id]

    while frontier:
        current = frontier.pop()
        for nxt in dependents.get(current, ()):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)

    return seen


def _fix_loop_keepable(task):
    """verdict=pass 的任务可否保留:需已落定或无需 Git 集成。"""
    status = task.get("status")
    if status in ("cleaned", "cleanup_ready", "integrated", "committed"):
        return True
    if status == "completed" and (task.get("integration_mode") or "none") != "git":
        return True
    return False


class SelectiveInvalidationResult(list):
    """选择性作废的结构化返回(仍是 list,兼容 legacy 调用方)。

    P1-1: selective 作废不能只返回 invalidated=[...]。部分失败时
    调用方必须能判断 all_targets_applied / pending / failed,
    否则会带着 [B,C] 的 latch 去等一个永远不会产生的 C-r2。
    legacy 路径( selective_target_task_ids is None )仍返回普通 list。
    """

    def __new__(cls, invalidated=None, *, applied=None, pending=None,
                failed=None):
        obj = super().__new__(cls, list(invalidated or []))
        obj.applied = list(applied or [])
        obj.pending = list(pending or [])
        obj.failed = dict(failed or {})
        obj.all_targets_applied = not obj.pending
        return obj


REWORKABLE_STATUSES = {"blocked", "agent_done", "working", "rework", "paused", "interrupted"}


def _rework_retry_task(task, gate_node_id, request_id=None, failure_facts=None):
    """Use the CLI ownership/CAS/transport contract; failure retains the Task."""
    gate_tasks = [t for t in (failure_facts if failure_facts is not None else load_tasks())
                  if t.get("workflow_id") == task.get("workflow_id")
                  and t.get("status") != "superseded" and not t.get("superseded_by")
                  and t.get("candidate_sha") == task.get("candidate_sha")
                  and (t.get("node") or t.get("stage")) in gate_node_id.split("+")
                  and t.get("stage_verdict") == "blocked"]
    if request_id is None:
        identity = sorted((t.get("task_id", ""), t.get("version", 0),
                           t.get("stage_verdict_note") or t.get("note") or "") for t in gate_tasks)
        token = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:20]
        request_id = f"fix-loop:{gate_node_id}:{task['task_id']}:{token}"
        if task.get("rework_delivery") == "pending" and str(task.get("rework_request_id") or "").startswith(f"fix-loop:{gate_node_id}:"):
            request_id = task["rework_request_id"]
    from herdr.supervisor.state import redact_text
    details = "\n".join(redact_text(f"{t.get('task_id')}: {t.get('stage_verdict_note') or t.get('note') or '读取门禁输出'}")[:2000]
                        for t in gate_tasks[:20])
    result = subprocess.run(
        [TASK_MANAGER, "rework", task["task_id"], "--request-id", request_id,
         "--reason", f"fix-loop: gate {gate_node_id} blocked",
         "--prompt", f"就地修复 gate {gate_node_id} 的阻断项，保留 Task 和 Pane，重新自测。\n{details}"],
        text=True, capture_output=True, timeout=30,
    )
    return result.returncode == 0, result.stderr.strip() or result.stdout.strip()


def _preserve_same_gate_task(task, retry_node, gate_ids):
    return (retry_node in gate_ids
            and (task.get("node") or task.get("stage")) == retry_node
            and task.get("status") in REWORKABLE_STATUSES
            and task.get("stage_verdict") != "blocked"
            and task.get("rework_delivery") != "pending")


def _rework_legacy_retry(workflow_id, retry_node, gate_node_id):
    if not retry_node:
        return []
    reused = []
    for task in load_tasks():
        if _preserve_same_gate_task(task, retry_node, gate_node_id.split("+")):
            continue
        if (task.get("workflow_id") == workflow_id
                and (task.get("node") or task.get("stage")) == retry_node
                and task.get("status") in REWORKABLE_STATUSES):
            try:
                ok, error = _rework_retry_task(task, gate_node_id)
            except (OSError, subprocess.TimeoutExpired) as exc:
                ok, error = False, str(exc)
            if not ok:
                print(f"[FIX LOOP REWORK DEFERRED] {task['task_id']}: {error}")
                return None
            reused.append(task["task_id"])
    return reused


def _invalidate_single_task(task_id, gate_node_id, reuse=False):
    """finalize(如需)+supersede 单个任务,返回 (ok, error)。"""
    task = get_task(task_id) or {}
    status = task.get("status")
    if reuse and status in REWORKABLE_STATUSES:
        try:
            return _rework_retry_task(task, gate_node_id)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
    if status in ("completed", "cleanup_ready"):
        result = subprocess.run(
            [TASK_MANAGER, "finalize", task_id],
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            print(f"[FIX LOOP INVALIDATE ERROR] finalize {task_id}: {err}")
            return False, f"finalize:{err}"
        status = (get_task(task_id) or {}).get("status")
    if status not in FIX_LOOP_SUPERSEDEABLE:
        print(
            f"[FIX LOOP INVALIDATE SKIP] task={task_id} "
            f"status={status} cannot be superseded"
        )
        return False, f"status_not_supersedeable:{status}"
    result = subprocess.run(
        [
            TASK_MANAGER, "supersede", task_id,
            "--allow-pending",
            "--reason", f"fix-loop: gate {gate_node_id} blocked",
        ],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip()
        print(f"[FIX LOOP INVALIDATE ERROR] supersede {task_id}: {err}")
        return False, f"supersede:{err}"
    return True, ""


def invalidate_for_fix_loop(workflow_id, gate_node_id, workflow_cfg, retry_node=None, selective_target_task_ids=None):
    """作废 gate 节点及其全部下游、以及 retry_node 到 gate 间全部中间节点的非 superseded 任务(fix-loop 回流前提)。

    completed/cleanup_ready 中间态先 finalize 规范化到 cleaned——
    completed→superseded 会被状态机拒绝;pending 不可作废,跳过。

    selective_target_task_ids(PR #110):非 None 时启用选择性返工——
    retry_node 节点进入扫描范围,但其中只有被点名的 Task 会被作废,
    其余实现任务零写入(状态/版本/分支/元数据全部不动);
    retry_node 之外的节点行为与 legacy 完全一致。
    None = legacy 精确原行为(retry_node 自身不进扫描范围)。

    P1-1 两阶段提交: selective 模式先作废全部目标谱系,任一目标
    未落定即整体返回(门禁/下游一律不动,无 latch、无通知),
    下轮 sweep 基于已持久化 Fact 重试剩余目标。返回
    SelectiveInvalidationResult(仍是 list),携带
    applied/pending/failed/all_targets_applied。
    """
    nodes_by_id = {
        n["id"]: n for n in workflow_cfg.get("nodes", [])
    }

    if not nodes_by_id and workflow_cfg.get("stages"):
        # normalize_workflow 总会合成 nodes;此处兜底纯 stages 配置:
        # 用 next 链重建线性依赖,保证下游作废闭包仍然成立。
        prev = None
        for stage in workflow_cfg.get("stages", []):
            stage_id = stage.get("key") or stage.get("id")
            nodes_by_id[stage_id] = {
                "id": stage_id,
                "depends_on": [prev] if prev else [],
            }
            prev = stage_id

    node_ids = set(_collect_downstream_nodes(nodes_by_id, gate_node_id))
    if retry_node and retry_node in nodes_by_id:
        downstream_retry = _collect_downstream_nodes(nodes_by_id, retry_node) - {retry_node}
        node_ids = node_ids | downstream_retry
        if selective_target_task_ids is not None:
            # 选择性返工:retry_node 自身进入扫描范围(由下方逐任务过滤点名目标)。
            node_ids.add(retry_node)

    selective_targets = None
    if selective_target_task_ids is not None:
        selective_targets = {
            str(t).strip() for t in selective_target_task_ids if str(t).strip()
        }

    # P1-1: selective 目标两阶段提交。阶段一先处理 retry_node 点名目标;
    # 任一目标 pending 即返回,不碰门禁/下游(门禁仍 blocked,下轮可重试)。
    reused = []
    if selective_targets is None:
        reused = _rework_legacy_retry(workflow_id, retry_node, gate_node_id)
        if reused is None:
            return []

    if selective_targets is not None and retry_node:
        ordered_targets = sorted(selective_targets)
        applied: list = []
        failed: dict = {}
        snapshot = {
            str(t.get("task_id") or ""): t
            for t in load_tasks()
            if isinstance(t, dict)
            and t.get("workflow_id") == workflow_id
        }
        for target_id in ordered_targets:
            record = snapshot.get(target_id)
            if record is None:
                failed[target_id] = "target_task_unknown"
                continue
            status = record.get("status")
            if status == "superseded" or record.get("superseded_by"):
                # 崩溃前已作废:视为已落定,不重复 supersede。
                applied.append(target_id)
                continue
            ok, err = _invalidate_single_task(target_id, gate_node_id, reuse=True)
            if ok:
                applied.append(target_id)
            else:
                failed[target_id] = err
        if selective_replan_core is not None:
            outcome = selective_replan_core.selective_invalidation_outcome(
                ordered_targets, applied, failed
            )
        else:
            outcome = {
                "applied": applied,
                "pending": [t for t in ordered_targets if t not in applied],
                "failed": failed,
                "all_targets_applied": len(failed) == 0
                and len(applied) == len(ordered_targets),
            }
        if not outcome["all_targets_applied"]:
            print(
                f"[SELECTIVE REPLAN PARTIAL] workflow={workflow_id} "
                f"gate={gate_node_id} retry={retry_node} "
                f"applied={','.join(outcome['applied'])} "
                f"pending={','.join(outcome['pending'])} "
                f"failed={','.join(sorted(outcome['failed']))}"
            )
            return SelectiveInvalidationResult(
                list(outcome["applied"]),
                applied=list(outcome["applied"]),
                pending=list(outcome["pending"]),
                failed=dict(outcome["failed"]),
            )
        # 阶段一全落定,进入阶段二:门禁/下游/中间节点(仍跳过 preserved)。
        # 目标已返工或归档，阶段二显式跳过，不能再次退役。

    supersedeable = FIX_LOOP_SUPERSEDEABLE
    invalidated = list(reused)
    invalidated_nodes = set()

    for task in load_tasks():
        if task.get("workflow_id") != workflow_id:
            continue
        if (
            task.get("node") not in node_ids
            and task.get("stage") not in node_ids
        ):
            continue

        status = task.get("status")
        task_id = task["task_id"]
        task_node = task.get("node") or task.get("stage")

        if selective_targets is None and _preserve_same_gate_task(task, retry_node, [gate_node_id]):
            continue
        if task_id in reused or status == "superseded" or (selective_targets is not None and task_id in selective_targets):
            continue

        if (
            selective_targets is not None
            and task_node == retry_node
            and task_id not in selective_targets
        ):
            # PR #110 preserved:未被点名的实现任务保持完全不动。
            print(
                f"[SELECTIVE REPLAN PRESERVE] task={task_id} "
                f"node={task_node} untouched"
            )
            continue

        # 返工只重跑受影响子集:仅在门禁自身重跑(not retry_node 或 retry_node == gate_node_id)时,
        # 门禁节点内 verdict=pass 且已落定的任务保留;
        # 若跨阶段回流(如 review 回流 implementation),上游已变更,门禁与中间节点任务不可复用。
        if (
            task_node == gate_node_id
            and (not retry_node or retry_node == gate_node_id)
            and task.get("stage_verdict") == "pass"
            and _fix_loop_keepable(task)
        ):
            print(
                f"[FIX LOOP SUBSET KEEP] task={task_id} "
                f"verdict=pass preserved"
            )
            continue

        if status in ("completed", "cleanup_ready"):
            result = subprocess.run(
                [TASK_MANAGER, "finalize", task_id],
                text=True,
                capture_output=True,
            )
            if result.returncode != 0:
                print(
                    f"[FIX LOOP INVALIDATE ERROR] finalize {task_id}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
                continue
            status = (get_task(task_id) or {}).get("status")

        if status not in supersedeable:
            print(
                f"[FIX LOOP INVALIDATE SKIP] task={task_id} "
                f"status={status} cannot be superseded"
            )
            continue

        result = subprocess.run(
            [
                TASK_MANAGER, "supersede", task_id,
                "--allow-pending",
                "--reason", f"fix-loop: gate {gate_node_id} blocked",
            ],
            text=True,
            capture_output=True,
        )

        if result.returncode == 0:
            invalidated.append(task_id)
            if task_node:
                invalidated_nodes.add(task_node)
        else:
            print(
                f"[FIX LOOP INVALIDATE ERROR] supersede {task_id}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

    if invalidated:
        void_nodes = invalidated_nodes | {gate_node_id}
        if retry_node:
            void_nodes.add(retry_node)
        _record_invalidation_note(
            workflow_id, gate_node_id, retry_node, void_nodes, invalidated
        )

    if selective_targets is not None:
        # P1-1:阶段一目标已全落定,最终返回需包含目标+门禁/下游。
        # 阶段二扫描跳过已 superseded 的目标,故在此补回。
        phase1_applied = []
        try:
            phase1_applied = list(ordered_targets)
        except NameError:
            phase1_applied = sorted(selective_targets)
        combined = list(phase1_applied) + [
            tid for tid in invalidated if tid not in phase1_applied
        ]
        return SelectiveInvalidationResult(
            combined,
            applied=list(phase1_applied),
            pending=[],
            failed={},
        )

    return invalidated


def _bump_fix_loop_count(workflow_id, retry_node):
    with lock:
        state = load_stage_state()
        key = f"{workflow_id}|fixloop|{retry_node}"
        count = int(state.get(key, 0)) + 1
        state[key] = count
        save_stage_state(state)
    return count


def _fix_loop_count(workflow_id, retry_node):
    with lock:
        state = load_stage_state()
        try:
            return int(state.get(f"{workflow_id}|fixloop|{retry_node}", 0))
        except (TypeError, ValueError):
            return 0


def _fix_loop_exhaustion_episode(workflow_id, gate_node_id):
    return attention_get(f"{workflow_id}:fix_loop_exhausted:{gate_node_id}")


def _exhaustion_fingerprint(episode):
    if not isinstance(episode, dict):
        return None
    try:
        detail = json.loads(episode.get("detail") or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(detail, dict):
        return None
    return detail.get("fingerprint")


def _record_fix_loop_exhaustion(
    workflow_id, gate_node_id, retry_node, reason, summary, fingerprint
):
    import json as _json

    record = dict(summary)
    record["fingerprint"] = fingerprint
    attention_note(
        f"{workflow_id}:fix_loop_exhausted:{gate_node_id}",
        {"task_id": f"fix_loop:{gate_node_id}:{retry_node}",
         "workflow_id": workflow_id},
        "fix_loop",
        reason=reason,
        attempts=_fix_loop_count(workflow_id, retry_node),
        detail=_json.dumps(record, ensure_ascii=False)[:2000],
    )


# ============================================================
# 基础设施失败自动补派 (auto-recover)
# ============================================================

# 仅这些失败原因属于"基础设施类"(投递熔断 / 进程崩溃),可自动作废补派;
# 质量类失败结论(如总指挥判定 failed)绝不自动翻案。
AUTO_RECOVER_REASONS = {"dispatch_delivery_fuse", "agent_process_crash"}
AUTO_RECOVER_MAX_ATTEMPTS = int(os.environ.get("HERDR_AUTO_RECOVER_MAX", "2"))

# 终化重试上限：commit 门禁瞬时失败(flaky)/集成冲突时按注意力退避自动重试；
# 达到上限后停止自动重试并升级人工(避免 gate 永久失败造成的无限重试风暴)。
FINALIZE_RETRY_MAX = int(os.environ.get("HERDR_FINALIZE_RETRY_MAX", "5"))


def should_retry_finalize(status, episode, now, max_attempts=None):
    """(should_retry, reason, exhausted) — committed/completed 的终化重试决策。

    `completed` 且 integration_mode=git 的任务若 commit 门禁瞬时失败，此前
    没有任何重试路径，只能人工或总指挥手工重试（实测一次 flaky gate 浪费
    55min 并烧掉总指挥大回合）。本函数给出统一的退避重试 + 上限判定。
    """
    if status not in ("committed", "completed"):
        return False, "", False

    limit = FINALIZE_RETRY_MAX if max_attempts is None else int(max_attempts)
    attempts = int((episode or {}).get("attempts") or 0)
    if attempts >= limit:
        return False, "", True

    if float((episode or {}).get("next_retry_at") or 0) > now:
        return False, "", False
    reason = "integration_retry" if status == "committed" else "commit_retry"
    return True, reason, False


def recover_infra_failed_tasks(workflow_id, tasks=None):
    """基础设施失败任务自动作废,交给 sweep 重新派发替代任务。

    此前 failed 任务只能等人工 relaunch(--supersedes),实测造成 28 分钟级
    空等;这里复用既有 superseded->replace 派发管线:作废后清掉该节点的
    stage-advance 闩,下一轮 sweep 的 direct dispatch 会自动补派 -rN。
    谱系级次数上限由纯函数 select_infra_failures_for_recovery 保证,
    达到上限后不再自动处置(只留日志与告警)。
    """
    if not workflow_id:
        return False

    if workflow_closed(workflow_id):
        return False

    wf_st = _workflow_entry(workflow_id).get("status")
    if wf_st in ("completed", "paused"):
        return False

    if tasks is None:
        tasks = load_tasks()

    candidates = liveness.select_infra_failures_for_recovery(
        [t for t in tasks if t.get("workflow_id") == workflow_id],
        AUTO_RECOVER_REASONS,
        max_attempts=AUTO_RECOVER_MAX_ATTEMPTS,
    )

    recovered = False
    for task in candidates:
        task_id = task.get("task_id")
        node_id = task.get("node") or task.get("stage")

        result = subprocess.run(
            [
                TASK_MANAGER, "supersede", task_id,
                "--allow-pending",
                "--reason", "auto-recover: infrastructure failure",
            ],
            text=True,
            capture_output=True,
        )

        if result.returncode != 0:
            print(
                f"[AUTO RECOVER ERROR] supersede {task_id}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
            continue

        if node_id:
            clear_stage_advance(workflow_id, node_id)

        recovered = True
        print(
            f"[AUTO RECOVER] workflow={workflow_id} "
            f"node={node_id} task={task_id} "
            "superseded -> pending redispatch by sweep"
        )

    return recovered


def process_crash_observations():
    """Consume durable Sentinel crash observations through the task CAS."""
    store = _get_store()
    try:
        events = store.list_events(event_type="agent_process_crash_observed")
    except (AttributeError, OSError, RuntimeError, ValueError):
        return 0
    processed = 0
    for event in events:
        task_id = event.get("task_id")
        if not task_id:
            continue
        task = store.get_task(task_id)
        if not task or task.get("status") not in {
            "dispatched", "working", "rework", "blocked"
        }:
            continue
        payload = event.get("payload") or {}
        try:
            expected_status = payload.get("observed_status") or task.get("status")
            expected_version = int(
                payload.get("observed_version", task.get("version"))
            )
        except (TypeError, ValueError):
            continue
        try:
            result = store.compare_and_set_task_transition(
                task_id,
                to_status="failed",
                reason="agent_process_crash",
                source="herdr-controller",
                metadata={
                    "sentinel_reason": "agent_process_crash",
                    "crash_event_id": event.get("id"),
                },
                expected_status=expected_status,
                expected_version=expected_version,
            )
        except (AttributeError, OSError, RuntimeError, ValueError):
            continue
        if result.get("accepted", False):
            processed += 1
    return processed


def recover_router_isolation_tasks(workflow_id, tasks=None):
    """Retry a router-rejected task only after an eligible pool exists."""
    from herdr.agent_router import choose_agent

    if not workflow_id or workflow_closed(workflow_id):
        return False
    if tasks is None:
        tasks = load_tasks()
    recovered = False
    for task in tasks or []:
        if (
            task.get("workflow_id") != workflow_id
            or task.get("status") != "failed"
            or task.get("failure_reason") != "router_isolation_rejected"
            or task.get("superseded_by")
        ):
            continue
        try:
            choose_agent(
                workflow_id,
                task.get("node") or task.get("stage"),
                task.get("task_type") or "test",
                requested=task.get("agent") or "auto",
            )
        except (OSError, RuntimeError, ValueError, AttributeError):
            continue
        result = subprocess.run(
            [
                TASK_MANAGER, "supersede", task.get("task_id"),
                "--allow-pending",
                "--reason", "router pool recovered; replacement eligible",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            recovered = True
            print(
                f"[ROUTER RECOVERY] workflow={workflow_id} "
                f"task={task.get('task_id')} eligible pool restored"
            )
        else:
            print(
                f"[ROUTER RECOVERY ERROR] task={task.get('task_id')}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
    return recovered


def blocked_verdict_dep(workflow_id, node):
    """依赖中是否存在任务级 blocked 验收结论(不问 gate_cfg)。

    plan/requirements 等节点没有 gate 配置,sweep 的 fix-loop 看不见它们,
    但自动推进同样不得越过 blocked 结论。返回首个被阻断的依赖 id。
    """
    for dep in ((node or {}).get("depends_on") or []):
        if gate_verdict(workflow_id, dep) == "blocked":
            return dep
    return None


def blocked_gate_dependency(workflow_id, ready_node, workflow_cfg):
    """ready_node 的依赖中是否存在 verdict=blocked 的门禁节点。"""
    nodes_by_id = {
        n["id"]: n for n in workflow_cfg.get("nodes", [])
    }

    for dep in ready_node.get("depends_on", []):
        gate_cfg = resolve_gate_config(nodes_by_id.get(dep), dep)

        if not gate_cfg:
            continue

        if gate_verdict(workflow_id, dep) == "blocked":
            return dep, gate_cfg

    return None


def _scheduler_current_frozen_candidate_sha(workflow_id):
    """最新一次冻结候选 SHA(从 freeze 台账读,不重算);无 → ""。"""
    if scheduler_facts_store is None:
        return ""
    try:
        return scheduler_facts_store.latest_frozen_candidate_sha(workflow_id)
    except (ValueError, OSError):
        return ''


def _scheduler_current_frozen_branch(workflow_id):
    """最新一次冻结候选的可证明分支(从 freeze 台账读);无 → ""。"""
    if scheduler_facts_store is None:
        return ""
    try:
        events = scheduler_facts_store.list_candidate_frozen_events(workflow_id)
    except Exception:
        return ""
    if not events:
        return ""
    return str((events[-1].get("payload") or {}).get("delivery_branch") or "")


def _resolve_selective_replan(workflow_id, gate_node_id, retry_node,
                              workflow_cfg, blocked_gate_tasks, tasks):
    """PR #110:把 blocked 门禁的显式 affected_task_ids 解析为选择性返工目标。

    返回 (target_task_ids or None, plan or None):
    - target_task_ids is None → legacy fix-loop 精确原行为(策略未开启、
      模块缺失、或任一环节无法证明时均如此);
    - 非 None → 选择性事实已持久化,只允许作废这些目标。

    Fail-Closed 顺序:先构建 plan,再持久化 immutable fact,
    最后才允许调用方作废——「无持久化 selective 事实,无选择性作废」。
    崩溃重放时 record 返回 exists,以已存储 payload 为权威(不重算)。
    """
    if selective_replan_core is None or scheduler_facts_store is None:
        return None, None
    policy = selective_replan_core.policy_from_workflow(workflow_cfg)
    if policy is None:
        # legacy workflow(未声明 selective_replan 策略):完全无感知。
        return None, None
    if policy["retry_node"] != str(retry_node or ""):
        # 目标按策略声明的 retry_node 校验,作废却按门禁解析出的 retry_node 执行:
        # 两者不一致时「保留」过滤器永不命中,会把未被点名的任务一并作废,
        # 同时留下一条自称 selective 的事实。证明不了同一节点即整体回退。
        print(
            f"[SELECTIVE REPLAN FALLBACK] workflow={workflow_id} "
            f"gate={gate_node_id} reason=retry_node_policy_mismatch "
            f"policy={policy['retry_node']} gate_config={retry_node}"
        )
        return None, None

    # P1-1 Resolve-once-persist-once-read-many:已有当前 gate episode 的
    # Fact 时直接作为 authority 逐个 reconcile,不再根据已变化的 Task
    # 状态重新决定 targets(否则 B 已 supersede 会导致整体 fallback,
    # C 永远得不到 supersede)。
    if len(blocked_gate_tasks) == 1 and isinstance(
        blocked_gate_tasks[0], dict
    ):
        try:
            _gate_task = blocked_gate_tasks[0]
            _frozen_sha = _scheduler_current_frozen_candidate_sha(workflow_id)
            _policy_fp = selective_replan_core.policy_identity(policy)
            try:
                _gate_version = int(_gate_task.get("version"))
            except (TypeError, ValueError):
                _gate_version = None
            if _gate_version is not None and _frozen_sha and _policy_fp:
                try:
                    if hasattr(
                        scheduler_facts_store,
                        "list_selective_replan_for_node",
                    ):
                        _prior_facts = (
                            scheduler_facts_store.list_selective_replan_for_node(
                                workflow_id, retry_node
                            )
                        )
                    else:
                        _prior_facts = (
                            scheduler_facts_store.list_selective_replan_decisions(
                                workflow_id,
                                limit=scheduler_facts_store.REPLAN_LOOKUP_SCAN_LIMIT,
                            )
                        )
                except Exception:
                    _prior_facts = []
                _reused = selective_replan_core.find_reusable_selective_fact(
                    _prior_facts,
                    retry_node=str(retry_node or ""),
                    gate_task_id=str(_gate_task.get("task_id") or ""),
                    gate_task_version=_gate_version,
                    frozen_candidate_sha=_frozen_sha,
                    policy_identity=_policy_fp,
                )
                if isinstance(_reused, dict) and _reused.get("mode") == (
                    selective_replan_core.MODE_SELECTIVE
                ):
                    print(
                        f"[SELECTIVE REPLAN REUSE] workflow={workflow_id} "
                        f"gate={gate_node_id} retry={retry_node} "
                        f"targets={','.join(_reused.get('target_task_ids') or [])} "
                        f"replan_id={_reused.get('replan_id')}"
                    )
                    return list(_reused.get("target_task_ids") or []), _reused
        except Exception as exc:
            print(
                f"[SELECTIVE REPLAN WARN] workflow={workflow_id} "
                f"gate={gate_node_id} reuse lookup skipped: {exc}"
            )

    requested_raw = None
    if len(blocked_gate_tasks) == 1:
        requested_raw = blocked_gate_tasks[0].get(
            "stage_verdict_affected_task_ids"
        )
    plan = selective_replan_core.build_selective_replan_plan(
        workflow_id=workflow_id,
        gate_node=gate_node_id,
        gate_tasks=blocked_gate_tasks,
        policy=policy,
        requested_raw=requested_raw,
        tasks=tasks,
        frozen_candidate_sha=_scheduler_current_frozen_candidate_sha(
            workflow_id
        ),
        supersedeable_statuses=FIX_LOOP_SUPERSEDEABLE,
    )
    # P1-3:把决策时刻冻结候选的可证明分支绑进事实,供 replacement
    # 派发时 fail-closed 校验基线(不进 identity,不影响 exists/mismatch)。
    try:
        plan["source_candidate_branch"] = _scheduler_current_frozen_branch(
            workflow_id
        )
    except Exception:
        pass

    authority = plan
    if plan.get("replan_id"):
        try:
            record = scheduler_facts_store.record_selective_replan_decision(
                workflow_id, plan
            )
        except Exception as exc:
            record = {"status": "error", "error": str(exc)}
        record_status = str((record or {}).get("status") or "error")
        if record_status == "exists":
            # 幂等重放:已落库的事实是权威,绝不用本次重算结果顶替。
            stored = scheduler_facts_store.find_selective_replan_decision(
                workflow_id, plan["replan_id"]
            )
            if not isinstance(stored, dict):
                print(
                    f"[SELECTIVE REPLAN FALLBACK] workflow={workflow_id} "
                    f"gate={gate_node_id} fact_status=exists "
                    "reason=stored_fact_unreadable"
                )
                return None, plan
            authority = stored
        elif record_status != "created":
            # 持久化被拒/冲突/异常:无事实 → 绝不选择性作废。
            # record_event_if_absent 用 reason 报告拒绝(如
            # identity_content_mismatch),只有异常分支才带 error。
            reject_reason = (
                (record or {}).get("error")
                or (record or {}).get("reason")
                or plan.get("reason")
            )
            print(
                f"[SELECTIVE REPLAN FALLBACK] workflow={workflow_id} "
                f"gate={gate_node_id} fact_status={record_status} "
                f"reason={reject_reason}"
            )
            return None, plan
    else:
        # 无法构成 episode 身份(门禁任务/版本不可证明):事实不可持久化。
        print(
            f"[SELECTIVE REPLAN FALLBACK] workflow={workflow_id} "
            f"gate={gate_node_id} reason={plan.get('reason')} "
            "fact=unpersistable"
        )
        return None, plan

    if authority.get("mode") != selective_replan_core.MODE_SELECTIVE:
        print(
            f"[SELECTIVE REPLAN FALLBACK] workflow={workflow_id} "
            f"gate={gate_node_id} reason={authority.get('reason')}"
        )
        return None, authority

    print(
        f"[SELECTIVE REPLAN] workflow={workflow_id} "
        f"gate={gate_node_id} retry={retry_node} "
        f"targets={','.join(authority.get('target_task_ids') or [])} "
        f"preserved={','.join(authority.get('preserved_task_ids') or [])} "
        f"replan_id={authority.get('replan_id')}"
    )
    return list(authority.get("target_task_ids") or []), authority


def _latest_selective_fact(workflow_id, node_id):
    """该 retry_node 最新的 selective 事实;无/异常/模块缺失 → None。

    legacy_fallback 事实由核心层跳过(它们不带 targets,不隐含待补派)。
    """
    if selective_replan_core is None or scheduler_facts_store is None:
        return None
    try:
        fact = scheduler_facts_store.latest_selective_replan_for_node(
            workflow_id, node_id
        )
    except Exception as exc:
        print(
            f"[SELECTIVE REPLAN WARN] workflow={workflow_id} "
            f"node={node_id}: {exc}"
        )
        return None
    return fact if isinstance(fact, dict) else None


def _all_selective_facts(workflow_id, node_id):
    """该 retry_node 全部 selective 事实(chronological);异常 → []。

    P1-2: test ∥ review 同轮 blocked 会留下两条单门禁事实。
    只读最新一条会丢目标,必须取并集。
    """
    if selective_replan_core is None or scheduler_facts_store is None:
        return []
    try:
        if hasattr(scheduler_facts_store, "list_selective_replan_for_node"):
            facts = scheduler_facts_store.list_selective_replan_for_node(
                workflow_id, node_id
            )
        else:
            facts = []
            for event in scheduler_facts_store.list_selective_replan_decisions(
                workflow_id,
                limit=scheduler_facts_store.REPLAN_LOOKUP_SCAN_LIMIT,
            ):
                payload = event.get("payload") or {}
                if str(payload.get("mode") or "") != "selective":
                    continue
                if str(payload.get("retry_node") or "") != str(node_id or ""):
                    continue
                facts.append({
                    "event_id": event.get("id"),
                    "timestamp": event.get("timestamp"),
                    **payload,
                })
    except Exception as exc:
        print(
            f"[SELECTIVE REPLAN WARN] workflow={workflow_id} "
            f"node={node_id}: {exc}"
        )
        return []
    return [f for f in (facts or []) if isinstance(f, dict)]


def _selective_replan_awaiting_redispatch(workflow_id, node_id, tasks=None):
    """PR #110:该节点是否存在「已作废、待补派」的 selective 目标谱系。

    以持久化 selective_replan_decision 事实为唯一依据(无事实 → False)。
    P1-2:同一节点多条 selective 事实取目标并集( test→[B] + review→[C]
    = [B,C] );已落定谱系无补派候选,自然不再触发,历史滞留无害。
    判定刻意复用补派管线自己的谓词 `lineage_redispatch_candidates`:
    「该谱系当前有补派候选」≡「它能被补派」。两处若各写一套定义,一旦
    分歧(典型:任务被标 superseded_by 指向一个从未创建的替代者),
    这里会永远判 True、节点永远不完成,而补派管线永远给不出候选——
    工作流永久卡死。用同一个谓词,这种状态自动被视为「无事可等」。
    """
    facts = _all_selective_facts(workflow_id, node_id)
    if not facts:
        return False
    roots = {
        str(r).strip()
        for fact in facts
        for r in (fact.get("target_lineage_roots") or [])
        if str(r).strip()
    }
    if not roots:
        return False
    if tasks is None:
        tasks = load_tasks()
    node_tasks = [
        task
        for task in tasks or []
        if isinstance(task, dict)
        and str(task.get("workflow_id") or "") == workflow_id
        and str(task.get("node") or task.get("stage") or "") == node_id
    ]
    try:
        from herdr import direct_dispatch as direct_dispatch_core

        pending = direct_dispatch_core.lineage_redispatch_candidates(node_tasks)
    except Exception as exc:
        print(
            f"[SELECTIVE REPLAN WARN] workflow={workflow_id} "
            f"node={node_id}: redispatch candidates unavailable: {exc}"
        )
        return False
    return any(
        direct_dispatch_core.lineage_key(task.get("task_id"))[0] in roots
        for task in pending
    )


def _selective_gate_inventory_block(workflow_id, workflow_cfg):
    """门禁派发时注入的可归因 Task 清单(PR #110);未开启策略 → None。

    清单只含 retry_node 当前谱系头(历史已作废任务绝不出现在清单里),
    由纯核心构建+渲染;这里只做 I/O 装配。
    """
    if selective_replan_core is None:
        return None
    policy = selective_replan_core.policy_from_workflow(workflow_cfg)
    if policy is None:
        return None
    inventory = selective_replan_core.build_task_inventory(
        load_tasks(), workflow_id=workflow_id, retry_node=policy["retry_node"]
    )
    return selective_replan_core.render_inventory_block(inventory)


def _selective_redispatch_blocker_notes(workflow_id, node_id):
    """补派 replacement 时注入的 blocker 上下文(PR #110);无事实 → None。

    返回 {被作废 task_id: 上下文文本};上下文只进 dispatch prompt,
    旧 Task 本身(状态/元数据)不被改写。
    P1-2:多条 selective 事实按目标取并集,每条目标沿用其所属事实的
    gate note( test→B 用 test 的 blocker, review→C 用 review 的)。
    """
    facts = _all_selective_facts(workflow_id, node_id)
    if not facts:
        return None
    notes = {}
    for fact in facts:
        try:
            render = selective_replan_core.render_replacement_blocker_note
        except Exception:
            continue
        for raw_id in fact.get("target_task_ids") or []:
            task_id = str(raw_id).strip()
            if task_id and task_id not in notes:
                notes[task_id] = render(fact, task_id)
    return notes or None


def _blocked_gate_tasks_for(workflow_id, gate_node_id):
    """该门禁节点当前存活的 blocked 结论任务(快照用,不含 superseded)。"""
    matched = []
    for task in load_tasks():
        if task.get("workflow_id") != workflow_id:
            continue
        if gate_node_id not in (task.get("node"), task.get("stage")):
            continue
        if task.get("status") == "superseded":
            continue
        if task.get("stage_verdict") == "blocked":
            matched.append(task)
    return matched


def _retry_node_for_gate(workflow_cfg, gate_cfg, gate_node_id):
    """解析 fix-loop 的 retry_node(含历史 fallback,与 handle 一致)。"""
    retry_node = (gate_cfg or {}).get("retry_node", "implementation")
    nodes_by_id = {n["id"]: n for n in (workflow_cfg or {}).get("nodes", [])}
    if nodes_by_id and retry_node not in nodes_by_id:
        gate_deps = nodes_by_id.get(gate_node_id, {}).get("depends_on", [])
        fallback = next(
            (dep for dep in gate_deps if dep in nodes_by_id),
            None,
        ) or next(iter(nodes_by_id))
        retry_node = fallback
    return retry_node


def invalidate_for_merged_fix_loop(
    workflow_id, gate_node_ids, workflow_cfg, retry_node=None,
    selective_target_task_ids=None,
):
    """多门禁共享 retry_node 时的单次原子作废(P1-2)。

    node 闭包取各门禁下游的并集 + retry 下游 + retry 自身(selective);
    目标两阶段提交与单门禁完全一致(任一目标 pending 即返回,
    各门禁/下游一律不动)。返回 SelectiveInvalidationResult 或普通 list。
    """
    gate_ids = sorted({str(g).strip() for g in (gate_node_ids or []) if str(g).strip()})
    if not gate_ids:
        return []
    primary = gate_ids[0]
    nodes_by_id = {n["id"]: n for n in (workflow_cfg or {}).get("nodes", [])}
    if not nodes_by_id and (workflow_cfg or {}).get("stages"):
        prev = None
        for stage in (workflow_cfg or {}).get("stages", []):
            stage_id = stage.get("key") or stage.get("id")
            nodes_by_id[stage_id] = {
                "id": stage_id,
                "depends_on": [prev] if prev else [],
            }
            prev = stage_id
    node_ids: set = set()
    for gid in gate_ids:
        node_ids |= set(_collect_downstream_nodes(nodes_by_id, gid))
    if retry_node and retry_node in nodes_by_id:
        node_ids |= _collect_downstream_nodes(nodes_by_id, retry_node) - {retry_node}
        if selective_target_task_ids is not None:
            node_ids.add(retry_node)

    selective_targets = None
    if selective_target_task_ids is not None:
        selective_targets = {
            str(t).strip() for t in selective_target_task_ids if str(t).strip()
        }

    reused = []
    if selective_targets is None:
        reused = _rework_legacy_retry(workflow_id, retry_node, "+".join(gate_ids))
        if reused is None:
            return []

    if selective_targets is not None and retry_node:
        ordered_targets = sorted(selective_targets)
        applied: list = []
        failed: dict = {}
        snapshot = {
            str(t.get("task_id") or ""): t
            for t in load_tasks()
            if isinstance(t, dict) and t.get("workflow_id") == workflow_id
        }
        for target_id in ordered_targets:
            record = snapshot.get(target_id)
            if record is None:
                failed[target_id] = "target_task_unknown"
                continue
            if record.get("status") == "superseded" or record.get("superseded_by"):
                applied.append(target_id)
                continue
            ok, err = _invalidate_single_task(target_id, "+".join(gate_ids), reuse=True)
            if ok:
                applied.append(target_id)
            else:
                failed[target_id] = err
        if selective_replan_core is not None:
            outcome = selective_replan_core.selective_invalidation_outcome(
                ordered_targets, applied, failed
            )
        else:
            outcome = {
                "applied": applied,
                "pending": [t for t in ordered_targets if t not in applied],
                "failed": failed,
                "all_targets_applied": not failed and len(applied) == len(ordered_targets),
            }
        if not outcome["all_targets_applied"]:
            print(
                f"[SELECTIVE REPLAN PARTIAL] workflow={workflow_id} "
                f"gates={'+'.join(gate_ids)} retry={retry_node} "
                f"applied={','.join(outcome['applied'])} "
                f"pending={','.join(outcome['pending'])}"
            )
            return SelectiveInvalidationResult(
                list(outcome["applied"]),
                applied=list(outcome["applied"]),
                pending=list(outcome["pending"]),
                failed=dict(outcome["failed"]),
            )

    invalidated = list(reused)
    invalidated_nodes = set()
    gate_id_set = set(gate_ids)
    for task in load_tasks():
        if task.get("workflow_id") != workflow_id:
            continue
        if task.get("node") not in node_ids and task.get("stage") not in node_ids:
            continue
        status = task.get("status")
        task_id = task["task_id"]
        task_node = task.get("node") or task.get("stage")
        if selective_targets is None and _preserve_same_gate_task(task, retry_node, gate_ids):
            continue
        if task_id in reused or status == "superseded" or (selective_targets is not None and task_id in selective_targets):
            continue
        if (
            selective_targets is not None
            and task_node == retry_node
            and task_id not in selective_targets
        ):
            continue
        if (
            task_node in gate_id_set
            and (not retry_node or retry_node not in gate_id_set)
            and task.get("stage_verdict") == "pass"
            and _fix_loop_keepable(task)
        ):
            continue
        if status in ("completed", "cleanup_ready"):
            result = subprocess.run(
                [TASK_MANAGER, "finalize", task_id],
                text=True, capture_output=True,
            )
            if result.returncode != 0:
                continue
            status = (get_task(task_id) or {}).get("status")
        if status not in FIX_LOOP_SUPERSEDEABLE:
            continue
        result = subprocess.run(
            [TASK_MANAGER, "supersede", task_id,
             "--allow-pending",
             "--reason", f"fix-loop: gates {'+'.join(gate_ids)} blocked"],
            text=True, capture_output=True,
        )
        if result.returncode == 0:
            invalidated.append(task_id)
            if task_node:
                invalidated_nodes.add(task_node)
    if invalidated:
        void_nodes = set(invalidated_nodes) | gate_id_set
        if retry_node:
            void_nodes.add(retry_node)
        _record_invalidation_note(
            workflow_id, "+".join(gate_ids), retry_node, void_nodes, invalidated
        )
    if selective_targets is not None:
        try:
            phase1 = list(ordered_targets)
        except NameError:
            phase1 = sorted(selective_targets)
        combined = list(phase1) + [t for t in invalidated if t not in phase1]
        return SelectiveInvalidationResult(
            combined, applied=list(phase1), pending=[], failed={})
    void_primary = sorted(invalidated_nodes | {primary})
    if retry_node:
        void_primary.append(retry_node)
    return invalidated


def _handle_merged_fix_loops(workflow_id, retry_node, gates, workflow_cfg):
    """同一 retry_node 多门禁 blocked 的单次合并处理(P1-2)。

    gates: [(gate_node_id, gate_cfg)] 已按 gate id 排序,确定性。
    流程:快照全部 blocked 任务 → 逐门禁 resolve(各 persist 其事实)
    → 任一 fallback 即整组回退 legacy 逐个处理 → 全 selective 则
    union targets 单次原子作废 + 单次 latch/通知。
    """
    from herdr import fix_loop as fix_loop_core

    gate_ids = [str(gid) for gid, _ in gates]
    # 快照:各门禁存活 blocked 任务(后续作废不得影响快照)。
    snapshots: dict = {}
    for gid, _gcfg in gates:
        snapshots[gid] = _blocked_gate_tasks_for(workflow_id, gid)
    # 任一门禁无存活 blocked 任务 → 整组无事可做(幂等空转)。
    if not any(snapshots.values()):
        return
    blockers = []
    for gid in sorted(snapshots):
        for task in sorted(snapshots[gid], key=lambda t: str(t.get("task_id") or "")):
            blockers.append({
                "task_id": task.get("task_id"),
                "note": task.get("stage_verdict_note", ""),
            })
    suggested_branch = latest_branch_for_node(workflow_id, retry_node)
    if selective_replan_core is not None:
        affected_union = selective_replan_core.merge_affected_task_ids([
            task.get("stage_verdict_affected_task_ids")
            for tasks in snapshots.values() for task in tasks
        ])
    else:
        affected_union = []
    fingerprint = fix_loop_core.verdict_fingerprint(
        suggested_branch, blockers, affected_task_ids=affected_union
    )
    try:
        max_loops = min(int((gcfg or {}).get("max_loops", FIX_LOOP_MAX)) for _, gcfg in gates)
    except (TypeError, ValueError):
        max_loops = FIX_LOOP_MAX
    with lock:
        _state = load_stage_state()
        try:
            prior_count = int(_state.get(f"{workflow_id}|fixloop|{retry_node}", 0))
        except (TypeError, ValueError):
            prior_count = 0
        stored_fp = _state.get(f"{workflow_id}|fixloop|{retry_node}|fp")
    # 任一门禁已有 exhaustion 记录即整组升级( fail-closed,不重试已升级门禁)。
    for gid in gate_ids:
        if _fix_loop_exhaustion_episode(workflow_id, gid):
            reason = "max_loops_exhausted"
            summary = fix_loop_core.summarize_fix_loop_item({
                "workflow_id": workflow_id, "gate_stage": "+".join(sorted(gate_ids)),
                "retry_node": retry_node, "loop_count": prior_count,
                "max_loops": max_loops, "suggested_branch": suggested_branch,
                "blockers": blockers, "exhausted": True,
            })
            _record_fix_loop_exhaustion(
                workflow_id, gid, retry_node, reason, summary, fingerprint)
            with lock:
                _state = load_stage_state()
                _state[f"{workflow_id}|fixloop|{retry_node}|pending_redo"] = {
                    "ts": time.time(), "gate": "+".join(sorted(gate_ids)),
                    "gates": sorted(gate_ids),
                }
                save_stage_state(_state)
            coordinator_queue.put({
                "kind": "fix_loop", "workflow_id": workflow_id,
                "gate_stage": "+".join(sorted(gate_ids)), "retry_node": retry_node,
                "blockers": blockers, "invalidated": [], "loop_count": prior_count,
                "max_loops": max_loops, "suggested_branch": suggested_branch,
                "exhausted": True, "escalation_reason": reason,
            })
            return
    if fix_loop_core.fix_loop_exhausted(prior_count, max_loops) or \
            fix_loop_core.is_repeat_verdict(fingerprint, stored_fp):
        reason = ("max_loops_exhausted" if fix_loop_core.fix_loop_exhausted(
            prior_count, max_loops) else "repeat_verdict")
        summary = fix_loop_core.summarize_fix_loop_item({
            "workflow_id": workflow_id, "gate_stage": "+".join(sorted(gate_ids)),
            "retry_node": retry_node, "loop_count": prior_count,
            "max_loops": max_loops, "suggested_branch": suggested_branch,
            "blockers": blockers, "exhausted": True,
        })
        for gid in gate_ids:
            _record_fix_loop_exhaustion(
                workflow_id, gid, retry_node, reason, summary, fingerprint)
        with lock:
            _state = load_stage_state()
            _state[f"{workflow_id}|fixloop|{retry_node}|pending_redo"] = {
                "ts": time.time(), "gate": "+".join(sorted(gate_ids)),
                "gates": sorted(gate_ids),
            }
            save_stage_state(_state)
        coordinator_queue.put({
            "kind": "fix_loop", "workflow_id": workflow_id,
            "gate_stage": "+".join(sorted(gate_ids)), "retry_node": retry_node,
            "blockers": blockers, "invalidated": [], "loop_count": prior_count,
            "max_loops": max_loops, "suggested_branch": suggested_branch,
            "exhausted": True, "escalation_reason": reason,
        })
        print(f"[FIX LOOP EXHAUSTED] workflow={workflow_id} gates={'+'.join(sorted(gate_ids))} reason={reason}")
        return

    # 逐门禁 resolve(各 persist 其事实)。任一非 selective 即整组回退 legacy。
    per_gate_targets: dict = {}
    per_gate_plans: dict = {}
    all_selective = True
    for gid, gcfg in gates:
        _rn = _retry_node_for_gate(workflow_cfg, gcfg, gid)
        tids, plan = _resolve_selective_replan(
            workflow_id, gid, _rn, workflow_cfg, snapshots[gid], load_tasks())
        if tids is None:
            all_selective = False
            break
        per_gate_targets[gid] = list(tids)
        per_gate_plans[gid] = plan
    if not all_selective:
        # 整组回退 legacy:逐门禁按原单门禁路径处理(legacy  sequential 安全,
        # 无 targets 可丢;首个作废后其余自然空转)。
        for gid, gcfg in gates:
            # 避免递归进合并:直接走单门禁 legacy?调用 handle 会再次检测
            # sibling 并合并 → 无限递归。改为临时只处理单门禁 legacy:
            # 此时 selective 已整体放弃,按 legacy invalidate 单门禁。
            _legacy_blocked = _blocked_gate_tasks_for(workflow_id, gid)
            if not _legacy_blocked:
                continue
            _inv = invalidate_for_fix_loop(
                workflow_id, gid, workflow_cfg,
                retry_node=_retry_node_for_gate(workflow_cfg, gcfg, gid),
                selective_target_task_ids=None,
            )
            if _inv:
                _lc = _bump_fix_loop_count(
                    workflow_id, _retry_node_for_gate(workflow_cfg, gcfg, gid))
                with lock:
                    _st = load_stage_state()
                    _st[f"{workflow_id}|fixloop|{_retry_node_for_gate(workflow_cfg, gcfg, gid)}|pending_redo"] = {
                        "ts": time.time(), "gate": gid}
                    save_stage_state(_st)
                coordinator_queue.put({
                    "kind": "fix_loop", "workflow_id": workflow_id,
                    "gate_stage": gid,
                    "retry_node": _retry_node_for_gate(workflow_cfg, gcfg, gid),
                    "blockers": [{"task_id": t.get("task_id"),
                                  "note": t.get("stage_verdict_note", "")}
                                 for t in _legacy_blocked],
                    "invalidated": list(_inv), "loop_count": _lc,
                    "max_loops": (gcfg or {}).get("max_loops", FIX_LOOP_MAX),
                    "suggested_branch": latest_branch_for_node(
                        workflow_id, _retry_node_for_gate(workflow_cfg, gcfg, gid)),
                })
        return

    union_targets = selective_replan_core.merge_affected_task_ids(
        list(per_gate_targets.values())) if selective_replan_core else sorted(
        {t for v in per_gate_targets.values() for t in v})
    union_roots = sorted({
        str(r).strip()
        for plan in per_gate_plans.values()
        for r in ((plan or {}).get("target_lineage_roots") or [])
        if str(r).strip()})
    # 单次原子作废(目标两阶段,门禁取并集)。
    invalidated = invalidate_for_merged_fix_loop(
        workflow_id, gate_ids, workflow_cfg, retry_node=retry_node,
        selective_target_task_ids=union_targets)
    if isinstance(invalidated, SelectiveInvalidationResult) and not invalidated.all_targets_applied:
        print(f"[SELECTIVE REPLAN DEFERRED] workflow={workflow_id} gates={'+'.join(sorted(gate_ids))} pending={','.join(invalidated.pending)} awaiting retry")
        return
    if not invalidated:
        return
    clear_stage_advance(workflow_id, retry_node)
    loop_count = _bump_fix_loop_count(workflow_id, retry_node)
    # latch 取各事实最早创建时间(恒早于一切 replacement,避免重试 race)。
    fact_ts = 0.0
    for plan in per_gate_plans.values():
        try:
            cand = float((plan or {}).get("created_at") or 0)
        except (TypeError, ValueError):
            cand = 0
        if cand and (not fact_ts or cand < fact_ts):
            fact_ts = cand
    with lock:
        _state = load_stage_state()
        _pending = {"ts": fact_ts or time.time(),
                    "gate": "+".join(sorted(gate_ids)),
                    "gates": sorted(gate_ids),
                    "mode": "selective",
                    "target_lineage_roots": union_roots}
        _state[f"{workflow_id}|fixloop|{retry_node}|pending_redo"] = _pending
        _state[f"{workflow_id}|fixloop|{retry_node}|fp"] = fingerprint
        save_stage_state(_state)
    coordinator_queue.put({
        "kind": "fix_loop", "workflow_id": workflow_id,
        "gate_stage": "+".join(sorted(gate_ids)), "retry_node": retry_node,
        "blockers": blockers, "invalidated": list(invalidated),
        "loop_count": loop_count,
        "max_loops": max_loops, "suggested_branch": suggested_branch,
        "mode": "selective", "target_lineage_roots": union_roots,
    })
    print(f"[FIX LOOP QUEUED] workflow={workflow_id} gates={'+'.join(sorted(gate_ids))} retry={retry_node} loop={loop_count} invalidated={len(invalidated)}")


def handle_fix_loop(workflow_id, gate_node_id, gate_cfg, workflow_cfg):
    """原子作废 + 计数 + 投递 fix_loop 事件;幂等(无作废即不重发)。"""
    retry_node = gate_cfg.get("retry_node", "implementation")

    nodes_by_id = {
        n["id"]: n for n in workflow_cfg.get("nodes", [])
    }
    if nodes_by_id and retry_node not in nodes_by_id:
        gate_deps = nodes_by_id.get(gate_node_id, {}).get("depends_on", [])
        fallback = next(
            (dep for dep in gate_deps if dep in nodes_by_id),
            None,
        ) or next(iter(nodes_by_id))
        print(
            f"[FIX LOOP RETRY NODE FALLBACK] "
            f"{retry_node} not in workflow nodes, using {fallback}"
        )
        retry_node = fallback

    # P1-2:同一 retry_node 的多门禁并行 blocked 时,先冻结本轮全部
    # blocked gate facts,再进入 invalidation。否则先处理的 gate 会把
    # 另一个 blocked gate Task 一并作废,后者的 affected_task_ids 丢失。
    # 仅 selective 策略开启时合并;legacy 工作流保持逐门禁原行为。
    try:
        _merge_enabled = (
            selective_replan_core is not None
            and selective_replan_core.policy_from_workflow(workflow_cfg) is not None
            and selective_replan_core.policy_from_workflow(
                workflow_cfg)["retry_node"] == str(retry_node or "")
        )
    except Exception:
        _merge_enabled = False
    if _merge_enabled:
        try:
            _sibling_gates: dict = {}
            for _task in load_tasks():
                if not isinstance(_task, dict):
                    continue
                if _task.get("workflow_id") != workflow_id:
                    continue
                if _task.get("status") == "superseded":
                    continue
                if _task.get("stage_verdict") != "blocked":
                    continue
                _node = str(_task.get("node") or _task.get("stage") or "")
                if not _node:
                    continue
                try:
                    _gcfg = resolve_gate_config(
                        (workflow_cfg.get("nodes") and
                         {n["id"]: n for n in workflow_cfg.get("nodes", [])}.get(_node)),
                        _node,
                    )
                except Exception:
                    _gcfg = None
                if not _gcfg:
                    continue
                _rn = _retry_node_for_gate(workflow_cfg, _gcfg, _node)
                if _rn != retry_node:
                    continue
                _sibling_gates.setdefault(_node, _gcfg)
            if len(_sibling_gates) > 1 and str(gate_node_id) in _sibling_gates:
                _ordered = sorted(_sibling_gates)
                # P1-2 round-2:组内任一门禁触发都必须进入合并处理,不得再要求
                # 调用方恰好是字典序首位。真实 sweep 经 blocked_gate_dependency
                # 按 depends_on 顺序永远先返回 test,而 sorted 首位是 review,
                # 旧限制会让合并入口永久不可达。_handle_merged_fix_loops 对
                # 已处理过的组自然空转(快照无存活 blocked 即返回),故任意入口
                # 触发都是幂等的。
                _merged = [(gid, _sibling_gates[gid]) for gid in _ordered]
                _handle_merged_fix_loops(workflow_id, retry_node, _merged, workflow_cfg)
                return
        except Exception as exc:
            print(
                f"[SELECTIVE REPLAN WARN] workflow={workflow_id} "
                f"sibling gate scan skipped: {exc}"
            )

    blockers = []
    blocked_gate_tasks = []

    for task in load_tasks():
        if task.get("workflow_id") != workflow_id:
            continue
        if gate_node_id not in (task.get("node"), task.get("stage")):
            continue
        if task.get("status") == "superseded":
            continue
        if task.get("stage_verdict") == "blocked":
            blockers.append(
                {
                    "task_id": task.get("task_id"),
                    "note": task.get("stage_verdict_note", ""),
                }
            )
            blocked_gate_tasks.append(task)

    from herdr import fix_loop as fix_loop_core

    try:
        max_loops = int((gate_cfg or {}).get("max_loops", FIX_LOOP_MAX))
    except (TypeError, ValueError):
        max_loops = FIX_LOOP_MAX
    suggested_branch = latest_branch_for_node(workflow_id, retry_node)
    # PR #110:同一 blocker 指向不同目标集合是两个不同的 verdict,
    # 显式 affected_task_ids 并入指纹(为空时与 legacy 逐字节一致)。
    affected_union = []
    for task in blocked_gate_tasks:
        for raw_id in (task.get("stage_verdict_affected_task_ids") or []):
            text = str(raw_id).strip()
            if text and text not in affected_union:
                affected_union.append(text)
    fingerprint = fix_loop_core.verdict_fingerprint(
        suggested_branch, blockers, affected_task_ids=affected_union
    )

    with lock:
        _state = load_stage_state()
        prior_count = _state.get(f"{workflow_id}|fixloop|{retry_node}", 0)
        try:
            prior_count = int(prior_count)
        except (TypeError, ValueError):
            prior_count = 0
        stored_fp = _state.get(f"{workflow_id}|fixloop|{retry_node}|fp")

    if _exhaustion_fingerprint(
        _fix_loop_exhaustion_episode(workflow_id, gate_node_id)
    ) == fingerprint:
        return

    if fix_loop_core.fix_loop_exhausted(
        prior_count, max_loops
    ) or fix_loop_core.is_repeat_verdict(fingerprint, stored_fp):
        reason = (
            "max_loops_exhausted"
            if fix_loop_core.fix_loop_exhausted(prior_count, max_loops)
            else "repeat_verdict"
        )
        summary = fix_loop_core.summarize_fix_loop_item(
            {
                "workflow_id": workflow_id,
                "gate_stage": gate_node_id,
                "retry_node": retry_node,
                "loop_count": prior_count,
                "max_loops": max_loops,
                "suggested_branch": suggested_branch,
                "blockers": blockers,
                "exhausted": True,
            }
        )
        _record_fix_loop_exhaustion(
            workflow_id, gate_node_id, retry_node, reason, summary,
            fingerprint,
        )
        with lock:
            _state = load_stage_state()
            _state[f"{workflow_id}|fixloop|{retry_node}|pending_redo"] = {
                "ts": time.time(),
                "gate": gate_node_id,
            }
            save_stage_state(_state)
        coordinator_queue.put(
            {
                "kind": "fix_loop",
                "workflow_id": workflow_id,
                "gate_stage": gate_node_id,
                "retry_node": retry_node,
                "blockers": blockers,
                "invalidated": [],
                "loop_count": prior_count,
                "max_loops": max_loops,
                "suggested_branch": suggested_branch,
                "exhausted": True,
                "escalation_reason": reason,
            }
        )
        print(
            f"[FIX LOOP EXHAUSTED] "
            f"workflow={workflow_id} "
            f"gate={gate_node_id} "
            f"retry={retry_node} "
            f"reason={reason} "
            f"loops={prior_count}/{max_loops}"
        )
        return

    # PR #110:选择性返工决策先于一切作废——先持久化事实,再定向作废;
    # legacy(None) 优先继续原任务；精确候选终态保持历史替换契约。
    selective_ids, replan_plan = _resolve_selective_replan(
        workflow_id, gate_node_id, retry_node, workflow_cfg,
        blocked_gate_tasks, load_tasks(),
    )

    invalidated = invalidate_for_fix_loop(
        workflow_id, gate_node_id, workflow_cfg, retry_node=retry_node,
        selective_target_task_ids=selective_ids,
    )

    if selective_ids is not None and isinstance(
        invalidated, SelectiveInvalidationResult
    ) and not invalidated.all_targets_applied:
        # P1-1:部分作废 fail-closed。门禁/下游一律未动(两阶段提交),
        # 无 latch、无计数、无通知;门禁仍 blocked,下轮 sweep 基于
        # 已持久化 Fact 重试剩余目标,绝不带着全量 roots 去等 C-r2。
        print(
            f"[SELECTIVE REPLAN DEFERRED] workflow={workflow_id} "
            f"gate={gate_node_id} retry={retry_node} "
            f"pending={','.join(invalidated.pending)} "
            "awaiting retry"
        )
        return

    if not invalidated:
        return

    if selective_ids is not None:
        # 选择性作废了 retry_node 内部谱系:清除阶段推进闩,
        # 下一轮 sweep 只补派已作废谱系；rework 目标保留原执行。
        clear_stage_advance(workflow_id, retry_node)

    loop_count = _bump_fix_loop_count(workflow_id, retry_node)

    with lock:
        _state = load_stage_state()
        pending_redo = {
            "ts": time.time(),
            "gate": gate_node_id,
        }
        if selective_ids is not None:
            pending_redo["mode"] = "selective"
            pending_redo["target_lineage_roots"] = list(
                (replan_plan or {}).get("target_lineage_roots") or []
            )
            # P1-1: latch 边界取 Fact 创建时间(首轮持久化时刻),
            # 而非本轮作废完成时刻。部分重试场景下 B-r2 可能在首轮
            # 部分作废后提前派发;若 latch 取第二轮时间,会把已完成
            # 的 B-r2 判为 stale 而永久等待。Fact 时间恒早于一切
            # replacement,旧完成( gated 前)又恒早于 Fact,故安全。
            try:
                _fact_ts = float((replan_plan or {}).get("created_at") or 0)
            except (TypeError, ValueError):
                _fact_ts = 0
            if _fact_ts > 0:
                pending_redo["ts"] = _fact_ts
        _state[f"{workflow_id}|fixloop|{retry_node}|pending_redo"] = pending_redo
        _state[f"{workflow_id}|fixloop|{retry_node}|fp"] = fingerprint
        save_stage_state(_state)

    fix_loop_item = {
        "kind": "fix_loop",
        "workflow_id": workflow_id,
        "gate_stage": gate_node_id,
        "retry_node": retry_node,
        "blockers": blockers,
        "invalidated": list(invalidated),
        "loop_count": loop_count,
        "max_loops": gate_cfg.get("max_loops", FIX_LOOP_MAX),
        "suggested_branch": suggested_branch,
    }
    if selective_ids is not None:
        # 目标上下文随通知持久化,coordinator 补投判断保持 target-aware。
        fix_loop_item["mode"] = "selective"
        fix_loop_item["target_lineage_roots"] = list(
            (replan_plan or {}).get("target_lineage_roots") or []
        )
    coordinator_queue.put(fix_loop_item)

    print(
        f"[FIX LOOP QUEUED] "
        f"workflow={workflow_id} "
        f"gate={gate_node_id} "
        f"retry={retry_node} "
        f"loop={loop_count} "
        f"invalidated={len(invalidated)}"
    )


def coordinator_pane_for_workflow(workflow_id=None):
    if workflow_id:
        project = project_for_workflow(workflow_id) or {}
        pane = project.get("coordinator_pane_id")
        if pane:
            return pane

    return None

listeners = {}
task_sockets = {}

lock = threading.Lock()

coordinator_queue = queue.Queue()
queued_events = set()

# Per-workflow concurrency: each workflow gets its own execution slot so that
# a blocked coordinator for workflow A cannot stall dispatch for workflow B.
_wf_dispatch_locks: dict = {}
_wf_dispatch_locks_meta = threading.Lock()
_coordinator_executor = ThreadPoolExecutor(
    max_workers=16,
    thread_name_prefix="coord-worker"
)


def _workflow_dispatch_lock(workflow_id: str) -> threading.Lock:
    """Return the per-workflow serialization lock (created on first use)."""
    with _wf_dispatch_locks_meta:
        if workflow_id not in _wf_dispatch_locks:
            _wf_dispatch_locks[workflow_id] = threading.Lock()
        return _wf_dispatch_locks[workflow_id]


# ============================================================
# Registry
# ============================================================

def load_tasks():
    store = _get_store()
    return store.list_tasks()


def get_task(task_id):
    store = _get_store()
    return store.get_task(task_id)


def set_task_status(
    task_id,
    status,
    *,
    expected_status=None,
    expected_version=None,
    source="herdr-controller",
    metadata=None,
):
    """Set a task status, optionally through the atomic StateStore gateway."""
    if expected_status is not None or expected_version is not None:
        from herdr import kernel

        result = kernel.transition_task(
            task_id=task_id,
            to_status=status,
            reason=f"{source}:{status}",
            source=source,
            metadata=metadata or {},
            expected_status=expected_status,
            expected_version=expected_version,
            store=_get_store(),
        )
        if not result.get("accepted", True):
            print(
                f"[STATE CAS REJECTED] {task_id}: "
                f"{result.get('reason', 'cas_mismatch')}"
            )
            return False
        return True

    result = subprocess.run(
        [
            TASK_MANAGER,
            "set",
            task_id,
            status
        ],
        text=True,
        capture_output=True
    )

    if result.returncode != 0:
        print(
            f"[STATE ERROR] {task_id}: "
            f"{result.stdout.strip() or result.stderr.strip()}"
        )
        return False

    print(
        f"[STATE] "
        f"{result.stdout.strip()}"
    )

    return True


def _task_version(task):
    try:
        version = int(task.get("version") or 0)
    except (AttributeError, TypeError, ValueError):
        return None
    return version if version > 0 else None


def _set_observed_status(task, status, reason):
    """Transition from a Controller snapshot without a get/rewrite TOCTOU."""
    task = task or {}
    if os.environ.get("HERDR_CONTROLLER_TEST") or _task_version(task) is None:
        return set_task_status(task.get("task_id"), status)
    return set_task_status(
        task.get("task_id"),
        status,
        expected_status=task.get("status"),
        expected_version=_task_version(task),
        source="herdr-controller",
        metadata={"controller_reason": reason},
    )


def _completion_elapsed(task, now):
    try:
        started = float(
            (task or {}).get("started_at")
            or (task or {}).get("created_at")
            or now
        )
    except (TypeError, ValueError):
        started = float(now)
    return float(now) - started


def process_completion_observation(task, now=None):
    """Controller-only completion arbitration for a Sentinel observation.

    Sentinel only records pane samples.  This function is the sole owner of
    the resulting ``working/dispatched -> agent_done`` transition and always
    supplies the observed status/version to the StateStore CAS.
    """
    task = task or {}
    if task.get("completion_protocol") == "receipt-v1":
        return False
    task_id = task.get("task_id")
    if not task_id:
        return False
    store = _get_store()
    now = time.time() if now is None else float(now)
    expected_status = task.get("status")
    expected_version = _task_version(task)
    try:
        result = store.compare_and_set_completion_transition(
            task_id,
            reason="completion_sentinel",
            source="herdr-controller",
            metadata={"sentinel_reason": "completion_sentinel"},
            expected_status=expected_status,
            expected_version=expected_version,
            now=now,
        )
    except (AttributeError, NotImplementedError):
        # A store without the atomic observation gateway is unsupported; never
        # fall back to a weaker status-only CAS.
        return False
    observation = result.get("observation") or {}
    if not result.get("accepted", True):
        quiet_reasons = {
            "completion_observation_missing",
            "completion_marker_absent",
            "completion_marker_vanished",
            "completion_first_sample_missing",
            "completion_confirmation_missing",
            "completion_sample_interval_short",
            "agent_not_idle",
            "completion_elapsed_short",
            "completion_status_inactive",
            "completion_epoch_unstable",
            "observation_epoch_stale",
            "completion_observation_stale",
        }
        if result.get("reason") not in quiet_reasons:
            try:
                store.record_event(
                    "completion_sentinel_cas_rejected",
                    {
                        "task_id": task_id,
                        "reason": result.get("reason"),
                        "expected_status": observation.get("observed_status"),
                        "expected_version": observation.get("observed_version"),
                        "authoritative": result.get("current") or {},
                    },
                    workflow_id=task.get("workflow_id"),
                    node_id=task.get("node") or task.get("stage"),
                    task_id=task_id,
                    source="herdr-controller",
                )
            except (OSError, RuntimeError, ValueError, AttributeError):
                pass
        return False
    try:
        store.record_event(
            "completion_sentinel_accepted",
            {
                "task_id": task_id,
                "elapsed_seconds": _completion_elapsed(task, now),
                "confirmations": observation.get("consecutive_samples"),
                "observed_version": observation.get("observed_version"),
            },
            workflow_id=task.get("workflow_id"),
            node_id=task.get("node") or task.get("stage"),
            task_id=task_id,
            source="herdr-controller",
        )
    except (OSError, RuntimeError, ValueError, AttributeError):
        pass
    enqueue_coordinator_event(result.get("task") or task, "done")
    return True


_COMPLETION_SCHEDULING_CURSORS = {}


def process_structured_completions(now=None):
    """Advance a bounded scheduling cursor even when execution gates reject."""
    from herdr.completion_receipt import consume_completion_receipt, pending_completion_task_ids
    store = _get_store()
    key = str(Path(store.db_path).resolve())
    if key not in _COMPLETION_SCHEDULING_CURSORS and len(_COMPLETION_SCHEDULING_CURSORS) >= 64:
        _COMPLETION_SCHEDULING_CURSORS.pop(next(iter(_COMPLETION_SCHEDULING_CURSORS)))
    cursor = _COMPLETION_SCHEDULING_CURSORS.get(key, 0)
    rows = pending_completion_task_ids(store, now=now, after_rowid=cursor, with_rows=True)
    if not rows and cursor:
        rows = pending_completion_task_ids(store, now=now, after_rowid=0, with_rows=True)
        _COMPLETION_SCHEDULING_CURSORS[key] = 0
    accepted = 0
    for task_id, rowid in rows:
        _COMPLETION_SCHEDULING_CURSORS[key] = rowid
        accepted += bool(consume_completion_receipt(task_id, store, now=now).get('accepted'))
    return accepted


def process_all_completion_observations(now=None):
    """Process durable Sentinel samples for all active tasks."""
    processed = 0
    for task in load_tasks():
        if task.get("status") not in {"dispatched", "working"}:
            continue
        if process_completion_observation(task, now=now):
            processed += 1
    return processed


def process_blocked_observations():
    """Apply one Controller-owned CAS for each fresh Sentinel blocker sample.

    A sample recorded before a ``set-status`` / human reopen / other Controller
    write is stale by construction: its ``observed_version`` can never match
    again.  Re-attempting that CAS on every sweep burns the transition budget
    and grows the events ledger without bound (observed: 238 identical
    rejections over 25 minutes on one task) while the task makes no progress.
    A stale sample is therefore skipped silently until Sentinel records a
    fresh one; rejections that are *not* explained by staleness are still
    recorded, but deduplicated per (task, expected_version).
    """
    from herdr import kernel
    from herdr.completion import observation_is_current

    store = _get_store()
    processed = 0
    for task in load_tasks():
        if task.get("status") not in {"dispatched", "working", "rework"}:
            _blocked_observation_stale.pop(task.get("task_id"), None)
            _blocked_observation_rejected.pop(task.get("task_id"), None)
            continue
        try:
            events = store.list_events(
                task_id=task.get("task_id"),
                event_type="blocked_marker_observed",
                limit=1,
                desc=True,
            )
        except (AttributeError, OSError, RuntimeError, ValueError):
            continue
        if not events:
            continue
        payload = events[0].get("payload") or {}
        expected_version = payload.get("observed_version")
        expected_status = payload.get("observed_status") or task.get("status")

        if not observation_is_current(
            payload,
            authoritative_status=task.get("status"),
            authoritative_version=_task_version(task),
        ):
            task_id = task["task_id"]
            if _blocked_observation_stale.get(task_id) != expected_version:
                _blocked_observation_stale[task_id] = expected_version
                print(
                    f"[BLOCKED OBSERVATION STALE] "
                    f"task={task_id} "
                    f"observed_version={expected_version} "
                    f"authoritative_version={_task_version(task)} -> "
                    f"awaiting fresh Sentinel sample"
                )
            continue
        _blocked_observation_stale.pop(task.get("task_id"), None)

        result = kernel.transition_task(
            task_id=task["task_id"],
            to_status="blocked",
            reason="inner_loop_exhausted",
            source="herdr-controller",
            metadata={"sentinel_reason": "inner_loop_exhausted"},
            expected_status=expected_status,
            expected_version=expected_version,
            store=store,
        )
        if not result.get("accepted", True):
            # An unexpected rejection still matters, but one fact recorded once
            # per sample beats re-asserting it on every sweep.
            task_id = task["task_id"]
            if _blocked_observation_rejected.get(task_id) != expected_version:
                _blocked_observation_rejected[task_id] = expected_version
                try:
                    store.record_event(
                        "blocked_observation_cas_rejected",
                        {
                            "task_id": task_id,
                            "expected_version": expected_version,
                            "expected_status": expected_status,
                            "authoritative_status": task.get("status"),
                            "authoritative_version": _task_version(task),
                            "reason": result.get("reason"),
                        },
                        workflow_id=task.get("workflow_id"),
                        node_id=task.get("node") or task.get("stage"),
                        task_id=task_id,
                        source="herdr-controller",
                    )
                except (OSError, RuntimeError, ValueError, AttributeError):
                    pass
            continue
        _blocked_observation_rejected.pop(task["task_id"], None)
        processed += 1
        fresh = result.get("task") or task
        enqueue_coordinator_event(fresh, "inner_loop_exhausted")
    return processed


# ============================================================
# Stage policies
# ============================================================

def load_stage_policies():
    try:
        with open(
            STAGE_POLICIES_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f)

    except Exception as e:
        print(
            f"[STAGE POLICY ERROR] {e}"
        )
        return {}


def get_stage_policy(stage_key):
    policies = load_stage_policies()

    return policies.get(
        stage_key,
        {}
    )



# ============================================================
# Workflow stage state
# ============================================================

def load_stage_state():
    if not os.path.exists(STAGE_STATE_FILE):
        return {}

    try:
        with open(
            STAGE_STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f)
    except Exception:
        return {}


def save_stage_state(data):
    tmp = STAGE_STATE_FILE + ".tmp"

    with open(
        tmp,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

    os.replace(
        tmp,
        STAGE_STATE_FILE
    )


def stage_advance_key(
    workflow_id,
    stage
):
    return f"{workflow_id}:{stage}"


def mark_stage_advance_queued(
    workflow_id,
    stage
):
    key = stage_advance_key(
        workflow_id,
        stage
    )

    with lock:
        state = load_stage_state()

        if state.get(key) in (
            "queued",
            "notified"
        ):
            return False

        state[key] = "queued"
        save_stage_state(state)

    return True


def mark_stage_advance_notified(
    workflow_id,
    stage
):
    key = stage_advance_key(
        workflow_id,
        stage
    )

    with lock:
        state = load_stage_state()
        state[key] = "notified"
        save_stage_state(state)


def clear_stage_advance(
    workflow_id,
    stage
):
    key = stage_advance_key(
        workflow_id,
        stage
    )

    with lock:
        state = load_stage_state()
        state.pop(key, None)
        save_stage_state(state)
def reset_queued_stage_states():
    with lock:
        state = load_stage_state()
        changed = False
        for k in list(state.keys()):
            if state[k] == "queued":
                del state[k]
                changed = True
        if changed:
            save_stage_state(state)



# ============================================================
# Coordinator queue
# ============================================================

def get_stage_status(
    workflow_id,
    stage
):
    try:
        output = subprocess.check_output(
            [
                TASK_MANAGER,
                "stage-status",
                workflow_id,
                stage
            ],
            text=True
        )

        return json.loads(output)

    except Exception as e:
        print(
            f"[STAGE STATUS ERROR] "
            f"workflow={workflow_id} "
            f"stage={stage}: {e}"
        )
        return None


def is_node_complete(workflow_id, node_id):
    tasks = [
        t for t in load_tasks()
        if t.get("workflow_id") == workflow_id
        and (t.get("node") == node_id or t.get("stage") == node_id)
    ]
    # Superseded tasks are excluded from completion calculation — they were
    # replaced by another task whose outcome is the authoritative result.
    active = [
        t for t in tasks
        if t.get("status") != "superseded" and not t.get("superseded_by")
    ]

    # No task, or only superseded ones: a reused verifier looks exactly like
    # this, because reuse never creates a Task (PR #108). Its verification is
    # satisfied by an immutable reuse fact bound to the current candidate, so
    # it is complete *for that candidate* — without this the workflow would
    # wait forever on a verifier that is not going to run again, and the join
    # gate would never see a satisfied branch.
    #
    # The binding is re-resolved against the latest frozen candidate on every
    # call, so a later rotation (A -> B -> C) drops the A -> B fact
    # automatically and the node stops counting as complete.
    cfg = workflow_config_for(workflow_id) or {}
    node = next((n for n in cfg.get("nodes", []) if n.get("id") == node_id), {})
    required_ids = node.get("required_task_ids")
    if not active and required_ids is None:
        # Current-candidate verifier reuse is a real replacement for execution;
        # missing/stale evidence remains false, including pending obligations.
        return _reverification_satisfies_node(workflow_id, node_id)

    if scheduler_core is not None:
        return scheduler_core.node_is_complete(tasks, required_ids)
    if required_ids is not None or any(t.get("replacement_pending") for t in tasks):
        return False
    return all(
        t.get("status") in (
            "completed", "committed", "integrated", "cleanup_ready", "cleaned"
        )
        and (t.get("integration_mode") != "git"
             or t.get("status") in ("integrated", "cleanup_ready", "cleaned"))
        for t in active
    )


def reconcile_stage_advance_states(workflow_id, workflow_cfg):
    """Revoke 'notified' stage-state entries when the node they guard has
    regressed — either because a predecessor regressed, or because the node's
    own task lineage died with no live member left to re-dispatch.

    Without this, a stage that regresses after the coordinator was already
    notified would never be re-triggered — because mark_stage_advance_queued
    returns False for 'notified' entries and the node never appears in
    get_ready_nodes again.

    Both regression shapes deadlock the sweep silently:

    * predecessor regressed (upstream check below);
    * the node's own tasks were all superseded while its predecessors stayed
      complete — a fix-loop backflow whose promised replacements were never
      created (zombie obligations). Real deadlock: wf-nexusarchive-1005-01,
      where `test`/`review` kept their 'notified' lock forever and the
      controller skipped both every sweep with no log line at all.
    """
    with lock:
        state = load_stage_state()
        changed = False
        wf_tasks = None

        nodes_by_id = {
            n["id"]: n
            for n in workflow_cfg.get("nodes", [])
        }

        for key in list(state.keys()):
            if not key.startswith(f"{workflow_id}:"):
                continue
            if state[key] != "notified":
                continue

            node_id = key.split(":", 1)[1]
            node = nodes_by_id.get(node_id)
            if not node:
                continue

            # Check whether all predecessors are still complete.
            deps = node.get("depends_on", [])
            predecessors_complete = all(
                is_node_complete(workflow_id, dep)
                for dep in deps
            )
            if predecessors_complete:
                # Upstream is intact, so the node can still regress on its own:
                # fix-loop may have superseded everything it dispatched without
                # the replacements ever landing. Tasks load at most once per call.
                if wf_tasks is None:
                    wf_tasks = load_tasks()
                if direct_dispatch_planner is not None and (
                    direct_dispatch_planner.lineage_redispatch_candidates(
                        direct_dispatch_planner.node_tasks_for_latch(wf_tasks, workflow_id, node_id)
                    )
                ):
                    reason = "own lineage fully superseded with no live task"
                else:
                    continue
            else:
                reason = "predecessors no longer complete"

            del state[key]
            changed = True
            print(
                f"[STAGE REVOKE] "
                f"workflow={workflow_id} node={node_id}: "
                f"{reason}, revoking 'notified' lock"
            )

        if changed:
            save_stage_state(state)


def enqueue_stage_advance(task):
    workflow_id = task.get("workflow_id")
    if workflow_id:
        check_workflow_stage_advance(workflow_id)


def direct_stage_dispatch_enabled():
    value = os.environ.get("HERDR_DIRECT_STAGE_DISPATCH", "1")
    return value.strip().lower() not in ("0", "false", "off", "no")


def coordinator_intake_enabled():
    """新工作流首个节点是否交由总指挥接单(默认开启)。

    产品期望:启动时先由总指挥接收需求、理解目标与约束,再派发第一个
    节点的 Task,而不是系统直接开始(旧直派路径跳过总指挥)。
    设 HERDR_COORDINATOR_INTAKE=0 退回直派快路径。
    """
    value = os.environ.get("HERDR_COORDINATOR_INTAKE", "1")
    return value.strip().lower() not in ("0", "false", "off", "no")


# 单个 launch 必须有界:子进程僵死时不得永久占用调度线程
# 与 per-workflow 锁,超时后走既有 PARTIAL→总指挥回退路径。
DIRECT_DISPATCH_LAUNCH_TIMEOUT = 300
SUPERVISOR_VERIFY_DISPATCH_TIMEOUT = 120


def _dispatch_candidate_ready(project_root, base_branch, specs, workflow_id=None):
    """候选非空预检:onto 分支相对基线无提交时拒绝派发(转总指挥)。

    r6 曾直派测试 main 空候选并恒 blocked，白烧内环。已冻结完整SHA
    同时匹配spec pin与实际onto时，等于基线不代表空候选。未知情况
    （缺 refs、git 失败）一律 fail-open 照常派发。
    """
    ontos = sorted(
        {s.get("onto_branch") or s.get("candidate_branch") for s in (specs or [])
         if s.get("onto_branch") or s.get("candidate_branch")}
    )
    if not ontos:
        return True
    base = (base_branch or "dev").strip() or "dev"
    checked = 0
    empty = 0
    for onto in ontos:
        try:
            result = subprocess.run(
                ["git", "-C", project_root, "rev-list", "--count",
                 f"{base}..{onto}", "--"],
                text=True,
                capture_output=True,
                timeout=10,
            )
        except Exception:
            return True
        if result.returncode != 0:
            return True
        try:
            is_empty = int(result.stdout.strip()) == 0
        except (TypeError, ValueError):
            return True
        checked += 1
        if is_empty:
            empty += 1
    if checked and empty == checked:
        frozen = _scheduler_current_frozen_candidate_sha(workflow_id) if workflow_id else ""
        if re.fullmatch(r"[0-9a-f]{40}", frozen) and all(
            (spec.get("onto_branch") or spec.get("candidate_branch")) and spec.get("candidate_sha") == frozen
            for spec in specs
        ):
            proven = True
            for onto in ontos:
                try:
                    resolved = subprocess.run(
                        ["git", "-C", project_root, "rev-parse", "--verify",
                         f"{onto}^{{commit}}"],
                        text=True, capture_output=True, timeout=10,
                    )
                except Exception:
                    return True  # Preserve the existing unknown-Git policy.
                if resolved.returncode != 0:
                    return True
                if resolved.stdout.strip() != frozen:
                    proven = False
                    break
            if proven:
                return True
        print(
            f"[DIRECT DISPATCH CANDIDATE EMPTY] "
            f"onto={','.join(ontos)} has no commits beyond {base} "
            "-> fallback to coordinator for adjudication"
        )
        return False
    return True


# ============================================================
# Critical-Path Scheduler v1:候选冻结 + 汇聚门禁 (HAFlow PR #107)
#
# - _scheduler_expected_candidate_sha:已发布episode优先，未发布基线沿用旧选择；
#   失败一律返回 ""(fail-open 原语义,门禁侧 fail-closed)。
# - _scheduler_freeze_candidate:implementation 完成后冻结候选(幂等)。
# - _scheduler_join_gate_allows:join 语义节点的确定性放行判定。
# ============================================================

def _scheduler_expected_candidate_sha(workflow_id, project_root, dep_ids, candidate_branch):
    """Use the published candidate; legacy baseline selection never publishes it."""
    frozen = _scheduler_current_frozen_candidate_sha(workflow_id)
    if re.fullmatch(r'[0-9a-f]{40}', frozen or ''):
        if scheduler_core is None:
            return ''
        resolved = scheduler_core.resolve_candidate_sha_for_branch(project_root, f'{frozen}^{{commit}}')
        return frozen if resolved == frozen else ''
    if (
        scheduler_core is None
        or delivery_record_mod is None
        or workflow_docs_mod is None
    ):
        return ""
    try:
        notes = workflow_docs_mod.load_notes(workflow_id)
        effective = delivery_record_mod.select_effective_delivery(
            notes, workflow_id=workflow_id
        )
    except Exception:
        effective = None
    if effective is not None:
        try:
            sha = delivery_record_mod._body_value(effective, "candidate_sha")
        except Exception:
            sha = ""
        sha = str(sha or effective.get("candidate_sha") or "").strip()
        if sha:
            return sha
    if candidate_branch is None:
        # Integration may publish a task ref without changing source HEAD.
        # A verifier can create its own branch at the proven frozen commit;
        # it must not borrow the implementation task's owned branch.
        frozen = _scheduler_current_frozen_candidate_sha(workflow_id)
        if re.fullmatch(r"[0-9a-f]{40}", frozen):
            commit = scheduler_core.resolve_candidate_sha_for_branch(
                project_root, f"{frozen}^{{commit}}"
            )
            if commit == frozen:
                return frozen
        return ""
    try:
        return scheduler_core.resolve_candidate_sha_for_branch(
            project_root, candidate_branch
        )
    except Exception:
        return ""


def _scheduler_freeze_candidate(workflow_id, project_root, source_node, dep_ids=None):
    """Freeze the candidate SHA and ensure a delivery record exists (Plan A).

    Two distinct facts, two distinct lifecycles (do not merge them):
    - candidate_frozen (this function): "which revision this verification
      round targets". Owned by the scheduler; carries no verifier identity.
    - delivery record (written later, by review-pass/wrapup with the REAL
      verifier task ids): "which real tasks verified and formed the".

    Freezing therefore records only the frozen-candidate fact. It never
    invents review_task/test_gate ids: delivery_record treats those as part
    of an immutable fingerprint, so a placeholder would both conflict with
    the real ids later and misrepresent delivery auditability.

    Returns the frozen SHA, or "" when unprovable (join side fail-closed).
    """
    if scheduler_core is None or scheduler_facts_store is None:
        return ""
    # Activity and delivery-note edits are not candidate publication events.
    from herdr import recovery_store, state_db
    store = _get_store()
    workflow, _, _ = recovery_store.read_snapshot(store.db_path, workflow_id)
    epoch = max(float(workflow.get('created_at') or 0), float(workflow.get('reopened_at') or 0))
    conn = state_db.get_readonly_db_connection(store.db_path)
    try:
        publication = conn.execute("SELECT * FROM events WHERE workflow_id=? AND event_type='candidate_published' AND source='herdr-task' AND json_extract(payload_json,'$.execution_id')=? AND timestamp>=? ORDER BY id DESC LIMIT 1",
                                   (workflow_id, workflow.get('execution_id'), epoch)).fetchone()
        episode = conn.execute("SELECT id FROM events WHERE workflow_id=? AND event_type='candidate_frozen' AND source='critical-path-scheduler' AND timestamp>=? ORDER BY id DESC LIMIT 1", (workflow_id, epoch)).fetchone()
    finally:
        conn.close()
    if publication is None or (episode and publication['id'] <= episode['id']):
        frozen = workflow.get('candidate_sha') or ''
        if not frozen:
            return ''
        resolved = scheduler_core.resolve_candidate_sha_for_branch(project_root, f'{frozen}^{{commit}}')
        return frozen if resolved == frozen else ''
    sha = json.loads(publication['payload_json']).get('candidate_sha')
    if scheduler_core.resolve_candidate_sha_for_branch(project_root, f'{sha}^{{commit}}') != sha:
        return ''
    try:
        scheduler_facts_store.record_candidate_frozen(workflow_id, sha, source_node=source_node,
            db_path=store.db_path, expected_episode_id=json.loads(publication['payload_json']).get('expected_episode_id'),
            publication_event_id=publication['id'])
    except ValueError as exc:
        print(f"[SCHEDULER FREEZE REJECTED] workflow={workflow_id}: {exc}")
        return ''
    return recovery_store.read_snapshot(store.db_path, workflow_id)[0].get('candidate_sha') or ''


def _scheduler_previous_candidate_sha(workflow_id):
    """The candidate frozen immediately before the newest one ("" when none).

    Read from the freeze ledger rather than recomputed: the ledger is the
    authoritative record of which revisions were verified in which order, so it
    is what makes A -> B -> A three distinct episodes instead of two.
    """
    if scheduler_facts_store is None:
        return ""
    try:
        events = scheduler_facts_store.list_candidate_frozen_events(workflow_id)
    except Exception as exc:
        print(f"[SCHEDULER FROZEN HISTORY WARN] workflow={workflow_id}: {exc}")
        return ""
    if len(events) < 2:
        return ""
    return str((events[-2].get("payload") or {}).get("candidate_sha") or "")


def _scheduler_frozen_candidate_identity(workflow_id, project_ctx, dep_ids=None):
    """The frozen candidate identity a fallback dispatch must carry verbatim.

    Direct dispatch binds the candidate through ``--candidate-sha`` and
    ``--onto``. When it falls back to the coordinator, that binding would
    otherwise be lost and the coordinator would re-derive a revision on its
    own, giving one scheduler decision two execution semantics.

    This returns the frozen fact only — never a freshly resolved SHA — so the
    fallback path cannot silently substitute a different candidate. Returns
    ("", "") when the scheduler never froze one (legacy workflows, and any
    resolution error), which leaves the legacy coordinator prompt unchanged.
    """
    if scheduler_facts_store is None:
        return "", ""
    try:
        events = scheduler_facts_store.list_candidate_frozen_events(workflow_id)
    except Exception as exc:
        print(f"[SCHEDULER FROZEN IDENTITY WARN] workflow={workflow_id}: {exc}")
        return "", ""
    if not events:
        return "", ""
    payload = (events[-1].get("payload") or {})
    return (
        str(payload.get("candidate_sha") or "").strip(),
        str(payload.get("delivery_branch") or "").strip(),
    )


# ============================================================
# Selective Reverification v1:选择性重新验证 (HAFlow PR #108)
#
# Task states from which a recorded verdict is a settled result. ``superseded``
# belongs here too: superseding is a status change, not a rewrite — it means
# "no longer the current conclusion", and the record of what the task verified
# has to survive for the reuse evidence to be reachable at all. A task that
# never reached one of these states also has no verdict, so it is refused
# twice over (by this filter and by the verdict check).
_SETTLED_VERIFICATION_STATUSES = frozenset((
    "completed", "committed", "integrated", "cleanup_ready", "cleaned",
    "superseded",
))
#
#
# - _reverification_gate_facts:读当前候选上的 reuse 事实(只读,失败即空)。
# - _reverification_satisfies_node:该 verifier 是否已被复用事实满足。
# - _reverification_plan_for_rotation:候选轮换时构建并落盘重新验证计划。
# - _reverification_reused_node:该节点本次不派发(复用是调度决策,不是 Agent 决策)。
# ============================================================

#: Per-sweep memo, keyed by workflow: the resolved policy context *and* the
#: reuse facts derived from it, resolved together.
#:
#: Memoised because ``workflow_config_for`` re-reads and re-parses the workflow
#: YAML from disk on every call, and this path runs once per node per 2-second
#: sweep — without the memo, readiness paid for a full YAML parse per node.
#: Cleared at the top of every sweep, so a fact written by this sweep (the plan)
#: is picked up by the readiness computation in the same pass and a later sweep
#: never trusts a previous pass's snapshot.
_REVERIFICATION_MEMO = {}

#: Bound on memoised workflows, so a long-lived controller that keeps sweeping
#: many workflows does not accumulate an entry for each forever.
_REVERIFICATION_MEMO_LIMIT = 256


def _reset_reverification_memo(workflow_id):
    _REVERIFICATION_MEMO.pop(workflow_id, None)


def _reverification_context_and_facts(workflow_id):
    """Resolve (expected_sha, policy_identity, branch_node_ids, reuse_facts) once.

    Returned as a single memoised tuple because they all come from the same two
    reads, and because the join gate and the readiness computation must agree on
    them exactly.
    """
    if scheduler_facts_store is None or reverification_mod is None:
        return "", "", [], []
    memo = _REVERIFICATION_MEMO.get(workflow_id)
    if memo is not None:
        return memo
    try:
        cfg = workflow_config_for(workflow_id) or {}
        freezes = scheduler_facts_store.list_candidate_frozen_events(workflow_id)
        expected = str(
            (freezes[-1].get("payload") or {}).get("candidate_sha") or ""
        ) if freezes else ""
        # The episode is the freeze event, not the SHA. A rollback re-freezes a
        # SHA that was already frozen; keying reuse on the SHA would let the
        # earlier round's fact resurrect and mark a candidate covered that was
        # never verified.
        episode = str(freezes[-1].get("id") or "") if freezes else ""
        policy_fp = str(reverification_mod.policy_identity(
            reverification_mod.policy_from_workflow(cfg)) or "")
    except Exception as exc:
        print(f"[REVERIFICATION POLICY WARN] workflow={workflow_id}: {exc}")
        return "", "", "", [], []
    if not expected or not policy_fp or not episode:
        _REVERIFICATION_MEMO[workflow_id] = ("", "", "", [], [])
        return _REVERIFICATION_MEMO[workflow_id]
    try:
        node_ids = scheduler_core.verifier_branch_node_ids(cfg)
    except Exception as exc:
        print(f"[REVERIFICATION BRANCHES WARN] workflow={workflow_id}: {exc}")
        return "", "", "", [], []
    found = []
    for node_id in node_ids:
        try:
            fact = scheduler_facts_store.find_reuse_fact(
                workflow_id, node_id, expected, policy_identity=policy_fp,
                episode_id=episode)
        except Exception as exc:
            print(f"[REVERIFICATION FACT WARN] workflow={workflow_id} "
                  f"node={node_id}: {exc}")
            return "", "", "", [], []
        if fact:
            found.append(fact)
    if len(_REVERIFICATION_MEMO) >= _REVERIFICATION_MEMO_LIMIT:
        _REVERIFICATION_MEMO.clear()
    _REVERIFICATION_MEMO[workflow_id] = (
        expected, policy_fp, episode, node_ids, found)
    return _REVERIFICATION_MEMO[workflow_id]


def _reverification_gate_facts(workflow_id):
    """当前候选冻结 + 当前策略下的 reuse 事实(只读)。

    事实按 (verifier, to_candidate_sha, policy_identity, 候选冻结 episode)
    精确绑定,所以这里读到的每一条都只对**当前这一轮**候选有效:候选再次
    轮换、回滚到曾冻结过的 SHA、或策略被收窄/删除,旧事实都失效。
    读取失败一律返回空列表——门禁侧 fail-closed,绝不会因为「查不到」而把
    复用当成通过。
    """
    return _reverification_context_and_facts(workflow_id)[4]


def _reverification_effective(workflow_id, node_id):
    """The verification that currently counts for one node.

    Delegates to the scheduler core rather than re-deriving it here. The core
    owns the precedence rule (fresh evidence outranks a reuse fact, and a reuse
    fact only applies when the branch has no live task), and the join gate
    applies exactly the same rule. Two implementations of one load-bearing
    predicate is how the ledger ends up claiming a verification the gate
    refuses, so there is deliberately only one.
    """
    if scheduler_core is None or reverification_mod is None:
        return {"status": "none", "source": "none"}
    try:
        context = _reverification_context_and_facts(workflow_id)
        return scheduler_core.resolve_effective_verification(
            load_tasks(), workflow_id, node_id, context[0],
            context[4])
    except Exception as exc:
        print(f"[REVERIFICATION EFFECTIVE WARN] workflow={workflow_id} "
              f"node={node_id}: {exc}")
        return {"status": "none", "source": "none"}


def _reverification_satisfies_node(workflow_id, node_id):
    """该 verifier 节点是否已由 reuse 事实满足(仅对当前候选与当前策略)。

    复用事实只对汇聚门禁真正会追问的分支有意义,而 ``_reverification_gate_facts``
    本身就只按分支节点取事实,因此这里不需要再单独做一次 DAG 查询。

    ``scheduler_core`` 的存在检查不能省:本函数在 ``is_node_complete`` 的热
    路径上,而那个函数在 #107 里只依赖任务状态、在 scheduler 缺失时不会崩。
    直接访问 ``scheduler_core.EFFECTIVE_REUSE`` 会在 scheduler 导入失败时抛
    AttributeError,把一个可选组件的缺失升级成控制面崩溃。
    """
    if not node_id or scheduler_core is None:
        return False
    return _reverification_effective(workflow_id, node_id).get("source") == \
        scheduler_core.EFFECTIVE_REUSE


def _reverification_episode_settled(workflow_id, from_sha, to_sha, episode_id):
    """Whether every verifier of this episode already has a recorded decision.

    A rotation stays "the current rotation" until a *third* candidate is frozen,
    so without this check the whole plan — canonicalise, ancestor check, diff,
    and a full task scan — would be recomputed on every 2-second sweep, forever,
    for a decision that is already immutable on disk. The facts are the record;
    re-deriving them cannot change the answer, it can only cost subprocesses.

    Scoped by the freeze episode as well as the SHA pair, so a repeated
    (from, to) in a later round is treated as a fresh episode and re-planned.
    """
    if scheduler_facts_store is None or scheduler_core is None:
        return False
    try:
        expected = _reverification_context_and_facts(workflow_id)[3]
    except Exception:
        return False
    if not expected:
        return False
    try:
        recorded = scheduler_facts_store.list_reverification_decisions(
            workflow_id, limit=scheduler_facts_store.REUSE_LOOKUP_SCAN_LIMIT)
    except Exception:
        return False
    seen = set()
    for event in recorded:
        payload = event.get("payload") or {}
        if (str(payload.get("from_candidate_sha") or "")
                != str(from_sha or "")):
            continue
        if str(payload.get("to_candidate_sha") or "") != str(to_sha or ""):
            continue
        if str(payload.get("candidate_frozen_event_id") or "") != str(
                episode_id or ""):
            continue
        verifier = str(payload.get("verifier") or "")
        if verifier:
            seen.add(verifier)
    return set(expected).issubset(seen)


def _scheduler_resolve_candidate_and_plan(workflow_id, workflow_cfg):
    """Freeze the current candidate and, on a real rotation, plan re-verification.

    Returns ``(frozen_sha, deferred)``. ``deferred`` is True only when a freeze
    was actually attempted and failed to produce an identity, which is the one
    case the caller must not latch on. Returns the frozen SHA, or "" when
    unprovable. Runs once per sweep, before readiness is computed, so that a
    rotation and the decision it implies are both visible to the same pass.

    The freeze is idempotent against the newest freeze (PR #107), so calling it
    on every sweep costs one ledger read when nothing changed. When the newest
    freeze differs from the one before it, that is a genuine A -> B rotation and
    the only place a reverification episode may begin. The plan is written
    before anything is latched or dispatched, so a crash replays from the facts
    instead of re-deriving a different decision.

    A workflow with a real project but no resolvable identity returns "" so the
    caller can defer rather than latch: verifiers would otherwise be refused at
    preflight or run against whatever happens to be checked out. A workflow with
    no project_root has no repository to resolve a candidate from at all, which
    is not a transient gap, so it keeps the legacy behaviour.
    """
    if scheduler_core is None or scheduler_facts_store is None:
        return "", False
    if not is_node_complete(workflow_id, "implementation"):
        # Not our business yet: the candidate is frozen when implementation
        # completes, not before. Reporting a deferral here would stall a
        # workflow that has not reached the point where a candidate exists.
        return "", False
    project_ctx = project_for_workflow(workflow_id) or {}
    project_root = project_ctx.get("project_root") or ""
    if not project_root:
        # No repository to resolve a candidate from at all: not a transient
        # identity gap, so keep the legacy behaviour rather than deadlocking.
        return "", False
    frozen_sha = _scheduler_freeze_candidate(
        workflow_id, project_root, "implementation", ["implementation"])
    if not frozen_sha:
        return "", True
    rotated_from = _scheduler_previous_candidate_sha(workflow_id)
    if rotated_from and rotated_from != frozen_sha:
        # The episode is the freeze event just recorded (or reused, when the
        # SHA did not change). Facts are bound to it, not to the SHA, so a
        # rollback onto an already-frozen SHA starts a new episode and cannot
        # inherit the previous round's reuse.
        try:
            freezes = scheduler_facts_store.list_candidate_frozen_events(
                workflow_id)
            episode_id = str(freezes[-1].get("id") or "") if freezes else ""
        except Exception:
            episode_id = ""
        if episode_id and not _reverification_episode_settled(
                workflow_id, rotated_from, frozen_sha, episode_id):
            _reverification_plan_for_rotation(
                workflow_id, workflow_cfg, project_root, rotated_from,
                frozen_sha, episode_id)
    return frozen_sha, False


def _reverification_source_verifications(workflow_id, from_sha):
    """Candidate ``from_sha`` 上可作为复用来源的既有验证(供举证)。

    三条硬性过滤,任一不满足即不作为来源:

    1. 必须带 ``verified_candidate_sha``(claim / baseline 都不是完成证据,§14);
    2. 必须**精确绑定** ``from_sha``——不是「在附近某个版本跑过」;
    3. 必须是真实跑出来的任务(reuse 不创建 Task,因此任何 Task 都是 fresh)。

    第 1 条刻意**不**走 ``scheduler.extract_task_verified_sha``:该函数为兼容
    pre-Scheduler workflow,会对没有 candidate claim 的任务回退到
    ``baseline_commit``。复用来源的要求比那个回退严格得多——它断言的是
    「这个 verifier 真的验证了 A」,而 launch baseline 只能说明「它从 A 出发」,
    正是 #107 引入完成证据要消灭的那个说法(中途 pull/rebase 的 Agent 仍会
    报告 baseline=A,却验证了别的树)。所以这里读字面字段。

    刻意**不**排除 superseded 任务:fix-loop 返工时会把上一轮的 test 任务
    作废,但「作废」只表示它不再是当前结论,不表示抹掉它验证过 A 这一事实。
    排除掉它们会让所有真实返工都无法复用,等于功能永远不生效。
    """
    if scheduler_core is None:
        return []
    wanted = str(from_sha or "").strip()
    if not wanted:
        return []
    sources = []
    for task in load_tasks():
        if not isinstance(task, dict):
            continue
        if task.get("workflow_id") != workflow_id:
            continue
        # A verdict is only meaningful on a task that actually reached one. A
        # task still running cannot have proved anything, and a stale
        # stage_verdict left on a non-terminal record must not be read as a
        # PASS. Superseded counts: fix-loop supersedes the previous round's
        # verifier, and excluding those would make reuse unreachable on every
        # real rework.
        if str(task.get("status") or "") not in _SETTLED_VERIFICATION_STATUSES:
            continue
        node_id = str(task.get("node") or task.get("stage") or "")
        if not node_id:
            continue
        verified = str(task.get("verified_candidate_sha") or "").strip()
        if not verified:
            continue
        if not scheduler_core.shas_identical(verified, wanted):
            continue
        try:
            updated = float(task.get("updated_at") or 0)
        except (TypeError, ValueError):
            updated = 0.0
        sources.append({
            "verifier": node_id,
            "task_id": str(task.get("task_id") or ""),
            "verdict": str(task.get("stage_verdict") or "").strip().lower(),
            "candidate_sha": scheduler_core.extract_task_candidate_claim(task),
            "verified_candidate_sha": verified,
            "source": reverification_mod.SOURCE_FRESH,
            "updated_at": updated,
        })
    return sources


def _reverification_plan_for_rotation(workflow_id, workflow_cfg, project_root,
                                      from_sha, to_sha, episode_id):
    """候选轮换时构建并落盘一次重新验证计划(best-effort,失败即全部 RERUN)。

    返回 ``(plan, reused_node_ids)``。任何异常都退化为「没有复用」,即所有
    verifier 照常重跑:漏跑一个 verifier 远好过跳过一个本该跑的 verifier。
    """
    empty = ({}, set())
    if (
        reverification_mod is None
        or scheduler_facts_store is None
        or scheduler_core is None
    ):
        return empty
    if not project_root or not from_sha or not to_sha or from_sha == to_sha:
        return empty
    if not episode_id:
        # Without the freeze that authorises it, a decision could not be bound to
        # a round, so nothing may be recorded as covered.
        print(f"[REVERIFICATION PLAN SKIPPED] workflow={workflow_id} "
              "no candidate freeze episode; treating as full re-verification")
        return empty
    try:
        policy = reverification_mod.policy_from_workflow(workflow_cfg)
        entries, diff_reason = reverification_mod.collect_candidate_changes(
            project_root, from_sha, to_sha)
        sources = _reverification_source_verifications(workflow_id, from_sha)
        plan = reverification_mod.build_reverification_plan(
            workflow_id, from_sha, to_sha, entries, sources,
            policy, diff_reason=diff_reason,
            verifiers=scheduler_core.verifier_branch_node_ids(workflow_cfg),
            episode_id=episode_id,
        )
    except Exception as exc:
        print(f"[REVERIFICATION PLAN WARN] workflow={workflow_id}: {exc}")
        return empty

    reused = set()
    for decision in plan["verifiers"].values():
        node_id = str(decision.get("verifier") or "")
        try:
            recorded = scheduler_facts_store.record_reverification_decision(
                workflow_id, decision)
        except Exception as exc:
            print(f"[REVERIFICATION WRITE WARN] workflow={workflow_id} "
                  f"node={node_id}: {exc}")
            continue
        if decision.get("decision") == reverification_mod.DECISION_REUSE:
            if recorded.get("status") == "rejected":
                print(
                    f"[REVERIFICATION REJECTED] workflow={workflow_id} "
                    f"node={node_id} reason="
                    f"{recorded.get('reason')}"
                )
                continue
            reused.add(node_id)
        print(
            f"[REVERIFICATION {str(decision.get('decision') or '').upper()}] "
            f"workflow={workflow_id} node={node_id} "
            f"{from_sha[:8]} -> {to_sha[:8]} "
            f"reason={decision.get('reason')} "
            f"changed={','.join(decision.get('changed_paths') or []) or '-'}"
        )
    return plan, reused


def _reverification_reused_node(workflow_id, node_id):
    """该节点本次是否应由 reuse 事实满足、从而**不创建 Task**。

    复用是调度决策,不是 Agent 决策(§19):控制器不会先创建 test(B) Task
    再让 Agent 自己决定不跑。
    """
    if not node_id:
        return False
    return _reverification_satisfies_node(workflow_id, node_id)


def _scheduler_join_gate_allows(workflow_id, node, tasks):
    """汇聚门禁放行判定:非 join 节点一律放行(保持原语义)。

    join 节点 = node_type 为 gate 且 depends_on >= 2 的节点。
    判定失败/异常一律拒绝(Fail-Closed)。
    """
    node = node or {}
    try:
        from herdr.recovery_store import read_snapshot
        from herdr.business_gate import business_gate_blockers
        store = _get_store()
        if store.get_workflow(workflow_id):
            authority, config, owned_tasks = read_snapshot(store.db_path, workflow_id)
            missing = business_gate_blockers(store, authority, config, owned_tasks, node.get('id'))
            if missing:
                print(f'[BUSINESS GATE WAIT] workflow={workflow_id} node={node.get("id")} tasks={missing}')
                return False
        elif any(t.get('completion_protocol') == 'receipt-v1' for t in tasks):
            return False
    except (OSError, ValueError) as exc:
        print(f'[BUSINESS GATE WAIT] workflow={workflow_id}: {type(exc).__name__}')
        return False
    deps = list(node.get("depends_on") or [])
    node_type = str(node.get("node_type") or "")
    # Join-before-dispatch applies to two shapes:
    # 1. explicit join nodes (node_type=gate with >=2 dependencies);
    # 2. scheduler-engaged fan-in agent nodes with >=2 dependencies
    #    (e.g. wrapup on [test, review]): once the scheduler froze a
    #    candidate for a workflow, every multi-dependency dispatch in that
    #    workflow must prove same-revision verification first.
    # Workflows the scheduler never engaged keep legacy passthrough.
    is_join_shape = (node_type == "gate" and len(deps) >= 2)
    engaged = False
    if not is_join_shape and len(deps) >= 2:
        if scheduler_facts_store is None:
            # Scheduler unavailable, so no candidate identity can be enforced
            # on a fan-in node. Refuse rather than wave the node through.
            print(
                f"[JOIN GATE REFUSED] workflow={workflow_id} "
                f"gate={node.get('id')} reason=scheduler_unavailable"
            )
            return False
        try:
            engaged = bool(
                scheduler_facts_store.latest_frozen_candidate_sha(workflow_id)
            )
        except Exception as exc:
            # "lookup failed" is not "never frozen". Treating the error as
            # un-engaged would classify a scheduler-managed workflow as legacy
            # and let a fan-in node (e.g. wrapup on [test, review]) bypass the
            # gate exactly when the identity of the candidate is unknown.
            # Fail closed (AGENTS.md §4.4); the next sweep retries.
            print(
                f"[JOIN GATE REFUSED] workflow={workflow_id} "
                f"gate={node.get('id')} reason=frozen_lookup_failed: {exc}"
            )
            return False
    if not (is_join_shape or engaged):
        return True
    if scheduler_core is None:
        return False
    expected = ""
    if scheduler_facts_store is not None:
        try:
            expected = scheduler_facts_store.latest_frozen_candidate_sha(workflow_id)
        except Exception as exc:
            # Cannot know which candidate must be matched: refuse. Returning a
            # blank expectation would let the gate compare against nothing.
            print(
                f"[JOIN GATE REFUSED] workflow={workflow_id} "
                f"gate={node.get('id')} reason=frozen_lookup_failed: {exc}"
            )
            return False
    try:
        passed, reason, details = scheduler_core.evaluate_join_gate(
            node, tasks, workflow_id, expected,
            reuse_facts=_reverification_gate_facts(workflow_id),
        )
    except Exception as exc:
        print(
            f"[JOIN GATE ERROR] workflow={workflow_id} "
            f"gate={node.get('id')}: {exc}"
        )
        return False
    if scheduler_facts_store is not None:
        try:
            scheduler_facts_store.record_join_gate_verdict(
                workflow_id, str(node.get("id") or ""), passed, reason, details
            )
        except Exception as exc:
            print(f"[JOIN GATE AUDIT WARN] workflow={workflow_id}: {exc}")
    if not passed:
        print(
            f"[JOIN GATE REFUSED] workflow={workflow_id} "
            f"gate={node.get('id')} reason={reason}"
        )
    return passed


def try_direct_stage_advance(item):
    """常规推进会:按节点模板规则化直接派发,失败回落总指挥。

    返回 True 表示事件已被处理(含 wait),False 表示必须走总指挥原路径。
    """
    if direct_dispatch_planner is None or not direct_stage_dispatch_enabled():
        return False

    node = item.get("node")
    workflow_id = item.get("workflow_id")
    ready_id = item.get("node_id") or item.get("next_stage")

    if not workflow_id or not ready_id:
        return False

    project_ctx = project_for_workflow(workflow_id) or {}
    if project_ctx.get("startup_ready") is False:
        return False

    project_root = project_ctx.get("project_root") or ""
    if not project_root or not project_ctx.get("coordinator_pane_id"):
        return False

    requirement = (project_ctx.get("requirement") or "").strip()

    # 历史 workflow.json 的节点字段可能为空(旧模板快照),
    # 与总指挥路径一致回退 stage-policies 后再做规则化决策。
    node = direct_dispatch_planner.merge_node_policy(
        node if node is not None else {"id": ready_id},
        get_stage_policy(ready_id),
    )
    if not node.get("id"):
        node["id"] = ready_id

    # 上游存在 blocked 验收结论时不得自动派发(与 sweep 对称),
    # 回退总指挥裁决。依赖未知时保持原行为(fail-open)。
    dep_ids = (item.get("node") or {}).get("depends_on")
    if dep_ids is None:
        try:
            wf_cfg = workflow_config_for(workflow_id) or {}
            found = find_node(wf_cfg, ready_id) if wf_cfg.get("nodes") else None
            dep_ids = (found or {}).get("depends_on") or []
        except ValueError:
            dep_ids = []
    verdict_dep = blocked_verdict_dep(workflow_id, {"depends_on": dep_ids})
    if verdict_dep:
        print(
            f"[DIRECT DISPATCH BLOCKED] "
            f"workflow={workflow_id} "
            f"node={ready_id} "
            f"blocked_dep={verdict_dep} "
            "-> fallback to coordinator for adjudication"
        )
        return False

    # Scheduler v1 汇聚门禁:join 判定先于一切闩与派发。
    # - 非 join 形状:直接放行,走原语义(零行为变化);
    # - join 未满足:返回 True(事件已处理)但不写任何闩,下轮 sweep 重估;
    # - 判定异常:Fail-Closed,回落总指挥(原 fallback 语义)。
    try:
        join_node = node if isinstance(node, dict) else {"id": ready_id}
        if not _scheduler_join_gate_allows(workflow_id, join_node, load_tasks()):
            return True
    except Exception as exc:
        print(
            f"[JOIN GATE ERROR] workflow={workflow_id} "
            f"node={ready_id}: {exc}"
        )
        return False

    gate_task = node_is_gate(workflow_id, ready_id)

    docs_block = shared_docs_block(
        workflow_id,
        ready_id,
        related_nodes=dep_ids,
        project_ctx=project_ctx,
    )

    # 门禁节点在派发时注入结论契约(状态目录 gate-verdicts/<task_id>.json +
    # 终端标记),由 try_auto_verdict 直接采纳,免除总指挥裁决回合。
    #
    # delivered_in_base=True:已 git 交付(已 integrate 进 base)的依赖,其交付物
    # 已在 base 上,onto 不应再指向本地任务分支 —— `herdr-task launch` 要求
    # `--onto` 存在于 refs/remotes/origin/,而任务分支通常从不推送。实测事故
    # (wf-project-0929-01):test 节点反复 "Onto branch not found on origin:
    # agent/opencode/feat-impl-t7-integration-gates-r2",任务在 pending 阶段
    # 即被判 router_isolation_rejected,从未真正执行。详见 lessons §110。
    candidate_branch = direct_dispatch_planner.candidate_branch_for_node(
        load_tasks(), workflow_id, ready_id, dep_ids, delivered_in_base=True
    )
    candidate_sha = _scheduler_expected_candidate_sha(
        workflow_id, project_root, dep_ids, candidate_branch
    )
    # P1-3: selective replacement 基线必须来自当前冻结 Candidate,
    # 禁止从普通 Task branch 猜。存在 selective 事实时 fail-closed:
    # 冻结 sha/branch 不可证明,或任一待补派谱系所属事实与当前冻结
    # 不 identical(轮换后旧事实),一律回落总指挥,不派发。
    try:
        _sel_facts = _all_selective_facts(workflow_id, ready_id)
    except Exception:
        _sel_facts = []
    if _sel_facts and selective_replan_core is not None:
        _frozen_sha = _scheduler_current_frozen_candidate_sha(workflow_id)
        _frozen_branch = _scheduler_current_frozen_branch(workflow_id)
        if not _frozen_sha or not _frozen_branch:
            print(
                f"[SELECTIVE REPLAN BASELINE REFUSED] workflow={workflow_id} "
                f"node={ready_id} reason=frozen_unproven -> fallback to coordinator"
            )
            return False
        try:
            from herdr import direct_dispatch as _dd
            _node_tasks = [
                t for t in (load_tasks() or [])
                if isinstance(t, dict)
                and str(t.get("workflow_id") or "") == workflow_id
                and str(t.get("node") or t.get("stage") or "") == ready_id
            ]
            _pending_roots = {
                _dd.lineage_key(t.get("task_id"))[0]
                for t in _dd.lineage_redispatch_candidates(_node_tasks)
            }
        except Exception:
            _pending_roots = set()
        _need_baseline = False
        for _fact in _sel_facts:
            _roots = {str(r).strip() for r in (_fact.get("target_lineage_roots") or []) if str(r).strip()}
            if _pending_roots and not (_roots & _pending_roots):
                continue
            _need_baseline = True
            _base = selective_replan_core.selective_replacement_baseline(
                _fact, _frozen_sha, _frozen_branch)
            if _base is None:
                print(
                    f"[SELECTIVE REPLAN BASELINE REFUSED] workflow={workflow_id} "
                    f"node={ready_id} reason=baseline_unproven "
                    f"fact={str(_fact.get('replan_id') or '')[:16]} -> fallback to coordinator"
                )
                return False
        if _need_baseline:
            candidate_branch = _frozen_branch
            candidate_sha = _frozen_sha
            print(
                f"[SELECTIVE REPLAN BASELINE] workflow={workflow_id} "
                f"node={ready_id} onto={_frozen_branch} "
                f"candidate={str(_frozen_sha)[:12]}"
            )
    # PR #110:门禁派发携带可归因 Task 清单;retry_node 补派携带
    # blocker 上下文。两者均只在策略显式开启时非 None。
    gate_inventory_block = None
    if gate_task:
        try:
            gate_inventory_block = _selective_gate_inventory_block(
                workflow_id, workflow_config_for(workflow_id) or {}
            )
        except Exception as exc:
            print(
                f"[SELECTIVE REPLAN WARN] workflow={workflow_id} "
                f"inventory injection skipped: {exc}"
            )
    plan = direct_dispatch_planner.plan_stage_dispatch(
        workflow_id,
        node,
        load_tasks(),
        requirement,
        context_branch=candidate_branch,
        gate_contract=gate_task,
        docs_block=docs_block,
        candidate_sha=candidate_sha,
        gate_inventory_block=gate_inventory_block,
        redispatch_blocker_notes=_selective_redispatch_blocker_notes(
            workflow_id, ready_id
        ),
    )

    if gate_task and plan.get("mode") == "dispatch":
        # 状态目录必须先于 Agent 写入存在(结论文件落 clone 外,交付零污染)。
        try:
            direct_dispatch_planner.gate_verdict_dir().mkdir(
                parents=True, exist_ok=True
            )
        except Exception as exc:
            print(f"[GATE VERDICT DIR WARN] {exc}")

    mode = plan.get("mode")

    # PR #108: a reused verifier must not be dispatched at all.
    #
    # This is defence in depth, not the primary mechanism. In the normal path
    # the reuse fact already makes the node complete, so readiness filtering
    # means a reused node never reaches here and the ledger and the scheduler
    # cannot disagree. It stays because the two are computed from different
    # reads at different times, and if they ever did disagree the safe
    # direction is unambiguous: re-running a verifier wastes work, while
    # dispatching a verifier that an immutable fact already covers is the
    # dangerous one.
    if _reverification_reused_node(workflow_id, ready_id):
        mark_stage_advance_notified(workflow_id, ready_id)
        print(
            f"[REVERIFICATION REUSE] workflow={workflow_id} "
            f"node={ready_id} no task created; "
            f"verification carried by a reuse fact bound to the current candidate"
        )
        return True

    if mode == "fallback":
        print(
            f"[DIRECT DISPATCH FALLBACK] "
            f"workflow={workflow_id} "
            f"node={ready_id} "
            f"reason={plan.get('reason')}"
        )
        return False

    if mode == "wait":
        # 节点已有活跃任务:推进会自然由状态机完成,不再唤醒总指挥。
        mark_stage_advance_notified(workflow_id, ready_id)
        print(
            f"[DIRECT DISPATCH WAIT] "
            f"workflow={workflow_id} "
            f"node={ready_id} "
            f"reason={plan.get('reason')}"
        )
        return True

    specs = plan.get("specs") or []
    if not specs:
        return False

    if not _dispatch_candidate_ready(
        project_root, project_ctx.get("base_branch"), specs, workflow_id=workflow_id
    ):
        return False

    launched = []

    for spec in specs:
        cmd = [
            TASK_MANAGER,
            "launch",
            "--task-id", spec["task_id"],
            "--workflow-id", workflow_id,
            "--node", ready_id,
            "--source", project_root,
            "--agent", "auto",
            "--task-type", spec["task_type"],
            "--integration-mode", spec["integration_mode"],
            "--dispatch-role", spec.get("dispatch_role") or "worker",
            "--dispatch-round", str(spec.get("dispatch_round") or 1),
            "--goal", spec["goal"],
            "--prompt", spec["prompt"],
            # Workflow execution identity: sibling launches keep distinct
            # run_ids but share one execution_id for the same execution.
            "--execution-id", workflow_id,
        ]

        if spec.get("artifact_mode"):
            cmd += ["--artifact-mode", spec["artifact_mode"]]

        if spec.get("onto_branch"):
            cmd += ["--onto", spec["onto_branch"]]

        # 补派必须把谱系前驱连起来：direct_dispatch 已经算出 redispatch_of，
        # 但历史上 launch argv 没带 --supersedes，导致旧任务的 superseded_by
        # 永远为空。required_task_ids 的链式解析（scheduler.node_is_complete）
        # 因此断在第一跳，实现节点永远判不出完成，日志只剩
        # [STAGE ADVANCE WAIT] coordinator=working。
        _redispatch_of = str(spec.get("redispatch_of") or spec.get("supersedes") or "").strip()
        if _redispatch_of:
            cmd += ["--supersedes", _redispatch_of,
                    "--supersede-reason", "controller redispatch after explicit invalidation"]

        if spec.get("candidate_sha"):
            cmd += ["--candidate-sha", spec["candidate_sha"]]

        for line in spec["acceptance"]:
            cmd += ["--acceptance", line]

        try:
            result = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=DIRECT_DISPATCH_LAUNCH_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            print(
                f"[DIRECT DISPATCH TIMEOUT] "
                f"task={spec['task_id']} "
                f"after={DIRECT_DISPATCH_LAUNCH_TIMEOUT}s"
            )
            result = None

        if result is None or result.returncode != 0:
            output = ""
            if result is not None:
                output = result.stderr.strip() or result.stdout.strip()
                print(
                    f"[DIRECT DISPATCH ERROR] "
                    f"task={spec['task_id']}: {output}"
                )
            failed_task = get_task(spec["task_id"])
            if failed_task and failed_task.get("status") == "failed" and (
                failed_task.get("failure_reason") == "router_isolation_rejected"
                or "ROUTER REJECTED" in output
                or "ROUTER OPT-OUT AUDIT FAILED" in output
            ):
                key = f"{spec['task_id']}:dispatch_failure"
                if not attention_get(key):
                    attention_note(
                        key,
                        failed_task,
                        "dispatch_failure",
                        reason="router_isolation_rejected",
                        attempts=1,
                        detail=output[:500],
                    )
                _record_blocked_event(
                    failed_task,
                    "dispatch_lifecycle_failed",
                    {
                        "task_id": spec["task_id"],
                        "actionable": True,
                        "pane_dispatched": False,
                        "error": output[:500],
                    },
                )
                mark_stage_advance_notified(workflow_id, ready_id)
                return True
            if launched:
                print(
                    f"[DIRECT DISPATCH PARTIAL] "
                    f"launched={','.join(launched)} "
                    "-> fallback to coordinator for reconciliation"
                )
            return False

        launched.append(spec["task_id"])

    mark_stage_advance_notified(workflow_id, ready_id)

    print(
        f"[STAGE ADVANCED DIRECT] "
        f"workflow={workflow_id} "
        f"node={ready_id} "
        f"tasks={','.join(launched)}"
    )

    # Collaboration accelerator: deterministic edges emit one HANDOFF each.
    # Best-effort only: the launched tasks already carry their own prompts,
    # so a handoff failure must never roll back the stage advance.
    try:
        for handoff in maybe_dispatch_node_handoffs(
            workflow_id=workflow_id, ready_id=ready_id,
            dep_ids=dep_ids, launched=launched,
        ):
            print(f"[COLLABORATION HANDOFF] {handoff}")
    except Exception as exc:
        print(
            f"[COLLABORATION HANDOFF SKIPPED] "
            f"workflow={workflow_id} node={ready_id}: {type(exc).__name__}"
        )

    maybe_compact_coordinator(workflow_id, reason=f"stage_advance:{ready_id}")

    return True


def execute_workflow_recovery(operation, owner):
    """Run existing Task primitives only after the durable recovery claim."""
    from herdr import recovery_store
    from herdr.workflow_recovery import successor_launch_command, repair_coverage
    from herdr.recovery_successor import has_confirmed_delivery, has_confirmed_rework, choose_recovery_source
    from herdr.repo_hygiene import check_source_cleanliness
    store = _get_store()
    wid, payload = operation['workflow_id'], operation['payload']
    workflow, config, snapshot_tasks = recovery_store.read_snapshot(store.db_path, wid)
    source = workflow.get('project_root')
    if not source:
        return 'waiting_human', {'reason': 'project_identity_unknown'}
    from herdr.workflow_recovery import existing_delivery_details
    bound = existing_delivery_details(operation, workflow, snapshot_tasks, store)
    if bound:
        operation = dict(operation, detail={**operation['detail'], **bound})
    verifying = (operation['detail'].get('action') == 'verify'
                 or bool(operation['detail'].get('successor_ids') or operation['detail'].get('rework_ids')))
    if not workflow.get('execution_id') or not config.get('nodes'):
        return 'waiting_human', {'reason': 'workflow_identity_or_config_unknown'}
    if not verifying:
        clean, paths = check_source_cleanliness(source)
        if not clean:
            return 'waiting_human', {'reason': 'source_wip_requires_decision', 'dirty_paths': paths[:50]}
    target_ids = payload.get('task_ids') if payload['kind'] == 'finalize' else payload.get('affected_task_ids')
    by_id = {t['task_id']: t for t in snapshot_tasks}
    targets = [by_id.get(tid) for tid in target_ids or []]
    if not targets or any(t is None for t in targets):
        return 'waiting_human', {'reason': 'recovery_target_unknown'}
    details = {'execution_id': workflow.get('execution_id'), 'successor_ids': [], 'rework_ids': [], 'target_runs': {}, 'source_runs': {}, 'repair_map': {}, 'rework_requests': {},
               'gate_nodes': sorted({n['id'] for n in config.get('nodes') or []
                   if n['id'] != 'wrapup' and (resolve_gate_config(n, n['id']) or {}).get('retry_node') == payload.get('retry_node')})}
    if verifying:
        details.update(operation['detail'])
    details['gate_nodes'] = [gate for gate in details['gate_nodes'] if gate != 'wrapup']
    def step(name, validate=True):
        if validate:
            recovery_store.renew_owner(store.db_path, operation['id'], owner, time.time(), lease_seconds=180)
        recovery_store.record_step(store.db_path, operation['id'], owner, name, details, time.time(), validate=validate)
    if verifying:
        from herdr.recovery_successor import link_committed_successor
        confirmed = details.get('successor_ids') or []
        reworked = details.get('rework_ids') or []
        if not confirmed and not reworked:
            return 'waiting_human', {'reason': 'delivery_inventory_missing'}
        for successor_id in confirmed:
            successor = store.get_task(successor_id)
            if not successor or not has_confirmed_delivery(store, successor):
                return 'waiting_human', {'reason': 'delivery_unknown'}
            details.setdefault('target_runs', {}).setdefault(successor_id, successor['run_id'])
            predecessor = store.get_task(successor.get('recovery_predecessor') or successor.get('supersedes'))
            if not predecessor:
                return 'waiting_human', {'reason': 'recovery_predecessor_unknown'}
            if not predecessor.get('superseded_by'):
                step('existing_delivery_linking')
                link_committed_successor(store, predecessor['task_id'], successor_id,
                    successor.get('expected_predecessor_version'), payload['candidate_sha'], 'verified recovery delivery')
            if (details.get('repair_map', {}).get(successor_id) or {}).get('kind') == 'delivered_successor':
                continue
            details.setdefault('repair_map', {})[predecessor['task_id']] = {
                'kind': 'successor', 'task_id': successor_id, 'run_id': details['target_runs'][successor_id],
                'source_run_id': details.get('source_runs', {}).get(predecessor['task_id'])}
        for task_id in reworked:
            task = store.get_task(task_id) or {}
            expected_request = details.get('rework_requests', {}).get(task_id)
            if payload['kind'] != 'finalize' and not has_confirmed_rework(store, task, expected_request):
                return 'waiting_human', {'reason': 'rework_delivery_unconfirmed'}
            details.setdefault('repair_map', {}).setdefault(task_id, {
                'kind': 'finalize' if payload['kind'] == 'finalize' else 'rework',
                'task_id': task_id, 'run_id': details.get('target_runs', {}).get(task_id),
                'source_run_id': details.get('source_runs', {}).get(task_id),
                'request_id': expected_request, 'completion_epoch':task.get('completion_epoch'),
                'completion_identity_path':task.get('completion_identity_path')})
        missing = repair_coverage(dict(operation, detail=details), store.list_tasks(workflow_id=wid))
        if missing:
            return 'waiting_human', {'reason': 'repair_coverage_incomplete', 'missing_task_ids': missing, **details}
        step('existing_delivery_verified', validate=False)
        if payload['kind'] == 'finalize':
            return 'awaiting_result', {'step': 'awaiting_delivery', **details}
    if payload['kind'] == 'finalize':
        # A failed candidate is never integrated as a prerequisite for repairing it.
        if any(t.get('stage_verdict') == 'blocked' for t in store.list_tasks(workflow_id=wid)
               if t.get('status') != 'superseded' and not t.get('superseded_by')):
            return 'waiting_human', {'reason': 'candidate_requires_repair_before_integration'}
        for task in targets:
            details['rework_ids'].append(task['task_id'])
            details['source_runs'][task['task_id']] = task.get('run_id')
            details['repair_map'][task['task_id']] = {'kind':'finalize', 'task_id':task['task_id'],
                'run_id':task.get('run_id'), 'source_run_id':task.get('run_id')}
            details['target_runs'][task['task_id']] = task.get('run_id')
            step('finalize_started')
            if not clear_finalize_escalation(task['task_id']):
                return 'waiting_human', {'reason': 'escalation_clear_failed', **details}
            finalize_completed_task(task['task_id'])
            fresh = store.get_task(task['task_id']) or {}
            if fresh.get('finalize_escalated'):
                return 'waiting_human', {'reason': fresh.get('finalize_escalate_reason') or 'finalize_refused', **details}
        return 'awaiting_result', {'step': 'awaiting_delivery', **details}
    for task in ([] if verifying else targets):
        tid = task['task_id']
        details['source_runs'][tid] = task.get('run_id')
        if task.get('status') == 'committed':
            if not workflow.get('execution_id'):
                return 'waiting_human', {'reason': 'workflow_execution_unknown'}
            next_id = next_recovery_task_id(tid, store.list_tasks(workflow_id=wid))
            details['successor_ids'].append(next_id)
            from herdr.supervisor.state import redact_text
            prompt = ('仅修复当前授权范围内的验收阻断，不扩大范围；若需要修改约定范围外文件，'
                      '持久报告 blocked 请求人工裁决。\n' + '\n'.join(
                redact_text(str(f.get('stage_verdict_note') or f.get('blocker') or ''))[:2000]
                for f in (payload.get('facts') or [])[:20]))
            try:
                recovery_source = choose_recovery_source(task, source)
            except ValueError as exc:
                return 'waiting_human', {'reason':str(exc), **details}
            details.setdefault('source_paths', {})[tid] = recovery_source
            command = successor_launch_command(TASK_MANAGER, workflow, task, next_id, recovery_source, prompt)
            step('successor_launch_started')
            result = subprocess.run(command, text=True, capture_output=True, timeout=120)
            successor = store.get_task(next_id)
            if result.returncode or not successor or not has_confirmed_delivery(store, successor):
                return 'waiting_human', {'reason': 'successor_delivery_unconfirmed', **details}
            if (store.get_task(tid) or {}).get('superseded_by') != next_id:
                return 'waiting_human', {'reason': 'successor_lineage_unconfirmed', **details}
            details['target_runs'][next_id] = successor['run_id']
            details['repair_map'][tid] = {'kind':'successor', 'task_id':next_id,
                'run_id':successor['run_id'], 'source_run_id':task.get('run_id')}
            step('successor_delivery_confirmed', validate=False)
        elif task.get('status') in REWORKABLE_STATUSES:
            details['rework_ids'].append(tid)
            details['target_runs'][tid] = task.get('run_id')
            request_id = 'recovery:' + uuid.uuid4().hex
            details['rework_requests'][tid] = request_id
            step('rework_started')
            ok, _ = _rework_retry_task(task, '+'.join(payload['gate_nodes']), request_id=request_id, failure_facts=payload.get('facts') or [])
            fresh = store.get_task(tid) or {}
            if not ok or not has_confirmed_rework(store, fresh, request_id):
                return 'waiting_human', {'reason': 'rework_delivery_unconfirmed', **details}
            details['repair_map'][tid] = {'kind':'rework', 'task_id':tid,
                'run_id':task.get('run_id'), 'source_run_id':task.get('run_id'),
                'request_id':request_id, 'completion_epoch':fresh.get('completion_epoch'),
                'completion_identity_path':fresh.get('completion_identity_path')}
            step('rework_delivery_confirmed', validate=False)
        else:
            return 'waiting_human', {'reason': 'target_requires_explicit_replacement', 'missing_task_ids':[tid], **details}
    missing = repair_coverage(dict(operation, detail=details), store.list_tasks(workflow_id=wid))
    if missing:
        return 'waiting_human', {'reason': 'repair_coverage_incomplete', 'missing_task_ids': missing, **details}
    # Existing invalidation receives no retry node: targets are already delivered
    # and must not be finalized/reworked a second time.
    if verifying and workflow.get('candidate_sha') != payload.get('candidate_sha'):
        return 'awaiting_result', {'step': 'awaiting_new_candidate', **details}
    step('gates_invalidating')
    for gate in payload['gate_nodes']:
        if gate in details.get('invalidated_gate_nodes', []):
            continue
        invalidate_for_fix_loop(wid, gate, config, retry_node=None)
        details.setdefault('invalidated_gate_nodes', []).append(gate)
        step('gate_invalidated', validate=False)
    remaining = [t for t in store.list_tasks(workflow_id=wid) if t.get('status') != 'superseded'
                 and not t.get('superseded_by') and t.get('stage_verdict') == 'blocked'
                 and (t.get('node') or t.get('stage')) in payload['gate_nodes']]
    if remaining:
        return 'waiting_human', {'reason': 'gate_invalidation_incomplete', **details}
    clear_stage_advance(wid, payload['retry_node'])
    return 'awaiting_result', {'step': 'awaiting_new_candidate', **details}


def next_recovery_task_id(task_id, tasks):
    from herdr.controller_actions import next_replacement_id
    occupied = {t.get('task_id') for t in tasks}
    next_id = next_replacement_id(task_id)
    while next_id in occupied:
        next_id = next_replacement_id(next_id)
    return next_id


def check_workflow_recovery(workflow_id):
    reconcile_node_dispatches(workflow_id, discover=False)
    from herdr.workflow_recovery import drive_recovery
    return drive_recovery(_get_store(), workflow_id, execute_workflow_recovery)


def reconcile_node_dispatches(workflow_id, *, discover=True):
    from herdr.node_dispatch_store import reconcile_workflow, operation_for_node
    state = load_stage_state()
    legacy = [key[len(workflow_id) + 1:] for key, value in state.items()
              if key.startswith(workflow_id + ':') and value == 'notified']
    enabled = coordinator_intake_enabled()
    if not enabled:
        record = _get_store().get_workflow(workflow_id) or {}
        for node in (record.get('config') or {}).get('nodes') or []:
            if node.get('depends_on'):
                continue
            op = operation_for_node(_get_store().db_path, workflow_id, node['id'])
            if op and not op['started'] and op['status'] in ('pending', 'running'):
                # Release the old latch before committing transfer; no direct queue is created yet.
                clear_stage_advance(workflow_id, node['id'])
    return reconcile_workflow(_get_store().db_path, workflow_id,
                              legacy_notified=legacy, discover=discover and enabled, intake_enabled=enabled)


def check_workflow_stage_advance(workflow_id):
    if not workflow_id:
        return

    # Closed workflows must never advance: a zero-task closed workflow is
    # vacuously "complete" at every stage and would ghost-advance forever.
    if workflow_closed(workflow_id):
        return

    # Discovery precedes Pane lookup and DAG readiness. Recovery execution runs
    # in the bounded background sweep; the database owns the obligation.
    from herdr import recovery_store
    from herdr.workflow_progress import assess_workflow
    record = _get_store().get_workflow(workflow_id)
    if record and record.get('status') == 'running':
        recovery_store.reconcile(_get_store().db_path, workflow_id)
        record, snapshot_config, snapshot_tasks = recovery_store.read_snapshot(_get_store().db_path, workflow_id)
        reconcile_stage_advance_states(workflow_id, snapshot_config)
        reconcile_node_dispatches(workflow_id)
        assessment = assess_workflow(record, snapshot_config, snapshot_tasks)
        if not assessment['can_advance']:
            return

    pane = coordinator_pane_for_workflow(workflow_id)
    if not pane:
        from herdr.node_dispatch_store import defer
        for op in recovery_store.list_operations(_get_store().db_path, workflow_id):
            if op['payload'].get('kind') == 'node_dispatch':
                defer(_get_store().db_path, op['id'], 'coordinator_missing')
        return

    workflow_cfg = workflow_config_for(workflow_id)
    if not workflow_cfg:
        return

    workflow_record = project_for_workflow(workflow_id) or {}
    # New factory starts persist the requirement and keep this gate closed
    # until Deep Preflight has completed. This prevents the controller from
    # delivering a stage event before the startup request is ready.
    if workflow_record.get("startup_ready") is False:
        print(f"[STARTUP WAIT] workflow={workflow_id} preflight/request not ready")
        return

    # 已关闭(含 abandoned)的 workflow 不再参与任何推进/回流判定;
    # 否则 abandoned 残留的 blocked verdict 会被 sweep 反复回流。
    wf_st = _workflow_entry(workflow_id).get("status")
    if wf_st in ("completed", "paused"):
        return

    if workflow_cfg.get("nodes"):
        # Revoke stale 'notified' locks before computing ready nodes,
        # so that regressed stages can be re-triggered.
        reconcile_stage_advance_states(workflow_id, workflow_cfg)

        # PR #108: resolve the candidate and its reverification plan BEFORE
        # completed_nodes is computed.
        #
        # Order is load-bearing. A reused verifier counts as complete only for
        # the candidate its fact is bound to, so a candidate rotation has to
        # happen first — otherwise a node satisfied by a now-stale A -> B fact
        # still looks complete in this sweep, no Task is created for C, and the
        # verifier would be skipped for C without ever having run. Running the
        # freeze first makes the rotation visible to the same sweep that acts
        # on it.
        deferred = False
        try:
            _frozen_sha, deferred = _scheduler_resolve_candidate_and_plan(
                workflow_id, workflow_cfg)
        except Exception as exc:
            print(f"[SCHEDULER SWEEP WARN] workflow={workflow_id}: {exc}")
            deferred = True
        # Reset AFTER the plan, not before: the plan writes the reuse facts, and
        # the episode check above reads the ledger first. Clearing afterwards
        # guarantees the memo is built from a ledger that already contains this
        # sweep's writes, so a fact recorded a moment ago is visible to the
        # readiness computation in the same pass.
        _reset_reverification_memo(workflow_id)

        completed_nodes = {
            n["id"]
            for n in workflow_cfg.get("nodes", [])
            if is_node_complete(workflow_id, n["id"])
        }

        from herdr.node_dispatch_store import operation_for_node
        for root in workflow_cfg.get('nodes', []):
            if root.get('depends_on'):
                continue
            dispatch = operation_for_node(_get_store().db_path, workflow_id, root['id'])
            if dispatch and dispatch['status'] not in ('resolved', 'superseded'):
                completed_nodes.discard(root['id'])

        # PR #110:选择性返工只作废 retry_node 内被点名的谱系,其余任务
        # 仍 completed-like——is_node_complete 会把节点误判为完成,
        # 替代任务(-rN)永远没有补派窗口。以持久化 selective fact 为准:
        # 目标谱系仍有补派候选时节点视为未完成,并自愈式清除阶段推进闩
        # (覆盖 invalidate 与 clear 之间的崩溃窗口)。未声明策略的流程
        # 整段跳过,legacy 行为零变化;任务表每轮最多加载一次。
        if selective_replan_core is not None and (
            selective_replan_core.policy_from_workflow(workflow_cfg)
        ):
            _await_tasks = None
            for _n in workflow_cfg.get("nodes", []):
                _nid = _n.get("id")
                if _await_tasks is None:
                    _await_tasks = load_tasks()
                if not _selective_replan_awaiting_redispatch(
                    workflow_id, _nid, _await_tasks
                ):
                    continue
                completed_nodes.discard(_nid)
                clear_stage_advance(workflow_id, _nid)
                print(
                    f"[SELECTIVE REPLAN AWAIT] workflow={workflow_id} "
                    f"node={_nid} target lineage pending redispatch"
                )

        if is_workflow_completed(workflow_cfg, completed_nodes):
            # reopen 闩在先:重开现场(sweep 每 2s 一次)不得刷
            # [WORKFLOW COMPLETE] 噪音,更不得触发 close。
            if _workflow_entry(workflow_id).get("suppress_auto_close"):
                return

            if _workflow_entry(workflow_id).get("status") == "completed":
                return

            # 交付终态门禁:任一门禁节点 verdict=blocked 时不得关闭,
            # 回流 fix-loop(由 handle_fix_loop 原子作废并派发事件)。
            nodes_by_id = {
                n["id"]: n for n in workflow_cfg.get("nodes", [])
            }
            blocked_gates = []

            for node_id in sorted(completed_nodes):
                gate_cfg = resolve_gate_config(
                    nodes_by_id.get(node_id), node_id
                )
                if (
                    gate_cfg
                    and gate_verdict(workflow_id, node_id) == "blocked"
                ):
                    blocked_gates.append((node_id, gate_cfg))

            if blocked_gates:
                for gate_node_id, gate_cfg in blocked_gates:
                    handle_fix_loop(
                        workflow_id, gate_node_id, gate_cfg, workflow_cfg
                    )
                return

            # 完成日志闩:close 可能失败或需要多轮,日志只允许出现一次,
            # 否则 sweep 会把 [WORKFLOW COMPLETE] 刷成日志风暴。
            if workflow_id not in _workflow_complete_logged:
                _workflow_complete_logged.add(workflow_id)
                print(
                    f"[WORKFLOW COMPLETE] "
                    f"workflow={workflow_id}"
                )

            maybe_close_completed_workflow(workflow_id)
            return

        ready_nodes = get_ready_nodes(workflow_cfg, completed_nodes)
        for ready_node in ready_nodes:
            latched_dep = next(
                (
                    dep
                    for dep in (ready_node.get("depends_on") or [])
                    if _fix_loop_latch_blocks(workflow_id, dep)
                ),
                None,
            )
            if latched_dep:
                latch_key = f"{workflow_id}:{latched_dep}"
                if latch_key not in _fix_latch_logged:
                    _fix_latch_logged.add(latch_key)
                    print(
                        f"[FIX LOOP LATCH] "
                        f"workflow={workflow_id} "
                        f"{ready_node['id']} waits for redo of {latched_dep}"
                    )
                continue
            blocked_dep = blocked_gate_dependency(
                workflow_id, ready_node, workflow_cfg
            )
            if blocked_dep:
                gate_node_id, gate_cfg = blocked_dep
                handle_fix_loop(
                    workflow_id, gate_node_id, gate_cfg, workflow_cfg
                )
                continue

            ready_id = ready_node["id"]
            verdict_dep = blocked_verdict_dep(workflow_id, ready_node)
            if verdict_dep:
                # 有门禁结论无门禁配置的依赖:不销毁下游,只暂停自动推进
                # 并 funnel 给总指挥裁决;作废过期结论后自动恢复。
                verdict_key = f"{workflow_id}:upstream_blocked:{ready_id}"
                if not attention_blocks_retry(verdict_key):
                    episode = attention_get(verdict_key) or {}
                    attempts = int(episode.get("attempts") or 0) + 1
                    attention_note(
                        verdict_key,
                        {"task_id": f"stage_advance:{ready_id}",
                         "workflow_id": workflow_id},
                        "stage_advance",
                        reason="upstream_blocked",
                        attempts=attempts,
                        next_retry_at=time.time() + liveness.attention_retry_interval(),
                        detail=f"blocked_dep={verdict_dep}",
                    )
                    notify_attention(
                        "Herdr Factory · 上游门禁阻断",
                        {"task_id": f"stage_advance:{ready_id}",
                         "workflow_id": workflow_id},
                        f"节点 {ready_id} 的上游 {verdict_dep} 存在 blocked 验收结论，"
                        "已暂停自动推进等待裁决（作废过期结论或返工后自动恢复）。",
                        "upstream_blocked",
                    )
                    print(
                        f"[STAGE ADVANCE BLOCKED] "
                        f"workflow={workflow_id} "
                        f"{ready_id} blocked_dep={verdict_dep} "
                        "-> awaiting adjudication"
                    )
                continue
            if attention_blocks_retry(f"{workflow_id}:stage_advance:{ready_id}"):
                continue

            deps = ready_node.get("depends_on", [])
            source_stage = deps[-1] if deps else "start"

            # Scheduler v1:join 判定先于 queued 闩。
            # join 未满足 -> continue(无闩),下轮 sweep 重估,修正证据后自动恢复。
            # implementation 完成 -> 冻结候选 SHA(+补 delivery note)供下游绑定。
            try:
                if not _scheduler_join_gate_allows(
                    workflow_id, ready_node, load_tasks()
                ):
                    continue
            except Exception as exc:
                print(
                    f"[SCHEDULER SWEEP WARN] workflow={workflow_id} "
                    f"node={ready_id}: {exc}"
                )
                continue

            # No candidate identity on a workflow that has a real project and a
            # completed implementation -> do not latch and do not dispatch this
            # node. Verifiers would either be refused at preflight (no identity
            # to prove) or run against whatever revision happens to be checked
            # out, and the stage latch would suppress the natural retry once the
            # branch/SHA becomes resolvable. Skipping the latch keeps the node
            # re-evaluated on the next sweep.
            #
            # Scoped to the ready-node loop. Verified against `3a84659`: the
            # freeze failure there also skipped every ready node in that pass,
            # so this is behaviour-preserving rather than an improvement — the
            # point of the change is only that it must not run *before*
            # `is_workflow_completed`, where it would stop a finished workflow
            # from ever closing.
            if deferred:
                print(
                    f"[SCHEDULER FREEZE DEFERRED] workflow={workflow_id} "
                    f"node={ready_id} no candidate identity resolvable; "
                    "stage not latched, dispatch deferred to next sweep"
                )
                continue

            dispatch_claim = None
            if not deps:
                from herdr.node_dispatch_store import operation_for_node, claim
                op = operation_for_node(_get_store().db_path, workflow_id, ready_id)
                if coordinator_intake_enabled() and not op and _get_store().get_workflow(workflow_id):
                    continue
                if op and not coordinator_intake_enabled() and op['status'] not in ('resolved', 'superseded'):
                    continue
                if op and coordinator_intake_enabled():
                    import uuid
                    dispatch_claim = claim(_get_store().db_path, op['id'], uuid.uuid4().hex)
                    if dispatch_claim is None:
                        continue
                    # SQLite reservation owns queued delivery; a lost JSON latch cannot suppress recovery.
                    clear_stage_advance(workflow_id, ready_id)

            if not mark_stage_advance_queued(workflow_id, ready_id):
                continue

            coordinator_queue.put(
                {
                    "kind": "stage_advance",
                    "workflow_id": workflow_id,
                    "stage": source_stage,
                    "node_id": ready_id,
                    "next_stage": ready_id,
                    "stage_label": ready_node.get("label", ready_id),
                    "node": ready_node,
                    "dispatch_operation_id": dispatch_claim['id'] if dispatch_claim else None,
                    "dispatch_owner": dispatch_claim['owner'] if dispatch_claim else None,
                }
            )

            print(
                f"[STAGE ADVANCE QUEUED] "
                f"workflow={workflow_id} "
                f"{source_stage} -> {ready_id}"
            )
        return

    # Fallback to legacy single-step stage advance
    for stage in workflow_cfg.get("stages", []):
        stage_key = stage.get("key") or stage.get("id")
        if not is_node_complete(workflow_id, stage_key):
            continue

        next_stage = stage.get("next")
        if not next_stage:
            continue

        gate_cfg = resolve_gate_config(None, stage_key)
        if gate_cfg and gate_verdict(workflow_id, stage_key) == "blocked":
            handle_fix_loop(workflow_id, stage_key, gate_cfg, workflow_cfg)
            continue

        if attention_blocks_retry(f"{workflow_id}:stage_advance:{next_stage}"):
            continue

        if _fix_loop_latch_blocks(workflow_id, stage_key):
            latch_key = f"{workflow_id}:{stage_key}"
            if latch_key not in _fix_latch_logged:
                _fix_latch_logged.add(latch_key)
                print(
                    f"[FIX LOOP LATCH] "
                    f"workflow={workflow_id} "
                    f"{next_stage} waits for redo of {stage_key}"
                )
            continue

        if not mark_stage_advance_queued(workflow_id, next_stage):
            continue

        coordinator_queue.put(
            {
                "kind": "stage_advance",
                "workflow_id": workflow_id,
                "stage": stage_key,
                "next_stage": next_stage,
                "stage_label": stage.get("label", stage_key),
            }
        )

        print(
            f"[STAGE ADVANCE QUEUED] "
            f"workflow={workflow_id} "
            f"{stage_key} -> {next_stage}"
        )


def active_registered_workflows():
    workflows = set()
    store = _get_store()
    for wf in store.list_workflows():
        wid = wf.get("workflow_id")
        if not wid or wf.get("status") == "completed":
            continue

        # 夹具/临时残留(pytest tmp、已删除的 workflow_file)绝不允许进入调度 sweep,
        # 否则会对不存在的 pane 无限重试并淹没日志。
        if liveness.workflow_is_foreign(wf):
            if wid not in _foreign_workflows_logged:
                _foreign_workflows_logged.add(wid)
                print(
                    f"[WORKFLOW FOREIGN SKIPPED] "
                    f"workflow={wid} "
                    f"file={wf.get('workflow_file')} "
                    f"(fixture/temp residue; excluded from scheduling)"
                )
            continue

        workflows.add(wid)
    return workflows


# fix-loop 作废闩去重日志:闩存续期间只提示一次,清除后下轮可再提示。
_fix_latch_logged = set()


def _fix_loop_latch_info(workflow_id, node_id):
    """Return (latch_ts, gate_node_id); accepts legacy float values."""
    with lock:
        state = load_stage_state()
        raw = state.get(f"{workflow_id}|fixloop|{node_id}|pending_redo")
    gate = None
    ts = 0.0
    if isinstance(raw, dict):
        gate = raw.get("gate")
        try:
            ts = float(raw.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
    else:
        try:
            ts = float(raw or 0)
        except (TypeError, ValueError):
            ts = 0.0
    return ts, gate


def _fix_loop_latch_gates(workflow_id, node_id):
    """闩关联的全部 gate id(P1-2 合并闩存 gates 列表,单门禁回退 gate)。"""
    with lock:
        state = load_stage_state()
        raw = state.get(f"{workflow_id}|fixloop|{node_id}|pending_redo")
    if not isinstance(raw, dict):
        return []
    gates = [str(g).strip() for g in (raw.get("gates") or []) if str(g).strip()]
    if gates:
        return gates
    gate = str(raw.get("gate") or "").strip()
    # 合并闩的 gate 形如 "test+review",拆回单门禁以便逐个清除升级记录。
    if "+" in gate:
        return [g.strip() for g in gate.split("+") if g.strip()]
    return [gate] if gate else []


def _fix_loop_latch_targets(workflow_id, node_id):
    """Selective replan (PR #110) 的目标谱系根;legacy 闩返回 []。"""
    with lock:
        state = load_stage_state()
        raw = state.get(f"{workflow_id}|fixloop|{node_id}|pending_redo")
    if not isinstance(raw, dict):
        return []
    return [
        str(r).strip()
        for r in (raw.get("target_lineage_roots") or [])
        if str(r).strip()
    ]


def _fix_loop_latch_blocks(workflow_id, node_id, tasks=None):
    """作废闩:被回流作废的节点在出现真正重做完成前挡住自动推进。

    有重做完成时顺带清除闩、计数与升级记录,下一轮阻断重新计数;
    指纹保留,后继任务重记录同一结论时按 repeat_verdict 升级而不是
    用新预算再开一轮。Selective 闩(PR #110)只有全部目标谱系重做
    完成才解除;被保留任务的任何更新都不会放行。
    """
    latch_ts, latch_gate = _fix_loop_latch_info(workflow_id, node_id)
    if not latch_ts:
        return False
    if tasks is None:
        tasks = load_tasks()
    from herdr import fix_loop as fix_loop_core

    if not fix_loop_core.latch_blocks_advance(
        tasks, node_id, latch_ts,
        target_lineage_roots=_fix_loop_latch_targets(workflow_id, node_id),
    ):
        _gates_to_clear = _fix_loop_latch_gates(workflow_id, node_id) or (
            [latch_gate] if latch_gate else [])
        with lock:
            state = load_stage_state()
            state.pop(
                f"{workflow_id}|fixloop|{node_id}|pending_redo", None
            )
            # |fp 故意保留:计数归零给 redo 新预算,但指纹必须留存,
            # 否则后继代任务重记录同一结论会逃过 repeat 检测,
            # fix-loop 对已终结 Task 反复下发并逐轮扣预算。
            state.pop(f"{workflow_id}|fixloop|{node_id}", None)
            save_stage_state(state)
        for _gate in _gates_to_clear:
            if _gate:
                attention_clear(f"{workflow_id}:fix_loop_exhausted:{_gate}")
        _fix_latch_logged.discard(f"{workflow_id}:{node_id}")
        return False
    return True


def redeliver_pending_fix_loop(workflow_id):
    """补投因总指挥忙而持久化的 fix-loop 通知(sweep 每轮调用,有界)。"""
    if workflow_closed(workflow_id):
        return False
    from herdr import fix_loop as fix_loop_core

    try:
        episodes = _attention_store.all()
    except Exception:
        return False
    now = time.time()
    redelivered = False
    for key, episode in episodes.items():
        if not key.startswith(f"{workflow_id}:fix_loop:"):
            continue
        if not isinstance(episode, dict):
            continue
        if (episode.get("event_type") or "") != "fix_loop":
            continue
        if (episode.get("reason") or "") != "coordinator_busy":
            continue
        try:
            summary = json.loads(episode.get("detail") or "{}")
        except (TypeError, ValueError):
            attention_clear(key)
            continue
        if not isinstance(summary, dict) or not summary.get("retry_node"):
            attention_clear(key)
            continue
        tasks = load_tasks()
        if fix_loop_core.redelivery_handled(
            tasks, summary["retry_node"], episode.get("first_seen_at"),
            target_lineage_roots=summary.get("target_lineage_roots"),
            rework_requests=summary.get("rework_requests"),
        ):
            attention_clear(key)
            print(
                f"[FIX LOOP REDELIVERY SKIP] "
                f"workflow={workflow_id} "
                f"retry={summary['retry_node']} "
                "coordinator already dispatched follow-up"
            )
            continue
        if not fix_loop_core.redelivery_due(episode, now):
            continue
        if coordinator_status(workflow_id) not in ("idle", "done"):
            continue
        redelivered_item = {
            "kind": "fix_loop",
            "workflow_id": workflow_id,
            "gate_stage": summary.get("gate_stage", ""),
            "retry_node": summary["retry_node"],
            "blockers": summary.get("blockers") or [],
            "invalidated": [],
            "loop_count": summary.get("loop_count", 0),
            "max_loops": summary.get("max_loops", FIX_LOOP_MAX),
            "suggested_branch": summary.get("suggested_branch"),
            "exhausted": bool(summary.get("exhausted")),
            "redelivered": True,
        }
        # PR #110:选择性返工通知在补投时保留 target 上下文。
        if summary.get("target_lineage_roots"):
            redelivered_item["mode"] = summary.get("mode") or "selective"
            redelivered_item["target_lineage_roots"] = summary[
                "target_lineage_roots"
            ]
        coordinator_queue.put(redelivered_item)
        attention_note(
            key,
            {"task_id": f"fix_loop:{summary.get('gate_stage')}",
             "workflow_id": workflow_id},
            "fix_loop",
            reason="coordinator_busy",
            attempts=int(episode.get("attempts") or 0),
            next_retry_at=now + liveness.attention_retry_interval(),
            detail=episode.get("detail"),
        )
        print(
            f"[FIX LOOP REDELIVERED] "
            f"workflow={workflow_id} "
            f"gate={summary.get('gate_stage')} "
            f"retry={summary['retry_node']}"
        )
        redelivered = True
    return redelivered


def check_workflow_continuation(workflow_id, now=None):
    """Persist unresolved obligations; notification delivery never resolves them."""
    from herdr.projects import inspect_continuation
    now = time.time() if now is None else now
    key = f"{workflow_id}:continuation"
    record = project_for_workflow(workflow_id) or {}
    pending = inspect_continuation(record, workflow_config_for(workflow_id) or {}, load_tasks())
    if pending is None and not attention_get(key):
        return
    queued = None
    notify = False
    interval = liveness.attention_retry_interval()
    with _attention_store.transaction() as episodes:
        if pending is None:
            episodes.pop(key, None)
            return
        episode = episodes.get(key) or {}
        if episode.get("fingerprint") != pending["fingerprint"]:
            episode = dict(pending, first_seen_at=now, attempts=0, claims=0,
                           next_retry_at=max(now, pending["last_progress_at"] + liveness.stage_advance_sla()))
        if now >= episode["next_retry_at"] and not episode.get("escalated"):
            if episode["attempts"] >= 2 or now - episode["first_seen_at"] >= 2 * interval:
                episode.update(escalated=True, notification_pending=True, notify_after=now)
            else:
                episode["claims"] += 1
                episode["next_retry_at"] = now + interval
                queued = {"kind": "workflow_continuation", "workflow_id": workflow_id,
                          "key": key, "fingerprint": pending["fingerprint"],
                          "claim": episode["claims"]}
        if episode.get("notification_pending") and now >= episode.get("notify_after", 0):
            episode["notify_after"] = now + interval
            notify = True
        episode.update(reason="workflow_continuation", event_type="workflow_continuation")
        episodes[key] = episode
    if queued:
        coordinator_queue.put(queued)
    if notify:
        delivered = notify_attention("Herdr Factory · 工作流等待人工推进", record,
                                     pending["message"], "workflow_continuation")
        with _attention_store.transaction() as episodes:
            current = episodes.get(key)
            if current and current.get("fingerprint") == pending["fingerprint"]:
                current["notification_pending"] = not delivered


def continuation_coordinator(record):
    """Resolve the canonical named coordinator; a cached Pane ID is not ownership."""
    from herdr.projects import coordinator_agent_name
    if not record.get("project_id"):
        return None
    name = coordinator_agent_name(record["project_id"])
    try:
        result = subprocess.run(["herdr", "agent", "get", name], text=True,
                                capture_output=True, timeout=5)
        if result.returncode != 0:
            return None
        agent = json.loads(result.stdout).get("result", {}).get("agent", {})
        if agent.get("name") == name and agent.get("agent_status") in ("idle", "done"):
            return name
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        pass
    return None


def handle_workflow_continuation(item):
    """Revalidate under the existing per-workflow executor before any prompt."""
    from herdr.projects import inspect_continuation
    wid = item["workflow_id"]
    record = project_for_workflow(wid) or {}
    pending = inspect_continuation(record, workflow_config_for(wid) or {}, load_tasks())
    if pending is None or pending["fingerprint"] != item["fingerprint"]:
        return
    coordinator = continuation_coordinator(record)
    if coordinator is None:
        return
    # Claim a send only after a live, owned coordinator is ready. A duplicate
    # queue item or a lost process cannot send the same claim twice.
    with _attention_store.transaction() as episodes:
        episode = episodes.get(item["key"]) or {}
        if (episode.get("fingerprint") != item["fingerprint"] or episode.get("escalated")
                or episode.get("claims") != item["claim"]
                or episode.get("last_sent_claim", 0) >= item["claim"] or episode.get("attempts", 0) >= 2):
            return
        episode["last_sent_claim"] = item["claim"]
        episode["attempts"] += 1
    message = ("HERDR_WORKFLOW_CONTINUATION_EVENT\n" +
               json.dumps(pending, ensure_ascii=False) +
               "\n请核对计划和持久集成引用，完成授权范围内的合流与缺失 Task 续派。"
               "先检查是否已有同 ID 或替代 Task，遵守串行屏障和候选版本门禁。"
               "不得 force-pass、删除计划义务、扩大范围或重复创建任务。"
               "若需人工裁决请落盘明确阻塞；通知收到不代表义务完成。\n" + COORDINATOR_DISCIPLINE)
    try:
        subprocess.run(["herdr", "agent", "prompt", coordinator, message, "--wait", "--timeout", "30000"],
                       text=True, capture_output=True, timeout=35)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[CONTINUATION DELIVERY ERROR] workflow={wid}: {type(exc).__name__}")


_CONTINUATION_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="continuation")
_continuation_scan_lock = threading.Lock()
_continuation_scan_at = 0.0


def schedule_workflow_continuations(workflow_ids, now):
    """One bounded background scan, without blocking the fast stage sweep."""
    if not _continuation_scan_lock.acquire(blocking=False):
        return
    def scan():
        try:
            for wid in workflow_ids:
                try:
                    check_workflow_recovery(wid)
                except Exception as exc:
                    print(f"[RECOVERY CHECK ERROR] workflow={wid}: {type(exc).__name__}")
                try:
                    check_workflow_continuation(wid, now=now)
                except Exception as exc:
                    print(f"[CONTINUATION CHECK ERROR] workflow={wid}: {type(exc).__name__}")
        finally:
            _continuation_scan_lock.release()
    try:
        _CONTINUATION_EXECUTOR.submit(scan)
    except RuntimeError:
        _continuation_scan_lock.release()


def check_all_workflows_stage_advance():
    global _continuation_scan_at
    now = time.time()
    workflow_ids = active_registered_workflows()
    if now - _continuation_scan_at >= 30:
        _continuation_scan_at = now
        schedule_workflow_continuations(workflow_ids, now)
    for wf in workflow_ids:
        try:
            redeliver_pending_fix_loop(wf)
        except Exception as e:
            print(f"[REDELIVER ERROR] workflow={wf}: {e}")
        try:
            check_workflow_stage_advance(wf)
        except Exception as e:
            print(f"[ADVANCE CHECK ERROR] workflow={wf}: {e}")



def coordinator_status(workflow_id=None):
    pane_id = coordinator_pane_for_workflow(
        workflow_id
    )

    if not pane_id:
        return "unknown"

    try:
        output = subprocess.check_output(
            [
                "herdr",
                "agent",
                "get",
                pane_id
            ],
            text=True
        )

        data = json.loads(output)

        return (
            data["result"]["agent"]
            .get("agent_status", "unknown")
        )

    except Exception as e:
        print(
            f"[COORDINATOR STATUS ERROR] "
            f"workflow={workflow_id} "
            f"pane={pane_id}: {e}"
        )
        return "unknown"


# ============================================================
# 总指挥上下文卫生 & 效率纪律
# ============================================================
#
# 背景(2026-09-17 复盘):wf-nexusarchive-0917-01 的总指挥会话上下文从
# 94K 一路涨到 684K,后期单回合纯 LLM 生成 27-58min;该工作流的控制面
# 事件全部串行等待这些长回合(累计 BUSY 2.35h)。阶段/fix-loop 边界压缩
# 上下文,可把后续回合耗时拉回分钟级;效率纪律约束"越权重活"与
# "决策后继续空转"。
COORDINATOR_COMPACT_AGENTS = {"opencode", "claude"}

COORDINATOR_DISCIPLINE = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
效率纪律(硬约束)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. 决策一旦落盘(herdr-task set ...),立即结束本回合;
   禁止在决策后继续探索、验证或执行任何命令。
2. 禁止运行重操作: herdr-task commit / integrate / cleanup、
   全量测试、npm/mvn 构建等;交付集成由 Controller 自动完成,
   你只做验收判定与必要修复指引。
3. 只读核验优先: verify-baseline / verify-metrics / 读取产物文件;
   禁止整段读取 Pane 全文。
4. 若你判断当前上下文已明显过大,先执行 /compact 再继续本事件。
""".strip()


def coordinator_compact_enabled():
    value = os.environ.get("HERDR_COORDINATOR_COMPACT", "1")
    return value.strip().lower() not in ("0", "false", "off", "no")


def coordinator_agent_kind(pane_id):
    """pane 内 agent 种类(herdr agent get -> result.agent.agent)。"""
    try:
        output = subprocess.check_output(
            ["herdr", "agent", "get", pane_id],
            text=True
        )
        agent = (json.loads(output).get("result") or {}).get("agent") or {}
        return agent.get("agent") or ""
    except Exception:
        return ""


def maybe_compact_coordinator(workflow_id, reason="stage_advance"):
    """阶段/fix-loop 边界给总指挥下发 /compact(有界等待,失败不阻断)。

    仅对支持 /compact 的 agent kind 生效;总指挥忙时跳过(下个边界再补)。
    这一调用把长工作流的上下文峰值压回可控区间,直接缩短后续回合耗时。
    """
    if not coordinator_compact_enabled():
        return False

    pane_id = coordinator_pane_for_workflow(workflow_id)
    if not pane_id:
        return False

    kind = coordinator_agent_kind(pane_id)
    if kind not in COORDINATOR_COMPACT_AGENTS:
        print(
            f"[COORDINATOR COMPACT SKIP] "
            f"workflow={workflow_id} pane={pane_id} "
            f"kind={kind or 'unknown'} reason={reason}"
        )
        return False

    if coordinator_status(workflow_id) not in ("idle", "done"):
        print(
            f"[COORDINATOR COMPACT SKIP] "
            f"workflow={workflow_id} pane={pane_id} "
            f"busy reason={reason}"
        )
        return False

    try:
        result = subprocess.run(
            [
                "herdr",
                "agent",
                "prompt",
                pane_id,
                "/compact",
                "--wait",
                "--timeout",
                "300000"
            ],
            text=True,
            capture_output=True
        )
    except Exception as exc:
        print(
            f"[COORDINATOR COMPACT ERROR] "
            f"workflow={workflow_id} pane={pane_id}: {exc}"
        )
        return False

    if result.returncode == 0:
        print(
            f"[COORDINATOR COMPACT] "
            f"workflow={workflow_id} pane={pane_id} "
            f"kind={kind} reason={reason}"
        )
        return True

    # 空会话/无可压缩内容时 TUI 本地即时完成,herdr 观测不到 working,
    # 返回 agent_prompt_stalled——这是良性情形,不是故障(2026-09-18 实测)。
    detail = (result.stderr.strip() or result.stdout.strip())
    if "agent_prompt_stalled" in detail:
        print(
            f"[COORDINATOR COMPACT SKIP] "
            f"workflow={workflow_id} pane={pane_id} "
            f"no_activity reason={reason}"
        )
        return False

    print(
        f"[COORDINATOR COMPACT ERROR] "
        f"workflow={workflow_id} pane={pane_id}: {detail}"
    )
    return False


def build_coordinator_message(task, event_type):
    workflow_id = task.get("workflow_id", "unknown")
    task_id = task["task_id"]

    goal = task.get("goal", "未定义")

    criteria = "\n".join(
        f"- {item}"
        for item in task.get(
            "acceptance_criteria",
            []
        )
    )

    if not criteria:
        criteria = "- 未定义"

    if event_type == "inner_loop_exhausted":
        blocker_content = (
            _read_loop_doc(task, "BLOCKER.md")
            or "（BLOCKER.md 未找到）"
        )

        return f"""
HERDR_CONTROLLER_BLOCKER_EVENT

workflow_id: {workflow_id}
task_id: {task_id}
stage: {task['stage']}
pane_id: {task['pane_id']}
agent: {task['agent']}
agent_status: blocked (inner_loop_exhausted)

任务目标：
{goal}

⚠️ 工位内循环已耗尽全部重试次数，无法自愈，主动请求总指挥仲裁。

━━━━━━━━━━━━━━━━━━━━━
工位求助单 (BLOCKER.md)
━━━━━━━━━━━━━━━━━━━━━
{blocker_content}

━━━━━━━━━━━━━━━━━━━━━
总指挥仲裁三选一(决策必须落盘)：
━━━━━━━━━━━━━━━━━━━━━

A. 问题可解决(提供具体指导后让工位继续)：
   ~/HAFlow/bin/herdr-task set {task_id} working
   然后用 herdr agent prompt 向工位下达具体修复指令。

B. 需要换策略(放弃本轮，换 Agent 或调整范围)：
   ~/HAFlow/bin/herdr-task set {task_id} failed
   再按需重新规划/重派。

C. 目标或验收标准有歧义(调整后重新派发)：
   ~/HAFlow/bin/herdr-task set {task_id} failed
   修正任务目标/验收标准后重新下发。

仲裁前不得把 Task 置为 completed。

{COORDINATOR_DISCIPLINE}
""".strip()

    if event_type == "blocked":
        return f"""
HERDR_CONTROLLER_BLOCKED_EVENT

workflow_id: {workflow_id}
task_id: {task_id}
stage: {task['stage']}
pane_id: {task['pane_id']}
agent: {task['agent']}
agent_status: blocked

任务目标：
{goal}

当前 Task 被 Agent 阻塞。

你现在只负责解除阻塞，不允许验收任务。

必须执行：

1. 使用 Herdr 读取 {task['pane_id']} 当前界面和最新输出。
2. 判断 blocked 的真实原因。
3. 如果属于低风险、当前任务范围内的正常操作，可以处理审批并让 Agent 继续。
4. 如果属于高风险操作，不得自动批准，向用户报告风险并保持 blocked。
5. blocked 阶段不得把 Task 设置为 completed。
6. blocked 阶段不得进行正式任务验收。
7. Agent 恢复执行后，Controller 会自动处理 working 状态。
8. 不要推进阶段。

blocked 只表示等待处理，不代表任务结束。

{COORDINATOR_DISCIPLINE}
""".strip()

    if event_type == "attention":
        return f"""
HERDR_CONTROLLER_ATTENTION_EVENT

workflow_id: {workflow_id}
task_id: {task_id}
stage: {task['stage']}
pane_id: {task['pane_id']}
agent: {task['agent']}
task_status: {task.get('status')}

任务目标：
{goal}

该 Task 长时间停留在需要人工/总指挥裁决的中间态（interrupted / paused），
既未完成也未失败，阻塞了当前节点的推进。

现在必须立即裁决，只处理这一个 Task：

1. 使用 Herdr 读取 {task['pane_id']} 当前界面与最新输出。
2. 判断现场是否仍有未完成的有效产出：
   - 产出已就绪 → 执行验收（verify-baseline），通过则 set completed（门禁阶段带 --verdict）。
   - 实现未完成 → set rework 并用 herdr agent prompt 继续下发指令。
   - 无法恢复 → set failed。
3. 严禁让任务继续停留在 interrupted / paused。
4. 不要创建新 Task，不要推进阶段。

{COORDINATOR_DISCIPLINE}
""".strip()

    if event_type == "done":
        return f"""
HERDR_CONTROLLER_DONE_EVENT

workflow_id: {workflow_id}
task_id: {task_id}
stage: {task['stage']}
pane_id: {task['pane_id']}
agent: {task['agent']}
agent_status: done

任务目标：
{goal}

验收标准：
{criteria}

Agent 本轮执行已经结束。

现在执行正式验收：

1. 使用 Herdr 读取 {task['pane_id']} 的最终输出。

2. 必须执行基线验证与量化指标核验：
   ~/HAFlow/bin/herdr-task verify-baseline {task_id}
   ~/HAFlow/bin/herdr-task verify-metrics {task_id} --if-present

3. `verify-baseline` 是判断当前 Task 文件变化的唯一事实来源：

   - `BASELINE_MATCH`
     表示 Agent 相对于 Task 创建时没有产生新的文件变化。

   - `TASK_CHANGED`
     后面列出的文件，才是当前 Task 真正产生的变化。

4. 验收决策指引（严禁混淆）：

   A. 验收通过（所有标准满足、测试绿灯）：
      ~/HAFlow/bin/herdr-task set {task_id} completed --verdict pass

   B. 发现代码缺陷需回炉（特别是 test / review 阶段查出问题）：
      **严禁对评审/测试任务执行 set rework！**
      必须以 blocked 结论闭环，Controller 会自动触发跨阶段回流并作废受影响链条：
      ~/HAFlow/bin/herdr-task set {task_id} completed --verdict blocked --note "<blocker 清单与修复指引>"

   C. 仅当当前任务自身未完成（如实现中途卡死、需在同一工位继续补全）：
      ~/HAFlow/bin/herdr-task rework {task_id} --prompt "<本任务修复指引>"
      保留同一 task_id 与原工位；不要 supersede + launch 扩增 Pane。

   D. 如果任务发生不可恢复的崩溃：
      ~/HAFlow/bin/herdr-task set {task_id} failed

阶段推进前必须执行：

~/HAFlow/bin/herdr-task list --workflow-id {workflow_id}

只能检查当前 workflow_id 下的任务。
禁止使用其他 Workflow 或历史 Task 判断当前阶段门禁。

只有当前 Workflow 当前阶段所有必要 Task 都 completed，
才能进入下一阶段。

{COORDINATOR_DISCIPLINE}
""".strip()

    return None


def _read_loop_doc(task, filename, max_lines=None):
    """读取工位内环目录(<clone>/.herdr-loop/)下的报告文档,失败返回空串。"""
    clone_path = task.get("clone_path") or ""
    if not clone_path:
        return ""
    path = Path(clone_path) / ".herdr-loop" / filename
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        return ""
    if max_lines:
        lines = lines[:max_lines]
    return "\n".join(lines)


def blocked_event_type(task):
    """blocked 事件细分:内环耗尽走专属仲裁卡,其余走通用解除阻塞卡。

    内环协议(herdr-loop/evaluator)在重试耗尽时写 <clone>/.herdr-loop/
    BLOCKER.md 并输出 HERDR_TASK_BLOCKER 标记,Sentinel 置 blocked
    (sentinel_reason=inner_loop_exhausted)。此前 Controller 只发通用
    blocked 卡,工位自述的 BLOCKER.md 被丢弃——这是 Phase 0 设计的
    最后一跳(2026-09-13 设计,09-18 补齐)。
    """
    # Transition history is the current decision; metadata can survive a
    # later generic blocker. Retain the legacy fallback when no reason exists.
    history = (task or {}).get("status_history") or []
    latest = history[-1] if history and isinstance(history[-1], dict) else {}
    if latest.get("to") == "blocked" and latest.get("reason"):
        return (
            "inner_loop_exhausted"
            if latest.get("reason") == "inner_loop_exhausted"
            or latest.get("sentinel_reason") == "inner_loop_exhausted"
            else "blocked"
        )
    if task and task.get("sentinel_reason") == "inner_loop_exhausted":
        return "inner_loop_exhausted"
    return "blocked"


def blocker_queue_episode(task):
    """Identity of the persisted blocker; metadata saves are not transitions."""
    history = task.get("status_history") or []
    latest = history[-1] if history and isinstance(history[-1], dict) else None
    identity = {
        "workflow_id": task.get("workflow_id"),
        "run_id": task.get("run_id"),
        "history_length": len(history),
        "transition": latest,
    }
    if latest is None:
        # Legacy rows have no transition identity. Never infer continuity
        # across a changed persisted version.
        identity["legacy_version"] = task.get("version")
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def enqueue_coordinator_event(task, event_type):
    key = f"{task['task_id']}:{event_type}"
    episode = (
        blocker_queue_episode(task)
        if event_type in {"blocked", "inner_loop_exhausted"} else None
    )
    queue_key = f"{key}:{episode}" if episode is not None else key

    with lock:
        if queue_key in queued_events:
            print(
                f"[QUEUE DUPLICATE SKIPPED] {key}"
            )
            return

        queued_events.add(queue_key)

    coordinator_queue.put(
        {
            "task_id": task["task_id"],
            "event_type": event_type,
            "key": key,
            "queue_key": queue_key,
            "blocker_episode": episode,
        }
    )

    print(
        f"[QUEUE] "
        f"task={task['task_id']} "
        f"event={event_type}"
    )


def handle_coordinator_delivery_stall(item, task, elapsed):
    """总指挥投递 SLA 到期:记录 attention、通知、让位(不再无限 BUSY)。

    registry watcher 会依据 episode 的 next_retry_at 做慢速重试;
    总指挥恢复后事件会自然补投,不需要人工重启。
    """
    key = item["key"]
    event_type = item["event_type"]
    workflow_id = task.get("workflow_id")
    pane_id = coordinator_pane_for_workflow(workflow_id) or "unknown"

    episode = attention_get(key) or {}
    attempts = int(episode.get("attempts") or 0) + 1
    retry_at = time.time() + liveness.attention_retry_interval()

    attention_note(
        key,
        task,
        event_type,
        reason="coordinator_stalled",
        attempts=attempts,
        next_retry_at=retry_at,
        detail=f"waited={int(elapsed)}s pane={pane_id}",
    )

    print(
        f"[COORDINATOR STALLED] "
        f"task={item['task_id']} "
        f"event={event_type} "
        f"pane={pane_id} "
        f"waited={int(elapsed)}s "
        f"attempts={attempts} "
        f"-> attention recorded, retry_in={int(liveness.attention_retry_interval())}s"
    )

    notify_attention(
        "Herdr Factory · 总指挥停滞",
        task,
        f"事件 {event_type} 等待总指挥超过 {int(elapsed)}s（pane={pane_id}）。"
        "已记录 attention 并将慢速重试，请检查总指挥状态与集成健康。",
        "coordinator_stalled",
    )


def wait_for_coordinator_decision(task_id, timeout=None):
    """等待总指挥判定落盘(completed/rework/...)。

    默认预算与真实回合尾部耗时对齐(env HERDR_COORDINATOR_DECISION_TIMEOUT);
    超时不立即重试:记录 attention 退避,避免再烧一整轮 10 分钟级回合。
    """
    if timeout is None:
        timeout = liveness.coordinator_decision_timeout()

    deadline = time.time() + timeout

    while time.time() < deadline:
        task = get_task(task_id)

        if not task:
            print(
                f"[DECISION ERROR] "
                f"task={task_id} missing"
            )
            return None

        status = task.get("status")

        if status != "agent_done":
            print(
                f"[DECISION] "
                f"task={task_id} "
                f"status={status}"
            )
            return status

        time.sleep(0.5)

    task = get_task(task_id)
    key = f"{task_id}:done"
    episode = attention_get(key) or {}
    attempts = int(episode.get("attempts") or 0) + 1

    if task:
        attention_note(
            key,
            task,
            "done",
            reason="decision_timeout",
            attempts=attempts,
            next_retry_at=time.time() + liveness.attention_retry_interval(),
            detail=f"waited={int(timeout)}s still=agent_done",
        )

    print(
        f"[DECISION TIMEOUT] "
        f"task={task_id} "
        f"still=agent_done "
        f"attempts={attempts} "
        f"-> attention recorded, "
        f"retry_in={int(liveness.attention_retry_interval())}s"
    )

    return "agent_done"


def retry_coordinator_decision(task_id):
    task = get_task(task_id)

    if not task:
        print(
            f"[RETRY ERROR] "
            f"task={task_id} missing"
        )
        return None

    if task.get("status") != "agent_done":
        return task.get("status")

    message = f"""
HERDR_CONTROLLER_RETRY_EVENT

workflow_id: {task.get('workflow_id', 'unknown')}
task_id: {task_id}
stage: {task.get('stage', 'unknown')}
pane_id: {task.get('pane_id', 'unknown')}
agent: {task.get('agent', 'unknown')}

这是一次自动重试。

该 Task 仍停留在 agent_done，
说明上一次验收通知没有完成状态落盘。

请立即只处理这个已有 Task，不要创建新 Task：

1. 读取 Task Registry。
2. 读取 Agent 最终输出。
3. 执行：
   ~/HAFlow/bin/herdr-task verify-baseline {task_id}
4. 根据任务目标和验收标准完成正式验收。
5. 必须将 Task 状态更新为以下之一：
   - completed（门禁阶段必须带 --verdict pass|blocked，blocked 另附 --note）
   - rework
   - failed
6. 不要只输出文字报告而不更新 Task Registry。

{COORDINATOR_DISCIPLINE}
""".strip()

    print(
        f"[COORDINATOR RETRY] "
        f"task={task_id}"
    )

    result = subprocess.run(
        [
            "herdr",
            "agent",
            "prompt",
            coordinator_pane_for_workflow(task.get("workflow_id")),
            message,
            "--wait",
            "--timeout",
            "120000"
        ],
        text=True,
        capture_output=True
    )

    if result.returncode != 0:
        print(
            f"[COORDINATOR RETRY ERROR] "
            f"task={task_id}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    return wait_for_coordinator_decision(
        task_id,
        timeout=30
    )

def maybe_complete_on_task_done(task_id, db_path=None):
    """Complete acknowledged handoffs whose target reached terminal done.

    Production hook for the authoritative task completion pipeline. Only
    ``acknowledged`` rows transition: a handoff never ACKed stays visible as
    stuck instead of being silently closed. Never raises.
    """
    if not collaboration_enabled():
        return []
    try:
        from herdr import state_db as _sdb
        done = []
        for row in _sdb.list_collaboration_events(
            task_id=task_id, status="acknowledged", db_path=db_path,
        ):
            if row["to_task_id"] != task_id:
                continue
            done.append(_sdb.mark_collaboration_completed(row["event_id"], db_path=db_path))
        return done
    except Exception as exc:
        print(f"[COLLABORATION COMPLETE SKIPPED] task={task_id}: {type(exc).__name__}")
        return []


def _parse_commit_result(output):
    """Extract HERDR_COMMIT_RESULT JSON from `herdr-task commit` output."""
    for line in (output or "").splitlines():
        if line.startswith("HERDR_COMMIT_RESULT="):
            try:
                payload = json.loads(line.split("=", 1)[1])
            except ValueError:
                return {}
            return payload if isinstance(payload, dict) else {}
    return {}


def _parse_integrate_result(output):
    """M-7: extract HERDR_INTEGRATE_RESULT JSON from integrate output."""
    for line in (output or "").splitlines():
        if line.startswith("HERDR_INTEGRATE_RESULT="):
            try:
                payload = json.loads(line.split("=", 1)[1])
            except ValueError:
                return {}
            return payload if isinstance(payload, dict) else {}
    return {}


def _record_finalize_event(task, event_type, payload):
    """Best-effort finalize observability event; never breaks finalization."""
    try:
        from herdr.trajectory import run_id_for_task

        store = _get_store()
        store.record_event(
            event_type,
            dict(payload or {}),
            workflow_id=(task or {}).get("workflow_id"),
            node_id=(task or {}).get("node") or (task or {}).get("stage"),
            task_id=(task or {}).get("task_id"),
            agent_id=(task or {}).get("agent"),
            source="controller",
            run_id=run_id_for_task(task or {}),
        )
    except (OSError, ValueError, RuntimeError, AttributeError,
             subprocess.SubprocessError) as exc:
        print(f"[FINALIZE EVENT ERROR] {event_type}: {exc}")


def _escalate_finalize(task, reason, detail=None):
    """Mark a git task escalated-to-human so close stops deferring on it."""
    task_id = (task or {}).get("task_id") or "unknown"
    payload = {"reason": reason, "status": (task or {}).get("status")}
    if detail:
        payload["detail"] = detail
    print(f"[FINALIZE ESCALATED] task={task_id} reason={reason}")
    _record_finalize_event(task, "finalize_escalated", payload)
    try:
        from herdr import kernel

        kernel.update_task_metadata(
            task_id,
            {
                "finalize_escalated": True,
                "finalize_escalate_reason": reason,
            },
            store=_get_store(),
        )
    except (OSError, ValueError, RuntimeError, AttributeError,
             subprocess.SubprocessError) as exc:
        print(f"[FINALIZE ESCALATE ERROR] task={task_id}: {exc}")


def _empty_auto_releasable(task):
    """H-3: anchored empties never auto-release; legacy time empties may.

    The old判据 returned True for any task with ``baseline_commit``, but
    ``commit_task`` EMPTY always persists ``commit_basis`` as
    ``baseline_commit``/``time``, so the ``empty_unreleasable`` branch was
    dead and every true vacuum task auto-advanced to ``cleanup_ready``.
    An anchor proves the *method* is credible, not that the *empty result*
    is credible: a completed git task that produced nothing needs a human.
    Only legacy anchor-less time-basis empties (same fail-closed timestamp
    guard as adoption) release; basis-absent records escalate.
    """
    task = task or {}
    if task.get("baseline_commit"):
        return False
    return task.get("commit_basis") == "time"


def clear_finalize_escalation(task_id):
    """H-1: revoke machine-set finalize_escalated so finalize re-drives."""
    task = get_task(task_id)
    if not task:
        return False
    if not task.get("finalize_escalated"):
        return True
    try:
        from herdr import kernel
        kernel.update_task_metadata(
            task_id,
            {"finalize_escalated": False,
             "finalize_escalate_reason": None},
            store=_get_store(),
        )
    except (OSError, ValueError, RuntimeError, AttributeError,
            subprocess.SubprocessError) as exc:
        print(f"[FINALIZE UNCLEAR ERROR] task={task_id}: {exc}")
        return False
    attention_clear(f"{task_id}:finalize")
    try:
        _finalize_retry_exhausted_logged.discard(task_id)
    except AttributeError:
        pass
    print(f"[FINALIZE UNESCALATED] task={task_id}")
    return True


def _check_finalize_retry(task, status, now):
    """Shared finalize-retry driver for completed/commit Retry paths.

    Returns True when a finalize attempt was made or exhaustion was recorded.

    M-2 (AC4-2/AC4-3): deterministic outcomes (rc=3 EMPTY, rc=4 REFUSED,
    rc=5 integrate-blocked, rc=6 conflict) never consume retry budget and
    rc=4 never emits a ``commit_retry`` log. ``finalize_completed_task``
    reports ``retryable``; only retryable attempts log
    ``[REGISTRY WATCHER] ... retry finalize`` and increment the attention
    episode. Already-escalated tasks are settled and skip re-driving
    entirely (revoke via ``clear_finalize_escalation``). M-6:
    ``subprocess.CalledProcessError`` from ``check=True``/``check_output``
    paths is escalated immediately instead of bypassing budget accounting.
    """
    task = task or {}
    task_id = task.get("task_id")
    if not task_id:
        return False
    if task.get("finalize_escalated"):
        return True
    if task.get("integration_mode") != "git" or workflow_closed(
        task.get("workflow_id")
    ):
        attention_clear(f"{task_id}:finalize")
        _finalize_retry_exhausted_logged.discard(task_id)
        return False
    key = f"{task_id}:finalize"
    owner_episode = blocker_queue_episode(task)
    def expire_owned_wait(current):
        detail = current.get("detail") or {}
        if (current.get("reason") == "git_index_lock"
                and isinstance(detail, dict) and detail.get("owner_episode")
                and detail["owner_episode"] != owner_episode):
            return {"__replace__": {}}
        return None
    # Only newly typed waits have a trustworthy owner. Never reset generic
    # failures or legacy escalations; metadata-only saves retain history ID.
    _attention_store.mutate(key, expire_owned_wait)
    # P1 recovery coherence: `herdr-task clear-escalation` (or any external
    # recovery) removes the shared attention episode from another process.
    # The in-memory exhausted latch must follow the episode: otherwise a
    # later re-exhaustion would stall silently with no log and no
    # re-escalation because the stale latch suppresses both.
    if attention_get(key) is None:
        _finalize_retry_exhausted_logged.discard(task_id)
    try:
        retry, reason, exhausted = should_retry_finalize(
            status, attention_get(key), now
        )
    except (OSError, RuntimeError, ValueError, KeyError,
            subprocess.SubprocessError) as exc:
        print(f"[FINALIZE RETRY ERROR] task={task_id}: {exc}")
        _escalate_finalize(get_task(task_id) or task, "retry_driver_error",
                           {"error": f"{type(exc).__name__}: {exc}"})
        return True
    if exhausted:
        if task_id not in _finalize_retry_exhausted_logged:
            _finalize_retry_exhausted_logged.add(task_id)
            print(
                f"[FINALIZE RETRY EXHAUSTED] task={task_id} "
                f"status={status} attempts>={FINALIZE_RETRY_MAX} "
                "-> manual/coordinator intervention required"
            )
            _escalate_finalize(
                get_task(task_id) or task,
                "retry_exhausted",
                {"status": status, "reason": reason},
            )
        return True
    if not retry:
        return False
    wait_episode_before = attention_get(key) or {}
    try:
        outcome = finalize_completed_task(task_id)
    except (OSError, RuntimeError, ValueError, KeyError,
            subprocess.SubprocessError) as exc:
        # M-6: integrate check=True / check_output raises
        # CalledProcessError (SubprocessError, not OSError). Without this
        # the exception escapes to registry_watcher, skips attention
        # budgeting, and retries every ~1s with no backoff.
        print(f"[FINALIZE SUBPROCESS ERROR] task={task_id}: "
              f"{type(exc).__name__}: {exc}")
        _escalate_finalize(get_task(task_id) or task,
                           "finalize_subprocess_error",
                           {"error": f"{type(exc).__name__}: {exc}"[:500]})
        return True
    if (isinstance(outcome, dict) and outcome.get("kind") == "wait"
            and outcome.get("reason") == "git_index_lock"
            and outcome.get("budgeted") is False):
        fresh = get_task(task_id)
        if (fresh and fresh.get("status") == status
                and fresh.get("run_id") == task.get("run_id")
                and fresh.get("version") == task.get("version")):
            def persist_wait(current):
                # Cross-process CAS under the existing EpisodeStore lock:
                # do not overwrite an episode another driver changed while
                # the CLI or the authoritative task read was in flight.
                if current != wait_episode_before:
                    return None
                episode = dict(current or {})
                episode.update(task_id=task_id, workflow_id=fresh.get("workflow_id"),
                               event_type="finalize", reason="git_index_lock",
                               attempts=int(episode.get("attempts") or 0),
                               last_attempt_at=time.time(), next_retry_at=now + 60,
                               detail={"run_id": fresh.get("run_id"), "version": fresh.get("version"),
                                       "owner_episode": owner_episode})
                episode.setdefault("first_seen_at", time.time())
                return episode
            _attention_store.mutate(key, persist_wait)
        # A late result must not clear or overwrite another run/transition's
        # shared-key episode. Leave its current owner to drive recovery.
        return True
    retryable = True
    if isinstance(outcome, dict) and "retryable" in outcome:
        retryable = bool(outcome.get("retryable"))
    cur_t = get_task(task_id)
    if not retryable:
        # Deterministic failure (EMPTY/REFUSED): settled via escalation or
        # auto-release, no budget consumed, no retry log (AC4-2/AC4-3).
        if cur_t and cur_t.get("status") != status:
            attention_clear(key)
            _finalize_retry_exhausted_logged.discard(task_id)
        return True
    print(
        f"[REGISTRY WATCHER] "
        f"task={task_id} "
        f"status={status} -> retry finalize ({reason})"
    )
    if cur_t and cur_t.get("status") == status:
        episode = attention_get(key) or {}
        attention_note(
            key,
            task,
            "finalize",
            reason=reason,
            attempts=int(episode.get("attempts") or 0) + 1,
        )
        attention_throttle(key, now=now)
    else:
        attention_clear(key)
        _finalize_retry_exhausted_logged.discard(task_id)
    return True


def finalize_completed_task(task_id):
    """Drive one git-finalize step; returns ``{"retryable": bool, ...}``.

    M-2: deterministic outcomes (rc=3 EMPTY, rc=4 REFUSED, rc=5
    integrate-blocked/main-dirty, rc=6 conflict) report
    ``retryable=False`` so ``_check_finalize_retry`` never consumes
    budget or logs a retry for them; only transient/unknown failures
    (rc=75, unexpected rc, state-transition failures) report True.
    H-1: rc=5 (main repo tracked changes) is persistent, never self-heals,
    so it escalates immediately instead of retrying 5x into
    ``finalize_escalated`` + silent ``delivered``.
    """
    task = get_task(task_id)

    if not task:
        print(f"[FINALIZE SKIP] task={task_id} missing")
        return {"retryable": False, "kind": "skip"}

    if task.get("status") not in ("completed", "committed"):
        print(
            f"[FINALIZE SKIP] "
            f"task={task_id} "
            f"status={task.get('status')}"
        )
        return {"retryable": False, "kind": "skip"}

    if task.get('superseded_by'):
        return {'retryable': False, 'kind': 'retired_lineage'}

    # Acceptance and delivery are separate: never land a known failed candidate.
    if task.get('integration_mode') == 'git' and task.get('commit'):
        failed_sha = task['commit']
        for verifier in _get_store().list_tasks(workflow_id=task.get('workflow_id')):
            if (verifier.get('status') != 'superseded' and not verifier.get('superseded_by')
                    and verifier.get('stage_verdict') == 'blocked'
                    and verifier.get('candidate_sha') == failed_sha):
                return {'retryable': False, 'kind': 'candidate_blocked'}

    # Collaboration accelerator: authoritative task completion closes the
    # handoffs targeting it, so handoff latency metrics stay trustworthy.
    for completed in maybe_complete_on_task_done(task_id):
        print(f"[COLLABORATION COMPLETED] {completed.get('event_id')}")

    mode = task.get(
        "integration_mode",
        "none"
    )

    print(
        f"[FINALIZE] "
        f"task={task_id} "
        f"integration_mode={mode}"
    )

    # --------------------------------
    # 需要 Git 集成
    # --------------------------------
    if mode == "git":

        clone_path = task.get("clone_path")
        if clone_path and ensure_no_git_processes is not None:
            try:
                ensure_no_git_processes(clone_path)
            except Exception as exc:
                print(
                    f"[FINALIZE WAIT] task={task_id} git process still active: {exc}"
                )
                return {"retryable": True, "kind": "wait"}

        # 1. 将 Task 自己产生的修改安全提交(若此前已 committed 则跳过)
        skip_integrate = False
        if task.get("status") == "completed":
            # 提交门禁拆分:herdr 任务克隆内只跑快速必需检查,
            # 全量测试由 workflow test 节点与 pre-push 门禁负责。
            commit_env = dict(os.environ)
            commit_env["HERDR_DEFER_HEAVY_TESTS"] = "1"

            result = subprocess.run(
                [
                    TASK_MANAGER,
                    "commit",
                    task_id,
                    "--message",
                    f"task: {task_id}"
                ],
                text=True,
                capture_output=True,
                env=commit_env
            )

            if result.stdout.strip():
                print(result.stdout.strip())

            commit_payload = _parse_commit_result(result.stdout)

            if result.returncode == 3:
                fresh = get_task(task_id) or task
                print(
                    f"[FINALIZE EMPTY] "
                    f"task={task_id} "
                    f"head={commit_payload.get('head')} "
                    f"basis={commit_payload.get('basis')}"
                )
                _record_finalize_event(
                    fresh, "finalize_empty", commit_payload
                )
                if not _empty_auto_releasable(fresh):
                    _escalate_finalize(
                        fresh,
                        "empty_unreleasable",
                        commit_payload,
                    )
                    return {"retryable": False, "kind": "empty", "rc": 3}
                if not set_task_status(task_id, "cleanup_ready"):
                    print(
                        f"[FINALIZE ERROR] "
                        f"task={task_id} "
                        f"empty release did not reach cleanup_ready"
                    )
                    return {"retryable": True, "kind": "error", "rc": 3}
                skip_integrate = True
                task = get_task(task_id)
            elif result.returncode == 4:
                print(
                    f"[FINALIZE REFUSED] "
                    f"task={task_id} "
                    f"reason={commit_payload.get('reason')}"
                )
                _escalate_finalize(
                    get_task(task_id) or task,
                    "commit_refused",
                    commit_payload,
                )
                return {"retryable": False, "kind": "refused", "rc": 4}
            elif result.returncode == 75:
                print(
                    f"[FINALIZE WAIT] "
                    f"task={task_id} "
                    f"reason=git_busy retry later"
                )
                if (commit_payload.get("task_id") == task_id
                        and commit_payload.get("result") == "wait"
                        and commit_payload.get("reason") == "git_index_lock"):
                    return {"retryable": True, "kind": "wait", "rc": 75,
                            "reason": "git_index_lock", "budgeted": False}
                return {"retryable": True, "kind": "wait", "rc": 75}
            elif result.returncode != 0:
                print(
                    f"[COMMIT ERROR] "
                    f"task={task_id} "
                    f"rc={result.returncode}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
                _record_finalize_event(
                    task,
                    "finalize_commit_error",
                    {
                        "rc": result.returncode,
                        "detail": (
                            result.stderr.strip()
                            or result.stdout.strip()
                        )[:500],
                    },
                )
                return {"retryable": True, "kind": "error", "rc": result.returncode}
            else:
                task = get_task(task_id)

                if not task or task.get("status") != "committed":
                    print(
                        f"[FINALIZE ERROR] "
                        f"task={task_id} "
                        f"did not reach committed"
                    )
                    return {"retryable": True, "kind": "error"}

        if skip_integrate:
            print(
                f"[FINALIZE EMPTY RELEASED] "
                f"task={task_id} "
                f"completed -> cleanup_ready"
            )
        else:
            # 2. Rebase + 导入主仓库 + Integration Branch
            result = subprocess.run(
                [
                    TASK_MANAGER,
                    "integrate",
                    task_id
                ],
                text=True,
                capture_output=True
            )

            if result.stdout.strip():
                print(result.stdout.strip())

            if result.returncode == 6:
                print(
                    f"[FINALIZE REFUSED] "
                    f"task={task_id} "
                    f"reason=integrate_rebase_conflict"
                )
                _escalate_finalize(
                    get_task(task_id) or task,
                    "integrate_rebase_conflict",
                    _parse_integrate_result(result.stdout),
                )
                return {"retryable": False, "kind": "refused", "rc": 6}

            if result.returncode == 4:
                # M-7: two exit-4 sources carry different payloads:
                # remote_diverged (explicit HERDR_INTEGRATE_RESULT) vs
                # not_based_cleanly (relation check). Do not conflate.
                integrate_payload = _parse_integrate_result(result.stdout)
                if integrate_payload.get("result") == "remote_diverged":
                    reason = "integrate_remote_diverged"
                else:
                    reason = "integrate_not_based_cleanly"
                print(
                    f"[FINALIZE REFUSED] "
                    f"task={task_id} "
                    f"reason={reason}"
                )
                _escalate_finalize(
                    get_task(task_id) or task,
                    reason,
                    integrate_payload,
                )
                return {"retryable": False, "kind": "refused", "rc": 4}

            if result.returncode == 5:
                # H-1: main-repo tracked changes are persistent (never
                # self-heal). Escalate deterministically; retrying 5x
                # then auto-closing as delivered would silently drop
                # the never-integrated deliverable.
                integrate_payload = _parse_integrate_result(result.stdout)
                print(
                    f"[FINALIZE REFUSED] "
                    f"task={task_id} "
                    f"reason=integrate_main_dirty"
                )
                _escalate_finalize(
                    get_task(task_id) or task,
                    "integrate_main_dirty",
                    integrate_payload,
                )
                return {"retryable": False, "kind": "refused", "rc": 5}

            if result.returncode != 0:
                print(
                    f"[INTEGRATE ERROR] "
                    f"task={task_id} "
                    f"rc={result.returncode}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
                _record_finalize_event(
                    task,
                    "finalize_integrate_error",
                    {
                        "rc": result.returncode,
                        "detail": (
                            result.stderr.strip()
                            or result.stdout.strip()
                        )[:500],
                    },
                )
                return {"retryable": True, "kind": "error", "rc": result.returncode}

            task = get_task(task_id)

            if not task or task.get("status") != "integrated":
                print(
                    f"[FINALIZE ERROR] "
                    f"task={task_id} "
                    f"did not reach integrated"
                )
                return {"retryable": True, "kind": "error"}

            # 3. 允许清理
            if not set_task_status(
                task_id,
                "cleanup_ready"
            ):
                return {"retryable": True, "kind": "error"}

    # --------------------------------
    # 不需要 Git 集成
    # --------------------------------
    elif mode == "none":
        if not set_task_status(
            task_id,
            "cleanup_ready"
        ):
            return {"retryable": True, "kind": "error"}

    else:
        print(
            f"[FINALIZE ERROR] "
            f"task={task_id} "
            f"unknown integration_mode={mode}"
        )
        return {"retryable": False, "kind": "error"}

    # --------------------------------
    # 自动 Cleanup
    # --------------------------------
    result = subprocess.run(
        [
            TASK_MANAGER,
            "cleanup",
            task_id
        ],
        text=True,
        capture_output=True
    )

    if result.stdout.strip():
        print(result.stdout.strip())

    if result.returncode != 0:
        print(
            f"[CLEANUP ERROR] "
            f"task={task_id}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
        return {"retryable": True, "kind": "error", "rc": result.returncode}

    print(
        f"[FINALIZED] task={task_id}"
    )

    # Task 最终 cleaned 后检查整个阶段是否已经完成。
    task = get_task(task_id)

    if task:
        enqueue_stage_advance(
            task
        )
    return {"retryable": False, "kind": "finalized"}


def coordinator_worker():
    """Main dispatcher: reads items from coordinator_queue and fans them out
    to per-workflow executor threads, guaranteeing at-most-one concurrent
    dispatch per workflow without blocking the queue for other workflows.
    """
    while True:
        item = coordinator_queue.get()
        try:
            # Resolve workflow_id for routing.
            workflow_id = item.get("workflow_id")
            if not workflow_id and "task_id" in item:
                t = get_task(item["task_id"])
                workflow_id = (t or {}).get("workflow_id", "unknown")

            wf_lock = _workflow_dispatch_lock(workflow_id or "unknown")
            _coordinator_executor.submit(
                _process_coordinator_item, item, wf_lock
            )
        finally:
            coordinator_queue.task_done()


def _process_coordinator_item(item, wf_lock):
    """Process one coordinator queue item inside the executor thread pool,
    serialized per-workflow via wf_lock.
    """
    with wf_lock:
        _handle_coordinator_item(item)


def _build_selective_replan_message(item, project_name="unknown"):
    """选择性返工的事件消息:告知归因结果,禁止总指挥重做整个阶段。"""
    workflow_id = item["workflow_id"]
    gate_stage = item["gate_stage"]
    retry_node = item["retry_node"]
    loop_count = item["loop_count"]
    max_loops = item["max_loops"]
    invalidated = item.get("invalidated") or []
    suggested_branch = item.get("suggested_branch")
    roots = [str(r).strip() for r in (item.get("target_lineage_roots") or [])]
    roots_text = ", ".join(roots) if roots else "(未记录)"

    blockers_text = "\n".join(
        f"- {b.get('task_id')}: {b.get('note') or '(未记录说明)'}"
        for b in item.get("blockers") or []
    ) or "- (未记录 blocker 说明,请读取 gate 阶段任务输出)"

    if loop_count >= max_loops:
        escalation = (
            f"\n注意:已达 fix-loop 上限({loop_count}/{max_loops})。"
            "先向用户请示(继续修 / 换方案 / 放弃),未经用户确认不得派发。\n"
        )
    else:
        escalation = ""

    branch_line = suggested_branch or "(未找到,请确认 retry_node 最近 committed 任务的分支)"

    docs_block = shared_docs_block(
        workflow_id,
        retry_node,
        related_nodes=(gate_stage,),
        project_ctx=project_for_workflow(workflow_id) or {},
    )

    return f"""
HERDR_CONTROLLER_SELECTIVE_REPLAN_EVENT

workflow_id: {workflow_id}
project_name: {project_name}
gate_stage: {gate_stage} — 验收结论 blocked(已显式归因)
retry_node: {retry_node}
suggested_branch: {branch_line}
loop_count: {loop_count}/{max_loops}
被点名的实现任务谱系(受影响,需重做): {roots_text}
{escalation}
门禁在结构化结论里明确指出了受影响的实现 Task,因此本次**不是**整体返工:
Controller 已只作废被点名的谱系(共 {len(invalidated)} 个 Task,见 Task Registry),
实现节点内其余任务保持原样、不重派。

可继续的任务由 Controller 就地 rework，复用 Task 和 Pane；
已终结任务的替代任务(如 <task>-r2)通过既有补派管线创建，记录理由并计入累计配额,
你**不需要**派发任何 fix task。

⛔ 禁止:对 --stage {retry_node} 派发全量 fix task。
那会与 Controller 正在执行的替代任务重复,并把改动范围扩大到被明确
保留的任务上,直接破坏本次归因的前提。

你现在只需确认流程继续:
- 替代任务已派发 → 无需动作,等 fix 完成,下游 test → review → wrapup 自动推进;
- 长时间没有出现替代任务(例如路由/准入阻断)→ 排查派发链路本身
  (`./bin/herdr-deep-preflight --deep` 看 Agent 准入),必要时向用户请示,
  仍**不要**改用全量 fix task 兜底。

Blocker 清单(blocked 结论与修复指引):
{blockers_text}

如需再次修复,由后续门禁结论重新归因;不要绕过 Controller 自行扩大作废范围。
派发/排查完成后结束当前回合,后续推进交给 Controller。

{docs_block}

{COORDINATOR_DISCIPLINE}
""".strip()


def build_fix_loop_message(item, project_name="unknown"):
    """fix_loop 事件消息;派发命令是建议骨架,裁量在总指挥。

    mode=selective(PR #110)时归因已明确到具体实现任务谱系,替代任务由
    Controller 的既有补派管线自动创建——此时绝不能发 legacy 的全量
    fix task 指引,否则总指挥会与 Controller 同时在同一分支上重做整个
    阶段,既重复又破坏「只重做被点名范围」的保证。
    """
    if item.get("mode") == "selective":
        return _build_selective_replan_message(item, project_name)

    workflow_id = item["workflow_id"]
    gate_stage = item["gate_stage"]
    retry_node = item["retry_node"]
    loop_count = item["loop_count"]
    max_loops = item["max_loops"]
    invalidated = item.get("invalidated") or []
    suggested_branch = item.get("suggested_branch")

    blockers_text = "\n".join(
        f"- {b.get('task_id')}: {b.get('note') or '(未记录说明)'}"
        for b in item.get("blockers") or []
    ) or "- (未记录 blocker 说明,请读取 gate 阶段任务输出)"

    escalation = ""
    if loop_count >= max_loops:
        escalation = (
            f"\n注意:已达 fix-loop 上限({loop_count}/{max_loops})。"
            "先向用户请示(继续修 / 换方案 / 放弃),"
            "未经用户确认不得派发。\n"
        )

    if item.get("exhausted"):
        escalation = (
            f"\n⛔ 回流预算已耗尽(loops={loop_count}/{max_loops},"
            f"原因={item.get('escalation_reason', 'unknown')})。"
            "Controller 已停止自动作废/重派。"
            "只允许三选一并落盘:接受现状推进 / 缩小范围重派 / 关闭工作流。"
            "禁止再次派发同范围 fix task。\n"
        )

    if suggested_branch:
        onto_flag = f"--onto {suggested_branch} "
        branch_line = suggested_branch
    else:
        onto_flag = ""
        branch_line = "(未找到,请自行确认 retry_node 最近 committed 任务的分支)"

    docs_block = shared_docs_block(
        workflow_id,
        retry_node,
        related_nodes=(gate_stage,),
        project_ctx=project_for_workflow(workflow_id) or {},
    )

    return f"""
HERDR_CONTROLLER_FIX_LOOP_EVENT

workflow_id: {workflow_id}
project_name: {project_name}
gate_stage: {gate_stage} — 验收结论 blocked
retry_node: {retry_node}
suggested_branch: {branch_line}
loop_count: {loop_count}/{max_loops}
{escalation}
Controller 已自动作废受影响的 gate 与下游 Task(共 {len(invalidated)} 个,见 Task Registry);
fix 完成后 DAG 将自动按 test → review → wrapup 顺序重新推进,旧 verdict 一并作废。

Blocker 清单(blocked 结论与修复指引):
{blockers_text}

默认继续 retry_node 的原 Task：Controller 已对可继续的任务执行 rework，复用原 Pane。
请先检查 Task Registry；已有 rework 时等待修复完成，不再 launch。
只有原任务已经终结、无法继续时，才按明确理由派发替代任务（计入累计配额）：

~/HAFlow/bin/herdr-task launch --workflow-id {workflow_id} --stage {retry_node} \\
  {onto_flag}--agent auto --task-type fix --integration-mode git \\
  --execution-id {workflow_id} \\
  --goal "修复 gate {gate_stage} 的阻断项" \\
  --acceptance "<逐条对应 Blocker 清单>" \\
  --prompt "<blocker 详情、修复范围与验证方式>"

fix 产出必须落分支("--integration-mode git"),否则测试仍测旧候选而恒 blocked。
如需再次修复，优先 herdr-task rework <task_id> --prompt "<阻断项>"。
终结任务例外替换需 --supersedes <task_id> --supersede-reason "<无法继续的具体理由>"。
派发完成后结束当前回合,后续推进交给 Controller。

{docs_block}

{COORDINATOR_DISCIPLINE}
""".strip()


def _handle_fix_loop_item(item):
    """门禁 blocked 的回流通知:作废已由 handle_fix_loop 原子完成,
    这里只负责把 blocker 清单与修复派发指引送到总指挥。"""
    if "rework_requests" not in item:
        affected = set(item.get("invalidated") or [])
        item["rework_requests"] = {t["task_id"]: t["rework_request_id"] for t in load_tasks()
                                  if t.get("workflow_id") == item["workflow_id"]
                                  and t.get("task_id") in affected
                                  and t.get("rework_request_id")}
    workflow_id = item["workflow_id"]
    coord_pane = coordinator_pane_for_workflow(workflow_id)

    if not coord_pane:
        print(
            f"[FIX LOOP SKIP] "
            f"no coordinator pane for workflow={workflow_id}"
        )
        return

    gate_stage = item["gate_stage"]
    retry_node = item["retry_node"]
    project_ctx = project_for_workflow(workflow_id) or {}

    message = build_fix_loop_message(
        item,
        project_ctx.get("project_name", "unknown"),
    )

    waited = 0

    while coordinator_status(workflow_id) not in ("idle", "done"):
        if waited >= 120:
            import json as _json

            from herdr import fix_loop as fix_loop_core

            summary = fix_loop_core.summarize_fix_loop_item(item)
            episode_key = f"{workflow_id}:fix_loop:{gate_stage}"
            episode = attention_get(episode_key) or {}
            try:
                attempts = int(episode.get("attempts") or 0) + 1
            except (TypeError, ValueError):
                attempts = 1
            attention_note(
                episode_key,
                {"task_id": f"fix_loop:{gate_stage}:{retry_node}",
                 "workflow_id": workflow_id},
                "fix_loop",
                reason="coordinator_busy",
                attempts=attempts,
                next_retry_at=(
                    time.time() + liveness.attention_retry_interval()
                ),
                detail=_json.dumps(summary, ensure_ascii=False),
            )
            print(
                f"[FIX LOOP WAIT TIMEOUT] "
                f"workflow={workflow_id} "
                f"-> persisted for redelivery "
                f"(attempts={attempts})"
            )
            return

        time.sleep(2)
        waited += 2

    result = subprocess.run(
        [
            "herdr",
            "agent",
            "prompt",
            coord_pane,
            message,
            "--wait",
            "--timeout",
            "600000"
        ],
        text=True,
        capture_output=True
    )

    if result.returncode == 0:
        attention_clear(f"{workflow_id}:fix_loop:{gate_stage}")
        print(
            f"[FIX LOOP NOTIFIED] "
            f"workflow={workflow_id} "
            f"gate={gate_stage} "
            f"retry={retry_node}"
        )

        maybe_compact_coordinator(
            workflow_id,
            reason=f"fix_loop:{gate_stage}->{retry_node}",
        )
    else:
        print(
            f"[FIX LOOP ERROR] "
            f"workflow={workflow_id}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def _handle_coordinator_item(item):
    """Actual item handling logic (stage_advance or normal task event)."""
    # ==============================================
    # Fix Loop (gate verdict blocked)
    # ==============================================
    if item.get("kind") == "workflow_continuation":
        handle_workflow_continuation(item)
        return

    if item.get("kind") == "fix_loop":
        _handle_fix_loop_item(item)
        return

    # ==============================================
    # Workflow Stage Advance
    # ==============================================
    if item.get("kind") == "stage_advance":
        dispatch_op = None
        dispatch_owner = None
        node = item.get("node")
        target_node_id = item.get("node_id") or item.get("next_stage")
        if not node:
            config = workflow_config_for(item["workflow_id"])
            if config:
                node = find_node(config, target_node_id)
        if node:
            item = dict(item, node=node)
            if node.get("node_type", "agent") != "agent":
                print(
                    f"[STAGE ADVANCE BLOCKED] workflow={item['workflow_id']} "
                    f"node={target_node_id} node_type={node.get('node_type')}: "
                    "native executor unavailable; manual handling required"
                )
                return
        if item.get('stage') in (None, '', 'start') and not coordinator_intake_enabled():
            if _get_store().get_workflow(item['workflow_id']):
                from herdr.node_dispatch_store import operation_for_node
                reconcile_node_dispatches(item['workflow_id'], discover=False)
                previous_dispatch = operation_for_node(_get_store().db_path, item['workflow_id'], target_node_id)
                if previous_dispatch and previous_dispatch['status'] not in ('resolved', 'superseded'):
                    return
        # 总指挥接单:新工作流首个节点(start -> first)默认交总指挥理解
        # 需求后再派发;HERDR_COORDINATOR_INTAKE=0 或非首节点保持直派。
        if (
            item.get("stage") in (None, "", "start")
            and coordinator_intake_enabled()
        ):
            item = dict(item, intake=True)
            if _get_store().get_workflow(item['workflow_id']):
                from herdr.node_dispatch_store import operation_for_node
                reconcile_node_dispatches(item['workflow_id'])
                dispatch_op = operation_for_node(_get_store().db_path, item['workflow_id'], target_node_id)
                if not dispatch_op:
                    print(f"[STAGE DISPATCH DROP] workflow={item['workflow_id']} node={target_node_id}: no current obligation")
                    return
                if item.get('dispatch_operation_id') is not None:
                    if (item['dispatch_operation_id'] != dispatch_op['id']
                            or item.get('dispatch_owner') != dispatch_op['owner']
                            or dispatch_op['status'] != 'running' or dispatch_op['started']):
                        return
                    dispatch_owner = item['dispatch_owner']
                elif dispatch_op['status'] != 'pending' or dispatch_op['started']:
                    return
                pinned = _get_store().get_workflow(item['workflow_id'])['config']
                node = find_node(pinned, target_node_id)
                item = dict(item, node=node)
            print(
                f"[COORDINATOR INTAKE] "
                f"workflow={item['workflow_id']} "
                f"node={target_node_id} "
                "-> dispatch via coordinator"
            )
        # 常规推进会:优先规则化直接派发(不再等待总指挥 LLM 回合);
        # 配置不足/需求缺失/launch 失败时回落既有总指挥路径。
        elif try_direct_stage_advance(item):
            return

        workflow_id = item["workflow_id"]
        coord_pane = coordinator_pane_for_workflow(workflow_id)
        if not coord_pane:
            if dispatch_op:
                from herdr.node_dispatch_store import defer
                defer(_get_store().db_path, dispatch_op['id'], 'coordinator_missing', owner=dispatch_owner)
                clear_stage_advance(workflow_id, target_node_id)
            print(
                f"[STAGE ADVANCE SKIP] "
                f"no coordinator pane for workflow={workflow_id}"
            )
            return

        stage = item["stage"]
        next_stage = item["next_stage"]
        target_node_id = item.get("node_id") or next_stage

        project_ctx = project_for_workflow(
            workflow_id
        ) or {}

        project_name = project_ctx.get(
            "project_name",
            "legacy/unknown"
        )

        project_root = project_ctx.get(
            "project_root",
            ""
        )

        base_branch = project_ctx.get(
            "base_branch",
            ""
        )

        node = item.get("node")
        if not node:
            wf_cfg = workflow_config_for(workflow_id)
            if wf_cfg:
                node = find_node(wf_cfg, next_stage)

        policy = get_stage_policy(
            next_stage
        )

        if node:
            purpose = node.get("purpose") or policy.get("purpose", "未定义")
            integration_mode = node.get("default_integration_mode") or policy.get("default_integration_mode", "none")
            task_type = node.get("default_task_type") or policy.get("default_task_type", "feat")
            node_label = node.get("label", next_stage)
            node_type = node.get("node_type", "agent")
            agent_policy = node.get("agent_policy", {})
            req_outs = node.get("required_outputs") or policy.get("required_outputs", [])
            rules_list = node.get("rules") or policy.get("rules", [])
        else:
            purpose = policy.get("purpose", "未定义")
            integration_mode = policy.get("default_integration_mode", "none")
            task_type = policy.get("default_task_type", "test")
            node_label = item.get("stage_label", next_stage)
            node_type = "agent"
            agent_policy = {}
            req_outs = policy.get("required_outputs", [])
            rules_list = policy.get("rules", [])

        required_outputs = "\n".join(
            f"- {out}" for out in req_outs
        ) or "- 未定义"

        rules = "\n".join(
            f"- {r}" for r in rules_list
        ) or "- 未定义"

        agent_policy_text = ""
        if agent_policy:
            pref = ", ".join(agent_policy.get("preferred", [])) or "无"
            exc = ", ".join(agent_policy.get("exclude", [])) or "无"
            fix = agent_policy.get("fixed") or "无"
            agent_policy_text = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Node Agent 策略
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 优先 Agent: {pref}
- 排除 Agent: {exc}
- 固定 Agent: {fix}
""".strip()

        wait_started = time.time()

        preserved_ids = [
            task["task_id"]
            for task in load_tasks()
            if task.get("workflow_id") == workflow_id
            and next_stage in (task.get("node"), task.get("stage"))
            and task.get("status") != "superseded"
            and task.get("stage_verdict") == "pass"
        ]
        preserved_text = ""
        if preserved_ids:
            preserved_text = (
                "\n已保留的有效任务（verdict=pass，禁止重复创建）：\n"
                + "\n".join(f"- {tid}" for tid in preserved_ids)
                + "\n只需补派缺失/被作废的 Task。\n"
            )

        # Candidate identity for the coordinator path. Direct dispatch binds
        # the frozen candidate through --candidate-sha/--onto; when it falls
        # back here that binding must be passed through verbatim, otherwise
        # the coordinator re-derives a revision and the same scheduler
        # decision gets two execution semantics.
        frozen_sha, frozen_branch = _scheduler_frozen_candidate_identity(
            workflow_id, project_ctx, (node or {}).get("depends_on") or [],
        )
        candidate_flags = ""
        candidate_block = ""
        if frozen_sha:
            onto_line = (f"   --onto {frozen_branch}\n"
                         if frozen_branch and next_stage not in ("test", "review") else "")
            candidate_flags = (
                f"{onto_line}   --candidate-sha {frozen_sha}"
            )
            candidate_block = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
候选身份（调度器已冻结，必须原样透传）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

candidate_sha: {frozen_sha}
candidate_branch: {frozen_branch or '（未记录）'}

创建 Task 时必须原样携带以上候选身份，
不得重新推断当前分支或版本。
验收节点（test/review）必须验证该冻结版本；
版本不符时门禁 fail-closed 拒绝合并。
""".strip()

        try:
            while True:
                # Re-validate on every wait iteration: the workflow may be
                # closed or deregistered while this event sat in the queue.
                if workflow_closed(workflow_id) or not project_for_workflow(workflow_id):
                    print(
                        f"[STAGE ADVANCE DROP] "
                        f"workflow={workflow_id} "
                        f"closed/deregistered while queued"
                    )
                    return

                startup_record = project_for_workflow(workflow_id) or {}
                if startup_record.get("startup_ready") is False:
                    print(
                        f"[STARTUP WAIT] workflow={workflow_id} "
                        "queued event held until request is ready"
                    )
                    time.sleep(1)
                    continue

                status = coordinator_status(workflow_id)

                if status in ("idle", "done"):
                    intake = bool(item.get("intake"))
                    event_header = (
                        "HERDR_WORKFLOW_INTAKE_EVENT"
                        if intake
                        else "HERDR_STAGE_ADVANCE_EVENT"
                    )
                    intake_note = (
                        "这是新工作流的接单(总指挥接单机制):\n"
                        "请先完整阅读下方用户需求,理解目标、范围与约束,\n"
                        "再严格按节点职责创建第一个节点的 Task。\n\n"
                        if intake
                        else ""
                    )
                    docs_block = shared_docs_block(
                        workflow_id,
                        next_stage,
                        related_nodes=(node or {}).get("depends_on") or [],
                        project_ctx=project_ctx,
                    )
                    message = f"""
{event_header}

{intake_note}workflow_id: {workflow_id}
project_name: {project_name}
project_root: {project_root}
base_branch: {base_branch}
completed_node: {stage}
next_node: {next_stage} ({node_label})
node_type: {node_type}

用户需求：
{project_ctx.get('requirement', '').strip() or '（未提供；请停止并等待需求正文）'}

当前工作流前置依赖已全部完成。
{preserved_text}
现在进入下一节点：

{next_stage} ({node_label})

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
节点职责
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{purpose}

默认配置：

integration_mode:
{integration_mode}

task_type:
{task_type}

必须产出：

{required_outputs}

执行规则：

{rules}

{docs_block}

{agent_policy_text}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
执行要求
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. 首先执行：

   ~/HAFlow/bin/herdr-task list --workflow-id {workflow_id}

   阅读当前 Workflow 已完成节点的真实成果。

2. 根据前面节点的实际成果，
   决定当前节点需要创建几个 Task。

3. 不要固定前端、后端、数据库等角色。

   Pane = Task。

   Agent 根据 Task 动态选择。

4. 每个 Task 必须明确：

   - task_id
   - goal
   - acceptance criteria
   - agent
   - task_type
   - integration_mode

5. 创建 Task 必须使用：

   ~/HAFlow/bin/herdr-task launch

   并指定：

   --workflow-id {workflow_id}
   --node {next_stage}
   --source {project_root}
{candidate_flags}
{('   --dispatch-operation-id ' + str(dispatch_op['id'])) if dispatch_op else ''}
{('   替代派发：每个前序任务各建一个替代Task，分别携带 --supersedes ' + ', --supersedes '.join(p['task_id'] for p in dispatch_op['payload'].get('predecessors', []))) if dispatch_op and dispatch_op['payload'].get('predecessors') else ''}

6. 默认使用本节点 policy：

   task_type={task_type}
   integration_mode={integration_mode}

   只有当前 Task 的真实性质明确需要不同配置时，
   才允许调整。

7. 不允许手工创建：

   - Clone
   - Pane
   - Branch
   - Agent

8. 可以创建一个 Task，
   也可以创建多个并行 Task。

   数量由实际工作决定。

{candidate_block}

9. 当前节点所有必要 Task 派发完成后，
   结束当前回合。

10. 后续执行、验收、返工、节点推进，
继续交给 Controller。

不要等待用户提醒。

{COORDINATOR_DISCIPLINE}
""".strip()

                    if dispatch_op:
                        from herdr.node_dispatch_store import claim, start, transport_finished
                        import uuid
                        if dispatch_owner is None:
                            dispatch_owner = uuid.uuid4().hex
                            claimed = claim(_get_store().db_path, dispatch_op['id'], dispatch_owner)
                            if claimed is None:
                                return
                        if start(_get_store().db_path, dispatch_op['id'], dispatch_owner) is None:
                            clear_stage_advance(workflow_id, target_node_id)
                            return
                    try:
                        result = subprocess.run(
                            ["herdr", "agent", "prompt", coord_pane, message,
                             "--wait", "--timeout", "600000"],
                            text=True, capture_output=True, timeout=600,
                        )
                    except Exception:
                        if not dispatch_op:
                            raise
                        result = None
                    if dispatch_op:
                        receipt = transport_finished(_get_store().db_path, dispatch_op['id'], dispatch_owner,
                            reason='dispatch_awaiting_task' if result and result.returncode == 0 else 'dispatch_delivery_unknown')
                        mark_stage_advance_notified(workflow_id, target_node_id)
                        if receipt['status'] == 'resolved':
                            print(f"[STAGE ADVANCED] workflow={workflow_id} {stage} -> {next_stage}")
                            maybe_compact_coordinator(workflow_id, reason=f"stage_advance:{next_stage}")
                        else:
                            print(f"[STAGE DISPATCH AWAITING_TASK] workflow={workflow_id} "
                                  f"node={target_node_id} operation={receipt['id']} "
                                  f"status={receipt['status']} deadline={receipt['detail'].get('deadline_at')}")
                    elif result.returncode == 0:
                        mark_stage_advance_notified(
                            workflow_id,
                            target_node_id
                        )

                        print(
                            f"[STAGE ADVANCED] "
                            f"workflow={workflow_id} "
                            f"{stage} -> {next_stage}"
                        )

                        maybe_compact_coordinator(
                            workflow_id,
                            reason=f"stage_advance:{next_stage}",
                        )
                    else:
                        clear_stage_advance(
                            workflow_id,
                            target_node_id
                        )

                        print(
                            f"[STAGE ADVANCE ERROR] "
                            f"workflow={workflow_id}: "
                            f"{result.stderr.strip() or result.stdout.strip()}"
                        )

                    break

                elapsed = time.time() - wait_started
                if elapsed >= liveness.stage_advance_sla():
                    if dispatch_op:
                        from herdr.node_dispatch_store import defer
                        defer(_get_store().db_path, dispatch_op['id'], 'coordinator_busy', owner=dispatch_owner)
                    # 有界等待:总指挥长期不可用(僵尸 pane / 状态僵死)时,
                    # 释放阶段闩并记录 attention,绝不占用 workflow 调度锁空转。
                    clear_stage_advance(
                        workflow_id,
                        target_node_id
                    )
                    key = f"{workflow_id}:stage_advance:{target_node_id}"
                    episode = attention_get(key) or {}
                    attempts = int(episode.get("attempts") or 0) + 1
                    attention_note(
                        key,
                        {"task_id": f"stage_advance:{target_node_id}", "workflow_id": workflow_id},
                        "stage_advance",
                        reason="coordinator_stalled",
                        attempts=attempts,
                        next_retry_at=time.time() + liveness.attention_retry_interval(),
                        detail=f"coordinator status={status} waited={int(elapsed)}s",
                    )
                    print(
                        f"[STAGE ADVANCE STALLED] "
                        f"workflow={workflow_id} "
                        f"{stage} -> {next_stage} "
                        f"coordinator={status} waited={int(elapsed)}s "
                        f"attempts={attempts} -> deferred"
                    )
                    notify_attention(
                        "Herdr Factory · 总指挥停滞",
                        {"task_id": f"stage_advance:{target_node_id}", "workflow_id": workflow_id},
                        f"阶段 {stage} -> {next_stage} 等待总指挥超过 {int(elapsed)}s，已延迟重试。请检查总指挥 pane 状态。",
                        "stage_advance_stalled",
                    )
                    return

                print(
                    f"[STAGE ADVANCE WAIT] "
                    f"coordinator={status} "
                    f"workflow={workflow_id}"
                )

                time.sleep(1)

        finally:
            pass  # task_done is called by coordinator_worker dispatcher

        return

    # ==============================================
    # Normal Task Event
    # ==============================================

    task_id = item["task_id"]
    event_type = item["event_type"]
    key = item["key"]

    if event_type == "attention":
        # attention 事件针对 interrupted/paused 等需要裁决的中间态,
        # 只要任务仍停留在待裁决状态就有效。
        expected_status = None
    elif event_type in {"blocked", "inner_loop_exhausted"}:
        expected_status = "blocked"
    else:
        expected_status = "agent_done"

    try:
        last_busy_log = 0
        wait_started = time.time()

        # 规则化验收快路径:非门禁节点铁证齐备直接 completed,
        # 门禁节点有唯一结论标记则直接落 verdict;两者都不命中才回落总指挥。
        if event_type == "done" and (
            try_auto_accept(task_id) or try_auto_verdict(task_id)
        ):
            attention_clear(key)
            finalize_completed_task(task_id)
            return

        while True:
            task = get_task(task_id)

            if not task:
                print(
                    f"[QUEUE DROP] "
                    f"task={task_id} missing"
                )
                break

            current_task_status = task.get("status")

            if event_type == "attention" and current_task_status not in (
                "interrupted",
                "paused",
            ):
                print(
                    f"[QUEUE STALE] "
                    f"task={task_id} "
                    f"attention resolved actual={current_task_status}"
                )
                attention_clear(key)
                break

            # 事件在等待期间已经失效
            if expected_status is not None and current_task_status != expected_status:
                print(
                    f"[QUEUE STALE] "
                    f"task={task_id} "
                    f"expected={expected_status} "
                    f"actual={current_task_status}"
                )
                break

            if event_type in {"blocked", "inner_loop_exhausted"} and (
                item.get("blocker_episode") != blocker_queue_episode(task)
                or blocked_event_type(task) != event_type
            ):
                print(f"[QUEUE STALE] task={task_id} blocker episode changed")
                # Legacy metadata updates cannot prove episode continuity.
                # Drop old authority, but retain reachability of the current
                # persisted blocker through the same episode-aware queue.
                enqueue_coordinator_event(task, blocked_event_type(task))
                break

            status = coordinator_status(task.get("workflow_id"))

            if status in ("idle", "done"):
                message = build_coordinator_message(
                    task,
                    event_type
                )

                print(
                    f"[COORDINATOR READY] "
                    f"task={task_id} "
                    f"event={event_type}"
                )

                if event_type in {"blocked", "inner_loop_exhausted"}:
                    current = get_task(task_id)
                    if (
                        not current or current.get("status") != "blocked"
                        or item.get("blocker_episode") != blocker_queue_episode(current)
                        or blocked_event_type(current) != event_type
                    ):
                        print(f"[QUEUE STALE] task={task_id} changed before prompt")
                        if current and current.get("status") == "blocked":
                            enqueue_coordinator_event(current, blocked_event_type(current))
                        break

                result = subprocess.run(
                    [
                        "herdr",
                        "agent",
                        "prompt",
                        coordinator_pane_for_workflow(task.get("workflow_id")),
                        message,
                        "--wait",
                        "--timeout",
                        "600000"
                    ],
                    text=True,
                    capture_output=True
                )

                if result.returncode == 0:
                    attention_clear(key)

                    print(
                        f"[COORDINATOR NOTIFIED] "
                        f"task={task_id} "
                        f"event={event_type}"
                    )

                    # done 事件经过总指挥正式验收后，
                    # 根据 integration_mode 自动集成并清理。
                    if event_type == "done":
                        decision = wait_for_coordinator_decision(
                            task_id
                        )

                        # 第一次没有形成决策时，只自动重试一次。
                        if decision == "agent_done":
                            decision = retry_coordinator_decision(
                                task_id
                            )

                        if decision == "completed":
                            finalize_completed_task(
                                task_id
                            )

                        elif decision == "rework":
                            print(
                                f"[FINALIZE DEFER] "
                                f"task={task_id} "
                                f"status=rework"
                            )

                        elif decision == "failed":
                            print(
                                f"[FINALIZE STOP] "
                                f"task={task_id} "
                                f"status=failed"
                            )

                        else:
                            print(
                                f"[FINALIZE WAIT] "
                                f"task={task_id} "
                                f"status={decision}"
                            )
                else:
                    # 投递失败(如 agent_prompt_stalled):记录退避节流,
                    # 由 registry watcher 在退避窗口后按需重试,杜绝 1s 级风暴。
                    episode = attention_get(key) or {}
                    attempts = int(episode.get("attempts") or 0) + 1
                    delay = liveness.backoff_delay(attempts)
                    attention_note(
                        key,
                        task,
                        event_type,
                        reason="delivery_failed",
                        attempts=attempts,
                        next_retry_at=time.time() + delay,
                        detail=(result.stderr.strip() or result.stdout.strip())[:400],
                    )

                    print(
                        "[COORDINATOR ERROR]",
                        result.stderr.strip()
                        or result.stdout.strip(),
                        f"-> retry_in={int(delay)}s attempts={attempts}"
                    )

                break

            now = time.time()

            elapsed = now - wait_started
            if elapsed >= liveness.coordinator_delivery_sla():
                handle_coordinator_delivery_stall(
                    item, task, elapsed
                )
                break

            if now - last_busy_log >= 15:
                last_busy_log = now
                print(
                    f"[COORDINATOR BUSY] "
                    f"status={status} "
                    f"task={task_id} "
                    f"waited={int(elapsed)}s"
                )

            time.sleep(2)

    finally:
        with lock:
            queued_events.discard(item.get("queue_key", key))


# ============================================================
# 规则化验收 (auto-accept) — 非门禁节点免除总指挥 LLM 回合
# ============================================================

# 背景(lessons §61)：agent_done 后的总指挥验收回合实测占用 2.6h/8h。非门禁
# 节点(需求/计划/实现)的验收标准本就是"产物落盘 + 变更受控"，可由
# verify-baseline 铁证直接判定；门禁节点(test/review/wrapup)保留裁决语义。
def auto_accept_enabled():
    value = os.environ.get("HERDR_AUTO_ACCEPT", "1")
    return value.strip().lower() not in ("0", "false", "off", "no")


def node_is_gate(workflow_id, node_id):
    """节点是否带门禁配置(含 GATE_DEFAULTS 的 test/review/wrapup)。

    配置不可判定时保守返回 True(保留总指挥路径),绝不因配置异常
    误吞门禁裁决。
    """
    try:
        wf_cfg = workflow_config_for(workflow_id) or {}
        nodes_by_id = {n["id"]: n for n in wf_cfg.get("nodes", [])}
        return resolve_gate_config(nodes_by_id.get(node_id), node_id) is not None
    except Exception:
        return True


def task_changes_recorded(task):
    """verify-baseline 铁证：至少一个受控文件变更(TASK_CHANGED)。"""
    task_id = task.get("task_id")
    if not task_id:
        return False
    try:
        result = subprocess.run(
            [TASK_MANAGER, "verify-baseline", task_id],
            text=True,
            capture_output=True,
            timeout=60,
        )
    except Exception:
        return False

    if result.returncode != 0:
        return False
    output = result.stdout or ""
    for line in output.splitlines():
        if line.startswith("HERDR_BASELINE_RESULT="):
            try:
                payload = json.loads(line.split("=", 1)[1])
            except (ValueError, IndexError):
                continue
            return bool(payload.get("changes"))
    return "TASK_CHANGED" in output


def try_auto_accept(task_id):
    """非门禁节点规则化验收：变更铁证齐备即 completed，免总指挥回合。

    保守口径：只接管 agent_done 的非门禁节点；产物/变更证据不足、
    门禁节点、被显式关闭(HERDR_AUTO_ACCEPT=0)时返回 False，走原路径。
    """
    if not auto_accept_enabled():
        return False

    task = get_task(task_id)
    if not task or task.get("status") != "agent_done":
        return False

    workflow_id = task.get("workflow_id")
    node_id = task.get("node") or task.get("stage")
    if not workflow_id or not node_id:
        return False

    if node_is_gate(workflow_id, node_id):
        return False
    if task.get("stage_verdict") == "blocked":
        return False
    # Review roles may share a requirements/plan node with its author.
    # Legacy task IDs also carry the role when dispatch_role was not recorded.
    role_text = " ".join(str(task.get(key) or "").lower() for key in
                         ("dispatch_role", "agent_role", "role", "task_id"))
    if re.search(r"(?:^|[^a-z])(review(?:er)?|adversarial)(?:$|[^a-z])", role_text):
        return False

    if not task_changes_recorded(task):
        return False

    if not set_task_status(
        task_id, "completed", expected_status="agent_done",
        expected_version=_task_version(task), source="herdr-controller:auto-accept",
        metadata={"auto_accept_reason": "controlled_changes", "acceptance_mode": "auto"},
    ):
        return False

    print(
        f"[AUTO ACCEPT] task={task_id} "
        f"node={node_id} baseline=TASK_CHANGED -> completed"
    )
    return True


# ============================================================
# 门禁规则化裁决 (auto-verdict) — 门禁节点免除总指挥 LLM 回合
# ============================================================

# 契约：门禁任务写入门禁结论文件，并在终端输出 HERDR_GATE_VERDICT: pass|blocked
# （可选 HERDR_GATE_NOTE: <原因>）。结论文件默认落在 clone 外状态目录
# （~/.herdr-controller/gate-verdicts/<task_id>.json），避免被 herdr-task commit
# 带进交付；为兼容旧契约与权限受限场景，clone 内 .herdr/gate-verdict.json 仍可读。
# 两个通道结论一致时直接落 verdict；缺失或互相矛盾一律回落总指挥。
GATE_VERDICT_FILE = ".herdr/gate-verdict.json"
GATE_VERDICT_MARKER = "HERDR_GATE_VERDICT:"
GATE_NOTE_MARKER = "HERDR_GATE_NOTE:"

_GATE_VERDICT_ALIASES = {
    "pass": "pass",
    "passed": "pass",
    "ok": "pass",
    "blocked": "blocked",
    "block": "blocked",
    "fail": "blocked",
    "failed": "blocked",
}


def auto_verdict_enabled():
    value = os.environ.get("HERDR_AUTO_VERDICT", "1")
    return value.strip().lower() not in ("0", "false", "off", "no")


def _normalize_gate_verdict(value):
    return _GATE_VERDICT_ALIASES.get(str(value or "").strip().lower())


def _gate_verdict_file_candidates(task):
    """结论文件候选路径：状态目录优先，clone 内旧契约路径兜底。"""
    candidates = []
    task_id = task.get("task_id")
    if task_id and direct_dispatch_planner is not None:
        try:
            candidates.append(direct_dispatch_planner.gate_verdict_path(task_id))
        except Exception:
            pass
    clone_path = task.get("clone_path")
    if clone_path:
        candidates.append(os.path.join(clone_path, GATE_VERDICT_FILE))
    return candidates


def _verdict_from_file(task):
    for path in _gate_verdict_file_candidates(task):
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        verdict = _normalize_gate_verdict(payload.get("verdict"))
        if verdict:
            return verdict, str(payload.get("note") or "").strip()
    return None, ""


def _verdict_affected_task_ids(task):
    """PR #110:仅从门禁结论文件读取 affected_task_ids(结构化字段)。

    屏幕输出是自由文本,绝不作为归因来源。取值与 _verdict_from_file
    选用同一文件(第一个给出合法 verdict 的候选)。字段缺失/非列表/
    含非字符串成员 → None(调用方不传 --affected-task-id,等价 legacy);
    显式空列表 → [](Verifier 表示无法归因,决策层据此 fallback)。
    """
    for path in _gate_verdict_file_candidates(task):
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if not _normalize_gate_verdict(payload.get("verdict")):
            continue
        raw = payload.get("affected_task_ids")
        if not isinstance(raw, list):
            return None
        ids = []
        for item in raw:
            text = str(item).strip() if isinstance(item, str) else ""
            if not text:
                return None
            if text not in ids:
                ids.append(text)
        return ids
    return None


def _is_instructional_or_ambiguous_verdict_line(line):
    """过滤指令模板、Prompt回显或歧义讨论行，防止误判为有效门禁结论。"""
    if not line:
        return True
    # 1. 单行出现多次标记(如 'HERDR_GATE_VERDICT: pass 或 HERDR_GATE_VERDICT: blocked')
    if line.count(GATE_VERDICT_MARKER) > 1:
        return True

    # 2. 含有二选一、条件词或模板占位符
    line_lower = line.lower()
    disjunctive_keywords = (
        "或", " or ", " / ", "二选一", "示例", "example", "格式", "template",
        "<pass", "[pass", "<blocked", "[blocked", "二者选一"
    )
    if any(k in line_lower for k in disjunctive_keywords):
        return True

    return False


def _verdict_from_screen(task):
    pane_id = task.get("pane_id")
    if not pane_id:
        return None, ""
    try:
        result = subprocess.run(
            ["herdr", "pane", "read", pane_id, "--source", "visible"],
            text=True,
            capture_output=True,
            timeout=10,
        )
    except Exception:
        return None, ""

    screen = (result.stdout or "") + "\n" + (result.stderr or "")
    verdicts = set()
    note = ""
    for line in screen.splitlines():
        if GATE_VERDICT_MARKER in line:
            if _is_instructional_or_ambiguous_verdict_line(line):
                continue
            raw = line.split(GATE_VERDICT_MARKER, 1)[1].strip()
            tokens = raw.split()
            if not tokens:
                continue
            first_token = tokens[0].strip().rstrip(".,;:")
            normalized = _normalize_gate_verdict(first_token)
            if normalized:
                # 检查第一词后续 token 中是否混有对立裁决词(如 'pass blocked' 等歧义讨论)
                if len(tokens) > 1:
                    trailing = {t.strip(".,;:()/[]{}").lower() for t in tokens[1:]}
                    conflicting = "blocked" if normalized == "pass" else "pass"
                    if conflicting in trailing:
                        continue
                verdicts.add(normalized)
        if GATE_NOTE_MARKER in line and not note:
            candidate_note = line.split(GATE_NOTE_MARKER, 1)[1].strip()
            # 过滤模板占位符说明如 'HERDR_GATE_NOTE: <原因>' 或 'HERDR_GATE_NOTE: 一句话结论'
            if candidate_note and not any(k in candidate_note for k in ("<原因>", "一句话结论", "阻塞原因清单", "note...")):
                note = candidate_note

    if len(verdicts) != 1:
        return None, ""
    return verdicts.pop(), note


def read_gate_verdict(task):
    """(verdict, note, source)：文件与终端两路信号一致才返回 verdict。"""
    file_verdict, file_note = _verdict_from_file(task)
    screen_verdict, screen_note = _verdict_from_screen(task)

    signals = {v for v in (file_verdict, screen_verdict) if v}
    if len(signals) != 1:
        return None, "", ""

    verdict = signals.pop()
    note = file_note or screen_note
    source = "file+screen" if file_verdict and screen_verdict else (
        "file" if file_verdict else "screen"
    )
    return verdict, note, source


def try_auto_verdict(task_id):
    """门禁节点规则化裁决：报告/终端给出唯一结论时直接落 verdict。

    pass -> 推进下一节点；blocked -> 既有 fix-loop 自动回流。
    信号缺失/冲突、非门禁节点、环境开关关闭时返回 False 回落总指挥。
    """
    if not auto_verdict_enabled():
        return False

    task = get_task(task_id)
    if not task or task.get("status") != "agent_done":
        return False

    workflow_id = task.get("workflow_id")
    node_id = task.get("node") or task.get("stage")
    if not workflow_id or not node_id:
        return False

    if not node_is_gate(workflow_id, node_id):
        return False

    verdict, note, source = read_gate_verdict(task)
    if verdict not in ("pass", "blocked"):
        return False
    if verdict == "blocked" and not note:
        note = f"gate verdict blocked (auto-verdict via {source or 'signal'})"

    set_cmd = [
        TASK_MANAGER, "set", task_id, "completed",
        "--verdict", verdict, "--note", note,
    ]
    if verdict == "blocked":
        # PR #110:结构化归因只认结论文件字段,屏幕文本绝不作为来源。
        affected_ids = _verdict_affected_task_ids(task)
        if affected_ids:
            for affected_id in affected_ids:
                set_cmd.extend(["--affected-task-id", affected_id])
    result = subprocess.run(set_cmd, text=True, capture_output=True)
    if result.returncode != 0:
        print(
            f"[AUTO VERDICT ERROR] task={task_id}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
        return False

    print(
        f"[AUTO VERDICT] task={task_id} node={node_id} "
        f"verdict={verdict} source={source}"
    )
    return True


def check_task_deliverables_ready(task):
    """Check whether deliverables required by the task or node are present and non-empty."""
    clone_path = task.get("clone_path")
    if not clone_path or not os.path.exists(clone_path):
        return False
    wf_id = task.get("workflow_id")
    node_id = task.get("node") or task.get("stage")
    required_outputs = []
    if wf_id and node_id:
        try:
            wf_cfg = workflow_config_for(wf_id)
            if wf_cfg:
                node = find_node(wf_cfg, node_id)
                if node:
                    required_outputs = node.get("required_outputs", [])
        except Exception:
            pass

    if required_outputs:
        for rel in required_outputs:
            p = os.path.join(clone_path, rel)
            if not os.path.exists(p) or os.path.getsize(p) == 0:
                return False
        return True

    # Fallback to checking git status for any changes
    try:
        res = subprocess.run(
            ["git", "-C", str(clone_path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=3
        )
        if res.stdout.strip():
            return True
    except Exception:
        pass
    return False


def gate_verdict_ready(task):
    """Return whether a gate task has a valid machine-readable verdict.

    Gate ``required_outputs`` are human-facing contract labels (for example,
    ``测试结论（PASS / FAIL）``), not repository-relative file paths. A valid
    verdict is therefore independent evidence that lets an idle gate task reach
    the normal ``agent_done`` -> auto-verdict/finalize flow.
    """
    workflow_id = task.get("workflow_id")
    node_id = task.get("node") or task.get("stage")
    if not workflow_id or not node_id:
        return False
    if not node_is_gate(workflow_id, node_id):
        return False
    verdict, _, _ = read_gate_verdict(task)
    return verdict in ("pass", "blocked")


def _supervisor_pane_report(task):
    """Agent done report source: bounded pane read, best-effort (never raises)."""
    pane_id = task.get("pane_id")
    if not pane_id:
        return None
    try:
        res = subprocess.run(
            ["herdr", "pane", "read", pane_id, "--source", "visible"],
            text=True,
            capture_output=True,
            timeout=3,
        )
        return (res.stdout or "") + "\n" + (res.stderr or "")
    except Exception:
        return None


def _supervisor_attention(task, decision, event_type):
    """Intervention ledger entry; task status is never touched here."""
    reasons = "; ".join((decision or {}).get("reasons") or ["policy intervention"])
    key = f"supervisor:{task.get('task_id')}"
    previous = attention_get(key) or {}
    note = attention_note(key, task, event_type, reasons)
    if previous.get("event_type") != event_type:
        _supervisor_notify(
            task,
            event_type.split("_")[-1],
            f"语义监督拦截: {reasons}",
        )
    return note


def _supervisor_notify(task, action, message):
    """Best-effort operator visibility for an enforced intervention."""
    if os.environ.get("HERDR_CONTROLLER_TEST"):
        return
    notify_attention(
        "Herdr Factory · 语义监督拦截",
        task,
        message,
        f"supervisor_{str(action).lower()}",
    )


def _supervisor_retry(task, decision, store=None):
    """RETRY -> the existing rework flow; return only observed state facts."""
    return _supervisor_retry_with_store(task, decision, store=store)


def _intervention_metadata(decision, action, attempt_count=None):
    intervention = (decision.get("intervention") or {}) if isinstance(decision, dict) else {}
    metadata = {
        "intervention_id": intervention.get("intervention_id"),
        "decision_id": intervention.get("decision_id") or decision.get("decision_id"),
        "action": action,
    }
    if attempt_count is not None:
        metadata["attempt_count"] = int(attempt_count)
    return {key: value for key, value in metadata.items() if value is not None}


def _supervisor_retry_with_store(task, decision, store=None):
    """Apply one new retry iteration using the existing legal task flow."""
    from herdr.intervention import attempt_count_for_task
    store = store or _get_store()
    fresh_before = store.get_task(task.get("task_id")) if store is not None else None
    fresh_before = fresh_before or task
    previous_status = fresh_before.get("status")
    current_attempt = attempt_count_for_task(fresh_before)
    intervention = (decision.get("intervention") or {}) if isinstance(decision, dict) else {}
    max_attempts = int(intervention.get("max_attempts") or 0)
    if max_attempts > 0 and current_attempt >= max_attempts:
        error = RuntimeError("RETRY budget exhausted")
        error.intervention_error = {
            "code": "retry_budget_exhausted",
            "attempt_count": current_attempt,
            "max_attempts": max_attempts,
        }
        raise error

    # A task already in rework is active work, not evidence that this new
    # Intervention ran. Re-enter the legal working path to start a new
    # iteration; never use rework -> rework as a fake retry.
    target_status = "working" if previous_status == "rework" else "rework"
    next_attempt = current_attempt + 1
    metadata = _intervention_metadata(decision, "RETRY", next_attempt)
    applied_status = target_status
    if store is not None and store.get_task(task.get("task_id")) is not None:
        from herdr import kernel
        kernel.transition_task(
            task_id=task.get("task_id"),
            to_status=target_status,
            reason="supervisor_retry",
            source="supervisor",
            metadata=metadata,
            store=store,
        )
    elif not set_task_status(task.get("task_id"), target_status):
        raise RuntimeError("RETRY handler could not enter retry flow")
    fresh = store.get_task(task.get("task_id")) if store is not None else None
    fresh = fresh or get_task(task.get("task_id")) or dict(fresh_before, status=applied_status)
    dispatch = _dispatch_supervisor_retry(fresh_before, decision, store)
    return {
        "action": "RETRY",
        "previous_status": previous_status,
        "new_status": fresh.get("status", target_status),
        "attempt_count": attempt_count_for_task(fresh),
        **dispatch,
    }


def _dispatch_supervisor_retry(task, decision, store):
    """Dispatch a real new retry iteration through the existing Agent path."""
    intervention = decision.get("intervention") or {}
    pane_id = task.get("pane_id")
    if not pane_id:
        raise RuntimeError("RETRY dispatch requires task pane_id")
    intervention_id = intervention.get("intervention_id")
    decision_id = intervention.get("decision_id") or decision.get("decision_id")
    from herdr.trajectory import run_id_for_task
    prior_intent = _latest_action_dispatch_intent(
        task, store, "RETRY", intervention_id=intervention_id,
    )
    if prior_intent is not None and task.get("completion_protocol") != "receipt-v1":
        payload = dict(prior_intent.get("payload") or {})
        payload["dispatch_recovered"] = True
        store.record_event(
            "retry_dispatched", payload,
            workflow_id=task.get("workflow_id"),
            node_id=task.get("node") or task.get("stage"),
            task_id=task.get("task_id"), agent_id=task.get("agent"),
            source="supervisor", run_id=run_id_for_task(task),
        )
        return {"retry_dispatched": True, "dispatch_recovered": True}
    payload = {
        "intervention_id": intervention_id,
        "decision_id": decision_id,
        "action": "RETRY",
        "pane_id": pane_id,
        "attempt_count": intervention.get("attempt"),
        "verification_pending": False,
    }
    working_context_ref = _working_context_ref_for_task(task, store=store)
    if working_context_ref:
        payload["working_context_id"] = working_context_ref
    if task.get("completion_protocol") != "receipt-v1":
        store.record_event(
            "retry_dispatch_intent", payload,
            workflow_id=task.get("workflow_id"),
            node_id=task.get("node") or task.get("stage"),
            task_id=task.get("task_id"), agent_id=task.get("agent"),
            source="supervisor", run_id=run_id_for_task(task),
        )

    from herdr.workflow_docs import cli_path
    import shlex
    artifact_task = shlex.quote(str(cli_path()))
    prompt = (
        "Supervisor requested a fresh retry iteration for this task.\n"
        "Continue the existing rework/fix loop now, run the configured work "
        "and verification steps, and only report completion after the new "
        "iteration has actually executed.\n"
        f"HERDR_RETRY_INTERVENTION_ID:{intervention_id}\n"
        f"HERDR_RETRY_DECISION_ID:{decision_id}\n"
        "HERDR_RETRY_ACTION:RETRY\n"
        + (f"WORKING_CONTEXT_REF:{working_context_ref}\n" if working_context_ref else "")
        + "Load the immutable context by reference before retrying.\n"
        + (f"{artifact_task} working-context get --context-id {working_context_ref}\n"
           if working_context_ref else "")
    )
    if task.get("completion_protocol") == "receipt-v1":
        from herdr.supervisor_delivery import deliver
        def send(pane, text):
            from herdr.supervisor_delivery import current_delivery_task
            pane = current_delivery_task(task, store, native=True)['pane_id']
            result = subprocess.run(
                ["herdr", "agent", "prompt", str(pane), text],
                text=True, capture_output=True, timeout=SUPERVISOR_VERIFY_DISPATCH_TIMEOUT,
            )
            if result.returncode != 0:
                raise RuntimeError("Supervisor native prompt transport failed")
        return deliver(task, store, 'RETRY', payload, prompt, send)
    result = subprocess.run(
        ["herdr", "agent", "prompt", str(pane_id), prompt],
        text=True, capture_output=True, timeout=SUPERVISOR_VERIFY_DISPATCH_TIMEOUT,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "retry prompt failed").strip()
        raise RuntimeError(f"RETRY dispatch failed: {detail[:400]}")
    store.record_event(
        "retry_dispatched", payload,
        workflow_id=task.get("workflow_id"),
        node_id=task.get("node") or task.get("stage"),
        task_id=task.get("task_id"), agent_id=task.get("agent"),
        source="supervisor", run_id=run_id_for_task(task),
    )
    return {"retry_dispatched": True}


def _supervisor_verify(task, decision, store=None):
    """VERIFY -> re-enter the existing verification/rework route.

    Verification facts are emitted later by the existing tests_completed and
    verification_completed path; this handler never manufactures a verdict.
    """
    store = store or _get_store()
    fresh_before = store.get_task(task.get("task_id")) if store is not None else None
    fresh_before = fresh_before or task
    previous_status = fresh_before.get("status")
    applied_status = previous_status
    if previous_status != "rework":
        metadata = _intervention_metadata(decision, "VERIFY")
        if store is not None and store.get_task(task.get("task_id")) is not None:
            from herdr import kernel
            kernel.transition_task(
                task_id=task.get("task_id"),
                to_status="rework",
                reason="supervisor_verify",
                source="supervisor",
                metadata=metadata,
                store=store,
            )
        elif not set_task_status(task.get("task_id"), "rework"):
            raise RuntimeError("VERIFY handler could not enter verification rework")
        else:
            applied_status = "rework"
    dispatch = _dispatch_supervisor_verification(fresh_before, decision, store)
    fresh = store.get_task(task.get("task_id")) if store is not None else None
    fresh = fresh or get_task(task.get("task_id")) or dict(fresh_before, status=applied_status)
    return {
        "action": "VERIFY",
        "previous_status": previous_status,
        "new_status": fresh.get("status"),
        **dispatch,
        "verification_requested": True,
        "verification_pending": True,
    }


def _working_context_ref_for_task(task, store=None):
    try:
        from herdr.context_compiler import compile_working_context, infer_agent_role
        context = compile_working_context(
            workflow_id=task.get("workflow_id"),
            task_id=task.get("task_id"),
            agent_role=infer_agent_role(task),
            store=store,
        )
        return context.context_id
    except Exception as exc:
        print(f"[WORKING_CONTEXT SKIPPED] task={task.get('task_id')}: {type(exc).__name__}")
        return None


def _dispatch_supervisor_verification(task, decision, store):
    """Submit one real verification prompt through the existing Agent path."""
    intervention = decision.get("intervention") or {}
    pane_id = task.get("pane_id")
    if not pane_id:
        raise RuntimeError("VERIFY dispatch requires task pane_id")
    intervention_id = intervention.get("intervention_id")
    decision_id = intervention.get("decision_id") or decision.get("decision_id")
    from herdr.trajectory import run_id_for_task
    prior_intent = _latest_verification_dispatch_intent(
        task, store, intervention_id=intervention_id,
    )
    if prior_intent is not None and task.get("completion_protocol") != "receipt-v1":
        payload = dict(prior_intent.get("payload") or {})
        payload["dispatch_recovered"] = True
        store.record_event(
            "verification_dispatched",
            payload,
            workflow_id=task.get("workflow_id"),
            node_id=task.get("node") or task.get("stage"),
            task_id=task.get("task_id"),
            agent_id=task.get("agent"),
            source="supervisor",
            run_id=run_id_for_task(task),
        )
        return {"verification_dispatched": True, "dispatch_recovered": True}
    dispatch_started_at = time.time()
    evidence_baseline = _verification_snapshot(task.get("clone_path"))
    dispatch_payload = {
        "intervention_id": intervention_id,
        "decision_id": decision_id,
        "action": "VERIFY",
        "pane_id": pane_id,
        "dispatch_started_at": dispatch_started_at,
        "evidence_baseline": evidence_baseline,
        "verification_pending": True,
    }
    working_context_ref = _working_context_ref_for_task(task, store=store)
    if working_context_ref:
        dispatch_payload["working_context_id"] = working_context_ref
    if task.get("completion_protocol") != "receipt-v1":
        store.record_event(
            "verification_dispatch_intent",
            dispatch_payload,
            workflow_id=task.get("workflow_id"),
            node_id=task.get("node") or task.get("stage"),
            task_id=task.get("task_id"),
            agent_id=task.get("agent"),
            source="supervisor",
            run_id=run_id_for_task(task),
        )

    from herdr.workflow_docs import cli_path
    import shlex
    artifact_task = shlex.quote(str(cli_path()))
    artifact_loop = shlex.quote(str(cli_path().with_name('herdr-loop')))
    prompt = (
        "Supervisor requested a fresh verification execution for this task.\n"
        "Run the existing project verification/test loop now (including "
        f"{artifact_loop} eval when configured), inspect the resulting "
        ".herdr-loop/EVAL_DONE.json, and only report completion after the new "
        "verification evidence is written.\n"
        f"HERDR_VERIFY_INTERVENTION_ID:{intervention_id}\n"
        f"HERDR_VERIFY_DECISION_ID:{decision_id}\n"
        "HERDR_VERIFY_ACTION:VERIFY\n"
        + (f"WORKING_CONTEXT_REF:{working_context_ref}\n" if working_context_ref else "")
        + "Load the immutable context by reference before verification.\n"
        + (f"{artifact_task} working-context get --context-id {working_context_ref}\n"
           if working_context_ref else "")
    )
    if task.get("completion_protocol") == "receipt-v1":
        from herdr.supervisor_delivery import deliver
        def send(pane, text):
            from herdr.supervisor_delivery import current_delivery_task
            pane = current_delivery_task(task, store, native=True)['pane_id']
            result = subprocess.run(
                ["herdr", "agent", "prompt", str(pane), text],
                text=True, capture_output=True, timeout=SUPERVISOR_VERIFY_DISPATCH_TIMEOUT,
            )
            if result.returncode != 0:
                raise RuntimeError("Supervisor native prompt transport failed")
        return deliver(task, store, 'VERIFY', dispatch_payload, prompt, send)
    result = subprocess.run(
        ["herdr", "agent", "prompt", str(pane_id), prompt],
        text=True,
        capture_output=True,
        timeout=SUPERVISOR_VERIFY_DISPATCH_TIMEOUT,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "verification prompt failed").strip()
        raise RuntimeError(f"VERIFY dispatch failed: {detail[:400]}")
    store.record_event(
        "verification_dispatched",
        dispatch_payload,
        workflow_id=task.get("workflow_id"),
        node_id=task.get("node") or task.get("stage"),
        task_id=task.get("task_id"),
        agent_id=task.get("agent"),
        source="supervisor",
        run_id=run_id_for_task(task),
    )
    return {"verification_dispatched": True}


def _default_collab_sender(pane_id, prompt):
    result = subprocess.run(
        ["herdr", "agent", "prompt", str(pane_id), prompt],
        text=True, capture_output=True, timeout=120,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "collaboration prompt failed").strip()
        raise RuntimeError(f"collaboration dispatch failed: {detail[:400]}")
    return {"ok": True}


def _collab_task_pane(task):
    pane = task.get("pane_id")
    if pane:
        return pane
    runtime = task.get("runtime") or {}
    return runtime.get("pane_id")


def _authoritative_task(task_id, fallback, db_path=None):
    from herdr import state_db as _sdb
    return _sdb.get_task(str(task_id or ""), db_path=db_path)


def _legacy_evidence_allowed(event, raw_ref, db_path=None):
    from herdr import state_db as _sdb
    from herdr.context_models import _canonical_evidence_ref
    from herdr.collaboration import collab_scope_for_task
    from herdr.trajectory import run_id_for_task

    ref = _canonical_evidence_ref(raw_ref) or str(raw_ref or "")
    if ":" not in ref:
        return False
    prefix, object_id = ref.split(":", 1)
    if prefix not in {"observation", "trajectory", "eval"} or not object_id:
        return False
    conn = _sdb.get_db_connection(db_path=db_path)
    try:
        if prefix == "observation":
            row = conn.execute(
                "SELECT run_id, task_id, workflow_id FROM observations WHERE observation_id = ?",
                (object_id,),
            ).fetchone()
        elif prefix == "eval":
            row = conn.execute(
                "SELECT run_id, task_id, workflow_id FROM eval_results WHERE eval_id = ?",
                (object_id,),
            ).fetchone()
        else:
            event_number = object_id.removeprefix("evt_")
            try:
                row = conn.execute(
                    "SELECT run_id, task_id, workflow_id FROM events WHERE id = ? AND source = 'trajectory'",
                    (int(event_number),),
                ).fetchone()
            except ValueError:
                row = None
        if row is None:
            return False
        row_workflow = str(row["workflow_id"] or "")
        event_workflow = str(event.get("workflow_id") or "")
        if row_workflow and row_workflow != event_workflow:
            return False
        task_id = row["task_id"]
        if task_id:
            task = _sdb.get_task(str(task_id), db_path=db_path)
            return bool(
                task
                and str(task.get("workflow_id") or "") == event_workflow
                and collab_scope_for_task(task) == str(event.get("run_id") or "")
                and str(run_id_for_task(task)) == str(row["run_id"] or "")
            )
        # A taskless fact has no persisted Task identity to bind it to this
        # Handoff. Compiler/storage accept taskless facts only through a
        # verified run-to-execution-scope map; the legacy prompt path has no
        # such snapshot, so fail closed instead of treating string equality as
        # provenance.
        return False
    finally:
        conn.close()


def _collab_task_run(task):
    # Collaboration scope, NOT the per-task run_id: every herdr-task launch
    # mints its own run_id, so scope must be the shared workflow execution
    # identity. Fail closed when nothing identifies the execution.
    try:
        from herdr.collaboration import collab_scope_for_task
        return collab_scope_for_task(task)
    except Exception:
        return None


def collaboration_enabled():
    import os as _os
    return _os.environ.get("HERDR_COLLABORATION_ENABLED", "1") != "0"


def _collab_prior_intent(event_id, to_task_id, db_path):
    from herdr import state_db as _sdb
    try:
        rows = _sdb.list_events(
            event_type="collaboration_dispatch_intent",
            task_id=to_task_id, db_path=db_path,
        )
    except Exception:
        return None
    for row in rows or []:
        payload = row.get("payload") or {}
        if payload.get("collaboration_event_id") == event_id:
            return row
    return None


def _reconcile_collaboration_ack(event_id, target, db_path):
    """ACK reconciliation shared by the dispatch and recovery paths.

    A target already working never emits another working transition, so a
    handoff created after that point would stick at dispatched without this
    check. Returns the acknowledged row, or None when not applicable.
    """
    from herdr import state_db as _sdb

    try:
        if (target.get("status") or "") == "working":
            return _sdb.mark_collaboration_acknowledged(event_id, db_path=db_path)
    except Exception as exc:
        print(f"[COLLABORATION ACK RECONCILE SKIPPED] event={event_id}: {type(exc).__name__}")
    return None


def _working_context_ref_valid(event, target_task, db_path=None):
    """Validate V1 refs strictly; only an absent ref is legacy-compatible."""
    from herdr import state_db as _sdb
    from herdr.context_compiler import infer_agent_role
    from herdr.trajectory import run_id_for_task

    refs = list(event.get("context_refs") or [])
    authoritative_task = _authoritative_task(
        event.get("to_task_id"), target_task, db_path=db_path,
    )
    if authoritative_task is None:
        return False
    target_task = authoritative_task
    if str(target_task.get("workflow_id") or "") != str(event.get("workflow_id") or ""):
        return False
    if not refs:
        return True
    try:
        expected_role = infer_agent_role(target_task)
    except ValueError:
        return False
    for ref in refs:
        value = str(ref or "")
        if not value.startswith("wc_"):
            return False
        context = _sdb.get_working_context(value, db_path=db_path)
        if context is None:
            return False
        try:
            source_watermark = int(context.get("source_watermark") or 0)
        except (TypeError, ValueError):
            return False
        if len(value) <= 3 or not context.get("source_version") or source_watermark <= 0:
            return False
        if str(context.get("task_id") or "") != str(event.get("to_task_id") or ""):
            return False
        if str(context.get("workflow_id") or "") != str(event.get("workflow_id") or ""):
            return False
        if str(context.get("run_scope") or "") != str(event.get("run_id") or ""):
            return False
        expected_run_id = run_id_for_task(dict(target_task))
        if str(context.get("run_id") or "") != str(expected_run_id or ""):
            return False
        if str(context.get("agent_role") or "") != expected_role:
            return False
    return True


def dispatch_collaboration_event(event_id, tasks_by_id, prompt_sender=None, db_path=None):
    """Dispatch one CollaborationEvent through the existing Herdr prompt path.

    Pure assembly: identity/run/pane checks, durable intent, minimal prompt,
    real ``herdr agent prompt`` delivery (injectable for tests). Never falls
    back to another pane or agent: missing target fails the event.
    """
    from herdr import collaboration as _collab
    from herdr import state_db as _sdb

    sender = prompt_sender or _default_collab_sender
    event = _sdb.get_collaboration_event(event_id, db_path=db_path)
    if event is None:
        raise ValueError(f"collaboration event '{event_id}' not found")
    if event["status"] in ("dispatched", "acknowledged", "completed"):
        return {"dispatched": True, "recovered": True,
                "status": event["status"], "event_id": event_id}
    if event["status"] == "failed":
        return {"dispatched": False, "status": "failed", "event_id": event_id}

    target = _authoritative_task(
        event["to_task_id"], (tasks_by_id or {}).get(event["to_task_id"]), db_path=db_path,
    )
    if target is None:
        return _sdb.mark_collaboration_failed(event_id, db_path=db_path)
    if (
        _collab_task_run(target) != event["run_id"]
        or str(target.get("workflow_id") or "") != str(event.get("workflow_id") or "")
    ):
        return _sdb.mark_collaboration_failed(event_id, db_path=db_path)
    pane_id = _collab_task_pane(target)
    if not pane_id:
        return _sdb.mark_collaboration_failed(event_id, db_path=db_path)
    source_task = _authoritative_task(
        event.get("from_task_id"), (tasks_by_id or {}).get(event.get("from_task_id")), db_path=db_path,
    )
    if (
        source_task is None
        or str(source_task.get("workflow_id") or "") != str(event.get("workflow_id") or "")
        or _collab.collab_scope_for_task(source_task) != str(event.get("run_id") or "")
    ):
        return _sdb.mark_collaboration_failed(event_id, db_path=db_path)
    if not _working_context_ref_valid(event, target, db_path=db_path):
        return _sdb.mark_collaboration_failed(event_id, db_path=db_path)
    prompt_event = dict(event)
    context_refs = event.get("context_refs") or []
    allowed_evidence_refs = set()
    for context_ref in context_refs:
        context_row = _sdb.get_working_context(context_ref, db_path=db_path)
        if context_row is None:
            continue
        for field_name in (
            "completed", "artifacts", "evidence", "findings", "decisions", "blockers",
            "open_questions", "verification", "handoffs",
        ):
            for item in context_row.get(field_name) or []:
                if field_name == "evidence" and item.get("source_ref"):
                    allowed_evidence_refs.add(str(item["source_ref"]))
                allowed_evidence_refs.update(
                    str(ref) for ref in item.get("evidence_refs") or [] if ref
                )
    if context_refs:
        from herdr.context_models import _canonical_evidence_ref
        filtered_evidence_refs = []
        for raw_ref in event.get("evidence_refs") or []:
            canonical_ref = _canonical_evidence_ref(raw_ref) or str(raw_ref)
            if canonical_ref in allowed_evidence_refs:
                filtered_evidence_refs.append(canonical_ref)
        prompt_event["evidence_refs"] = filtered_evidence_refs
    else:
        prompt_event["evidence_refs"] = [
            str(raw_ref)
            for raw_ref in event.get("evidence_refs") or []
            if _legacy_evidence_allowed(event, raw_ref, db_path=db_path)
        ]

    if _collab_prior_intent(event_id, event["to_task_id"], db_path) is not None:
        recovered = _sdb.mark_collaboration_dispatched(event_id, db_path=db_path)
        recovered["recovered"] = True
        recovered["dispatched"] = True
        acked = _reconcile_collaboration_ack(event_id, target, db_path)
        if acked is not None:
            acked["recovered"] = True
            acked["dispatched"] = True
            return acked
        return recovered

    _sdb.record_event(
        {"event_type": "collaboration_dispatch_intent",
         "workflow_id": event.get("workflow_id"),
         "task_id": event["to_task_id"],
         "agent_id": event.get("to_agent"),
         "run_id": event["run_id"],
         "payload": {"collaboration_event_id": event_id, "pane_id": pane_id},
         "source": "collaboration"},
        db_path=db_path,
    )
    prompt = _collab.build_handoff_prompt(
        prompt_event, next_action=f"Proceed as {event.get('to_agent') or ''}.",
    )
    try:
        sender(pane_id, prompt)
    except Exception:
        return _sdb.mark_collaboration_failed(event_id, db_path=db_path)
    marked = _sdb.mark_collaboration_dispatched(event_id, db_path=db_path)
    marked["dispatched"] = True
    # ACK fast-path: the target may already be working (it was launched
    # before this event existed, so the working-transition hook never fired
    # for it). Reconcile immediately instead of waiting for a transition
    # that may never come.
    acked = _reconcile_collaboration_ack(event_id, target, db_path)
    if acked is not None:
        acked["dispatched"] = True
        return acked
    return marked


def ack_collaboration_event_for_task(to_task_id, db_path=None):
    """ACK all dispatched handoffs targeting a task that just entered working."""
    from herdr import state_db as _sdb

    acked = []
    for row in _sdb.list_collaboration_events(
        task_id=to_task_id, status="dispatched", db_path=db_path,
    ):
        if row["to_task_id"] != to_task_id:
            continue
        acked.append(_sdb.mark_collaboration_acknowledged(row["event_id"], db_path=db_path))
    return acked


def maybe_ack_on_working(task_id, db_path=None):
    """Accelerator hook for the working transition: never breaks the caller."""
    if not collaboration_enabled():
        return []
    try:
        return ack_collaboration_event_for_task(task_id, db_path=db_path)
    except Exception as exc:
        print(f"[COLLABORATION ACK SKIPPED] task={task_id}: {type(exc).__name__}")
        return []


_NODE_DONE_STATUSES = frozenset({
    "completed", "committed", "integrated", "cleanup_ready", "cleaned",
})


def maybe_dispatch_node_handoffs(*, workflow_id, ready_id, dep_ids, launched,
                                 tasks_by_id=None, prompt_sender=None, db_path=None):
    """Accelerator hook for deterministic node advance.

    For each newly launched task on a known edge (implementation→review,
    review→test), creates one HANDOFF from the latest completed upstream task
    and dispatches it through Herdr. Unknown edges return ``skipped`` so the
    existing Coordinator path stays authoritative. Never raises: a handoff
    failure must not break the already-launched downstream task.
    """
    from herdr import collaboration as _collab
    from herdr import state_db as _sdb

    launched = launched or []
    if not collaboration_enabled():
        return [{"task_id": t, "skipped": True, "reason": "disabled"} for t in launched]
    try:
        if tasks_by_id is None:
            tasks_by_id = {t.get("task_id"): t for t in (load_tasks() or [])
                           if isinstance(t, dict) and t.get("task_id")}
        authoritative_tasks = {}
        for task_id in (tasks_by_id or {}):
            current_task = _sdb.get_task(str(task_id), db_path=db_path)
            if current_task is not None:
                authoritative_tasks[task_id] = current_task
        tasks = authoritative_tasks
    except Exception as exc:
        return [{"task_id": t, "status": "failed", "error": type(exc).__name__}
                for t in launched]

    results = []
    for to_id in launched:
        try:
            target = tasks.get(to_id)
            if target is None:
                results.append({"task_id": to_id, "status": "failed",
                                "reason": "target_task_missing"})
                continue
            dep_hit = None
            trigger = None
            for dep in (dep_ids or []):
                inferred = _collab.infer_handoff_trigger(dep, ready_id)
                if inferred is not None:
                    dep_hit = dep
                    trigger = inferred
                    break
            if trigger is None:
                results.append({"task_id": to_id, "skipped": True,
                                "reason": "no_deterministic_route"})
                continue
            route = _collab.route_deterministic_handoff(trigger=trigger)
            target_scope = _collab.collab_scope_for_task(target)
            target_has_explicit_scope = bool(
                target.get("workflow_run_id") or target.get("execution_id")
            )

            def legacy_pair_is_proven(candidate):
                if target_has_explicit_scope or candidate.get("workflow_run_id") or candidate.get("execution_id"):
                    return _collab.collab_scope_for_task(candidate) == target_scope
                if not target.get("run_id") or not candidate.get("run_id"):
                    return False
                return str(target.get("run_id")) == str(candidate.get("run_id"))

            upstream = [t for t in tasks.values()
                        if isinstance(t, dict)
                        and t.get("workflow_id") == workflow_id
                        and (t.get("node") == dep_hit or t.get("stage") == dep_hit)
                        and t.get("status") in _NODE_DONE_STATUSES
                        and (not target_scope or _collab.collab_scope_for_task(t) == target_scope)
                        and legacy_pair_is_proven(t)]
            if not upstream:
                results.append({"task_id": to_id, "skipped": True,
                                "reason": "no_completed_upstream"})
                continue
            upstream.sort(key=lambda t: float(t.get("updated_at") or 0))
            from_task = upstream[-1]
            run_id = _collab.collab_scope_for_task(from_task)
            branch = from_task.get("branch")
            event = _sdb.create_collaboration_event({
                "run_id": run_id,
                "workflow_id": workflow_id,
                "from_task_id": from_task.get("task_id"),
                "from_agent": from_task.get("agent") or "",
                "from_pane_id": _collab_task_pane(from_task),
                "to_task_id": to_id,
                "to_agent": target.get("agent") or (route or {}).get("to_agent") or "",
                "type": (route or {}).get("type") or "HANDOFF",
                "summary": str(from_task.get("goal") or f"{dep_hit} completed."),
                "artifact_refs": ([f"branch:{branch}"] if branch else []),
                "evidence_refs": [],
                "context_refs": [],
                "source_fact_id": f"{workflow_id}:{dep_hit}:completed",
            }, db_path=db_path)
            try:
                from herdr.context_compiler import compile_working_context, infer_agent_role
                target_context = compile_working_context(
                    workflow_id=workflow_id,
                    task_id=to_id,
                    agent_role=infer_agent_role(target),
                    planned_links=[{
                        "from_task_id": from_task.get("task_id"),
                        "to_task_id": to_id,
                    }],
                    db_path=db_path,
                )
                event = _sdb.attach_working_context_ref(
                    event["event_id"], target_context.context_id, db_path=db_path,
                )
            except Exception as exc:
                print(f"[WORKING_CONTEXT SKIPPED] task={to_id}: {type(exc).__name__}")
            dispatched = dispatch_collaboration_event(
                event["event_id"], tasks, prompt_sender, db_path=db_path)
            results.append({"task_id": to_id, **dispatched})
        except Exception as exc:
            results.append({"task_id": to_id, "status": "failed",
                            "error": type(exc).__name__})
    return results


def _verification_snapshot(clone_path):
    """Return the version of EVAL_DONE visible at a dispatch boundary."""
    if not clone_path:
        return None
    path = Path(clone_path) / ".herdr-loop" / "EVAL_DONE.json"
    try:
        raw = path.read_bytes()
        snapshot = json.loads(raw.decode("utf-8"))
        return {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "mtime_ns": path.stat().st_mtime_ns,
            "iteration": snapshot.get("iteration"),
            "completed_at": snapshot.get("completed_at"),
        }
    except Exception:
        return None


def _verification_evidence_is_new(dispatch, test_evidence):
    """Reject the exact EVAL_DONE snapshot that predates VERIFY dispatch."""
    payload = dispatch.get("payload") or {}
    baseline = payload.get("evidence_baseline")
    started_at = float(payload.get("dispatch_started_at") or dispatch.get("timestamp") or 0)
    current_sha = test_evidence.get("snapshot_sha256")
    if not current_sha:
        return False
    if baseline and current_sha == baseline.get("sha256"):
        return False
    completed_at = test_evidence.get("snapshot_completed_at")
    if started_at:
        if completed_at is None:
            return False
        try:
            if float(completed_at) <= started_at:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _intervention_execution_evidence(task, intervention, store):
    """Find durable evidence for this exact Intervention, never by status alone."""
    intervention_id = intervention.get("intervention_id")
    action = intervention.get("action")
    task_id = intervention.get("task_id")
    run_id = intervention.get("run_id")
    fresh = store.get_task(task_id) if store is not None else None
    fresh = fresh or task
    if store is None:
        return None
    if action == "RETRY":
        for event in store.list_events(
            task_id=task_id, event_type="retry_dispatched", limit=200, desc=True,
        ):
            payload = event.get("payload") or {}
            if (
                event.get("run_id") == run_id
                and payload.get("intervention_id") == intervention_id
                and payload.get("action") == action
            ):
                return {
                    "action": action,
                    "previous_status": fresh.get("status"),
                    "new_status": fresh.get("status"),
                    "attempt_count": payload.get("attempt_count"),
                    "retry_dispatched": True,
                    "already_applied": True,
                    "execution_evidence": True,
                }
        return None
    for event in store.list_events(task_id=task_id, event_type="verification_dispatched", limit=200, desc=True):
        payload = event.get("payload") or {}
        if (
            event.get("run_id") == run_id
            and payload.get("intervention_id") == intervention_id
            and payload.get("action") == action
        ):
            return {
                "action": action,
                "previous_status": fresh.get("status"),
                "new_status": fresh.get("status"),
                "verification_dispatched": True,
                "verification_requested": True,
                "verification_pending": True,
                "already_applied": True,
                "execution_evidence": True,
            }
    return None


def _latest_verification_dispatch(task, store=None):
    from herdr.completion import latest_verification_dispatch
    return latest_verification_dispatch(task, store or _get_store())


def _latest_action_dispatch_intent(task, store, action, intervention_id=None):
    store = store or _get_store()
    from herdr.trajectory import run_id_for_task
    event_type = {
        "VERIFY": "verification_dispatch_intent",
        "RETRY": "retry_dispatch_intent",
    }.get(action, f"{action.lower()}_dispatch_intent")
    rows = store.list_events(
        task_id=task.get("task_id"),
        event_type=event_type,
        source="supervisor", limit=100, desc=True,
    )
    expected_run = run_id_for_task(task)
    for event in rows:
        payload = event.get("payload") or {}
        if (
            event.get("run_id") == expected_run
            and payload.get("action") == action
            and (intervention_id is None or payload.get("intervention_id") == intervention_id)
        ):
            return event
    return None


def _latest_verification_dispatch_intent(task, store=None, intervention_id=None):
    return _latest_action_dispatch_intent(task, store, "VERIFY", intervention_id)


def _latest_retry_dispatch(task, store=None):
    from herdr.completion import latest_retry_dispatch
    return latest_retry_dispatch(task, store or _get_store())


def _retry_execution_complete_for_rework(task, store=None):
    from herdr.completion import retry_execution_complete_for_rework
    return retry_execution_complete_for_rework(task, store or _get_store())


def _verification_execution_complete_for_rework(task, store=None):
    from herdr.completion import verification_execution_complete_for_rework
    return verification_execution_complete_for_rework(task, store or _get_store())


_INTERVENTION_EXECUTION_OWNER = f"controller:{os.getpid()}:{uuid.uuid4()}"


def _execute_supervisor_intervention(task, decision, action_handler, store=None):
    """Claim and execute one durable action, retaining legacy handler support."""
    intervention = decision.get("intervention") if isinstance(decision, dict) else None
    if not intervention:
        return action_handler(task, decision)
    store = store or _get_store()
    intervention_id = intervention.get("intervention_id")
    current = store.get_intervention(intervention_id)
    if current is None:
        raise RuntimeError("canonical intervention disappeared")
    if current.get("status") in ("completed", "failed", "superseded"):
        if current.get("status") == "failed":
            raise RuntimeError("intervention already failed")
        return current.get("result") or {"already_applied": True}
    claimed = store.claim_intervention(
        intervention_id,
        execution_owner=_INTERVENTION_EXECUTION_OWNER,
        recover_running=bool(decision.get("_recovery")),
    )
    if claimed is None:
        return {"already_claimed": True}
    owner = claimed.get("execution_owner")
    try:
        evidence = _intervention_execution_evidence(task, claimed, store)
        if evidence is not None:
            result = evidence
        elif action_handler in (_supervisor_retry, _supervisor_verify):
            result = action_handler(task, decision, store=store) or {}
        else:
            result = action_handler(task, decision) or {}
        store.complete_intervention(intervention_id, result, execution_owner=owner)
        return result
    except Exception as exc:
        from herdr.supervisor.state import redact_text
        error = getattr(exc, "intervention_error", None) or {
            "type": type(exc).__name__, "message": redact_text(str(exc)[:500]),
        }
        try:
            store.fail_intervention(intervention_id, error, execution_owner=owner)
        except ValueError:
            # A reclaimed owner or a concurrent terminal transition already
            # owns the durable outcome; do not mask the original action error.
            pass
        raise


def recover_pending_interventions(store=None, run_id=None, task_id=None):
    """Recover requested/running actions before a done redelivery can pass."""
    store = store or _get_store()
    recovered = []
    pending = store.list_interventions(
        run_id=run_id, task_id=task_id, statuses=["requested", "running"],
    )
    for item in pending:
        task = store.get_task(item.get("task_id"))
        if not task:
            store.fail_intervention(item["intervention_id"], {"code": "task_missing"})
            continue
        from herdr.trajectory import run_id_for_task
        if str(run_id_for_task(task)) != str(item.get("run_id")):
            store.fail_intervention(item["intervention_id"], {"code": "run_mismatch"})
            continue
        decision = dict(item)
        decision["intervention"] = item
        decision["_recovery"] = True
        if item.get("action") == "RETRY":
            result = _execute_supervisor_intervention(task, decision, _supervisor_retry, store=store)
        elif item.get("action") == "VERIFY":
            result = _execute_supervisor_intervention(task, decision, _supervisor_verify, store=store)
        else:
            store.fail_intervention(item["intervention_id"], {"code": "unsupported_action"})
            continue
        recovered.append({"intervention": item, "result": result})
    return recovered


def supervisor_continue_flow(result):
    """Whether the caller may run its default continuation (the done event).

    Fail-safe: no supervision result (disabled / skipped / crash) continues
    the original flow unchanged. An enforced intervention returns
    ``continue_flow=False`` so HAFlow orchestration owns the next step.
    """
    if not result:
        return True
    return bool(result.get("continue_flow", True))


def _supervisor_action_recovery_enabled():
    """Read the current kill switch before replaying durable actions."""
    try:
        if supervisor_harness is None:
            return False
        cfg = supervisor_harness.load_config()
        from herdr.supervisor.config import supervisor_enabled
        return bool(cfg.get("enforce") and supervisor_enabled(cfg))
    except Exception:
        return False


def emit_done_if_allowed(task, report_text=None):
    """The single gateway for agent_done -> coordinator done.

    Every Controller path that would announce a task as done goes through
    the supervisor checkpoint first; an enforced intervention blocks the
    default flow (the action handler owns the next step instead). When the
    checkpoint is rate-gated/skipped, a still-pending enforced intervention
    from the events ledger keeps blocking redelivery until a newer decision
    or a new agent_done transition supersedes it.

    The terminal Trajectory Observer checkpoint also lives here: listener,
    recovery and registry-redelivery paths all funnel through this gateway,
    and the observation must be queued before the task can advance.
    """
    observer_complete = threading.Event()
    supervisor_complete = threading.Event()
    store = _get_store()
    try:
        from herdr.trajectory import run_id_for_task
        if hasattr(store, "list_interventions") and _supervisor_action_recovery_enabled():
            recovered = recover_pending_interventions(
                store=store, run_id=run_id_for_task(task), task_id=task.get("task_id"),
            )
            if recovered:
                return False
    except Exception as exc:
        print(f"[INTERVENTION RECOVERY UNAVAILABLE] task={task.get('task_id')}: {type(exc).__name__}")
        return False
    _observer_terminal_checkpoint(task, completion_event=observer_complete)
    _schedule_context_compact(
        task, wait_for=observer_complete, supervisor_done=supervisor_complete,
    )
    try:
        checkpoint = supervisor_checkpoint(task, "agent_done", report_text=report_text)
        if checkpoint is None:
            pending = None
            if supervisor_harness is not None:
                try:
                    pending = supervisor_harness.pending_intervention(task, store)
                except Exception as exc:
                    if hasattr(store, "list_interventions"):
                        print(
                            f"[INTERVENTION RECOVERY UNAVAILABLE] "
                            f"task={task.get('task_id')}: {type(exc).__name__}"
                        )
                        return False
                    pending = None
            if pending:
                _supervisor_log_pending(task, pending)
                return False
            enqueue_coordinator_event(task, "done")
            return True
        if not supervisor_continue_flow(checkpoint):
            return False
        enqueue_coordinator_event(task, "done")
        return True
    finally:
        supervisor_complete.set()


def _schedule_context_compact(task, wait_for=None, supervisor_done=None):
    """Schedule working-memory creation without joining or affecting done flow."""
    if not task:
        return False
    try:
        from herdr.context_compact import compact_run_best_effort
        from herdr.trajectory import run_id_for_task
        store = _get_store()
        target_task_id = str(task.get("task_id") or "")
        target_run_id = str(run_id_for_task(task))
        if not target_task_id or not target_run_id:
            return False

        def worker():
            try:
                if wait_for is not None:
                    wait_for.wait()
                if supervisor_done is not None:
                    supervisor_done.wait()
                from herdr import state_db
                fresh_task = state_db.get_task(
                    target_task_id, db_path=getattr(store, "db_path", None),
                )
                if not fresh_task or str(run_id_for_task(fresh_task)) != target_run_id:
                    print(f"[CONTEXT COMPACT SKIPPED] task={target_task_id}: task/run identity changed")
                    return
                provider = None
                try:
                    from herdr.observer.harness import get_provider
                    provider = get_provider()
                except Exception as provider_exc:
                    print(f"[CONTEXT COMPACT PROVIDER FALLBACK] task={task.get('task_id')}: {type(provider_exc).__name__}: {provider_exc}")
                compact_run_best_effort(
                    target_run_id, task=fresh_task, store=store, provider=provider,
                )
            except Exception as exc:  # defensive boundary isolation
                print(f"[CONTEXT COMPACT WORKER SKIPPED] task={task.get('task_id')}: {type(exc).__name__}: {exc}")

        thread = threading.Thread(
            target=worker,
            name=f"context-compact-{task.get('task_id', 'run')}",
            daemon=True,
        )
        thread.start()
        return True
    except Exception as exc:
        print(f"[CONTEXT COMPACT SKIPPED] task={task.get('task_id')}: {type(exc).__name__}: {exc}")
        return False


def _supervisor_log_pending(task, action):
    """Record/refresh the pending-intervention ledger entry (log/notify once)."""
    key = f"supervisor:{task.get('task_id')}"
    reason = f"action {action} pending; done flow blocked until resolved"
    episode = attention_get(key) or {}
    if episode.get("reason") == reason:
        return
    print(f"[SUPERVISOR PENDING] task={task.get('task_id')} {reason}")
    attention_note(key, task, "supervisor_pending", reason)
    _supervisor_notify(task, action, reason)


def redeliver_done_event(task, now=None):
    """Registry-watcher done redelivery: supervisor-gated and throttled.

    This is a primary production path (sentinel-driven completions only reach
    the coordinator through here), so it must use the same gateway as
    handle_event; an enforced intervention keeps the done event withheld.
    """
    task_id = task.get("task_id")
    key = f"{task_id}:done"
    now = time.time() if now is None else now
    with lock:
        if key in queued_events:
            return False
    if attention_blocks_retry(key, now):
        return False
    print(
        f"[REGISTRY WATCHER] "
        f"task={task_id} "
        f"status=agent_done -> supervisor-gated done event"
    )
    if not emit_done_if_allowed(task):
        return False
    if attention_get(key):
        attention_throttle(key, now=now)
    return True


def _observer_terminal_checkpoint(task, now=None, completion_event=None):
    """Terminal Trajectory Observer checkpoint for agent_done.

    Fail-safe by contract: any failure only logs and returns False, so the
    existing done flow is never blocked. The observation itself runs on the
    observer's daemon worker (async, non-blocking).
    """
    if observer_harness is None or not task:
        if completion_event is not None:
            completion_event.set()
        return False
    try:
        kwargs = {"store": _get_store(), "now": now}
        if completion_event is not None:
            kwargs["completion_event"] = completion_event
        try:
            submitted = observer_harness.submit_terminal_observation(task, **kwargs)
        except TypeError as exc:
            # Keep older test adapters/in-process integrations fail-safe while
            # the built-in harness uses the completion barrier.
            if completion_event is None or "completion_event" not in str(exc):
                raise
            kwargs.pop("completion_event", None)
            submitted = observer_harness.submit_terminal_observation(task, **kwargs)
            completion_event.set()
        if not submitted and completion_event is not None:
            completion_event.set()
        return submitted
    except Exception as exc:
        if completion_event is not None:
            completion_event.set()
        print(f"[OBSERVER TERMINAL CHECK ERROR] task={task.get('task_id')}: {exc}")
        return False


def supervisor_checkpoint(task, trigger, report_text=None, test_evidence=None, evidence_id=None, now=None):
    """Semantic Supervisor 观察点:Jev/Provider 只产生信号与 Policy 结论,
    状态推进全部走既有流程;整体 fail-safe,绝不影响任务主链路。

    返回 harness 结果(None=未监督或异常)。调用方必须用
    ``supervisor_continue_flow(result)`` 决定是否继续 done:
    被 enforce 拦截的 intervention 绝不再走默认完成流程。
    """
    if supervisor_harness is None or not task:
        return None
    try:
        fresh = get_task(task.get("task_id")) or task
        actions = {
            # Policy actions map onto existing flows only. Durable VERIFY/RETRY
            # claims are owned by the Controller wrapper, not the Supervisor.
            "RETRY": lambda t, d: _execute_supervisor_intervention(t, d, _supervisor_retry),
            "ESCALATE": lambda t, d: _supervisor_attention(t, d, "supervisor_escalate"),
            "VERIFY": lambda t, d: _execute_supervisor_intervention(t, d, _supervisor_verify),
            "PAUSE": lambda t, d: _supervisor_attention(t, d, "supervisor_pause"),
            "REROUTE": lambda t, d: _supervisor_attention(t, d, "supervisor_reroute"),
        }
        if report_text is not None:
            report_reader = lambda: report_text
        else:
            report_reader = lambda: _supervisor_pane_report(fresh)
        result = supervisor_harness.run_checkpoint(
            task=fresh,
            trigger=trigger,
            store=_get_store(),
            actions=actions,
            report_reader=report_reader,
            test_evidence=test_evidence,
            evidence_id=evidence_id,
            now=now,
            log=print,
        )
        if result and result.get("intercepted") and not result.get("handled"):
            action = (result.get("decision") or {}).get("action")
            attention_note(
                f"supervisor:{fresh.get('task_id')}",
                fresh,
                "supervisor_unhandled",
                f"intercepted action {action} without a handler; done flow blocked",
            )
        return result
    except Exception as e:
        print(f"[SUPERVISOR SKIPPED] task={task.get('task_id')}: {type(e).__name__}: {e}")
        return None


def check_task_tests_completed(task, store=None, now=None):
    """Continuous Evaluation Checkpoint: tests_completed.

    Deterministic trigger when a real test run produces new METRICS.json evidence.
    Does NOT complete tasks, does NOT disrupt working agents on intermediate failures,
    uses persisted evidence_id dedup (restart-safe) and RateGate throttling.
    """
    if supervisor_harness is None or not task:
        return None

    # 1. Kill switches — checked FIRST, before any file I/O (Fix 5).
    #    supervisor_enabled() verifies: enabled flag + provider flag + API key.
    cfg = supervisor_harness.load_config()
    from herdr.supervisor.config import supervisor_enabled
    if not supervisor_enabled(cfg):
        return None

    clone_path = task.get("clone_path")
    if not clone_path or not os.path.isdir(clone_path):
        return None

    # 2. Extract test evidence (gated by EVAL_DONE.json atomic sentinel)
    from herdr.supervisor import evidence as supervisor_evidence
    test_evidence = supervisor_evidence.extract_test_evidence(clone_path)
    if not test_evidence:
        return None
    if task.get('completion_protocol') == 'receipt-v1' and (
            test_evidence.get('task_id') != task.get('task_id')
            or test_evidence.get('run_id') != task.get('run_id')
            or test_evidence.get('epoch') != task.get('completion_epoch')):
        # Never rebind an old or unattached evaluator snapshot to a current Run.
        return None

    # 3. Build deterministic evidence fingerprint
    evidence_id = supervisor_evidence.build_test_evidence_id(test_evidence)

    # 4. Dedup against persisted evaluation events (restart-safe).
    #    Fix 4: query specifically for supervisor_evaluation events from the
    #    semantic_supervisor source, so the dedup window is never squeezed out
    #    by unrelated events flooding the ledger.
    st = store if store is not None else _get_store()
    task_id = task.get("task_id")
    try:
        events = st.list_events(
            task_id=task_id,
            event_type="supervisor_evaluation",
            source="semantic_supervisor",
            desc=True,
        ) or []
    except Exception:
        events = []

    from herdr.supervisor.evaluation import latest_tests_completed_evidence_id
    latest_ev_id = latest_tests_completed_evidence_id(events)
    if latest_ev_id == evidence_id:
        return None

    # 5. RateGate check: can supervisor evaluate now?
    # If RateGate skips, we DEFER (do not evaluate now, but keep evidence un-evaluated
    # so it can be evaluated when RateGate interval clears).
    sup = supervisor_harness.get_supervisor(cfg)
    if sup is not None:
        skip_reason = sup.should_evaluate(task_id, "tests_completed", now=now)
        if skip_reason is not None:
            return None

    # 6. Materialize the compact verification receipt as immutable evidence.
    verification = {
        "type": "tests_completed",
        "epoch": test_evidence.get("epoch"),
        "candidate_sha": test_evidence.get("candidate_sha"),
        "execution_mode": test_evidence.get("execution_mode", "unknown"),
        "command": test_evidence.get("command"),
        "environment": test_evidence.get("environment"),
        "source_fingerprint": test_evidence.get("source_fingerprint"),
        "exit_code": test_evidence.get("exit_code"),
        "counts": {"pass": test_evidence.get("passed_tests"),
                   "fail": test_evidence.get("failing_count"), "skip": test_evidence.get("skipped_tests")},
        "passed": bool(test_evidence.get("converged", False)),
        "evidence_id": evidence_id,
        "passed_tests": test_evidence.get("passed_tests"),
        "total_tests": test_evidence.get("total_tests"),
        "failing_count": test_evidence.get("failing_count", 0),
        "lint_errors": test_evidence.get("lint_errors", 0),
        "type_errors": test_evidence.get("type_errors", 0),
        "composite_score": test_evidence.get("composite_score", 0.0),
    }
    dispatch = _latest_verification_dispatch(task, st)
    if dispatch is not None:
        if not _verification_evidence_is_new(dispatch, test_evidence):
            print(
                f"[VERIFY EVIDENCE DEFERRED] task={task_id}: "
                "EVAL_DONE predates current VERIFY dispatch"
            )
            return None
        dispatch_payload = dispatch.get("payload") or {}
        verification["intervention_id"] = dispatch_payload.get("intervention_id")
        verification["decision_id"] = dispatch_payload.get("decision_id")
    try:
        observation, _created = create_verification_observation_with_status(
            verification,
            run_id=task.get("run_id") or f"run_{task_id}",
            task_id=task_id,
            workflow_id=task.get("workflow_id"),
            store=ObservationStore(getattr(st, "db_path", None)),
        )
        verification["observation_id"] = observation.observation_id
        record_observation_created(
            task,
            observation,
            ledger=TrajectoryLedger(getattr(st, "db_path", None)),
        )
    except Exception as exc:
        print(f"[VERIFICATION OBSERVATION SKIPPED] task={task_id}: {type(exc).__name__}")

    # 7. Record deterministic tests_completed event
    try:
        st.record_event(
            "tests_completed",
            {
                "task_id": task_id,
                "workflow_id": task.get("workflow_id"),
                "iteration": test_evidence.get("iteration"),
                "evidence_id": evidence_id,
                "passed_tests": test_evidence.get("passed_tests"),
                "total_tests": test_evidence.get("total_tests"),
                "failing_count": test_evidence.get("failing_count", 0),
                "lint_errors": test_evidence.get("lint_errors", 0),
                "type_errors": test_evidence.get("type_errors", 0),
                "composite_score": test_evidence.get("composite_score", 0.0),
                "intervention_id": verification.get("intervention_id"),
                "decision_id": verification.get("decision_id"),
            },
            task_id=task_id,
            workflow_id=task.get("workflow_id"),
            node_id=task.get("node") or task.get("stage"),
            agent_id=task.get("agent"),
            source="herdr-controller",
        )
        receipt = record_trajectory_event(
            task,
            "verification_completed",
            ledger=TrajectoryLedger(getattr(st, "db_path", None)),
            verification=verification,
        )
        if not receipt:
            raise RuntimeError("verification_completed receipt was not persisted")
    except Exception as exc:
        print(f"[TESTS_COMPLETED EVENT ERROR] task={task_id}: {exc}")
        # Do not consume/deduplicate this evidence until the durable receipt
        # exists. The next watcher pass must be able to retry the receipt.
        return None

    # 7. Run supervisor checkpoint
    return supervisor_checkpoint(
        task,
        "tests_completed",
        test_evidence=test_evidence,
        evidence_id=evidence_id,
        now=now,
    )


def _completion_marker_snapshot(task):
    """Read the task-owned completion marker without treating idle as done."""
    task = task or {}
    pane_id = task.get("pane_id")
    task_id = task.get("task_id")
    if not pane_id or not task_id:
        return False, ""
    try:
        result = subprocess.run(
            ["herdr", "pane", "read", pane_id, "--source", "visible"],
            text=True,
            capture_output=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    screen = (result.stdout or "") + (result.stderr or "")
    from herdr.completion import marker_present as _marker_present

    return _marker_present(screen, task_id), screen


def _record_completion_sample(
    task,
    marker_present,
    now=None,
    agent_status="idle",
):
    """Persist one Controller/Sentinel sample and arbitrate it atomically."""
    task = task or {}
    task_id = task.get("task_id")
    if not task_id:
        return False
    now = time.time() if now is None else float(now)
    try:
        _get_store().observe_completion(
            task_id,
            marker_present=bool(marker_present),
            agent_status=agent_status,
            observed_at=now,
        )
    except (AttributeError, OSError, RuntimeError, ValueError):
        return False
    return process_completion_observation(task, now=now)


def handle_event(task_id, agent_status):
    task = get_task(task_id)

    if not task:
        return

    current_status = task.get("status")

    print(
        f"[TASK] "
        f"id={task_id} "
        f"workflow={task.get('workflow_id')} "
        f"stage={task.get('stage', 'unknown')} "
        f"pane={task.get('pane_id', 'unknown')} "
        f"agent={task.get('agent', 'unknown')} "
        f"status={agent_status} "
        f"task_status={current_status}"
    )

    # 终态绝不能被 Agent 普通事件覆盖
    if current_status in (
        "completed",
        "failed"
    ):
        return

    if (
        current_status == "blocked"
        and blocked_event_type(task) == "inner_loop_exhausted"
        and agent_status in {"working", "idle", "done"}
    ):
        # Runtime liveness is not the coordinator's persisted decision.
        return

    if agent_status == "working":
        if current_status in (
            "dispatched",
            "blocked",
            "rework"
        ):
            _set_observed_status(task, "working", "recovery_working")
            # Collaboration accelerator: a working target ACKs its handoff.
            maybe_ack_on_working(task_id)

    elif agent_status in {"idle", "done"}:
        if task.get("completion_protocol") == "receipt-v1":
            return
        if current_status in {"dispatched", "working", "rework"}:
            has_done_marker, screen = _completion_marker_snapshot(task)
            if agent_status == "idle" and not has_done_marker:
                # A gate verdict is an explicit machine recovery source for
                # prose-only gate outputs; it is not a Sentinel completion
                # sample and still uses the versioned task CAS.
                if current_status == "working" and gate_verdict_ready(task):
                    if _set_observed_status(task, "agent_done", "gate_verdict_recovery"):
                        emit_done_if_allowed(get_task(task_id), report_text=screen)
                    return
                # Rework recovery is allowed only after the existing
                # verification/retry evidence gates have passed.
                if (
                    current_status == "rework"
                    and check_task_deliverables_ready(task)
                    and _verification_execution_complete_for_rework(task)
                    and _retry_execution_complete_for_rework(task)
                ):
                    if _set_observed_status(task, "agent_done", "rework_recovery"):
                        emit_done_if_allowed(get_task(task_id), report_text=screen)
                    return
            # Both idle and done are settled runtime states; the durable
            # marker, sampling, elapsed-time and CAS gates still arbitrate.
            _record_completion_sample(
                task,
                has_done_marker,
                agent_status="idle" if agent_status == "idle" else "done",
            )
        return

    elif agent_status == "blocked":
        if current_status in (
            "working",
            "dispatched",
            "rework"
        ):
            if _set_observed_status(task, "blocked", "agent_status_blocked"):
                task = get_task(task_id)

                enqueue_coordinator_event(
                    task,
                    blocked_event_type(task)
                )


# ============================================================
# Crash recovery / startup reconciliation
# ============================================================

def get_agent_runtime_status(pane_id):
    try:
        output = subprocess.check_output(
            [
                "herdr",
                "agent",
                "get",
                pane_id
            ],
            text=True
        )

        data = json.loads(output)

        return (
            data["result"]["agent"]
            .get("agent_status", "unknown")
        )

    except Exception as e:
        print(
            f"[RECOVERY STATUS ERROR] "
            f"pane={pane_id}: {e}"
        )
        return None


def reconcile_task_state(task_id):
    task = get_task(task_id)

    if not task:
        return

    current = task.get("status")

    if current in (
        "completed",
        "committed",
        "integrated",
        "cleanup_ready",
        "cleaned",
        "failed"
    ):
        return

    # Registry 已经知道 Agent 执行结束，
    # 但 Controller 可能在通知总指挥前重启。
    if current == "agent_done":
        print(
            f"[RECOVERY] "
            f"task={task_id} "
            f"registry=agent_done "
            f"→ restore done event"
        )

        emit_done_if_allowed(task)
        return

    pane_id = task.get("pane_id")

    if not pane_id:
        return

    runtime = get_agent_runtime_status(
        pane_id
    )

    if runtime is None:
        return

    print(
        f"[RECOVERY] "
        f"task={task_id} "
        f"registry={current} "
        f"agent={runtime}"
    )

    if (
        current == "blocked"
        and blocked_event_type(task) == "inner_loop_exhausted"
        and runtime in {"working", "idle", "done"}
    ):
        # Recover the volatile arbitration queue, keeping the durable blocker.
        enqueue_coordinator_event(task, "inner_loop_exhausted")
        return

    # --------------------------------
    # Agent 当前正在运行
    # --------------------------------
    if runtime == "working":
        if current in (
            "dispatched",
            "blocked",
            "rework"
        ):
            _set_observed_status(task, "working", "recovery_working")
        return

    # --------------------------------
    # Agent 当前 blocked
    # --------------------------------
    if runtime == "blocked":
        if current in (
            "dispatched",
            "working",
            "rework"
        ):
            if not _set_observed_status(task, "blocked", "recovery_blocked"):
                return

        task = get_task(task_id)

        if task and task.get("status") == "blocked":
            enqueue_coordinator_event(
                task,
                blocked_event_type(task)
            )

        return

    # --------------------------------
    # Agent 已经 done，但 Controller
    # 错过了 working/done 事件
    # --------------------------------
    if runtime == "done":
        current = get_task(task_id).get(
            "status"
        )

        if current == "dispatched":
            if not _set_observed_status(
                task,
                "working",
                "recovery_working",
            ):
                return
            current = "working"

        elif current == "blocked":
            if not _set_observed_status(
                task,
                "working",
                "recovery_working",
            ):
                return
            current = "working"

        elif current == "rework":
            has_done_marker, _screen = _completion_marker_snapshot(task)
            _record_completion_sample(
                task,
                has_done_marker,
                agent_status="idle" if runtime == "idle" else "done",
            )
            return

        if current == "working":
            has_done_marker, _screen = _completion_marker_snapshot(task)
            _record_completion_sample(
                task,
                has_done_marker,
                agent_status="idle" if runtime == "idle" else "done",
            )
            return

        task = get_task(task_id)

        if task and task.get("status") == "agent_done":
            emit_done_if_allowed(task)

        return

    # Agent 曾经进入 working/rework，随后 Controller 重启时发现已经 idle
    if runtime == "idle":
        if current in {"working", "rework"}:
            has_done_marker, _screen = _completion_marker_snapshot(task)
            _record_completion_sample(task, has_done_marker, agent_status="idle")
        return

    # dispatched -> idle 不自动推断完成；
    # unknown 也不自动推断。
    print(
        f"[RECOVERY NOOP] "
        f"task={task_id} "
        f"agent={runtime}"
    )



# ============================================================
# Per-task Herdr subscriptions
# ============================================================

def listen_task(task_id):
    task = get_task(task_id)

    if not task:
        return

    pane_id = task["pane_id"]

    sock = socket.socket(
        socket.AF_UNIX,
        socket.SOCK_STREAM
    )

    try:
        sock.connect(SOCKET_PATH)

        with lock:
            task_sockets[task_id] = sock

        request = {
            "id": f"task-{task_id}",
            "method": "events.subscribe",
            "params": {
                "subscriptions": [
                    {
                        "type":
                        "pane.agent_status_changed",
                        "pane_id": pane_id
                    }
                ]
            }
        }

        sock.sendall(
            (json.dumps(request) + "\n").encode()
        )

        file = sock.makefile("r")

        first = file.readline()

        if first:
            # 订阅响应可能是错误(如目标 pane 已不存在),必须显式失败,
            # 交由 registry_watcher 的退避逻辑处理,禁止伪装成已订阅。
            try:
                payload = json.loads(first)
            except Exception:
                payload = None

            if isinstance(payload, dict) and payload.get("error"):
                raise RuntimeError(
                    str(payload["error"].get("message") or payload["error"])
                )

            print(
                f"[SUBSCRIBED] "
                f"task={task_id} "
                f"pane={pane_id}"
            )

            # Controller 重启后立即核对
            # Registry 与 Agent 当前真实状态。
            reconcile_task_state(
                task_id
            )

        for line in file:
            if not line:
                break

            event = json.loads(line)

            if (
                event.get("event")
                != "pane.agent_status_changed"
            ):
                continue

            status = (
                event["data"]
                .get(
                    "agent_status",
                    "unknown"
                )
            )

            handle_event(
                task_id,
                status
            )

    except Exception as e:
        print(
            f"[LISTENER ERROR] "
            f"task={task_id}: {e}"
        )

    finally:
        with lock:
            task_sockets.pop(
                task_id,
                None
            )
            listeners.pop(
                task_id,
                None
            )

        try:
            sock.close()
        except Exception:
            pass


def start_task_listener(task_id):
    thread = threading.Thread(
        target=listen_task,
        args=(task_id,),
        daemon=True
    )

    with lock:
        listeners[task_id] = thread

    thread.start()


def stop_task_listener(task_id):
    with lock:
        sock = task_sockets.pop(
            task_id,
            None
        )

    if sock:
        try:
            sock.shutdown(
                socket.SHUT_RDWR
            )
        except Exception:
            pass

        try:
            sock.close()
        except Exception:
            pass

        print(
            f"[UNSUBSCRIBED] "
            f"task={task_id}"
        )


# ============================================================
# Awake guard (长跑 workflow 防休眠)
# ============================================================

_awake_guard_proc = None


def sync_awake_guard(active_workflows):
    """有活跃 workflow 时持有 caffeinate,避免机器休眠冻结整条流水线。

    controller 进程退出或 workflow 清零时自动释放;HERDR_AWAKE_GUARD=0 关闭。
    """
    global _awake_guard_proc

    disabled = os.environ.get("HERDR_AWAKE_GUARD", "1").strip().lower() in (
        "0",
        "false",
        "off",
        "no",
    )

    want = (
        not disabled
        and bool(active_workflows)
        and sys.platform == "darwin"
        and shutil.which("caffeinate") is not None
    )

    if want:
        if _awake_guard_proc is None or _awake_guard_proc.poll() is not None:
            try:
                _awake_guard_proc = subprocess.Popen(
                    ["caffeinate", "-i", "-s", "-w", str(os.getpid())],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                print(
                    f"[AWAKE GUARD] caffeinate started "
                    f"workflows={len(active_workflows)}"
                )
            except Exception as exc:
                print(f"[AWAKE GUARD ERROR] {exc}")
                _awake_guard_proc = None
        return

    if _awake_guard_proc is not None:
        try:
            _awake_guard_proc.terminate()
        except Exception:
            pass
        _awake_guard_proc = None
        print("[AWAKE GUARD] caffeinate released")


# ============================================================
# Registry watcher
# ============================================================

def registry_watcher():
    active_statuses = {
        "dispatched",
        "working",
        "blocked",
        "agent_done",
        "rework",
    }
    TERMINAL_LIKE_STATUSES = (
        "completed",
        "failed",
        "superseded",
        "cleaned",
        "committed",
        "integrated",
        "cleanup_ready",
    )

    last_advance_check = 0

    while True:
        try:
            now = time.time()
            if now - last_advance_check >= 2:
                last_advance_check = now
                check_all_workflows_stage_advance()
                sync_awake_guard(active_registered_workflows())

            tasks = load_tasks()
            # Sentinel records observations; Controller is the only actor that
            # promotes a confirmed completion or blocker to a task transition.
            process_structured_completions(now=now)
            process_all_completion_observations(now=now)
            process_blocked_observations()
            tasks = load_tasks()
            task_ids_now = set()
            _wf_closed_cache = {}

            def _is_wf_closed(wfid):
                if not wfid:
                    return False
                if wfid not in _wf_closed_cache:
                    _wf_closed_cache[wfid] = bool(workflow_closed(wfid))
                return _wf_closed_cache[wfid]

            for task in tasks:
                task_id = task["task_id"]
                status = task.get("status")
                task_ids_now.add(task_id)

                wf_id = task.get("workflow_id")
                if status in TERMINAL_LIKE_STATUSES or _is_wf_closed(wf_id):
                    with lock:
                        running = (
                            task_id
                            in task_sockets
                        )

                    if running:
                        stop_task_listener(
                            task_id
                        )

                    _listener_backoff.pop(task_id, None)
                    _listener_giveup_logged.discard(task_id)
                    attention_clear(f"{task_id}:done")
                    attention_clear(f"{task_id}:blocked")
                    attention_clear(f"{task_id}:attention")

                    # 基础设施失败(投递熔断/进程崩溃)自动作废补派,
                    # 不让整个节点空等人工 relaunch。
                    if status == "failed" and not _is_wf_closed(wf_id):
                        try:
                            recover_router_isolation_tasks(
                                task.get("workflow_id"), tasks
                            )
                            recover_infra_failed_tasks(
                                task.get("workflow_id"), tasks
                            )
                        except (OSError, RuntimeError, ValueError, AttributeError) as exc:
                            print(f"[AUTO RECOVER ERROR] {exc}")

                    # completed/committed + git 的终化重试必须在这里驱动:
                    # 该分支随即 continue,走不到后方的重试块。
                    if status in ("completed", "committed"):
                        try:
                            _check_finalize_retry(task, status, now)
                        except (OSError, RuntimeError, ValueError, KeyError,
                                subprocess.SubprocessError) as exc:
                            print(f"[FINALIZE RETRY ERROR] {exc}")

                    continue

                # ---- listener 订阅:指数退避 + 封顶(僵尸 pane 护栏) ----
                if status in active_statuses:
                    with lock:
                        already = (
                            task_id
                            in listeners
                        )

                    if not already:
                        signature = (status, task.get("updated_at"))
                        slot = _listener_backoff.get(task_id)
                        if not slot or slot.get("signature") != signature:
                            # 任务真实状态变化即重置退避,避免误封活工位。
                            slot = {
                                "attempts": 0,
                                "next_allowed_at": 0.0,
                                "signature": signature,
                            }
                            _listener_backoff[task_id] = slot

                        if now >= slot.get("next_allowed_at", 0.0):
                            attempts = slot.get("attempts", 0) + 1
                            slot["attempts"] = attempts
                            if attempts >= liveness.subscribe_max_attempts():
                                slot["next_allowed_at"] = (
                                    now + liveness.subscribe_backoff_cap()
                                )
                                if task_id not in _listener_giveup_logged:
                                    _listener_giveup_logged.add(task_id)
                                    print(
                                        f"[LISTENER GIVEUP] "
                                        f"task={task_id} "
                                        f"pane={task.get('pane_id')} "
                                        f"attempts={attempts} -> "
                                        f"retry every {int(liveness.subscribe_backoff_cap())}s"
                                    )
                            else:
                                slot["next_allowed_at"] = (
                                    now + liveness.backoff_delay(attempts)
                                )
                            start_task_listener(
                                task_id
                            )

                # ---- tests_completed 连续评估检查 (仅对活跃工作中的任务) ----
                # dispatched 排除：Agent 尚未开始工作，不会有真实测试结果；
                # rework 保留：Agent 仍在 inner loop 迭代，和 working 等价对待。
                if status in ("working", "rework"):
                    try:
                        check_task_tests_completed(task, store=_get_store(), now=now)
                    except Exception as exc:
                        print(f"[TESTS_COMPLETED CHECK ERROR] task={task_id}: {exc}")

                # ---- Trajectory Observer: 旁路观察,非阻塞投递 ----
                # 观察线程与主轮询物理隔离;超时/模型失败/内部异常均不影响
                # Task/Workflow/Runtime 与事件推进。
                if (
                    status in ("working", "rework", "blocked")
                    and observer_harness is not None
                ):
                    try:
                        observer_harness.submit_observation(
                            task, store=_get_store(), now=now
                        )
                    except Exception as exc:
                        print(f"[OBSERVER CHECK ERROR] task={task_id}: {exc}")

                # ---- done 事件投递:受 attention episode 节流 + 监督网关把关 ----
                # terminal Observer checkpoint 已挂在统一 Done Gateway
                # (emit_done_if_allowed) 内,覆盖 listener/recovery/redelivery 全部路径。
                if status == "agent_done":
                    redeliver_done_event(task, now=now)

                # ---- blocked SLA: Controller owns the one re-push and escalation ----
                if status == "blocked":
                    schedule_blocked_sla_task(task, now=now)
                else:
                    attention_clear(f"{task_id}:blocked")
                    attention_clear(_blocked_sla_key(task_id))

                # ---- interrupted / paused 死区:超时未裁决即升级给总指挥 ----
                if status in ("interrupted", "paused"):
                    key = f"{task_id}:attention"
                    updated = float(task.get("updated_at") or 0)
                    with lock:
                        already_queued = key in queued_events
                    if (
                        updated
                        and now - updated >= liveness.attention_grace()
                        and not already_queued
                        and not attention_blocks_retry(key, now)
                    ):
                        print(
                            f"[REGISTRY WATCHER] "
                            f"task={task_id} "
                            f"status={status} -> attention event to coordinator"
                        )
                        enqueue_coordinator_event(task, "attention")
                        if not attention_get(key):
                            attention_note(
                                key,
                                task,
                                "attention",
                                reason="status_requires_attention",
                                attempts=1,
                            )
                        attention_throttle(key, now=now)
                else:
                    attention_clear(f"{task_id}:attention")

                # ---- committed 滞留 / completed 但 commit 门禁瞬时失败(flaky):
                #      integrate 失败或 commit gate 抖动时按退避自动重试,
                #      达到上限后停止并升级人工(避免无限重试风暴)。
                #      completed 分支在上方的 terminal 分支内已驱动(随即
                #      continue,走不到这里);这里覆盖 committed 等终化中状态。
                _check_finalize_retry(task, status, now)

                if status == "rework" and not _is_wf_closed(task.get("workflow_id")):
                    pane_id = task.get("pane_id")
                    if pane_id:
                        runtime = get_agent_runtime_status(pane_id)
                        if (
                            runtime in ("idle", "done")
                            and check_task_deliverables_ready(task)
                            and _verification_execution_complete_for_rework(task)
                            and _retry_execution_complete_for_rework(task)
                        ):
                            has_done_marker, _screen = _completion_marker_snapshot(task)
                            _record_completion_sample(
                                task,
                                has_done_marker,
                                agent_status="idle" if runtime == "idle" else "done",
                            )

            for stale_id in list(_listener_backoff.keys()):
                if stale_id not in task_ids_now:
                    _listener_backoff.pop(stale_id, None)
                    _listener_giveup_logged.discard(stale_id)

        except Exception as e:
            print(
                f"[REGISTRY ERROR] {e}"
            )

        time.sleep(1)


# ============================================================
# Main
# ============================================================

def report_integration_gaps():
    """集成健康检查:lifecycle 集成缺失时 herdr 退化为屏幕探测,
    残影文本会造成 agent 状态永久误判(卡死根因之一),必须在启动时大声暴露。"""
    try:
        result = subprocess.run(
            ["herdr", "integration", "status"],
            text=True,
            capture_output=True,
            timeout=30,
        )
        status_text = result.stdout or result.stderr
    except Exception as exc:
        print(f"[INTEGRATION CHECK ERROR] {exc}")
        return

    kinds = {t.get("agent") for t in load_tasks() if t.get("agent")}
    for gap in liveness.integration_gaps(status_text, kinds):
        print(
            f"[INTEGRATION GAP] "
            f"agent={gap['agent']} "
            f"integration={gap['integration']} "
            f"not installed ({gap['status']}) -> "
            f"状态将退化为屏幕探测,可能误判卡死; "
            f"修复: herdr integration install {gap['integration']}"
        )


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
        {"service": "com.user.herdr-controller", "pid": os.getpid(), **fingerprint}, sort_keys=True,
    ), flush=True)
    print("[CONTROLLER V12] starting")
    print(f"[REGISTRY] {TASKS_FILE}")
    print(
        f"[COORDINATOR] "
        f"{COORDINATOR_PANE}"
    )
    print(
        "[QUEUE] coordinator event "
        "serialization enabled"
    )

    report_integration_gaps()

    reset_queued_stage_states()

    worker = threading.Thread(
        target=coordinator_worker,
        daemon=True
    )
    worker.start()

    registry_watcher()


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print(
            "\n[CONTROLLER V12] stopped"
        )
