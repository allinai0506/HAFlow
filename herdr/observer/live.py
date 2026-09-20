#!/usr/bin/env python3
"""Read-only live execution probes for the Trajectory Observer.

Shell-lite helpers that answer two questions the persisted task record cannot:

- is the task's Pane / Agent session *currently* alive (``probe_live_runtime``);
- what is the Agent writing to its Pane right now (``read_live_transcript``).

Both reuse the existing bounded herdr CLI surface the controller already
depends on (``herdr pane list/get``, ``herdr agent get``, ``herdr pane read``),
run with an explicit timeout, never mutate anything, and degrade to
``unknown`` / ``None`` on any failure. A *failed* probe is never evidence of
unavailability: only a successful pane listing that omits the pane (or a
successful pane get) proves liveness. Transcripts are bounded and redacted
before they can reach a provider or a finding.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, Callable, Dict, Optional, Tuple

from . import context as observation_context

LOGGER = logging.getLogger(__name__)

AVAILABLE = "available"
UNAVAILABLE = "unavailable"
UNKNOWN = "unknown"

Runner = Callable[..., Tuple[int, str, str]]


def _run_herdr(argv, timeout: float) -> Tuple[int, str, str]:
    """One bounded, read-only herdr CLI call (stderr never raises)."""
    result = subprocess.run(
        list(argv), text=True, capture_output=True, timeout=max(0.1, float(timeout)),
    )
    return result.returncode, result.stdout or "", result.stderr or ""


def _call(
    runner: Runner, argv, timeout: float, log=None,
) -> Optional[Tuple[int, str, str]]:
    try:
        return runner(argv, timeout)
    except Exception as exc:  # timeout / missing binary / daemon down -> unknown
        _log(log, f"[OBSERVER LIVE PROBE SKIPPED] {' '.join(argv[:3])}: {type(exc).__name__}")
        return None


def _pane_ids(payload: str) -> Optional[set]:
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    result = data.get("result", data) if isinstance(data, dict) else None
    if isinstance(result, list):
        panes = result
    elif isinstance(result, dict):
        panes = result.get("panes")
    else:
        return None
    if not isinstance(panes, list):
        return None
    return {
        str(pane.get("pane_id"))
        for pane in panes
        if isinstance(pane, dict) and pane.get("pane_id")
    }


def _agent_status(payload: str) -> Optional[str]:
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    agent = data.get("result", {}).get("agent") if isinstance(data, dict) else None
    if not isinstance(agent, dict):
        return None
    status = agent.get("agent_status")
    return str(status) if status is not None else None


def probe_live_runtime(
    task: Dict[str, Any],
    *,
    runner: Optional[Runner] = None,
    timeout: float = 2.0,
    log=None,
) -> Dict[str, Any]:
    """Live availability of the task's Pane / Agent session (never raises).

    ``unavailable`` requires positive structural evidence (a successful pane
    listing/get that does not contain the task's pane). Every probe failure
    (timeout, non-zero exit, unparseable output, daemon down) is ``unknown``:
    the observer must not claim a dead runtime it cannot prove.
    """
    try:
        runtime = task.get("runtime") if isinstance(task.get("runtime"), dict) else {}
        pane_id = task.get("pane_id") or runtime.get("pane_id")
        if not pane_id:
            return {"status": UNKNOWN, "reason": "no_pane_id"}
        pane_id = str(pane_id)
        workspace_id = task.get("workspace_id") or runtime.get("workspace_id")
        runner = runner or _run_herdr

        if workspace_id:
            listed = _call(
                runner,
                ["herdr", "pane", "list", "--workspace", str(workspace_id)],
                timeout,
                log=log,
            )
            if listed is None:
                return {"status": UNKNOWN, "reason": "probe_failed", "pane_id": pane_id}
            returncode, stdout, _ = listed
            if returncode != 0:
                return {"status": UNKNOWN, "reason": "pane_list_failed", "pane_id": pane_id}
            pane_ids = _pane_ids(stdout)
            if pane_ids is None:
                return {"status": UNKNOWN, "reason": "pane_list_unparseable", "pane_id": pane_id}
            if pane_id not in pane_ids:
                # The workspace listing is authoritative only while the
                # workspace binding is fresh; a stale workspace_id would
                # otherwise produce a false "unavailable". Confirm with a
                # direct pane get: alive -> available (stale binding), gone
                # (non-zero while the daemon is clearly up) -> unavailable,
                # probe failure -> unknown.
                direct = _call(runner, ["herdr", "pane", "get", pane_id], timeout, log=log)
                if direct is None:
                    return {"status": UNKNOWN, "reason": "probe_failed", "pane_id": pane_id}
                if direct[0] == 0:
                    return {
                        "status": AVAILABLE,
                        "reason": "pane_in_other_workspace",
                        "pane_id": pane_id,
                    }
                return {"status": UNAVAILABLE, "reason": "pane_missing", "pane_id": pane_id}
        else:
            pane = _call(runner, ["herdr", "pane", "get", pane_id], timeout, log=log)
            if pane is None:
                return {"status": UNKNOWN, "reason": "probe_failed", "pane_id": pane_id}
            if pane[0] != 0:
                # Without a workspace listing a failed pane get cannot tell
                # "pane gone" from "daemon unreachable" -> unknown.
                return {"status": UNKNOWN, "reason": "pane_get_failed", "pane_id": pane_id}

        agent = _call(runner, ["herdr", "agent", "get", pane_id], timeout, log=log)
        agent_status = _agent_status(agent[1]) if agent is not None and agent[0] == 0 else None
        return {
            "status": AVAILABLE,
            "reason": "pane_alive",
            "pane_id": pane_id,
            "agent_status": agent_status,
        }
    except Exception as exc:  # the probe is best-effort by contract
        _log(log, f"[OBSERVER LIVE PROBE SKIPPED] {type(exc).__name__}")
        return {"status": UNKNOWN, "reason": "probe_failed"}


def read_live_transcript(
    task: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
    *,
    runner: Optional[Runner] = None,
    timeout: Optional[float] = None,
    log=None,
) -> Optional[Dict[str, Any]]:
    """Bounded, redacted tail of the task's current Pane transcript.

    Returns None when no pane is known or the pane read fails; the observer
    then falls back to the persisted evidence file. The CLI call is bounded by
    ``--lines`` and the subprocess timeout, and the returned excerpt is bounded
    by the same bytes/lines/chars limits as the file tail reader.
    """
    cfg = config or {}
    try:
        runtime = task.get("runtime") if isinstance(task.get("runtime"), dict) else {}
        pane_id = task.get("pane_id") or runtime.get("pane_id")
        if not pane_id:
            return None
        pane_id = str(pane_id)
        limit_seconds = (
            float(timeout) if timeout is not None
            else float(cfg.get("live_transcript_timeout", 3.0))
        )
        max_lines = int(cfg.get("log_tail_lines", 200))
        runner = runner or _run_herdr
        read = _call(
            runner,
            ["herdr", "pane", "read", pane_id, "--source", "recent-unwrapped",
             "--lines", str(max_lines)],
            limit_seconds,
            log=log,
        )
        if read is None or read[0] != 0 or not read[1].strip():
            return None
        return observation_context.bound_transcript(
            read[1], ref=f"pane:{pane_id}", config=cfg,
        )
    except Exception as exc:  # transcript capture is best-effort
        _log(log, f"[OBSERVER LIVE TRANSCRIPT SKIPPED] {type(exc).__name__}")
        return None


def _log(log, message: str) -> None:
    LOGGER.warning(message)
    if log is not None:
        try:
            log(message)
        except Exception:
            pass


__all__ = [
    "AVAILABLE",
    "UNAVAILABLE",
    "UNKNOWN",
    "probe_live_runtime",
    "read_live_transcript",
]
