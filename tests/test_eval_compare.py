"""Eval compare returns only the four factual before/after values."""

from __future__ import annotations

FACT_FIELDS = {
    "requirements_satisfied",
    "verification_passed",
    "human_intervention_count",
    "final_status",
}


def test_compare_contains_only_four_facts_and_no_ranking():
    from herdr import eval_engine

    before = {
        "requirements_satisfied": None,
        "verification_passed": True,
        "human_intervention_count": 2,
        "final_status": "working",
        "scores": {"score": 100},
        "verdict": "pass",
    }
    after = {
        "requirements_satisfied": True,
        "verification_passed": True,
        "human_intervention_count": 1,
        "final_status": "completed",
        "winner": "after",
        "rank": 1,
    }

    diff = eval_engine.compare_evals(before, after)

    assert set(diff) == {"before", "after"}
    assert set(diff["before"]) == FACT_FIELDS
    assert set(diff["after"]) == FACT_FIELDS
    assert diff["before"]["requirements_satisfied"] is None
    assert diff["after"]["final_status"] == "completed"


def test_compare_null_safe_preserving_unknown_values():
    from herdr import eval_engine

    diff = eval_engine.compare_evals(None, {"verification_passed": False})
    assert diff["before"] == dict.fromkeys(FACT_FIELDS)
    assert diff["after"]["verification_passed"] is False
    assert set(diff["after"]) == FACT_FIELDS
