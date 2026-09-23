"""Eval Compare contract tests (T2 RED).

Factual before/after diffs; null-safe on either side.
Initially fails: herdr.eval_engine.compare_evals does not exist.
"""

from __future__ import annotations


def _row(run_id="run-a", revision=1, verdict="pass"):
    return {
        "eval_id": f"eval-{run_id}-{revision}",
        "run_id": run_id,
        "revision": revision,
        "verdict": verdict,
        "scores": {"accuracy": 1.0} if verdict == "pass" else {"accuracy": 0.0},
        "evidence": [{"kind": "verification", "ref": f"evt-{revision}"}],
        "created_at": 10.0,
    }


def test_compare_detects_verdict_transition():
    from herdr import eval_engine

    before = _row(run_id="run-a", revision=1, verdict="pass")
    after = _row(run_id="run-b", revision=1, verdict="fail")
    diff = eval_engine.compare_evals(before, after)
    assert diff["verdict_changed"] is True
    assert diff["verdict_transition"] == "pass->fail"
    assert diff["before"]["verdict"] == "pass"
    assert diff["after"]["verdict"] == "fail"


def test_compare_identical_is_no_change():
    from herdr import eval_engine

    before = _row(verdict="pass")
    after = _row(verdict="pass")
    diff = eval_engine.compare_evals(before, after)
    assert diff["verdict_changed"] is False
    assert diff["verdict_transition"] == "pass->pass"


def test_compare_null_safe_on_either_side():
    from herdr import eval_engine

    after = _row(verdict="pass")
    diff = eval_engine.compare_evals(None, after)
    assert diff["verdict_changed"] is True
    assert diff["verdict_transition"] == "null->pass"

    before = _row(verdict="fail")
    diff2 = eval_engine.compare_evals(before, None)
    assert diff2["verdict_changed"] is True
    assert diff2["verdict_transition"] == "fail->null"

    diff3 = eval_engine.compare_evals(None, None)
    assert diff3["verdict_changed"] is False
    assert diff3["verdict_transition"] == "null->null"


def test_compare_reports_warning_deltas():
    from herdr import eval_engine

    before = _row(verdict=None)
    before["warnings"] = ["run_incomplete"]
    after = _row(verdict="pass")
    after["warnings"] = []
    diff = eval_engine.compare_evals(before, after)
    assert "run_incomplete" in diff["warnings_removed"]
    assert diff["warnings_added"] == []
