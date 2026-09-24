"""Bounded source snapshot adapter for Context Compiler.

This module owns SQLite reads and scope validation for WorkingContext.  The
compiler core consumes the returned snapshot and never performs source I/O.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from . import state_db
from .collaboration import collab_scope_for_task
from .context_models import RELEVANT_EVENT_TYPES
from .trajectory import run_id_for_task


def _task_run(task: Mapping[str, Any]) -> Optional[str]:
    try:
        return str(run_id_for_task(dict(task)))
    except (TypeError, ValueError):
        return None


def _db_path(store: Any = None) -> Optional[Path]:
    value = getattr(store, "db_path", None)
    return Path(value) if value is not None else None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> List[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Bounded, same-connection source snapshot


def _decode_workflow_row(row: Any) -> Dict[str, Any]:
    metadata = json.loads(row["metadata_json"] or "{}")
    config = json.loads(row["config_json"] or "{}")
    result = dict(metadata)
    result.update({
        "workflow_id": row["workflow_id"],
        "title": row["title"],
        "status": row["status"],
        "template_name": row["template_name"],
        "current_stage": row["current_stage"],
        "config": config,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    })
    return result


def _decode_event_row(row: Any) -> Dict[str, Any]:
    payload = json.loads(row["payload_json"] or "{}")
    return {
        "event_id": f"evt_{row['id']}",
        "run_id": row["run_id"],
        "workflow_id": row["workflow_id"],
        "task_id": row["task_id"],
        "node_id": row["node_id"],
        "event_type": row["event_type"],
        "sequence": row["sequence"],
        "timestamp": row["timestamp"],
        "payload": payload,
    }


def _decode_eval_row(row: Any) -> Dict[str, Any]:
    names = set(row.keys())
    def decode(name: str, default: Any) -> Any:
        if name not in names or row[name] is None:
            return default
        try:
            return json.loads(row[name])
        except (TypeError, ValueError):
            return default
    return {
        "eval_id": row["eval_id"],
        "run_id": row["run_id"],
        "revision": int(row["revision"] or 0),
        "task_id": row["task_id"] if "task_id" in names else None,
        "workflow_id": row["workflow_id"] if "workflow_id" in names else None,
        "verification_passed": row["verification_passed"] if "verification_passed" in names else None,
        "requirements_satisfied": row["requirements_satisfied"] if "requirements_satisfied" in names else None,
        "final_status": row["final_status"] if "final_status" in names else None,
        "evidence": decode("evidence_json", None),
        "warnings": decode("warnings_json", []),
        "created_at": row["created_at"],
    }


def _merge_verification_events(
    conn: Any,
    run_values: Sequence[str],
    events: List[Dict[str, Any]],
    *,
    task_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    allowed_runs: Set[str] | None = None,
    workflow_id: str | None = None,
    run_scope: str | None = None,
    target_task_id: str | None = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    bounded_limit = max(0, int(limit))
    if bounded_limit == 0:
        return []
    if not run_values:
        return events[:bounded_limit]
    placeholders = ",".join("?" for _ in run_values)
    task_ids = list(task_by_id or {})
    task_filter_parts = ["e.task_id IS NULL"]
    if task_ids:
        task_filter_parts.append(
            f"e.task_id IN ({','.join('?' for _ in task_ids)})"
        )
    task_filter = " OR ".join(task_filter_parts)
    window_limit = max(
        bounded_limit,
        max(1, len(task_ids), len(run_values)) * 3,
    )
    rows = conn.execute(
        f"""SELECT * FROM (
                SELECT e.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY e.run_id, COALESCE(e.task_id, '')
                        ORDER BY e.sequence DESC, e.id DESC
                    ) AS latest_rank,
                    ROW_NUMBER() OVER (
                        PARTITION BY e.run_id, COALESCE(e.task_id, '')
                        ORDER BY CASE WHEN (
                            json_type(e.payload_json, '$.verification.passed') = 'false'
                            OR json_type(e.payload_json, '$.verification_passed') = 'false'
                            OR json_type(e.payload_json, '$.verification.verification_passed') = 'false'
                        ) THEN 0 ELSE 1 END, e.sequence DESC, e.id DESC
                    ) AS strict_rank,
                    ROW_NUMBER() OVER (
                        PARTITION BY e.run_id, COALESCE(e.task_id, '')
                        ORDER BY CASE WHEN (
                            json_type(e.payload_json, '$.verification.passed') = 'true'
                            OR json_type(e.payload_json, '$.verification_passed') = 'true'
                            OR json_type(e.payload_json, '$.verification.verification_passed') = 'true'
                        ) THEN 0 ELSE 1 END, e.sequence DESC, e.id DESC
                    ) AS pass_rank
                  FROM events e
                 WHERE e.source = 'trajectory'
                   AND e.run_id IN ({placeholders})
                   AND e.event_type IN ('verification_completed', 'tests_completed')
                   AND (e.workflow_id = ? OR (
                       e.task_id IS NOT NULL AND (e.workflow_id IS NULL OR e.workflow_id = '')
                   ))
                   AND ({task_filter})
                   AND length(e.payload_json) <= 20000
            ) WHERE latest_rank = 1 OR strict_rank = 1 OR pass_rank = 1
            ORDER BY CASE WHEN task_id = ? THEN 0 ELSE 1 END,
                     sequence DESC, id DESC
            LIMIT ?""",
        (*run_values, workflow_id, *task_ids, target_task_id, window_limit),
    ).fetchall()
    critical_types = (
        "task_failed", "agent_failed", "run_failed", "blocker",
        "task_started", "task_completed", "run_completed", "agent_done",
        "task_status_changed",
    )
    critical_placeholders = ",".join("?" for _ in critical_types)
    critical_rows = conn.execute(
        f"""SELECT * FROM (
                SELECT e.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY e.run_id, COALESCE(e.task_id, '')
                        ORDER BY e.sequence DESC, e.id DESC
                    ) AS latest_rank,
                    ROW_NUMBER() OVER (
                        PARTITION BY e.run_id, COALESCE(e.task_id, '')
                        ORDER BY CASE WHEN e.event_type IN (
                            'task_failed', 'agent_failed', 'run_failed', 'blocker'
                        ) THEN 0 ELSE 1 END, e.sequence DESC, e.id DESC
                    ) AS failure_rank
                  FROM events e
                 WHERE e.source = 'trajectory'
                   AND e.run_id IN ({placeholders})
                   AND e.event_type IN ({critical_placeholders})
                   AND (e.workflow_id = ? OR (
                       e.task_id IS NOT NULL AND (e.workflow_id IS NULL OR e.workflow_id = '')
                   ))
                   AND ({task_filter})
                   AND length(e.payload_json) <= 20000
            ) WHERE latest_rank = 1 OR failure_rank = 1
            ORDER BY CASE WHEN task_id = ? THEN 0 ELSE 1 END,
                     sequence DESC, id DESC
            LIMIT ?""",
        (
            *run_values,
            *critical_types,
            workflow_id,
            *task_ids,
            target_task_id,
            window_limit,
        ),
    ).fetchall()
    oversized_types = tuple(dict.fromkeys(
        critical_types + ("verification_completed", "tests_completed") + tuple(RELEVANT_EVENT_TYPES)
    ))
    oversized_placeholders = ",".join("?" for _ in oversized_types)
    oversized_rows = conn.execute(
        f"""SELECT e.id, e.run_id, e.task_id, e.workflow_id, e.node_id,
                   e.event_type, e.sequence, e.timestamp
              FROM events e
             WHERE e.source = 'trajectory'
               AND e.run_id IN ({placeholders})
               AND e.event_type IN ({oversized_placeholders})
               AND (e.workflow_id = ? OR (
                   e.task_id IS NOT NULL AND (e.workflow_id IS NULL OR e.workflow_id = '')
               ))
               AND ({task_filter})
               AND length(e.payload_json) > 20000
             ORDER BY CASE WHEN e.event_type IN (
                                  'task_failed', 'agent_failed', 'run_failed', 'blocker'
                              ) THEN 0
                            WHEN e.event_type IN ('verification_completed', 'tests_completed') THEN 1
                            ELSE 2 END,
                      CASE WHEN e.task_id = ? THEN 0 ELSE 1 END,
                      e.sequence DESC, e.id DESC
             LIMIT ?""",
        (
            *run_values,
            *oversized_types,
            workflow_id,
            *task_ids,
            target_task_id,
            window_limit,
        ),
    ).fetchall()
    merged = {str(event.get("event_id")): event for event in events}
    reserved_event_ids: set[str] = set()
    critical_event_ids: set[str] = set()
    for row in oversized_rows:
        event = {
            "event_id": f"evt_{row['id']}",
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "workflow_id": row["workflow_id"],
            "node_id": row["node_id"],
            "event_type": row["event_type"],
            "sequence": row["sequence"],
            "timestamp": row["timestamp"],
            "payload": (
                {"verification": {"status": "unknown", "source_truncated": True}}
                if row["event_type"] in {"verification_completed", "tests_completed"}
                else {"metadata": {"reason": "source payload truncated"}, "source_truncated": True}
            ),
        }
        if (
            task_by_id is not None
            and allowed_runs is not None
            and workflow_id is not None
            and run_scope is not None
            and not _source_allowed(
                event,
                task_by_id=task_by_id,
                allowed_runs=allowed_runs,
                workflow_id=workflow_id,
                run_scope=run_scope,
            )
        ):
            continue
        event_id = str(event["event_id"])
        merged[event_id] = event
        if event["event_type"] in {"verification_completed", "tests_completed"}:
            reserved_event_ids.add(event_id)
        else:
            critical_event_ids.add(event_id)
    for row in critical_rows:
        event = _decode_event_row(row)
        if (
            task_by_id is not None
            and allowed_runs is not None
            and workflow_id is not None
            and run_scope is not None
            and not _source_allowed(
                event,
                task_by_id=task_by_id,
                allowed_runs=allowed_runs,
                workflow_id=workflow_id,
                run_scope=run_scope,
            )
        ):
            continue
        event_id = str(event.get("event_id"))
        critical_event_ids.add(event_id)
        merged[event_id] = event
    for row in rows:
        event = _decode_event_row(row)
        if (
            task_by_id is not None
            and allowed_runs is not None
            and workflow_id is not None
            and run_scope is not None
            and not _source_allowed(
                event,
                task_by_id=task_by_id,
                allowed_runs=allowed_runs,
                workflow_id=workflow_id,
                run_scope=run_scope,
            )
        ):
            continue
        event_id = str(event.get("event_id"))
        reserved_event_ids.add(event_id)
        merged[event_id] = event
    def verification_order(event: Mapping[str, Any]) -> Tuple[int, str]:
        return (
            int(event.get("sequence") or 0),
            str(event.get("event_id") or ""),
        )

    def verification_value(event: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        nested = payload.get("verification")
        return nested if isinstance(nested, Mapping) else payload

    def verification_strength(event: Mapping[str, Any]) -> int:
        value = verification_value(event)
        if value.get("source_truncated") is True:
            return 3
        if value.get("passed") is False or value.get("verification_passed") is False:
            return 2
        if value.get("passed") is True or value.get("verification_passed") is True:
            return 1
        return 0

    reserved_by_key: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for event_id in reserved_event_ids:
        event = merged.get(event_id)
        if event is None:
            continue
        key = (str(event.get("run_id") or ""), str(event.get("task_id") or ""))
        reserved_by_key.setdefault(key, []).append(event)
    preferred_event_ids: set[str] = set()
    for key_events in reserved_by_key.values():
        latest = max(key_events, key=verification_order)
        failure = max(
            (event for event in key_events if verification_strength(event) == 2),
            key=verification_order,
            default=None,
        )
        recovery = max(
            (event for event in key_events if verification_strength(event) == 1),
            key=verification_order,
            default=None,
        )
        truncated = max(
            (event for event in key_events if verification_strength(event) == 3),
            key=verification_order,
            default=None,
        )
        selected = latest
        if failure is not None and (
            recovery is None or verification_order(recovery) <= verification_order(failure)
        ):
            selected = failure
        elif verification_strength(latest) == 1:
            selected = latest
        elif truncated is not None:
            selected = truncated
        preferred_event_ids.add(str(selected.get("event_id")))

    ordered = sorted(
        merged.values(),
        key=lambda item: (
            0 if str(item.get("event_id")) in critical_event_ids else 1,
            0 if str(item.get("event_id")) in preferred_event_ids else 1,
            0 if str(item.get("event_id")) in reserved_event_ids else 1,
            0 if target_task_id and str(item.get("task_id") or "") == str(target_task_id) else 1,
            -int(item.get("sequence") or 0),
            str(item.get("event_id")),
        )
    )
    return ordered[:bounded_limit]


def _read_source_snapshot(
    *,
    workflow_id: str,
    task_id: str,
    store: Any = None,
    db_path: Optional[Path] = None,
    explicit_task: Optional[Mapping[str, Any]] = None,
    explicit_workflow: Optional[Mapping[str, Any]] = None,
    planned_links: Optional[Sequence[Mapping[str, Any]]] = None,
    max_events: int = 300,
    max_findings: int = 500,
    max_observations: int = 300,
    max_collaborations: int = 50,
    max_evals: int = 100,
) -> Dict[str, Any]:
    conn = state_db.get_db_connection(db_path or _db_path(store))
    try:
        conn.execute("BEGIN;")
        task_row = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ? AND length(payload_json) <= 200000",
            (str(task_id),),
        ).fetchone()
        task = state_db._decode_task_row(task_row) if task_row is not None else dict(explicit_task or {})
        if not task:
            raise ValueError(f"task not found: {task_id}")
        if str(task.get("task_id") or "") != str(task_id):
            raise ValueError("explicit task does not match requested task_id")
        if str(task.get("workflow_id") or "") != str(workflow_id):
            raise ValueError("task does not belong to requested workflow")

        workflow_row = conn.execute(
            "SELECT * FROM workflows WHERE workflow_id = ?", (str(workflow_id),)
        ).fetchone()
        workflow = _decode_workflow_row(workflow_row) if workflow_row is not None else dict(explicit_workflow or {})
        workflow.setdefault("workflow_id", workflow_id)

        task_rows = conn.execute(
            "SELECT * FROM tasks WHERE workflow_id = ? AND length(payload_json) <= 200000 ORDER BY created_at ASC, task_id ASC LIMIT 1001",
            (str(workflow_id),),
        ).fetchall()
        if len(task_rows) > 1000:
            raise ValueError("workflow task set exceeds the bounded WorkingContext source limit")
        tasks = [state_db._decode_task_row(row) for row in task_rows]
        if not any(str(item.get("task_id")) == str(task_id) for item in tasks):
            tasks.append(dict(task))
        run_scope = collab_scope_for_task(task) or _task_run(task)
        if not run_scope:
            raise ValueError("task has no resolvable workflow execution scope")
        scoped_tasks = [
            item for item in tasks
            if (collab_scope_for_task(item) or _task_run(item)) == run_scope
        ]
        explicit_execution_scope = bool(task.get("workflow_run_id") or task.get("execution_id"))
        if explicit_execution_scope:
            allowed_runs = {_task_run(item) for item in scoped_tasks if _task_run(item)}
        else:
            # Legacy tasks may share a workflow_id while belonging to distinct
            # execution runs.  Without an explicit workflow execution id,
            # only the target run is authoritative until a collaboration event
            # proves a sibling handoff link.
            target_run = _task_run(task)
            allowed_runs = {target_run} if target_run else set()
        allowed_runs.discard(None)
        if not allowed_runs:
            raise ValueError("workflow execution scope has no run identity")
        task_by_id = {str(item.get("task_id")): item for item in scoped_tasks}
        if not explicit_execution_scope and planned_links:
            allowed_link_nodes = set(_dependency_ids(task, workflow))
            for link in planned_links:
                from_id = str(link.get("from_task_id") or "")
                to_id = str(link.get("to_task_id") or "")
                if str(task_id) not in {from_id, to_id}:
                    continue
                linked_id = to_id if from_id == str(task_id) else from_id
                linked_task = task_by_id.get(linked_id)
                linked_node = str((linked_task or {}).get("node") or (linked_task or {}).get("stage") or "")
                if linked_task is None or linked_node not in allowed_link_nodes:
                    continue
                target_legacy_run = task.get("run_id")
                linked_legacy_run = linked_task.get("run_id")
                if (
                    not task.get("workflow_run_id")
                    and not task.get("execution_id")
                    and not linked_task.get("workflow_run_id")
                    and not linked_task.get("execution_id")
                    and (
                        not target_legacy_run
                        or not linked_legacy_run
                        or str(target_legacy_run) != str(linked_legacy_run)
                    )
                ):
                    continue
                linked_run = _task_run(linked_task)
                if linked_run:
                    allowed_runs.add(linked_run)
            if allowed_runs:
                scoped_tasks = [
                    item for item in scoped_tasks if _task_run(item) in allowed_runs
                ]
                task_by_id = {str(item.get("task_id")): item for item in scoped_tasks}

        placeholders = ",".join("?" for _ in allowed_runs)
        run_values = list(allowed_runs)
        event_rows = conn.execute(
            f"""SELECT * FROM events
                WHERE source = 'trajectory'
                  AND run_id IN ({placeholders})
                  AND event_type IN ({",".join("?" for _ in RELEVANT_EVENT_TYPES)})
                  AND length(payload_json) <= 20000
                ORDER BY sequence DESC, id DESC LIMIT ?""",
            (*run_values, *sorted(RELEVANT_EVENT_TYPES), int(max_events)),
        ).fetchall()
        events = _merge_verification_events(
            conn,
            run_values,
            [_decode_event_row(row) for row in event_rows],
            task_by_id=task_by_id,
            allowed_runs=allowed_runs,
            workflow_id=str(workflow_id),
            run_scope=run_scope,
            target_task_id=str(task_id),
            limit=int(max_events),
        )

        finding_rows = conn.execute(
            f"""SELECT * FROM trajectory_findings
                WHERE run_id IN ({placeholders})
                  AND length(COALESCE(summary, '')) <= 20000
                  AND length(COALESCE(metadata_json, '{{}}')) <= 20000
                  AND length(COALESCE(evidence_json, '[]')) <= 20000
                ORDER BY created_at DESC, rowid DESC LIMIT ?""",
            (*run_values, int(max_findings)),
        ).fetchall()
        findings = [state_db._decode_finding_row(row) for row in finding_rows]
        critical_finding_rows = conn.execute(
            f"""SELECT * FROM trajectory_findings
                WHERE run_id IN ({placeholders})
                  AND (LOWER(COALESCE(severity, '')) = 'critical'
                       OR finding_type IN ('verification_failure', 'repeated_failure'))
                  AND LOWER(COALESCE(status, 'open')) NOT IN ('resolved', 'closed', 'superseded')
                ORDER BY CASE WHEN task_id = ? THEN 0 ELSE 1 END,
                         created_at DESC, rowid DESC
                LIMIT ?""",
            (*run_values, str(task_id), int(max_findings)),
        ).fetchall()
        findings_by_id = {
            str(finding.get("finding_id")): finding for finding in findings
        }
        for row in critical_finding_rows:
            finding = state_db._decode_finding_row(row)
            findings_by_id[str(finding.get("finding_id"))] = finding
        findings = list(findings_by_id.values())
        findings.sort(key=lambda finding: (
            0 if str(finding.get("task_id") or "") == str(task_id)
            and (str(finding.get("severity") or "").lower() == "critical"
                 or finding.get("finding_type") in {"verification_failure", "repeated_failure"})
            else 1,
            -float(finding.get("created_at") or 0.0),
            str(finding.get("finding_id") or ""),
        ))
        findings = findings[:max_findings]
        relation_targets = set()
        for finding in findings:
            metadata = finding.get("metadata") if isinstance(finding.get("metadata"), Mapping) else {}
            for key in ("supersedes", "superseded_by"):
                values = finding.get(key) if finding.get(key) is not None else metadata.get(key)
                if isinstance(values, (str, Mapping)):
                    values = [values]
                for value in values or []:
                    if isinstance(value, Mapping):
                        value = value.get("finding_id") or value.get("id")
                    if value:
                        relation_targets.add(str(value))
        known_finding_ids = {str(item.get("finding_id")) for item in findings}
        missing_relation_ids = sorted(relation_targets - known_finding_ids)
        if missing_relation_ids:
            relation_placeholders = ",".join("?" for _ in missing_relation_ids)
            relation_rows = conn.execute(
                f"""SELECT * FROM trajectory_findings
                    WHERE finding_id IN ({relation_placeholders})
                      AND run_id IN ({placeholders})
                      AND workflow_id = ?
                    ORDER BY created_at ASC, rowid ASC LIMIT ?""",
                (*missing_relation_ids, *run_values, str(workflow_id), int(max_findings)),
            ).fetchall()
            findings.extend(state_db._decode_finding_row(row) for row in relation_rows)

        observation_rows = conn.execute(
            f"""SELECT * FROM observations
                WHERE run_id IN ({placeholders})
                ORDER BY created_at DESC, observation_id DESC LIMIT ?""",
            (*run_values, int(max_observations)),
        ).fetchall()
        observations = []
        for row in observation_rows:
            decoded = state_db._decode_observation_row(row)
            observations.append({
                "observation_id": decoded.get("observation_id"),
                "run_id": decoded.get("run_id"),
                "task_id": decoded.get("task_id"),
                "workflow_id": decoded.get("workflow_id"),
                "source_type": decoded.get("source_type"),
                "source_ref": decoded.get("source_ref"),
                "sha256": decoded.get("sha256"),
                "excerpt": decoded.get("excerpt"),
                "created_at": decoded.get("created_at"),
            })

        task_ids = list(task_by_id)
        task_placeholders = ",".join("?" for _ in task_ids) or "NULL"
        collab_rows = conn.execute(
            f"""SELECT * FROM collaboration_events
                WHERE run_id = ? AND workflow_id = ?
                  AND (from_task_id IN ({task_placeholders}) OR to_task_id IN ({task_placeholders}))
                ORDER BY created_at DESC, event_id DESC LIMIT ?""",
            (run_scope, str(workflow_id), *task_ids, *task_ids, int(max_collaborations)),
        ).fetchall()
        incoming_collab_rows = conn.execute(
            """SELECT * FROM collaboration_events
                WHERE run_id = ? AND workflow_id = ? AND to_task_id = ?
                ORDER BY created_at DESC, event_id DESC LIMIT ?""",
            (run_scope, str(workflow_id), str(task_id), int(max_collaborations)),
        ).fetchall()
        collaborations_by_id = {
            str(state_db._decode_collaboration_row(row)["event_id"]): state_db._decode_collaboration_row(row)
            for row in collab_rows
        }
        for row in incoming_collab_rows:
            decoded = state_db._decode_collaboration_row(row)
            collaborations_by_id[str(decoded["event_id"])] = decoded
        collaborations = list(collaborations_by_id.values())
        collaborations.sort(key=lambda event: (
            0 if str(event.get("to_task_id") or "") == str(task_id) else 1,
            -float(event.get("created_at") or 0.0),
            str(event.get("event_id") or ""),
        ))
        collaborations = collaborations[:max_collaborations]
        if not explicit_execution_scope:
            linked_task_ids = {
                linked_id
                for event in collaborations
                if str(event.get("type") or "").upper() == "HANDOFF"
                for linked_id in (
                    str(event.get("from_task_id") or ""),
                    str(event.get("to_task_id") or ""),
                )
                if str(task_id) in {
                    str(event.get("from_task_id") or ""),
                    str(event.get("to_task_id") or ""),
                }
            }
            for linked_id in linked_task_ids:
                linked_task = task_by_id.get(linked_id)
                linked_run = _task_run(linked_task) if linked_task else None
                if linked_run:
                    allowed_runs.add(linked_run)
            scoped_tasks = [
                item for item in scoped_tasks if _task_run(item) in allowed_runs
            ]
            task_by_id = {str(item.get("task_id")): item for item in scoped_tasks}
            if len(allowed_runs) > 1:
                placeholders = ",".join("?" for _ in allowed_runs)
                run_values = list(allowed_runs)
                events = _merge_verification_events(
                    conn,
                    run_values,
                    [
                        _decode_event_row(row) for row in conn.execute(
                            f"""SELECT * FROM events
                                WHERE source = 'trajectory'
                                  AND run_id IN ({placeholders})
                                  AND event_type IN ({",".join("?" for _ in RELEVANT_EVENT_TYPES)})
                                  AND length(payload_json) <= 20000
                                ORDER BY sequence DESC, id DESC LIMIT ?""",
                            (*run_values, *sorted(RELEVANT_EVENT_TYPES), int(max_events)),
                        ).fetchall()
                    ],
                    task_by_id=task_by_id,
                    allowed_runs=allowed_runs,
                    workflow_id=str(workflow_id),
                    run_scope=run_scope,
                )
                findings = [
                    state_db._decode_finding_row(row) for row in conn.execute(
                        f"""SELECT * FROM trajectory_findings
                            WHERE run_id IN ({placeholders})
                              AND length(COALESCE(summary, '')) <= 20000
                              AND length(COALESCE(metadata_json, '{{}}')) <= 20000
                              AND length(COALESCE(evidence_json, '[]')) <= 20000
                            ORDER BY created_at DESC, rowid DESC LIMIT ?""",
                        (*run_values, int(max_findings)),
                    ).fetchall()
                ]
                relation_targets = set()
                for finding in findings:
                    metadata = finding.get("metadata") if isinstance(finding.get("metadata"), Mapping) else {}
                    for key in ("supersedes", "superseded_by"):
                        values = finding.get(key) if finding.get(key) is not None else metadata.get(key)
                        if isinstance(values, (str, Mapping)):
                            values = [values]
                        for value in values or []:
                            if isinstance(value, Mapping):
                                value = value.get("finding_id") or value.get("id")
                            if value:
                                relation_targets.add(str(value))
                known_finding_ids = {str(item.get("finding_id")) for item in findings}
                missing_relation_ids = sorted(relation_targets - known_finding_ids)
                if missing_relation_ids:
                    relation_placeholders = ",".join("?" for _ in missing_relation_ids)
                    relation_rows = conn.execute(
                        f"""SELECT * FROM trajectory_findings
                            WHERE finding_id IN ({relation_placeholders})
                              AND run_id IN ({placeholders})
                              AND workflow_id = ?
                            ORDER BY created_at ASC, rowid ASC LIMIT ?""",
                        (*missing_relation_ids, *run_values, str(workflow_id), int(max_findings)),
                    ).fetchall()
                    findings.extend(state_db._decode_finding_row(row) for row in relation_rows)
                observations = []
                for row in conn.execute(
                    f"""SELECT * FROM observations
                        WHERE run_id IN ({placeholders})
                        ORDER BY created_at DESC, observation_id DESC LIMIT ?""",
                    (*run_values, int(max_observations)),
                ).fetchall():
                    decoded = state_db._decode_observation_row(row)
                    observations.append({
                        "observation_id": decoded.get("observation_id"),
                        "run_id": decoded.get("run_id"),
                        "task_id": decoded.get("task_id"),
                        "workflow_id": decoded.get("workflow_id"),
                        "source_type": decoded.get("source_type"),
                        "source_ref": decoded.get("source_ref"),
                        "sha256": decoded.get("sha256"),
                        "excerpt": decoded.get("excerpt"),
                        "created_at": decoded.get("created_at"),
                    })

        eval_task_ids = list(task_by_id)
        eval_task_filter_parts = ["task_id IS NULL"]
        if eval_task_ids:
            eval_task_filter_parts.append(
                f"task_id IN ({','.join('?' for _ in eval_task_ids)})"
            )
        eval_task_filter = " OR ".join(eval_task_filter_parts)
        eval_limit = max(0, int(max_evals))
        if eval_limit == 0:
            eval_rows = []
        else:
            eval_window_limit = max(
                eval_limit,
                max(1, len(eval_task_ids), len(run_values)) * 3,
            )
            eval_rows = conn.execute(
                f"""SELECT * FROM (
                        SELECT er.*,
                            ROW_NUMBER() OVER (
                                PARTITION BY er.run_id, COALESCE(er.task_id, '')
                                ORDER BY er.revision DESC, er.rowid DESC
                            ) AS latest_rank,
                            ROW_NUMBER() OVER (
                                PARTITION BY er.run_id, COALESCE(er.task_id, '')
                                ORDER BY CASE WHEN er.verification_passed = 0 THEN 0 ELSE 1 END,
                                         er.revision DESC, er.rowid DESC
                            ) AS strict_rank,
                            ROW_NUMBER() OVER (
                                PARTITION BY er.run_id, COALESCE(er.task_id, '')
                                ORDER BY CASE WHEN er.verification_passed = 1 THEN 0 ELSE 1 END,
                                         er.revision DESC, er.rowid DESC
                            ) AS pass_rank
                          FROM eval_results er
                         WHERE er.run_id IN ({placeholders})
                           AND (er.workflow_id = ? OR (
                               er.task_id IS NOT NULL AND (er.workflow_id IS NULL OR er.workflow_id = '')
                           ))
                           AND ({eval_task_filter})
                           AND length(COALESCE(er.evidence_json, 'null')) <= 20000
                           AND length(COALESCE(er.warnings_json, '[]')) <= 20000
                    ) WHERE latest_rank = 1 OR strict_rank = 1 OR pass_rank = 1
                    ORDER BY CASE WHEN task_id = ? THEN 0 ELSE 1 END,
                             run_id ASC, revision DESC, eval_id ASC
                    LIMIT ?""",
                (
                    *run_values,
                    str(workflow_id),
                    *eval_task_ids,
                    str(task_id),
                    eval_window_limit,
                ),
            ).fetchall()
        oversized_eval_rows = []
        if eval_limit:
            oversized_eval_rows = conn.execute(
                f"""SELECT er.eval_id, er.run_id, er.revision, er.verification_passed,
                           er.requirements_satisfied, er.final_status, er.task_id,
                           er.workflow_id, er.created_at
                      FROM eval_results er
                     WHERE er.run_id IN ({placeholders})
                       AND (er.workflow_id = ? OR (
                           er.task_id IS NOT NULL AND (er.workflow_id IS NULL OR er.workflow_id = '')
                       ))
                       AND ({eval_task_filter})
                       AND (length(COALESCE(er.evidence_json, 'null')) > 20000
                            OR length(COALESCE(er.warnings_json, '[]')) > 20000)
                     ORDER BY CASE WHEN er.task_id = ? THEN 0 ELSE 1 END,
                              er.revision DESC, er.eval_id ASC
                     LIMIT ?""",
                (
                    *run_values,
                    str(workflow_id),
                    *eval_task_ids,
                    str(task_id),
                    eval_window_limit,
                ),
            ).fetchall()
        evals_by_key: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for row in eval_rows:
            decoded = _decode_eval_row(row)
            if not _source_allowed(
                {
                    "run_id": decoded.get("run_id"),
                    "task_id": decoded.get("task_id"),
                    "workflow_id": decoded.get("workflow_id"),
                },
                task_by_id=task_by_id,
                allowed_runs=allowed_runs,
                workflow_id=str(workflow_id),
                run_scope=run_scope,
            ):
                continue
            key = (str(decoded.get("run_id") or ""), str(decoded.get("task_id") or ""))
            evals_by_key.setdefault(key, []).append(decoded)
        for row in oversized_eval_rows:
            decoded = {
                "eval_id": row["eval_id"],
                "run_id": row["run_id"],
                "revision": int(row["revision"] or 0),
                "task_id": row["task_id"],
                "workflow_id": row["workflow_id"],
                "verification_passed": 0 if row["verification_passed"] in (0, False) else None,
                "requirements_satisfied": row["requirements_satisfied"],
                "final_status": row["final_status"],
                "evidence": None,
                "warnings": [],
                "created_at": row["created_at"],
                "source_truncated": True,
            }
            if not _source_allowed(
                {
                    "run_id": decoded.get("run_id"),
                    "task_id": decoded.get("task_id"),
                    "workflow_id": decoded.get("workflow_id"),
                },
                task_by_id=task_by_id,
                allowed_runs=allowed_runs,
                workflow_id=str(workflow_id),
                run_scope=run_scope,
            ):
                continue
            key = (str(decoded.get("run_id") or ""), str(decoded.get("task_id") or ""))
            evals_by_key.setdefault(key, []).append(decoded)
        evals = [item for items in evals_by_key.values() for item in items]
        preferred_eval_ids: set[str] = set()
        for key_evals in evals_by_key.values():
            latest = max(key_evals, key=lambda item: (
                int(item.get("revision") or 0), str(item.get("eval_id") or "")
            ))
            failure = max(
                (item for item in key_evals if item.get("verification_passed") == 0),
                key=lambda item: (int(item.get("revision") or 0), str(item.get("eval_id") or "")),
                default=None,
            )
            recovery = max(
                (item for item in key_evals if item.get("verification_passed") == 1),
                key=lambda item: (int(item.get("revision") or 0), str(item.get("eval_id") or "")),
                default=None,
            )
            truncated = max(
                (item for item in key_evals if item.get("source_truncated") is True),
                key=lambda item: (int(item.get("revision") or 0), str(item.get("eval_id") or "")),
                default=None,
            )
            selected = latest
            if failure is not None and (
                recovery is None
                or (int(recovery.get("revision") or 0), str(recovery.get("eval_id") or ""))
                <= (int(failure.get("revision") or 0), str(failure.get("eval_id") or ""))
            ):
                selected = failure
            elif latest.get("verification_passed") == 1:
                selected = latest
            elif truncated is not None:
                selected = truncated
            preferred_eval_ids.add(str(selected.get("eval_id")))
        evals.sort(key=lambda item: (
            0 if str(item.get("eval_id")) in preferred_eval_ids else 1,
            0 if str(item.get("task_id") or "") == str(task_id) else 1,
            -int(item.get("revision") or 0),
            str(item.get("eval_id") or ""),
        ))
        evals = evals[:eval_limit]
        source_clock_row = conn.execute(
            """
            SELECT revision
            FROM working_context_source_clock
            WHERE run_scope = ? AND workflow_id = ?
            """,
            (str(run_scope), str(workflow_id)),
        ).fetchone()
        source_clock = int(source_clock_row["revision"] if source_clock_row else 0)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()

    snapshot = {
        "task": task,
        "workflow": workflow,
        "tasks": scoped_tasks,
        "task_by_id": task_by_id,
        "allowed_runs": allowed_runs,
        "run_scope": run_scope,
        "events": events,
        "findings": findings,
        "observations": observations,
        "collaborations": collaborations,
        "evals": evals,
        "source_clock": source_clock,
    }
    snapshot["source_version"] = _hash(_source_projection(snapshot))
    return snapshot


def _stable_runtime_projection(task: Mapping[str, Any]) -> Dict[str, Any]:
    runtime = task.get("runtime") if isinstance(task.get("runtime"), Mapping) else {}
    status = runtime.get("status")
    return {"status": status} if status is not None else {}


def _bounded_source_value(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return "[bounded]"
    if isinstance(value, str):
        if len(value) <= 4000:
            return value
        return {
            "bounded_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
            "original_chars": len(value),
        }
    if isinstance(value, list):
        return [_bounded_source_value(item, depth + 1) for item in value[:100]]
    if isinstance(value, dict):
        return {
            str(key): _bounded_source_value(item, depth + 1)
            for key, item in list(value.items())[:100]
        }
    return value


def _workflow_config_projection(
    workflow: Mapping[str, Any], task: Mapping[str, Any],
) -> Any:
    config = workflow.get("config")
    if not isinstance(config, Mapping):
        return config
    item_keys = [
        key for key in ("nodes", "stages")
        if isinstance(config.get(key), list)
    ]
    if not item_keys:
        return config
    relevant_ids = {
        str(value)
        for value in (
            task.get("node"), task.get("stage"),
            *(task.get("depends_on") or [] if isinstance(task.get("depends_on"), list) else []),
        )
        if value
    }
    projection = {key: value for key, value in config.items() if key not in item_keys}
    for item_key in item_keys:
        items = config[item_key]
        relevant_items = [
            item for item in items
            if isinstance(item, Mapping)
            and str(item.get("id") or item.get("stage") or item.get("name") or "") in relevant_ids
        ]
        canonical_items = json.dumps(items, ensure_ascii=False, sort_keys=True, default=str)
        projection[item_key] = relevant_items
        projection[f"all_{item_key}_sha256"] = hashlib.sha256(
            canonical_items.encode("utf-8")
        ).hexdigest()
    return projection


def _source_projection(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    """Keep the source-version hash bounded to fields that affect compilation."""
    return _bounded_source_value({
        "task": {
            key: snapshot["task"].get(key)
            for key in (
                "task_id", "workflow_id", "run_id", "workflow_run_id", "execution_id",
                "node", "stage", "agent", "agent_role", "status", "goal", "blocker",
                "blocked_reason", "open_blockers", "acceptance_criteria", "requirements",
                "depends_on", "created_at",
                "acceptance", "stage_verdict", "stage_verdict_note", "artifacts", "artifact_refs",
                "changed_artifacts", "deliverables", "blockers", "open_questions", "questions",
                "question", "decision_question", "acceptance_gap", "decision",
            )
        },
        "runtime": _stable_runtime_projection(snapshot["task"]),
        "workflow": {
            **{
                key: snapshot["workflow"].get(key)
                for key in ("workflow_id", "title", "status", "current_stage")
            },
            "config": _workflow_config_projection(
                snapshot["workflow"], snapshot["task"],
            ),
        },
        "tasks": [
            {
                **{key: item.get(key) for key in (
                    "task_id", "run_id", "workflow_run_id", "execution_id", "node", "stage",
                    "agent", "agent_role", "status", "goal", "blocker", "blocked_reason",
                    "open_blockers", "acceptance_criteria", "requirements", "acceptance",
                    "depends_on", "created_at",
                    "stage_verdict", "stage_verdict_note", "artifacts", "artifact_refs",
                    "changed_artifacts", "deliverables", "blockers", "open_questions", "questions",
                    "question", "decision_question", "acceptance_gap", "decision",
                )},
                "runtime": _stable_runtime_projection(item),
            }
            for item in snapshot.get("tasks", [])
        ],
        "events": snapshot.get("events", []),
        "findings": snapshot.get("findings", []),
        "observations": snapshot.get("observations", []),
        "collaborations": [
            {key: value for key, value in event.items() if key != "context_refs"}
            for event in snapshot.get("collaborations", [])
            if str(event.get("type") or "").upper() == "HANDOFF"
        ],
        "evals": snapshot.get("evals", []),
    })


# ---------------------------------------------------------------------------
# Provenance and validity


def _source_allowed(
    record: Mapping[str, Any],
    *,
    task_by_id: Mapping[str, Mapping[str, Any]],
    allowed_runs: Set[str],
    workflow_id: str,
    run_scope: str,
) -> bool:
    task_id = record.get("task_id")
    run_id = record.get("run_id")
    if record.get("workflow_id") and str(record["workflow_id"]) != str(workflow_id):
        return False
    if task_id:
        task = task_by_id.get(str(task_id))
        if task is None:
            return False
        if str(task.get("workflow_id") or "") != str(workflow_id):
            return False
        if (collab_scope_for_task(task) or _task_run(task)) != run_scope:
            return False
        if run_id and _task_run(task) != str(run_id):
            return False
        return True
    return bool(
        record.get("workflow_id")
        and run_id
        and str(run_id) in allowed_runs
    )


def _task_value(task: Mapping[str, Any], *, include_goal: bool = True) -> Dict[str, Any]:
    value = {
        "task_id": task.get("task_id"),
        "node": task.get("node") or task.get("stage"),
        "status": task.get("status"),
    }
    if include_goal and task.get("goal"):
        value["goal"] = task.get("goal")
    if task.get("stage_verdict"):
        value["stage_verdict"] = task.get("stage_verdict")
    return value


def _workflow_node(workflow: Mapping[str, Any], node_id: Optional[str]) -> Dict[str, Any]:
    config = workflow.get("config") or {}
    nodes = config.get("nodes") if isinstance(config, dict) else None
    for node in nodes or []:
        if isinstance(node, Mapping) and str(node.get("id")) == str(node_id):
            return dict(node)
    stages = config.get("stages") if isinstance(config, dict) else None
    for index, stage in enumerate(stages or []):
        if not isinstance(stage, Mapping):
            continue
        stage_id = str(stage.get("key") or stage.get("id") or "")
        if stage_id == str(node_id):
            result = dict(stage)
            result.setdefault("id", stage_id)
            result.setdefault("depends_on", [str(stages[index - 1].get("key"))] if index else [])
            return result
    return {}


def _dependency_ids(task: Mapping[str, Any], workflow: Mapping[str, Any]) -> List[str]:
    node = _workflow_node(workflow, task.get("node") or task.get("stage"))
    values = node.get("depends_on") or task.get("depends_on") or []
    return [str(value) for value in values if str(value).strip()]


def _scope_task_for_node(
    node_id: str,
    tasks: Sequence[Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    candidates = [
        task for task in tasks
        if str(task.get("node") or task.get("stage") or "") == str(node_id)
    ]
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda item: (
            _safe_float(item.get("created_at")),
            str(item.get("task_id") or ""),
        ),
    )[-1]


def _requirements(task: Mapping[str, Any], node: Mapping[str, Any]) -> List[str]:
    values: List[str] = []
    for key in ("acceptance_criteria", "requirements", "acceptance"):
        for value in _as_list(task.get(key)):
            values.append(str(value))
    for key in ("purpose", "rules"):
        for value in _as_list(node.get(key)):
            values.append(str(value))
    return list(dict.fromkeys(value for value in values if value.strip()))
