"""Isolated real Console/Factory/SQLite sandbox for HTTP and browser verification.

Only terminal provisioning, agent health, pool configuration and trust setup
are replaced: no paid models or host services run. UI routes, API dispatch,
project writes, factory workflow registration, decisions and files are real.
Run from repository root with --root <temporary-directory> --port <port>.
"""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def configure(root, stack):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for key, value in {
        'HERDR_STATE_DB': root / 'state.db',
        'WORKFLOWS_FILE': root / 'workflows.json',
        'TASKS_FILE': root / 'tasks.json',
        'HERDR_WORKFLOW_DOCS_DIR': root / 'docs',
        'HERDR_CLONES_DIR': root / 'clones',
        'HERDR_ATTENTION_FILE': root / 'attention.json',
        'HERDR_OBSERVER_ENABLED': '0',
        'HERDR_OBSERVER_JEV_ENABLED': '0',
    }.items():
        stack.enter_context(patch.dict(os.environ, {key: str(value)}))
    from console import herdr_factory_console as console
    from herdr import projects, state_store, workflow, workspace_trust
    state_store.reset_state_store()
    for module in (console, projects):
        stack.enter_context(patch.object(module, 'ROOT', root))
        for key, name in [('PROJECTS_FILE', 'projects.json'), ('WORKFLOWS_FILE', 'workflows.json')]:
            stack.enter_context(patch.object(module, key, root / name))
    stack.enter_context(patch.object(console, 'TASKS_FILE', root / 'tasks.json'))
    stack.enter_context(patch.object(workflow, 'USER_TEMPLATES_DIR', root / 'templates'))
    loader = importlib.machinery.SourceFileLoader('simple_sandbox_factory', str(ROOT / 'bin/herdr-factory'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    factory = importlib.util.module_from_spec(spec)
    loader.exec_module(factory)
    # Terminal transport and model-dependent boundaries only.
    def terminal_json(command):
        ident = uuid.uuid4().hex[:8]
        return {'result': {'workspace': {'workspace_id': 'test-workspace'},
                           'tab': {'tab_id': 'test-tab-' + ident},
                           'root_pane': {'pane_id': 'test-pane-' + ident}}}
    stack.enter_context(patch.object(projects, '_run_json', terminal_json))
    actual_project_run = projects._run
    def project_run(command, **kwargs):
        if command[0] == 'herdr':
            return subprocess.CompletedProcess(command, 0, '{}', '')
        return actual_project_run(command, **kwargs)
    stack.enter_context(patch.object(projects, '_run', project_run))
    stack.enter_context(patch.object(projects, '_start_coordinator', lambda *a: None))
    stack.enter_context(patch.object(projects, '_workspace_alive', lambda *a: True))
    stack.enter_context(patch.object(projects, '_pane_alive', lambda *a: True))
    stack.enter_context(patch.object(workspace_trust, 'ensure_controller_env_trust', lambda *a: True))
    stack.enter_context(patch.object(factory, 'ensure_pool_for_project', lambda *a: None))
    stack.enter_context(patch.object(factory, 'set_workflow_agent_override', lambda *a: None))
    stack.enter_context(patch.object(factory, 'run_workflow_preflight', lambda *a, **k: (['sandbox'], {})))
    actual_run = console.run
    def run(command, timeout=20, check=False):
        if command[0] == str(ROOT / 'bin/herdr-factory'):
            # Preserve real run command parsing, start core and persisted frozen definition.
            parser = argparse.ArgumentParser()
            parser.add_argument('action')
            parser.add_argument('--project')
            parser.add_argument('--agent')
            parser.add_argument('--template')
            parser.add_argument('--title', default='')
            parser.add_argument('--context', action='append', default=[])
            parser.add_argument('requirement')
            args = parser.parse_args(command[1:])
            output, errors = io.StringIO(), io.StringIO()
            exit_code = 0
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                try:
                    factory.start_workflow(args.requirement, project_path=args.project,
                                           agent_override=args.agent, template=args.template,
                                           title=args.title, context_args=args.context)
                except SystemExit as exc:
                    exit_code = int(exc.code or 0)
            return subprocess.CompletedProcess(command, exit_code, output.getvalue(), errors.getvalue())
        if command[0] == 'herdr':
            return subprocess.CompletedProcess(command, 0, json.dumps({'result': {'workspaces': [], 'tabs': [], 'panes': []}}), '')
        return actual_run(command, timeout, check)
    stack.enter_context(patch.object(console, 'run', run))
    return console, factory


def main():
    from http.server import ThreadingHTTPServer
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--port', type=int, default=8877)
    args = parser.parse_args()
    with contextlib.ExitStack() as stack:
        console, _ = configure(args.root, stack)
        print(f'SANDBOX_ROOT={args.root}', flush=True)
        ThreadingHTTPServer(('127.0.0.1', args.port), console.Handler).serve_forever()


if __name__ == '__main__':
    main()
