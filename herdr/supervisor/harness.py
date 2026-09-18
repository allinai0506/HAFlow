#!/usr/bin/env python3
"""Semantic Supervisor checkpoint harness (herdr/supervisor/harness.py).

Imperative Shell glue for the controller: given one task checkpoint, run the
supervisor, persist the evaluation and the policy decision as WorkflowEvents
(reusing the canonical events store - no new tables), and hand the action to
caller-provided action handlers.

Invariants enforced here:
- every public entry point is fail-safe: any exception inside supervision is
  logged as a skipped checkpoint, never raised into the task flow;
- actions reach HAFlow state only through handlers the orchestrator supplies;
  with enforcement off (V1 default) the decision is recorded, nothing else;
- provider signals never bypass the policy engine: this module consumes
  PolicyDecision, not raw results.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from ..decision import create_provider
from . import policy as policy_engine
from .config import jev_provider_config, load_config, supervisor_enabled
from .engine import RateGate, SemanticSupervisor
from .evaluation import (
    EVALUATION_EVENT,
    POLICY_EVENT,
    latest_evaluation,
)

EVENT_SOURCE = "semantic_supervisor"

_supervisors: Dict[str, SemanticSupervisor] = {}


def get_supervisor(config: Optional[dict] = None) -> Optional[SemanticSupervisor]:
    """Build (and memoize per config path) the supervisor for this process."""
    cfg = config or load_config()
    key = str(cfg.get("provider")) + "|" + str(cfg.get("interval")) + "|" + str(cfg.get("enforce"))
    cached = _supervisors.get(key)
    if cached is not None:
        return cached
    provider = None
    if cfg.get("provider") == "jev":
        provider = create_provider("jev", jev_provider_config(cfg))
    else:
        provider = create_provider(str(cfg.get("provider")), cfg.get("provider_config") or {})
    supervisor = SemanticSupervisor(cfg, provider)
    _supervisors[key] = supervisor
    return supervisor


def collect_facts(task: dict, store=None, config: Optional[dict] = None) -> Dict[str, Any]:
    """Deterministic facts from the task record (fact layer, not provider)."""
    cfg = config or {}
    runtime = task.get("runtime") if isinstance(task.get("runtime"), dict) else {}
    payload = task if isinstance(task, dict) else {}
    started_at = runtime.get("started_at") or payload.get("created_at")
    elapsed = None
    if started_at:
        elapsed = max(0.0, round(time.time() - float(started_at), 1))
    fix_loop = payload.get("fix_loop") if isinstance(payload.get("fix_loop"), dict) else {}
    return {
        "task_status": task.get("status"),
        "runtime_status": runtime.get("status"),
        "attempt_count": int(
            payload.get("attempt_count")
            or fix_loop.get("loop_count")
            or 0
        ),
        "verification_count": int(payload.get("verification_count") or 0),
        "elapsed_seconds": elapsed,
        "auto_execute_allowed": bool(
            (cfg.get("policy") or {}).get("allow_auto_execute", True)
        ),
    }


def run_checkpoint(
    *,
    task: dict,
    trigger: str,
    store,
    actions: Optional[Dict[str, Callable[[dict, dict], Any]]] = None,
    config: Optional[dict] = None,
    supervisor: Optional[SemanticSupervisor] = None,
    log: Callable[[str], None] = print,
) -> Optional[Dict[str, Any]]:
    """One supervised checkpoint. Returns {'evaluation':…, 'decision':…} or None.

    ``actions`` maps PolicyDecision.action -> handler(task, decision_dict);
    handlers are the orchestrator's own existing flows (rework, attention,
    coordinator events). The harness itself changes no HAFlow state.
    """
    cfg = config or load_config()
    # Cheap global kill-switch only; provider availability is re-checked per
    # checkpoint inside the engine so a stub/injected supervisor still runs.
    if not cfg.get("enabled", False):
        return None
    task_id = task.get("task_id")
    try:
        supervisor = supervisor or get_supervisor(cfg)
        if supervisor is None:
            return None
        events: list = []
        previous: Optional[dict] = None
        if store is not None:
            try:
                events = store.list_events(task_id=task_id, limit=200) or []
                previous = latest_evaluation(events)
            except Exception:
                events = []
        facts = collect_facts(task, store=store, config=cfg)
        evaluation = supervisor.evaluate(
            task,
            trigger,
            events=events,
            facts={k: v for k, v in facts.items() if k in (
                "attempt_count", "elapsed_seconds", "verification_count")},
            previous_evaluation=previous,
        )
        if evaluation is None:
            return None
        if store is not None:
            try:
                store.record_event(
                    EVALUATION_EVENT, evaluation,
                    workflow_id=task.get("workflow_id"),
                    node_id=task.get("node") or task.get("stage"),
                    task_id=task_id,
                    agent_id=task.get("agent"),
                    source=EVENT_SOURCE,
                    timestamp=evaluation.get("timestamp"),
                )
            except Exception as exc:
                log(f"[SUPERVISOR EVENT WRITE FAILED] task={task_id}: {type(exc).__name__}")
        decision = policy_engine.decide(evaluation, facts, cfg)
        payload = {
            "decision_id": evaluation["evaluation_id"],
            "evaluation_id": evaluation["evaluation_id"],
            "task_id": task_id,
            "trigger": trigger,
            **decision.to_dict(),
        }
        if store is not None:
            try:
                store.record_event(
                    POLICY_EVENT, payload,
                    workflow_id=task.get("workflow_id"),
                    node_id=task.get("node") or task.get("stage"),
                    task_id=task_id,
                    agent_id=task.get("agent"),
                    source=EVENT_SOURCE,
                )
            except Exception as exc:
                log(f"[SUPERVISOR EVENT WRITE FAILED] task={task_id}: {type(exc).__name__}")
        log(
            f"[SUPERVISOR] task={task_id} trigger={trigger} "
            f"provider={evaluation.get('provider')} action={decision.action} "
            f"why={'; '.join(decision.reasons)}"
        )
        if cfg.get("enforce") and decision.action != policy_engine.CONTINUE:
            handler = (actions or {}).get(decision.action)
            if handler is not None:
                try:
                    handler(task, payload)
                except Exception as exc:
                    log(
                        f"[SUPERVISOR ACTION FAILED] task={task_id} "
                        f"action={decision.action}: {type(exc).__name__}"
                    )
            else:
                log(
                    f"[SUPERVISOR ACTION UNMAPPED] task={task_id} "
                    f"action={decision.action} (no handler; observation only)"
                )
        return {"evaluation": evaluation, "decision": payload}
    except Exception as exc:
        # Supervision failure is never a task failure.
        log(f"[SUPERVISOR SKIPPED] task={task_id} trigger={trigger}: {type(exc).__name__}: {exc}")
        return None


def reset_process_state() -> None:
    """Drop memoized supervisors/gates (tests / config reload)."""
    _supervisors.clear()


__all__ = [
    "EVALUATION_EVENT",
    "POLICY_EVENT",
    "RateGate",
    "collect_facts",
    "get_supervisor",
    "reset_process_state",
    "run_checkpoint",
]
