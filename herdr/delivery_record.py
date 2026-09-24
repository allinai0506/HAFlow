"""FR-4 delivery candidate identity and selection (pure, no I/O).

A delivery note is an append-only candidate claim, not an ordering hint.  The
selector therefore requires one explicit, non-invalidated identity and rejects
ambiguity instead of guessing the newest timestamp.  Supersession is
transitive for eligibility: once a replacement claims an older candidate, an
invalidated replacement must not cause a fallback to that older candidate.
"""

from __future__ import annotations

import hashlib
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


class DeliveryIdentityError(DeliveryAmbiguityError):
    """Raised when a delivery edge or identity cannot be trusted."""

    def __init__(self, message: str, candidates: list[dict] | None = None):
        self.candidates = candidates or []
        ValueError.__init__(self, message)


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
        str(note.get("task_id") or ""),
        _body_value(note, "delivery_id"),
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
        values.update(_list(_body_value(note, field)))
    return {value for value in values if value}


def _superseded_by(note: dict) -> set[str]:
    values = set()
    for field in (
        "superseded_by", "superseded_by_candidate", "superseded_by_delivery",
        "invalidated_by", "invalidated_by_candidate",
    ):
        values.update(_list(note.get(field)))
        values.update(_list(_body_value(note, field)))
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
        sha = str(
            pr.get("head_sha") or pr.get("headSha")
            or pr.get("headRefOid") or ""
        )
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


def _has_candidate_identity(note: dict) -> bool:
    """Whether a delivery note carries an explicit candidate identity.

    Legacy notes may omit optional branch/review/gate fields, but a note with
    only an ID is not a delivery claim and must fail closed.
    """
    explicit = str(note.get("delivery_id") or note.get("candidate_id") or "").strip()
    candidate_sha = _body_value(note, "candidate_sha")
    return bool(candidate_sha and explicit) or all(
        _body_value(note, field) for field in REQUIRED_FIELDS
    )


def _delivery_fingerprint(note: dict) -> tuple:
    """Return the semantic identity payload, excluding replay metadata."""
    return tuple(
        (field, _body_value(note, field))
        for field in (*REQUIRED_FIELDS, "base", "supersedes")
    )


def _timestamp(note: dict) -> float:
    try:
        return float(note.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _scope(note: dict) -> tuple[str, str]:
    return (
        str(note.get("workflow_id") or ""),
        str(note.get("run_id") or ""),
    )


def select_effective_delivery(
    notes: list[dict] | None,
    *,
    workflow_id: str | None = None,
    run_id: str | None = None,
) -> dict | None:
    """Select one explicit candidate or fail closed on uncertainty.

    Candidate identity is never inferred from timestamp order.  Exact replays
    of one identity/payload collapse to one canonical note; conflicting
    payloads, mixed workflow/run scopes, unknown supersede targets, and
    multiple eligible tips all raise ``DeliveryAmbiguityError``.
    """
    source_notes = [note for note in (notes or []) if isinstance(note, dict)]
    delivery_notes = [note for note in source_notes if note.get("kind") == "delivery"]
    if not delivery_notes:
        return None
    malformed = [note for note in delivery_notes if not _has_candidate_identity(note)]
    if malformed:
        raise DeliveryAmbiguityError(malformed)

    scopes = {_scope(note) for note in delivery_notes}
    if workflow_id is not None:
        delivery_notes = [
            note for note in delivery_notes
            if not note.get("workflow_id") or note.get("workflow_id") == workflow_id
        ]
    if run_id is not None:
        delivery_notes = [
            note for note in delivery_notes
            if not note.get("run_id") or note.get("run_id") == run_id
        ]
    if not delivery_notes:
        return None
    filtered_scopes = {_scope(note) for note in delivery_notes}
    if len(filtered_scopes) > 1:
        raise DeliveryAmbiguityError(delivery_notes)
    if workflow_id is None:
        workflow_scopes = {scope[0] for scope in scopes if scope[0]}
        if len(workflow_scopes) > 1 or (workflow_scopes and "" in {
            scope[0] for scope in scopes
        }):
            raise DeliveryAmbiguityError(delivery_notes)
    if run_id is None:
        run_scopes = {scope[1] for scope in scopes if scope[1]}
        if len(run_scopes) > 1 or (run_scopes and "" in {
            scope[1] for scope in scopes
        }):
            raise DeliveryAmbiguityError(delivery_notes)

    by_identity: dict[str, list[dict]] = {}
    for note in delivery_notes:
        by_identity.setdefault(candidate_identity(note), []).append(note)

    canonical: dict[str, dict] = {}
    for identity, candidates in by_identity.items():
        fresh = [item for item in candidates if not item.get("stale")]
        if not fresh:
            continue
        fingerprints = {_delivery_fingerprint(item) for item in fresh}
        if len(fingerprints) > 1:
            raise DeliveryAmbiguityError(fresh)
        # Replayed identical claims are idempotent.  Earliest note_id is a
        # stable representative and avoids timestamp-based candidate churn.
        canonical[identity] = min(
            fresh,
            key=lambda item: (
                _timestamp(item),
                str(item.get("note_id") or ""),
            ),
        )

    aliases: dict[str, str] = {}
    for note in delivery_notes:
        identity = candidate_identity(note)
        for alias in _candidate_aliases(note):
            previous = aliases.get(alias)
            if previous is not None and previous != identity:
                raise DeliveryAmbiguityError(
                    [note for note in delivery_notes if candidate_identity(note) in {previous, identity}]
                )
            aliases[alias] = identity

    superseded: set[str] = set()
    invalidated: set[str] = set()
    unresolved: list[str] = []

    def _resolve(targets):
        resolved = set()
        for target in targets:
            identity = aliases.get(str(target))
            if identity is None:
                unresolved.append(str(target))
            else:
                resolved.add(identity)
        return resolved

    for note in delivery_notes:
        superseded.update(_resolve(_supersedes(note)))
        superseded.update(_resolve(_superseded_by(note)))
    for note in source_notes:
        if note.get("kind") != "invalidation":
            continue
        inv_wf = str(note.get("workflow_id") or "")
        targets = set()
        for field in (
            "invalidated_candidates", "invalidates_candidates",
            "invalidated_delivery_ids", "invalidated_candidate_shas",
            "delivery_ids", "candidate_ids", "candidate_sha", "candidate_shas",
        ):
            targets.update(_list(note.get(field)))
        if targets:
            invalidated.update(_resolve(targets))
            continue
        inv_ts = _timestamp(note)
        inv_nodes = set(_list(note.get("invalidates")))
        for candidate in canonical.values():
            candidate_wf = str(candidate.get("workflow_id") or "")
            if inv_wf and candidate_wf and candidate_wf != inv_wf:
                continue
            if (
                inv_nodes
                and str(candidate.get("node") or "") in inv_nodes
                and _timestamp(candidate) < inv_ts
            ):
                invalidated.add(candidate_identity(candidate))
    if unresolved:
        # Unknown graph edges are not an effective candidate.  Returning no
        # candidate is deterministic fail-closed and keeps the predecessor
        # from being resurrected.
        return None

    eligible = []
    for identity, note in canonical.items():
        if identity in superseded or identity in invalidated:
            continue
        if note.get("superseded") or note.get("invalidated") or note.get("stale"):
            continue
        eligible.append(note)
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
    delivery_id: str = "",
    agent: str = "",
) -> dict:
    """Append one replacement claim with an explicit supersession edge."""
    from . import workflow_docs

    payload = {
        "delivery_id": delivery_id or f"candidate-{candidate_sha}",
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
    identity = candidate_identity(payload)

    def _precondition(existing_notes):
        aliases = {
            alias
            for note in existing_notes
            if note.get("kind") == "delivery"
            for alias in _candidate_aliases(note)
        }
        if supersedes not in aliases:
            raise DeliveryIdentityError(
                f"unknown delivery identity to supersede: {supersedes}"
            )
        for note in existing_notes:
            if note.get("kind") != "delivery":
                continue
            if candidate_identity(note) == identity:
                continue
            if supersedes in _supersedes(note):
                raise DeliveryAmbiguityError([
                    note,
                    {"delivery_id": identity, "reason": "concurrent_replacement"},
                ])
        return True

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
        precondition=_precondition,
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
