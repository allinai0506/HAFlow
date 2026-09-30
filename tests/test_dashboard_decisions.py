#!/usr/bin/env python3
"""Dashboard must surface "waiting on a human" decisions (herdr/dashboard.py).

Before this the dashboard only ever counted blocked / failed / escalated
tasks, so a coordinator's open question (Barrier-0 rulings such as DU-10)
was invisible in the console even though it blocked the whole wave.
"""

import re

from herdr import dashboard as d

CLOCK_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def _task(tid, status="working", updated=1700000000, **kw):
    t = {
        "task_id": tid,
        "workflow_id": "wf-1",
        "node": "implementation",
        "stage": "implementation",
        "agent": "codex",
        "status": status,
        "goal": f"goal-{tid}",
        "created_at": updated - 100,
        "updated_at": updated,
        "last_activity_at": updated,
    }
    t.update(kw)
    return t


def test_decisions_section_present_with_real_clocks():
    payload = d.build_dashboard(
        [_task("t-1")],
        decisions=[{
            "decision_id": "DU-10",
            "status": "open",
            "title": "DU-10",
            "question": "MATCH 是否入 V1？",
            "options": ["入", "不入"],
            "recommended": "入",
            "workflow_id": "wf-1",
            "node": "implementation",
            "task_id": "",
            "source": "human",
            "note_id": "n-1",
            "raised_at": 1700003000,
        }],
        now=1700003600.0,
    )
    assert "decisions" in payload
    assert payload["counts"]["decisions"] == 1
    item = payload["decisions"][0]
    assert item["decision_id"] == "DU-10"
    assert item["question"] == "MATCH 是否入 V1？"
    assert CLOCK_RE.match(item["raised_at_text"]), item
    # No endpoint/payload: a ruling needs a human-written `decision` value, so
    # there is nothing a one-click executor could legitimately send.
    assert "endpoint" not in item
    assert "payload" not in item
    # The console must know which workflow to open, since decisions are
    # workflow-scoped and the dashboard can span many.
    assert item["workflow_id"] == "wf-1"


def test_open_decisions_count_into_attention():
    payload = d.build_dashboard(
        [_task("t-ok")],
        decisions=[{
            "decision_id": "DU-10", "question": "入?",
            "workflow_id": "wf-1", "raised_at": 1700003000,
            "options": [], "recommended": "",
        }],
        now=1700003600.0,
    )
    ids = {a.get("decision_id") for a in payload["attention"]}
    assert "DU-10" in ids
    entry = next(a for a in payload["attention"] if a.get("decision_id") == "DU-10")
    assert entry["default_action"], "an open decision must carry a default action"
    assert entry["default_action_text"]


def test_no_decisions_keeps_existing_shape():
    payload = d.build_dashboard([_task("t-ok")], now=1700003600.0)
    assert payload["decisions"] == []
    assert payload["counts"]["decisions"] == 0
    assert "decisions" in payload["counts"]


def test_decisions_are_bounded_and_ordered_newest_first():
    decisions = [
        {"decision_id": f"D{i}", "question": f"q{i}", "workflow_id": "wf-1",
         "raised_at": 1700000000 + i}
        for i in range(10)
    ]
    payload = d.build_dashboard([], decisions=decisions, now=1700003600.0)
    got = [x["decision_id"] for x in payload["decisions"]]
    assert got[:3] == ["D9", "D8", "D7"]


def test_decisions_do_not_mutate_input():
    decisions = [{"decision_id": "D1", "question": "q", "workflow_id": "wf-1",
                  "raised_at": 1, "options": ["a"]}]
    d.build_dashboard([], decisions=decisions, now=2.0)
    assert decisions[0] == {"decision_id": "D1", "question": "q",
                            "workflow_id": "wf-1", "raised_at": 1,
                            "options": ["a"]}
