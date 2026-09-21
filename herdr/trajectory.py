"""Append-only, fact-only Agent Trajectory Ledger.

The ledger reuses HAFlow's SQLite event store. RuntimeState remains the source
of current execution identity; this module records only historical facts.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from . import state_db


LOGGER = logging.getLogger(__name__)
_APPEND_LOCK = threading.RLock()

_EVENT_FIELDS = (
    "event_id",
    "run_id",
    "task_id",
    "workflow_id",
    "timestamp",
    "sequence",
    "event_type",
    "node",
    "stage",
    "agent",
    "agent_name",
    "agent_session_id",
    "workspace_id",
    "tab_id",
    "pane_id",
    "status",
    "action",
    "observation",
    "artifact",
    "verification",
    "usage",
    "metadata",
)


def _present(value: Any) -> bool:
    return value is not None and value != ""


@dataclass(frozen=True)
class TrajectoryEvent:
    """Stable structured representation of one historical execution fact."""

    run_id: str
    event_type: str
    event_id: Optional[str] = None
    task_id: Optional[str] = None
    workflow_id: Optional[str] = None
    timestamp: Optional[float] = None
    sequence: Optional[int] = None
    node: Optional[str] = None
    stage: Optional[str] = None
    agent: Optional[str] = None
    agent_name: Optional[str] = None
    agent_session_id: Optional[str] = None
    workspace_id: Optional[str] = None
    tab_id: Optional[str] = None
    pane_id: Optional[str] = None
    status: Optional[str] = None
    action: Optional[Dict[str, Any]] = None
    observation: Optional[Dict[str, Any]] = None
    artifact: Optional[Dict[str, Any]] = None
    verification: Optional[Dict[str, Any]] = None
    usage: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Dict[str, Any]) -> "TrajectoryEvent":
        if not isinstance(raw, dict):
            raise TypeError("trajectory event must be a dict")
        run_id = raw.get("run_id")
        event_type = raw.get("event_type")
        if not run_id:
            raise ValueError("run_id is required")
        if not event_type:
            raise ValueError("event_type is required")
        metadata = raw.get("metadata")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be a dict")
        values = {key: raw.get(key) for key in _EVENT_FIELDS if key not in {"run_id", "event_type", "metadata"}}
        return cls(run_id=str(run_id), event_type=str(event_type), metadata=dict(metadata), **values)

    def to_mapping(self) -> Dict[str, Any]:
        values = {
            key: getattr(self, key)
            for key in _EVENT_FIELDS
            if key != "metadata"
        }
        values["metadata"] = dict(self.metadata)
        return {
            key: value
            for key, value in values.items()
            if _present(value) or (key == "metadata" and value)
        }


class TrajectoryLedger:
    """Small append/list facade over the existing SQLite event store."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path is not None else None

    def append_event(self, event: Union[TrajectoryEvent, Dict[str, Any]]) -> Dict[str, Any]:
        raw_event = event if isinstance(event, dict) else event.to_mapping()
        normalized = event if isinstance(event, TrajectoryEvent) else TrajectoryEvent.from_mapping(event)
        mapping = normalized.to_mapping()
        artifact_result = self._capture_artifact_observation(raw_event, mapping)
        with _APPEND_LOCK:
            stored = state_db.record_trajectory_event(
                {
                    "run_id": mapping["run_id"],
                    "event_type": mapping["event_type"],
                    "workflow_id": mapping.get("workflow_id"),
                    "node": mapping.get("node"),
                    "task_id": mapping.get("task_id"),
                    "agent": mapping.get("agent"),
                    "timestamp": mapping.get("timestamp"),
                    "payload": mapping,
                },
                db_path=self.db_path,
            )
        decoded = self._decode(stored)
        if artifact_result is not None:
            record_observation_created(
                {"run_id": mapping["run_id"], "task_id": mapping.get("task_id"), "workflow_id": mapping.get("workflow_id")},
                artifact_result[0],
                ledger=self,
            )
        return decoded

    def _capture_artifact_observation(self, raw_event: Dict[str, Any], mapping: Dict[str, Any]) -> Any:
        if mapping.get("event_type") != "artifact_created" or not isinstance(mapping.get("artifact"), dict):
            return None
        artifact = dict(mapping["artifact"])
        artifact_path = artifact.get("path") or artifact.get("ref")
        if not artifact_path:
            return None
        path = Path(str(artifact_path))
        if not path.is_absolute():
            base = raw_event.get("clone_path") or raw_event.get("workspace_path")
            if base:
                path = Path(str(base)) / path
        try:
            from .observation import ObservationStore

            observation, created = ObservationStore(self.db_path).create_external_with_status(
                path,
                run_id=mapping["run_id"],
                source_ref=str(artifact.get("ref") or artifact.get("path")),
                artifact_kind=artifact.get("kind"),
                task_id=mapping.get("task_id"),
                workflow_id=mapping.get("workflow_id"),
            )
            mapping["artifact"] = artifact
            mapping["artifact"]["observation_id"] = observation.observation_id
            return observation, created
        except Exception as exc:  # pragma: no cover - best-effort evidence boundary
            LOGGER.warning("artifact observation skipped: run=%s path=%s error=%s", mapping.get("run_id"), path, exc)
            return None

    def list_events(
        self,
        run_id: str,
        event_type: Optional[str] = None,
        task_id: Optional[str] = None,
        agent_session_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        rows = state_db.list_trajectory_events(
            run_id=run_id,
            event_type=event_type,
            task_id=task_id,
            db_path=self.db_path,
        )
        events = [self._decode(row) for row in rows]
        if agent_session_id is not None:
            events = [event for event in events if event.get("agent_session_id") == agent_session_id]
        return events

    @staticmethod
    def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
        event = dict(row.get("payload") or {})
        event.update({
            "event_id": f"evt_{row['id']}",
            "run_id": row["run_id"],
            "event_type": row["event_type"],
            "timestamp": row["timestamp"],
            "sequence": row["sequence"],
        })
        if row.get("workflow_id") is not None:
            event.setdefault("workflow_id", row["workflow_id"])
        if row.get("node_id") is not None:
            event.setdefault("node", row["node_id"])
        if row.get("task_id") is not None:
            event.setdefault("task_id", row["task_id"])
        if row.get("agent_id") is not None:
            event.setdefault("agent", row["agent_id"])
        event.setdefault("metadata", {})
        return event


def run_id_for_task(task: Dict[str, Any]) -> str:
    """Return the persisted run id or a stable legacy-task fallback."""
    run_id = task.get("run_id")
    if run_id:
        return str(run_id)
    task_id = task.get("task_id")
    if not task_id:
        raise ValueError("task_id is required when run_id is absent")
    return f"run_{task_id}"


def record_trajectory_event(
    task: Dict[str, Any],
    event_type: str,
    *,
    ledger: Optional[TrajectoryLedger] = None,
    status: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    **fields: Any,
) -> Dict[str, Any]:
    """Build an event from existing task/runtime facts and append it."""
    runtime = task.get("runtime") or {}
    values: Dict[str, Any] = {
        "run_id": run_id_for_task(task),
        "event_type": event_type,
        "task_id": task.get("task_id"),
        "workflow_id": task.get("workflow_id"),
        "node": task.get("node") or task.get("stage"),
        "stage": task.get("stage") or task.get("node"),
        "agent": task.get("agent") or runtime.get("agent"),
        "agent_name": task.get("agent_name") or runtime.get("agent_name"),
        "agent_session_id": task.get("agent_session_id") or runtime.get("agent_session_id"),
        "workspace_id": task.get("workspace_id") or runtime.get("workspace_id"),
        "tab_id": task.get("tab_id") or runtime.get("tab_id"),
        "pane_id": task.get("pane_id") or runtime.get("pane_id"),
        "status": status,
        "metadata": metadata or {},
    }
    values.update(fields)
    if event_type == "artifact_created":
        values["clone_path"] = task.get("clone_path")
        values["workspace_path"] = task.get("workspace_path")
    return (ledger or TrajectoryLedger()).append_event(values)


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"


def record_trajectory_event_best_effort(task: Dict[str, Any], event_type: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Record a fact without making telemetry availability a runtime dependency."""
    try:
        return record_trajectory_event(task, event_type, **kwargs)
    except Exception as exc:  # pragma: no cover - exercised through integration failures
        LOGGER.warning("trajectory event skipped: task=%s type=%s error=%s", task.get("task_id"), event_type, exc)
        return None


def record_observation_created(
    task: Dict[str, Any],
    observation: Any,
    *,
    ledger: Optional[TrajectoryLedger] = None,
) -> Optional[Dict[str, Any]]:
    """Index an Observation without copying its evidence into the Ledger."""
    mapping = observation.to_mapping() if hasattr(observation, "to_mapping") else dict(observation)
    receipt = {
        key: mapping[key]
        for key in ("observation_id", "source_type", "source_ref", "size_bytes", "sha256")
        if mapping.get(key) is not None
    }
    target = ledger or TrajectoryLedger()
    try:
        stored = state_db.record_observation_receipt(
            {
                "run_id": run_id_for_task(task),
                "task_id": task.get("task_id"),
                "workflow_id": task.get("workflow_id"),
                "node": task.get("node") or task.get("stage"),
                "agent": task.get("agent"),
                "payload": {"observation": receipt},
            },
            receipt["observation_id"],
            db_path=target.db_path,
        )
        return target._decode(stored) if stored is not None else None
    except Exception as exc:  # pragma: no cover - telemetry remains best effort
        LOGGER.warning("observation receipt skipped: task=%s error=%s", task.get("task_id"), exc)
        return None
