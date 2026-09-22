from __future__ import annotations

import json
from pathlib import Path

import pytest

from herdr import state_db
from herdr.context_compact import ContextPack
from herdr.metrics import get_run_metrics
from herdr.observation import ObservationStore, create_observation
from herdr.trajectory import TrajectoryLedger


def _task(run_id: str, task_id: str = "task-1", status: str = "completed") -> dict:
    return {
        "task_id": task_id,
        "run_id": run_id,
        "workflow_id": "wf-1",
        "node": "implementation",
        "stage": "implementation",
        "agent": "claude",
        "status": status,
        "goal": "measure the run",
        "created_at": 100.0,
    }


def _pack(run_id: str, context_id: str, created_at: float) -> dict:
    return ContextPack(
        context_id=context_id,
        run_id=run_id,
        task_id="task-1",
        workflow_id="wf-1",
        goal="measure the run",
        source_event_sequence=2,
        created_at=created_at,
        metadata={"fixture": True},
    ).to_mapping()


def test_empty_run_returns_valid_zero_metrics_and_unknown_capabilities(tmp_path: Path):
    db_path = tmp_path / "state.db"

    metrics = get_run_metrics("run-empty", db_path=db_path, now=200.0)

    assert metrics.run_id == "run-empty"
    assert metrics.trajectory_events == 0
    assert metrics.observations_created == 0
    assert metrics.findings_created == 0
    assert metrics.context_packs_created == 0
    assert metrics.finished_at is None
    assert metrics.wall_time_seconds is None
    assert metrics.model_requests is None
    assert metrics.metadata["observation_reads"] == "unsupported"
    assert metrics.mechanisms["handoff"]["trigger_count"] is None


def test_complete_run_aggregates_existing_authoritative_facts(tmp_path: Path):
    db_path = tmp_path / "state.db"
    store = ObservationStore(db_path)
    ledger = TrajectoryLedger(db_path)
    state_db.save_task(_task("run-complete"), db_path=db_path)

    ledger.append_event({"run_id": "run-complete", "task_id": "task-1", "workflow_id": "wf-1",
                         "event_type": "run_started", "timestamp": 100.0})
    ledger.append_event({"run_id": "run-complete", "task_id": "task-1", "workflow_id": "wf-1",
                         "event_type": "task_started", "timestamp": 101.0})
    ledger.append_event({"run_id": "run-complete", "task_id": "task-1", "workflow_id": "wf-1",
                         "event_type": "verification_completed", "timestamp": 110.0,
                         "verification": {"passed": True}})
    ledger.append_event({"run_id": "run-complete", "task_id": "task-1", "workflow_id": "wf-1",
                         "event_type": "verification_completed", "timestamp": 111.0,
                         "verification": {"passed": False}})
    create_observation(run_id="run-complete", task_id="task-1", workflow_id="wf-1",
                       source_type="agent_log", source_ref="pane:1", content="one", store=store)
    create_observation(run_id="run-complete", task_id="task-1", workflow_id="wf-1",
                       source_type="tool_output", source_ref="tool:1", content={"two": "value"},
                       media_type="application/json", store=store)
    state_db.upsert_trajectory_finding({
        "finding_id": "finding-1", "finding_key": "finding-key-1", "run_id": "run-complete",
        "task_id": "task-1", "workflow_id": "wf-1", "finding_type": "risk",
        "severity": "warning", "summary": "risk", "created_at": 112.0,
    }, db_path=db_path)
    state_db.save_context_pack(_pack("run-complete", "ctx-1", 113.0), db_path=db_path)
    ledger.append_event({"run_id": "run-complete", "task_id": "task-1", "workflow_id": "wf-1",
                         "event_type": "agent_done", "timestamp": 120.0})
    ledger.append_event({"run_id": "run-complete", "task_id": "task-1", "workflow_id": "wf-1",
                         "event_type": "run_completed", "timestamp": 130.0})

    metrics = get_run_metrics("run-complete", db_path=db_path, now=200.0)

    assert metrics.task_id == "task-1"
    assert metrics.workflow_id == "wf-1"
    assert metrics.started_at == 100.0
    assert metrics.finished_at == 130.0
    assert metrics.wall_time_seconds == 30.0
    assert metrics.task_completed is True
    assert metrics.final_status == "completed"
    assert metrics.trajectory_events == 6
    assert metrics.event_counts == {
        "task_started": 1,
        "verification_completed": 2,
        "agent_done": 1,
        "artifact_created": 0,
    }
    assert metrics.observations_created == 2
    assert metrics.observation_bytes == len(b"one") + len(json.dumps({"two": "value"}, ensure_ascii=False, separators=(",", ":")).encode())
    assert metrics.findings_created == 1
    assert metrics.context_packs_created == 1
    assert metrics.latest_context_pack_bytes is not None
    assert metrics.verification_total == 2
    assert metrics.verification_passed == 1
    assert metrics.verification_failed == 1
    assert metrics.context_compactions == 1
    assert metrics.mechanisms["context_compact"]["trigger_count"] == 1


def test_observation_dedup_and_run_isolation(tmp_path: Path):
    db_path = tmp_path / "state.db"
    store = ObservationStore(db_path)
    create_observation(run_id="run-a", source_type="agent_log", source_ref="same",
                       content="same", store=store)
    create_observation(run_id="run-a", source_type="agent_log", source_ref="same",
                       content="same", store=store)
    create_observation(run_id="run-b", source_type="agent_log", source_ref="same",
                       content="same", store=store)

    metrics = get_run_metrics("run-a", db_path=db_path, now=10.0)

    assert metrics.observations_created == 1
    assert metrics.observation_bytes == len(b"same")


def test_latest_context_pack_and_incomplete_run(tmp_path: Path):
    db_path = tmp_path / "state.db"
    state_db.save_context_pack(_pack("run-open", "ctx-old", 10.0), db_path=db_path)
    state_db.save_context_pack(_pack("run-open", "ctx-new", 20.0), db_path=db_path)
    ledger = TrajectoryLedger(db_path)
    ledger.append_event({"run_id": "run-open", "event_type": "run_started", "timestamp": 5.0})
    ledger.append_event({"run_id": "run-open", "event_type": "progress", "timestamp": 25.0})

    metrics = get_run_metrics("run-open", db_path=db_path, now=40.0)

    assert metrics.finished_at is None
    assert metrics.task_completed is False
    assert metrics.wall_time_seconds == 35.0
    assert metrics.context_packs_created == 2
    assert metrics.latest_context_pack_bytes is not None


def test_failed_run_is_terminal_but_not_completed(tmp_path: Path):
    db_path = tmp_path / "state.db"
    ledger = TrajectoryLedger(db_path)
    ledger.append_event({"run_id": "run-failed", "event_type": "run_started", "timestamp": 5.0})
    ledger.append_event({"run_id": "run-failed", "event_type": "run_failed", "timestamp": 8.0})

    metrics = get_run_metrics("run-failed", db_path=db_path, now=40.0)

    assert metrics.final_status == "failed"
    assert metrics.finished_at == 8.0
    assert metrics.task_completed is False


@pytest.mark.parametrize("status", ["committed", "cleaned", "superseded"])
def test_run_completed_remains_completed_across_task_lifecycle(status: str, tmp_path: Path):
    db_path = tmp_path / "state.db"
    state_db.save_task(_task("run-lifecycle", status=status), db_path=db_path)
    ledger = TrajectoryLedger(db_path)
    ledger.append_event({
        "run_id": "run-lifecycle",
        "task_id": "task-1",
        "workflow_id": "wf-1",
        "event_type": "run_completed",
        "timestamp": 8.0,
    })

    metrics = get_run_metrics("run-lifecycle", db_path=db_path, now=40.0)

    assert metrics.final_status == status
    assert metrics.task_completed is True
