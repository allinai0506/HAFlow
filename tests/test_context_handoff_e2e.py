"""Writer->reader handoff E2E (Agent A half).

Writer half builds history using ONLY Trajectory -> Observation -> Finding
-> ContextPack. Reader half (Agent B) resolves the contract from
ContextPack + on-demand reads.

Deterministic Finding path (no mocks, no hand-made Findings):

    real failed trajectory
    -> observe_run(..., use_model=False)
    -> repeated_failure Finding
    -> compact_run(..., provider=None)
    -> ContextPack
"""
from __future__ import annotations

import json
from pathlib import Path

from herdr.context_compact import compact_run, get_latest_context
from herdr.observation import ObservationStore
from herdr.observer import config as observer_config
from herdr.observer import harness as observer_harness
from herdr.trajectory import TrajectoryLedger, record_trajectory_event

RUN_ID = "run-handoff-e2e"
TASK_ID = "task-handoff-e2e"
WORKFLOW_ID = "wf-handoff-e2e"
GOAL = "Validate writer->reader handoff via ContextPack only"

# Large-content marker: placed beyond the 1000-char observation excerpt
# cutoff so the test can prove the full evidence body never enters the pack.
LARGE_TAIL_MARKER = "LARGE-TAIL-handoff-e2e-UNIQUE-7f3a9c"
FAILED_EVIDENCE_IDS = ("handoff-red-1", "handoff-red-2")
ARTIFACT_REF = "tests/test_context_handoff_e2e.py"


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


def _observer_config() -> dict:
    """Hermetic deterministic observer config (no model, no live probes)."""
    config = observer_config.load_config(path="", env={})
    config["enabled"] = True
    config["provider"] = "rule"
    config["live_probe"] = False
    return config


def build_writer_history(db_path: Path, run_id: str = RUN_ID):
    """Record writer-half facts; return (task, ledger, store, events, observations, findings).

    Full deterministic chain: real trajectory (1 passed + 2 trailing failed
    verifications) -> real ``observe_run(use_model=False)`` producing a
    ``repeated_failure`` Finding -> ``compact_run(provider=None)``.
    No hand-made Findings.
    """
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
                "ref": ARTIFACT_REF,
                "kind": "test",
                "path": str(Path(__file__).resolve()),
            },
        )
    )
    large_tool_content = (
        "writer half green: 3 passed\n" + "X" * 4000 + "\n" + LARGE_TAIL_MARKER + "\n"
    )
    tool_obs, _ = store.create_with_status(
        run_id=run_id,
        task_id=TASK_ID,
        workflow_id=WORKFLOW_ID,
        source_type="tool_output",
        source_ref="pytest:writer-half",
        content=large_tool_content,
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
    # Two trailing failures: the deterministic trigger for repeated_failure
    # (tail consecutive passed=false >= 2, task still in_progress).
    for evidence_id in FAILED_EVIDENCE_IDS:
        failed_payload = {
            "passed": False,
            "passed_tests": 2,
            "total_tests": 3,
            "evidence_id": evidence_id,
            "failing_count": 1,
        }
        events.append(
            record_trajectory_event(
                task,
                "verification_completed",
                ledger=ledger,
                verification=failed_payload,
            )
        )
    # Real deterministic Observer: no model calls, no hand-made Finding.
    findings = observer_harness.observe_run(
        run_id,
        task=task,
        store=store,
        ledger=ledger,
        config=_observer_config(),
        use_model=False,
    )
    return task, ledger, store, events, [tool_obs, ver_obs], findings


def _finding_refs(pack) -> set:
    refs: set = set()
    for item in list(pack.important_findings):
        if isinstance(item, dict) and item.get("finding_id"):
            refs.add(str(item["finding_id"]))
    for key in ("open_issues", "next_focus"):
        for item in list(getattr(pack, key)):
            for ref in (item.get("refs") or []):
                refs.add(str(ref))
    return refs


def _authoritative_artifact_refs(ledger, run_id: str = RUN_ID) -> set:
    """Authoritative artifact set from trajectory events (never from the pack)."""
    refs = set()
    for event in ledger.list_events(run_id):
        if event.get("event_type") != "artifact_created":
            continue
        artifact = event.get("artifact") or {}
        ref = artifact.get("ref") or artifact.get("path")
        if ref:
            refs.add(str(ref))
    return refs


def test_writer_events_persisted(tmp_path: Path):
    db_path = tmp_path / "state.db"
    task, ledger, store, events, _, _ = build_writer_history(db_path)
    persisted = ledger.list_events(RUN_ID)
    by_type = {e["event_type"] for e in persisted}
    assert {"task_started", "progress", "artifact_created", "verification_completed"} <= by_type
    verifications = [e for e in persisted if e["event_type"] == "verification_completed"]
    assert any((e.get("verification") or {}).get("passed") is True for e in verifications)
    assert any((e.get("verification") or {}).get("evidence_id") == "handoff-green-1"
               for e in verifications)
    # Trailing chain must be two consecutive failures (deterministic trigger).
    trailing = verifications[-2:]
    assert len(trailing) == 2
    assert all((e.get("verification") or {}).get("passed") is False for e in trailing)
    assert {(e.get("verification") or {}).get("evidence_id") for e in trailing} == set(FAILED_EVIDENCE_IDS)


def test_writer_observations_resolvable(tmp_path: Path):
    db_path = tmp_path / "state.db"
    task, ledger, store, events, observations, _ = build_writer_history(db_path)
    assert len(observations) == 2
    for obs in observations:
        fetched = store.get(obs.observation_id)
        assert fetched is not None
        body = store.read(obs.observation_id, verify=True)
        assert body["observation_id"] == obs.observation_id
        assert store.verify(obs.observation_id)["valid"] is True
    payload = json.loads(store.read(observations[1].observation_id)["content"])
    assert payload["passed"] is True
    # Large tool evidence exists in the store (proves the no-copy check below is real).
    tool_body = store.read(observations[0].observation_id)["content"]
    assert LARGE_TAIL_MARKER in tool_body


def test_writer_observer_produces_repeated_failure(tmp_path: Path):
    """Gap 1a: real observe_run(use_model=False) naturally yields a Finding."""
    from herdr import state_db as _state_db

    db_path = tmp_path / "state.db"
    task, ledger, store, events, observations, findings = build_writer_history(db_path)
    assert findings, "deterministic observer must produce at least one finding"
    repeated = [f for f in findings if f.finding_type == "repeated_failure"]
    assert repeated, f"expected repeated_failure, got {[f.finding_type for f in findings]}"
    finding = repeated[0]
    # Production ID format.
    assert finding.finding_id.startswith("fnd_"), finding.finding_id
    # Belongs to this run.
    assert finding.run_id == RUN_ID
    # Persisted and scoped to this run.
    persisted = _state_db.list_trajectory_findings(RUN_ID, db_path=db_path)
    assert any(row["finding_id"] == finding.finding_id for row in persisted)
    fetched = _state_db.get_trajectory_finding_by_id(
        finding.finding_id, RUN_ID, db_path=db_path,
    )
    assert fetched is not None
    assert fetched["finding_type"] == "repeated_failure"
    assert fetched["run_id"] == RUN_ID
    # Cross-run isolation: another run must not resolve this finding.
    assert _state_db.get_trajectory_finding_by_id(
        finding.finding_id, "run-handoff-e2e-other", db_path=db_path,
    ) is None
    # Evidence anchors real trajectory events.
    event_ids = {e["event_id"] for e in ledger.list_events(RUN_ID)}
    evidence_event_ids = {item.get("event_id") for item in (finding.evidence or []) if item.get("event_id")}
    assert evidence_event_ids, "finding evidence must anchor real events"
    assert evidence_event_ids <= event_ids


def build_reader_view(context_pack):
    """READER CONTRACT (Agent B): derive working view from ContextPack only.

    Inputs: ContextPack + on-demand store reads by the caller. Never touches
    observer internals, the raw transcript, or writer agent history.
    """
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
    task, ledger, store, events, observations, findings = build_writer_history(db_path)
    assert findings, "observer must produce findings before compact"
    finding_id = next(f.finding_id for f in findings if f.finding_type == "repeated_failure")
    pack = compact_run(RUN_ID, task=task, store=store, provider=None)
    view = build_reader_view(pack.to_mapping())
    # Reader resolves the working contract from the pack alone.
    assert view["goal"] == GOAL
    assert view["completed"], "reader must surface completed milestones"
    assert view["verified"], "reader must surface verified facts"
    assert any(v.get("passed") is True for v in view["verified"])
    # Gap 1b: strong Finding propagation (empty lists must not pass).
    assert view["open_issues"], "reader must surface the real open issue"
    assert view["next_focus"], "reader must surface the real next focus"
    reader_refs = set()
    for key in ("open_issues", "next_focus"):
        for item in view[key]:
            reader_refs.update(str(ref) for ref in item.get("refs", []))
    assert finding_id in reader_refs, "reader view must carry the real finding ref"
    # Every ref in the view must resolve in authoritative source stores.
    # Artifact authority comes from trajectory/store, never from the pack itself.
    event_ids = {e["event_id"] for e in ledger.list_events(RUN_ID)}
    finding_ids = {f["finding_id"] for f in _state_db.list_trajectory_findings(RUN_ID, db_path=db_path)}
    assert finding_id in finding_ids
    obs_ids = {o.observation_id for o in store.list(run_id=RUN_ID)}
    authority_artifacts = _authoritative_artifact_refs(ledger, RUN_ID)
    assert authority_artifacts, "trajectory must carry the authoritative artifact ref"
    for entry in pack.artifact_refs:
        ref = entry.get("ref")
        assert ref in authority_artifacts, f"pack artifact {ref!r} not in trajectory authority"
        assert _state_db.artifact_ref_exists(ref, RUN_ID, db_path=db_path), \
            f"pack artifact {ref!r} missing from authoritative store"
    # Cross-run protection: the same ref must not validate for another run.
    for entry in pack.artifact_refs:
        assert not _state_db.artifact_ref_exists(
            entry.get("ref"), "run-handoff-e2e-other", db_path=db_path,
        )
    valid = event_ids | finding_ids | obs_ids | authority_artifacts
    for ref in pack.evidence_refs:
        assert ref in obs_ids, f"evidence {ref!r} must resolve in the observation store"
        assert store.get(ref) is not None
    checked = 0
    for key in ("completed", "verified", "open_issues", "next_focus"):
        for item in view[key]:
            for ref in item.get("refs", []):
                assert ref in valid, f"unresolvable ref {ref!r} in {key}"
                checked += 1
    assert checked > 0


def test_writer_context_pack_resolves(tmp_path: Path):
    from herdr import state_db as _state_db

    db_path = tmp_path / "state.db"
    task, ledger, store, events, observations, findings = build_writer_history(db_path)
    assert findings, "observer must produce findings before compact"
    finding_id = next(f.finding_id for f in findings if f.finding_type == "repeated_failure")
    pack = compact_run(RUN_ID, task=task, store=store, provider=None)
    assert pack.goal == GOAL
    assert pack.run_id == RUN_ID
    assert pack.current_state.get("task_status") == "in_progress"
    assert any(f.get("passed") is True for f in pack.verified_facts
               if f.get("fact_type") == "verification")
    assert any(f.get("passed") is False for f in pack.verified_facts
               if f.get("fact_type") == "verification")
    assert get_latest_context(RUN_ID, store=store).context_id == pack.context_id
    for ref in pack.evidence_refs:
        assert store.get(ref) is not None
    # Gap 1b: ContextPack truly references the real Finding (all three slots).
    assert any(item.get("finding_id") == finding_id for item in pack.important_findings), \
        "important_findings must carry the real finding"
    pack_refs = _finding_refs(pack)
    assert finding_id in pack_refs
    assert pack.open_issues, "open_issues must be non-empty (weak 'in view' asserts are banned)"
    assert pack.next_focus, "next_focus must be non-empty (weak 'in view' asserts are banned)"
    assert any(finding_id in [str(ref) for ref in item.get("refs", [])]
               for item in pack.open_issues)
    assert any(finding_id in [str(ref) for ref in item.get("refs", [])]
               for item in pack.next_focus)
    assert _state_db.get_trajectory_finding_by_id(
        finding_id, RUN_ID, db_path=db_path,
    ) is not None
    # Gap 2: every pack artifact ref validates against the authoritative store.
    authority = _authoritative_artifact_refs(ledger, RUN_ID)
    assert ARTIFACT_REF in authority
    assert any(a.get("ref") == ARTIFACT_REF for a in pack.artifact_refs)
    for entry in pack.artifact_refs:
        ref = entry.get("ref")
        assert ref in authority, f"pack artifact {ref!r} not in trajectory authority"
        assert _state_db.artifact_ref_exists(ref, RUN_ID, db_path=db_path)
    # Observation boundary: large evidence body never enters the pack.
    pack_json = json.dumps(pack.to_mapping(), ensure_ascii=False)
    assert LARGE_TAIL_MARKER not in pack_json
