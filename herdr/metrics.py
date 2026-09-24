"""Read-only, facts-only Harness Run Metrics aggregation."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from . import state_db
from .trajectory import run_id_for_task
from .transitions import COMPLETED_TASK_STATUSES


@dataclass(frozen=True)
class HarnessRunMetrics:
    run_id: str
    task_id: Optional[str]
    workflow_id: Optional[str]
    started_at: Optional[float]
    finished_at: Optional[float]
    wall_time_seconds: Optional[float]
    final_status: Optional[str]
    task_completed: bool
    trajectory_events: int
    event_counts: Dict[str, int] = field(default_factory=dict)
    tool_calls: Optional[int] = None
    observations_created: int = 0
    observation_bytes: int = 0
    observation_reads: Optional[int] = None
    observation_read_bytes: Optional[int] = None
    findings_created: int = 0
    context_packs_created: int = 0
    context_compactions: Optional[int] = None
    latest_context_pack_bytes: Optional[int] = None
    working_context_compiles: int = 0
    working_context_reused: int = 0
    working_context_changed: int = 0
    latest_working_context_bytes: Optional[int] = None
    verification_total: int = 0
    verification_passed: int = 0
    verification_failed: int = 0
    model_requests: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    estimated_cost: Optional[float] = None
    mechanisms: Dict[str, Dict[str, Optional[int]]] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "workflow_id": self.workflow_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "wall_time_seconds": self.wall_time_seconds,
            "final_status": self.final_status,
            "task_completed": self.task_completed,
            "trajectory_events": self.trajectory_events,
            "event_counts": dict(self.event_counts),
            "tool_calls": self.tool_calls,
            "observations_created": self.observations_created,
            "observation_bytes": self.observation_bytes,
            "observation_reads": self.observation_reads,
            "observation_read_bytes": self.observation_read_bytes,
            "findings_created": self.findings_created,
            "context_packs_created": self.context_packs_created,
            "context_compactions": self.context_compactions,
            "latest_context_pack_bytes": self.latest_context_pack_bytes,
            "working_context_compiles": self.working_context_compiles,
            "working_context_reused": self.working_context_reused,
            "working_context_changed": self.working_context_changed,
            "latest_working_context_bytes": self.latest_working_context_bytes,
            "verification_total": self.verification_total,
            "verification_passed": self.verification_passed,
            "verification_failed": self.verification_failed,
            "model_requests": self.model_requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "estimated_cost": self.estimated_cost,
            "mechanisms": {key: dict(value) for key, value in self.mechanisms.items()},
            "metadata": dict(self.metadata),
        }


def _latest_context_pack_bytes(row: Optional[Dict[str, Any]]) -> Optional[int]:
    if row is None:
        return None
    payload = {
        "context_id": row["context_id"],
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "workflow_id": row["workflow_id"],
        "goal": row["goal"],
        "current_state": json.loads(row["current_state_json"] or "{}"),
        "completed": json.loads(row["completed_json"] or "[]"),
        "verified_facts": json.loads(row["verified_facts_json"] or "[]"),
        "important_findings": json.loads(row["important_findings_json"] or "[]"),
        "evidence_refs": json.loads(row["evidence_refs_json"] or "[]"),
        "artifact_refs": json.loads(row["artifact_refs_json"] or "[]"),
        "open_issues": json.loads(row["open_issues_json"] or "[]"),
        "next_focus": json.loads(row["next_focus_json"] or "[]"),
        "source_event_sequence": int(row["source_event_sequence"] or 0),
        "metadata": json.loads(row["metadata_json"] or "{}"),
        "created_at": float(row["created_at"]),
    }
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _latest_working_context_bytes(row: Optional[Dict[str, Any]]) -> Optional[int]:
    if row is None:
        return None
    return len(str(row.get("payload_json") or "").encode("utf-8"))


def get_run_metrics(
    run_id: str,
    *,
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> HarnessRunMetrics:
    """Aggregate one Run without writing state or reading Observation content."""
    run_id = str(run_id or "").strip()
    if not run_id:
        raise ValueError("run_id is required")

    facts = state_db.aggregate_run_metric_rows(run_id, db_path=db_path)
    task = state_db.get_task(facts["task_id"], db_path=db_path) if facts["task_id"] else None
    task_id = facts["task_id"]
    workflow_id = facts["workflow_id"]
    if task is not None and run_id_for_task(task) != run_id:
        # A task owned by another run must never leak its identity or status.
        task = None
        task_id = None
        workflow_id = None
    started_at = float(facts["started_at"]) if facts["started_at"] is not None else None
    finished_at = float(facts["finished_at"]) if facts["finished_at"] is not None else None
    current_time = float(time.time() if now is None else now)
    wall_time = None
    if started_at is not None:
        wall_time = round(max(0.0, (finished_at if finished_at is not None else current_time) - started_at), 6)

    final_status = task.get("status") if task else None
    if facts["run_failed"] and final_status not in COMPLETED_TASK_STATUSES:
        final_status = "failed"
    elif final_status is None:
        if facts["run_completed"]:
            final_status = "completed"
        elif facts["run_failed"]:
            final_status = "failed"
    task_completed = bool(facts["run_completed"]) or final_status in COMPLETED_TASK_STATUSES
    event_counts = {
        key: int(facts.get(key, 0))
        for key in ("task_started", "verification_completed", "artifact_created", "agent_done")
    }
    event_counts["verification_completed"] = facts["verification_total"]
    packs = facts["context_packs_created"]
    return HarnessRunMetrics(
        run_id=run_id,
        task_id=task_id,
        workflow_id=workflow_id or (task or {}).get("workflow_id"),
        started_at=started_at,
        finished_at=finished_at,
        wall_time_seconds=wall_time,
        final_status=final_status,
        task_completed=task_completed,
        trajectory_events=facts["trajectory_events"],
        event_counts=event_counts,
        observations_created=facts["observations_created"],
        observation_bytes=facts["observation_bytes"],
        findings_created=facts["findings_created"],
        context_packs_created=packs,
        context_compactions=packs if packs else 0,
        latest_context_pack_bytes=_latest_context_pack_bytes(facts["latest_context"]),
        working_context_compiles=facts.get("working_context_compiles", 0),
        working_context_reused=facts.get("working_context_reused", 0),
        working_context_changed=facts.get("working_context_changed", 0),
        latest_working_context_bytes=_latest_working_context_bytes(facts.get("latest_working_context")),
        verification_total=facts["verification_total"],
        verification_passed=facts["verification_passed"],
        verification_failed=facts["verification_failed"],
        mechanisms={
            "observation_pack": {"trigger_count": facts["observations_created"]},
            "context_compact": {"trigger_count": packs},
            "observer": {"trigger_count": None},
            "handoff": {"trigger_count": None},
        },
        metadata={
            "aggregation_source": "sqlite",
            "model_usage": "unsupported",
            "observation_reads": "unsupported",
            "observer_triggers": "unsupported",
            "handoff_triggers": "unsupported",
            "context_compactions": "successful_context_pack_creations",
        },
    )
