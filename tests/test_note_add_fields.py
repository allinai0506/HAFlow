#!/usr/bin/env python3
"""`note-add --field KEY=VALUE` writes the decision contract (bin/herdr-task).

The console and the coordinator (a CLI user) must be able to write the *same*
decision record; otherwise an ask raised from the terminal would be invisible
to the console's decision panel.  The CLI only parses and forwards — field
name / reserved-name / length validation stays in
``herdr.workflow_docs.append_note``.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HERDR_TASK = ROOT / "bin" / "herdr-task"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DOCS_DIR_ENV = "HERDR_WORKFLOW_DOCS_DIR"


def _run(args, docs_dir, env_extra=None):
    env = dict(os.environ)
    env[DOCS_DIR_ENV] = str(docs_dir)
    env["PYTHONPATH"] = str(ROOT)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(HERDR_TASK), *args],
        text=True, capture_output=True, env=env, cwd=str(ROOT), timeout=60,
    )


@pytest.fixture
def docs_dir(tmp_path, monkeypatch):
    """Point both the CLI subprocess and this process at one temp ledger."""
    path = tmp_path / "docs"
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(DOCS_DIR_ENV, str(path))
    return path


@pytest.fixture
def wdocs():
    from herdr import workflow_docs as module
    return module


@pytest.fixture
def hdec():
    from herdr import human_decisions as module
    return module


def test_field_is_parsed_and_persisted(docs_dir, wdocs, hdec):
    r = _run([
        "note-add", "--workflow-id", "wf-cli-01", "--kind", "decision",
        "--title", "DU-10",
        "--text", "MATCH 是否入 V1？",
        "--field", "decision_id=DU-10",
        "--field", "decision_status=open",
        "--field", 'options=["入", "不入"]',
        "--field", "recommended=入",
    ], docs_dir)
    assert r.returncode == 0, r.stderr
    assert "[NOTE_ADDED]" in r.stdout

    notes = wdocs.load_notes("wf-cli-01")
    items = hdec.collect_open_decisions(notes)
    assert len(items) == 1
    assert items[0]["decision_id"] == "DU-10"
    assert items[0]["options"] == ["入", "不入"]
    assert items[0]["recommended"] == "入"
    assert items[0]["question"] == "MATCH 是否入 V1？"


def test_cli_written_decision_can_be_resolved_by_console_writer(docs_dir, wdocs, hdec):
    """Cross-writer contract: CLI opens it, the console HTTP layer closes it."""
    r = _run([
        "note-add", "--workflow-id", "wf-cli-02", "--kind", "decision",
        "--title", "超管 fail-closed 语义",
        "--text", "取哪一档？",
        "--field", "decision_id=DU-11",
        "--field", "decision_status=open",
        "--field", 'options=["全量 fail-closed", "只对越权 fail-closed"]',
    ], docs_dir)
    assert r.returncode == 0, r.stderr

    # Same append_note + same field names the console uses.
    wdocs.append_note(
        "wf-cli-02", kind="decision", title="DU-11 裁决",
        body="只对越权 fail-closed", source=wdocs.SOURCE_HUMAN,
        fields={"decision_id": "DU-11", "decision_status": "resolved",
                "question": "取哪一档？", "decision": "只对越权 fail-closed"},
    )
    assert hdec.collect_open_decisions(wdocs.load_notes("wf-cli-02")) == []


def test_malformed_field_is_rejected(docs_dir):
    r = _run([
        "note-add", "--workflow-id", "wf-cli-03", "--kind", "note",
        "--title", "x", "--field", "no_equals_sign",
    ], docs_dir)
    assert r.returncode == 2
    assert "KEY=VALUE" in (r.stderr + r.stdout)


def test_reserved_field_name_is_still_rejected_by_core(docs_dir):
    """The CLI must not become a second validation layer."""
    r = _run([
        "note-add", "--workflow-id", "wf-cli-04", "--kind", "note",
        "--title", "x", "--field", "note_id=forged",
    ], docs_dir)
    assert r.returncode == 2
    assert "cannot overwrite note metadata" in (r.stderr + r.stdout)
    assert json.dumps(["note_id"]) in (r.stderr + r.stdout) or "note_id" in (r.stderr + r.stdout)


def test_invalid_field_name_is_rejected_by_core(docs_dir):
    r = _run([
        "note-add", "--workflow-id", "wf-cli-05", "--kind", "note",
        "--title", "x", "--field", "bad name=1",
    ], docs_dir)
    assert r.returncode == 2
    assert "invalid note field name" in (r.stderr + r.stdout)


def test_existing_note_add_without_fields_is_unchanged(docs_dir, wdocs):
    r = _run([
        "note-add", "--workflow-id", "wf-cli-06", "--kind", "plan",
        "--title", "普通条目", "--text", "正文",
    ], docs_dir)
    assert r.returncode == 0, r.stderr
    notes = wdocs.load_notes("wf-cli-06")
    assert len(notes) == 1
    assert notes[0]["kind"] == "plan"
    assert "decision_id" not in notes[0]
