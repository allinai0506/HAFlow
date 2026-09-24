"""FR-4 delivery-record decision layer (pure, no I/O).

Delivery identity lives in ``herdr/workflow_docs`` as ``kind="delivery"``
notes (append-only, workflow-scoped, clone-external). The effective
identity is "latest non-stale delivery note" (A5/A6裁定). Gate-verdict
files stay task-scoped; the delivery note is the only workflow-level
candidate identity, and it participates in the fix-loop invalidation chain
via EVIDENCE_KINDS.

This module holds pure validators so ``bin/herdr-task`` stays thin:
  * delivery payload validation (4 fields non-empty)
  * content-equivalence check (A-5: cherry/patch-id or tree equivalence,
    never fast-forward reachability)
  * same-headRefName warning (FR-4.4, post-hoc attribution, not interception)
  * effective-candidate selection with supersede/no-fallback semantics (A-6)
"""

from __future__ import annotations

REQUIRED_FIELDS = (
    "delivery_branch",
    "candidate_sha",
    "review_task",
    "test_gate",
)


def validate_delivery_payload(payload: dict | None) -> tuple[bool, list[str]]:
    """Check the 4-field delivery contract (presence + non-empty)."""
    payload = payload or {}
    missing: list[str] = []
    for field in REQUIRED_FIELDS:
        value = payload.get(field)
        if value is None or str(value).strip() == "":
            missing.append(field)
    return (len(missing) == 0, missing)


def delivery_note_title(branch: str, sha: str) -> str:
    """Human-scannable delivery title (branch + short sha)."""
    short = str(sha or "")[:12]
    return f"delivery {branch}@{short}"


def delivery_note_body(payload: dict | None) -> str:
    """Render the delivery note body (4要素, PR template source)."""
    payload = payload or {}
    lines = [
        f"delivery_branch: {payload.get('delivery_branch', '')}",
        f"candidate_sha: {payload.get('candidate_sha', '')}",
        f"review_task: {payload.get('review_task', '')}",
        f"test_gate: {payload.get('test_gate', '')}",
        f"base: {payload.get('base', '')}",
    ]
    return "\n".join(lines)


def candidates_equivalent(
    *,
    candidate_sha: str,
    head_sha: str,
    cherry_lines: str | None = None,
    head_tree: str | None = None,
    candidate_tree: str | None = None,
) -> tuple[bool, str]:
    """Content-equivalence判据 (A-5, replaces fast-forward).

    Accept when:
      * head_sha equals candidate_sha (exact), or
      * ``git cherry`` output shows no ``+`` (missing) lines for the
        candidate range, or
      * head_tree == candidate_tree (squash/merge-commit tolerant).
    Otherwise return (False, reason). Never uses reachability, so squash
    merges cannot permanently poison later deliveries.
    """
    cand = str(candidate_sha or "").strip()
    head = str(head_sha or "").strip()
    if cand and head and cand == head:
        return True, "exact_sha_match"
    if (
        head_tree is not None
        and candidate_tree is not None
        and str(head_tree).strip() != ""
        and str(head_tree) == str(candidate_tree)
    ):
        return True, "tree_equivalent"
    if cherry_lines is not None:
        missing = [
            line
            for line in str(cherry_lines).splitlines()
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
    """FR-4.4 same-headRefName post-hoc attribution (warn, never block).

    ``merged_prs`` items: {"number", "head_sha", "merged_at"}.
    Returns {"warn": bool, "detail": str, "prs": [...]}. This cannot intercept
    a GitHub-UI merge (C-5); it only attributes after the fact.
    """
    prs = list(merged_prs or [])
    same = [
        pr
        for pr in prs
        if str(pr.get("head_ref") or pr.get("headRefName") or "") == str(head_ref_name or "")
        or "head_ref" not in pr
        and "headRefName" not in pr
    ]
    # When callers pass only same-branch PRs, treat the list as the scope.
    scoped = same if same else prs
    if not scoped:
        return {"warn": False, "detail": "no_prior_merged_pr", "prs": []}
    cand = str(candidate_sha or "").strip()
    related = []
    for pr in scoped:
        sha = str(pr.get("head_sha") or pr.get("headSha") or "")
        related.append({"number": pr.get("number"), "head_sha": sha})
        if sha and cand and sha != cand:
            return {
                "warn": True,
                "detail": (
                    f"branch {head_ref_name} previously merged "
                    f"PR #{pr.get('number')} at {sha}, "
                    f"candidate is {cand}: not ancestor-or-equal, needs review"
                ),
                "prs": related,
            }
    return {"warn": False, "detail": "same_branch_consistent", "prs": related}


def select_effective_delivery(notes: list[dict] | None) -> dict | None:
    """Select the single effective delivery (latest non-stale, A-6).

    * Only ``kind == "delivery"`` notes without ``stale`` are eligible.
    * Latest by (ts, note_id) wins; ties never merge.
    * Superseded candidates are ineligible only when their own note is
      marked stale via the invalidation chain; this function never silently
      falls back to an older candidate when the latest is explicitly
      superseded-but-not-stale -- callers must treat ambiguity as reject.
    Returns the note dict or None.
    """
    eligible = [
        note
        for note in (notes or [])
        if isinstance(note, dict)
        and note.get("kind") == "delivery"
        and not note.get("stale")
    ]
    if not eligible:
        return None
    def _sort_key(note: dict) -> tuple:
        try:
            ts = float(note.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        return (ts, str(note.get("note_id") or ""))
    eligible.sort(key=_sort_key)
    return eligible[-1]


def pr_template_fields(
    *,
    branch: str,
    head_sha: str,
    base: str,
    review_ref: str,
) -> dict:
    """FR-4.3 PR description four-element template (pure)."""
    return {
        "branch": str(branch or ""),
        "head_sha": str(head_sha or ""),
        "base": str(base or ""),
        "review_ref": str(review_ref or ""),
    }


def format_pr_body(fields: dict | None) -> str:
    """Render the four-element PR body (10s recognizability)."""
    fields = fields or {}
    return (
        f"branch: {fields.get('branch', '')}\n"
        f"head-sha: {fields.get('head_sha', '')}\n"
        f"base: {fields.get('base', '')}\n"
        f"review: {fields.get('review_ref', '')}\n"
    )
