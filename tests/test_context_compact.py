from __future__ import annotations

import json
import importlib
import threading
import time
from pathlib import Path
import pytest

from herdr.context_compact import (
    ContextPack,
    MAX_CONTEXT_REFS,
    MAX_CONTEXT_SERIALIZED_CHARS,
    MAX_CONTEXT_TEXT_CHARS,
    compact_run,
    get_context,
    get_latest_context,
    list_contexts,
)
from herdr.observation import ObservationStore, create_artifact_observation, create_observation
from herdr.state_db import get_db_connection, upsert_trajectory_finding
import herdr.state_db as state_db
from herdr.trajectory import TrajectoryLedger


def _task(run_id: str = "run-1"):
    return {
        "task_id": "task-1",
        "run_id": run_id,
        "workflow_id": "wf-1",
        "goal": "Repair parser and prove the boundary cases",
        "node": "implementation",
        "stage": "implementation",
        "agent": "claude",
        "agent_name": "worker-2",
        "status": "rework",
        "runtime": {"status": "running", "agent": "claude", "agent_name": "worker-2"},
    }


def _finding(run_id: str = "run-1", finding_id: str = "finding-real"):
    return {
        "finding_id": finding_id,
        "finding_key": f"key-{finding_id}",
        "run_id": run_id,
        "task_id": "task-1",
        "workflow_id": "wf-1",
        "node": "implementation",
        "agent": "claude",
        "finding_type": "repeated_failure",
        "severity": "warning",
        "status": "open",
        "summary": "The same boundary case still fails",
        "recommended_action": "Inspect heading split",
        "confidence": 0.9,
        "evidence": [{"event_id": "evt_1"}],
        "metadata": {},
    }


def test_context_pack_basic_round_trip_and_program_facts(tmp_path: Path):
    db_path = tmp_path / "state.db"
    store = ObservationStore(db_path)
    ledger = TrajectoryLedger(db_path)
    task = _task()
    ledger.append_event({"run_id": "run-1", "task_id": "task-1", "workflow_id": "wf-1", "event_type": "run_started"})
    verification = ledger.append_event({
        "run_id": "run-1",
        "task_id": "task-1",
        "event_type": "verification_completed",
        "verification": {"passed": False, "passed_tests": 143, "total_tests": 146, "evidence_id": "tevd-1"},
    })
    observation = create_observation(
        run_id="run-1", task_id="task-1", source_type="agent_log", source_ref="pane:p1",
        content="heading split failed", excerpt="heading split failed", store=store,
    )
    upsert_trajectory_finding(_finding(), db_path=db_path)

    pack = compact_run("run-1", task=task, store=store, provider=None, config={"enabled": True}, now=10.0)

    assert pack.context_id.startswith("ctx_")
    assert pack.goal == task["goal"]
    assert pack.current_state["task_status"] == "rework"
    assert pack.source_event_sequence == verification["sequence"]
    assert pack.evidence_refs == [observation.observation_id]
    assert pack.important_findings[0]["finding_id"] == "finding-real"
    assert pack.verified_facts[0]["passed"] is False
    assert pack.verified_facts[0]["passed_tests"] == 143
    assert get_context(pack.context_id, store=store) == pack
    assert get_latest_context("run-1", store=store) == pack


def test_context_pack_does_not_duplicate_large_content(tmp_path: Path):
    db_path = tmp_path / "state.db"
    store = ObservationStore(db_path)
    observation = create_observation(
        run_id="run-large", source_type="agent_log", source_ref="pane:p-large",
        content="x" * 1_000_000, excerpt="small excerpt", store=store,
    )
    pack = compact_run("run-large", task={"task_id": "task-large", "run_id": "run-large", "goal": "g"}, store=store, provider=None)
    raw = json.dumps(pack.to_mapping(), ensure_ascii=False)
    assert observation.observation_id in pack.evidence_refs
    assert len(raw) < 20_000
    assert "x" * 100_000 not in raw
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM context_packs WHERE context_id = ?", (pack.context_id,)).fetchone()
        assert row["metadata_json"]
        assert "x" * 100_000 not in json.dumps(dict(row))


def test_hallucinated_refs_are_removed(tmp_path: Path):
    class FakeReducer:
        def reduce(self, payload):
            return {
                "completed": [{"text": "fake", "refs": ["evt_fake"]}],
                "open_issues": [{"text": "fake", "refs": ["finding_fake", "obs_fake"]}],
                "next_focus": [{"text": "fake", "refs": ["artifact_fake"]}],
                "selected_findings": ["finding_fake"],
                "selected_observations": ["obs_fake"],
                "selected_artifacts": ["artifact_fake"],
            }

    ledger = TrajectoryLedger(tmp_path / "state.db")
    event = ledger.append_event({"run_id": "run-refs", "event_type": "task_started"})
    pack = compact_run(
        "run-refs", task={"task_id": "task-refs", "run_id": "run-refs", "goal": "g"},
        store=ObservationStore(tmp_path / "state.db"), provider=FakeReducer(),
    )
    assert pack.completed == []
    assert pack.open_issues == []
    assert pack.next_focus == []
    raw = json.dumps(pack.to_mapping())
    assert "evt_fake" not in raw
    assert "finding_fake" not in raw
    assert "obs_fake" not in raw
    assert "artifact_fake" not in raw


def test_model_cannot_forge_verified_facts(tmp_path: Path):
    class FakeReducer:
        def reduce(self, payload):
            return {"verified_facts": [{"fact": "tests passed 100%", "passed": True}]}

    ledger = TrajectoryLedger(tmp_path / "state.db")
    ledger.append_event({
        "run_id": "run-facts", "event_type": "verification_completed",
        "verification": {"passed": False, "passed_tests": 1, "total_tests": 2},
    })
    pack = compact_run(
        "run-facts", task={"task_id": "task-facts", "run_id": "run-facts", "goal": "g"},
        store=ObservationStore(tmp_path / "state.db"), provider=FakeReducer(),
    )
    assert all(fact.get("passed") is not True for fact in pack.verified_facts)
    assert pack.verified_facts[0]["passed"] is False


def test_hard_input_budget_and_recent_event_cap(tmp_path: Path):
    class RecordingReducer:
        payload = None

        def reduce(self, payload):
            self.payload = payload
            return {}

    db_path = tmp_path / "state.db"
    ledger = TrajectoryLedger(db_path)
    for index in range(5000):
        ledger.append_event({"run_id": "run-budget", "event_type": "progress", "metadata": {"text": "x" * 200, "i": index}})
    reducer = RecordingReducer()
    compact_run(
        "run-budget", task={"task_id": "task-budget", "run_id": "run-budget", "goal": "g"},
        store=ObservationStore(db_path), provider=reducer, config={"max_input_chars": 12000, "max_recent_events": 100},
    )
    serialized = json.dumps(reducer.payload, ensure_ascii=False, separators=(",", ":"))
    assert len(serialized) <= 12000
    assert len(reducer.payload["recent_events"]) <= 100


def test_previous_context_is_retained_and_new_sequence_becomes_latest(tmp_path: Path):
    db_path = tmp_path / "state.db"
    store = ObservationStore(db_path)
    task = {"task_id": "task-prev", "run_id": "run-prev", "goal": "g"}
    ledger = TrajectoryLedger(db_path)
    ledger.append_event({"run_id": "run-prev", "event_type": "task_started"})
    first = compact_run("run-prev", task=task, store=store, provider=None, now=1.0)
    ledger.append_event({"run_id": "run-prev", "event_type": "progress"})
    second = compact_run("run-prev", task=task, store=store, provider=None, now=2.0)
    assert first.context_id != second.context_id
    assert get_latest_context("run-prev", store=store).context_id == second.context_id
    assert [item.context_id for item in list_contexts("run-prev", store=store)] == [first.context_id, second.context_id]


def test_same_source_sequence_deduplicates(tmp_path: Path):
    db_path = tmp_path / "state.db"
    store = ObservationStore(db_path)
    task = {"task_id": "task-dedup", "run_id": "run-dedup", "goal": "g"}
    TrajectoryLedger(db_path).append_event({"run_id": "run-dedup", "event_type": "task_started"})
    first = compact_run("run-dedup", task=task, store=store, provider=None, now=1.0)
    second = compact_run("run-dedup", task=task, store=store, provider=None, now=2.0)
    assert second.context_id == first.context_id
    assert len(list_contexts("run-dedup", store=store)) == 1


def test_provider_failure_returns_deterministic_fallback(tmp_path: Path):
    class BrokenReducer:
        def reduce(self, payload):
            raise TimeoutError("provider unavailable")

    pack = compact_run(
        "run-fallback", task={"task_id": "task-fallback", "run_id": "run-fallback", "goal": "g"},
        store=ObservationStore(tmp_path / "state.db"), provider=BrokenReducer(),
    )
    assert pack.context_id.startswith("ctx_")
    assert pack.completed == []
    assert pack.open_issues == []
    assert pack.next_focus == []


def test_artifact_refs_are_metadata_only(tmp_path: Path):
    db_path = tmp_path / "state.db"
    artifact = tmp_path / "report.txt"
    artifact.write_text("artifact body", encoding="utf-8")
    store = ObservationStore(db_path)
    observation = create_artifact_observation(artifact, run_id="run-artifact", task_id="task-artifact", store=store)
    TrajectoryLedger(db_path).append_event({
        "run_id": "run-artifact", "task_id": "task-artifact", "event_type": "artifact_created",
        "artifact": {"ref": "report.txt", "path": str(artifact), "kind": "report", "observation_id": observation.observation_id},
    })
    pack = compact_run("run-artifact", task={"task_id": "task-artifact", "run_id": "run-artifact", "goal": "g"}, store=store)
    assert pack.artifact_refs
    assert "artifact body" not in json.dumps(pack.to_mapping())


def test_compact_never_reads_observation_content(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "state.db"
    store = ObservationStore(db_path)
    create_observation(
        run_id="run-no-read", source_type="agent_log", source_ref="pane:no-read",
        content="evidence", store=store,
    )

    def fail_read(*args, **kwargs):
        raise AssertionError("compact must not load full Observation content")

    monkeypatch.setattr("herdr.observation.read_observation", fail_read)
    pack = compact_run(
        "run-no-read", task={"task_id": "task-no-read", "run_id": "run-no-read", "goal": "g"}, store=store,
    )
    assert pack.evidence_refs


def test_verified_facts_are_program_facts_and_semantic_fields_are_analysis(tmp_path: Path):
    ledger = TrajectoryLedger(tmp_path / "state.db")
    ledger.append_event({"run_id": "run-separation", "event_type": "verification_completed", "verification": {"passed": True}})

    class Reducer:
        def reduce(self, payload):
            return {"completed": [{"text": "semantic conclusion", "refs": ["evt_1"]}], "verified_facts": [{"passed": False}]}

    pack = compact_run(
        "run-separation", task={"task_id": "task-separation", "run_id": "run-separation", "goal": "g"},
        store=ObservationStore(tmp_path / "state.db"), provider=Reducer(),
    )
    assert pack.verified_facts[0]["passed"] is True
    assert pack.completed[0]["text"] == "semantic conclusion"
    assert pack.metadata["analysis"]["completed"] is True


def test_existing_decision_provider_can_select_important_finding(tmp_path: Path):
    db_path = tmp_path / "state.db"
    upsert_trajectory_finding(_finding(run_id="run-provider", finding_id="finding-provider"), db_path=db_path)

    class SelectionProvider:
        def choose(self, question, state, options):
            assert question["instructions"]
            return type("Result", (), {"value": "finding-provider"})()

    pack = compact_run(
        "run-provider", task={"task_id": "task-provider", "run_id": "run-provider", "goal": "g"},
        store=ObservationStore(db_path), provider=SelectionProvider(),
    )
    assert [item["finding_id"] for item in pack.important_findings] == ["finding-provider"]


def test_previous_verified_refs_survive_bounded_window(tmp_path: Path):
    db_path = tmp_path / "state.db"
    store = ObservationStore(db_path)
    ledger = TrajectoryLedger(db_path)
    task = {"task_id": "task-memory", "run_id": "run-memory", "goal": "g"}
    first_event = ledger.append_event({"run_id": "run-memory", "event_type": "task_completed"})
    observation = create_observation(
        run_id="run-memory", source_type="agent_log", source_ref="old", content="old evidence", store=store,
    )
    artifact = tmp_path / "old.txt"
    artifact.write_text("old artifact", encoding="utf-8")
    ledger.append_event({
        "run_id": "run-memory", "event_type": "artifact_created",
        "artifact": {"ref": "old.txt", "path": str(artifact), "observation_id": observation.observation_id},
    })
    first = compact_run("run-memory", task=task, store=store, provider=None)
    assert first_event["event_id"] in {ref for item in first.completed for ref in item["refs"]}
    assert observation.observation_id in first.evidence_refs
    assert any(item["ref"] == "old.txt" for item in first.artifact_refs)
    for index in range(500):
        ledger.append_event({"run_id": "run-memory", "event_type": "progress", "metadata": {"i": index}})
    second = compact_run("run-memory", task=task, store=store, provider=None)
    assert first_event["event_id"] in {ref for item in second.completed for ref in item["refs"]}
    assert observation.observation_id in second.evidence_refs
    assert any(item["ref"] == "old.txt" for item in second.artifact_refs)


def test_decision_provider_reduces_all_three_semantic_fields(tmp_path: Path):
    ledger = TrajectoryLedger(tmp_path / "state.db")
    ledger.append_event({"run_id": "run-reducer", "event_type": "task_completed"})
    upsert_trajectory_finding(_finding(run_id="run-reducer", finding_id="finding-reducer"), db_path=tmp_path / "state.db")

    class Reducer:
        def judge_many(self, questions, state):
            return {key: type("Result", (), {"value": 1.0})() for key in questions}

        def choose(self, question, state, options):
            return type("Result", (), {"value": next(iter(options))})()

    pack = compact_run(
        "run-reducer", task={"task_id": "task-reducer", "run_id": "run-reducer", "goal": "g"},
        store=ObservationStore(tmp_path / "state.db"), provider=Reducer(),
    )
    assert pack.completed
    assert pack.open_issues
    assert pack.next_focus

def test_fingerprint_changes_for_finding_and_task_state_without_new_trajectory(tmp_path: Path):
    db_path = tmp_path / "state.db"
    store = ObservationStore(db_path)
    ledger = TrajectoryLedger(db_path)
    ledger.append_event({"run_id": "run-fingerprint", "event_type": "task_started"})
    first = compact_run("run-fingerprint", task={"task_id": "task-fingerprint", "run_id": "run-fingerprint", "status": "running", "goal": "g"}, store=store)
    upsert_trajectory_finding(_finding(run_id="run-fingerprint", finding_id="finding-fingerprint"), db_path=db_path)
    second = compact_run("run-fingerprint", task={"task_id": "task-fingerprint", "run_id": "run-fingerprint", "status": "running", "goal": "g"}, store=store)
    assert second.context_id != first.context_id
    third = compact_run("run-fingerprint", task={"task_id": "task-fingerprint", "run_id": "run-fingerprint", "status": "rework", "goal": "g"}, store=store)
    assert third.context_id != second.context_id
    assert second.metadata["context_source_fingerprint"] != first.metadata["context_source_fingerprint"]


def test_compact_queries_findings_and_observations_with_sql_limits(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "state.db"
    store = ObservationStore(db_path)
    for index in range(4):
        create_observation(run_id="run-sql", source_type="agent_log", source_ref=str(index), content="x", store=store)
        upsert_trajectory_finding(_finding(run_id="run-sql", finding_id=f"finding-sql-{index}"), db_path=db_path)
    traces = []
    original = state_db.get_db_connection

    def traced(path=None):
        conn = original(path)
        conn.set_trace_callback(traces.append)
        return conn

    monkeypatch.setattr(state_db, "get_db_connection", traced)
    compact_run("run-sql", task={"task_id": "task-sql", "run_id": "run-sql", "goal": "g"}, store=store, provider=None, config={"max_findings": 2, "max_observations": 2})
    sql = "\n".join(traces).upper()
    assert "FROM TRAJECTORY_FINDINGS" in sql and "LIMIT 2" in sql
    assert "FROM OBSERVATIONS" in sql and "LIMIT 2" in sql


def test_mismatched_explicit_task_fails_closed(tmp_path: Path):
    store = ObservationStore(tmp_path / "state.db")
    with pytest.raises(ValueError, match="does not match"):
        compact_run("run-a", task={"task_id": "task-b", "run_id": "run-b", "goal": "g"}, store=store, provider=None)
    assert list_contexts("run-a", store=store) == []


def test_auto_task_lookup_does_not_import_task_from_another_run(tmp_path: Path):
    db_path = tmp_path / "state.db"
    store = ObservationStore(db_path)
    state_db.save_task({"task_id": "shared-task", "run_id": "run-new", "status": "running", "goal": "new goal"}, db_path=db_path)
    TrajectoryLedger(db_path).append_event({"run_id": "run-old", "task_id": "shared-task", "event_type": "task_started", "metadata": {"goal": "old goal"}})
    pack = compact_run("run-old", store=store, provider=None)
    assert pack.current_state == {}
    assert pack.goal == "old goal"


def test_compact_redacts_provider_input_previous_context_and_pack_text(tmp_path: Path):
    db_path = tmp_path / "state.db"
    store = ObservationStore(db_path)
    secret = "VERY-SECRET-123456"
    ledger = TrajectoryLedger(db_path)
    ledger.append_event({"run_id": "run-redact", "event_type": "progress", "metadata": {"nested": {"api_key": secret}}})
    captured = {}

    class Provider:
        def reduce(self, payload):
            captured["payload"] = payload
            return {"completed": [{"text": f"token={secret}", "refs": ["evt_1"]}], "open_issues": [], "next_focus": []}

    first = compact_run("run-redact", task={"task_id": "task-redact", "run_id": "run-redact", "goal": f"goal password={secret}"}, store=store, provider=Provider())
    assert secret not in json.dumps(captured["payload"], ensure_ascii=False)
    assert secret not in json.dumps(first.to_mapping(), ensure_ascii=False)
    ledger.append_event({"run_id": "run-redact", "event_type": "progress"})
    compact_run("run-redact", task={"task_id": "task-redact", "run_id": "run-redact", "goal": f"goal password={secret}"}, store=store, provider=Provider())
    assert secret not in json.dumps(captured["payload"], ensure_ascii=False)


def test_finding_mutation_changes_context_fingerprint(tmp_path: Path):
    db_path = tmp_path / "state.db"
    store = ObservationStore(db_path)
    first_finding = _finding(run_id="run-finding-mutate", finding_id="finding-mutate")
    upsert_trajectory_finding(first_finding, db_path=db_path)
    first = compact_run("run-finding-mutate", task={"task_id": "task-mutate", "run_id": "run-finding-mutate", "goal": "g"}, store=store)
    changed = dict(first_finding, severity="critical", summary="new summary", recommended_action="new action")
    upsert_trajectory_finding(changed, db_path=db_path)
    second = compact_run("run-finding-mutate", task={"task_id": "task-mutate", "run_id": "run-finding-mutate", "goal": "g"}, store=store)
    assert second.context_id != first.context_id
    assert second.important_findings[0]["severity"] == "critical"
    assert second.important_findings[0]["summary"] == "new summary"


def test_previous_merge_is_bounded_and_current_artifact_wins(tmp_path: Path):
    db_path = tmp_path / "state.db"
    store = ObservationStore(db_path)
    ledger = TrajectoryLedger(db_path)
    ledger.append_event({"run_id": "run-merge", "event_type": "task_started"})
    old = ContextPack(
        context_id="ctx-old", run_id="run-merge", task_id="task-merge", workflow_id=None, goal="g",
        completed=[{"text": f"done-{i}", "refs": ["evt_1"]} for i in range(30)],
        verified_facts=[{"fact_type": "verification", "event_id": "evt_1"} for _ in range(30)],
        evidence_refs=[f"obs_{i}" for i in range(30)],
        artifact_refs=[{"ref": "report.txt", "kind": "old", "observation_id": "obs_1"}],
        open_issues=[{"text": f"issue-{i}", "refs": ["evt_1"]} for i in range(30)],
        next_focus=[{"text": f"focus-{i}", "refs": ["evt_1"]} for i in range(10)],
        source_event_sequence=1, created_at=1.0, metadata={"context_source_fingerprint": "old"},
    )
    state_db.save_context_pack(old.to_mapping(), db_path=db_path)
    ledger.append_event({"run_id": "run-merge", "event_type": "artifact_created", "artifact": {"ref": "report.txt", "kind": "current"}})
    pack = compact_run("run-merge", task={"task_id": "task-merge", "run_id": "run-merge", "goal": "g"}, store=store, provider=None, config={"max_observations": 2, "max_artifacts": 2})
    assert len(pack.completed) <= 20
    assert len(pack.open_issues) <= 20
    assert len(pack.next_focus) <= 3
    assert len(pack.verified_facts) <= 20
    assert len(pack.evidence_refs) <= 2
    assert len(pack.artifact_refs) <= 2
    assert pack.artifact_refs[0]["kind"] == "current"
    assert len(json.dumps(pack.to_mapping(), ensure_ascii=False)) < 20000


@pytest.mark.parametrize("mode", ["all_accept", "partial", "all_reject", "malformed", "exception"])
def test_reducer_result_contract_is_bounded_and_fail_safe(tmp_path: Path, mode: str):
    ledger = TrajectoryLedger(tmp_path / "state.db")
    ledger.append_event({"run_id": f"run-contract-{mode}", "event_type": "task_completed"})

    class Provider:
        def reduce(self, payload):
            if mode == "exception":
                raise RuntimeError("provider down")
            if mode == "malformed":
                return {"completed": None, "open_issues": 3, "next_focus": [{"text": 4, "refs": "evt_1"}]}
            item = {"text": "selected", "refs": ["evt_1"]}
            if mode == "all_accept":
                return {"completed": [item], "open_issues": [], "next_focus": [item], "selected_findings": [], "selected_observations": [], "selected_artifacts": []}
            if mode == "partial":
                return {"completed": [item], "open_issues": [], "next_focus": []}
            return {"completed": [], "open_issues": [], "next_focus": [], "selected_findings": [], "selected_observations": [], "selected_artifacts": []}

    pack = compact_run(
        f"run-contract-{mode}", task={"task_id": f"task-{mode}", "run_id": f"run-contract-{mode}", "goal": "g"},
        store=ObservationStore(tmp_path / "state.db"), provider=Provider(),
    )
    if mode == "all_accept":
        assert pack.completed and pack.next_focus
    elif mode == "partial":
        assert pack.completed and pack.open_issues == [] and pack.next_focus == []
    elif mode == "all_reject":
        assert pack.completed == [] and pack.open_issues == [] and pack.next_focus == []
    else:
        assert pack.completed


def test_over_budget_branch_never_reintroduces_raw_secret(tmp_path: Path):
    db_path = tmp_path / "state.db"
    ledger = TrajectoryLedger(db_path)
    secret = "sk-budget-secret-123456789"
    ledger.append_event({"run_id": "run-budget-redact", "event_type": "progress", "metadata": {"blob": "x" * 5000, "api_key": secret}})
    captured = {}

    class Provider:
        def reduce(self, payload):
            captured["payload"] = payload
            return {"completed": [], "open_issues": [], "next_focus": []}

    compact_run(
        "run-budget-redact",
        task={"task_id": "task-budget-redact", "run_id": "run-budget-redact", "goal": f"password={secret}"},
        store=ObservationStore(db_path), provider=Provider(), config={"max_input_chars": 300},
    )
    serialized = json.dumps(captured["payload"], ensure_ascii=False)
    assert len(serialized) <= 300
    assert secret not in serialized


def test_context_pack_limits_text_refs_and_serialized_size(tmp_path: Path):
    db_path = tmp_path / "state.db"
    ledger = TrajectoryLedger(db_path)
    events = [ledger.append_event({"run_id": "run-pack-limits", "event_type": "progress"}) for _ in range(MAX_CONTEXT_REFS + 10)]

    class Provider:
        def reduce(self, payload):
            return {
                "completed": [{"text": "x" * (MAX_CONTEXT_TEXT_CHARS * 3), "refs": [item["event_id"] for item in events]}],
                "open_issues": [], "next_focus": [],
            }

    pack = compact_run(
        "run-pack-limits", task={"task_id": "task-pack-limits", "run_id": "run-pack-limits", "goal": "g" * (MAX_CONTEXT_TEXT_CHARS * 3)},
        store=ObservationStore(db_path), provider=Provider(),
    )
    assert len(pack.goal or "") <= MAX_CONTEXT_TEXT_CHARS
    assert len(pack.completed[0]["text"]) <= MAX_CONTEXT_TEXT_CHARS
    assert len(pack.completed[0]["refs"]) <= MAX_CONTEXT_REFS
    assert events[0]["event_id"] in pack.completed[0]["refs"]
    assert len(json.dumps(pack.to_mapping(), ensure_ascii=False)) <= MAX_CONTEXT_SERIALIZED_CHARS


def test_updated_finding_supersedes_previous_semantic_items(tmp_path: Path):
    db_path = tmp_path / "state.db"
    store = ObservationStore(db_path)
    old = _finding(run_id="run-supersede", finding_id="finding-supersede")
    upsert_trajectory_finding(old, db_path=db_path)
    first = compact_run("run-supersede", task={"task_id": "task-supersede", "run_id": "run-supersede", "goal": "g"}, store=store)
    changed = dict(old, severity="critical", summary="new contradictory issue", recommended_action="use new recommendation")
    upsert_trajectory_finding(changed, db_path=db_path)
    TrajectoryLedger(db_path).append_event({"run_id": "run-supersede", "event_type": "progress"})
    second = compact_run("run-supersede", task={"task_id": "task-supersede", "run_id": "run-supersede", "goal": "g"}, store=store)
    assert first.open_issues and first.next_focus
    assert [item["text"] for item in second.open_issues if "finding-supersede" in item["refs"]] == ["new contradictory issue"]
    assert [item["text"] for item in second.next_focus if "finding-supersede" in item["refs"]] == ["use new recommendation"]


def test_save_context_pack_dedups_only_latest_fingerprint_and_latest_matches_return(tmp_path: Path):
    db_path = tmp_path / "state.db"

    def pack(context_id, fingerprint, created_at):
        return ContextPack(context_id=context_id, run_id="run-aba", task_id=None, workflow_id=None, goal="g", source_event_sequence=1, created_at=created_at, metadata={"context_source_fingerprint": fingerprint}).to_mapping()

    first = state_db.save_context_pack(pack("ctx-a", "A", 1), db_path=db_path)
    second = state_db.save_context_pack(pack("ctx-b", "B", 2), db_path=db_path)
    third = state_db.save_context_pack(pack("ctx-a2", "A", 3), db_path=db_path)
    assert first["context_id"] == "ctx-a"
    assert second["context_id"] == "ctx-b"
    assert third["context_id"] == "ctx-a2"
    assert state_db.get_latest_context_pack("run-aba", db_path=db_path)["context_id"] == "ctx-a2"


def test_terminal_observer_barrier_persists_finding_before_compact(tmp_path: Path, monkeypatch):
    controller = importlib.import_module("services.herdr-controller")
    db_path = tmp_path / "state.db"
    store = ObservationStore(db_path)
    task = {"task_id": "task-terminal-order", "run_id": "run-terminal-order", "goal": "g"}
    TrajectoryLedger(db_path).append_event({"run_id": "run-terminal-order", "task_id": "task-terminal-order", "event_type": "agent_done"})
    barrier = threading.Event()
    monkeypatch.setattr(controller, "_get_store", lambda: store)
    monkeypatch.setattr("herdr.observer.harness.get_provider", lambda: None)
    controller._schedule_context_compact(task, wait_for=barrier)
    time.sleep(0.05)
    assert state_db.get_latest_context_pack("run-terminal-order", db_path=db_path) is None
    upsert_trajectory_finding(_finding(run_id="run-terminal-order", finding_id="finding-terminal"), db_path=db_path)
    barrier.set()
    deadline = time.time() + 3
    while time.time() < deadline and state_db.get_latest_context_pack("run-terminal-order", db_path=db_path) is None:
        time.sleep(0.02)
    latest = state_db.get_latest_context_pack("run-terminal-order", db_path=db_path)
    assert latest is not None
    assert latest["important_findings"][0]["finding_id"] == "finding-terminal"


def test_done_gateway_passes_observer_completion_barrier_to_compact(monkeypatch):
    controller = importlib.import_module("services.herdr-controller")
    calls = []
    barrier = {}

    def observer(task, completion_event=None, **kwargs):
        calls.append("observer")
        barrier["event"] = completion_event
        return True

    def compact(task, wait_for=None):
        calls.append("compact")
        assert wait_for is barrier["event"]
        return True

    monkeypatch.setattr(controller, "_observer_terminal_checkpoint", observer)
    monkeypatch.setattr(controller, "_schedule_context_compact", compact)
    monkeypatch.setattr(controller, "supervisor_harness", None)
    monkeypatch.setattr(controller, "enqueue_coordinator_event", lambda *args, **kwargs: None)
    assert controller.emit_done_if_allowed({"task_id": "task-order", "run_id": "run-order"}) is True
    assert calls == ["observer", "compact"]
