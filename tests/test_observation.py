from __future__ import annotations

import base64
import hashlib
import json
import multiprocessing
from pathlib import Path

import pytest

from herdr.observation import (
    ObservationStore,
    create_artifact_observation,
    create_observation,
    create_verification_observation,
    get_observation,
    list_observations,
    read_observation,
    verify_observation,
)
from herdr.state_db import get_db_connection


def _create_same_observation(db_path: str, result_queue) -> None:
    store = ObservationStore(Path(db_path))
    observation = create_observation(
        run_id="run-process-race",
        source_type="agent_log",
        source_ref="pane:p-race",
        content="same evidence",
        store=store,
    )
    result_queue.put(observation.observation_id)


def test_create_text_observation_persists_redacted_receipt_and_content(tmp_path: Path):
    store = ObservationStore(tmp_path / "state.db")

    observation = create_observation(
        run_id="run-1",
        task_id="task-1",
        workflow_id="wf-1",
        source_type="agent_log",
        source_ref="pane:p1",
        content="api_key=VERYSECRET123\nfinished",
        excerpt="api_key=VERYSECRET123\nfinished",
        metadata={"command": "echo api_key=VERYSECRET123"},
        store=store,
    )

    assert observation.observation_id.startswith("obs_")
    assert observation.size_bytes == len(b"[redacted]\nfinished")
    assert observation.sha256 == hashlib.sha256(b"[redacted]\nfinished").hexdigest()
    assert Path(observation.content_ref).is_file()
    assert Path(observation.content_ref).read_bytes() == b"[redacted]\nfinished"
    assert "VERYSECRET123" not in json.dumps(observation.to_mapping())
    assert get_observation(observation.observation_id, store=store) == observation


def test_text_bytes_and_json_string_are_redacted_before_hashing(tmp_path: Path):
    store = ObservationStore(tmp_path / "state.db")

    text = create_observation(
        run_id="run-bytes",
        source_type="tool_output",
        source_ref="command:bytes",
        content=b"api_key=VERYSECRET123",
        media_type="text/plain",
        store=store,
    )
    json_text = create_observation(
        run_id="run-json-string",
        source_type="verification",
        source_ref="verification:json-string",
        content='{"api_key":"VERYSECRET123","passed":false}',
        media_type="application/json",
        store=store,
    )

    assert Path(text.content_ref).read_text(encoding="utf-8") == "[redacted]"
    assert "VERYSECRET123" not in Path(json_text.content_ref).read_text(encoding="utf-8")


@pytest.mark.parametrize("content", [
    "client_secret=VERYSECRET123\naccess_token=VERYSECRET123\nrefresh_token=VERYSECRET123\nprivate_key=VERYSECRET123",
    b"client_secret=VERYSECRET123\naccess_token=VERYSECRET123\nrefresh_token=VERYSECRET123\nprivate_key=VERYSECRET123",
    "client-secret=VERYSECRET123\naccess-token=VERYSECRET123\nrefresh-token=VERYSECRET123\nprivate-key=VERYSECRET123",
])
def test_plain_text_credential_aliases_redact_content_excerpt_metadata_and_hash(tmp_path: Path, content):
    store = ObservationStore(tmp_path / "state.db")
    excerpt = "client-secret=VERYSECRET123"
    observation = create_observation(
        run_id=f"run-text-alias-{hash(content)}",
        source_type="agent_log",
        source_ref=f"pane:text-alias-{hash(content)}",
        content=content,
        media_type="text/plain",
        excerpt=excerpt,
        metadata={"tool_output": "access-token=VERYSECRET123"},
        store=store,
    )

    stored = Path(observation.content_ref).read_bytes()
    with get_db_connection(store.db_path) as conn:
        metadata_json = conn.execute(
            "SELECT metadata_json FROM observations WHERE observation_id = ?",
            (observation.observation_id,),
        ).fetchone()[0]
    assert b"VERYSECRET123" not in stored
    assert "VERYSECRET123" not in (observation.excerpt or "")
    assert "VERYSECRET123" not in metadata_json
    assert observation.sha256 == hashlib.sha256(stored).hexdigest()


def test_credential_key_variants_redact_content_metadata_and_excerpt(tmp_path: Path):
    store = ObservationStore(tmp_path / "state.db")
    keys = (
        "api-key", "passwd", "access_token", "refresh_token",
        "client_secret", "private_key", "API_KEY", "client-secret",
    )
    payload = {key: "VERYSECRET123" for key in keys}
    observation = create_observation(
        run_id="run-key-variants",
        source_type="verification",
        source_ref="verification:key-variants",
        content=payload,
        media_type="application/json",
        excerpt=json.dumps(payload),
        metadata=payload,
        store=store,
    )

    stored_content = Path(observation.content_ref).read_text(encoding="utf-8")
    with get_db_connection(store.db_path) as conn:
        metadata_json = conn.execute(
            "SELECT metadata_json FROM observations WHERE observation_id = ?",
            (observation.observation_id,),
        ).fetchone()[0]
    assert "VERYSECRET123" not in stored_content
    assert "VERYSECRET123" not in metadata_json
    assert "VERYSECRET123" not in (observation.excerpt or "")


def test_metadata_and_source_ref_have_bounded_receipts(tmp_path: Path):
    store = ObservationStore(tmp_path / "state.db")

    with pytest.raises(ValueError, match="source_ref"):
        create_observation(
            run_id="run-bounds",
            source_type="agent_log",
            source_ref="x" * 513,
            content="content",
            store=store,
        )
    with pytest.raises(ValueError, match="metadata"):
        create_observation(
            run_id="run-bounds",
            source_type="agent_log",
            source_ref="pane:bounded",
            content="content",
            metadata={"payload": "x" * 20000},
            store=store,
        )


def test_read_observation_is_bounded_and_reports_truncation(tmp_path: Path):
    store = ObservationStore(tmp_path / "state.db")
    observation = create_observation(
        run_id="run-large",
        source_type="agent_log",
        source_ref="pane:p-large",
        content="x" * (1024 * 1024),
        store=store,
    )

    result = read_observation(observation.observation_id, limit=16 * 1024, store=store)

    assert result["returned_bytes"] == 16 * 1024
    assert result["total_bytes"] == 1024 * 1024
    assert result["truncated"] is True
    assert len(result["content"].encode("utf-8")) <= 16 * 1024

    with pytest.raises(ValueError, match="64 KiB"):
        read_observation(observation.observation_id, limit=64 * 1024 + 1, store=store)


def test_verify_observation_detects_tampering(tmp_path: Path):
    store = ObservationStore(tmp_path / "state.db")
    observation = create_observation(
        run_id="run-integrity",
        source_type="agent_log",
        source_ref="pane:p-integrity",
        content="original",
        store=store,
    )
    Path(observation.content_ref).write_text("changed", encoding="utf-8")

    result = verify_observation(observation.observation_id, store=store)

    assert result["valid"] is False
    assert result["reason"] in {"size_mismatch", "sha256_mismatch"}


def test_binary_observation_read_is_base64_encoded_without_corruption(tmp_path: Path):
    store = ObservationStore(tmp_path / "state.db")
    observation = create_observation(
        run_id="run-binary",
        source_type="other",
        source_ref="binary:payload",
        content=bytes(range(256)),
        media_type="application/octet-stream",
        store=store,
    )

    result = read_observation(observation.observation_id, limit=256, store=store)

    assert result["content_encoding"] == "base64"
    assert base64.b64decode(result["content"]) == bytes(range(256))


def test_observation_content_is_immutable_and_duplicate_is_canonical(tmp_path: Path):
    store = ObservationStore(tmp_path / "state.db")
    first = create_observation(
        run_id="run-dedup",
        source_type="verification",
        source_ref="verification:e-1",
        content={"passed": False, "evidence_id": "e-1"},
        media_type="application/json",
        store=store,
    )
    second = create_observation(
        run_id="run-dedup",
        source_type="verification",
        source_ref="verification:e-1",
        content={"passed": False, "evidence_id": "e-1"},
        media_type="application/json",
        store=store,
    )

    assert second == first
    assert len(list_observations(run_id="run-dedup", store=store)) == 1
    with pytest.raises(FileExistsError):
        store.write_content(first.content_ref, b"different")


def test_changed_content_creates_new_observation(tmp_path: Path):
    store = ObservationStore(tmp_path / "state.db")
    first = create_observation(
        run_id="run-changing",
        source_type="agent_log",
        source_ref="pane:p-changing",
        content="one",
        store=store,
    )
    second = create_observation(
        run_id="run-changing",
        source_type="agent_log",
        source_ref="pane:p-changing",
        content="two",
        store=store,
    )

    assert second.observation_id != first.observation_id
    assert second.sha256 != first.sha256
    assert len(list_observations(run_id="run-changing", store=store)) == 2


def test_verification_adapter_preserves_evidence_id(tmp_path: Path):
    store = ObservationStore(tmp_path / "state.db")
    observation = create_verification_observation(
        {
            "passed": False,
            "passed_tests": 97,
            "total_tests": 100,
            "failing_count": 3,
            "lint_errors": 0,
            "type_errors": 0,
            "evidence_id": "tevd-1",
        },
        run_id="run-verification",
        store=store,
    )

    payload = json.loads(Path(observation.content_ref).read_text(encoding="utf-8"))
    assert observation.source_type == "verification"
    assert observation.source_ref == "verification:tevd-1"
    assert payload["evidence_id"] == "tevd-1"
    assert payload["failing_count"] == 3


def test_artifact_adapter_references_without_copying(tmp_path: Path):
    store = ObservationStore(tmp_path / "state.db")
    artifact = tmp_path / "report.json"
    artifact.write_text('{"status":"ok"}', encoding="utf-8")

    observation = create_artifact_observation(
        artifact,
        run_id="run-artifact",
        artifact_kind="verification-report",
        store=store,
    )

    assert observation.source_type == "artifact"
    assert Path(observation.content_ref) == artifact
    assert observation.size_bytes == artifact.stat().st_size
    assert observation.sha256 == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert not (tmp_path / "observations" / observation.observation_id).exists()


def test_relative_artifact_path_is_stable_across_cwd_changes(tmp_path: Path, monkeypatch):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    artifact = source_dir / "relative" / "path" / "report.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"status":"ok"}', encoding="utf-8")
    store = ObservationStore(tmp_path / "state.db")
    monkeypatch.chdir(source_dir)

    observation = create_artifact_observation(
        Path("relative/path/report.json"), run_id="run-relative-artifact", store=store,
    )

    monkeypatch.chdir(tmp_path)
    assert Path(observation.content_ref).is_absolute()
    assert Path(observation.content_ref) == artifact.resolve()
    assert read_observation(observation.observation_id, store=store)["content"]
    assert verify_observation(observation.observation_id, store=store)["valid"] is True


def test_artifact_hash_and_integrity_use_chunked_reads(tmp_path: Path, monkeypatch):
    store = ObservationStore(tmp_path / "state.db")
    artifact = tmp_path / "large-artifact.bin"
    artifact.write_bytes(b"x" * (1024 * 1024))
    observation = create_artifact_observation(artifact, run_id="run-chunked", store=store)

    def fail_read_bytes(_path):
        raise AssertionError("artifact hashing must not read the full file at once")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)
    assert verify_observation(observation.observation_id, store=store)["valid"] is True


def test_concurrent_processes_return_one_canonical_observation(tmp_path: Path):
    db_path = tmp_path / "state.db"
    ObservationStore(db_path)
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(target=_create_same_observation, args=(str(db_path), queue))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)

    assert [process.exitcode for process in processes] == [0, 0]
    assert queue.get(timeout=2) == queue.get(timeout=2)
    assert len(list_observations(run_id="run-process-race", store=ObservationStore(db_path))) == 1


def test_invalid_bounds_and_missing_artifact_are_rejected(tmp_path: Path):
    store = ObservationStore(tmp_path / "state.db")
    observation = create_observation(
        run_id="run-invalid",
        source_type="agent_log",
        source_ref="pane:p-invalid",
        content="content",
        store=store,
    )

    with pytest.raises(ValueError, match="non-negative"):
        read_observation(observation.observation_id, offset=-1, store=store)
    with pytest.raises(FileNotFoundError):
        create_artifact_observation(tmp_path / "missing.log", run_id="run-invalid", store=store)
