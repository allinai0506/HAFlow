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
import multiprocessing
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
    upsert_trajectory_finding,
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
    config["live_probe"] = False  # hermetic tests: host processes never call herdr
    return config


def _mp_record_worker(
    db_path: str, finding_key: str, local_finding_id: str, barrier, results
) -> None:
    """Spawned worker: race two INSERTs of the same finding_key."""
    try:
        barrier.wait(timeout=30)
        row = upsert_trajectory_finding(
            {
                "finding_id": local_finding_id,
                "finding_key": finding_key,
                "run_id": "run-race",
                "finding_type": "repeated_failure",
                "severity": "warning",
                "summary": f"local {local_finding_id}",
                "created_at": 1.0,
            },
            db_path=Path(db_path),
        )
        results.put(
            {"ok": True, "returned": (row or {}).get("finding_id"), "local": local_finding_id}
        )
    except Exception as exc:  # pragma: no cover - surfaced through the queue
        results.put({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def _mp_observe_worker(db_path: str, run_id: str, barrier, results) -> None:
    """Spawned worker: race full observations of the same run."""
    try:
        store = SQLiteStateStore(Path(db_path))
        barrier.wait(timeout=30)
        findings = observer_harness.observe_run(
            run_id, store=store, config=_base_config(), use_model=False,
        )
        persisted = list_trajectory_findings(run_id, db_path=Path(db_path))
        results.put({
            "ok": True,
            "returned": [finding.finding_id for finding in findings],
            "persisted": [row["finding_id"] for row in persisted],
        })
    except Exception as exc:  # pragma: no cover - surfaced through the queue
        results.put({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


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
        stored = upsert_trajectory_finding(finding, db_path=db_path)

        assert stored is not None
        assert stored["finding_id"] == "fnd_1"
        assert stored["finding_type"] == "repeated_failure"
        assert stored["evidence"][0]["event_id"] == "evt_2"
        assert stored["status"] == "open"

        rows = list_trajectory_findings("run-1", db_path=db_path)
        assert [row["finding_key"] for row in rows] == ["fk_1"]
        assert get_trajectory_finding("fk_1", db_path=db_path)["run_id"] == "run-1"

    def test_duplicate_finding_key_returns_canonical_persisted_row(self, tmp_path: Path):
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
        first = upsert_trajectory_finding(finding, db_path=db_path)
        second = upsert_trajectory_finding({**finding, "finding_id": "fnd_2"}, db_path=db_path)

        assert first is not None and first["finding_id"] == "fnd_1"
        assert second is not None  # canonical persisted row, not the losing local one
        assert second["finding_id"] == "fnd_1"
        assert len(list_trajectory_findings("run-1", db_path=db_path)) == 1

    def test_list_filters_by_type_and_run(self, tmp_path: Path):
        db_path = tmp_path / "state.db"
        for index, (run_id, finding_type) in enumerate(
            [("run-1", "repeated_failure"), ("run-1", "stalled_execution"), ("run-2", "repeated_failure")]
        ):
            upsert_trajectory_finding(
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

    def test_no_progress_uses_latest_episode_boundary(self, tmp_path: Path):
        config = _base_config()
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        stored = _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "timestamp": 99.0},
            {"run_id": "run-1", "event_type": "task_status_changed", "status": "rework",
             "timestamp": 100.0},
            _verification("run-1", True, "tevd-pass"),
            {"run_id": "run-1", "event_type": "task_status_changed", "status": "rework",
             "timestamp": 102.0},
            {"run_id": "run-1", "event_type": "task_status_changed", "status": "rework",
             "timestamp": 103.0},
            {"run_id": "run-1", "event_type": "task_status_changed", "status": "rework",
             "timestamp": 104.0},
            {"run_id": "run-1", "event_type": "task_status_changed", "status": "working",
             "timestamp": 105.0},
        ])
        task = _task(runtime={"status": "running", "agent": "claude"})

        signals = _detect(store, task, now=110.0, config=config)
        no_progress = [signal for signal in signals if signal.finding_type == "no_progress"]

        assert len(no_progress) == 1
        assert no_progress[0].facts["rework_count"] == 3
        # anchor is the first rework of the CURRENT episode, not the historic one
        assert no_progress[0].anchor == f"first_rework:{stored[3]['event_id']}"

    def test_no_progress_episode_boundary_end_to_end(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        stored = _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "task_id": "task-1",
             "timestamp": 99.0},
            {"run_id": "run-1", "event_type": "task_status_changed", "status": "rework",
             "task_id": "task-1", "timestamp": 100.0},
            _verification("run-1", True, "tevd-pass"),
            {"run_id": "run-1", "event_type": "task_status_changed", "status": "rework",
             "task_id": "task-1", "timestamp": 102.0},
            {"run_id": "run-1", "event_type": "task_status_changed", "status": "rework",
             "task_id": "task-1", "timestamp": 103.0},
            {"run_id": "run-1", "event_type": "task_status_changed", "status": "rework",
             "task_id": "task-1", "timestamp": 104.0},
        ])
        task = _task(run_id="run-1", runtime={"status": "running", "agent": "claude"})
        store.save_task(task)

        findings = observer_harness.observe_run(
            "run-1", task=task, store=store, config=_base_config(),
            provider=CapturingProvider({"no_progress": 0.9}), now=110.0,
        )

        assert [finding.finding_type for finding in findings] == ["no_progress"]
        assert findings[0].metadata["anchor"] == f"first_rework:{stored[3]['event_id']}"
        assert len(list_trajectory_findings("run-1", db_path=store.db_path)) == 1

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

        monkeypatch.setattr(observer_engine.state_db, "upsert_trajectory_finding", explode)

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


def _run_spawned_workers(worker, per_worker_args: List[tuple]) -> List[Dict[str, Any]]:
    """Run one real OS process per arg tuple, all racing on a shared barrier."""
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(len(per_worker_args))
    results = context.Queue()
    processes = [
        context.Process(target=worker, args=(*worker_args, barrier, results))
        for worker_args in per_worker_args
    ]
    for process in processes:
        process.start()
    try:
        payloads = [results.get(timeout=60) for _ in processes]
    except Exception:
        for process in processes:
            process.terminate()
        raise
    for process in processes:
        process.join(timeout=30)
        assert not process.is_alive(), "spawned worker did not finish"
    return payloads


# ---------------------------------------------------------------------------
# run_id-only resolution (no task argument required)
# ---------------------------------------------------------------------------


class TestRunIdOnlyResolution:
    def test_runtime_unavailable_resolved_from_run_id_without_task(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "task_id": "task-1",
             "timestamp": 100.0},
            {"run_id": "run-1", "event_type": "agent_started", "task_id": "task-1",
             "timestamp": 101.0},
        ])
        store.save_task(_task(run_id="run-1", runtime={
            "status": "unavailable", "agent": "claude",
            "agent_session_id": "session-r", "pane_id": "pane-r",
        }))

        findings = observer_harness.observe_run(
            "run-1", store=store, config=_base_config(), use_model=False,
        )

        assert [finding.finding_type for finding in findings] == ["runtime_unavailable"]
        assert findings[0].severity == "critical"
        assert findings[0].task_id == "task-1"

    def test_evidence_log_is_read_via_run_id_only(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "task_id": "task-1",
             "timestamp": 100.0},
        ])
        log_path = tmp_path / "terminal.log"
        log_path.write_text(
            "\n".join(["Error: cannot find module 'herdr'"] * 3), encoding="utf-8",
        )
        store.save_task(_task(
            run_id="run-1", runtime={"status": "running", "agent": "claude"},
            evidence=str(log_path),
        ))
        provider = CapturingProvider({"possible_context_problem": 0.9})

        findings = observer_harness.observe_run(
            "run-1", store=store, config=_base_config(), provider=provider,
        )

        assert provider.calls, "provider must be asked after the log signal"
        assert provider.calls[0]["state"]["logs"][0]["ref"] == str(log_path)
        assert [finding.finding_type for finding in findings] == ["possible_context_problem"]

    def test_task_found_by_persisted_run_id_when_events_carry_no_task_id(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        TrajectoryLedger(store.db_path).append_event({
            "run_id": "run-1", "event_type": "run_started", "timestamp": 100.0,
        })
        store.save_task(_task(
            run_id="run-1", runtime={"status": "unavailable", "agent": "claude"},
        ))

        findings = observer_harness.observe_run(
            "run-1", store=store, config=_base_config(), use_model=False,
        )

        assert [finding.finding_type for finding in findings] == ["runtime_unavailable"]

    def test_stale_run_events_never_borrow_another_runs_runtime(self, tmp_path: Path):
        # task was re-launched as run-new; old run-old events still carry task-1
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-old", "event_type": "run_started", "task_id": "task-1",
             "timestamp": 100.0},
            _verification("run-old", False, "tevd-a"),
            _verification("run-old", False, "tevd-b"),
        ])
        store.save_task(_task(
            run_id="run-new",
            runtime={"status": "unavailable", "agent": "claude", "pane_id": "pane-new"},
        ))

        findings = observer_harness.observe_run(
            "run-old", store=store, config=_base_config(), use_model=False,
        )

        assert [finding.finding_type for finding in findings] == ["repeated_failure"]
        assert all("runtime" not in item.get("type", "") for f in findings for item in f.evidence)

    def test_read_failure_falls_back_to_already_persisted_canonical_finding(
        self, tmp_path: Path, monkeypatch,
    ):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "task_id": "task-1",
             "timestamp": 100.0},
            _verification("run-1", False, "tevd-a"),
            _verification("run-1", False, "tevd-b"),
        ])
        store.save_task(_task(run_id="run-1", runtime={"status": "running", "agent": "claude"}))
        canonical = observer_harness.observe_run(
            "run-1", store=store, config=_base_config(), use_model=False,
        )
        assert len(canonical) == 1
        persisted_id = canonical[0].finding_id

        import herdr.observer.engine as observer_engine

        def explode(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(observer_engine.state_db, "upsert_trajectory_finding", explode)

        findings = observer_harness.observe_run(
            "run-1", store=store, config=_base_config(), use_model=False,
        )

        assert [finding.finding_id for finding in findings] == [persisted_id]


# ---------------------------------------------------------------------------
# Concurrent dedup (true multiprocessing)
# ---------------------------------------------------------------------------


class TestConcurrentDedup:
    def test_concurrent_same_key_records_converge_on_canonical_finding_id(self, tmp_path: Path):
        db_path = tmp_path / "state.db"
        SQLiteStateStore(db_path)  # ensure schema before forking workers

        payloads = _run_spawned_workers(
            _mp_record_worker,
            [(str(db_path), "fk_race", f"fnd_local_{index}") for index in range(4)],
        )

        assert all(payload["ok"] for payload in payloads), payloads
        returned = {payload["returned"] for payload in payloads}
        rows = list_trajectory_findings("run-race", db_path=db_path)
        assert len(rows) == 1
        assert returned == {rows[0]["finding_id"]}

    def test_concurrent_observe_run_converges_on_one_persisted_finding(self, tmp_path: Path):
        db_path = tmp_path / "state.db"
        store = SQLiteStateStore(db_path)
        ledger = TrajectoryLedger(db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "task_id": "task-1",
             "timestamp": 100.0},
            _verification("run-1", False, "tevd-a"),
            _verification("run-1", False, "tevd-b"),
            _verification("run-1", False, "tevd-c"),
        ])
        store.save_task(_task(run_id="run-1", runtime={"status": "running", "agent": "claude"}))

        payloads = _run_spawned_workers(
            _mp_observe_worker, [(str(db_path), "run-1")] * 4,
        )

        assert all(payload["ok"] for payload in payloads), payloads
        rows = list_trajectory_findings("run-1", db_path=db_path)
        assert len(rows) == 1
        canonical_id = rows[0]["finding_id"]
        for payload in payloads:
            assert payload["returned"] == [canonical_id]
            assert payload["persisted"] == [canonical_id]


# ---------------------------------------------------------------------------
# Live runtime probe (persisted vs live availability)
# ---------------------------------------------------------------------------


class _StubRunner:
    """Deterministic stand-in for the bounded herdr subprocess runner."""

    def __init__(self, mapping: Dict[str, Any]) -> None:
        self.mapping = mapping
        self.calls: List[tuple] = []

    def __call__(self, argv, timeout=None):
        self.calls.append((list(argv), timeout))
        joined = " ".join(argv)
        for key, result in self.mapping.items():
            if key in joined:
                if isinstance(result, Exception):
                    raise result
                return result
        return (1, "", "no stub for command")


def _pane_payload(*pane_ids: str) -> str:
    return json.dumps({"result": {"panes": [{"pane_id": pane_id} for pane_id in pane_ids]}})


def _pane_info(session: Optional[str] = None, pane_id: str = "pane-x") -> str:
    pane: Dict[str, Any] = {"pane_id": pane_id}
    if session is not None:
        pane["agent_session"] = session
    return json.dumps({"result": {"pane": pane}})


def _agent_info(status: str = "working", session: Optional[str] = None) -> str:
    agent: Dict[str, Any] = {"agent_status": status, "agent": "claude"}
    if session is not None:
        agent["agent_session"] = session
    return json.dumps({"result": {"agent": agent}})


def _error_payload(code: str) -> str:
    return json.dumps({"error": {"code": code, "message": code}})


class TestLiveRuntimeProbe:
    def test_live_unavailable_with_persisted_running_fires_runtime_unavailable(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "task_id": "task-1",
             "timestamp": 100.0},
            {"run_id": "run-1", "event_type": "agent_started", "task_id": "task-1",
             "timestamp": 101.0},
        ])
        task = _task(run_id="run-1", runtime={
            "status": "running", "agent": "claude", "pane_id": "pane-live",
            "agent_session_id": "session-live",
        })
        store.save_task(task)
        provider = CapturingProvider({"runtime_unavailable": 0.95})

        findings = observer_harness.observe_run(
            "run-1",
            task=task,
            store=store,
            config=_base_config(),
            provider=provider,
            runtime_probe=lambda probe_task: {"status": "unavailable", "reason": "pane_missing",
                                              "pane_id": "pane-live"},
        )

        assert [finding.finding_type for finding in findings] == ["runtime_unavailable"]
        assert findings[0].severity == "critical"
        assert any(item.get("type") == "runtime_live" for item in findings[0].evidence)
        context = provider.calls[0]["state"]
        assert context["runtime"]["persisted"]["status"] == "running"
        assert context["runtime"]["live"]["status"] == "unavailable"

    def test_live_probe_exception_or_unknown_never_fabricates_unavailable(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "task_id": "task-1",
             "timestamp": 100.0},
            {"run_id": "run-1", "event_type": "agent_started", "task_id": "task-1",
             "timestamp": 101.0},
        ])
        task = _task(run_id="run-1", runtime={
            "status": "running", "agent": "claude", "pane_id": "pane-live",
        })
        store.save_task(task)
        events_before = store.list_events(task_id="task-1")

        def exploding_probe(_task_arg):
            raise RuntimeError("herdr unreachable")

        failed = observer_harness.observe_run(
            "run-1", task=task, store=store, config=_base_config(),
            provider=CapturingProvider({"runtime_unavailable": 0.99}),
            runtime_probe=exploding_probe,
        )
        unknown = observer_harness.observe_run(
            "run-1", task=task, store=store, config=_base_config(),
            provider=CapturingProvider({"runtime_unavailable": 0.99}),
            runtime_probe=lambda probe_task: {"status": "unknown", "reason": "probe_timeout"},
        )

        assert failed == []
        assert unknown == []
        assert store.get_task("task-1")["status"] == "working"
        assert len(store.list_events(task_id="task-1")) == len(events_before)

    def test_probe_live_runtime_validates_identity(self):
        from herdr.observer import live as observer_live

        session_task = _task(runtime={
            "status": "running", "pane_id": "pane-x", "workspace_id": "ws-1",
            "agent_session_id": "session-A", "agent_name": "agent-x",
        })
        legacy_task = _task(runtime={"status": "running", "pane_id": "pane-x",
                                     "workspace_id": "ws-1"})

        missing = observer_live.probe_live_runtime(
            session_task,
            runner=_StubRunner({
                "pane list": (0, _pane_payload("other"), ""),
                "pane get": (1, _error_payload("pane_not_found"), ""),
            }),
        )
        match = observer_live.probe_live_runtime(
            session_task,
            runner=_StubRunner({
                "pane list": (0, _pane_payload("pane-x"), ""),
                "pane get": (0, _pane_info("session-A"), ""),
            }),
        )
        mismatch = observer_live.probe_live_runtime(
            session_task,
            runner=_StubRunner({
                "pane list": (0, _pane_payload("pane-x"), ""),
                "pane get": (0, _pane_info("session-B"), ""),
            }),
        )
        agent_not_found = observer_live.probe_live_runtime(
            session_task,
            runner=_StubRunner({
                "pane list": (0, _pane_payload("pane-x"), ""),
                "pane get": (0, _pane_info(), ""),
                "agent get": (1, _error_payload("agent_not_found"), ""),
            }),
        )
        insufficient = observer_live.probe_live_runtime(
            session_task,
            runner=_StubRunner({
                "pane list": (0, _pane_payload("pane-x"), ""),
                "pane get": (0, _pane_info(), ""),
                "agent get": (0, _agent_info("idle"), ""),
            }),
        )
        agent_garbage = observer_live.probe_live_runtime(
            session_task,
            runner=_StubRunner({
                "pane list": (0, _pane_payload("pane-x"), ""),
                "pane get": (0, _pane_info(), ""),
                "agent get": (0, "not json", ""),
            }),
        )
        agent_schema_drift = observer_live.probe_live_runtime(
            session_task,
            runner=_StubRunner({
                "pane list": (0, _pane_payload("pane-x"), ""),
                "pane get": (0, _pane_info(), ""),
                "agent get": (0, json.dumps({"result": {}}), ""),
            }),
        )
        stale_workspace = observer_live.probe_live_runtime(
            legacy_task,
            runner=_StubRunner({
                "pane list": (0, _pane_payload("other"), ""),
                "pane get": (0, _pane_info("session-A"), ""),
            }),
        )
        list_failed = observer_live.probe_live_runtime(
            session_task, runner=_StubRunner({"pane list": (1, "", "daemon down")}),
        )
        timed_out = observer_live.probe_live_runtime(
            session_task, runner=_StubRunner({"pane list": TimeoutError("timeout")}),
        )
        unparseable = observer_live.probe_live_runtime(
            session_task,
            runner=_StubRunner({
                "pane list": (0, _pane_payload("pane-x"), ""),
                "pane get": (0, "not json", ""),
            }),
        )
        no_pane = observer_live.probe_live_runtime(
            _task(runtime={"status": "running"}), runner=_StubRunner({}),
        )

        assert missing["status"] == "unavailable" and missing["reason"] == "pane_missing"
        assert match["status"] == "available" and match["reason"] == "identity_match"
        assert mismatch["status"] == "unavailable" and mismatch["reason"] == "identity_mismatch"
        assert agent_not_found["status"] == "unavailable"
        assert agent_not_found["reason"] == "agent_not_found"
        assert insufficient["status"] == "unknown"
        assert insufficient["reason"] == "insufficient_identity"
        assert agent_garbage["status"] == "unknown"
        assert agent_garbage["reason"] == "agent_payload_invalid"
        assert agent_schema_drift["status"] == "unknown"
        assert agent_schema_drift["reason"] == "agent_payload_invalid"
        assert stale_workspace["status"] == "available"
        assert stale_workspace["workspace_mismatch"] is True
        assert list_failed["status"] == "unknown"
        assert timed_out["status"] == "unknown"
        assert unparseable["status"] == "unknown"
        assert no_pane["status"] == "unknown"


# ---------------------------------------------------------------------------
# Runtime identity guard (probe + transcript must never cross runs)
# ---------------------------------------------------------------------------


class TestRuntimeIdentityGuard:
    def _setup(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "task_id": "task-1",
             "timestamp": 100.0},
            {"run_id": "run-1", "event_type": "agent_started", "task_id": "task-1",
             "timestamp": 101.0},
        ])
        task = _task(run_id="run-1", runtime={
            "status": "running", "agent": "claude", "pane_id": "pane-x",
            "workspace_id": "ws-1", "agent_session_id": "session-A",
        })
        store.save_task(task)
        return store, task

    def _observe(self, store, task, stub):
        from herdr.observer import live as observer_live

        config = {**_base_config(), "live_probe": True}
        return observer_harness.observe_run(
            "run-1",
            task=task,
            store=store,
            config=config,
            use_model=False,
            runtime_probe=lambda probe_task: observer_live.probe_live_runtime(
                probe_task, runner=stub,
            ),
            transcript_reader=lambda reader_task: observer_live.read_live_transcript(
                reader_task, config, runner=stub,
            ),
        )

    @staticmethod
    def _pane_read_called(stub) -> bool:
        return any(list(argv[:2]) == ["pane", "read"] for argv, _ in stub.calls)

    def test_identity_mismatch_marks_runtime_unavailable_and_skips_pane_read(self, tmp_path: Path):
        store, task = self._setup(tmp_path)
        stub = _StubRunner({
            "pane list": (0, _pane_payload("pane-x"), ""),
            "pane get": (0, _pane_info("session-B"), ""),
            "pane read": (0, "Error: cannot find module 'herdr'\n" * 3, ""),
        })

        findings = self._observe(store, task, stub)

        assert [finding.finding_type for finding in findings] == ["runtime_unavailable"]
        assert any(item.get("type") == "runtime_live" and item.get("reason") == "identity_mismatch"
                   for item in findings[0].evidence)
        assert not self._pane_read_called(stub)

    def test_agent_not_found_marks_runtime_unavailable(self, tmp_path: Path):
        store, task = self._setup(tmp_path)
        stub = _StubRunner({
            "pane list": (0, _pane_payload("pane-x"), ""),
            "pane get": (0, _pane_info(), ""),
            "agent get": (1, _error_payload("agent_not_found"), ""),
            "pane read": (0, "should not be read", ""),
        })

        findings = self._observe(store, task, stub)

        assert [finding.finding_type for finding in findings] == ["runtime_unavailable"]
        assert any(item.get("reason") == "agent_not_found" for item in findings[0].evidence)
        assert not self._pane_read_called(stub)

    def test_unknown_identity_is_never_unavailable_and_never_reads_pane(self, tmp_path: Path):
        store, task = self._setup(tmp_path)
        timed_out = _StubRunner({
            "pane list": TimeoutError("identity probe timeout"),
            "pane read": (0, "should not be read", ""),
        })
        unparseable = _StubRunner({
            "pane list": (0, _pane_payload("pane-x"), ""),
            "pane get": (0, "not json", ""),
            "pane read": (0, "should not be read", ""),
        })

        assert self._observe(store, task, timed_out) == []
        assert self._observe(store, task, unparseable) == []
        assert not self._pane_read_called(timed_out)
        assert not self._pane_read_called(unparseable)
        assert store.get_task("task-1")["status"] == "working"


# ---------------------------------------------------------------------------
# Live pane transcript (live preferred, evidence file fallback)
# ---------------------------------------------------------------------------


class TestLiveTranscript:
    def test_live_transcript_enables_context_finding_without_evidence_file(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "task_id": "task-1",
             "timestamp": 100.0},
            {"run_id": "run-1", "event_type": "agent_started", "task_id": "task-1",
             "timestamp": 101.0},
        ])
        task = _task(run_id="run-1", runtime={"status": "running", "agent": "claude",
                                              "pane_id": "pane-live"})
        store.save_task(task)
        assert not task.get("evidence")
        provider = CapturingProvider({"possible_context_problem": 0.9})

        findings = observer_harness.observe_run(
            "run-1",
            task=task,
            store=store,
            config=_base_config(),
            provider=provider,
            transcript_reader=lambda reader_task: {
                "ref": (
                    "pane:"
                    + str(
                        reader_task.get("pane_id")
                        or (reader_task.get("runtime") or {}).get("pane_id")
                    )
                ),
                "excerpt": "\n".join(["Error: cannot find module 'herdr'"] * 3),
                "truncated": False,
            },
        )

        assert [finding.finding_type for finding in findings] == ["possible_context_problem"]
        assert provider.calls[0]["state"]["logs"][0]["ref"] == "pane:pane-live"
        assert findings[0].evidence[0]["ref"] == "pane:pane-live"

    def test_live_transcript_is_preferred_over_evidence_file(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "task_id": "task-1",
             "timestamp": 100.0},
        ])
        stale_log = tmp_path / "terminal.log"
        stale_log.write_text("all good here\n", encoding="utf-8")
        task = _task(run_id="run-1", runtime={"status": "running", "agent": "claude",
                                              "pane_id": "pane-live"},
                     evidence=str(stale_log))
        store.save_task(task)
        provider = CapturingProvider({"possible_context_problem": 0.9})

        observer_harness.observe_run(
            "run-1", task=task, store=store, config=_base_config(), provider=provider,
            transcript_reader=lambda reader_task: {
                "ref": "pane:pane-live",
                "excerpt": "\n".join(["Error: cannot find module 'herdr'"] * 3),
                "truncated": False,
            },
        )

        assert provider.calls[0]["state"]["logs"][0]["ref"] == "pane:pane-live"

    def test_transcript_failure_falls_back_to_evidence_file(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "task_id": "task-1",
             "timestamp": 100.0},
        ])
        log_path = tmp_path / "terminal.log"
        log_path.write_text("\n".join(["Error: cannot find module 'herdr'"] * 3), encoding="utf-8")
        task = _task(run_id="run-1", runtime={"status": "running", "agent": "claude",
                                              "pane_id": "pane-live"},
                     evidence=str(log_path))
        store.save_task(task)
        provider = CapturingProvider({"possible_context_problem": 0.9})

        def exploding_reader(_task_arg):
            raise RuntimeError("pane read timeout")

        observer_harness.observe_run(
            "run-1", task=task, store=store, config=_base_config(), provider=provider,
            transcript_reader=exploding_reader,
        )

        assert provider.calls[0]["state"]["logs"][0]["ref"] == str(log_path)

    def test_default_live_transcript_is_bounded_and_redacted(self):
        from herdr.observer import live as observer_live

        secret = "ghp_abcdef1234567890secret"
        raw = "\n".join([f"line {index} padding" for index in range(5000)])
        raw += f"\nError: token={secret} denied\n" * 3
        identity_stub = {"pane get": (0, _pane_info("sess-9", pane_id="pane-9"), "")}
        runner = _StubRunner({**identity_stub, "pane read": (0, raw, "")})
        config = {
            **_base_config(),
            "log_tail_lines": 50,
            "log_tail_chars": 300,
            "log_tail_bytes": 4096,
        }
        task = _task(runtime={"pane_id": "pane-9", "agent_session_id": "sess-9"})

        tail = observer_live.read_live_transcript(task, config, runner=runner)
        failed = observer_live.read_live_transcript(
            task, config,
            runner=_StubRunner({**identity_stub, "pane read": (1, "", "gone")}),
        )
        crashed = observer_live.read_live_transcript(
            task, config,
            runner=_StubRunner({**identity_stub, "pane read": TimeoutError("timeout")}),
        )

        assert tail is not None
        assert tail["ref"] == "pane:pane-9"
        assert len(tail["excerpt"]) <= 300
        assert tail["truncated"] is True
        assert secret not in tail["excerpt"]
        assert failed is None and crashed is None

    def test_oversized_live_transcript_secret_never_reaches_provider_or_db(self, tmp_path: Path):
        from herdr.observer import live as observer_live

        secret = "ghp_abcdef1234567890secret"
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "task_id": "task-1",
             "timestamp": 100.0},
        ])
        task = _task(run_id="run-1", runtime={"status": "running", "agent": "claude",
                                              "pane_id": "pane-9",
                                              "agent_session_id": "sess-9"})
        store.save_task(task)
        raw = "\n".join([f"Error: token={secret} denied"] * 5000)
        config = {**_base_config(), "log_tail_lines": 40, "log_tail_chars": 200}
        provider = CapturingProvider({"possible_context_problem": 0.9})
        identity_stub = {"pane get": (0, _pane_info("sess-9", pane_id="pane-9"), "")}

        findings = observer_harness.observe_run(
            "run-1", task=task, store=store, config=config, provider=provider,
            transcript_reader=lambda reader_task: observer_live.read_live_transcript(
                reader_task, config,
                runner=_StubRunner({**identity_stub, "pane read": (0, raw, "")}),
            ),
        )

        provider_dump = json.dumps(provider.calls[0]["state"], ensure_ascii=False)
        db_dump = json.dumps(
            list_trajectory_findings("run-1", db_path=store.db_path), ensure_ascii=False,
        )
        assert secret not in provider_dump
        assert secret not in db_dump
        assert findings and len(
            [item for item in findings[0].evidence if item["type"] == "log"][0]["excerpt"]
        ) <= 300


# ---------------------------------------------------------------------------
# Finding escalation (same episode, same finding_id, stronger evidence)
# ---------------------------------------------------------------------------


class TestFindingEscalation:
    def test_same_episode_escalates_in_place(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        stored = _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "task_id": "task-1",
             "timestamp": 100.0},
            _verification("run-1", False, "tevd-1"),
            _verification("run-1", False, "tevd-2"),
        ])
        task = _task(run_id="run-1", runtime={"status": "running", "agent": "claude"})
        store.save_task(task)

        first = observer_harness.observe_run(
            "run-1", task=task, store=store, config=_base_config(), use_model=False,
        )
        assert [finding.severity for finding in first] == ["warning"]
        finding_id = first[0].finding_id

        later = _append(ledger, [
            _verification("run-1", False, "tevd-3"),
            _verification("run-1", False, "tevd-4"),
        ])
        second = observer_harness.observe_run(
            "run-1", task=task, store=store, config=_base_config(), use_model=False,
        )

        assert len(second) == 1
        assert second[0].finding_id == finding_id
        assert second[0].severity == "critical"
        assert second[0].confidence == first[0].confidence or second[0].confidence > 0
        assert any(item.get("event_id") == later[-1]["event_id"] for item in second[0].evidence)
        rows = list_trajectory_findings("run-1", db_path=store.db_path)
        assert len(rows) == 1
        assert rows[0]["finding_id"] == finding_id
        assert rows[0]["severity"] == "critical"

    def test_upsert_never_downgrades_severity_but_refreshes_evidence(self, tmp_path: Path):
        db_path = tmp_path / "state.db"
        base = {
            "finding_id": "fnd_1",
            "finding_key": "fk_esc",
            "run_id": "run-1",
            "finding_type": "repeated_failure",
            "summary": "strong",
            "evidence": [{"type": "verification", "event_id": "evt_9"}],
            "created_at": 1.0,
        }
        upsert_trajectory_finding({**base, "severity": "critical"}, db_path=db_path)

        weaker = upsert_trajectory_finding(
            {**base, "severity": "warning", "summary": "weak", "evidence": []},
            db_path=db_path,
        )
        stronger = upsert_trajectory_finding(
            {**base, "severity": "critical", "summary": "stronger",
             "evidence": [{"type": "verification", "event_id": "evt_10"}]},
            db_path=db_path,
        )

        assert weaker["severity"] == "critical"
        assert weaker["summary"] == "strong"
        assert weaker["finding_id"] == "fnd_1"
        assert stronger["severity"] == "critical"
        assert stronger["summary"] == "stronger"
        assert stronger["evidence"][0]["event_id"] == "evt_10"
        assert len(list_trajectory_findings("run-1", db_path=db_path)) == 1


# ---------------------------------------------------------------------------
# JSON stdout purity and task/run identity
# ---------------------------------------------------------------------------


class TestJsonStdoutPurity:
    def test_engine_diagnostics_go_to_stderr_not_stdout(self, tmp_path: Path, capfd):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "task_id": "task-1",
             "timestamp": 100.0},
            _verification("run-1", False, "tevd-a"),
            _verification("run-1", False, "tevd-b"),
        ])

        findings = observer_harness.observe_run(
            "run-1", store=store, config=_base_config(),
            provider=CapturingProvider(raises=True),
        )
        captured = capfd.readouterr()

        assert [finding.finding_type for finding in findings] == ["repeated_failure"]
        assert captured.out == ""
        assert "[OBSERVER PROVIDER FAILED]" in captured.err

    def test_cli_json_stays_pure_when_provider_fails(self, tmp_path: Path):
        import os
        import subprocess
        import sys

        repo_root = Path(__file__).resolve().parent.parent
        db_path = tmp_path / "state.db"
        store = SQLiteStateStore(db_path)
        _append(TrajectoryLedger(db_path), [
            {"run_id": "run-cli-json", "event_type": "run_started", "task_id": "task-cli-json"},
            _verification("run-cli-json", False, "tevd-j1"),
            _verification("run-cli-json", False, "tevd-j2"),
        ])
        env = {
            **os.environ,
            "HERDR_STATE_DB": str(db_path),
            "HERDR_OBSERVER_CONFIG": str(tmp_path / "absent.json"),
            "HERDR_OBSERVER_ENABLED": "1",
            "HERDR_OBSERVER_LIVE_PROBE": "0",
            "HERDR_OBSERVER_PROVIDER": "jev",
            "HERDR_OBSERVER_JEV_BASE_URL": "http://127.0.0.1:1",
            "HERDR_OBSERVER_JEV_TIMEOUT": "1",
            "JEV_API_KEY": "sk-fake-test-key",
        }
        command = [
            sys.executable, str(repo_root / "bin" / "herdr-task"),
            "observe", "--run-id", "run-cli-json", "--json",
        ]

        result = subprocess.run(command, capture_output=True, text=True, timeout=90, env=env)
        payload = json.loads(result.stdout)

        assert result.returncode == 0
        assert payload["run_id"] == "run-cli-json"
        assert payload["findings"][0]["finding_type"] == "repeated_failure"
        assert "[OBSERVER PROVIDER FAILED]" in result.stderr


class TestTaskRunIdentity:
    def test_cli_rejects_task_id_and_run_id_together(self, tmp_path: Path):
        import os
        import subprocess
        import sys

        repo_root = Path(__file__).resolve().parent.parent
        env = {
            **os.environ,
            "HERDR_STATE_DB": str(tmp_path / "state.db"),
            "HERDR_OBSERVER_CONFIG": str(tmp_path / "absent.json"),
        }
        command = [
            sys.executable, str(repo_root / "bin" / "herdr-task"),
            "observe", "--task-id", "task-a", "--run-id", "run-b", "--json",
        ]

        result = subprocess.run(command, capture_output=True, text=True, timeout=60, env=env)

        assert result.returncode == 2
        assert "not allowed with" in result.stderr
        assert result.stdout == ""

    def test_engine_never_borrows_runtime_from_a_mismatched_task(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-b", "event_type": "run_started", "task_id": "task-b",
             "timestamp": 100.0},
            _verification("run-b", False, "tevd-b1"),
            _verification("run-b", False, "tevd-b2"),
        ])
        task_a = _task("task-a", run_id="run-a", runtime={
            "status": "unavailable", "agent": "claude", "pane_id": "pane-a",
        })
        store.save_task(task_a)

        findings = observer_harness.observe_run(
            "run-b", task=task_a, store=store, config=_base_config(), use_model=False,
        )

        assert [finding.finding_type for finding in findings] == ["repeated_failure"]


# ---------------------------------------------------------------------------
# Hard context budget
# ---------------------------------------------------------------------------


class TestHardContextBudget:
    def _hostile_inputs(self, store, ledger):
        huge = "X" * 8000
        task = {
            "task_id": "task-1",
            "workflow_id": "wf-1",
            "node": huge,
            "stage": huge,
            "agent": huge,
            "status": "working",
            "goal": huge,
            "run_id": "run-1",
            "runtime": {
                "status": "running", "agent": huge, "agent_name": huge,
                "agent_session_id": huge, "cwd": huge, "pane_id": huge,
                "workspace_id": huge, "tab_id": huge,
            },
        }
        events = _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "task_id": "task-1",
             "timestamp": 100.0, "metadata": {"note": huge}},
            _verification("run-1", False, "tevd-a"),
            _verification("run-1", False, "tevd-b"),
        ])
        events[-1]["metadata"] = {"note": huge}
        return task

    def test_config_clamps_max_context_size_to_product_minimum(self):
        clamped = observer_config.load_config(
            path="", env={"HERDR_OBSERVER_MAX_CONTEXT_SIZE": "100"},
        )
        default = observer_config.load_config(path="", env={})

        assert observer_config.MIN_MAX_CONTEXT_SIZE == 500
        assert clamped["max_context_size"] == 500
        assert default["max_context_size"] == 8000

    def test_hostile_nested_fields_cannot_break_the_budget(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        task = self._hostile_inputs(store, ledger)
        config = {**_base_config(), "max_context_size": 600,
                  "confidence_threshold": 0.0}
        provider = CapturingProvider({"repeated_failure": 0.9})

        observer_harness.observe_run(
            "run-1", task=task, store=store, config=config, provider=provider,
            runtime_probe=lambda probe_task: {
                "status": "unknown", "reason": "X" * 8000,
            },
        )

        context = provider.calls[0]["state"]
        serialized = json.dumps(context, ensure_ascii=False)
        assert len(serialized) <= 600
        assert context["run"]["run_id"] == "run-1"

    def test_squeezed_context_is_kept_instead_of_collapsing_to_minimal(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        events = [{"run_id": "run-1", "event_type": "run_started", "task_id": "task-1",
                   "timestamp": 0.0}]
        events += [
            {"run_id": "run-1", "event_type": "task_status_changed", "task_id": "task-1",
             "status": "working", "timestamp": float(index + 1),
             "metadata": {"reason": "note-" + "x" * 140}}
            for index in range(60)
        ]
        _append(ledger, events)
        config = _base_config()
        signal = observer_signals.Signal(
            finding_type="repeated_failure",
            severity="warning",
            summary="连续验证失败",
            suspected_cause="路径无法收敛",
            recommended_action="replan",
            confidence=0.8,
            evidence=[{"type": "verification", "event_id": "evt_2"}],
            anchor="evt_2",
            requires_confirmation=False,
            facts={"consecutive_failures": 2},
        )

        ctx = observation_context.build_observation_context(
            run_id="run-1",
            task=_task(run_id="run-1", runtime={"status": "running", "agent": "claude"}),
            events=ledger.list_events("run-1"),
            runtime={"status": "running", "agent": "claude"},
            log_tail=None,
            signals=[signal],
            config=config,
            now=100.0,
        )

        assert len(json.dumps(ctx, ensure_ascii=False)) <= config["max_context_size"]
        assert "window" in ctx and "runtime" in ctx
        assert len(ctx.get("recent_events") or []) >= 1
        assert ctx["signals"] and "summary" in ctx["signals"][0]

    def test_minimal_identity_survives_extreme_budget(self, tmp_path: Path):
        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        task = self._hostile_inputs(store, ledger)
        config = {**_base_config(), "max_context_size": 100}
        signals = observer_signals.detect_signals(
            run_id="run-1",
            events=ledger.list_events("run-1"),
            task=task,
            runtime=task["runtime"],
            log_tail=None,
            now=110.0,
            config=config,
        )

        context = observation_context.build_observation_context(
            run_id="run-1",
            task=task,
            events=ledger.list_events("run-1"),
            runtime=task["runtime"],
            log_tail=None,
            signals=signals,
            config=config,
            now=110.0,
        )

        serialized = json.dumps(context, ensure_ascii=False)
        assert len(serialized) <= 500
        assert context["run"]["run_id"] == "run-1"


class TestProviderConstructionIsolation:
    """A broken provider factory must not disable deterministic findings."""

    def test_construction_failure_keeps_evidence_backed_findings(self, tmp_path: Path, monkeypatch, capfd):
        import herdr.observer.harness as observer_harness_module

        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "task_id": "task-1",
             "timestamp": 100.0},
            _verification("run-1", False, "tevd-a"),
            _verification("run-1", False, "tevd-b"),
        ])

        def explode(*args, **kwargs):
            raise RuntimeError("provider factory exploded")

        monkeypatch.setattr(observer_harness_module, "get_provider", explode)

        findings = observer_harness.observe_run(
            "run-1", store=store, config=_base_config(),
        )
        captured = capfd.readouterr()

        assert [finding.finding_type for finding in findings] == ["repeated_failure"]
        assert len(list_trajectory_findings("run-1", db_path=store.db_path)) == 1
        assert "[OBSERVER PROVIDER SKIPPED]" in captured.err

    def test_weak_signals_stay_silent_when_construction_fails(self, tmp_path: Path, monkeypatch):
        import herdr.observer.harness as observer_harness_module

        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "task_id": "task-1",
             "timestamp": 100.0},
            {"run_id": "run-1", "event_type": "task_status_changed", "status": "working",
             "task_id": "task-1", "timestamp": 200.0},
        ])

        def explode(*args, **kwargs):
            raise RuntimeError("provider factory exploded")

        monkeypatch.setattr(observer_harness_module, "get_provider", explode)

        findings = observer_harness.observe_run(
            "run-1", store=store, config=_base_config(), now=5000.0,
        )

        assert findings == []  # stalled_execution requires confirmation


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

    def test_slow_live_probe_never_blocks_submit(self, tmp_path: Path, monkeypatch):
        import herdr.observer.live as observer_live

        probed: List[str] = []

        def slow_probe(*args, **kwargs):
            probed.append("probe")
            time.sleep(1.0)
            return {"status": "unknown", "reason": "slow"}

        def slow_transcript(*args, **kwargs):
            probed.append("transcript")
            time.sleep(1.0)
            return None

        monkeypatch.setattr(observer_live, "probe_live_runtime", slow_probe)
        monkeypatch.setattr(observer_live, "read_live_transcript", slow_transcript)

        store = SQLiteStateStore(tmp_path / "state.db")
        ledger = TrajectoryLedger(store.db_path)
        _append(ledger, [
            {"run_id": "run-1", "event_type": "run_started", "task_id": "task-1",
             "timestamp": 100.0},
            _verification("run-1", False, "tevd-a"),
            _verification("run-1", False, "tevd-b"),
        ])
        task = _task(run_id="run-1", runtime={"status": "running", "agent": "claude",
                                              "pane_id": "pane-9"})
        store.save_task(task)
        config = {**_base_config(), "live_probe": True, "interval": 0, "max_calls_per_run": 5}
        scheduler = observer_harness.ObservationScheduler(config=config)

        started = time.monotonic()
        assert scheduler.submit("run-1", task=task, store=store) is True
        elapsed = time.monotonic() - started
        scheduler.drain(timeout=10)

        assert elapsed < 0.5, "probe I/O must not block the submitting (polling) thread"
        assert probed, "probes must still run on the worker thread"
