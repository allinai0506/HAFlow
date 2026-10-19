"""Build the Herdr Task CLI parser from cohesive command families."""

import argparse

from herdr.task_cli_parser_execution import add_execution_parsers
from herdr.task_cli_parser_workflow import add_workflow_parsers
from herdr.task_cli_parser_delivery import add_delivery_parsers
from herdr.task_cli_parser_observability import add_observability_parsers


def build_parser():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    add_execution_parsers(sub)
    add_workflow_parsers(sub)
    add_delivery_parsers(sub)
    add_observability_parsers(sub)
    return parser
