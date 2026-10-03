import pytest
from unittest.mock import MagicMock

from herdr.observer.signals import Signal, question_for
from herdr.decision.providers.jev import JevDecisionProvider


def test_observer_question_for_removes_string_criteria_and_merges_instructions():
    sig = Signal(
        finding_type="worker_stuck",
        severity="medium",
        summary="agent is stuck in loop",
        suspected_cause="loop",
        recommended_action="restart",
        confidence=0.8,
        evidence=[],
        anchor="a1",
        requires_confirmation=False,
    )
    q = question_for(sig)
    assert "criteria" not in q
    assert "instructions" in q
    assert "agent is stuck in loop" in q["instructions"]
    assert "证据不足或证据不支持时给出低概率" in q["instructions"]


def test_jev_provider_judge_merges_string_criteria():
    provider = JevDecisionProvider()
    provider._ask = MagicMock(return_value=({"q": {"type": "noul", "noul": 0.8}}, 12.0, {}))

    # Passing string criteria to judge (noul question)
    res = provider.judge(
        {"instructions": "Is the task done?", "criteria": "Only say true if tests pass."},
        state="some state",
    )

    assert res.value == 0.8
    assert provider._ask.called
    args, _ = provider._ask.call_args
    asked_req = args[0]
    q_body = asked_req["q"]
    assert q_body["type"] == "noul"
    # String criteria must be merged into instructions, NOT sent as string criteria
    assert "criteria" not in q_body
    assert "Only say true if tests pass." in q_body["instructions"]


def test_jev_provider_judge_preserves_dict_criteria():
    provider = JevDecisionProvider()
    provider._ask = MagicMock(return_value=({"q": {"type": "noul", "noul": 0.9}}, 10.0, {}))

    dict_criteria = {"true": "Clear evidence.", "false": "No evidence."}
    res = provider.judge(
        {"instructions": "Evaluate progress.", "criteria": dict_criteria},
        state="state",
    )

    assert res.value == 0.9
    args, _ = provider._ask.call_args
    asked_req = args[0]
    q_body = asked_req["q"]
    assert q_body["criteria"] == dict_criteria


def test_jev_provider_judge_many_merges_string_criteria():
    provider = JevDecisionProvider()
    provider._ask = MagicMock(return_value=(
        {"q1": {"type": "noul", "noul": 0.7}},
        15.0,
        {},
    ))

    questions = {
        "q1": {
            "instructions": "Candidate focus?",
            "criteria": "Return a probability from 0 to 1.",
        }
    }
    results = provider.judge_many(questions, state="state")
    assert "q1" in results

    args, _ = provider._ask.call_args
    asked_req = args[0]
    q1_body = asked_req["q1"]
    assert q1_body["type"] == "noul"
    assert "criteria" not in q1_body
    assert "Return a probability from 0 to 1." in q1_body["instructions"]
