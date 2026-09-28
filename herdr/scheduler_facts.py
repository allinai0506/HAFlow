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

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

EVENT_SCHEDULER_DECISION = "scheduler_decision"
EVENT_CANDIDATE_FROZEN = "candidate_frozen"
EVENT_JOIN_GATE_VERDICT = "join_gate_verdict"
EVENT_REVERIFICATION_DECISION = "reverification_decision"

SCHEDULER_EVENT_SOURCE = "critical-path-scheduler"

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
    workflow_id,
    candidate_sha,
    source_node="",
    delivery_branch="",
    db_path=None,
):
    """Freeze a candidate SHA as the expected revision (idempotent).

    Idempotency is defined against the *latest* freeze only, never against the
    whole history. Candidate identity is an episode, not a set: freezing
    A -> B -> A is a real rotation back to A, and the newest freeze must win.
    Comparing against all prior events would make that third freeze a no-op and
    leave latest_frozen_candidate_sha() reporting B while the live candidate is
    A again — every later verifier would then target a stale expected SHA and
    the workflow would deadlock.
    """
    sha = str(candidate_sha or "").strip()
    if not sha:
        raise ValueError("candidate_sha is required")
    store = _store(db_path)
    prior = list_candidate_frozen_events(workflow_id, db_path=db_path)
    if prior:
        latest = str(
            (prior[-1].get("payload") or {}).get("candidate_sha") or ""
        )
        if latest == sha:
            return {"status": "exists", "event": prior[-1]}
    rotated_from = ""
    if prior:
        rotated_from = str(
            (prior[-1].get("payload") or {}).get("candidate_sha") or ""
        )
    event = store.record_event(
        EVENT_CANDIDATE_FROZEN,
        {
            "candidate_sha": sha,
            "source_node": str(source_node or ""),
            "delivery_branch": str(delivery_branch or ""),
            "rotated_from": rotated_from,
        },
        workflow_id=workflow_id,
        source=SCHEDULER_EVENT_SOURCE,
        timestamp=time.time(),
    )
    return {"status": "created", "event": event}


def list_candidate_frozen_events(workflow_id, db_path=None):
    """List frozen-candidate events in chronological order."""
    return _store(db_path).list_events(
        workflow_id=workflow_id,
        event_type=EVENT_CANDIDATE_FROZEN,
    )


def latest_frozen_candidate_sha(workflow_id, db_path=None):
    """Return the newest frozen candidate SHA, or "" when none."""
    events = list_candidate_frozen_events(workflow_id, db_path=db_path)
    if not events:
        return ""
    return str((events[-1].get("payload") or {}).get("candidate_sha") or "")


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
