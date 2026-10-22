from types import SimpleNamespace

import pytest

from herdr.task_cli_launch_preflight import (
    preflight_delivery_identity,
    validate_allocated_launch_baselines,
)


class Store:
    def __init__(self):
        self.events = []

    def record_event(self, *args, **kwargs):
        self.events.append((args, kwargs))


# code-craft-v1 shape: the gates are declared by role, not by the legacy ids.
GIT_CONFIG = {
    'name': 'code-craft-v1',
    'execution': {'mode': 'git'},
    'nodes': [
        {'id': 'craft', 'default_task_type': 'feat'},
        {'id': 'cleanroom_test', 'default_task_type': 'test'},
        {'id': 'cleanroom_review', 'default_task_type': 'test'},
    ],
}
# A context workflow has no candidate SHA, so there is no identity to prove.
CONTEXT_CONFIG = {
    'name': 'business-research-v1',
    'execution': {'mode': 'context'},
    'nodes': [{'id': 'executive_briefing', 'default_task_type': 'test'}],
}


def allocated(args, node_id, *, calls, config, onto_branch=None, candidate_sha=None):
    args.candidate_sha = candidate_sha
    validate_allocated_launch_baselines(
        args, node_id, 'base-sha', '/source', onto_branch,
        clone_path='clone', pane_id='pane', pane_source='dynamic',
        validate_test_review=lambda *a: calls.append('test_review'),
        validate_selective=lambda *a: calls.append('selective'),
        reclaim=lambda *a, **kw: calls.append('reclaim'),
        get_store=lambda: Store(),
        workflow_config_for=lambda _: config,
    )


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


@pytest.mark.parametrize('node_id', ['cleanroom_test', 'cleanroom_review'])
def test_declared_verifier_gets_the_candidate_baseline_gate(node_id):
    """wf-haflow-1021-01: the cleanroom clone sat on the baseline and nothing objected."""
    calls = []
    allocated(SimpleNamespace(workflow_id='wf', task_id='t1'), node_id,
              calls=calls, config=GIT_CONFIG)
    assert calls == ['test_review']


def test_context_verifier_is_not_held_to_a_candidate_it_cannot_have():
    calls = []
    allocated(SimpleNamespace(workflow_id='wf', task_id='t1'), 'executive_briefing',
              calls=calls, config=CONTEXT_CONFIG)
    assert calls == []


def test_producer_keeps_the_selective_replan_gate():
    calls = []
    allocated(SimpleNamespace(workflow_id='wf', task_id='t1'), 'craft', calls=calls,
              config=GIT_CONFIG, onto_branch='herdr/integration-craft',
              candidate_sha='a' * 40)
    assert calls == ['selective']


def test_delivery_identity_is_required_for_a_declared_verifier():
    with pytest.raises(SystemExit) as error:
        preflight_delivery_identity(
            'wf', 'cleanroom_test', 't1', '',
            get_store=lambda: Store(),
            resolve_expected_candidate=lambda *a: ('', ''),
            workflow_config_for=lambda _: GIT_CONFIG,
        )
    assert error.value.code == 2


def test_delivery_identity_is_not_required_without_git_semantics():
    assert preflight_delivery_identity(
        'wf', 'executive_briefing', 't1', '',
        get_store=lambda: Store(),
        resolve_expected_candidate=lambda *a: ('', ''),
        workflow_config_for=lambda _: CONTEXT_CONFIG,
    ) is None


def test_delivery_identity_still_binds_legacy_ids_without_a_config():
    with pytest.raises(SystemExit) as error:
        preflight_delivery_identity(
            'wf', 'review', 't1', '',
            get_store=lambda: Store(),
            resolve_expected_candidate=lambda *a: ('', ''),
            workflow_config_for=lambda _: None,
        )
    assert error.value.code == 2
