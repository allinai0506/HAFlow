"""Scheduler decision facts (HAFlow PR #107).

Join Gate 与候选冻结的审计证据层:把调度器每次关键判定以标准
WorkflowEvent 形式落盘,回答"这次 test/review 到底验证了哪个版本、
门禁为什么通过/拒绝"。读/写都经过 StateStore,无新表、无新枚举。

事件类型(均为 payload 扩字段,不改语义名):
- scheduler_decision:ready 计算 + 派发明细(每次 check_advance 落一条)。
- candidate_frozen:某候选 SHA 被冻结为期望版本(幂等,同 SHA 只记首条)。
- join_gate_verdict:汇聚门禁每次判定结果(pass/wait/block/stale/mismatch)。
- reverification_decision:候选变化后每个 verifier 的 reuse/rerun 判定
  (PR #108;幂等键是 episode 级 decision_identity,不是候选集合成员)。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

EVENT_SCHEDULER_DECISION = "scheduler_decision"
EVENT_CANDIDATE_FROZEN = "candidate_frozen"
EVENT_JOIN_GATE_VERDICT = "join_gate_verdict"
EVENT_REVERIFICATION_DECISION = "reverification_decision"
EVENT_SELECTIVE_REPLAN_DECISION = "selective_replan_decision"

SCHEDULER_EVENT_SOURCE = "critical-path-scheduler"
SELECTIVE_REPLAN_EVENT_SOURCE = "selective-replan"

#: 只有 ``pass`` 结论可以成为 reuse 来源(PR #108 §13:只有 PASS 可以复用)。
REUSE_SOURCE_VERDICT = "pass"

#: How far back a reuse lookup scans. A workflow accumulates one fact pair per
#: candidate rotation, so a few hundred entries covers any realistic history;
#: the cap exists to bound a hot-path read, not to truncate real evidence.
REUSE_LOOKUP_SCAN_LIMIT = 200

#: Payload fields that must match for two facts to count as the same fact.
#: ``created_at`` is excluded on purpose: it is stamped per write, so two
#: writers of an otherwise identical decision must still be recognised as
#: replays of each other.
_COMPARED_FIELDS = (
    "verifier", "decision", "from_candidate_sha", "to_candidate_sha",
    "source_verdict", "source_verified_candidate_sha", "policy_version",
    "policy_identity", "reason", "changed_paths", "reusable_scope",
)


def _store(db_path: Optional[Path] = None):
    from .state_store import get_state_store

    return get_state_store(db_path) if db_path is not None else get_state_store()


def shas_identical(left, right):
    """Strict revision equality (no ancestor semantics)."""
    from .scheduler import shas_identical as _impl

    return _impl(left, right)


def decision_identity(workflow_id, from_candidate_sha, to_candidate_sha,
                      verifier, policy_version, policy_identity="",
                      episode_id=""):
    from .reverification import decision_identity as _impl

    return _impl(workflow_id, from_candidate_sha, to_candidate_sha,
                 verifier, policy_version, policy_identity, episode_id)


def record_scheduler_decision(
    workflow_id: str,
    ready_node_ids: List[str],
    expected_candidate_sha: str = "",
    dispatched: Optional[List[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """记录一次 ready 计算 + 派发决策(审计证据,best-effort 由调用方决定)。"""
    payload: Dict[str, Any] = {
        "ready_nodes": list(ready_node_ids or []),
        "expected_candidate_sha": str(expected_candidate_sha or ""),
        "dispatched": list(dispatched or []),
    }
    if extra:
        payload.update(extra)
    return _store(db_path).record_event(
        EVENT_SCHEDULER_DECISION,
        payload,
        workflow_id=workflow_id,
        source=SCHEDULER_EVENT_SOURCE,
        timestamp=time.time(),
    )


def record_candidate_frozen(
    workflow_id, candidate_sha, source_node="", delivery_branch="", db_path=None,
    expected_episode_id=None, publication_event_id=None,
):
    """Explicit candidate publication, serialized with episode CAS.

    Same-SHA replay is idempotent; A -> B -> A remains three episodes.
    Scheduler consumption additionally binds a durable integration publication.
    """
    from . import recovery_store, state_db
    sha = str(candidate_sha or "").strip()
    if not re.fullmatch(r'[0-9a-fA-F]{40}', sha):
        raise ValueError('candidate_sha must be a full commit SHA')
    store = _store(db_path)
    with recovery_store._transaction(store.db_path) as conn:
        workflow_row = conn.execute('SELECT metadata_json,created_at FROM workflows WHERE workflow_id=?', (workflow_id,)).fetchone()
        if workflow_row is None:
            raise ValueError('workflow not found')
        metadata = json.loads(workflow_row['metadata_json'] or '{}')
        epoch = max(float(workflow_row['created_at'] or 0), float(metadata.get('reopened_at') or 0))
        row = conn.execute("SELECT * FROM events WHERE workflow_id=? AND event_type='candidate_frozen' AND source=? AND timestamp>=? ORDER BY id DESC LIMIT 1",
                           (workflow_id, SCHEDULER_EVENT_SOURCE, epoch)).fetchone()
        latest_id = row['id'] if row else 0
        if expected_episode_id is not None and expected_episode_id != latest_id:
            raise ValueError('candidate episode changed')
        prior = json.loads(row['payload_json']) if row else {}
        if publication_event_id is not None:
            if publication_event_id <= latest_id:
                return {'status': 'exists', 'event': dict(row)}
            pub = conn.execute("SELECT * FROM events WHERE id=? AND workflow_id=? AND event_type='candidate_published' AND source='herdr-task'",
                               (publication_event_id, workflow_id)).fetchone()
            workflow, _, tasks = recovery_store._snapshot(conn, workflow_id)
            value = json.loads(pub['payload_json']) if pub else {}
            task = next((t for t in tasks if pub and t['task_id'] == pub['task_id']), {})
            if value.get('expected_episode_id') != latest_id:
                raise ValueError('candidate publication episode changed')
            if (not pub or value.get('candidate_sha') != sha or not workflow.get('execution_id')
                    or value.get('execution_id') != workflow['execution_id']
                    or task.get('run_id') != pub['run_id'] or task.get('execution_id') != workflow['execution_id']
                    or task.get('integrated_commit') != sha or task.get('superseded_by')
                    or task.get('status') not in {'integrated', 'cleanup_ready', 'cleaned'}):
                raise ValueError('candidate publication identity unknown')
            if publication_event_id <= prior.get('publication_event_id', 0):
                return {'status': 'exists', 'event': dict(row)}
        if prior.get('candidate_sha') == sha:
            return {'status': 'exists', 'event': dict(row)}
        event = state_db.record_event({'event_type': EVENT_CANDIDATE_FROZEN,
            'workflow_id': workflow_id, 'source': SCHEDULER_EVENT_SOURCE,
            'payload': {'candidate_sha': sha, 'source_node': str(source_node or ''),
                        'delivery_branch': str(delivery_branch or ''),
                        'rotated_from': prior.get('candidate_sha', ''),
                        'publication_event_id': publication_event_id or 0}}, conn=conn)
        return {'status': 'created', 'event': event}


def publish_integrated_candidate(store, task_id):
    """The integrate entry publishes once per immutable Task/Run result."""
    from . import recovery_store, state_db
    with recovery_store._transaction(store.db_path) as conn:
        row = conn.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        task = state_db._decode_task_row(row) if row else {}
        wid = task.get('workflow_id')
        workflow, _, _ = recovery_store._snapshot(conn, wid)
        sha = task.get('integrated_commit')
        if (task.get('status') not in {'integrated', 'cleanup_ready', 'cleaned'}
                or not re.fullmatch(r'[0-9a-f]{40}', sha or '') or task.get('commit') != sha
                or not task.get('run_id') or not workflow.get('execution_id')
                or task.get('execution_id') != workflow['execution_id'] or not task.get('integration_ref')):
            raise ValueError('integrated publication identity unknown')
        prior = conn.execute("SELECT * FROM events WHERE workflow_id=? AND task_id=? AND run_id=? AND event_type='candidate_published' AND source='herdr-task' AND json_extract(payload_json,'$.candidate_sha')=? ORDER BY id DESC LIMIT 1",
                             (wid, task_id, task['run_id'], sha)).fetchone()
        if prior:
            return dict(prior)
        parent_episode = task.get('integration_publication_episode_id')
        if sha == workflow.get('candidate_sha'):
            return {'status': 'already_frozen', 'candidate_episode_id': workflow['candidate_episode_id']}
        if type(parent_episode) is not int or parent_episode != workflow.get('candidate_episode_id'):
            raise ValueError('integrated publication episode unknown or changed')
        return state_db.record_event({'event_type': 'candidate_published', 'workflow_id': wid,
            'task_id': task_id, 'node_id': task.get('node') or task.get('stage'), 'run_id': task['run_id'],
            'source': 'herdr-task', 'payload': {'candidate_sha': sha,
                'execution_id': workflow['execution_id'], 'integration_ref': task['integration_ref'],
                'expected_episode_id': parent_episode}}, conn=conn)


def list_candidate_frozen_events(workflow_id, db_path=None):
    """List frozen-candidate events in chronological order."""
    from . import recovery_store, state_db
    try:
        conn = state_db.get_readonly_db_connection(state_db.resolve_state_db_path(db_path))
    except FileNotFoundError:
        return []
    try:
        conn.execute('BEGIN')
        workflow, _ = recovery_store._workflow_snapshot(conn, workflow_id)
        epoch = max(float(workflow.get('created_at') or 0), float(workflow.get('reopened_at') or 0))
        return [dict(row, payload=json.loads(row['payload_json'])) for row in conn.execute(
            "SELECT * FROM events WHERE workflow_id=? AND event_type='candidate_frozen' AND source=? AND timestamp>=? ORDER BY id",
            (workflow_id, SCHEDULER_EVENT_SOURCE, epoch))]
    except ValueError as exc:
        if str(exc) == 'workflow not found':
            return []
        raise
    finally:
        conn.close()


def latest_frozen_candidate_sha(workflow_id, db_path=None):
    """Return the newest frozen candidate SHA, or "" when none."""
    from . import recovery_store, state_db
    try:
        workflow, _, _ = recovery_store.read_snapshot(state_db.resolve_state_db_path(db_path), workflow_id)
    except (FileNotFoundError, ValueError):
        return ''
    return str(workflow.get('candidate_sha') or '')


def list_reverification_decisions(workflow_id, db_path=None, limit=None):
    """List reverification decision events in chronological order.

    Bounded by ``limit`` (newest N) so a reader on the hot sweep path cannot
    scan an unbounded event history. Omitted, the read is unbounded — use it
    only where the whole episode history is genuinely the input.
    """
    kwargs = {}
    if limit is not None:
        # list_events applies LIMIT after ordering, so the newest N requires the
        # descending direction to be requested explicitly.
        kwargs = {"desc": True, "limit": int(limit)}
        events = _store(db_path).list_events(
            workflow_id=workflow_id,
            event_type=EVENT_REVERIFICATION_DECISION,
            **kwargs)
        return list(reversed(events))
    return _store(db_path).list_events(
        workflow_id=workflow_id,
        event_type=EVENT_REVERIFICATION_DECISION,
    )


def find_reuse_fact(workflow_id, verifier, candidate_sha, policy_identity=None,
                    episode_id=None, db_path=None):
    """Return the newest reuse fact bound to this exact verifier/candidate/episode.

    ``None`` when there is none. Every binding here is exact on purpose:

    - ``episode_id`` is the id of the ``candidate_frozen`` event that authorised
      the decision. Binding on the candidate **SHA** is not enough: a rollback
      re-freezes a SHA that was already frozen, and a SHA-keyed lookup would
      resurrect a previous episode's reuse and mark a candidate covered that was
      never verified. Binding the episode is what makes "facts do not cross
      episodes" true, and it also makes a repeated (from, to) pair in a later
      round a distinct, separately decided episode.
    - ``policy_identity`` scopes the lookup to the *resolved* policy, not a
      version label, so narrowing the scope really does revoke reuse.
    - ``to_candidate_sha`` keeps the §23 binding on top of all that.

    An empty expectation returns no fact: "I do not know which episode or policy
    applies" is not a reason to honour anything.
    """
    wanted_verifier = str(verifier or "").strip()
    wanted_sha = str(candidate_sha or "").strip()
    wanted_policy = str(policy_identity or "").strip()
    wanted_episode = "" if episode_id in (None, "") else str(episode_id)
    if (not wanted_verifier or not wanted_sha or not wanted_policy
            or not wanted_episode):
        return None
    # One workflow's verification rounds are few, but the read is on the sweep
    # path and runs once per branch per sweep, so it is bounded anyway.
    for event in reversed(list_reverification_decisions(
            workflow_id, db_path=db_path, limit=REUSE_LOOKUP_SCAN_LIMIT)):
        payload = event.get("payload") or {}
        if str(payload.get("decision") or "") != "reuse":
            continue
        if str(payload.get("verifier") or "").strip() != wanted_verifier:
            continue
        if str(payload.get("to_candidate_sha") or "").strip() != wanted_sha:
            continue
        if str(payload.get("candidate_frozen_event_id") or "") != wanted_episode:
            continue
        if str(payload.get("policy_identity") or "") != wanted_policy:
            continue
        if str(payload.get("source_verdict") or "") != REUSE_SOURCE_VERDICT:
            continue
        if not str(payload.get("source_verified_candidate_sha") or "").strip():
            continue
        if not str(payload.get("source_candidate_sha") or "").strip():
            continue
        return {"event_id": event.get("id"), "timestamp": event.get("timestamp"),
                **payload}
    return None


def record_reverification_decision(workflow_id, decision, db_path=None):
    """Record one reverification decision (immutable, idempotent per episode).

    A reuse decision is a *derived* fact, never an edit of the source task
    (§5): `test(A) PASS` keeps meaning "test verified A" forever. Reuse is
    proven by what this fact carries — the source task id, the source verdict,
    the SHA that source was actually bound to, the changed paths, and the
    policy version that allowed the reuse.

    Idempotency is keyed on `decision_identity`, which mixes workflow,
    from/to candidate, verifier, the resolved policy and the **candidate-freeze
    episode**. A -> B and B -> A are different episodes, and so is a later round
    that happens to reach the same (from, to) pair again — a rollback re-freezes
    a SHA that was already frozen, and the fact from the earlier round must not
    be inherited (§26).

    The check-and-insert runs inside one SQLite write transaction
    (`state_db.record_event_if_absent`), so two concurrent sweeps cannot both
    insert the same decision. That is the existing `BEGIN IMMEDIATE` write lock
    the repository already uses for every other fact that has to be exactly-once
    — no new table, no new lock system.

    Returns {"status": "created" | "exists" | "rejected", ...}. Rejection is
    fail-closed: a reuse claim that does not carry a passed, candidate-bound
    source is never persisted at all.
    """
    payload = dict(decision or {})
    verdict = str(payload.get("decision") or "").strip()
    verifier = str(payload.get("verifier") or "").strip()
    from_sha = str(payload.get("from_candidate_sha") or "").strip()
    to_sha = str(payload.get("to_candidate_sha") or "").strip()
    episode = "" if payload.get("candidate_frozen_event_id") in (None, "") else str(
        payload.get("candidate_frozen_event_id"))

    if not verdict or not verifier or not to_sha or not from_sha:
        # `from_candidate_sha` is required for BOTH decisions: a decision that
        # does not name the candidate it replaces is not a reverification
        # decision at all, and admitting it would let a "reuse" claim appear
        # without a provable A -> B episode behind it.
        return {"status": "rejected", "reason": "missing_identity_fields"}
    if not episode:
        # Without the freeze that authorised it, a fact cannot be attributed to
        # a round, and an unattributable fact must never satisfy a lookup.
        return {"status": "rejected", "reason": "missing_candidate_episode"}
    if verdict == "reuse":
        if from_sha == to_sha:
            # A -> A is the same candidate, not a reverification episode (§17).
            return {"status": "rejected", "reason": "same_candidate"}
        if str(payload.get("source_verdict") or "") != REUSE_SOURCE_VERDICT:
            return {"status": "rejected", "reason": "source_verdict_not_pass"}
        bound = str(payload.get("source_verified_candidate_sha") or "").strip()
        if not bound:
            return {"status": "rejected", "reason": "source_not_bound"}
        if not shas_identical(bound, from_sha):
            return {"status": "rejected", "reason": "source_bound_to_other_candidate"}
        # The dispatch claim must bind to the same candidate as the
        # completion evidence. A task told to verify B whose verdict-time read
        # says A is a claim/evidence mismatch — PR #107's join gate refuses it
        # as proof of either revision, so it cannot be promoted into proof of A
        # here either.
        claimed = str(payload.get("source_candidate_sha") or "").strip()
        if not claimed or not shas_identical(claimed, from_sha):
            return {"status": "rejected", "reason": "source_claim_mismatch"}

    policy_version = str(payload.get("policy_version") or "").strip()
    policy_fp = str(payload.get("policy_identity") or "").strip()
    # The identity is always recomputed from the fact's own fields, never taken
    # from the payload. A caller-supplied identity that disagrees with the
    # fields would file a decision under a key that does not describe it, and
    # the dedup lookup would then treat two different decisions as one.
    identity = decision_identity(
        workflow_id, from_sha, to_sha, verifier, policy_version, policy_fp,
        episode)
    claimed = str(payload.get("decision_identity") or "").strip()
    if claimed and claimed != identity:
        return {
            "status": "rejected",
            "reason": "decision_identity_mismatch",
            "expected": identity,
        }
    created_at = float(payload.get("created_at") or 0) or time.time()

    body = {
        "decision_identity": identity,
        "verifier": verifier,
        "decision": verdict,
        "from_candidate_sha": from_sha,
        "to_candidate_sha": to_sha,
        # The freeze this decision belongs to. Carried so a reader can reject a
        # fact from a previous round even when the candidate SHA repeats.
        "candidate_frozen_event_id": episode,
        "source_task_id": str(payload.get("source_task_id") or ""),
        "source_verdict": str(payload.get("source_verdict") or ""),
        "source_candidate_sha": str(payload.get("source_candidate_sha") or ""),
        "source_verified_candidate_sha": str(
            payload.get("source_verified_candidate_sha") or ""),
        "changed_paths": [str(p) for p in (payload.get("changed_paths") or [])],
        "changed_entries": [dict(e) for e in (payload.get("changed_entries") or [])
                            if isinstance(e, dict)],
        # The authorising scope travels with the fact. Without it the ledger
        # records *that* a reuse happened and *which* files changed, but not
        # *which* declared scope permitted it — so the decision could not be
        # explained from the fact alone, which is the whole point of persisting
        # it as an immutable, self-describing record.
        "reusable_scope": [str(p) for p in
                           (payload.get("reusable_scope") or [])],
        "out_of_scope_paths": [str(p) for p in
                               (payload.get("out_of_scope_paths") or [])],
        "policy": payload.get("policy") or {},
        "policy_version": policy_version,
        "policy_identity": policy_fp,
        "reason": str(payload.get("reason") or ""),
        "created_at": created_at,
    }

    # Check-and-insert under one write lock. A plain read-then-compare lets two
    # concurrent sweeps both observe "absent" and both insert, putting a
    # duplicate into an append-only audit ledger. `state_db` provides the
    # exactly-once write the same way every other fact in this repository gets
    # it: the existing SQLite `BEGIN IMMEDIATE` write lock, no new table and no
    # new lock system.
    from . import state_db

    outcome = state_db.record_event_if_absent(
        {
            "workflow_id": workflow_id,
            "node_id": verifier,
            "event_type": EVENT_REVERIFICATION_DECISION,
            "timestamp": created_at,
            "payload": body,
            "source": SCHEDULER_EVENT_SOURCE,
        },
        identity_field="decision_identity",
        identity_value=identity,
        compare_fields=_COMPARED_FIELDS,
        scan_limit=REUSE_LOOKUP_SCAN_LIMIT,
        db_path=db_path,
    )
    if outcome.get("status") == "rejected":
        # Same identity, different content: the caller handed us a decision
        # that does not match the identity it claims. Treating it as a replay
        # would file it under the wrong identity and hide the change.
        return outcome
    return outcome


# ============================================================
# Selective Replan v1 facts (PR #110)
# ============================================================

#: Payload fields that must match for two replan facts to count as the same
#: episode's decision. ``replan_id`` deliberately excludes the target lists,
#: so a same-episode target change lands on the same identity and is caught
#: here as identity_content_mismatch instead of filing a contradictory fact.
_REPLAN_COMPARED_FIELDS = (
    "gate_node", "gate_task_id", "gate_task_version", "gate_candidate_sha",
    "retry_node", "mode", "requested_task_ids", "target_task_ids",
    "target_lineage_roots", "preserved_task_ids", "policy_version",
    "policy_identity", "reason",
)

REPLAN_LOOKUP_SCAN_LIMIT = 200


def _replan_identity_for(workflow_id, payload):
    from .selective_replan import replan_identity

    return replan_identity(
        workflow_id,
        payload.get("gate_task_id"),
        payload.get("gate_task_version"),
        payload.get("gate_candidate_sha"),
        payload.get("retry_node"),
        payload.get("policy_identity"),
    )


def record_selective_replan_decision(workflow_id, decision, db_path=None):
    """Record one selective-replan decision (immutable, idempotent per episode).

    Exactly-once semantics come from ``state_db.record_event_if_absent`` —
    the same SQLite ``BEGIN IMMEDIATE`` write lock every other fact in this
    repository uses; no new table, no new lock system.

    Fail-closed validation:
    - identity is always recomputed from the fact's own fields; a caller
      claiming a different ``replan_id`` is rejected;
    - a decision that cannot name its gate episode (task id, task version,
      verified candidate sha) is rejected rather than persisted;
    - same identity with different content (e.g. targets edited after the
      fact) is rejected as ``identity_content_mismatch`` by the shared
      primitive.

    Returns {"status": "created" | "exists" | "rejected", ...}.
    """
    payload = dict(decision or {})
    workflow_id = str(workflow_id or "").strip()
    gate_task_id = str(payload.get("gate_task_id") or "").strip()
    retry_node = str(payload.get("retry_node") or "").strip()
    mode = str(payload.get("mode") or "").strip()
    if not workflow_id or not gate_task_id or not retry_node:
        return {"status": "rejected", "reason": "missing_identity_fields"}
    if mode not in ("selective", "legacy_fallback"):
        return {"status": "rejected", "reason": "unknown_mode"}
    try:
        version = int(payload.get("gate_task_version"))
    except (TypeError, ValueError):
        return {"status": "rejected", "reason": "gate_task_version_unproven"}
    gate_candidate_sha = str(payload.get("gate_candidate_sha") or "").strip()
    if not gate_candidate_sha:
        return {"status": "rejected", "reason": "gate_candidate_unproven"}

    body = {
        "replan_id": "",
        "workflow_id": workflow_id,
        "gate_node": str(payload.get("gate_node") or ""),
        "gate_task_id": gate_task_id,
        "gate_task_version": version,
        "gate_candidate_sha": gate_candidate_sha,
        # P1-3: replacement 基线审计字段。决策时刻冻结候选的可证明分支,
        #  informational(不进 _REPLAN_COMPARED_FIELDS):老事实无此键时
        #  仍按原 compare 判定 exists/mismatch,不因升级产生误拒。
        "source_candidate_branch": str(
            payload.get("source_candidate_branch") or ""
        ).strip(),
        "gate_note": str(payload.get("gate_note") or ""),
        "retry_node": retry_node,
        "mode": mode,
        "requested_task_ids": [
            str(t) for t in (payload.get("requested_task_ids") or [])
        ],
        "target_task_ids": [
            str(t) for t in (payload.get("target_task_ids") or [])
        ],
        "target_lineage_roots": [
            str(r) for r in (payload.get("target_lineage_roots") or [])
        ],
        "preserved_task_ids": [
            str(t) for t in (payload.get("preserved_task_ids") or [])
        ],
        "policy_version": str(payload.get("policy_version") or ""),
        "policy_identity": str(payload.get("policy_identity") or ""),
        "reason": str(payload.get("reason") or ""),
        "created_at": float(payload.get("created_at") or 0) or time.time(),
    }
    identity = _replan_identity_for(workflow_id, body)
    claimed = str(payload.get("replan_id") or "").strip()
    if claimed and claimed != identity:
        return {
            "status": "rejected",
            "reason": "replan_identity_mismatch",
            "expected": identity,
        }
    body["replan_id"] = identity

    from . import state_db

    return state_db.record_event_if_absent(
        {
            "workflow_id": workflow_id,
            "node_id": str(payload.get("gate_node") or ""),
            "task_id": gate_task_id,
            "event_type": EVENT_SELECTIVE_REPLAN_DECISION,
            "timestamp": body["created_at"],
            "payload": body,
            "source": SELECTIVE_REPLAN_EVENT_SOURCE,
        },
        identity_field="replan_id",
        identity_value=identity,
        compare_fields=_REPLAN_COMPARED_FIELDS,
        scan_limit=REPLAN_LOOKUP_SCAN_LIMIT,
        db_path=db_path,
    )


def list_selective_replan_decisions(workflow_id, db_path=None, limit=None):
    """List selective-replan decision events in chronological order."""
    kwargs = {}
    if limit is not None:
        kwargs = {"desc": True, "limit": int(limit)}
        events = _store(db_path).list_events(
            workflow_id=workflow_id,
            event_type=EVENT_SELECTIVE_REPLAN_DECISION,
            **kwargs)
        return list(reversed(events))
    return _store(db_path).list_events(
        workflow_id=workflow_id,
        event_type=EVENT_SELECTIVE_REPLAN_DECISION,
    )


def find_selective_replan_decision(workflow_id, replan_id, db_path=None):
    """Return the newest fact payload with this exact replan identity, or None.

    An empty identity returns no fact: "I do not know which episode applies"
    is not a reason to honour anything (same fail-closed rule as the #108
    reuse lookup).
    """
    wanted = str(replan_id or "").strip()
    if not wanted:
        return None
    for event in reversed(list_selective_replan_decisions(
            workflow_id, db_path=db_path, limit=REPLAN_LOOKUP_SCAN_LIMIT)):
        payload = event.get("payload") or {}
        if str(payload.get("replan_id") or "") != wanted:
            continue
        return {"event_id": event.get("id"),
                "timestamp": event.get("timestamp"), **payload}
    return None


def latest_selective_replan_for_node(workflow_id, retry_node, db_path=None):
    """Newest *selective* fact whose retry_node matches, or None.

    Legacy-fallback facts carry no targets and therefore never imply pending
    redispatch; they are skipped here on purpose.
    """
    node = str(retry_node or "").strip()
    if not node:
        return None
    for event in reversed(list_selective_replan_decisions(
            workflow_id, db_path=db_path, limit=REPLAN_LOOKUP_SCAN_LIMIT)):
        payload = event.get("payload") or {}
        if str(payload.get("mode") or "") != "selective":
            continue
        if str(payload.get("retry_node") or "") != node:
            continue
        return {"event_id": event.get("id"),
                "timestamp": event.get("timestamp"), **payload}
    return None


def list_selective_replan_for_node(workflow_id, retry_node, db_path=None):
    """All *selective* facts for one retry_node, chronological.

    P1-2: test ∥ review 并行 blocked 会在同一轮留下两条单门禁事实
    (test→[B], review→[C])。只读最新一条会丢掉另一条的目标;
    awaiting/notes 必须对该节点全部 selective 事实取并集,
    已落定谱系(无补派候选)自然不再触发,故历史事实滞留无害。
    """
    node = str(retry_node or "").strip()
    if not node:
        return []
    matched = []
    for event in list_selective_replan_decisions(
            workflow_id, db_path=db_path, limit=REPLAN_LOOKUP_SCAN_LIMIT):
        payload = event.get("payload") or {}
        if str(payload.get("mode") or "") != "selective":
            continue
        if str(payload.get("retry_node") or "") != node:
            continue
        matched.append({"event_id": event.get("id"),
                        "timestamp": event.get("timestamp"), **payload})
    return matched


def record_join_gate_verdict(
    workflow_id,
    gate_node_id,
    passed,
    reason,
    details=None,
    db_path=None,
):
    """Record one join-gate verdict (audit trail, newest wins on read)."""
    payload = dict(details or {})
    payload.update({
        "gate": str(gate_node_id or ""),
        "passed": bool(passed),
        "reason": str(reason or ""),
    })
    return _store(db_path).record_event(
        EVENT_JOIN_GATE_VERDICT,
        payload,
        workflow_id=workflow_id,
        node_id=str(gate_node_id or ""),
        source=SCHEDULER_EVENT_SOURCE,
        timestamp=time.time(),
    )
