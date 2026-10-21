"""context source validation persistence; extracted without changing behavior."""

import json
import sqlite3
from typing import Any, Dict, List, Mapping, Optional, Tuple
from herdr.transitions import COMPLETED_TASK_STATUSES


def _valid_context_source_ref(value: Any) -> bool:
    if not isinstance(value, str) or ":" not in value:
        return False
    prefix, rest = value.split(":", 1)
    return prefix in {
        "task", "workflow", "trajectory", "finding", "observation",
        "collaboration", "eval", "policy", "evidence", "artifact",
    } and bool(rest.strip()) and not any(char.isspace() for char in rest)

def _validate_context_source_existence(
    conn: sqlite3.Connection, context: Dict[str, Any],
) -> None:
    refs = set()
    item_bindings: Dict[str, List[Tuple[Optional[str], Optional[str]]]] = {}
    refs.update(str(ref) for ref in (context.get("source_refs") or []))
    refs.update(str(ref) for ref in (context.get("goal_source_ref"), context.get("next_action_source_ref")) if ref)
    refs.update(str(ref) for ref in (context.get("current_state_refs") or {}).values())
    for field_name in (
        "completed", "artifacts", "evidence", "findings", "decisions", "blockers",
        "open_questions", "verification", "handoffs",
    ):
        for item in context.get(field_name) or []:
            item_ref = str(item.get("source_ref") or "")
            refs.add(item_ref)
            item_bindings.setdefault(item_ref, []).append((
                str(item.get("source_task")) if item.get("source_task") else None,
                str(item.get("source_run")) if item.get("source_run") else None,
            ))
            for evidence_ref in item.get("evidence_refs") or []:
                refs.add(str(evidence_ref))

    taskless_allowed_runs: set[str] = set()
    _ARTIFACT_KEYS = ("artifacts", "artifact_refs", "changed_artifacts", "deliverables")
    _BLOCKER_KEYS = ("blocker", "blocked_reason", "open_blockers", "blockers")
    _QUESTION_KEYS = ("open_questions", "questions", "question", "decision_question", "acceptance_gap")

    def _length_expr(key: str) -> str:
        return (
            "COALESCE(CASE WHEN payload_json IS NULL THEN 0 "
            "WHEN NOT json_valid(payload_json) THEN 0 "
            f"WHEN json_type(payload_json, '$.{key}') = 'array' "
            f"THEN COALESCE(json_array_length(payload_json, '$.{key}'), 0) "
            f"WHEN json_extract(payload_json, '$.{key}') IS NULL THEN 0 "
            f"WHEN json_extract(payload_json, '$.{key}') = '' THEN 0 "
            "ELSE 1 END, 0)"
        )

    def _scalar_expr(key: str) -> str:
        return (
            "CASE WHEN json_valid(payload_json) "
            f"THEN substr(json_extract(payload_json, '$.{key}'), 1, 4000) END"
        )

    def _raw_expr(key: str) -> str:
        return (
            "CASE WHEN json_valid(payload_json) "
            f"THEN json_extract(payload_json, '$.{key}') END"
        )

    def _decode_list_field(raw):
        if raw is None:
            return []
        if isinstance(raw, (int, float)):
            return [raw] if raw not in (None, "") else []
        if isinstance(raw, str):
            stripped = raw.strip()
            if stripped == "":
                return []
            if stripped.startswith("["):
                if len(stripped) > 512 * 1024:
                    return None
                try:
                    arr = json.loads(stripped)
                except (TypeError, json.JSONDecodeError):
                    return []
                if isinstance(arr, list):
                    return list(arr)
                return [arr]
            if stripped.startswith("{"):
                if len(stripped) > 512 * 1024:
                    return None
                try:
                    obj = json.loads(stripped)
                except (TypeError, json.JSONDecodeError):
                    return [raw]
                return [obj]
            return [raw]
        return [raw]

    def task_record(task_id: str):
        row = conn.execute(
            f"""SELECT task_id, workflow_id, node, stage, agent, status,
                        stage_verdict, blocker, goal,
                        {_scalar_expr("run_id")} AS j_run_id,
                        {_scalar_expr("workflow_run_id")} AS j_workflow_run_id,
                        {_scalar_expr("execution_id")} AS j_execution_id,
                        {_scalar_expr("agent_role")} AS j_agent_role,
                        {_scalar_expr("status")} AS j_status,
                        {_scalar_expr("stage_verdict")} AS j_stage_verdict,
                        {_scalar_expr("decision")} AS j_decision,
                        {_scalar_expr("goal")} AS j_goal,
                        {_scalar_expr("workflow_id")} AS j_workflow_id,
                        {_scalar_expr("node")} AS j_node,
                        {_scalar_expr("stage")} AS j_stage,
                        {_scalar_expr("agent")} AS j_agent,
                        {_length_expr("artifacts")} AS len_artifacts,
                        {_length_expr("artifact_refs")} AS len_artifact_refs,
                        {_length_expr("changed_artifacts")} AS len_changed_artifacts,
                        {_length_expr("deliverables")} AS len_deliverables,
                        {_length_expr("blocker")} AS len_blocker,
                        {_length_expr("blocked_reason")} AS len_blocked_reason,
                        {_length_expr("open_blockers")} AS len_open_blockers,
                        {_length_expr("blockers")} AS len_blockers,
                        {_length_expr("open_questions")} AS len_open_questions,
                        {_length_expr("questions")} AS len_questions,
                        {_length_expr("question")} AS len_question,
                        {_length_expr("decision_question")} AS len_decision_question,
                        {_length_expr("acceptance_gap")} AS len_acceptance_gap,
                        {_raw_expr("artifacts")} AS raw_artifacts,
                        {_raw_expr("artifact_refs")} AS raw_artifact_refs,
                        {_raw_expr("changed_artifacts")} AS raw_changed_artifacts,
                        {_raw_expr("deliverables")} AS raw_deliverables,
                        {_raw_expr("blocker")} AS raw_blocker,
                        {_raw_expr("blocked_reason")} AS raw_blocked_reason,
                        {_raw_expr("open_blockers")} AS raw_open_blockers,
                        {_raw_expr("blockers")} AS raw_blockers,
                        {_raw_expr("open_questions")} AS raw_open_questions,
                        {_raw_expr("questions")} AS raw_questions,
                        {_raw_expr("question")} AS raw_question,
                        {_raw_expr("decision_question")} AS raw_decision_question,
                        {_raw_expr("acceptance_gap")} AS raw_acceptance_gap,
                        CASE WHEN payload_json IS NULL OR json_valid(payload_json)
                             THEN 1 ELSE 0 END AS payload_valid
                 FROM tasks WHERE task_id = ? LIMIT 1""",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        payload_valid = bool(row["payload_valid"])
        def _pick(column_value, json_value):
            if column_value not in (None, ""):
                return column_value
            if not payload_valid:
                return json_value if json_value not in (None, "") else ""
            return json_value if json_value not in (None, "") else ""
        status_value = _pick(row["status"], row["j_status"])
        stage_verdict_value = _pick(row["stage_verdict"], row["j_stage_verdict"])
        blocker_value = _pick(row["blocker"], None)
        goal_value = _pick(row["goal"], row["j_goal"])
        run_value = row["j_run_id"] if payload_valid and row["j_run_id"] not in (None, "") else ""
        effective_run_id = str(run_value) if run_value else f"run_{task_id}"
        scope_value = (
            (row["j_workflow_run_id"] if payload_valid else None)
            or (row["j_execution_id"] if payload_valid else None)
            or row["workflow_id"] or ""
        )
        lengths = {
            "artifacts": int(row["len_artifacts"] or 0),
            "artifact_refs": int(row["len_artifact_refs"] or 0),
            "changed_artifacts": int(row["len_changed_artifacts"] or 0),
            "deliverables": int(row["len_deliverables"] or 0),
            "blocker": int(row["len_blocker"] or 0),
            "blocked_reason": int(row["len_blocked_reason"] or 0),
            "open_blockers": int(row["len_open_blockers"] or 0),
            "blockers": int(row["len_blockers"] or 0),
            "open_questions": int(row["len_open_questions"] or 0),
            "questions": int(row["len_questions"] or 0),
            "question": int(row["len_question"] or 0),
            "decision_question": int(row["len_decision_question"] or 0),
            "acceptance_gap": int(row["len_acceptance_gap"] or 0),
        }
        raw_map = {
            "artifacts": row["raw_artifacts"],
            "artifact_refs": row["raw_artifact_refs"],
            "changed_artifacts": row["raw_changed_artifacts"],
            "deliverables": row["raw_deliverables"],
            "blocker": row["raw_blocker"],
            "blocked_reason": row["raw_blocked_reason"],
            "open_blockers": row["raw_open_blockers"],
            "blockers": row["raw_blockers"],
            "open_questions": row["raw_open_questions"],
            "questions": row["raw_questions"],
            "question": row["raw_question"],
            "decision_question": row["raw_decision_question"],
            "acceptance_gap": row["raw_acceptance_gap"],
        }
        lists: Dict[str, Optional[List[Any]]] = {}
        if payload_valid:
            for key, raw in raw_map.items():
                lists[key] = _decode_list_field(raw)
        else:
            for key in raw_map:
                lists[key] = []
        # Column-backed blocker replaces payload blocker (OR semantics in
        # legacy task_record): when the tasks.blocker column is set, it is
        # the authoritative blocker value, not an addition to payload lists.
        if row["blocker"] not in (None, ""):
            lists["blocker"] = [row["blocker"]]
            lengths["blocker"] = 1
        task_payload = {
            "status": status_value,
            "stage_verdict": stage_verdict_value,
            "blocker": blocker_value,
            "goal": goal_value,
            "decision": row["j_decision"] if payload_valid else None,
        }
        return {
            "task_id": task_id,
            "workflow_id": row["workflow_id"] or (row["j_workflow_id"] if payload_valid else None),
            "node": row["node"] or (row["j_node"] if payload_valid else None),
            "stage": row["stage"] or (row["j_stage"] if payload_valid else None),
            "agent": row["agent"] or (row["j_agent"] if payload_valid else None),
            "run_id": effective_run_id,
            "agent_role": row["j_agent_role"] if payload_valid else None,
            "scope": scope_value,
            "payload": task_payload,
            "_lengths": lengths,
            "_lists": lists,
            "_payload_valid": payload_valid,
        }

    def _task_list_element_valid(task_id: str, key: str, local_idx: int, *, artifact: bool = False) -> bool:
        if local_idx < 0:
            return False
        safe_key = str(key)
        if safe_key not in {
            "artifacts", "artifact_refs", "changed_artifacts", "deliverables",
            "blocker", "blocked_reason", "open_blockers", "blockers",
            "open_questions", "questions", "question", "decision_question",
            "acceptance_gap",
        }:
            return False
        row = conn.execute(
            f"""SELECT
                    CASE WHEN json_valid(payload_json)
                         THEN json_type(payload_json, '$.{safe_key}') END AS jtype,
                    CASE WHEN json_valid(payload_json)
                         THEN json_extract(payload_json, '$.{safe_key}[{int(local_idx)}]') END AS arr_elem,
                    CASE WHEN json_valid(payload_json)
                         THEN substr(json_extract(payload_json, '$.{safe_key}'), 1, 4000) END AS scalar_val,
                    CASE WHEN json_valid(payload_json)
                         THEN length(COALESCE(json_extract(payload_json, '$.{safe_key}[{int(local_idx)}]'), '')) END AS arr_len
                 FROM tasks WHERE task_id = ? LIMIT 1""",
            (task_id,),
        ).fetchone()
        if row is None:
            return False
        jtype = row["jtype"]
        if jtype == "array":
            elem = row["arr_elem"]
            if elem is None:
                return False
            if isinstance(elem, str) and elem == "":
                return False
            if artifact and isinstance(elem, str) and elem.lstrip().startswith("{"):
                try:
                    decoded = json.loads(elem)
                except (TypeError, json.JSONDecodeError):
                    return True
                if isinstance(decoded, Mapping):
                    ref = decoded.get("ref") or decoded.get("path") or decoded.get("name")
                    return ref not in (None, "")
                return True
            if artifact and isinstance(elem, str) and len(elem) >= 4000:
                return True
            return True
        if int(local_idx) != 0:
            return False
        scalar = row["scalar_val"]
        if scalar is None:
            return False
        if isinstance(scalar, str):
            if scalar == "":
                return False
            if artifact and scalar.lstrip().startswith("{"):
                if len(scalar) >= 4000:
                    return True
                try:
                    decoded = json.loads(scalar)
                except (TypeError, json.JSONDecodeError):
                    return True
                if isinstance(decoded, Mapping):
                    ref = decoded.get("ref") or decoded.get("path") or decoded.get("name")
                    return ref not in (None, "")
                return True
            return True
        return True

    taskless_scope_by_run: Dict[str, Optional[str]] = {}
    for identity_row in conn.execute(
        """SELECT task_id,
                  COALESCE(NULLIF(CASE WHEN json_valid(payload_json) THEN json_extract(payload_json, '$.run_id') END, ''), 'run_' || task_id) AS run_id,
                  COALESCE(NULLIF(CASE WHEN json_valid(payload_json) THEN json_extract(payload_json, '$.workflow_run_id') END, ''), NULLIF(CASE WHEN json_valid(payload_json) THEN json_extract(payload_json, '$.execution_id') END, ''), workflow_id, '') AS scope,
                  CASE WHEN payload_json IS NULL OR json_valid(payload_json)
                       THEN 1 ELSE 0 END AS identity_valid
           FROM tasks WHERE workflow_id = ?""",
        (str(context.get("workflow_id") or ""),),
    ).fetchall():
        if not identity_row["identity_valid"]:
            continue
        identity_run = str(identity_row["run_id"] or "")
        identity_scope = str(identity_row["scope"] or "")
        if not identity_run or not identity_scope:
            continue
        previous = taskless_scope_by_run.get(identity_run, "__missing__")
        if previous != "__missing__" and previous != identity_scope:
            taskless_scope_by_run[identity_run] = None
        elif previous == "__missing__":
            taskless_scope_by_run[identity_run] = identity_scope

    target_record = task_record(str(context.get("task_id") or ""))
    if (
        target_record
        and str(target_record.get("workflow_id") or "") == str(context.get("workflow_id") or "")
        and str(target_record.get("scope") or "") == str(context.get("run_scope") or "")
    ):
        taskless_allowed_runs.add(str(target_record.get("run_id") or ""))
    if str(context.get("run_scope") or "") != str(context.get("workflow_id") or ""):
        for scoped_row in conn.execute(
            """SELECT COALESCE(NULLIF(CASE WHEN json_valid(payload_json) THEN json_extract(payload_json, '$.run_id') END, ''), 'run_' || task_id) AS run_id
               FROM tasks WHERE workflow_id = ?
                 AND COALESCE(NULLIF(CASE WHEN json_valid(payload_json) THEN json_extract(payload_json, '$.workflow_run_id') END, ''), NULLIF(CASE WHEN json_valid(payload_json) THEN json_extract(payload_json, '$.execution_id') END, ''), workflow_id, '') = ?
                 AND (payload_json IS NULL OR json_valid(payload_json))""",
            (str(context.get("workflow_id") or ""), str(context.get("run_scope") or "")),
        ).fetchall():
            taskless_allowed_runs.add(str(scoped_row["run_id"] or ""))

    def record_scope(record):
        task_id = record.get("task_id")
        if task_id:
            task = task_record(str(task_id))
            if task is None:
                return False
            if (
                str(context.get("run_scope") or "") == str(context.get("workflow_id") or "")
                and str(task_id) != str(context.get("task_id") or "")
                and str(task_id) not in relevant_peer_tasks
            ):
                return False
            if str(task.get("workflow_id") or "") != str(context.get("workflow_id") or ""):
                return False
            if str(task.get("scope") or "") != str(context.get("run_scope") or ""):
                return False
            if record.get("run_id") and str(task.get("run_id") or "") != str(record["run_id"]):
                return False
            return True
        if not record.get("workflow_id"):
            return False
        run_id = str(record.get("run_id") or "")
        if not run_id:
            return False
        mapped_scope = taskless_scope_by_run.get(run_id)
        return mapped_scope is not None and str(mapped_scope) == str(context.get("run_scope") or "")

    def fetch_record(prefix: str, object_id: str):
        if prefix == "task":
            return task_record(object_id)
        if prefix == "workflow":
            row = conn.execute(
                "SELECT workflow_id FROM workflows WHERE workflow_id = ? LIMIT 1",
                (object_id,),
            ).fetchone()
            return {"workflow_id": row["workflow_id"]} if row is not None else None
        if prefix == "observation":
            row = conn.execute(
                "SELECT run_id, task_id, workflow_id FROM observations WHERE observation_id = ? LIMIT 1",
                (object_id,),
            ).fetchone()
        elif prefix == "finding":
            row = conn.execute(
                "SELECT run_id, task_id, workflow_id FROM trajectory_findings WHERE finding_id = ? LIMIT 1",
                (object_id,),
            ).fetchone()
        elif prefix == "eval":
            row = conn.execute(
                "SELECT run_id, task_id, workflow_id FROM eval_results WHERE eval_id = ? LIMIT 1",
                (object_id,),
            ).fetchone()
        elif prefix == "trajectory":
            event_number = object_id.removeprefix("evt_")
            try:
                row = conn.execute(
                    "SELECT run_id, task_id, workflow_id FROM events WHERE id = ? AND source = 'trajectory' LIMIT 1",
                    (int(event_number),),
                ).fetchone()
            except ValueError:
                row = conn.execute(
                    "SELECT run_id, task_id, workflow_id FROM events WHERE source = 'trajectory' AND CASE WHEN json_valid(payload_json) THEN json_extract(payload_json, '$.event_id') END = ? LIMIT 1",
                    (object_id,),
                ).fetchone()
        elif prefix == "collaboration":
            row = conn.execute(
                "SELECT run_id, workflow_id, from_task_id, to_task_id, type, status FROM collaboration_events WHERE event_id = ? LIMIT 1",
                (object_id,),
            ).fetchone()
        else:
            return {"synthetic": True}
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    verified_handoff_tasks: set[str] = set()
    if str(context.get("run_scope") or "") == str(context.get("workflow_id") or ""):
        target_task_id = str(context.get("task_id") or "")
        for ref in refs:
            if not ref.startswith("collaboration:"):
                continue
            object_id = ref.split(":", 1)[1].split(":", 1)[0]
            handoff = fetch_record("collaboration", object_id)
            if not handoff or str(handoff.get("type") or "").upper() != "HANDOFF":
                continue
            endpoints = {
                str(value) for value in (
                    handoff.get("from_task_id"), handoff.get("to_task_id")
                ) if value
            }
            if target_task_id not in endpoints:
                continue
            verified_handoff_tasks.update(endpoints - {target_task_id})

    # Dependency-closure peers are DB-grounded relevance (workflow DAG +
    # latest task per depended node), mirroring the compiler's
    # priority_task_ids. The compiler deliberately includes predecessor
    # evidence in launch-boundary contexts; rejecting it here made every
    # such compile fail ([WORKING_CONTEXT SKIPPED] ValueError) while the
    # save path comment itself is "relevant-aware: current task +
    # dependency closure + handoff peers". Forged/unlinked refs stay
    # rejected: membership is recomputed from the workflow config and the
    # task table, never taken from context content.
    def _dependency_closure_peers() -> set[str]:
        from herdr.context_sources import _dependency_ids, _scope_task_for_node
        peers: set[str] = set()
        try:
            target = task_record(str(context.get("task_id") or ""))
            wf_row = conn.execute(
                "SELECT * FROM workflows WHERE workflow_id = ?",
                (str(context.get("workflow_id") or ""),),
            ).fetchone()
            if target is None or wf_row is None:
                return peers
            from herdr.context_sources import _decode_workflow_row
            workflow = _decode_workflow_row(wf_row)
            rows = conn.execute(
                "SELECT task_id, node, stage, created_at FROM tasks WHERE workflow_id = ?",
                (str(context.get("workflow_id") or ""),),
            ).fetchall()
            candidates = [
                {"task_id": r["task_id"], "node": r["node"],
                 "stage": r["stage"], "created_at": r["created_at"]}
                for r in rows
            ]
            for dependency_id in _dependency_ids(target, workflow):
                dep = _scope_task_for_node(dependency_id, candidates)
                if dep and dep.get("task_id"):
                    peers.add(str(dep["task_id"]))
        except Exception:
            # Fail closed: on any auxiliary error keep the previous
            # strict behavior (own task + verified handoffs only).
            return set()
        return peers

    relevant_peer_tasks: set[str] = set(verified_handoff_tasks)
    if str(context.get("run_scope") or "") == str(context.get("workflow_id") or ""):
        relevant_peer_tasks.update(_dependency_closure_peers())

    def _task_derived_ref_valid(task: Mapping[str, Any], remainder: str) -> bool:
        parts = remainder.split(":")
        if len(parts) == 1:
            return True
        kind = parts[1]
        task_id = str(task.get("task_id") or "")
        data = task.get("payload") if isinstance(task.get("payload"), Mapping) else task
        raw_lists = task.get("_lists") if isinstance(task.get("_lists"), Mapping) else {}
        lengths = task.get("_lengths") if isinstance(task.get("_lengths"), Mapping) else {}
        def _values(key: str):
            items = raw_lists.get(key)
            if items is None:
                return None
            if isinstance(items, list):
                return list(items)
            return [items]
        def _filtered(key: str, *, artifact: bool = False):
            values = _values(key)
            if values is None:
                return None
            result = []
            for value in values:
                item = value
                if artifact and isinstance(item, Mapping):
                    item = item.get("ref") or item.get("path") or item.get("name")
                if item not in (None, ""):
                    result.append(item)
            return result
        def _length(key: str) -> int:
            filtered = _filtered(key)
            if filtered is not None:
                return len(filtered)
            try:
                return int(lengths.get(key) or 0)
            except (TypeError, ValueError):
                return 0
        def _element_valid(key: str, filtered_index: int, *, artifact: bool = False) -> bool:
            filtered = _filtered(key)
            if filtered is not None:
                if filtered_index < 0 or filtered_index >= len(filtered):
                    return False
                if artifact:
                    item = filtered[filtered_index]
                    if isinstance(item, Mapping):
                        item = item.get("ref") or item.get("path") or item.get("name")
                    return item not in (None, "")
                return True
            try:
                size = int(lengths.get(key) or 0)
            except (TypeError, ValueError):
                return False
            if filtered_index < 0 or filtered_index >= size:
                return False
            return _task_list_element_valid(task_id, key, filtered_index, artifact=artifact)
        if kind == "artifact":
            if len(parts) != 3 or not parts[2].isdigit():
                return False
            remaining = int(parts[2])
            for key in _ARTIFACT_KEYS:
                # Rebuild per-key filtered values exactly like candidates:
                # Mapping artifacts count only when ref/path/name is present.
                values = _values(key)
                if values is None:
                    size = _length(key)
                    if remaining < size:
                        return _task_list_element_valid(task_id, key, remaining, artifact=True)
                    remaining -= size
                    continue
                filtered_count = 0
                for value in values:
                    item = value
                    if isinstance(item, Mapping):
                        item = item.get("ref") or item.get("path") or item.get("name")
                    if item in (None, ""):
                        continue
                    if remaining == filtered_count:
                        return True
                    filtered_count += 1
                remaining -= filtered_count
            return False
        if kind == "blocker":
            collections = []
            fallback_to_lengths = False
            for key in _BLOCKER_KEYS:
                filtered = _filtered(key)
                if filtered is None:
                    fallback_to_lengths = True
                    break
                collections.append(filtered)
            if not fallback_to_lengths:
                blockers: List[Any] = [item for group in collections for item in group]
                if not blockers and str(data.get("status") or "") in {"blocked", "failed"}:
                    blockers = [str(data.get("status"))]
                if len(parts) == 2 or (len(parts) == 3 and parts[2] == "current"):
                    return bool(blockers)
                if len(parts) != 3 or not parts[2].isdigit():
                    return False
                index = int(parts[2])
                return 0 <= index < len(blockers)
            total = sum(_length(key) for key in _BLOCKER_KEYS)
            has_status_fallback = (
                total == 0 and str(data.get("status") or "") in {"blocked", "failed"}
            )
            if len(parts) == 2 or (len(parts) == 3 and parts[2] == "current"):
                return bool(total > 0 or has_status_fallback)
            if len(parts) != 3 or not parts[2].isdigit():
                return False
            remaining = int(parts[2])
            if has_status_fallback:
                return remaining == 0
            for key in _BLOCKER_KEYS:
                size = _length(key)
                if remaining < size:
                    return _task_list_element_valid(task_id, key, remaining, artifact=False)
                remaining -= size
            return False
        if kind == "completed":
            return len(parts) == 2 and data.get("status") in COMPLETED_TASK_STATUSES
        if kind == "decision":
            return len(parts) == 2 and bool(data.get("stage_verdict") or data.get("decision"))
        if kind == "open_question":
            if len(parts) != 4 or not parts[2].isdigit() or not parts[3].isdigit():
                return False
            if int(parts[2]) >= len(_QUESTION_KEYS):
                return False
            key = _QUESTION_KEYS[int(parts[2])]
            index = int(parts[3])
            return _element_valid(key, index, artifact=False)
        return False

    for ref in sorted(refs):
        if not ref:
            continue
        if ref.startswith("policy:"):
            if ref in item_bindings:
                raise ValueError(f"working context item cannot use a policy source: {ref}")
            continue
        if ref.startswith(("artifact:", "evidence:")):
            raise ValueError(f"working context source reference is not a stored source: {ref}")
        prefix, remainder = ref.split(":", 1)
        object_id = remainder.split(":", 1)[0]
        if not object_id:
            raise ValueError("working context source reference has no object id")
        record = fetch_record(prefix, object_id)
        if record is None:
            raise ValueError(f"working context source reference does not exist: {ref}")
        if prefix == "task" and not _task_derived_ref_valid(record, remainder):
            raise ValueError(f"working context task source reference has an invalid derived target: {ref}")
        if record.get("workflow_id") and str(record["workflow_id"]) != str(context.get("workflow_id") or ""):
            raise ValueError(f"working context source reference crosses workflow: {ref}")
        bound_task = record.get("task_id") or record.get("to_task_id")
        bound_run = record.get("run_id")
        for source_task, source_run in item_bindings.get(ref, []):
            if prefix == "workflow" and (source_task or source_run):
                raise ValueError(f"working context workflow item cannot carry task/run provenance: {ref}")
            if bound_task:
                if str(source_task or "") != str(bound_task):
                    raise ValueError(f"working context item source_task does not match reference: {ref}")
            elif source_task:
                raise ValueError(f"working context taskless item has a source_task: {ref}")
            if bound_run and str(source_run or "") != str(bound_run):
                raise ValueError(f"working context item source_run does not match reference: {ref}")
        referenced_tasks = [bound_task] if bound_task else []
        if prefix == "collaboration":
            referenced_tasks.extend(
                task_id for task_id in (record.get("from_task_id"), record.get("to_task_id")) if task_id
            )
        for referenced_task in referenced_tasks:
            task = task_record(str(referenced_task))
            if (
                task
                and str(task.get("workflow_id") or "") == str(context.get("workflow_id") or "")
                and str(task.get("scope") or "") == str(context.get("run_scope") or "")
                and (
                    str(context.get("run_scope") or "") != str(context.get("workflow_id") or "")
                    or str(referenced_task) == str(context.get("task_id") or "")
                    or str(referenced_task) in relevant_peer_tasks
                )
            ):
                taskless_allowed_runs.add(str(task.get("run_id") or ""))
        if prefix == "collaboration":
            if str(record.get("run_id") or "") != str(context.get("run_scope") or ""):
                raise ValueError(f"working context collaboration scope mismatch: {ref}")
            for task_id in (record.get("from_task_id"), record.get("to_task_id")):
                if not record_scope({"task_id": task_id}):
                    raise ValueError(f"working context collaboration task scope mismatch: {ref}")
        elif prefix != "workflow" and not record_scope(record):
            raise ValueError(f"working context source reference crosses run scope: {ref}")

    head = conn.execute(
        """
        SELECT 1 FROM working_context_source_heads
        WHERE run_scope = ? AND workflow_id = ? AND task_id = ?
        LIMIT 1
        """,
        (
            str(context.get("run_scope") or ""),
            str(context.get("workflow_id") or ""),
            str(context.get("task_id") or ""),
        ),
    ).fetchone()
    if head is None:
        raise ValueError("working context source revision is not registered")
    target_task = task_record(str(context.get("task_id") or ""))
    if target_task is None or str(target_task.get("workflow_id") or "") != str(context.get("workflow_id") or ""):
        raise ValueError("working context target task is not in the requested workflow")
    if str(target_task.get("scope") or "") != str(context.get("run_scope") or ""):
        raise ValueError("working context target task scope does not match run_scope")
    if str(target_task.get("run_id") or "") != str(context.get("run_id") or ""):
        raise ValueError("working context run_id does not match target task")
