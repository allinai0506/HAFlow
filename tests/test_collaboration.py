"""Collaboration protocol core tests (pure, no I/O).

Covers task Test I (minimal context) + core semantics:
identity stability, type whitelist, status lifecycle, deterministic routing.
"""

import sys
from pathlib import Path

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr import collaboration as collab


def test_supported_types_whitelist():
    assert collab.SUPPORTED_TYPES == frozenset({
        "HANDOFF", "REQUEST", "RESULT", "BLOCKER", "REVIEW_REQUEST", "ACK",
    })


def test_identity_key_stable():
    k1 = collab.identity_key("run-1", "task-a", "task-b", "HANDOFF", "fact-1")
    k2 = collab.identity_key("run-1", "task-a", "task-b", "HANDOFF", "fact-1")
    assert k1 == k2
    assert k1 == "run-1:task-a:task-b:HANDOFF:fact-1"


def test_identity_key_rejects_inference_without_source_fact():
    try:
        collab.identity_key("run-1", "task-a", "task-b", "HANDOFF", "")
    except ValueError:
        pass
    else:
        raise AssertionError("empty source_fact_id must be rejected")


def test_create_handoff_minimal_refs_only():
    ev = collab.create_handoff(
        run_id="run-1",
        workflow_id="wf-1",
        from_task_id="task-a",
        from_agent="developer",
        to_task_id="task-b",
        to_agent="reviewer",
        summary="Rate-limit implementation completed. Review concurrency and crash recovery only.",
        artifact_refs=["commit:abc123"],
        evidence_refs=["test:rate-limit-concurrency"],
        source_fact_id="evt-1",
    )
    assert ev["type"] == "HANDOFF"
    assert ev["status"] == "created"
    assert ev["identity_key"] == "run-1:task-a:task-b:HANDOFF:evt-1"
    assert ev["requires_response"] is True


def test_handoff_prompt_minimal_no_dumps():
    ev = collab.create_handoff(
        run_id="run-1",
        workflow_id="wf-1",
        from_task_id="task-a",
        from_agent="developer",
        to_task_id="task-b",
        to_agent="reviewer",
        summary="Rate-limit done. Review concurrency only.",
        artifact_refs=["commit:abc123"],
        evidence_refs=["test:rate-limit-concurrency"],
        source_fact_id="evt-1",
    )
    prompt = collab.build_handoff_prompt(ev, next_action="Review concurrency and crash recovery.")
    assert "HANDOFF FROM: developer" in prompt
    assert "HANDOFF_ID:" in prompt
    assert "commit:abc123" in prompt
    assert "Review concurrency" in prompt
    for forbidden in ("trajectory", "terminal transcript", "ContextPack", "chain-of-thought"):
        assert forbidden.lower() not in prompt.lower()


def test_handoff_prompt_summary_bounded():
    ev = collab.create_handoff(
        run_id="run-1",
        workflow_id="wf-1",
        from_task_id="task-a",
        from_agent="developer",
        to_task_id="task-b",
        to_agent="reviewer",
        summary="x" * 5000,
        source_fact_id="evt-1",
    )
    prompt = collab.build_handoff_prompt(ev, next_action="Review.")
    assert len(ev["summary"]) <= 500
    assert len(prompt) <= 2000


def test_deterministic_routes_only_three():
    r1 = collab.route_deterministic_handoff(trigger="implementation_completed")
    assert r1 is not None and r1["to_agent"] == "reviewer" and r1["type"] == "HANDOFF"
    r2 = collab.route_deterministic_handoff(trigger="review_completed")
    assert r2 is not None and r2["type"] == "HANDOFF"
    r3 = collab.route_deterministic_handoff(trigger="blocker")
    assert r3 is not None and r3["to_agent"] == "coordinator" and r3["type"] == "BLOCKER"
    assert collab.route_deterministic_handoff(trigger="something_vague") is None


def test_status_lifecycle_valid():
    assert collab.is_valid_transition("created", "dispatched") is True
    assert collab.is_valid_transition("dispatched", "acknowledged") is True
    assert collab.is_valid_transition("acknowledged", "completed") is True
    assert collab.is_valid_transition("created", "failed") is True
    assert collab.is_valid_transition("created", "completed") is False
    assert collab.is_valid_transition("dispatched", "completed") is False


def test_huge_refs_never_amputate_handoff_id():
    ev = collab.create_handoff(
        run_id="run-1",
        workflow_id="wf-1",
        from_task_id="task-a",
        from_agent="developer",
        to_task_id="task-b",
        to_agent="reviewer",
        summary="y" * 5000,
        artifact_refs=["commit:" + "a" * 1000 for _ in range(50)],
        evidence_refs=["test:" + "b" * 1000 for _ in range(50)],
        source_fact_id="evt-1",
    )
    prompt = collab.build_handoff_prompt(ev, next_action="z" * 5000)
    assert len(prompt) <= 2000
    assert f"HANDOFF_ID: {ev['event_id']}" in prompt
