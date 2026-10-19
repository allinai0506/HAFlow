import pytest
from contextlib import contextmanager

from herdr.task_cli_workflow_close import close_workflow_owned


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
