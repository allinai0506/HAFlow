"""Bind observability and run analysis commands to the selected state store."""


def build_analysis_bindings(namespace):
    from herdr import task_cli_observability, task_cli_runs

    get_store = lambda: namespace["_get_store"]()
    return {
        "cmd_observe": lambda args: task_cli_observability.cmd_observe(
            args, get_store=get_store
        ),
        "cmd_compact": lambda args: task_cli_observability.cmd_compact(
            args, get_store=get_store
        ),
        "cmd_route_shadow": lambda args: task_cli_observability.cmd_route_shadow(
            args, get_store=get_store
        ),
        "_parse_shadow_since": task_cli_observability._parse_shadow_since,
        "cmd_shadow_eval": task_cli_observability.cmd_shadow_eval,
        "cmd_canary_eval": task_cli_observability.cmd_canary_eval,
        "cmd_metrics": lambda args: task_cli_runs.cmd_metrics(args, get_store=get_store),
        "cmd_working_context": lambda args: task_cli_runs.cmd_working_context(
            args, get_store=get_store
        ),
        "cmd_eval": lambda args: task_cli_runs.cmd_eval(args, get_store=get_store),
        "cmd_replay": lambda args: task_cli_runs.cmd_replay(args, get_store=get_store),
        "cmd_eval_compare": lambda args: task_cli_runs.cmd_eval_compare(
            args, get_store=get_store
        ),
    }
