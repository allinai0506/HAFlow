"""Regression gate: entry scripts must bootstrap the repo root on sys.path.

A script executed directly only gets its own directory on sys.path[0]
(``services/`` or ``bin/``). Any entry script that imports the ``herdr``
package must therefore prepend the repo root before its first herdr import.

``services/herdr-sentinel.py`` was the first offender (lessons §36). The same
breakage recurred in ``services/herdr-worker.py`` while a fallback import to a
non-existent module masked it (lessons §74), so the rule is now an automated
gate over every entry script instead of a single-script fix.
"""

import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ENTRY_DIRS = (ROOT / "bin", ROOT / "services")
HERDR_PACKAGE = "herdr"


def _entry_scripts():
    for directory in ENTRY_DIRS:
        for path in sorted(directory.iterdir()):
            if path.is_file():
                yield path


def _first_herdr_import_line(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name == HERDR_PACKAGE or alias.name.startswith(HERDR_PACKAGE + ".")
                for alias in node.names
            ):
                return node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.module and (
                node.module == HERDR_PACKAGE
                or node.module.startswith(HERDR_PACKAGE + ".")
            ):
                return node.lineno
    return None


def _sys_path_insert_line(tree):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("insert", "append")
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "path"
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "sys"
        ):
            return node.lineno
    return None


class EntryScriptBootstrapGateTest(unittest.TestCase):
    def test_entry_scripts_importing_herdr_bootstrap_repo_root_first(self):
        checked = []
        for script in _entry_scripts():
            try:
                tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
            except (UnicodeDecodeError, SyntaxError):
                continue
            import_line = _first_herdr_import_line(tree)
            if import_line is None:
                continue
            checked.append(script.name)
            bootstrap_line = _sys_path_insert_line(tree)
            self.assertIsNotNone(
                bootstrap_line,
                f"{script} imports herdr but never inserts the repo root into sys.path",
            )
            self.assertLess(
                bootstrap_line,
                import_line,
                f"{script} imports herdr before bootstrapping sys.path",
            )
        self.assertTrue(checked, "gate scanned zero herdr-importing entry scripts")


class WorkerStandaloneLaunchTest(unittest.TestCase):
    def test_worker_help_runs_from_foreign_cwd_without_pythonpath(self):
        worker = ROOT / "services" / "herdr-worker.py"
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        result = subprocess.run(
            [sys.executable, str(worker), "--help"],
            cwd=str(ROOT.parent),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage", (result.stdout + result.stderr).lower())


if __name__ == "__main__":
    unittest.main()
