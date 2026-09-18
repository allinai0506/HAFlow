"""HAFlow Semantic Supervisor V1.

A supervision layer independent of any specific judgment backend:

    runtime facts -> SupervisorState -> DecisionProvider (Jev first)
                  -> semantic signals -> Policy Engine -> action

The supervisor observes and proposes; only HAFlow orchestration acts.
"""

from .config import load_config, supervisor_enabled
from .engine import RateGate, SemanticSupervisor
from .evaluation import EVALUATION_EVENT, POLICY_EVENT, build_evaluation, compute_deltas
from .signals import SIGNAL_DESCRIPTIONS, SIGNAL_NAMES, signal_questions
from .state import build_supervisor_state, redact_text

__all__ = [
    "EVALUATION_EVENT",
    "POLICY_EVENT",
    "RateGate",
    "SIGNAL_DESCRIPTIONS",
    "SIGNAL_NAMES",
    "SemanticSupervisor",
    "build_evaluation",
    "build_supervisor_state",
    "compute_deltas",
    "load_config",
    "redact_text",
    "signal_questions",
    "supervisor_enabled",
]
