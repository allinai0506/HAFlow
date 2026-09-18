"""Global pytest environment for the HAFlow test suite.

Keep the workflow shared-docs ledger out of the real controller state dir:
tests that exercise gate verdicts / fix-loop invalidation append machine
evidence, and must never pollute ~/.herdr-controller/workflows/.
"""

import os
import tempfile

os.environ.setdefault(
    "HERDR_WORKFLOW_DOCS_DIR",
    tempfile.mkdtemp(prefix="herdr-test-workflow-docs-"),
)
