#!/usr/bin/env python3
"""Compatibility facade for the SQLite state engine.

Implementations live in ``herdr.state``. Existing callers, private imports,
and monkeypatches keep using this module; leaf calls resolve its attributes
at call time through ``herdr.state._facade``. Shared mutable state is aliased
from constants, never copied. Transaction and schema behavior is unchanged.
"""

# Retain the original imported module objects for compatibility, notably time.
import hashlib as hashlib
import json as json
import math as math
import os as os
import re as re
import sqlite3 as sqlite3
import time as time
import uuid as uuid
from dataclasses import (
    dataclass as dataclass,
)
from contextlib import (
    contextmanager as contextmanager,
    nullcontext as nullcontext,
)
from pathlib import (
    Path as Path,
)

import fcntl as fcntl
from typing import (
    Any as Any,
    Dict as Dict,
    List as List,
    Mapping as Mapping,
    Optional as Optional,
    Tuple as Tuple,
)

from herdr.transitions import (
    ACTIVE_TASK_STATUSES as ACTIVE_TASK_STATUSES,
    COMPLETED_TASK_STATUSES as COMPLETED_TASK_STATUSES,
    TERMINAL_TASK_STATUSES as TERMINAL_TASK_STATUSES,
    InvalidTransitionError as InvalidTransitionError,
    validate_task_transition as validate_task_transition,
    validate_workflow_transition as validate_workflow_transition,
)


from .state.constants import (
    HOME,
    CONTROLLER_DIR,
    _INITIALIZED_DBS,
    SHADOW_READ_CAPABILITY,
    OUTCOME_SCHEMA_VERSION,
    OUTCOME_PER_AGENT_LIMIT,
    _OUTCOME_COLUMNS,
    _OUTCOME_COLUMN_NAMES,
    ROUTE_DECISION_DEFAULT_LIMIT,
    ROUTE_DECISION_MAX_LIMIT,
    OUTCOME_LOOKUP_CHUNK,
    _route_decision_bucket_exprs,
    PROTECTED_TASK_METADATA_FIELDS,
    PROTECTED_WORKFLOW_METADATA_FIELDS,
    RESERVED_EVENT_METADATA_FIELDS,
)

from .state.connection import (
    _db_path_for_tasks_file,
    _db_path_for_workflow_file,
    get_default_db_path,
    get_db_connection,
    init_db,
    ReadonlySchemaError,
    resolve_state_db_path,
    get_readonly_db_connection,
    _resolve_shadow_read_capability,
    assert_shadow_read_capability,
    _open_shadow_read_connection,
    _get_credentials_dir,
    save_user_token,
    get_user_token,
    delete_user_token,
)

from .state.schema import (
    _schema_initialization_lock,
    _ensure_schema,
)

from .state.schema_ddl_helpers import (
    _ensure_working_context_source_heads_schema,
    _ensure_working_context_source_clock_schema,
    _ensure_rollout_schema,
    _migrate_rollout_state_key,
    _ensure_task_columns,
    _ensure_completion_columns,
    _ensure_event_columns,
    _ensure_intervention_columns,
    _ensure_working_context_columns,
    _ensure_eval_replay_columns,
)

from .state.schema_v1 import (
    migrate_v1_to_v2,
)

from .state.workflow_task_store import (
    TaskAlreadyExistsError,
    save_workflow,
    get_workflow,
    list_workflows,
    delete_workflow,
    save_task,
    _decode_task_row,
    get_task,
    get_tasks,
    list_tasks,
    delete_task,
)

from .state.transition_task import (
    transition_task,
    compare_and_set_task_transition,
    compare_and_set_completion_transition,
)

from .state.transition_gate import (
    register_workflow,
    transition_workflow,
    update_task_metadata,
    update_workflow_metadata,
    force_pass_gate,
)

from .state.completion import (
    _decode_completion_observation,
    observe_completion,
    _coerce_version,
    _blocker_sample_consumed,
    observe_blocker_marker,
    get_completion_observation,
    clear_completion_observation,
    clear_completion_observations_for_statuses,
)

from .state.checkpoint import (
    create_checkpoint,
    list_checkpoints,
    get_checkpoint,
    restore_checkpoint,
    fork_workflow_from_checkpoint,
    get_checkpoint_lineage,
)

from .state.steering import (
    save_steer,
    get_steer,
    list_steers,
    update_steer_status,
    record_steering_history,
    list_steering_history,
)

from .state.intervention import (
    _decode_intervention_row,
    _record_intervention_event,
    create_intervention,
    get_intervention,
    list_interventions,
    claim_intervention,
    complete_intervention,
    fail_intervention,
    _finish_intervention,
)

from .state.collaboration import (
    _decode_collaboration_row,
    create_collaboration_event,
    get_collaboration_event,
    attach_working_context_ref,
    list_collaboration_events,
    _transition_collaboration_event,
    mark_collaboration_dispatched,
    mark_collaboration_acknowledged,
    mark_collaboration_completed,
    mark_collaboration_failed,
)

from .state.events import (
    record_event,
    record_event_if_absent,
    list_events,
    aggregate_run_metric_rows,
)

from .state.trajectory import (
    record_trajectory_event,
    record_observation_receipt,
    list_trajectory_events,
    latest_trajectory_sequence,
    trajectory_event_exists,
    artifact_ref_exists,
)

from .state.outcome_route import (
    _outcome_autofinalize_enabled,
    _decode_outcome_row,
    insert_execution_outcome,
    get_execution_outcome,
    read_execution_outcome,
    query_execution_outcomes,
    batch_get_execution_outcomes,
    ExactDecisionBucket,
    _exact_bucket_sql,
    _route_decisions_sql,
    query_route_decisions,
)

from .state.rollout import (
    rollout_bucket_key,
    _rollout_tables_present,
    _rollout_read_connection,
    RolloutSnapshot,
    RolloutWrite,
    read_rollout_snapshot,
    get_rollout_stage,
    list_rollout_states,
    list_rollout_audit,
    rollout_stage_known,
    _decode_rollout_audit_row,
    transact_rollout_stage,
)

from .state.observation_finding import (
    _decode_finding_row,
    upsert_trajectory_finding,
    get_trajectory_finding,
    get_trajectory_finding_by_id,
    list_trajectory_findings,
    list_trajectory_findings_bounded,
    _finding_source_fingerprint,
    _observation_source_fingerprint,
    _compact_observation_metadata,
    _context_source_version_in_conn,
    get_context_source_version,
    _decode_observation_row,
    insert_observation,
    get_observation,
    find_observation_by_dedup,
    list_observations,
)

from .state.context_pack import (
    _decode_context_pack_row,
    _trajectory_rows_in_conn,
    _task_matches_run,
    read_context_compact_snapshot,
    save_context_pack,
    get_context_pack,
    get_latest_context_pack,
    list_context_packs,
)

from .state.working_context import (
    _decode_working_context_row,
    save_working_context,
    register_working_context_source,
    working_context_source_is_current,
    record_working_context_metric,
    get_working_context,
    get_latest_working_context,
    list_working_contexts,
)

from .state.context_source_validation import (
    _valid_context_source_ref,
    _validate_context_source_existence,
)

__all__ = [
    'CONTROLLER_DIR', 'ExactDecisionBucket', 'HOME', 'OUTCOME_LOOKUP_CHUNK',
    'OUTCOME_PER_AGENT_LIMIT', 'OUTCOME_SCHEMA_VERSION', 'PROTECTED_TASK_METADATA_FIELDS', 'PROTECTED_WORKFLOW_METADATA_FIELDS',
    'RESERVED_EVENT_METADATA_FIELDS', 'ROUTE_DECISION_DEFAULT_LIMIT', 'ROUTE_DECISION_MAX_LIMIT', 'ReadonlySchemaError',
    'RolloutSnapshot', 'RolloutWrite', 'SHADOW_READ_CAPABILITY', '_INITIALIZED_DBS',
    '_OUTCOME_COLUMNS', '_OUTCOME_COLUMN_NAMES', '_blocker_sample_consumed', '_coerce_version',
    '_compact_observation_metadata', '_context_source_version_in_conn', '_db_path_for_tasks_file', '_db_path_for_workflow_file',
    '_decode_collaboration_row', '_decode_completion_observation', '_decode_context_pack_row', '_decode_finding_row',
    '_decode_intervention_row', '_decode_observation_row', '_decode_outcome_row', '_decode_rollout_audit_row',
    '_decode_task_row', '_decode_working_context_row', '_ensure_completion_columns', '_ensure_eval_replay_columns',
    '_ensure_event_columns', '_ensure_intervention_columns', '_ensure_rollout_schema', '_ensure_schema',
    '_ensure_task_columns', '_ensure_working_context_columns', '_ensure_working_context_source_clock_schema', '_ensure_working_context_source_heads_schema',
    '_exact_bucket_sql', '_finding_source_fingerprint', '_finish_intervention', '_get_credentials_dir',
    '_migrate_rollout_state_key', '_observation_source_fingerprint', '_open_shadow_read_connection', '_outcome_autofinalize_enabled',
    '_record_intervention_event', '_resolve_shadow_read_capability', '_rollout_read_connection', '_rollout_tables_present',
    '_route_decision_bucket_exprs', '_route_decisions_sql', '_schema_initialization_lock', '_task_matches_run',
    '_trajectory_rows_in_conn', '_transition_collaboration_event', '_valid_context_source_ref', '_validate_context_source_existence',
    'aggregate_run_metric_rows', 'artifact_ref_exists', 'assert_shadow_read_capability', 'attach_working_context_ref',
    'batch_get_execution_outcomes', 'claim_intervention', 'clear_completion_observation', 'clear_completion_observations_for_statuses',
    'compare_and_set_completion_transition', 'compare_and_set_task_transition', 'complete_intervention', 'create_checkpoint',
    'create_collaboration_event', 'create_intervention', 'delete_task', 'delete_user_token',
    'delete_workflow', 'fail_intervention', 'find_observation_by_dedup', 'force_pass_gate',
    'fork_workflow_from_checkpoint', 'get_checkpoint', 'get_checkpoint_lineage', 'get_collaboration_event',
    'get_completion_observation', 'get_context_pack', 'get_context_source_version', 'get_db_connection',
    'get_default_db_path', 'get_execution_outcome', 'get_intervention', 'get_latest_context_pack',
    'get_latest_working_context', 'get_observation', 'get_readonly_db_connection', 'get_rollout_stage',
    'get_steer', 'get_task', 'get_tasks', 'get_trajectory_finding',
    'get_trajectory_finding_by_id', 'get_user_token', 'get_workflow', 'get_working_context',
    'init_db', 'insert_execution_outcome', 'insert_observation', 'latest_trajectory_sequence',
    'list_checkpoints', 'list_collaboration_events', 'list_context_packs', 'list_events',
    'list_interventions', 'list_observations', 'list_rollout_audit', 'list_rollout_states',
    'list_steering_history', 'list_steers', 'list_tasks', 'list_trajectory_events',
    'list_trajectory_findings', 'list_trajectory_findings_bounded', 'list_workflows', 'list_working_contexts',
    'mark_collaboration_acknowledged', 'mark_collaboration_completed', 'mark_collaboration_dispatched', 'mark_collaboration_failed',
    'migrate_v1_to_v2', 'observe_blocker_marker', 'observe_completion', 'query_execution_outcomes',
    'query_route_decisions', 'read_context_compact_snapshot', 'read_execution_outcome', 'read_rollout_snapshot',
    'record_event', 'record_event_if_absent', 'record_observation_receipt', 'record_steering_history',
    'record_trajectory_event', 'record_working_context_metric', 'register_working_context_source', 'resolve_state_db_path',
    'restore_checkpoint', 'rollout_bucket_key', 'rollout_stage_known', 'save_context_pack',
    'save_steer', 'save_task', 'save_user_token', 'save_workflow',
    'save_working_context', 'trajectory_event_exists', 'transact_rollout_stage', 'transition_task',
    'register_workflow', 'transition_workflow', 'update_steer_status', 'update_task_metadata', 'update_workflow_metadata',
    'upsert_trajectory_finding', 'working_context_source_is_current',
]
