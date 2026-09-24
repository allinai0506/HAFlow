from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from herdr import state_db
from herdr.observation import ObservationStore, create_observation
from herdr.trajectory import TrajectoryLedger


def _bind_storage_fingerprint(payload):
    from herdr.context_models import WorkingContext, _hash, _payload_digest
    from herdr.context_projection import _config, _fingerprint_payload
    config = _config(None)
    payload["_fingerprint_config"] = config
    payload["context_fingerprint"] = _hash(
        _fingerprint_payload(WorkingContext.from_mapping(payload), config)
    )
    payload.setdefault("metrics", {})["payload_digest"] = _payload_digest(payload)
    return payload


def _save_working_context_in_process(db_path, payload, gate=None, ready=None):
    if ready is not None:
        ready.set()
    if gate is not None:
        gate.wait(10)
    state_db.save_working_context(payload, db_path=Path(db_path))


def _source_clock_revision(db: Path, run_scope: str, workflow_id: str) -> int:
    row = state_db.get_db_connection(db).execute(
        """
        SELECT revision
        FROM working_context_source_clock
        WHERE run_scope = ? AND workflow_id = ?
        """,
        (run_scope, workflow_id),
    ).fetchone()
    return int(row["revision"] or 0) if row is not None else 0


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


def test_legacy_scope_accepts_taskless_source_from_target_run(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _task("task-legacy-taskless", node="review")
    target.pop("workflow_run_id", None)
    target = _seed_task(db, target)
    observation = create_observation(
        run_id=target["run_id"], task_id=None, workflow_id=target["workflow_id"],
        source_type="verification", source_ref="verification:legacy-taskless",
        content="bounded", store=ObservationStore(db),
    )
    context = _compile(db, target, "reviewer")
    assert observation.observation_id in json.dumps(context.to_mapping(), ensure_ascii=False)


def test_explicit_scope_accepts_taskless_source_from_allowed_run(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    _seed_task(db, _task("task-scope-target", scope="scope-a"))
    _seed_task(db, _task("task-scope-sibling", scope="scope-a", run_id="run-sibling"))
    observation = create_observation(
        run_id="run-sibling", task_id=None, workflow_id="wf-context",
        source_type="verification", source_ref="verification:taskless",
        content="bounded", store=ObservationStore(db),
    )
    context = _compile(db, _task("task-scope-target", scope="scope-a"), "reviewer")
    assert observation.observation_id in json.dumps(context.to_mapping(), ensure_ascii=False)


def test_storage_rejects_scope_foreign_finding_even_when_row_exists(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-storage-scope-target", scope="scope-a"))
    foreign = _seed_task(db, _task("task-storage-scope-foreign", scope="scope-b"))
    state_db.upsert_trajectory_finding(
        _finding(foreign["run_id"], "fnd-storage-foreign", task_id=foreign["task_id"]),
        db_path=db,
    )
    context = _compile(db, target, "reviewer")
    forged = dict(context.to_mapping())
    forged["context_id"] = "wc_storage_scope_forged"
    forged["context_fingerprint"] = "f" * 64
    forged["findings"] = [{
        "kind": "finding", "value": "foreign",
        "source_ref": "finding:fnd-storage-foreign",
    }]
    forged["source_refs"] = ["finding:fnd-storage-foreign"]
    _bind_storage_fingerprint(forged)
    with pytest.raises(ValueError, match="crosses run scope|source_task|source_run"):
        state_db.save_working_context(forged, db_path=db)


def test_storage_rejects_taskless_source_without_workflow_identity(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-taskless-workflow-identity"))
    observation = create_observation(
        run_id=target["run_id"], task_id=None, workflow_id=None,
        source_type="verification", source_ref="verification:missing-workflow",
        content="ambiguous", store=ObservationStore(db),
    )
    context = _compile(db, target, "developer")
    forged = dict(context.to_mapping())
    forged["context_id"] = "wc_taskless_missing_workflow"
    forged["evidence"] = [{
        "kind": "evidence", "value": "ambiguous",
        "source_ref": f"observation:{observation.observation_id}",
        "source_run": target["run_id"],
    }]
    forged["source_refs"] = [f"observation:{observation.observation_id}"]
    _bind_storage_fingerprint(forged)
    with pytest.raises(ValueError, match="workflow|run scope"):
        state_db.save_working_context(forged, db_path=db)


def test_storage_rejects_unlinked_legacy_sibling_finding(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _task("task-legacy-storage-target")
    target.pop("workflow_run_id", None)
    sibling = _task("task-legacy-storage-sibling")
    sibling.pop("workflow_run_id", None)
    target = _seed_task(db, target)
    sibling = _seed_task(db, sibling)
    state_db.upsert_trajectory_finding(
        _finding(sibling["run_id"], "fnd-unlinked-storage", task_id=sibling["task_id"]),
        db_path=db,
    )
    context = _compile(db, target, "developer")
    forged = dict(context.to_mapping())
    forged["context_id"] = "wc_unlinked_legacy_storage"
    forged["findings"] = [{
        "kind": "finding", "value": "unlinked",
        "source_ref": "finding:fnd-unlinked-storage",
        "source_task": sibling["task_id"], "source_run": sibling["run_id"],
    }]
    forged["source_refs"] = ["finding:fnd-unlinked-storage"]
    _bind_storage_fingerprint(forged)
    with pytest.raises(ValueError, match="crosses run scope"):
        state_db.save_working_context(forged, db_path=db)


def test_storage_rejects_unrelated_legacy_collaboration_scope_expansion(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _task("task-legacy-unrelated-target")
    target.pop("workflow_run_id", None)
    source = _task("task-legacy-unrelated-source")
    source.pop("workflow_run_id", None)
    other = _task("task-legacy-unrelated-other")
    other.pop("workflow_run_id", None)
    target = _seed_task(db, target)
    source = _seed_task(db, source)
    other = _seed_task(db, other)
    state_db.upsert_trajectory_finding(
        _finding(source["run_id"], "fnd-unrelated-legacy", task_id=source["task_id"]),
        db_path=db,
    )
    unrelated = state_db.create_collaboration_event(
        {
            "run_id": "wf-context",
            "workflow_id": "wf-context",
            "from_task_id": source["task_id"],
            "to_task_id": other["task_id"],
            "type": "REQUEST",
            "summary": "Unrelated request",
            "source_fact_id": "fact-unrelated-legacy",
        },
        db_path=db,
    )
    context = _compile(db, target, "developer")
    forged = dict(context.to_mapping())
    forged["context_id"] = "wc_unrelated_legacy_scope"
    forged["findings"] = [{
        "kind": "finding",
        "value": "unrelated sibling",
        "source_ref": "finding:fnd-unrelated-legacy",
        "source_task": source["task_id"],
        "source_run": source["run_id"],
    }]
    forged["source_refs"] = [
        "finding:fnd-unrelated-legacy",
        f"collaboration:{unrelated['event_id']}",
    ]
    _bind_storage_fingerprint(forged)
    with pytest.raises(ValueError, match="handoff|crosses run scope|collaboration task scope"):
        state_db.save_working_context(forged, db_path=db)


def test_storage_rejects_non_boolean_verification_value(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-storage-verification-schema"))
    context = _compile(db, target, "tester")
    forged = dict(context.to_mapping())
    forged["context_id"] = "wc_storage_verification_schema"
    forged["verification"] = [{
        "kind": "verification", "value": {"passed": "false"},
        "source_ref": f"task:{target['task_id']}",
        "source_task": target["task_id"], "source_run": target["run_id"],
    }]
    _bind_storage_fingerprint(forged)
    with pytest.raises(ValueError, match="verification|bool"):
        state_db.save_working_context(forged, db_path=db)


def test_low_level_storage_normalizes_empty_fingerprint_config(tmp_path: Path):
    from herdr.context_models import _hash, _payload_digest
    from herdr.context_projection import _config, _fingerprint_payload
    from herdr.context_models import WorkingContext

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-storage-empty-config"))
    payload = dict(_compile(db, target, "developer").to_mapping())
    payload["context_id"] = "wc_storage_empty_config"
    config = _config({})
    payload["artifacts"] = [
        {"kind": "artifact", "value": {"ref": f"empty-config-{index}"},
         "source_ref": f"task:{target['task_id']}"}
        for index in range(50)
    ]
    payload["_fingerprint_config"] = config
    payload["context_fingerprint"] = _hash(
        _fingerprint_payload(WorkingContext.from_mapping(payload), config)
    )
    payload.setdefault("metrics", {})["payload_digest"] = _payload_digest(payload)
    with pytest.raises(ValueError, match="cap|budget"):
        state_db.save_working_context(payload, db_path=db, fingerprint_config={})


def test_storage_rejects_phantom_task_artifact_ref(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-storage-phantom-artifact"))
    payload = dict(_compile(db, target, "developer").to_mapping())
    payload["context_id"] = "wc_storage_phantom_artifact"
    payload["artifacts"] = [{
        "kind": "artifact", "value": {"ref": "phantom"},
        "source_ref": f"task:{target['task_id']}:artifact:0",
    }]
    _bind_storage_fingerprint(payload)
    with pytest.raises(ValueError, match="invalid derived target"):
        state_db.save_working_context(payload, db_path=db)


def test_storage_rejects_conflicting_verification_aliases(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-storage-conflicting-verification"))
    payload = dict(_compile(db, target, "tester").to_mapping())
    payload["context_id"] = "wc_storage_conflicting_verification"
    payload["verification"] = [{
        "kind": "verification", "source_ref": f"task:{target['task_id']}",
        "source_task": target["task_id"], "source_run": target["run_id"],
        "value": {"passed": True, "verification_passed": False},
    }]
    _bind_storage_fingerprint(payload)
    with pytest.raises(ValueError, match="verification aliases"):
        state_db.save_working_context(payload, db_path=db)


def test_storage_rejects_incomplete_aggregate_source_refs(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, dict(
        _task("task-storage-source-ref-closure"),
        artifacts=[{"ref": "artifact-source-ref-closure"}],
    ))
    payload = dict(_compile(db, target, "developer").to_mapping())
    payload["context_id"] = "wc_storage_source_ref_closure"
    item_ref = payload["artifacts"][0]["source_ref"]
    payload["source_refs"] = [ref for ref in payload.get("source_refs", []) if ref != item_ref]
    _bind_storage_fingerprint(payload)
    with pytest.raises(ValueError, match="source_refs"):
        state_db.save_working_context(payload, db_path=db)


def test_storage_rejects_kind_cap_violation(tmp_path: Path):
    from herdr.context_models import WorkingContext, _hash, _payload_digest
    from herdr.context_projection import _config, _fingerprint_payload

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-storage-kind-cap"))
    payload = dict(_compile(db, target, "developer").to_mapping())
    payload["context_id"] = "wc_storage_kind_cap"
    config = _config({"max_items_per_kind": {"artifacts": 1}})
    payload["artifacts"] = [
        {"kind": "artifact", "value": {"ref": f"artifact-{index}"},
         "source_ref": f"task:{target['task_id']}"}
        for index in range(3)
    ]
    payload["_fingerprint_config"] = config
    payload["context_fingerprint"] = _hash(
        _fingerprint_payload(WorkingContext.from_mapping(payload), config)
    )
    payload.setdefault("metrics", {})["payload_digest"] = _payload_digest(payload)
    with pytest.raises(ValueError, match="cap"):
        state_db.save_working_context(payload, db_path=db, fingerprint_config=config)


def test_storage_enforces_fingerprint_config_budget(tmp_path: Path):
    from herdr.context_models import WorkingContext, _hash, _payload_digest
    from herdr.context_projection import _config, _fingerprint_payload

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-storage-budget-schema"))
    context = _compile(db, target, "developer")
    payload = dict(context.to_mapping())
    payload["context_id"] = "wc_storage_budget_schema"
    config = _config({"max_chars": 500})
    payload["_fingerprint_config"] = config
    payload["context_fingerprint"] = _hash(
        _fingerprint_payload(WorkingContext.from_mapping(payload), config)
    )
    payload.setdefault("metrics", {})["payload_digest"] = _payload_digest(payload)
    with pytest.raises(ValueError, match="max_chars|budget|size"):
        state_db.save_working_context(payload, db_path=db, fingerprint_config=config)


def test_storage_rejects_item_source_identity_mismatch(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-item-binding-target"))
    finding = _finding(target["run_id"], "fnd-item-binding", task_id=target["task_id"])
    state_db.upsert_trajectory_finding(finding, db_path=db)
    context = _compile(db, target, "developer")
    forged = dict(context.to_mapping())
    forged["context_id"] = "wc_item_binding_forged"
    forged["findings"] = [dict(forged["findings"][0], source_task="foreign-task", source_run="foreign-run")]
    _bind_storage_fingerprint(forged)
    with pytest.raises(ValueError, match="source_task|source_run"):
        state_db.save_working_context(forged, db_path=db)


def test_storage_rejects_empty_provenance_with_foreign_identity(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db, workflow_id="wf")
    task = _task("task-empty-provenance", scope="scope-a", run_id="run-a")
    task["workflow_id"] = "wf"
    _seed_task(db, task)
    state_db.register_working_context_source(
        run_scope="scope-a", workflow_id="wf", source_version="source", db_path=db,
    )
    with pytest.raises(ValueError, match="goal_source_ref|target task scope|run_id|fingerprint configuration"):
        state_db.save_working_context({
            "context_id": "wc_empty_forged", "run_scope": "scope-a", "run_id": "run-b",
            "workflow_id": "wf", "task_id": task["task_id"], "node_id": "review",
            "agent_role": "developer", "goal": "goal", "current_state": {},
            "findings": [], "artifacts": [], "evidence": [], "completed": [],
            "decisions": [], "blockers": [], "open_questions": [], "verification": [],
            "handoffs": [], "next_action": "continue", "source_refs": [],
            "context_fingerprint": "a" * 64, "source_version": "source",
            "source_watermark": 1, "metrics": {}, "compiled_at": 1.0,
        }, db_path=db)


def test_legacy_task_without_run_id_uses_stable_fallback_for_storage(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _task("task-legacy-missing-run")
    target.pop("run_id", None)
    target.pop("workflow_run_id", None)
    _seed_task(db, target)
    context = _compile(db, target, "developer")
    from herdr.context_compiler import get_working_context
    assert context.run_id == "run_task-legacy-missing-run"
    assert get_working_context(context.context_id, db_path=db) is not None


def test_storage_rejects_payload_changed_without_digest_refresh(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-payload-digest"))
    context = _compile(db, target, "developer")
    forged = dict(context.to_mapping())
    forged["context_id"] = "wc_payload_digest_forged"
    forged["goal"] = forged["goal"] + " tampered"
    from herdr.context_projection import _config
    forged["_fingerprint_config"] = _config(None)
    with pytest.raises(ValueError, match="fingerprint"):
        state_db.save_working_context(forged, db_path=db)


def test_storage_rejects_nonexistent_typed_source_ref(tmp_path: Path):
    db = tmp_path / "state.db"
    payload = {
        "context_id": "wc_forged", "run_scope": "scope", "run_id": "run",
        "workflow_id": "wf", "task_id": "task", "node_id": "review",
        "agent_role": "reviewer", "goal": "goal", "current_state": {},
        "findings": [{"kind": "finding", "value": "x", "source_ref": "finding:missing"}],
        "artifacts": [], "evidence": [], "completed": [], "decisions": [], "blockers": [],
        "open_questions": [], "verification": [], "handoffs": [], "next_action": "review",
        "goal_source_ref": "task:task",
        "source_refs": [], "context_fingerprint": "f" * 64, "source_version": "v",
        "metrics": {"source_clock": 0}, "compiled_at": 1.0,
    }
    _bind_storage_fingerprint(payload)
    with pytest.raises(ValueError, match="does not exist|source_refs"):
        state_db.save_working_context(payload, db_path=db)


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


def test_tight_budget_prefers_target_failure_when_both_fail(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = dict(
        _task("task-verification-target-failure", node="test", role="tester"),
        acceptance_criteria=[f"criterion-{index}" for index in range(20)],
    )
    sibling = _seed_task(db, _task("task-verification-sibling-failure", node="review"))
    _seed_task(db, target)
    ledger = TrajectoryLedger(db)
    for task in (target, sibling):
        ledger.append_event({
            "run_id": task["run_id"], "task_id": task["task_id"],
            "workflow_id": task["workflow_id"], "event_type": "verification_completed",
            "verification": {"passed": False},
        })
    context = _compile(db, target, "tester", config={"max_chars": 2000})
    assert context.verification
    assert context.verification[0].get("source_task") == target["task_id"]


def test_tight_budget_keeps_failed_sibling_verification(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = dict(
        _task("task-verification-budget-target", node="test", role="tester"),
        acceptance_criteria=[f"criterion-{index}" for index in range(20)],
    )
    sibling = _seed_task(db, _task("task-verification-budget-sibling", node="review"))
    _seed_task(db, target)
    for index in range(100):
        state_db.upsert_trajectory_finding(
            _finding(sibling["run_id"], f"fnd-verification-budget-{index}", task_id=sibling["task_id"]),
            db_path=db,
        )
    ledger = TrajectoryLedger(db)
    ledger.append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "verification_completed",
        "verification": {"passed": True},
    })
    ledger.append_event({
        "run_id": sibling["run_id"], "task_id": sibling["task_id"],
        "workflow_id": sibling["workflow_id"], "event_type": "verification_completed",
        "verification": {"passed": False},
    })
    context = _compile(db, target, "tester", config={"max_chars": 2000})
    assert any(item.get("value", {}).get("passed") is False for item in context.verification)
    assert context.next_action == "Resolve failed verification."


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
        runtime={"status": "running"},
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
    assert context.current_state.get("runtime_status") == "running"
    assert context.blockers
    assert any(item.get("value", {}).get("passed") is False for item in context.verification)


def test_large_candidate_budget_keeps_required_state_and_failure_evidence(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    upstream = _seed_task(db, _task("task-budget-upstream", node="implementation"))
    target = dict(
        _task("task-budget-state", node="test", role="tester", status="blocked"),
        acceptance_criteria=[f"criterion-{index}" for index in range(20)],
        blockers=[f"blocker-{index}" for index in range(5)],
    )
    _seed_task(db, target)
    for index in range(120):
        state_db.upsert_trajectory_finding(
            _finding(upstream["run_id"], f"fnd-state-budget-{index}", task_id=upstream["task_id"]),
            db_path=db,
        )
    TrajectoryLedger(db).append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "verification_completed",
        "verification": {"passed": False},
    })
    context = _compile(db, target, "tester", config={"max_chars": 2000})
    assert context.goal
    assert context.current_state.get("acceptance_criteria")
    assert context.blockers
    assert any(item.get("value", {}).get("passed") is False for item in context.verification)
    assert context.metrics["context_chars"] <= 2000


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


def test_global_item_budget_keeps_blocker_and_failure_verification(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, dict(_task("task-item-required", node="test", role="tester", status="blocked"), blocker="required blocker"))
    TrajectoryLedger(db).append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "verification_completed",
        "verification": {"passed": False},
    })
    context = _compile(db, target, "tester", config={"max_items": 1})
    assert context.blockers
    assert any(item.get("value", {}).get("passed") is False for item in context.verification)


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


def test_required_verification_cap_cannot_be_configured_to_zero(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-zero-verification-cap"))
    with pytest.raises(ValueError, match="verification"):
        _compile(db, target, "tester", config={"max_items_per_kind": {"verification": 0}})


def test_impossible_context_budget_fails_closed_instead_of_dropping_goal(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-impossible-budget"))
    with pytest.raises(ValueError, match="max_chars"):
        _compile(db, target, "developer", config={"max_chars": 500})


def test_source_clock_rejects_toctou_candidate_after_source_write(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    upstream = _seed_task(db, _task("task-clock-race-upstream"))
    target = _seed_task(db, _task("task-clock-race-target"))
    state_db.upsert_trajectory_finding(
        _finding(upstream["run_id"], "fnd-clock-race", task_id=upstream["task_id"], summary="old"),
        db_path=db,
    )
    old = _compile(db, target, "reviewer")
    state_db.upsert_trajectory_finding(
        _finding(upstream["run_id"], "fnd-clock-race", task_id=upstream["task_id"], summary="new"),
        db_path=db,
    )
    late_payload = dict(old.to_mapping())
    late_payload["context_id"] = "wc_clock_race_late"
    from herdr.context_projection import _config
    late_payload["_fingerprint_config"] = _config(None)
    late = state_db.save_working_context(late_payload, db_path=db)
    assert late.get("_stale_snapshot") is True


def test_existing_context_id_retry_is_idempotent_after_source_clock_change(tmp_path: Path):
    from herdr.context_projection import _config

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-idempotent-clock"))
    context = _compile(db, target, "developer")
    state_db.save_task(dict(target, goal="changed after compile"), db_path=db)
    retried = state_db.save_working_context(
        context.to_mapping(), db_path=db, fingerprint_config=_config(None),
    )
    assert retried["context_id"] == context.context_id
    assert retried.get("_stale_snapshot") is not True


def test_source_clock_ignores_writes_from_another_workflow(tmp_path: Path):
    from herdr.context_projection import _config

    db = tmp_path / "state.db"
    _seed_workflow(db, workflow_id="wf-a", scope="scope-a")
    _seed_workflow(db, workflow_id="wf-b", scope="scope-b")
    target_a = _seed_task(db, _task("task-clock-a", workflow_id="wf-a", scope="scope-a"))
    target_b = _seed_task(db, _task("task-clock-b", workflow_id="wf-b", scope="scope-b"))
    context = _compile(db, target_a, "developer")
    a_clock = _source_clock_revision(db, "scope-a", "wf-a")
    state_db.save_task(
        dict(target_b, goal=f"{target_b['goal']} updated"),
        db_path=db,
    )
    TrajectoryLedger(db).append_event({
        "run_id": target_b["run_id"], "task_id": target_b["task_id"],
        "workflow_id": target_b["workflow_id"], "event_type": "task_started",
    })
    state_db.upsert_trajectory_finding(
        _finding(target_b["run_id"], "fnd-other-workflow", task_id=target_b["task_id"]),
        db_path=db,
    )
    assert _source_clock_revision(db, "scope-a", "wf-a") == a_clock
    assert _source_clock_revision(db, "scope-b", "wf-b") > 0
    candidate = dict(context.to_mapping())
    candidate["context_id"] = "wc_scope_clock_cross_workflow"
    saved = state_db.save_working_context(
        candidate, db_path=db, fingerprint_config=_config(None),
    )
    assert saved.get("_stale_snapshot") is not True
    assert state_db.get_working_context(saved["context_id"], db_path=db) is not None


def test_source_clock_still_detects_writes_in_same_workflow_scope(tmp_path: Path):
    from herdr.context_projection import _config

    db = tmp_path / "state.db"
    _seed_workflow(db, workflow_id="wf-a", scope="scope-a")
    target = _seed_task(db, _task("task-clock-same-scope", workflow_id="wf-a", scope="scope-a"))
    context = _compile(db, target, "developer")
    state_db.save_task(
        dict(target, goal=f"{target['goal']} updated"),
        db_path=db,
    )
    candidate = dict(context.to_mapping())
    candidate["context_id"] = "wc_scope_clock_same_workflow"
    saved = state_db.save_working_context(
        candidate, db_path=db, fingerprint_config=_config(None),
    )
    assert saved.get("_stale_snapshot") is True


def test_task_scope_update_advances_old_and_new_source_clocks(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db, workflow_id="wf-a", scope="scope-a")
    target = _seed_task(db, _task("task-scope-move", workflow_id="wf-a", scope="scope-a"))
    _compile(db, target, "developer")
    old_clock = _source_clock_revision(db, "scope-a", "wf-a")
    sibling = _seed_task(db, _task("task-scope-sibling", workflow_id="wf-a", scope="scope-b"))
    sibling_clock = _source_clock_revision(db, "scope-b", "wf-a")
    state_db.save_task(dict(target, goal="target update"), db_path=db)
    assert _source_clock_revision(db, "scope-a", "wf-a") > old_clock
    assert _source_clock_revision(db, "scope-b", "wf-a") == sibling_clock
    state_db.save_task(
        dict(target, workflow_run_id="scope-b", goal="scope move"),
        db_path=db,
    )
    assert _source_clock_revision(db, "scope-a", "wf-a") > old_clock
    assert _source_clock_revision(db, "scope-b", "wf-a") > sibling_clock


def test_task_workflow_move_advances_old_workflow_clock(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db, workflow_id="wf-a", scope="shared-exec")
    _seed_workflow(db, workflow_id="wf-b", scope="shared-exec")
    target = _seed_task(db, _task("task-workflow-move", workflow_id="wf-a", scope="shared-exec"))
    _compile(db, target, "developer")
    old_clock = _source_clock_revision(db, "shared-exec", "wf-a")
    state_db.save_task(dict(target, workflow_id="wf-b"), db_path=db)
    assert _source_clock_revision(db, "shared-exec", "wf-a") > old_clock
    assert _source_clock_revision(db, "shared-exec", "wf-b") > 0


def test_taskless_source_with_reused_run_advances_all_workflow_scopes(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db, workflow_id="wf-a", scope="scope-a")
    _seed_workflow(db, workflow_id="wf-b", scope="scope-b")
    _seed_task(db, _task("task-reused-run-a", workflow_id="wf-a", scope="scope-a", run_id="reused-run"))
    target_b = _seed_task(db, _task("task-reused-run-b", workflow_id="wf-b", scope="scope-b", run_id="reused-run"))
    _compile(db, target_b, "developer")
    before = _source_clock_revision(db, "scope-b", "wf-b")
    state_db.record_trajectory_event(
        {
            "run_id": "reused-run", "task_id": None, "workflow_id": "wf-b",
            "event_type": "verification_completed", "payload": {"verification": {"passed": False}},
        },
        db_path=db,
    )
    assert _source_clock_revision(db, "scope-b", "wf-b") > before
    assert _source_clock_revision(db, "scope-a", "wf-a") > 0


def test_same_execution_scope_source_heads_are_isolated_by_workflow(tmp_path: Path):
    from herdr.context_projection import _config

    db = tmp_path / "state.db"
    _seed_workflow(db, workflow_id="wf-a", scope="same-exec")
    _seed_workflow(db, workflow_id="wf-b", scope="same-exec")
    target_a = _seed_task(db, _task("task-head-a", workflow_id="wf-a", scope="same-exec"))
    target_b = _seed_task(db, _task("task-head-b", workflow_id="wf-b", scope="same-exec"))
    context_a = _compile(db, target_a, "developer")
    _compile(db, target_b, "developer")
    candidate = dict(context_a.to_mapping())
    candidate["context_id"] = "wc_same_exec_other_workflow"
    saved = state_db.save_working_context(
        candidate, db_path=db, fingerprint_config=_config(None),
    )
    assert saved.get("_stale_snapshot") is not True


def test_workflow_update_makes_same_execution_scope_candidate_stale(tmp_path: Path):
    from herdr.context_projection import _config

    db = tmp_path / "state.db"
    _seed_workflow(db, workflow_id="wf-a", scope="scope-a")
    target = _seed_task(db, _task("task-workflow-clock", workflow_id="wf-a", scope="scope-a"))
    context = _compile(db, target, "developer")
    workflow = state_db.get_workflow("wf-a", db_path=db)
    workflow["status"] = "paused"
    workflow["current_stage"] = "test"
    state_db.save_workflow(workflow, db_path=db)
    candidate = dict(context.to_mapping())
    candidate["context_id"] = "wc_workflow_scope_stale"
    saved = state_db.save_working_context(
        candidate, db_path=db, fingerprint_config=_config(None),
    )
    assert saved.get("_stale_snapshot") is True


def test_source_backed_context_cannot_use_null_clock(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-clock-null"))
    context = _compile(db, target, "developer")
    forged = dict(context.to_mapping())
    forged["context_id"] = "wc_clock_null_forged"
    forged["metrics"] = dict(forged.get("metrics") or {})
    forged["metrics"]["source_clock"] = None
    from herdr.context_projection import _config
    forged["_fingerprint_config"] = _config(None)
    with pytest.raises(ValueError, match="source_clock"):
        state_db.save_working_context(forged, db_path=db)


def test_source_backed_context_cannot_omit_clock(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-clock-required"))
    context = _compile(db, target, "developer")
    forged = dict(context.to_mapping())
    forged["context_id"] = "wc_clock_required_forged"
    forged["metrics"] = dict(forged.get("metrics") or {})
    forged["metrics"].pop("source_clock", None)
    from herdr.context_projection import _config
    forged["_fingerprint_config"] = _config(None)
    with pytest.raises(ValueError, match="source_clock"):
        state_db.save_working_context(forged, db_path=db)


def test_late_old_source_candidate_is_not_latest_after_revision_change(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    upstream = _seed_task(db, _task("task-late-source-upstream"))
    target = _seed_task(db, _task("task-late-source-target"))
    state_db.upsert_trajectory_finding(
        _finding(upstream["run_id"], "fnd-late-source", task_id=upstream["task_id"], summary="old"),
        db_path=db,
    )
    old = _compile(db, target, "reviewer")
    state_db.upsert_trajectory_finding(
        _finding(upstream["run_id"], "fnd-late-source", task_id=upstream["task_id"], summary="new"),
        db_path=db,
    )
    new = _compile(db, target, "reviewer")
    late_payload = dict(old.to_mapping())
    late_payload["context_id"] = "wc_late_old_candidate"
    from herdr.context_projection import _config
    late_payload["_fingerprint_config"] = _config(None)
    late = state_db.save_working_context(late_payload, db_path=db)
    assert late.get("_stale_snapshot") is True
    assert state_db.get_latest_working_context(target["task_id"], db_path=db)["context_id"] == new.context_id


def test_source_revision_covers_workflow_nodes_beyond_projection_cap(tmp_path: Path):
    db = tmp_path / "state.db"
    nodes = [{"id": f"node-{index}", "depends_on": []} for index in range(101)]
    nodes[-1]["id"] = "review"
    nodes[-1]["purpose"] = "first purpose"
    state_db.save_workflow(
        {"workflow_id": "wf-node-cap", "title": "fixture", "status": "running", "config": {"nodes": nodes}},
        db_path=db,
    )
    target = _seed_task(db, _task("task-node-cap", workflow_id="wf-node-cap", node="review"))
    first = _compile(db, target, "reviewer")
    nodes[-1]["purpose"] = "second purpose"
    state_db.save_workflow(
        {"workflow_id": "wf-node-cap", "title": "fixture", "status": "running", "config": {"nodes": nodes}},
        db_path=db,
    )
    second = _compile(db, target, "reviewer")
    assert second.source_version != first.source_version
    assert second.source_watermark > first.source_watermark


def test_source_revision_covers_legacy_stages_beyond_projection_cap(tmp_path: Path):
    db = tmp_path / "state.db"
    stages = [{"id": f"stage-{index}", "depends_on": []} for index in range(101)]
    stages[-1]["id"] = "review"
    stages[-1]["purpose"] = "first purpose"
    state_db.save_workflow(
        {"workflow_id": "wf-stage-cap", "title": "fixture", "status": "running", "config": {"stages": stages}},
        db_path=db,
    )
    target = _seed_task(db, _task("task-stage-cap", workflow_id="wf-stage-cap", node="review"))
    first = _compile(db, target, "reviewer")
    stages[-1]["purpose"] = "second purpose"
    state_db.save_workflow(
        {"workflow_id": "wf-stage-cap", "title": "fixture", "status": "running", "config": {"stages": stages}},
        db_path=db,
    )
    second = _compile(db, target, "reviewer")
    assert second.source_version != first.source_version
    assert second.source_watermark > first.source_watermark


def test_source_revision_covers_mixed_nodes_and_stages_projection(tmp_path: Path):
    db = tmp_path / "state.db"
    nodes = [{"id": f"node-{index}"} for index in range(101)]
    stages = [{"id": f"stage-{index}", "depends_on": []} for index in range(101)]
    nodes[-1]["id"] = "review"
    stages[-1]["id"] = "review"
    stages[-1]["rules"] = "first rule"
    config = {"nodes": nodes, "stages": stages}
    state_db.save_workflow(
        {"workflow_id": "wf-mixed-cap", "title": "fixture", "status": "running", "config": config},
        db_path=db,
    )
    target = _seed_task(db, _task("task-mixed-cap", workflow_id="wf-mixed-cap", node="review"))
    first = _compile(db, target, "reviewer")
    stages[-1]["rules"] = "second rule"
    state_db.save_workflow(
        {"workflow_id": "wf-mixed-cap", "title": "fixture", "status": "running", "config": config},
        db_path=db,
    )
    second = _compile(db, target, "reviewer")
    assert second.source_version != first.source_version
    assert second.source_watermark > first.source_watermark


def test_source_revision_advances_for_requirement_update(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = dict(_task("task-requirement-revision"), requirements=["first requirement"])
    target = _seed_task(db, target)
    first = _compile(db, target, "developer")
    state_db.save_task(
        dict(target, requirements=["second requirement"]),
        db_path=db,
    )
    second = _compile(db, target, "developer")
    assert second.source_watermark > first.source_watermark
    assert second.source_version != first.source_version


def test_source_revision_advances_for_artifact_update(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, dict(_task("task-artifact-revision"), artifacts=[{"ref": "a"}]))
    first = _compile(db, target, "reviewer")
    state_db.save_task(dict(target, artifacts=[{"ref": "b"}]), db_path=db)
    second = _compile(db, target, "reviewer")
    assert second.source_watermark > first.source_watermark


def test_source_revision_advances_for_in_place_finding_update(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    upstream = _seed_task(db, _task("task-source-revision-upstream"))
    target = _seed_task(db, _task("task-source-revision-target"))
    original = _finding(upstream["run_id"], "fnd-revision", task_id=upstream["task_id"], summary="old")
    state_db.upsert_trajectory_finding(original, db_path=db)
    first = _compile(db, target, "reviewer")
    updated = dict(original, summary="new")
    state_db.upsert_trajectory_finding(updated, db_path=db)
    second = _compile(db, target, "reviewer")
    assert second.source_watermark > first.source_watermark
    from herdr.context_compiler import get_latest_working_context
    assert get_latest_working_context(target["task_id"], db_path=db).context_id == second.context_id


def test_fingerprint_binds_scalar_provenance_fields(tmp_path: Path):
    from herdr.context_models import WorkingContext, _hash
    from herdr.context_projection import _config, _fingerprint_payload

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-scalar-provenance"))
    context = _compile(db, target, "developer")
    mutated = dict(context.to_mapping())
    mutated["goal_source_ref"] = "workflow:wf-context"
    mutated["source_refs"] = list(dict.fromkeys(
        [*mutated.get("source_refs", []), "workflow:wf-context"]
    ))
    mutated_fingerprint = _hash(
        _fingerprint_payload(WorkingContext.from_mapping(mutated), _config(None))
    )
    assert mutated_fingerprint != context.context_fingerprint


def test_fingerprint_is_independent_of_compile_wall_clock(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    upstream = _seed_task(db, _task("task-clock-upstream", node="implementation"))
    target = _seed_task(db, _task("task-clock-target", node="review"))
    for index in range(3):
        state_db.upsert_trajectory_finding(
            _finding(upstream["run_id"], f"fnd-clock-{index}", task_id=upstream["task_id"], created_at=float(index)),
            db_path=db,
        )
    first = _compile(db, target, "reviewer", now=1000.0)
    second = _compile(db, target, "reviewer", now=200000.0)
    assert first.context_id == second.context_id
    assert first.context_fingerprint == second.context_fingerprint


def test_fingerprint_ignores_updated_at_only_task_resave(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-resave-fingerprint", status="working"))
    first = _compile(db, target, "developer")
    state_db.save_task(dict(target), db_path=db)
    second = _compile(db, target, "developer")
    assert second.context_id == first.context_id
    assert second.context_fingerprint == first.context_fingerprint


def test_completed_task_resave_does_not_change_derived_context(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-resave-completed", status="completed"))
    first = _compile(db, target, "developer")
    state_db.save_task(dict(target), db_path=db)
    second = _compile(db, target, "developer")
    assert second.context_id == first.context_id
    assert second.context_fingerprint == first.context_fingerprint


def test_runtime_timestamp_only_resave_does_not_change_context(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = dict(_task("task-resave-runtime", status="working"), runtime={"status": "running", "updated_at": 1.0})
    target = _seed_task(db, target)
    first = _compile(db, target, "developer")
    changed_runtime = dict(target)
    changed_runtime["runtime"] = {"status": "running", "updated_at": 999.0}
    state_db.save_task(changed_runtime, db_path=db)
    second = _compile(db, changed_runtime, "developer")
    assert second.context_id == first.context_id
    assert second.source_version == first.source_version


def test_workflow_only_requirements_keep_workflow_provenance(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _task("task-workflow-requirements", node="review")
    target.pop("acceptance_criteria")
    _seed_task(db, target)
    context = _compile(db, target, "developer")
    assert context.current_state_refs["requirements"].startswith("workflow:")


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


def test_critical_finding_survives_sibling_finding_noise(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-critical-finding-window", node="review"))
    sibling = _seed_task(db, _task("task-finding-noise", node="implementation"))
    state_db.upsert_trajectory_finding(
        _finding(
            target["run_id"], "fnd-critical-window", task_id=target["task_id"],
            severity="critical", summary="critical blocker",
        ),
        db_path=db,
    )
    for index in range(500):
        state_db.upsert_trajectory_finding(
            _finding(sibling["run_id"], f"fnd-noise-{index}", task_id=sibling["task_id"]),
            db_path=db,
        )
    context = _compile(db, target, "developer")
    assert context.blockers
    assert any("fnd-critical-window" in json.dumps(item) for item in context.blockers)


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

    mismatched_observation = create_observation(
        run_id=target["run_id"], task_id=target["task_id"],
        workflow_id="wf-other", source_type="verification",
        source_ref="verification:other-workflow", content="other evidence",
        store=ObservationStore(db),
    )
    state_db.upsert_trajectory_finding(
        _finding(
            target["run_id"], "fnd-mismatched-evidence", task_id=target["task_id"],
            evidence=[{"observation_id": mismatched_observation.observation_id}],
        ),
        db_path=db,
    )

    context = _compile(db, target, "coordinator")
    serialized = json.dumps(context.to_mapping(), ensure_ascii=False)
    assert "fnd-other" not in serialized
    assert other_obs.observation_id not in serialized
    assert "other completion" not in serialized
    assert mismatched_event["event_id"] not in serialized
    assert "mismatched-workflow-artifact" not in serialized
    assert mismatched_observation.observation_id not in serialized
    assert context.run_scope == "wf-exec-1"
    assert context.run_id == "run-target"


def test_non_handoff_collaboration_does_not_change_source_fingerprint(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _task("task-request-fingerprint-target", node="review")
    target.pop("workflow_run_id", None)
    other = _task("task-request-fingerprint-other", node="implementation")
    other.pop("workflow_run_id", None)
    _seed_task(db, target)
    _seed_task(db, other)
    first = _compile(db, target, "reviewer")
    state_db.create_collaboration_event({
        "run_id": target["workflow_id"], "workflow_id": target["workflow_id"],
        "from_task_id": other["task_id"], "to_task_id": target["task_id"],
        "type": "REQUEST", "source_fact_id": "request-only",
    }, db_path=db)
    second = _compile(db, target, "reviewer")
    assert second.source_version == first.source_version
    assert second.context_id == first.context_id


def test_legacy_planned_link_does_not_authorize_distinct_runs(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _task("task-planned-legacy-target", node="test")
    target.pop("workflow_run_id")
    upstream = _task("task-planned-legacy-upstream", node="implementation")
    upstream.pop("workflow_run_id")
    upstream["artifacts"] = [{"ref": "planned-foreign-artifact"}]
    _seed_task(db, target)
    _seed_task(db, upstream)
    context = _compile(
        db, target, "tester",
        planned_links=[{
            "from_task_id": upstream["task_id"],
            "to_task_id": target["task_id"],
        }],
    )
    assert "planned-foreign-artifact" not in json.dumps(context.to_mapping(), ensure_ascii=False)


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
    state_db.create_collaboration_event(
        {
            "run_id": "wf-context",
            "workflow_id": "wf-context",
            "from_task_id": upstream["task_id"],
            "to_task_id": unrelated["task_id"],
            "type": "REQUEST",
            "source_fact_id": "unrelated-request",
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


def test_out_of_scope_completion_cannot_clear_real_failure(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-failure-scope", status="working"))
    ledger = TrajectoryLedger(db)
    failed = ledger.append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "task_failed",
        "metadata": {"reason": "real failure"},
    })
    ledger.append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": "wf-other", "event_type": "task_completed",
    })
    context = _compile(db, target, "developer")
    assert any(failed["event_id"] in ref for ref in context.source_refs)
    assert context.blockers


def test_completed_task_does_not_project_stale_persisted_blocker(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, dict(_task("task-completed-blocker", status="completed"), blocker="stale"))
    context = _compile(db, target, "developer")
    assert not context.blockers


def test_oversized_failure_survives_oversized_decision_noise(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-oversized-critical-noise", node="implementation"))
    state_db.record_trajectory_event(
        {
            "run_id": target["run_id"], "task_id": target["task_id"],
            "workflow_id": target["workflow_id"], "event_type": "task_failed",
            "payload": {"reason": "x" * 21000},
        },
        db_path=db,
    )
    for index in range(300):
        state_db.record_trajectory_event(
            {
                "run_id": target["run_id"], "task_id": target["task_id"],
                "workflow_id": target["workflow_id"], "event_type": "decision",
                "payload": {"decision": "x" * 21000},
            },
            db_path=db,
        )
    context = _compile(db, target, "developer")
    assert context.blockers
    assert any(
        item.get("value", {}).get("source_truncated") is True
        for item in context.blockers
    )


def test_oversized_failure_survives_same_source_verification_noise(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-oversized-same-class-noise", node="test", role="tester"))
    state_db.record_trajectory_event(
        {
            "run_id": target["run_id"], "task_id": target["task_id"],
            "workflow_id": target["workflow_id"], "event_type": "task_failed",
            "payload": {"reason": "x" * 21000},
        },
        db_path=db,
    )
    for index in range(301):
        state_db.record_trajectory_event(
            {
                "run_id": target["run_id"], "task_id": target["task_id"],
                "workflow_id": target["workflow_id"], "event_type": "verification_completed",
                "payload": {"verification": {"status": "unknown"}, "blob": "x" * 21000},
            },
            db_path=db,
        )
    context = _compile(db, target, "tester")
    assert context.blockers
    assert any(
        item.get("value", {}).get("source_truncated") is True
        for item in context.blockers
    )
    assert context.verification
    assert context.next_action != "Continue."


def test_oversized_decision_payload_is_retained_as_truncated_marker(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-oversized-decision", node="implementation"))
    state_db.record_trajectory_event(
        {
            "run_id": target["run_id"], "task_id": target["task_id"],
            "workflow_id": target["workflow_id"], "event_type": "decision",
            "payload": {"decision": "x" * 21000},
        },
        db_path=db,
    )
    for index in range(300):
        state_db.record_trajectory_event(
            {
                "run_id": target["run_id"], "task_id": target["task_id"],
                "workflow_id": target["workflow_id"], "event_type": "decision",
                "payload": {"decision": f"ordinary-{index}"},
            },
            db_path=db,
        )
    context = _compile(db, target, "developer")
    assert any(
        item.get("value", {}).get("source_truncated") is True
        for item in context.decisions
    )


def test_oversized_failure_payload_is_retained_as_truncated_blocker(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-oversized-failure", node="implementation"))
    state_db.record_trajectory_event(
        {
            "run_id": target["run_id"], "task_id": target["task_id"],
            "workflow_id": target["workflow_id"], "event_type": "task_failed",
            "payload": {
                "metadata": {"reason": "real failure"},
                "oversized": "x" * 21000,
            },
        },
        db_path=db,
    )
    context = _compile(db, target, "developer")
    assert context.blockers
    assert any(
        item.get("value", {}).get("reason") in {"real failure", "source payload truncated"}
        for item in context.blockers
    )


def test_legacy_task_bound_failure_and_verification_survive_missing_workflow(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-legacy-missing-workflow-source", node="test", role="tester"))
    state_db.record_trajectory_event(
        {
            "run_id": target["run_id"], "task_id": target["task_id"],
            "workflow_id": None, "event_type": "task_failed",
            "payload": {"metadata": {"reason": "legacy failure"}},
        },
        db_path=db,
    )
    state_db.record_trajectory_event(
        {
            "run_id": target["run_id"], "task_id": target["task_id"],
            "workflow_id": None, "event_type": "verification_completed",
            "payload": {"verification_passed": False},
        },
        db_path=db,
    )
    for index in range(301):
        state_db.record_trajectory_event(
            {
                "run_id": target["run_id"], "task_id": target["task_id"],
                "workflow_id": None, "event_type": "decision",
                "payload": {"decision": f"legacy-noise-{index}"},
            },
            db_path=db,
        )
    context = _compile(db, target, "tester")
    assert any(item.get("value", {}).get("reason") == "legacy failure" for item in context.blockers)
    assert any(
        item.get("value", {}).get("verification_passed") is False
        for item in context.verification
    )


def test_failure_window_survives_unrelated_event_noise(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-failure-window", node="implementation"))
    ledger = TrajectoryLedger(db)
    ledger.append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "task_failed",
        "metadata": {"reason": "real failure"},
    })
    for index in range(301):
        ledger.append_event({
            "run_id": target["run_id"], "task_id": target["task_id"],
            "workflow_id": target["workflow_id"], "event_type": "decision",
            "decision": f"failure-noise-{index}",
        })
    context = _compile(db, target, "developer")
    assert any(item.get("value", {}).get("reason") == "real failure" for item in context.blockers)


def test_status_only_blocked_task_compiles_synthetic_blocker(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-status-only-blocked", status="blocked"))
    context = _compile(db, target, "developer")
    assert any(item.get("value") == "blocked" for item in context.blockers)


def test_current_task_blocker_survives_sibling_failure_cap(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, dict(
        _task("task-current-blocker-cap", node="implementation"),
        blocker="TARGET BLOCKER",
    ))
    for index in range(6):
        sibling = _seed_task(db, _task(f"task-blocker-noise-{index}", node="implementation"))
        state_db.record_trajectory_event(
            {
                "run_id": sibling["run_id"], "task_id": sibling["task_id"],
                "workflow_id": sibling["workflow_id"], "event_type": "task_failed",
                "payload": {"metadata": {"reason": f"sibling failure {index}"}},
            },
            db_path=db,
        )
    context = _compile(
        db, target, "developer",
        config={"max_items_per_kind": {"blockers": 5}},
    )
    assert any(
        item.get("value") == "TARGET BLOCKER"
        or item.get("value", {}).get("reason") == "TARGET BLOCKER"
        for item in context.blockers
    )


def test_verification_cap_prioritizes_target_failure(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-verification-cap-target", node="test", role="tester"))
    sibling = _seed_task(db, _task("task-verification-cap-sibling", node="implementation"))
    ledger = TrajectoryLedger(db)
    ledger.append_event({
        "run_id": sibling["run_id"], "task_id": sibling["task_id"],
        "workflow_id": sibling["workflow_id"], "event_type": "verification_completed",
        "verification": {"passed": True},
    })
    target_event = ledger.append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "verification_completed",
        "verification": {"passed": False},
    })
    context = _compile(db, target, "tester", config={"max_items_per_kind": {"verification": 1}})
    assert any(target_event["event_id"] in ref for ref in context.source_refs)
    assert context.verification[0].get("value", {}).get("passed") is False


def test_verification_reserved_window_survives_event_noise(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-verification-window", node="test", role="tester"))
    ledger = TrajectoryLedger(db)
    verification = ledger.append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "verification_completed",
        "verification": {"passed": False},
    })
    for index in range(301):
        ledger.append_event({
            "run_id": target["run_id"], "task_id": target["task_id"],
            "workflow_id": target["workflow_id"], "event_type": "decision",
            "decision": f"noise-{index}",
        })
    context = _compile(db, target, "tester")
    assert any(verification["event_id"] in ref for ref in context.source_refs)
    assert any(item.get("value", {}).get("passed") is False for item in context.verification)


def test_source_windows_apply_requested_limits_to_verification_and_eval(tmp_path: Path):
    from herdr.context_sources import _read_source_snapshot
    from herdr.eval_store import record_eval_result

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-source-window-limit", node="test", role="tester"))
    ledger = TrajectoryLedger(db)
    values = (False, True, "unknown", False, True, "unknown")
    for index, value in enumerate(values):
        verification = {"passed": value} if isinstance(value, bool) else {"status": value}
        ledger.append_event({
            "run_id": target["run_id"], "task_id": target["task_id"],
            "workflow_id": target["workflow_id"], "event_type": "verification_completed",
            "verification": verification, "timestamp": float(index),
        })
        record_eval_result(
            target["run_id"], task_id=target["task_id"], workflow_id=target["workflow_id"],
            revision=index + 1,
            verification_passed=value if isinstance(value, bool) else None,
            requirements_satisfied=True if not isinstance(value, bool) else None,
            db_path=db,
        )
    snapshot = _read_source_snapshot(
        workflow_id=target["workflow_id"], task_id=target["task_id"],
        store=ObservationStore(db), db_path=db, max_events=2, max_evals=2,
    )
    assert len(snapshot["events"]) <= 2
    assert len(snapshot["evals"]) <= 2
    empty_window = _read_source_snapshot(
        workflow_id=target["workflow_id"], task_id=target["task_id"],
        store=ObservationStore(db), db_path=db, max_events=0, max_evals=0,
    )
    assert empty_window["events"] == []
    assert empty_window["evals"] == []


def test_single_slot_preserves_strict_failure_without_recovery(tmp_path: Path):
    from herdr.context_sources import _read_source_snapshot
    from herdr.eval_store import record_eval_result

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-single-slot-strict", node="test", role="tester"))
    ledger = TrajectoryLedger(db)
    failure = ledger.append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "verification_completed",
        "verification": {"passed": False},
    })
    ledger.append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "verification_completed",
        "verification": {"status": "unknown"},
    })
    record_eval_result(
        target["run_id"], task_id=target["task_id"], workflow_id=target["workflow_id"],
        revision=1, verification_passed=False, db_path=db,
    )
    record_eval_result(
        target["run_id"], task_id=target["task_id"], workflow_id=target["workflow_id"],
        revision=2, requirements_satisfied=True, db_path=db,
    )
    snapshot = _read_source_snapshot(
        workflow_id=target["workflow_id"], task_id=target["task_id"],
        store=ObservationStore(db), db_path=db, max_events=1, max_evals=1,
    )
    assert any(item.get("event_id") == failure["event_id"] for item in snapshot["events"])
    assert any(item.get("verification_passed") == 0 for item in snapshot["evals"])


def test_tight_budget_compile_preserves_compiled_at_precision(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-compiled-at-precision"))
    first = _compile(db, target, "developer", now=1000.1)
    second = _compile(db, target, "developer", config={"max_chars": 2500}, now=1000.2)
    assert first.compiled_at == 1000.1
    assert second.compiled_at == 1000.2


def test_oversized_eval_evidence_cannot_clear_failure(tmp_path: Path):
    from herdr.eval_store import record_eval_result

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-oversized-eval", node="test", role="tester"))
    record_eval_result(
        target["run_id"], task_id=target["task_id"], workflow_id=target["workflow_id"],
        revision=1, verification_passed=False,
        evidence=[{"blob": "x" * 21000}], db_path=db,
    )
    record_eval_result(
        target["run_id"], task_id=target["task_id"], workflow_id=target["workflow_id"],
        revision=2, verification_passed=True, db_path=db,
    )
    context = _compile(db, target, "tester")
    assert not any(item.get("value", {}).get("verification_passed") is True for item in context.verification)
    assert context.verification
    tight = _compile(db, target, "tester", config={"max_chars": 2500})
    assert tight.next_action != "Continue."


def test_oversized_eval_failure_survives_same_source_unknown_noise(tmp_path: Path):
    from herdr.eval_store import record_eval_result

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-oversized-eval-noise", node="test", role="tester"))
    record_eval_result(
        target["run_id"], task_id=target["task_id"], workflow_id=target["workflow_id"],
        revision=1, verification_passed=False,
        evidence=[{"blob": "x" * 21000}], db_path=db,
    )
    for revision in range(2, 103):
        record_eval_result(
            target["run_id"], task_id=target["task_id"], workflow_id=target["workflow_id"],
            revision=revision, requirements_satisfied=True,
            evidence=[{"blob": "x" * 21000}], db_path=db,
        )
    context = _compile(db, target, "tester")
    assert any(
        item.get("value", {}).get("verification_passed") is False
        or item.get("value", {}).get("source_truncated") is True
        for item in context.verification
    )
    assert not any(item.get("value", {}).get("verification_passed") is True for item in context.verification)


def test_oversized_verification_payload_cannot_clear_failure(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-oversized-verification", node="test", role="tester"))
    state_db.record_trajectory_event(
        {
            "run_id": target["run_id"], "task_id": target["task_id"],
            "workflow_id": target["workflow_id"], "event_type": "verification_completed",
            "payload": {
                "verification": {"passed": False},
                "oversized": "x" * 21000,
            },
        },
        db_path=db,
    )
    state_db.record_trajectory_event(
        {
            "run_id": target["run_id"], "task_id": target["task_id"],
            "workflow_id": target["workflow_id"], "event_type": "verification_completed",
            "payload": {"verification": {"passed": True}},
        },
        db_path=db,
    )
    context = _compile(db, target, "tester")
    assert not any(item.get("value", {}).get("passed") is True for item in context.verification)
    assert context.verification


def test_strict_failure_window_ignores_non_boolean_verification_values(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-verification-type-window", node="test", role="tester"))
    ledger = TrajectoryLedger(db)
    valid_failure = ledger.append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "verification_completed",
        "verification": {"passed": False},
    })
    ledger.append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "verification_completed",
        "verification": {"passed": "false"},
    })
    for index in range(301):
        ledger.append_event({
            "run_id": target["run_id"], "task_id": target["task_id"],
            "workflow_id": target["workflow_id"], "event_type": "decision",
            "decision": f"type-noise-{index}",
        })
    context = _compile(db, target, "tester")
    assert any(valid_failure["event_id"] in ref for ref in context.source_refs)
    assert any(item.get("value", {}).get("passed") is False for item in context.verification)


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
    _seed_workflow(db, workflow_id="wf")
    target = _task("task-reused", scope="scope-a", run_id="run-scope-a")
    target["workflow_id"] = "wf"
    _seed_task(db, target)
    state_db.register_working_context_source(
        run_scope="scope-a", workflow_id="wf", source_version="source", db_path=db,
    )

    clock = _source_clock_revision(db, "scope-a", "wf")

    def payload(context_id: str, fingerprint: str, created_at: float):
        result = {
            "context_id": context_id,
            "run_scope": "scope-a",
            "run_id": "run-scope-a",
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
            "goal_source_ref": "task:task-reused",
            "current_state_refs": {"task_id": "task:task-reused"},
            "source_refs": ["task:task-reused"],
            "context_fingerprint": fingerprint,
            "source_version": "source",
            "source_watermark": 1,
            "compiled_at": created_at,
            "metrics": {"source_clock": clock},
        }
        from herdr.context_models import _payload_digest
        result["metrics"]["payload_digest"] = _payload_digest(result)
        return _bind_storage_fingerprint(result)

    first = state_db.save_working_context(payload("wc_scope_a", "a" * 64, 1.0), db_path=db)
    second_payload = payload("wc_scope_b", "b" * 64, 2.0)
    second_payload["goal"] = "changed goal"
    _bind_storage_fingerprint(second_payload)
    second = state_db.save_working_context(second_payload, db_path=db)
    assert first["context_id"] == "wc_scope_a"
    assert second["context_id"] == "wc_scope_b"
    assert len(state_db.list_working_contexts("task-reused", db_path=db)) == 2


def test_supersession_target_outside_finding_window_is_loaded(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-supersession-window", node="review"))
    upstream = _seed_task(db, _task("task-supersession-window-upstream"))
    old = _finding(
        upstream["run_id"], "fnd-window-old", task_id=upstream["task_id"], created_at=1.0,
    )
    successor = _finding(
        upstream["run_id"], "fnd-window-new", task_id=upstream["task_id"], created_at=1000.0,
    )
    successor["metadata"] = {"supersedes": "fnd-window-old"}
    state_db.upsert_trajectory_finding(old, db_path=db)
    state_db.upsert_trajectory_finding(successor, db_path=db)
    for index in range(501):
        state_db.upsert_trajectory_finding(
            _finding(
                upstream["run_id"], f"fnd-window-noise-{index}",
                task_id=upstream["task_id"], created_at=10.0 + index,
            ), db_path=db,
        )
    context = _compile(db, target, "reviewer")
    assert any("fnd-window-new" in ref for ref in context.source_refs)
    assert any("fnd-window-old" in ref for ref in context.source_refs) is False


def test_supersession_relation_does_not_import_foreign_scope_finding(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-relation-scope-target", scope="scope-a"))
    local_task = _seed_task(db, _task("task-relation-scope-local", scope="scope-a"))
    foreign_task = _seed_task(db, _task("task-relation-scope-foreign", scope="scope-b"))
    foreign = _finding(
        foreign_task["run_id"], "fnd-relation-foreign",
        task_id=foreign_task["task_id"], summary="foreign",
    )
    state_db.upsert_trajectory_finding(foreign, db_path=db)
    local = _finding(
        local_task["run_id"], "fnd-relation-local",
        task_id=local_task["task_id"], summary="local",
    )
    local["metadata"] = {"supersedes": "fnd-relation-foreign"}
    state_db.upsert_trajectory_finding(local, db_path=db)
    first = _compile(db, target, "reviewer")
    state_db.upsert_trajectory_finding(
        {**foreign, "summary": "foreign changed"}, db_path=db,
    )
    second = _compile(db, target, "reviewer")
    assert "fnd-relation-foreign" not in json.dumps(second.to_mapping(), ensure_ascii=False)
    assert second.source_version == first.source_version


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
    _seed_workflow(db, workflow_id="wf")
    concurrent_task = _task("task-concurrent", scope="scope", run_id="run")
    concurrent_task["workflow_id"] = "wf"
    _seed_task(db, concurrent_task)
    state_db.register_working_context_source(
        run_scope="scope", workflow_id="wf", source_version="source", db_path=db,
    )
    clock = _source_clock_revision(db, "scope", "wf")

    def payload(context_id: str, fingerprint: str, created_at: float, source_watermark: int = 1):
        result = {
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
            "goal_source_ref": "task:task-concurrent",
            "current_state_refs": {"task_id": "task:task-concurrent"},
            "source_refs": ["task:task-concurrent"],
            "context_fingerprint": fingerprint,
            "source_version": "source",
            "source_watermark": source_watermark,
            "compiled_at": created_at,
            "metrics": {"source_clock": clock},
        }
        from herdr.context_models import _payload_digest
        result["metrics"]["payload_digest"] = _payload_digest(result)
        return _bind_storage_fingerprint(result)

    ctx = multiprocessing.get_context("spawn")
    gate = ctx.Event()
    ready = ctx.Event()
    old = ctx.Process(
        target=_save_working_context_in_process,
        args=(str(db), payload("wc_old", "a" * 64, 10.0), gate, ready),
    )
    old.start()
    assert ready.wait(10)
    state_db.save_working_context(payload("wc_new", "b" * 64, 20.0), db_path=db)
    gate.set()
    old.join(10)
    assert old.exitcode == 0
    latest = state_db.get_latest_working_context("task-concurrent", db_path=db)
    assert latest["context_id"] == "wc_new"


def test_storage_rejects_old_source_watermark_after_newer_snapshot(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db, workflow_id="wf")
    version_task = _task("task-version", scope="scope", run_id="run")
    version_task["workflow_id"] = "wf"
    _seed_task(db, version_task)
    state_db.register_working_context_source(
        run_scope="scope", workflow_id="wf", source_version="v2", db_path=db,
    )
    clock = _source_clock_revision(db, "scope", "wf")
    def payload(context_id: str, fingerprint: str, version: str, watermark: int, created_at: float):
        result = {
            "context_id": context_id, "run_scope": "scope", "run_id": "run",
            "workflow_id": "wf", "task_id": "task-version", "node_id": "review",
            "agent_role": "reviewer", "goal": "goal", "current_state": {},
            "findings": [], "artifacts": [], "evidence": [], "completed": [],
            "decisions": [], "blockers": [], "open_questions": [], "verification": [],
            "handoffs": [], "next_action": "review",
            "goal_source_ref": "task:task-version",
            "current_state_refs": {"task_id": "task:task-version"},
            "source_refs": ["task:task-version"],
            "context_fingerprint": fingerprint, "source_version": version,
            "source_watermark": watermark, "compiled_at": created_at,
            "metrics": {"source_clock": clock},
        }
        from herdr.context_models import _payload_digest
        result["metrics"]["payload_digest"] = _payload_digest(result)
        return _bind_storage_fingerprint(result)

    first = state_db.save_working_context(
        payload("wc_v2", "b" * 64, "v2", 1, 20.0), db_path=db,
    )
    returned = state_db.save_working_context(
        payload("wc_v1_late", "a" * 64, "v1", 0, 30.0), db_path=db,
    )
    assert returned["context_id"] == "wc_v1_late"
    assert state_db.get_latest_working_context("task-version", db_path=db)["context_id"] == "wc_v2"


def test_storage_rejects_items_without_provenance(tmp_path: Path):
    db = tmp_path / "state.db"
    with pytest.raises(ValueError, match="source_ref"):
        state_db.save_working_context(
            {
                "context_id": "wc_invalid",
                "run_scope": "scope",
                "run_id": "run-invalid",
                "workflow_id": "wf-invalid",
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


def test_run_metrics_count_alternate_verification_failure(tmp_path: Path):
    from herdr.metrics import get_run_metrics

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-metrics-alternate-verification"))
    state_db.record_trajectory_event(
        {
            "run_id": target["run_id"], "task_id": target["task_id"],
            "workflow_id": target["workflow_id"], "event_type": "verification_completed",
            "payload": {"verification": {"passed": True}, "verification_passed": False},
        },
        db_path=db,
    )
    metrics = get_run_metrics(target["run_id"], db_path=db)
    assert metrics.verification_failed == 1
    assert metrics.verification_passed == 0


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


def test_run_metrics_aggregate_sibling_tasks_in_explicit_scope(tmp_path: Path):
    from herdr.metrics import get_run_metrics

    db = tmp_path / "state.db"
    _seed_workflow(db)
    first = _seed_task(db, _task("task-metrics-sibling-a", scope="wf-exec-shared"))
    second = _seed_task(db, _task("task-metrics-sibling-b", scope="wf-exec-shared"))
    _compile(db, first, "developer")
    _compile(db, second, "developer")
    metrics = get_run_metrics(first["run_id"], db_path=db)
    assert metrics.working_context_compiles >= 2


def test_legacy_run_metrics_do_not_aggregate_other_per_task_runs(tmp_path: Path):
    from herdr.metrics import get_run_metrics

    db = tmp_path / "state.db"
    _seed_workflow(db)
    first = _task("task-legacy-metrics-a", scope="wf-exec-legacy")
    second = _task("task-legacy-metrics-b", scope="wf-exec-legacy")
    first.pop("workflow_run_id")
    second.pop("workflow_run_id")
    first = _seed_task(db, first)
    second = _seed_task(db, second)
    _compile(db, first, "developer")
    _compile(db, second, "developer")
    metrics = get_run_metrics(first["run_id"], db_path=db)
    assert metrics.working_context_compiles == 1


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
    assert second.metrics["context_chars"] == len(json.dumps(second.to_mapping(), ensure_ascii=False))

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
        "timestamp": 1000.0,
    })
    ledger.append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "verification_completed",
        "verification": {"passed": False},
        "timestamp": 1.0,
    })
    context = _compile(db, target, "tester")
    values = [item.get("value", {}).get("passed") for item in context.verification]
    assert values == [False]


def test_eval_failure_is_not_replaced_by_trajectory_pass_for_same_task(tmp_path: Path):
    from herdr.eval_store import record_eval_result

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-eval-vs-trajectory", node="test", role="tester"))
    TrajectoryLedger(db).append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "verification_completed",
        "verification": {"passed": True}, "timestamp": 1.0,
    })
    record_eval_result(
        target["run_id"], task_id=target["task_id"], workflow_id=target["workflow_id"],
        revision=1, verification_passed=False, db_path=db,
    )
    context = _compile(db, target, "tester")
    values = [item.get("value", {}) for item in context.verification]
    assert any(value.get("passed") is True for value in values)
    assert any(value.get("verification_passed") is False for value in values)


def test_verification_windows_are_per_run_not_global(tmp_path: Path):
    from herdr.eval_store import record_eval_result

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-window-target", node="test", role="tester", scope="scope-window"))
    sibling = _seed_task(db, _task("task-window-sibling", node="implementation", scope="scope-window"))
    ledger = TrajectoryLedger(db)
    for index in range(101):
        ledger.append_event({
            "run_id": sibling["run_id"], "task_id": sibling["task_id"],
            "workflow_id": sibling["workflow_id"], "event_type": "verification_completed",
            "verification": {"passed": True}, "timestamp": float(index),
        })
        record_eval_result(
            sibling["run_id"], task_id=sibling["task_id"],
            workflow_id=sibling["workflow_id"], revision=index + 1,
            verification_passed=True, db_path=db,
        )
    ledger.append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "verification_completed",
        "verification": {"passed": False}, "timestamp": 1000.0,
    })
    record_eval_result(
        target["run_id"], task_id=target["task_id"], workflow_id=target["workflow_id"],
        revision=1, verification_passed=False, db_path=db,
    )
    context = _compile(db, target, "tester")
    values = [item.get("value", {}) for item in context.verification]
    assert any(value.get("passed") is False for value in values)
    assert any(value.get("verification_passed") is False for value in values)


def test_taskless_verification_survives_related_event_noise(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-taskless-verification", node="test", role="tester"))
    ledger = TrajectoryLedger(db)
    valid = ledger.append_event({
        "run_id": target["run_id"], "task_id": None,
        "workflow_id": target["workflow_id"], "event_type": "verification_completed",
        "verification": {"passed": False}, "timestamp": 1.0,
    })
    for index in range(320):
        ledger.append_event({
            "run_id": target["run_id"], "task_id": target["task_id"],
            "workflow_id": target["workflow_id"], "event_type": "verification_completed",
            "verification": {"passed": True}, "timestamp": 10.0 + index,
        })
    context = _compile(db, target, "tester")
    assert any(valid["event_id"] in ref for ref in context.source_refs)
    assert any(item.get("value", {}).get("passed") is False for item in context.verification)


def test_alternate_verification_passed_payload_preserves_strict_failure(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-alternate-verification", node="test", role="tester"))
    event = TrajectoryLedger(db).append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "verification_completed",
        "verification_passed": False,
    })
    context = _compile(db, target, "tester")
    assert any(item.get("source_ref") == f"trajectory:{event['event_id']}" for item in context.verification)
    assert any(item.get("value", {}).get("verification_passed") is False for item in context.verification)


def test_conflicting_verification_fields_cannot_hide_failure(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-conflicting-verification", node="test", role="tester"))
    TrajectoryLedger(db).append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "verification_completed",
        "verification": {"passed": True}, "verification_passed": False,
    })
    context = _compile(db, target, "tester")
    assert any(
        item.get("value", {}).get("verification_passed") is False
        for item in context.verification
    )


def test_nested_and_top_level_verification_conflict_prefers_failure(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-nested-verification-conflict", node="test", role="tester"))
    state_db.record_trajectory_event(
        {
            "run_id": target["run_id"], "task_id": target["task_id"],
            "workflow_id": target["workflow_id"], "event_type": "verification_completed",
            "payload": {
                "verification": {"verification_passed": False},
                "verification_passed": True,
            },
        },
        db_path=db,
    )
    context = _compile(db, target, "tester")
    assert any(
        item.get("value", {}).get("verification_passed") is False
        for item in context.verification
    )


def test_newer_pass_replaces_old_failure_after_recovery(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-recovered-verification", node="test", role="tester"))
    ledger = TrajectoryLedger(db)
    ledger.append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "verification_completed",
        "verification": {"passed": False},
    })
    ledger.append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "verification_completed",
        "verification": {"passed": True},
    })
    context = _compile(db, target, "tester")
    assert any(item.get("value", {}).get("passed") is True for item in context.verification)
    assert not any(item.get("value", {}).get("passed") is False for item in context.verification)


def test_verification_unknown_after_pass_does_not_reactivate_failure(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-recovery-unknown-trajectory", node="test", role="tester"))
    ledger = TrajectoryLedger(db)
    for value in (False, True, {"status": "unknown"}):
        verification = {"passed": value} if isinstance(value, bool) else value
        ledger.append_event({
            "run_id": target["run_id"], "task_id": target["task_id"],
            "workflow_id": target["workflow_id"], "event_type": "verification_completed",
            "verification": verification,
        })
    context = _compile(db, target, "tester")
    assert not any(item.get("value", {}).get("passed") is False for item in context.verification)
    assert any(
        item.get("value", {}).get("status") == "unknown"
        or item.get("value", {}).get("passed") is True
        for item in context.verification
    )


def test_eval_unknown_after_pass_does_not_reactivate_failure(tmp_path: Path):
    from herdr.eval_store import record_eval_result

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-recovery-unknown-eval", node="test", role="tester"))
    record_eval_result(
        target["run_id"], task_id=target["task_id"], workflow_id=target["workflow_id"],
        revision=1, verification_passed=False, db_path=db,
    )
    record_eval_result(
        target["run_id"], task_id=target["task_id"], workflow_id=target["workflow_id"],
        revision=2, verification_passed=True, db_path=db,
    )
    record_eval_result(
        target["run_id"], task_id=target["task_id"], workflow_id=target["workflow_id"],
        revision=3, requirements_satisfied=True, db_path=db,
    )
    context = _compile(db, target, "tester")
    assert not any(item.get("value", {}).get("verification_passed") is False for item in context.verification)
    assert any(item.get("value", {}).get("verification_passed") is None for item in context.verification)


def test_unknown_latest_verification_does_not_erase_strict_failure(tmp_path: Path):
    from herdr.eval_store import record_eval_result

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-unknown-verification", node="test", role="tester"))
    ledger = TrajectoryLedger(db)
    ledger.append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "verification_completed",
        "verification": {"passed": False},
    })
    ledger.append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "verification_completed",
        "verification": {"passed": "false"},
    })
    record_eval_result(
        target["run_id"], task_id=target["task_id"], workflow_id=target["workflow_id"],
        revision=1, verification_passed=False, db_path=db,
    )
    record_eval_result(
        target["run_id"], task_id=target["task_id"], workflow_id=target["workflow_id"],
        revision=2, requirements_satisfied=True, db_path=db,
    )
    context = _compile(db, target, "tester")
    assert any(
        item.get("value", {}).get("passed") is False
        or item.get("value", {}).get("verification_passed") is False
        for item in context.verification
    )


def test_foreign_verification_event_cannot_shadow_valid_failure(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-foreign-event-shadow", node="test", role="tester"))
    ledger = TrajectoryLedger(db)
    valid = ledger.append_event({
        "run_id": target["run_id"], "task_id": target["task_id"],
        "workflow_id": target["workflow_id"], "event_type": "verification_completed",
        "verification": {"passed": False}, "timestamp": 1000.0,
    })
    for index in range(500):
        ledger.append_event({
            "run_id": target["run_id"], "task_id": target["task_id"],
            "workflow_id": "wf-other", "event_type": "verification_completed",
            "verification": {"passed": True}, "timestamp": 2000.0 + index,
        })
    context = _compile(db, target, "tester")
    assert any(valid["event_id"] in ref for ref in context.source_refs)
    assert any(item.get("value", {}).get("passed") is False for item in context.verification)


def test_foreign_eval_cannot_shadow_valid_failure(tmp_path: Path):
    from herdr.eval_store import record_eval_result

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-foreign-eval-shadow", node="test", role="tester"))
    record_eval_result(
        target["run_id"], task_id=target["task_id"],
        workflow_id=target["workflow_id"], revision=1,
        verification_passed=False, db_path=db,
    )
    record_eval_result(
        target["run_id"], task_id=target["task_id"],
        workflow_id="wf-other", revision=2,
        verification_passed=True, db_path=db,
    )
    for revision in range(3, 153):
        record_eval_result(
            target["run_id"], task_id=target["task_id"],
            workflow_id="wf-other", revision=revision,
            verification_passed=True, db_path=db,
        )
    context = _compile(db, target, "tester")
    assert any(item.get("value", {}).get("verification_passed") is False for item in context.verification)


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


def test_task_derived_items_have_distinct_source_refs_for_diff(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = dict(_task("task-derived-diff", status="blocked"))
    target["artifacts"] = [{"ref": "artifact-a"}, {"ref": "artifact-b"}]
    target["blockers"] = ["blocker-a", "blocker-b"]
    target["open_questions"] = ["question-a", "question-b"]
    _seed_task(db, target)
    context = _compile(db, target, "reviewer")
    for values in (context.artifacts, context.blockers, context.open_questions):
        refs = [item["source_ref"] for item in values]
        assert len(refs) == len(set(refs))


def test_corrupt_eval_verification_value_is_not_coerced_to_true(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-corrupt-eval", node="test", role="tester"))
    from herdr.eval_store import record_eval_result
    record_eval_result(
        target["run_id"], task_id=target["task_id"],
        workflow_id=target["workflow_id"], verification_passed=True, db_path=db,
    )
    with state_db.get_db_connection(db) as conn:
        conn.execute("UPDATE eval_results SET verification_passed = 'false'")
        conn.commit()
    context = _compile(db, target, "tester")
    assert all(item.get("value", {}).get("verification_passed") is not True for item in context.verification)


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

    one_way_old = context([item("finding", "finding:fnd-one-way-old", "old", metadata={"superseded_by": "fnd-one-way-new"})])
    one_way_new = context([item("finding", "finding:fnd-one-way-new", "new")])
    one_way_diff = diff_working_context(one_way_old, one_way_new)
    assert any(row["source_ref"] == "finding:fnd-one-way-old" for row in one_way_diff["superseded"])

    provenance_old = context(old_items)
    provenance_new = context(old_items).to_mapping()
    provenance_new["goal_source_ref"] = "workflow:wf-new"
    provenance_diff = diff_working_context(provenance_old, provenance_new)
    assert any(row["source_ref"] == "context:goal_source_ref" for row in provenance_diff["changed"])

    duplicate_old = context([
        item("artifact", "artifact:same", "one"),
        item("artifact", "artifact:same", "two"),
    ])
    duplicate_new = context([item("artifact", "artifact:same", "one")])
    duplicate_diff = diff_working_context(duplicate_old, duplicate_new)
    assert duplicate_diff["removed"]
    duplicate_reorder = diff_working_context(
        duplicate_old,
        context([
            item("artifact", "artifact:same", "two"),
            item("artifact", "artifact:same", "one"),
        ]),
    )
    assert not duplicate_reorder["changed"]

    identity_new = context([]).to_mapping()
    identity_new["context_id"] = "wc-new"
    identity_diff = diff_working_context(old, identity_new)
    assert any(row["source_ref"] == "context:context_id" for row in identity_diff["changed"])


def test_incoming_handoff_survives_other_handoff_noise(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-incoming-handoff-window", node="review"))
    source = _seed_task(db, _task("task-incoming-handoff-source", node="implementation"))
    state_db.create_collaboration_event(
        {
            "run_id": "wf-exec-1", "workflow_id": "wf-context",
            "from_task_id": source["task_id"], "to_task_id": target["task_id"],
            "type": "HANDOFF", "summary": "target incoming",
            "source_fact_id": "fact-incoming-window",
        },
        db_path=db,
    )
    for index in range(50):
        other = _seed_task(db, _task(f"task-handoff-noise-{index}", node="implementation"))
        state_db.create_collaboration_event(
            {
                "run_id": "wf-exec-1", "workflow_id": "wf-context",
                "from_task_id": other["task_id"], "to_task_id": source["task_id"],
                "type": "HANDOFF", "summary": f"noise-{index}",
                "source_fact_id": f"fact-handoff-noise-{index}",
            },
            db_path=db,
        )
    context = _compile(db, target, "reviewer")
    assert any("target incoming" in json.dumps(item) for item in context.handoffs)


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


def test_explicit_task_fallback_cannot_substitute_another_task(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    other = _seed_task(db, _task("task-explicit-other"))
    from herdr.context_compiler import compile_working_context

    with pytest.raises(ValueError, match="task_id"):
        compile_working_context(
            workflow_id=other["workflow_id"],
            task_id="task-requested",
            agent_role="developer",
            task=other,
            db_path=db,
        )


def test_handoff_evidence_refs_are_scope_filtered(tmp_path: Path):
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-handoff-evidence", node="review", role="reviewer"))
    source = _seed_task(db, _task("task-handoff-source", scope="wf-exec-1"))
    foreign_observation = create_observation(
        run_id="run-foreign", task_id="task-foreign",
        source_type="verification", source_ref="verification:foreign-handoff",
        content="foreign", store=ObservationStore(db),
    )
    state_db.create_collaboration_event(
        {
            "run_id": "wf-exec-1", "workflow_id": "wf-context",
            "from_task_id": source["task_id"], "to_task_id": target["task_id"],
            "evidence_refs": [foreign_observation.observation_id],
            "source_fact_id": "fact-handoff-evidence",
        },
        db_path=db,
    )
    context = _compile(db, target, "reviewer")
    assert all(
        foreign_observation.observation_id not in ref
        for item in context.handoffs
        for ref in item.get("value", {}).get("evidence_refs", [])
    )


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


def test_dispatch_does_not_treat_context_source_refs_as_evidence(tmp_path: Path):
    import importlib.machinery
    import importlib.util

    controller_path = Path(__file__).resolve().parent.parent / "services" / "herdr-controller.py"
    spec = importlib.util.spec_from_loader(
        "context_compiler_evidence_scope_test",
        importlib.machinery.SourceFileLoader(
            "context_compiler_evidence_scope_test", str(controller_path)
        ),
    )
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _task("task-dispatch-evidence-target", node="review", role="reviewer")
    target["pane_id"] = "pane-evidence-target"
    target = _seed_task(db, target)
    source = _task("task-dispatch-evidence-source", node="implementation", role="developer")
    source["pane_id"] = "pane-evidence-source"
    source = _seed_task(db, source)
    context = _compile(db, target, "reviewer")
    event = state_db.create_collaboration_event(
        {
            "run_id": "wf-exec-1",
            "workflow_id": "wf-context",
            "from_task_id": source["task_id"],
            "to_task_id": target["task_id"],
            "to_agent": "reviewer",
            "type": "HANDOFF",
            "evidence_refs": [f"task:{target['task_id']}"],
            "context_refs": [context.context_id],
            "source_fact_id": "fact-evidence-scope",
        },
        db_path=db,
    )
    calls = []
    result = controller.dispatch_collaboration_event(
        event["event_id"],
        {target["task_id"]: target, source["task_id"]: source},
        lambda pane, prompt: calls.append((pane, prompt)),
        db_path=db,
    )
    assert result["dispatched"] is True
    assert "EVIDENCE:\n- task:task-dispatch-evidence-target" not in calls[0][1]


def test_dispatch_rejects_cross_workflow_target_in_shared_scope(tmp_path: Path):
    import importlib.machinery
    import importlib.util

    controller_path = Path(__file__).resolve().parent.parent / "services" / "herdr-controller.py"
    spec = importlib.util.spec_from_loader(
        "context_compiler_cross_workflow_dispatch_test",
        importlib.machinery.SourceFileLoader(
            "context_compiler_cross_workflow_dispatch_test", str(controller_path)
        ),
    )
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    db = tmp_path / "state.db"
    _seed_workflow(db, workflow_id="wf-a", scope="shared-exec")
    _seed_workflow(db, workflow_id="wf-b", scope="shared-exec")
    source = _seed_task(db, _task("task-cross-source", workflow_id="wf-a", scope="shared-exec"))
    target = _seed_task(db, _task("task-cross-target", workflow_id="wf-b", scope="shared-exec"))
    target["pane_id"] = "pane-cross-target"
    state_db.save_task(target, db_path=db)
    event = state_db.create_collaboration_event(
        {
            "run_id": "shared-exec", "workflow_id": "wf-a",
            "from_task_id": source["task_id"], "to_task_id": target["task_id"],
            "type": "HANDOFF", "source_fact_id": "fact-cross-workflow",
        },
        db_path=db,
    )
    calls = []
    result = controller.dispatch_collaboration_event(
        event["event_id"], {source["task_id"]: source, target["task_id"]: target},
        lambda pane, prompt: calls.append((pane, prompt)), db_path=db,
    )
    assert result["status"] == "failed"
    assert calls == []


def test_legacy_handoff_filters_task_evidence_from_old_run(tmp_path: Path):
    import importlib.machinery
    import importlib.util

    controller_path = Path(__file__).resolve().parent.parent / "services" / "herdr-controller.py"
    spec = importlib.util.spec_from_loader(
        "context_compiler_old_run_evidence_test",
        importlib.machinery.SourceFileLoader(
            "context_compiler_old_run_evidence_test", str(controller_path)
        ),
    )
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _task("task-old-run-evidence-target", node="review", role="reviewer")
    target["pane_id"] = "pane-old-run-evidence"
    target = _seed_task(db, target)
    source = _task("task-old-run-evidence-source", node="implementation", role="developer")
    source = _seed_task(db, source)
    old_observation = create_observation(
        run_id="run-old", task_id=target["task_id"], workflow_id=target["workflow_id"],
        source_type="verification", source_ref="verification:old-run",
        content="old run", store=ObservationStore(db),
    )
    state_db.save_task(dict(target, run_id="run-new"), db_path=db)
    event = state_db.create_collaboration_event(
        {
            "run_id": "wf-exec-1", "workflow_id": "wf-context",
            "from_task_id": source["task_id"], "to_task_id": target["task_id"],
            "type": "HANDOFF", "evidence_refs": [old_observation.observation_id],
            "source_fact_id": "fact-old-run-evidence",
        },
        db_path=db,
    )
    calls = []
    controller.dispatch_collaboration_event(
        event["event_id"], {target["task_id"]: target, source["task_id"]: source},
        lambda pane, prompt: calls.append((pane, prompt)), db_path=db,
    )
    assert old_observation.observation_id not in calls[0][1]


def test_legacy_task_evidence_without_workflow_is_scope_validated(tmp_path: Path):
    import importlib.machinery
    import importlib.util

    controller_path = Path(__file__).resolve().parent.parent / "services" / "herdr-controller.py"
    spec = importlib.util.spec_from_loader(
        "context_compiler_missing_workflow_evidence_test",
        importlib.machinery.SourceFileLoader(
            "context_compiler_missing_workflow_evidence_test", str(controller_path)
        ),
    )
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _task("task-missing-workflow-evidence-target", node="review", role="reviewer")
    target["pane_id"] = "pane-missing-workflow-evidence"
    target = _seed_task(db, target)
    source = _seed_task(db, _task("task-missing-workflow-evidence-source", node="implementation"))
    observation = create_observation(
        run_id=target["run_id"], task_id=target["task_id"], workflow_id=None,
        source_type="verification", source_ref="verification:missing-workflow-valid",
        content="valid legacy evidence", store=ObservationStore(db),
    )
    event = state_db.create_collaboration_event(
        {
            "run_id": "wf-exec-1", "workflow_id": "wf-context",
            "from_task_id": source["task_id"], "to_task_id": target["task_id"],
            "type": "HANDOFF", "evidence_refs": [observation.observation_id],
            "source_fact_id": "fact-missing-workflow-valid",
        },
        db_path=db,
    )
    calls = []
    controller.dispatch_collaboration_event(
        event["event_id"], {target["task_id"]: target, source["task_id"]: source},
        lambda pane, prompt: calls.append((pane, prompt)), db_path=db,
    )
    assert observation.observation_id in calls[0][1]


def test_legacy_evidence_missing_workflow_cannot_cross_event_workflow(tmp_path: Path):
    import importlib.machinery
    import importlib.util

    controller_path = Path(__file__).resolve().parent.parent / "services" / "herdr-controller.py"
    spec = importlib.util.spec_from_loader(
        "context_compiler_cross_workflow_evidence_test",
        importlib.machinery.SourceFileLoader(
            "context_compiler_cross_workflow_evidence_test", str(controller_path)
        ),
    )
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    db = tmp_path / "state.db"
    _seed_workflow(db, workflow_id="wf-a", scope="shared-exec")
    _seed_workflow(db, workflow_id="wf-b", scope="shared-exec")
    source = _task("task-evidence-source-a", workflow_id="wf-a", scope="shared-exec")
    source["pane_id"] = "pane-evidence-source-a"
    source = _seed_task(db, source)
    target = _task("task-evidence-target-a", workflow_id="wf-a", scope="shared-exec")
    target["pane_id"] = "pane-evidence-target-a"
    target = _seed_task(db, target)
    foreign_task = _seed_task(db, _task("task-evidence-foreign-b", workflow_id="wf-b", scope="shared-exec"))
    observation = create_observation(
        run_id=foreign_task["run_id"], task_id=foreign_task["task_id"], workflow_id=None,
        source_type="verification", source_ref="verification:foreign-workflow",
        content="foreign", store=ObservationStore(db),
    )
    event = state_db.create_collaboration_event(
        {
            "run_id": "shared-exec", "workflow_id": "wf-a",
            "from_task_id": source["task_id"], "to_task_id": target["task_id"],
            "type": "HANDOFF", "evidence_refs": [observation.observation_id],
            "source_fact_id": "fact-cross-workflow-evidence",
        },
        db_path=db,
    )
    calls = []
    controller.dispatch_collaboration_event(
        event["event_id"], {source["task_id"]: source, target["task_id"]: target},
        lambda pane, prompt: calls.append((pane, prompt)), db_path=db,
    )
    assert observation.observation_id not in calls[0][1]


def test_legacy_handoff_without_context_filters_unverified_evidence(tmp_path: Path):
    import importlib.machinery
    import importlib.util

    controller_path = Path(__file__).resolve().parent.parent / "services" / "herdr-controller.py"
    spec = importlib.util.spec_from_loader(
        "context_compiler_legacy_evidence_test",
        importlib.machinery.SourceFileLoader(
            "context_compiler_legacy_evidence_test", str(controller_path)
        ),
    )
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _task("task-legacy-evidence-target", node="review", role="reviewer")
    target["pane_id"] = "pane-legacy-evidence"
    target = _seed_task(db, target)
    source = _task("task-legacy-evidence-source", node="implementation", role="developer")
    source["pane_id"] = "pane-legacy-evidence-source"
    source = _seed_task(db, source)
    foreign = create_observation(
        run_id="run-foreign", task_id=None, workflow_id="wf-other",
        source_type="verification", source_ref="verification:foreign-legacy",
        content="foreign", store=ObservationStore(db),
    )
    event = state_db.create_collaboration_event(
        {
            "run_id": "wf-exec-1", "workflow_id": "wf-context",
            "from_task_id": source["task_id"], "to_task_id": target["task_id"],
            "type": "HANDOFF", "evidence_refs": [foreign.observation_id],
            "source_fact_id": "fact-legacy-evidence",
        },
        db_path=db,
    )
    calls = []
    result = controller.dispatch_collaboration_event(
        event["event_id"], {target["task_id"]: target, source["task_id"]: source},
        lambda pane, prompt: calls.append((pane, prompt)), db_path=db,
    )
    assert result["dispatched"] is True
    assert foreign.observation_id not in calls[0][1]


def test_dispatch_rejects_stale_caller_task_snapshot(tmp_path: Path):
    import importlib.machinery
    import importlib.util

    controller_path = Path(__file__).resolve().parent.parent / "services" / "herdr-controller.py"
    spec = importlib.util.spec_from_loader(
        "context_compiler_stale_task_test",
        importlib.machinery.SourceFileLoader(
            "context_compiler_stale_task_test", str(controller_path)
        ),
    )
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)

    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _task("task-stale-dispatch-target", node="review", role="reviewer", run_id="run-old")
    target["pane_id"] = "pane-stale-dispatch"
    target = _seed_task(db, target)
    source = _task("task-stale-dispatch-source", node="implementation", role="developer")
    source["pane_id"] = "pane-stale-dispatch-source"
    source = _seed_task(db, source)
    context = _compile(db, target, "reviewer")
    stale_target = dict(target)
    state_db.save_task(dict(target, run_id="run-new"), db_path=db)
    event = state_db.create_collaboration_event(
        {
            "run_id": "wf-exec-1",
            "workflow_id": "wf-context",
            "from_task_id": source["task_id"],
            "to_task_id": target["task_id"],
            "to_agent": "reviewer",
            "type": "HANDOFF",
            "context_refs": [context.context_id],
            "source_fact_id": "fact-stale-task",
        },
        db_path=db,
    )
    calls = []
    result = controller.dispatch_collaboration_event(
        event["event_id"],
        {target["task_id"]: stale_target, source["task_id"]: source},
        lambda pane, prompt: calls.append((pane, prompt)),
        db_path=db,
    )
    assert result["status"] == "failed"
    assert calls == []


def test_dispatch_rejects_deleted_target_task(tmp_path: Path):
    import importlib.machinery
    import importlib.util

    controller_path = Path(__file__).resolve().parent.parent / "services" / "herdr-controller.py"
    spec = importlib.util.spec_from_loader(
        "context_compiler_deleted_target_test",
        importlib.machinery.SourceFileLoader(
            "context_compiler_deleted_target_test", str(controller_path)
        ),
    )
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _task("task-deleted-dispatch-target", node="review", role="reviewer")
    target["pane_id"] = "pane-deleted-target"
    target = _seed_task(db, target)
    source = _task("task-deleted-dispatch-source", node="implementation", role="developer")
    source = _seed_task(db, source)
    context = _compile(db, target, "reviewer")
    stale_target = dict(target)
    state_db.delete_task(target["task_id"], db_path=db)
    state_db.delete_task(source["task_id"], db_path=db)
    event = state_db.create_collaboration_event(
        {
            "run_id": "wf-exec-1", "workflow_id": "wf-context",
            "from_task_id": source["task_id"], "to_task_id": target["task_id"],
            "type": "HANDOFF", "context_refs": [context.context_id],
            "source_fact_id": "fact-deleted-target",
        },
        db_path=db,
    )
    calls = []
    result = controller.dispatch_collaboration_event(
        event["event_id"], {target["task_id"]: stale_target, source["task_id"]: source},
        lambda pane, prompt: calls.append((pane, prompt)), db_path=db,
    )
    assert result["status"] == "failed"
    assert calls == []


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


def test_dispatch_rejects_foreign_source_task_scope(tmp_path: Path):
    import importlib.machinery
    import importlib.util

    controller_path = Path(__file__).resolve().parent.parent / "services" / "herdr-controller.py"
    spec = importlib.util.spec_from_loader(
        "context_compiler_foreign_source_test",
        importlib.machinery.SourceFileLoader("context_compiler_foreign_source_test", str(controller_path)),
    )
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    db = tmp_path / "state.db"
    _seed_workflow(db)
    target = _seed_task(db, _task("task-foreign-target", node="review", role="reviewer"))
    target["pane_id"] = "pane-foreign-target"
    state_db.save_task(target, db_path=db)
    foreign = _seed_task(db, _task("task-foreign-source", scope="wf-exec-2"))
    context = _compile(db, target, "reviewer")
    event = state_db.create_collaboration_event(
        {
            "run_id": "wf-exec-1", "workflow_id": "wf-context",
            "from_task_id": foreign["task_id"], "to_task_id": target["task_id"],
            "context_refs": [context.context_id], "source_fact_id": "fact-foreign-source",
        },
        db_path=db,
    )
    calls = []
    result = controller.dispatch_collaboration_event(
        event["event_id"], {target["task_id"]: target, foreign["task_id"]: foreign},
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
