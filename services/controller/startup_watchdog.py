"""Bounded startup-orphan recovery for workflows that never became ready.

The Controller sweep is the only actor allowed to move workflow status, so the
watchdog lives next to the stage-advance sweep. It scans at most once per
interval, examines a bounded keyset page, and reclaims a bounded number of
records per scan; every reclaim re-reads authoritative fields inside a single
``BEGIN IMMEDIATE`` transaction.
"""
from herdr.state_store import sync_workflows_projection
from herdr.workflow_startup import (
    SOURCE_WATCHDOG, find_startup_orphan_candidates, reclaim_startup_orphan,
    startup_deadline_seconds,
)

SCAN_INTERVAL_SECONDS = 60.0
MAX_CANDIDATES_PER_SCAN = 100
MAX_RECLAIM_PER_SCAN = 20


def _sync_projection(store) -> bool:
    try:
        return bool(sync_workflows_projection(store=store))
    except Exception as exc:
        print(f"[STARTUP WATCHDOG PROJECTION WARN] {type(exc).__name__}: {exc}")
        return False


def _retry_stale_projection(ports, store) -> None:
    """Compensate a projection write that failed after a committed reclaim."""
    if not getattr(ports, "_startup_projection_stale", False):
        return
    if _sync_projection(store):
        ports._startup_projection_stale = False


def _advance_cursor(ports, store, candidates, examined) -> None:
    """Bounded keyset progress so a foreign backlog cannot starve later orphans."""
    if not candidates:
        ports._startup_watchdog_cursor = None
        return
    if examined < len(candidates):
        last = candidates[examined - 1]
    elif len(candidates) < MAX_CANDIDATES_PER_SCAN:
        ports._startup_watchdog_cursor = None
        return
    else:
        last = candidates[-1]
    try:
        created_at = (store.get_workflow(last) or {}).get("created_at")
    except Exception:
        created_at = None
    if created_at is None:
        ports._startup_watchdog_cursor = None
        return
    ports._startup_watchdog_cursor = (float(created_at), last)


def scan_startup_orphans(ports, now=None):
    now = ports.time.time() if now is None else float(now)
    if now - float(ports._startup_watchdog_scan_at) < SCAN_INTERVAL_SECONDS:
        return []
    ports._startup_watchdog_scan_at = now
    store = ports._get_store()
    _retry_stale_projection(ports, store)
    deadline = startup_deadline_seconds(
        ports.os.environ, warn=lambda msg: print(msg))
    candidates = find_startup_orphan_candidates(
        store.db_path, now, deadline,
        limit=MAX_CANDIDATES_PER_SCAN,
        after=getattr(ports, "_startup_watchdog_cursor", None),
    )
    reclaimed = []
    examined = 0
    for wid in candidates:
        if len(reclaimed) >= MAX_RECLAIM_PER_SCAN:
            break
        examined += 1
        try:
            result = reclaim_startup_orphan(
                store, wid, now, deadline,
                foreign_check=ports.liveness.workflow_is_foreign,
                source=SOURCE_WATCHDOG,
            )
        except Exception as exc:
            print(f"[STARTUP WATCHDOG ERROR] workflow={wid} "
                  f"{type(exc).__name__}: {exc}")
            continue
        if result.get("ok"):
            print(f"[STARTUP WATCHDOG RECLAIMED] workflow={wid} deadline={deadline}s")
            reclaimed.append(wid)
    _advance_cursor(ports, store, candidates, examined)
    if not reclaimed:
        return []
    if not _sync_projection(store):
        ports._startup_projection_stale = True
    for wid in reclaimed:
        try:
            ports.clear_workflow_stage_state(wid)
        except Exception as exc:
            print(f"[STARTUP WATCHDOG STAGE CLEANUP WARN] workflow={wid} "
                  f"{type(exc).__name__}: {exc}")
    return reclaimed


DEPENDENCIES = {
    "scan_startup_orphans": (
        "_startup_watchdog_scan_at", "_startup_watchdog_cursor",
        "_startup_projection_stale", "_get_store", "clear_workflow_stage_state",
        "liveness", "os", "time",
    ),
}
__all__ = tuple(DEPENDENCIES)
