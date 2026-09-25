"""impl-fix8 regression: supersede_delivery_note precondition arity (B1)."""
from __future__ import annotations

import pytest

from herdr import delivery_record, workflow_docs


def _seed_delivery(workflow_id: str) -> dict:
    return workflow_docs.append_note(
        workflow_id,
        kind="delivery",
        title="delivery branch@abc123",
        body="seed",
        node="wrapup",
        task_id="review-1",
        agent="",
        source=workflow_docs.SOURCE_CONTROLLER,
        fields={
            "delivery_id": "candidate-abc123",
            "delivery_branch": "branch",
            "candidate_sha": "abc123",
            "review_task": "review-1",
            "test_gate": "gate-1",
        },
    )


def test_supersede_delivery_note_replaces_known_candidate(
    tmp_path, monkeypatch,
):
    """Direct call must not raise TypeError; known supersedes appends."""
    monkeypatch.setenv(workflow_docs.DOCS_DIR_ENV, str(tmp_path / "shared"))
    workflow_id = "wf-fix8-supersede-ok"
    _seed_delivery(workflow_id)
    record = delivery_record.supersede_delivery_note(
        workflow_id,
        supersedes="candidate-abc123",
        delivery_branch="branch",
        candidate_sha="def456",
        review_task="review-1",
        test_gate="gate-1",
    )
    assert record["kind"] == "delivery"
    assert record.get("supersedes") == "candidate-abc123"
    assert record.get("candidate_sha") == "def456"


def test_supersede_delivery_note_unknown_target_fail_closed(
    tmp_path, monkeypatch,
):
    """Unknown supersedes must fail closed, not TypeError or silent append."""
    monkeypatch.setenv(workflow_docs.DOCS_DIR_ENV, str(tmp_path / "shared"))
    workflow_id = "wf-fix8-supersede-unknown"
    _seed_delivery(workflow_id)
    with pytest.raises(delivery_record.DeliveryIdentityError):
        delivery_record.supersede_delivery_note(
            workflow_id,
            supersedes="unknown-id-zzz",
            delivery_branch="branch",
            candidate_sha="fff999",
            review_task="review-1",
            test_gate="gate-1",
        )
