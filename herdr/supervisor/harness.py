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
  PolicyDecision, not raw results;
- an enforced intervention (RETRY/REROUTE/PAUSE/VERIFY/ESCALATE) sets
  ``continue_flow=False``: the caller's default continuation (the normal
  ``done`` event) must not run while HAFlow handles the action instead.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Optional

from ..decision import create_provider
from . import evidence as evidence_layer
from . import policy as policy_engine
from .config import (
    jev_provider_config,
    load_config,
    provider_enabled,
    supervisor_enabled,
)
from .engine import RateGate, SemanticSupervisor
from ..intervention import (
    ACTION_RETRY,
    ACTION_VERIFY,
    STATUS_REQUESTED,
    request_intervention,
)
from .evaluation import (
    EVALUATION_EVENT,
    POLICY_EVENT,
    latest_evaluation,
)

EVENT_SOURCE = "semantic_supervisor"

_FACT_EVIDENCE_KEYS = ("tests", "diff_summary", "output_summary", "test_progress", "trigger")

_supervisors: Dict[str, SemanticSupervisor] = {}


def _supervisor_signature(cfg: dict) -> str:
    """Fields whose change must rebuild the memoized supervisor."""
    jev = cfg.get("jev")
    jev_sig = (
        {key: jev.get(key) for key in ("enabled", "model", "base_url", "api_key_env",
                                       "timeout")}
        if isinstance(jev, dict) else jev
    )
    return json.dumps({
        "provider": cfg.get("provider"),
        "enabled": cfg.get("enabled"),
        "enforce": cfg.get("enforce"),
        "interval": cfg.get("interval"),
        "cooldown": cfg.get("cooldown"),
        "max_calls_per_task": cfg.get("max_calls_per_task"),
        "max_context_size": cfg.get("max_context_size"),
        "signals": cfg.get("signals"),
        "thresholds": cfg.get("thresholds"),
        "policy": cfg.get("policy"),
        "jev": jev_sig,
        "provider_config": cfg.get("provider_config"),
    }, sort_keys=True, default=str)


def get_supervisor(config: Optional[dict] = None) -> Optional[SemanticSupervisor]:
    """Build (and memoize per effective config) the supervisor for this process."""
    cfg = config or load_config()
    if not provider_enabled(cfg):
        return None
    key = _supervisor_signature(cfg)
    cached = _supervisors.get(key)
    if cached is not None:
        return cached
    provider = None
    if cfg.get("provider") == "jev":
        provider = create_provider("jev", jev_provider_config(cfg))
    else:
        provider = create_provider(str(cfg.get("provider")), cfg.get("provider_config") or {})
    if provider is None:
        return None
    supervisor = SemanticSupervisor(cfg, provider)
    _supervisors[key] = supervisor
    return supervisor


def collect_facts(
    task: dict,
    store=None,
    config: Optional[dict] = None,
    report_text: Optional[str] = None,
    trigger: Optional[str] = None,
    test_evidence: Optional[Dict[str, Any]] = None,
    previous: Optional[dict] = None,
) -> Dict[str, Any]:
    """Deterministic facts from the task record + real execution evidence.

    Evidence sources (bounded summaries only, never raw payloads):
    HERDR loop report (``.herdr-loop`` METRICS/STATE), git status/diff stat,
    Agent done report (verdict/blocker/status history + report tail).
    """
    cfg = config or {}
    runtime = task.get("runtime") if isinstance(task.get("runtime"), dict) else {}
    payload = task if isinstance(task, dict) else {}
    started_at = runtime.get("started_at") or payload.get("created_at")
    elapsed = None
    if started_at:
        elapsed = max(0.0, round(time.time() - float(started_at), 1))
    fix_loop = payload.get("fix_loop") if isinstance(payload.get("fix_loop"), dict) else {}
    status_history = payload.get("status_history")
    rework_count = sum(
        1
        for entry in status_history
        if isinstance(entry, dict) and entry.get("to") == "rework"
    ) if isinstance(status_history, list) else 0
    facts = {
        "trigger": trigger or "agent_done",
        "task_status": task.get("status"),
        "runtime_status": runtime.get("status"),
        "attempt_count": int(
            payload.get("attempt_count")
            or fix_loop.get("loop_count")
            or rework_count
            or 0
        ),
        "verification_count": int(payload.get("verification_count") or 0),
        "elapsed_seconds": elapsed,
        "auto_execute_allowed": bool(
            (cfg.get("policy") or {}).get("allow_auto_execute", True)
        ),
    }
    try:
        facts.update(
            evidence_layer.collect_execution_evidence(
                task,
                trigger=trigger,
                report_text=report_text,
            )
        )
    except Exception:
        pass
    if test_evidence:
        facts["tests"] = test_evidence

    if facts.get("tests"):
        prev_test_summary = None
        if isinstance(previous, dict):
            prev_test_summary = (
                (previous.get("metadata") or {}).get("test_summary")
                or (previous.get("facts") or {}).get("tests")
            )
        progress = evidence_layer.compute_test_progress(facts["tests"], prev_test_summary)
        if progress:
            facts["test_progress"] = progress

    return facts


def run_checkpoint(
    *,
    task: dict,
    trigger: str,
    store,
    actions: Optional[Dict[str, Callable[[dict, dict], Any]]] = None,
    config: Optional[dict] = None,
    supervisor: Optional[SemanticSupervisor] = None,
    report_reader: Optional[Callable[[], Optional[str]]] = None,
    test_evidence: Optional[Dict[str, Any]] = None,
    evidence_id: Optional[str] = None,
    now: Optional[float] = None,
    log: Callable[[str], None] = print,
) -> Optional[Dict[str, Any]]:
    """One supervised checkpoint. Returns None, or a dict carrying:

    - ``evaluation`` / ``decision``: the persisted records;
    - ``intercepted``: an enforced intervention owns this checkpoint, so the
      caller must NOT run its default continuation (e.g. the ``done`` event);
    - ``handled``: an orchestrator-supplied action handler executed;
    - ``continue_flow``: the caller may proceed with its default flow.

    ``actions`` maps PolicyDecision.action -> handler(task, decision_dict);
    handlers are the orchestrator's own existing flows (rework, attention,
    coordinator events). The harness itself changes no HAFlow state.
    """
    cfg = config or load_config()
    # Cheap kill-switches first: global enablement plus provider-specific
    # enablement (HERDR_SUPERVISOR_JEV_ENABLED=false -> zero provider traffic).
    if not cfg.get("enabled", False) or not provider_enabled(cfg):
        return None
    task_id = task.get("task_id")
    try:
        supervisor = supervisor or get_supervisor(cfg)
        if supervisor is None:
            return None
        # Pre-probe (non-mutating) so gated checkpoints skip evidence I/O.
        try:
            if supervisor.should_evaluate(task_id, trigger, now=now) is not None:
                return None
        except Exception:
            pass
        events: list = []
        previous: Optional[dict] = None
        if store is not None:
            try:
                events = store.list_events(task_id=task_id, limit=200, desc=True) or []
                previous = latest_evaluation(events)
            except Exception:
                events = []
        report_text = None
        if report_reader is not None:
            try:
                report_text = report_reader()
            except Exception:
                report_text = None
        facts = collect_facts(
            task,
            store=store,
            config=cfg,
            report_text=report_text,
            trigger=trigger,
            test_evidence=test_evidence,
            previous=previous,
        )
        evaluation = supervisor.evaluate(
            task,
            trigger,
            now=now,
            events=events,
            facts={k: v for k, v in facts.items() if k in (
                "attempt_count", "elapsed_seconds", "verification_count",
                *_FACT_EVIDENCE_KEYS)},
            previous_evaluation=previous,
        )
        if evaluation is None:
            return None

        # Augment evaluation metadata with evidence_id & test_summary for persistence & dedup
        meta = evaluation.setdefault("metadata", {})
        meta["trigger"] = trigger
        if evidence_id:
            meta["evidence_id"] = evidence_id
            evaluation["evidence_id"] = evidence_id
        tests_data = facts.get("tests")
        if tests_data:
            meta["test_summary"] = {
                "passed_tests": tests_data.get("passed_tests"),
                "total_tests": tests_data.get("total_tests"),
                "failing_count": tests_data.get("failing_count", len(tests_data.get("failing_tests") or [])),
                "composite_score": tests_data.get("composite_score"),
                "iteration": tests_data.get("iteration"),
                "evidence_id": evidence_id or tests_data.get("evidence_id"),
            }
        if facts.get("test_progress"):
            meta["test_progress"] = dict(facts["test_progress"])

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
        enforce_on = bool(cfg.get("enforce"))
        intercepted = bool(
            enforce_on and decision.action in policy_engine.INTERVENTION_ACTIONS
        )
        durable_action = decision.action in (ACTION_RETRY, ACTION_VERIFY)
        payload = {
            "decision_id": evaluation["evaluation_id"],
            "evaluation_id": evaluation["evaluation_id"],
            "task_id": task_id,
            "trigger": trigger,
            "enforced": intercepted,
            **decision.to_dict(),
        }
        if evidence_id:
            payload["evidence_id"] = evidence_id
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
        handled = False
        durable_intervention = None
        durable_request_attempted = False
        if intercepted and durable_action:
            if not hasattr(store, "create_intervention"):
                # Keep the pre-V1 handler contract for lightweight legacy
                # stores used by integrations/tests. The production
                # StateStore has create_intervention; a real persistence
                # failure on that path remains fail-safe below.
                log(
                    f"[SUPERVISOR INTERVENTION REQUEST SKIPPED] task={task_id}: "
                    "legacy store; using supplied handler"
                )
            else:
                durable_request_attempted = True
                try:
                    durable_intervention = request_intervention(
                        store, task, evaluation, payload, cfg,
                    )
                    if durable_intervention is not None:
                        payload["intervention"] = durable_intervention
                        if durable_intervention.get("status") == "completed":
                            # A replay of a completed canonical action is
                            # idempotent and may resume the default flow.
                            intercepted = False
                    else:
                        intercepted = False
                except Exception as exc:
                    intercepted = False
                    log(
                        f"[SUPERVISOR INTERVENTION REQUEST FAILED] task={task_id}: "
                        f"{type(exc).__name__}"
                    )
        if enforce_on and decision.action != policy_engine.CONTINUE:
            handler = (actions or {}).get(decision.action)
            can_execute = (
                not durable_request_attempted
                or (
                    durable_intervention is not None
                    and durable_intervention.get("status") == STATUS_REQUESTED
                )
            )
            if handler is not None and can_execute:
                try:
                    handler(task, payload)
                    handled = True
                except Exception as exc:
                    log(
                        f"[SUPERVISOR ACTION FAILED] task={task_id} "
                        f"action={decision.action}: {type(exc).__name__}"
                    )
            elif intercepted:
                log(
                    f"[SUPERVISOR INTERCEPTED-UNMAPPED] task={task_id} "
                    f"action={decision.action} (no handler; default flow blocked)"
                )
        if intercepted:
            log(
                f"[SUPERVISOR INTERCEPTED] task={task_id} action={decision.action} "
                f"handled={handled} -> default continuation blocked"
            )
        return {
            "evaluation": evaluation,
            "decision": payload,
            "intercepted": intercepted,
            "handled": handled,
            "continue_flow": not intercepted,
        }
    except Exception as exc:
        # Supervision failure is never a task failure.
        log(f"[SUPERVISOR SKIPPED] task={task_id} trigger={trigger}: {type(exc).__name__}: {exc}")
        return None


def reset_process_state() -> None:
    """Drop memoized supervisors/gates (tests / config reload)."""
    _supervisors.clear()


def _last_agent_done_at(task: dict) -> Optional[float]:
    history = task.get("status_history") if isinstance(task, dict) else None
    if not isinstance(history, list):
        return None
    for entry in reversed(history):
        if isinstance(entry, dict) and entry.get("to") == "agent_done":
            try:
                value = float(entry.get("timestamp") or 0)
            except (TypeError, ValueError):
                return None
            return value or None
    return None


def pending_intervention(task: dict, store, config: Optional[dict] = None
                         ) -> Optional[str]:
    """Latest enforced intervention not superseded by a newer agent_done.

    Durable guard for redelivery/recovery paths where the RateGate may skip a
    fresh evaluation: once HAFlow intercepted a completion, re-announcing it
    as ``done`` must not silently bypass the pending action. Returns None when
    supervision/enforcement is currently off, so disabling the supervisor
    restores the original behavior exactly.
    """
    cfg = config or load_config()
    if not cfg.get("enabled", False) or not cfg.get("enforce"):
        return None
    task_id = task.get("task_id")
    if not task_id or store is None:
        return None
    durable_supported = hasattr(store, "list_interventions")
    if durable_supported:
        try:
            from ..trajectory import run_id_for_task
            all_rows = store.list_interventions(
                run_id=run_id_for_task(task), task_id=task_id,
                limit=100,
            ) or []
            durable = [
                item for item in all_rows
                if item.get("status") in ("requested", "running")
            ]
            for item in durable:
                if item.get("action") in (ACTION_RETRY, ACTION_VERIFY):
                    return str(item["action"])
            # A durable V1 store is authoritative for VERIFY/RETRY. Do not
            # resurrect a completed/failed request from a legacy policy event.
            last_done = _last_agent_done_at(task)
            v1_rows = all_rows
            for item in v1_rows:
                if item.get("action") not in (ACTION_RETRY, ACTION_VERIFY):
                    continue
                if item.get("status") == "superseded":
                    continue
                boundary = float(item.get("finished_at") or item.get("requested_at") or 0)
                if last_done is None or boundary >= last_done:
                    return str(item["action"])
            if any(item.get("action") in (ACTION_RETRY, ACTION_VERIFY) for item in v1_rows):
                return None
        except Exception:
            return None
    if not supervisor_enabled(cfg):
        return None
    try:
        events = store.list_events(task_id=task_id, limit=100, desc=True) or []
    except Exception:
        return None

    latest_action = None
    latest_ts = -1.0
    for event in events:
        if not isinstance(event, dict) or event.get("event_type") != POLICY_EVENT:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("enforced") is not True:
            # Observe-mode decisions never block the default flow.
            continue
        try:
            ts = float(event.get("timestamp") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts >= latest_ts:
            latest_ts = ts
            latest_action = str(payload.get("action") or "")
    if latest_action not in policy_engine.INTERVENTION_ACTIONS:
        return None
    since = _last_agent_done_at(task)
    if since is not None and latest_ts < since:
        return None
    return latest_action


__all__ = [
    "EVALUATION_EVENT",
    "POLICY_EVENT",
    "RateGate",
    "collect_facts",
    "get_supervisor",
    "pending_intervention",
    "reset_process_state",
    "run_checkpoint",
]
