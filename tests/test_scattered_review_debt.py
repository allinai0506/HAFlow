"""批次③评审遗留守卫：#334×3 + #355×2 + #338 + #362。

1. gate 声明 v2：作者显式写下的 gate:{}（含同时写全 worker_policy 与
   default_integration_mode）是声明；归一化合成占位由
   gate_placeholder_normalized 标记唯一标识，规范化幂等。
2. 残壳回收：声明交付物仍在 clone 内时不得回收（含 close 路径 fail-closed）。
3. .agents/skills 任务自建真实内容不是 launcher 拥有物，交付不被吞。
4. 默认 stage 技能/契约只属于适配流。
5. topology 回收的正向归属：只有 stage anchor 与任务注册表记录的 pane 可关。
6. hub stop：探测不可验证 → exit 5/unverifiable，不冒充 foreign。
7. review_closure：运行级 delivery_ceiling 元数据优先于模板默认。
"""
import json
import subprocess
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from herdr.state_store import SQLiteStateStore

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------- gate v2

def test_gate_declaration_authored_vs_legacy_compat():
    """Codex PR #334 + #375 P1 双向守卫：

    - 作者只写 gate:{}（模板常规形态）→ 声明，不被吞（#334）；
    - pre-marker 存量（gate:{} + worker_policy + default_integration_mode，
      旧归一化产物）→ 占位，迁移期不误翻成门禁（#375 P1）；
    - 合成占位带 marker → 永远占位；规范化幂等。
    """
    from herdr.workflow import normalize_workflow
    from herdr.node_acceptance import node_uses_gate_loop
    normalized = normalize_workflow({"nodes": [
        {"id": "audit", "node_type": "agent", "gate": {}},
        {"id": "audit-full", "node_type": "agent", "gate": {},
         "worker_policy": {"preferred": ["codex"]},
         "default_integration_mode": "none"},
        {"id": "requirements", "node_type": "agent"},
    ]})
    audit, audit_full, requirements = normalized["nodes"]
    assert audit.get("gate_declared") is True
    assert node_uses_gate_loop(audit, "audit")
    # pre-marker 存量形态：占位，标记写入后完成迁移
    assert "gate_declared" not in audit_full
    assert audit_full.get("gate_placeholder_normalized") is True
    assert not node_uses_gate_loop(audit_full, "audit-full")
    assert requirements.get("gate_placeholder_normalized") is True
    again = normalize_workflow(normalized)
    assert again["nodes"] == normalized["nodes"]


def test_gate_normalization_is_idempotent_and_marks_synthetics():
    from herdr.workflow import normalize_workflow
    normalized = normalize_workflow({"nodes": [
        {"id": "audit", "node_type": "agent", "gate": {}},
        {"id": "requirements", "node_type": "agent"},
    ]})
    audit, requirements = normalized["nodes"]
    assert audit.get("gate_declared") is True
    assert requirements.get("gate_placeholder_normalized") is True
    assert "gate_declared" not in requirements
    again = normalize_workflow(normalized)
    assert again["nodes"] == normalized["nodes"]


# ---------------------------------------------------------------- residual

def _make_clone(tmp_path, declared_rel=None):
    # 纯 bypass 壳：除 bypass 目录内的内容外没有别的文件
    root = tmp_path / "clone"
    (root / "build").mkdir(parents=True)
    if declared_rel:
        target = root / declared_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("deliverable\n")
    return root


def test_residual_shell_keeps_declared_outputs(tmp_path):
    """Codex PR #362 P1：声明交付物仍在 clone 内时不是残壳。"""
    from herdr.clone_archive import is_residual_clone_shell
    root = _make_clone(tmp_path, declared_rel="build/analysis.md")
    # build/analysis.md 位于 bypass 目录名下：无 declared 时被判残壳（旧行为）
    assert is_residual_clone_shell(root) is True
    # 声明集合保护它
    assert is_residual_clone_shell(root, declared={"build/analysis.md"}) is False
    # 声明文件不在 clone 内 → 仍可回收
    assert is_residual_clone_shell(root, declared={"dist/other.md"}) is True


def test_residual_shell_reclaim_declares_via_close(tmp_path):
    """close 路径把声明集合传给回收；声明不可计算时 fail-closed 跳过。"""
    import inspect
    from herdr.task_cli_workflow_close import close_workflow  # noqa: F401
    source = inspect.getsource(__import__(
        "herdr.task_cli_workflow_close", fromlist=["x"]))
    assert "is_residual_clone_shell(path, declared=declared)" in source
    assert "remaining.append(task[\"task_id\"])" in source


# ---------------------------------------------------------------- launcher

def test_launcher_owned_distinguishes_symlink_skills_from_real_content(tmp_path):
    """Codex PR #355 P2：launcher 符号链接技能排除，任务自建真实内容保留。"""
    from herdr.repo_hygiene import is_launcher_owned
    (tmp_path / ".agents" / "skills" / "six-step-finish").mkdir(parents=True)
    (tmp_path / ".agents" / "skills" / "new-task-skill").mkdir(parents=True)
    (tmp_path / ".agents" / "skills" / "new-task-skill" / "SKILL.md").write_text("x\n")
    skill_link = tmp_path / ".agents" / "skills" / "linked"
    skill_link.symlink_to(tmp_path / ".agents" / "skills" / "six-step-finish")

    assert is_launcher_owned(".agents/skills/linked/SKILL.md", tmp_path) is True
    assert is_launcher_owned(".agents/skills/new-task-skill/SKILL.md", tmp_path) is False
    assert is_launcher_owned(".agents/notes.md", tmp_path) is True
    assert is_launcher_owned("src/main.py", tmp_path) is True


def test_manifest_records_only_actually_installed_skills(tmp_path):
    """Codex #376 二轮：clone 已有同名真实目录且 unlink 失败时，该技能
    不得记入 manifest（否则任务自建内容被当 launcher 拥有物排除）。"""
    from herdr.stage_skills import (
        LAUNCHER_MANAGED_MANIFEST, stage_skills_into_workspace,
    )
    import herdr.stage_skills as ss
    clone = tmp_path / "clone"
    target = clone / ".agents" / "skills"
    target.mkdir(parents=True)
    # clone 已有同名真实目录（含任务内容），unlink 会失败
    (target / "six-step-finish").mkdir()
    (target / "six-step-finish" / "task-content.md").write_text("task 的\n")
    # 让 resolve_stage_skill 找到可解析的同名技能（repo 内置技能）
    installed = stage_skills_into_workspace(str(clone), ["six-step-finish"])
    manifest = target / LAUNCHER_MANAGED_MANIFEST
    if manifest.exists():
        managed = json.loads(manifest.read_text())
        assert "six-step-finish" not in managed, managed
    # 任务内容不被吞（is_launcher_owned 判定为非 launcher 拥有）
    from herdr.repo_hygiene import is_launcher_owned
    assert is_launcher_owned(".agents/skills/six-step-finish/task-content.md", clone) is False


def test_manifest_file_itself_is_launcher_owned(tmp_path):
    """Codex #376 二轮：manifest 文件自身不得被当业务交付提交。"""
    from herdr.repo_hygiene import is_launcher_owned
    (tmp_path / ".agents" / "skills").mkdir(parents=True)
    (tmp_path / ".agents" / "skills" / ".herdr-launcher-managed.json").write_text("[]")
    assert is_launcher_owned(".agents/skills/.herdr-launcher-managed.json", tmp_path) is True


def test_launcher_copied_skills_owned_via_manifest(tmp_path):
    """Codex 二轮：复制回退（cross-volume）的场景——manifest 是所有权权威来源。"""
    import json as _json
    from herdr.repo_hygiene import is_launcher_owned
    from herdr.stage_skills import LAUNCHER_MANAGED_MANIFEST
    (tmp_path / ".agents" / "skills" / "copied-skill").mkdir(parents=True)
    (tmp_path / ".agents" / "skills" / "copied-skill" / "SKILL.md").write_text("x\n")
    (tmp_path / ".agents" / "skills" / "task-owned-skill").mkdir(parents=True)
    # launcher 装配记录：copied-skill 属于 launcher；task-owned-skill 不在册
    (tmp_path / ".agents" / "skills" / LAUNCHER_MANAGED_MANIFEST).write_text(
        _json.dumps(["copied-skill"]), encoding="utf-8")
    assert is_launcher_owned(".agents/skills/copied-skill/SKILL.md", tmp_path) is True
    assert is_launcher_owned(".agents/skills/task-owned-skill/SKILL.md", tmp_path) is False


def test_delivery_staging_keeps_real_skill_content(store_and_clone):
    """commit_task 的 untracked 过滤不再吞掉任务自建技能内容。"""
    from herdr.repo_hygiene import is_launcher_owned, is_internal_untracked
    clone = store_and_clone
    real_skill = ".agents/skills/new-task-skill/SKILL.md"
    # 生产过滤表达式（task_cli_delivery）：internal 且 launcher-owned 才排除
    excluded = is_internal_untracked(real_skill) and is_launcher_owned(real_skill, clone)
    assert not excluded
    launcher_link = ".agents/skills/linked/SKILL.md"
    assert is_internal_untracked(launcher_link) and is_launcher_owned(launcher_link, clone)


@pytest.fixture
def store_and_clone(tmp_path):
    clone = tmp_path / "clone"
    (clone / ".agents" / "skills" / "new-task-skill").mkdir(parents=True)
    (clone / ".agents" / "skills" / "new-task-skill" / "SKILL.md").write_text("x\n")
    linked = clone / ".agents" / "skills" / "linked"
    linked.symlink_to(clone / ".agents" / "skills" / "new-task-skill")
    return clone


# ---------------------------------------------------------------- stage gating

def test_default_stage_skills_only_for_adapted_workflows():
    """Codex PR #355 P2：默认绑定只属于适配流；显式配置恒被尊重。"""
    from herdr.stage_skills import stage_skills_for_node
    assert stage_skills_for_node("wrapup", None, None) != []
    assert stage_skills_for_node(
        "wrapup", None, {"workflow_template": "code-craft-v1"}) == []
    assert stage_skills_for_node(
        "wrapup", None, {"workflow_template": "software-development-v1"}) != []
    assert stage_skills_for_node(
        "wrapup", ["custom-x"], {"workflow_template": "code-craft-v1"}) == ["custom-x"]


# ---------------------------------------------------------------- topology

def test_topology_reclaim_requires_positive_ownership(monkeypatch):
    """Codex PR #334 P1：agentless root-cwd pane 必须被权威记录认领才可关闭。"""
    from herdr import topology

    anchor = {"pane_id": "pane-anchor", "tab_id": "tab", "label": "impl",
              "cwd": "/proj"}
    task_pane = {"pane_id": "pane-task", "tab_id": "tab", "label": "task:t1",
                 "cwd": "/proj"}
    user_shell = {"pane_id": "pane-user", "tab_id": "tab", "label": "bash",
                  "cwd": "/proj"}
    stage = {"label": "impl", "anchor_pane_id": "pane-anchor"}
    store = SimpleNamespace(list_tasks=lambda: [
        {"task_id": "t1", "pane_id": "pane-task"}])

    monkeypatch.setattr(topology, "_panes", lambda _w: [anchor, task_pane, user_shell])
    monkeypatch.setattr(topology, "_lifecycle_lock", lambda: nullcontext())
    monkeypatch.setattr(
        "herdr.state_store.get_state_store", lambda: store)
    closed = []

    def transport(argv, **kwargs):
        if argv[1:3] == ["agent", "get"]:
            return subprocess.CompletedProcess(argv, 0, '{"result":{"agent":null}}', "")
        if argv[1:3] == ["pane", "close"]:
            closed.append(argv[3])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(topology.subprocess, "run", transport)
    topology._resolve_or_create_anchor("/proj", "tab", "/proj", stage)
    # 用户 shell（无任何记录认领）必须存活；任务 pane 与 anchor 之外的
    # 已认领 pane 可回收；anchor 自身永不被关。
    assert "pane-user" not in closed
    assert "pane-anchor" not in closed


# ---------------------------------------------------------------- hub

def test_stop_endpoint_reports_unverifiable_probe_as_exit5():
    """Codex PR #338 P2：探测不可验证 → exit 5/unverifiable，不冒充 foreign。"""
    from herdr.experience import hub_lifecycle as lifecycle
    check = lifecycle.IdentityCheck(
        holds_endpoint=False, holds_cmdline=False, holds_registry=False,
        reasons=("endpoint probe unverifiable (lsof timed out)",), unverifiable=True)
    outcome = lifecycle.stop_endpoint(
        target_pid=100,
        verify=lambda: check,
        probe_listeners=lambda: [],
        pid_alive=lambda pid: False,
        manager_for_pid=lambda pid: "unknown",
        send_signal=lambda pid, sig: (_ for _ in ()).throw(AssertionError("no signal")),
    )
    assert outcome.exit_code == lifecycle.EXIT_UNVERIFIABLE
    assert outcome.state == lifecycle.STATE_UNVERIFIABLE

    confirmed = lifecycle.IdentityCheck(
        holds_endpoint=False, holds_cmdline=True, holds_registry=True,
        reasons=("pid does not hold the selected endpoint",))
    outcome2 = lifecycle.stop_endpoint(
        target_pid=100,
        verify=lambda: confirmed,
        probe_listeners=lambda: [],
        pid_alive=lambda pid: False,
        manager_for_pid=lambda pid: "unknown",
        send_signal=lambda pid, sig: (_ for _ in ()).throw(AssertionError("no signal")),
    )
    assert outcome2.exit_code == lifecycle.EXIT_AMBIGUOUS
    assert outcome2.state == lifecycle.STATE_FOREIGN


# ---------------------------------------------------------------- ceiling

def test_run_level_delivery_ceiling_overrides_template_default(tmp_path):
    """Codex PR #334 P1：运行级 delivery_ceiling 元数据优先于模板工作区默认。"""
    from herdr.review_closure import closure_required
    store = SQLiteStateStore(tmp_path / "state.db")
    wrapup_cfg = {"nodes": [{"id": "wrapup", "review_closure_required": True}]}
    # 模板默认 workspace（无运行级覆盖）→ 收尾门禁关闭
    store.save_workflow({"workflow_id": "wf-ws", "status": "running",
                         "config": {**wrapup_cfg, "delivery_ceiling_default": "workspace"}})
    assert closure_required(store, {"workflow_id": "wf-ws"}) is False
    # 运行级显式授权 PR → 门禁打开（工作区默认不得静默关掉它）
    store.save_workflow({"workflow_id": "wf-pr", "status": "running",
                         "delivery_ceiling": "pr",
                         "config": {**wrapup_cfg, "delivery_ceiling_default": "workspace"}})
    assert closure_required(store, {"workflow_id": "wf-pr"}) is True
    # 运行级显式声明 workspace → 门禁关闭
    store.save_workflow({"workflow_id": "wf-ws2", "status": "running",
                         "delivery_ceiling": "workspace",
                         "config": {**wrapup_cfg, "delivery_ceiling_default": "workspace"}})
    assert closure_required(store, {"workflow_id": "wf-ws2"}) is False
