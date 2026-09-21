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

os.environ.setdefault(
    "HERDR_WORKFLOW_DOCS_DIR",
    tempfile.mkdtemp(prefix="herdr-test-workflow-docs-"),
)

# Hard override (not setdefault): a developer shell exporting
# HERDR_OBSERVER_ENABLED=1 must not silently re-enable production writes from
# the suite. Tests that need the observer set it explicitly per test.
os.environ["HERDR_OBSERVER_ENABLED"] = "0"
