"""PR #211 R24: Experience Hub POST body admission control (P1-01).

Behavior-level regression tests for the Hub HTTP boundary:

- authentication first: an unauthenticated POST is rejected with 401 without
  touching a single body byte, at constant cost regardless of Content-Length;
- Content-Length must be present (411), exactly one pure-decimal value (400),
  and within MAX_REQUEST_BODY_BYTES (413, body left unread);
- Transfer-Encoding is unsupported (400), including TE+CL combinations;
- truncated bodies (EOF short read or socket idle timeout) are rejected with
  400 and never fall through to the ``{}`` default-action path (no default
  approve on a truncated review);
- existing submit/review/revoke semantics and the documented CL=0 empty-body
  behavior are unchanged.

Every test drives a real ``ThreadingHTTPServer`` with a real ``HubHTTPHandler``
subclass, a temporary ``HubStorage`` and real loopback sockets /
``http.client``. No source-text assertions; failures are behavioral.
"""

from __future__ import annotations

import http.client
import json
import socket
import sqlite3
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

from herdr.experience import hub_server
from herdr.experience.client import ExperienceClient
from herdr.experience.hub_server import HubHTTPHandler, HubStorage
from herdr.experience.models import (
    AppliesTo,
    BusinessRuleExperience,
    ExperienceSource,
    ExperienceStatus,
    ExperienceType,
)

# Contract value frozen by the P1-01 ruling; asserted explicitly below.
LIMIT = 1_048_576
HOST = "127.0.0.1"


class _CountingReader:
    """Wrap the real ``rfile`` and record read calls as runtime evidence."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw
        self.read_calls: list[tuple[str, int]] = []
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        data = self._raw.read(size)
        self.read_calls.append(("read", size))
        self.bytes_read += len(data or b"")
        return data

    def readline(self, size: int = -1) -> bytes:
        data = self._raw.readline(size)
        self.read_calls.append(("readline", size))
        self.bytes_read += len(data or b"")
        return data

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)


class _TrackingHubHandler(HubHTTPHandler):
    """Test-only handler; the fixture subclass owns the two registries."""

    storage: HubStorage
    read_registry: ClassVar[list[_CountingReader]] = []
    finished: ClassVar[list[_CountingReader]] = []

    def setup(self) -> None:
        super().setup()
        wrapped = _CountingReader(self.rfile)
        self.rfile = wrapped
        type(self).read_registry.append(wrapped)

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            # Completion is recorded even when closing the connection fails, so
            # the stall test observes server-side handler release, not the
            # client giving up.
            type(self).finished.append(self.rfile)


class _HubEnv:
    """A real Hub server plus helpers for one test."""

    def __init__(self, server: ThreadingHTTPServer, storage: HubStorage, handler_cls: type) -> None:
        self.server = server
        self.storage = storage
        self.handler_cls = handler_cls

    @property
    def host(self) -> str:
        return HOST

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def client(self, token: str) -> ExperienceClient:
        return ExperienceClient(base_url=self.base_url, token=token)

    def raw(self, payload: bytes, timeout: float = 5.0) -> bytes:
        return _raw_exchange(self.host, self.port, payload, timeout=timeout)

    def last_reader(self) -> _CountingReader:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if self.handler_cls.read_registry:
                return self.handler_cls.read_registry[-1]
            time.sleep(0.01)
        raise AssertionError("no connection was handled by the test server")

    def snapshot(self) -> tuple[list[dict[str, Any]], int]:
        """Full experiences rows + audit count: any write changes the tuple."""
        conn = sqlite3.connect(str(self.storage.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM experiences ORDER BY experience_id")]
            audit = conn.execute("SELECT COUNT(*) AS c FROM audit_logs").fetchone()["c"]
            return rows, audit
        finally:
            conn.close()

    def experience(self, exp_id: str) -> dict[str, Any] | None:
        conn = sqlite3.connect(str(self.storage.db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM experiences WHERE experience_id = ?", (exp_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


@pytest.fixture
def hub():
    with tempfile.TemporaryDirectory() as td:
        storage = HubStorage(Path(td) / "hub.db")
        storage.register_token("token_emp_a", user_id="emp_a", role="contributor",
                               allowed_customers=["customer_acme"])
        storage.register_token("token_emp_b", user_id="emp_b", role="contributor",
                               allowed_customers=["customer_beta"])
        storage.register_token("token_reviewer", user_id="rev_1", role="reviewer",
                               allowed_customers=["*"])
        storage.register_token("token_rev_acme", user_id="rev_acme", role="reviewer",
                               allowed_customers=["customer_acme"])
        storage.register_token("token_admin", user_id="admin_1", role="admin",
                               allowed_customers=["*"])
        handler_cls = type(
            "R24TrackingHandler",
            (_TrackingHubHandler,),
            {"storage": storage, "read_registry": [], "finished": []},
        )
        server = ThreadingHTTPServer((HOST, 0), handler_cls)
        thread = threading.Thread(target=server.serve_forever, daemon=True, name="r24-hub")
        thread.start()
        env = _HubEnv(server, storage, handler_cls)
        try:
            yield env
        finally:
            # Bounded teardown: never wait for a malicious/unfinished client;
            # request threads are daemons (ThreadingHTTPServer contract).
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# Protocol helpers
# --------------------------------------------------------------------------- #

def _request_bytes(
    path: str,
    *,
    token: str | None = None,
    headers: list[str] | None = None,
    body: bytes = b"",
) -> bytes:
    lines = [f"POST {path} HTTP/1.0", "Host: 127.0.0.1"]
    if token:
        lines.append(f"Authorization: Bearer {token}")
    lines.extend(headers or [])
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body


def _declared_request(
    path: str,
    declared_len: int,
    token: str | None,
    *,
    body: bytes = b"",
    headers: list[str] | None = None,
) -> bytes:
    return _request_bytes(
        path,
        token=token,
        headers=[f"Content-Length: {declared_len}"] + list(headers or []),
        body=body,
    )


def _raw_exchange(host: str, port: int, payload: bytes, timeout: float = 5.0) -> bytes:
    """Send raw bytes over a real socket; collect until server closes.

    A client-side timeout is an assertion failure: the server must respond and
    close within the deadline. The client giving up is never proof of anything.
    """
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    chunks: list[bytes] = []
    try:
        sock.sendall(payload)
        while True:
            try:
                chunk = sock.recv(65536)
            except TimeoutError:
                received = b"".join(chunks)[:200]
                raise AssertionError(
                    f"server did not respond and close within {timeout:.1f}s; "
                    f"received: {received!r}"
                )
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        sock.close()


def _parse_response(data: bytes) -> tuple[int, dict[str, str], dict[str, Any]]:
    head, _, body = data.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("iso-8859-1")
    status = int(status_line.split(" ")[1])
    headers: dict[str, str] = {}
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name:
            headers[name.strip().lower().decode("ascii")] = value.strip().decode("iso-8859-1")
    payload = json.loads(body.decode("utf-8")) if body.strip() else {}
    return status, headers, payload


def _http_json(
    hub_env: _HubEnv,
    method: str,
    path: str,
    token: str | None = None,
    data: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Real http.client round-trip; returns (status, parsed JSON body)."""
    headers: dict[str, str] = {}
    body: bytes | None = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection(hub_env.host, hub_env.port, timeout=8.0)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, json.loads(raw.decode("utf-8") or "{}")
    finally:
        conn.close()


def _make_candidate(
    exp_id: str,
    customer: str = "customer_acme",
    content: str = "报价必须打9折",
) -> BusinessRuleExperience:
    return BusinessRuleExperience(
        experience_id=exp_id,
        type=ExperienceType.BUSINESS_RULE.value,
        title=f"{customer} 报价规则",
        content=content,
        contributor="emp_a",
        source=ExperienceSource(task_id="t1", run_id="r1"),
        applies_to=AppliesTo(customer=customer, task_type="quote"),
        status=ExperienceStatus.PENDING_REVIEW.value,
    )


def _candidate_json(exp_id: str, customer: str = "customer_acme", content: str = "报价必须打9折") -> dict[str, Any]:
    return _make_candidate(exp_id, customer=customer, content=content).to_dict()


def _padded_json(payload: dict[str, Any], size: int) -> bytes:
    """Serialise ``payload`` and pad with spaces so its byte length is exact."""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(raw) <= size, f"payload minimum {len(raw)} bytes exceeds requested {size}"
    return raw + b" " * (size - len(raw))


def _submit_pending(hub_env: _HubEnv, exp_id: str, token: str = "token_emp_a") -> None:
    result = hub_env.client(token).submit_candidate(_make_candidate(exp_id))
    assert result.get("ok") is True, result


# --------------------------------------------------------------------------- #
# 1. Authentication before body: 401 at zero body cost
# --------------------------------------------------------------------------- #

def test_max_request_body_bytes_exported_and_frozen():
    assert hub_server.MAX_REQUEST_BODY_BYTES == LIMIT


def test_unauthenticated_huge_body_is_401_without_body_read(hub):
    head = _request_bytes(
        "/api/v1/experiences/candidates",
        headers=[f"Content-Length: {LIMIT * 1000}"],
    )
    before = hub.snapshot()

    data = hub.raw(head, timeout=3.0)

    status, headers, payload = _parse_response(data)
    assert status == 401
    assert payload["error"] == "unauthorized"
    assert headers.get("connection") == "close"

    reader = hub.last_reader()
    # Zero body cost: everything consumed was the request head we sent, and no
    # bounded body-read call was ever issued.
    assert reader.bytes_read == len(head)
    assert [call for call in reader.read_calls if call[0] == "read"] == []

    # The read loop in _raw_exchange only returns on socket EOF: the server
    # closed the connection by itself.
    assert hub.snapshot() == before


# --------------------------------------------------------------------------- #
# 2. Byte-budget boundary across submit / review / revoke
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("delta,exp_id", [(-1, "exp_r24_sub_below"), (0, "exp_r24_sub_exact")])
def test_submit_at_and_below_limit_is_accepted(hub, delta, exp_id):
    size = LIMIT + delta
    body = _padded_json(_candidate_json(exp_id), size)
    data = hub.raw(
        _declared_request("/api/v1/experiences/candidates", size, "token_emp_a", body=body),
        timeout=15.0,
    )
    status, _, payload = _parse_response(data)
    assert status == 201
    assert payload["experience_id"] == exp_id
    assert payload["reason"] == "created"
    assert set(payload) == {"ok", "experience_id", "status", "reason", "experience"}


@pytest.mark.parametrize("delta,exp_id", [(-1, "exp_r24_rev_below"), (0, "exp_r24_rev_exact")])
def test_review_at_and_below_limit_is_accepted(hub, delta, exp_id):
    _submit_pending(hub, exp_id)
    size = LIMIT + delta
    body = _padded_json({"action": "approve"}, size)
    data = hub.raw(
        _declared_request(f"/api/v1/experiences/{exp_id}/review", size, "token_reviewer", body=body),
        timeout=15.0,
    )
    status, _, payload = _parse_response(data)
    assert status == 200
    assert payload["status"] == "published"
    assert hub.experience(exp_id)["status"] == "published"


@pytest.mark.parametrize("delta,exp_id", [(-1, "exp_r24_rv_below"), (0, "exp_r24_rv_exact")])
def test_revoke_at_and_below_limit_is_accepted(hub, delta, exp_id):
    _submit_pending(hub, exp_id)
    assert hub.client("token_admin").review_candidate(exp_id, action="approve").get("ok") is True
    size = LIMIT + delta
    body = _padded_json({"reason": "政策到期撤回"}, size)
    data = hub.raw(
        _declared_request(f"/api/v1/experiences/{exp_id}/revoke", size, "token_reviewer", body=body),
        timeout=15.0,
    )
    status, _, payload = _parse_response(data)
    assert status == 200
    assert payload["status"] == "revoked"
    assert hub.experience(exp_id)["status"] == "revoked"


@pytest.mark.parametrize("kind", ["submit", "review", "revoke"])
def test_over_limit_is_413_without_read_and_without_writes(hub, kind):
    if kind == "submit":
        path, token = "/api/v1/experiences/candidates", "token_emp_a"
    else:
        exp_id = f"exp_r24_over_{kind}"
        _submit_pending(hub, exp_id)
        if kind == "revoke":
            assert hub.client("token_admin").review_candidate(exp_id, action="approve").get("ok") is True
        path, token = f"/api/v1/experiences/{exp_id}/{kind}", "token_reviewer"

    before = hub.snapshot()
    # Header only: the over-limit rejection must not require (or consume) any
    # body byte, hence the short client deadline.
    data = hub.raw(_declared_request(path, LIMIT + 1, token), timeout=5.0)

    status, headers, payload = _parse_response(data)
    assert status == 413
    assert payload["error"] == "payload_too_large"
    assert headers.get("connection") == "close"
    assert hub.snapshot() == before


# --------------------------------------------------------------------------- #
# 3. Content-Length syntax
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "raw_value",
    ["abc", "-1", "1e3", "+5", "10.5", "1 0", "010", "12345678901234567890"],
    ids=["alpha", "negative", "exponent", "plus_sign", "fraction", "inner_space",
         "leading_zero", "overlong"],
)
def test_malformed_content_length_is_400_and_server_survives(hub, raw_value):
    head = _request_bytes(
        "/api/v1/experiences/candidates",
        token="token_emp_a",
        headers=[f"Content-Length: {raw_value}"],
    )
    data = hub.raw(head, timeout=3.0)

    status, headers, payload = _parse_response(data)
    assert status == 400
    assert payload["error"] == "bad_request"
    assert headers.get("connection") == "close"

    # Server survives: a fresh, well-formed request still succeeds.
    health_status, health = _http_json(hub, "GET", "/api/v1/health")
    assert health_status == 200
    assert health["status"] == "ok"


def test_duplicate_conflicting_content_length_is_400_without_side_effects(hub):
    exp_id = "exp_r24_dup_conflict"
    _submit_pending(hub, exp_id)
    before = hub.snapshot()

    request = _request_bytes(
        f"/api/v1/experiences/{exp_id}/review",
        token="token_reviewer",
        # First value short (which a first-value reader would use), second
        # value over the limit: classic request-smuggling shape.
        headers=["Content-Length: 2", f"Content-Length: {LIMIT + 1}"],
        body=b"{}",
    )
    data = hub.raw(request, timeout=3.0)

    status, headers, payload = _parse_response(data)
    assert status == 400
    assert payload["error"] == "bad_request"
    assert headers.get("connection") == "close"
    assert hub.experience(exp_id)["status"] == "pending_review"
    assert hub.snapshot() == before


def test_duplicate_identical_content_length_is_400(hub):
    exp_id = "exp_r24_dup_identical"
    _submit_pending(hub, exp_id)
    before = hub.snapshot()

    request = _request_bytes(
        f"/api/v1/experiences/{exp_id}/review",
        token="token_reviewer",
        headers=["Content-Length: 2", "Content-Length: 2"],
        body=b"{}",
    )
    data = hub.raw(request, timeout=3.0)

    status, _, payload = _parse_response(data)
    assert status == 400
    assert payload["error"] == "bad_request"
    assert hub.experience(exp_id)["status"] == "pending_review"
    assert hub.snapshot() == before


# --------------------------------------------------------------------------- #
# 4. Framing contract: missing length and Transfer-Encoding
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "case,extra_headers,body,expected_status,expected_error",
    [
        ("no_length_no_body", [], b"", 411, "length_required"),
        ("no_length_with_body", [], b'{"action": "approve"}', 411, "length_required"),
        ("chunked", ["Transfer-Encoding: chunked"], b"2\r\n{}\r\n0\r\n\r\n", 400, "bad_request"),
        ("chunked_with_length", ["Transfer-Encoding: chunked", "Content-Length: 2"], b"{}", 400, "bad_request"),
    ],
    ids=["no_length_no_body", "no_length_with_body", "chunked", "chunked_with_length"],
)
def test_framing_rejections_do_not_touch_business_state(
    hub, case, extra_headers, body, expected_status, expected_error,
):
    exp_id = f"exp_r24_frame_{case}"
    _submit_pending(hub, exp_id)
    before = hub.snapshot()

    request = _request_bytes(
        f"/api/v1/experiences/{exp_id}/review",
        token="token_reviewer",
        headers=extra_headers,
        body=body,
    )
    data = hub.raw(request, timeout=3.0)

    status, headers, payload = _parse_response(data)
    assert status == expected_status
    assert payload["error"] == expected_error
    assert headers.get("connection") == "close"
    assert hub.experience(exp_id)["status"] == "pending_review"
    assert hub.snapshot() == before


# --------------------------------------------------------------------------- #
# 5. Declared boundary: trailing pseudo-request bytes are never executed
# --------------------------------------------------------------------------- #

def test_declared_short_body_honours_boundary_and_drops_trailing_request(hub):
    first, target = "exp_r24_boundary_first", "exp_r24_boundary_target"
    _submit_pending(hub, first)
    _submit_pending(hub, target)

    declared_body = json.dumps({"action": "junk"}, separators=(",", ":")).encode("utf-8")
    pseudo = _request_bytes(
        f"/api/v1/experiences/{target}/review",
        token="token_reviewer",
        headers=["Content-Length: 2"],
        body=b"{}",
    )
    request = _request_bytes(
        f"/api/v1/experiences/{first}/review",
        token="token_reviewer",
        headers=[f"Content-Length: {len(declared_body)}"],
        body=declared_body + pseudo,
    )

    data = hub.raw(request, timeout=5.0)

    # Exactly one HTTP response; the trailing pseudo request was never executed.
    assert data.count(b"HTTP/1.0 ") == 1
    status, _, payload = _parse_response(data)
    assert status == 400
    assert payload["error"] == "invalid_action"
    assert hub.experience(first)["status"] == "pending_review"
    assert hub.experience(target)["status"] == "pending_review"

    # Positive boundary: a complete declared body is processed normally and the
    # trailing pseudo request is still dropped when the connection closes.
    first2, target2 = "exp_r24_boundary_first2", "exp_r24_boundary_target2"
    _submit_pending(hub, first2)
    _submit_pending(hub, target2)
    pseudo2 = _request_bytes(
        f"/api/v1/experiences/{target2}/review",
        token="token_reviewer",
        headers=["Content-Length: 2"],
        body=b"{}",
    )
    request2 = _request_bytes(
        f"/api/v1/experiences/{first2}/review",
        token="token_reviewer",
        headers=["Content-Length: 2"],
        body=b"{}" + pseudo2,
    )
    data2 = hub.raw(request2, timeout=5.0)
    assert data2.count(b"HTTP/1.0 ") == 1
    status2, _, payload2 = _parse_response(data2)
    assert status2 == 200
    assert payload2["status"] == "published"
    assert hub.experience(first2)["status"] == "published"
    assert hub.experience(target2)["status"] == "pending_review"


# --------------------------------------------------------------------------- #
# 6. Truncated bodies: EOF short read and idle timeout
# --------------------------------------------------------------------------- #

def test_truncated_body_eof_is_400_without_default_approve(hub):
    exp_id = "exp_r24_truncated"
    _submit_pending(hub, exp_id)
    before = hub.snapshot()

    declared = 200
    partial = b'{"action": "app'
    request = _declared_request(
        f"/api/v1/experiences/{exp_id}/review", declared, "token_reviewer", body=partial)

    sock = socket.create_connection((hub.host, hub.port), timeout=5.0)
    sock.settimeout(5.0)
    chunks: list[bytes] = []
    try:
        sock.sendall(request)
        sock.shutdown(socket.SHUT_WR)  # EOF: no further body byte will ever arrive
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        sock.close()

    status, headers, payload = _parse_response(b"".join(chunks))
    assert status == 400
    assert payload["error"] == "bad_request"
    assert headers.get("connection") == "close"
    # No default approve: the truncated review body must not act as ``{}``.
    assert hub.experience(exp_id)["status"] == "pending_review"
    assert hub.snapshot() == before


def test_stalled_body_releases_handler_within_idle_timeout(hub):
    exp_id = "exp_r24_stall"
    _submit_pending(hub, exp_id)
    before = hub.snapshot()

    declared = 50
    partial = b'{"action"'
    request = _declared_request(
        f"/api/v1/experiences/{exp_id}/review", declared, "token_reviewer", body=partial)

    sock = socket.create_connection((hub.host, hub.port), timeout=18.0)
    sock.settimeout(18.0)
    chunks: list[bytes] = []
    try:
        sock.sendall(request)
        stalled_reader = hub.last_reader()
        started = time.monotonic()
        while True:
            try:
                chunk = sock.recv(65536)
            except TimeoutError:
                raise AssertionError(
                    "client-side timeout: the server never released the stalled body read")
            if not chunk:
                break
            chunks.append(chunk)
        elapsed = time.monotonic() - started
    finally:
        sock.close()

    status, _, payload = _parse_response(b"".join(chunks))
    assert status == 400
    assert payload["error"] == "bad_request"
    # The response arrived after the handler's own idle timeout, but well
    # before our client deadline: this is server-side release, not the client
    # giving up.
    assert 9.0 <= elapsed <= 15.0, f"idle timeout not enforced; elapsed={elapsed:.2f}s"

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and stalled_reader not in hub.handler_cls.finished:
        time.sleep(0.05)
    assert stalled_reader in hub.handler_cls.finished, "handler thread did not finish"

    # No side effects from the stalled request and the server keeps serving.
    assert hub.experience(exp_id)["status"] == "pending_review"
    assert hub.snapshot() == before
    health_status, _ = _http_json(hub, "GET", "/api/v1/health")
    assert health_status == 200
    created_status, created = _http_json(
        hub, "POST", "/api/v1/experiences/candidates",
        token="token_emp_a", data=_candidate_json("exp_r24_stall_after"))
    assert created_status == 201
    assert created["reason"] == "created"


# --------------------------------------------------------------------------- #
# 7. Existing POST semantics unchanged
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Non-object JSON bodies: explicit 400 instead of an empty response
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw_body", [b"null", b"[]", b'"text"', b"123", b"true"])
def test_non_object_json_body_is_400_invalid_payload(hub, raw_body):
    """Syntactically valid JSON that is not an object must get an explicit 400.

    ``null`` used to decode to ``None``, which was indistinguishable from the
    internal "rejection already sent" sentinel: the handler returned without
    sending anything and the client observed a dropped connection. Every
    non-object JSON value must now produce the same ``invalid_payload``
    rejection and leave business state untouched.
    """
    exp_id = "exp_r24_non_object"
    _submit_pending(hub, exp_id)
    before = hub.snapshot()

    head = _request_bytes(
        "/api/v1/experiences/candidates",
        token="token_emp_a",
        headers=[f"Content-Length: {len(raw_body)}"],
        body=raw_body,
    )
    data = hub.raw(head, timeout=3.0)

    status, headers, payload = _parse_response(data)
    assert status == 400
    assert payload["error"] == "invalid_payload"
    assert headers.get("connection") == "close"
    assert hub.snapshot() == before

    # The server survives; later requests on the same routes still work.
    health_status, health = _http_json(hub, "GET", "/api/v1/health")
    assert health_status == 200
    assert health["status"] == "ok"

    review_status, review = _http_json(
        hub, "POST", f"/api/v1/experiences/{exp_id}/review",
        token="token_reviewer", data={"action": "approve"})
    assert review_status == 200
    assert review["status"] == "published"


def test_existing_post_semantics_unchanged(hub):
    health_status, health = _http_json(hub, "GET", "/api/v1/health")
    assert health_status == 200
    assert health["status"] == "ok"

    # submit -> 201 with the baseline response structure
    submit_status, created = _http_json(
        hub, "POST", "/api/v1/experiences/candidates",
        token="token_emp_a", data=_candidate_json("exp_r24_keep_1"))
    assert submit_status == 201
    assert set(created) == {"ok", "experience_id", "status", "reason", "experience"}
    assert created["ok"] is True
    assert created["reason"] == "created"
    assert created["status"] == "pending_review"

    # idempotent resubmit -> 200 already_exists
    again_status, again = _http_json(
        hub, "POST", "/api/v1/experiences/candidates",
        token="token_emp_a", data=_candidate_json("exp_r24_keep_1"))
    assert again_status == 200
    assert again["reason"] == "already_exists"

    # same id with different content -> 409 conflict
    conflict_status, conflict = _http_json(
        hub, "POST", "/api/v1/experiences/candidates",
        token="token_emp_a", data=_candidate_json("exp_r24_keep_1", content="变更后的内容"))
    assert conflict_status == 409
    assert conflict["error"] == "conflict"

    # contributor cannot approve -> 403
    forbidden_status, _ = _http_json(
        hub, "POST", "/api/v1/experiences/exp_r24_keep_1/review",
        token="token_emp_a", data={"action": "approve"})
    assert forbidden_status == 403

    # reviewer approves -> 200 published with the baseline structure
    approve_status, approved = _http_json(
        hub, "POST", "/api/v1/experiences/exp_r24_keep_1/review",
        token="token_reviewer", data={"action": "approve", "comment": "合规"})
    assert approve_status == 200
    assert set(approved) == {"ok", "experience_id", "status", "experience"}
    assert approved["status"] == "published"

    # reject path still works
    _submit_pending(hub, "exp_r24_keep_2")
    reject_status, rejected = _http_json(
        hub, "POST", "/api/v1/experiences/exp_r24_keep_2/review",
        token="token_reviewer", data={"action": "reject", "comment": "不适用"})
    assert reject_status == 200
    assert rejected["status"] == "rejected"

    # revoke published rule -> 200 revoked with the baseline structure
    revoke_status, revoked = _http_json(
        hub, "POST", "/api/v1/experiences/exp_r24_keep_1/revoke",
        token="token_reviewer", data={"reason": "政策到期"})
    assert revoke_status == 200
    assert set(revoked) == {"ok", "experience_id", "status", "experience"}
    assert revoked["status"] == "revoked"

    # cross-customer matrix
    beta_status, beta = _http_json(
        hub, "POST", "/api/v1/experiences/candidates",
        token="token_emp_b", data=_candidate_json("exp_r24_keep_beta", customer="customer_beta"))
    assert beta_status == 201
    assert beta["ok"] is True
    cross_review_status, _ = _http_json(
        hub, "POST", "/api/v1/experiences/exp_r24_keep_beta/review",
        token="token_rev_acme", data={"action": "approve"})
    assert cross_review_status == 403
    cross_submit_status, _ = _http_json(
        hub, "POST", "/api/v1/experiences/candidates",
        token="token_emp_a", data=_candidate_json("exp_r24_keep_cross", customer="customer_beta"))
    assert cross_submit_status == 403

    # CL=0 empty body keeps the documented semantics: {} default action.
    _submit_pending(hub, "exp_r24_keep_zero")
    zero_review = hub.raw(_request_bytes(
        "/api/v1/experiences/exp_r24_keep_zero/review",
        token="token_reviewer",
        headers=["Content-Length: 0"],
    ), timeout=5.0)
    zero_status, _, zero_payload = _parse_response(zero_review)
    assert zero_status == 200
    assert zero_payload["status"] == "published"  # review default action is approve (unchanged)

    zero_submit = hub.raw(_request_bytes(
        "/api/v1/experiences/candidates",
        token="token_emp_a",
        headers=["Content-Length: 0"],
    ), timeout=5.0)
    zero_submit_status, _, zero_submit_payload = _parse_response(zero_submit)
    assert zero_submit_status == 400
    assert zero_submit_payload["error"] == "invalid_payload"
