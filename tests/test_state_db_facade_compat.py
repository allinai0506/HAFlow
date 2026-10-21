"""Facade compatibility contract for the ``herdr/state_db.py`` split.

The module stays a *module* (not a package) and keeps every one of the 167
top-level bindings from the pre-split baseline.  These tests are the executable
form of INV-5:

* all 167 names import from ``herdr.state_db``;
* the kind (function/class/constant) and the constant's runtime type do not
  change;
* private symbols stay addressable (three production modules import
  ``_decode_task_row`` directly);
* shared mutable objects are the same object in the facade and their host;
* ``herdr.state_db.time`` is the stdlib ``time`` module, so
  ``patch("herdr.state_db.time.time")`` keeps working.

The baseline is read from ``scripts/check_state_db_split.py`` so this test and
the gate cannot drift apart.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import sys
import time
from pathlib import Path

import pytest

from herdr import state_db

ROOT = Path(__file__).resolve().parents[1]


def _load_checker():
    path = ROOT / "scripts" / "check_state_db_split.py"
    spec = importlib.util.spec_from_file_location("check_state_db_split", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CHECKER = _load_checker()
SYMBOLS = CHECKER.SYMBOLS
SYMBOL_BY_NAME = CHECKER.SYMBOL_BY_NAME
BASELINE_NAMES = CHECKER.BASELINE_NAMES


def _has_state_package() -> bool:
    return (ROOT / "herdr" / "state").is_dir()


def _state_constants():
    try:
        return importlib.import_module("herdr.state.constants")
    except ImportError:
        return None


def test_baseline_is_exactly_167_bindings():
    assert len(SYMBOLS) == 167
    assert len(BASELINE_NAMES) == 167


@pytest.mark.parametrize("name", sorted(BASELINE_NAMES))
def test_each_baseline_binding_is_importable(name: str):
    assert hasattr(state_db, name), f"herdr.state_db.{name} is missing"
    namespace = {}
    exec(f"from herdr.state_db import {name} as imported", namespace)
    assert namespace["imported"] is getattr(state_db, name)


def test_binding_kinds_and_constant_types_are_unchanged():
    drift = []
    for name in sorted(BASELINE_NAMES):
        expected = SYMBOL_BY_NAME[name]
        value = getattr(state_db, name)
        actual = CHECKER._normal_type(value)
        if actual != expected["type"]:
            drift.append(f"{name}: expected {expected['type']}, got {actual}")
    assert not drift, "\n".join(drift)


def test_functions_and_classes_keep_their_kind():
    for name in sorted(BASELINE_NAMES):
        expected = SYMBOL_BY_NAME[name]["kind"]
        value = getattr(state_db, name)
        if expected == "function":
            assert inspect.isfunction(value), f"{name} is not a function"
        elif expected == "class":
            assert inspect.isclass(value), f"{name} is not a class"


def test_private_symbols_are_importable_and_exported_when_all_exists():
    private = sorted(name for name in BASELINE_NAMES if name.startswith("_"))
    assert "_decode_task_row" in private
    assert "_INITIALIZED_DBS" in private

    all_names = getattr(state_db, "__all__", None)
    if all_names is None:
        # Pre-split baseline: there is no __all__ yet.  The contract is
        # attribute presence; T4 adds the explicit 167-name block and the
        # ``--final`` gate then enforces it.
        for name in private:
            assert hasattr(state_db, name)
        return

    exported = set(all_names)
    missing = sorted(set(private) - exported)
    assert not missing, f"private symbols missing from __all__: {missing}"
    assert BASELINE_NAMES.issubset(exported)
    assert len(all_names) == len(exported), "__all__ contains duplicates"


def test_herdr_state_db_time_is_the_stdlib_time_module():
    assert state_db.time is time
    assert callable(state_db.time.time)


def test_shared_mutable_objects_are_single_host_aliases():
    initialized = state_db._INITIALIZED_DBS
    shadow = state_db.SHADOW_READ_CAPABILITY
    assert isinstance(initialized, set)
    assert isinstance(shadow, dict)

    constants = _state_constants()
    if constants is None:
        assert not _has_state_package(), (
            "herdr/state exists but herdr.state.constants cannot be imported"
        )
        return
    assert initialized is constants._INITIALIZED_DBS
    assert shadow is constants.SHADOW_READ_CAPABILITY


def test_symbolic_imports_used_by_production_modules_still_work():
    # herdr/task_resources.py, herdr/recovery_store.py and console/read_api.py
    # import this private symbol directly.
    from herdr import state_db as facade
    from herdr.state_db import (
        CONTROLLER_DIR,
        OUTCOME_SCHEMA_VERSION,
        ROUTE_DECISION_MAX_LIMIT,
        _decode_task_row,
    )

    assert facade is state_db
    assert _decode_task_row is state_db._decode_task_row
    assert CONTROLLER_DIR is state_db.CONTROLLER_DIR
    assert OUTCOME_SCHEMA_VERSION == state_db.OUTCOME_SCHEMA_VERSION
    assert ROUTE_DECISION_MAX_LIMIT == state_db.ROUTE_DECISION_MAX_LIMIT


def test_import_time_constant_consumers_see_the_facade_value():
    execution_outcome = importlib.import_module("herdr.execution_outcome")
    shadow_rows = importlib.import_module("herdr.shadow_rows")

    assert execution_outcome.OUTCOME_SCHEMA_VERSION == state_db.OUTCOME_SCHEMA_VERSION
    assert shadow_rows.state_db is state_db
    assert (
        shadow_rows.state_db.ROUTE_DECISION_DEFAULT_LIMIT
        == state_db.ROUTE_DECISION_DEFAULT_LIMIT
    )


def test_all_public_modules_are_still_loaded_through_the_same_module_object():
    # The facade must remain the same module identity: tests and production
    # code patch its attributes, and `from herdr import state_db` must not
    # create a second facade object.
    assert sys.modules["herdr.state_db"] is state_db


def test_nested_upward_import_is_rejected(tmp_path):
    package = tmp_path / 'herdr' / 'state'
    package.mkdir(parents=True)
    (package / 'connection.py').write_text(
        'def open_db():\n    from .schema import _ensure_schema\n')
    result = CHECKER.gate_g5(tmp_path)
    assert result.status == 'BLOCK'
    assert any('connection -> schema' in detail for detail in result.details)


def test_facade_helper_may_import_facade_inside_function(tmp_path):
    package = tmp_path / 'herdr' / 'state'
    package.mkdir(parents=True)
    (package / '_facade.py').write_text(
        'def _facade():\n    from herdr import state_db\n    return state_db\n')
    assert CHECKER.gate_g5(tmp_path).status == 'PASS'


def test_lazy_helper_exception_does_not_hide_other_upward_imports(tmp_path):
    package = tmp_path / 'herdr' / 'state'
    package.mkdir(parents=True)
    (package / '_facade.py').write_text(
        'def _facade():\n    from .schema import _ensure_schema\n')
    import ast
    edges = CHECKER._resolve_import_edges(
        '_facade', ast.parse((package / '_facade.py').read_text()))
    assert edges == {'schema'}


def test_snapshot_uses_live_facade_and_rejects_missing_binding(tmp_path):
    import shutil
    snapshot = CHECKER.snapshot_baseline(ROOT)
    assert snapshot['source'] == 'live herdr/state_db.py'
    assert snapshot['symbol_count'] == 167
    assert snapshot['coverage']['total'] == 8169
    target = tmp_path / 'herdr'
    target.mkdir()
    source = (ROOT / 'herdr/state_db.py').read_text()
    (target / 'state_db.py').write_text(source.replace("'register_workflow', ", ''))
    with pytest.raises(ValueError, match='refusing snapshot'):
        CHECKER.snapshot_baseline(tmp_path)
    shutil.copytree(ROOT / 'herdr/state', target / 'state')
    (target / 'state_db.py').write_text(source)
    owner = target / 'state/transition_gate.py'
    owner.write_text(owner.read_text().replace('def register_workflow(', 'def unknown_register('))
    with pytest.raises(ValueError, match='unknown or duplicate owner'):
        CHECKER.snapshot_baseline(tmp_path)
