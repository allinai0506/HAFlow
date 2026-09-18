#!/usr/bin/env python3
"""V1 semantic signal definitions (herdr/supervisor/signals.py).

Exactly nine narrow yes/no (noul-style) judgments. Each asks one coherent
question about meaning/quality — never a runtime fact (liveness, exit code,
workspace ids), which belong to the fact layer and must not be delegated.

The questions are provider-neutral: they map onto any DecisionProvider's
judge()/judge_many() (for Jev: one batched noul request).
"""

from __future__ import annotations

from typing import Dict

SIGNAL_DESCRIPTIONS: Dict[str, Dict[str, str]] = {
    "meaningful_progress": {
        "instructions": (
            "Based on the execution facts, did the agent make meaningful "
            "forward progress toward the task goal during the recent period "
            "(real work product, not busywork or repetition)?"
        ),
        "criteria": {
            "true": "New artifacts, code edits, test runs, or decisions clearly advance the goal.",
            "false": "Activity repeats prior attempts, churns without effect, or stalls.",
        },
    },
    "worker_stuck": {
        "instructions": (
            "Is the agent stuck - looping on the same error, re-reading the "
            "same material, or unable to take a next effective step - even "
            "though its session may still be alive?"
        ),
        "criteria": {
            "true": "Repeated identical/oscillating actions with no new progress.",
            "false": "The agent still has untried paths and keeps moving.",
        },
    },
    "work_off_track": {
        "instructions": (
            "Is the current work drifting away from or contradicting the "
            "stated task goal and acceptance criteria (wrong scope, wrong "
            "files, ignoring explicit requirements)?"
        ),
        "criteria": {
            "true": "Recent work no longer serves the goal as stated.",
            "false": "Work remains aligned with the goal and constraints.",
        },
    },
    "requirements_satisfied": {
        "instructions": (
            "Considering the goal, required outputs and acceptance notes, "
            "are the task's substantive requirements satisfied by what has "
            "been produced so far?"
        ),
        "criteria": {
            "true": "Every stated requirement is visibly addressed.",
            "false": "One or more requirements are missing or unmet.",
        },
    },
    "implementation_complete": {
        "instructions": (
            "Is the implementation itself complete - no obvious half-written "
            "code, missing pieces, TODO stubs or unhandled core paths left "
            "for this task's scope?"
        ),
        "criteria": {
            "true": "The deliverable looks finished for the requested scope.",
            "false": "Material parts of the deliverable are absent or partial.",
        },
    },
    "tests_sufficient": {
        "instructions": (
            "Given the test facts (runs, pass/fail, coverage of the change), "
            "is the verification effort sufficient to trust this result for "
            "the task's risk level?"
        ),
        "criteria": {
            "true": "Relevant tests exist, were run, and pass or their gaps are immaterial.",
            "false": "Tests are missing, failing, stale, or do not cover the change.",
        },
    },
    "needs_verification": {
        "instructions": (
            "Should an independent verification step (a different agent or "
            "gate reviewing the result against the goal) happen before this "
            "task is accepted?"
        ),
        "criteria": {
            "true": "Residual doubt is high enough that independent review pays off.",
            "false": "Evidence already closes the loop; extra review adds little.",
        },
    },
    "needs_human": {
        "instructions": (
            "Does this task now require a human decision or intervention - "
            "e.g. repeated failed attempts, ambiguous requirements only a "
            "person can resolve, or risk beyond automated authority?"
        ),
        "criteria": {
            "true": "Continuing autonomously is likely wasteful or unsafe.",
            "false": "The system can proceed without a person.",
        },
    },
    "ready_to_finish": {
        "instructions": (
            "Taking goal, implementation, tests and risk together, is this "
            "task ready to be wrapped up and handed off now?"
        ),
        "criteria": {
            "true": "Accepting and closing the task now would be reasonable.",
            "false": "Substantive work or verification still has to happen.",
        },
    },
}

SIGNAL_NAMES = tuple(SIGNAL_DESCRIPTIONS)


def signal_questions(enabled: Dict[str, bool] = None) -> Dict[str, Dict[str, object]]:
    """Question map for one batched multi-signal evaluation."""
    flags = enabled or {}
    return {
        name: {
            "type": "noul",
            "instructions": spec["instructions"],
            "criteria": dict(spec["criteria"]),
        }
        for name, spec in SIGNAL_DESCRIPTIONS.items()
        if flags.get(name, True)
    }
