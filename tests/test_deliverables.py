"""Workflow deliverable projection and safe file access."""

import os
from pathlib import Path

import pytest

from herdr import deliverables as d


NODES = [
    {"id": "requirements", "label": "需求整理", "required_outputs": ["需求清单.md"]},
    {"id": "proposal", "label": "方案编写", "required_outputs": ["解决方案.md"]},
]


def test_task_projection_reads_git_head_once(tmp_path, monkeypatch):
    import subprocess

    root, workspace = _workspace(tmp_path, "task-dev")
    monkeypatch.setenv("HERDR_CLONES_DIR", str(root))
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=workspace, text=True).strip()
    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (workspace / "baseline.md").write_text("baseline")
    git("add", ".")
    git("commit", "-qm", "baseline")
    baseline = git("rev-parse", "HEAD")
    (workspace / "delivery.md").write_text("delivery")
    git("add", ".")
    git("commit", "-qm", "delivery")
    delivery = git("rev-parse", "HEAD")
    original = d._head_sha
    reads = []
    def head(path):
        reads.append(path)
        return original(path)
    monkeypatch.setattr(d, "_head_sha", head)
    packed = d.project_for_tasks("wf-biz", [_task("task-dev", workspace, "dev", baseline_commit=baseline)], [])
    assert any(item["name"] == "delivery.md" for item in packed["items"])
    assert any(item.get("sha") == delivery for item in packed["items"])
    assert reads == [workspace]


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


@pytest.mark.parametrize("status,successor", [("superseded", None), ("completed", "task-next")])
def test_superseded_task_file_is_readable_as_history(tmp_path, monkeypatch, status, successor):
    root, ws1 = _workspace(tmp_path, "task-old")
    monkeypatch.setenv("HERDR_CLONES_DIR", str(root))
    content = "旧稿\n"
    (ws1 / "需求清单.md").write_text(content, encoding="utf-8")
    task = _task("task-old", ws1, "requirements", status=status, superseded_by=successor)
    packed = d.project_for_tasks("wf-biz", [task], NODES)
    item = next(row for row in packed["items"] if row["name"] == "需求清单.md")
    assert item["status"] == "historical"
    assert "历史" in item["status_label"]
    assert item["readable"] is True
    assert item["actions"] == ["view", "download"]
    assert packed["counts"]["present"] == 0
    assert packed["counts"]["historical"] == 1
    assert d.read_deliverable("wf-biz", [task], NODES, "task-old", item["id"])["text"] == content
    downloaded = d.read_deliverable("wf-biz", [task], NODES, "task-old", item["id"], download=True)
    assert downloaded["body"] == content.encode()
    assert "attachment" in downloaded["headers"]["Content-Disposition"]
    with pytest.raises(d.DeliverableError):
        d.read_deliverable("other-workflow", [task], NODES, "task-old", item["id"])
    (ws1 / "需求清单.md").unlink()
    with pytest.raises(d.DeliverableError):
        d.read_deliverable("wf-biz", [task], NODES, "task-old", item["id"])


def test_superseded_file_does_not_satisfy_the_node_expectation(tmp_path, monkeypatch):
    root, old = _workspace(tmp_path, "task-old")
    current = root / "task-now"
    current.mkdir()
    monkeypatch.setenv("HERDR_CLONES_DIR", str(root))
    (old / "requirements.md").write_text("旧稿\n", encoding="utf-8")
    nodes = [{"id": "requirements", "label": "需求分析", "required_outputs": ["requirements.md"]}]
    old_task = _task("task-old", old, "requirements", status="superseded", superseded_by="task-now")
    now_task = _task("task-now", current, "requirements", agent="codex")

    packed = d.project_for_tasks("wf-biz", [old_task, now_task], nodes)
    history = next(item for item in packed["items"] if item["task_id"] == "task-old")
    assert history["status"] == "historical"
    assert history["readable"] is True
    gaps = [item for item in packed["items"] if item["task_id"] == ""]
    assert [item["status"] for item in gaps] == ["missing"]
    assert gaps[0]["name"] == "requirements.md"
    assert gaps[0]["status_label"] == "未产生"
    assert packed["counts"]["present"] == 0

    (current / "requirements.md").write_text("新稿\n", encoding="utf-8")
    done = d.project_for_tasks("wf-biz", [old_task, now_task], nodes)
    assert any(item["task_id"] == "task-now" and item["status"] == "present" for item in done["items"])
    assert any(item["task_id"] == "task-old" and item["status"] == "historical" for item in done["items"])
    assert not any(item["status"] == "missing" for item in done["items"])

    alone = d.project_for_tasks("wf-biz", [old_task], nodes)
    assert any(item["task_id"] == "task-old" and item["status"] == "historical" for item in alone["items"])
    unknown = [item for item in alone["items"] if item["task_id"] == ""]
    assert [item["status"] for item in unknown] == ["unconfirmed"]
    assert unknown[0]["status_label"] == "不可确认"
    assert alone["counts"]["present"] == 0
    assert alone["counts"]["missing"] == 0
    assert alone["counts"]["unconfirmed"] == 1


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


def _seed_console_workflow(tmp_path, monkeypatch, tasks, nodes):
    from herdr import state_db
    db = tmp_path / 'state.db'
    monkeypatch.setenv('HERDR_STATE_DB', str(db))
    state_db.save_workflow({'workflow_id': 'wf-biz', 'config': {'nodes': nodes}}, db_path=db)
    for task in tasks:
        state_db.save_task(task, db_path=db)
    return db


def test_console_read_uses_the_same_catalog(tmp_path, monkeypatch):
    from console import herdr_factory_console as console

    root, workspace = _workspace(tmp_path, "task-req")
    monkeypatch.setenv("HERDR_CLONES_DIR", str(root))
    (workspace / "需求清单.md").write_text("客户要报价。\n", encoding="utf-8")
    task = _task("task-req", workspace, "requirements")
    _seed_console_workflow(tmp_path, monkeypatch, [task], NODES)

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
    assert "工作区文件" in source
    assert "packed.notice" in source
    assert "include_expectations=False" in source


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


def test_context_inputs_are_workspace_files_until_registered(tmp_path, monkeypatch):
    root, workspace = _workspace(tmp_path, "task-quote")
    monkeypatch.setenv("HERDR_CLONES_DIR", str(root))
    (workspace / "需求清单.md").write_text("# 需求\n", encoding="utf-8")
    (workspace / "客户原始资料.pdf").write_bytes(b"%PDF-input")
    (workspace / "正式报价单.xlsx").write_bytes(b"quote-bytes")
    task = _task(
        "task-quote", workspace, "requirements",
        observations=[{
            "source_type": "artifact",
            "metadata": {"artifact_path": str(workspace / "正式报价单.xlsx")},
        }],
    )
    packed = d.project_for_tasks("wf-biz", [task], NODES)
    by_name = {item["name"]: item for item in packed["items"]}

    assert by_name["需求清单.md"]["status"] == "present"
    assert by_name["需求清单.md"]["status_label"] == "已产生"
    assert by_name["正式报价单.xlsx"]["status"] == "present"
    assert by_name["客户原始资料.pdf"]["status"] == "workspace"
    assert by_name["客户原始资料.pdf"]["status_label"] == "工作区文件"
    assert by_name["客户原始资料.pdf"]["readable"] is True
    assert packed["counts"]["present"] == 2
    assert packed["counts"]["workspace"] == 1
    downloaded = d.read_deliverable(
        "wf-biz", [task], NODES, "task-quote", by_name["客户原始资料.pdf"]["id"], download=True,
    )
    assert downloaded["body"] == b"%PDF-input"


def test_node_expected_outputs_are_not_required_of_every_task(tmp_path, monkeypatch):
    root, first = _workspace(tmp_path, "task-a")
    second = root / "task-b"
    second.mkdir()
    monkeypatch.setenv("HERDR_CLONES_DIR", str(root))
    (first / "requirements.md").write_text("规格\n", encoding="utf-8")
    (second / "review.md").write_text("审查\n", encoding="utf-8")
    nodes = [{
        "id": "requirements",
        "label": "需求整理",
        "required_outputs": ["requirements.md", "review.md"],
    }]
    tasks = [
        _task("task-a", first, "requirements"),
        _task("task-b", second, "requirements", agent="codex"),
    ]
    packed = d.project_for_tasks("wf-biz", tasks, nodes)
    missing = [item for item in packed["items"] if item["status"] == "missing"]
    assert missing == []
    by_task = {}
    for item in packed["items"]:
        by_task.setdefault(item["task_id"], set()).add(item["name"])
    assert by_task["task-a"] == {"requirements.md"}
    assert by_task["task-b"] == {"review.md"}

    own = d.project_for_tasks("wf-biz", [tasks[0]], nodes, include_expectations=False)
    assert [item["name"] for item in own["items"]] == ["requirements.md"]
    assert all(item["status"] != "missing" for item in own["items"])

    (second / "review.md").unlink()
    partial = d.project_for_tasks("wf-biz", tasks, nodes)
    gaps = [item for item in partial["items"] if item["status"] == "missing"]
    assert [item["name"] for item in gaps] == ["review.md"]
    assert gaps[0]["task_id"] == ""
    assert gaps[0]["node_id"] == "requirements"
    assert all(item["task_id"] != "task-a" or item["status"] != "missing" for item in partial["items"])


def test_scan_cap_marks_the_catalog_incomplete(tmp_path, monkeypatch):
    root, workspace = _workspace(tmp_path, "task-req")
    monkeypatch.setenv("HERDR_CLONES_DIR", str(root))
    (workspace / "需求清单.md").write_text("正式\n", encoding="utf-8")
    for index in range(d.MAX_FILES + 1):
        (workspace / f"资料{index:02d}.txt").write_text("参考\n", encoding="utf-8")
    packed = d.project_for_tasks("wf-biz", [_task("task-req", workspace, "requirements")], NODES)
    names = [item["name"] for item in packed["items"]]
    assert "需求清单.md" in names
    assert next(item for item in packed["items"] if item["name"] == "需求清单.md")["status"] == "present"
    assert len(packed["items"]) <= d.MAX_FILES
    assert packed["truncated"] is True
    assert packed["notice"] == "成果列表可能未完整。"
    assert len(names) < (d.MAX_FILES + 2)


def test_console_task_view_omits_sibling_expectations(tmp_path, monkeypatch):
    from console import herdr_factory_console as console

    root, first = _workspace(tmp_path, "task-a")
    second = root / "task-b"
    second.mkdir()
    monkeypatch.setenv("HERDR_CLONES_DIR", str(root))
    (first / "requirements.md").write_text("规格\n", encoding="utf-8")
    (first / "客户原始资料.pdf").write_bytes(b"%PDF")
    (second / "review.md").write_text("审查\n", encoding="utf-8")
    nodes = [{
        "id": "requirements",
        "label": "需求整理",
        "required_outputs": ["requirements.md", "review.md"],
    }]
    tasks = [
        _task("task-a", first, "requirements"),
        _task("task-b", second, "requirements", agent="codex"),
    ]
    monkeypatch.setattr(
        console.herdr_projection, "project_task",
        lambda tid: {"task_id": tid, "workflow_id": "wf-biz"},
    )
    _seed_console_workflow(tmp_path, monkeypatch, tasks, nodes)
    console._deliverable_observation_loader = lambda task_id: (
        [{"metadata": {"artifact_path": str(first / "客户原始资料.pdf")}}]
        if task_id == "task-a" else []
    )
    try:
        workflow = console.api_workflow_deliverables("wf-biz")
        own = console.api_task_projection("task-a")
    finally:
        console._deliverable_observation_loader = None

    workflow_names = {item["name"]: item["status"] for item in workflow["items"]}
    assert workflow_names["requirements.md"] == "present"
    assert workflow_names["review.md"] == "present"
    assert workflow_names["客户原始资料.pdf"] == "present"
    assert "missing" not in workflow_names.values()
    assert [item["name"] for item in own["deliverables"]["items"]] == ["requirements.md", "客户原始资料.pdf"]
    assert all(item["status"] != "missing" for item in own["deliverables"]["items"])


def test_task_projection_keeps_soft_catalog_error(tmp_path, monkeypatch):
    from console import herdr_factory_console as console

    monkeypatch.setattr(
        console.herdr_projection, "project_task",
        lambda tid: {"task_id": tid, "workflow_id": "wf-biz"},
    )
    _seed_console_workflow(tmp_path, monkeypatch, [{
        "task_id": "task-req", "workflow_id": "wf-biz", "node": "requirements",
    }], [{"id": "requirements"}])
    def forbid_global(*args, **kwargs):
        raise AssertionError("task catalog must use its readonly workflow snapshot")
    monkeypatch.setattr(console, "tasks_for_workflow", forbid_global)
    monkeypatch.setattr(console, "workflows", forbid_global)
    monkeypatch.setattr(
        console.herdr_deliverables, "project_for_tasks",
        lambda *args, **kwargs: d.pack([], error="无法列出 Git 变更，未列出的文件不代表尚未生成。"),
    )
    proj = console.api_task_projection("task-req")
    assert proj["deliverables"]["items"] == []
    assert "无法列出" in proj["deliverables"]["error"]


def test_console_projects_persisted_pr_receipt_for_cleaned_task(tmp_path, monkeypatch):
    """A PR created through the platform must remain visible after clone cleanup."""
    from console import herdr_factory_console as console
    from herdr import state_db

    db = tmp_path / 'state.db'
    monkeypatch.setenv('HERDR_STATE_DB', str(db))
    monkeypatch.setattr(console, '_deliverable_pr_loader', console._load_deliverable_pr)
    task = {'task_id': 'impl', 'workflow_id': 'wf-biz', 'node': 'impl', 'status': 'cleaned'}
    _seed_console_workflow(tmp_path, monkeypatch, [task], [])
    state_db.record_event({'workflow_id': 'wf-biz', 'task_id': 'impl',
                          'event_type': 'pull_request_created', 'source': 'herdr-task',
                          'payload': {'url': 'https://gitee.com/example/repo/pulls/1611',
                                      'number': 1611, 'candidate_sha': 'a' * 40}}, db_path=db)
    packed = console.api_workflow_deliverables('wf-biz')
    prs = [item for item in packed['items'] if item['kind'] == 'git_pr']
    assert len(prs) == 1
    assert prs[0]['url'] == 'https://gitee.com/example/repo/pulls/1611'
    assert prs[0]['task_id'] == 'impl'
    assert prs[0]['status'] == 'present'
    assert prs[0]['actions'] == ['open']
    assert 'pull_request' not in task
    assert 'pull_request' not in state_db.get_task('impl', db_path=db)


@pytest.mark.parametrize('workflow,task_id,event_type,source', [
    ('wf-other', 'impl', 'pull_request_created', 'herdr-task'),
    ('wf-biz', 'other', 'pull_request_created', 'herdr-task'),
    ('wf-biz', 'impl', 'pull_request_intent', 'herdr-task'),
    ('wf-biz', 'impl', 'pull_request_created', 'agent'),
])
def test_console_excludes_unowned_or_unconfirmed_pr_events(tmp_path, monkeypatch, workflow, task_id, event_type, source):
    from console import herdr_factory_console as console
    from herdr import state_db

    db = tmp_path / 'state.db'
    monkeypatch.setenv('HERDR_STATE_DB', str(db))
    monkeypatch.setattr(console, '_deliverable_pr_loader', console._load_deliverable_pr)
    task = {'task_id': 'impl', 'workflow_id': 'wf-biz', 'node': 'impl', 'status': 'cleaned'}
    _seed_console_workflow(tmp_path, monkeypatch, [task], [])
    state_db.record_event({'workflow_id': workflow, 'task_id': task_id,
                          'event_type': event_type, 'source': source,
                          'payload': {'url': 'https://gitee.com/example/repo/pulls/999'}}, db_path=db)
    packed = console.api_workflow_deliverables('wf-biz')
    assert not [item for item in packed['items'] if item['kind'] == 'git_pr']


def test_workspace_probes_are_parallel_bounded_and_keep_task_order(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    lock = threading.Lock()
    ready, release = threading.Event(), threading.Event()
    active = peak = 0
    tasks = [{'workflow_id': 'wf', 'task_id': str(i)} for i in range(8)]
    def probe(task, nodes):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 4:
                ready.set()
        assert release.wait(5), 'probe release was not signalled'
        with lock:
            active -= 1
        tid = task['task_id']
        return ([{'id': tid, 'task_id': tid, 'status': 'present'}],
                'first error' if tid == '1' else '', tid == '2')
    monkeypatch.setattr(d, '_task_items', probe)
    with ThreadPoolExecutor(max_workers=2) as callers:
        results = [callers.submit(d.project_for_tasks, 'wf', tasks, [], include_expectations=False)
                   for _ in range(2)]
        try:
            assert ready.wait(2), 'serial probes delay the complete catalog'
        finally:
            release.set()
        for result in results:
            packed = result.result(timeout=5)
            assert [item['task_id'] for item in packed['items']] == [str(i) for i in range(8)]
            assert packed['error'] == 'first error'
            assert packed['truncated'] is True
    assert peak == 4, 'probe concurrency must be capped across requests in this process'


def test_completed_probe_releases_capacity_before_slow_peer_finishes(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    release, next_started = threading.Event(), threading.Event()
    tasks = [{'workflow_id': 'wf', 'task_id': str(i)} for i in range(5)]
    def probe(task, nodes):
        tid = task['task_id']
        if tid == '0':
            assert release.wait(5)
        if tid == '4':
            next_started.set()
        return ([{'id': tid, 'task_id': tid, 'status': 'present'}], '', False)
    monkeypatch.setattr(d, '_task_items', probe)
    with ThreadPoolExecutor(max_workers=1) as callers:
        result = callers.submit(d.project_for_tasks, 'wf', tasks, [], include_expectations=False)
        try:
            assert next_started.wait(2), 'completed probes must free capacity without a batch barrier'
        finally:
            release.set()
        assert [item['task_id'] for item in result.result(timeout=5)['items']] == [str(i) for i in range(5)]


def test_catalog_read_error_is_quiet_expandable_and_keeps_results():
    """Execute shared production renderer, including empty workflow catalogs."""
    import subprocess
    from console import herdr_factory_console as console

    source = console.HTML
    notes = source[source.index('function deliverableNoteHtml('):source.index('function deliverableCountsText(')]
    renderer = source[source.index('function renderDeliverables('):source.index('async function openDeliverable(')]
    program = r'''
const assert=require('node:assert/strict');
const esc=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const raw='无法读取任务工作区，未列出的文件不代表尚未生成。 <script>alert(1)</script>';
const packed={items:[{name:'PR #1611'}],counts:{present:1},error:raw};
const state={workflow:{deliverables:packed}};
const panel={hidden:true},body={innerHTML:''},count={textContent:''};
const document={getElementById:id=>({deliverablesPanel:panel,deliverablesBody:body,deliverablesCount:count})[id],querySelector:()=>({dataset:{workspace:'flow'}})};
const workflowDeliverables=()=>state.workflow.deliverables;
const deliverableCountsText=()=> '已产生 1 项';
const deliverableRowHtml=item=>'<div>'+esc(item.name)+'</div>';
''' + notes + renderer + r'''
renderDeliverables();
assert.ok(body.innerHTML.includes('<summary>部分文件暂时无法展示</summary>'));
assert.ok(body.innerHTML.includes('<details class="deliverables-notice">'));
assert.ok(!body.innerHTML.includes('deliverables-notice error'));
assert.ok(body.innerHTML.includes('PR #1611'));
assert.ok(body.innerHTML.includes('&lt;script&gt;'));
assert.ok(!body.innerHTML.includes('<script>'));
packed.items=[];renderDeliverables();
assert.ok(body.innerHTML.includes('部分文件暂时无法展示'));
assert.ok(!body.innerHTML.includes('还没有交付成果'));
packed.error='';packed.notice='成果列表可能未完整。';renderDeliverables();
assert.ok(body.innerHTML.includes(packed.notice));
packed.notice='';renderDeliverables();
assert.ok(body.innerHTML.includes('还没有交付成果'));
'''
    result = subprocess.run(['node', '-e', program], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_historical_file_preview_and_download_over_http(tmp_path, monkeypatch):
    import json
    import threading
    import urllib.error
    import urllib.parse
    import urllib.request
    from http.server import ThreadingHTTPServer
    from console import herdr_factory_console as console

    root, workspace = _workspace(tmp_path, 'old-task')
    monkeypatch.setenv('HERDR_CLONES_DIR', str(root))
    content = '# 历史报告\n\n可查阅、可下载。\n'
    (workspace / '需求清单.md').write_text(content)
    task = _task('old-task', workspace, 'requirements', status='superseded')
    _seed_console_workflow(tmp_path, monkeypatch, [task], NODES)
    server = ThreadingHTTPServer(('127.0.0.1', 0), console.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with urllib.request.urlopen(base + '/api/workflow/deliverables?id=wf-biz') as response:
            packed = json.load(response)['data']
        row = next(r for r in packed['items'] if r['task_id'] == 'old-task')
        assert row['status'] == 'historical' and row['actions'] == ['view', 'download']
        query = urllib.parse.urlencode(dict(workflow_id='wf-biz', task_id='old-task', id=row['id']))
        with urllib.request.urlopen(base + '/api/workflow/deliverable?' + query) as response:
            assert json.load(response)['data']['text'] == content
        with urllib.request.urlopen(base + '/api/workflow/deliverable?' + query + '&download=1') as response:
            assert response.read() == content.encode()
            assert 'attachment' in response.headers['Content-Disposition']
        wrong = query.replace('task_id=old-task', 'task_id=other-task')
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(base + '/api/workflow/deliverable?' + wrong)
        assert caught.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
