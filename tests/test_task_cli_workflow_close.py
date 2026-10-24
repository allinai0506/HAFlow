import pytest
from contextlib import contextmanager

from herdr.task_cli_workflow_close import close_workflow, close_workflow_owned


def test_close_workflow_returns_existing_claim_receipt_without_teardown():
    from herdr.task_cli_workflow_close import close_workflow

    calls = []

    class Claim:
        state = 'completed'
        receipt = {'workflow_id': 'wf-1', 'close_state': 'completed'}

    @contextmanager
    def claim(store, workflow_id, *, dry_run, options):
        calls.append((store, workflow_id, dry_run, options))
        yield Claim()

    report = close_workflow(
        'wf-1', abandon=True,
        get_store=lambda: 'store',
        workflow_close_claim=claim,
        close_workflow_owned=lambda *args, **kwargs: pytest.fail('already closed'),
        business_gate_blockers=lambda *args: pytest.fail('abandon skips gate'),
        validate_delivery_inventory=lambda *args: None,
        build_delivery_report=lambda *args: {},
    )
    assert report == Claim.receipt
    assert calls == [('store', 'wf-1', False, {
        'include_coordinator': False, 'purge_clones': False, 'abandon': True,
        'force': False, 'accept_escalated': False,
    })]


def _escalation_scene(tmp_path, *, task_status, mode, escalated):
    from herdr.state_store import SQLiteStateStore

    store = SQLiteStateStore(tmp_path / "state.db")
    store.save_workflow({
        "workflow_id": "wf-esc", "status": "running", "execution_id": "exec-esc",
        "project_root": str(tmp_path), "base_branch": "main",
        "config": {"nodes": [{"id": "wrapup"}]},
    })
    store.save_task({
        "task_id": "wrap", "workflow_id": "wf-esc", "node": "wrapup",
        "stage": "wrapup", "status": task_status, "run_id": "run-esc",
        "execution_id": "exec-esc", "integration_mode": mode,
        "finalize_escalated": escalated,
        "finalize_escalate_reason": "commit_refused" if escalated else None,
    })
    return store


def _escalation_close_deps(store, tmp_path, calls):
    from herdr.state_db import ACTIVE_TASK_STATUSES
    from herdr.task_cli_teardown import _teardown_output_guard
    from herdr.task_cli_workflow_close import close_workflow_owned
    from herdr.workflow_close import workflow_close_claim

    def mark_workflow_completed(workflow_id, outcome="delivered", force=False, **kwargs):
        calls.setdefault("outcome", []).append(outcome)
        store.save_workflow({
            "workflow_id": workflow_id, "status": "completed",
            "execution_id": "exec-esc",
        })

    def finalize_one(task, **options):
        calls.setdefault("finalize", []).append((task["task_id"], options))
        return {
            "task_id": task["task_id"], "status": task.get("status"),
            "action": "cleaned", "pane_closed": False,
        }

    inner = dict(
        load_tasks=lambda: {"tasks": store.list_tasks(workflow_id="wf-esc")},
        load_workflow_entry=lambda workflow_id: (store.get_workflow(workflow_id), []),
        get_store=lambda: store,
        finalize_one=finalize_one,
        close_pane=lambda pane: calls.setdefault("pane", []).append(pane) or False,
        stage_reset=lambda *a: calls.setdefault("reset", []).append(a),
        mark_workflow_completed=mark_workflow_completed,
        workflow_stage_tabs=lambda *a: {
            "workspace_id": None, "tab_ids": [],
            "owned_pane_ids": [], "coordinator_pane": None,
        },
        live_workspace_panes=lambda *a: [],
        foreign_panes_in_tab=lambda *a: [],
        close_owned_pane=lambda *a: False,
        teardown_output_guard=_teardown_output_guard,
        record_output_retention=lambda *a, **k: calls.setdefault(
            "retention", []).append(a),
        clone_root=str(tmp_path / "clones"),
        teardown_blocking_statuses={*ACTIVE_TASK_STATUSES, "pending"},
    )

    def bound_close_workflow_owned(*args, **kwargs):
        return close_workflow_owned(*args, **{**kwargs, **inner})

    return dict(
        get_store=lambda: store,
        workflow_close_claim=workflow_close_claim,
        business_gate_blockers=lambda *a: [],
        validate_delivery_inventory=lambda *a: None,
        close_workflow_owned=bound_close_workflow_owned,
        build_delivery_report=lambda *a: {},
    )


def test_close_blocks_escalated_none_mode_task(tmp_path, capsys):
    """H-1 门禁对 none 模式的机器升级任务同样生效。

    wf-haflow-1022-01-wrapup-auto-r2（mode=none, finalize_escalated=1,
    commit_refused）曾在无 --accept-escalated/--force 的情况下被 close 按
    delivered 收口：unsettled/escalated 收集都硬性过滤
    integration_mode=='git'，而 none 模式 finalize 分支的 preservation
    commit 被拒时同样机器升级（git_finalize.py none 分支）。
    """
    from herdr.workflow_close import workflow_close_claim

    store = _escalation_scene(
        tmp_path, task_status="cleaned", mode="none", escalated=True)
    calls = {}
    with pytest.raises(SystemExit) as error:
        close_workflow(
            "wf-esc", **_escalation_close_deps(store, tmp_path, calls)
        )
    assert error.value.code == 2
    output = capsys.readouterr().out
    assert "wrap" in output and "escalated" in output
    assert "finalize" not in calls, "gate must fire before any teardown"
    # preflight 拒绝不得冻结 close operation 行（可换 flag 重试）
    with workflow_close_claim(store, "wf-esc") as claim:
        assert claim.state == "owner"


def test_close_accept_escalated_covers_none_mode_task(tmp_path):
    store = _escalation_scene(
        tmp_path, task_status="cleaned", mode="none", escalated=True)
    calls = {}
    report = close_workflow(
        "wf-esc", accept_escalated=True,
        **_escalation_close_deps(store, tmp_path, calls)
    )
    assert report["outcome"] == "escalated_accepted"
    assert report["escalated_accepted"] == [
        {"task_id": "wrap", "via": "accept-escalated"}
    ]
    assert calls["finalize"][0][0] == "wrap"
    assert calls["finalize"][0][1]["output_override"] == "accept-escalated"


def test_close_still_blocks_escalated_git_task(tmp_path):
    store = _escalation_scene(
        tmp_path, task_status="completed", mode="git", escalated=True)
    calls = {}
    with pytest.raises(SystemExit) as error:
        close_workflow(
            "wf-esc", **_escalation_close_deps(store, tmp_path, calls)
        )
    assert error.value.code == 2
    assert "finalize" not in calls


def test_close_ignores_superseded_escalated_task(tmp_path):
    store = _escalation_scene(
        tmp_path, task_status="superseded", mode="none", escalated=True)
    calls = {}
    report = close_workflow(
        "wf-esc", **_escalation_close_deps(store, tmp_path, calls)
    )
    assert report["outcome"] == "delivered"
    assert report["escalated_accepted"] == []


def test_close_unknown_workflow_exits_before_resource_operations():
    calls = []
    with pytest.raises(SystemExit) as error:
        close_workflow_owned(
            'missing', load_tasks=lambda: {'tasks': []},
            load_workflow_entry=lambda workflow_id: (None, []),
            get_store=lambda: calls.append('store'),
            finalize_one=lambda *a, **k: calls.append('finalize'),
            close_pane=lambda pane: calls.append(pane),
            stage_reset=lambda *a: calls.append('reset'),
            mark_workflow_completed=lambda *a, **k: calls.append('complete'),
            workflow_stage_tabs=lambda *a: [], live_workspace_panes=lambda *a: [],
            foreign_panes_in_tab=lambda *a: [], close_owned_pane=lambda *a: False,
            teardown_output_guard=lambda *a: {}, record_output_retention=lambda *a, **k: None,
            clone_root='/tmp',
            teardown_blocking_statuses=set(),
        )
    assert error.value.code == 1
    assert calls == []
