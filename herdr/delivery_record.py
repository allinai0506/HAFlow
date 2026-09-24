"""FR-4 delivery candidate identity and selection (pure, no I/O).

A delivery note is an append-only candidate claim, not an ordering hint.  The
selector therefore requires one explicit, non-invalidated identity and rejects
ambiguity instead of guessing the newest timestamp.  Supersession is
transitive for eligibility: once a replacement claims an older candidate, an
invalidated replacement must not cause a fallback to that older candidate.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


REQUIRED_FIELDS = (
    "delivery_branch",
    "candidate_sha",
    "review_task",
    "test_gate",
)


class DeliveryAmbiguityError(ValueError):
    """Raised when more than one delivery identity is still eligible."""

    def __init__(self, candidates: list[dict]):
        self.candidates = candidates
        identities = [candidate_identity(item) for item in candidates]
        super().__init__(
            "ambiguous delivery candidates: " + ", ".join(identities)
        )


def _body_value(note: dict, field: str) -> str:
    value = note.get(field)
    if value not in (None, ""):
        return str(value).strip()
    body = str(note.get("body") or "")
    prefix = f"{field}:"
    for line in body.splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


def candidate_identity(note: dict | None) -> str:
    """Return the explicit candidate id, or a deterministic legacy identity."""
    note = note or {}
    for field in ("delivery_id", "candidate_id", "identity"):
        value = note.get(field)
        if value not in (None, ""):
            return str(value).strip()
    branch = _body_value(note, "delivery_branch")
    sha = _body_value(note, "candidate_sha")
    review = _body_value(note, "review_task")
    test_gate = _body_value(note, "test_gate")
    if sha:
        return sha
    if branch or review or test_gate:
        raw = "|".join((branch, sha, review, test_gate))
        return "legacy-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return str(note.get("note_id") or "")


def _candidate_aliases(note: dict) -> set[str]:
    values = {
        candidate_identity(note),
        str(note.get("note_id") or ""),
        str(note.get("delivery_id") or ""),
        str(note.get("candidate_id") or ""),
        _body_value(note, "candidate_sha"),
    }
    return {value for value in values if value}


def _list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _supersedes(note: dict) -> set[str]:
    values = set()
    for field in (
        "supersedes", "supersedes_candidate", "supersedes_delivery",
        "supersedes_id", "replaces", "replaced_candidate",
    ):
        values.update(_list(note.get(field)))
    return {value for value in values if value}


def _superseded_by(note: dict) -> set[str]:
    values = set()
    for field in (
        "superseded_by", "superseded_by_candidate", "superseded_by_delivery",
        "invalidated_by", "invalidated_by_candidate",
    ):
        values.update(_list(note.get(field)))
    return {value for value in values if value}


def validate_delivery_payload(payload: dict | None) -> tuple[bool, list[str]]:
    """Check the four-field delivery contract."""
    payload = payload or {}
    missing = [
        field for field in REQUIRED_FIELDS
        if payload.get(field) in (None, "")
        or not str(payload.get(field)).strip()
    ]
    return len(missing) == 0, missing


def delivery_note_title(branch: str, sha: str) -> str:
    short = str(sha or "")[:12]
    return f"delivery {branch}@{short}"


def delivery_note_body(payload: dict | None) -> str:
    payload = payload or {}
    lines = [
        f"delivery_id: {payload.get('delivery_id', '')}",
        f"delivery_branch: {payload.get('delivery_branch', '')}",
        f"candidate_sha: {payload.get('candidate_sha', '')}",
        f"review_task: {payload.get('review_task', '')}",
        f"test_gate: {payload.get('test_gate', '')}",
        f"base: {payload.get('base', '')}",
    ]
    if payload.get("supersedes"):
        lines.append(f"supersedes: {payload['supersedes']}")
    return "\n".join(lines)


def candidates_equivalent(
    *,
    candidate_sha: str,
    head_sha: str,
    cherry_lines: str | None = None,
    head_tree: str | None = None,
    candidate_tree: str | None = None,
) -> tuple[bool, str]:
    """Check exact/tree/cherry content equivalence without fast-forward bias."""
    cand = str(candidate_sha or "").strip()
    head = str(head_sha or "").strip()
    if cand and head and cand == head:
        return True, "exact_sha_match"
    if (
        head_tree is not None
        and candidate_tree is not None
        and str(head_tree).strip()
        and str(head_tree) == str(candidate_tree)
    ):
        return True, "tree_equivalent"
    if cherry_lines is not None:
        missing = [
            line for line in str(cherry_lines).splitlines()
            if line.startswith("+")
        ]
        if not missing:
            return True, "cherry_equivalent"
        return False, f"cherry_missing_{len(missing)}"
    return False, "not_equivalent"


def same_branch_warning(
    *,
    head_ref_name: str,
    merged_prs: list[dict] | None,
    candidate_sha: str,
) -> dict:
    """Return post-hoc same-branch attribution without pretending to block."""
    prs = list(merged_prs or [])
    scoped = [
        pr for pr in prs
        if (
            str(pr.get("head_ref") or pr.get("headRefName") or "")
            == str(head_ref_name or "")
        )
    ]
    if not scoped:
        return {"warn": False, "detail": "no_prior_merged_pr", "prs": []}
    candidate = str(candidate_sha or "").strip()
    related = []
    warning = False
    for pr in scoped:
        sha = str(pr.get("head_sha") or pr.get("headSha") or "")
        related.append({"number": pr.get("number"), "head_sha": sha})
        if sha and candidate and sha != candidate:
            warning = True
    if warning:
        return {
            "warn": True,
            "detail": (
                f"branch {head_ref_name} has a prior merged SHA different "
                f"from candidate {candidate}; review required"
            ),
            "prs": related,
        }
    return {"warn": False, "detail": "same_branch_consistent", "prs": related}


def apply_invalidation(
    notes: list[dict] | None,
    current_base_sha: str | None = None,
) -> list[dict]:
    """Attach workflow-document staleness to a candidate selection snapshot."""
    from . import workflow_docs

    return workflow_docs.annotate_notes(
        list(notes or []), current_base_sha=current_base_sha
    )


def select_effective_delivery(notes: list[dict] | None) -> dict | None:
    """Select exactly one active candidate or reject ambiguity/no candidate.

    ``None`` means there is no eligible candidate, including the case where a
    superseding candidate was later invalidated.  That distinction prevents a
    stale predecessor from being resurrected.
    """
    delivery_notes = [
        note for note in (notes or [])
        if isinstance(note, dict) and note.get("kind") == "delivery"
    ]
    if not delivery_notes:
        return None

    by_identity: dict[str, list[dict]] = {}
    for note in delivery_notes:
        by_identity.setdefault(candidate_identity(note), []).append(note)

    all_aliases = set()
    superseded_aliases = set()
    invalidated_aliases = set()
    for note in delivery_notes:
        aliases = _candidate_aliases(note)
        all_aliases.update(aliases)
        superseded_aliases.update(_superseded_by(note))
        for target in _supersedes(note):
            superseded_aliases.add(target)
    for note in notes or []:
        if not isinstance(note, dict) or note.get("kind") != "invalidation":
            continue
        for field in (
            "invalidated_candidates", "invalidates_candidates",
            "invalidated_delivery_ids", "invalidated_candidate_shas",
        ):
            invalidated_aliases.update(_list(note.get(field)))
        if note.get("candidate_sha"):
            invalidated_aliases.add(str(note["candidate_sha"]))

    eligible: list[dict] = []
    for identity, candidates in by_identity.items():
        fresh = [item for item in candidates if not item.get("stale")]
        if not fresh:
            continue
        chosen = max(
            fresh,
            key=lambda item: (
                float(item.get("ts") or 0),
                str(item.get("note_id") or ""),
            ),
        )
        aliases = _candidate_aliases(chosen)
        if (
            aliases & superseded_aliases
            or aliases & invalidated_aliases
            or chosen.get("superseded")
            or chosen.get("invalidated")
        ):
            continue
        eligible.append(chosen)

    if not eligible:
        return None
    if len(eligible) > 1:
        raise DeliveryAmbiguityError(eligible)
    return eligible[0]


def supersede_delivery_note(
    workflow_id: str,
    *,
    supersedes: str,
    delivery_branch: str,
    candidate_sha: str,
    review_task: str,
    test_gate: str,
    base: str = "",
    node: str = "wrapup",
    agent: str = "",
) -> dict:
    """Append one replacement claim with an explicit supersession edge."""
    from . import workflow_docs

    payload = {
        "delivery_id": f"candidate-{candidate_sha[:12]}",
        "delivery_branch": delivery_branch,
        "candidate_sha": candidate_sha,
        "review_task": review_task,
        "test_gate": test_gate,
        "base": base,
        "supersedes": supersedes,
    }
    valid, missing = validate_delivery_payload(payload)
    if not valid:
        raise ValueError(f"delivery payload missing fields: {missing}")
    return workflow_docs.append_note(
        workflow_id,
        kind="delivery",
        title=delivery_note_title(delivery_branch, candidate_sha),
        body=delivery_note_body(payload),
        node=node,
        task_id=review_task,
        agent=agent,
        source=workflow_docs.SOURCE_CONTROLLER,
        base_sha=base,
        fields={
            "delivery_id": payload["delivery_id"],
            "delivery_branch": delivery_branch,
            "candidate_sha": candidate_sha,
            "review_task": review_task,
            "test_gate": test_gate,
            "supersedes": supersedes,
        },
    )


def pr_template_fields(
    *,
    branch: str,
    head_sha: str,
    base: str,
    review_ref: str,
    delivery_id: str = "",
) -> dict:
    return {
        "branch": str(branch or ""),
        "head_sha": str(head_sha or ""),
        "base": str(base or ""),
        "review_ref": str(review_ref or ""),
        "delivery_id": str(delivery_id or ""),
    }


def format_pr_body(fields: dict | None) -> str:
    fields = fields or {}
    return (
        f"branch: {fields.get('branch', '')}\n"
        f"head-sha: {fields.get('head_sha', '')}\n"
        f"base: {fields.get('base', '')}\n"
        f"review: {fields.get('review_ref', '')}\n"
        f"delivery-id: {fields.get('delivery_id', '')}\n"
    )
