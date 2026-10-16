"""Shared wrapup review-closure invariant (Codex review, wf-haflow-1015-01).

The wrapup node's rules require the delivery PR and its CI/review findings to
be closed out before completion. Prompt text alone is bypassable, so this
module owns the machine-checked invariant used by every completion source:
the CLI ``set`` path and the controller's CAS auto-accept path — both funnel
through :func:`herdr.kernel.transition_task`.

An acceptable receipt carries, in one ``review_closure_recorded`` event:

- the 40-hex head SHA it certifies — a commit after that head makes the
  receipt **stale** and forces a fresh closure round;
- for a normal closure: the delivery PR URL, ``ci_status=pass`` and a
  non-empty findings disposition;
- for a deferred closure (the PR genuinely cannot be created): a concrete
  reason carrying the blocking evidence, plus the same head SHA.

A ``review_closure_skipped`` event (written by the audited
``--skip-review-closure --note`` bypass) also allows completion.
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


def skipped_recently(store, task_id: str) -> bool:
    return _latest_event(store, task_id, "review_closure_skipped") is not None


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
    if not str(payload.get("findings") or "").strip():
        return False, "--findings must state the unresolved P1/P2 counts and disposition"
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


def closure_required(task: Any) -> bool:
    """True when the workflow's pinned configuration opts into the contract.

    Legacy workflows and custom templates whose wrapup node predates the
    contract keep their previous behaviour — the invariant must not brick
    upgrades with repeated auto-accept refusals (Codex review P1). The opt-in
    marker is the explicit ``review_closure_required`` capability on the
    wrapup node of the task's pinned workflow configuration.
    """
    path = str((task or {}).get("workflow_config") or "").strip()
    if not path:
        return False
    try:
        with open(path, "r", encoding="utf-8") as stream:
            config = json.load(stream)
    except (OSError, ValueError):
        return False
    for node in (config.get("nodes") or []):
        if isinstance(node, dict) and (node.get("id") or node.get("key")) == "wrapup":
            return bool(node.get("review_closure_required"))
    return False


def check_completion_allowed(store, task: Any) -> Tuple[bool, str]:
    """(ok, refusal_reason) for a wrapup completion transition."""
    task_id = str((task or {}).get("task_id") or "")
    if not closure_required(task):
        # Not opted in (legacy workflow / custom template): previous behaviour.
        return True, ""
    if skipped_recently(store, task_id):
        return True, ""
    payload = latest_receipt(store, task_id)
    if payload is None:
        return False, (
            "wrapup completion requires a review-closure receipt. Run "
            f"`herdr-task record-review-closure {task_id} --pr-url <url> "
            '--ci-status pass --findings "<conclusions>" --head-sha <head>` '
            '(or `--deferred --reason "<evidence>"`); audited emergency '
            'bypass: --skip-review-closure --note "<reason>".'
        )
    ok, why = validate_receipt_payload(payload)
    if not ok:
        return False, (
            f"review-closure receipt is not acceptable ({why}); re-record it "
            "with the required fields"
        )
    head = str(payload.get("head_sha") or "").strip().lower()
    current = clone_head_sha((task or {}).get("clone_path"))
    if current and current != head:
        return False, (
            f"review-closure receipt is stale: it closed out head {head[:12]} "
            f"but the task clone is now at {current[:12]}. A commit after the "
            "reviewed head invalidates the receipt — re-run the review closure "
            "(forge: re-trigger the review on the new head; platform: new "
            "effective delivery identity) and record a fresh receipt."
        )
    return True, ""
