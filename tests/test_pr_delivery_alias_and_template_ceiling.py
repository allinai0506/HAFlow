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

    # collect_deferred_evidence recognizes github-* remotes and rejects deferred
    import herdr.review_closure as rc
    monkeypatch.setattr(rc, "delivery_identity", lambda s, t: ([], "", "", ""))
    monkeypatch.setattr(rc, "git_remote_urls", lambda p: ["git@github-allinai:allinai0506/HAFlow.git"])
    is_deferred, msg, meta = collect_deferred_evidence(None, {"source_repo": "/dummy"})
    assert not is_deferred
    assert "a GitHub remote exists" in msg


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
