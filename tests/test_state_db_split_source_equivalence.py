"""Compare moved definitions against the current-main source AST."""
import ast
import copy
import inspect
import subprocess
import textwrap
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
        if original.name == '_ensure_schema':
            continue  # Its five ordered slices have the runtime DDL contract.
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
    assert len(compared) == 153


def test_time_calls_remain_35_unchanged_expressions():
    def calls(tree):
        return sorted(ast.dump(n) for n in ast.walk(tree)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                      and isinstance(n.func.value, ast.Name) and n.func.value.id == 'time')
    expected = calls(BASELINE)
    files = [ROOT / 'herdr/state_db.py'] + list((ROOT / 'herdr/state').glob('*.py'))
    actual = [call for path in files for call in calls(ast.parse(path.read_text()))]
    assert len(expected) == 35
    assert sorted(actual) == expected
