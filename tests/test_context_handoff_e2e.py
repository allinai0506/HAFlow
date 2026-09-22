"""Writer->reader handoff E2E (Agent A half).

Writer half builds history using ONLY Trajectory -> Observation -> ContextPack.
Reader half (Agent B) resolves the contract from ContextPack + on-demand reads.
"""
from __future__ import annotations

import json
from pathlib import Path

from herdr.context_compact import compact_run, get_latest_context
from herdr.observation import ObservationStore
from herdr.trajectory import TrajectoryLedger, record_trajectory_event

RUN_ID = "run-handoff-e2e"
TASK_ID = "task-handoff-e2e"
WORKFLOW_ID = "wf-handoff-e2e"
GOAL = "Validate writer->reader handoff via ContextPack only"


def _task(run_id: str = RUN_ID):
    return {
        "task_id": TASK_ID,
        "run_id": run_id,
        "workflow_id": WORKFLOW_ID,
        "goal": GOAL,
        "node": "implementation",
        "stage": "implementation",
        "agent": "agent-a",
        "agent_name": "agent-a",
        "status": "in_progress",
        "runtime": {"status": "running", "agent": "agent-a", "agent_name": "agent-a"},
    }


def build_writer_history(db_path: Path, run_id: str = RUN_ID):
    """Record writer-half facts; return (task, ledger, store, events, observations)."""
    task = _task(run_id)
    ledger = TrajectoryLedger(db_path)
    store = ObservationStore(db_path)
    events = []
    events.append(record_trajectory_event(task, "task_started", ledger=ledger))
    events.append(
        record_trajectory_event(
            task, "progress", ledger=ledger, metadata={"note": "writer half implemented"}
        )
    )
    # Artifact is this test file itself (absolute path always resolves).
    events.append(
        record_trajectory_event(
            task,
            "artifact_created",
            ledger=ledger,
            artifact={
                "ref": "tests/test_context_handoff_e2e.py",
                "kind": "test",
                "path": str(Path(__file__).resolve()),
            },
        )
    )
    tool_obs, _ = store.create_with_status(
        run_id=run_id,
        task_id=TASK_ID,
        workflow_id=WORKFLOW_ID,
        source_type="tool_output",
        source_ref="pytest:writer-half",
        content="writer half green: 3 passed",
        media_type="text/plain",
    )
    verification_payload = {"passed": True, "passed_tests": 3, "total_tests": 3,
                            "evidence_id": "handoff-green-1"}
    ver_obs, _ = store.create_with_status(
        run_id=run_id,
        task_id=TASK_ID,
        workflow_id=WORKFLOW_ID,
        source_type="verification",
        source_ref="verification:handoff-green-1",
        content=verification_payload,
        media_type="application/json",
    )
    events.append(
        record_trajectory_event(
            task,
            "verification_completed",
            ledger=ledger,
            verification={**verification_payload, "observation_id": ver_obs.observation_id},
        )
    )
    return task, ledger, store, events, [tool_obs, ver_obs]


def test_writer_events_persisted(tmp_path: Path):
    db_path = tmp_path / "state.db"
    task, ledger, store, events, _ = build_writer_history(db_path)
    persisted = ledger.list_events(RUN_ID)
    by_type = {e["event_type"] for e in persisted}
    assert {"task_started", "progress", "artifact_created", "verification_completed"} <= by_type
    verifications = [e for e in persisted if e["event_type"] == "verification_completed"]
    assert any((e.get("verification") or {}).get("passed") is True for e in verifications)
    assert any((e.get("verification") or {}).get("evidence_id") == "handoff-green-1"
               for e in verifications)


def test_writer_observations_resolvable(tmp_path: Path):
    db_path = tmp_path / "state.db"
    task, ledger, store, events, observations = build_writer_history(db_path)
    assert len(observations) == 2
    for obs in observations:
        fetched = store.get(obs.observation_id)
        assert fetched is not None
        body = store.read(obs.observation_id, verify=True)
        assert body["observation_id"] == obs.observation_id
        assert store.verify(obs.observation_id)["valid"] is True
    payload = json.loads(store.read(observations[1].observation_id)["content"])
    assert payload["passed"] is True


def build_reader_view(context_pack):
    """READER CONTRACT (Agent B): derive working view from ContextPack only."""
    if hasattr(context_pack, "to_mapping"):
        context_pack = context_pack.to_mapping()
    pack = dict(context_pack or {})
    if not pack.get("goal"):
        raise ValueError("context pack has no goal")
    verified = []
    for fact in pack.get("verified_facts") or []:
        item = dict(fact)
        refs = list(item.get("refs") or [])
        event_id = fact.get("event_id")
        if event_id and event_id not in refs:
            refs.append(event_id)
        item["refs"] = refs
        verified.append(item)
    return {
        "goal": pack.get("goal"),
        "run_id": pack.get("run_id"),
        "task_id": pack.get("task_id"),
        "completed": list(pack.get("completed") or []),
        "verified": verified,
        "open_issues": list(pack.get("open_issues") or []),
        "next_focus": list(pack.get("next_focus") or []),
        "evidence_refs": list(pack.get("evidence_refs") or []),
        "artifact_refs": list(pack.get("artifact_refs") or []),
    }


def test_handoff_reader_completes_contract(tmp_path: Path):
    from herdr import state_db as _state_db

    db_path = tmp_path / "state.db"
    task, ledger, store, events, observations = build_writer_history(db_path)
    pack = compact_run(RUN_ID, task=task, store=store, provider=None)
    view = build_reader_view(pack.to_mapping())
    # Reader resolves the working contract from the pack alone.
    assert view["goal"] == GOAL
    assert view["completed"], "reader must surface completed milestones"
    assert view["verified"], "reader must surface verified facts"
    assert "open_issues" in view and "next_focus" in view
    assert any(v.get("passed") is True for v in view["verified"])
    # Every ref in the view must resolve on demand in the source stores.
    event_ids = {e["event_id"] for e in ledger.list_events(RUN_ID)}
    finding_ids = {f["finding_id"] for f in _state_db.list_trajectory_findings(RUN_ID, db_path=db_path)}
    obs_ids = {o.observation_id for o in store.list(run_id=RUN_ID)}
    artifact_refs = {a.get("ref") for a in pack.artifact_refs}
    valid = event_ids | finding_ids | obs_ids | artifact_refs | set(pack.evidence_refs)
    checked = 0
    for key in ("completed", "verified", "open_issues", "next_focus"):
        for item in view[key]:
            for ref in item.get("refs", []):
                assert ref in valid, f"unresolvable ref {ref!r} in {key}"
                checked += 1
    assert checked > 0


def test_writer_context_pack_resolves(tmp_path: Path):
    db_path = tmp_path / "state.db"
    task, ledger, store, events, observations = build_writer_history(db_path)
    pack = compact_run(RUN_ID, task=task, store=store, provider=None)
    assert pack.goal == GOAL
    assert pack.run_id == RUN_ID
    assert any(f.get("passed") is True for f in pack.verified_facts
               if f.get("fact_type") == "verification")
    assert get_latest_context(RUN_ID, store=store).context_id == pack.context_id
    for ref in pack.evidence_refs:
        assert store.get(ref) is not None
    assert any(a.get("ref") == "tests/test_context_handoff_e2e.py"
               for a in pack.artifact_refs)
