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
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from . import state_db
from .context_models import (
    ContextItem,
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_ITEMS,
    DEFAULT_MAX_ITEMS_PER_KIND,
    WorkingContext,
    _as_list,
    _bound_value,
    _canonical_json,
    _clip_text,
    _db_path,
    _hash,
    _item,
    _safe_float,
    _task_run,
    _valid_source_ref,
    infer_agent_role,
    normalize_agent_role,
)
from .context_sources import (
    _dependency_ids,
    _read_source_snapshot,
    _requirements,
    _scope_task_for_node,
    _workflow_node,
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


def _config(value: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    max_chars = int((value or {}).get("max_chars", DEFAULT_MAX_CHARS))
    max_items = int((value or {}).get("max_items", DEFAULT_MAX_ITEMS))
    if max_chars < 500:
        raise ValueError("max_chars must be at least 500")
    if max_items < 1:
        raise ValueError("max_items must be positive")
    caps = dict(DEFAULT_MAX_ITEMS_PER_KIND)
    supplied = (value or {}).get("max_items_per_kind") or {}
    for key, cap in supplied.items():
        if key in caps:
            cap = int(cap)
            if cap < 0:
                raise ValueError(f"max_items_per_kind[{key}] must be non-negative")
            caps[key] = cap
    return {"max_chars": max_chars, "max_items": max_items, "max_items_per_kind": caps}


def _selected_refs(context: WorkingContext) -> List[str]:
    refs = [context.goal_source_ref, context.next_action_source_ref]
    refs.extend(context.current_state_refs.values())
    for field_name in (
        "completed", "artifacts", "evidence", "findings", "decisions", "blockers",
        "open_questions", "verification", "handoffs",
    ):
        for item in getattr(context, field_name):
            ref = item.get("source_ref")
            if ref:
                refs.append(str(ref))
                refs.extend(str(value) for value in item.get("evidence_refs") or [])
    return list(dict.fromkeys(ref for ref in refs if ref))


def _fingerprint_payload(context: WorkingContext, config: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "compiler_version": "context_compiler_v1",
        "run_scope": context.run_scope,
        "run_id": context.run_id,
        "workflow_id": context.workflow_id,
        "task_id": context.task_id,
        "node_id": context.node_id,
        "agent_role": context.agent_role,
        "goal": context.goal,
        "current_state": context.current_state,
        "current_state_refs": context.current_state_refs,
        "completed": context.completed,
        "artifacts": context.artifacts,
        "evidence": context.evidence,
        "findings": context.findings,
        "decisions": context.decisions,
        "blockers": context.blockers,
        "open_questions": context.open_questions,
        "verification": context.verification,
        "handoffs": context.handoffs,
        "next_action": context.next_action,
        "source_refs": context.source_refs,
        "source_version": context.source_version,
        "config": dict(config),
    }


def _fit_budget(context: WorkingContext, config: Mapping[str, Any]) -> WorkingContext:
    max_chars = int(config["max_chars"])
    max_items = int(config["max_items"])
    caps = config["max_items_per_kind"]
    # Per-kind caps first, then global priority trimming.
    values = {}
    for field_name in (
        "completed", "artifacts", "evidence", "findings", "decisions", "blockers",
        "open_questions", "verification", "handoffs",
    ):
        values[field_name] = list(getattr(context, field_name))[: int(caps.get(field_name, 0))]
    context = WorkingContext(**{**context.to_mapping(), **values})

    priority = [
        "completed", "handoffs", "artifacts", "findings", "evidence", "decisions",
        "verification", "open_questions", "blockers",
    ]
    while sum(len(getattr(context, field)) for field in priority) > max_items:
        for field_name in priority:
            if getattr(context, field_name):
                values[field_name] = list(getattr(context, field_name))[:-1]
                context = WorkingContext(**{**context.to_mapping(), **values})
                break
        else:
            break

    def size(candidate: WorkingContext) -> int:
        return len(json.dumps(candidate.to_mapping(), ensure_ascii=False))

    # Reduce bulky values and low-priority history while retaining identity/state.
    for field_name in priority:
        if size(context) <= max_chars:
            break
        while getattr(context, field_name) and size(context) > max_chars:
            values[field_name] = list(getattr(context, field_name))[:-1]
            context = WorkingContext(**{**context.to_mapping(), **values})
    if size(context) > max_chars:
        context = WorkingContext(**{
            **context.to_mapping(),
            "goal": _clip_text(context.goal, 512),
            "current_state": _bound_value(context.current_state, 256),
            "next_action": _clip_text(context.next_action, 256),
        })
    if size(context) > max_chars:
        context = WorkingContext(**{
            **context.to_mapping(),
            "goal": _clip_text(context.goal, 256),
            "current_state": _bound_value(context.current_state, 128),
            "next_action": _clip_text(context.next_action, 128),
        })
    if size(context) > max_chars:
        context = WorkingContext(**{
            **context.to_mapping(),
            "source_refs": list(context.source_refs[:12]),
        })
    return context


def _fit_final_budget(context: WorkingContext, max_chars: int) -> WorkingContext:
    """Fit the serialized snapshot after metrics are attached."""
    for _ in range(4):
        metrics = dict(context.metrics)
        metrics.pop("boundary", None)
        metrics["context_chars"] = 0
        context = WorkingContext(**{**context.to_mapping(), "metrics": metrics})
        size = len(json.dumps(context.to_mapping(), ensure_ascii=False))
        metrics["context_chars"] = size
        context = WorkingContext(**{**context.to_mapping(), "metrics": metrics})
        if len(json.dumps(context.to_mapping(), ensure_ascii=False)) <= max_chars:
            return context
        context = WorkingContext(**{
            **context.to_mapping(),
            "goal": _clip_text(context.goal, 128),
            "next_action": "Continue.",
            "source_version": "",
            "current_state": _bound_value(context.current_state, 128),
            "current_state_refs": dict(context.current_state_refs),
            "source_refs": [
                ref for ref in (context.goal_source_ref, context.next_action_source_ref) if ref
            ],
        })
    raise ValueError("max_chars is too small for the required WorkingContext identity")


def compile_working_context(
    *,
    workflow_id: str,
    task_id: str,
    agent_role: str,
    store: Any = None,
    task: Optional[Mapping[str, Any]] = None,
    workflow: Optional[Mapping[str, Any]] = None,
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
    )
    target = snapshot["task"]
    valid_evidence_refs = {
        f"observation:{item['observation_id']}"
        for item in snapshot["observations"]
        if item.get("observation_id")
    } | {
        f"trajectory:{item['event_id']}"
        for item in snapshot["events"]
        if item.get("event_id")
    } | {
        f"eval:{item['eval_id']}"
        for item in snapshot["evals"]
        if item.get("eval_id")
    }
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
    if role == "developer":
        current_state["requirements"] = requirements
        current_state_refs["requirements"] = f"task:{current_task_id}"
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
        current_state_refs["acceptance_criteria"] = f"task:{current_task_id}"
        current_state_refs["verification_targets"] = f"workflow:{workflow_id}"
    else:
        current_state["dependency_state"] = dependency_state
        current_state["workflow_stage"] = snapshot["workflow"].get("current_stage")
        current_state_refs["workflow_stage"] = f"workflow:{workflow_id}"

    completed_tasks, task_blockers, task_decisions, questions = _task_candidates(
        snapshot["tasks"],
        dependency_ids=dependency_ids,
    )
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
        workflow_id=str(workflow_id),
        run_scope=snapshot["run_scope"],
    )
    blockers = [*task_blockers, *event_blockers]
    # Explicit current task blocker is always retained, including when it is
    # not represented by a status transition event.
    if target.get("blocker") and not any(
        item.get("source_ref") == f"task:{current_task_id}" for item in blockers
    ):
        blockers.append(_item(
            "blocker",
            {"reason": target.get("blocker"), "task_id": current_task_id},
            f"task:{current_task_id}",
            source_task=current_task_id,
            source_run=_task_run(target),
            created_at=target.get("updated_at"),
            metadata={"status": str(target.get("status") or ""), "node": node_id},
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
            now=now_value,
        )

    # Verification is latest-per-task/source, not a historical pass/fail dump.
    latest_verification: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for item in all_verification:
        key = (str(item.get("source_run") or ""), str(item.get("source_task") or ""))
        old = latest_verification.get(key)
        if old is None or _safe_float(item.get("created_at")) >= _safe_float(old.get("created_at")):
            latest_verification[key] = item
    selected["verification"] = sorted(
        latest_verification.values(),
        key=lambda item: str(item.get("source_ref") or ""),
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
        source_watermark=int(snapshot.get("source_watermark") or 0),
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
    metrics = {
        "raw_candidate_items": raw_candidate_items,
        "selected_items": actual_selected_items,
        "context_chars": 0,
        "compile_latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "context_reuse": False,
        "context_changed": True,
        "boundary": str(boundary),
    }
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
        )
        if fresh_snapshot.get("source_version") != snapshot.get("source_version"):
            return compile_working_context(
                workflow_id=workflow_id,
                task_id=task_id,
                agent_role=role,
                store=store,
                task=task,
                workflow=workflow,
                db_path=db_path,
                boundary=boundary,
                config=config,
                now=now,
                _retry=_retry + 1,
            )
    stored = state_db.save_working_context(
        context.to_mapping(), db_path=db_path or _db_path(store),
    )
    same_fingerprint = str(stored.get("context_fingerprint")) == context.context_fingerprint
    if not same_fingerprint and str(stored.get("context_id")) != context.context_id and _retry < 1:
        return compile_working_context(
            workflow_id=workflow_id,
            task_id=task_id,
            agent_role=role,
            store=store,
            task=task,
            workflow=workflow,
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
    state_db.record_working_context_metric(
        result_mapping,
        reused=reused,
        db_path=db_path or _db_path(store),
    )
    return _fit_final_budget(WorkingContext.from_mapping(result_mapping), int(cfg["max_chars"]))


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
) -> Optional[WorkingContext]:
    row = state_db.get_latest_working_context(
        task_id,
        agent_role=normalize_agent_role(agent_role) if agent_role else None,
        db_path=Path(db_path) if db_path is not None else _db_path(store),
    )
    return WorkingContext.from_mapping(row) if row is not None else None


def list_working_contexts(
    task_id: str,
    *,
    agent_role: Optional[str] = None,
    store: Any = None,
    db_path: Optional[Path] = None,
) -> List[WorkingContext]:
    rows = state_db.list_working_contexts(
        task_id,
        agent_role=normalize_agent_role(agent_role) if agent_role else None,
        db_path=Path(db_path) if db_path is not None else _db_path(store),
    )
    return [WorkingContext.from_mapping(row) for row in rows]


# ---------------------------------------------------------------------------
# Diff


def _as_context_mapping(value: Any) -> Dict[str, Any]:
    if isinstance(value, WorkingContext):
        return value.to_mapping()
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("working context must be WorkingContext or mapping")


def _item_maps(context: Mapping[str, Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    result: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for field_name in (
        "completed", "artifacts", "evidence", "findings", "decisions", "blockers",
        "open_questions", "verification", "handoffs",
    ):
        for raw in context.get(field_name) or []:
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            kind = str(item.get("kind") or field_name)
            ref = str(item.get("source_ref") or "")
            if ref:
                result[(kind, ref)] = item
    return result


def _relation_refs(item: Mapping[str, Any], key: str) -> Set[str]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    values = item.get(key) or metadata.get(key)
    refs: Set[str] = set()
    for value in _as_list(values):
        text = str(value)
        refs.add(text if _valid_source_ref(text) else f"finding:{text}")
    return refs


def diff_working_context(
    old: WorkingContext | Mapping[str, Any],
    new: WorkingContext | Mapping[str, Any],
) -> Dict[str, List[Dict[str, Any]]]:
    """Return deterministic added/removed/superseded/changed item diff."""
    old_map = _as_context_mapping(old)
    new_map = _as_context_mapping(new)
    old_items = _item_maps(old_map)
    new_items = _item_maps(new_map)
    added = [item for key, item in new_items.items() if key not in old_items]
    removed = [item for key, item in old_items.items() if key not in new_items]
    changed: List[Dict[str, Any]] = []
    superseded: List[Dict[str, Any]] = []
    superseded_old_keys: Set[Tuple[str, str]] = set()
    for key, new_item in new_items.items():
        old_item = old_items.get(key)
        if old_item is not None and _canonical_json(old_item) != _canonical_json(new_item):
            changed.append({
                "kind": key[0],
                "source_ref": key[1],
                "old": old_item,
                "new": new_item,
            })
        if key[0] == "finding":
            old_candidates = _relation_refs(new_item, "supersedes")
            for old_ref in old_candidates:
                old_key = ("finding", old_ref)
                old_candidate = old_items.get(old_key)
                if old_candidate is not None:
                    superseded.append({
                        "kind": "finding",
                        "source_ref": old_ref,
                        "old": old_candidate,
                        "new": new_item,
                    })
                    superseded_old_keys.add(old_key)
            for old_ref in _relation_refs(new_item, "superseded_by"):
                old_key = ("finding", old_ref)
                if old_key in old_items:
                    superseded.append({
                        "kind": "finding",
                        "source_ref": old_ref,
                        "old": old_items[old_key],
                        "new": new_item,
                    })
                    superseded_old_keys.add(old_key)
    removed = [item for item in removed if (str(item.get("kind")), str(item.get("source_ref"))) not in superseded_old_keys]
    for field_name in ("goal", "current_state", "next_action", "node_id", "agent_role"):
        old_value = old_map.get(field_name)
        new_value = new_map.get(field_name)
        if _canonical_json(old_value) != _canonical_json(new_value):
            changed.append({
                "kind": "context",
                "source_ref": f"context:{field_name}",
                "old": old_value,
                "new": new_value,
            })
    return {
        "added": sorted(added, key=lambda item: (item.get("kind", ""), item.get("source_ref", ""))),
        "removed": sorted(removed, key=lambda item: (item.get("kind", ""), item.get("source_ref", ""))),
        "superseded": sorted(superseded, key=lambda item: (item.get("kind", ""), item.get("source_ref", ""))),
        "changed": sorted(changed, key=lambda item: (item.get("kind", ""), item.get("source_ref", ""))),
    }


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
