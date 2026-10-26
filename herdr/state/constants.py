"""constants persistence; extracted without changing behavior."""

from pathlib import Path
from typing import Dict, Tuple


HOME = Path.home()

CONTROLLER_DIR = HOME / ".herdr-controller"

_INITIALIZED_DBS: set = set()

SHADOW_READ_CAPABILITY: Dict[str, Tuple[str, ...]] = {
    "events": (
        "id", "workflow_id", "node_id", "task_id", "agent_id",
        "event_type", "timestamp", "source", "run_id", "payload_json",
    ),
    # Placeholder: ``_OUTCOME_COLUMN_NAMES`` is defined further below
    # and bound by ``_resolve_shadow_read_capability`` on first use.
    "agent_execution_outcomes": (),
}

OUTCOME_SCHEMA_VERSION = 1

#: Newest-first outcome cap per candidate agent. Each candidate gets its
#: own window so a high-frequency agent cannot crowd others out.

OUTCOME_PER_AGENT_LIMIT = 500

_OUTCOME_COLUMNS = (
    "outcome_id, run_id, task_id, workflow_id, agent, node, task_type, "
    "started_at, finished_at, wall_time_seconds, final_status, "
    "requirements_satisfied, verification_passed, qualified_success, "
    "rework_count, blocked_count, human_intervention_count, recorded_at, "
    "source_eval_id, source_eval_revision, source_task_version, schema_version"
)

#: Outcome column names in SELECT order: the read-only capability
#: contract ``SHADOW_READ_CAPABILITY`` (defined near ``init_db``) binds
#: these names lazily (``_resolve_shadow_read_capability``) so the check
#: can never drift from what shadow reads actually bind against.

_OUTCOME_COLUMN_NAMES: Tuple[str, ...] = tuple(
    name.strip() for name in _OUTCOME_COLUMNS.split(",")
)

ROUTE_DECISION_DEFAULT_LIMIT = 1000

#: Hard cap: evaluation never loads unbounded event history in one call.

ROUTE_DECISION_MAX_LIMIT = 10000

#: Chunk size for batched (task_id, run_id) point lookups (2 bind vars
#: per pair; stays far below SQLite's variable limit).

OUTCOME_LOOKUP_CHUNK = 400

def _route_decision_bucket_exprs() -> Tuple[str, str, str, str]:
    """Canonical SQL expressions for the exact decision-bucket columns.

    Both the ``idx_events_route_decision_bucket`` expression index and
    the WHERE fragments from :func:`_exact_bucket_sql` are built from
    these exact strings: SQLite only serves a query from an expression
    index when the query expression is structurally identical to the
    indexed one, so the two must never be allowed to drift.

    Every extraction is wrapped in ``CASE WHEN json_valid(payload_json)``
    because index expressions are evaluated on INSERT: a malformed
    historical payload must index as NULL (and simply never match an
    exact-bucket read), never fail the write.
    """
    def _scoped(core: str) -> str:
        return f"(CASE WHEN json_valid(payload_json) THEN {core} END)"

    mode = _scoped("COALESCE(json_extract(payload_json, '$.mode'), '')")
    recommended = _scoped(
        "COALESCE(json_extract(payload_json, '$.recommended_agent'), '')")
    node = _scoped(
        "COALESCE(NULLIF(json_extract(payload_json, '$.node'), ''), "
        "node_id, '')")
    task_type = _scoped(
        "COALESCE(json_extract(payload_json, '$.task_type'), '')")
    return mode, recommended, node, task_type

PROTECTED_TASK_METADATA_FIELDS = {
    "execution_contract_v1",
    "task_id",
    "workflow_id",
    "run_id",
    "node",
    "stage",
    "agent",
    "status",
    "created_at",
    "updated_at",
    "version",
}

PROTECTED_WORKFLOW_METADATA_FIELDS = {
    "execution_declarations_v1",
    "workflow_id",
    "status",
    "created_at",
    "updated_at",
}

RESERVED_EVENT_METADATA_FIELDS = {
    "from",
    "to",
    "from_status",
    "to_status",
    "reason",
    "source",
    "timestamp",
    "forced",
}
