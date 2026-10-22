"""Global pytest environment for the HAFlow test suite.

Keep the workflow shared-docs ledger out of the real controller state dir:
tests that exercise gate verdicts / fix-loop invalidation append machine
evidence, and must never pollute ~/.herdr-controller/workflows/.

Also disable the Trajectory Observer by default: controller done-path tests
(listener/recovery/gateway) would otherwise trigger the default observation
scheduler and write findings into the production state DB. Tests that need the
observer enable it explicitly (HERDR_OBSERVER_ENABLED=1).
"""

import atexit
import os
import shutil
import site
import tempfile
from pathlib import Path

import pytest


def _isolate_home():
    """Point HOME at a throwaway directory before any HAFlow module reads Path.home().

    herdr.projects, herdr.state_db, herdr.kernel and console.common compute ROOT
    (~/.herdr-controller) at import time, so this must run before test modules import them.
    PYTHONUSERBASE pins user site-packages for subprocesses; git identity is copied so
    commits made by tests keep working.
    """
    real_home = Path(os.path.expanduser("~"))
    os.environ.setdefault("PYTHONUSERBASE", site.getuserbase())
    home = Path(tempfile.mkdtemp(prefix="haflow-test-home-"))
    for name in (".gitconfig", ".config/git"):
        src, dst = real_home / name, home / name
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        elif src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
    os.environ["HOME"] = str(home)
    atexit.register(shutil.rmtree, home, True)


_isolate_home()

os.environ.setdefault(
    "HERDR_WORKFLOW_DOCS_DIR",
    tempfile.mkdtemp(prefix="herdr-test-workflow-docs-"),
)

# Hard override: a developer shell exporting HERDR_OBSERVER_ENABLED=1 must not
# silently re-enable production writes from the suite.
os.environ["HERDR_OBSERVER_ENABLED"] = "0"

# Workflow continuation uses the shared attention ledger even after Tasks finish.
# Background scans from tests must never write the host Controller ledger.
os.environ["HERDR_ATTENTION_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="herdr-test-attention-"), "attention.json"
)

# Enabling deterministic observer tests must not implicitly enable model spend.
os.environ["HERDR_OBSERVER_JEV_ENABLED"] = "0"

# Allow legacy SC-010 operator regex tests to run without credentials unless strictly tested.
os.environ.setdefault("HERDR_ALLOW_UNAUTHENTICATED_OPERATOR", "1")


@pytest.fixture(autouse=True)
def isolate_model_credentials(monkeypatch):
    """Explicit fake-key tests may opt in; never inherit real shell credentials."""
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def reset_global_state_store():
    """Reset the global StateStore singleton and isolate environment variables to prevent cross-test pollution."""
    old_env = dict(os.environ)
    from herdr.state_store import reset_state_store
    reset_state_store()
    yield
    os.environ.clear()
    os.environ.update(old_env)
    reset_state_store()
