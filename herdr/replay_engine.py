"""Replay Engine: rebuild a new run from history (herdr/replay_engine.py).

Reads the source run through existing state chains, freezes the replay
definition into a run-private snapshot, registers a new workflow with a
single write, saves one pending task for the new run identity, and
records the lineage edge. The source run, task, and trajectory rows are
only read, never written. ``dry_run`` plans without any write.
"""

from __future__ import annotations

import json
import os
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
    source_workflow_id = str(source_task.get("workflow_id") or "")
    replay_spec = eval_store.get_replay_spec(source_run_id, db_path=db_path)
    candidate = (replay_spec or {}).get("snapshot")
    if not candidate and source_workflow_id:
        candidate = (source_workflow or {}).get("workflow_file")
        try:
            from .workflow_docs import docs_root, validate_workflow_id

            expected = docs_root() / validate_workflow_id(source_workflow_id) / "workflow.json"
            if not candidate or Path(str(candidate)).expanduser().resolve() != expected.resolve():
                candidate = None
        except (OSError, ValueError):
            candidate = None
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
    raise ValueError(f"source run has no frozen definition snapshot: {source_run_id}")


def _policy_in_definition(definition: Any, node: str) -> dict[str, Any] | None:
    if not isinstance(definition, dict):
        return None
    for key in ("policy_snapshot", "agent_policy", "policy"):
        value = _resolve_policy(definition.get(key))
        if value is not None:
            return value
    nodes = definition.get("nodes")
    if isinstance(nodes, list):
        for item in nodes:
            if isinstance(item, dict) and str(item.get("id") or item.get("node") or "") == node:
                value = _resolve_policy(item.get("agent_policy") or item.get("worker_policy"))
                if value is not None:
                    return value
    return None


def _source_policy(source_task: dict[str, Any], source_workflow: dict[str, Any] | None,
                   source_spec: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str]:
    node = str(source_task.get("node") or source_task.get("stage") or "")
    value = _resolve_policy((source_spec or {}).get("policy"))
    if value is None:
        value = _policy_in_definition((source_spec or {}).get("definition"), node)
    if value is None and (source_spec or {}).get("snapshot"):
        try:
            data = json.loads(Path(source_spec["snapshot"]).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        value = _policy_in_definition(data, node)
    if value is not None:
        return value, "source_snapshot"
    for key in ("agent_policy", "policy_snapshot"):
        value = _resolve_policy(source_task.get(key))
        if value is not None:
            return value, "source_snapshot"
    workflow = source_workflow or {}
    for key in ("policy_snapshot", "agent_policy", "frozen_metadata"):
        candidate = workflow.get(key)
        value = (
            _policy_in_definition(candidate, node)
            if key == "frozen_metadata"
            else _resolve_policy(candidate)
        )
        if value is not None:
            return value, "source_snapshot"
    # The workflow path is accepted only when it is the private frozen file
    # for this source workflow; shared/current template files are not authority.
    workflow_id = str(source_task.get("workflow_id") or "")
    candidate = workflow.get("workflow_file")
    if workflow_id and candidate:
        try:
            from .workflow_docs import docs_root, validate_workflow_id

            expected = docs_root() / validate_workflow_id(workflow_id) / "workflow.json"
            if Path(str(candidate)).expanduser().resolve() == expected.resolve():
                data = json.loads(expected.read_text(encoding="utf-8"))
                value = _policy_in_definition(data, node)
                if value is not None:
                    return value, "source_snapshot"
        except (OSError, ValueError):
            pass
    return None, "unavailable"


def _run_probe_argv(
    argv: list[str], timeout: int = 120, db_path: Path | None = None,
) -> dict[str, Any]:
    env = None
    if db_path is not None:
        env = os.environ.copy()
        env["HERDR_STATE_DB"] = str(db_path)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            check=False, env=env,
        )
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
    node: str = "dev",
    run_id: str = "",
    replay_of: str = "",
    policy: dict[str, Any] | None = None,
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
        "--node", str(node),
        "--run-id", str(run_id),
        "--replay-of", str(replay_of),
        "--agent-policy", json.dumps(policy, ensure_ascii=False),
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
    launch: bool = True,
    run_preflight: bool = True,
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
    if any(str(task.get("run_id") or "") == target_run
           for task in state_db.list_tasks(db_path=db_path)):
        raise ValueError(f"replay run id already exists: {target_run}")
    if eval_store.get_replay_spec(target_run, db_path=db_path) is not None:
        raise ValueError(f"replay run id already has a ReplaySpec: {target_run}")
    from .workflow_docs import docs_root, validate_workflow_id

    target_snapshot = docs_root() / validate_workflow_id(target_workflow) / "workflow.json"
    if target_snapshot.exists():
        raise ValueError(f"replay definition snapshot already exists: {target_snapshot}")

    warnings: list[str] = []
    requested_policy = _resolve_policy(policy)
    if definition is None:
        resolved_definition: dict[str, Any] = _default_definition_from_source(
            source_task, source_run_id, db_path)
    else:
        resolved_definition = definition
    effective_agent = str(agent or source_task.get("agent") or "auto")
    effective_goal = str(goal or source_task.get("goal") or f"replay of {source_run_id}")
    effective_prompt = str(
        prompt or source_task.get("prompt") or effective_goal)
    source_workflow = state_db.get_workflow(
        str(source_task.get("workflow_id") or ""), db_path=db_path)
    source_spec = eval_store.get_replay_spec(source_run_id, db_path=db_path)
    inherited_policy, inherited_source = _source_policy(
        source_task, source_workflow, source_spec)
    effective_policy = requested_policy if requested_policy is not None else inherited_policy
    policy_source = "explicit_override" if requested_policy is not None else inherited_source
    requested_source = str(source or "").strip()
    effective_source = requested_source if requested_source and requested_source != "replay" else str(
        (source_workflow or {}).get("project_root") or ".")
    launch_argv = build_launch_argv(
        task_id=target_task,
        workflow_id=target_workflow,
        source=effective_source,
        agent=effective_agent,
        goal=effective_goal,
        prompt=effective_prompt,
        node=str(source_task.get("node") or source_task.get("stage") or "dev"),
        run_id=target_run,
        replay_of=source_run_id,
        policy=effective_policy,
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
                "policy_source": policy_source,
            },
            "workflow_id": target_workflow,
            "task_id": target_task,
            "replay_run_id": target_run,
            "snapshot": None,
            "definition": resolved_definition,
            "policy": effective_policy,
            "policy_source": policy_source,
            "launch_argv": launch_argv,
            "preflight_argv": preflight_argv,
            "dry_run": True,
            "warnings": warnings,
        }

    preflight_result = None
    preflight_result = _run_probe_argv(preflight_argv, db_path=db_path)
    if not preflight_result.get("ok"):
        raise ValueError("replay preflight failed; run was not created")

    snapshot: str | None = None
    try:
        snapshot = projects.freeze_run_definition(
            target_workflow, definition=resolved_definition)
    except (OSError, sqlite3.Error):
        snapshot = None
    if snapshot is None:
        raise ValueError("replay definition could not be frozen")

    project = {
        "project_id": f"proj-{target_workflow}",
        "project_name": target_workflow,
        "project_root": effective_source,
        "base_branch": "main",
        "workspace_id": (source_workflow or {}).get("workspace_id") or f"ws-{target_workflow}",
        "coordinator_pane_id": (source_workflow or {}).get("coordinator_pane_id") or "pane-replay",
        "workflow_file": snapshot,
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
            "policy_source": policy_source,
            "replayed_at": time.time(),
        },
    )

    out: dict[str, Any] = {
        "spec": None,
        "workflow_id": target_workflow,
        "task_id": target_task,
        "replay_run_id": target_run,
        "snapshot": snapshot,
        "definition": resolved_definition,
        "policy": effective_policy,
        "policy_source": policy_source,
        "launch_argv": launch_argv,
        "preflight_argv": preflight_argv,
        "dry_run": False,
        "warnings": warnings,
    }
    if preflight_result is not None:
        out["preflight"] = preflight_result
    # Replay V1 always executes the existing launch chain. `launch` remains
    # accepted for CLI compatibility but cannot turn a plan into a launched run.
    launched = _run_probe_argv(launch_argv, db_path=db_path)
    out["launch"] = launched
    try:
        if not launched.get("ok"):
            warnings.append("launch_failed")
            raise RuntimeError("replay launch chain failed")
        launched_task = state_db.get_task(target_task, db_path=db_path)
        if (launched_task is None
                or str(launched_task.get("run_id") or "") != target_run
                or str(launched_task.get("replay_of") or "") != source_run_id):
            raise ValueError("launch chain did not persist the replay task identity and source")
        out["spec"] = eval_store.record_replay_spec(
            source_run_id, target_run, workflow_id=target_workflow,
            definition=resolved_definition,
            lineage={"source_run_id": source_run_id, "replay_run_id": target_run,
                     "workflow_id": target_workflow, "snapshot": snapshot,
                     "policy": effective_policy, "policy_source": policy_source},
            snapshot=snapshot, policy=effective_policy, db_path=db_path)
    except Exception:
        try:
            eval_store.delete_replay_spec(target_run, db_path=db_path)
        except sqlite3.Error:
            pass
        try:
            conn = state_db.get_db_connection(db_path)
            try:
                conn.execute("DELETE FROM events WHERE run_id = ? AND task_id = ?",
                             (target_run, target_task))
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            pass
        try:
            state_db.delete_workflow(target_workflow, db_path=db_path)
        except sqlite3.Error:
            pass
        try:
            Path(snapshot).unlink(missing_ok=True)
        except OSError:
            pass
        try:
            from .state_store import (
                get_state_store,
                sync_tasks_projection,
                sync_workflows_projection,
            )

            projection_store = get_state_store(db_path=db_path)
            sync_tasks_projection(store=projection_store)
            sync_workflows_projection(store=projection_store)
        except (OSError, ValueError, sqlite3.Error):
            pass
        raise
    return out


__all__ = [
    "build_launch_argv",
    "build_preflight_argv",
    "replay_run",
]
