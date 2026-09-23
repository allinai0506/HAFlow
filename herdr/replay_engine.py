"""Replay Engine: rebuild a new run from history (herdr/replay_engine.py).

Reads the source run through existing state chains, freezes the replay
definition into a run-private snapshot, registers a new workflow with a
single write, saves one pending task for the new run identity, and
records the lineage edge. The source run, task, and trajectory rows are
only read, never written. ``dry_run`` plans without any write.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from . import eval_store, projects, state_db
from .trajectory import run_id_for_task
from .transitions import ACTIVE_TASK_STATUSES


def _resolve_db_path(db_path: Path | None, store: Any) -> Path | None:
    if db_path is not None:
        return db_path
    return getattr(store, "db_path", None)


def _short() -> str:
    return uuid.uuid4().hex[:8]


def _resolve_policy(policy: dict[str, Any] | str | None) -> dict[str, Any] | None:
    if policy is None:
        return None
    if isinstance(policy, dict):
        return dict(policy)
    if isinstance(policy, str):
        text = policy.strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"policy must be a JSON object: {exc}") from exc
        if not isinstance(data, dict):
            raise TypeError("policy must be a JSON object")
        return data
    raise TypeError("policy must be a mapping, JSON string, or None")


def _default_definition_from_source(
    source_task: dict[str, Any],
    source_run_id: str,
    db_path: Path | None,
) -> dict[str, Any]:
    try:
        source_workflow = state_db.get_workflow(
            str(source_task.get("workflow_id") or ""), db_path=db_path)
    except sqlite3.Error:
        source_workflow = None
    candidate = (source_workflow or {}).get("workflow_file")
    if candidate:
        try:
            raw = Path(str(candidate)).expanduser().read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and data:
            frozen = dict(data)
            frozen.setdefault("replay_of", source_run_id)
            return frozen
    node = source_task.get("node") or source_task.get("stage") or "dev"
    return {
        "nodes": [{"id": str(node)}],
        "replay_of": source_run_id,
        "source_task_id": source_task.get("task_id"),
        "source_workflow_id": source_task.get("workflow_id"),
    }


def _run_probe_argv(argv: list[str], timeout: int = 120) -> dict[str, Any]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"argv": list(argv), "ok": False, "error": str(exc)}
    return {
        "argv": list(argv),
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[-2000:],
        "stderr": (proc.stderr or "")[-2000:],
    }


def build_launch_argv(
    *,
    task_id: str,
    workflow_id: str,
    source: str,
    agent: str = "auto",
    goal: str = "",
    prompt: str = "",
) -> list[str]:
    """Build the existing launch-chain argv without side effects."""
    return [
        "herdr-task", "launch",
        "--task-id", str(task_id),
        "--workflow-id", str(workflow_id),
        "--source", str(source),
        "--agent", str(agent),
        "--goal", str(goal),
        "--prompt", str(prompt),
    ]


def build_preflight_argv(*, source: str) -> list[str]:
    """Build the existing preflight probe argv without side effects."""
    return ["herdr-preflight", "--source", str(source)]


def replay_run(
    source_run_id: str,
    *,
    replay_run_id: str | None = None,
    workflow_id: str | None = None,
    task_id: str | None = None,
    definition: dict[str, Any] | None = None,
    policy: dict[str, Any] | str | None = None,
    agent: str | None = None,
    goal: str | None = None,
    prompt: str | None = None,
    source: str = "replay",
    launch: bool = False,
    run_preflight: bool = False,
    dry_run: bool = False,
    db_path: Path | None = None,
    store: Any | None = None,
) -> dict[str, Any]:
    """Replay one historical run into a new run identity."""
    source_run_id = str(source_run_id or "").strip()
    if not source_run_id:
        raise ValueError("source_run_id is required")
    if definition is not None and not isinstance(definition, dict):
        raise ValueError("definition must be a mapping or None")
    target_run = str(replay_run_id or f"run_{uuid.uuid4().hex}").strip()
    if not target_run:
        raise ValueError("replay_run_id must not be empty")
    if target_run == source_run_id:
        raise ValueError("source_run_id and replay_run_id must differ")
    db_path = _resolve_db_path(db_path, store)

    facts = state_db.aggregate_run_metric_rows(source_run_id, db_path=db_path)
    if not facts.get("trajectory_events") and not facts.get("task_id"):
        raise ValueError(f"source run has no history: {source_run_id}")

    source_task: dict[str, Any] | None = None
    if facts.get("task_id"):
        candidate = state_db.get_task(str(facts["task_id"]), db_path=db_path)
        if candidate is not None:
            try:
                owned = str(run_id_for_task(candidate)) == source_run_id
            except (ValueError, KeyError, TypeError):
                owned = False
            if owned:
                source_task = candidate
    if source_task is None:
        raise ValueError(f"source run has no owned task: {source_run_id}")
    if source_task.get("status") in ACTIVE_TASK_STATUSES:
        raise ValueError(f"source run is still active: {source_run_id}")

    target_workflow = str(workflow_id or f"wf-replay-{_short()}").strip()
    target_task = str(task_id or f"t-replay-{_short()}").strip()
    if state_db.get_workflow(target_workflow, db_path=db_path) is not None:
        raise ValueError(f"workflow already exists: {target_workflow}")
    if state_db.get_task(target_task, db_path=db_path) is not None:
        raise ValueError(f"task already exists: {target_task}")

    warnings: list[str] = []
    effective_policy = _resolve_policy(policy)
    if definition is None:
        resolved_definition: dict[str, Any] = _default_definition_from_source(
            source_task, source_run_id, db_path)
    else:
        resolved_definition = definition
    effective_source = str(source or "replay").strip() or "replay"
    effective_agent = str(agent or source_task.get("agent") or "auto")
    effective_goal = str(goal or source_task.get("goal") or f"replay of {source_run_id}")
    effective_prompt = str(
        prompt or source_task.get("prompt") or effective_goal)
    launch_argv = build_launch_argv(
        task_id=target_task,
        workflow_id=target_workflow,
        source=effective_source,
        agent=effective_agent,
        goal=effective_goal,
        prompt=effective_prompt,
    )
    preflight_argv = build_preflight_argv(source=effective_source)

    if dry_run:
        return {
            "spec": {
                "source_run_id": source_run_id,
                "replay_run_id": target_run,
                "workflow_id": target_workflow,
                "definition": resolved_definition,
                "snapshot": None,
                "policy": effective_policy,
            },
            "workflow_id": target_workflow,
            "task_id": target_task,
            "replay_run_id": target_run,
            "snapshot": None,
            "definition": resolved_definition,
            "policy": effective_policy,
            "launch_argv": launch_argv,
            "preflight_argv": preflight_argv,
            "dry_run": True,
            "warnings": warnings,
        }

    snapshot: str | None = None
    try:
        snapshot = projects.freeze_run_definition(
            target_workflow, definition=resolved_definition)
    except ValueError:
        raise
    except (OSError, sqlite3.Error):
        snapshot = None
    if snapshot is None:
        warnings.append("definition_freeze_failed")

    source_workflow = state_db.get_workflow(
        str(source_task.get("workflow_id") or ""), db_path=db_path)
    project = {
        "project_id": f"proj-{target_workflow}",
        "project_name": target_workflow,
        "project_root": ".",
        "base_branch": "main",
        "workspace_id": (source_workflow or {}).get("workspace_id") or f"ws-{target_workflow}",
        "coordinator_pane_id": (source_workflow or {}).get("coordinator_pane_id") or "pane-replay",
        "workflow_file": snapshot or (source_workflow or {}).get("workflow_file") or "workflow.json",
    }
    projects.register_workflow(
        target_workflow,
        project,
        requirement=f"replay of {source_run_id}",
        title=f"replay of {source_run_id}",
        workflow_file=snapshot,
        metadata={
            "replay_of": source_run_id,
            "replay_run_id": target_run,
            "source_task_id": source_task.get("task_id"),
            "replayed_at": time.time(),
        },
    )

    state_db.save_task(
        {
            "task_id": target_task,
            "workflow_id": target_workflow,
            "run_id": target_run,
            "node": source_task.get("node") or source_task.get("stage") or "dev",
            "stage": source_task.get("stage") or source_task.get("node") or "dev",
            "agent": effective_agent,
            "status": "pending",
            "goal": effective_goal,
            "prompt": effective_prompt,
            "agent_policy": effective_policy,
            "replay_of": source_run_id,
        },
        db_path=db_path,
    )
    from .trajectory import TrajectoryLedger

    ledger = TrajectoryLedger(db_path)
    ledger.append_event({"run_id": target_run, "task_id": target_task,
                         "workflow_id": target_workflow,
                         "event_type": "run_started", "timestamp": time.time()})
    ledger.append_event({"run_id": target_run, "task_id": target_task,
                         "workflow_id": target_workflow,
                         "event_type": "task_started", "timestamp": time.time()})
    spec = eval_store.record_replay_spec(
        source_run_id,
        target_run,
        workflow_id=target_workflow,
        definition=resolved_definition,
        lineage={
            "source_run_id": source_run_id,
            "replay_run_id": target_run,
            "workflow_id": target_workflow,
            "snapshot": snapshot,
            "policy": effective_policy,
        },
        snapshot=snapshot,
        policy=effective_policy,
        db_path=db_path,
    )
    out: dict[str, Any] = {
        "spec": spec,
        "workflow_id": target_workflow,
        "task_id": target_task,
        "replay_run_id": target_run,
        "snapshot": snapshot,
        "definition": resolved_definition,
        "policy": effective_policy,
        "launch_argv": launch_argv,
        "preflight_argv": preflight_argv,
        "dry_run": False,
        "warnings": warnings,
    }
    if run_preflight:
        probe = _run_probe_argv(preflight_argv)
        out["preflight"] = probe
        if not probe.get("ok"):
            warnings.append("preflight_failed")
    if launch:
        launched = _run_probe_argv(launch_argv)
        out["launch"] = launched
        if not launched.get("ok"):
            warnings.append("launch_failed")
    return out


__all__ = [
    "build_launch_argv",
    "build_preflight_argv",
    "replay_run",
]
