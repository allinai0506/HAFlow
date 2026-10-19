def test_entry_dispatches_registered_handlers_to_cli_parser():
    from herdr.task_cli_entry import main

    registered = {
        'add_task', 'close_workflow', 'launch_task', 'node_status',
        'record_review_closure', 'set_status', 'verify_baseline',
    }
    dependencies = {name: object() for name in registered}
    captured = {}

    def cli_main(*, handlers):
        captured.update(handlers)
        return 23

    result = main(
        dependencies=dependencies, cli_main=cli_main,
        handler_names=tuple(sorted(registered)),
    )

    assert result == 23
    assert set(captured) == registered
    assert captured == dependencies


def test_task_registry_handlers_bind_to_owner_module():
    import pytest
    from herdr.task_cli_entry import build_task_registry_handlers

    handlers = build_task_registry_handlers({
        'load_tasks': lambda: {'tasks': []},
        'find_task': lambda data, task_id: None,
        'new_run_id': lambda: 'run-1',
        '_now': lambda: 1,
        '_init_status_history': lambda *args: None,
        'save_tasks': lambda data: None,
        '_get_store': lambda: None,
        'TrajectoryLedger': object,
        'record_trajectory_event_best_effort': lambda *args, **kwargs: None,
    })

    with pytest.raises(SystemExit) as error:
        handlers['get_task']('missing')
    assert error.value.code == 1
    assert handlers['get_task'].func.__module__ == 'herdr.task_cli_tasks'


def test_registry_compatibility_helpers_remain_available_when_script_is_loaded():
    import importlib.machinery
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "bin" / "herdr-task"
    name = "herdr_task_registry_compat_test"
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, str(script))
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert callable(module.add_task)
    assert callable(module.get_task)
    assert callable(module.list_tasks)
    assert callable(module.check_zombie_obligations)
