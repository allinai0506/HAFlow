#!/usr/bin/env python3
"""Human decision intake (herdr/human_decisions.py).

The console could not remind the human about anything the coordinator wrote
as free text.  These tests pin the fold that turns the append-only
``workflow_docs`` ledger into an explicit "waiting on you" list:

- only notes carrying a ``decision_id`` are asks (a plain ``kind=decision``
  note is a *record* of a decision already taken, not a new question);
- the newest note per ``decision_id`` wins, so a resolution closes the ask;
- a stale (fix-loop invalidated) ask is not a live ask;
- the coordinator's latest advice stays readable as an ordered timeline.
"""

from herdr import human_decisions as hd


def _note(nid, ts, kind="decision", **fields):
    note = {
        "note_id": nid,
        "ts": ts,
        "workflow_id": "wf-001",
        "kind": kind,
        "title": f"note {nid}",
        "body": "  正文第一段。\n第二段。  ",
        "node": "implementation",
        "source": "agent",
        "task_id": "",
        "agent": "opencode",
    }
    note.update(fields)
    return note


def test_collect_open_decisions_folds_by_decision_id():
    notes = [
        _note("n-1", 100.0, decision_id="DU-10", decision_status="open",
              question="MATCH 是否入 V1？", options=["入", "不入"], recommended="入"),
    ]
    items = hd.collect_open_decisions(notes)
    assert len(items) == 1
    item = items[0]
    assert item["decision_id"] == "DU-10"
    assert item["question"] == "MATCH 是否入 V1？"
    assert item["options"] == ["入", "不入"]
    assert item["recommended"] == "入"
    assert item["status"] == "open"
    assert item["workflow_id"] == "wf-001"
    assert item["raised_at"] == 100.0
    assert item["title"] == "note n-1"


def test_decision_note_without_id_is_a_record_not_an_ask():
    notes = [_note("n-1", 100.0, kind="decision", title="Wave-2 归属裁定")]
    assert hd.collect_open_decisions(notes) == []


def test_latest_note_wins_and_resolution_closes_the_ask():
    notes = [
        _note("n-1", 100.0, decision_id="DU-10", question="入?"),
        _note("n-2", 200.0, decision_id="DU-10", decision_status="resolved",
              question="入?", decision="入"),
    ]
    assert hd.collect_open_decisions(notes) == []


def test_resolution_then_reopen_shows_the_reopened_ask():
    notes = [
        _note("n-1", 100.0, decision_id="DU-10", question="入?"),
        _note("n-2", 200.0, decision_id="DU-10", decision_status="resolved", question="入?"),
        _note("n-3", 300.0, decision_id="DU-10", question="范围变了，重新确认"),
    ]
    items = hd.collect_open_decisions(notes)
    assert len(items) == 1
    assert items[0]["question"] == "范围变了，重新确认"


def test_stale_ask_is_not_a_live_ask():
    notes = [
        _note("n-1", 100.0, decision_id="DU-10", question="入?", stale=True,
              stale_reason="fix-loop invalidation"),
    ]
    assert hd.collect_open_decisions(notes) == []


def test_options_accept_json_string_and_blank_entries_are_dropped():
    notes = [
        _note("n-1", 100.0, decision_id="D1", options='["a", "", "b"]'),
        _note("n-2", 100.0, decision_id="D2", options="", recommended=""),
    ]
    items = {i["decision_id"]: i for i in hd.collect_open_decisions(notes)}
    assert items["D1"]["options"] == ["a", "b"]
    assert items["D2"]["options"] == []
    assert items["D2"]["recommended"] == ""


def test_missing_optional_fields_never_raise():
    items = hd.collect_open_decisions([{"decision_id": "D1"}])
    assert items[0]["title"] == ""
    assert items[0]["node"] == ""
    assert items[0]["raised_at"] == 0.0
    assert items[0]["options"] == []


def test_question_falls_back_to_body_excerpt():
    """A coordinator writing only `--text` must still produce a readable ask."""
    items = hd.collect_open_decisions([
        _note("n-1", 1.0, decision_id="D1", body="MATCH 是否入 V1？\n第二段。"),
    ])
    assert items[0]["question"] == "MATCH 是否入 V1？ 第二段。"
    assert items[0]["detail"] == items[0]["question"]


def test_collect_open_decisions_is_bounded_and_newest_first():
    notes = [
        _note(f"n-{i}", float(i), decision_id=f"D{i}", question=f"q{i}")
        for i in range(10)
    ]
    items = hd.collect_open_decisions(notes, limit=3)
    assert [i["decision_id"] for i in items] == ["D9", "D8", "D7"]


def test_collect_advice_returns_newest_first_without_asking_questions():
    notes = [
        _note("n-1", 100.0, kind="plan", title="方案 Barrier 重排"),
        _note("n-2", 200.0, kind="evidence", title="排障恢复：可续接"),
        _note("n-3", 300.0, kind="decision", decision_id="D1", question="q"),
    ]
    advice = hd.collect_advice(notes, limit=5)
    assert [a["note_id"] for a in advice] == ["n-3", "n-2", "n-1"]
    assert advice[0]["is_decision"] is True
    assert advice[1]["is_decision"] is False
    # body excerpt is whitespace-collapsed and bounded for the UI
    assert "\n" not in advice[1]["summary"]
    assert len(advice[1]["summary"]) <= hd.ADVICE_SUMMARY_LIMIT


def test_collect_advice_never_invents_content():
    assert hd.collect_advice([]) == []
    assert hd.collect_advice([{"kind": "plan"}])[0]["title"] == ""


def test_advice_kinds_alias_the_authoritative_ledger():
    """A second hand-maintained list would hide a newly added note kind."""
    from herdr import workflow_docs

    assert set(hd.ADVICE_KINDS) == set(workflow_docs.NOTE_KINDS)

    original = workflow_docs.NOTE_KINDS + ("brand_new_kind",)
    try:
        workflow_docs.NOTE_KINDS = original
        import importlib

        importlib.reload(hd)
        items = importlib.reload(hd).collect_advice(
            [_note("n-1", 1.0, kind="brand_new_kind", title="新种类条目")]
        )
        assert [a["title"] for a in items] == ["新种类条目"]
    finally:
        workflow_docs.NOTE_KINDS = tuple(n for n in original if n != "brand_new_kind")
        import importlib

        importlib.reload(hd)


def test_invalid_status_is_coerced_to_open_not_dropped():
    """Fail-safe: an unrecognized status must surface, never silently hide."""
    items = hd.collect_open_decisions([
        _note("n-1", 1.0, decision_id="D1", question="q", decision_status="banana"),
    ])
    assert len(items) == 1
    assert items[0]["status"] == hd.STATUS_OPEN


def test_identical_ts_latest_note_still_wins():
    notes = [
        _note("n-1", 5.0, decision_id="D1", question="旧问题"),
        _note("n-2", 5.0, decision_id="D1", question="新问题",
              decision_status="resolved"),
    ]
    assert hd.collect_open_decisions(notes) == []


def test_build_decision_fields_round_trip_through_note_fields():
    fields = hd.build_decision_fields(
        "DU-10", "open", question="MATCH 入 V1?", options=["入", "不入"],
        recommended="入",
    )
    assert fields["decision_id"] == "DU-10"
    assert fields["decision_status"] == "open"
    assert fields["question"] == "MATCH 入 V1?"
    # options must survive the flat, single-line note `fields` contract
    assert '"入"' in fields["options"]
    items = hd.collect_open_decisions([_note("n-1", 1.0, **fields)])
    assert items[0]["options"] == ["入", "不入"]


def test_build_decision_fields_rejects_blank_decision_id():
    try:
        hd.build_decision_fields("  ", "open")
    except ValueError as exc:
        assert "decision_id" in str(exc)
    else:  # pragma: no cover - contract violation
        raise AssertionError("blank decision_id must be rejected")
