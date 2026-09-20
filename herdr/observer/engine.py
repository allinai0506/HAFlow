#!/usr/bin/env python3
"""Trajectory Observer engine (herdr/observer/engine.py).

Imperative-ish core for one observation: read the fact ledger, detect
deterministic signals, build a bounded context, ask the shared
DecisionProvider to confirm, then persist deduplicated findings.

Fail-safe contract (mirrors the Semantic Supervisor harness):
- any failure returns the best safe answer (``[]`` on infrastructure failure,
  evidence-backed findings when only the provider failed) and never raises;
- the observer only ever writes the ``trajectory_findings`` analysis table; it
  never mutates tasks, workflows, runtime state, or the events ledger;
- no provider is imported here: the caller injects a ``DecisionProvider``.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import state_db
from ..decision.models import DecisionProviderError, clamp_probability
from ..supervisor.state import redact_text
from ..trajectory import TrajectoryLedger
from . import context as observation_context
from . import signals as signal_layer
from .config import load_config
from .models import TrajectoryFinding

LOGGER = logging.getLogger(__name__)

_REDACTED_EVIDENCE_FIELDS = ("excerpt", "signature")


def _redact_evidence(evidence: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Defense-in-depth: credential-shaped text never persists in evidence."""
    redacted: List[Dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, dict):
            redacted.append(item)
            continue
        cleaned = dict(item)
        for key in _REDACTED_EVIDENCE_FIELDS:
            if isinstance(cleaned.get(key), str):
                cleaned[key] = redact_text(cleaned[key])
        redacted.append(cleaned)
    return redacted


class TrajectoryObserver:
    """One observer bound to a config/provider/store; owns no task authority."""

    def __init__(
        self,
        *,
        config: Optional[Dict[str, Any]] = None,
        provider: Any = None,
        store: Any = None,
        ledger: Optional[TrajectoryLedger] = None,
        log=print,
    ) -> None:
        self.config = config or load_config()
        self.provider = provider
        self.store = store
        self.log = log
        self.db_path = self._resolve_db_path(store)
        self.ledger = ledger or TrajectoryLedger(self.db_path)

    @staticmethod
    def _resolve_db_path(store: Any) -> Path:
        if store is not None:
            db_path = getattr(store, "db_path", None)
            if db_path:
                return Path(db_path)
        return state_db.get_default_db_path()

    def observe_run(
        self,
        run_id: str,
        *,
        task: Optional[Dict[str, Any]] = None,
        now: Optional[float] = None,
        use_model: bool = True,
    ) -> List[TrajectoryFinding]:
        """Observe one run; always returns a list, never raises."""
        try:
            return self._observe(run_id, task=task, now=now, use_model=use_model)
        except Exception as exc:  # observation failure is never a task failure
            self._log(f"[OBSERVER SKIPPED] run={run_id}: {type(exc).__name__}: {exc}")
            return []

    def _observe(
        self,
        run_id: str,
        *,
        task: Optional[Dict[str, Any]],
        now: Optional[float],
        use_model: bool,
    ) -> List[TrajectoryFinding]:
        if not self.config.get("enabled", True):
            return []
        ts = now if now is not None else time.time()
        task = task if isinstance(task, dict) else None

        events = self.ledger.list_events(run_id)
        if task is None:
            task = self._resolve_task(run_id, events)
        if not events and task is None:
            return []
        runtime = (task or {}).get("runtime")
        runtime = runtime if isinstance(runtime, dict) else {}

        log_tail = None
        if task is not None:
            try:
                log_tail = observation_context.read_log_tail(task, self.config)
            except Exception:
                log_tail = None

        signals = signal_layer.detect_signals(
            run_id=run_id,
            events=events,
            task=task,
            runtime=runtime,
            log_tail=log_tail,
            now=ts,
            config=self.config,
        )
        if not signals:
            return []

        context = observation_context.build_observation_context(
            run_id=run_id,
            task=task,
            events=events,
            runtime=runtime,
            log_tail=log_tail,
            signals=signals,
            config=self.config,
            now=ts,
        )
        results = self._ask_provider(signals, context, use_model=use_model)
        candidates = self._consolidate(run_id, task, runtime, events, signals, results, ts)
        return self._persist(candidates)

    def _resolve_task(
        self, run_id: str, events: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Resolve the task for a run so callers only need the run_id.

        Order: the task_id carried by the run's own trajectory events, then a
        persisted run_id match (covers a launched run whose events are not
        written yet). A task whose persisted run_id differs from the observed
        run (stale events after a re-launch) is never trusted; lookup failures
        degrade to events-only observation.
        """
        task_id = next(
            (event.get("task_id") for event in events if event.get("task_id")), None,
        )
        if task_id:
            try:
                task = state_db.get_task(str(task_id), db_path=self.db_path)
                if isinstance(task, dict) and self._task_matches_run(task, run_id):
                    return task
            except Exception as exc:
                self._log(f"[OBSERVER TASK LOOKUP SKIPPED] run={run_id}: {type(exc).__name__}")
        try:
            for candidate in state_db.list_tasks(db_path=self.db_path):
                if isinstance(candidate, dict) and candidate.get("run_id") == run_id:
                    return candidate
        except Exception as exc:
            self._log(f"[OBSERVER TASK LOOKUP SKIPPED] run={run_id}: {type(exc).__name__}")
        return None

    @staticmethod
    def _task_matches_run(task: Dict[str, Any], run_id: str) -> bool:
        """True when the task belongs to this run (legacy tasks have no run_id)."""
        task_run_id = task.get("run_id")
        return not task_run_id or str(task_run_id) == run_id

    def _ask_provider(
        self,
        signals: List[signal_layer.Signal],
        context: Dict[str, Any],
        *,
        use_model: bool,
    ) -> Dict[str, Any]:
        """One batched provider call; returns {} when unavailable/disabled."""
        provider = self.provider if use_model else None
        if provider is None:
            return {}
        try:
            if not provider.available():
                return {}
            questions = {
                signal.finding_type: signal_layer.question_for(signal)
                for signal in signals
            }
            return provider.judge_many(questions, context) or {}
        except DecisionProviderError as exc:
            self._log(f"[OBSERVER PROVIDER FAILED] kind={exc.kind}: {exc}")
            return {}
        except Exception as exc:  # a provider must never break the observer
            self._log(f"[OBSERVER PROVIDER FAILED] unexpected: {type(exc).__name__}")
            return {}

    def _consolidate(
        self,
        run_id: str,
        task: Optional[Dict[str, Any]],
        runtime: Dict[str, Any],
        events: List[Dict[str, Any]],
        signals: List[signal_layer.Signal],
        results: Dict[str, Any],
        now: float,
    ) -> List[TrajectoryFinding]:
        threshold = float(self.config.get("confidence_threshold", 0.6))
        last = events[-1] if events else {}
        task = task or {}
        node = task.get("node") or task.get("stage") or last.get("node") or last.get("stage")
        agent = task.get("agent") or last.get("agent")
        agent_session_id = runtime.get("agent_session_id") or last.get("agent_session_id")

        findings: List[TrajectoryFinding] = []
        for signal in signals:
            result = results.get(signal.finding_type)
            probability = clamp_probability(getattr(result, "value", None))
            if probability is not None and probability < threshold:
                continue  # the provider explicitly denied this candidate
            if probability is None and signal.requires_confirmation:
                continue  # weak signal without model confirmation: stay quiet
            confidence = probability if probability is not None else signal.confidence
            findings.append(TrajectoryFinding(
                run_id=run_id,
                finding_type=signal.finding_type,
                severity=signal.severity,
                summary=redact_text(signal.summary),
                task_id=task.get("task_id") or last.get("task_id"),
                workflow_id=task.get("workflow_id") or last.get("workflow_id"),
                node=node,
                agent=agent,
                agent_session_id=agent_session_id,
                created_at=now,
                evidence=_redact_evidence(signal.evidence),
                suspected_cause=(
                    redact_text(signal.suspected_cause) if signal.suspected_cause
                    else signal.suspected_cause
                ),
                recommended_action=signal.recommended_action,
                confidence=confidence,
                metadata={
                    "anchor": signal.anchor,
                    "provider": getattr(self.provider, "name", None),
                    "model_confirmed": probability is not None,
                    "facts": {
                        key: (redact_text(value) if isinstance(value, str) else value)
                        for key, value in signal.facts.items()
                    },
                    "total_events": len(events),
                },
            ))
        return findings[: max(1, int(self.config.get("max_findings", 10)))]

    def _persist(self, findings: List[TrajectoryFinding]) -> List[TrajectoryFinding]:
        """Write only new keys; return each key's canonical persisted finding."""
        stored: List[TrajectoryFinding] = []
        seen = set()
        for finding in findings:
            if finding.finding_key in seen:
                continue
            seen.add(finding.finding_key)
            canonical = None
            try:
                canonical = state_db.record_trajectory_finding(
                    finding.to_mapping(), db_path=self.db_path,
                )
            except Exception as exc:
                self._log(f"[OBSERVER STORE ERROR] write: {type(exc).__name__}: {exc}")
                try:
                    canonical = state_db.get_trajectory_finding(
                        finding.finding_key, db_path=self.db_path,
                    )
                except Exception:
                    canonical = None
            if canonical is not None:
                stored.append(TrajectoryFinding.from_mapping(canonical))
            else:
                stored.append(finding)  # store unavailable: report best-effort in-memory
        return stored

    def _log(self, message: str) -> None:
        LOGGER.warning(message)
        try:
            self.log(message)
        except Exception:
            pass


def list_findings(
    run_id: Optional[str] = None,
    *,
    store: Any = None,
    finding_type: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[TrajectoryFinding]:
    """Read persisted findings (open set) for a run, newest last."""
    db_path = TrajectoryObserver._resolve_db_path(store)
    rows = state_db.list_trajectory_findings(
        run_id=run_id, finding_type=finding_type, limit=limit, db_path=db_path,
    )
    return [TrajectoryFinding.from_mapping(row) for row in rows]


__all__ = ["TrajectoryObserver", "list_findings"]
