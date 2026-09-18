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
    """Bounded Agent done report: verdict/blocker/transitions + report tail."""
    parts: List[str] = []
    verdict = task.get("stage_verdict")
    if verdict:
        parts.append(f"stage_verdict={_clean(verdict, 60)}")
    note = task.get("stage_verdict_note")
    if note:
        parts.append(f"verdict_note={_clean(note, 160)}")
    for key in ("blocker", "failure_reason", "error", "message", "last_result"):
        value = task.get(key)
        if value:
            parts.append(f"{key}={_clean(value, 120)}")

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

    tail = _report_tail(report_text if report_text is not None
                        else _read_transcript_tail(task))
    if tail:
        parts.append(f"agent_tail={tail}")

    if not parts:
        return None
    return _clean("; ".join(parts), MAX_REPORT_CHARS)


def collect_execution_evidence(
    task: dict,
    *,
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
    output = summarize_report(task, report_text=report_text)
    if output:
        facts["output_summary"] = output
    return facts


__all__ = [
    "collect_execution_evidence",
    "summarize_git",
    "summarize_loop",
    "summarize_report",
]
