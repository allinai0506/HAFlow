"""Compare moved definitions against the current-main source AST."""
import ast
import copy
import inspect
import subprocess
import textwrap
from collections import Counter
from pathlib import Path

from herdr import state_db

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ast.parse(subprocess.check_output(
    ['git', 'show', '4d8e00c106a969f4bf6bc2af2eadacc61b1a142e:herdr/state_db.py'],
    cwd=ROOT,
    text=True,
))


class UndoExtractionBindings(ast.NodeTransformer):
    def visit_Attribute(self, node):
        node = self.generic_visit(node)
        if (isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                and node.value.func.id == '_facade' and not node.value.args):
            return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
        return node

    def visit_ImportFrom(self, node):
        if node.module == 'herdr':
            node.module = None
            node.level = 1
        elif node.module and node.module.startswith('herdr.'):
            node.module = node.module[len('herdr.'):]
            node.level = 1
        return node


def test_moved_function_bodies_preserve_authorized_ast():
    compared = []
    for original in BASELINE.body:
        if not isinstance(original, (ast.FunctionDef, ast.ClassDef)):
            continue
        if original.name in {'_ensure_schema', 'save_workflow', 'save_task', 'create_checkpoint', 'restore_checkpoint', 'force_pass_gate', 'update_workflow_metadata', 'transition_task'}:
            # _ensure_schema has the runtime DDL contract. save_task, create_checkpoint,
            # restore_checkpoint are changed by Controller Cockpit fencing.
            # save_workflow now atomically freezes V1 execution declarations;
            # behavior is covered by tests/test_execution_obligations.py.
            # force_pass_gate is changed by Issue #307 enterprise credential auth.
            # update_workflow_metadata gained the expected_status CAS guard with the
            # PR #359 re-land (terminal workflows must not be resurrected by
            # startup-window metadata writes); behavior is covered by
            # tests/test_state_store.py::test_workflow_metadata_update_cas_guard and
            # tests/test_startup_watchdog.py.
            # transition_task records blocked_status_timestamp and status_updated_at
            # on entering blocked state to prevent SLA episode resets (Issue #8);
            # behavior is covered by tests/test_impl_fix4_blocker_regression.py.
            continue
        function = getattr(state_db, original.name)
        actual = ast.parse(textwrap.dedent(inspect.getsource(function))).body[0]
        actual = UndoExtractionBindings().visit(actual)
        expected = UndoExtractionBindings().visit(copy.deepcopy(original))
        if original.name == 'register_workflow':
            class RemoveInlineTransitionImport(ast.NodeTransformer):
                def visit_ImportFrom(self, node):
                    if node.module == 'transitions':
                        return None
                    return node
            expected = RemoveInlineTransitionImport().visit(expected)
        assert ast.dump(actual) == ast.dump(expected), original.name
        compared.append(original.name)
    assert len(compared) == 146


def test_time_calls_remain_35_unchanged_expressions():
    def calls(tree):
        return sorted(ast.dump(n) for n in ast.walk(tree)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                      and isinstance(n.func.value, ast.Name) and n.func.value.id == 'time')
    expected = calls(BASELINE)
    files = [ROOT / 'herdr/state_db.py'] + list((ROOT / 'herdr/state').glob('*.py'))
    actual = [call for path in files for call in calls(ast.parse(path.read_text()))]
    assert len(expected) == 35
    expected_counts = Counter(expected)
    actual_counts = Counter(actual)
    assert not (expected_counts - actual_counts)
    assert actual_counts - expected_counts == Counter({
        "Call(func=Attribute(value=Name(id='time', ctx=Load()), attr='time', ctx=Load()))": 3,
    })
