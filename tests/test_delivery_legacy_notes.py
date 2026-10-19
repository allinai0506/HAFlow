"""Identity-less delivery notes are human evidence, not machine claims.

A coordinator wrote prose notes with ``note-add --kind delivery`` (no
``delivery_branch``/``candidate_sha``/``review_task``/``test_gate``).  The
selector treated them as competing machine candidates and refused every
test/review launch (``delivery_ambiguous``, FR-4 fail-closed), locking the
whole workflow until the shared document was repaired by hand.

These tests pin the intended boundary: a note without candidate identity is
ignored and reported (``unidentified_delivery_notes``), while real ambiguity
between identified claims still fails closed.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from herdr import delivery_record, workflow_docs


ROOT = Path(__file__).resolve().parent.parent
WF = "wf-legacy-notes"
SHA = "a" * 40


def load_script(name: str, relative: str):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(name, str(ROOT / relative)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def docs_root(tmp_path, monkeypatch):
    monkeypatch.setenv(workflow_docs.DOCS_DIR_ENV, str(tmp_path / "docs"))
    return tmp_path / "docs"


def _legacy_note(workflow_id=WF, title="prose delivery note"):
    return workflow_docs.append_note(
        workflow_id, kind="delivery", title=title, body="人工留痕，无候选身份字段。",
        node="implementation",
    )


def _claim(note_id="n-claim", sha=SHA, **extra):
    note = {
        "note_id": note_id,
        "kind": "delivery",
        "workflow_id": WF,
        "delivery_id": f"candidate-{sha}",
        "delivery_branch": "herdr/integration-legacy-notes",
        "candidate_sha": sha,
        "review_task": f"{WF}-review-auto",
        "test_gate": f"{WF}-test-auto",
        "base": "",
        "supersedes": "",
    }
    note.update(extra)
    return note


def test_identity_less_delivery_note_is_ignored_not_fatal(docs_root):
    legacy = _legacy_note()

    assert delivery_record.unidentified_delivery_notes([legacy]) == [legacy]
    assert delivery_record.select_effective_delivery([legacy]) is None


def test_identity_less_note_does_not_hide_a_valid_claim(docs_root):
    legacy = _legacy_note()
    claim = _claim()

    effective = delivery_record.select_effective_delivery([legacy, claim])

    assert effective is not None
    assert effective["candidate_sha"] == SHA
    assert delivery_record.unidentified_delivery_notes([legacy, claim]) == [legacy]


def test_real_ambiguity_between_identified_claims_still_fails_closed(docs_root):
    legacy = _legacy_note()
    first = _claim(note_id="n-a", sha="1" * 40)
    second = _claim(note_id="n-b", sha="2" * 40)

    with pytest.raises(delivery_record.DeliveryAmbiguityError):
        delivery_record.select_effective_delivery([legacy, first, second])


def test_supersede_edge_to_a_legacy_note_still_resolves(docs_root):
    legacy = _legacy_note()
    claim = _claim(supersedes=legacy["note_id"])

    effective = delivery_record.select_effective_delivery([legacy, claim])

    assert effective is not None
    assert effective["candidate_sha"] == SHA


def test_note_add_rejects_the_delivery_kind(docs_root, capsys):
    module = load_script("herdr-task-legacy-notes", "bin/herdr-task")
    args = SimpleNamespace(
        workflow_id=WF, kind="delivery", title="prose", text="", file=None,
        node=None, task=None, agent=None, source=None, base_sha=None,
        round=1, invalidates=None, field=None,
    )

    with pytest.raises(SystemExit) as exc:
        module.note_add(args)

    assert exc.value.code == 2
    output = capsys.readouterr().out
    assert "record-delivery" in output
    assert workflow_docs.load_notes(WF) == []


def test_note_add_still_writes_human_kinds(docs_root):
    module = load_script("herdr-task-legacy-notes-human", "bin/herdr-task")
    args = SimpleNamespace(
        workflow_id=WF, kind="decision", title="裁决", text="正文", file=None,
        node="implementation", task=None, agent=None, source="human",
        base_sha=None, round=1, invalidates=None, field=None,
    )

    module.note_add(args)

    notes = workflow_docs.load_notes(WF)
    assert [note["kind"] for note in notes] == ["decision"]


def test_partially_identified_delivery_note_fails_closed(docs_root):
    for partial in (
        {"delivery_id": "candidate-x"},
        {"candidate_sha": SHA},
        {"delivery_branch": "herdr/integration-partial", "candidate_sha": SHA},
        {"review_task": f"{WF}-review-auto", "test_gate": f"{WF}-test-auto"},
        {"delivery_id": "candidate-x", "supersedes": "n-something"},
    ):
        note = {"note_id": "n-partial", "kind": "delivery", "workflow_id": WF}
        note.update(partial)
        with pytest.raises(delivery_record.DeliveryAmbiguityError):
            delivery_record.select_effective_delivery([note])


def test_ignored_note_cannot_suppress_a_real_claim(docs_root):
    claim = _claim()
    # An ignored (identity-less) note carrying a supersedes edge that targets
    # the claim must have no effect on the claim's eligibility.
    ignored = _legacy_note()
    ignored["supersedes"] = claim["note_id"]

    effective = delivery_record.select_effective_delivery([ignored, claim])

    assert effective is not None
    assert effective["candidate_sha"] == SHA
    assert delivery_record.unidentified_delivery_notes([ignored, claim]) == [ignored]


def test_ignored_note_is_not_a_claim_even_with_an_edge(docs_root):
    ignored = _legacy_note()
    ignored["supersedes"] = "n-anything"

    assert delivery_record._is_delivery_claim(ignored) is False
    assert ignored in delivery_record.unidentified_delivery_notes([ignored])

def test_launch_preflight_degrades_legacy_notes_and_uses_the_frozen_candidate(docs_root, capsys, monkeypatch):
    from herdr import task_cli_launch_preflight as module
    legacy = _legacy_note()
    frozen = "c" * 40
    monkeypatch.setattr(module, "_frozen_candidate_sha", lambda workflow_id: frozen)

    resolved = module.preflight_delivery_identity(
        WF, "review", f"{WF}-review-auto", claim=frozen,
        get_store=lambda: pytest.fail("valid frozen identity must not write state"),
    )

    assert resolved == {"candidate_sha": frozen, "identity_source": "frozen"}
    output = capsys.readouterr().out
    assert "[BASELINE DEGRADED]" in output
    assert legacy["note_id"] in output
