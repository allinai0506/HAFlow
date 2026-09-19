#!/usr/bin/env python3
"""Execution evidence summaries (herdr/supervisor/evidence.py).

Functional Core: turns HAFlow's real execution artifacts into bounded,
redacted facts for ``SupervisorState``. Reuses the canonical producers:

- HERDR loop report -> ``<clone>/.herdr-loop/`` (STATE.md via
  ``evaluator.read_state``, METRICS.json test/lint/score counts);
- git workspace -> ``git status`` / ``git diff --stat`` bounded summary;
- Agent done report -> task verdict/blocker/status_history plus a bounded
  tail of the report text the orchestrator supplies (pane tail / transcript).

Never returns source code, full diffs or full stdout: every string is
truncated and credential-redacted before it can reach a provider.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..evaluator import LOOP_DIR_NAME, read_state
from ..projection import strip_ansi_codes
from .state import redact_text

MAX_FILES = 8
MAX_FILE_CHARS = 80
MAX_FAILING = 5
MAX_REPORT_LINES = 4
MAX_REPORT_LINE_CHARS = 120
MAX_REPORT_CHARS = 400
MAX_STAT_CHARS = 160
GIT_TIMEOUT = 3.0
TRANSCRIPT_TAIL_BYTES = 16384

_STAT_RE = re.compile(
    r"(\d+) files? changed"
    r"(?:, (\d+) insertions?\(\+\))?"
    r"(?:, (\d+) deletions?\(-\))?"
)


def _clean(value: Any, limit: int) -> str:
    text = redact_text(str(value)).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _run_git(clone_path: str, args: List[str]) -> Optional[str]:
    """Best-effort bounded git read; None on any failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(clone_path)] + list(args),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout or ""


def summarize_loop(clone_path: Optional[str]) -> Optional[Dict[str, Any]]:
    """HERDR loop report + test result counts, bounded to a small dict."""
    if not clone_path:
        return None
    loop_dir = Path(clone_path) / LOOP_DIR_NAME
    if not loop_dir.is_dir():
        return None

    facts: Dict[str, Any] = {}
    try:
        state = read_state(loop_dir)
    except Exception:
        state = {}
    if isinstance(state, dict):
        if state.get("status") not in (None, "unknown"):
            facts["loop_status"] = _clean(state.get("status"), 40)
        iteration = _int_or_none(state.get("iteration"))
        if iteration is not None:
            facts["iteration"] = iteration
        max_iterations = _int_or_none(state.get("max_iterations"))
        if max_iterations is not None:
            facts["max_iterations"] = max_iterations
        if state.get("converged") is not None:
            facts["converged"] = bool(state.get("converged"))
    if (loop_dir / "BLOCKER.md").exists():
        facts["blocker_report"] = True

    try:
        metrics = json.loads((loop_dir / "METRICS.json").read_text(encoding="utf-8"))
    except Exception:
        metrics = None
    if isinstance(metrics, dict):
        for key in ("total_tests", "passed_tests", "lint_errors", "type_errors",
                    "composite_score"):
            value = metrics.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                facts[key] = round(float(value), 2)
        failing = metrics.get("failing_tests")
        if isinstance(failing, list):
            facts["failing_count"] = len(failing)
            if failing:
                facts["failing_tests"] = [
                    _clean(name, MAX_FILE_CHARS) for name in failing[:MAX_FAILING]
                ]
        if metrics.get("has_repro_test"):
            facts["has_repro_test"] = True

    return facts or None


def build_test_evidence_id(test_evidence: Dict[str, Any]) -> str:
    """Stable, cross-process test evidence fingerprint (sha256).

    Builds a canonical, sorted JSON string of key test metrics and computes
    sha256. Does NOT rely on Python's process-local hash().
    """
    canonical = {
        "composite_score": round(float(test_evidence.get("composite_score") or 0.0), 2),
        "converged": bool(test_evidence.get("converged", False)),
        "failing_count": int(test_evidence.get("failing_count") or len(test_evidence.get("failing_tests") or [])),
        "failing_tests": sorted(str(t) for t in (test_evidence.get("failing_tests") or [])),
        "iteration": int(test_evidence.get("iteration") or 0),
        "lint_errors": int(test_evidence.get("lint_errors") or 0),
        "passed_tests": int(test_evidence.get("passed_tests") or 0),
        "total_tests": int(test_evidence.get("total_tests") or 0),
        "type_errors": int(test_evidence.get("type_errors") or 0),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"tevd-{hashlib.sha256(encoded).hexdigest()[:16]}"


def extract_test_evidence(clone_path: Optional[str]) -> Optional[Dict[str, Any]]:
    """Extract bounded test evidence exclusively from .herdr-loop/EVAL_DONE.json.

    EVAL_DONE.json is the atomic evaluation snapshot written by bin/herdr-loop
    as its very last action in run_evaluation().  Because it is replaced
    atomically (rename over the old file), a reader always sees either the
    *complete* previous iteration's snapshot OR the *complete* new iteration's
    snapshot — never a mix of old STATE.md + new METRICS.json.

    METRICS.json and STATE.md are NOT read here; they continue to serve
    herdr-loop display and BLOCKER reporting.

    Returns None if:
    - clone_path is invalid or .herdr-loop does not exist;
    - EVAL_DONE.json is absent or unparseable (loop has not run yet);
    - Snapshot looks like an un-evaluated placeholder (iteration 0, no tests).
    """
    if not clone_path:
        return None
    loop_dir = Path(clone_path) / LOOP_DIR_NAME
    if not loop_dir.is_dir():
        return None

    # Single-file atomic read — no cross-file consistency checks needed.
    snap_path = loop_dir / "EVAL_DONE.json"
    try:
        snap = json.loads(snap_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(snap, dict):
        return None

    # Require at least the iteration and total_tests fields from a full snapshot
    # (old thin sentinels that only had {"iteration", "ts"} will miss these).
    if "total_tests" not in snap:
        return None

    iteration = _int_or_none(snap.get("iteration")) or 0
    total_tests = _int_or_none(snap.get("total_tests")) or 0
    passed_tests = _int_or_none(snap.get("passed_tests")) or 0
    lint_errors = _int_or_none(snap.get("lint_errors")) or 0
    type_errors = _int_or_none(snap.get("type_errors")) or 0
    composite_score = round(float(snap.get("composite_score") or 0.0), 2)
    converged = bool(snap.get("converged", False))
    failing_tests = snap.get("failing_tests") or []
    if not isinstance(failing_tests, list):
        failing_tests = []
    failing_count = len(failing_tests)

    # Un-evaluated placeholder: iteration 0, no tests, zero score.
    if iteration == 0 and total_tests == 0 and composite_score == 0.0 and not failing_tests:
        return None

    evidence_data: Dict[str, Any] = {
        "iteration": iteration,
        "max_iterations": _int_or_none(snap.get("max_iterations")) or 5,
        "converged": converged,
        "loop_status": _clean(snap.get("status") or "unknown", 40),
        "total_tests": total_tests,
        "passed_tests": passed_tests,
        "failing_count": failing_count,
        "failing_tests": [_clean(str(t), MAX_FILE_CHARS) for t in failing_tests[:MAX_FAILING]],
        "lint_errors": lint_errors,
        "type_errors": type_errors,
        "composite_score": composite_score,
        "has_repro_test": bool(snap.get("has_repro_test")),
    }
    evidence_data["evidence_id"] = build_test_evidence_id(evidence_data)
    return evidence_data


def compute_test_progress(
    current: Optional[Dict[str, Any]],
    previous: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Deterministic delta math between current and previous test results."""
    if not current:
        return None
    progress: Dict[str, Any] = {}
    curr_passed = current.get("passed_tests")
    curr_failed = current.get("failing_count")
    if curr_failed is None and "failing_tests" in current:
        curr_failed = len(current.get("failing_tests") or [])
    curr_score = current.get("composite_score")

    if curr_passed is not None:
        progress["current_passed"] = int(curr_passed)
    if curr_failed is not None:
        progress["current_failed"] = int(curr_failed)
    if curr_score is not None:
        progress["current_score"] = round(float(curr_score), 2)

    if isinstance(previous, dict):
        prev_passed = previous.get("passed_tests")
        prev_failed = previous.get("failing_count")
        if prev_failed is None and "failing_tests" in previous:
            prev_failed = len(previous.get("failing_tests") or [])
        prev_score = previous.get("composite_score")

        if prev_passed is not None and curr_passed is not None:
            progress["previous_passed"] = int(prev_passed)
            progress["passed_delta"] = int(curr_passed) - int(prev_passed)
        if prev_failed is not None and curr_failed is not None:
            progress["previous_failed"] = int(prev_failed)
            progress["failed_delta"] = int(curr_failed) - int(prev_failed)
        if prev_score is not None and curr_score is not None:
            progress["previous_score"] = round(float(prev_score), 2)
            progress["score_delta"] = round(float(curr_score) - float(prev_score), 2)

    return progress or None


def summarize_git(clone_path: Optional[str],
                  run: Optional[Callable[[str, List[str]], Optional[str]]] = None
                  ) -> Optional[Dict[str, Any]]:
    """Bounded git workspace summary: counts + stat line, never a diff body."""
    if not clone_path or not os.path.isdir(clone_path):
        return None
    runner = run or _run_git

    status = runner(clone_path, ["status", "--porcelain"])
    if status is None:
        return None
    files = [line[3:].strip() for line in status.splitlines() if line.strip()]

    stat_text = runner(clone_path, ["diff", "--stat", "HEAD"]) or ""
    summary_line = stat_text.strip().splitlines()[-1] if stat_text.strip() else ""

    if not files and not summary_line:
        return None

    summary: Dict[str, Any] = {
        "files_changed": len(files),
        "sample_files": [_clean(name, MAX_FILE_CHARS) for name in files[:MAX_FILES]],
    }
    if summary_line:
        summary["stat"] = _clean(summary_line, MAX_STAT_CHARS)
        match = _STAT_RE.search(summary_line)
        if match:
            summary["insertions"] = int(match.group(2) or 0)
            summary["deletions"] = int(match.group(3) or 0)
    return summary


def _read_transcript_tail(task: dict) -> Optional[str]:
    candidates = []
    evidence_path = task.get("evidence")
    if evidence_path:
        candidates.append(Path(str(evidence_path)))
    task_id = task.get("task_id")
    if task_id:
        candidates.append(
            Path.home() / ".herdr-controller" / "logs" / "tasks" / str(task_id) / "terminal.log"
        )
    for path in candidates:
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
            with path.open("rb") as handle:
                if size > TRANSCRIPT_TAIL_BYTES:
                    handle.seek(-TRANSCRIPT_TAIL_BYTES, os.SEEK_END)
                return handle.read().decode("utf-8", "replace")
        except OSError:
            continue
    return None


def _report_tail(report_text: Optional[str]) -> Optional[str]:
    clean = strip_ansi_codes(report_text or "")
    if not clean.strip():
        return None
    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    picked: List[str] = []
    for line in reversed(lines):
        if re.fullmatch(r"[-=_*#>\s]{0,12}", line):
            continue
        picked.append(_clean(line, MAX_REPORT_LINE_CHARS))
        if len(picked) >= MAX_REPORT_LINES:
            break
    if not picked:
        return None
    return " | ".join(reversed(picked))


def summarize_report(task: dict,
                     report_text: Optional[str] = None) -> Optional[str]:
    """Bounded Agent done report: report tail + transitions + failure fields.

    The actual agent output tail comes first so the 400-char budget can never
    truncate it away behind task-record fields (stage_verdict/blocker already
    live top-level in SupervisorState and are not duplicated here).
    """
    parts: List[str] = []

    tail = _report_tail(report_text if report_text is not None
                        else _read_transcript_tail(task))
    if tail:
        parts.append(f"agent_tail={tail}")

    history = task.get("status_history")
    if isinstance(history, list) and history:
        transitions = []
        for entry in history[-3:]:
            if not isinstance(entry, dict):
                continue
            row = f"{entry.get('from')}->{entry.get('to')}"
            reason = entry.get("reason")
            if reason:
                row += f"({_clean(reason, 40)})"
            transitions.append(row)
        if transitions:
            parts.append("recent_transitions=" + ", ".join(transitions))

    note = task.get("stage_verdict_note")
    if note:
        parts.append(f"verdict_note={_clean(note, 160)}")
    for key in ("failure_reason", "error", "message", "last_result"):
        value = task.get(key)
        if value:
            parts.append(f"{key}={_clean(value, 120)}")

    if not parts:
        return None
    return _clean("; ".join(parts), MAX_REPORT_CHARS)


def collect_execution_evidence(
    task: dict,
    *,
    trigger: Optional[str] = None,
    report_text: Optional[str] = None,
    git_runner: Optional[Callable[[str, List[str]], Optional[str]]] = None,
) -> Dict[str, Any]:
    """All bounded execution facts for one checkpoint (no raw payloads)."""
    facts: Dict[str, Any] = {}
    clone_path = task.get("clone_path")

    tests = summarize_loop(clone_path)
    if tests:
        facts["tests"] = tests
    diff = summarize_git(clone_path, run=git_runner)
    if diff:
        facts["diff_summary"] = diff
    # tests_completed checkpoint only needs tests + git summary, skips heavy transcript/terminal reads
    if trigger != "tests_completed":
        output = summarize_report(task, report_text=report_text)
        if output:
            facts["output_summary"] = output
    return facts


__all__ = [
    "build_test_evidence_id",
    "collect_execution_evidence",
    "compute_test_progress",
    "extract_test_evidence",
    "summarize_git",
    "summarize_loop",
    "summarize_report",
]
