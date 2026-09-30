#!/usr/bin/env python3
"""Pipeline / recovery controller actions (herdr/controller_actions.py).

Covers the actions the console could not render before: git finalization
(commit/integrate/finalize), finalize-escalation handling, and a real
re-drive of a live pane (``herdr agent prompt``) that is deliberately
distinct from injected ``steer``.

``commands`` holds *relative* argv; ``command_base`` names the binary the
executor must prefix (``herdr-task`` for the CLI, ``herdr`` for the pane
transport).  Keeping the binary out of the pure core avoids path I/O here.
"""

import pytest

from herdr.controller_actions import (
    collect_workflow_actions,
    generate_progress_actions,
    resolve_workflow_blockers,
)


def _task(tid="impl-x", status="committed", **kw):
    task = {
        "task_id": tid,
        "workflow_id": kw.pop("workflow_id", "wf-001"),
        "node": "implementation",
        "stage": "implementation",
        "status": status,
        "agent": "opencode",
        "integration_mode": kw.pop("integration_mode", "git"),
        "pane_id": kw.pop("pane_id", "w1:p9"),
    }
    task.update(kw)
    return task


def _ids(actions):
    return [a.action_id for a in actions]


def _by_id(actions, suffix):
    return next(a for a in actions if a.action_id.endswith(suffix))


def test_agent_done_offers_accept_and_commit():
    act = _by_id(
        generate_progress_actions(_task(status="agent_done"), {"workflow_id": "wf-001"}),
        ":accept_and_commit",
    )
    assert act.recommended is True
    assert act.group == "pipeline"
    assert act.command_base == "herdr-task"
    assert act.commands == [["set", "impl-x", "completed"], ["commit", "impl-x"]]
    assert "set impl-x completed" in act.command_line
    assert "commit impl-x" in act.command_line


def test_completed_offers_commit():
    act = _by_id(
        generate_progress_actions(_task(status="completed"), {"workflow_id": "wf-001"}),
        ":commit",
    )
    assert act.commands == [["commit", "impl-x"]]
    assert act.recommended is True


def test_committed_offers_integrate():
    """The wf-project-0929-01 unblock action: integrate a committed task."""
    act = _by_id(
        generate_progress_actions(_task(status="committed"), {"workflow_id": "wf-001"}),
        ":integrate",
    )
    assert act.commands == [["integrate", "impl-x"]]
    assert act.recommended is True
    assert "集成" in act.title
    assert act.effect, "every button must state its plain-language effect"


def test_integrated_offers_only_finalize():
    actions = generate_progress_actions(_task(status="integrated"), {"workflow_id": "wf-001"})
    assert _ids(actions) == ["impl-x:finalize"]
    assert actions[0].commands == [["finalize", "impl-x"]]


def test_finalize_escalated_offers_three_routes():
    actions = generate_progress_actions(
        _task(status="committed", finalize_escalated=True,
              finalize_escalate_reason="rebase conflict"),
        {"workflow_id": "wf-001"},
    )
    clear = _by_id(actions, ":clear_escalation")
    assert clear.recommended is True
    assert clear.commands == [["clear-escalation", "impl-x"]]
    assert clear.is_destructive is False

    close = _by_id(actions, ":close_workflow_accept_escalated")
    assert close.is_destructive is True
    assert close.commands == [["close-workflow", "wf-001", "--accept-escalated"]]

    sup = _by_id(actions, ":supersede")
    assert sup.is_destructive is True
    assert sup.commands[0][:2] == ["supersede", "impl-x"]
    assert "--reason" in sup.commands[0]


def test_in_flight_offers_redrive_steer_and_halt():
    actions = generate_progress_actions(_task(status="working"), {"workflow_id": "wf-001"})
    redrive = _by_id(actions, ":redrive")
    # A real re-drive is a direct pane prompt on the herdr binary,
    # NOT an entry in the injected steering queue.
    assert redrive.command_base == "herdr"
    assert redrive.commands[0][:3] == ["agent", "prompt", "w1:p9"]
    assert redrive.commands[0][-2:] == ["--timeout", "180000"]

    steer = _by_id(actions, ":steer")
    assert steer.command_base == "herdr-task"
    assert steer.commands[0][:2] == ["steer", "impl-x"]

    halt = _by_id(actions, ":halt")
    assert halt.is_destructive is True
    assert halt.commands == [["halt", "impl-x"]]


def test_no_pane_means_no_redrive():
    actions = generate_progress_actions(
        _task(status="working", pane_id=""), {"workflow_id": "wf-001"},
    )
    assert not any(a.action_id.endswith(":redrive") for a in actions)


def test_non_git_task_has_no_commit_or_integrate_button():
    """bin/herdr-task commit/integrate exit 2 without a git clone."""
    for status, forbidden in (("agent_done", ":accept_and_commit"),
                              ("completed", ":commit"),
                              ("committed", ":integrate")):
        actions = generate_progress_actions(
            _task(status=status, integration_mode="none"),
            {"workflow_id": "wf-001"},
        )
        assert not any(a.action_id.endswith(forbidden) for a in actions), status


def test_context_mode_task_has_no_commit_or_integrate_button():
    for status, forbidden in (("completed", ":commit"),
                              ("committed", ":integrate")):
        actions = generate_progress_actions(
            _task(status=status, execution_mode="context"),
            {"workflow_id": "wf-001"},
        )
        assert not any(a.action_id.endswith(forbidden) for a in actions), status


@pytest.mark.parametrize("integration_mode", ["none", "git"])
@pytest.mark.parametrize("execution_mode", ["", "context"])
def test_settled_tasks_can_always_be_finalized(integration_mode, execution_mode):
    """`finalize` only needs a settled status (TEARDOWN_BLOCKING_STATUSES).

    Gating the whole table on git left the majority non-git task class with
    no button at all, which is the very bug this surface exists to fix.
    """
    for status in ("integrated", "cleanup_ready"):
        actions = generate_progress_actions(
            _task(status=status, integration_mode=integration_mode,
                  execution_mode=execution_mode),
            {"workflow_id": "wf-001"},
        )
        assert _ids(actions), f"{integration_mode}/{execution_mode}/{status}"
        assert all(a.action_id.endswith((":finalize", ":cleanup")) for a in actions), _ids(actions)


def test_cleanup_ready_offers_both_finalize_and_cleanup():
    actions = generate_progress_actions(
        _task(status="cleanup_ready"), {"workflow_id": "wf-001"},
    )
    assert _ids(actions) == ["impl-x:finalize", "impl-x:cleanup"]
    cleanup = _by_id(actions, ":cleanup")
    assert cleanup.commands == [["cleanup", "impl-x"]]
    assert cleanup.recommended is False, "only one step may be the recommendation"


@pytest.mark.parametrize("status", ["cleaned", "superseded", "failed", "pending"])
def test_terminal_or_unstarted_tasks_get_no_pipeline_actions(status):
    assert generate_progress_actions(_task(status=status), {"workflow_id": "wf-001"}) == []


def test_collect_workflow_actions_never_forces_pass_on_history():
    """generate_controller_actions always appends the destructive fallback.

    Calling it for every task would offer "强制放行" on cleaned / superseded
    history, which is exactly the kind of button that must not exist.
    """
    tasks = [
        _task("t-cleaned", status="cleaned", pane_id=""),
        _task("t-superseded", status="superseded", pane_id=""),
        _task("t-ok", status="integrated", pane_id=""),
    ]
    ids = _ids(collect_workflow_actions(tasks, {"workflow_id": "wf-001"}))
    assert not any(i.endswith(":force_pass_advance") for i in ids), ids
    assert not any(i.endswith(":relaunch_with_agent") for i in ids), ids
    # The integrated task still gets its legitimate next step.
    assert "t-ok:finalize" in ids


def test_collect_workflow_actions_keeps_blocker_group_and_adds_pipeline():
    tasks = [
        _task("t-blocked", status="blocked", stage_verdict="blocked",
              stage_verdict_note="等裁决", pane_id=""),
        _task("t-committed", status="committed", pane_id=""),
    ]
    actions = collect_workflow_actions(tasks, {"workflow_id": "wf-001"}, project_root="/p")
    groups = {a.action_id: a.group for a in actions}
    assert groups["t-blocked:relaunch_with_agent"] == "blocker"
    assert groups["t-committed:integrate"] == "pipeline"
    assert len({a.action_id for a in actions}) == len(actions), "action_id must stay unique"


def test_collect_workflow_actions_resume_when_workflow_paused():
    actions = collect_workflow_actions(
        [_task("t-cleaned", status="cleaned")],
        {"workflow_id": "wf-001", "status": "paused"},
        workflow_paused=True,
    )
    resume = next(a for a in actions if a.action_id == "wf-001:resume_workflow")
    # Workflow resume is a kernel capability, not a bin/herdr-task subcommand.
    assert resume.commands == []
    assert resume.api_endpoint == "/api/kernel/resume"
    assert resume.api_payload == {"workflow_id": "wf-001", "node_id": None}
    assert resume.recommended is True


def test_resolve_workflow_blockers_still_ignores_pipeline_tasks():
    """Regression guard: pipeline actions must not turn healthy tasks into blockers."""
    tasks = [_task("t-ok", status="integrated", pane_id=""),
             _task("t-bad", status="blocked", stage_verdict="blocked", pane_id="")]
    assert [b["task_id"] for b in resolve_workflow_blockers(tasks, {})] == ["t-bad"]


@pytest.mark.parametrize("bad", [{"task_id": ""},
                                 {"task_id": "x", "status": "committed",
                                  "integration_mode": "git",
                                  "execution_mode": "context", "pane_id": ""}])
def test_progress_actions_are_defensive_on_partial_records(bad):
    assert isinstance(generate_progress_actions(bad, {"workflow_id": "wf-001"}), list)
