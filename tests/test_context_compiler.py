from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from herdr import state_db
from herdr.observation import ObservationStore, create_observation
from herdr.trajectory import TrajectoryLedger


def _save_working_context_in_process(db_path, payload, gate=None, ready=None):
    if ready is not None:
        ready.set()
    if gate is not None:
        gate.wait(10)
    state_db.save_working_context(payload, db_path=Path(db_path))


def _seed_workflow(db: Path, *, workflow_id: str = "wf-context", scope: str = "wf-exec-1") -> None:
    state_db.save_workflow(
        {
            "workflow_id": workflow_id,
            "title": "Context Compiler fixture",
            "status": "running",
            "current_stage": "review",
            "config": {
                "nodes": [
                    {
                        "id": "implementation",
                        "label": "Implementation",
                        "depends_on": [],
                        "purpose": "Implement the requested behavior",
                        "rules": ["Keep source facts authoritative"],
                    },
                    {
                        "id": "review",
                        "label": "Review",
                        "depends_on": ["implementation"],
                        "purpose": "Review the implementation",
                        "rules": ["Check evidence and risks"],
                    },
                    {
                        "id": "test",
                        "label": "Test",
                        "depends_on": ["review"],
                        "purpose": "Verify acceptance criteria",
                        "rules": ["Do not claim unverified success"],
                    },
                ]
            },
        },
        db_path=db,
    )


def _task(
    task_id: str,
    *,
    workflow_id: str = "wf-context",
    scope: str = "wf-exec-1",
    run_id: str | None = None,
    node: str = "review",
    status: str = "working",
    role: str = "developer",
) -> dict:
    return {
        "task_id": task_id,
        "workflow_id": workflow_id,
        "run_id": run_id or f"run-{task_id}",
        "workflow_run_id": scope,
        "node": node,
        "stage": node,
        "agent": role,
        "agent_role": role,
        "status": status,
        "goal": f"Complete the {node} work for {task_id}",
        "acceptance_criteria": "The requested behavior is implemented and verified.",
        "created_at": 1.0,
    }


def _seed_task(db: Path, task: dict) -> dict:
    state_db.save_task(task, db_path=db)
    return task


def _finding(
    run_id: str,
    finding_id: str,
    *,
    task_id: str = "task-upstream",
    node: str = "implementation",
    summary: str = "A bounded implementation risk",
    severity: str = "warning",
    metadata: dict | None = None,
    evidence: list | None = None,
    created_at: float = 10.0,
) -> dict:
    return {
        "finding_id": finding_id,
        "finding_key": f"key-{finding_id}",
        "run_id": run_id,
        "task_id": task_id,
        "workflow_id": "wf-context",
        "node": node,
        "agent": "developer",
        "finding_type": "other",
        "severity": severity,
        "status": "open",
        "summary": summary,
        "recommended_action": "inspect",
        "confidence": 0.8,
        "evidence": evidence or [],
        "metadata": metadata or {},
        "created_at": created_at,
    }


def _compile(db: Path, task: dict, role: str, **kwargs):
    from herdr.context_compiler import compile_working_context

    return compile_working_context(
        workflow_id=task["workflow_id"],
        task_id=task["task_id"],
        agent_role=role,
        store=ObservationStore(db),
        **kwargs,
    )


def test_relevance_uses_state_role_and_dependency_before_recency():
    from herdr.context_compiler import context_relevance

    base = {"kind": "finding", "source_task": "other", "created_at": 1.0, "metadata": {}}
    dependency = dict(base, source_task="dependency-task")
    recent_irrelevant = dict(base, source_task="unrelated", created_at=999.0)
    state = {"task_id": "target", "task_status": "working", "current_node": "review"}
    assert context_relevance(
        dependency,
        agent_role="reviewer",
        current_state=state,
        dependency_ids=("dependency-task",),
        current_node_id="implementation",
        now=1000.0,
    ) > context_relevance(
        recent_irrelevant,
        agent_role="reviewer",
        current_state=state,
        dependency_ids=("dependency-task",),
        current_node_id="implementation",
        now=1000.0,
    )


def test_direct_dependency_state_has_task_provenance(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    upstream = _seed_task(db, _task("task-dependency", node="implementation", status="completed"))
    target = _seed_task(db, _task("task-dependency-target", node="review"))
    context = _compile(db, target, "reviewer")
    assert context.current_state["dependency_state"]["implementation"] == "completed"
    assert f"task:{upstream['task_id']}" in context.source_refs


def test_task_artifact_fields_are_projected_without_body(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = dict(_task("task-artifact-fields", node="review"))
    target["artifacts"] = [{"ref": "reports/review.md", "kind": "report", "content": "SECRET BODY"}]
    _seed_task(db, target)
    context = _compile(db, target, "reviewer")
    assert any(item.get("value", {}).get("ref") == "reports/review.md" for item in context.artifacts)
    assert "SECRET BODY" not in json.dumps(context.to_mapping(), ensure_ascii=False)


def test_state_aware_contexts_are_distinct(tmp_path: Path):
    from herdr.context_compiler import compile_working_context

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-target", node="review", role="developer"))
    upstream = _seed_task(db, _task("task-upstream", node="implementation", role="developer"))
    state_db.upsert_trajectory_finding(
        _finding(upstream["run_id"], "fnd-upstream", task_id=upstream["task_id"]),
        db_path=db,
    )

    developer = compile_working_context(
        workflow_id=target["workflow_id"],
        task_id=target["task_id"],
        agent_role="developer",
        store=ObservationStore(db),
    )
    reviewer = compile_working_context(
        workflow_id=target["workflow_id"],
        task_id=target["task_id"],
        agent_role="reviewer",
        store=ObservationStore(db),
    )

    assert developer.context_id != reviewer.context_id
    assert developer.agent_role == "developer"
    assert reviewer.agent_role == "reviewer"
    assert developer.current_state != reviewer.current_state
    assert developer.context_fingerprint != reviewer.context_fingerprint
    assert "requirements" in developer.current_state
    assert "review_scope" in reviewer.current_state
    assert any("fnd-upstream" in ref for ref in reviewer.source_refs)

    state_db.upsert_trajectory_finding(
        _finding(
            target["run_id"], "fnd-target-implementation",
            task_id=target["task_id"], node="implementation", severity="critical",
        ),
        db_path=db,
    )
    developer_with_target = compile_working_context(
        workflow_id=target["workflow_id"], task_id=target["task_id"],
        agent_role="developer", store=ObservationStore(db),
    )
    reviewer_with_target = compile_working_context(
        workflow_id=target["workflow_id"], task_id=target["task_id"],
        agent_role="reviewer", store=ObservationStore(db),
    )
    assert any("fnd-target-implementation" in ref for ref in developer_with_target.source_refs)
    assert all("fnd-target-implementation" not in item.get("source_ref", "") for item in reviewer_with_target.findings)


def test_global_item_budget_updates_reported_metrics(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-item-budget"))
    upstream = _seed_task(db, _task("task-upstream"))
    for index in range(8):
        state_db.upsert_trajectory_finding(
            _finding(upstream["run_id"], f"fnd-item-{index}", task_id=upstream["task_id"]),
            db_path=db,
        )
    context = _compile(db, target, "developer", config={"max_items": 1})
    assert context.metrics["selected_items"] <= 1
    assert sum(len(value) for value in (
        context.completed, context.artifacts, context.evidence, context.findings,
        context.decisions, context.blockers, context.open_questions,
        context.verification, context.handoffs,
    )) <= 1


def test_budget_preserves_role_state_blockers_and_failed_verification(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = dict(
        _task("task-budget-priority-state", node="test", role="tester", status="blocked"),
        acceptance_criteria=[f"criterion-{index}" for index in range(20)],
        blockers=[f"blocker-{index}" for index in range(5)],
    )
    _seed_task(db, target)
    TrajectoryLedger(db).append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "verification_completed",
        "verification": {"passed": False},
    })
    context = _compile(db, target, "tester")
    assert context.goal
    assert context.current_state.get("acceptance_criteria")
    assert context.blockers
    assert any(item.get("value", {}).get("passed") is False for item in context.verification)


def test_budget_keeps_blocker_before_completed_history(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    upstream = _seed_task(db, _task("task-history", node="implementation", status="completed"))
    target = _seed_task(db, dict(_task("task-blocker-priority", node="review", status="blocked"), blocker="critical blocker"))
    context = _compile(db, target, "developer", config={"max_items": 1})
    assert context.blockers
    assert context.completed == []
    assert any(item.get("value") == "critical blocker" for item in context.blockers)
    assert all(item.get("source_task") != upstream["task_id"] for item in context.completed)


def test_total_character_budget_is_hard_for_large_goal_and_refs(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    upstream = _seed_task(db, _task("task-large-source"))
    target = dict(_task("task-large-context", node="review"), goal="g" * 50000)
    target["artifacts"] = [{"ref": f"artifact-{i}", "kind": "report"} for i in range(50)]
    _seed_task(db, target)
    state_db.upsert_trajectory_finding(
        _finding(upstream["run_id"], "fnd-large", task_id=upstream["task_id"], summary="s" * 5000),
        db_path=db,
    )
    context = _compile(db, target, "reviewer", config={"max_chars": 2000})
    assert context.metrics["context_chars"] <= 2000
    assert len(json.dumps(context.to_mapping(), ensure_ascii=False)) <= 2000
    assert context.goal
    assert context.current_state.get("task_status") == "working"


def test_impossible_context_budget_fails_closed_instead_of_dropping_goal(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-impossible-budget"))
    with pytest.raises(ValueError, match="max_chars"):
        _compile(db, target, "developer", config={"max_chars": 500})


def test_state_aware_context_changes_after_node_transition(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-state", node="implementation", status="working"))
    first = _compile(db, target, "developer")

    state_db.save_task(
        dict(target, node="review", stage="review", status="agent_done"),
        db_path=db,
    )
    second = _compile(db, dict(target, node="review", status="agent_done"), "reviewer")

    assert first.context_id != second.context_id
    assert first.context_fingerprint != second.context_fingerprint
    assert first.current_state["current_node"] == "implementation"
    assert second.current_state["current_node"] == "review"
    assert second.current_state["task_status"] == "agent_done"


def test_superseded_finding_is_excluded_but_history_remains(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-supersede"))
    upstream = _seed_task(db, _task("task-upstream"))
    old = _finding(upstream["run_id"], "fnd-old", task_id=upstream["task_id"], summary="old timeout exists")
    state_db.upsert_trajectory_finding(old, db_path=db)
    new = _finding(
        upstream["run_id"],
        "fnd-new",
        task_id=upstream["task_id"],
        summary="timeout fixed",
        metadata={"supersedes": "fnd-old"},
        created_at=20.0,
    )
    state_db.upsert_trajectory_finding(new, db_path=db)

    context = _compile(db, target, "developer")

    assert not any("fnd-old" in ref for ref in context.source_refs)
    assert any("fnd-new" in ref for ref in context.source_refs)
    assert state_db.get_trajectory_finding_by_id("fnd-old", db_path=db) is not None
    assert state_db.get_trajectory_finding_by_id("fnd-new", db_path=db) is not None


def test_invalid_or_cyclic_supersession_is_excluded(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-invalid-supersession"))
    upstream = _seed_task(db, _task("task-upstream"))
    missing = _finding(upstream["run_id"], "fnd-missing-relation", task_id=upstream["task_id"], summary="missing")
    missing["metadata"] = {"supersedes": "does-not-exist"}
    cycle_a = _finding(upstream["run_id"], "fnd-cycle-a", task_id=upstream["task_id"], summary="cycle a")
    cycle_b = _finding(upstream["run_id"], "fnd-cycle-b", task_id=upstream["task_id"], summary="cycle b")
    cycle_a["metadata"] = {"supersedes": "fnd-cycle-b"}
    cycle_b["metadata"] = {"supersedes": "fnd-cycle-a"}
    for finding in (missing, cycle_a, cycle_b):
        state_db.upsert_trajectory_finding(finding, db_path=db)

    context = _compile(db, target, "reviewer")
    assert not any(any(token in ref for token in ("missing-relation", "cycle-a", "cycle-b")) for ref in context.source_refs)


def test_top_level_finding_supersession_is_normalized(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-top-level-supersede"))
    upstream = _seed_task(db, _task("task-upstream"))
    old = _finding(upstream["run_id"], "fnd-top-old", task_id=upstream["task_id"])
    state_db.upsert_trajectory_finding(old, db_path=db)
    new = _finding(upstream["run_id"], "fnd-top-new", task_id=upstream["task_id"], summary="fixed")
    new["supersedes"] = "fnd-top-old"
    state_db.upsert_trajectory_finding(new, db_path=db)

    stored = state_db.get_trajectory_finding_by_id("fnd-top-new", db_path=db)
    assert stored["metadata"]["supersedes"] == "fnd-top-old"
    context = _compile(db, target, "developer")
    assert not any("fnd-top-old" in ref for ref in context.source_refs)
    assert any("fnd-top-new" in ref for ref in context.source_refs)


def test_context_metadata_is_redacted_before_persistence(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-secret-metadata"))
    target["node"] = "password=metadata-secret"
    _seed_task(db, target)
    context = _compile(db, target, "developer")
    assert "metadata-secret" not in json.dumps(context.to_mapping(), ensure_ascii=False)


def test_finding_preserves_evidence_ref_without_reading_content(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-evidence"))
    upstream = _seed_task(db, _task("task-upstream"))
    observation = create_observation(
        run_id=upstream["run_id"],
        task_id=upstream["task_id"],
        source_type="verification",
        source_ref="verification:pytest-1",
        content="x" * 10000,
        excerpt="bounded test evidence",
        store=ObservationStore(db),
    )
    state_db.upsert_trajectory_finding(
        _finding(
            upstream["run_id"],
            "fnd-evidence",
            task_id=upstream["task_id"],
            evidence=[{"observation_id": observation.observation_id}],
        ),
        db_path=db,
    )

    context = _compile(db, target, "reviewer")

    assert any(observation.observation_id in ref for ref in context.source_refs)
    finding_items = [item for item in context.findings if item.get("kind") == "finding"]
    assert finding_items
    assert any(observation.observation_id in ref for item in finding_items for ref in item.get("evidence_refs", []))
    assert "x" * 1000 not in json.dumps(context.to_mapping(), ensure_ascii=False)


def test_run_isolation_rejects_other_run_sources(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-isolation", scope="wf-exec-1", run_id="run-target"))
    other = _seed_task(
        db,
        _task("task-other", scope="wf-exec-2", run_id="run-other", workflow_id="wf-other"),
    )
    state_db.upsert_trajectory_finding(
        _finding(other["run_id"], "fnd-other", task_id=other["task_id"]),
        db_path=db,
    )
    other_obs = create_observation(
        run_id=other["run_id"],
        task_id=other["task_id"],
        source_type="agent_log",
        source_ref="other-pane",
        content="other secret transcript",
        store=ObservationStore(db),
    )
    ledger = TrajectoryLedger(db)
    ledger.append_event(
        {
            "run_id": other["run_id"],
            "task_id": other["task_id"],
            "workflow_id": other["workflow_id"],
            "event_type": "task_completed",
            "metadata": {"text": "other completion"},
        }
    )
    mismatched_event = ledger.append_event(
        {
            "run_id": target["run_id"],
            "task_id": target["task_id"],
            "workflow_id": "wf-other",
            "event_type": "artifact_created",
            "artifact": {"ref": "mismatched-workflow-artifact"},
        }
    )

    context = _compile(db, target, "coordinator")
    serialized = json.dumps(context.to_mapping(), ensure_ascii=False)
    assert "fnd-other" not in serialized
    assert other_obs.observation_id not in serialized
    assert "other completion" not in serialized
    assert mismatched_event["event_id"] not in serialized
    assert "mismatched-workflow-artifact" not in serialized
    assert context.run_scope == "wf-exec-1"
    assert context.run_id == "run-target"


def test_legacy_scope_does_not_mix_unlinked_task_runs(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = dict(_task("task-legacy-target"), workflow_run_id=None)
    target.pop("workflow_run_id")
    upstream = dict(_task("task-legacy-upstream"), workflow_run_id=None, status="completed", blocker="unlinked blocker")
    upstream.pop("workflow_run_id")
    _seed_task(db, target)
    _seed_task(db, upstream)
    state_db.upsert_trajectory_finding(
        _finding(upstream["run_id"], "fnd-unlinked", task_id=upstream["task_id"]),
        db_path=db,
    )
    unrelated_task = _task("task-legacy-unrelated")
    unrelated_task.pop("workflow_run_id")
    unrelated = _seed_task(db, unrelated_task)
    state_db.create_collaboration_event(
        {
            "run_id": "wf-context",
            "workflow_id": "wf-context",
            "from_task_id": upstream["task_id"],
            "to_task_id": unrelated["task_id"],
            "source_fact_id": "unrelated-handoff",
        },
        db_path=db,
    )

    context = _compile(db, target, "developer")
    assert not any("fnd-unlinked" in ref for ref in context.source_refs)
    assert "unlinked blocker" not in json.dumps(context.to_mapping(), ensure_ascii=False)


def test_budget_limits_hundreds_of_findings_and_total_chars(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-budget"))
    upstream = _seed_task(db, _task("task-upstream"))
    for index in range(120):
        state_db.upsert_trajectory_finding(
            _finding(
                upstream["run_id"],
                f"fnd-budget-{index}",
                task_id=upstream["task_id"],
                summary=f"finding {index}",
                severity="critical" if index == 0 else "info",
                created_at=float(index),
            ),
            db_path=db,
        )

    context = _compile(
        db,
        target,
        "reviewer",
        config={"max_items_per_kind": {"findings": 10}, "max_chars": 12000},
    )

    assert len(context.findings) <= 10
    assert context.metrics["selected_items"] <= 40
    assert context.metrics["context_chars"] <= 12000
    assert len(json.dumps(context.to_mapping(), ensure_ascii=False)) <= 12000


def test_context_contains_no_raw_history_or_transcript(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-history"))
    upstream = _seed_task(db, _task("task-upstream"))
    ledger = TrajectoryLedger(db)
    ledger.append_event(
        {
            "run_id": upstream["run_id"],
            "task_id": upstream["task_id"],
            "workflow_id": upstream["workflow_id"],
            "event_type": "terminal_transcript",
            "metadata": {"transcript": "CHAIN_OF_THOUGHT_SECRET"},
        }
    )
    ledger.append_event(
        {
            "run_id": upstream["run_id"],
            "task_id": upstream["task_id"],
            "workflow_id": upstream["workflow_id"],
            "event_type": "chat_history",
            "metadata": {"messages": "PRIVATE_CHAT_SECRET"},
        }
    )

    context = _compile(db, target, "developer")
    serialized = json.dumps(context.to_mapping(), ensure_ascii=False)
    assert "CHAIN_OF_THOUGHT_SECRET" not in serialized
    assert "PRIVATE_CHAT_SECRET" not in serialized
    assert "terminal_transcript" not in serialized
    assert "chat_history" not in serialized


def test_recovered_failure_event_is_not_current_blocker(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-recovered-failure", status="completed"))
    ledger = TrajectoryLedger(db)
    failed = ledger.append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "task_failed",
        "metadata": {"reason": "old failure"},
    })
    ledger.append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "task_completed",
    })
    context = _compile(db, target, "developer")
    assert failed["event_id"] not in json.dumps(context.to_mapping(), ensure_ascii=False)
    assert "old failure" not in json.dumps(context.to_mapping(), ensure_ascii=False)
    assert not context.blockers


def test_compile_does_not_write_source_fact_tables(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-no-source-write"))
    upstream = _seed_task(db, _task("task-upstream"))
    state_db.upsert_trajectory_finding(
        _finding(upstream["run_id"], "fnd-no-write", task_id=upstream["task_id"]),
        db_path=db,
    )
    before = {}
    with state_db.get_db_connection(db) as conn:
        for table in ("tasks", "events", "trajectory_findings", "observations", "collaboration_events"):
            before[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    _compile(db, target, "reviewer")
    with state_db.get_db_connection(db) as conn:
        for table, count in before.items():
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count
        assert conn.execute("SELECT COUNT(*) FROM working_contexts").fetchone()[0] == 1


def test_snapshot_is_immutable_and_recompiles_after_source_change(tmp_path: Path):
    from herdr.context_compiler import get_working_context

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-immutable", status="working"))
    first = _compile(db, target, "developer")
    first_payload = first.to_mapping()

    state_db.save_task(dict(target, status="rework", blocker="new blocker"), db_path=db)
    second = _compile(db, dict(target, status="rework", blocker="new blocker"), "developer")

    assert first.context_id != second.context_id
    assert get_working_context(first.context_id, db_path=db).to_mapping() == first_payload
    assert second.current_state["task_status"] == "rework"
    assert any(item.get("value") == "new blocker" for item in second.blockers)


def test_storage_fingerprint_does_not_cross_run_scope(tmp_path: Path):
    db = tmp_path / "state.db"

    def payload(context_id: str, scope: str, fingerprint: str, created_at: float):
        return {
            "context_id": context_id,
            "run_scope": scope,
            "run_id": f"run-{scope}",
            "workflow_id": "wf",
            "task_id": "task-reused",
            "node_id": "review",
            "agent_role": "reviewer",
            "goal": "goal",
            "current_state": {},
            "findings": [],
            "artifacts": [],
            "evidence": [],
            "completed": [],
            "decisions": [],
            "blockers": [],
            "open_questions": [],
            "verification": [],
            "handoffs": [],
            "next_action": "review",
            "source_refs": [],
            "context_fingerprint": fingerprint,
            "source_version": fingerprint,
            "compiled_at": created_at,
            "metrics": {},
        }

    first = state_db.save_working_context(payload("wc-scope-a", "scope-a", "same", 1.0), db_path=db)
    second = state_db.save_working_context(payload("wc-scope-b", "scope-b", "same", 2.0), db_path=db)
    assert first["context_id"] == "wc-scope-a"
    assert second["context_id"] == "wc-scope-b"
    assert len(state_db.list_working_contexts("task-reused", db_path=db)) == 2


def test_missing_finding_evidence_is_not_emitted_as_a_valid_reference(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-missing-evidence"))
    upstream = _seed_task(db, _task("task-upstream"))
    state_db.upsert_trajectory_finding(
        _finding(
            upstream["run_id"], "fnd-missing-evidence",
            task_id=upstream["task_id"],
            evidence=[{"observation_id": "obs_missing"}],
        ),
        db_path=db,
    )
    context = _compile(db, target, "reviewer")
    assert not any("obs_missing" in ref for ref in context.source_refs)
    assert not any("obs_missing" in ref for item in context.findings for ref in item.get("evidence_refs", []))


def test_concurrent_context_writers_do_not_replace_newer_latest(tmp_path: Path):
    db = tmp_path / "state.db"

    def payload(context_id: str, fingerprint: str, created_at: float, source_watermark: int = 0):
        return {
            "context_id": context_id,
            "run_scope": "scope",
            "run_id": "run",
            "workflow_id": "wf",
            "task_id": "task-concurrent",
            "node_id": "review",
            "agent_role": "developer",
            "goal": "goal",
            "current_state": {},
            "findings": [],
            "artifacts": [],
            "evidence": [],
            "completed": [],
            "decisions": [],
            "blockers": [],
            "open_questions": [],
            "verification": [],
            "handoffs": [],
            "next_action": "continue",
            "source_refs": [],
            "context_fingerprint": fingerprint,
            "source_version": fingerprint,
            "source_watermark": source_watermark,
            "compiled_at": created_at,
            "metrics": {},
        }

    ctx = multiprocessing.get_context("spawn")
    gate = ctx.Event()
    ready = ctx.Event()
    old = ctx.Process(
        target=_save_working_context_in_process,
        args=(str(db), payload("wc-old", "old", 10.0), gate, ready),
    )
    old.start()
    assert ready.wait(10)
    state_db.save_working_context(payload("wc-new", "new", 20.0), db_path=db)
    gate.set()
    old.join(10)
    assert old.exitcode == 0
    latest = state_db.get_latest_working_context("task-concurrent", db_path=db)
    assert latest["context_id"] == "wc-new"


def test_storage_rejects_old_source_watermark_after_newer_snapshot(tmp_path: Path):
    db = tmp_path / "state.db"
    state_db.save_working_context(
        {
            "context_id": "wc-v2", "run_scope": "scope", "run_id": "run",
            "workflow_id": "wf", "task_id": "task-version", "node_id": "review",
            "agent_role": "reviewer", "goal": "goal", "current_state": {},
            "findings": [], "artifacts": [], "evidence": [], "completed": [],
            "decisions": [], "blockers": [], "open_questions": [], "verification": [],
            "handoffs": [], "next_action": "review", "source_refs": [],
            "context_fingerprint": "v2", "source_version": "v2", "source_watermark": 20,
            "compiled_at": 20.0, "metrics": {},
        },
        db_path=db,
    )
    returned = state_db.save_working_context(
        {
            "context_id": "wc-v1-late", "run_scope": "scope", "run_id": "run",
            "workflow_id": "wf", "task_id": "task-version", "node_id": "review",
            "agent_role": "reviewer", "goal": "goal", "current_state": {},
            "findings": [], "artifacts": [], "evidence": [], "completed": [],
            "decisions": [], "blockers": [], "open_questions": [], "verification": [],
            "handoffs": [], "next_action": "review", "source_refs": [],
            "context_fingerprint": "v1", "source_version": "v1", "source_watermark": 10,
            "compiled_at": 30.0, "metrics": {},
        },
        db_path=db,
    )
    assert returned["context_id"] == "wc-v2"
    assert state_db.get_latest_working_context("task-version", db_path=db)["context_id"] == "wc-v2"


def test_storage_rejects_items_without_provenance(tmp_path: Path):
    db = tmp_path / "state.db"
    with pytest.raises(ValueError, match="source_ref"):
        state_db.save_working_context(
            {
                "context_id": "wc-invalid",
                "run_scope": "scope",
                "task_id": "task-invalid",
                "agent_role": "developer",
                "context_fingerprint": "fingerprint",
                "findings": [{"kind": "finding", "value": "unproven"}],
            },
            db_path=db,
        )


def test_state_store_exposes_working_context_readers(tmp_path: Path):
    from herdr.context_compiler import get_latest_working_context, list_working_contexts
    from herdr.state_store import SQLiteStateStore

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-store-readers"))
    context = _compile(db, target, "developer")
    store = SQLiteStateStore(db_path=db)
    assert store.get_working_context(context.context_id)["task_id"] == target["task_id"]
    assert get_latest_working_context(target["task_id"], db_path=db).context_id == context.context_id
    assert [item.context_id for item in list_working_contexts(target["task_id"], db_path=db)] == [context.context_id]


def test_run_metrics_count_reused_compile_invocations(tmp_path: Path):
    from herdr.metrics import get_run_metrics

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-run-metrics"))
    first = _compile(db, target, "developer")
    second = _compile(db, target, "developer")
    metrics = get_run_metrics(target["run_id"], db_path=db)
    assert metrics.working_context_compiles >= 2
    assert metrics.working_context_reused >= 1
    assert metrics.working_context_changed >= 1
    assert first.context_id == second.context_id


def test_metrics_report_reuse_and_changed_without_quality_score(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-metrics", status="working"))
    first = _compile(db, target, "developer")
    second = _compile(db, target, "developer")
    assert first.context_id == second.context_id
    assert second.metrics["context_reuse"] is True
    assert second.metrics["context_changed"] is False
    assert "quality_score" not in second.metrics

    state_db.save_task(dict(target, status="rework"), db_path=db)
    changed = _compile(db, dict(target, status="rework"), "developer")
    assert changed.context_id != first.context_id
    assert changed.metrics["context_reuse"] is False
    assert changed.metrics["context_changed"] is True


def test_trajectory_verification_event_is_projected_and_does_not_shadow_list(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-verification-event", node="test", role="tester"))
    observation = create_observation(
        run_id=target["run_id"], task_id=target["task_id"],
        source_type="verification", source_ref="verification:event-test",
        content="failed test", store=ObservationStore(db),
    )
    TrajectoryLedger(db).append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"],
        "event_type": "verification_completed",
        "verification": {"passed": False, "observation_id": observation.observation_id},
    })
    context = _compile(db, target, "tester")
    assert any(item.get("value", {}).get("passed") is False for item in context.verification)
    assert any(observation.observation_id in ref for item in context.verification for ref in item.get("evidence_refs", []))


def test_latest_verification_failure_wins_over_old_success(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-verification-order", node="test", role="tester"))
    ledger = TrajectoryLedger(db)
    ledger.append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "verification_completed",
        "verification": {"passed": True},
    })
    ledger.append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "verification_completed",
        "verification": {"passed": False},
    })
    context = _compile(db, target, "tester")
    values = [item.get("value", {}).get("passed") for item in context.verification]
    assert values == [False]


def test_eval_verification_is_available_as_bounded_evidence(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-eval", node="test", role="tester"))
    state_db.get_db_connection(db).close()
    from herdr.eval_store import record_eval_result
    observation = create_observation(
        run_id=target["run_id"], task_id=target["task_id"],
        source_type="verification", source_ref="verification:eval-test",
        content="verified", store=ObservationStore(db),
    )

    record_eval_result(
        target["run_id"],
        task_id=target["task_id"],
        workflow_id=target["workflow_id"],
        verification_passed=True,
        evidence={"observation_id": observation.observation_id},
        db_path=db,
    )
    context = _compile(db, target, "tester")
    assert context.verification
    assert any("eval:" in ref for ref in context.source_refs)
    assert any(observation.observation_id in ref for item in context.verification for ref in item.get("evidence_refs", []))


def test_a_b_a_source_cycle_keeps_append_only_history(tmp_path: Path):
    from herdr.context_compiler import list_working_contexts

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-cycle", status="working"))
    first = _compile(db, target, "developer")
    state_db.save_task(dict(target, status="rework", blocker="middle state"), db_path=db)
    second = _compile(db, dict(target, status="rework", blocker="middle state"), "developer")
    state_db.save_task(target, db_path=db)
    third = _compile(db, target, "developer")
    assert len({first.context_id, second.context_id, third.context_id}) == 3
    assert [item.context_id for item in list_working_contexts(target["task_id"], db_path=db)] == [
        first.context_id, second.context_id, third.context_id,
    ]


def test_diff_reports_added_removed_superseded_and_changed(tmp_path: Path):
    from herdr.context_compiler import ContextItem, WorkingContext, diff_working_context

    def item(kind: str, ref: str, value: str, **extra):
        return ContextItem(kind=kind, value=value, source_ref=ref, **extra).to_mapping()

    def context(items):
        return WorkingContext(
            context_id="wc-old",
            run_scope="scope",
            run_id="run",
            workflow_id="wf",
            task_id="task",
            node_id="review",
            agent_role="reviewer",
            goal="goal",
            current_state={},
            findings=items,
            artifacts=[],
            evidence=[],
            completed=[],
            decisions=[],
            blockers=[],
            open_questions=[],
            verification=[],
            handoffs=[],
            next_action="review",
            source_refs=[],
            context_fingerprint="old",
            source_version="old",
            metrics={},
            compiled_at=1.0,
        )

    old = context([
        item("finding", "finding:fnd-old", "old"),
        item("finding", "finding:fnd-same", "before"),
        item("artifact", "artifact:old", "old artifact"),
    ])
    new = context([
        item("finding", "finding:fnd-new", "new", metadata={"supersedes": "fnd-old"}),
        item("finding", "finding:fnd-same", "after"),
    ])
    # The same source ref with a changed value is a changed item.
    diff = diff_working_context(old, new)

    assert any(row["source_ref"] == "artifact:old" for row in diff["removed"])
    assert any(row["source_ref"] == "finding:fnd-new" for row in diff["added"])
    assert any(row["source_ref"] == "finding:fnd-old" for row in diff["superseded"])
    assert any(row["source_ref"] == "finding:fnd-same" for row in diff["changed"])

    old_items = [item("artifact", "artifact:a", "a"), item("artifact", "artifact:b", "b")]
    new_items = [item("artifact", "artifact:a", "a"), item("artifact", "artifact:c", "c"), item("artifact", "artifact:b", "b")]
    insertion_diff = diff_working_context(context(old_items), context(new_items))
    assert any(row["source_ref"] == "artifact:c" for row in insertion_diff["added"])

    old_state = old.to_mapping()
    new_state = new.to_mapping()
    new_state["goal"] = "updated goal"
    new_state["current_state"] = {"task_status": "rework"}
    state_diff = diff_working_context(old_state, new_state)
    assert any(row["source_ref"] == "context:goal" for row in state_diff["changed"])
    assert any(row["source_ref"] == "context:current_state" for row in state_diff["changed"])


def test_handoff_event_can_load_target_working_context(tmp_path: Path):
    from herdr.context_compiler import get_working_context

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-handoff-target", node="review", role="reviewer"))
    context = _compile(db, target, "reviewer")
    event = state_db.create_collaboration_event(
        {
            "run_id": "wf-exec-1",
            "workflow_id": "wf-context",
            "from_task_id": "task-upstream",
            "to_task_id": target["task_id"],
            "to_agent": "reviewer",
            "type": "HANDOFF",
            "summary": "Implementation is ready for review",
            "context_refs": [context.context_id],
            "source_fact_id": "fact-handoff",
        },
        db_path=db,
    )

    loaded = get_working_context(event["context_refs"][0], db_path=db)
    assert loaded is not None
    assert loaded.task_id == target["task_id"]
    assert loaded.run_scope == "wf-exec-1"
    assert "Implementation is ready" in event["summary"]
    assert "goal" not in event


def test_unknown_role_fails_closed(tmp_path: Path):
    from herdr.context_compiler import compile_working_context

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-role"))

    with pytest.raises(ValueError, match="agent_role"):
        compile_working_context(
            workflow_id=target["workflow_id"],
            task_id=target["task_id"],
            agent_role="unknown",
            store=ObservationStore(db),
        )


def test_launch_boundary_compiles_context_reference(tmp_path: Path, monkeypatch):
    import importlib.machinery
    import importlib.util

    from herdr.context_compiler import get_working_context

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-launch", node="implementation", role="developer"))
    monkeypatch.setenv("HERDR_STATE_DB", str(db))
    module_path = Path(__file__).resolve().parent.parent / "bin" / "herdr-task"
    spec = importlib.util.spec_from_loader(
        "context_compiler_task_cli_test",
        importlib.machinery.SourceFileLoader("context_compiler_task_cli_test", str(module_path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ref = module._compile_working_context_ref(target)
    assert ref and get_working_context(ref, db_path=db).task_id == target["task_id"]


def test_handoff_prompt_preserves_context_ref_at_size_limit():
    from herdr.collaboration import build_handoff_prompt, create_handoff

    event = create_handoff(
        run_id="wf-exec-1", workflow_id="wf-context",
        from_task_id="task-upstream", to_task_id="task-target",
        summary="s" * 500,
        artifact_refs=["a" * 200 for _ in range(10)],
        evidence_refs=["e" * 200 for _ in range(10)],
        context_refs=["wc_important"],
        source_fact_id="fact-size",
    )
    prompt = build_handoff_prompt(event, next_action="n" * 500)
    assert len(prompt) <= 2000
    assert "WORKING_CONTEXT_REF: wc_important" in prompt
    assert "HANDOFF_ID:" in prompt


def test_handoff_prompt_contains_only_bounded_context_reference(tmp_path: Path):
    from herdr.collaboration import build_handoff_prompt, create_handoff

    event = create_handoff(
        run_id="wf-exec-1",
        workflow_id="wf-context",
        from_task_id="task-upstream",
        to_task_id="task-target",
        to_agent="reviewer",
        context_refs=["wc_123"],
        source_fact_id="fact-context",
    )
    prompt = build_handoff_prompt(event, next_action="Review the implementation.")
    assert "WORKING_CONTEXT_REF: wc_123" in prompt
    assert "context_id" in prompt
    assert "goal" not in prompt.lower()


def test_supervisor_retry_prompt_carries_working_context_ref(monkeypatch):
    import importlib.machinery
    import importlib.util

    controller_path = Path(__file__).resolve().parent.parent / "services" / "herdr-controller.py"
    spec = importlib.util.spec_from_loader(
        "context_compiler_retry_test",
        importlib.machinery.SourceFileLoader("context_compiler_retry_test", str(controller_path)),
    )
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    events = []
    prompts = []

    class Store:
        def record_event(self, *args, **kwargs):
            events.append((args, kwargs))

    def fake_run(command, *args, **kwargs):
        prompts.append(command)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(controller, "_latest_action_dispatch_intent", lambda *args, **kwargs: None)
    monkeypatch.setattr(controller, "_working_context_ref_for_task", lambda *args, **kwargs: "wc_retry")
    monkeypatch.setattr(controller.subprocess, "run", fake_run)
    result = controller._dispatch_supervisor_retry(
        {"task_id": "task-retry", "workflow_id": "wf", "run_id": "run", "pane_id": "pane", "node": "implementation"},
        {"intervention": {"intervention_id": "int", "decision_id": "dec"}},
        Store(),
    )
    assert result["retry_dispatched"] is True
    assert events
    assert any("WORKING_CONTEXT_REF:wc_retry" in str(item) for item in prompts)


def test_dispatch_rejects_unknown_nonempty_context_ref(tmp_path: Path):
    import importlib.machinery
    import importlib.util

    controller_path = Path(__file__).resolve().parent.parent / "services" / "herdr-controller.py"
    spec = importlib.util.spec_from_loader(
        "context_compiler_invalid_ref_test",
        importlib.machinery.SourceFileLoader("context_compiler_invalid_ref_test", str(controller_path)),
    )
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-invalid-ref", node="review", role="reviewer"))
    target["pane_id"] = "pane-invalid-ref"
    state_db.save_task(target, db_path=db)
    event = state_db.create_collaboration_event(
        {
            "run_id": "wf-exec-1", "workflow_id": "wf-context",
            "from_task_id": "task-upstream", "to_task_id": target["task_id"],
            "context_refs": ["not-a-working-context"],
            "source_fact_id": "fact-invalid-ref",
        },
        db_path=db,
    )
    calls = []
    result = controller.dispatch_collaboration_event(
        event["event_id"], {target["task_id"]: target},
        lambda pane, prompt: calls.append((pane, prompt)), db_path=db,
    )
    assert result["status"] == "failed"
    assert calls == []


def test_dispatch_rejects_context_role_workflow_mismatch(tmp_path: Path):
    import importlib.machinery
    import importlib.util

    controller_path = Path(__file__).resolve().parent.parent / "services" / "herdr-controller.py"
    spec = importlib.util.spec_from_loader(
        "context_compiler_ref_identity_test",
        importlib.machinery.SourceFileLoader("context_compiler_ref_identity_test", str(controller_path)),
    )
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-ref-role", node="review", role="reviewer"))
    target["pane_id"] = "pane-ref-role"
    state_db.save_task(target, db_path=db)
    context = _compile(db, target, "developer")
    event = state_db.create_collaboration_event(
        {
            "run_id": "wf-exec-1", "workflow_id": "wf-context",
            "from_task_id": "task-upstream", "to_task_id": target["task_id"],
            "context_refs": [context.context_id], "source_fact_id": "fact-ref-role",
        },
        db_path=db,
    )
    calls = []
    result = controller.dispatch_collaboration_event(
        event["event_id"], {target["task_id"]: target},
        lambda pane, prompt: calls.append((pane, prompt)), db_path=db,
    )
    assert result["status"] == "failed"
    assert calls == []


def test_dispatch_rejects_context_ref_for_wrong_target(tmp_path: Path):
    import importlib.machinery
    import importlib.util

    controller_path = Path(__file__).resolve().parent.parent / "services" / "herdr-controller.py"
    spec = importlib.util.spec_from_loader(
        "context_compiler_controller_test",
        importlib.machinery.SourceFileLoader("context_compiler_controller_test", str(controller_path)),
    )
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-dispatch-target", node="review", role="reviewer"))
    target["pane_id"] = "pane-target"
    state_db.save_task(target, db_path=db)
    other = _seed_task(db, _task("task-dispatch-other", node="implementation", role="developer"))
    other_context = _compile(db, other, "developer")
    event = state_db.create_collaboration_event(
        {
            "run_id": "wf-exec-1",
            "workflow_id": "wf-context",
            "from_task_id": other["task_id"],
            "to_task_id": target["task_id"],
            "to_agent": "reviewer",
            "context_refs": [other_context.context_id],
            "source_fact_id": "fact-wrong-context",
        },
        db_path=db,
    )
    calls = []
    result = controller.dispatch_collaboration_event(
        event["event_id"],
        {target["task_id"]: target},
        lambda pane, prompt: calls.append((pane, prompt)),
        db_path=db,
    )
    assert result["status"] == "failed"
    assert calls == []
