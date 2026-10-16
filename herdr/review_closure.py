"""Shared wrapup review-closure invariant (Codex review, wf-haflow-1015-01).

The wrapup node's rules require the delivery PR and its CI/review findings to
be closed out before completion. Prompt text alone is bypassable, so this
module owns the machine-checked invariant used by every completion source:
the CLI ``set`` path and the controller's CAS auto-accept path — both funnel
through :func:`herdr.kernel.transition_task`.

An acceptable receipt carries, in one ``review_closure_recorded`` event:

- the 40-hex head SHA it certifies — completion compares it with the task
  clone's current HEAD: a commit after that head makes the receipt **stale**,
  and an unverifiable head fails closed instead of waving the transition
  through;
- for a normal closure: the delivery PR URL, ``ci_status=pass``, the
  structured unresolved P1/P2 counts (P1 must be 0) and a disposition text;
- for a deferred closure (the PR genuinely cannot be created): a concrete
  reason carrying the blocking evidence, plus the same head SHA.

A ``review_closure_skipped`` event (written by the audited
``--skip-review-closure --note`` bypass) also allows completion, but only for
the head it was bound to: rework or a branch advance expires the waiver.

The whole contract is **opt-in**: it applies only when the workflow's pinned
configuration declares ``review_closure_required`` on its wrapup node. The
authoritative source is the normalized definition persisted with the workflow
registration (SQLite ``config_json``); the task's pinned ``workflow_config``
file is only a fallback for registrations that carry no config snapshot. A
configuration that exists but cannot be read fails closed, while a verified
absent capability (legacy workflows, custom templates without the flag)
keeps the previous completion path (Codex review P1).
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_HEX40 = re.compile(r"[0-9a-f]{40}")


def _latest_event(store, task_id: str, event_type: str) -> Optional[Dict[str, Any]]:
    try:
        rows = store.list_events(
            task_id=task_id,
            event_type=event_type,
            source="herdr-task",
            desc=True,
            limit=1,
        )
    except (OSError, ValueError, RuntimeError):
        return None
    if not rows:
        return None
    payload = rows[0].get("payload")
    return payload if isinstance(payload, dict) else {}


def latest_receipt(store, task_id: str) -> Optional[Dict[str, Any]]:
    return _latest_event(store, task_id, "review_closure_recorded")


def latest_skip(store, task_id: str) -> Optional[Dict[str, Any]]:
    return _latest_event(store, task_id, "review_closure_skipped")


def _wrapup_flag(config: Any) -> bool:
    if not isinstance(config, dict):
        return False
    for node in (config.get("nodes") or []):
        if isinstance(node, dict) and (node.get("id") or node.get("key")) == "wrapup":
            return bool(node.get("review_closure_required"))
    return False


def closure_required(store, task: Any) -> bool:
    """True when the workflow's pinned configuration opts into the contract.

    Authoritative source first (the workflow registration's normalized
    ``config`` snapshot); the task's pinned config file only as fallback when
    no snapshot exists. An existing-but-unreadable configuration fails
    closed; a verified absent capability keeps the previous behaviour.
    """
    task = task or {}
    workflow_id = str(task.get("workflow_id") or "").strip()
    if not workflow_id:
        return False
    try:
        workflow = store.get_workflow(workflow_id) or {}
    except Exception:
        return True  # authoritative snapshot unreadable → fail closed
    config = workflow.get("config")
    if isinstance(config, dict) and config.get("nodes"):
        return _wrapup_flag(config)
    path = str(task.get("workflow_config") or "").strip()
    if not path:
        return False  # legacy: no pinned nodes definition anywhere
    try:
        with open(path, "r", encoding="utf-8") as stream:
            file_config = json.load(stream)
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True  # pinned configuration unreadable → fail closed
    if not isinstance(file_config, dict):
        return True
    return _wrapup_flag(file_config)


def validate_receipt_payload(payload: Any) -> Tuple[bool, str]:
    """Content validation: an unacceptable receipt must never be written."""
    if not isinstance(payload, dict):
        return False, "receipt payload is missing"
    head = str(payload.get("head_sha") or "").strip().lower()
    if not _HEX40.fullmatch(head):
        return False, "--head-sha must be the 40-hex commit the review closed out"
    if payload.get("deferred"):
        if not str(payload.get("reason") or "").strip():
            return False, "deferred receipt requires --reason with the blocking evidence"
        return True, ""
    if not str(payload.get("pr_url") or "").strip():
        return False, "--pr-url is required (or use --deferred --reason)"
    if str(payload.get("ci_status") or "").strip().lower() != "pass":
        return False, (
            "--ci-status must be 'pass' (CI green); use --deferred --reason "
            "when CI cannot be obtained"
        )
    p1 = payload.get("unresolved_p1")
    if isinstance(p1, bool) or not isinstance(p1, int) or p1 < 0:
        return False, "--unresolved-p1 must report the unresolved P1 count from the review"
    if p1 != 0:
        return False, (
            f"receipt refused: {p1} unresolved P1 finding(s) — resolve them "
            "(fix the new head, re-run the review) before wrapup completion"
        )
    p2 = payload.get("unresolved_p2")
    if isinstance(p2, bool) or not isinstance(p2, int) or p2 < 0:
        return False, "--unresolved-p2 must report the unresolved P2 count from the review"
    if not str(payload.get("findings") or "").strip():
        return False, "--findings must state the P2/P3 disposition"
    return True, ""


def clone_head_sha(clone_path: Any) -> str:
    """Current HEAD of the task clone, or "" when not a readable git worktree."""
    if not clone_path:
        return ""
    clone = Path(str(clone_path)).expanduser()
    if not (clone / ".git").exists():
        return ""
    try:
        probe = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "--verify", "HEAD"],
            text=True, capture_output=True, timeout=10, check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    if probe.returncode:
        return ""
    return (probe.stdout or "").strip().lower()


def check_completion_allowed(store, task: Any) -> Tuple[bool, str]:
    """(ok, refusal_reason) for a wrapup completion transition."""
    task = task or {}
    task_id = str(task.get("task_id") or "")
    if not closure_required(store, task):
        # Not opted in (legacy workflow / custom template): previous behaviour.
        return True, ""

    current_head = clone_head_sha(task.get("clone_path"))
    receipt_refusal = ""
    payload = latest_receipt(store, task_id)
    if payload is None:
        receipt_refusal = (
            "wrapup completion requires a review-closure receipt. Run "
            f"`herdr-task record-review-closure {task_id} --pr-url <url> "
            "--ci-status pass --unresolved-p1 0 --unresolved-p2 <n> "
            '--findings "<dispositions>" --head-sha <head>` (or `--deferred '
            '--reason "<evidence>"`); audited emergency bypass: '
            '--skip-review-closure --note "<reason>".'
        )
    else:
        ok, why = validate_receipt_payload(payload)
        if not ok:
            receipt_refusal = (
                f"review-closure receipt is not acceptable ({why}); re-record "
                "it with the required fields"
            )
        elif not current_head:
            receipt_refusal = (
                "cannot verify the reviewed head: the task clone/HEAD is "
                "unavailable (missing or unreadable clone_path). Completion "
                "fails closed until the head can be verified; keep the clone "
                "retained or fix clone_path."
            )
        else:
            head = str(payload.get("head_sha") or "").strip().lower()
            if current_head != head:
                receipt_refusal = (
                    f"review-closure receipt is stale: it closed out head "
                    f"{head[:12]} but the task clone is now at {current_head[:12]}. "
                    "A commit after the reviewed head invalidates the receipt — "
                    "re-run the review closure (forge: re-trigger the review on "
                    "the new head; platform: new effective delivery identity) and "
                    "record a fresh receipt."
                )
            else:
                return True, ""

    skip = latest_skip(store, task_id)
    if skip is not None:
        skip_head = str(skip.get("head_sha") or "").strip().lower()
        if current_head and skip_head and current_head == skip_head:
            return True, ""
        if not current_head:
            receipt_refusal += (
                " The audited bypass cannot be validated either: the task "
                "clone/HEAD is unavailable."
            )
        else:
            receipt_refusal += (
                f" The audited bypass is expired: it was bound to head "
                f"{(skip_head[:12] or 'unknown')} but the task clone is now at "
                f"{current_head[:12]}; rework requires a fresh closure (or a new "
                "audited bypass recorded on the current head)."
            )
    return False, receipt_refusal
