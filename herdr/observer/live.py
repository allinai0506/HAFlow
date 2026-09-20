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


def _session_value(value: Any) -> Optional[str]:
    """Normalize herdr's agent_session (string or {"value": ...}) to a string."""
    if isinstance(value, dict):
        value = value.get("value")
    if value is None or value == "":
        return None
    return str(value)


def _error_code(payload: str) -> Optional[str]:
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    error = data.get("error") if isinstance(data, dict) else None
    return str(error.get("code")) if isinstance(error, dict) and error.get("code") else None


def _call_error_code(result: Tuple[int, str, str]) -> Optional[str]:
    """herdr reports errors as JSON on stderr (rc=1); accept either stream."""
    return _error_code(result[1]) or _error_code(result[2])


def _pane_payload(payload: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    pane = data.get("result", {}).get("pane") if isinstance(data, dict) else None
    return pane if isinstance(pane, dict) else None


def _agent_payload(payload: str) -> Tuple[bool, Optional[Dict[str, Any]], bool]:
    """(valid, agent, explicit_empty) for one agent get payload.

    ``valid=False`` means the payload is unparseable or uses an unexpected
    schema: that is a probe failure (unknown), never an agentless statement.
    ``explicit_empty=True`` means the registry explicitly returned `null`/`{}`
    for this pane's agent.
    """
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return False, None, False
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict) or "agent" not in result:
        return False, None, False
    agent = result.get("agent")
    if agent is None or agent == {}:
        return True, None, True
    if not isinstance(agent, dict):
        return False, None, False
    return True, agent, False


def _get_pane(
    runner: Runner, pane_id: str, timeout: float, log=None,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """(outcome, pane) where outcome is ok / not_found / failed."""
    got = _call(runner, ["herdr", "pane", "get", pane_id], timeout, log=log)
    if got is None:
        return "failed", None
    if got[0] == 0:
        pane = _pane_payload(got[1])
        return ("ok", pane) if pane is not None else ("failed", None)
    if _call_error_code(got) == "pane_not_found":
        return "not_found", None
    return "failed", None


def _session_verdict(
    pane_id: str, persisted_session: str, live_session: str,
    agent_status: Optional[str] = None,
) -> Dict[str, Any]:
    if live_session == persisted_session:
        result = {
            "status": AVAILABLE, "reason": "identity_match", "pane_id": pane_id,
            "agent_session_id": live_session,
        }
    else:
        result = {
            "status": UNAVAILABLE, "reason": "identity_mismatch", "pane_id": pane_id,
            "agent_session_id": live_session,
        }
    if agent_status is not None:
        result["agent_status"] = agent_status
    return result


def _identity_result(
    pane_id: str, persisted: Dict[str, Any], pane_info: Dict[str, Any],
    runner: Runner, timeout: float, log=None,
) -> Dict[str, Any]:
    """Validate that the live pane *and agent* still belong to this run.

    A matching pane-level session is not proof of a live agent: HAFlow's
    pane_pool uses ``herdr agent get`` as the real live-agent check, so a run
    that explicitly persisted an agent must still confirm the agent registry.
    """
    persisted_session = persisted["session"]
    persisted_agent = persisted["agent"]
    live_session = _session_value(pane_info.get("agent_session"))

    if live_session and persisted_session and live_session != persisted_session:
        # The pane itself contradicts the run identity; no agent call needed.
        return {
            "status": UNAVAILABLE, "reason": "identity_mismatch", "pane_id": pane_id,
            "agent_session_id": live_session,
        }

    if not (persisted_session or persisted_agent):
        # No agent identity was persisted: pane existence (plus any live
        # session) is the only knowable fact.
        result: Dict[str, Any] = {
            "status": AVAILABLE, "reason": "pane_alive", "pane_id": pane_id,
        }
        if live_session:
            result["agent_session_id"] = live_session
        return result

    # The run explicitly had an Agent: always confirm it is still alive.
    agent_call = _call(runner, ["herdr", "agent", "get", pane_id], timeout, log=log)
    if agent_call is None:
        return {"status": UNKNOWN, "reason": "probe_failed", "pane_id": pane_id}
    if agent_call[0] == 0:
        valid, agent_info, explicit_empty = _agent_payload(agent_call[1])
        if not valid:
            return {"status": UNKNOWN, "reason": "agent_payload_invalid", "pane_id": pane_id}
        if explicit_empty:
            return {"status": UNAVAILABLE, "reason": "agent_not_found", "pane_id": pane_id}
        live_agent_session = _session_value(agent_info.get("agent_session"))
        if live_agent_session and persisted_session:
            return _session_verdict(
                pane_id, persisted_session, live_agent_session,
                _agent_status(agent_call[1]),
            )
        if persisted_session and not live_agent_session:
            # An agent answers but exposes no session: identity is unverifiable.
            return {"status": UNKNOWN, "reason": "insufficient_identity", "pane_id": pane_id}
        result = {"status": AVAILABLE, "reason": "agent_alive", "pane_id": pane_id}
        agent_status = _agent_status(agent_call[1])
        if agent_status is not None:
            result["agent_status"] = agent_status
        if live_agent_session:
            result["agent_session_id"] = live_agent_session
        return result
    if _call_error_code(agent_call) == "agent_not_found":
        return {"status": UNAVAILABLE, "reason": "agent_not_found", "pane_id": pane_id}
    return {"status": UNKNOWN, "reason": "agent_probe_failed", "pane_id": pane_id}


def probe_live_runtime(
    task: Dict[str, Any],
    *,
    runner: Optional[Runner] = None,
    timeout: float = 2.0,
    log=None,
) -> Dict[str, Any]:
    """Live availability *and identity* of the task's Pane / Agent session.

    ``unavailable`` requires positive structural evidence: an explicit
    ``pane_not_found``, a persisted/live ``agent_session_id`` mismatch, or an
    explicit ``agent_not_found`` for a run that had an agent. Every probe
    failure (timeout, daemon error, unparseable payload) and every
    "not enough identity information" case degrades to ``unknown`` — unknown is
    never reported as unavailable.
    """
    try:
        runtime = task.get("runtime") if isinstance(task.get("runtime"), dict) else {}
        pane_id = task.get("pane_id") or runtime.get("pane_id")
        if not pane_id:
            return {"status": UNKNOWN, "reason": "no_pane_id"}
        pane_id = str(pane_id)
        workspace_id = task.get("workspace_id") or runtime.get("workspace_id")
        persisted = {
            "session": _session_value(
                task.get("agent_session_id") or runtime.get("agent_session_id")
            ),
            "agent": (
                task.get("agent_name") or runtime.get("agent_name")
                or runtime.get("agent") or task.get("agent")
            ),
        }
        runner = runner or _run_herdr

        pane_info: Optional[Dict[str, Any]] = None
        workspace_mismatch = False
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
            # The listing only proves daemon/workspace reachability; identity
            # always comes from a direct pane get (explicit pane_not_found is
            # the only positive evidence of a missing pane).
            workspace_mismatch = pane_id not in pane_ids
            outcome, pane_info = _get_pane(runner, pane_id, timeout, log=log)
        else:
            # Without a workspace listing, only an explicit pane_not_found proves
            # the pane is gone; other failures stay unknown.
            outcome, pane_info = _get_pane(runner, pane_id, timeout, log=log)

        if outcome == "not_found":
            return {"status": UNAVAILABLE, "reason": "pane_missing", "pane_id": pane_id}
        if outcome != "ok":
            return {"status": UNKNOWN, "reason": "pane_get_failed", "pane_id": pane_id}

        result = _identity_result(
            pane_id, persisted, pane_info or {}, runner, timeout, log=log,
        )
        if workspace_mismatch:
            result["workspace_mismatch"] = True
        return result
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

    The same identity guard as the runtime probe applies: the pane read only
    happens when the pane is confirmed to belong to this run. An identity
    mismatch or unavailable pane returns None so the caller falls back to the
    persisted evidence file; an unverifiable identity also returns None rather
    than reading a potentially reused pane. The CLI call is bounded by
    ``--lines`` and the subprocess timeout, and the returned excerpt by the
    same bytes/lines/chars limits as the file tail reader.
    """
    cfg = config or {}
    try:
        runtime = task.get("runtime") if isinstance(task.get("runtime"), dict) else {}
        pane_id = task.get("pane_id") or runtime.get("pane_id")
        if not pane_id:
            return None
        pane_id = str(pane_id)
        runner = runner or _run_herdr
        identity = probe_live_runtime(
            task,
            runner=runner,
            timeout=float(cfg.get("live_probe_timeout", 2.0)),
            log=log,
        )
        if identity.get("status") != AVAILABLE:
            _log(
                log,
                "[OBSERVER LIVE TRANSCRIPT SKIPPED] identity="
                f"{identity.get('status')}/{identity.get('reason')}",
            )
            return None
        limit_seconds = (
            float(timeout) if timeout is not None
            else float(cfg.get("live_transcript_timeout", 3.0))
        )
        max_lines = int(cfg.get("log_tail_lines", 200))
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
