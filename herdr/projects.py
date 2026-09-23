#!/opt/homebrew/bin/python3
import datetime
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

HOME = Path.home()
ROOT = HOME / ".herdr-controller"
PROJECTS_FILE = ROOT / "projects.json"
WORKFLOWS_FILE = ROOT / "workflows.json"
LEGACY_WORKFLOW_FILE = ROOT / "workflow.json"

try:
    from .workflow import (
        load_template,
        normalize_workflow,
        find_node,
        execution_mode,
        validate_context_contract,
    )
except ImportError:
    from herdr.workflow import (
        load_template,
        normalize_workflow,
        find_node,
        execution_mode,
        validate_context_contract,
    )

STAGES = [
    ("requirements", "2需求分析", "plan"),
    ("plan", "3计划", "implementation"),
    ("implementation", "4实现", "test"),
    ("test", "5测试", "review"),
    ("review", "6评审", "wrapup"),
    ("wrapup", "7收尾", None),
]


def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _run(cmd, check=True):
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        check=check,
    )


def _run_json(cmd):
    result = _run(cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or result.stdout.strip()
            or f"command failed: {' '.join(cmd)}"
        )
    try:
        return json.loads(result.stdout)
    except Exception as exc:
        raise RuntimeError(
            f"invalid JSON from {' '.join(cmd)}: {result.stdout[:500]}"
        ) from exc


def canonical_root(path):
    return str(Path(path).expanduser().resolve())


def detect_git_root(cwd=None):
    cwd = canonical_root(cwd or os.getcwd())
    result = _run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "当前目录不在 Git 项目中。请先 cd 到目标项目，再运行 herdr-factory。"
        )
    return canonical_root(result.stdout.strip())


def project_id_for(root):
    root = canonical_root(root)
    base = Path(root).name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", base).strip("-") or "project"
    digest = hashlib.sha1(root.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def detect_base_branch(root):
    root = canonical_root(root)

    current = _run(
        ["git", "-C", root, "branch", "--show-current"],
        check=False,
    )
    branch = current.stdout.strip()
    if current.returncode == 0 and branch:
        return branch

    remote_head = _run(
        ["git", "-C", root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        check=False,
    )
    value = remote_head.stdout.strip()
    if remote_head.returncode == 0 and value.startswith("origin/"):
        return value.split("/", 1)[1]

    for candidate in ("dev", "main", "master"):
        probe = _run(
            ["git", "-C", root, "show-ref", "--verify", "--quiet", f"refs/heads/{candidate}"],
            check=False,
        )
        if probe.returncode == 0:
            return candidate

    raise RuntimeError(
        f"无法确定项目基础分支: {root}。请先切到项目的正常开发分支后重试。"
    )


def load_projects():
    return _load(PROJECTS_FILE, {"version": 1, "projects": {}})


def save_projects(data):
    _save(PROJECTS_FILE, data)


def _get_store():
    try:
        from .state_store import get_state_store
    except ImportError:
        from herdr.state_store import get_state_store
    if os.environ.get("HERDR_STATE_DB"):
        return get_state_store(Path(os.environ["HERDR_STATE_DB"]))
    wf_file = globals().get("WORKFLOWS_FILE") or os.environ.get("WORKFLOWS_FILE")
    if wf_file and Path(wf_file) != (ROOT / "workflows.json"):
        p = Path(wf_file)
        db_path = p.parent / "state.db" if p.name == "workflows.json" else p.with_suffix(".db")
        if db_path.parent.exists():
            return get_state_store(db_path=db_path)
    t_file = globals().get("TASKS_FILE") or os.environ.get("TASKS_FILE")
    if t_file and Path(t_file) != (ROOT / "tasks.json"):
        p = Path(t_file)
        db_path = p.parent / "state.db" if p.name == "tasks.json" else p.with_suffix(".db")
        if db_path.parent.exists():
            return get_state_store(db_path=db_path)
    if os.environ.get("WORKFLOWS_FILE"):
        p = Path(os.environ["WORKFLOWS_FILE"])
        db_path = p.parent / "state.db" if p.name == "workflows.json" else p.with_suffix(".db")
        if db_path.parent.exists():
            return get_state_store(db_path=db_path)
    if os.environ.get("TASKS_FILE"):
        p = Path(os.environ["TASKS_FILE"])
        db_path = p.parent / "state.db" if p.name == "tasks.json" else p.with_suffix(".db")
        if db_path.parent.exists():
            return get_state_store(db_path=db_path)
    return get_state_store()


def load_workflows():
    store = _get_store()
    return store.export_workflows_json()


def save_workflows(data):
    store = _get_store()
    for wid, wf in data.get("workflows", {}).items():
        wf.setdefault("workflow_id", wid)
        store.save_workflow(wf)
    # 投影必须经 StateStore 同步(环境变量感知)，严禁直写硬编码路径，
    # 否则测试/沙盒写入会覆盖线上 workflows.json。
    try:
        from .state_store import sync_workflows_projection
    except ImportError:
        from herdr.state_store import sync_workflows_projection
    sync_workflows_projection(store=store)


TERMINAL_WORKFLOW_STATUSES = {"completed"}


def active_workflows_for_project(project_id):
    """Registry entries of project_id that have not reached a terminal status."""
    store = _get_store()
    return [
        entry
        for entry in store.list_workflows()
        if entry.get("project_id") == project_id
        and entry.get("status") not in TERMINAL_WORKFLOW_STATUSES
    ]


def non_terminal_workflow_ids():
    store = _get_store()
    return {
        entry["workflow_id"]
        for entry in store.list_workflows()
        if entry.get("workflow_id") and entry.get("status") not in TERMINAL_WORKFLOW_STATUSES
    }


def workflow_closed(workflow_id):
    entry = project_for_workflow(workflow_id)
    return bool(entry) and entry.get("status") in TERMINAL_WORKFLOW_STATUSES


def workflow_registered(workflow_id):
    return bool(project_for_workflow(workflow_id))


@contextmanager
def workflow_creation_lock(project_id):
    """Serialize same-project workflow creation.

    The active-workflow check and register_workflow must be atomic, or two
    concurrent creates can both observe "no active workflow" and register.
    """
    lock_dir = ROOT / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_dir / f"{project_id}.workflow-create.lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def project_by_root(root):
    root = canonical_root(root)
    return load_projects().get("projects", {}).get(root)


def project_for_workflow(workflow_id):
    store = _get_store()
    record = store.get_workflow(workflow_id)
    if record and not (record.get("status") == "unknown" and not record.get("project_id")):
        return record
    return None


def workflow_config_for(workflow_id):
    record = project_for_workflow(workflow_id)
    if record and record.get("workflow_file"):
        path = Path(record["workflow_file"]).expanduser()
        if path.exists():
            cfg = _load(path, None)
            if cfg:
                return normalize_workflow(cfg)
    if LEGACY_WORKFLOW_FILE.exists():
        cfg = _load(LEGACY_WORKFLOW_FILE, None)
        if cfg:
            return normalize_workflow(cfg)
    return None


def save_workflow_config_for(workflow_id, workflow_data):
    record = project_for_workflow(workflow_id)
    if record and record.get("workflow_file"):
        path = Path(record["workflow_file"]).expanduser()
        _save(path, workflow_data)
        return True
    if LEGACY_WORKFLOW_FILE.exists():
        _save(LEGACY_WORKFLOW_FILE, workflow_data)
        return True
    return False


SUBJECT_MAX_LEN = 64
_SUBJECT_MARKDOWN_PREFIX = re.compile(
    r"^(#{1,6}\s+|[-*+>]+\s+|\d+[.、)]\s*|\[[ xX]\]\s*)+"
)


def requirement_subject(requirement=""):
    """Extract a one-line human-readable subject from a free-form requirement.

    Prefers the first non-heading line (a bare `## 需求` title says nothing),
    strips markdown heading/list/emphasis markers, then truncates.
    """
    candidates = []
    for raw in str(requirement or "").splitlines():
        text = raw.strip()
        stripped = _SUBJECT_MARKDOWN_PREFIX.sub("", text).strip()
        if not stripped:
            continue
        candidates.append((text.startswith("#"), stripped))
    if not candidates:
        return ""
    line = next((s for is_heading, s in candidates if not is_heading), None)
    if line is None:
        line = candidates[0][1]
    line = re.sub(r"\s{2,}", " ", line.replace("**", "").replace("`", ""))
    if len(line) > SUBJECT_MAX_LEN:
        return line[: SUBJECT_MAX_LEN - 1] + "…"
    return line


def generate_workflow_id(project, prefix="wf", now=None):
    """Generate human-readable workflow ID using Option A: wf-{project}-{MMDD}-{seq:02d}."""
    if now is None:
        now = datetime.datetime.now()
    name = project.get("project_name") or Path(project.get("project_root", "")).name or "project"
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-") or "project"
    date_str = now.strftime("%m%d")
    expected_prefix = f"{prefix}-{slug}-{date_str}-"

    # Scan existing workflows in registry to find next sequence number
    existing_seqs = []
    store = _get_store()
    workflows = {w["workflow_id"]: w for w in store.list_workflows() if w.get("workflow_id")}
    for wid in workflows:
        if wid.startswith(expected_prefix):
            remainder = wid[len(expected_prefix):]
            if remainder.isdigit():
                existing_seqs.append(int(remainder))
            elif "-" in remainder:
                first_part = remainder.split("-")[0]
                if first_part.isdigit():
                    existing_seqs.append(int(first_part))

    next_seq = max(existing_seqs, default=0) + 1
    candidate_id = f"{expected_prefix}{next_seq:02d}"
    while candidate_id in workflows:
        next_seq += 1
        candidate_id = f"{expected_prefix}{next_seq:02d}"

    return candidate_id


def _snapshot_workflow_definition(workflow_id, source_file):
    """Workflow Run Definition Snapshot：把创建时刻的项目级定义固化为 Run 私有不可变文件。

    项目共享 workflow.json 代表"下一次 Workflow 用的当前模板"，会在模板切换时被覆盖；
    每个 Workflow Run 的执行定义必须创建即冻结。复用 workflow 级根目录
    （~/.herdr-controller/workflows/<workflow_id>/，与 shared/ 同级互不干扰）。
    任何失败都回退旧行为（Run 继续读共享文件）并向 stderr 告警，绝不静默、绝不阻断创建。
    """
    reason = None
    if not source_file:
        return None
    try:
        try:
            from .workflow_docs import docs_root, validate_workflow_id
        except ImportError:
            from herdr.workflow_docs import docs_root, validate_workflow_id
        src = Path(source_file).expanduser()
        cfg = _load(src, None) if src.exists() else None
        if cfg is None:
            reason = f"定义源不可读: {source_file}"
        else:
            run_dir = docs_root() / validate_workflow_id(workflow_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            snapshot = run_dir / "workflow.json"
            _save(snapshot, cfg)
            return str(snapshot)
    except (ValueError, OSError) as exc:
        reason = str(exc)
    print(
        f"warning: Workflow {workflow_id} 定义快照失败（{reason}），"
        f"本次 Run 将继续读取项目共享 workflow.json，模板切换后其执行定义可能被覆盖。",
        file=sys.stderr,
    )
    return None


def freeze_run_definition(workflow_id, definition=None, *, source_file=None):
    """Freeze one run definition into an immutable run-private file.

    Either ``definition`` (a mapping) or ``source_file`` supplies the content.
    When both are absent, the project record for ``workflow_id`` provides the
    source file. Only file state is written; no run or task rows are touched.
    Returns the snapshot path string, or None when freezing is impossible.
    """
    if not str(workflow_id or "").strip():
        raise ValueError("workflow_id is required")
    if definition is not None:
        if not isinstance(definition, dict):
            raise ValueError("definition must be a mapping or None")
        try:
            try:
                from .workflow_docs import docs_root, validate_workflow_id
            except ImportError:
                from herdr.workflow_docs import docs_root, validate_workflow_id
            run_dir = docs_root() / validate_workflow_id(workflow_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            snapshot = run_dir / "workflow.json"
            _save(snapshot, definition)
            return str(snapshot)
        except (ValueError, OSError) as exc:
            print(
                f"warning: Workflow {workflow_id} definition freeze failed ({exc})",
                file=sys.stderr,
            )
            return None
    if source_file is not None:
        return _snapshot_workflow_definition(workflow_id, source_file)
    try:
        record = project_for_workflow(workflow_id)
    except Exception:
        record = None
    source = (record or {}).get("workflow_file")
    if not source:
        return None
    return _snapshot_workflow_definition(workflow_id, source)


def register_workflow(workflow_id, project, requirement="", title="", execution=None, context=None, workflow_file=None, metadata=None):
    title = (title or "").strip()
    subject = title or requirement_subject(requirement) or "未命名工作流"
    # Single-write contract: workflow_file and metadata are merged into the
    # entry before the one save_workflow call below. No second write follows.
    resolved_workflow_file = workflow_file or project["workflow_file"]
    wf_entry = {
        "workflow_id": workflow_id,
        "title": title,
        "requirement_subject": subject,
        "project_id": project["project_id"],
        "project_name": project["project_name"],
        "project_root": project["project_root"],
        "base_branch": project.get("base_branch"),
        "workspace_id": project["workspace_id"],
        "coordinator_pane_id": project["coordinator_pane_id"],
        "workflow_file": resolved_workflow_file,
        "requirement": requirement,
        "startup_ready": False,
        "status": "running",
    }
    if isinstance(metadata, dict):
        for key, value in metadata.items():
            if key not in wf_entry:
                wf_entry[key] = value
    if execution:
        wf_entry["execution"] = execution
        snapshot_file = _snapshot_workflow_definition(
            workflow_id, project.get("workflow_file")
        )
        if snapshot_file and not workflow_file:
            wf_entry["workflow_file"] = snapshot_file
    if context:
        wf_entry["context"] = context
    store = _get_store()
    store.save_workflow(wf_entry)
    try:
        from .state_store import sync_workflows_projection
    except ImportError:
        from herdr.state_store import sync_workflows_projection
    sync_workflows_projection(store=store)


def mark_workflow_startup_ready(workflow_id, healthy_agents=None, unhealthy_agents=None):
    store = _get_store()
    record = store.get_workflow(workflow_id)
    if not record:
        raise RuntimeError(f"Workflow registry missing: {workflow_id}")

    record["startup_ready"] = True
    if healthy_agents is not None:
        record["healthy_agents"] = healthy_agents
    if unhealthy_agents is not None:
        record["unhealthy_agents"] = unhealthy_agents

    store.save_workflow(record)

    try:
        from .state_store import sync_workflows_projection
    except ImportError:
        from herdr.state_store import sync_workflows_projection
    sync_workflows_projection(store=store)


def _workspace_alive(workspace_id):
    result = _run(
        ["herdr", "workspace", "get", workspace_id],
        check=False,
    )
    return result.returncode == 0


def _pane_alive(pane_id):
    result = _run(
        ["herdr", "pane", "get", pane_id],
        check=False,
    )
    return result.returncode == 0


def _coordinator_alive(pane_id):
    result = _run(
        ["herdr", "agent", "get", pane_id],
        check=False,
    )
    return result.returncode == 0


def _start_coordinator(project_id, coordinator_pane_id):
    # Herdr agent name must:
    # - start with lowercase letter
    # - contain only a-z, 0-9, - or _
    # - max 32 chars
    safe_id = re.sub(
        r"[^a-z0-9_-]+",
        "-",
        str(project_id).lower()
    ).strip("-_")

    if not safe_id or not safe_id[0].isalpha():
        safe_id = "project-" + safe_id

    agent_name = (safe_id + "-coordinator")[:32]

    return _run_json([
        "herdr",
        "agent",
        "start",
        agent_name,
        "--kind",
        "opencode",
        "--pane",
        coordinator_pane_id,
        "--timeout",
        "120000",
        "--",
        "--auto",
    ])

def project_alive(project):
    if not project:
        return False
    if not _workspace_alive(project.get("workspace_id", "")):
        return False
    if not _pane_alive(project.get("coordinator_pane_id", "")):
        return False
    workflow = _load(project.get("workflow_file", ""), None)
    if not workflow:
        return False

    # Stage tabs/anchors are repairable topology, not project identity.
    # Missing anchors must never reprovision the whole workspace.
    return True


def import_legacy_project(root, legacy_workflow=None):
    root = canonical_root(root)
    if project_by_root(root):
        return project_by_root(root)

    legacy_path = Path(
        legacy_workflow or LEGACY_WORKFLOW_FILE
    ).expanduser()
    if not legacy_path.exists():
        return None

    workflow = _load(legacy_path, None)
    if not workflow:
        return None

    workspace_id = workflow.get("workspace_id")
    coordinator = workflow.get("coordinator", {})
    coordinator_pane_id = coordinator.get("pane_id")

    if not workspace_id or not coordinator_pane_id:
        return None

    if not _workspace_alive(workspace_id):
        return None

    project_id = project_id_for(root)
    project_name = Path(root).name
    project_dir = ROOT / "projects" / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    workflow_file = project_dir / "workflow.json"

    workflow["project_id"] = project_id
    workflow["project_name"] = project_name
    workflow["project_root"] = root
    workflow["base_branch"] = detect_base_branch(root)
    workflow_file.write_text(
        json.dumps(workflow, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    record = {
        "project_id": project_id,
        "project_name": project_name,
        "project_root": root,
        "base_branch": detect_base_branch(root),
        "workspace_id": workspace_id,
        "coordinator_pane_id": coordinator_pane_id,
        "workflow_file": str(workflow_file),
    }

    data = load_projects()
    data.setdefault("projects", {})[root] = record
    save_projects(data)
    return record


def provision_project(root, template_name="software-development-v1", project_name=None, context_bindings=None):
    root = canonical_root(root)
    project_id = project_id_for(root)
    if not project_name:
        project_name = Path(root).name

    template_name = template_name or "software-development-v1"
    template = load_template(template_name)
    nodes = template.get("nodes", [])
    is_context = execution_mode(template) == "context"

    created = _run_json([
        "herdr",
        "workspace",
        "create",
        "--cwd",
        root,
        "--label",
        project_name,
        "--no-focus",
    ])

    result = created["result"]
    workspace_id = result["workspace"]["workspace_id"]
    coordinator_tab_id = result["tab"]["tab_id"]
    coordinator_pane_id = result["root_pane"]["pane_id"]

    _run([
        "herdr",
        "tab",
        "rename",
        coordinator_tab_id,
        "1总指挥",
    ])
    _run([
        "herdr",
        "pane",
        "rename",
        coordinator_pane_id,
        "总指挥",
    ])

    _start_coordinator(project_id, coordinator_pane_id)

    runtime_nodes = []
    for node in nodes:
        created_tab = _run_json([
            "herdr",
            "tab",
            "create",
            "--workspace",
            workspace_id,
            "--cwd",
            root,
            "--label",
            node["label"],
            "--no-focus",
        ])
        tab_result = created_tab["result"]
        tab_id = tab_result["tab"]["tab_id"]
        anchor_pane_id = tab_result["root_pane"]["pane_id"]
        _run([
            "herdr",
            "pane",
            "rename",
            anchor_pane_id,
            "Anchor",
        ], check=False)

        n = dict(node)
        n["tab_id"] = tab_id
        n["anchor_pane_id"] = anchor_pane_id
        runtime_nodes.append(n)

    return _register_project_workflow(
        project_id=project_id,
        project_name=project_name,
        root=root,
        workspace_id=workspace_id,
        template_name=template.get("name", template_name),
        coordinator_tab_id=coordinator_tab_id,
        coordinator_pane_id=coordinator_pane_id,
        runtime_nodes=runtime_nodes,
        base_branch="" if is_context else None,
        execution=template.get("execution") if is_context else None,
        context_contract=template.get("context") if is_context else None,
        context_bindings=context_bindings if is_context else None,
    )


def _register_project_workflow(
    project_id,
    project_name,
    root,
    workspace_id,
    template_name,
    coordinator_tab_id,
    coordinator_pane_id,
    runtime_nodes,
    base_branch=None,
    execution=None,
    context_contract=None,
    context_bindings=None,
):
    workflow = {
        "project_id": project_id,
        "project_name": project_name,
        "project_root": root,
        "base_branch": detect_base_branch(root) if base_branch is None else base_branch,
        "workspace_id": workspace_id,
        "workflow_template": template_name,
        "coordinator": {
            "tab_id": coordinator_tab_id,
            "label": "1总指挥",
            "pane_id": coordinator_pane_id,
        },
        "nodes": runtime_nodes,
    }
    if execution:
        workflow["execution"] = execution
    if context_contract:
        workflow["context"] = context_contract
    if context_bindings:
        workflow["context_bindings"] = context_bindings
    workflow = normalize_workflow(workflow)

    project_dir = ROOT / "projects" / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    workflow_file = project_dir / "workflow.json"
    workflow_file.write_text(
        json.dumps(workflow, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    record = {
        "project_id": project_id,
        "project_name": project_name,
        "project_root": root,
        "base_branch": workflow["base_branch"],
        "workspace_id": workspace_id,
        "coordinator_pane_id": coordinator_pane_id,
        "workflow_file": str(workflow_file),
    }
    if execution:
        record["execution"] = execution
    if context_bindings:
        record["context_bindings"] = context_bindings

    data = load_projects()
    data.setdefault("projects", {})[root] = record
    save_projects(data)
    return record


def create_project(root, project_name=None, template_name="software-development-v1"):
    root = detect_git_root(root)
    record = project_by_root(root)
    if record and _workspace_alive(record.get("workspace_id", "")):
        if template_name and template_name != _workflow_template_of(record):
            return reprovision_project_template(record, template_name)
        return dict(record, already_registered=True)

    return provision_project(root, template_name=template_name, project_name=project_name)


def adopt_workspace_as_project(
    workspace_id,
    root=None,
    template_name="software-development-v1",
    project_name=None,
):
    if not _workspace_alive(workspace_id):
        raise RuntimeError(f"Herdr 空间不存在或已关闭: {workspace_id}")

    if not root:
        pane_list_res = _run_json(["herdr", "pane", "list", "--workspace", workspace_id])
        panes = pane_list_res.get("result", {}).get("panes", [])
        for p in panes:
            cwd = p.get("cwd") or p.get("foreground_cwd")
            if cwd:
                root = cwd
                break

    if not root:
        raise ValueError(f"无法推断空间 {workspace_id} 的工作目录，请指定项目根路径")

    root = detect_git_root(root)
    project_id = project_id_for(root)
    if not project_name:
        project_name = Path(root).name

    template = load_template(template_name)
    nodes = template.get("nodes", [])

    tab_list_res = _run_json(["herdr", "tab", "list", "--workspace", workspace_id])
    tabs = tab_list_res.get("result", {}).get("tabs", [])
    pane_list_res = _run_json(["herdr", "pane", "list", "--workspace", workspace_id])
    panes = pane_list_res.get("result", {}).get("panes", [])

    if not tabs:
        raise RuntimeError(f"Herdr 空间 {workspace_id} 没有任何可用 Tab")

    # 1. Coordinator Tab & Pane
    coord_tab = next((t for t in tabs if "总指挥" in t.get("label", "")), None)
    if not coord_tab:
        coord_tab = tabs[0]
        _run(["herdr", "tab", "rename", coord_tab["tab_id"], "1总指挥"], check=False)
        coord_tab["label"] = "1总指挥"

    coord_panes = [p for p in panes if p.get("tab_id") == coord_tab["tab_id"]]
    coord_pane = next((p for p in coord_panes if p.get("label") == "总指挥"), None) or (coord_panes[0] if coord_panes else None)
    if not coord_pane:
        split_res = _run_json(["herdr", "pane", "split", coord_tab["tab_id"], "--cwd", root])
        coord_pane = split_res.get("result", {}).get("pane", {})

    coordinator_pane_id = coord_pane["pane_id"]
    _run(["herdr", "pane", "rename", coordinator_pane_id, "总指挥"], check=False)
    _start_coordinator(project_id, coordinator_pane_id)

    # 2. Stage nodes Tabs & Anchor Panes
    runtime_nodes = []
    for node in nodes:
        node_label = node["label"]
        matched_tab = next(
            (t for t in tabs if t["tab_id"] != coord_tab["tab_id"] and (t.get("label") == node_label or node_label in t.get("label", ""))),
            None,
        )
        if matched_tab:
            tab_id = matched_tab["tab_id"]
            tab_panes = [p for p in panes if p.get("tab_id") == tab_id]
            anchor_pane = next((p for p in tab_panes if p.get("label") == "Anchor"), None) or (tab_panes[0] if tab_panes else None)
            if not anchor_pane:
                split_res = _run_json(["herdr", "pane", "split", tab_id, "--cwd", root])
                anchor_pane = split_res.get("result", {}).get("pane", {})
            anchor_pane_id = anchor_pane["pane_id"]
            _run(["herdr", "pane", "rename", anchor_pane_id, "Anchor"], check=False)
        else:
            created_tab = _run_json([
                "herdr", "tab", "create",
                "--workspace", workspace_id,
                "--cwd", root,
                "--label", node_label,
                "--no-focus",
            ])
            tab_result = created_tab["result"]
            tab_id = tab_result["tab"]["tab_id"]
            anchor_pane_id = tab_result["root_pane"]["pane_id"]
            _run(["herdr", "pane", "rename", anchor_pane_id, "Anchor"], check=False)

        n = dict(node)
        n["tab_id"] = tab_id
        n["anchor_pane_id"] = anchor_pane_id
        runtime_nodes.append(n)

    return _register_project_workflow(
        project_id=project_id,
        project_name=project_name,
        root=root,
        workspace_id=workspace_id,
        template_name=template.get("name", template_name),
        coordinator_tab_id=coord_tab["tab_id"],
        coordinator_pane_id=coordinator_pane_id,
        runtime_nodes=runtime_nodes,
    )


def unregister_project(root_or_id, close_workspace=False, force=False):
    """Unregister a project from Herdr Factory registry."""
    data = load_projects()
    projects_dict = data.get("projects", {})

    target_key = None
    target_project = None

    try:
        resolved_root = canonical_root(root_or_id)
    except Exception:
        resolved_root = str(root_or_id)

    for k, v in projects_dict.items():
        if root_or_id in (k, v.get("project_id"), v.get("project_root")) or resolved_root in (k, v.get("project_root")):
            target_key, target_project = k, v
            break

    if not target_project:
        raise ValueError(f"未找到注册的项目: {root_or_id}")

    project_id = target_project.get("project_id")
    if project_id:
        active = active_workflows_for_project(project_id)
        if active and not force:
            raise RuntimeError(
                f"项目【{target_project.get('project_name', project_id)}】仍有 {len(active)} 个未完成的活跃工作流，禁止注销。"
                "请先等待工作流完成或使用 force 强制注销。"
            )

    if close_workspace:
        wid = target_project.get("workspace_id")
        if wid and _workspace_alive(wid):
            _run(["herdr", "workspace", "close", wid], check=False)

    if target_key in projects_dict:
        del projects_dict[target_key]
        save_projects(data)

    return target_project


def _workflow_template_of(record):
    """已有项目当前生效的模板名(读 workflow.json;缺失返回空串)。"""
    workflow_file = record.get("workflow_file")
    if not workflow_file:
        return ""
    cfg = _load(workflow_file, {}) or {}
    return cfg.get("workflow_template") or ""


def reprovision_project_template(record, template_name, context_bindings=None):
    """切换已有项目的模板:保留 Workspace 与协调者 Pane,重编节点 Tab/Anchor。

    背景(wf-nexusarchive-0918-01):`ensure_project` 对已注册项目直接返回旧
    record,控制台选择的模板被静默忽略,general-task-v1 实际按
    software-development-v1 运行。切换前必须无活跃工作流,否则拒绝。

    Workspace 身份与模板解耦:context 项目换模板时保留 execution/base_branch=""
    /契约与本次绑定,绝不重新引入 Git 语义。
    """
    root = canonical_root(record["project_root"])
    project_id = record["project_id"]
    project_name = record.get("project_name") or Path(root).name
    workspace_id = record.get("workspace_id", "")

    if not _workspace_alive(workspace_id):
        return provision_project(
            root,
            template_name=template_name,
            project_name=project_name,
            context_bindings=context_bindings,
        )

    active = active_workflows_for_project(project_id)
    if active:
        raise RuntimeError(
            "项目存在活跃工作流,拒绝切换模板: "
            + ", ".join(e.get("workflow_id", "?") for e in active)
            + ";请先收尾(close-workflow)后再切换。"
        )

    workflow_file = record.get("workflow_file")
    old_cfg = _load(workflow_file, {}) if workflow_file else {}
    old_nodes = (old_cfg or {}).get("nodes") or []
    coordinator_cfg = (old_cfg or {}).get("coordinator") or {}
    coordinator_tab_id = coordinator_cfg.get("tab_id") or ""
    coordinator_pane_id = (
        record.get("coordinator_pane_id")
        or coordinator_cfg.get("pane_id")
        or ""
    )

    template = load_template(template_name)
    nodes = template.get("nodes", [])

    runtime_nodes = []
    for node in nodes:
        created_tab = _run_json([
            "herdr",
            "tab",
            "create",
            "--workspace",
            workspace_id,
            "--cwd",
            root,
            "--label",
            node["label"],
            "--no-focus",
        ])
        tab_result = created_tab["result"]
        tab_id = tab_result["tab"]["tab_id"]
        anchor_pane_id = tab_result["root_pane"]["pane_id"]
        _run([
            "herdr",
            "pane",
            "rename",
            anchor_pane_id,
            "Anchor",
        ], check=False)

        n = dict(node)
        n["tab_id"] = tab_id
        n["anchor_pane_id"] = anchor_pane_id
        runtime_nodes.append(n)

    # 关闭旧模板节点 Tab(协调者 Tab 永远保留;新拓扑 Tab 刚创建)。
    new_tab_ids = {n["tab_id"] for n in runtime_nodes}
    for old in old_nodes:
        tab_id = old.get("tab_id")
        if not tab_id or tab_id in new_tab_ids or tab_id == coordinator_tab_id:
            continue
        _run(["herdr", "tab", "close", tab_id], check=False)

    # context 模板切换不得引入 Git 语义：detect_base_branch 只属于 git 路径。
    is_context = execution_mode(template) == "context"

    return _register_project_workflow(
        project_id=project_id,
        project_name=project_name,
        root=root,
        workspace_id=workspace_id,
        template_name=template.get("name", template_name),
        coordinator_tab_id=coordinator_tab_id,
        coordinator_pane_id=coordinator_pane_id,
        runtime_nodes=runtime_nodes,
        base_branch="" if is_context else None,
        execution=template.get("execution") if is_context else None,
        context_contract=template.get("context") if is_context else None,
        context_bindings=context_bindings if is_context else None,
    )


def ensure_project(root, template_name=None):
    root = canonical_root(root)
    record = project_by_root(root)

    if record:
        workspace_id = record.get("workspace_id", "")
        coordinator_pane_id = record.get("coordinator_pane_id", "")

        # Existing registered project: keep the existing Workspace.
        # Do NOT reprovision the entire project merely because an anchor Pane
        # or Agent lifecycle check looks stale. User-created/retained Panes
        # are part of the project state and must be preserved.
        if _workspace_alive(workspace_id):
            if not _pane_alive(coordinator_pane_id):
                raise RuntimeError(
                    "Registered Herdr Workspace is alive but coordinator Pane "
                    f"is missing: workspace={workspace_id} "
                    f"pane={coordinator_pane_id}. Refusing automatic reprovision."
                )

            if not _coordinator_alive(coordinator_pane_id):
                _start_coordinator(
                    record["project_id"],
                    coordinator_pane_id,
                )

            # 显式请求了不同模板(如控制台 run --template)时切换节点拓扑;
            # 未指定模板(template_name=None)保持现状。
            if template_name and template_name != _workflow_template_of(record):
                return reprovision_project_template(record, template_name)

            return record

        # Only a genuinely missing Workspace is provisioned again.
        return provision_project(
            root, template_name=template_name or "software-development-v1"
        )

    return provision_project(
        root, template_name=template_name or "software-development-v1"
    )


def ensure_context_project(root, template_name, context_bindings=None):
    """execution.mode=context 项目：任意真实目录即可注册，不要求 Git Repository。

    契约校验（required 缺失 fail-fast / unknown 拒绝）在此完成；
    binding 路径统一解析为绝对路径后写入项目与 Workflow 记录。
    """
    root = canonical_root(root)
    if not Path(root).is_dir():
        raise RuntimeError(f"Context runtime 目录不存在或不是目录: {root}")

    template = load_template(template_name)
    if execution_mode(template) != "context":
        raise RuntimeError(
            f"模板 {template_name} 不是 context 执行模式，无法按 Context 项目注册"
        )

    bindings = dict(context_bindings or {})
    validate_context_contract(template, bindings)

    resolved = {}
    for ctx_id, path in bindings.items():
        canonical = str(Path(path).expanduser().resolve())
        # Context 是文件系统引用：目录或普通文件（Markdown/PDF/Excel/JSON…）均合法。
        if not Path(canonical).exists():
            raise RuntimeError(f"Context path not found: {ctx_id}={path}")
        resolved[ctx_id] = canonical

    record = project_by_root(root)
    if record:
        if _workspace_alive(record.get("workspace_id", "")):
            if not _pane_alive(record.get("coordinator_pane_id", "")):
                raise RuntimeError(
                    "Registered Context Workspace is alive but coordinator Pane "
                    f"is missing: workspace={record.get('workspace_id')} "
                    f"pane={record.get('coordinator_pane_id')}. "
                    "Refusing automatic reprovision."
                )
            # Workspace Identity != Workflow Template：
            # 同一业务 Workspace 可依次运行不同 context 模板（无活跃工作流时）。
            current_template = _workflow_template_of(record)
            if template_name and current_template and current_template != template_name:
                return reprovision_project_template(
                    record,
                    template_name,
                    context_bindings=resolved,
                )
            return record

    return provision_project(
        root,
        template_name=template_name,
        context_bindings=resolved,
    )


def ensure_node_runtime(workflow_id_or_root, node_id):
    """Ensure that the Tab and Anchor Pane for a node exist and are healthy.

    If the Tab was closed/deleted, recreate it.
    If the Anchor Pane was closed/purged, detect or split a new Anchor Pane.
    Persists repaired runtime mappings to workflow.json.
    """
    workflow_id = None
    project = None
    workflow_cfg = None

    if project_for_workflow(workflow_id_or_root):
        workflow_id = workflow_id_or_root
        project = project_for_workflow(workflow_id)
        workflow_cfg = workflow_config_for(workflow_id)
    else:
        project = project_by_root(workflow_id_or_root)
        if project:
            path = Path(project.get("workflow_file", "")).expanduser()
            workflow_cfg = normalize_workflow(_load(path, None)) if path.exists() else None
        else:
            # Maybe it's a project_id or workflow_id that needs lookup in workflows
            all_wf = load_workflows().get("workflows", {})
            if workflow_id_or_root in all_wf:
                workflow_id = workflow_id_or_root
                project = all_wf[workflow_id]
                workflow_cfg = workflow_config_for(workflow_id)

    if not workflow_cfg:
        raise RuntimeError(f"Workflow config missing for: {workflow_id_or_root}")

    workspace_id = workflow_cfg.get("workspace_id")
    if not workspace_id or not _workspace_alive(workspace_id):
        raise RuntimeError(f"Herdr workspace is not alive: {workspace_id}")

    project_root = workflow_cfg.get("project_root", os.getcwd())

    # Find node (by id or stage key)
    node = find_node(workflow_cfg, node_id)
    if not node:
        raise RuntimeError(f"Node '{node_id}' not found in workflow definition.")

    tab_id = node.get("tab_id")
    anchor_pane_id = node.get("anchor_pane_id")
    label = node.get("label", node_id)
    repaired = False

    # Check Tab
    tab_alive = False
    if tab_id:
        res = _run(["herdr", "tab", "get", tab_id], check=False)
        tab_alive = (res.returncode == 0)

    if not tab_alive:
        created_tab = _run_json([
            "herdr", "tab", "create",
            "--workspace", workspace_id,
            "--cwd", project_root,
            "--label", label,
            "--no-focus",
        ])
        tab_result = created_tab["result"]
        tab_id = tab_result["tab"]["tab_id"]
        anchor_pane_id = tab_result["root_pane"]["pane_id"]
        _run(["herdr", "pane", "rename", anchor_pane_id, "Anchor"], check=False)
        node["tab_id"] = tab_id
        node["anchor_pane_id"] = anchor_pane_id
        repaired = True
    else:
        # Tab is alive, check Anchor Pane
        anchor_alive = False
        if anchor_pane_id:
            res = _run(["herdr", "pane", "get", anchor_pane_id], check=False)
            anchor_alive = (res.returncode == 0)

        if not anchor_alive:
            panes_data = _run_json(["herdr", "pane", "list", "--workspace", workspace_id])
            panes_list = panes_data.get("result", {}).get("panes", [])
            tab_panes = [p for p in panes_list if p.get("tab_id") == tab_id]

            if tab_panes:
                anchor_candidates = [p for p in tab_panes if p.get("label") == "Anchor"]
                if anchor_candidates:
                    anchor_pane_id = anchor_candidates[0]["pane_id"]
                else:
                    parent_pane = tab_panes[0]["pane_id"]
                    split_res = _run_json([
                        "herdr", "pane", "split",
                        parent_pane,
                        "--direction", "right",
                        "--cwd", project_root,
                        "--no-focus",
                    ])
                    anchor_pane_id = split_res["result"]["pane"]["pane_id"]
                    _run(["herdr", "pane", "rename", anchor_pane_id, "Anchor"], check=False)

                node["anchor_pane_id"] = anchor_pane_id
                repaired = True
            else:
                created_tab = _run_json([
                    "herdr", "tab", "create",
                    "--workspace", workspace_id,
                    "--cwd", project_root,
                    "--label", label,
                    "--no-focus",
                ])
                tab_result = created_tab["result"]
                tab_id = tab_result["tab"]["tab_id"]
                anchor_pane_id = tab_result["root_pane"]["pane_id"]
                _run(["herdr", "pane", "rename", anchor_pane_id, "Anchor"], check=False)
                node["tab_id"] = tab_id
                node["anchor_pane_id"] = anchor_pane_id
                repaired = True

    if repaired:
        normalized = normalize_workflow(workflow_cfg)
        for n in normalized.get("nodes", []):
            if n["id"] == node["id"]:
                n["tab_id"] = tab_id
                n["anchor_pane_id"] = anchor_pane_id
        for s in normalized.get("stages", []):
            if s["key"] == node["id"]:
                s["tab_id"] = tab_id
                s["anchor_pane_id"] = anchor_pane_id

        if workflow_id:
            save_workflow_config_for(workflow_id, normalized)
        elif project and project.get("workflow_file"):
            _save(project["workflow_file"], normalized)

    return {
        "workspace_id": workspace_id,
        "project_root": project_root,
        "node_id": node["id"],
        "node_label": label,
        "tab_id": tab_id,
        "anchor_pane_id": anchor_pane_id,
        "depends_on": node.get("depends_on", []),
        "node_type": node.get("node_type", "agent"),
        "agent_policy": node.get("agent_policy", {}),
        "purpose": node.get("purpose", ""),
        "default_integration_mode": node.get("default_integration_mode", "none"),
        "default_task_type": node.get("default_task_type", "docs"),
        "required_outputs": node.get("required_outputs", []),
        "rules": node.get("rules", []),
    }
