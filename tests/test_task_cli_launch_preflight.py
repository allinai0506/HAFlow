from types import SimpleNamespace

import pytest

from herdr.task_cli_launch_preflight import validate_allocated_launch_baselines


class Store:
    def __init__(self):
        self.events = []

    def record_event(self, *args, **kwargs):
        self.events.append((args, kwargs))


def test_allocated_baseline_rejection_reclaims_and_audits():
    args = SimpleNamespace(workflow_id='wf', task_id='task')
    store = Store()
    reclaimed = []

    with pytest.raises(SystemExit) as error:
        validate_allocated_launch_baselines(
            args, 'review', 'base-sha', '/source', None,
            clone_path='clone', pane_id='pane', pane_source='dynamic',
            validate_test_review=lambda *a: (_ for _ in ()).throw(ValueError('unprovable')),
            validate_selective=lambda *a: None,
            reclaim=lambda *a, **kw: reclaimed.append((a, kw)),
            get_store=lambda: store,
        )

    assert error.value.code == 2
    assert reclaimed == [((args,), {'clone_path': 'clone', 'pane_id': 'pane',
                                    'pane_source': 'dynamic'})]
    assert store.events[0][0][0] == 'test_baseline_rejected'


def test_allocated_explicit_refusal_reclaims_and_preserves_exit():
    args = SimpleNamespace(workflow_id='wf', task_id='task')
    store = Store()
    reclaimed = []

    with pytest.raises(SystemExit) as error:
        validate_allocated_launch_baselines(
            args, 'review', 'base-sha', '/source', None,
            clone_path='clone', pane_id='pane', pane_source='dynamic',
            validate_test_review=lambda *a: (_ for _ in ()).throw(SystemExit(7)),
            validate_selective=lambda *a: None,
            reclaim=lambda *a, **kw: reclaimed.append((a, kw)),
            get_store=lambda: store,
        )

    assert error.value.code == 7
    assert len(reclaimed) == 1
    assert store.events == []
