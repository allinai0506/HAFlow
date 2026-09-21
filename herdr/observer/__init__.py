"""HAFlow Trajectory Observer V1.

A best-effort, read-only observation layer over the Agent Trajectory Ledger:

    Trajectory + Runtime + bounded logs
        -> deterministic signals (facts)
        -> DecisionProvider confirmation (existing abstraction)
        -> TrajectoryFinding (analysis, evidence-backed)

The observer only detects, explains, and recommends. It never terminates,
restarts, replans, reroutes, repairs, or mutates any execution state.
"""

from .config import load_config
from .engine import TrajectoryObserver, list_findings
from .harness import ObservationScheduler, observe_run, reset_process_state, submit_observation
from .models import (
    FINDING_TYPES,
    RECOMMENDED_ACTIONS,
    SEVERITIES,
    TrajectoryFinding,
)

__all__ = [
    "FINDING_TYPES",
    "RECOMMENDED_ACTIONS",
    "SEVERITIES",
    "ObservationScheduler",
    "TrajectoryFinding",
    "TrajectoryObserver",
    "list_findings",
    "load_config",
    "observe_run",
    "reset_process_state",
    "submit_observation",
]
