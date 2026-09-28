"""Scheduler decision facts (HAFlow PR #107).

Join Gate 与候选冻结的审计证据层:把调度器每次关键判定以标准
WorkflowEvent 形式落盘,回答"这次 test/review 到底验证了哪个版本、
门禁为什么通过/拒绝"。读/写都经过 StateStore,无新表、无新枚举。

事件类型(均为 payload 扩字段,不改语义名):
- scheduler_decision:ready 计算 + 派发明细(每次 check_advance 落一条)。
- candidate_frozen:某候选 SHA 被冻结为期望版本(幂等,同 SHA 只记首条)。
- join_gate_verdict:汇聚门禁每次判定结果(pass/wait/block/stale/mismatch)。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

EVENT_SCHEDULER_DECISION = "scheduler_decision"
EVENT_CANDIDATE_FROZEN = "candidate_frozen"
EVENT_JOIN_GATE_VERDICT = "join_gate_verdict"

SCHEDULER_EVENT_SOURCE = "critical-path-scheduler"


def _store(db_path: Optional[Path] = None):
    from .state_store import get_state_store

    return get_state_store(db_path) if db_path is not None else get_state_store()


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

    Same-SHA refreeze is a no-op returning the first record ("exists");
    a different SHA freezes anew and marks the rotation ("created").
    """
    sha = str(candidate_sha or "").strip()
    if not sha:
        raise ValueError("candidate_sha is required")
    store = _store(db_path)
    prior = list_candidate_frozen_events(workflow_id, db_path=db_path)
    for event in prior:
        payload = event.get("payload") or {}
        if str(payload.get("candidate_sha") or "") == sha:
            return {"status": "exists", "event": event}
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
