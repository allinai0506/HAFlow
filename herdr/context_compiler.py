"""Deterministic, state-aware WorkingContext compiler for HAFlow.

WorkingContext is an immutable projection.  It reads existing HAFlow facts and
never writes back to Task, Workflow, Trajectory, Observation, Finding, Eval, or
CollaborationEvent stores.  The module keeps selection and diff logic
 deterministic and delegates persistence to :mod:`herdr.state_db`.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import state_db
from .context_models import (
    ContextItem,
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_ITEMS,
    DEFAULT_MAX_ITEMS_PER_KIND,
    WorkingContext,
    _bound_value,
    _clip_text,
    _db_path,
    _hash,
    _item,
    _payload_digest,
    _safe_float,
    _task_run,
    infer_agent_role,
    normalize_agent_role,
)
from .transitions import COMPLETED_TASK_STATUSES
from .context_sources import (
    _dependency_ids,
    _read_source_snapshot,
    _requirements,
    _scope_task_for_node,
    _source_allowed,
    _workflow_node,
)


from .context_projection import (
    _calibrate_context_metrics,
    _config,
    _fit_budget,
    _fit_final_budget,
    _fingerprint_payload,
    _selected_refs,
)


from .context_candidates import (
    _eval_candidates,
    _event_candidates,
    _finding_candidates,
    _handoff_candidates,
    _next_action,
    _observation_candidates,
    _task_artifact_candidates,
    _task_candidates,
)
from .context_selection import (
    _select_items,
    context_relevance,
)


# ---------------------------------------------------------------------------
# Public compiler and storage facade


def _attach_payload_digest(context: WorkingContext) -> WorkingContext:
    metrics = dict(context.metrics)
    metrics["payload_digest"] = _payload_digest(context)
    return _calibrate_context_metrics(
        WorkingContext(**{**context.to_mapping(), "metrics": metrics})
    )


def compile_working_context(
    *,
    workflow_id: str,
    task_id: str,
    agent_role: str,
    store: Any = None,
    task: Optional[Mapping[str, Any]] = None,
    workflow: Optional[Mapping[str, Any]] = None,
    planned_links: Optional[Sequence[Mapping[str, Any]]] = None,
    db_path: Optional[Path] = None,
    boundary: str = "execution",
    config: Optional[Mapping[str, Any]] = None,
    now: Optional[float] = None,
    _retry: int = 0,
) -> WorkingContext:
    """Compile one deterministic, role-aware execution-boundary snapshot."""
    started = time.perf_counter()
    role = normalize_agent_role(agent_role)
    cfg = _config(config)
    snapshot = _read_source_snapshot(
        workflow_id=str(workflow_id),
        task_id=str(task_id),
        store=store,
        db_path=db_path,
        explicit_task=task,
        explicit_workflow=workflow,
        planned_links=planned_links,
    )
    snapshot["source_revision"] = state_db.register_working_context_source(
        run_scope=str(snapshot["run_scope"]),
        workflow_id=str(workflow_id),
        source_version=str(snapshot["source_version"]),
        db_path=db_path or _db_path(store),
    )
    target = snapshot["task"]
    valid_evidence_refs = set()
    for collection, prefix, key in (
        (snapshot["observations"], "observation", "observation_id"),
        (snapshot["events"], "trajectory", "event_id"),
        (snapshot["evals"], "eval", "eval_id"),
    ):
        for item in collection:
            if not item.get(key) or not _source_allowed(
                item,
                task_by_id=snapshot["task_by_id"],
                allowed_runs=snapshot["allowed_runs"],
                workflow_id=str(workflow_id),
                run_scope=snapshot["run_scope"],
            ):
                continue
            valid_evidence_refs.add(f"{prefix}:{item[key]}")
    current_task_id = str(target.get("task_id") or task_id)
    node_id = _clip_text(target.get("node") or target.get("stage") or "") or None
    current_state: Dict[str, Any] = {
        "task_id": current_task_id,
        "task_status": target.get("status"),
        "current_node": node_id,
        "workflow_status": snapshot["workflow"].get("status"),
        "workflow_current_stage": snapshot["workflow"].get("current_stage"),
    }
    runtime = target.get("runtime") if isinstance(target.get("runtime"), Mapping) else {}
    if runtime.get("status"):
        current_state["runtime_status"] = runtime.get("status")
    current_state = {key: value for key, value in current_state.items() if value is not None}
    current_state_refs = {
        "task_id": f"task:{current_task_id}",
        "task_status": f"task:{current_task_id}",
        "current_node": f"task:{current_task_id}",
    }
    if snapshot["workflow"].get("status") is not None:
        current_state_refs["workflow_status"] = f"workflow:{workflow_id}"
    if snapshot["workflow"].get("current_stage") is not None:
        current_state_refs["workflow_current_stage"] = f"workflow:{workflow_id}"
    if runtime.get("status"):
        current_state_refs["runtime_status"] = f"task:{current_task_id}"

    node = _workflow_node(snapshot["workflow"], node_id)
    dependency_ids = _dependency_ids(target, snapshot["workflow"])
    dependency_state: Dict[str, str] = {}
    dependency_task_ids: List[str] = []
    for dependency_id in dependency_ids:
        dependency_task = _scope_task_for_node(dependency_id, snapshot["tasks"])
        dependency_state[dependency_id] = str(dependency_task.get("status")) if dependency_task else "unknown"
        if dependency_task and dependency_task.get("task_id"):
            dependency_task_ids.append(str(dependency_task["task_id"]))
        current_state_refs[f"dependency:{dependency_id}"] = (
            f"task:{dependency_task.get('task_id')}" if dependency_task else f"workflow:{workflow_id}"
        )
    current_state["dependency_state"] = dependency_state
    current_state_refs["dependency_state"] = f"workflow:{workflow_id}"
    requirements = _requirements(target, node)
    has_task_requirements = any(
        target.get(key) for key in ("acceptance_criteria", "requirements", "acceptance")
    )
    has_workflow_requirements = bool(node.get("purpose") or node.get("rules"))
    workflow_requirements = (
        ([node.get("purpose")] if node.get("purpose") else [])
        + list(node.get("rules") or [])
    )
    requirements_ref = (
        f"task:{current_task_id}" if has_task_requirements else f"workflow:{workflow_id}"
    )
    if role == "developer":
        current_state["requirements"] = requirements
        current_state_refs["requirements"] = requirements_ref
        if has_task_requirements and has_workflow_requirements:
            current_state["requirements_workflow"] = workflow_requirements
            current_state_refs["requirements_workflow"] = f"workflow:{workflow_id}"
    elif role == "reviewer":
        current_state["review_scope"] = {
            "node": node_id,
            "dependency_nodes": dependency_ids,
            "rules": list(node.get("rules") or []),
        }
        current_state_refs["review_scope"] = f"workflow:{workflow_id}"
    elif role == "tester":
        current_state["acceptance_criteria"] = requirements
        current_state["verification_targets"] = list(node.get("rules") or [])
        current_state_refs["acceptance_criteria"] = requirements_ref
        current_state_refs["verification_targets"] = f"workflow:{workflow_id}"
        if has_task_requirements and has_workflow_requirements:
            current_state["acceptance_criteria_workflow"] = workflow_requirements
            current_state_refs["acceptance_criteria_workflow"] = f"workflow:{workflow_id}"
    else:
        current_state["dependency_state"] = dependency_state
        current_state["workflow_stage"] = snapshot["workflow"].get("current_stage")
        current_state_refs["workflow_stage"] = f"workflow:{workflow_id}"

    completed_tasks, task_blockers, task_decisions, questions = _task_candidates(
        snapshot["tasks"],
        dependency_ids=dependency_ids,
    )
    for blocker in task_blockers:
        if blocker.get("source_task") == current_task_id:
            blocker.setdefault("metadata", {})["current_task_blocker"] = True
    event_artifacts, event_completed, event_verification, event_decisions, event_blockers = _event_candidates(
        snapshot["events"],
        task_by_id=snapshot["task_by_id"],
        allowed_runs=snapshot["allowed_runs"],
        valid_evidence_refs=valid_evidence_refs,
        workflow_id=str(workflow_id),
        run_scope=snapshot["run_scope"],
    )
    eval_verification = _eval_candidates(
        snapshot["evals"],
        task_by_id=snapshot["task_by_id"],
        allowed_runs=snapshot["allowed_runs"],
        valid_evidence_refs=valid_evidence_refs,
        workflow_id=str(workflow_id),
        run_scope=snapshot["run_scope"],
    )
    event_artifacts.extend(_task_artifact_candidates(snapshot["tasks"]))
    findings = _finding_candidates(
        snapshot["findings"],
        task_by_id=snapshot["task_by_id"],
        allowed_runs=snapshot["allowed_runs"],
        valid_evidence_refs=valid_evidence_refs,
        workflow_id=str(workflow_id),
        run_scope=snapshot["run_scope"],
    )
    evidence = _observation_candidates(
        snapshot["observations"],
        task_by_id=snapshot["task_by_id"],
        allowed_runs=snapshot["allowed_runs"],
        workflow_id=str(workflow_id),
        run_scope=snapshot["run_scope"],
    )
    handoffs = _handoff_candidates(
        snapshot["collaborations"],
        target_task_id=current_task_id,
        task_by_id=snapshot["task_by_id"],
        valid_evidence_refs=valid_evidence_refs,
        workflow_id=str(workflow_id),
        run_scope=snapshot["run_scope"],
    )
    blockers = [*task_blockers, *event_blockers]
    # Explicit current task blocker is always retained, including when it is
    # not represented by a status transition event.
    if (
        str(target.get("status") or "") not in COMPLETED_TASK_STATUSES
        and target.get("blocker")
        and not any(
            item.get("source_task") == current_task_id for item in blockers
        )
    ):
        blockers.append(_item(
            "blocker",
            {"reason": target.get("blocker"), "task_id": current_task_id},
            f"task:{current_task_id}:blocker:current",
            source_task=current_task_id,
            source_run=_task_run(target),
            created_at=target.get("created_at"),
            metadata={
                "status": str(target.get("status") or ""),
                "node": node_id,
                "current_task_blocker": True,
            },
        ))
    for finding in findings:
        metadata = finding.get("metadata") or {}
        if metadata.get("severity") == "critical" or metadata.get("finding_type") in {"verification_failure", "repeated_failure"}:
            blockers.append(_item(
                "blocker",
                {"reason": finding.get("value"), "finding_id": finding.get("source_ref")},
                str(finding.get("source_ref")),
                source_task=finding.get("source_task"),
                source_run=finding.get("source_run"),
                created_at=finding.get("created_at"),
                metadata={"status": "blocked", "derived_from": "finding"},
            ))
    for handoff in handoffs:
        if (handoff.get("metadata") or {}).get("failed"):
            blockers.append(_item(
                "blocker",
                {"reason": handoff.get("value"), "handoff_id": handoff.get("source_ref")},
                str(handoff.get("source_ref")),
                source_task=handoff.get("source_task"),
                source_run=handoff.get("source_run"),
                created_at=handoff.get("created_at"),
                metadata={"status": "failed", "derived_from": "handoff"},
            ))

    decisions = [*task_decisions, *event_decisions]
    completed = [*completed_tasks, *event_completed]
    all_verification = [*event_verification, *eval_verification]
    all_candidates = [
        *completed, *event_artifacts, *evidence, *findings, *decisions,
        *blockers, *questions, *all_verification, *handoffs,
    ]
    raw_candidate_items = len(all_candidates)
    now_value = float(now if now is not None else time.time())
    selection_now = max(
        (_safe_float(item.get("created_at")) for item in all_candidates),
        default=0.0,
    )
    selected: Dict[str, List[Dict[str, Any]]] = {}
    for field_name, kind in (
        ("completed", "completed"),
        ("artifacts", "artifact"),
        ("evidence", "evidence"),
        ("findings", "finding"),
        ("decisions", "decision"),
        ("blockers", "blocker"),
        ("open_questions", "open_question"),
        ("verification", "verification"),
        ("handoffs", "handoff"),
    ):
        source_items = {
            "completed": completed,
            "artifacts": event_artifacts,
            "evidence": evidence,
            "findings": findings,
            "decisions": decisions,
            "blockers": blockers,
            "open_questions": questions,
            "verification": all_verification,
            "handoffs": handoffs,
        }[field_name]
        selected[field_name] = _select_items(
            source_items,
            kind=kind,
            role=role,
            current_state=current_state,
            target_task_id=current_task_id,
            dependency_ids=dependency_task_ids,
            limit=int(cfg["max_items_per_kind"].get(field_name, 0)),
            now=selection_now,
        )

    # Verification is latest-per-task/source, not a historical pass/fail dump.
    def verification_order(item: Mapping[str, Any]) -> Tuple[float, int, int, str]:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        return (
            int(metadata.get("sequence") or 0),
            int(metadata.get("revision") or 0),
            _safe_float(item.get("created_at")),
            str(item.get("source_ref") or ""),
        )

    latest_verification: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    latest_failure: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    latest_pass: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    def verification_strength(item: Mapping[str, Any]) -> int:
        value = item.get("value") if isinstance(item.get("value"), Mapping) else {}
        if value.get("source_truncated") is True:
            return 3
        if value.get("passed") is False or value.get("verification_passed") is False:
            return 2
        if value.get("passed") is True or value.get("verification_passed") is True:
            return 1
        return 0

    for item in all_verification:
        source_ref = str(item.get("source_ref") or "")
        source_kind = "eval" if source_ref.startswith("eval:") else "trajectory"
        key = (
            str(item.get("source_run") or ""),
            str(item.get("source_task") or ""),
            source_kind,
        )
        order = verification_order(item)
        current = latest_verification.get(key)
        strength = verification_strength(item)
        current_strength = verification_strength(current) if current is not None else 0
        current_order = verification_order(current) if current is not None else None
        if (
            current is None
            or (strength == 3 and current_strength != 3)
            or (
                current_order is not None
                and order >= current_order
                and not (current_strength == 3 and strength < 3)
            )
        ):
            latest_verification[key] = item
        if strength == 2:
            previous = latest_failure.get(key)
            if previous is None or order >= verification_order(previous):
                latest_failure[key] = item
        elif strength == 1:
            previous = latest_pass.get(key)
            if previous is None or order >= verification_order(previous):
                latest_pass[key] = item

    for key, latest in list(latest_verification.items()):
        if verification_strength(latest) != 0:
            continue
        failure = latest_failure.get(key)
        recovery = latest_pass.get(key)
        if failure is not None and (
            recovery is None
            or verification_order(recovery) <= verification_order(failure)
        ):
            latest_verification[key] = failure
    def verification_failed(item: Mapping[str, Any]) -> bool:
        value = item.get("value") if isinstance(item.get("value"), Mapping) else {}
        return (
            value.get("passed") is False
            or value.get("verification_passed") is False
            or value.get("source_truncated") is True
            or str(value.get("status") or item.get("status") or "").lower()
            in {"failed", "failure", "blocked"}
        )

    selected["verification"] = sorted(
        latest_verification.values(),
        key=lambda item: (
            0 if verification_failed(item) else 1,
            0 if item.get("source_task") == current_task_id else 1,
            -verification_order(item)[0],
            -verification_order(item)[1],
            -verification_order(item)[2],
            str(item.get("source_ref") or ""),
        ),
    )[: int(cfg["max_items_per_kind"]["verification"])]

    # Explicitly keep the latest incoming handoff first, then bounded history.
    selected["handoffs"] = sorted(
        selected["handoffs"],
        key=lambda item: (
            not bool((item.get("metadata") or {}).get("incoming")),
            -_safe_float(item.get("created_at")),
            str(item.get("source_ref") or ""),
        ),
    )[: int(cfg["max_items_per_kind"]["handoffs"])]

    next_action = _next_action(role, target, selected["blockers"])
    goal = str(target.get("goal") or snapshot["workflow"].get("title") or node.get("purpose") or "")
    context = WorkingContext(
        context_id=f"wc_{uuid.uuid4().hex}",
        run_scope=str(snapshot["run_scope"]),
        run_id=_task_run(target),
        workflow_id=str(workflow_id),
        task_id=current_task_id,
        node_id=node_id,
        agent_role=role,
        goal=_clip_text(goal),
        goal_source_ref=f"task:{current_task_id}" if target.get("goal") else f"workflow:{workflow_id}",
        current_state=_bound_value(current_state, 512),
        current_state_refs=current_state_refs,
        next_action=_clip_text(next_action, 512),
        next_action_source_ref=f"policy:context_compiler_v1:{role}",
        source_refs=[],
        source_version=str(snapshot["source_version"]),
        source_watermark=int(snapshot.get("source_revision") or 0),
        compiled_at=now_value,
        **selected,
    )
    context = WorkingContext(**{
        **context.to_mapping(),
        "source_refs": _selected_refs(context),
    })
    fingerprint = _hash(_fingerprint_payload(context, cfg))
    context = WorkingContext(**{**context.to_mapping(), "context_fingerprint": fingerprint})
    context = _fit_budget(context, cfg)
    fingerprint = _hash(_fingerprint_payload(context, cfg))
    context = WorkingContext(**{
        **context.to_mapping(),
        "context_fingerprint": fingerprint,
        "source_refs": _selected_refs(context),
    })
    actual_selected_items = sum(
        len(getattr(context, field_name))
        for field_name in (
            "completed", "artifacts", "evidence", "findings", "decisions", "blockers",
            "open_questions", "verification", "handoffs",
        )
    )
    taskless_source_runs = sorted({
        str(item.get("source_run"))
        for field_name in (
            "completed", "artifacts", "evidence", "findings", "decisions", "blockers",
            "open_questions", "verification", "handoffs",
        )
        for item in selected.get(field_name, [])
        if not item.get("source_task") and item.get("source_run")
    })
    metrics = {
        "raw_candidate_items": raw_candidate_items,
        "selected_items": actual_selected_items,
        "context_chars": 0,
        "compile_latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "context_reuse": False,
        "context_changed": True,
        "boundary": str(boundary),
        "source_clock": int(snapshot.get("source_clock") or 0),
    }
    if taskless_source_runs:
        metrics["source_run_ids"] = taskless_source_runs
    context = WorkingContext(**{**context.to_mapping(), "metrics": metrics})
    metrics["context_chars"] = len(json.dumps(context.to_mapping(), ensure_ascii=False))
    context = WorkingContext(**{**context.to_mapping(), "metrics": metrics})
    context = _fit_final_budget(context, int(cfg["max_chars"]))
    context = WorkingContext(**{
        **context.to_mapping(),
        "context_fingerprint": _hash(_fingerprint_payload(context, cfg)),
    })
    context = _fit_final_budget(context, int(cfg["max_chars"]))
    context = WorkingContext(**{
        **context.to_mapping(),
        "context_fingerprint": _hash(_fingerprint_payload(context, cfg)),
    })
    context = _calibrate_context_metrics(context)
    context = _attach_payload_digest(context)
    if len(json.dumps(context.to_mapping(), ensure_ascii=False)) > int(cfg["max_chars"]):
        raise ValueError("max_chars is too small for the required WorkingContext identity")
    if _retry < 1:
        fresh_snapshot = _read_source_snapshot(
            workflow_id=str(workflow_id),
            task_id=str(task_id),
            store=store,
            db_path=db_path,
            explicit_task=task,
            explicit_workflow=workflow,
            planned_links=planned_links,
        )
        fresh_snapshot["source_revision"] = state_db.register_working_context_source(
            run_scope=str(fresh_snapshot["run_scope"]),
            workflow_id=str(workflow_id),
            source_version=str(fresh_snapshot["source_version"]),
            db_path=db_path or _db_path(store),
        )
        if fresh_snapshot.get("source_version") != snapshot.get("source_version"):
            return compile_working_context(
                workflow_id=workflow_id,
                task_id=task_id,
                agent_role=role,
                store=store,
                task=task,
                workflow=workflow,
                planned_links=planned_links,
                db_path=db_path,
                boundary=boundary,
                config=config,
                now=now,
                _retry=_retry + 1,
            )
    if not state_db.working_context_source_is_current(
        context.to_mapping(), db_path=db_path or _db_path(store),
    ):
        if _retry < 1:
            return compile_working_context(
                workflow_id=workflow_id,
                task_id=task_id,
                agent_role=role,
                store=store,
                task=task,
                workflow=workflow,
                planned_links=planned_links,
                db_path=db_path,
                boundary=boundary,
                config=config,
                now=now,
                _retry=_retry + 1,
            )
        raise RuntimeError("WorkingContext source changed before persistence")
    stored = state_db.save_working_context(
        context.to_mapping(), db_path=db_path or _db_path(store),
        fingerprint_config=cfg,
    )
    if stored.pop("_stale_snapshot", False):
        if _retry < 1:
            return compile_working_context(
                workflow_id=workflow_id,
                task_id=task_id,
                agent_role=role,
                store=store,
                task=task,
                workflow=workflow,
                planned_links=planned_links,
                db_path=db_path,
                boundary=boundary,
                config=config,
                now=now,
                _retry=_retry + 1,
            )
        raise RuntimeError("WorkingContext candidate remained stale after retry")
    same_fingerprint = str(stored.get("context_fingerprint")) == context.context_fingerprint
    if not same_fingerprint and str(stored.get("context_id")) != context.context_id and _retry < 1:
        return compile_working_context(
            workflow_id=workflow_id,
            task_id=task_id,
            agent_role=role,
            store=store,
            task=task,
            workflow=workflow,
            planned_links=planned_links,
            db_path=db_path,
            boundary=boundary,
            config=config,
            now=now,
            _retry=_retry + 1,
        )
    if not same_fingerprint and str(stored.get("context_id")) != context.context_id:
        raise RuntimeError("WorkingContext candidate was superseded before persistence")
    reused = same_fingerprint and str(stored.get("context_id")) != context.context_id
    metrics["context_reuse"] = reused
    metrics["context_changed"] = not reused
    result_mapping = dict(stored)
    result_mapping["metrics"] = metrics
    result_mapping["context_fingerprint"] = stored.get("context_fingerprint", context.context_fingerprint)
    try:
        state_db.record_working_context_metric(
            result_mapping,
            reused=reused,
            db_path=db_path or _db_path(store),
        )
    except Exception:
        # Metrics are diagnostic side effects; an unavailable sink must not
        # invalidate an already persisted immutable context.
        pass
    result_context = _fit_final_budget(
        WorkingContext.from_mapping(result_mapping), int(cfg["max_chars"]),
    )
    return _attach_payload_digest(_calibrate_context_metrics(result_context))


def get_working_context(
    context_id: str,
    *,
    store: Any = None,
    db_path: Optional[Path] = None,
) -> Optional[WorkingContext]:
    row = state_db.get_working_context(
        context_id,
        db_path=Path(db_path) if db_path is not None else _db_path(store),
    )
    return WorkingContext.from_mapping(row) if row is not None else None


def get_latest_working_context(
    task_id: str,
    *,
    agent_role: Optional[str] = None,
    store: Any = None,
    db_path: Optional[Path] = None,
    run_scope: Optional[str] = None,
    workflow_id: Optional[str] = None,
) -> Optional[WorkingContext]:
    row = state_db.get_latest_working_context(
        task_id,
        agent_role=normalize_agent_role(agent_role) if agent_role else None,
        db_path=Path(db_path) if db_path is not None else _db_path(store),
        run_scope=run_scope,
        workflow_id=workflow_id,
    )
    return WorkingContext.from_mapping(row) if row is not None else None


def list_working_contexts(
    task_id: str,
    *,
    agent_role: Optional[str] = None,
    store: Any = None,
    db_path: Optional[Path] = None,
    run_scope: Optional[str] = None,
    workflow_id: Optional[str] = None,
    limit: int = 1000,
    offset: int = 0,
) -> List[WorkingContext]:
    rows = state_db.list_working_contexts(
        task_id,
        agent_role=normalize_agent_role(agent_role) if agent_role else None,
        db_path=Path(db_path) if db_path is not None else _db_path(store),
        run_scope=run_scope,
        workflow_id=workflow_id,
        limit=limit,
        offset=offset,
    )
    return [WorkingContext.from_mapping(row) for row in rows]


# ---------------------------------------------------------------------------
# Diff


from .context_diff import diff_working_context


__all__ = [
    "ContextItem",
    "WorkingContext",
    "compile_working_context",
    "context_relevance",
    "diff_working_context",
    "get_latest_working_context",
    "get_working_context",
    "infer_agent_role",
    "list_working_contexts",
    "normalize_agent_role",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_ITEMS",
    "DEFAULT_MAX_ITEMS_PER_KIND",
]
