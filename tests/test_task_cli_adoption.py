import pytest

from herdr.task_cli_adoption import adopt_head_if_attributable


def test_empty_head_adoption_persists_empty_result_and_uses_exit_three(monkeypatch):
    import herdr.kernel

    classified = []
    updates = []
    monkeypatch.setattr(herdr.kernel, 'update_task_metadata',
                        lambda task_id, metadata, store: updates.append((task_id, metadata, store)))
    result = ('empty', {'basis': 'baseline_commit', 'reason': 'no_changes',
                        'commits': 0, 'noop_commits': [], 'baseline': 'base'})

    with pytest.raises(SystemExit) as error:
        adopt_head_if_attributable(
            'task-1', {'baseline_commit': 'base', 'branch': 'agent/task-1',
                       'integration_mode': 'git', 'status': 'completed'},
            '/clone', classify_commit_state=lambda **kwargs: classified.append(kwargs) or result,
            explain=lambda *args: 'no changes',
            get_store=lambda: 'store', adoption_skew_seconds=lambda: 120,
            git_head_sha=lambda clone: 'head', git_current_branch=lambda clone: 'agent/task-1',
            git_is_ancestor=lambda *args: True,
            git_interval_commits=lambda *args: [],
            git_head_history=lambda *args: [],
            git_remote_contained_shas=lambda *args, **kwargs: set(),
            coerce_epoch=lambda value: 10.0,
            set_status=lambda *args: None,
        )

    assert error.value.code == 3
    assert classified[0]['baseline_commit'] == 'base'
    assert classified[0]['current_branch'] == 'agent/task-1'
    assert updates[0][0] == 'task-1'
    assert updates[0][1]['commit_result'] == 'empty'
