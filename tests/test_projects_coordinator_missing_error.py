"""协调器 Pane 丢失时的就地自愈（coordinator pane missing → in-place heal）。

背景：Workspace 会话存活但协调器 Pane 被关闭/崩溃时，旧逻辑直接 raise 并
"Refusing automatic reprovision"，导致启动彻底卡死。修复后应在既有 Workspace
内就地重建/重新挂接协调器 Pane，仅当无法安全恢复时才降级为可操作的报错。
"""
import json
from unittest.mock import patch

import pytest

from herdr import projects
from herdr.projects import ensure_project


def _record(tmp_path):
    return {
        "project_id": "test_proj",
        "project_name": "test",
        "project_root": str(tmp_path),
        "workspace_id": "w15",
        "coordinator_pane_id": "w15:p1",
        "workflow_file": str(tmp_path / "workflow.json"),
    }


def _write_workflow(tmp_path, pane_id="w15:p1", tab_id="w15:t1"):
    cfg = {
        "project_id": "test_proj",
        "workspace_id": "w15",
        "workflow_template": "software-development-v1",
        "coordinator": {"tab_id": tab_id, "label": "1总指挥", "pane_id": pane_id},
        "nodes": [],
    }
    path = tmp_path / "workflow.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return path


def test_coordinator_pane_missing_recreates_tab_and_heals(tmp_path):
    """协调器 Tab 也不见：在既有 Workspace 内新建 1总指挥 Tab 并落盘新 Pane。"""
    record = _record(tmp_path)
    _write_workflow(tmp_path)

    def fake_run_json(cmd):
        if cmd[:3] == ["herdr", "tab", "list"]:
            return {"result": {"tabs": [{"tab_id": "w15:t9", "label": "4实现"}]}}
        if cmd[:3] == ["herdr", "pane", "list"]:
            return {"result": {"panes": [{"pane_id": "w15:p9", "tab_id": "w15:t9"}]}}
        if cmd[:3] == ["herdr", "tab", "create"]:
            return {"result": {"tab": {"tab_id": "w15:tNEW"}, "root_pane": {"pane_id": "w15:pNEW"}}}
        raise AssertionError(f"unexpected _run_json call: {cmd}")

    with patch.object(projects, "project_by_root", return_value=record), \
         patch.object(projects, "_workspace_alive", return_value=True), \
         patch.object(projects, "_pane_alive", return_value=False), \
         patch.object(projects, "_coordinator_alive", return_value=True), \
         patch.object(projects, "_run_json", side_effect=fake_run_json), \
         patch.object(projects, "_run"), \
         patch.object(projects, "load_projects",
                      return_value={"projects": {str(tmp_path): record}}), \
         patch.object(projects, "save_projects") as mock_save:
        result = ensure_project(str(tmp_path))

    # 返回的记录与新落盘 Pane 一致，启动不再抛错。
    assert result["coordinator_pane_id"] == "w15:pNEW"
    assert mock_save.called
    saved = mock_save.call_args.args[0]
    assert saved["projects"][str(tmp_path)]["coordinator_pane_id"] == "w15:pNEW"
    # workflow.json 也回写了新的 coordinator pane/tab。
    cfg = json.loads((tmp_path / "workflow.json").read_text(encoding="utf-8"))
    assert cfg["coordinator"]["pane_id"] == "w15:pNEW"
    assert cfg["coordinator"]["tab_id"] == "w15:tNEW"


def test_coordinator_pane_missing_reattaches_existing_label(tmp_path):
    """协调器 Tab 在、且已有"总指挥" Pane：重新挂接，零副作用创建。"""
    record = _record(tmp_path)
    _write_workflow(tmp_path)

    def fake_run_json(cmd):
        if cmd[:3] == ["herdr", "tab", "list"]:
            return {"result": {"tabs": [{"tab_id": "w15:t1", "label": "1总指挥"}]}}
        if cmd[:3] == ["herdr", "pane", "list"]:
            return {"result": {"panes": [
                {"pane_id": "w15:p1b", "tab_id": "w15:t1", "label": "总指挥"},
            ]}}
        raise AssertionError(f"unexpected _run_json call: {cmd}")

    with patch.object(projects, "project_by_root", return_value=record), \
         patch.object(projects, "_workspace_alive", return_value=True), \
         patch.object(projects, "_pane_alive", return_value=False), \
         patch.object(projects, "_coordinator_alive", return_value=True), \
         patch.object(projects, "_run_json", side_effect=fake_run_json), \
         patch.object(projects, "_run") as mock_run, \
         patch.object(projects, "load_projects",
                      return_value={"projects": {str(tmp_path): record}}), \
         patch.object(projects, "save_projects"):
        result = ensure_project(str(tmp_path))

    assert result["coordinator_pane_id"] == "w15:p1b"
    # 重新挂接路径不得创建 Tab 或 Pane，也不得 rename。
    joined = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]
    assert not any("tab" in j and "create" in j for j in joined)
    assert not any("pane" in j and ("split" in j or "rename" in j) for j in joined)


def test_coordinator_pane_missing_splits_within_coordinator_tab(tmp_path):
    """协调器 Tab 在但无"总指挥" Pane：从该 Tab 内存活 Pane 分裂新协调器 Pane。"""
    record = _record(tmp_path)
    _write_workflow(tmp_path)

    def fake_run_json(cmd):
        if cmd[:3] == ["herdr", "tab", "list"]:
            return {"result": {"tabs": [{"tab_id": "w15:t1", "label": "1总指挥"}]}}
        if cmd[:3] == ["herdr", "pane", "list"]:
            return {"result": {"panes": [
                {"pane_id": "w15:pRoot", "tab_id": "w15:t1", "label": ""},
            ]}}
        if cmd[:3] == ["herdr", "pane", "split"]:
            assert cmd[3] == "w15:pRoot"  # 必须从存活父 Pane 分裂
            return {"result": {"pane": {"pane_id": "w15:pSPLIT"}}}
        raise AssertionError(f"unexpected _run_json call: {cmd}")

    with patch.object(projects, "project_by_root", return_value=record), \
         patch.object(projects, "_workspace_alive", return_value=True), \
         patch.object(projects, "_pane_alive", return_value=False), \
         patch.object(projects, "_coordinator_alive", return_value=True), \
         patch.object(projects, "_run_json", side_effect=fake_run_json), \
         patch.object(projects, "_run"), \
         patch.object(projects, "load_projects",
                      return_value={"projects": {str(tmp_path): record}}), \
         patch.object(projects, "save_projects"):
        result = ensure_project(str(tmp_path))

    assert result["coordinator_pane_id"] == "w15:pSPLIT"


def test_coordinator_pane_missing_unrecoverable_degrades(tmp_path):
    """无法安全恢复（无法创建 Tab）时，降级为可操作报错而非静默卡死。"""
    record = _record(tmp_path)
    _write_workflow(tmp_path)

    def fake_run_json(cmd):
        if cmd[:3] == ["herdr", "tab", "list"]:
            return {"result": {"tabs": []}}
        if cmd[:3] == ["herdr", "pane", "list"]:
            return {"result": {"panes": []}}
        if cmd[:3] == ["herdr", "tab", "create"]:
            return {"result": {}}  # 创建失败：无 tab/root_pane
        raise AssertionError(f"unexpected _run_json call: {cmd}")

    with patch.object(projects, "project_by_root", return_value=record), \
         patch.object(projects, "_workspace_alive", return_value=True), \
         patch.object(projects, "_pane_alive", return_value=False), \
         patch.object(projects, "_run_json", side_effect=fake_run_json), \
         patch.object(projects, "_run"):
        with pytest.raises(RuntimeError) as exc_info:
            ensure_project(str(tmp_path))

    assert "无法在既有工作区内就地恢复" in str(exc_info.value)



def test_ensure_node_runtime_error_messages():
    from herdr.projects import ensure_node_runtime

    with patch("herdr.projects.project_for_workflow", return_value=None), \
         patch("herdr.projects.project_by_root", return_value=None), \
         patch("herdr.projects.load_workflows", return_value={"workflows": {}}):
        with pytest.raises(RuntimeError) as exc_info:
            ensure_node_runtime("wf-nonexistent", "node1")
        assert "未找到工作流配置: wf-nonexistent" in str(exc_info.value)

    mock_wf = {
        "workspace_id": "ws-dead",
        "nodes": [{"id": "node-a"}],
    }
    with patch("herdr.projects.project_for_workflow", return_value={"project_id": "p1"}), \
         patch("herdr.projects.workflow_config_for", return_value=mock_wf), \
         patch("herdr.projects._workspace_alive", return_value=False):
        with pytest.raises(RuntimeError) as exc_info:
            ensure_node_runtime("wf-1", "node-a")
        assert "Herdr 工作区会话不存在或已退出: ws-dead" in str(exc_info.value)

    with patch("herdr.projects.project_for_workflow", return_value={"project_id": "p1"}), \
         patch("herdr.projects.workflow_config_for", return_value=mock_wf), \
         patch("herdr.projects._workspace_alive", return_value=True):
        with pytest.raises(RuntimeError) as exc_info:
            ensure_node_runtime("wf-1", "node-nonexistent")
        assert "在工作流定义中未找到节点 'node-nonexistent'。" in str(exc_info.value)

