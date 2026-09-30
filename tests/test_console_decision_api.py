#!/usr/bin/env python3
"""Console API for pipeline actions and human decision intake.

Covers:
- GET  /api/workflow/controller-actions now returns pipeline actions
      (integrate / commit / finalize / re-drive) for a workflow whose tasks
      are healthy but not yet finalized, plus a resume action when paused;
- GET  /api/workflow/decisions projects the open decision asks and the
      coordinator's latest advice from the shared docs ledger;
- POST /api/workflow/decision records a human ruling (append-only), and
      POST /api/workflow/decision/raise opens a new ask;
- POST /api/controller/execute-action dispatches the new action types.
"""

import json
from pathlib import Path

import pytest
from unittest.mock import patch, MagicMock

from console import herdr_factory_console as c
from herdr import workflow_docs as wd


@pytest.fixture
def console_env(tmp_path, monkeypatch):
    wf_file = tmp_path / "workflows.json"
    tasks_file = tmp_path / "tasks.json"
    docs_dir = tmp_path / "docs"

    monkeypatch.setenv("WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setenv("TASKS_FILE", str(tasks_file))
    monkeypatch.setenv(wd.DOCS_DIR_ENV, str(docs_dir))
    monkeypatch.setattr(c, "WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setattr(c, "TASKS_FILE", str(tasks_file))

    wf_data = {
        "workflows": {
            "wf-test-02": {
                "workflow_id": "wf-test-02",
                "title": "交付链路工作流",
                "status": "paused",
                "project_root": "/tmp/test-proj2",
            }
        }
    }
    tasks_data = {
        "tasks": [
            {
                "task_id": "wf-test-02-impl",
                "workflow_id": "wf-test-02",
                "node": "implementation",
                "stage": "implementation",
                "status": "committed",
                "agent": "opencode",
                "integration_mode": "git",
                "pane_id": "w1:p3",
            },
            {
                "task_id": "wf-test-02-working",
                "workflow_id": "wf-test-02",
                "node": "implementation",
                "stage": "implementation",
                "status": "working",
                "agent": "codex",
                "integration_mode": "git",
                "pane_id": "w1:p4",
            },
        ]
    }
    wf_file.write_text(json.dumps(wf_data), encoding="utf-8")
    tasks_file.write_text(json.dumps(tasks_data), encoding="utf-8")
    return {"wf_file": wf_file, "tasks_file": tasks_file, "docs_dir": docs_dir}


def _action_ids(res):
    return [a["action_id"] for a in res["actions"]]


def _set_escalated(console_env, task_id):
    """Mark a seeded task as machine-escalated (H-1 finalize escalation)."""
    import json as _json
    path = console_env["tasks_file"]
    data = _json.loads(path.read_text(encoding="utf-8"))
    for task in data["tasks"]:
        if task["task_id"] == task_id:
            task["finalize_escalated"] = True
            task["finalize_escalate_reason"] = "rebase conflict"
    path.write_text(_json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------- actions


def test_controller_actions_include_pipeline_steps(console_env):
    res = c.api_workflow_controller_actions("wf-test-02")
    ids = _action_ids(res)
    assert "wf-test-02-impl:integrate" in ids
    assert "wf-test-02-working:redrive" in ids
    assert "wf-test-02:resume_workflow" in ids, "paused workflow needs a resume button"
    integrate = next(a for a in res["actions"] if a["action_id"].endswith(":integrate"))
    assert integrate["group"] == "pipeline"
    assert integrate["commands"] == [["integrate", "wf-test-02-impl"]]
    assert integrate["effect"]


def test_controller_actions_never_regress_to_empty_for_pending_pipeline(console_env):
    """The reported bug: a healthy-but-unintegrated task showed zero buttons."""
    res = c.api_workflow_controller_actions("wf-test-02")
    assert res["actions"], "pipeline-only workflows must still render buttons"
    assert all(a.get("effect") for a in res["actions"]), "every button explains its effect"


def test_execute_action_task_git_step_runs_commands_in_order(console_env):
    calls = []

    def fake_run(cmd, timeout=20, check=False):
        calls.append(list(cmd))
        return MagicMock(returncode=0, stdout="ok", stderr="")

    with patch.object(c, "run", side_effect=fake_run):
        res = c.api_controller_execute_action({
            "type": "task_git_step",
            "task_id": "wf-test-02-impl",
            "workflow_id": "wf-test-02",
            "step": "accept_and_commit",
        })
    assert res.get("ok") is True
    assert [c_[1] for c_ in calls] == ["set", "commit"]
    assert calls[0][1:4] == ["set", "wf-test-02-impl", "completed"]
    assert calls[1][1:3] == ["commit", "wf-test-02-impl"]
    assert res["ran"] == 2


def test_execute_action_task_git_step_stops_at_first_failure(console_env):
    calls = []

    def fake_run(cmd, timeout=20, check=False):
        calls.append(list(cmd))
        if cmd[1] == "set":
            if check:
                raise RuntimeError("cannot set from status")
            return MagicMock(returncode=2, stdout="", stderr="cannot set from status")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    with patch.object(c, "run", side_effect=fake_run):
        with pytest.raises(RuntimeError) as exc:
            c.api_controller_execute_action({
                "type": "task_git_step",
                "task_id": "wf-test-02-impl",
                "workflow_id": "wf-test-02",
                "step": "accept_and_commit",
            })
    assert "cannot set" in str(exc.value)
    assert [x[1] for x in calls] == ["set"], "must not run commit after set failed"


def test_execute_action_redrive_uses_herdr_binary_with_wait(console_env):
    calls = []

    def fake_run(cmd, timeout=20, check=False):
        calls.append(list(cmd))
        return MagicMock(returncode=0, stdout="delivered", stderr="")

    with patch.object(c, "run", side_effect=fake_run):
        res = c.api_controller_execute_action({
            "type": "redrive",
            "task_id": "wf-test-02-working",
            "workflow_id": "wf-test-02",
            "pane_id": "w1:p4",
            "instruction": "继续当前任务并在完成后输出 HERDR_TASK_DONE",
        })
    assert res.get("ok") is True
    cmd = calls[0]
    assert cmd[:4] == ["herdr", "agent", "prompt", "w1:p4"]
    assert "--wait" in cmd


def test_execute_action_clear_escalation(console_env):
    """argv is core-authored, so the task must really carry the escalation."""
    _set_escalated(console_env, "wf-test-02-impl")
    calls = []
    with patch.object(c, "run", side_effect=lambda cmd, timeout=20, check=False:
                      (calls.append(list(cmd)),
                       MagicMock(returncode=0, stdout="ok", stderr=""))[1]):
        res = c.api_controller_execute_action({
            "type": "clear_escalation", "task_id": "wf-test-02-impl",
            "workflow_id": "wf-test-02",
        })
    assert res.get("ok") is True
    assert calls[0][1:3] == ["clear-escalation", "wf-test-02-impl"]


def test_action_backed_branches_refuse_when_the_precondition_is_gone(console_env):
    """A stale console tab must not be able to run a now-invalid action."""
    for act_type in ("clear_escalation", "supersede", "redrive"):
        with pytest.raises(RuntimeError):
            c.api_controller_execute_action({
                "type": act_type, "task_id": "wf-test-02-impl",
                "workflow_id": "wf-test-02",
            })


def test_action_backed_branches_refuse_an_unknown_task(console_env):
    with pytest.raises(RuntimeError):
        c.api_controller_execute_action({
            "type": "redrive", "task_id": "nope", "workflow_id": "wf-test-02",
        })


def test_redrive_uses_the_herdr_binary_declared_by_the_core(console_env):
    calls = []
    with patch.object(c, "run", side_effect=lambda cmd, timeout=20, check=False:
                      (calls.append(list(cmd)),
                       MagicMock(returncode=0, stdout="delivered", stderr=""))[1]):
        res = c.api_controller_execute_action({
            "type": "redrive", "task_id": "wf-test-02-working",
            "workflow_id": "wf-test-02", "pane_id": "w1:p4",
            "instruction": "客户端伪造的指令必须被忽略",
        })
    assert res["ok"] is True
    assert calls[0][:4] == ["herdr", "agent", "prompt", "w1:p4"]
    assert "客户端伪造的指令必须被忽略" not in " ".join(calls[0])
    assert "--wait" in calls[0]


def test_execute_action_rejects_unknown_task_git_step(console_env):
    with pytest.raises(RuntimeError):
        c.api_controller_execute_action({
            "type": "task_git_step", "task_id": "x",
            "workflow_id": "wf-test-02", "step": "not_a_step",
        })


def test_console_does_not_keep_its_own_copy_of_the_delivery_chain(console_env):
    """A second argv table in the shell would drift from the core's."""
    src = Path(c.__file__).read_text(encoding="utf-8")
    assert "PIPELINE_STEP_COMMANDS" not in src
    assert "herdr_controller_actions.GIT_PIPELINE_FORWARD" in src


def test_execute_action_ignores_client_supplied_argv(console_env):
    """argv comes from the core table, never from the request body."""
    calls = []
    with patch.object(c, "run", side_effect=lambda cmd, timeout=20, check=False:
                      (calls.append(list(cmd)),
                       MagicMock(returncode=0, stdout="ok", stderr=""))[1]):
        c.api_controller_execute_action({
            "type": "task_git_step",
            "task_id": "wf-test-02-impl",
            "workflow_id": "wf-test-02",
            "step": "integrate",
            "commands": [["close-workflow", "wf-test-02", "--force"]],
        })
    assert calls[0][1] == "integrate"
    assert calls[0][2] == "wf-test-02-impl"


def test_close_workflow_force_is_not_reachable_from_the_api(console_env):
    """`--force` bypasses the whole human-confirmation contract.

    No action ever declares it, so accepting it from the request body would
    hand a crafted POST a one-click workflow kill.
    """
    calls = []
    with patch.object(c, "run", side_effect=lambda cmd, timeout=20, check=False:
                      (calls.append(list(cmd)),
                       MagicMock(returncode=0, stdout="ok", stderr=""))[1]):
        c.api_controller_execute_action({
            "type": "close_workflow", "workflow_id": "wf-test-02",
            "task_id": "wf-test-02-impl", "accept_escalated": True, "force": True,
        })
    assert "--force" not in calls[0]
    assert "--accept-escalated" in calls[0]


def test_halt_and_steer_reuse_the_existing_in_process_helpers(console_env):
    """No reason to shell out when herdr.steering already does it in-process."""
    with patch.object(c, "api_task_halt", return_value={"ok": True}) as halt, \
         patch.object(c, "api_task_steer", return_value={"ok": True}) as steer:
        c.api_controller_execute_action({
            "type": "halt", "task_id": "wf-test-02-working",
            "workflow_id": "wf-test-02", "reason": "人工制动",
        })
        c.api_controller_execute_action({
            "type": "steer", "task_id": "wf-test-02-working",
            "workflow_id": "wf-test-02", "instruction": "继续",
        })
    halt.assert_called_once()
    steer.assert_called_once()


def test_run_task_commands_rejects_absolute_or_flag_first_argv(console_env):
    for bad in (["herdr-task", "integrate", None],
                ["--force", None]):
        with pytest.raises(RuntimeError):
            c._run_task_commands("wf-test-02-impl", [bad])


def test_every_generated_pipeline_step_is_executable_through_the_api(console_env):
    """Round-trip: whatever the action advertises, the executor accepts."""
    from herdr import controller_actions as ca

    res = c.api_workflow_controller_actions("wf-test-02")
    generated = {
        a["api_payload"]["step"]
        for a in res["actions"]
        if a["api_payload"].get("type") == "task_git_step"
    }
    assert generated, "expected at least one pipeline step to advertise"
    for step in generated:
        with patch.object(c, "run", side_effect=lambda *a, **k:
                          MagicMock(returncode=0, stdout="ok", stderr="")):
            out = c.api_controller_execute_action({
                "type": "task_git_step", "task_id": "wf-test-02-impl",
                "workflow_id": "wf-test-02", "step": step,
            })
        assert out["ok"] is True
    # Every step the core can emit must be executable via the same resolver,
    # whether or not this particular workflow happens to be in that state.
    for row in ca.GIT_PIPELINE_FORWARD:
        name = row[1]
        assert c.pipeline_step_commands(name), name


# --------------------------------------------------------------- decisions


def test_decisions_endpoint_returns_empty_for_clean_workflow(console_env):
    res = c.api_workflow_decisions("wf-test-02")
    assert res["decisions"] == []
    assert isinstance(res["advice"], list)


def test_raise_then_resolve_decision_round_trip(console_env):
    c.api_workflow_decide_raise({
        "workflow_id": "wf-test-02",
        "decision_id": "DU-10",
        "question": "MATCH 是否入 V1？",
        "options": ["入", "不入"],
        "recommended": "入",
        "node": "implementation",
    })
    res = c.api_workflow_decisions("wf-test-02")
    assert [d["decision_id"] for d in res["decisions"]] == ["DU-10"]
    assert res["decisions"][0]["options"] == ["入", "不入"]

    c.api_workflow_decide_resolve({
        "workflow_id": "wf-test-02",
        "decision_id": "DU-10",
        "decision": "入 V1",
    })
    assert c.api_workflow_decisions("wf-test-02")["decisions"] == []


def test_decisions_advice_includes_latest_notes(console_env):
    wd.append_note("wf-test-02", kind="decision", title="Wave-2 归属裁定",
                   body="FondsPredicate 由 T2 独占创建。", node="implementation")
    res = c.api_workflow_decisions("wf-test-02")
    titles = [a["title"] for a in res["advice"]]
    assert "Wave-2 归属裁定" in titles
    assert res["advice"][0]["is_decision"] is False, "no decision_id means a record, not an ask"


def test_raise_decision_validates_input(console_env):
    with pytest.raises(RuntimeError):
        c.api_workflow_decide_raise({"workflow_id": "wf-test-02", "question": "x"})
    with pytest.raises(RuntimeError):
        c.api_workflow_decide_raise({"decision_id": "D1", "question": "x"})


def test_resolve_decision_validates_input(console_env):
    with pytest.raises(RuntimeError):
        c.api_workflow_decide_resolve({"workflow_id": "wf-test-02", "decision": "x"})
    with pytest.raises(RuntimeError):
        c.api_workflow_decide_resolve({"decision_id": "D1", "decision": "x"})
