"""Controller assembly contracts, exercised through the legacy service entry."""

import ast
import importlib.util
import inspect
from importlib import import_module
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "services" / "herdr-controller.py"


def load_controller(name):
    spec = importlib.util.spec_from_file_location(name, ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_service_entry_stays_within_500_lines():
    assert len(ENTRY.read_text().splitlines()) <= 500


def test_legacy_function_signatures_survive_assembly():
    controller = load_controller("controller_signatures")
    for name, expected in {
        "get_task": "(task_id)",
        "load_tasks": "()",
        "_handle_coordinator_item": "(item)",
        "try_direct_stage_advance": "(item)",
        "process_completion_observation": "(task, now=None)",
        "finalize_completed_task": "(task_id, before_action=None)",
        "wait_for_coordinator_decision": "(task_id, timeout=None)",
    }.items():
        assert inspect.isfunction(getattr(controller, name))
        assert str(inspect.signature(getattr(controller, name))) == expected


def test_separate_entry_loads_keep_state_and_dependency_overrides_isolated(monkeypatch):
    first = load_controller("controller_first")
    second = load_controller("controller_second")
    assert first.coordinator_queue is not second.coordinator_queue
    assert first.lock is not second.lock
    assert first.queued_events is not second.queued_events
    monkeypatch.setattr(first, "_workflow_entry", lambda _: {"status": "completed"})
    monkeypatch.setattr(second, "_workflow_entry", lambda _: {"status": "running"})
    assert first.workflow_closed("wf") is True
    assert second.workflow_closed("wf") is False


def test_coordinator_router_keeps_kind_precedence_and_task_fallback(monkeypatch):
    controller = load_controller("controller_router")
    seen = []
    routes = {
        "workflow_continuation": "handle_workflow_continuation",
        "fix_loop": "_handle_fix_loop_item",
        "stage_advance": "_handle_stage_advance_item",
        "task": "_handle_task_event",
    }
    for kind, name in routes.items():
        monkeypatch.setattr(controller, name, lambda item, kind=kind: seen.append((kind, item)))
    for kind in routes:
        item = {"kind": kind, "task_id": "same-task"}
        controller._handle_coordinator_item(item)
        assert seen[-1] == (kind, item)
    assert len(seen) == 4


def test_blocked_sla_resources_and_queued_callbacks_belong_to_the_entry(monkeypatch):
    first = load_controller("controller_blocked_first")
    second = load_controller("controller_blocked_second")
    assert first._BLOCKED_SLA_EXECUTOR is not second._BLOCKED_SLA_EXECUTOR
    assert first._BLOCKED_SLA_EXECUTOR._max_workers == 4
    assert first._blocked_sla_scheduled is not second._blocked_sla_scheduled
    assert first._blocked_sla_schedule_lock is not second._blocked_sla_schedule_lock

    class DeferredExecutor:
        def __init__(self):
            self.callbacks = []

        def submit(self, callback):
            self.callbacks.append(callback)

    first_executor, second_executor = DeferredExecutor(), DeferredExecutor()
    monkeypatch.setattr(first, "_BLOCKED_SLA_EXECUTOR", first_executor)
    monkeypatch.setattr(second, "_BLOCKED_SLA_EXECUTOR", second_executor)
    seen = []
    monkeypatch.setattr(first, "process_blocked_sla_task", lambda *a, **k: seen.append("old"))
    monkeypatch.setattr(second, "process_blocked_sla_task", lambda *a, **k: seen.append("second"))
    task = {"task_id": "same-task"}
    assert first.schedule_blocked_sla_task(task, now=1.0) is True
    assert first.schedule_blocked_sla_task(task, now=1.0) is False
    assert second.schedule_blocked_sla_task(task, now=1.0) is True
    monkeypatch.setattr(first, "process_blocked_sla_task", lambda *a, **k: seen.append("new"))
    first_executor.callbacks[0]()
    assert seen == ["new"]
    assert first._blocked_sla_scheduled == set()
    assert second._blocked_sla_scheduled == {"same-task"}
    second_executor.callbacks[0]()
    assert seen == ["new", "second"]

    class ClosedExecutor:
        def submit(self, callback):
            raise RuntimeError("executor closed")

    monkeypatch.setattr(first, "_BLOCKED_SLA_EXECUTOR", ClosedExecutor())
    assert first.schedule_blocked_sla_task(task, now=2.0) is False
    assert first._blocked_sla_scheduled == set()


def test_split_implementations_never_import_the_entry_or_execute_source():
    package = ROOT / "services" / "controller"
    assert package.is_dir()
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert "herdr-controller" not in (node.module or ""), path
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"exec", "eval", "compile"}, path


def test_declared_responsibility_dependencies_are_acyclic_and_resolvable():
    controller = load_controller("controller_dependency_graph")
    package = ROOT / "services" / "controller"
    modules = [
        import_module(f"services.controller.{path.stem}")
        for path in package.glob("*.py")
        if path.stem not in {"__init__", "bindings"}
    ]
    owners = {}
    for module in modules:
        assert len(Path(module.__file__).read_text().splitlines()) <= 500
        for operation, required in module.DEPENDENCIES.items():
            assert operation not in owners
            owners[operation] = module.__name__
            assert set(required) <= vars(controller).keys()
    graph = {module.__name__: set() for module in modules}
    for module in modules:
        for required in module.DEPENDENCIES.values():
            graph[module.__name__].update(
                owners[name] for name in required
                if name in owners and owners[name] != module.__name__
            )
    visited = set()

    def check(name, pending):
        assert name not in pending, pending + (name,)
        if name in visited:
            return
        for dependency in graph[name]:
            check(dependency, pending + (name,))
        visited.add(name)

    for name in graph:
        check(name, ())


def test_dependency_view_rejects_undeclared_reads_and_writes():
    from services.controller.bindings import Dependencies

    values = {"allowed": 1, "private": 2}
    dependencies = Dependencies(values, ("allowed",))
    assert dependencies.allowed == 1
    dependencies.allowed = 3
    assert values["allowed"] == 3
    with pytest.raises(AttributeError):
        _ = dependencies.private
    with pytest.raises(AttributeError):
        dependencies.private = 4


def test_state_read_can_run_without_loading_the_service_entry(tmp_path):
    from types import SimpleNamespace
    from services.controller.persistence import get_task
    from herdr.state_store import get_state_store

    store = get_state_store(tmp_path / "state.db")
    store.save_task({"task_id": "isolated", "workflow_id": "wf", "status": "working"})
    task = get_task(SimpleNamespace(_get_store=lambda: store), "isolated")
    assert task["task_id"] == "isolated"
    assert task["status"] == "working"


def test_ontology_audit_resolves_only_registered_implemented_operations(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["audit_ontology.py", str(ROOT)])
    spec = importlib.util.spec_from_file_location(
        "controller_ontology_audit", ROOT / "ontology" / "tests" / "audit_ontology.py"
    )
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    assert {"try_auto_accept", "try_auto_verdict", "maybe_close_completed_workflow"} <= (
        audit.controller_definitions(ENTRY)
    )
    entry = tmp_path / "entry.py"
    entry.write_text('install_modules(globals(), ("registered",))\n')
    package = tmp_path / "controller"
    package.mkdir()
    (package / "registered.py").write_text(
        'def exposed(ports): pass\n'
        'def private_helper(ports): pass\n'
        'DEPENDENCIES = {"exposed": (), "not_implemented": ()}\n'
    )
    (package / "unregistered.py").write_text(
        'def unregistered(ports): pass\nDEPENDENCIES = {"unregistered": ()}\n'
    )
    assert audit.controller_definitions(entry) == {"exposed"}
