"""Workflow deliverable projection and safe file access."""

import os
from pathlib import Path

import pytest

from herdr import deliverables as d


NODES = [
    {"id": "requirements", "label": "需求整理", "required_outputs": ["需求清单.md"]},
    {"id": "proposal", "label": "方案编写", "required_outputs": ["解决方案.md"]},
]


def _workspace(tmp_path, task_id):
    root = tmp_path / "clones"
    root.mkdir()
    workspace = root / task_id
    workspace.mkdir()
    return root, workspace


def _task(task_id, workspace, node, agent="claude", status="working", **extra):
    row = {
        "task_id": task_id,
        "workflow_id": "wf-biz",
        "node": node,
        "agent": agent,
        "status": status,
        "clone_path": str(workspace),
    }
    row.update(extra)
    return row


def test_context_workspace_files_roll_up_without_claiming_verification(tmp_path, monkeypatch):
    root, ws1 = _workspace(tmp_path, "task-req")
    ws2 = root / "task-proposal"
    ws2.mkdir()
    monkeypatch.setenv("HERDR_CLONES_DIR", str(root))
    (ws1 / "需求清单.md").write_text("# 需求\n\n客户要报价。\n", encoding="utf-8")
    (ws2 / "解决方案.md").write_text("# 方案\n\n两周交付。\n", encoding="utf-8")
    tasks = [
        _task("task-req", ws1, "requirements", agent="claude", status="working"),
        _task("task-proposal", ws2, "proposal", agent="codex", status="working"),
    ]

    packed = d.project_for_tasks("wf-biz", tasks, NODES)
    by_name = {item["name"]: item for item in packed["items"]}

    assert set(by_name) >= {"需求清单.md", "解决方案.md"}
    req = by_name["需求清单.md"]
    assert req["status"] == "present"
    assert req["status_label"] == "已产生"
    assert req["status"] != "verified"
    assert req["node_id"] == "requirements"
    assert req["node_label"] == "需求整理"
    assert req["agent"] == "claude"
    assert req["type_label"] == "文档"
    assert "view" in req["actions"]
    assert req["task_id"] == "task-req"
    assert "clone_path" not in req
    assert "path" not in req
    assert packed["counts"]["present"] == 2
    assert packed["counts"]["verified"] == 0

    preview = d.read_deliverable(
        "wf-biz", tasks, NODES, "task-req", req["id"], download=False,
    )
    assert "客户要报价" in preview["text"]
    assert preview["truncated"] is False


def test_missing_expected_output_stays_missing_until_the_file_exists(tmp_path, monkeypatch):
    root, ws1 = _workspace(tmp_path, "task-req")
    monkeypatch.setenv("HERDR_CLONES_DIR", str(root))
    tasks = [_task("task-req", ws1, "requirements")]

    packed = d.project_for_tasks("wf-biz", tasks, NODES)
    missing = packed["items"][0]
    assert missing["name"] == "需求清单.md"
    assert missing["status"] == "missing"
    assert missing["status_label"] == "未产生"
    assert missing["actions"] == []
    assert missing["readable"] is False


def test_same_file_identity_is_stable_and_deduped(tmp_path, monkeypatch):
    root, ws1 = _workspace(tmp_path, "task-req")
    monkeypatch.setenv("HERDR_CLONES_DIR", str(root))
    (ws1 / "需求清单.md").write_text("一次\n", encoding="utf-8")
    task = _task("task-req", ws1, "requirements")
    first = d.project_for_tasks("wf-biz", [task, task], NODES)
    second = d.project_for_tasks("wf-biz", [task], NODES)
    names = [item["name"] for item in first["items"]]
    assert names.count("需求清单.md") == 1
    assert first["items"][0]["id"] == second["items"][0]["id"]


def test_superseded_task_file_is_not_readable(tmp_path, monkeypatch):
    root, ws1 = _workspace(tmp_path, "task-old")
    monkeypatch.setenv("HERDR_CLONES_DIR", str(root))
    (ws1 / "需求清单.md").write_text("旧稿\n", encoding="utf-8")
    task = _task("task-old", ws1, "requirements", status="superseded")
    packed = d.project_for_tasks("wf-biz", [task], NODES)
    item = next(row for row in packed["items"] if row["name"] == "需求清单.md")
    assert item["status"] == "unavailable"
    assert item["readable"] is False
    with pytest.raises(d.DeliverableError) as caught:
        d.read_deliverable("wf-biz", [task], NODES, "task-old", item["id"])
    assert caught.value.status == 409
    assert "旧稿" not in caught.value.message


def test_git_commit_pr_and_diff_are_not_presented_as_downloads(tmp_path, monkeypatch):
    root, workspace = _workspace(tmp_path, "task-dev")
    monkeypatch.setenv("HERDR_CLONES_DIR", str(root))
    os.system(f"git init -q '{workspace}'")
    (workspace / "docs").mkdir()
    (workspace / "docs" / "设计说明.md").write_text("# 设计\n", encoding="utf-8")
    task = _task(
        "task-dev",
        workspace,
        "implementation",
        agent="codex",
        candidate_sha="abc1234",
        pr_url="https://github.com/example/repo/pull/9",
        branch="feat/demo",
        workflow_id="wf-dev",
    )
    nodes = [{"id": "implementation", "label": "开发实现", "required_outputs": []}]
    packed = d.project_for_tasks("wf-dev", [task], nodes)
    kinds = {item["kind"]: item for item in packed["items"]}
    assert kinds["git_commit"]["sha"] == "abc1234"
    assert kinds["git_commit"]["actions"] == []
    assert kinds["git_pr"]["url"] == "https://github.com/example/repo/pull/9"
    assert kinds["git_pr"]["actions"] == ["open"]
    doc = next(item for item in packed["items"] if item["name"] == "设计说明.md")
    assert "view" in doc["actions"]
    assert doc["kind"] == "document"


def test_rejects_other_workflow_missing_file_traversal_symlink_and_oversize(tmp_path, monkeypatch):
    root, ws1 = _workspace(tmp_path, "task-req")
    other = root / "task-other"
    other.mkdir()
    monkeypatch.setenv("HERDR_CLONES_DIR", str(root))
    (ws1 / "需求清单.md").write_text("本人的文件\n", encoding="utf-8")
    (other / "需求清单.md").write_text("别人的文件\n", encoding="utf-8")
    secret = tmp_path / "outside.txt"
    secret.write_text("outside-secret", encoding="utf-8")
    (ws1 / "leak.md").symlink_to(secret)
    huge = ws1 / "巨大说明.md"
    huge.write_bytes(b"x" * (d.DOWNLOAD_LIMIT + 1))
    tasks = [_task("task-req", ws1, "requirements")]
    other_tasks = [_task("task-other", other, "requirements", workflow_id="wf-other")]
    packed = d.project_for_tasks("wf-biz", tasks, NODES)
    own = next(item for item in packed["items"] if item["name"] == "需求清单.md")

    ok = d.read_deliverable("wf-biz", tasks, NODES, "task-req", own["id"])
    assert "本人的文件" in ok["text"]

    with pytest.raises(d.DeliverableError) as missing:
        d.read_deliverable("wf-biz", tasks, NODES, "task-req", "does-not-exist")
    assert missing.value.status == 404

    with pytest.raises(d.DeliverableError) as foreign:
        d.read_deliverable("wf-biz", other_tasks, NODES, "task-other", own["id"])
    assert foreign.value.status == 404
    assert "别人的文件" not in foreign.value.message

    with pytest.raises(d.DeliverableError) as escaped:
        d.resolve_workspace_file(ws1, "../outside.txt")
    assert escaped.value.status == 403

    with pytest.raises(d.DeliverableError) as linked:
        d.resolve_workspace_file(ws1, "leak.md")
    assert linked.value.status == 403
    assert "outside-secret" not in linked.value.message

    huge_item = next(item for item in packed["items"] if item["name"] == "巨大说明.md")
    with pytest.raises(d.DeliverableError) as oversized:
        d.read_deliverable("wf-biz", tasks, NODES, "task-req", huge_item["id"])
    assert oversized.value.status == 413


def test_markdown_html_is_escaped_and_long_text_is_truncated(tmp_path, monkeypatch):
    root, ws1 = _workspace(tmp_path, "task-req")
    monkeypatch.setenv("HERDR_CLONES_DIR", str(root))
    payload = "<script>alert(1)</script>\n\n" + ("段落\n\n" * 40000)
    (ws1 / "需求清单.md").write_text(payload, encoding="utf-8")
    tasks = [_task("task-req", ws1, "requirements")]
    packed = d.project_for_tasks("wf-biz", tasks, NODES)
    item = next(row for row in packed["items"] if row["name"] == "需求清单.md")
    preview = d.read_deliverable("wf-biz", tasks, NODES, "task-req", item["id"])
    assert "<script>" not in preview["html"]
    assert "&lt;script&gt;" in preview["html"]
    assert preview["truncated"] is True
    assert len(preview["text"].encode("utf-8")) <= d.PREVIEW_LIMIT


def test_unmanaged_path_and_secret_names_are_not_served(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_CLONES_DIR", str(tmp_path / "clones"))
    outside = tmp_path / "home-project"
    outside.mkdir()
    (outside / "需求清单.md").write_text("不应被扫到\n", encoding="utf-8")
    task = _task("task-req", outside, "requirements")
    packed = d.project_for_tasks("wf-biz", [task], NODES)
    hidden = [item for item in packed["items"] if item["name"] == "需求清单.md"]
    assert hidden and hidden[0]["status"] == "unavailable"
    assert "尚未生成" not in hidden[0]["reason"]
    assert packed.get("error")
    assert not any(item.get("readable") for item in packed["items"])

    root, ws1 = _workspace(tmp_path, "task-safe")
    monkeypatch.setenv("HERDR_CLONES_DIR", str(root))
    (ws1 / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (ws1 / "notes.txt").write_text("可以看\n", encoding="utf-8")
    safe = _task("task-safe", ws1, "requirements")
    visible = d.project_for_tasks("wf-biz", [safe], NODES)
    assert all(item["name"] != ".env" for item in visible["items"])
    with pytest.raises(d.DeliverableError):
        d.resolve_workspace_file(ws1, ".env")


def test_download_headers_are_attachment_and_nosniff():
    headers = d.download_headers("报价明细.xlsx")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in headers["Content-Security-Policy"]
    assert headers["Content-Disposition"].startswith("attachment;")
    assert "text/html" not in headers["Content-Type"]


def test_console_read_uses_the_same_catalog(tmp_path, monkeypatch):
    from console import herdr_factory_console as console

    root, workspace = _workspace(tmp_path, "task-req")
    monkeypatch.setenv("HERDR_CLONES_DIR", str(root))
    (workspace / "需求清单.md").write_text("客户要报价。\n", encoding="utf-8")
    task = _task("task-req", workspace, "requirements")
    monkeypatch.setattr(console, "workflows", lambda: {
        "wf-biz": {"workflow_id": "wf-biz", "config": {"nodes": NODES}},
    })
    monkeypatch.setattr(console, "tasks_for_workflow", lambda wid: [task] if wid == "wf-biz" else [])

    packed = console.api_workflow_deliverables("wf-biz")
    item = next(row for row in packed["items"] if row["name"] == "需求清单.md")
    preview = console.api_read_deliverable("wf-biz", "task-req", item["id"])
    assert "客户要报价" in preview["text"]
    downloaded = console.api_read_deliverable("wf-biz", "task-req", item["id"], download=True)
    assert downloaded["body"].startswith("客户要报价".encode())
    assert "text/html" not in downloaded["headers"]["Content-Type"]

    with pytest.raises(d.DeliverableError) as foreign:
        console.api_read_deliverable("wf-other", "task-req", item["id"])
    assert foreign.value.status == 404


def test_console_uses_deliverable_section_and_same_api():
    source = Path(__file__).resolve().parents[1].joinpath(
        "console", "herdr_factory_console.py"
    ).read_text(encoding="utf-8")
    assert 'id="deliverablesPanel"' in source
    assert "交付成果" in source
    assert "/api/workflow/deliverable" in source
    assert "renderTaskArtifacts" in source
    assert "本节点成果" in source
    assert "toast(" in source.split("async function openDeliverable", 1)[1].split("async function", 1)[0]
    assert "成果目录读取失败" in source


def test_prose_contract_labels_are_not_missing_files(tmp_path, monkeypatch):
    root, workspace = _workspace(tmp_path, "task-req")
    monkeypatch.setenv("HERDR_CLONES_DIR", str(root))
    nodes = [{
        "id": "requirements",
        "label": "需求整理",
        "required_outputs": ["测试结论（PASS / FAIL）", "招标基本信息表", "需求清单.md"],
    }]
    packed = d.project_for_tasks("wf-biz", [_task("task-req", workspace, "requirements")], nodes)
    names = [item["name"] for item in packed["items"]]
    assert names == ["需求清单.md"]
    assert packed["items"][0]["status"] == "missing"
    assert "FAIL" not in names[0]


def test_git_commit_file_is_listed_without_claiming_older_docs(tmp_path, monkeypatch):
    import subprocess

    root, workspace = _workspace(tmp_path, "task-dev")
    monkeypatch.setenv("HERDR_CLONES_DIR", str(root))
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "core.quotepath", "true"], cwd=workspace, check=True)
    (workspace / "docs").mkdir()
    (workspace / "docs" / "旧说明.md").write_text("旧文档\n", encoding="utf-8")
    subprocess.run(["git", "add", "docs/旧说明.md"], cwd=workspace, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "old"], cwd=workspace, check=True, env=env)
    (workspace / "报价.md").write_text("# 报价\n", encoding="utf-8")
    (workspace / "客户需求.md").write_text("未提交\n", encoding="utf-8")
    subprocess.run(["git", "add", "报价.md"], cwd=workspace, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "quote"], cwd=workspace, check=True, env=env)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace, text=True, capture_output=True, check=True,
    ).stdout.strip()
    task = _task("task-dev", workspace, "implementation", candidate_sha=sha, workflow_id="wf-dev")
    nodes = [{
        "id": "implementation",
        "label": "开发实现",
        "required_outputs": ["测试结论（PASS / FAIL）", "报价.md"],
    }]
    packed = d.project_for_tasks("wf-dev", [task], nodes)
    names = {item["name"] for item in packed["items"]}
    assert "报价.md" in names
    assert "客户需求.md" in names
    assert "旧说明.md" not in names
    assert all("\\" not in item["name"] and "FAIL" not in item["name"] for item in packed["items"])
    quote = next(item for item in packed["items"] if item["name"] == "报价.md")
    assert quote["status"] == "present"
    preview = d.read_deliverable("wf-dev", [task], nodes, "task-dev", quote["id"])
    assert "报价" in preview["text"]


def test_git_status_failure_does_not_hide_an_existing_file(tmp_path, monkeypatch):
    import subprocess

    root, workspace = _workspace(tmp_path, "task-req")
    monkeypatch.setenv("HERDR_CLONES_DIR", str(root))
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    (workspace / "需求清单.md").write_text("在\n", encoding="utf-8")
    monkeypatch.setattr(
        d, "_changed_files",
        lambda workspace: ([], "无法列出 Git 变更，未列出的文件不代表尚未生成。"),
    )
    packed = d.project_for_tasks("wf-biz", [_task("task-req", workspace, "requirements")], NODES)
    assert "无法列出" in packed["error"]
    item = next(row for row in packed["items"] if row["name"] == "需求清单.md")
    assert item["status"] == "present"
    assert item["reason"] != "尚未生成"


def test_task_projection_scan_failure_is_not_an_empty_success(monkeypatch):
    from console import herdr_factory_console as console

    def boom(*_args, **_kwargs):
        raise RuntimeError("scan broke")

    monkeypatch.setattr(
        console.herdr_projection, "project_task",
        lambda tid: {"task_id": tid, "workflow_id": "wf-biz"},
    )
    monkeypatch.setattr(console.herdr_deliverables, "project_for_tasks", boom)
    proj = console.api_task_projection("task-req")
    assert proj["deliverables"]["items"] == []
    assert proj["deliverables"]["error"] == "成果目录读取失败"


def test_baseline_commit_is_not_listed_as_this_task_delivery(tmp_path, monkeypatch):
    import subprocess

    root, workspace = _workspace(tmp_path, "task-dev")
    monkeypatch.setenv("HERDR_CLONES_DIR", str(root))
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    (workspace / "docs").mkdir()
    (workspace / "docs" / "旧说明.md").write_text("旧文档\n", encoding="utf-8")
    subprocess.run(["git", "add", "docs/旧说明.md"], cwd=workspace, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=workspace, check=True, env=env)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace, text=True, capture_output=True, check=True,
    ).stdout.strip()
    (workspace / "报价.md").write_text("# 报价\n", encoding="utf-8")
    subprocess.run(["git", "add", "报价.md"], cwd=workspace, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "quote"], cwd=workspace, check=True, env=env)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace, text=True, capture_output=True, check=True,
    ).stdout.strip()
    task = _task(
        "task-dev", workspace, "implementation",
        candidate_sha=base, baseline_commit=base, workflow_id="wf-dev",
    )
    nodes = [{"id": "implementation", "label": "开发实现", "required_outputs": []}]
    packed = d.project_for_tasks("wf-dev", [task], nodes)
    names = {item["name"] for item in packed["items"]}
    assert "报价.md" in names
    assert "旧说明.md" not in names
    commit = next(item for item in packed["items"] if item["kind"] == "git_commit")
    assert commit["sha"] == head


def test_task_projection_keeps_soft_catalog_error(monkeypatch):
    from console import herdr_factory_console as console

    monkeypatch.setattr(
        console.herdr_projection, "project_task",
        lambda tid: {"task_id": tid, "workflow_id": "wf-biz"},
    )
    monkeypatch.setattr(console, "tasks_for_workflow", lambda wid: [{
        "task_id": "task-req", "workflow_id": wid,
    }])
    monkeypatch.setattr(console, "workflows", lambda: {"wf-biz": {}})
    monkeypatch.setattr(
        console.herdr_deliverables, "project_for_tasks",
        lambda *args, **kwargs: d.pack([], error="无法列出 Git 变更，未列出的文件不代表尚未生成。"),
    )
    proj = console.api_task_projection("task-req")
    assert proj["deliverables"]["items"] == []
    assert "无法列出" in proj["deliverables"]["error"]
