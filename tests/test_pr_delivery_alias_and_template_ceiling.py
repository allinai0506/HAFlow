"""Tests for PR delivery alias compatibility and template delivery ceiling defaults.

Verifies:
1. herdr/pr_delivery.py:repository_identity supports SSH host aliases like github-*
   and maps them to canonical host 'github.com' (and gitee-* to 'gitee.com').
2. herdr/review_closure.py:parse_remote_identity and check_deferred_justified
   recognize SSH host aliases like github-*.
3. workflow_templates/software-development-v1.yaml has delivery_ceiling_default: pr
   giving wrapup node explicit authorization to create PR.
4. herdr/direct_dispatch.py and herdr/stage_skills.py default or resolve delivery_ceiling
   to 'pr' unless explicitly configured otherwise.
"""
import pytest
import yaml
from pathlib import Path

from herdr.pr_delivery import repository_identity, PullRequestClient
from herdr.review_closure import parse_remote_identity, collect_deferred_evidence, _closure_demanded
from herdr.stage_skills import assemble_stage_context


def test_repository_identity_github_aliases():
    # Standard formats
    assert repository_identity("git@github.com:allinai0506/HAFlow.git") == ("github.com", "allinai0506", "HAFlow")
    assert repository_identity("https://github.com/allinai0506/HAFlow.git") == ("github.com", "allinai0506", "HAFlow")
    assert repository_identity("https://github.com/allinai0506/HAFlow") == ("github.com", "allinai0506", "HAFlow")

    # SSH aliases (github-allinai, github-new, github-bid, github-work, etc.)
    assert repository_identity("git@github-allinai:allinai0506/HAFlow.git") == ("github.com", "allinai0506", "HAFlow")
    assert repository_identity("git@github-new:allinai0506/HAFlow.git") == ("github.com", "allinai0506", "HAFlow")
    assert repository_identity("git@github-bid:org/repo.git") == ("github.com", "org", "repo")
    assert repository_identity("ssh://git@github-custom/org/repo.git") == ("github.com", "org", "repo")
    assert repository_identity("git@github-prod:org-team/my-repo") == ("github.com", "org-team", "my-repo")

    # Gitee aliases
    assert repository_identity("git@gitee.com:allinai0506/HAFlow.git") == ("gitee.com", "allinai0506", "HAFlow")
    assert repository_identity("git@gitee-enterprise:allinai0506/HAFlow.git") == ("gitee.com", "allinai0506", "HAFlow")


def test_repository_identity_invalid():
    # Unsupported or malformed remotes must raise ValueError
    with pytest.raises(ValueError, match="PR publication requires an identified GitHub or Gitee origin"):
        repository_identity("https://gitlab.com/allinai0506/HAFlow.git")

    with pytest.raises(ValueError, match="PR publication requires an identified GitHub or Gitee origin"):
        repository_identity("git@bitbucket.org:allinai0506/HAFlow.git")

    with pytest.raises(ValueError, match="PR publication requires an identified GitHub or Gitee origin"):
        repository_identity("not-a-remote")


def test_review_closure_remote_identity_github_aliases(monkeypatch):
    assert parse_remote_identity("git@github-allinai:allinai0506/HAFlow.git") == ("allinai0506", "HAFlow")
    assert parse_remote_identity("git@github-new:allinai0506/HAFlow.git") == ("allinai0506", "HAFlow")
    assert parse_remote_identity("git@github.com:allinai0506/HAFlow.git") == ("allinai0506", "HAFlow")
    assert parse_remote_identity("https://github.com/allinai0506/HAFlow.git") == ("allinai0506", "HAFlow")

    # Unsupported forge with github- in repo path should NOT be treated as GitHub
    assert parse_remote_identity("https://gitlab.com/team/github-migration.git") is None
    assert parse_remote_identity("git@gitlab.com:team/github-migration.git") is None

    # collect_deferred_evidence recognizes github-* remotes and rejects deferred
    import herdr.review_closure as rc
    monkeypatch.setattr(rc, "delivery_identity", lambda s, t: ([], "", "", ""))
    monkeypatch.setattr(rc, "git_remote_urls", lambda p: ["git@github-allinai:allinai0506/HAFlow.git"])
    is_deferred, msg, meta = collect_deferred_evidence(None, {"source_repo": "/dummy"})
    assert not is_deferred
    assert "a GitHub remote exists" in msg
    # But an unsupported forge with github- in repo path allows deferred (not treated as GitHub)
    monkeypatch.setattr(rc, "git_remote_urls", lambda p: ["https://gitlab.com/team/github-migration.git"])
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: type("P", (), {"returncode": 0, "stderr": ""})())
    is_deferred, msg, meta = collect_deferred_evidence(None, {"source_repo": "/dummy"})
    assert is_deferred
    assert meta["provider"] == "unsupported-forge"


def test_software_development_v1_template_delivery_ceiling():
    template_path = Path(__file__).resolve().parent.parent / "workflow_templates" / "software-development-v1.yaml"
    with open(template_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # Must be 'pr' to authorize wrapup agent to create PR by default
    assert cfg.get("delivery_ceiling_default") == "pr"
    # By default, template opts into review closure because delivery ceiling is 'pr'
    assert _closure_demanded(cfg) is True


def test_stage_skills_delivery_ceiling_default_is_pr():
    ctx = assemble_stage_context(
        workflow_id="wf-test-ceiling",
        task_id="wf-test-ceiling-wrapup-auto",
        run_id="run-1",
        node_id="wrapup",
    )
    assert ctx["delivery_ceiling"] == "pr"
    assert "交付授权: pr（不得超越此上限）" in ctx["prompt_block"]


def test_direct_dispatch_passes_delivery_ceiling(monkeypatch):
    from herdr.direct_dispatch import _prompt
    import herdr.projects as projects

    node = {
        "id": "wrapup",
        "label": "收尾",
        "workflow_id": "wf-dispatch-test",
        "skills": ["six-step-finish"],
        "required_outputs": ["report"],
        "rules": ["rule 1"],
        "purpose": "wrapup",
    }

    # Case 1: Workflow config has delivery_ceiling_default: pr
    monkeypatch.setattr(
        projects,
        "workflow_config_for",
        lambda wid: {"name": "software-development-v1", "delivery_ceiling_default": "pr"},
    )
    prompt = _prompt(node, "requirements text", "goal text", ["acceptance item"])
    assert "交付授权: pr（不得超越此上限）" in prompt

    # Case 2: Node explicitly overrides delivery_ceiling to workspace
    node_ws = {
        **node,
        "delivery_ceiling": "workspace",
    }
    prompt_ws = _prompt(node_ws, "requirements text", "goal text", ["acceptance item"])
    assert "交付授权: workspace（不得超越此上限）" in prompt_ws


def test_plan_stage_dispatch_preserves_node_ceiling_and_workflow_id(monkeypatch):
    """Codex P1 finding 1: plan_stage_dispatch passes normalized node into prompt without dropping ceiling."""
    from herdr.direct_dispatch import plan_stage_dispatch
    import herdr.projects as projects

    node_workspace = {
        "id": "wrapup",
        "label": "收尾",
        "purpose": "完成收尾",
        "skills": ["six-step-finish"],
        "delivery_ceiling": "workspace",
    }

    # Template config has delivery_ceiling_default: pr
    monkeypatch.setattr(
        projects,
        "workflow_config_for",
        lambda wid: {"name": "software-development-v1", "delivery_ceiling_default": "pr"},
    )

    res = plan_stage_dispatch("wf-p1-test", node_workspace, [], "requirements text")
    assert res["mode"] == "dispatch"
    spec = res["specs"][0]
    prompt = spec["prompt"]
    # Node explicit override to workspace MUST be honored, not overwritten by default 'pr'
    assert "交付授权: workspace（不得超越此上限）" in prompt
    assert "workflow=wf-p1-test" in prompt


def test_run_level_delivery_ceiling_overrides_template_default(monkeypatch):
    """Codex P1 finding 2: run-level delivery_ceiling in workflow record overrides template default in prompt."""
    from herdr.direct_dispatch import plan_stage_dispatch
    from herdr.review_closure import closure_required
    import herdr.projects as projects
    from herdr.state_store import get_state_store

    node_wrapup = {
        "id": "wrapup",
        "label": "收尾",
        "purpose": "完成收尾",
        "skills": ["six-step-finish"],
        "review_closure_required": True,
    }

    # Workflow config snapshot defaults to pr
    wf_config = {
        "name": "software-development-v1",
        "delivery_ceiling_default": "pr",
        "nodes": [node_wrapup],
    }
    monkeypatch.setattr(projects, "workflow_config_for", lambda wid: wf_config)

    # But authoritative workflow record in DB has run-level explicit delivery_ceiling: workspace
    mock_store = type("MockStore", (), {
        "get_workflow": lambda self, wid: {
            "workflow_id": wid,
            "delivery_ceiling": "workspace",
            "config": wf_config,
        }
    })()
    monkeypatch.setattr("herdr.state_store.get_state_store", lambda: mock_store)

    # 1. Prompt must honor run-level workspace ceiling
    res = plan_stage_dispatch("wf-run-level-test", node_wrapup, [], "req")
    prompt = res["specs"][0]["prompt"]
    assert "交付授权: workspace（不得超越此上限）" in prompt

    # 2. closure_required must also honor run-level workspace ceiling (skips review closure)
    task = {"workflow_id": "wf-run-level-test", "node": "wrapup"}
    assert closure_required(mock_store, task) is False


def test_explicit_workflow_ceiling_overrides_node_default(monkeypatch):
    """Codex Round 2 P1 finding: explicit workflow ceiling overrides node delivery_ceiling_default."""
    from herdr.direct_dispatch import plan_stage_dispatch
    from herdr.review_closure import closure_required, _recorded_ceiling
    import herdr.projects as projects

    node_wrapup = {
        "id": "wrapup",
        "label": "收尾",
        "purpose": "完成收尾",
        "skills": ["six-step-finish"],
        "delivery_ceiling_default": "pr",
        "review_closure_required": True,
    }

    # Workflow config explicitly restricts delivery to workspace
    wf_config = {
        "name": "software-development-v1",
        "delivery_ceiling": "workspace",
        "nodes": [node_wrapup],
    }
    monkeypatch.setattr(projects, "workflow_config_for", lambda wid: wf_config)

    mock_store = type("MockStore", (), {
        "get_workflow": lambda self, wid: {
            "workflow_id": wid,
            "config": wf_config,
        }
    })()
    monkeypatch.setattr("herdr.state_store.get_state_store", lambda: mock_store)

    # 1. Authoritative _recorded_ceiling yields workspace
    assert _recorded_ceiling(wf_config) == "workspace"

    # 2. plan_stage_dispatch prompt must yield workspace, NOT node's delivery_ceiling_default pr
    res = plan_stage_dispatch("wf-explicit-wf-test", node_wrapup, [], "requirements text")
    prompt = res["specs"][0]["prompt"]
    assert "交付授权: workspace（不得超越此上限）" in prompt
    assert "交付授权: pr" not in prompt

    # 3. closure_required aligns with recorded workspace restriction (skips review closure)
    task = {"workflow_id": "wf-explicit-wf-test", "node": "wrapup"}
    assert closure_required(mock_store, task) is False


def test_remote_identity_and_deferred_closure_with_explicit_port(monkeypatch):
    """Codex Round 2 P2 finding: explicit-port and mixed-case GitHub URLs are recognized and reject deferred closure."""
    import herdr.review_closure as rc

    # 1. parse_remote_identity recognizes explicit port and mixed-case host
    assert parse_remote_identity("ssh://git@github.com:22/org/repo.git") == ("org", "repo")
    assert parse_remote_identity("ssh://git@github-work:2222/org/repo.git") == ("org", "repo")
    assert parse_remote_identity("https://github.com:443/org/repo.git") == ("org", "repo")
    assert parse_remote_identity("https://GitHub.com/MyOrg/MyRepo.git") == ("MyOrg", "MyRepo")
    assert parse_remote_identity("git@GITHUB.COM:MyOrg/MyRepo.git") == ("MyOrg", "MyRepo")

    # 2. repository_identity recognizes explicit port and mixed-case host
    assert repository_identity("ssh://git@github.com:22/org/repo.git") == ("github.com", "org", "repo")
    assert repository_identity("ssh://git@github-custom:2222/org/repo.git") == ("github.com", "org", "repo")
    assert repository_identity("ssh://git@gitee.com:22/org/repo.git") == ("gitee.com", "org", "repo")
    assert repository_identity("https://github.com:443/org/repo.git") == ("github.com", "org", "repo")
    assert repository_identity("https://GitHub.com/MyOrg/MyRepo.git") == ("github.com", "MyOrg", "MyRepo")

    # 3. collect_deferred_evidence rejects deferred closure when explicit-port or mixed-case GitHub remote is present
    monkeypatch.setattr(rc, "delivery_identity", lambda s, t: ([], "", "", ""))
    monkeypatch.setattr(rc, "git_remote_urls", lambda p: ["ssh://git@github.com:22/org/repo.git"])
    is_deferred, msg, meta = collect_deferred_evidence(None, {"source_repo": "/dummy"})
    assert not is_deferred
    assert "a GitHub remote exists" in msg

    monkeypatch.setattr(rc, "git_remote_urls", lambda p: ["https://GitHub.com/MyOrg/MyRepo.git"])
    is_deferred, msg, meta = collect_deferred_evidence(None, {"source_repo": "/dummy"})
    assert not is_deferred
    assert "a GitHub remote exists" in msg


@pytest.mark.parametrize("host", [
    "github-work", "github-enterprise.com", "github-corp.example",
    "gitee-work", "gitee-enterprise.com", "github.com.evil", "gitee.com.evil",
])
@pytest.mark.parametrize("port", ["", ":443"])
def test_repository_identity_rejects_https_alias_hosts(host, port):
    with pytest.raises(ValueError):
        repository_identity(f"https://{host}{port}/Org/Repo.git")
    assert parse_remote_identity(f"https://{host}{port}/Org/Repo.git") is None


@pytest.mark.parametrize("host,forge", [
    ("GitHub.com", "github.com"), ("GITEE.COM", "gitee.com"),
    ("GitHub-Work", "github.com"), ("Gitee-Work", "gitee.com"),
])
@pytest.mark.parametrize("form", ["git@{host}:Org/Repo.git", "ssh://git@{host}:2222/Org/Repo.git"])
def test_ssh_alias_boundaries_keep_working(host, forge, form):
    url = form.format(host=host)
    assert repository_identity(url) == (forge, "Org", "Repo")
    assert parse_remote_identity(url) == (("Org", "Repo") if forge == "github.com" else None)


@pytest.mark.parametrize("host,forge", [("GitHub.com", "github.com"), ("GITEE.COM", "gitee.com")])
@pytest.mark.parametrize("port", ["", ":443"])
def test_public_https_host_boundaries(host, forge, port):
    url = f"https://{host}{port}/Org/Repo.git"
    assert repository_identity(url) == (forge, "Org", "Repo")
    assert parse_remote_identity(url) == (("Org", "Repo") if forge == "github.com" else None)


@pytest.mark.parametrize("host", ["github.com", "github-work", "gitee.com", "gitee-work"])
def test_scp_style_with_port_is_rejected(host):
    url = f"git@{host}:2222/Org/Repo.git"
    with pytest.raises(ValueError):
        repository_identity(url)
    assert parse_remote_identity(url) is None


@pytest.mark.parametrize("url", [
    "http://github-work/Org/Repo.git", "git+ssh://git@github-work/Org/Repo.git",
    "https://github.com:Org/Repo.git", "ssh://git@github-work:Org/Repo.git",
])
def test_unconfirmed_ssh_forms_are_rejected(url):
    with pytest.raises(ValueError):
        repository_identity(url)
    assert parse_remote_identity(url) is None


@pytest.mark.parametrize("url,unsupported", [
    ("https://github-enterprise.com/Org/Repo.git", True),
    ("https://GitHub-Work:443/Org/Repo.git", True),
    ("https://gitee-work/Org/Repo.git", True),
    ("ssh://git@GitHub-Work:2222/Org/Repo.git", False),
    ("git@github-work:Org/Repo.git", False),
])
def test_collect_deferred_evidence_https_enterprise_is_unsupported(monkeypatch, url, unsupported):
    import subprocess

    import herdr.review_closure as rc

    monkeypatch.setattr(rc, "delivery_identity", lambda s, t: ([], "", "", ""))
    monkeypatch.setattr(rc, "git_remote_urls", lambda p: [url])
    monkeypatch.setattr(rc.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    deferred, _reason, evidence = collect_deferred_evidence(None, {"source_repo": "/controlled"})
    assert deferred is unsupported
    if unsupported:
        assert evidence["provider"] == "unsupported-forge"
    else:
        assert evidence is None
