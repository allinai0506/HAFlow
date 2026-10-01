"""Global pytest environment for the HAFlow test suite.

Keep the workflow shared-docs ledger out of the real controller state dir:
tests that exercise gate verdicts / fix-loop invalidation append machine
evidence, and must never pollute ~/.herdr-controller/workflows/.

Also disable the Trajectory Observer by default: controller done-path tests
(listener/recovery/gateway) would otherwise trigger the default observation
scheduler and write findings into the production state DB. Tests that need the
observer enable it explicitly (HERDR_OBSERVER_ENABLED=1).
"""

import os
import tempfile

import pytest

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


@pytest.fixture(autouse=True)
def isolate_model_credentials(monkeypatch):
    """Explicit fake-key tests may opt in; never inherit real shell credentials."""
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
