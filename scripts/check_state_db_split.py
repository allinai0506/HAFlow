#!/usr/bin/env python3
"""Machine gates for the ``herdr/state_db.py`` module split (wf-haflow-1019-02).

This script is intentionally stdlib-only (``ast`` + ``sqlite3``) so it can run in
CI and in a bare worker without installing anything.

Gates implemented here
----------------------
G-1  completeness     facade still exposes the frozen 167 top-level bindings.
G-2  binding form     (a) patch-surface symbols are only reached via
                          ``_facade().NAME`` in ``herdr/state/``;
                      (b) shared mutable objects have a single host + alias;
                      (c) no static import of a patch-surface symbol / no
                          legacy ``from herdr.state_db import`` inside leaves.
G-3  band             each leaf file is 300-500 lines unless registered as an
                      exception in the architecture baseline.
G-5  import graph     no cycles and only legal downward static imports.
G-7  schema           a freshly initialized database has the same
                      ``sqlite_master`` objects as the frozen baseline.

G-4 (runtime DDL trace equality) lives in
``tests/test_state_db_ddl_sequence.py``; G-6 (real CLI chain) is a later
integration gate and is not part of the T1 scaffold.

The baseline data (167 symbols, module ownership, original line spans, schema
objects and normalized DDL traces) is embedded as a zlib+base64 blob.  Use
``--snapshot-baseline`` to print or write it back as JSON, and
``--schema-dump`` / ``--schema-check`` to capture/diff a live schema.

Exit status: 0 when every enabled gate passes, 1 on BLOCK.
"""

from __future__ import annotations

import argparse
import ast
import base64
import difflib
import hashlib
import importlib
import inspect
import json
import sqlite3
import sys
import tempfile
import zlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BASELINE_SHA256 = '68407032f6bab83fbd719b4c31c38d698554efd3c3f85c6e42d147db5b437843'
BASELINE_BLOB = (
    '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'
    '8gWl0I073l4jO++C/PqXeY1HOBr3GsmB47M49hP5kO+xmMlS9YZ4b2V9K+unlPUN8ZIg3r/aYH0rxAvM+tNAvG8+IkiYJXY08IZ4'
    'P4nle3uN39lrvCHeLyVwb2X9nZX1DfHeEO9fe9r/tibv85T3ecr78PMXF7i3sv7OyppcBPbe8be2vLUlWVveSvKOxN+ReFoknrmI'
    '9y+sJslcG/wXxsI/eyT7rin62WuKvu82W/r9tX/ZjZO3rX+DyTeYfOdJfnGBeyvr76ys7+OlN6D7UYDuO8rD0y6cvwHdv93OvRMf'
    'v3Pi410v9EsJ3FtZf2dlfQO6d73QjwJ031EMnvaFmDeg+7fbubeP+J19xBvQ/VIC91bW31lZ34DuDejeBeA/hzV5n5X8zmcl74PN'
    'X0rg3sr6OyvruwD8rS1vbXkXgL8j8Z+Ed/8Bkfi7ANyn4WePZN/1Qj93vVDSP7n87eXgKbPFisMT/7Hn90HUz+FR37Dzd4Od74zK'
    'LyVwb2X9nZX1fRD1hn5/DfT75sLxlNliZeRv6PeTWcR3MuV3Tqa8a5B+KYF7K+vvrKxv6PeuQfproN83l5inzBYrOH9Dv5/MIr69'
    'ye/sTd7Q75cSuLey/s7K+oZ+b+j3Lj//OazJ+/zldz5/eR+W/lIC91bW31lZ3+Xnb215a8u7/Pwdif8kvPsPiMTf5ec+DT97JPuu'
    'QfplapB+TNl5lnLz94HTz+I53/Dyd4OX78zJLyVwb2X9nZX1feD0hng/FuL9iPLyLGXlb4j3s3iNd3Lkd0uOvGuKfimBeyvr76ys'
    'b4j3rin6sRDvR5SRZykff0O8t9f4NXn363uNN8T7pQTuray/s7K+Id4b4r3Lxn8Oa/I+T/mdz1Peh5+/lMC9lfV3VtZ32fhbW97a'
    '8i4bf0fiPwnv/gMi8XfZuE/Dzx7JvmuKfu6aIo/Ab68XDz4eKxT3X/6vP1tKM7Uum1/ZruiY77Lt37zgK/ueLNifiIWTg9bfnHk/'
    'PqX7TQr4zdV8wcdjZXw/gwK6/uvVlkbHfJcMffOCr2Qo2bd/4gzv8wr4GzDvJ1HAb661CD4eK7J4K+BbAX925v0Sh5pvuPLGej8K'
    '60Xy0G+mvE3hp7NKn0r//PA8zWcRjiWugOxsrPNc1UwFjvueiP/pZPH4Pz70XWn6k6TM3+dKv9u50rtk4pcSuLey/s7K+ksEZe+z'
    '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'
)

PATCH_FUNCTIONS = frozenset(
    {
        "_decode_task_row",
        "_get_credentials_dir",
        "artifact_ref_exists",
        "get_db_connection",
        "get_default_db_path",
        "get_readonly_db_connection",
        "get_rollout_stage",
        "get_task",
        "get_trajectory_finding_by_id",
        "init_db",
        "list_rollout_audit",
        "list_rollout_states",
        "list_steers",
        "query_execution_outcomes",
        "read_rollout_snapshot",
        "record_event",
        "record_event_if_absent",
        "record_observation_receipt",
        "rollout_stage_known",
        "save_working_context",
        "upsert_trajectory_finding",
    }
)
PATCH_CONSTANT = "CONTROLLER_DIR"
PATCH_SYMBOLS = PATCH_FUNCTIONS | {PATCH_CONSTANT}
SHARED_MUTABLE = ("_INITIALIZED_DBS", "SHADOW_READ_CAPABILITY")

#: Constants that leaves may import statically (architecture §2 F-1).
STATIC_CONSTANT_WHITELIST = frozenset(
    {
        "HOME",
        "PROTECTED_TASK_METADATA_FIELDS",
        "PROTECTED_WORKFLOW_METADATA_FIELDS",
        "RESERVED_EVENT_METADATA_FIELDS",
        "OUTCOME_SCHEMA_VERSION",
        "OUTCOME_PER_AGENT_LIMIT",
        "OUTCOME_LOOKUP_CHUNK",
        "_OUTCOME_COLUMNS",
        "_OUTCOME_COLUMN_NAMES",
        "ROUTE_DECISION_DEFAULT_LIMIT",
        "ROUTE_DECISION_MAX_LIMIT",
        "_INITIALIZED_DBS",
        "SHADOW_READ_CAPABILITY",
    }
)

BAND_MIN = 300
BAND_MAX = 500

#: Architecture §4.7.  A registered direction lets a module leave the band in
#: exactly that direction; anything else is a BLOCK.
BAND_EXCEPTIONS: dict[str, tuple[str, str]] = {
    "transition_gate.py": ("above", "ARCH-4.7-transition-gate-port"),
    "constants.py": ("below", "ARCH-4.7-constants"),
    "schema.py": ("below", "ARCH-4.7-schema"),
    "schema_v1.py": ("below", "ARCH-4.7-schema-v1"),
    "steering.py": ("below", "ARCH-4.7-steering"),
    "trajectory.py": ("below", "ARCH-4.7-trajectory"),
    "completion.py": ("above", "ARCH-4.7-completion"),
    "working_context.py": ("above", "ARCH-4.7-working-context"),
    "context_source_validation.py": ("above", "ARCH-4.7-context-source-validation"),
}

#: Static import layers.  A module may only import a strictly lower layer.
#: ``__init__`` is the registry and may import anything; the facade may import
#: anything as well.  Same-layer cross-domain calls must use ``_facade()``.
LAYER: dict[str, int] = {
    "constants": 0,
    "_facade": 0,
    "connection": 1,
    "schema_ddl_helpers": 2,
    "schema_ddl_txn": 3,
    "schema_v1": 3,
    "schema": 4,
    "_domains": 5,
}
FACADE = "state_db"
REGISTRY = "__init__"

PATCH_OWNER_HINTS = {
    # Filled from the embedded module ownership at load time.
}


# ---------------------------------------------------------------------------
# Baseline data
# ---------------------------------------------------------------------------


def _decode_baseline() -> dict[str, Any]:
    blob = "".join(BASELINE_BLOB.split())
    raw = zlib.decompress(base64.b64decode(blob))
    digest = hashlib.sha256(raw).hexdigest()
    if digest != BASELINE_SHA256:
        raise SystemExit(
            "embedded baseline is corrupt: "
            f"expected {BASELINE_SHA256}, got {digest}"
        )
    return json.loads(raw.decode("utf-8"))


BASELINE = _decode_baseline()
SYMBOLS: list[dict[str, Any]] = BASELINE["symbols"]
SYMBOL_BY_NAME: dict[str, dict[str, Any]] = {s["name"]: s for s in SYMBOLS}
MODULE_SYMBOLS: dict[str, list[str]] = BASELINE["module_symbols"]
MODULE_META: dict[str, list[Any]] = BASELINE["module_meta"]
MODULE_ORDER: list[str] = BASELINE["module_order"]
MODULE_BODIES: dict[str, int] = BASELINE["bodies"]
MODULE_SEGMENTS: dict[str, int] = {
    "schema": 4,  # head/guard, pre-transaction DDL, post-commit DDL, function tail
    "schema_ddl_txn": 1,  # BEGIN IMMEDIATE .. COMMIT
    "schema_v1": 1,  # 979-1115 V1 bootstrap tail
}
EXPECTED_SCHEMA: dict[str, dict[str, str]] = BASELINE["schema_objects"]
EXPECTED_SCHEMA_SQL: dict[str, str] = BASELINE["schema_sql"]
EXPECTED_SCHEMA_COUNTS: dict[str, int] = BASELINE["schema_counts"]
EXPECTED_TRACES: dict[str, dict[str, Any]] = BASELINE["traces"]
BASELINE_NAMES: set[str] = set(SYMBOL_BY_NAME)
DOMAIN_MODULES: list[str] = [
    m
    for m in MODULE_ORDER
    if m not in {"constants", "connection", "schema", "schema_ddl_txn", "schema_ddl_helpers", "schema_v1"}
]
for _name, _info in SYMBOL_BY_NAME.items():
    PATCH_OWNER_HINTS[_name] = _info["module"]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


@dataclass
class GateResult:
    gate: str
    status: str  # PASS | WARN | PENDING | BLOCK
    summary: str
    details: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "status": self.status,
            "summary": self.summary,
            "details": self.details,
        }


def _read_ast(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _rel(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _normal_sql(value: str | None) -> str:
    return " ".join((value or "").split())


def _normal_type(value: Any) -> str:
    if isinstance(value, Path):
        return "path"
    if inspect.isfunction(value):
        return "function"
    if inspect.isclass(value):
        return "class"
    return type(value).__name__


def _ensure_import_path(root: Path) -> None:
    entry = str(root)
    if entry not in sys.path:
        sys.path.insert(0, entry)


def _import_module(root: Path, name: str) -> Any:
    _ensure_import_path(root)
    importlib.invalidate_caches()
    return importlib.import_module(name)


def _is_facade_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_facade"
        and not node.args
        and not node.keywords
    )


def _is_facade_attr(node: ast.Attribute) -> bool:
    return _is_facade_call(node.value)


def _scope_walk(tree: ast.AST) -> Iterator[tuple[ast.AST, tuple[str, ...]]]:
    """Yield every node together with its enclosing function/class names."""

    stack: list[str] = []

    def walk(node: ast.AST) -> Iterator[tuple[ast.AST, tuple[str, ...]]]:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.append(node.name)
            for child in ast.iter_child_nodes(node):
                yield from walk(child)
            stack.pop()
            return
        yield node, tuple(stack)
        for child in ast.iter_child_nodes(node):
            yield from walk(child)

    yield from walk(tree)


def _collect_import_from(node: ast.ImportFrom) -> list[str]:
    """Return the imported symbol names (not module names)."""
    return [alias.name for alias in node.names]


def _short_state_target(full: str) -> str | None:
    if full == "herdr.state_db":
        return FACADE
    if full == "herdr.state":
        return REGISTRY
    if full.startswith("herdr.state."):
        return full[len("herdr.state.") :].split(".", 1)[0]
    return None


def _resolve_import_edges(module: str, tree: ast.Module) -> set[str]:
    """Resolve all static state imports of ``module`` to short module names.

    Relative imports are resolved against the importing module's package:
    ``herdr/state_db.py`` lives in ``herdr``; every leaf lives in
    ``herdr.state``.  This matters for ``from .state.constants import X`` in
    the facade versus ``from .constants import X`` in a leaf.
    """
    package = "herdr" if module == FACADE else "herdr.state"
    edges: set[str] = set()
    for node in ast.walk(tree):
        # The dedicated lazy helper is the sole permitted facade import.
        if (module == "_facade" and node not in tree.body
                and isinstance(node, ast.ImportFrom) and node.level == 0
                and node.module == "herdr"
                and all(alias.name == "state_db" for alias in node.names)):
            continue
        if isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                climb = max(0, node.level - 1)
                prefix = ".".join(parts[: len(parts) - climb]) if climb else package
                if node.module:
                    target = _short_state_target(prefix + "." + node.module)
                    if target:
                        edges.add(target)
                else:
                    for alias in node.names:
                        target = _short_state_target(prefix + "." + alias.name)
                        if target:
                            edges.add(target)
            elif node.module:
                target = _short_state_target(node.module)
                if target:
                    edges.add(target)
                elif node.module == "herdr":
                    for alias in node.names:
                        if alias.name == "state_db":
                            edges.add(FACADE)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                target = _short_state_target(alias.name)
                if target:
                    edges.add(target)
    edges.discard(module)
    return edges


def _layer(module: str) -> int:
    if module in {FACADE, REGISTRY, "_facade"}:
        return 99
    if module in LAYER:
        return LAYER[module]
    return LAYER["_domains"]


# ---------------------------------------------------------------------------
# G-1: 167 bindings
# ---------------------------------------------------------------------------


def _collect_facade_bindings(tree: ast.Module) -> tuple[set[str], list[str] | None]:
    bound: set[str] = set()
    all_names: list[str] | None = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    bound.add(target.id)
        elif isinstance(node, ast.ImportFrom):
            relative = bool(node.level) or (
                node.module is not None
                and (node.module == "herdr.state" or node.module.startswith("herdr.state."))
            )
            if relative:
                for alias in node.names:
                    bound.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "herdr.state" or alias.name.startswith("herdr.state."):
                    bound.add(alias.asname or alias.name.split(".")[0])
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == "__all__":
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    value = None
                if isinstance(value, (list, tuple)) and all(
                    isinstance(item, str) for item in value
                ):
                    all_names = list(value)
    return bound, all_names


def gate_g1(root: Path, final: bool) -> GateResult:
    facade = root / "herdr" / "state_db.py"
    if not facade.is_file():
        return GateResult("G-1", "BLOCK", "herdr/state_db.py is missing")
    try:
        tree = _read_ast(facade)
    except SyntaxError as exc:
        return GateResult("G-1", "BLOCK", f"cannot parse facade: {exc}")
    bound, all_names = _collect_facade_bindings(tree)
    exported = set(all_names) if all_names is not None else bound
    missing = sorted(BASELINE_NAMES - exported)
    extra = sorted(set(all_names or []) - BASELINE_NAMES)
    details: list[str] = []
    status = "PASS"

    if missing:
        status = "BLOCK"
        details.append(f"missing {len(missing)} baseline symbols: {', '.join(missing)}")
    if extra:
        status = "BLOCK"
        details.append(f"__all__ contains {len(extra)} non-baseline names: {', '.join(extra)}")
    if all_names is None:
        if (root / "herdr" / "state").is_dir():
            message = (
                "facade has no __all__; re-export completeness is only provable by "
                "attribute presence (T4 must add the explicit 167-name block)"
            )
            if final:
                status = "BLOCK"
                details.append(message)
            else:
                details.append("WARN: " + message)
                if status == "PASS":
                    status = "WARN"
        else:
            details.append(
                "monolith has no __all__ (expected pre-split); 167/167 names are "
                "checked by attribute presence in the split tests"
            )
    if all_names is not None and len(all_names) != len(set(all_names)):
        status = "BLOCK"
        details.append("__all__ contains duplicate names")

    # Runtime check: importability and kind/type stability.
    try:
        state_db = _import_module(root, "herdr.state_db")
    except Exception as exc:  # noqa: BLE001 - gate reports the failure
        status = "BLOCK"
        details.append(f"import herdr.state_db failed: {type(exc).__name__}: {exc}")
    else:
        for name in sorted(BASELINE_NAMES):
            expected = SYMBOL_BY_NAME[name]
            if not hasattr(state_db, name):
                status = "BLOCK"
                details.append(f"runtime missing: herdr.state_db.{name}")
                continue
            actual = _normal_type(getattr(state_db, name))
            wanted = expected["type"]
            if actual != wanted:
                status = "BLOCK"
                details.append(
                    f"type drift: {name} expected {wanted}, got {actual}"
                )
    summary = (
        f"{len(BASELINE_NAMES) - len(missing)}/{len(BASELINE_NAMES)} baseline "
        "bindings present"
    )
    return GateResult("G-1", status, summary, details)


# ---------------------------------------------------------------------------
# G-2: binding form
# ---------------------------------------------------------------------------


def _inventory_monolith(root: Path) -> tuple[dict[str, list[str]], int, int]:
    facade = root / "herdr" / "state_db.py"
    tree = _read_ast(facade)
    points: dict[str, list[str]] = {}
    calls = 0
    constants = 0
    for node, scope in _scope_walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in PATCH_FUNCTIONS:
                calls += 1
                where = ".".join(scope) or "<module>"
                points.setdefault(node.id, []).append(
                    f"{_rel(root, facade)}:{node.lineno}:{where}:{node.id}"
                )
            elif node.id == PATCH_CONSTANT:
                constants += 1
                where = ".".join(scope) or "<module>"
                points.setdefault(node.id, []).append(
                    f"{_rel(root, facade)}:{node.lineno}:{where}:{node.id}"
                )
    return points, calls, constants


def _state_files(root: Path) -> list[Path]:
    state_dir = root / "herdr" / "state"
    if not state_dir.is_dir():
        return []
    return sorted(path for path in state_dir.glob("*.py") if path.is_file())


def _gate_g2a(root: Path, state_files: Sequence[Path]) -> tuple[GateResult, list[str]]:
    details: list[str] = []
    for path in state_files:
        tree = _read_ast(path)
        module = path.stem
        for node, scope in _scope_walk(tree):
            where = ".".join(scope) or "<module>"
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id in PATCH_SYMBOLS:
                    details.append(
                        f"{_rel(root, path)}:{node.lineno}:{where}: bare {node.id} "
                        "(must use _facade().NAME)"
                    )
            elif isinstance(node, ast.Attribute) and node.attr in PATCH_SYMBOLS:
                if not _is_facade_attr(node):
                    details.append(
                        f"{_rel(root, path)}:{node.lineno}:{where}: "
                        f"{node.attr} accessed off a non-_facade object "
                        "(patch interception would be lost)"
                    )
            elif isinstance(node, ast.ImportFrom):
                leaked = [name for name in _collect_import_from(node) if name in PATCH_SYMBOLS]
                if leaked:
                    details.append(
                        f"{_rel(root, path)}:{node.lineno}: static import of "
                        f"patch symbol(s) {', '.join(sorted(leaked))} in {module}"
                    )
    status = "BLOCK" if details else "PASS"
    summary = (
        f"{len(details)} bare/static patch-surface reference(s) in "
        f"{len(state_files)} leaf file(s)"
    )
    return GateResult("G-2a", status, summary, details), details


def _gate_g2b(root: Path, state_files: Sequence[Path], final: bool) -> GateResult:
    details: list[str] = []
    facade = root / "herdr" / "state_db.py"
    files = list(state_files)
    if not files:
        tree = _read_ast(facade)
        hosts: dict[str, list[tuple[str, int, str]]] = {name: [] for name in SHARED_MUTABLE}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in hosts:
                        hosts[target.id].append((_rel(root, facade), node.lineno, "assign"))
        for name in SHARED_MUTABLE:
            if len(hosts[name]) != 1:
                details.append(
                    f"monolith host for {name} must be exactly 1 assignment, "
                    f"found {len(hosts[name])}"
                )
        status = "BLOCK" if details else "PASS"
        return GateResult(
            "G-2b",
            status,
            "baseline mode: shared mutable objects live in herdr.state_db",
            details,
        )

    hosts = {name: [] for name in SHARED_MUTABLE}
    imports = {name: [] for name in SHARED_MUTABLE}
    for path in files + [facade]:
        tree = _read_ast(path)
        module = path.stem
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in hosts:
                        hosts[target.id].append((module, node.lineno, "assign"))
            elif isinstance(node, ast.AnnAssign):
                target = node.target
                if isinstance(target, ast.Name) and target.id in hosts:
                    hosts[target.id].append((module, node.lineno, "assign"))
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    bound = alias.asname or alias.name
                    if bound in imports:
                        imports[bound].append((node.module or ".", node.lineno, module))

    for name in SHARED_MUTABLE:
        assign_hosts = [host for host in hosts[name] if host[0] != FACADE]
        facade_assigns = [host for host in hosts[name] if host[0] == FACADE]
        if len(assign_hosts) != 1 or assign_hosts[0][0] != "constants":
            details.append(
                f"{name} must have exactly one assignment host (constants), "
                f"found {assign_hosts}"
            )
        if facade_assigns:
            details.append(
                f"facade must alias {name} via import, not assignment: {facade_assigns}"
            )
        if imports[name] and not any(
            module == "constants" or module.endswith(".constants")
            for module, _, _ in imports[name]
        ):
            details.append(f"{name} imported from a non-constants module: {imports[name]}")

    # Runtime identity is the only proof that a re-export is an alias.
    try:
        state_db = _import_module(root, "herdr.state_db")
        constants = _import_module(root, "herdr.state.constants")
        for name in SHARED_MUTABLE:
            left = getattr(state_db, name)
            right = getattr(constants, name)
            if left is not right:
                details.append(
                    f"{name} is not the same object in facade and constants "
                    "(second host/copy detected)"
                )
    except Exception as exc:  # noqa: BLE001 - gate reports the failure
        details.append(f"runtime alias check failed: {type(exc).__name__}: {exc}")

    status = "BLOCK" if details else "PASS"
    return GateResult(
        "G-2b",
        status,
        "shared mutable objects: single host + facade alias",
        details,
    )


def _gate_g2c(root: Path, state_files: Sequence[Path], final: bool) -> GateResult:
    details: list[str] = []
    facade = root / "herdr" / "state_db.py"
    for path in state_files:
        module = path.stem
        tree = _read_ast(path)
        if module == REGISTRY:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for target in _resolve_import_edges(module, ast.Module(body=[node], type_ignores=[])):
                    if target == FACADE and module != "_facade":
                        details.append(
                            f"{_rel(root, path)}:{node.lineno}: leaf imports the facade "
                            "(upward import; use _facade())"
                        )
                for alias in node.names:
                    if alias.name in PATCH_SYMBOLS:
                        details.append(
                            f"{_rel(root, path)}:{node.lineno}: patch symbol "
                            f"{alias.name} must not be statically imported"
                        )
                    if (
                        node.module
                        and (node.module == "constants" or node.module.endswith(".constants"))
                        and alias.name not in STATIC_CONSTANT_WHITELIST
                        and alias.name != "_route_decision_bucket_exprs"
                        # constants.py may also host pure helpers (e.g. the
                        # §4.4 _coerce_version fallback); only *constants*
                        # are whitelist-controlled.
                        and alias.name.isupper()
                    ):
                        details.append(
                            f"{_rel(root, path)}:{node.lineno}: {alias.name} is not "
                            "in the static-constant whitelist (architecture §2 F-1)"
                        )
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = node.name
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                target = node.targets[0] if isinstance(node, ast.Assign) else node.target
                name = target.id if isinstance(target, ast.Name) else ""
            else:
                name = ""
            if name in PATCH_SYMBOLS:
                expected = PATCH_OWNER_HINTS.get(name)
                if expected and expected != module:
                    details.append(
                        f"{_rel(root, path)}:{node.lineno}: {name} defined in "
                        f"{module}, expected host {expected}"
                    )
    # Facade must stay a re-export layer, not a proxy/magic namespace.
    try:
        facade_tree = _read_ast(facade)
    except SyntaxError as exc:
        details.append(f"cannot parse facade: {exc}")
    else:
        for node in facade_tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "__getattr__":
                details.append("facade defines __getattr__ (proxy magic is forbidden)")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(node):
                    if (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Name)
                        and child.func.id in {"exec", "eval"}
                    ):
                        details.append(
                            f"facade uses {child.func.id}() at line {child.lineno} "
                            "(dynamic binding is forbidden)"
                        )
    status = "BLOCK" if details else "PASS"
    return GateResult(
        "G-2c",
        status,
        "binding form: no static patch symbols, legal hosts, no proxy magic",
        details,
    )


def gate_g2(root: Path, final: bool) -> GateResult:
    state_files = _state_files(root)
    facade = root / "herdr" / "state_db.py"
    if not state_files:
        points, calls, constants = _inventory_monolith(root)
        details = [
            (
                f"inventory: {calls} bare patch-function calls + {constants} "
                f"{PATCH_CONSTANT} reads in {_rel(root, facade)}"
            )
        ]
        for name in sorted(points):
            details.extend(points[name])
        status = "PASS"
        if calls != 111 or constants != 6:
            status = "WARN"
            details.append(
                f"WARN: expected 111 calls + 6 constant reads, got {calls} + {constants} "
                "(monolith changed before the split?)"
            )
        return GateResult(
            "G-2",
            status,
            "baseline mode: herdr/state absent; rewrite inventory attached",
            details,
        )

    part_a, _ = _gate_g2a(root, state_files)
    part_b = _gate_g2b(root, state_files, final)
    part_c = _gate_g2c(root, state_files, final)
    details = part_a.details + part_b.details + part_c.details
    # Remaining monolith references are legal while a domain is not migrated
    # yet, but T3 needs the exact rewrite points.  Keep the inventory visible
    # without blocking on it.
    points, calls, constants = _inventory_monolith(root)
    details.append(
        f"remaining monolith rewrite inventory: {calls} bare patch-function "
        f"calls + {constants} {PATCH_CONSTANT} reads"
    )
    for name in sorted(points):
        details.extend(points[name])
    statuses = {part_a.status, part_b.status, part_c.status}
    status = "BLOCK" if "BLOCK" in statuses else ("WARN" if "WARN" in statuses else "PASS")
    return GateResult(
        "G-2",
        status,
        f"G-2a={part_a.status}; G-2b={part_b.status}; G-2c={part_c.status}",
        details,
    )


# ---------------------------------------------------------------------------
# G-3: band
# ---------------------------------------------------------------------------


def gate_g3(root: Path, final: bool) -> GateResult:
    state_dir = root / "herdr" / "state"
    if not state_dir.is_dir():
        return GateResult(
            "G-3",
            "PENDING",
            "herdr/state absent (T2/T3 modules not created yet)",
        )
    details: list[str] = []
    pending: list[str] = []
    for module in MODULE_ORDER:
        path = state_dir / f"{module}.py"
        if not path.is_file():
            pending.append(module)
            continue
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if BAND_MIN <= lines <= BAND_MAX:
            details.append(f"{module}: {lines} lines (in band)")
            continue
        direction = "below" if lines < BAND_MIN else "above"
        exception = BAND_EXCEPTIONS.get(f"{module}.py")
        if exception is None:
            details.append(
                f"BLOCK {module}: {lines} lines outside [{BAND_MIN},{BAND_MAX}] "
                "and no architecture §4.7 exception"
            )
        elif exception[0] != direction:
            details.append(
                f"BLOCK {module}: {lines} lines ({direction}) does not match "
                f"registered exception direction {exception[0]} ({exception[1]})"
            )
        else:
            details.append(
                f"{module}: {lines} lines ({direction} exception {exception[1]})"
            )
    blocked = any(item.startswith("BLOCK") for item in details)
    if pending:
        details.append(f"pending modules ({len(pending)}): {', '.join(pending)}")
        if final:
            blocked = True
    status = "BLOCK" if blocked else ("PENDING" if pending else "PASS")
    return GateResult(
        "G-3",
        status,
        f"checked {len(MODULE_ORDER) - len(pending)}/{len(MODULE_ORDER)} modules",
        details,
    )


# ---------------------------------------------------------------------------
# G-5: import graph
# ---------------------------------------------------------------------------


def _state_module_files(root: Path) -> dict[str, Path]:
    state_dir = root / "herdr" / "state"
    if not state_dir.is_dir():
        return {}
    return {path.stem: path for path in state_dir.glob("*.py") if path.is_file()}


def gate_g5(root: Path) -> GateResult:
    state_dir = root / "herdr" / "state"
    if not state_dir.is_dir():
        return GateResult(
            "G-5",
            "PASS",
            "baseline mode: herdr/state absent; no internal import graph to check",
        )
    files = _state_module_files(root)
    facade = root / "herdr" / "state_db.py"
    details: list[str] = []
    for module in sorted(files):
        if module not in MODULE_ORDER and module not in {REGISTRY, "_facade"}:
            details.append(
                f"BLOCK unknown module herdr/state/{module}.py "
                "(layout is 22 domains + scaffolding)"
            )
    known = set(files) | {FACADE, REGISTRY}
    if facade.is_file():
        files[FACADE] = facade
    edges: dict[str, set[str]] = {}
    for module, path in files.items():
        try:
            tree = _read_ast(path)
        except SyntaxError as exc:
            details.append(f"BLOCK cannot parse {_rel(root, path)}: {exc}")
            continue
        targets = _resolve_import_edges(module, tree)
        edges[module] = targets
        for target in sorted(targets):
            if target not in known and target not in MODULE_ORDER:
                details.append(
                    f"BLOCK {module} imports unknown state module {target}"
                )
                continue
            if module in {REGISTRY, FACADE}:
                continue
            if module == "_facade" and target == FACADE:
                details.append(
                    "BLOCK _facade.py imports herdr.state_db at module level "
                    "(the helper must import inside the function body)"
                )
                continue
            if target in {REGISTRY}:
                details.append(
                    f"BLOCK {module} imports the herdr.state registry package "
                    "(leaf modules must import leaf modules directly)"
                )
                continue
            if target == FACADE:
                details.append(
                    f"BLOCK {module} statically imports herdr.state_db "
                    "(upward import; use _facade())"
                )
                continue
            if target == "_facade":
                continue
            if _layer(module) <= _layer(target):
                details.append(
                    f"BLOCK illegal static edge {module} -> {target} "
                    f"(layer {_layer(module)} is not above {_layer(target)}; "
                    "same-layer/upward calls must use _facade())"
                )
    # Cycle detection (includes the registry and facade).
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module: str, path: list[str]) -> None:
        if module in visited:
            return
        if module in visiting:
            cycle = " -> ".join(path[path.index(module) :] + [module])
            details.append(f"BLOCK import cycle: {cycle}")
            return
        visiting.add(module)
        for target in sorted(edges.get(module, set())):
            if target in edges:
                visit(target, path + [module])
        visiting.remove(module)
        visited.add(module)

    for module in sorted(edges):
        visit(module, [])
    status = "BLOCK" if details else "PASS"
    return GateResult(
        "G-5",
        status,
        f"{len(edges)} module(s), {sum(len(v) for v in edges.values())} static edge(s)",
        details,
    )


# ---------------------------------------------------------------------------
# G-7: sqlite_master dump
# ---------------------------------------------------------------------------


def _dump_schema(root: Path) -> tuple[dict[str, dict[str, str]], dict[str, int]]:
    state_db = _import_module(root, "herdr.state_db")
    previous_checkpoints = None
    import os

    previous_checkpoints = os.environ.get("CHECKPOINTS_DIR")
    with tempfile.TemporaryDirectory(prefix="state-db-schema-") as tmp:
        tmp_path = Path(tmp)
        empty_checkpoints = tmp_path / "checkpoints"
        empty_checkpoints.mkdir()
        os.environ["CHECKPOINTS_DIR"] = str(empty_checkpoints)
        try:
            db_path = tmp_path / "state.db"
            state_db.init_db(db_path)
            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                ).fetchall()
            finally:
                conn.close()
        finally:
            if previous_checkpoints is None:
                os.environ.pop("CHECKPOINTS_DIR", None)
            else:
                os.environ["CHECKPOINTS_DIR"] = previous_checkpoints
    objects: dict[str, dict[str, str]] = {}
    counts = {"table": 0, "index": 0, "trigger": 0}
    for obj_type, name, table_name, sql in rows:
        key = f"{obj_type}:{name}"
        normalized = _normal_sql(sql)
        objects[key] = {
            "tbl_name": table_name or "",
            "sql": normalized,
            "sha256": hashlib.sha256(normalized.encode()).hexdigest()[:16],
        }
        if obj_type in counts:
            counts[obj_type] += 1
    return objects, counts


def _compare_schema(
    actual: dict[str, dict[str, str]],
    actual_counts: dict[str, int],
    expected: dict[str, dict[str, str]],
    expected_counts: dict[str, int],
    expected_sql: dict[str, str] | None = None,
) -> list[str]:
    details: list[str] = []
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(
        key
        for key in set(expected) & set(actual)
        if expected[key]["sha256"] != actual[key]["sha256"]
    )
    if missing:
        details.append(f"missing schema objects ({len(missing)}): {', '.join(missing)}")
    if extra:
        details.append(f"extra schema objects ({len(extra)}): {', '.join(extra)}")
    for key in changed:
        details.append(f"changed schema object: {key}")
        if expected_sql and key in expected_sql:
            diff = list(
                difflib.unified_diff(
                    expected_sql[key].splitlines(),
                    actual[key]["sql"].splitlines(),
                    fromfile=f"expected/{key}",
                    tofile=f"actual/{key}",
                    lineterm="",
                )
            )
            details.extend(diff[:20])
    for kind, count in expected_counts.items():
        if actual_counts.get(kind) != count:
            details.append(
                f"schema count drift: {kind} expected {count}, got {actual_counts.get(kind)}"
            )
    return details


def gate_g7(root: Path) -> GateResult:
    try:
        actual, counts = _dump_schema(root)
    except Exception as exc:  # noqa: BLE001 - gate reports the failure
        return GateResult(
            "G-7",
            "BLOCK",
            f"cannot build a fresh schema: {type(exc).__name__}: {exc}",
        )
    cockpit_indexes = {
        "index:idx_events_checkpoint_identity": "32f04667cedd12e1",
        "index:idx_events_control_reason": "0a32e1f00397bfb1",
    }
    details = []
    for key, expected_hash in cockpit_indexes.items():
        row = actual.get(key)
        if not row:
            details.append(f"missing Controller Cockpit schema object: {key}")
        elif row["sha256"] != expected_hash:
            details.append(f"changed Controller Cockpit schema object: {key}")
    baseline_actual = {key: value for key, value in actual.items() if key not in cockpit_indexes}
    baseline_counts = dict(counts)
    baseline_counts["index"] = baseline_counts.get("index", 0) - sum(
        1 for key in cockpit_indexes if key in actual
    )
    details.extend(_compare_schema(
        baseline_actual, baseline_counts, EXPECTED_SCHEMA, EXPECTED_SCHEMA_COUNTS, EXPECTED_SCHEMA_SQL
    ))
    status = "BLOCK" if details else "PASS"
    return GateResult(
        "G-7",
        status,
        f"{counts.get('table', 0)} tables / {counts.get('index', 0)} indexes / "
        f"{counts.get('trigger', 0)} triggers",
        details,
    )


# ---------------------------------------------------------------------------
# Report / inventory / snapshots
# ---------------------------------------------------------------------------


def _band_label(est: int, module: str) -> str:
    if BAND_MIN <= est <= BAND_MAX:
        return "OK"
    direction = "below" if est < BAND_MIN else "above"
    exception = BAND_EXCEPTIONS.get(f"{module}.py")
    if exception and exception[0] == direction:
        return f"{'BELOW' if direction == 'below' else 'ABOVE'} (registered {exception[1]})"
    return f"{'BELOW' if direction == 'below' else 'ABOVE'} (UNREGISTERED)"


def report_data(root: Path) -> dict[str, Any]:
    modules: list[dict[str, Any]] = []
    for module in MODULE_ORDER:
        body = MODULE_BODIES[module]
        est = body + 25
        group, owner, _plan_body = MODULE_META[module]
        path = root / "herdr" / "state" / f"{module}.py"
        modules.append(
            {
                "module": module,
                "file": f"herdr/state/{module}.py",
                "body": body,
                "est": est,
                "band": _band_label(est, module),
                "group": group,
                "owner": owner,
                "symbols": len(MODULE_SYMBOLS.get(module, [])),
                "segments": MODULE_SEGMENTS.get(module, 0),
                "actual_lines": len(path.read_text(encoding="utf-8").splitlines())
                if path.is_file()
                else None,
            }
        )
    covered = BASELINE["coverage"]["covered"]
    total = BASELINE["coverage"]["total"]
    return {
        "modules": modules,
        "covered": covered,
        "total": total,
        "uncovered": BASELINE["coverage"]["uncovered"],
        "symbols": SYMBOLS,
        "module_symbols": MODULE_SYMBOLS,
    }


def print_report(root: Path) -> None:
    data = report_data(root)
    print("## state_db split allocation (architecture §4.8)")
    print()
    print("| # | module | body | est | band | group | owner | symbols | segs | actual |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for index, row in enumerate(data["modules"], 1):
        actual = "-" if row["actual_lines"] is None else str(row["actual_lines"])
        print(
            f"| {index} | `{row['module']}.py` | {row['body']} | {row['est']} | "
            f"{row['band']} | {row['group']} | {row['owner']} | {row['symbols']} | "
            f"{row['segments']} | {actual} |"
        )
    print()
    print(
        f"covered={data['covered']}/{data['total']} "
        f"({data['covered'] * 100.0 / data['total']:.2f}%), "
        f"uncovered={','.join(data['uncovered'])}"
    )
    print(
        "note: body = AST span to the next top-level statement; schema.py and "
        "schema_v1.py include their registered _ensure_schema segments."
    )


def print_inventory(root: Path) -> None:
    points, calls, constants = _inventory_monolith(root)
    print(
        f"G-2(a) rewrite inventory: {calls} bare patch-function calls + "
        f"{constants} {PATCH_CONSTANT} reads"
    )
    for name in sorted(points):
        for point in points[name]:
            print(point)


def snapshot_baseline(root: Path) -> dict[str, Any]:
    facade = root / "herdr" / "state_db.py"
    tree = _read_ast(facade)
    bound, all_names = _collect_facade_bindings(tree)
    monolith = any(isinstance(n, ast.FunctionDef) and n.name == "get_db_connection"
                   for n in tree.body)
    data = json.loads(json.dumps(BASELINE))
    if monolith:
        symbols = []
        for index, node in enumerate(tree.body):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names = [node.name]
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [n.id for n in targets if isinstance(n, ast.Name)]
                kind = "constant"
            else:
                continue
            next_start = (tree.body[index + 1].lineno if index + 1 < len(tree.body)
                          else len(facade.read_text().splitlines()) + 1)
            for name in names:
                if name == "register_workflow":
                    base = {"module": "transition_gate", "type": "function"}
                elif name in SYMBOL_BY_NAME:
                    base = SYMBOL_BY_NAME[name]
                else:
                    raise ValueError(f"unknown baseline owner: {name}")
                symbols.append({**base, "name": name, "kind": kind,
                                "lineno": node.lineno, "end_lineno": node.end_lineno,
                                "next_start": next_start})
        data["symbols"] = sorted(symbols, key=lambda row: row["name"])
        data["source_line_count"] = len(facade.read_text().splitlines())
        data["coverage"]["total"] = data["source_line_count"]
        data["coverage"]["covered"] = data["source_line_count"] - 40
        data["module_symbols"] = {
            module: [r["name"] for r in symbols if r["module"] == module]
            for module in MODULE_ORDER}
        for module in MODULE_ORDER:
            body = sum(r["next_start"] - r["lineno"] for r in symbols
                       if r["module"] == module)
            old_body = data["bodies"][module]
            # Schema allocation uses registered slices, rather than whole symbols.
            if module not in {"schema", "schema_ddl_txn", "schema_ddl_helpers", "schema_v1"}:
                data["bodies"][module] = body
                data["module_meta"][module][2] += body - old_body
    else:
        exported = set(all_names or [])
        if exported != BASELINE_NAMES or not exported <= bound:
            raise ValueError("facade bindings differ from baseline; refusing snapshot")
        from collections import Counter
        owners = Counter()
        for path in (root / "herdr" / "state").glob("*.py"):
            for node in _read_ast(path).body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    owners[(path.stem, node.name)] += 1
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            owners[(path.stem, target.id)] += 1
        for row in SYMBOLS:
            if owners[(row["module"], row["name"])] != 1:
                raise ValueError(f"unknown or duplicate owner: {row['name']}")
    data.update(baseline_commit="a56739e", source="live herdr/state_db.py",
                symbol_count=len(data["symbols"]))
    return data


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run_gates(root: Path, final: bool) -> list[GateResult]:
    return [
        gate_g1(root, final),
        gate_g2(root, final),
        gate_g3(root, final),
        gate_g5(root),
        gate_g7(root),
    ]


def _print_gate(result: GateResult) -> None:
    print(f"[{result.status}] {result.gate}: {result.summary}")
    for detail in result.details:
        print(f"    {detail}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent.parent),
        help="repository root (default: parent of scripts/)",
    )
    parser.add_argument("--final", action="store_true", help="missing modules/__all__ are BLOCK")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--report", action="store_true", help="print the §4.8 allocation report")
    parser.add_argument("--symbols", action="store_true", help="include the 167-row symbol table in --report")
    parser.add_argument("--snapshot-baseline", action="store_true", help="emit the 167-binding baseline")
    parser.add_argument("--out", help="write snapshot/report JSON to this path")
    parser.add_argument("--inventory", action="store_true", help="print G-2(a) rewrite points")
    parser.add_argument("--schema-dump", metavar="PATH", help="write the live sqlite_master dump")
    parser.add_argument("--schema-check", metavar="PATH", help="diff the live schema against a dump")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    if args.snapshot_baseline:
        payload = snapshot_baseline(root)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.out:
            Path(args.out).write_text(text + "\n", encoding="utf-8")
            print(f"wrote {payload['symbol_count']} bindings to {args.out}")
        else:
            print(text)
        return 0

    if args.inventory:
        print_inventory(root)
        return 0

    if args.schema_dump:
        objects, counts = _dump_schema(root)
        payload = {"objects": objects, "counts": counts}
        Path(args.schema_dump).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"wrote {len(objects)} schema objects to {args.schema_dump}")
        return 0

    if args.schema_check:
        expected_payload = json.loads(Path(args.schema_check).read_text(encoding="utf-8"))
        actual, counts = _dump_schema(root)
        details = _compare_schema(
            actual,
            counts,
            expected_payload["objects"],
            expected_payload.get("counts", {}),
        )
        for detail in details:
            print(detail)
        print("SCHEMA_MATCH" if not details else "SCHEMA_DRIFT")
        return 0 if not details else 1

    if args.report:
        payload = report_data(root)
        if args.json:
            if args.symbols:
                payload["symbols"] = SYMBOLS
            text = json.dumps(payload, ensure_ascii=False, indent=2)
        else:
            print_report(root)
            if args.symbols:
                print()
                print("| symbol | module | start | end | kind |")
                print("|---|---|---|---|---|")
                for symbol in SYMBOLS:
                    print(
                        f"| `{symbol['name']}` | {symbol['module']} | "
                        f"{symbol['lineno']} | {symbol['end_lineno']} | {symbol['kind']} |"
                    )
            text = ""
        if args.out:
            Path(args.out).write_text(text + "\n", encoding="utf-8")
            print(f"wrote report to {args.out}")
        elif text:
            print(text)
        return 0

    results = run_gates(root, args.final)
    blocked = any(result.status == "BLOCK" for result in results)
    if args.json:
        payload = {
            "ok": not blocked,
            "baseline_commit": "a56739e",
            "gates": [result.as_dict() for result in results],
            "g4": "tests/test_state_db_ddl_sequence.py",
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.out:
            Path(args.out).write_text(text + "\n", encoding="utf-8")
        else:
            print(text)
    else:
        print("STATE_DB SPLIT GATES (baseline a56739e)")
        for result in results:
            _print_gate(result)
        print("G-4: runtime DDL trace equality -> tests/test_state_db_ddl_sequence.py")
        print("OVERALL:", "BLOCK" if blocked else "PASS")
    return 1 if blocked else 0


# GA-3: fifth trace captured from unchanged schema on dispatched a56739e monolith (baseline arbitration pending).
EXPECTED_TRACES["legacy_rollout"] = {'count': 147, 'sha256': 'a2aae8da6dc428b3c567dee1eb848ffca7cda95586d53a4533c1cd8252557881', 'b64': 'eJztXd1v47gRf7+/Qg8F7KDG4lKgfdmmgC9RtkYdO+c4vV0UhaDIdKyLLXn1kWxa9H/vUKQoUqJk2ZGz8mruZW2RGpLz+ZvhOHc7G366GRoPcfhqRe6G+HF0cf4z/Pfxp1s29LsfB569tjb+glz8NhyLgfDVc1aB7/lxeDGZzm6koaUfEPfRs57Ia3gxnXz86XJmDuemMR/+MjaN0bUxmc4N8/Pobn5nvPjB03Ltv4RGX3y23IUxNz/PjdvZ6GY4+2L8w/wyMCI3WpPk+cAIIzuKQ/4lIpvt2o6I5dmbdIITBwHxIgsmPopnvrd0H63fQ9/jTzYkshd2ZMvPnIAArYVlRwbsejww4u1CfmCc1TqPFRDHfybBq+VvSWBHru/RI8LJRpO5+cmcyYczhvfz6WgCZG/MCezBXcDe3eiVcpBxgq4wuR+PjfvJ6Nd7c1DklZgxM6/NmTm5NCXm9qXpZ8Z0YlyZYxNOcDm8uxxeAbmt/br27UXGCEFPYbb0FM4WwqHEccT6V+b18H48N34eGP6LRwLO1zWxQ2LFcK41Z6wdUclFYSUJj3yLrEVMMnnAdgKQR+VbCxCsu9acRkzq/fd/vYKwpePlpZ6OnKXCH02uzM854QuZC3bDzim7q7RCls2A8XognfqsStsiO3yiakX/1VtNXk+Ao2DJmRkJ64BPXqQzr2SSBZteuE6ke2Z5fpRS2doeyZZ69O01//iw9p0noQwFddttd/U07no6M0efJolRqUp/gFnsMHXHB79DqAwt/yEkwbOw8n3EwV6FcypMF09zh5ZGCvzZ2AFw2NoGJKSirGJTImx1xaUbhPCIEC+juLZLHtn05LmHYmPiMTjckDhx5D4T/kq1rT/bnhuudpg22frOynJWtve4YyZXuTxDym07pz5ciorqJAaXjeytMiviPG1914uommTf6ioLmJ2woCS+FUloLEnQUcMmPYTjxzJnQs/ehis/2hUk39PMyDNh7KoXO/UOT2ZhZpupIYivyVpW9Lol5Y6K4iRg5GabxiNASE46P4i9jFpIvsbEg7F029UHjQL7d+JEPkSHpestXO+Rnpp/1GtIOliOE6QNSXquMqEyRshxgbsNElKXlM1OdyH4JoMHYGkAUKYOqMiCM0RHD8JzGG/Aqb2m7IzDLfAHNNux41BwHCLqZkO8BdV4h3pgGe4tEv4zQZFn9nVPBLhDajnXL33Vi+wwgTAl03OYDQVkmR8BDkRUYJqhDVm4tp6c+x9iPbxGGl8Ngyv7T3/+S/4V8s0hwTYqZen+6Itpb5/xSjm9fN50P2f1RUQBGHG30RFFVXaqnWgChAWob2s7CaRLvze4MwmOSflRRGpIikEdoksQsqn/+ncvQWnu0oWZS7DGcPd8d7P1g8iGvaROb/c7wpJBCWpMh3zBpbupOZ06H8sNw5jUmJwg9aXvxDXmcs1lQaYQHHQQ5u22VCNdpa6ba1stxQsdv+g2lMCnAy4SIs7eUkMzCy+Bvy5QT/cEKvIIriZweUgqusEULytu8wVYE1B4XMnsqhwYBBG4TrWMhaW46xJB/GJ+Gk2M0c2NeTUCiYiCSWQ/AJx2vaXfzwnE4idYEXsRVqaCVS+CVEsFV15IEAfrlchPmaHhv6weYLLa5K0Kjcsory8OoGxZ7CyJAbXZ6dDU4BB2Ji++nZ0V7Khx6OSsd4ChL+fGuXE9m94Y4de1C458A4kYJNm//R0wuJFE9wujl7CjZwwnV0ZSIYNHVaez1uTRdl57+3CHmYcloDr/rnUhef+i+pAGnUu5OwkI4Mfq5LFOgnmgzwURQYykkD4iG8qt5EFJ0VN/tCIPXC+MgjjDv9JkVmry09pLHDzK5QHpPDImF9oKwZBGZVDahRtu7QgyTjnFz+eaRSdakxkrN6SpT+08z9aeVYVACmOLPNqDNXDM8gxw3zND9KKVEk8UhaXvejWoURDWQ8LaxkKe7XWsYuFscEEcN9QP6cUgsUvvANOUsR4i2xPv6QvWvNxcjbfsb1atsnRA0VuoD1u8QJ3ZBBw2qWtlT8i3pC5GK4eVJfKAhPFaqcaQIPCD2prm+Ou1/eCzSnPmnhkCPbamLQN/Y5WoW+SXjSRvZUWHEjji75yS0FHL0fBW7oEu/5VqDnrKe6YTeypvGh7rzaZ66IKWwPRwS8ut5Vp7XmYYPPgnRyrKozxJ18UDSF49/wXw76NaCE7TR/EIwuvCXy6tcvLpDN0y6Zh+OUE7v+yuWASLbGwKZmwwEmEAin1QN0nqmB4gjMCK/CeSuPjkm84MYFxbhlnZIXu7UqA1sTNfXV5PrpnorrLcxbfEQkPrRQiJlm5ZAVxB4JkIMzRekzZXSEGXfd/9tigtW9vAp0VT+ZIN+H05vDMpCp4Y1IIsEJq76CvJ9JkxF8NgbgEovzphYPT+8IFThxP1zgxzcrV7Z842xzCp5F/GNpDo3WUN0vr7poSVyUr68X5tmcDe2bVCft+6ywZtusUijHYoEbB2pGTf2rkKdKqT8dVQJrZpEBs9NvuiSkrgvdq0kvp5Ro2XOQ6hlAqXU+KyPIQSC5gZqfTq46BdUY+VkcquTQ4hxi9QMnLswSGkkgwyLaxlBEUdmQ9RmFOfph9DZi0g8EPsPBGZj3pnI+cE3NdcTodjMHSzrzgdeWLic2inTe8MPnKXMyhxZ4evoFyZUC1463I05Iyud6zqZecShb9jnjIxFaqT6umEUiVKwEsl0r3fhdFTJd4TtZZUjYQR6r1ELiFM6jMcerPVuMLT0oy48mP1GXUf8gUFjxa9XUoriuiw2SSuFi8VhTnsHYEE8dT+ddTl28AD1lBCGz+EEs6Kux/krnL2XCR1sbqg2eQynGfKMsrNVmNL8Wu6wmLstqwG0JCuohKfKuMZefBwVVLXYL0IZYtoZFFzlTwYYAslJUEOGOXRbKGsdKhW9N+yqnS88oUbWCypoIo6vG41qapcuCChizawDSu9MGH13OIu1JKxvKW3ClkszZ3H7oX1qnU1m95ma6ZZnc46UlhxgGHJYEVvWdpLw91MEXXV1LupVedM4+qmXEqhtkiVl28PAqgKqE/FpjwU3FDqbvXUQ6Weblwln+PGQeuwWlu6fV3l7S3eMqHN2j3LqMv1NrnCVl/GfJm0Bli6klwk3NW8Za8tVsVkhUf4ukdHQ8U9Wa4h1fED+bpe3+3Dq2UblnbAkULaqZC1v7HeBYeddWuHoTy4ije2p2hTvnsOkA8cT2mye7EDT+1p4PCx2Ip3QEtJoVEm5VdS3Ukc2Hw2+kQZl7mwKHgsuEzl/i8rrrgeoIdIyDcjJUl4b3LG8HpOqUzuzNlcrdvw+3E2Amyd7riGLbsTztjAkXs+U5mYv31Q3uiJzGSPqeccz9d+xfjr3+AfeubL6eR6PLqcl97wXk2N+9sryvY7c57ZwUX28Y/G+cdj8OoKxDqawIfVB+mdlXoifu1c2XSw4uxRXoUT1GcXTYmkXVD21X+5QS5D5tiEObFqbWPmxMhxc+LHQHNCc2q9Ob2DRk7HV3U1smJqmUaWvsI1kjK6v99bewhidizKbRfyu5tSOV/bF1wWhF51NhZcGDkeXPjvNb5PcPm+powWUZNdNYJL+82J3Uo3kvbIpAopD0vk382EtDcrNA5obleUS5X8nORiRazNsk52s8KFeZQFs24p/WrHhZ9dZV7zDrAB02wkhZJJFdInNE00zbYz7ySwSS1hUUiwi3f5OW8S1sEL7hLWcWFqV5lXmVB3mSnoCk+rHNIA9mkkw5dJFbL7NmCfDtgvRoS3R4S2mCZvEm2kZKDQKtQMeAvh8a2zv4+5RjvFHzWnbQcttkvTohI9S4pmzCNG6S9qP6Q39RcGjRvpt/HoZjQ3zmkTK/KuhHcyv0Z32a9ZKLRrbsMyU7iQeGdJJqPKwC8Npk0V+6asfDBVBYVHh2lVRTRAhUNjRWMtGutJlEha0htwPIeVKEwGgfrqH62oRbU9WRiHZ42UoBVahRo0Qr3v7gExenQ5eiDUOymFQ2PtsrEi1EOo9743muhN6nhiWsJFT1yfdzK/3s0T00XLPLG+Bi8N7oZNlSQO8kJ6rULYhMaKxrqXsepbK1DiaC1oLXprQSPBTBwz8apMvHYz1vEQYP0eryPmwm3PZFvyu6EjasGB1QiJausunhrp/1NoFRoA8eLpu0cBhJldhplYQTkphUNj7bKx4sUTQr2mf+lR/IOqDf0cvZRw8cfpmv8PJSLCdgQZrKl0raaCrUgnpXBorF02VkSE2Ir0Doiwmb+CUkq4+DdREBG2ylFikOlykEFEeFIKh8baZWNFRIiIEJvT2+FN8Lamy7c1eLV6UgqHxtplY8XmdLQWtBZsTsdMvCW8+wEycWxOZ3toeyaLHUun3rHUzF+qLSVc/Lu1eD/VqmCBaLTLaBQLLSelcGisXTZWvJ9CRNgwIvQfQhI82+z/t95I87qGYqFrXZ6D4K8l8QSrLF2rsmBz0kkpHBprl40VwR82Jx0T/DXSp66hWGhQR/DXEp+I8aTL8QTB30kpHBprl40VwR+CP+xMb4c3wTuYLt/B4IXpSSkcGmuXjRU709Fa0FqwMx0z8Zbw7gfIxLEzne2h7Zks9iGdbB9SIy3pGoqFXnS8impJXEDg2WXgiTWVk1I4NNYuGyteRSH4Ox74swLiEHcbNd+Mnqdc1ZQu5iIobEmcwUJL1wot2J90UgqHxtplY0VQiP1J7wEKm25Sz1OualZHUNgGX4lxpstxBkHhSSkcGmuXjRVBIYJCbFpvhzfBO5su39ngBetJKRwaa5eNFZvW0VrQWrBpHTPxlvDuB8jEsWmd7aHtmSz2LZ1831LTzet5ylVN7HhF1YZ4gYC0y4AUay0npXBorF02VryiQlDYMCgkoOqAw8J43VQTu4ZioXldnoPgryXxBAstXSu0YH/SSSkcGmuXjRXBH/YnHRP8NdKsrqFYaFJH8NcSn4jxpMvxBMHfSSkcGmuXjRXBH4I/bE5vhzfBO5gu38HghelJKRwaa5eNFZvT0VrQWrA5HTPxlvDuB8jEsTmd7aHtmSz2IZ1sH1IjTekaioVmdLyKaklcQODZZeCJNZWTUjg01i4bK15FIfhrGPw5/nptP/gB+1UgeSZeU83oFZQLTem6ue8ACstyzcq8bo/+DX10O3TV5q2/ec1ppJOtgnKhow0157tozjvwthAAK939HlC2krd7r6q/53gLtbeLu3adbK+CVuOVp+bdTyPViwrKhSrG93U/p2UiTWvO5fQGoNlHIe3hL2MzJ+uAbNf2qxVuiROCedJ/KRabm5/nxu1sdDOcfTH+YX4ZGFwgHKkn42l+MEip6AaN+8no13tTOUUyY2AsyNL13EQvaHLAn65dj9iPRH4UevY2XPmRtbWjFX+29deu8yrPcgIC8W9h2ZEB5x1nOzj7+NPtbPjpZgjQ82FNAGIt/b5cXdNOkDlzJng4mlyZn3M8dBff1IIe5YMQR66S108VQ0y4AtXZcwF+1AJtiQUqVZ3ogcleZGXpox9Hjr8hVA34R70m6FUgRfG5x3mpS0PJ+vmHnr8gWtLR6zY3AEe8Ht6P52A5oCKRHUjCHxhUtcKV/OTFXq+tyIVjhQTcDORPYiawEAhEcVhU7K+xG5BN4uFC8GHh0gW+g8syqd/MJj6TwF26DvNyWzsMtbO+xpA2UwpWGDsOCUPNnIBQnoEjjIE7+WFx5p8HxgP1jUBq98xVvLFBZuBZA+p/6RZ3vxQAk4KFxp6EL0hUryhXeVQoefGcfFoiXOCedpa8n9BZkY29e+r5gLucPlfJVGPPqCPgcSL8umbJKHxwI2Jt7BCYw925Z28IONQeNbvUKKyHGJgd9VQz1cygNllmWf1kgCm5pNYqr6nhDmQLrOcfZHfFoyfdivy4r/jwPUmyLwWSiuffk2TqGwpE5YBXHb0AXACnEuMl4LcadSlbAszyIqBZqZcs7dKZSTry8FqxCnjsNITpJ0i+1yhRoDN9iFO4c7Z7AkdlNzfm1Qg4/vGn4ZhiOcZ4ldUzczK8AZlM1efWmjzazqsqMhRSJqS0oDODbdyOh5cmQ785FpW5ieyw8onkM6RbFYBYISVwqkQzgaD1CTOvqRU6y1OKIhdKUQON8vfseOFGoCpuFkRlDg/v59PRBOjcmBMAfszxWk/k9agQY0uDmR+HVoXOAXXyUj3BdmhMKAKNHRrGXXr5BHv96AdutNqIEFk+twoq7/LgsoikmKc8L9Xg+uFMWUbCu+o6OcArL8ANAFBITHisZwhiQyKbR3qqMhDon8+tjfvIU9SF75GeeL2vr2GnESoU1W9q0xXl7p3TYG/O09Z3aXq8czKAFcCbkC4DdtnUmM/TbjEv80SJ+5EZ0weeDBjXzox/Dsf35p3R13AIPMd5Ty12wJv5/JRx/4LO/fh/d2Dhmg=='}


if __name__ == "__main__":
    raise SystemExit(main())
