"""Patch-interception baseline for the 22 ``herdr.state_db`` patch targets.

Every consumer in this repository patches the *facade module attribute*
(``setattr(state_db, "NAME", fake)``, ``patch.object(state_db, "NAME")`` or
``patch("herdr.state_db.NAME")``).  Moving an implementation into
``herdr/state/<domain>.py`` silently loses that interception unless the leaf
module calls ``_facade().NAME(...)`` instead of the bare global.

These tests pin the pre-split behaviour: for each of the 22 patch-surface
symbols, patch ``herdr.state_db`` and drive a **real entry point** (a state API
or a production consumer).  They must stay green after the split.

Two facts are recorded as baseline, not invented as new capabilities:

* a symbol imported by value with ``from herdr.state_db import NAME`` keeps a
  copy and is *not* intercepted by patching the facade (today's behaviour);
* ``rollout_stage_known`` has no production caller in this baseline.  Its
  patch-surface entry is exercised through a consumer function in this module;
  the absence of a production caller is recorded explicitly.

``time.*`` is explicitly out of scope for INV-BIND: patching
``herdr.state_db.time.time`` mutates the stdlib ``time`` module object, so every
module that calls ``time.time()`` is intercepted.  One test freezes that fact.
"""

from __future__ import annotations

import importlib
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from herdr import state_db
from herdr.observer.engine import TrajectoryObserver
from herdr.observer.models import TrajectoryFinding
from herdr.state_store import SQLiteStateStore

PATCH_FUNCTIONS = (
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
)
PATCH_CONSTANTS = ("CONTROLLER_DIR",)
PATCH_SYMBOLS = PATCH_FUNCTIONS + PATCH_CONSTANTS


def _spy(monkeypatch, name, fake):
    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return fake(*args, **kwargs) if callable(fake) else fake

    monkeypatch.setattr(state_db, name, spy)
    return calls


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    state_db.init_db(path)
    return path


@pytest.fixture()
def seeded(db: Path) -> Path:
    state_db.save_workflow(
        {"workflow_id": "wf-patch", "title": "patch", "status": "running"},
        db_path=db,
    )
    state_db.save_task(
        {
            "task_id": "task-patch",
            "workflow_id": "wf-patch",
            "run_id": "run-patch",
            "workflow_run_id": "scope-patch",
            "node": "review",
            "stage": "review",
            "agent": "developer",
            "status": "working",
            "goal": "patch interception",
            "created_at": 1.0,
        },
        db_path=db,
    )
    return db


def test_all_22_patch_symbols_exist_on_the_facade():
    assert len(PATCH_SYMBOLS) == 22
    missing = [name for name in PATCH_SYMBOLS if not hasattr(state_db, name)]
    assert not missing, f"missing patch-surface symbols: {missing}"


def test_get_db_connection_is_intercepted_by_save_workflow(monkeypatch, db: Path):
    original = state_db.get_db_connection
    calls = _spy(monkeypatch, "get_db_connection", original)
    state_db.save_workflow({"workflow_id": "wf-hook"}, db_path=db)
    assert calls, "save_workflow did not call state_db.get_db_connection"


def test_init_db_is_intercepted_by_state_store_constructor(monkeypatch, db: Path):
    calls = _spy(monkeypatch, "init_db", lambda path=None: Path(path or db))
    SQLiteStateStore(db)
    assert calls, "SQLiteStateStore.__init__ did not call state_db.init_db"


def test_get_default_db_path_is_intercepted_by_get_db_connection(
    monkeypatch, tmp_path: Path
):
    target = tmp_path / "default.db"
    calls = _spy(monkeypatch, "get_default_db_path", lambda: target)
    conn = state_db.get_db_connection()
    try:
        assert calls, "get_db_connection did not call state_db.get_default_db_path"
    finally:
        conn.close()


def test_get_readonly_db_connection_is_intercepted_by_list_workflows(
    monkeypatch, seeded: Path
):
    original = state_db.get_readonly_db_connection
    calls = _spy(monkeypatch, "get_readonly_db_connection", original)
    state_db.list_workflows(db_path=seeded)
    assert calls, "list_workflows did not call state_db.get_readonly_db_connection"


def test_get_credentials_dir_is_intercepted_by_get_user_token(
    monkeypatch, tmp_path: Path
):
    cred_dir = tmp_path / "credentials"
    cred_dir.mkdir()
    calls = _spy(monkeypatch, "_get_credentials_dir", lambda: cred_dir)
    assert state_db.get_user_token("nobody", "no-such-token") is None
    assert calls, "get_user_token did not call state_db._get_credentials_dir"


def test_record_event_is_intercepted_by_record_steering_history(
    monkeypatch, db: Path
):
    calls = _spy(monkeypatch, "record_event", lambda *a, **k: {"id": 1})
    state_db.record_steering_history(
        {"task_id": "task-patch", "action": "note", "instruction": "go"}, db_path=db
    )
    assert calls, "record_steering_history did not call state_db.record_event"


def test_decode_task_row_is_intercepted_by_get_task(monkeypatch, seeded: Path):
    original = state_db._decode_task_row
    calls = _spy(monkeypatch, "_decode_task_row", original)
    task = state_db.get_task("task-patch", db_path=seeded)
    assert task is not None
    assert calls, "get_task did not call state_db._decode_task_row"


def test_get_task_is_intercepted_by_sqlite_state_store(monkeypatch, seeded: Path):
    calls = _spy(monkeypatch, "get_task", lambda *a, **k: {"task_id": "fake"})
    assert SQLiteStateStore(seeded).get_task("task-patch") == {"task_id": "fake"}
    assert calls, "SQLiteStateStore.get_task did not call state_db.get_task"


def test_record_event_if_absent_is_intercepted_by_scheduler_facts(
    monkeypatch, db: Path
):
    facts = importlib.import_module("herdr.scheduler_facts")
    calls = _spy(
        monkeypatch, "record_event_if_absent", lambda *a, **k: {"status": "created"}
    )
    result = facts.record_selective_replan_decision(
        "wf-patch",
        {
            "gate_task_id": "task-patch",
            "gate_task_version": 1,
            "gate_candidate_sha": "a" * 40,
            "retry_node": "review",
            "mode": "selective",
        },
        db_path=db,
    )
    assert result["status"] == "created"
    assert calls, "scheduler_facts did not call state_db.record_event_if_absent"


def test_save_working_context_is_intercepted_by_state_store(
    monkeypatch, db: Path
):
    calls = _spy(monkeypatch, "save_working_context", lambda *a, **k: {"ok": True})
    result = SQLiteStateStore(db).save_working_context({"context_id": "wc-hook"})
    assert result == {"ok": True}
    assert calls, "SQLiteStateStore.save_working_context did not call state_db"


def test_upsert_trajectory_finding_is_intercepted_by_observer(
    monkeypatch, db: Path
):
    calls = _spy(monkeypatch, "upsert_trajectory_finding", lambda *a, **k: None)
    observer = TrajectoryObserver(
        config={"enabled": True}, store=SimpleNamespace(db_path=db)
    )
    finding = TrajectoryFinding(
        run_id="run-patch",
        finding_type="other",
        severity="warning",
        summary="patch interception",
    )
    observer._persist([finding])
    assert calls, "TrajectoryObserver._persist did not call state_db"


def test_finding_and_artifact_reads_are_intercepted_by_context_compact(
    monkeypatch, db: Path
):
    compact = importlib.import_module("herdr.context_compact")
    finding_calls = _spy(
        monkeypatch, "get_trajectory_finding_by_id", lambda *a, **k: True
    )
    artifact_calls = _spy(monkeypatch, "artifact_ref_exists", lambda *a, **k: True)
    previous = SimpleNamespace(
        important_findings=[{"finding_id": "fnd-1"}],
        evidence_refs=["obs-1"],
        artifact_refs=[{"ref": "artifact-1"}],
        completed=[],
        open_issues=[],
        next_focus=[],
    )
    verified = compact._previously_verified_refs(previous, "run-patch", db)
    assert verified["findings"] == {"fnd-1"}
    assert verified["artifacts"] == {"artifact-1"}
    assert finding_calls, "context_compact did not call get_trajectory_finding_by_id"
    assert artifact_calls, "context_compact did not call artifact_ref_exists"


def test_list_steers_is_intercepted_by_state_store(monkeypatch, db: Path):
    calls = _spy(monkeypatch, "list_steers", lambda *a, **k: [])
    assert SQLiteStateStore(db).list_steers(task_id="task-patch") == []
    assert calls, "SQLiteStateStore.list_steers did not call state_db.list_steers"


def test_query_execution_outcomes_is_intercepted_by_adaptive_router(
    monkeypatch, db: Path
):
    router = importlib.import_module("herdr.adaptive_router")
    calls = _spy(monkeypatch, "query_execution_outcomes", lambda *a, **k: [])
    assert (
        router.collect_samples(
            db, node="review", task_type="implementation", cutoff=1.0, limit=5
        )
        == []
    )
    assert calls, "adaptive_router did not call state_db.query_execution_outcomes"


def test_get_rollout_stage_is_intercepted_by_rollout_policy(monkeypatch, db: Path):
    policy = importlib.import_module("herdr.rollout_policy")
    calls = _spy(monkeypatch, "get_rollout_stage", lambda *a, **k: 0)
    assert policy.get_stage(db, "agent", "review", "implementation") == 0
    assert calls, "rollout_policy.get_stage did not call state_db.get_rollout_stage"


def test_list_rollout_audit_is_intercepted_by_rollout_policy(monkeypatch, db: Path):
    policy = importlib.import_module("herdr.rollout_policy")
    calls = _spy(monkeypatch, "list_rollout_audit", lambda *a, **k: [])
    assert policy.get_history(db_path=db) == []
    assert calls, "rollout_policy.get_history did not call state_db.list_rollout_audit"


def test_list_rollout_states_is_intercepted_by_rollout_policy(monkeypatch, db: Path):
    policy = importlib.import_module("herdr.rollout_policy")
    calls = _spy(monkeypatch, "list_rollout_states", lambda *a, **k: [])
    assert policy.list_states(db) == []
    assert calls, "rollout_policy.list_states did not call state_db.list_rollout_states"


def test_read_rollout_snapshot_is_intercepted_by_effective_percentage(
    monkeypatch, db: Path
):
    policy = importlib.import_module("herdr.rollout_policy")
    calls = _spy(
        monkeypatch,
        "read_rollout_snapshot",
        lambda *a, **k: state_db.RolloutSnapshot(exists=False, percentage=0),
    )
    assert (
        policy.effective_percentage(
            db, agent="agent", node="review", task_type="implementation"
        )
        == 0
    )
    assert calls, "effective_percentage did not call state_db.read_rollout_snapshot"


def test_rollout_stage_known_patch_is_visible_to_a_consumer(monkeypatch):
    # Baseline fact: no production module calls rollout_stage_known today.
    # The patch surface is exercised by tests; this consumer models them.
    calls = _spy(monkeypatch, "rollout_stage_known", lambda *a, **k: True)

    def consumer():
        return state_db.rollout_stage_known("agent", "review", "implementation", None)

    assert consumer() is True
    assert calls, "rollout_stage_known patch was not visible through the facade"


def test_record_observation_receipt_is_intercepted_by_trajectory(
    monkeypatch, db: Path
):
    trajectory = importlib.import_module("herdr.trajectory")
    calls = _spy(monkeypatch, "record_observation_receipt", lambda *a, **k: {})
    observation = {
        "observation_id": "obs-patch",
        "source_type": "agent_log",
        "source_ref": "pane:1",
        "size_bytes": 12,
        "sha256": "a" * 64,
    }
    trajectory.record_observation_created(
        {"run_id": "run-patch", "task_id": "task-patch"},
        observation,
        ledger=trajectory.TrajectoryLedger(db),
    )
    assert calls, "trajectory.record_observation_created did not call state_db"


def test_controller_dir_is_intercepted_by_get_default_db_path(monkeypatch, tmp_path: Path):
    target = tmp_path / "controller"
    target.mkdir()
    monkeypatch.setattr(state_db, "CONTROLLER_DIR", target)
    for key in (
        "HERDR_STATE_DB",
        "CHECKPOINTS_DIR",
        "WORKFLOWS_FILE",
        "TASKS_FILE",
        "WORKFLOW_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    assert state_db.get_default_db_path() == target / "state.db"


def test_time_time_patch_is_effective_outside_the_facade(monkeypatch):
    # `state_db.time` is the stdlib module object.  Patching its `.time`
    # attribute is global; herdr/collaboration.py imports the same module.
    collaboration = importlib.import_module("herdr.collaboration")
    frozen = 1_234_567_890.0
    monkeypatch.setattr(state_db.time, "time", lambda: frozen)
    assert time.time() == frozen
    handoff = collaboration.create_handoff(
        run_id="run-patch",
        workflow_id="wf-patch",
        from_task_id="task-a",
        to_task_id="task-b",
        source_fact_id="fact-1",
    )
    assert handoff["created_at"] == frozen


def test_from_import_copy_keeps_the_pre_split_non_interception_baseline(
    monkeypatch, seeded: Path
):
    # herdr/execution_trace.py imports the function by value at module import
    # time.  Patching the facade attribute cannot replace that copy; this is
    # today's behaviour and the split must not make it worse.
    execution_trace = importlib.import_module("herdr.execution_trace")
    calls = _spy(
        monkeypatch,
        "get_readonly_db_connection",
        lambda *a, **k: pytest.fail("facade patch must not reach a from-import copy"),
    )
    rows = execution_trace.read_task_records(seeded)
    assert rows, "the real read path should still return the seeded task"
    assert not calls, "unexpected interception of a from-import copy"


# Frozen call-site inventory, captured before moving any production definition.
# Binding probes below complement (and do not replace) the real entry-point
# tests above: each probe executes the exact reference expression at its site.
import ast
import inspect
import json
import textwrap

SITE_MATRIX = json.loads(
    (Path(__file__).parent / 'fixtures/state_db_patch_sites.json').read_text()
)


def test_patch_matrix_is_complete():
    assert len(SITE_MATRIX['internal']) == 119
    assert sum(row['binding'] == 'module_copy' for row in SITE_MATRIX['imports']) == 8
    assert sum(row['binding'] == 'late_import' for row in SITE_MATRIX['imports']) == 26


@pytest.mark.parametrize('index,row', list(enumerate(SITE_MATRIX['internal'])),
                         ids=lambda value: str(value) if isinstance(value, int) else None)
def test_every_internal_patch_reference_resolves_facade_patch(monkeypatch, index, row):
    scope = row['scope'][0]
    function = inspect.unwrap(getattr(state_db, scope))
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    symbol = row['symbol']
    previous = [r for r in SITE_MATRIX['internal'][:index]
                if r['scope'][0] == scope and r['symbol'] == symbol]
    references = sorted(
        (node for node in ast.walk(tree)
         if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == symbol)
         or (isinstance(node, ast.Attribute) and node.attr == symbol)),
        key=lambda node: (node.lineno, node.col_offset),
    )
    assert len(references) > len(previous), f'missing call site {row}'
    reference = references[len(previous)]
    marker = object()
    monkeypatch.setattr(state_db, symbol, marker)
    resolved = eval(compile(ast.Expression(reference), '<patch-call-site>', 'eval'), function.__globals__)
    assert resolved is marker, f'facade patch lost at baseline {row}'


@pytest.mark.parametrize('row', SITE_MATRIX['imports'],
                         ids=lambda row: f"{row['binding']}:{row['file']}:{row['line']}")
def test_symbolic_import_binding_window(monkeypatch, row):
    # Execute the recorded production import statement, with its original
    # package, before/inside the patch window according to the frozen scope.
    statement = row['source']
    package = str(Path(row['file']).parent).replace('/', '.')
    namespace = {'__package__': package}
    exec(statement, namespace)
    original = {name: namespace[name] for name in row['symbols']}
    markers = {name: object() for name in row['symbols']}
    for name, marker in markers.items():
        monkeypatch.setattr(state_db, name, marker)
    if row['binding'] == 'late_import':
        exec(statement, namespace)
        assert all(namespace[name] is marker for name, marker in markers.items())
    else:
        assert all(namespace[name] is value for name, value in original.items())
        assert all(namespace[name] is not marker for name, marker in markers.items())


def test_real_path_receipt_covers_current_implementation():
    import hashlib
    receipt = json.loads((Path(__file__).parent / 'fixtures/state_db_real_path_receipt.json').read_text())
    assert receipt['baseline'] == 'a56739e'
    assert len(receipt['internal']) == 119
    assert {row['index'] for row in receipt['internal']} == set(range(119))
    for row in receipt['internal']:
        assert row['tests'], f'no real call path for {row}'
        assert row['symbol'] == SITE_MATRIX['internal'][row['index']]['symbol']
    root = Path(__file__).resolve().parents[1]
    for filename, digest in receipt['source_hashes'].items():
        assert hashlib.sha256((root / filename).read_bytes()).hexdigest() == digest, filename
    assert sum(row['binding'] == 'module_copy' for row in receipt['imports']) == 8
    assert sum(row['binding'] == 'late_import' for row in receipt['imports']) == 26
