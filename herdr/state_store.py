#!/usr/bin/env python3
"""Herdr Unified State Store (herdr/state_store.py).

Provides a polymorphic StateStore abstraction and SQLiteStateStore concrete
implementation, establishing a strict Single Source of Truth backed by SQLite.

Design Principles:
1. Single Source of Truth: All runtime mutations go directly through StateStore -> SQLite.
2. Legacy Independence: JSON files are used exclusively for migration, export, or compatibility.
3. Thread and Process Safety: Backed by SQLite WAL mode with atomic transactions.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from . import eval_store, state_db


def _atomic_write_json(file_path: Path, data: Any) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(file_path.parent),
        prefix=f".tmp_{file_path.name}_",
    )
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, file_path)


def _sync_projection_locked(file_path: Path, export_fn: Any) -> bool:
    """Write the projection under the cross-process lock.

    Returns True when the projection was refreshed, False when it could not be
    (callers that must not roll back committed state treat it as advisory).
    """
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = file_path.parent / f".{file_path.name}.lock"
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                data = export_fn()
                _atomic_write_json(file_path, data)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
        return True
    except Exception:
        return False


def resolve_tasks_projection_file(
    store: StateStore | None = None,
    tasks_file: Path | str | None = None,
) -> Path:
    """Resolve destination tasks.json path following strict precedence:
    1. Explicit tasks_file argument
    2. os.environ["TASKS_FILE"]
    3. store.db_path.parent / "tasks.json" (if store has db_path)
    4. state_db.CONTROLLER_DIR / "tasks.json"
    """
    if tasks_file:
        return Path(tasks_file)
    env_file = os.environ.get("TASKS_FILE")
    if env_file:
        return Path(env_file)
    db_p = getattr(store, "db_path", None)
    if db_p:
        return Path(db_p).parent / "tasks.json"
    return state_db.CONTROLLER_DIR / "tasks.json"


def resolve_workflows_projection_file(
    store: StateStore | None = None,
    wf_file: Path | str | None = None,
) -> Path:
    """Resolve destination workflows.json path following strict precedence:
    1. Explicit wf_file argument
    2. os.environ["WORKFLOWS_FILE"]
    3. store.db_path.parent / "workflows.json" (if store has db_path)
    4. state_db.CONTROLLER_DIR / "workflows.json"
    """
    if wf_file:
        return Path(wf_file)
    env_file = os.environ.get("WORKFLOWS_FILE")
    if env_file:
        return Path(env_file)
    db_p = getattr(store, "db_path", None)
    if db_p:
        return Path(db_p).parent / "workflows.json"
    return state_db.CONTROLLER_DIR / "workflows.json"


def sync_tasks_projection(
    store: StateStore | None = None,
    tasks_file: Path | str | None = None,
) -> bool:
    """Safely synchronize SQLite tasks into tasks.json under cross-process lock."""
    s = store or get_state_store()
    target_file = resolve_tasks_projection_file(store=s, tasks_file=tasks_file)
    return _sync_projection_locked(target_file, s.export_tasks_json)


def sync_workflows_projection(
    store: StateStore | None = None,
    wf_file: Path | str | None = None,
) -> bool:
    """Safely synchronize SQLite workflows into workflows.json under cross-process lock."""
    s = store or get_state_store()
    target_file = resolve_workflows_projection_file(store=s, wf_file=wf_file)
    return _sync_projection_locked(target_file, s.export_workflows_json)


class StateStore(ABC):
    """Abstract StateStore interface governing all Herdr system state."""

    # Workflows
    def register_workflow(
        self,
        workflow: dict[str, Any],
        event_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically upsert a workflow record and persist its authoritative workflow_created event."""
        self.save_workflow(workflow)
        return {"ok": True, "workflow_id": workflow.get("workflow_id")}

    @abstractmethod
    def save_workflow(self, workflow: dict[str, Any]) -> None:
        """Upsert a workflow record."""

    @abstractmethod
    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        """Fetch a workflow by its workflow_id."""

    @abstractmethod
    def list_workflows(self, status: str | None = None) -> list[dict[str, Any]]:
        """List workflows optionally filtered by status."""

    @abstractmethod
    def delete_workflow(self, workflow_id: str) -> bool:
        """Delete a workflow and cascade its associated tasks."""

    @abstractmethod
    def transition_workflow(
        self,
        workflow_id: str,
        to_status: str,
        reason: str,
        source: str = "system",
        metadata: dict[str, Any] | None = None,
        force: bool = False,
        expected_status: str | None = None,
    ) -> dict[str, Any]:
        """Atomically validate and transition a workflow status, appending a WorkflowEvent."""

    @abstractmethod
    def update_workflow_metadata(
        self,
        workflow_id: str,
        updates: dict[str, Any],
        expected_status: str | None = None,
    ) -> dict[str, Any]:
        """Atomically update non-protected metadata fields of a workflow without touching status."""

    @abstractmethod
    def record_agent_unhealthy(
        self,
        workflow_id: str,
        agent: str,
        status: str = "TOKEN_EXHAUSTED",
    ) -> dict[str, Any]:
        """Atomically mark an agent as unhealthy in workflow metadata inside an immediate transaction."""

    # Tasks
    @abstractmethod
    def save_task(self, task: dict[str, Any], fail_if_exists: bool = False) -> None:
        """Upsert a task record, or fail closed if task already exists."""

    @abstractmethod
    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Fetch a task by its task_id."""

    @abstractmethod
    def list_tasks(
        self,
        workflow_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List tasks optionally filtered by workflow_id and/or status."""

    @abstractmethod
    def delete_task(self, task_id: str) -> bool:
        """Delete a task by its task_id."""

    @abstractmethod
    def transition_task(
        self,
        task_id: str,
        to_status: str,
        reason: str,
        source: str = "system",
        metadata: dict[str, Any] | None = None,
        force: bool = False,
        intent: str | None = None,
    ) -> dict[str, Any]:
        """Atomically validate and transition a task status, appending a WorkflowEvent."""

    @abstractmethod
    def compare_and_set_task_transition(
        self,
        task_id: str,
        to_status: str,
        reason: str,
        source: str = "system",
        metadata: dict[str, Any] | None = None,
        force: bool = False,
        expected_status: str | None = None,
        expected_version: int | None = None,
        expected_updated_at: float | None = None,
        intent: str | None = None,
    ) -> dict[str, Any]:
        """Atomically transition a task only when its observed epoch matches."""

    def compare_and_set_completion_transition(
        self,
        task_id: str,
        *,
        reason: str = "completion_sentinel",
        source: str = "herdr-controller",
        metadata: dict[str, Any] | None = None,
        expected_status: str | None = None,
        expected_version: int | None = None,
        expected_updated_at: float | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Consume a confirmed completion observation atomically, if supported."""
        raise NotImplementedError(
            "completion observation CAS is not implemented by this StateStore"
        )

    @abstractmethod
    def observe_completion(
        self,
        task_id: str,
        *,
        marker_present: bool,
        agent_status: str | None,
        observed_at: float | None = None,
    ) -> dict[str, Any]:
        """Record a durable completion observation."""

    @abstractmethod
    def observe_blocker_marker(
        self,
        task_id: str,
        *,
        marker_present: bool,
        event_payload: dict[str, Any] | None = None,
        observed_at: float | None = None,
    ) -> dict[str, Any]:
        """Track durable BLOCKER marker presence; classify and record."""

    @abstractmethod
    def get_completion_observation(
        self, task_id: str,
    ) -> dict[str, Any] | None:
        """Return the durable completion observation, if present."""

    @abstractmethod
    def clear_completion_observation(
        self,
        task_id: str,
        *,
        expected_status: str | None = None,
        expected_version: int | None = None,
    ) -> bool:
        """Clear a consumed observation only when its epoch still matches."""

    @abstractmethod
    def update_task_metadata(
        self,
        task_id: str,
        updates: dict[str, Any],
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """Atomically update non-protected metadata fields of a task without touching status."""

    @abstractmethod
    def force_pass_gate(
        self,
        workflow_id: str,
        gate_node_id: str,
        note: str = "human forced pass",
        operator: str = "human",
        expected_version: int | None = None,
        task_id: str | None = None,
        expected_pane_id: str | None = None,
        expected_task_versions: dict[str, int] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Atomically force-pass a gate node within a single database transaction."""

    # Steering
    @abstractmethod
    def save_steer(self, steer_item: dict[str, Any]) -> None:
        """Upsert a steering queue item."""

    @abstractmethod
    def get_steer(self, steer_id: str) -> dict[str, Any] | None:
        """Fetch a steering item by its steer_id."""

    @abstractmethod
    def list_steers(
        self,
        task_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List steering items optionally filtered by task_id and/or status."""

    @abstractmethod
    def update_steer_status(
        self,
        steer_id: str,
        status: str,
        dispatched_at: float | None = None,
    ) -> bool:
        """Update status and dispatched_at for a steering item."""

    @abstractmethod
    def record_steering_history(self, record: dict[str, Any]) -> None:
        """Record an intervention/steering audit entry."""

    @abstractmethod
    def list_steering_history(self, task_id: str | None = None) -> list[dict[str, Any]]:
        """List steering audit entries."""

    # Events
    @abstractmethod
    def record_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        workflow_id: str | None = None,
        node_id: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        source: str = "system",
        timestamp: float | None = None,
        run_id: str | None = None,
    ) -> Any:
        """Record a generic lifecycle event and return its receipt, if any."""

    @abstractmethod
    def list_events(
        self,
        workflow_id: str | None = None,
        node_id: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        event_type: str | None = None,
        source: str | None = None,
        limit: int | None = None,
        desc: bool = False,
    ) -> list[dict[str, Any]]:
        """List canonical WorkflowEvent records (desc=newest first)."""

    # Durable Supervisor actions
    @abstractmethod
    def create_intervention(self, intervention: dict[str, Any]) -> dict[str, Any]:
        pass

    @abstractmethod
    def create_intervention_with_limit(
        self, intervention: dict[str, Any], verification_limit: int,
    ) -> dict[str, Any]:
        """Atomically enforce the VERIFY budget while creating the row."""

    @abstractmethod
    def get_intervention(self, intervention_id: str) -> dict[str, Any] | None:
        pass

    @abstractmethod
    def list_interventions(
        self,
        run_id: str | None = None,
        task_id: str | None = None,
        statuses: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    def claim_intervention(
        self,
        intervention_id: str,
        execution_owner: str | None = None,
        lease_seconds: float = 300.0,
        recover_running: bool = False,
    ) -> dict[str, Any] | None:
        pass

    @abstractmethod
    def complete_intervention(
        self, intervention_id: str, result: dict[str, Any],
        execution_owner: str | None = None,
    ) -> dict[str, Any]:
        pass

    @abstractmethod
    def fail_intervention(
        self, intervention_id: str, error: dict[str, Any],
        execution_owner: str | None = None,
    ) -> dict[str, Any]:
        pass

    # Semantic ContextPack working memory
    @abstractmethod
    def save_context_pack(self, context_pack: dict[str, Any]) -> dict[str, Any]:
        """Append a ContextPack snapshot, applying source-sequence deduplication."""

    @abstractmethod
    def get_context_pack(self, context_id: str) -> dict[str, Any] | None:
        """Fetch one ContextPack by id."""

    @abstractmethod
    def get_latest_context_pack(self, run_id: str) -> dict[str, Any] | None:
        """Fetch the latest ContextPack for a run."""

    @abstractmethod
    def list_context_packs(self, run_id: str) -> list[dict[str, Any]]:
        """List ContextPack snapshots for a run in creation order."""

    # Immutable WorkingContext projections.  These are concrete on
    # SQLiteStateStore; the base interface stays compatible with existing
    # lightweight test doubles.
    def save_working_context(
        self, context: dict[str, Any], *, fingerprint_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def get_working_context(self, context_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def get_latest_working_context(
        self, task_id: str, agent_role: str | None = None,
        run_scope: str | None = None, workflow_id: str | None = None,
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    def list_working_contexts(
        self, task_id: str, agent_role: str | None = None,
        run_scope: str | None = None, workflow_id: str | None = None,
        limit: int = 1000, offset: int = 0,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    # Checkpoints
    @abstractmethod
    def create_checkpoint(
        self,
        workflow_id: str,
        tag: str | None = None,
        parent_checkpoint_id: str | None = None,
        checkpoint_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        expected_identity=None,
    ) -> dict[str, Any]:
        """Capture an atomic point-in-time snapshot of a workflow and its tasks."""

    @abstractmethod
    def list_checkpoints(self, workflow_id: str) -> list[dict[str, Any]]:
        """List checkpoints for a workflow."""

    @abstractmethod
    def get_checkpoint(self, workflow_id: str, checkpoint_id: str) -> dict[str, Any]:
        """Retrieve full snapshot payload from checkpoint."""

    @abstractmethod
    def restore_checkpoint(self, workflow_id: str, checkpoint_id: str, expected_identity=None) -> dict[str, Any]:
        """Restore workflow state and tasks atomically from checkpoint."""

    @abstractmethod
    def fork_workflow_from_checkpoint(
        self,
        checkpoint_id: str,
        new_workflow_id: str,
        new_title: str | None = None,
    ) -> dict[str, Any]:
        """Fork a new workflow from historical checkpoint snapshot."""

    @abstractmethod
    def get_checkpoint_lineage(self, workflow_id: str) -> list[dict[str, Any]]:
        """Retrieve checkpoint DAG lineage."""

    # Eval results (point-in-time facts, distinct from Metrics aggregation)
    @abstractmethod
    def record_eval_result(
        self,
        run_id: str,
        revision: int | None = None,
        evidence: Any | None = None,
        requirements_satisfied: bool | None = None,
        verification_passed: bool | None = None,
        human_intervention_count: int | None = None,
        final_status: str | None = None,
        warnings: list[Any] | None = None,
        task_id: str | None = None,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        """Record one eval fact; same run and revision returns existing row."""

    @abstractmethod
    def get_eval_result(
        self, run_id: str, revision: int,
    ) -> dict[str, Any] | None:
        """Fetch one eval row; absent rows return None."""

    @abstractmethod
    def get_latest_eval_result(self, run_id: str) -> dict[str, Any] | None:
        """Fetch the max-revision eval row; absent runs return None."""

    @abstractmethod
    def list_eval_results(
        self, run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List eval rows ordered by revision."""

    @abstractmethod
    def get_max_eval_revision(self, run_id: str) -> int | None:
        """Return max revision for a run; absent runs return None."""

    # Replay specs (lineage edges, never write source run state)
    @abstractmethod
    def record_replay_spec(
        self,
        source_run_id: str,
        replay_run_id: str,
        workflow_id: str | None = None,
        definition: Any | None = None,
        lineage: Any | None = None,
        snapshot: str | None = None,
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record one replay edge; same replay run returns existing row."""

    @abstractmethod
    def get_replay_spec(
        self, replay_run_id: str,
    ) -> dict[str, Any] | None:
        """Fetch one replay spec by replay run."""

    @abstractmethod
    def get_replay_spec_by_id(
        self, spec_id: str,
    ) -> dict[str, Any] | None:
        """Fetch one replay spec by primary key."""

    @abstractmethod
    def list_replay_specs(
        self,
        source_run_id: str | None = None,
        workflow_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List replay specs with optional filters."""

    @abstractmethod
    def get_replay_lineage(self, replay_run_id: str) -> list[str]:
        """Walk source edges back to the origin run."""

    @abstractmethod
    def latest_run_verification_fact(
        self, run_id: str,
    ) -> dict[str, Any] | None:
        """Return latest strict verification fact for one run."""

    # Export & Compatibility
    @abstractmethod
    def export_workflows_json(self) -> dict[str, Any]:
        """Export current workflows in legacy workflows.json format."""

    @abstractmethod
    def export_tasks_json(self) -> dict[str, Any]:
        """Export current tasks in legacy tasks.json format."""

    @abstractmethod
    def export_steering_json(self) -> dict[str, Any]:
        """Export current steering queues and history in legacy steering.json format."""

    @abstractmethod
    def export_all_json(self, target_dir: Path | None = None) -> dict[str, Any]:
        """Export all state to disk as JSON for external tools/inspection."""

    @abstractmethod
    def import_from_json(
        self,
        workflows_file: Path | None = None,
        tasks_file: Path | None = None,
        steering_file: Path | None = None,
        checkpoints_dir: Path | None = None,
    ) -> dict[str, Any]:
        """Migrate legacy JSON files into SQLite database."""


class SQLiteStateStore(StateStore):
    """Concrete SQLite implementation of StateStore."""

    def __init__(
        self,
        db_path: Path | None = None,
        auto_migrate_json: bool = False,
    ) -> None:
        self.db_path = Path(db_path) if db_path else state_db.get_default_db_path()
        state_db.init_db(self.db_path)

        if auto_migrate_json:
            self._maybe_auto_migrate()

    def _maybe_auto_migrate(self) -> None:
        """Seamlessly migrate existing V1 JSON files if SQLite database is empty."""
        try:
            wfs = self.list_workflows()
            tasks = self.list_tasks()
            if not wfs and not tasks:
                wf_path = Path(os.environ.get("WORKFLOWS_FILE") or (self.db_path.parent / "workflows.json"))
                tasks_path = Path(os.environ.get("TASKS_FILE") or (self.db_path.parent / "tasks.json"))
                st_path = Path(os.environ.get("STEERING_FILE") or (self.db_path.parent / "steering.json"))
                cp_path = Path(os.environ.get("CHECKPOINTS_DIR") or (self.db_path.parent / "checkpoints"))

                if wf_path.exists() or tasks_path.exists() or st_path.exists():
                    # A missing selected file is still a selected input.
                    # None would ask the migration reader for host defaults.
                    self.import_from_json(
                        workflows_file=wf_path,
                        tasks_file=tasks_path,
                        steering_file=st_path,
                        checkpoints_dir=cp_path,
                    )
        except Exception:
            pass

    # Workflows
    def register_workflow(
        self,
        workflow: dict[str, Any],
        event_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return state_db.register_workflow(
            workflow,
            event_payload=event_payload,
            db_path=self.db_path,
            save_workflow_fn=self.save_workflow,
        )

    def save_workflow(
        self,
        workflow: dict[str, Any],
        conn: sqlite3.Connection | None = None,
    ) -> None:
        state_db.save_workflow(workflow, db_path=self.db_path, conn=conn)

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        return state_db.get_workflow(workflow_id, db_path=self.db_path)

    def read_workflow_snapshot(self, workflow_id):
        from .recovery_store import read_snapshot
        return read_snapshot(self.db_path, workflow_id)

    def list_workflows(self, status: str | None = None) -> list[dict[str, Any]]:
        return state_db.list_workflows(status=status, db_path=self.db_path)

    def delete_workflow(self, workflow_id: str) -> bool:
        return state_db.delete_workflow(workflow_id, db_path=self.db_path)

    def transition_workflow(
        self,
        workflow_id: str,
        to_status: str,
        reason: str,
        source: str = "system",
        metadata: dict[str, Any] | None = None,
        force: bool = False,
        expected_status: str | None = None,
    ) -> dict[str, Any]:
        return state_db.transition_workflow(
            workflow_id=workflow_id,
            to_status=to_status,
            reason=reason,
            source=source,
            metadata=metadata,
            force=force,
            db_path=self.db_path,
            **({"expected_status": expected_status} if expected_status is not None else {}),
        )

    def update_workflow_metadata(
        self,
        workflow_id: str,
        updates: dict[str, Any],
        expected_status: str | None = None,
    ) -> dict[str, Any]:
        return state_db.update_workflow_metadata(
            workflow_id=workflow_id,
            updates=updates,
            db_path=self.db_path,
            expected_status=expected_status,
            save_workflow_fn=self.save_workflow,
        )

    def record_agent_unhealthy(
        self,
        workflow_id: str,
        agent: str,
        status: str = "TOKEN_EXHAUSTED",
    ) -> dict[str, Any]:
        return state_db.record_agent_unhealthy(
            workflow_id=workflow_id,
            agent=agent,
            status=status,
            db_path=self.db_path,
            save_workflow_fn=self.save_workflow,
        )

    # Tasks
    def save_task(self, task: dict[str, Any], fail_if_exists: bool = False) -> None:
        state_db.save_task(task, db_path=self.db_path, fail_if_exists=fail_if_exists)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return state_db.get_task(task_id, db_path=self.db_path)

    def list_tasks(
        self,
        workflow_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        return state_db.list_tasks(workflow_id=workflow_id, status=status, db_path=self.db_path)

    def delete_task(self, task_id: str) -> bool:
        return state_db.delete_task(task_id, db_path=self.db_path)

    def transition_task(
        self,
        task_id: str,
        to_status: str,
        reason: str,
        source: str = "system",
        metadata: dict[str, Any] | None = None,
        force: bool = False,
        intent: str | None = None,
    ) -> dict[str, Any]:
        return state_db.transition_task(
            task_id=task_id,
            to_status=to_status,
            reason=reason,
            source=source,
            metadata=metadata,
            force=force,
            db_path=self.db_path,
            intent=intent,
        )

    def compare_and_set_task_transition(
        self,
        task_id: str,
        to_status: str,
        reason: str,
        source: str = "system",
        metadata: dict[str, Any] | None = None,
        force: bool = False,
        expected_status: str | None = None,
        expected_version: int | None = None,
        expected_updated_at: float | None = None,
        intent: str | None = None,
    ) -> dict[str, Any]:
        return state_db.compare_and_set_task_transition(
            task_id=task_id,
            to_status=to_status,
            reason=reason,
            source=source,
            metadata=metadata,
            force=force,
            expected_status=expected_status,
            expected_version=expected_version,
            expected_updated_at=expected_updated_at,
            db_path=self.db_path,
            intent=intent,
        )

    def compare_and_set_completion_transition(
        self,
        task_id: str,
        *,
        reason: str = "completion_sentinel",
        source: str = "herdr-controller",
        metadata: dict[str, Any] | None = None,
        expected_status: str | None = None,
        expected_version: int | None = None,
        expected_updated_at: float | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        return state_db.compare_and_set_completion_transition(
            task_id,
            reason=reason,
            source=source,
            metadata=metadata,
            expected_status=expected_status,
            expected_version=expected_version,
            expected_updated_at=expected_updated_at,
            now=now,
            db_path=self.db_path,
        )

    def observe_completion(
        self,
        task_id: str,
        *,
        marker_present: bool,
        agent_status: str | None,
        observed_at: float | None = None,
    ) -> dict[str, Any]:
        return state_db.observe_completion(
            task_id,
            marker_present=marker_present,
            agent_status=agent_status,
            observed_at=observed_at,
            db_path=self.db_path,
        )

    def observe_blocker_marker(
        self,
        task_id: str,
        *,
        marker_present: bool,
        event_payload: dict[str, Any] | None = None,
        observed_at: float | None = None,
    ) -> dict[str, Any]:
        return state_db.observe_blocker_marker(
            task_id,
            marker_present=marker_present,
            event_payload=event_payload,
            observed_at=observed_at,
            db_path=self.db_path,
        )

    def get_completion_observation(
        self, task_id: str,
    ) -> dict[str, Any] | None:
        return state_db.get_completion_observation(task_id, db_path=self.db_path)

    def clear_completion_observation(
        self,
        task_id: str,
        *,
        expected_status: str | None = None,
        expected_version: int | None = None,
    ) -> bool:
        return state_db.clear_completion_observation(
            task_id,
            db_path=self.db_path,
            expected_status=expected_status,
            expected_version=expected_version,
        )

    def update_task_metadata(
        self,
        task_id: str,
        updates: dict[str, Any],
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        return state_db.update_task_metadata(
            task_id=task_id,
            updates=updates,
            db_path=self.db_path,
            expected_version=expected_version,
        )

    def force_pass_gate(
        self,
        workflow_id: str,
        gate_node_id: str,
        note: str = "human forced pass",
        operator: str = "human",
        expected_version: int | None = None,
        task_id: str | None = None,
        expected_pane_id: str | None = None,
        expected_task_versions: dict[str, int] | None = None,
        token: str | None = None,
        credentials_path: str | None = None,
        strict_auth: bool | None = None,
        secret: str | None = None,
        principal: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return state_db.force_pass_gate(
            workflow_id=workflow_id,
            gate_node_id=gate_node_id,
            note=note,
            operator=operator,
            db_path=self.db_path,
            expected_version=expected_version,
            task_id=task_id,
            expected_pane_id=expected_pane_id,
            expected_task_versions=expected_task_versions,
            token=token,
            credentials_path=credentials_path,
            strict_auth=strict_auth,
            secret=secret,
            principal=principal,
            **kwargs,
        )

    # Steering
    def save_steer(self, steer_item: dict[str, Any]) -> None:
        state_db.save_steer(steer_item, db_path=self.db_path)

    def get_steer(self, steer_id: str) -> dict[str, Any] | None:
        return state_db.get_steer(steer_id, db_path=self.db_path)

    def list_steers(
        self,
        task_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        return state_db.list_steers(task_id=task_id, status=status, db_path=self.db_path)

    def update_steer_status(
        self,
        steer_id: str,
        status: str,
        dispatched_at: float | None = None,
    ) -> bool:
        return state_db.update_steer_status(
            steer_id=steer_id,
            status=status,
            dispatched_at=dispatched_at,
            db_path=self.db_path,
        )

    def record_steering_history(self, record: dict[str, Any]) -> None:
        state_db.record_steering_history(record, db_path=self.db_path)

    def list_steering_history(self, task_id: str | None = None) -> list[dict[str, Any]]:
        return state_db.list_steering_history(task_id=task_id, db_path=self.db_path)

    # Events
    def record_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        workflow_id: str | None = None,
        node_id: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        source: str = "system",
        timestamp: float | None = None,
        run_id: str | None = None,
    ) -> Any:
        return state_db.record_event({
            "workflow_id": workflow_id,
            "node_id": node_id,
            "task_id": task_id,
            "agent_id": agent_id,
            "event_type": event_type,
            "timestamp": timestamp,
            "payload": payload,
            "source": source,
            "run_id": run_id,
        }, db_path=self.db_path)

    def list_events(
        self,
        workflow_id: str | None = None,
        node_id: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        event_type: str | None = None,
        source: str | None = None,
        limit: int | None = None,
        desc: bool = False,
    ) -> list[dict[str, Any]]:
        return state_db.list_events(
            workflow_id=workflow_id,
            node_id=node_id,
            task_id=task_id,
            agent_id=agent_id,
            event_type=event_type,
            source=source,
            limit=limit,
            db_path=self.db_path,
            desc=desc,
        )

    def create_intervention(self, intervention: dict[str, Any]) -> dict[str, Any]:
        return state_db.create_intervention(intervention, db_path=self.db_path)

    def create_intervention_with_limit(
        self, intervention: dict[str, Any], verification_limit: int,
    ) -> dict[str, Any]:
        return state_db.create_intervention(
            intervention,
            db_path=self.db_path,
            verification_limit=verification_limit,
        )

    def get_intervention(self, intervention_id: str) -> dict[str, Any] | None:
        return state_db.get_intervention(intervention_id, db_path=self.db_path)

    def list_interventions(
        self,
        run_id: str | None = None,
        task_id: str | None = None,
        statuses: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return state_db.list_interventions(
            run_id=run_id, task_id=task_id, statuses=statuses, limit=limit, db_path=self.db_path,
        )

    def claim_intervention(
        self,
        intervention_id: str,
        execution_owner: str | None = None,
        lease_seconds: float = 300.0,
        recover_running: bool = False,
    ) -> dict[str, Any] | None:
        return state_db.claim_intervention(
            intervention_id,
            db_path=self.db_path,
            execution_owner=execution_owner,
            lease_seconds=lease_seconds,
            recover_running=recover_running,
        )

    def complete_intervention(
        self, intervention_id: str, result: dict[str, Any],
        execution_owner: str | None = None,
    ) -> dict[str, Any]:
        return state_db.complete_intervention(
            intervention_id, result, db_path=self.db_path,
            execution_owner=execution_owner,
        )

    def fail_intervention(
        self, intervention_id: str, error: dict[str, Any],
        execution_owner: str | None = None,
    ) -> dict[str, Any]:
        return state_db.fail_intervention(
            intervention_id, error, db_path=self.db_path,
            execution_owner=execution_owner,
        )

    # Checkpoints
    def create_checkpoint(
        self,
        workflow_id: str,
        tag: str | None = None,
        parent_checkpoint_id: str | None = None,
        checkpoint_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        expected_identity=None,
    ) -> dict[str, Any]:
        return state_db.create_checkpoint(
            workflow_id=workflow_id,
            tag=tag,
            parent_checkpoint_id=parent_checkpoint_id,
            checkpoint_id=checkpoint_id,
            metadata=metadata,
            db_path=self.db_path,
            expected_identity=expected_identity,
        )

    # Semantic ContextPack working memory
    def save_context_pack(self, context_pack: dict[str, Any]) -> dict[str, Any]:
        return state_db.save_context_pack(context_pack, db_path=self.db_path)

    def get_context_pack(self, context_id: str) -> dict[str, Any] | None:
        return state_db.get_context_pack(context_id, db_path=self.db_path)

    def get_latest_context_pack(self, run_id: str) -> dict[str, Any] | None:
        return state_db.get_latest_context_pack(run_id, db_path=self.db_path)

    def list_context_packs(self, run_id: str) -> list[dict[str, Any]]:
        return state_db.list_context_packs(run_id, db_path=self.db_path)

    def save_working_context(
        self, context: dict[str, Any], *, fingerprint_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if fingerprint_config is None:
            from .context_projection import _config
            fingerprint_config = _config(None)
        return state_db.save_working_context(
            context, db_path=self.db_path, fingerprint_config=fingerprint_config,
        )

    def get_working_context(self, context_id: str) -> dict[str, Any] | None:
        return state_db.get_working_context(context_id, db_path=self.db_path)

    def get_latest_working_context(
        self, task_id: str, agent_role: str | None = None,
        run_scope: str | None = None, workflow_id: str | None = None,
    ) -> dict[str, Any] | None:
        return state_db.get_latest_working_context(
            task_id, agent_role=agent_role, db_path=self.db_path,
            run_scope=run_scope, workflow_id=workflow_id,
        )

    def list_working_contexts(
        self, task_id: str, agent_role: str | None = None,
        run_scope: str | None = None, workflow_id: str | None = None,
        limit: int = 1000, offset: int = 0,
    ) -> list[dict[str, Any]]:
        return state_db.list_working_contexts(
            task_id, agent_role=agent_role, db_path=self.db_path,
            run_scope=run_scope, workflow_id=workflow_id,
            limit=limit, offset=offset,
        )

    def list_checkpoints(self, workflow_id: str) -> list[dict[str, Any]]:
        return state_db.list_checkpoints(workflow_id=workflow_id, db_path=self.db_path)

    def get_checkpoint(self, workflow_id: str, checkpoint_id: str) -> dict[str, Any]:
        return state_db.get_checkpoint(workflow_id=workflow_id, checkpoint_id=checkpoint_id, db_path=self.db_path)

    def restore_checkpoint(self, workflow_id: str, checkpoint_id: str, expected_identity=None) -> dict[str, Any]:
        return state_db.restore_checkpoint(workflow_id=workflow_id, checkpoint_id=checkpoint_id,
                                           db_path=self.db_path, expected_identity=expected_identity)

    def fork_workflow_from_checkpoint(
        self,
        checkpoint_id: str,
        new_workflow_id: str,
        new_title: str | None = None,
    ) -> dict[str, Any]:
        return state_db.fork_workflow_from_checkpoint(
            checkpoint_id=checkpoint_id,
            new_workflow_id=new_workflow_id,
            new_title=new_title,
            db_path=self.db_path,
        )

    def get_checkpoint_lineage(self, workflow_id: str) -> list[dict[str, Any]]:
        return state_db.get_checkpoint_lineage(workflow_id=workflow_id, db_path=self.db_path)

    # Eval results (delegated without side effects on projections)
    def record_eval_result(
        self,
        run_id: str,
        revision: int | None = None,
        evidence: Any | None = None,
        requirements_satisfied: bool | None = None,
        verification_passed: bool | None = None,
        human_intervention_count: int | None = None,
        final_status: str | None = None,
        warnings: list[Any] | None = None,
        task_id: str | None = None,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        return eval_store.record_eval_result(
            run_id,
            revision=revision,
            evidence=evidence,
            requirements_satisfied=requirements_satisfied,
            verification_passed=verification_passed,
            human_intervention_count=human_intervention_count,
            final_status=final_status,
            warnings=warnings,
            task_id=task_id,
            workflow_id=workflow_id,
            db_path=self.db_path,
        )

    def get_eval_result(
        self, run_id: str, revision: int,
    ) -> dict[str, Any] | None:
        return eval_store.get_eval_result(run_id, revision, db_path=self.db_path)

    def get_latest_eval_result(self, run_id: str) -> dict[str, Any] | None:
        return eval_store.get_latest_eval_result(run_id, db_path=self.db_path)

    def list_eval_results(
        self, run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return eval_store.list_eval_results(run_id, db_path=self.db_path)

    def get_max_eval_revision(self, run_id: str) -> int | None:
        return eval_store.get_max_eval_revision(run_id, db_path=self.db_path)

    # Replay specs (delegated without touching source run state)
    def record_replay_spec(
        self,
        source_run_id: str,
        replay_run_id: str,
        workflow_id: str | None = None,
        definition: Any | None = None,
        lineage: Any | None = None,
        snapshot: str | None = None,
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return eval_store.record_replay_spec(
            source_run_id,
            replay_run_id,
            workflow_id=workflow_id,
            definition=definition,
            lineage=lineage,
            snapshot=snapshot,
            policy=policy,
            db_path=self.db_path,
        )

    def get_replay_spec(
        self, replay_run_id: str,
    ) -> dict[str, Any] | None:
        return eval_store.get_replay_spec(replay_run_id, db_path=self.db_path)

    def get_replay_spec_by_id(
        self, spec_id: str,
    ) -> dict[str, Any] | None:
        return eval_store.get_replay_spec_by_id(spec_id, db_path=self.db_path)

    def list_replay_specs(
        self,
        source_run_id: str | None = None,
        workflow_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return eval_store.list_replay_specs(
            source_run_id, workflow_id=workflow_id, db_path=self.db_path,
        )

    def get_replay_lineage(self, replay_run_id: str) -> list[str]:
        return eval_store.get_replay_lineage(replay_run_id, db_path=self.db_path)

    def latest_run_verification_fact(
        self, run_id: str,
    ) -> dict[str, Any] | None:
        return eval_store.latest_run_verification_fact(run_id, db_path=self.db_path)

    # Export & Compatibility
    def export_workflows_json(self) -> dict[str, Any]:
        wfs = self.list_workflows()
        return {"workflows": {w["workflow_id"]: w for w in wfs}}

    def export_tasks_json(self) -> dict[str, Any]:
        tasks = self.list_tasks()
        return {"tasks": tasks}

    def export_steering_json(self) -> dict[str, Any]:
        steers = self.list_steers()
        queues: dict[str, list[dict[str, Any]]] = {}
        for s in steers:
            tid = s.get("task_id")
            if tid:
                queues.setdefault(tid, []).append(s)
        history = self.list_steering_history()
        return {"steering_queues": queues, "history": history}

    def export_all_json(self, target_dir: Path | None = None) -> dict[str, Any]:
        out_dir = Path(target_dir) if target_dir else self.db_path.parent
        out_dir.mkdir(parents=True, exist_ok=True)

        wf_path = out_dir / "workflows.json"
        tasks_path = out_dir / "tasks.json"
        st_path = out_dir / "steering.json"

        _atomic_write_json(wf_path, self.export_workflows_json())
        _atomic_write_json(tasks_path, self.export_tasks_json())
        _atomic_write_json(st_path, self.export_steering_json())

        return {
            "ok": True,
            "target_dir": str(out_dir),
            "workflows_file": str(wf_path),
            "tasks_file": str(tasks_path),
            "steering_file": str(st_path),
        }

    def import_from_json(
        self,
        workflows_file: Path | None = None,
        tasks_file: Path | None = None,
        steering_file: Path | None = None,
        checkpoints_dir: Path | None = None,
    ) -> dict[str, Any]:
        return state_db.migrate_v1_to_v2(
            workflows_file=workflows_file,
            tasks_file=tasks_file,
            checkpoints_dir=checkpoints_dir,
            steering_file=steering_file,
            db_path=self.db_path,
        )


_GLOBAL_STATE_STORE: StateStore | None = None


def get_state_store(db_path: Path | None = None) -> StateStore:
    """Get or instantiate global StateStore singleton."""
    global _GLOBAL_STATE_STORE
    resolved_path = Path(db_path) if db_path else state_db.get_default_db_path()

    if _GLOBAL_STATE_STORE is None or isinstance(_GLOBAL_STATE_STORE, SQLiteStateStore) and _GLOBAL_STATE_STORE.db_path != resolved_path:
        _GLOBAL_STATE_STORE = SQLiteStateStore(db_path=resolved_path)

    return _GLOBAL_STATE_STORE


def set_state_store(store: StateStore | None) -> None:
    """Explicitly set global StateStore (useful for testing or mocking)."""
    global _GLOBAL_STATE_STORE
    _GLOBAL_STATE_STORE = store


def reset_state_store() -> None:
    """Reset global StateStore singleton."""
    global _GLOBAL_STATE_STORE
    _GLOBAL_STATE_STORE = None
