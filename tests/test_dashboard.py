"""Dashboard V3 regression: human-readable 4-section payload with real clocks.

Covers herdr/dashboard.py pure builder:
- tasks / attention / deliveries / stuck sections present
- every timestamp has real-clock text (YYYY-MM-DD HH:MM:SS)
- attention excludes superseded/cleaned, keeps blocked/failed/escalated
- bounded limits, input not mutated
"""

import copy
import re

from herdr import dashboard as d

CLOCK_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def _task(tid, status, updated=1700000000, **kw):
    t = {
        "task_id": tid,
        "workflow_id": kw.get("workflow_id", "wf-1"),
        "node": kw.get("node", "implementation"),
        "stage": kw.get("stage", "implementation"),
        "agent": kw.get("agent", "codex"),
        "status": status,
        "goal": kw.get("goal", f"goal-{tid}"),
        "created_at": kw.get("created_at", updated - 100),
        "updated_at": updated,
        "last_activity_at": kw.get("last_activity_at", updated),
    }
    t.update(kw)
    return t


def test_dashboard_sections_and_real_clocks():
    now = 1700003600.0
    tasks = [
        _task("t-working", "working", updated=1700003000),
        _task("t-blocked", "blocked", updated=1700003100,
              blocker="门禁等你拍板", stage_verdict="blocked"),
        _task("t-done", "cleaned", updated=1700003200),
    ]
    payload = d.build_dashboard(
        tasks,
        blockers=[tasks[1]],
        actions_by_task={"t-blocked": {
            "title": "去会签放行/打回",
            "effect": "你确认产物后点放行或打回，无需敲命令。",
            "command_line": "bin/herdr-task advance wf-1",
        }},
        deliveries=[{
            "workflow_id": "wf-1", "delivery_branch": "herdr/wf-1",
            "candidate_sha": "abc123", "review_task": "t-done",
            "test_gate": "t-done", "ts": 1700003200,
        }],
        stalls={"wf-1": {"message": "推进悬挂已超 45 秒",
                         "suggested_action": "retry_advance",
                         "target_task_id": None}},
        anomalies=[{"kind": "FAILED", "task_id": "t-old",
                    "workflow_id": "wf-1", "last_activity_at": 1700001000}],
        now=now,
    )
    assert set(payload) >= {"generated_at", "generated_at_text", "tasks",
                            "attention", "deliveries", "stuck", "counts"}
    assert CLOCK_RE.match(payload["generated_at_text"]), payload["generated_at_text"]
    for t in payload["tasks"]:
        assert CLOCK_RE.match(t["updated_at_text"]), t
    for a in payload["attention"]:
        assert CLOCK_RE.match(a["updated_at_text"]), a
        assert a["default_action"], "attention must carry default action"
    for dl in payload["deliveries"]:
        assert CLOCK_RE.match(dl["updated_at_text"]), dl
    assert payload["counts"]["tasks"] == 3


def test_attention_excludes_history_keeps_escalated():
    now = 1700003600.0
    tasks = [
        _task("t-old", "superseded", updated=1700001000,
              superseded_by="t-new", stage_verdict="blocked"),
        _task("t-clean", "cleaned", updated=1700002000),
        _task("t-esc", "working", updated=1700003000,
              finalize_escalated=True,
              finalize_escalate_reason="终化冲突需你确认"),
    ]
    before = copy.deepcopy(tasks)
    payload = d.build_dashboard(tasks, blockers=[], actions_by_task={},
                                deliveries=[], stalls={}, anomalies=[],
                                now=now)
    ids = [a["task_id"] for a in payload["attention"]]
    assert "t-old" not in ids
    assert "t-clean" not in ids
    assert "t-esc" in ids
    assert tasks == before, "builder must not mutate inputs"


def test_dashboard_bounded_limits():
    now = 1700003600.0
    tasks = [_task(f"t-{i:03d}", "working", updated=1700000000 + i)
             for i in range(60)]
    deliveries = [{
        "workflow_id": "wf-1", "delivery_branch": "b",
        "candidate_sha": f"sha{i:03d}", "review_task": "t",
        "test_gate": "t", "ts": 1700000000 + i,
    } for i in range(30)]
    payload = d.build_dashboard(tasks, blockers=[], actions_by_task={},
                                deliveries=deliveries, stalls={},
                                anomalies=[], now=now, limits={"tasks": 20, "deliveries": 10})
    assert len(payload["tasks"]) <= 20
    assert len(payload["deliveries"]) <= 10


def test_dashboard_carries_pane_runtime_and_executable_action():
    now = 1700003600.0
    tasks = [_task("t-block", "blocked", updated=1700003100,
                   pane_id="w1:p1", blocker="等你拍板",
                   stage_verdict="blocked")]
    payload = d.build_dashboard(
        tasks,
        blockers=[tasks[0]],
        actions_by_task={"t-block": {
            "title": "换人重跑",
            "effect": "旧任务保留备查。",
            "command_line": "bin/herdr-task launch --task-id x",
            "api_endpoint": "/api/controller/execute-action",
            "api_payload": {"type": "launch", "task_id": "x"},
        }},
        deliveries=[], stalls={},
        anomalies=[],
        runtimes={"w1:p1": {"agent_status": "working"}},
        now=now,
    )
    t = next(x for x in payload["tasks"] if x["task_id"] == "t-block")
    assert t["pane_id"] == "w1:p1"
    assert t["runtime_status"] == "working"
    a = next(x for x in payload["attention"] if x["task_id"] == "t-block")
    assert a["pane_id"] == "w1:p1"
    assert a["endpoint"] == "/api/controller/execute-action"
    assert a["payload"]["task_id"] == "x"
    assert a["node"] == "implementation"
