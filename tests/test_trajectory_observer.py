"""Trajectory Observer V1 tests.

Covers the eight required scenarios plus storage, scheduler isolation,
kill-switch, and dedup-key stability:

1. healthy run -> no findings, provider not called
2. consecutive verification failures -> repeated_failure with real evidence
3. runtime unavailable -> runtime_unavailable
4. observer failure -> task/workflow/events untouched
5. repeated observation -> no duplicate findings
6. finding evidence references real event_id / sequence / evidence_id
7. oversized logs are bounded before reaching the provider
8. oversized trajectories are bounded before reaching the provider
"""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from herdr.decision.base import DecisionProvider
from herdr.decision.models import DecisionProviderError, DecisionResult
from herdr.observer import config as observer_config
from herdr.observer import context as observation_context
from herdr.observer import harness as observer_harness
from herdr.observer import signals as observer_signals
from herdr.observer.models import (
    FINDING_TYPES,
    RECOMMENDED_ACTIONS,
    SEVERITIES,
    TrajectoryFinding,
    finding_key_for,
)
from herdr.state_db import (
    get_trajectory_finding,
    list_trajectory_findings,
    record_trajectory_finding,
)
from herdr.state_store import SQLiteStateStore
from herdr.trajectory import TrajectoryLedger


class CapturingProvider(DecisionProvider):
    """Deterministic test provider; records every batched question state."""

    name = "capturing"

    def __init__(self, values: Optional[Dict[str, float]] = None,
                 available: bool = True, raises: bool = False) -> None:
        self.values = dict(values or {})
        self.calls: List[Dict[str, Any]] = []
        self._available = available
        self._raises = raises

    def available(self) -> bool:
        return self._available

    def judge(self, question, state) -> DecisionResult:
        raise NotImplementedError

    def score(self, question, state, levels) -> DecisionResult:
        raise NotImplementedError

    def choose(self, question, state, options) -> DecisionResult:
        raise NotImplementedError

    def judge_many(self, questions, state):
        self.calls.append({"questions": copy.deepcopy(questions), "state": copy.deepcopy(state)})
        if self._raises:
            raise DecisionProviderError("provider exploded", kind="unavailable")
        return {
            question_id: DecisionResult(
                value=self.values.get(question_id, 0.5),
                confidence=0.9,
            )
            for question_id in questions
        }


def _task(task_id: str = "task-1", status: str = "working", *,
          run_id: Optional[str] = None, runtime: Optional[Dict[str, Any]] = None,
          evidence: Optional[str] = None) -> Dict[str, Any]:
    task = {
        "task_id": task_id,
        "workflow_id": "wf-1",
        "node": "implementation",
        "stage": "implementation",
        "agent": "claude",
        "status": status,
        "goal": "implement the feature",
    }
    if run_id:
        task["run_id"] = run_id
    if runtime is not None:
        task["runtime"] = runtime
    if evidence:
        task["evidence"] = evidence
    return task


def _append(ledger: TrajectoryLedger, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [ledger.append_event(event) for event in events]


def _verification(run_id: str, passed: bool, evidence_id: str) -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "event_type": "verification_completed",
        "task_id": "task-1",
        "verification": {
            "type": "tests_completed",
            "passed": passed,
            "evidence_id": evidence_id,
            "failing_count": 0 if passed else 3,
        },
    }


def _base_config() -> Dict[str, Any]:
    config = observer_config.load_config(path="", env={})
    config["enabled"] = True
    config["provider"] = "rule"
    return config


# ---------------------------------------------------------------------------
# Finding storage contract
# ---------------------------------------------------------------------------


class TestFindingStore:
    def test_record_and_list_round_trip(self, tmp_path: Path):
        db_path = tmp_path / "state.db"
        finding = {
            "finding_id": "fnd_1",
            "finding_key": "fk_1",
            "run_id": "run-1",
            "task_id": "task-1",
            "finding_type": "repeated_failure",
            "severity": "warning",
            "summary": "连续验证失败",
            "evidence": [{"type": "verification", "event_id": "evt_2", "sequence": 2}],
            "metadata": {"provider": "test"},
            "created_at": 100.0,
        }
        stored = record_trajectory_finding(finding, db_path=db_path)

        assert stored is not None
        assert stored["finding_id"] == "fnd_1"
        assert stored["finding_type"] == "repeated_failure"
        assert stored["evidence"][0]["event_id"] == "evt_2"
        assert stored["status"] == "open"

        rows = list_trajectory_findings("run-1", db_path=db_path)
        assert [row["finding_key"] for row in rows] == ["fk_1"]
        assert get_trajectory_finding("fk_1", db_path=db_path)["run_id"] == "run-1"

    def test_duplicate_finding_key_is_not_inserted_twice(self, tmp_path: Path):
        db_path = tmp_path / "state.db"
        finding = {
            "finding_id": "fnd_1",
            "finding_key": "fk_dup",
            "run_id": "run-1",
            "finding_type": "runtime_unavailable",
            "severity": "critical",
            "summary": "runtime gone",
            "created_at": 1.0,
        }
        first = record_trajectory_finding(finding, db_path=db_path)
        second = record_trajectory_finding({**finding, "finding_id": "fnd_2"}, db_path=db_path)

        assert first is not None
        assert second is None
        assert len(list_trajectory_findings("run-1", db_path=db_path)) == 1

    def test_list_filters_by_type_and_run(self, tmp_path: Path):
        db_path = tmp_path / "state.db"
        for index, (run_id, finding_type) in enumerate(
            [("run-1", "repeated_failure"), ("run-1", "stalled_execution"), ("run-2", "repeated_failure")]
        ):
            record_trajectory_finding(
                {
                    "finding_id": f"fnd_{index}",
                    "finding_key": f"fk_{index}",
                    "run_id": run_id,
                    "finding_type": finding_type,
                    "severity": "warning",
                    "summary": "x",
                    "created_at": float(index),
                },
                db_path=db_path,
            )

        assert len(list_trajectory_findings("run-1", db_path=db_path)) == 2
        assert len(
            list_trajectory_findings("run-1", finding_type="repeated_failure", db_path=db_path)
        ) == 1
        assert [row["finding_id"] for row in list_trajectory_findings(db_path=db_path)] == [
            "fnd_0", "fnd_1", "fnd_2",
        ]


# ---------------------------------------------------------------------------
# Models and dedup keys
# ---------------------------------------------------------------------------


class TestFindingModel:
    def test_mapping_round_trip_preserves_structured_fields(self):
        finding = TrajectoryFinding(
            finding_id="fnd_x",
            finding_key="fk_x",
            run_id="run-1",
            task_id="task-1",
            finding_type="repeated_failure",
            severity="warning",
            summary="s",
            evidence=[{"type": "trajectory", "event_id": "evt_9", "sequence": 9}],
            confidence=0.82,
            metadata={"provider": "rule"},
            created_at=5.0,
        )
        mapping = finding.to_mapping()
        restored = TrajectoryFinding.from_mapping(mapping)

        assert restored == finding

    def test_invalid_enums_are_rejected(self):
        with pytest.raises(ValueError):
            TrajectoryFinding(run_id="r", finding_type="nonsense", severity="warning", summary="s")
        with pytest.raises(ValueError):
            TrajectoryFinding(run_id="r", finding_type="repeated_failure", severity="nah", summary="s")
        with pytest.raises(ValueError):
            TrajectoryFinding(
                run_id="r", finding_type="repeated_failure", severity="warning",
                summary="s", recommended_action="do_everything",
            )

    def test_finding_key_is_stable_and_anchor_sensitive(self):
        first = finding_key_for("run-1", "stalled_execution", "implementation", "session-1", "evt_7")
        again = finding_key_for("run-1", "stalled_execution", "implementation", "session-1", "evt_7")
        changed_anchor = finding_key_for(
            "run-1", "stalled_execution", "implementation", "session-1", "evt_8"
        )

        assert first == again
        assert first != changed_anchor
        assert first.startswith("fk_")

    def test_enums_expose_required_members(self):
        assert set(FINDING_TYPES) == {
            "stalled_execution", "repeated_failure", "repeated_action", "no_progress",
            "verification_failure", "runtime_unavailable", "possible_context_problem", "other",
        }
        assert set(SEVERITIES) == {"info", "warning", "critical"}
        assert set(RECOMMENDED_ACTIONS) == {
            "continue", "inspect", "replan", "retry", "change_agent", "request_human", "interrupt",
        }


# ---------------------------------------------------------------------------
# Deterministic signals
# ---------------------------------------------------------------------------


def _detect(store: SQLiteStateStore, task: Optional[dict], now: float, config: Optional[dict] = None,
            run_id: str = "run-1", ledger: Optional[TrajectoryLedger] = None):
    ledger = ledger or TrajectoryLedger(store.db_path)
    events = ledger.list_events(run_id)
    runtime = (task or {}).get("runtime") or None
    return observer_signals.detect_signals(
        run_id=run_id,
        events=events,
        task=task,
        runtime=runtime,
        log_tail=None,
        now=now,
        config=config or _base_config(),
    )


class TestSignals:
    def test_healthy_run_produces_no_signals(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 100.0},
            {"run_id": "run-1", "event_type": "agent_started", "timestamp": 101.0},
            {"run_id": "run-1", "event_type": "task_status_changed", "status": "working", "timestamp": 102.0},
            _verification("run-1", True, "tevd-ok"),
        ])
        task = _task(runtime={"status": "running", "agent": "claude"})

        assert _detect(store, task, now=110.0) == []

    def test_repeated_verification_failure_is_detected_with_refs(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        stored = _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 100.0},
            _verification("run-1", False, "tevd-a"),
            _verification("run-1", False, "tevd-b"),
            _verification("run-1", False, "tevd-c"),
        ])
        task = _task(runtime={"status": "running", "agent": "claude"})

        signals = _detect(store, task, now=110.0)
        repeated = [signal for signal in signals if signal.finding_type == "repeated_failure"]

        assert len(repeated) == 1
        signal = repeated[0]
        assert signal.severity == "warning"
        assert signal.recommended_action == "replan"
        assert signal.anchor == stored[1]["event_id"]
        refs = {item.get("event_id") for item in signal.evidence}
        assert stored[1]["event_id"] in refs
        assert {item.get("evidence_id") for item in signal.evidence} >= {"tevd-a", "tevd-b", "tevd-c"}

    def test_runtime_unavailable_is_detected(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 100.0},
            {"run_id": "run-1", "event_type": "agent_started", "timestamp": 101.0},
        ])
        task = _task(runtime={
            "status": "unavailable", "agent": "claude", "pane_id": "pane-1",
            "agent_session_id": "session-1",
        })

        signals = _detect(store, task, now=110.0)
        unavailable = [s for s in signals if s.finding_type == "runtime_unavailable"]

        assert len(unavailable) == 1
        assert unavailable[0].severity == "critical"
        assert unavailable[0].requires_confirmation is False
        assert any(item.get("type") == "runtime" for item in unavailable[0].evidence)

    def test_stall_time_alone_never_produces_critical(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 100.0},
            {"run_id": "run-1", "event_type": "task_status_changed", "status": "working", "timestamp": 200.0},
        ])
        task = _task(runtime={"status": "running", "agent": "claude"})

        signals = _detect(store, task, now=200.0 + 4000.0)
        stalled = [s for s in signals if s.finding_type == "stalled_execution"]

        assert len(stalled) == 1
        assert stalled[0].severity == "warning"
        assert stalled[0].requires_confirmation is True

    def test_stall_is_not_reported_for_finished_or_unavailable_runs(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-done", "event_type": "run_started", "timestamp": 100.0},
            {"run_id": "run-done", "event_type": "run_completed", "timestamp": 200.0},
        ])
        task = _task(status="completed", runtime={"status": "completed", "agent": "claude"})

        assert [s for s in _detect(store, task, now=9000.0, run_id="run-done")
                if s.finding_type == "stalled_execution"] == []

    def test_repeated_action_requires_action_events(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 100.0},
            {"run_id": "run-1", "event_type": "action_failed", "timestamp": 101.0,
             "action": {"command": "pytest tests/test_a.py", "status": "failed"}},
            {"run_id": "run-1", "event_type": "action_failed", "timestamp": 102.0,
             "action": {"command": "pytest tests/test_a.py", "status": "failed"}},
            {"run_id": "run-1", "event_type": "action_failed", "timestamp": 103.0,
             "action": {"command": "pytest tests/test_a.py", "status": "failed"}},
        ])
        task = _task(runtime={"status": "running", "agent": "claude"})

        signals = _detect(store, task, now=110.0)
        repeated = [s for s in signals if s.finding_type == "repeated_action"]

        assert len(repeated) == 1
        assert repeated[0].severity == "warning"

    def test_verification_failure_on_done_claim(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 100.0},
            _verification("run-1", False, "tevd-last"),
            {"run_id": "run-1", "event_type": "task_status_changed", "status": "agent_done",
             "timestamp": 105.0},
        ])
        task = _task(status="agent_done", runtime={"status": "running", "agent": "claude"})

        signals = _detect(store, task, now=110.0)
        failures = [s for s in signals if s.finding_type == "verification_failure"]

        assert len(failures) == 1
        assert failures[0].requires_confirmation is False
        assert any(item.get("evidence_id") == "tevd-last" for item in failures[0].evidence)

    def test_possible_context_problem_from_repeated_log_signature(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 100.0},
            {"run_id": "run-1", "event_type": "agent_started", "timestamp": 101.0},
        ])
        log_tail = {
            "ref": str(tmp_path / "terminal.log"),
            "excerpt": "\n".join([
                "Error: cannot find module 'herdr'",
                "doing work",
                "Error: cannot find module 'herdr'",
                "Error: cannot find module 'herdr'",
            ]),
        }
        signals = observer_signals.detect_signals(
            run_id="run-1",
            events=ledger.list_events("run-1"),
            task=_task(runtime={"status": "running", "agent": "claude"}),
            runtime={"status": "running", "agent": "claude"},
            log_tail=log_tail,
            now=110.0,
            config=_base_config(),
        )
        problems = [s for s in signals if s.finding_type == "possible_context_problem"]

        assert len(problems) == 1
        assert problems[0].requires_confirmation is True
        assert problems[0].evidence[0]["type"] == "log"


# ---------------------------------------------------------------------------
# Bounded context and log tail
# ---------------------------------------------------------------------------


class TestBoundedContext:
    def test_read_log_tail_is_byte_line_and_char_bounded(self, tmp_path: Path):
        log_path = tmp_path / "terminal.log"
        with log_path.open("w", encoding="utf-8") as handle:
            handle.write("HEAD_MARKER\n")
            for index in range(200000):
                handle.write(f"line {index} padding padding padding\n")
            handle.write("TAIL_MARKER\n")
        config = _base_config()
        config.update({"log_tail_lines": 100, "log_tail_bytes": 4096, "log_tail_chars": 600})

        tail = observation_context.read_log_tail(_task(evidence=str(log_path)), config)

        assert tail is not None
        assert tail["ref"] == str(log_path)
        assert "HEAD_MARKER" not in tail["excerpt"]
        assert len(tail["excerpt"]) <= 600
        assert tail["truncated"] is True

    def test_context_bounds_thousand_events_and_preserves_terminal(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        events = [{"run_id": "run-1", "event_type": "run_started", "timestamp": 0.0}]
        events += [
            {"run_id": "run-1", "event_type": "task_status_changed", "status": "working",
             "timestamp": float(index + 1)}
            for index in range(995)
        ]
        events += [
            _verification("run-1", False, "tevd-a"),
            _verification("run-1", False, "tevd-b"),
            _verification("run-1", False, "tevd-c"),
            {"run_id": "run-1", "event_type": "run_failed", "timestamp": 1000.0},
        ]
        _append(ledger, events)
        config = _base_config()
        config["recent_events"] = 50
        all_events = ledger.list_events("run-1")
        task = _task(runtime={"status": "failed", "agent": "claude"})

        ctx = observation_context.build_observation_context(
            run_id="run-1",
            task=task,
            events=all_events,
            runtime=task["runtime"],
            log_tail=None,
            signals=[],
            config=config,
            now=1001.0,
        )

        assert ctx["window"]["total_events"] == 1000
        assert len(ctx["recent_events"]) <= 50
        assert ctx["window"]["truncated"] is True
        serialized = json.dumps(ctx, ensure_ascii=False)
        assert len(serialized) <= config["max_context_size"]
        assert "evt_5" not in serialized
        terminal_types = {event["event_type"] for event in ctx["terminal_events"]}
        assert "run_failed" in terminal_types

    def test_context_redacts_secret_shaped_values(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 1.0,
             "metadata": {"note": "api_key=sk-abcdef1234567890"}},
        ])
        ctx = observation_context.build_observation_context(
            run_id="run-1",
            task=_task(runtime={"status": "running", "agent": "claude"}),
            events=ledger.list_events("run-1"),
            runtime={"status": "running", "agent": "claude"},
            log_tail=None,
            signals=[],
            config=_base_config(),
            now=2.0,
        )

        assert "sk-abcdef1234567890" not in json.dumps(ctx, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Observer engine + harness
# ---------------------------------------------------------------------------


class TestObserverEngine:
    def _observe(self, store, ledger, run_id, task, provider, now=1000.0, config=None):
        return observer_harness.observe_run(
            run_id,
            task=task,
            store=store,
            ledger=ledger,
            provider=provider,
            config=config or _base_config(),
            now=now,
        )

    def test_healthy_run_returns_empty_and_skips_provider(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 100.0},
            {"run_id": "run-1", "event_type": "agent_started", "timestamp": 101.0},
            _verification("run-1", True, "tevd-ok"),
        ])
        provider = CapturingProvider()

        findings = self._observe(
            store, ledger, "run-1", _task(runtime={"status": "running", "agent": "claude"}), provider
        )

        assert findings == []
        assert provider.calls == []
        assert list_trajectory_findings("run-1", db_path=store.db_path) == []

    def test_repeated_failure_produces_evidence_backed_finding(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        stored = _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 100.0},
            _verification("run-1", False, "tevd-a"),
            _verification("run-1", False, "tevd-b"),
            _verification("run-1", False, "tevd-c"),
        ])
        provider = CapturingProvider({"repeated_failure": 0.93})

        findings = self._observe(
            store, ledger, "run-1", _task(runtime={"status": "running", "agent": "claude"}), provider,
            now=110.0,
        )

        assert [finding.finding_type for finding in findings] == ["repeated_failure"]
        finding = findings[0]
        assert finding.severity == "warning"
        assert finding.recommended_action in RECOMMENDED_ACTIONS
        assert finding.confidence == pytest.approx(0.93)
        assert finding.status == "open"
        event_ids = {row["event_id"] for row in stored}
        assert {item["event_id"] for item in finding.evidence} <= event_ids
        assert {item["evidence_id"] for item in finding.evidence} >= {"tevd-a", "tevd-b", "tevd-c"}
        assert all("sequence" in item for item in finding.evidence if item["type"] == "verification")

    def test_weak_signal_requires_model_confirmation(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 100.0},
            {"run_id": "run-1", "event_type": "task_status_changed", "status": "working", "timestamp": 200.0},
        ])
        task = _task(runtime={"status": "running", "agent": "claude"})
        denying = CapturingProvider({"stalled_execution": 0.2})
        confirming = CapturingProvider({"stalled_execution": 0.91})

        denied = self._observe(store, ledger, "run-1", task, denying, now=5000.0)
        confirmed = self._observe(store, ledger, "run-1", task, confirming, now=5000.0)

        assert [finding.finding_type for finding in denied] == []
        assert [finding.finding_type for finding in confirmed] == ["stalled_execution"]
        assert len(confirming.calls) == 1

    def test_runtime_unavailable_survives_without_provider(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 100.0},
            {"run_id": "run-1", "event_type": "agent_started", "timestamp": 101.0},
        ])
        task = _task(runtime={
            "status": "unavailable", "agent": "claude", "pane_id": "pane-9",
            "agent_session_id": "session-9",
        })

        findings = self._observe(store, ledger, "run-1", task, provider=None, now=110.0)

        assert [finding.finding_type for finding in findings] == ["runtime_unavailable"]
        assert findings[0].severity == "critical"

    def test_provider_crash_never_touches_task_workflow_or_events(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 100.0},
            _verification("run-1", False, "tevd-a"),
            _verification("run-1", False, "tevd-b"),
        ])
        task = _task(runtime={"status": "running", "agent": "claude"})
        store.save_task(task)
        events_before = store.list_events(task_id="task-1")
        provider = CapturingProvider(raises=True)

        findings = self._observe(store, ledger, "run-1", task, provider, now=110.0)

        assert [finding.finding_type for finding in findings] == ["repeated_failure"]
        assert store.get_task("task-1")["status"] == "working"
        assert len(store.list_events(task_id="task-1")) == len(events_before)

    def test_observer_internal_failure_returns_empty(self, tmp_path: Path, monkeypatch):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        task = _task(runtime={"status": "running", "agent": "claude"})
        store.save_task(task)

        def explode(*args, **kwargs):
            raise RuntimeError("ledger unavailable")

        monkeypatch.setattr(ledger, "list_events", explode)

        findings = self._observe(
            store, ledger, "run-1", task, CapturingProvider({"repeated_failure": 0.9})
        )

        assert findings == []
        assert store.get_task("task-1")["status"] == "working"

    def test_repeated_observation_does_not_duplicate_findings(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 100.0},
            _verification("run-1", False, "tevd-a"),
            _verification("run-1", False, "tevd-b"),
        ])
        task = _task(runtime={"status": "running", "agent": "claude"})
        values = {"repeated_failure": 0.9}

        first = self._observe(store, ledger, "run-1", task, CapturingProvider(values), now=110.0)
        second = self._observe(store, ledger, "run-1", task, CapturingProvider(values), now=140.0)

        assert len(first) == 1
        assert len(second) == 1
        assert first[0].finding_id == second[0].finding_id
        assert len(list_trajectory_findings("run-1", db_path=store.db_path)) == 1

    def test_oversized_log_never_reaches_provider_unbounded(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 100.0},
            {"run_id": "run-1", "event_type": "agent_started", "timestamp": 101.0},
        ])
        log_path = tmp_path / "terminal.log"
        with log_path.open("w", encoding="utf-8") as handle:
            handle.write("UNIQUE_HEAD_SENTINEL\n")
            for _ in range(50000):
                handle.write("Error: cannot find module 'herdr'\n")
            handle.write("UNIQUE_TAIL_SENTINEL\n")
        config = _base_config()
        config.update({"log_tail_lines": 50, "log_tail_bytes": 2048, "log_tail_chars": 400})
        task = _task(runtime={"status": "running", "agent": "claude"}, evidence=str(log_path))
        provider = CapturingProvider({"possible_context_problem": 0.9})

        findings = self._observe(store, ledger, "run-1", task, provider, now=110.0, config=config)

        assert len(provider.calls) == 1
        state = provider.calls[0]["state"]
        assert state["logs"][0]["ref"] == str(log_path)
        assert len(state["logs"][0]["excerpt"]) <= 400
        assert "UNIQUE_HEAD_SENTINEL" not in json.dumps(state, ensure_ascii=False)
        assert [finding.finding_type for finding in findings] == ["possible_context_problem"]
        log_evidence = [item for item in findings[0].evidence if item["type"] == "log"]
        assert log_evidence and len(log_evidence[0]["excerpt"]) <= 300

    def test_thousand_events_never_reach_provider_complete(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        events = [{"run_id": "run-1", "event_type": "run_started", "timestamp": 0.0}]
        events += [
            {"run_id": "run-1", "event_type": "task_status_changed", "status": "working",
             "timestamp": float(index + 1)}
            for index in range(996)
        ]
        events += [
            _verification("run-1", False, "tevd-a"),
            _verification("run-1", False, "tevd-b"),
            _verification("run-1", False, "tevd-c"),
        ]
        _append(ledger, events)
        config = _base_config()
        config["recent_events"] = 50
        task = _task(runtime={"status": "running", "agent": "claude"})
        provider = CapturingProvider({"repeated_failure": 0.9})

        findings = self._observe(store, ledger, "run-1", task, provider, now=1005.0, config=config)

        assert len(provider.calls) == 1
        state = provider.calls[0]["state"]
        assert state["window"]["total_events"] == 1000
        assert len(state["recent_events"]) <= 50
        assert len(json.dumps(state, ensure_ascii=False)) <= config["max_context_size"]
        assert "evt_5" not in json.dumps(state, ensure_ascii=False)
        assert [finding.finding_type for finding in findings] == ["repeated_failure"]

    def test_kill_switch_disables_observation(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 1.0},
            _verification("run-1", False, "tevd-a"),
            _verification("run-1", False, "tevd-b"),
        ])
        config = _base_config()
        config["enabled"] = False
        provider = CapturingProvider({"repeated_failure": 0.9})

        findings = self._observe(
            store, ledger, "run-1", _task(runtime={"status": "running", "agent": "claude"}),
            provider, config=config,
        )

        assert findings == []
        assert provider.calls == []
        assert list_trajectory_findings("run-1", db_path=store.db_path) == []


class TestHardeningRegressions:
    """Regression coverage added after the independent S6 review (round 1)."""

    def _observe(self, store, ledger, run_id, task, provider, now=1000.0, config=None):
        return observer_harness.observe_run(
            run_id, task=task, store=store, ledger=ledger, provider=provider,
            config=config or _base_config(), now=now,
        )

    def test_log_secrets_never_reach_provider_or_store(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 100.0},
            {"run_id": "run-1", "event_type": "agent_started", "timestamp": 101.0},
        ])
        log_path = tmp_path / "terminal.log"
        log_path.write_text(
            "\n".join([
                "Error: auth failed for api_key=sk-abcdef1234567890secret",
                "Error: auth failed for api_key=sk-abcdef1234567890secret",
                "Error: auth failed for api_key=sk-abcdef1234567890secret",
            ]),
            encoding="utf-8",
        )
        task = _task(runtime={"status": "running", "agent": "claude"}, evidence=str(log_path))
        provider = CapturingProvider({"possible_context_problem": 0.9})

        findings = self._observe(store, ledger, "run-1", task, provider, now=110.0)

        secret = "sk-abcdef1234567890secret"
        provider_state = json.dumps(provider.calls[0]["state"], ensure_ascii=False)
        provider_questions = json.dumps(provider.calls[0]["questions"], ensure_ascii=False)
        persisted = json.dumps(
            list_trajectory_findings("run-1", db_path=store.db_path), ensure_ascii=False
        )
        assert secret not in provider_state
        assert secret not in provider_questions
        assert secret not in persisted
        assert secret not in json.dumps([f.to_mapping() for f in findings], ensure_ascii=False)

    def test_action_command_secrets_never_reach_provider_or_metadata(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        secret = "ghp_abcdef1234567890secret"
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 100.0},
            *[
                {"run_id": "run-1", "event_type": "action_failed", "timestamp": 100.0 + index,
                 "action": {"command": f"curl -H 'token={secret}' api", "status": "failed"}}
                for index in range(3)
            ],
            {"run_id": "run-1", "event_type": "task_status_changed", "status": "working",
             "timestamp": 110.0},
        ])
        task = _task(runtime={"status": "running", "agent": "claude"})
        provider = CapturingProvider({"repeated_action": 0.9})

        findings = self._observe(store, ledger, "run-1", task, provider, now=120.0)

        assert [finding.finding_type for finding in findings] == ["repeated_action"]
        assert secret not in json.dumps(provider.calls[0]["questions"], ensure_ascii=False)
        assert secret not in json.dumps(provider.calls[0]["state"], ensure_ascii=False)
        dumped = json.dumps(
            list_trajectory_findings("run-1", db_path=store.db_path), ensure_ascii=False
        )
        assert secret not in dumped
        assert secret not in json.dumps(findings[0].to_mapping(), ensure_ascii=False)

    def test_cli_observe_is_idempotent_across_processes(self, tmp_path: Path):
        import os
        import subprocess
        import sys

        repo_root = Path(__file__).resolve().parent.parent
        db_path = tmp_path / "state.db"
        store = SQLiteStateStore(db_path)
        _append(TrajectoryLedger(db_path), [
            {"run_id": "run-cli", "event_type": "run_started", "task_id": "task-cli"},
            {"run_id": "run-cli", "event_type": "verification_completed", "task_id": "task-cli",
             "verification": {"type": "tests_completed", "passed": False, "evidence_id": "tevd-1"}},
            {"run_id": "run-cli", "event_type": "verification_completed", "task_id": "task-cli",
             "verification": {"type": "tests_completed", "passed": False, "evidence_id": "tevd-2"}},
        ])
        env = {
            **os.environ,
            "HERDR_STATE_DB": str(db_path),
            "HERDR_OBSERVER_CONFIG": "",
            "HERDR_OBSERVER_ENABLED": "1",
        }
        command = [
            sys.executable, str(repo_root / "bin" / "herdr-task"),
            "observe", "--run-id", "run-cli", "--no-model", "--json",
        ]

        first = subprocess.run(command, capture_output=True, text=True, timeout=60, env=env)
        second = subprocess.run(command, capture_output=True, text=True, timeout=60, env=env)

        assert first.returncode == 0, first.stderr
        assert second.returncode == 0, second.stderr
        first_payload = json.loads(first.stdout)
        second_payload = json.loads(second.stdout)
        assert first_payload["findings"][0]["finding_type"] == "repeated_failure"
        assert (
            first_payload["findings"][0]["finding_id"]
            == second_payload["findings"][0]["finding_id"]
        )
        assert len(list_trajectory_findings("run-cli", db_path=db_path)) == 1

    def test_no_progress_fires_on_rework_loop_and_defers_to_recovery(self, tmp_path: Path):
        config = _base_config()
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        reworks = [
            {"run_id": "run-1", "event_type": "task_status_changed", "status": "rework",
             "timestamp": float(100 + index)}
            for index in range(3)
        ]
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 99.0},
            *reworks,
            {"run_id": "run-1", "event_type": "task_status_changed", "status": "working",
             "timestamp": 104.0},
        ])
        task = _task(runtime={"status": "running", "agent": "claude"})

        signals = _detect(store, task, now=110.0, config=config)
        no_progress = [s for s in signals if s.finding_type == "no_progress"]

        assert len(no_progress) == 1
        assert no_progress[0].requires_confirmation is True
        assert no_progress[0].facts["rework_count"] == 3

        recovery_store = SQLiteStateStore(tmp_path / "state_recovered.db")
        recovery_ledger = TrajectoryLedger(recovery_store.db_path)
        _append(recovery_ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 99.0},
            *reworks,
            _verification("run-1", True, "tevd-recovered"),
        ])
        recovered = _detect(
            recovery_store, task, now=110.0, config=config,
            ledger=recovery_ledger,
        )
        assert [s for s in recovered if s.finding_type == "no_progress"] == []

        artifact_store = SQLiteStateStore(tmp_path / "state_artifact.db")
        artifact_ledger = TrajectoryLedger(artifact_store.db_path)
        _append(artifact_ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 99.0},
            *reworks,
            {"run_id": "run-1", "event_type": "artifact_created", "timestamp": 104.0,
             "artifact": {"ref": "clone/out.md", "kind": "file"}},
        ])
        with_artifact = _detect(
            artifact_store, task, now=110.0, config=config, ledger=artifact_ledger,
        )
        assert [s for s in with_artifact if s.finding_type == "no_progress"] == []

    def test_done_claim_uses_verification_failure_not_repeated_failure(self, tmp_path: Path):
        config = _base_config()
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 100.0},
            _verification("run-1", False, "tevd-a"),
            _verification("run-1", False, "tevd-b"),
            {"run_id": "run-1", "event_type": "task_status_changed", "status": "agent_done",
             "timestamp": 105.0},
        ])
        task = _task(status="agent_done", runtime={"status": "running", "agent": "claude"})

        types = {signal.finding_type for signal in _detect(store, task, now=110.0, config=config)}

        assert "verification_failure" in types
        assert "repeated_failure" not in types

    def test_repeated_action_stays_silent_without_action_events(self, tmp_path: Path):
        config = _base_config()
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 100.0},
            {"run_id": "run-1", "event_type": "task_status_changed", "status": "working",
             "timestamp": 101.0},
        ])

        types = {
            signal.finding_type
            for signal in _detect(store, _task(runtime={"status": "running"}), now=110.0,
                                  config=config)
        }
        assert "repeated_action" not in types

    def test_findings_never_masquerade_as_trajectory_events(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 100.0},
            _verification("run-1", False, "tevd-a"),
            _verification("run-1", False, "tevd-b"),
        ])
        findings = observer_harness.observe_run(
            "run-1",
            task=_task(runtime={"status": "running", "agent": "claude"}),
            store=store,
            ledger=ledger,
            provider=CapturingProvider({"repeated_failure": 0.9}),
            config=_base_config(),
            now=110.0,
        )

        assert findings
        assert len(ledger.list_events("run-1")) == 3
        assert store.list_events(event_type="repeated_failure") == []
        trajectory_event_types = {
            event["event_type"] for event in store.list_events(source="trajectory")
        }
        assert trajectory_event_types == {"run_started", "verification_completed"}

    def test_store_write_failure_still_returns_in_memory_finding(self, tmp_path: Path, monkeypatch):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 100.0},
            _verification("run-1", False, "tevd-a"),
            _verification("run-1", False, "tevd-b"),
        ])

        import herdr.observer.engine as observer_engine

        def explode(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(observer_engine.state_db, "record_trajectory_finding", explode)

        findings = observer_harness.observe_run(
            "run-1",
            task=_task(runtime={"status": "running", "agent": "claude"}),
            store=store,
            ledger=ledger,
            provider=CapturingProvider({"repeated_failure": 0.9}),
            config=_base_config(),
            now=110.0,
        )

        assert [finding.finding_type for finding in findings] == ["repeated_failure"]

    def test_context_with_many_signals_still_fits_budget(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 1.0},
        ])
        config = _base_config()
        config["max_context_size"] = 1200
        many_signals = [
            observer_signals.Signal(
                finding_type=finding_type,
                severity="warning",
                summary="很长的摘要" * 40,
                suspected_cause="原因" * 40,
                recommended_action="inspect",
                confidence=0.5,
                evidence=[{"type": "trajectory", "event_id": f"evt_{index}"}],
                anchor=f"a{index}",
                requires_confirmation=True,
                facts={"blob": "事实" * 60},
            )
            for index, finding_type in enumerate(FINDING_TYPES)
        ]

        ctx = observation_context.build_observation_context(
            run_id="run-1",
            task=_task(runtime={"status": "running", "agent": "claude"}),
            events=ledger.list_events("run-1"),
            runtime={"status": "running", "agent": "claude"},
            log_tail=None,
            signals=many_signals,
            config=config,
            now=5.0,
        )

        serialized = json.dumps(ctx, ensure_ascii=False)
        assert len(serialized) <= config["max_context_size"]

    def test_harness_observe_run_never_raises_on_config_failure(self, monkeypatch):
        import herdr.observer.harness as observer_harness_module

        def explode(*args, **kwargs):
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(observer_harness_module.observer_config, "load_config", explode)

        assert observer_harness_module.observe_run("run-1") == []


# ---------------------------------------------------------------------------
# Scheduler (controller-side, non-blocking, failure isolated)
# ---------------------------------------------------------------------------

class TestObservationScheduler:
    def test_submit_is_non_blocking_and_dedupes_in_flight_runs(self):
        gate = threading.Event()
        release = threading.Event()
        runs: List[str] = []

        def slow_observe(run_id, **kwargs):
            runs.append(run_id)
            gate.set()
            release.wait(timeout=5)
            return []

        scheduler = observer_harness.ObservationScheduler(observe=slow_observe)
        started = time.monotonic()
        first = scheduler.submit("run-1")
        gate.wait(timeout=5)
        elapsed = time.monotonic() - started
        second = scheduler.submit("run-1")
        release.set()
        scheduler.drain(timeout=5)

        assert first is True
        assert second is False
        assert elapsed < 0.5
        assert runs == ["run-1"]

    def test_scheduler_swallows_observer_crash(self):
        def exploding_observe(run_id, **kwargs):
            raise RuntimeError("boom")

        scheduler = observer_harness.ObservationScheduler(observe=exploding_observe)

        assert scheduler.submit("run-1") is True
        scheduler.drain(timeout=5)
        assert scheduler.in_flight() == []

    def test_scheduler_respects_interval_gate(self):
        calls: List[str] = []

        def observe(run_id, **kwargs):
            calls.append(run_id)
            return []

        scheduler = observer_harness.ObservationScheduler(
            observe=observe, config={**_base_config(), "interval": 3600, "max_calls_per_run": 5}
        )
        assert scheduler.submit("run-1", now=1000.0) is True
        scheduler.drain(timeout=5)
        assert scheduler.submit("run-1", now=1010.0) is False
        assert calls == ["run-1"]
