"""Herdr test package.

Isolate the workflow shared-docs ledger for unittest discovery as well
(pytest isolation lives in conftest.py).
"""

import os
import tempfile

os.environ.setdefault(
    "HERDR_WORKFLOW_DOCS_DIR",
    tempfile.mkdtemp(prefix="herdr-test-workflow-docs-"),
)
