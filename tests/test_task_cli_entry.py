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
