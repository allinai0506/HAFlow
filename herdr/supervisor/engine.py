#!/usr/bin/env python3
"""SemanticSupervisor engine (herdr/supervisor/engine.py).

Functional Core (rate bookkeeping is passed-in state, no I/O): turns one
checkpoint into zero or one SupervisorEvaluation.

Cost/safety contract:
- one batched provider call per evaluation (all signals, one request);
- minimum interval + cooldown per task -> repeated events aggregate into
  the single evaluation that fires;
- hard max_calls_per_task budget -> supervision can never run away;
- provider failure NEVER propagates: a failed evaluation record (or None)
  comes back, and the caller's task flow is untouched.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

from ..decision.models import DecisionProviderError
from .config import provider_enabled
from .evaluation import build_evaluation
from .signals import signal_questions
from .state import build_supervisor_state

TRIGGER_PREFIX = "supervisor:"


class RateGate:
    """Per-task evaluation budget and quiet windows (plain dict bookkeeping).

    Listener threads and the registry watcher may checkpoint concurrently, so
    bookkeeping is lock-protected; the worst remaining race is a duplicate
    provider call inside one interval, never state corruption.
    """

    def __init__(self, state: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self._lock = threading.Lock()
        self._tasks: Dict[str, Dict[str, Any]] = {}
        for task_id, entry in (state or {}).items():
            self._tasks[task_id] = dict(entry)

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {task_id: dict(entry) for task_id, entry in self._tasks.items()}

    def _entry(self, task_id: str) -> Dict[str, Any]:
        return self._tasks.setdefault(
            task_id, {"calls": 0, "last_at": None, "last_trigger": None}
        )

    def check(self, task_id: str, trigger: str, config: dict,
              now: Optional[float] = None) -> Optional[str]:
        """None = allowed; otherwise the reason evaluation is being skipped."""
        ts = now if now is not None else time.time()
        with self._lock:
            entry = self._entry(task_id)
            if entry["calls"] >= int(config.get("max_calls_per_task", 12)):
                return "budget_exhausted"
            last_at = entry.get("last_at")
            if last_at is not None:
                since = ts - float(last_at)
                if since < float(config.get("interval", 300)):
                    return "min_interval"
                if since < float(config.get("cooldown", 120)) and trigger == entry.get("last_trigger"):
                    return "cooldown_duplicate"
            return None

    def record(self, task_id: str, trigger: str,
               now: Optional[float] = None) -> None:
        with self._lock:
            entry = self._entry(task_id)
            entry["calls"] = int(entry["calls"]) + 1
            entry["last_at"] = now if now is not None else time.time()
            entry["last_trigger"] = trigger


class SemanticSupervisor:
    """Checkpoint -> (optionally) one evaluation. Owns no task authority."""

    def __init__(self, config: dict, provider, gate: Optional[RateGate] = None) -> None:
        self.config = config
        self.provider = provider
        self.gate = gate or RateGate()

    def should_evaluate(self, task_id: Optional[str], trigger: str,
                        now: Optional[float] = None) -> Optional[str]:
        """None when the checkpoint may proceed; else the skip reason."""
        if not task_id:
            return "no_task_id"
        if not self.config.get("enabled", False):
            return "disabled"
        if not provider_enabled(self.config):
            return "provider_disabled"
        provider = self.provider
        if provider is None:
            return "no_provider"
        try:
            if not provider.available():
                return "provider_unavailable"
        except Exception:
            return "provider_unavailable"
        return self.gate.check(str(task_id), trigger, self.config, now=now)

    def evaluate(
        self,
        task: dict,
        trigger: str,
        *,
        now: Optional[float] = None,
        events: Optional[list] = None,
        facts: Optional[dict] = None,
        previous_evaluation: Optional[dict] = None,
    ) -> Optional[Dict[str, Any]]:
        """Run one supervised evaluation. Fail-safe: returns a record or None."""
        ts = now if now is not None else time.time()
        task_id = str(task.get("task_id") or "")
        skip = self.should_evaluate(task_id, trigger, now=ts)
        if skip:
            return None
        questions = signal_questions(self.config.get("signals"))
        state = build_supervisor_state(
            task,
            now=ts,
            events=events,
            facts=facts,
            previous_evaluation=previous_evaluation,
            max_context_size=int(self.config.get("max_context_size", 8000)),
            recent_events_limit=int(self.config.get("recent_events_limit", 15)),
        )
        self.gate.record(task_id, trigger, now=ts)
        try:
            results = self.provider.judge_many(questions, state)
            error = None
        except DecisionProviderError as exc:
            results, error = {}, f"{exc.kind}: {exc}"
        except Exception as exc:  # a provider must never break HAFlow
            results, error = {}, f"unexpected: {type(exc).__name__}"
        evaluation = build_evaluation(
            task_id=task.get("task_id"),
            workflow_id=task.get("workflow_id"),
            trigger=trigger,
            provider=getattr(self.provider, "name", "unknown"),
            results=results,
            requested_signals=list(questions),
            previous=previous_evaluation,
            latency_ms=_max_latency(results),
            fallback_used=bool(error and results),
            error=error,
            metadata={"state_keys": sorted(state.keys()),
                      "signals_requested": list(questions)},
            now=ts,
        )
        return evaluation


def _max_latency(results: dict) -> Optional[float]:
    latencies = [
        r.latency_ms for r in results.values() if r.latency_ms is not None
    ]
    return max(latencies) if latencies else None
