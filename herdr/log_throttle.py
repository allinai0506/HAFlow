"""Process-local log throttles with injectable clocks.

Throttle state is deliberately not persisted: a Controller restart may print
one extra line, which stays bounded and avoids a second fact source.
"""
import threading
import time

DEFAULT_INTERVAL_SECONDS = 600.0
MAX_TRACKED_WORKFLOWS = 1024


class StartupWaitLog:
    """At most one ``[STARTUP WAIT]`` line per workflow per interval, per process."""

    def __init__(
        self,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        clock=None,
        max_entries: int = MAX_TRACKED_WORKFLOWS,
    ) -> None:
        self._interval = float(interval_seconds)
        self._clock = clock or time.monotonic
        self._max_entries = max(1, int(max_entries))
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def _now(self, now):
        return float(self._clock() if now is None else now)

    def waiting(self, workflow_id: str, now=None) -> bool:
        """Return True only when this emission is allowed; record it."""
        now = self._now(now)
        with self._lock:
            last = self._last.pop(workflow_id, None)
            if last is not None and now - last < self._interval:
                self._last[workflow_id] = last
                return False
            self._last[workflow_id] = now
            while len(self._last) > self._max_entries:
                self._last.pop(next(iter(self._last)))
            return True

    def finished(self, workflow_id: str) -> bool:
        """Consume the latch once so a state change is announced at most once."""
        with self._lock:
            return self._last.pop(workflow_id, None) is not None

    def clear(self, workflow_id: str) -> None:
        with self._lock:
            self._last.pop(workflow_id, None)


FALLBACK_STARTUP_WAIT_LOG = StartupWaitLog()


def startup_wait_log_for(ports):
    return getattr(ports, "_startup_wait_log", None) or FALLBACK_STARTUP_WAIT_LOG


def startup_wait_hold(ports, workflow_id, workflow_record, message=""):
    """Return True while the workflow must wait for startup; log at a bounded rate."""
    log = startup_wait_log_for(ports)
    if workflow_record.get("startup_ready") is not False:
        if log.finished(workflow_id):
            print(f"[STARTUP READY] workflow={workflow_id}")
        return False
    if log.waiting(workflow_id, ports.time.time()):
        suffix = f" {message}" if message else ""
        print(f"[STARTUP WAIT] workflow={workflow_id}{suffix}")
    return True


def startup_wait_closed(ports, workflow_id):
    if startup_wait_log_for(ports).finished(workflow_id):
        print(f"[STARTUP WAIT ENDED] workflow={workflow_id} closed")
