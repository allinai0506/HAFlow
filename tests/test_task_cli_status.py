import pytest

from herdr.task_cli_status import adopt_task_verdict, set_task_status


def test_set_task_status_missing_task_exits_before_mutation():
    calls = []
    with pytest.raises(SystemExit) as error:
        set_task_status(
            'missing', 'completed',
            load_tasks=lambda: {'tasks': []},
            find_task=lambda data, task_id: None,
            get_store=lambda: calls.append('store'),
            transitions={},
            active_task_statuses=set(),
            is_gate_task=lambda task: False,
            capture_verified_candidate=lambda task: None,
            enforce_wrapup_review_closure=lambda *a, **k: None,
            authenticate_gate_verdict_evidence=lambda *a, **k: (True, None),
            record_gate_note=lambda *a: None,
            clear_suppress_auto_close=lambda workflow_id: None,
            sync_tasks_projection=lambda **kwargs: None,
            tasks_file=None,
        )
    assert error.value.code == 1
    assert calls == []


def test_adopt_task_verdict_missing_task_exits_before_evidence_read():
    calls = []
    with pytest.raises(SystemExit) as error:
        adopt_task_verdict(
            'missing',
            load_tasks=lambda: {'tasks': []},
            find_task=lambda data, task_id: None,
            is_gate_task=lambda task: False,
            authenticate_evidence=lambda *a, **k: calls.append('evidence'),
            set_task_status=lambda *a, **k: calls.append('status'),
        )
    assert error.value.code == 1
    assert calls == []
