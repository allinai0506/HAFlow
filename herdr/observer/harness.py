#!/usr/bin/env python3
"""Trajectory Observer harness (herdr/observer/harness.py).

Public, fail-safe entry points for the observer:

- ``observe_run``: synchronous one-shot observation (CLI, tests, callers);
- ``list_findings``: read persisted findings for a run;
- ``ObservationScheduler`` / ``submit_observation``: the controller-side
  non-blocking trigger. Submissions run on daemon worker threads so a slow or
  broken provider can never delay the registry polling loop, and every
  exception dies inside the worker.

Provider construction reuses the shared ``herdr/decision`` registry; the
observer package never imports a concrete backend.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from ..decision import create_provider
from ..trajectory import run_id_for_task
from ..supervisor.engine import RateGate
from . import config as observer_config
from .engine import TrajectoryObserver, stderr_log
from .models import TrajectoryFinding

LOGGER = logging.getLogger(__name__)

_providers: Dict[str, Any] = {}
_providers_lock = threading.Lock()

# Bound for the once-per-run terminal memory (one day is far beyond any run).
TERMINAL_SEEN_TTL_SECONDS = 86400.0


def _provider_signature(config: Dict[str, Any]) -> str:
    return json.dumps({
        "provider": config.get("provider"),
        "jev": {
            key: (config.get("jev") or {}).get(key)
            for key in ("enabled", "model", "base_url", "api_key_env", "timeout")
        },
        "provider_config": config.get("provider_config"),
    }, sort_keys=True, default=str)


def get_provider(config: Optional[Dict[str, Any]] = None):
    """Build (and memoize per effective config) the observer's provider."""
    cfg = config or observer_config.load_config()
    if not observer_config.provider_enabled(cfg):
        return None
    signature = _provider_signature(cfg)
    with _providers_lock:
        if signature in _providers:
            return _providers[signature]
    name = str(cfg.get("provider") or "")
    if name == "jev":
        provider = create_provider("jev", observer_config.jev_provider_config(cfg))
    else:
        provider = create_provider(name, cfg.get("provider_config") or {})
    with _providers_lock:
        _providers[signature] = provider
    return provider


def _default_probes(cfg: Dict[str, Any]):
    """Bounded read-only live probes (pane/agent liveness + pane transcript).

    Built only when ``live_probe`` is enabled; the callables are resolved at
    call time so tests and operators can swap the live module.
    """
    if not cfg.get("live_probe", True):
        return None, None
    from . import live as live_module

    probe_timeout = float(cfg.get("live_probe_timeout", 2.0))

    def runtime_probe(task):
        return live_module.probe_live_runtime(task, timeout=probe_timeout)

    def transcript_reader(task):
        return live_module.read_live_transcript(task, cfg)

    return runtime_probe, transcript_reader


def observe_run(
    run_id: str,
    *,
    task: Optional[Dict[str, Any]] = None,
    store: Any = None,
    config: Optional[Dict[str, Any]] = None,
    provider: Any = None,
    ledger: Any = None,
    now: Optional[float] = None,
    use_model: bool = True,
    runtime_probe: Any = None,
    transcript_reader: Any = None,
    log: Any = None,
) -> List[TrajectoryFinding]:
    """Observe one run and return its current findings (never raises)."""
    if not run_id:
        return []
    try:
        cfg = config or observer_config.load_config()
        if provider is None and use_model:
            try:
                provider = get_provider(cfg)
            except Exception as exc:
                # Provider construction failure must never disable the
                # deterministic observer: evidence-backed findings
                # (requires_confirmation=false) still flow; weak signals stay
                # silent without a confirmer.
                message = f"[OBSERVER PROVIDER SKIPPED] {type(exc).__name__}: {exc}"
                LOGGER.warning(message)
                stderr_log(message)
                provider = None
        if runtime_probe is None and transcript_reader is None:
            runtime_probe, transcript_reader = _default_probes(cfg)
        observer = TrajectoryObserver(
            config=cfg,
            provider=provider,
            store=store,
            ledger=ledger,
            runtime_probe=runtime_probe,
            transcript_reader=transcript_reader,
            log=log,
        )
        return observer.observe_run(run_id, task=task, now=now, use_model=use_model)
    except Exception as exc:  # the observer is best-effort by contract
        LOGGER.warning("observer skipped: run=%s error=%s: %s", run_id, type(exc).__name__, exc)
        return []


class ObservationScheduler:
    """Bounded, non-blocking, per-run-deduplicated observation dispatcher.

    The per-run budget is process-local (RateGate bookkeeping): it limits
    provider/live-probe spend inside one controller process and resets on
    controller restart. No observation counters are persisted in V1.
    """

    def __init__(
        self,
        *,
        observe=None,
        config: Optional[Dict[str, Any]] = None,
        max_concurrent: int = 2,
        log=stderr_log,
    ) -> None:
        self._observe = observe or observe_run
        self._config = config or observer_config.load_config()
        self._max_concurrent = max(1, int(max_concurrent))
        self._gate = RateGate()
        # Process-local per-run observation budget (resets on restart).
        self._gate_config = {
            "interval": float(self._config.get("interval", 300)),
            "cooldown": 0,
            "max_calls_per_task": int(self._config.get("max_calls_per_run", 24)),
        }
        self._lock = threading.Lock()
        self._inflight: set = set()
        self._threads: List[threading.Thread] = []
        self._terminal_seen: Dict[str, float] = {}
        self.log = log

    def submit(
        self,
        run_id: str,
        *,
        task: Optional[Dict[str, Any]] = None,
        store: Any = None,
        now: Optional[float] = None,
    ) -> bool:
        """Queue one observation. Returns False when gated/disabled/duplicate."""
        if not run_id or not self._config.get("enabled", True):
            return False
        with self._lock:
            if run_id in self._inflight or len(self._inflight) >= self._max_concurrent:
                return False
            skip = self._gate.check(run_id, "observe", self._gate_config, now=now)
            if skip:
                return False
            self._inflight.add(run_id)
            self._gate.record(run_id, "observe", now=now)
            return self._spawn_locked(run_id, run_id, task, store)

    def submit_terminal(
        self,
        run_id: str,
        *,
        task: Optional[Dict[str, Any]] = None,
        store: Any = None,
        now: Optional[float] = None,
    ) -> bool:
        """One terminal (agent_done) observation per run per controller process.

        Independent of the periodic interval/budget gate: a working observation
        taken seconds earlier must not swallow the completion checkpoint where
        ``verification_failure`` lives. Still asynchronous, bounded, and
        deduplicated; failures can never block the caller's done flow.
        """
        if not run_id or not self._config.get("enabled", True):
            return False
        ts = now if now is not None else time.time()
        key = f"terminal:{run_id}"
        with self._lock:
            self._prune_terminal_seen(ts)
            if run_id in self._terminal_seen:
                return False
            terminal_inflight = sum(
                1 for entry in self._inflight if str(entry).startswith("terminal:")
            )
            if terminal_inflight >= self._max_concurrent:
                return False  # not marked seen: retried on the next tick
            self._terminal_seen[run_id] = ts
            self._inflight.add(key)
            started = self._spawn_locked(key, run_id, task, store)
            if not started:
                self._terminal_seen.pop(run_id, None)  # retry on the next tick
            return started

    def _spawn_locked(
        self, key: str, run_id: str, task: Optional[Dict[str, Any]], store: Any,
    ) -> bool:
        """Spawn the worker thread; caller holds the lock."""
        self._threads = [thread for thread in self._threads if thread.is_alive()]
        thread = threading.Thread(
            target=self._run,
            args=(key, run_id, task, store),
            name=f"trajectory-observer-{run_id}",
            daemon=True,
        )
        self._threads.append(thread)
        try:
            thread.start()
        except Exception as exc:
            self._inflight.discard(key)
            self._log(f"[OBSERVER WORKER START FAILED] run={run_id}: {type(exc).__name__}")
            return False
        return True

    def _prune_terminal_seen(self, now: float) -> None:
        """Bound the once-per-run memory (caller holds the lock)."""
        cutoff = now - TERMINAL_SEEN_TTL_SECONDS
        self._terminal_seen = {
            run_id: seen_at
            for run_id, seen_at in self._terminal_seen.items()
            if seen_at >= cutoff
        }

    def _run(self, key: str, run_id: str, task: Optional[Dict[str, Any]], store: Any) -> None:
        try:
            self._observe(run_id, task=task, store=store, config=self._config)
        except Exception as exc:  # a worker failure must die here
            self._log(f"[OBSERVER WORKER FAILED] run={run_id}: {type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self._inflight.discard(key)

    def in_flight(self) -> List[str]:
        with self._lock:
            return sorted(self._inflight)

    def drain(self, timeout: float = 5.0) -> None:
        """Wait for queued workers (tests/shutdown); bounded by ``timeout``."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        with self._lock:
            self._threads = [thread for thread in self._threads if thread.is_alive()]

    def _log(self, message: str) -> None:
        LOGGER.warning(message)
        try:
            self.log(message)
        except Exception:
            pass


_DEFAULT_SCHEDULER: Optional[ObservationScheduler] = None
_DEFAULT_LOCK = threading.Lock()


def _default_scheduler() -> ObservationScheduler:
    global _DEFAULT_SCHEDULER
    with _DEFAULT_LOCK:
        if _DEFAULT_SCHEDULER is None:
            _DEFAULT_SCHEDULER = ObservationScheduler()
        return _DEFAULT_SCHEDULER


def submit_observation(
    task: Optional[Dict[str, Any]],
    *,
    store: Any = None,
    now: Optional[float] = None,
) -> bool:
    """Controller-side trigger: queue an observation for a live task's run."""
    if not isinstance(task, dict):
        return False
    try:
        run_id = run_id_for_task(task)
    except Exception:
        return False
    return _default_scheduler().submit(run_id, task=task, store=store, now=now)


def submit_terminal_observation(
    task: Optional[Dict[str, Any]],
    *,
    store: Any = None,
    now: Optional[float] = None,
) -> bool:
    """Controller-side terminal trigger for ``agent_done`` (once per run).

    Independent of the periodic gate so the completion checkpoint is never
    swallowed by an observation taken seconds earlier; failures cannot block
    the caller's done flow.
    """
    if not isinstance(task, dict):
        return False
    try:
        run_id = run_id_for_task(task)
    except Exception:
        return False
    return _default_scheduler().submit_terminal(run_id, task=task, store=store, now=now)


def reset_process_state() -> None:
    """Drop memoized providers/scheduler (tests / config reload)."""
    global _DEFAULT_SCHEDULER
    with _providers_lock:
        _providers.clear()
    with _DEFAULT_LOCK:
        _DEFAULT_SCHEDULER = None


__all__ = [
    "ObservationScheduler",
    "get_provider",
    "observe_run",
    "reset_process_state",
    "submit_observation",
    "submit_terminal_observation",
]
