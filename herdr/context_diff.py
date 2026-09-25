"""Deterministic WorkingContext structural diff."""

from __future__ import annotations

import hashlib

from typing import Any, Dict, List, Mapping, Set, Tuple

from .context_models import (
    WorkingContext,
    _as_list,
    _canonical_json,
    _valid_source_ref,
)


def _as_context_mapping(value: Any) -> Dict[str, Any]:
    if isinstance(value, WorkingContext):
        return value.to_mapping()
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("working context must be WorkingContext or mapping")


def _item_maps(context: Mapping[str, Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
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
                grouped.setdefault((kind, ref), []).append(item)

    result: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for (kind, ref), items in grouped.items():
        unique = {}
        for item in items:
            unique.setdefault(_canonical_json(item), item)
        if len(unique) == 1:
            result[(kind, ref)] = next(iter(unique.values()))
            continue
        for item in sorted(unique.values(), key=_canonical_json):
            suffix = hashlib.sha256(
                _canonical_json(item).encode("utf-8")
            ).hexdigest()[:8]
            stable_item = dict(item)
            stable_item["source_ref"] = f"{ref}#{suffix}"
            key = (kind, stable_item["source_ref"])
            collision = 2
            while key in result:
                stable_item["source_ref"] = f"{ref}#{suffix}-{collision}"
                key = (kind, stable_item["source_ref"])
                collision += 1
            result[key] = stable_item
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
    superseded_pairs: Set[Tuple[str, str]] = set()

    def add_superseded(old_ref: str, old_item: Mapping[str, Any], new_item: Mapping[str, Any]) -> None:
        pair = (old_ref, str(new_item.get("source_ref") or ""))
        if pair in superseded_pairs:
            return
        superseded_pairs.add(pair)
        superseded.append({
            "kind": "finding",
            "source_ref": old_ref,
            "old": dict(old_item),
            "new": dict(new_item),
        })
        superseded_old_keys.add(("finding", old_ref))
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
                    add_superseded(old_ref, old_candidate, new_item)
            for old_ref in _relation_refs(new_item, "superseded_by"):
                old_key = ("finding", old_ref)
                if old_key in old_items:
                    add_superseded(old_ref, old_items[old_key], new_item)
    for old_key, old_item in old_items.items():
        if old_key[0] != "finding":
            continue
        for new_ref in _relation_refs(old_item, "superseded_by"):
            new_key = ("finding", new_ref)
            new_item = new_items.get(new_key)
            if new_item is not None:
                add_superseded(old_key[1], old_item, new_item)
    removed = [item for item in removed if (str(item.get("kind")), str(item.get("source_ref"))) not in superseded_old_keys]
    for field_name in (
        "context_id", "run_scope", "run_id", "workflow_id", "task_id",
        "goal", "current_state", "next_action", "node_id", "agent_role",
        "goal_source_ref", "next_action_source_ref", "current_state_refs",
        "source_refs", "source_version",
    ):
        old_value = old_map.get(field_name)
        new_value = new_map.get(field_name)
        if _canonical_json(old_value) != _canonical_json(new_value):
            changed.append({
                "kind": "context" if field_name in {
                    "goal", "current_state", "next_action", "node_id", "agent_role",
                } else "provenance",
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
