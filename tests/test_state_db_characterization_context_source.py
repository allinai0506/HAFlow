"""Characterization table for ``_validate_context_source_existence``.

T3-5 must move this 667-line function as one block.  Before it does, this file
pins the current input -> (pass | ValueError) mapping so the move cannot
silently change validation order or messages.

The table deliberately records *today's* boundary semantics:

* ``run_scope`` (not ``run_id``) is the cross-run boundary for referenced
  tasks; a task in the same workflow/scope with another ``run_id`` passes.
* task-less observations are accepted only when their ``run_id`` maps to the
  request's ``run_scope`` (the ``taskless_allowed_runs`` path).
* ``policy:`` references are declared-only; they pass at the top level but may
  not be the ``source_ref`` of an item (completed/findings/...).
* ``artifact:``/``evidence:`` are valid reference *patterns* but are not
  stored source records, so existence validation rejects them.
* malformed patterns are rejected at the ``save_working_context`` boundary by
  ``_valid_context_source_ref``; this function assumes that gate has run.

Every case uses a temporary database.  No production state is touched.
"""

from __future__ import annotations

import ast
import inspect
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from herdr import state_db


@dataclass(frozen=True)
class ContextCase:
    name: str
    overrides: dict[str, Any] = field(default_factory=dict)
    match: str | None = None  # None => expected to pass


def _task(task_id: str, *, workflow_id: str = "wf", scope: str = "scope", run_id: str | None = None, **extra: Any) -> dict[str, Any]:
    value = {
        "task_id": task_id,
        "workflow_id": workflow_id,
        "run_id": run_id or f"run-{task_id}",
        "workflow_run_id": scope,
        "node": "review",
        "stage": "review",
        "agent": "developer",
        "agent_role": "developer",
        "status": "working",
        "goal": f"goal for {task_id}",
        "created_at": 1.0,
    }
    value.update(extra)
    return value


def _head(conn: sqlite3.Connection, *, run_scope: str, workflow_id: str, task_id: str) -> None:
    conn.execute(
        """
        INSERT INTO working_context_source_heads
            (run_scope, workflow_id, task_id, source_version, revision, updated_at)
        VALUES (?, ?, ?, 'v1', 1, 1.0)
        ON CONFLICT(run_scope, workflow_id, task_id) DO UPDATE SET
            source_version = excluded.source_version,
            revision = excluded.revision,
            updated_at = excluded.updated_at
        """,
        (run_scope, workflow_id, task_id),
    )
    conn.commit()


@pytest.fixture()
def context_db(tmp_path: Path) -> dict[str, Any]:
    db = tmp_path / "state.db"
    state_db.init_db(db)
    state_db.save_workflow(
        {
            "workflow_id": "wf",
            "title": "wf",
            "status": "running",
            "config": {"nodes": [{"id": "review", "depends_on": []}]},
        },
        db_path=db,
    )
    state_db.save_workflow(
        {
            "workflow_id": "wf2",
            "title": "wf2",
            "status": "running",
            "config": {"nodes": [{"id": "review", "depends_on": []}]},
        },
        db_path=db,
    )

    target = _task(
        "task-target",
        artifacts=[{"ref": "artifact-1"}, {"path": "artifact-2"}],
        open_questions=["question-1"],
    )
    state_db.save_task(target, db_path=db)
    state_db.save_task(_task("task-peer"), db_path=db)
    state_db.save_task(_task("task-other-run", run_id="run-other"), db_path=db)
    state_db.save_task(_task("task-other-wf", workflow_id="wf2", scope="scope2"), db_path=db)
    state_db.save_task(_task("task-other-scope", scope="scope-x", run_id="run-other-scope"), db_path=db)
    state_db.save_task(_task("task-no-head"), db_path=db)

    conn = state_db.get_db_connection(db)
    try:
        # Normal heads: registered directly so odd target identities are reachable.
        _head(conn, run_scope="scope", workflow_id="wf", task_id="task-target")
        _head(conn, run_scope="scope", workflow_id="wf", task_id="task-peer")
        # Deliberately inconsistent heads so the target-identity checks are
        # reachable (the head lookup happens before those checks).
        _head(conn, run_scope="scope", workflow_id="wf", task_id="task-other-wf")
        _head(conn, run_scope="scope", workflow_id="wf", task_id="task-other-scope")

        observation_base = {
            "source_type": "agent_log",
            "content_ref": "content",
            "media_type": "text/plain",
            "size_bytes": 1,
            "sha256": "a" * 64,
            "created_at": 1.0,
        }
        state_db.insert_observation(
            {
                "observation_id": "obs-taskless",
                "run_id": "run-task-target",
                "task_id": None,
                "workflow_id": "wf",
                "source_ref": "pane:taskless",
                **observation_base,
            },
            db_path=db,
        )
        state_db.insert_observation(
            {
                "observation_id": "obs-taskless-orphan",
                "run_id": "run-orphan",
                "task_id": None,
                "workflow_id": "wf",
                "source_ref": "pane:orphan",
                **observation_base,
            },
            db_path=db,
        )
        state_db.insert_observation(
            {
                "observation_id": "obs-task",
                "run_id": "run-task-target",
                "task_id": "task-target",
                "workflow_id": "wf",
                "source_ref": "pane:task",
                **observation_base,
            },
            db_path=db,
        )
        trajectory = state_db.record_trajectory_event(
            {
                "run_id": "run-task-target",
                "task_id": "task-target",
                "workflow_id": "wf",
                "event_type": "note",
            },
            db_path=db,
        )
        conn.execute(
            """
            INSERT INTO eval_results
                (eval_id, run_id, revision, verdict, created_at, task_id, workflow_id)
            VALUES ('eval-1', 'run-task-target', 1, 'pass', 1.0, 'task-target', 'wf')
            """,
        )
        conn.commit()
    finally:
        conn.close()

    handoff_ok = state_db.create_collaboration_event(
        {
            "run_id": "scope",
            "workflow_id": "wf",
            "from_task_id": "task-target",
            "to_task_id": "task-peer",
            "source_fact_id": "fact-ok",
            "type": "HANDOFF",
        },
        db_path=db,
    )
    handoff_bad_run = state_db.create_collaboration_event(
        {
            "run_id": "scope-other",
            "workflow_id": "wf",
            "from_task_id": "task-target",
            "to_task_id": "task-peer",
            "source_fact_id": "fact-run",
            "type": "HANDOFF",
        },
        db_path=db,
    )
    handoff_bad_task = state_db.create_collaboration_event(
        {
            "run_id": "scope",
            "workflow_id": "wf",
            "from_task_id": "task-target",
            "to_task_id": "task-other-scope",
            "source_fact_id": "fact-task",
            "type": "HANDOFF",
        },
        db_path=db,
    )

    return {
        "db": db,
        "trajectory_id": trajectory["id"],
        "handoff_ok": handoff_ok["event_id"],
        "handoff_bad_run": handoff_bad_run["event_id"],
        "handoff_bad_task": handoff_bad_task["event_id"],
    }


def _base_context() -> dict[str, Any]:
    return {
        "context_id": "wc-characterization",
        "workflow_id": "wf",
        "run_scope": "scope",
        "run_id": "run-task-target",
        "task_id": "task-target",
        "node_id": "review",
        "agent_role": "developer",
        "goal_source_ref": "task:task-target",
        "source_refs": [],
    }


CASES: list[ContextCase] = [
    # Happy paths.
    ContextCase("self task reference"),
    ContextCase("workflow reference", {"source_refs": ["workflow:wf"]}),
    ContextCase("declared policy reference", {"source_refs": ["policy:review-policy"]}),
    ContextCase("peer task in the same scope", {"source_refs": ["task:task-peer"]}),
    ContextCase(
        "task in the same scope but another run",
        {"source_refs": ["task:task-other-run"]},
    ),
    ContextCase(
        "taskless observation whose run maps to the scope",
        {"source_refs": ["observation:obs-taskless"]},
    ),
    ContextCase(
        "task-attached observation",
        {"source_refs": ["observation:obs-task"]},
    ),
    ContextCase("trajectory event", {"source_refs": ["trajectory:evt_{trajectory_id}"]}),
    ContextCase("eval result", {"source_refs": ["eval:eval-1"]}),
    ContextCase("collaboration handoff", {"source_refs": ["collaboration:{handoff_ok}"]}),
    ContextCase(
        "valid derived artifact target",
        {"source_refs": ["task:task-target:artifact:0"]},
    ),
    ContextCase(
        "valid derived open-question target",
        {"source_refs": ["task:task-target:open_question:0:0"]},
    ),
    ContextCase(
        "current_state_refs are validated",
        {"current_state_refs": {"task": "task:task-target"}},
    ),
    # Existing object but invalid derived target.
    ContextCase(
        "artifact index past the end",
        {"source_refs": ["task:task-target:artifact:99"]},
        "invalid derived target",
    ),
    ContextCase(
        "open-question index past the end",
        {"source_refs": ["task:task-target:open_question:0:9"]},
        "invalid derived target",
    ),
    # Missing objects.
    ContextCase("missing task", {"source_refs": ["task:missing"]}, "does not exist"),
    ContextCase(
        "missing observation",
        {"source_refs": ["observation:missing"]},
        "does not exist",
    ),
    ContextCase("missing finding", {"source_refs": ["finding:missing"]}, "does not exist"),
    ContextCase("missing eval", {"source_refs": ["eval:missing"]}, "does not exist"),
    ContextCase(
        "missing trajectory",
        {"source_refs": ["trajectory:evt_999"]},
        "does not exist",
    ),
    ContextCase(
        "missing collaboration",
        {"source_refs": ["collaboration:missing"]},
        "does not exist",
    ),
    ContextCase("empty object id", {"source_refs": ["task:"]}, "has no object id"),
    # Unknown prefixes never reach here through save_working_context
    # (`_valid_context_source_ref` rejects them); directly they have no
    # stored record and fail closed through the scope check.
    ContextCase(
        "unknown prefix has no stored record",
        {"source_refs": ["unknown:thing"]},
        "crosses run scope",
    ),
    # Policy / artifact / evidence boundary.
    ContextCase(
        "policy as an item source",
        {"findings": [{"source_ref": "policy:review-policy"}]},
        "cannot use a policy source",
    ),
    ContextCase(
        "artifact ref is not a stored source",
        {"source_refs": ["artifact:build.log"]},
        "not a stored source",
    ),
    ContextCase(
        "evidence ref is not a stored source",
        {"source_refs": ["evidence:log#1"]},
        "not a stored source",
    ),
    # Scope boundaries.
    ContextCase(
        "workflow from another workflow",
        {"source_refs": ["workflow:wf2"]},
        "crosses workflow",
    ),
    ContextCase(
        "task from another workflow",
        {"source_refs": ["task:task-other-wf"]},
        "crosses workflow",
    ),
    ContextCase(
        "taskless observation with an orphan run",
        {"source_refs": ["observation:obs-taskless-orphan"]},
        "crosses run scope",
    ),
    ContextCase(
        "collaboration run scope mismatch",
        {"source_refs": ["collaboration:{handoff_bad_run}"]},
        "collaboration scope mismatch",
    ),
    ContextCase(
        "collaboration endpoint task scope mismatch",
        {"source_refs": ["collaboration:{handoff_bad_task}"]},
        "collaboration task scope mismatch",
    ),
    # Item provenance bindings.
    ContextCase(
        "workflow item carries task provenance",
        {"handoffs": [{"source_ref": "workflow:wf", "source_task": "task-target"}]},
        "workflow item cannot carry task/run provenance",
    ),
    ContextCase(
        "taskless item carries a source_task",
        {
            "evidence": [
                {"source_ref": "observation:obs-taskless", "source_task": "task-target"}
            ]
        },
        "taskless item has a source_task",
    ),
    ContextCase(
        "item source_task does not match the reference",
        {"findings": [{"source_ref": "task:task-target", "source_task": "task-peer"}]},
        "source_task does not match",
    ),
    ContextCase(
        "item source_run does not match the reference",
        {
            "findings": [
                {
                    "source_ref": "task:task-target",
                    "source_task": "task-target",
                    "source_run": "run-other",
                }
            ]
        },
        "source_run does not match",
    ),
    # Evidence refs inside an item.
    ContextCase(
        "nested evidence_ref is checked",
        {
            "evidence": [
                {
                    "source_ref": "task:task-target",
                    "evidence_refs": ["task:missing"],
                }
            ]
        },
        "does not exist",
    ),
    # Target identity checks (head rows exist for these deliberately).
    ContextCase("missing source head", {"task_id": "task-no-head"}, "not registered"),
    ContextCase(
        "target task belongs to another workflow",
        {"task_id": "task-other-wf"},
        "target task is not in the requested workflow",
    ),
    ContextCase(
        "target task has another run scope",
        {"task_id": "task-other-scope"},
        "target task scope does not match run_scope",
    ),
    ContextCase("run_id does not match the target", {"run_id": "run-other"}, "does not match target task"),
]


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_context_source_existence_characterization(context_db, case: ContextCase):
    context = _base_context()
    context.update(case.overrides)
    for key, value in list(context.items()):
        if isinstance(value, str) and "{" in value:
            for token, replacement in (
                ("{trajectory_id}", str(context_db["trajectory_id"])),
                ("{handoff_ok}", context_db["handoff_ok"]),
                ("{handoff_bad_run}", context_db["handoff_bad_run"]),
                ("{handoff_bad_task}", context_db["handoff_bad_task"]),
            ):
                value = value.replace(token, replacement)
            context[key] = value
    if isinstance(context.get("source_refs"), list):
        context["source_refs"] = [
            str(item)
            .replace("{trajectory_id}", str(context_db["trajectory_id"]))
            .replace("{handoff_ok}", context_db["handoff_ok"])
            .replace("{handoff_bad_run}", context_db["handoff_bad_run"])
            .replace("{handoff_bad_task}", context_db["handoff_bad_task"])
            for item in context["source_refs"]
        ]

    conn = state_db.get_db_connection(context_db["db"])
    try:
        if case.match is None:
            state_db._validate_context_source_existence(conn, context)
        else:
            with pytest.raises(ValueError, match=re.escape(case.match)):
                state_db._validate_context_source_existence(conn, context)
    finally:
        conn.close()


VALID_PATTERNS = [
    "task:task-1",
    "workflow:wf-1",
    "trajectory:evt_1",
    "finding:fnd-1",
    "observation:obs-1",
    "collaboration:event-1",
    "eval:eval-1",
    "policy:policy-1",
    "evidence:log#1",
    "artifact:build.log",
]


@pytest.mark.parametrize("value", VALID_PATTERNS)
def test_valid_context_source_ref_patterns(value: str):
    assert state_db._valid_context_source_ref(value)


INVALID_PATTERNS = [
    "",
    "task",
    "task-task-1",
    "unknown:thing",
    "task:",
    "task:has space",
    "policy: ",
    None,
    42,
]


@pytest.mark.parametrize("value", INVALID_PATTERNS)
def test_invalid_context_source_ref_patterns(value):
    assert not state_db._valid_context_source_ref(value)


def test_characterization_covers_every_raise_site_in_the_current_function():
    """A missing branch is a T3-5 blocker, not something T3-5 may invent.

    The baseline function has exactly 17 ``raise ValueError`` sites.  The table
    above has cases for all of them (policy, not-stored, no-id, missing,
    derived target, cross-workflow, provenance x4, collaboration x2, run scope,
    head, target workflow/scope/run).  Re-run this assertion before T3-5 and
    extend the table if the count changes.
    """
    source = inspect.getsource(state_db._validate_context_source_existence)
    tree = ast.parse(source)
    raises = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == "ValueError"
    ]
    assert len(raises) == 17, (
        f"raise-site count changed ({len(raises)}); extend the characterization "
        "table before moving the function"
    )
    assert len(CASES) >= 38
    names = [case.name for case in CASES]
    assert len(names) == len(set(names)), "case names must be unique"
