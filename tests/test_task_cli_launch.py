from contextlib import contextmanager
from types import SimpleNamespace

import pytest


def test_launch_task_records_rejection_when_workflow_lock_is_busy():
    from herdr.task_cli_launch import launch_task

    calls = []

    class NodeLaunchBusy(Exception):
        pass

    class GitOperationBusyError(Exception):
        pass

    class GitProcessActiveError(Exception):
        pass

    @contextmanager
    def busy_lock(*args):
        raise NodeLaunchBusy()
        yield

    args = SimpleNamespace(workflow_id='wf-1', task_id='task-1', source=None)
    with pytest.raises(SystemExit) as error:
        launch_task(args, deps={
            'NodeLaunchBusy': NodeLaunchBusy,
            'GitOperationBusyError': GitOperationBusyError,
            'GitProcessActiveError': GitProcessActiveError,
            'workflow_launch_lock': busy_lock,
            'get_store': lambda: SimpleNamespace(db_path='state.db'),
            'record_launch_rejection': lambda *a, **k: calls.append((a, k)),
            'launch': lambda _: pytest.fail('lock should reject before launch'),
        })
    assert error.value.code == 75
    assert calls and calls[0][1]['reason'] == 'launch_lock_busy'
