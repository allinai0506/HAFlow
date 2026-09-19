#!/usr/bin/env python3
"""Workflow Template Execution & Context Contract V1 测试。

覆盖:
1. Legacy 模板无 execution → 默认 git(行为不变)
2. git 模板契约归一化
3. context 模板不要求 Git(项目 provision 无 git 调用)
4. required context 缺失 fail-fast
5. optional context 缺失放行
6. Context binding 随 Workflow 持久化(StateStore)
7. Workflow Context 传递到 Task(继承)
8. Agent Prompt 只给 Context 引用(id+绝对路径)，不展开内容
9. Context 模式 Task Workspace:无 clone/无 branch,但有独立 cwd
10. Context 任务禁止 git commit/integrate 路径
"""

import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent

import pytest

from herdr import workflow as wf_mod
from herdr.workflow import (
    execution_mode,
    load_template,
    normalize_workflow,
    parse_context_binding_args,
    render_context_reference_block,
    validate_context_contract,
    validate_integration_for_execution,
    validate_onto_for_execution,
)


CONTEXT_TEMPLATE = {
    "name": "sales-quotation",
    "label": "销售报价",
    "execution": {"mode": "context"},
    "context": {
        "required": [
            {"id": "company", "label": "公司公共销售知识"},
            {"id": "customer", "label": "客户工作空间"},
        ],
        "optional": [{"id": "tender", "label": "标书"}],
    },
    "nodes": [
        {"id": "analyze", "label": "信息分析", "depends_on": []},
    ],
}


# ---------------------------------------------------------------- 契约层


class TestExecutionContract(unittest.TestCase):
    def test_legacy_template_defaults_to_git(self):
        legacy = {"name": "old", "nodes": [{"id": "a", "depends_on": []}]}
        normalized = normalize_workflow(legacy)
        self.assertEqual(normalized["execution"], {"mode": "git"})
        self.assertEqual(execution_mode(legacy), "git")
        self.assertEqual(execution_mode(normalized), "git")

    def test_legacy_stages_only_defaults_to_git(self):
        legacy = {"stages": ["requirements", "plan"]}
        normalized = normalize_workflow(legacy)
        self.assertEqual(execution_mode(normalized), "git")

    def test_git_mode_normalized(self):
        wf = normalize_workflow({
            "nodes": [{"id": "a"}],
            "execution": {"mode": "git"},
        })
        self.assertEqual(wf["execution"], {"mode": "git"})

    def test_unknown_execution_mode_rejected(self):
        with self.assertRaises(ValueError):
            normalize_workflow({"nodes": [{"id": "a"}], "execution": {"mode": "docker"}})

    def test_execution_mode_only_git_or_context(self):
        self.assertEqual(wf_mod.EXECUTION_MODES, {"git", "context"})

    def test_context_contract_normalizes_shorthand_and_dict(self):
        wf = normalize_workflow({
            "nodes": [{"id": "a"}],
            "execution": {"mode": "context"},
            "context": {
                "required": ["company", {"id": "customer", "label": "客户"}],
                "optional": [],
            },
        })
        self.assertEqual(
            wf["context"]["required"],
            [
                {"id": "company", "label": "company"},
                {"id": "customer", "label": "客户"},
            ],
        )

    def test_duplicate_context_id_rejected(self):
        with self.assertRaises(ValueError):
            normalize_workflow({
                "nodes": [{"id": "a"}],
                "context": {"required": [{"id": "x"}, {"id": "x"}]},
            })

    def test_normalize_is_idempotent(self):
        once = normalize_workflow(dict(CONTEXT_TEMPLATE))
        twice = normalize_workflow(once)
        self.assertEqual(once["execution"], twice["execution"])
        self.assertEqual(once["context"], twice["context"])
        self.assertEqual(once["nodes"], twice["nodes"])


class TestContextBindingValidation(unittest.TestCase):
    def setUp(self):
        self.template = normalize_workflow(dict(CONTEXT_TEMPLATE))

    def test_missing_required_fails_fast(self):
        with self.assertRaises(ValueError) as ctx:
            validate_context_contract(self.template, {"company": "/tmp/sales"})
        self.assertIn("Missing required context: customer", str(ctx.exception))

    def test_optional_missing_allowed(self):
        validate_context_contract(self.template, {
            "company": "/tmp/sales",
            "customer": "/tmp/cust",
        })

    def test_unknown_binding_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            validate_context_contract(self.template, {
                "company": "/tmp/s", "customer": "/tmp/c", "secret": "/x",
            })
        self.assertIn("secret", str(ctx.exception))

    def test_empty_binding_treated_as_missing(self):
        with self.assertRaises(ValueError):
            validate_context_contract(self.template, {"company": "/tmp/s", "customer": "  "})

    def test_git_template_takes_no_bindings(self):
        git_wf = normalize_workflow({"nodes": [{"id": "a"}]})
        validate_context_contract(git_wf, {})
        with self.assertRaises(ValueError):
            validate_context_contract(git_wf, {"company": "/tmp/x"})

    def test_parse_binding_args(self):
        self.assertEqual(
            parse_context_binding_args(["company=/a", "customer=/b c"]),
            {"company": "/a", "customer": "/b c"},
        )
        with self.assertRaises(ValueError):
            parse_context_binding_args(["broken"])
        with self.assertRaises(ValueError):
            parse_context_binding_args(["=nope"])
        with self.assertRaises(ValueError):
            parse_context_binding_args(["a=/x", "a=/y"])


# ---------------------------------------------------------------- 模板


class TestTemplates(unittest.TestCase):
    def test_software_development_v1_is_git_mode(self):
        wf = load_template("software-development-v1")
        self.assertEqual(execution_mode(wf), "git")

    def test_legacy_bundled_templates_all_git(self):
        from herdr.workflow import list_templates
        for name in list_templates():
            wf = load_template(name)
            if name == "context-smoke-test":
                continue
            self.assertEqual(execution_mode(wf), "git", f"template {name} must stay git")

    def test_context_smoke_test_template(self):
        wf = load_template("context-smoke-test")
        self.assertEqual(execution_mode(wf), "context")
        ids = [r["id"] for r in wf["context"]["required"]]
        self.assertEqual(ids, ["common", "workspace"])


# ---------------------------------------------------------------- Prompt 引用块


class TestContextPromptBlock(unittest.TestCase):
    def test_block_contains_ids_and_absolute_paths_only(self):
        block = render_context_reference_block({
            "company": "/abs/sales-common",
            "customer": "/abs/customers/福寿康",
        })
        self.assertIn("company", block)
        self.assertIn("/abs/sales-common", block)
        self.assertIn("customer", block)
        self.assertIn("/abs/customers/福寿康", block)
        self.assertIn("Workflow Context", block)
        self.assertIn("不要假设不存在的事实", block)
        # 只有引用，没有展开内容
        self.assertLess(len(block), 800)

    def test_empty_bindings_no_block(self):
        self.assertEqual(render_context_reference_block({}), "")


# ---------------------------------------------------------------- 集成模式防呆


class TestIntegrationGuard(unittest.TestCase):
    def test_context_mode_rejects_git_integration(self):
        with self.assertRaises(ValueError):
            validate_integration_for_execution("context", "git")
        validate_integration_for_execution("context", "none")
        validate_integration_for_execution("git", "git")
        validate_integration_for_execution("git", "none")


# ---------------------------------------------------------------- StateStore 持久化 + Task 继承


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    db_file = tmp_path / "state.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(db_file))
    monkeypatch.setenv("WORKFLOWS_FILE", str(tmp_path / "workflows.json"))
    monkeypatch.setenv("TASKS_FILE", str(tmp_path / "tasks.json"))
    from herdr.state_store import reset_state_store
    reset_state_store()
    yield {"db_file": db_file, "tmp": tmp_path}
    reset_state_store()


def test_register_workflow_persists_execution_and_context(store_env):
    from herdr.projects import register_workflow
    project = {
        "project_id": "sales-common",
        "project_name": "sales-common",
        "project_root": "/tmp/sales-common",
        "base_branch": "",
        "workspace_id": "w1",
        "coordinator_pane_id": "p1",
        "workflow_file": "/tmp/wf.json",
    }
    register_workflow(
        "wf-sales-0919-01", project, requirement="给福寿康生成报价",
        execution={"mode": "context"},
        context={"company": "/tmp/sales-common", "customer": "/tmp/cust-fsk"},
    )
    from herdr.state_store import get_state_store
    record = get_state_store().get_workflow("wf-sales-0919-01")
    assert record["execution"] == {"mode": "context"}
    assert record["context"] == {"company": "/tmp/sales-common", "customer": "/tmp/cust-fsk"}


def test_register_workflow_legacy_signature_unchanged(store_env):
    """不传 execution/context 时，记录不携带这两个键 → git 语义完全不变。"""
    from herdr.projects import register_workflow
    project = {
        "project_id": "p", "project_name": "n", "project_root": "/r",
        "base_branch": "main", "workspace_id": "w", "coordinator_pane_id": "c",
        "workflow_file": "/tmp/wf2.json",
    }
    register_workflow("wf-p-0919-01", project, requirement="x")
    from herdr.state_store import get_state_store
    record = get_state_store().get_workflow("wf-p-0919-01")
    assert "execution" not in record or record["execution"]["mode"] == "git"
    assert not record.get("context")


# ---------------------------------------------------------------- 项目 provision 去 Git 依赖


def load_module(path, name):
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, str(path))
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeHerdrCli:
    """伪造 herdr workspace/tab/pane/agent 子命令，记录全部 git 调用。"""

    def __init__(self):
        self.calls = []
        self.git_calls = []
        self._n = 0

    def next(self, kind):
        self._n += 1
        return f"{kind}-{self._n}"

    def run(self, cmd, check=True):
        self.calls.append(list(cmd))
        if cmd[0] == "git":
            self.git_calls.append(list(cmd))
            raise AssertionError(f"context mode must not call git: {cmd}")

        class R:
            returncode = 0
            stdout = "{}"
            stderr = ""
        r = R()
        return r

    def run_json(self, cmd):
        self.calls.append(list(cmd))
        head = cmd[:3]
        if head == ["herdr", "workspace", "create"]:
            return {"result": {
                "workspace": {"workspace_id": self.next("w")},
                "tab": {"tab_id": self.next("t")},
                "root_pane": {"pane_id": self.next("pane")},
            }}
        if head == ["herdr", "tab", "create"]:
            return {"result": {
                "tab": {"tab_id": self.next("t")},
                "root_pane": {"pane_id": self.next("pane")},
            }}
        return {"result": {}}


def test_ensure_context_project_skips_git(store_env, tmp_path, monkeypatch):
    from herdr import projects as projects_mod

    fake = FakeHerdrCli()
    monkeypatch.setattr(projects_mod, "_run", fake.run)
    monkeypatch.setattr(projects_mod, "_run_json", fake.run_json)
    monkeypatch.setattr(projects_mod, "ROOT", tmp_path / "ctrl")
    monkeypatch.setattr(projects_mod, "PROJECTS_FILE", tmp_path / "ctrl" / "projects.json")

    root = tmp_path / "sales-run"
    root.mkdir()

    record = projects_mod.ensure_context_project(
        root,
        template_name="context-smoke-test",
        context_bindings={"common": str(root), "workspace": str(root)},
    )
    assert record["execution"] == {"mode": "context"}
    assert fake.git_calls == []

    wf_file = Path(record["workflow_file"])
    cfg = json.loads(wf_file.read_text(encoding="utf-8"))
    assert cfg["execution"] == {"mode": "context"}
    assert [n["id"] for n in cfg["nodes"]] == ["analyze"]


# ---------------------------------------------------------------- 同一 Workspace 换模板
#
# 业务模型：Workspace Identity != Workflow Template。
# 一个业务 Workspace（如 customers/福寿康）可依次运行多个 context 模板。

CTX_BETA_TEMPLATE = """\
name: ctx-beta
label: 第二阶段分析
version: "1.0"
description: 模板切换测试用第二 context 模板
execution:
  mode: context
context:
  required:
    - common
    - workspace
nodes:
  - id: evaluate
    label: 评估
    node_type: agent
    purpose: 评估上下文产物
    default_task_type: docs
    default_integration_mode: none
"""


def _context_switch_env(store_env, tmp_path, monkeypatch):
    """共享的 fake herdr CLI + 隔离状态 + 用户模板目录(ctx-beta)。返回 (fake, root)。"""
    from herdr import projects as projects_mod
    from herdr import workflow as wf_mod

    fake = FakeHerdrCli()
    monkeypatch.setattr(projects_mod, "_run", fake.run)
    monkeypatch.setattr(projects_mod, "_run_json", fake.run_json)
    monkeypatch.setattr(projects_mod, "ROOT", tmp_path / "ctrl")
    monkeypatch.setattr(projects_mod, "PROJECTS_FILE", tmp_path / "ctrl" / "projects.json")
    templates = tmp_path / "user-templates"
    templates.mkdir()
    (templates / "ctx-beta.yaml").write_text(CTX_BETA_TEMPLATE, encoding="utf-8")
    monkeypatch.setattr(wf_mod, "USER_TEMPLATES_DIR", templates)

    root = tmp_path / "customers-fsk"
    root.mkdir()
    bindings = {"common": str(root), "workspace": str(root)}
    return fake, projects_mod, root, bindings


def test_context_project_switches_template_keeps_workspace(store_env, tmp_path, monkeypatch):
    fake, projects_mod, root, bindings = _context_switch_env(store_env, tmp_path, monkeypatch)

    record_a = projects_mod.ensure_context_project(
        root, template_name="context-smoke-test", context_bindings=bindings,
    )
    # 本次绑定与 A 不同：切换必须应用本次绑定，而非沿用旧 record。
    company_b = tmp_path / "company-b"
    company_b.mkdir()
    bindings_b = {"common": str(company_b), "workspace": str(root)}
    record_b = projects_mod.ensure_context_project(
        root, template_name="ctx-beta", context_bindings=bindings_b,
    )

    # Workspace 与 Coordinator 保留
    assert record_b["workspace_id"] == record_a["workspace_id"]
    assert record_b["coordinator_pane_id"] == record_a["coordinator_pane_id"]
    assert record_b["project_id"] == record_a["project_id"]

    # 模板与节点拓扑更新为 B
    cfg = json.loads(Path(record_b["workflow_file"]).read_text(encoding="utf-8"))
    assert cfg["workflow_template"] == "ctx-beta"
    assert [n["id"] for n in cfg["nodes"]] == ["evaluate"]

    # context 语义在切换后完整保留，且无 Git 依赖
    assert record_b["execution"] == {"mode": "context"}
    assert cfg["execution"] == {"mode": "context"}
    assert cfg["base_branch"] == ""
    assert cfg["context"] == {
        "required": [
            {"id": "common", "label": "common"},
            {"id": "workspace", "label": "workspace"},
        ],
        "optional": [],
    }
    assert cfg["context_bindings"] == bindings_b
    assert fake.git_calls == []

    # 旧模板 Node Tab 被关闭
    assert any(c[:3] == ["herdr", "tab", "close"] for c in fake.calls)


def test_context_project_switch_refuses_with_active_workflow(store_env, tmp_path, monkeypatch):
    fake, projects_mod, root, bindings = _context_switch_env(store_env, tmp_path, monkeypatch)
    record_a = projects_mod.ensure_context_project(
        root, template_name="context-smoke-test", context_bindings=bindings,
    )

    from herdr.projects import register_workflow
    register_workflow(
        "wf-switch-active-01", record_a, requirement="running",
        execution={"mode": "context"}, context=bindings,
    )

    with pytest.raises(RuntimeError, match="活跃工作流"):
        projects_mod.ensure_context_project(
            root, template_name="ctx-beta", context_bindings=bindings,
        )


def test_context_path_accepts_file_and_rejects_missing(store_env, tmp_path, monkeypatch):
    fake, projects_mod, root, _ = _context_switch_env(store_env, tmp_path, monkeypatch)
    contract_dir = tmp_path / "company"
    contract_dir.mkdir()
    quote_pdf = tmp_path / "报价单.pdf"
    quote_pdf.write_bytes(b"%PDF-1.4 fake")

    record = projects_mod.ensure_context_project(
        root,
        template_name="context-smoke-test",
        context_bindings={
            "common": str(contract_dir),
            "workspace": str(quote_pdf),  # 普通文件也是合法 Context 引用
        },
    )
    assert record["context_bindings"]["workspace"] == str(quote_pdf)
    assert fake.git_calls == []

    # 不存在的路径 fail-fast（目录/文件同理）
    with pytest.raises(RuntimeError, match="Context path not found"):
        projects_mod.ensure_context_project(
            root,
            template_name="context-smoke-test",
            context_bindings={"common": str(contract_dir), "workspace": str(tmp_path / "nope.docx")},
        )


# ---------------------------------------------------------------- Workflow Run Definition Snapshot
#
# Workflow Run 一旦创建，其执行定义不可变：项目共享 workflow.json 表示
# "下一次 Run 的当前模板"，切换覆盖它不得污染历史 Run 的 DAG。


def test_workflow_definition_snapshot_survives_template_switch(store_env, tmp_path, monkeypatch):
    fake, projects_mod, root, bindings = _context_switch_env(store_env, tmp_path, monkeypatch)
    from herdr import workflow_docs as wd
    monkeypatch.setenv(wd.DOCS_DIR_ENV, str(tmp_path / "workflows"))

    record_a = projects_mod.ensure_context_project(
        root, template_name="context-smoke-test", context_bindings=bindings,
    )
    projects_mod.register_workflow(
        "wf-snap-a", record_a, requirement="A",
        execution={"mode": "context"}, context=bindings,
    )

    # Run A 正常结束并关闭（活跃 Workflow 会拒绝切换，这是既有门禁）
    from herdr.state_store import get_state_store
    get_state_store().transition_workflow(
        "wf-snap-a", "completed", "run finished", source="test", force=True,
    )

    # 切换模板并注册新 Run（项目共享文件此刻被覆盖为 B）
    record_b = projects_mod.ensure_context_project(
        root, template_name="ctx-beta", context_bindings=bindings,
    )
    projects_mod.register_workflow(
        "wf-snap-b", record_b, requirement="B",
        execution={"mode": "context"}, context=bindings,
    )

    reg_a = projects_mod.project_for_workflow("wf-snap-a")
    reg_b = projects_mod.project_for_workflow("wf-snap-b")

    # Registry 指向各自 Run 的私有 snapshot，而不是项目共享文件
    assert Path(reg_a["workflow_file"]).parent == tmp_path / "workflows" / "wf-snap-a"
    assert Path(reg_a["workflow_file"]).name == "workflow.json"
    assert reg_a["workflow_file"] != reg_b["workflow_file"]
    assert reg_a["workflow_file"] != record_a["workflow_file"]

    cfg_a = projects_mod.workflow_config_for("wf-snap-a")
    cfg_b = projects_mod.workflow_config_for("wf-snap-b")
    assert cfg_a["workflow_template"] == "context-smoke-test"
    assert [n["id"] for n in cfg_a["nodes"]] == ["analyze"]
    assert cfg_b["workflow_template"] == "ctx-beta"
    assert [n["id"] for n in cfg_b["nodes"]] == ["evaluate"]

    # 项目共享 workflow.json 允许停留在 Template B，三者互不冲突
    shared_cfg = json.loads(Path(record_b["workflow_file"]).read_text(encoding="utf-8"))
    assert shared_cfg["workflow_template"] == "ctx-beta"

    # 快照不破坏 workflow 级 shared/ 目录约定（同一父目录共存）
    assert wd.workflow_docs_dir("wf-snap-a") == tmp_path / "workflows" / "wf-snap-a" / "shared"


def test_git_workflow_registration_unchanged_by_snapshot(store_env, tmp_path):
    """git Run（不传 execution）不触发 snapshot：registry workflow_file 语义不变。"""
    from herdr.projects import register_workflow
    project = {
        "project_id": "p-git", "project_name": "n", "project_root": "/r",
        "base_branch": "main", "workspace_id": "w", "coordinator_pane_id": "c",
        "workflow_file": str(tmp_path / "shared-workflow.json"),
    }
    register_workflow("wf-git-snap-01", project, requirement="x")
    from herdr.state_store import get_state_store
    record = get_state_store().get_workflow("wf-git-snap-01")
    assert record["workflow_file"] == project["workflow_file"]


# ---------------------------------------------------------------- Worker: context 模式任务工作区


def _load_worker(clones_dir):
    os.environ["HERDR_CLONES_DIR"] = str(clones_dir)
    worker = load_module(ROOT / "services" / "herdr-worker.py", "herdr_worker_ctx_test")
    del os.environ["HERDR_CLONES_DIR"]
    return worker


class TestWorkerContextMode(unittest.TestCase):
    def test_context_workspace_isolated_no_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            clones = Path(tmp) / "clones"
            worker = _load_worker(clones)
            workspace = worker.create_context_task_workspace("task-ctx-001")
            self.assertTrue(workspace.is_dir())
            self.assertEqual(workspace, clones / "task-ctx-001")
            self.assertFalse((workspace / ".git").exists())

    def test_stale_workspace_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            clones = Path(tmp) / "clones"
            worker = _load_worker(clones)
            first = worker.create_context_task_workspace("task-ctx-002")
            (first / "leftover.txt").write_text("stale", encoding="utf-8")
            with patch.object(worker, "is_task_active_in_registry", return_value=False):
                second = worker.create_context_task_workspace("task-ctx-002")
            self.assertTrue(second.is_dir())
            self.assertFalse((second / "leftover.txt").exists())

    def test_context_task_context_file_records_bindings(self):
        with tempfile.TemporaryDirectory() as tmp:
            clones = Path(tmp) / "clones"
            worker = _load_worker(clones)
            workspace = worker.create_context_task_workspace("task-ctx-003")
            ctx, baseline = worker.write_task_context(
                workspace, "pi", None,
                mode="context",
                context={"company": "/abs/company"},
            )
            text = ctx.read_text(encoding="utf-8")
            self.assertIn("mode=context", text)
            self.assertIn("context.company=/abs/company", text)
            self.assertEqual(baseline, "disabled")


# ---------------------------------------------------------------- herdr-task: verify 指纹与继承


class TestTaskContextFingerprint(unittest.TestCase):
    def test_context_fingerprint_tracks_outputs_and_skips_internal(self):
        task_mod = load_module(ROOT / "bin" / "herdr-task", "herdr_task_ctx_test")
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "报价方案.md").write_text("v1", encoding="utf-8")
            (ws / ".agent-task-context").write_text("mode=context", encoding="utf-8")
            (ws / ".herdr-loop").mkdir()
            (ws / ".herdr-loop" / "GOAL.md").write_text("g", encoding="utf-8")
            (ws / "out" ).mkdir()
            (ws / "out" / "result.txt").write_text("r", encoding="utf-8")

            fp = task_mod.context_workspace_fingerprint(str(ws))
            self.assertEqual(fp["tracked"], {})
            self.assertEqual(
                sorted(fp["untracked"]),
                ["out/result.txt", "报价方案.md"],
            )

    def test_launch_inherits_workflow_context(self):
        """task 记录构建纯函数：workflow 记录 → 任务继承 execution/context。"""
        task_mod = load_module(ROOT / "bin" / "herdr-task", "herdr_task_ctx_test2")
        task = task_mod.execution_fields_from_workflow({
            "workflow_id": "wf-x",
            "execution": {"mode": "context"},
            "context": {"company": "/abs/c"},
        })
        self.assertEqual(task["execution_mode"], "context")
        self.assertEqual(task["context"], {"company": "/abs/c"})
        task_git = task_mod.execution_fields_from_workflow({})
        self.assertEqual(task_git["execution_mode"], "git")
        self.assertNotIn("context", task_git)


# ---------------------------------------------------------------- git 路径防呆守卫


class TestGitPathGuards(unittest.TestCase):
    def test_onto_allowed_for_git_rejected_for_context(self):
        validate_onto_for_execution("git", "agent/pi/feat-x")
        validate_onto_for_execution("context", None)
        with self.assertRaises(ValueError) as cm:
            validate_onto_for_execution("context", "agent/pi/feat-x")
        self.assertIn("--onto", str(cm.exception))

    def test_commit_task_blocks_context_mode(self):
        task_mod = load_module(ROOT / "bin" / "herdr-task", "herdr_task_guards_commit")
        ctx_task = {
            "task_id": "t-ctx-commit",
            "execution_mode": "context",
            "status": "completed",
            "clone_path": "/tmp/does-not-matter",
        }
        with patch.object(task_mod, "load_tasks", return_value={"tasks": [ctx_task]}), \
             patch.object(task_mod, "save_tasks"), \
             patch.object(sys, "stdout", new_callable=io.StringIO) as out:
            with self.assertRaises(SystemExit) as cm:
                task_mod.commit_task("t-ctx-commit")
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("no git commit path", out.getvalue())

    def test_integrate_task_blocks_context_mode(self):
        task_mod = load_module(ROOT / "bin" / "herdr-task", "herdr_task_guards_integrate")
        ctx_task = {
            "task_id": "t-ctx-integrate",
            "execution_mode": "context",
            "status": "committed",
            "clone_path": "/tmp/does-not-matter",
        }
        with patch.object(task_mod, "load_tasks", return_value={"tasks": [ctx_task]}), \
             patch.object(task_mod, "save_tasks"), \
             patch.object(sys, "stdout", new_callable=io.StringIO) as out:
            with self.assertRaises(SystemExit) as cm:
                task_mod.integrate_task("t-ctx-integrate")
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("no git integrate path", out.getvalue())

    def test_worker_rejects_context_onto(self):
        with tempfile.TemporaryDirectory() as tmp:
            clones = Path(tmp) / "clones"
            worker = _load_worker(clones)
            argv = [
                "herdr-worker.py",
                "--task-id", "task-onto-ctx",
                "--source", tmp,
                "--agent", "pi",
                "--execution-mode", "context",
                "--onto", "agent/pi/feat-x",
                "--context-json", "{}",
            ]
            with patch.object(sys, "argv", argv):
                with self.assertRaises(RuntimeError) as cm:
                    worker.main()
            self.assertIn("--onto is not supported", str(cm.exception))
            self.assertFalse((clones / "task-onto-ctx").exists())


if __name__ == "__main__":
    unittest.main()
