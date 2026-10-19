from contextlib import contextmanager


def test_git_operation_lock_wraps_task_and_source_repositories(tmp_path):
    from herdr.task_cli_git_lock import with_git_operation_lock

    events = []

    class Lock:
        def __init__(self, path):
            self.path = path

        def acquire(self):
            events.append(('acquire', self.path))

        def release(self):
            events.append(('release', self.path))

    deps = {
        'load_tasks': lambda: {'tasks': [{'task_id': 't1', 'clone_path': str(tmp_path / 'clone'),
                                         'workflow_id': 'wf-1', 'source_repo': str(tmp_path / 'source')}]},
        'find_task': lambda data, task_id: data['tasks'][0] if task_id == 't1' else None,
        'project_for_workflow': lambda _: {},
        'GitOperationLock': Lock,
        'ensure_no_git_processes': lambda path: events.append(('check', path)),
        'GitOperationBusyError': RuntimeError,
        'GitProcessActiveError': OSError,
    }
    decorated = with_git_operation_lock(
        include_source_repo=True, deps=deps)(lambda task_id: events.append(('run', task_id)))
    decorated('t1')

    assert events == [
        ('acquire', str(tmp_path / 'clone')),
        ('acquire', str(tmp_path / 'source')),
        ('check', str(tmp_path / 'clone')),
        ('check', str(tmp_path / 'source')),
        ('run', 't1'),
        ('release', str(tmp_path / 'source')),
        ('release', str(tmp_path / 'clone')),
    ]
