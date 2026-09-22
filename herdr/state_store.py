#!/usr/bin/env python3
"""Herdr Unified State Store (herdr/state_store.py).

Provides a polymorphic StateStore abstraction and SQLiteStateStore concrete
implementation, establishing a strict Single Source of Truth backed by SQLite.

Design Principles:
1. Single Source of Truth: All runtime mutations go directly through StateStore -> SQLite.
2. Legacy Independence: JSON files are used exclusively for migration, export, or compatibility.
3. Thread and Process Safety: Backed by SQLite WAL mode with atomic transactions.
"""

from abc import ABC, abstractmethod
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import fcntl
from . import state_db


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


def _sync_projection_locked(file_path: Path, export_fn: Any) -> None:
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
    except Exception:
        pass


def resolve_tasks_projection_file(
    store: Optional["StateStore"] = None,
    tasks_file: Optional[Union[Path, str]] = None,
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
    store: Optional["StateStore"] = None,
    wf_file: Optional[Union[Path, str]] = None,
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
    store: Optional["StateStore"] = None,
    tasks_file: Optional[Union[Path, str]] = None,
) -> None:
    """Safely synchronize SQLite tasks into tasks.json under cross-process lock."""
    s = store or get_state_store()
    target_file = resolve_tasks_projection_file(store=s, tasks_file=tasks_file)
    _sync_projection_locked(target_file, s.export_tasks_json)


def sync_workflows_projection(
    store: Optional["StateStore"] = None,
    wf_file: Optional[Union[Path, str]] = None,
) -> None:
    """Safely synchronize SQLite workflows into workflows.json under cross-process lock."""
    s = store or get_state_store()
    target_file = resolve_workflows_projection_file(store=s, wf_file=wf_file)
    _sync_projection_locked(target_file, s.export_workflows_json)


class StateStore(ABC):
    """Abstract StateStore interface governing all Herdr system state."""

    # Workflows
    @abstractmethod
    def save_workflow(self, workflow: Dict[str, Any]) -> None:
        """Upsert a workflow record."""
        pass

    @abstractmethod
    def get_workflow(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a workflow by its workflow_id."""
        pass

    @abstractmethod
    def list_workflows(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """List workflows optionally filtered by status."""
        pass

    @abstractmethod
    def delete_workflow(self, workflow_id: str) -> bool:
        """Delete a workflow and cascade its associated tasks."""
        pass

    @abstractmethod
    def transition_workflow(
        self,
        workflow_id: str,
        to_status: str,
        reason: str,
        source: str = "system",
        metadata: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Atomically validate and transition a workflow status, appending a WorkflowEvent."""
        pass

    @abstractmethod
    def update_workflow_metadata(
        self,
        workflow_id: str,
        updates: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Atomically update non-protected metadata fields of a workflow without touching status."""
        pass

    # Tasks
    @abstractmethod
    def save_task(self, task: Dict[str, Any]) -> None:
        """Upsert a task record."""
        pass

    @abstractmethod
    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a task by its task_id."""
        pass

    @abstractmethod
    def list_tasks(
        self,
        workflow_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List tasks optionally filtered by workflow_id and/or status."""
        pass

    @abstractmethod
    def delete_task(self, task_id: str) -> bool:
        """Delete a task by its task_id."""
        pass

    @abstractmethod
    def transition_task(
        self,
        task_id: str,
        to_status: str,
        reason: str,
        source: str = "system",
        metadata: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Atomically validate and transition a task status, appending a WorkflowEvent."""
        pass

    @abstractmethod
    def update_task_metadata(
        self,
        task_id: str,
        updates: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Atomically update non-protected metadata fields of a task without touching status."""
        pass

    # Steering
    @abstractmethod
    def save_steer(self, steer_item: Dict[str, Any]) -> None:
        """Upsert a steering queue item."""
        pass

    @abstractmethod
    def get_steer(self, steer_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a steering item by its steer_id."""
        pass

    @abstractmethod
    def list_steers(
        self,
        task_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List steering items optionally filtered by task_id and/or status."""
        pass

    @abstractmethod
    def update_steer_status(
        self,
        steer_id: str,
        status: str,
        dispatched_at: Optional[float] = None,
    ) -> bool:
        """Update status and dispatched_at for a steering item."""
        pass

    @abstractmethod
    def record_steering_history(self, record: Dict[str, Any]) -> None:
        """Record an intervention/steering audit entry."""
        pass

    @abstractmethod
    def list_steering_history(self, task_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List steering audit entries."""
        pass

    # Events
    @abstractmethod
    def record_event(
        self,
        event_type: str,
        payload: Dict[str, Any],
        workflow_id: Optional[str] = None,
        node_id: Optional[str] = None,
        task_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        source: str = "system",
        timestamp: Optional[float] = None,
        run_id: Optional[str] = None,
    ) -> None:
        """Record a generic lifecycle event."""
        pass

    @abstractmethod
    def list_events(
        self,
        workflow_id: Optional[str] = None,
        node_id: Optional[str] = None,
        task_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        event_type: Optional[str] = None,
        source: Optional[str] = None,
        limit: Optional[int] = None,
        desc: bool = False,
    ) -> List[Dict[str, Any]]:
        """List canonical WorkflowEvent records (desc=newest first)."""
        pass

    # Durable Supervisor actions
    @abstractmethod
    def create_intervention(self, intervention: Dict[str, Any]) -> Dict[str, Any]:
        pass

    @abstractmethod
    def get_intervention(self, intervention_id: str) -> Optional[Dict[str, Any]]:
        pass

    @abstractmethod
    def list_interventions(
        self,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
        statuses: Optional[List[str]] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        pass

    @abstractmethod
    def claim_intervention(self, intervention_id: str) -> Optional[Dict[str, Any]]:
        pass

    @abstractmethod
    def complete_intervention(self, intervention_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
        pass

    @abstractmethod
    def fail_intervention(self, intervention_id: str, error: Dict[str, Any]) -> Dict[str, Any]:
        pass

    # Semantic ContextPack working memory
    @abstractmethod
    def save_context_pack(self, context_pack: Dict[str, Any]) -> Dict[str, Any]:
        """Append a ContextPack snapshot, applying source-sequence deduplication."""
        pass

    @abstractmethod
    def get_context_pack(self, context_id: str) -> Optional[Dict[str, Any]]:
        """Fetch one ContextPack by id."""
        pass

    @abstractmethod
    def get_latest_context_pack(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Fetch the latest ContextPack for a run."""
        pass

    @abstractmethod
    def list_context_packs(self, run_id: str) -> List[Dict[str, Any]]:
        """List ContextPack snapshots for a run in creation order."""
        pass

    # Checkpoints
    @abstractmethod
    def create_checkpoint(
        self,
        workflow_id: str,
        tag: Optional[str] = None,
        parent_checkpoint_id: Optional[str] = None,
        checkpoint_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Capture an atomic point-in-time snapshot of a workflow and its tasks."""
        pass

    @abstractmethod
    def list_checkpoints(self, workflow_id: str) -> List[Dict[str, Any]]:
        """List checkpoints for a workflow."""
        pass

    @abstractmethod
    def get_checkpoint(self, workflow_id: str, checkpoint_id: str) -> Dict[str, Any]:
        """Retrieve full snapshot payload from checkpoint."""
        pass

    @abstractmethod
    def restore_checkpoint(self, workflow_id: str, checkpoint_id: str) -> Dict[str, Any]:
        """Restore workflow state and tasks atomically from checkpoint."""
        pass

    @abstractmethod
    def fork_workflow_from_checkpoint(
        self,
        checkpoint_id: str,
        new_workflow_id: str,
        new_title: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fork a new workflow from historical checkpoint snapshot."""
        pass

    @abstractmethod
    def get_checkpoint_lineage(self, workflow_id: str) -> List[Dict[str, Any]]:
        """Retrieve checkpoint DAG lineage."""
        pass

    # Export & Compatibility
    @abstractmethod
    def export_workflows_json(self) -> Dict[str, Any]:
        """Export current workflows in legacy workflows.json format."""
        pass

    @abstractmethod
    def export_tasks_json(self) -> Dict[str, Any]:
        """Export current tasks in legacy tasks.json format."""
        pass

    @abstractmethod
    def export_steering_json(self) -> Dict[str, Any]:
        """Export current steering queues and history in legacy steering.json format."""
        pass

    @abstractmethod
    def export_all_json(self, target_dir: Optional[Path] = None) -> Dict[str, Any]:
        """Export all state to disk as JSON for external tools/inspection."""
        pass

    @abstractmethod
    def import_from_json(
        self,
        workflows_file: Optional[Path] = None,
        tasks_file: Optional[Path] = None,
        steering_file: Optional[Path] = None,
        checkpoints_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Migrate legacy JSON files into SQLite database."""
        pass


class SQLiteStateStore(StateStore):
    """Concrete SQLite implementation of StateStore."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
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
                wf_path = Path(os.environ.get("WORKFLOWS_FILE") or (state_db.CONTROLLER_DIR / "workflows.json"))
                tasks_path = Path(os.environ.get("TASKS_FILE") or (state_db.CONTROLLER_DIR / "tasks.json"))
                st_path = Path(os.environ.get("STEERING_FILE") or (state_db.CONTROLLER_DIR / "steering.json"))
                cp_path = Path(os.environ.get("CHECKPOINTS_DIR") or (state_db.CONTROLLER_DIR / "checkpoints"))

                if wf_path.exists() or tasks_path.exists() or st_path.exists():
                    self.import_from_json(
                        workflows_file=wf_path if wf_path.exists() else None,
                        tasks_file=tasks_path if tasks_path.exists() else None,
                        steering_file=st_path if st_path.exists() else None,
                        checkpoints_dir=cp_path if cp_path.exists() else None,
                    )
        except Exception:
            pass

    # Workflows
    def save_workflow(self, workflow: Dict[str, Any]) -> None:
        state_db.save_workflow(workflow, db_path=self.db_path)

    def get_workflow(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        return state_db.get_workflow(workflow_id, db_path=self.db_path)

    def list_workflows(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        return state_db.list_workflows(status=status, db_path=self.db_path)

    def delete_workflow(self, workflow_id: str) -> bool:
        return state_db.delete_workflow(workflow_id, db_path=self.db_path)

    def transition_workflow(
        self,
        workflow_id: str,
        to_status: str,
        reason: str,
        source: str = "system",
        metadata: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        return state_db.transition_workflow(
            workflow_id=workflow_id,
            to_status=to_status,
            reason=reason,
            source=source,
            metadata=metadata,
            force=force,
            db_path=self.db_path,
        )

    def update_workflow_metadata(
        self,
        workflow_id: str,
        updates: Dict[str, Any],
    ) -> Dict[str, Any]:
        return state_db.update_workflow_metadata(
            workflow_id=workflow_id,
            updates=updates,
            db_path=self.db_path,
        )

    # Tasks
    def save_task(self, task: Dict[str, Any]) -> None:
        state_db.save_task(task, db_path=self.db_path)

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        return state_db.get_task(task_id, db_path=self.db_path)

    def list_tasks(
        self,
        workflow_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return state_db.list_tasks(workflow_id=workflow_id, status=status, db_path=self.db_path)

    def delete_task(self, task_id: str) -> bool:
        return state_db.delete_task(task_id, db_path=self.db_path)

    def transition_task(
        self,
        task_id: str,
        to_status: str,
        reason: str,
        source: str = "system",
        metadata: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        return state_db.transition_task(
            task_id=task_id,
            to_status=to_status,
            reason=reason,
            source=source,
            metadata=metadata,
            force=force,
            db_path=self.db_path,
        )

    def update_task_metadata(
        self,
        task_id: str,
        updates: Dict[str, Any],
    ) -> Dict[str, Any]:
        return state_db.update_task_metadata(
            task_id=task_id,
            updates=updates,
            db_path=self.db_path,
        )

    # Steering
    def save_steer(self, steer_item: Dict[str, Any]) -> None:
        state_db.save_steer(steer_item, db_path=self.db_path)

    def get_steer(self, steer_id: str) -> Optional[Dict[str, Any]]:
        return state_db.get_steer(steer_id, db_path=self.db_path)

    def list_steers(
        self,
        task_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return state_db.list_steers(task_id=task_id, status=status, db_path=self.db_path)

    def update_steer_status(
        self,
        steer_id: str,
        status: str,
        dispatched_at: Optional[float] = None,
    ) -> bool:
        return state_db.update_steer_status(
            steer_id=steer_id,
            status=status,
            dispatched_at=dispatched_at,
            db_path=self.db_path,
        )

    def record_steering_history(self, record: Dict[str, Any]) -> None:
        state_db.record_steering_history(record, db_path=self.db_path)

    def list_steering_history(self, task_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return state_db.list_steering_history(task_id=task_id, db_path=self.db_path)

    # Events
    def record_event(
        self,
        event_type: str,
        payload: Dict[str, Any],
        workflow_id: Optional[str] = None,
        node_id: Optional[str] = None,
        task_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        source: str = "system",
        timestamp: Optional[float] = None,
        run_id: Optional[str] = None,
    ) -> None:
        state_db.record_event({
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
        workflow_id: Optional[str] = None,
        node_id: Optional[str] = None,
        task_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        event_type: Optional[str] = None,
        source: Optional[str] = None,
        limit: Optional[int] = None,
        desc: bool = False,
    ) -> List[Dict[str, Any]]:
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

    def create_intervention(self, intervention: Dict[str, Any]) -> Dict[str, Any]:
        return state_db.create_intervention(intervention, db_path=self.db_path)

    def get_intervention(self, intervention_id: str) -> Optional[Dict[str, Any]]:
        return state_db.get_intervention(intervention_id, db_path=self.db_path)

    def list_interventions(
        self,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
        statuses: Optional[List[str]] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        return state_db.list_interventions(
            run_id=run_id, task_id=task_id, statuses=statuses, limit=limit, db_path=self.db_path,
        )

    def claim_intervention(self, intervention_id: str) -> Optional[Dict[str, Any]]:
        return state_db.claim_intervention(intervention_id, db_path=self.db_path)

    def complete_intervention(self, intervention_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
        return state_db.complete_intervention(intervention_id, result, db_path=self.db_path)

    def fail_intervention(self, intervention_id: str, error: Dict[str, Any]) -> Dict[str, Any]:
        return state_db.fail_intervention(intervention_id, error, db_path=self.db_path)

    # Checkpoints
    def create_checkpoint(
        self,
        workflow_id: str,
        tag: Optional[str] = None,
        parent_checkpoint_id: Optional[str] = None,
        checkpoint_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return state_db.create_checkpoint(
            workflow_id=workflow_id,
            tag=tag,
            parent_checkpoint_id=parent_checkpoint_id,
            checkpoint_id=checkpoint_id,
            metadata=metadata,
            db_path=self.db_path,
        )

    # Semantic ContextPack working memory
    def save_context_pack(self, context_pack: Dict[str, Any]) -> Dict[str, Any]:
        return state_db.save_context_pack(context_pack, db_path=self.db_path)

    def get_context_pack(self, context_id: str) -> Optional[Dict[str, Any]]:
        return state_db.get_context_pack(context_id, db_path=self.db_path)

    def get_latest_context_pack(self, run_id: str) -> Optional[Dict[str, Any]]:
        return state_db.get_latest_context_pack(run_id, db_path=self.db_path)

    def list_context_packs(self, run_id: str) -> List[Dict[str, Any]]:
        return state_db.list_context_packs(run_id, db_path=self.db_path)

    def list_checkpoints(self, workflow_id: str) -> List[Dict[str, Any]]:
        return state_db.list_checkpoints(workflow_id=workflow_id, db_path=self.db_path)

    def get_checkpoint(self, workflow_id: str, checkpoint_id: str) -> Dict[str, Any]:
        return state_db.get_checkpoint(workflow_id=workflow_id, checkpoint_id=checkpoint_id, db_path=self.db_path)

    def restore_checkpoint(self, workflow_id: str, checkpoint_id: str) -> Dict[str, Any]:
        return state_db.restore_checkpoint(workflow_id=workflow_id, checkpoint_id=checkpoint_id, db_path=self.db_path)

    def fork_workflow_from_checkpoint(
        self,
        checkpoint_id: str,
        new_workflow_id: str,
        new_title: Optional[str] = None,
    ) -> Dict[str, Any]:
        return state_db.fork_workflow_from_checkpoint(
            checkpoint_id=checkpoint_id,
            new_workflow_id=new_workflow_id,
            new_title=new_title,
            db_path=self.db_path,
        )

    def get_checkpoint_lineage(self, workflow_id: str) -> List[Dict[str, Any]]:
        return state_db.get_checkpoint_lineage(workflow_id=workflow_id, db_path=self.db_path)

    # Export & Compatibility
    def export_workflows_json(self) -> Dict[str, Any]:
        wfs = self.list_workflows()
        return {"workflows": {w["workflow_id"]: w for w in wfs}}

    def export_tasks_json(self) -> Dict[str, Any]:
        tasks = self.list_tasks()
        return {"tasks": tasks}

    def export_steering_json(self) -> Dict[str, Any]:
        steers = self.list_steers()
        queues: Dict[str, List[Dict[str, Any]]] = {}
        for s in steers:
            tid = s.get("task_id")
            if tid:
                queues.setdefault(tid, []).append(s)
        history = self.list_steering_history()
        return {"steering_queues": queues, "history": history}

    def export_all_json(self, target_dir: Optional[Path] = None) -> Dict[str, Any]:
        out_dir = Path(target_dir) if target_dir else state_db.CONTROLLER_DIR
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
        workflows_file: Optional[Path] = None,
        tasks_file: Optional[Path] = None,
        steering_file: Optional[Path] = None,
        checkpoints_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        return state_db.migrate_v1_to_v2(
            workflows_file=workflows_file,
            tasks_file=tasks_file,
            checkpoints_dir=checkpoints_dir,
            steering_file=steering_file,
            db_path=self.db_path,
        )


_GLOBAL_STATE_STORE: Optional[StateStore] = None


def get_state_store(db_path: Optional[Path] = None) -> StateStore:
    """Get or instantiate global StateStore singleton."""
    global _GLOBAL_STATE_STORE
    resolved_path = Path(db_path) if db_path else state_db.get_default_db_path()

    if _GLOBAL_STATE_STORE is None:
        _GLOBAL_STATE_STORE = SQLiteStateStore(db_path=resolved_path)
    elif isinstance(_GLOBAL_STATE_STORE, SQLiteStateStore) and _GLOBAL_STATE_STORE.db_path != resolved_path:
        _GLOBAL_STATE_STORE = SQLiteStateStore(db_path=resolved_path)

    return _GLOBAL_STATE_STORE


def set_state_store(store: Optional[StateStore]) -> None:
    """Explicitly set global StateStore (useful for testing or mocking)."""
    global _GLOBAL_STATE_STORE
    _GLOBAL_STATE_STORE = store


def reset_state_store() -> None:
    """Reset global StateStore singleton."""
    global _GLOBAL_STATE_STORE
    _GLOBAL_STATE_STORE = None
