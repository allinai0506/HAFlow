"""Deterministic fingerprint and budget projection helpers."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .context_models import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_ITEMS,
    DEFAULT_MAX_ITEMS_PER_KIND,
    WorkingContext,
    _bound_value,
    _clip_text,
)

#: Hard storage ceiling enforced by `save_working_context`. Compiler budgets
#: above this could produce contexts persistence must reject, so configuration
#: fails fast here instead of compiling an unsavable snapshot.
MAX_STORAGE_CONTEXT_CHARS = 20_000


def _config(value: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    max_chars = int((value or {}).get("max_chars", DEFAULT_MAX_CHARS))
    max_items = int((value or {}).get("max_items", DEFAULT_MAX_ITEMS))
    if max_chars < 500:
        raise ValueError("max_chars must be at least 500")
    if max_chars > MAX_STORAGE_CONTEXT_CHARS:
        raise ValueError(
            f"max_chars must not exceed the {MAX_STORAGE_CONTEXT_CHARS}-char storage limit"
        )
    if max_items < 1:
        raise ValueError("max_items must be positive")
    caps = dict(DEFAULT_MAX_ITEMS_PER_KIND)
    supplied = (value or {}).get("max_items_per_kind") or {}
    for key, cap in supplied.items():
        if key in caps:
            cap = int(cap)
            if key in {"blockers", "verification", "open_questions"} and cap < 1:
                raise ValueError(f"max_items_per_kind[{key}] must preserve at least one item")
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
        "goal_source_ref": context.goal_source_ref,
        "next_action_source_ref": context.next_action_source_ref,
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

    def _created_at(item: Any) -> float:
        try:
            return float(item.get("created_at") or 0.0)
        except (TypeError, ValueError, AttributeError):
            return 0.0

    def _latest_incoming_handoff(items: Sequence[Any]) -> Any:
        """Return the single mandatory incoming handoff, if any is present."""
        best = None
        for item in items:
            if not isinstance(item, Mapping):
                continue
            if not bool((item.get("metadata") or {}).get("incoming")):
                continue
            if best is None or (
                _created_at(item),
                str(item.get("source_ref") or ""),
            ) > (
                _created_at(best),
                str(best.get("source_ref") or ""),
            ):
                best = item
        return best

    # Per-kind caps first, then global priority trimming.
    values = {}
    for field_name in (
        "completed", "artifacts", "evidence", "findings", "decisions", "blockers",
        "open_questions", "verification", "handoffs",
    ):
        cap = int(caps.get(field_name, 0))
        items = list(getattr(context, field_name))
        # The latest incoming handoff is mandatory for proving the current
        # handoff fact downstream: keep one slot for it even when the
        # per-kind cap would drop all handoffs. It still counts toward
        # max_items, and an over-budget mandatory set fails closed below.
        if field_name == "handoffs":
            retained = _latest_incoming_handoff(items)
            if retained is not None:
                cap = max(cap, 1)
        post = items[:cap]
        if (
            field_name == "handoffs"
            and retained is not None
            and all(id(candidate) != id(retained) for candidate in post)
        ):
            post = [retained, *post]
        values[field_name] = post
    retained_handoff = _latest_incoming_handoff(values["handoffs"])
    retained_ids = {id(retained_handoff)} if retained_handoff is not None else set()
    context = WorkingContext(**{**context.to_mapping(), **values})

    def _drop_last_trimmable(field_name: str) -> bool:
        items = list(values[field_name])
        keep = retained_ids if field_name == "handoffs" else set()
        for index in range(len(items) - 1, -1, -1):
            if id(items[index]) not in keep:
                del items[index]
                values[field_name] = items
                return True
        return False

    def _rebuild() -> None:
        nonlocal context
        context = WorkingContext(**{**context.to_mapping(), **values})

    priority = [
        "completed", "handoffs", "artifacts", "findings", "evidence", "decisions",
        "verification", "open_questions", "blockers",
    ]
    protected = {"blockers", "verification", "open_questions"}
    while sum(len(getattr(context, field)) for field in priority) > max_items:
        for field_name in priority:
            if field_name in protected:
                continue
            if _drop_last_trimmable(field_name):
                _rebuild()
                break
        else:
            break
    if sum(len(getattr(context, field)) for field in priority) > max_items:
        raise ValueError(
            "context budget cannot preserve mandatory items "
            f"within max_items={max_items}"
        )

    def size(candidate: WorkingContext) -> int:
        return len(json.dumps(candidate.to_mapping(), ensure_ascii=False))

    # Reduce bulky values and low-priority history while retaining identity/state.
    for field_name in priority:
        if field_name in protected:
            continue
        if size(context) <= max_chars:
            break
        while getattr(context, field_name) and size(context) > max_chars:
            if not _drop_last_trimmable(field_name):
                break
            _rebuild()
            if size(context) <= max_chars:
                break
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


def _calibrate_context_metrics(context: WorkingContext) -> WorkingContext:
    """Keep context_chars equal to the actual default JSON serialization size."""
    metrics = dict(context.metrics)
    metrics["context_chars"] = 0
    context = WorkingContext(**{**context.to_mapping(), "metrics": metrics})
    zero_size = len(json.dumps(context.to_mapping(), ensure_ascii=False))
    estimate = zero_size
    for _ in range(5):
        estimate = zero_size + len(str(estimate)) - 1
        metrics["context_chars"] = estimate
        context = WorkingContext(**{**context.to_mapping(), "metrics": metrics})
        actual = len(json.dumps(context.to_mapping(), ensure_ascii=False))
        if actual == estimate:
            return context
        zero_size = actual - len(str(actual)) + 1
    return context


def _fit_final_budget(context: WorkingContext, max_chars: int) -> WorkingContext:
    """Fit the serialized snapshot after metrics are attached."""
    role_state_key = {
        "developer": "requirements",
        "reviewer": "review_scope",
        "tester": "acceptance_criteria",
        "coordinator": "dependency_state",
    }.get(context.agent_role)

    state_keys = {
        "task_id", "task_status", "current_node", "workflow_status", "runtime_status", "dependency_state",
        role_state_key,
    }

    def compact_state(state: Mapping[str, Any]) -> Dict[str, Any]:
        result = {
            key: value for key, value in state.items() if key in state_keys
        }
        for key, value in list(result.items()):
            if isinstance(value, list) and len(value) > 1:
                result[key] = value[:1]
        if role_state_key and role_state_key in result:
            value = result[role_state_key]
            result[role_state_key] = value[:1] if isinstance(value, list) else value
        return _bound_value(result, 24)

    def compact_item(item: Mapping[str, Any]) -> Dict[str, Any]:
        keys = ("kind", "value", "source_ref", "source_task", "source_run", "evidence_refs")
        return {key: item[key] for key in keys if key in item}

    def compact_refs(candidate: WorkingContext) -> list[str]:
        state_keys = {"task_id", "task_status", "current_node", "workflow_status", "runtime_status", "dependency_state", role_state_key}
        refs = [candidate.goal_source_ref, candidate.next_action_source_ref]
        refs.extend(
            value for key, value in candidate.current_state_refs.items()
            if key in state_keys
        )
        for field_name in ("blockers", "verification", "open_questions", "handoffs"):
            for item in getattr(candidate, field_name)[:1]:
                refs.append(str(item.get("source_ref") or ""))
                refs.extend(str(value) for value in item.get("evidence_refs") or [])
        return list(dict.fromkeys(ref for ref in refs if ref))[:12]

    def verification_failed(item: Mapping[str, Any]) -> bool:
        value = item.get("value") if isinstance(item.get("value"), Mapping) else {}
        return (
            value.get("passed") is False
            or value.get("verification_passed") is False
            or value.get("source_truncated") is True
            or str(value.get("status") or item.get("status") or "").lower()
            in {"failed", "failure", "blocked"}
        )

    fit_limit = max(300, max_chars - 64)
    for _ in range(4):
        metrics = dict(context.metrics)
        metrics.pop("boundary", None)
        metrics["context_chars"] = 0
        context = WorkingContext(**{**context.to_mapping(), "metrics": metrics})
        size = len(json.dumps(context.to_mapping(), ensure_ascii=False))
        metrics["context_chars"] = size
        context = WorkingContext(**{**context.to_mapping(), "metrics": metrics})
        metrics["selected_items"] = sum(
            len(getattr(context, field_name))
            for field_name in (
                "completed", "artifacts", "evidence", "findings", "decisions", "blockers",
                "open_questions", "verification", "handoffs",
            )
        )
        context = WorkingContext(**{**context.to_mapping(), "metrics": metrics})
        context = _calibrate_context_metrics(context)
        if len(json.dumps(context.to_mapping(), ensure_ascii=False)) <= fit_limit:
            return context
        context = WorkingContext(**{
            **context.to_mapping(),
            "goal": _clip_text(context.goal, 24),
            "next_action": (
                "Resolve blocker."
                if context.blockers
                else "Resolve failed verification."
                if any(verification_failed(item) for item in context.verification)
                else "Continue."
            ),
            "compiled_at": context.compiled_at,
            "current_state": compact_state(context.current_state),
            "current_state_refs": {
                key: value for key, value in context.current_state_refs.items()
                if key in state_keys
            },
            "completed": [],
            "artifacts": [],
            "evidence": [],
            "findings": [],
            "decisions": [],
            "open_questions": [compact_item(item) for item in context.open_questions[:1]],
            "handoffs": [compact_item(item) for item in context.handoffs[:1]],
            "blockers": [compact_item(item) for item in context.blockers[:1]],
            "verification": [
                compact_item(item)
                for item in sorted(
                    context.verification,
                    key=lambda item: (
                        not verification_failed(item),
                        str(item.get("source_task") or "") != str(context.task_id or ""),
                        str(item.get("source_ref") or ""),
                    ),
                )[:1]
            ],
        })
        context = WorkingContext(**{
            **context.to_mapping(),
            "source_refs": compact_refs(context),
        })
    if len(json.dumps(context.to_mapping(), ensure_ascii=False)) > fit_limit:
        compact_metrics = {
            key: context.metrics[key]
            for key in ("source_clock", "context_reuse", "context_changed", "selected_items")
            if key in context.metrics
        }
        compact_state_refs = {
            key: value for key, value in context.current_state_refs.items()
            if key in state_keys
        }
        context = WorkingContext(**{
            **context.to_mapping(),
            "goal": _clip_text(context.goal, 16),
            "next_action": _clip_text(context.next_action, 64),
            "current_state": _bound_value(context.current_state, 16),
            "current_state_refs": compact_state_refs,
            "metrics": compact_metrics,
        })
        context = WorkingContext(**{
            **context.to_mapping(),
            "source_refs": compact_refs(context),
        })
        context = _calibrate_context_metrics(context)
    if len(json.dumps(context.to_mapping(), ensure_ascii=False)) <= fit_limit:
        return context
    raise ValueError("max_chars is too small for the required WorkingContext identity")
