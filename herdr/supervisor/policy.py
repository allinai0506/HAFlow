#!/usr/bin/env python3
"""Policy Engine - the only place HAFlow turns signals into actions.

Semantic Supervisor -> signals -> THIS module -> action. A provider (Jev)
never touches task/runtime/workflow state: its output is only an input to
deterministic rules here, combined with runtime facts, with every decision
carrying machine-readable reasons ("why VERIFY", not "Jev said so").

Actions: CONTINUE VERIFY RETRY REROUTE PAUSE FINISH ESCALATE.
Confidence policy: a high-risk action (RETRY/REROUTE/PAUSE/FINISH/ESCALATE)
requires its trigger signals to clear their thresholds by a safety margin;
barely-over-threshold (low-confidence) judgments degrade to CONTINUE/VERIFY.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

CONTINUE = "CONTINUE"
VERIFY = "VERIFY"
RETRY = "RETRY"
REROUTE = "REROUTE"
PAUSE = "PAUSE"
FINISH = "FINISH"
ESCALATE = "ESCALATE"

ACTIONS = (CONTINUE, VERIFY, RETRY, REROUTE, PAUSE, FINISH, ESCALATE)

# V1 reserves these future dispatch dimensions without implementing them;
# a decision may carry a suggestion, orchestration stays authoritative.
EXECUTION_STRATEGIES = ("SIMPLE", "STANDARD", "VERIFY", "PARALLEL", "HUMAN")
MODEL_TIERS = ("FAST", "BALANCED", "STRONG")

# Actions that must never fire on a shaky judgment.
HIGH_RISK_ACTIONS = frozenset({RETRY, REROUTE, PAUSE, FINISH, ESCALATE})

# Actions that are compatible with the caller's default continuation
# (e.g. an agent_done checkpoint may still emit the normal done event).
PASS_THROUGH_ACTIONS = frozenset({CONTINUE, FINISH})

# Actions that, once enforced, must intercept the caller's default
# continuation: HAFlow orchestration owns what happens instead.
INTERVENTION_ACTIONS = frozenset(ACTIONS) - PASS_THROUGH_ACTIONS

# Task statuses in which supervision may not steer anything.
SETTLED_TASK_STATUSES = frozenset({
    "completed", "committed", "integrated", "cleanup_ready", "cleaned",
    "failed", "superseded",
})


@dataclass
class PolicyDecision:
    action: str
    reasons: List[str] = field(default_factory=list)
    signals_used: Dict[str, float] = field(default_factory=dict)
    facts_used: Dict[str, Any] = field(default_factory=dict)
    suggested_execution: Optional[str] = None  # V1 reserved, always None
    suggested_model_tier: Optional[str] = None  # V1 reserved, always None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "reasons": list(self.reasons),
            "signals_used": dict(self.signals_used),
            "facts_used": dict(self.facts_used),
            "suggested_execution": self.suggested_execution,
            "suggested_model_tier": self.suggested_model_tier,
        }


def _threshold(config: dict, name: str) -> float:
    return float((config.get("thresholds") or {}).get(name, 0.5))


def decide(
    evaluation: Dict[str, Any],
    facts: Dict[str, Any],
    config: Dict[str, Any],
) -> PolicyDecision:
    """Choose one action from semantic signals + deterministic facts.

    ``facts`` carries the deterministic context the rules check:
    task_status, runtime_status, attempt_count, verification_count,
    auto_execute_allowed. Liveness-style questions are answered by facts,
    never delegated to the provider.
    """
    signals = {
        name: float(value)
        for name, value in (evaluation.get("signals") or {}).items()
        if isinstance(value, (int, float))
    }
    policy_cfg = dict(config.get("policy") or {})
    min_margin = float(policy_cfg.get("min_margin", 0.05))
    max_attempts = int(policy_cfg.get("max_attempts", 2))
    max_verifications = int(policy_cfg.get("max_verifications", 2))

    task_status = str(facts.get("task_status") or "")
    runtime_status = str(facts.get("runtime_status") or "")
    attempt_count = int(facts.get("attempt_count") or 0)
    verification_count = int(facts.get("verification_count") or 0)
    auto_allowed = bool(facts.get(
        "auto_execute_allowed", policy_cfg.get("allow_auto_execute", True)))
    trigger = str(evaluation.get("trigger") or facts.get("trigger") or "agent_done")

    base_facts = {
        "trigger": trigger,
        "task_status": task_status,
        "runtime_status": runtime_status,
        "attempt_count": attempt_count,
        "verification_count": verification_count,
        "auto_execute_allowed": auto_allowed,
    }
    if facts.get("test_progress"):
        base_facts["test_progress"] = dict(facts.get("test_progress"))

    def needs(name: str) -> float:
        return signals.get(name, 0.0)

    def high(name: str) -> bool:
        return needs(name) >= _threshold(config, name)

    def low(name: str) -> bool:
        return needs(name) < _threshold(config, name)

    def margin(*names: str) -> float:
        return min(needs(name) - _threshold(config, name) for name in names)

    # --- facts dominate: no usable signals, or task already settled
    if evaluation.get("status") == "failed" or not signals:
        return PolicyDecision(
            action=CONTINUE,
            reasons=["no usable semantic signals (evaluation failed or empty)"],
            facts_used={**base_facts, "evaluation_status": evaluation.get("status")},
        )
    if task_status in SETTLED_TASK_STATUSES:
        return PolicyDecision(
            action=CONTINUE,
            reasons=[f"task already settled ({task_status}); supervision never reopens history"],
            signals_used=signals,
            facts_used=base_facts,
        )

    # --- human escalation: semantic need x retry/verification exhaustion x authority
    if high("needs_human"):
        if not auto_allowed:
            return _guarded(ESCALATE, margin("needs_human"), min_margin, [
                f"needs_human {needs('needs_human'):.2f} >= threshold",
                "task does not allow automated execution",
            ], signals, base_facts)
        if attempt_count >= max_attempts or verification_count >= max_verifications:
            return _guarded(ESCALATE, margin("needs_human"), min_margin, [
                f"needs_human {needs('needs_human'):.2f} >= threshold",
                f"attempt/verification budget exhausted "
                f"({attempt_count}/{max_attempts}, {verification_count}/{max_verifications})",
            ], signals, base_facts)
        return PolicyDecision(action=CONTINUE, reasons=[
            f"needs_human {needs('needs_human'):.2f} high but retries/verifications remain "
            f"({attempt_count}/{max_attempts}, {verification_count}/{max_verifications})",
        ], signals_used=signals, facts_used=base_facts)

    # --- stuck: retryable only while the runtime is alive and budget remains
    if high("worker_stuck"):
        if trigger == "tests_completed":
            test_progress = facts.get("test_progress") or {}
            failed_delta = test_progress.get("failed_delta")
            score_delta = test_progress.get("score_delta")
            is_improving = (
                (failed_delta is not None and failed_delta < 0) or
                (score_delta is not None and score_delta > 0) or
                not low("meaningful_progress")
            )
            tests_dict = facts.get("tests") or {}
            iteration = int(tests_dict.get("iteration") or 0)
            max_iter = int(tests_dict.get("max_iterations") or 5)
            exhausted_iterations = bool(iteration >= max_iter and tests_dict.get("converged") is False)

            if is_improving or (runtime_status == "running"
                                 and task_status in ("working", "rework")
                                 and not exhausted_iterations):
                return PolicyDecision(action=CONTINUE, reasons=[
                    f"tests_completed: worker_stuck {needs('worker_stuck'):.2f} high but agent is actively "
                    f"iterating on tests (improving={is_improving}, iteration={iteration}, "
                    f"task_status={task_status}); inner loop continues",
                ], signals_used=signals, facts_used=base_facts)

            if runtime_status == "running" and attempt_count < max_attempts:
                return _guarded(RETRY, margin("worker_stuck"), min_margin, [
                    f"worker_stuck {needs('worker_stuck'):.2f} >= threshold",
                    "tests_completed: consecutive non-improving tests without progress; rework triggered",
                    f"runtime_status == running, attempt_count {attempt_count} < {max_attempts}",
                ], signals, base_facts)
            return PolicyDecision(action=CONTINUE, reasons=[
                f"worker_stuck {needs('worker_stuck'):.2f} high but attempts exhausted ({attempt_count}/{max_attempts})",
            ], signals_used=signals, facts_used=base_facts)

        if runtime_status == "running" and attempt_count < max_attempts:
            return _guarded(RETRY, margin("worker_stuck"), min_margin, [
                f"worker_stuck {needs('worker_stuck'):.2f} >= threshold",
                f"runtime_status == running, attempt_count {attempt_count} < {max_attempts}",
            ], signals, base_facts)
        return PolicyDecision(action=CONTINUE, reasons=[
            f"worker_stuck {needs('worker_stuck'):.2f} high but not retryable "
            f"(runtime={runtime_status or 'unknown'}, attempts {attempt_count}/{max_attempts})",
        ], signals_used=signals, facts_used=base_facts)

    # --- off track: reroute (reserved) / pause / escalate by remaining budget
    if high("work_off_track"):
        if policy_cfg.get("allow_reroute") and attempt_count < max_attempts:
            return _guarded(REROUTE, margin("work_off_track"), min_margin, [
                f"work_off_track {needs('work_off_track'):.2f} >= threshold",
                "reroute policy enabled",
            ], signals, base_facts)
        if attempt_count < max_attempts:
            return _guarded(PAUSE, margin("work_off_track"), min_margin, [
                f"work_off_track {needs('work_off_track'):.2f} >= threshold",
                "reroute disabled; hold the scene for inspection instead of burning attempts",
            ], signals, base_facts)
        return _guarded(ESCALATE, margin("work_off_track"), min_margin, [
            f"work_off_track {needs('work_off_track'):.2f} >= threshold",
            f"attempt budget exhausted ({attempt_count}/{max_attempts})",
        ], signals, base_facts)

    # --- finish: all three positive gates aligned
    if high("ready_to_finish") and high("requirements_satisfied") and high("tests_sufficient"):
        if trigger == "tests_completed":
            return PolicyDecision(action=CONTINUE, reasons=[
                f"tests_completed: tests sufficient ({needs('tests_sufficient'):.2f}), "
                f"requirements satisfied ({needs('requirements_satisfied'):.2f}), "
                f"ready_to_finish ({needs('ready_to_finish'):.2f}); "
                "tests level satisfied, agent continues towards agent_done"
            ], signals_used=signals, facts_used=base_facts)
        return _guarded(FINISH, margin("ready_to_finish", "requirements_satisfied",
                                       "tests_sufficient"), min_margin, [
            f"ready_to_finish {needs('ready_to_finish'):.2f} >= threshold",
            f"requirements_satisfied {needs('requirements_satisfied'):.2f} >= threshold",
            f"tests_sufficient {needs('tests_sufficient'):.2f} >= threshold",
        ], signals, base_facts)

    # --- verify: work looks mostly done but the evidence loop is open
    if (high("needs_verification") or low("tests_sufficient")) and (
        high("requirements_satisfied") or high("implementation_complete")
    ):
        reasons = []
        if high("needs_verification"):
            reasons.append(f"needs_verification {needs('needs_verification'):.2f} >= threshold")
        if low("tests_sufficient"):
            reasons.append(
                f"tests_sufficient {needs('tests_sufficient'):.2f} below threshold "
                f"{_threshold(config, 'tests_sufficient'):.2f}")
        reasons.append(
            f"requirements_satisfied {needs('requirements_satisfied'):.2f} / "
            f"implementation_complete {needs('implementation_complete'):.2f} indicate near-done work")
        return PolicyDecision(action=VERIFY, reasons=reasons,
                              signals_used=signals, facts_used=base_facts)

    # --- claim of done without substance: verify before accepting
    if high("implementation_complete") and low("requirements_satisfied"):
        return PolicyDecision(
            action=VERIFY,
            reasons=[
                f"implementation_complete {needs('implementation_complete'):.2f} but "
                f"requirements_satisfied {needs('requirements_satisfied'):.2f} below threshold",
            ],
            signals_used=signals, facts_used=base_facts)

    return PolicyDecision(action=CONTINUE, reasons=[
        f"no rule fired: progress {needs('meaningful_progress'):.2f}, "
        f"stuck {needs('worker_stuck'):.2f}, off_track {needs('work_off_track'):.2f}, "
        f"ready_to_finish {needs('ready_to_finish'):.2f}",
    ], signals_used=signals, facts_used=base_facts)


def _guarded(action: str, margin: float, min_margin: float, reasons: List[str],
             signals: Dict[str, float], facts: Dict[str, Any]) -> PolicyDecision:
    """Confidence policy: high-risk actions need threshold + safety margin."""
    if action in HIGH_RISK_ACTIONS and margin < min_margin:
        return PolicyDecision(
            action=CONTINUE,
            reasons=[
                f"blocked by confidence policy: trigger signals exceed thresholds "
                f"by only {margin:.2f} < required margin {min_margin:.2f}"
            ] + reasons,
            signals_used=signals, facts_used=facts)
    return PolicyDecision(action=action, reasons=reasons,
                          signals_used=signals, facts_used=facts)
