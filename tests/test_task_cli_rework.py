from types import SimpleNamespace

import pytest

from herdr.task_cli_rework import handle_rework


class Store:
    db_path = ':memory:'

    def get_task(self, task_id):
        return None


def test_rework_missing_task_fails_before_side_effects():
    with pytest.raises(SystemExit) as error:
        handle_rework(
            SimpleNamespace(task_id='missing'),
            get_store=Store,
            herdr=lambda *args: pytest.fail('must not message an agent'),
            is_gate_task=lambda task: False,
            recover_unconsumed_gate_cleanup_id=lambda task, store: None,
            cleanup_gate_for_rework=lambda *args: None,
            now=lambda: 0,
        )
    assert error.value.code == 2
