"""Immutable, redacted evidence storage for ObservationPack V1."""

from __future__ import annotations

import hashlib
import base64
import json
import mimetypes
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from . import state_db
from .supervisor.state import redact_text


SOURCE_TYPES = frozenset({
    "agent_log", "verification", "artifact", "tool_output", "runtime", "other",
})
DEFAULT_READ_LIMIT = 16 * 1024
MAX_READ_LIMIT = 64 * 1024
MAX_EXCERPT_CHARS = 1000
MAX_SOURCE_REF_CHARS = 512
MAX_METADATA_BYTES = 16 * 1024
_HASH_CHUNK_SIZE = 64 * 1024
_SENSITIVE_METADATA_KEYS = frozenset({"api_key", "apikey", "authorization", "password", "secret", "token"})


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if str(key).lower() in _SENSITIVE_METADATA_KEYS else _redact_value(item)
            for key, item in value.items()
        }
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(_redact_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _content_bytes(content: Any, media_type: str) -> bytes:
    if media_type == "application/json" or isinstance(content, (dict, list, tuple)):
        if isinstance(content, (bytes, bytearray)):
            content = bytes(content).decode("utf-8", errors="replace")
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                return redact_text(content).encode("utf-8")
        return _json_bytes(content)
    if isinstance(content, (bytes, bytearray)):
        if media_type.startswith("text/"):
            return redact_text(bytes(content).decode("utf-8", errors="replace")).encode("utf-8")
        return bytes(content)
    if isinstance(content, str):
        return redact_text(content).encode("utf-8")
    return redact_text(str(content)).encode("utf-8")


def _excerpt(content: bytes, media_type: str, explicit: Optional[str]) -> Optional[str]:
    if explicit is not None:
        return redact_text(str(explicit)).strip()[:MAX_EXCERPT_CHARS]
    if not content or not media_type.startswith("text/") and media_type != "application/json":
        return None
    return content.decode("utf-8", errors="replace").strip()[:MAX_EXCERPT_CHARS]


def _validate_metadata(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    value = _redact_value(metadata or {})
    if not isinstance(value, dict):
        raise ValueError("metadata must be a dict")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_METADATA_BYTES:
        raise ValueError(f"metadata exceeds {MAX_METADATA_BYTES} bytes")
    return value


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


@dataclass(frozen=True)
class Observation:
    observation_id: str
    run_id: str
    task_id: Optional[str]
    workflow_id: Optional[str]
    source_type: str
    source_ref: str
    content_ref: str
    media_type: str
    size_bytes: int
    sha256: str
    excerpt: Optional[str]
    created_at: float
    metadata: Dict[str, Any]

    def to_mapping(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Dict[str, Any]) -> "Observation":
        return cls(
            observation_id=str(value["observation_id"]),
            run_id=str(value["run_id"]),
            task_id=value.get("task_id"),
            workflow_id=value.get("workflow_id"),
            source_type=str(value["source_type"]),
            source_ref=str(value["source_ref"]),
            content_ref=str(value["content_ref"]),
            media_type=str(value["media_type"]),
            size_bytes=int(value["size_bytes"]),
            sha256=str(value["sha256"]),
            excerpt=value.get("excerpt"),
            created_at=float(value["created_at"]),
            metadata=dict(value.get("metadata") or {}),
        )


class ObservationStore:
    """SQLite metadata plus immutable local content files."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else state_db.get_default_db_path()
        state_db.init_db(self.db_path)

    @property
    def content_dir(self) -> Path:
        return self.db_path.parent / "observations"

    def write_content(self, content_ref: Union[str, Path], content: bytes) -> None:
        """Create a content file exactly once; used by the store and integrity tests."""
        path = Path(content_ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(content)

    def _validate_common(self, run_id: str, source_type: str, source_ref: str) -> tuple[str, str, str]:
        run_id = str(run_id or "").strip()
        source_type = str(source_type or "").strip()
        source_ref = redact_text(str(source_ref or "").strip())
        if not run_id:
            raise ValueError("run_id is required")
        if source_type not in SOURCE_TYPES:
            raise ValueError(f"unsupported source_type: {source_type}")
        if not source_ref:
            raise ValueError("source_ref is required")
        if len(source_ref) > MAX_SOURCE_REF_CHARS:
            raise ValueError(f"source_ref exceeds {MAX_SOURCE_REF_CHARS} characters")
        return run_id, source_type, source_ref

    def _insert_or_get(
        self,
        row: Dict[str, Any],
        *,
        owned_content: Optional[Path] = None,
    ) -> Observation:
        conn = state_db.get_db_connection(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE;")
            existing = state_db.find_observation_by_dedup(
                row["run_id"], row["source_type"], row["source_ref"], row["sha256"], conn=conn,
            )
            if existing is not None:
                conn.execute("COMMIT;")
                if owned_content is not None:
                    owned_content.unlink(missing_ok=True)
                return Observation.from_mapping(existing)
            try:
                stored = state_db.insert_observation(row, conn=conn)
            except sqlite3.IntegrityError:
                existing = state_db.find_observation_by_dedup(
                    row["run_id"], row["source_type"], row["source_ref"], row["sha256"], conn=conn,
                )
                if existing is None:
                    raise
                stored = existing
            conn.execute("COMMIT;")
            if owned_content is not None and stored["observation_id"] != row["observation_id"]:
                owned_content.unlink(missing_ok=True)
            return Observation.from_mapping(stored)
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            if owned_content is not None:
                owned_content.unlink(missing_ok=True)
            raise
        finally:
            conn.close()

    def create(
        self,
        *,
        run_id: str,
        task_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
        source_type: str,
        source_ref: str,
        content: Any,
        media_type: str = "text/plain",
        metadata: Optional[Dict[str, Any]] = None,
        excerpt: Optional[str] = None,
        created_at: Optional[float] = None,
    ) -> Observation:
        run_id, source_type, source_ref = self._validate_common(run_id, source_type, source_ref)
        media_type = str(media_type or "application/octet-stream")
        metadata = _validate_metadata(metadata)
        content_bytes = _content_bytes(content, media_type)
        digest = hashlib.sha256(content_bytes).hexdigest()
        existing = state_db.find_observation_by_dedup(run_id, source_type, source_ref, digest, db_path=self.db_path)
        if existing is not None:
            return Observation.from_mapping(existing)

        observation_id = f"obs_{uuid.uuid4().hex}"
        suffix = ".json" if media_type == "application/json" else ".txt" if media_type.startswith("text/") else ".bin"
        content_ref = self.content_dir / f"{observation_id}{suffix}"
        self.write_content(content_ref, content_bytes)
        row = {
            "observation_id": observation_id,
            "run_id": run_id,
            "task_id": task_id,
            "workflow_id": workflow_id,
            "source_type": source_type,
            "source_ref": source_ref,
            "content_ref": str(content_ref),
            "media_type": media_type,
            "size_bytes": len(content_bytes),
            "sha256": digest,
            "excerpt": _excerpt(content_bytes, media_type, excerpt),
            "metadata": metadata,
            "created_at": float(created_at if created_at is not None else time.time()),
        }
        return self._insert_or_get(row, owned_content=content_ref)

    def create_external(
        self,
        path: Union[str, Path],
        *,
        run_id: str,
        source_ref: Optional[str] = None,
        artifact_kind: Optional[str] = None,
        task_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
        created_at: Optional[float] = None,
    ) -> Observation:
        artifact = Path(path).expanduser()
        if not artifact.is_file():
            raise FileNotFoundError(str(artifact))
        size_bytes, digest = _hash_file(artifact)
        media_type = mimetypes.guess_type(str(artifact))[0] or "application/octet-stream"
        metadata = {"artifact_kind": artifact_kind, "artifact_path": str(artifact)}
        return self._create_external_receipt(
            size_bytes=size_bytes,
            digest=digest,
            run_id=run_id,
            source_ref=source_ref or f"artifact:{artifact}",
            content_ref=str(artifact),
            media_type=media_type,
            metadata=metadata,
            task_id=task_id,
            workflow_id=workflow_id,
            created_at=created_at,
        )

    def _create_external_receipt(
        self,
        *,
        size_bytes: int,
        digest: str,
        run_id: str,
        source_ref: str,
        content_ref: str,
        media_type: str,
        metadata: Optional[Dict[str, Any]],
        task_id: Optional[str],
        workflow_id: Optional[str],
        created_at: Optional[float],
    ) -> Observation:
        run_id, source_type, source_ref = self._validate_common(run_id, "artifact", source_ref)
        metadata = _validate_metadata(metadata)
        row = {
            "observation_id": f"obs_{uuid.uuid4().hex}",
            "run_id": run_id,
            "task_id": task_id,
            "workflow_id": workflow_id,
            "source_type": source_type,
            "source_ref": source_ref,
            "content_ref": content_ref,
            "media_type": media_type,
            "size_bytes": size_bytes,
            "sha256": digest,
            "excerpt": None,
            "metadata": metadata,
            "created_at": float(created_at if created_at is not None else time.time()),
        }
        return self._insert_or_get(row)

    def get(self, observation_id: str) -> Optional[Observation]:
        row = state_db.get_observation(observation_id, db_path=self.db_path)
        return Observation.from_mapping(row) if row else None

    def list(self, *, run_id: Optional[str] = None, task_id: Optional[str] = None, source_type: Optional[str] = None) -> List[Observation]:
        return [
            Observation.from_mapping(row)
            for row in state_db.list_observations(run_id=run_id, task_id=task_id, source_type=source_type, db_path=self.db_path)
        ]

    def read(self, observation_id: str, offset: int = 0, limit: int = DEFAULT_READ_LIMIT, *, verify: bool = False) -> Dict[str, Any]:
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if limit < 0 or limit > MAX_READ_LIMIT:
            raise ValueError("limit must be between 0 and 64 KiB")
        observation = self.get(observation_id)
        if observation is None:
            raise KeyError(observation_id)
        with Path(observation.content_ref).open("rb") as handle:
            handle.seek(offset)
            content = handle.read(limit)
        total = Path(observation.content_ref).stat().st_size
        result = {
            "observation_id": observation_id,
            "offset": offset,
            "returned_bytes": len(content),
            "total_bytes": total,
            "truncated": offset + len(content) < total,
            "content": (
                content.decode("utf-8", errors="replace")
                if observation.media_type.startswith("text/") or observation.media_type == "application/json"
                else base64.b64encode(content).decode("ascii")
            ),
        }
        if not observation.media_type.startswith("text/") and observation.media_type != "application/json":
            result["content_encoding"] = "base64"
        if verify:
            result["integrity"] = self.verify(observation_id)
        return result

    def verify(self, observation_id: str) -> Dict[str, Any]:
        observation = self.get(observation_id)
        if observation is None:
            raise KeyError(observation_id)
        path = Path(observation.content_ref)
        if not path.is_file():
            return {"observation_id": observation_id, "valid": False, "reason": "content_missing"}
        size_bytes, digest = _hash_file(path)
        if size_bytes != observation.size_bytes:
            return {"observation_id": observation_id, "valid": False, "reason": "size_mismatch"}
        if digest != observation.sha256:
            return {"observation_id": observation_id, "valid": False, "reason": "sha256_mismatch"}
        return {"observation_id": observation_id, "valid": True}


def _store(store: Optional[ObservationStore]) -> ObservationStore:
    return store or ObservationStore()


def create_observation(*, store: Optional[ObservationStore] = None, **kwargs: Any) -> Observation:
    return _store(store).create(**kwargs)


def get_observation(observation_id: str, *, store: Optional[ObservationStore] = None) -> Optional[Observation]:
    return _store(store).get(observation_id)


def list_observations(*, run_id: Optional[str] = None, task_id: Optional[str] = None, source_type: Optional[str] = None, store: Optional[ObservationStore] = None) -> List[Observation]:
    return _store(store).list(run_id=run_id, task_id=task_id, source_type=source_type)


def read_observation(observation_id: str, offset: int = 0, limit: int = DEFAULT_READ_LIMIT, *, verify: bool = False, store: Optional[ObservationStore] = None) -> Dict[str, Any]:
    return _store(store).read(observation_id, offset=offset, limit=limit, verify=verify)


def verify_observation(observation_id: str, *, store: Optional[ObservationStore] = None) -> Dict[str, Any]:
    return _store(store).verify(observation_id)


def create_verification_observation(
    verification: Dict[str, Any], *, run_id: str, task_id: Optional[str] = None,
    workflow_id: Optional[str] = None, store: Optional[ObservationStore] = None,
) -> Observation:
    evidence_id = str(verification.get("evidence_id") or "unknown")
    payload = {key: verification.get(key) for key in (
        "passed", "passed_tests", "total_tests", "failing_count", "lint_errors", "type_errors", "evidence_id",
    ) if key in verification}
    return _store(store).create(
        run_id=run_id,
        task_id=task_id,
        workflow_id=workflow_id,
        source_type="verification",
        source_ref=f"verification:{evidence_id}",
        content=payload,
        media_type="application/json",
        metadata={"verification_id": evidence_id},
    )


def create_artifact_observation(
    path: Union[str, Path], *, run_id: str, source_ref: Optional[str] = None,
    artifact_kind: Optional[str] = None, task_id: Optional[str] = None,
    workflow_id: Optional[str] = None, store: Optional[ObservationStore] = None,
) -> Observation:
    return _store(store).create_external(
        path,
        run_id=run_id,
        source_ref=source_ref,
        artifact_kind=artifact_kind,
        task_id=task_id,
        workflow_id=workflow_id,
    )


__all__ = [
    "DEFAULT_READ_LIMIT", "MAX_READ_LIMIT", "Observation", "ObservationStore",
    "create_artifact_observation", "create_observation", "create_verification_observation",
    "get_observation", "list_observations", "read_observation", "verify_observation",
]
