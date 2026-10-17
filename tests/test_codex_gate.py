"""Unit tests for the codex-gate filter semantics (fixture-driven, no network)."""

import importlib.util
from pathlib import Path


def _load():
    spec = importlib.util.spec_from_file_location(
        "codex_gate", str(Path(__file__).resolve().parent.parent / "scripts" / "codex-gate.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _thread(login, body, resolved=False, outdated=False, path="a.py"):
    return {"id": "t", "path": path, "isResolved": resolved, "isOutdated": outdated,
            "comments": {"nodes": [{"author": {"login": login}, "body": body}]}}


def _payload(*threads):
    return {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": list(threads)}}}}}


def test_unresolved_p1_blocks():
    mod = _load()
    out = mod.find_blocking_threads(_payload(_thread("chatgpt-codex-connector[bot]", "[![P1 Badge](x)] boom")))
    assert len(out) == 1 and out[0]["path"] == "a.py"


def test_unresolved_p0_blocks():
    mod = _load()
    out = mod.find_blocking_threads(_payload(_thread("chatgpt-codex-connector[bot]", "[![P0 Badge](x)] critical")))
    assert len(out) == 1 and out[0]["path"] == "a.py"


def test_resolved_outdated_p2_other_bot_pass():
    mod = _load()
    out = mod.find_blocking_threads(_payload(
        _thread("chatgpt-codex-connector[bot]", "[![P1 Badge](x)] fixed", resolved=True),
        _thread("chatgpt-codex-connector[bot]", "[![P1 Badge](x)] stale", outdated=True),
        _thread("chatgpt-codex-connector[bot]", "[![P2 Badge](x)] minor"),
        _thread("someone-else", "[![P1 Badge](x)] noise"),
    ))
    assert out == []


def test_fail_on_any_catches_p2():
    mod = _load()
    out = mod.find_blocking_threads(
        _payload(_thread("chatgpt-codex-connector[bot]", "[![P2 Badge](x)] minor")),
        fail_on="any")
    assert len(out) == 1


def test_malformed_payload_never_blocks():
    mod = _load()
    assert mod.find_blocking_threads({}) == []
    assert mod.find_blocking_threads({"data": None}) == []


def test_fetch_graphql_errors_raise(monkeypatch):
    import json as _json
    import urllib.request as _req
    mod = _load()

    class _Resp:
        def __init__(self, payload):
            self._raw = _json.dumps(payload).encode()
        def read(self):
            return self._raw
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(_req, "urlopen",
                        lambda req, timeout=30: _Resp({"errors": [{"message": "rate limited"}]}))
    import pytest as _pt
    with _pt.raises(RuntimeError):
        mod.fetch_threads("o", "r", 1, "t")
    monkeypatch.setattr(_req, "urlopen",
                        lambda req, timeout=30: _Resp({"data": {"repository": {"pullRequest": None}}}))
    with _pt.raises(RuntimeError):
        mod.fetch_threads("o", "r", 1, "t")


def _workflow_text():
    from pathlib import Path as _P
    return (_P(__file__).resolve().parent.parent / ".github" / "workflows" / "codex-gate.yml").read_text()


def _permissions_block():
    lines = _workflow_text().splitlines()
    idx = next(i for i, l in enumerate(lines) if l.strip() == "permissions:")
    return "\n".join(lines[idx + 1:idx + 4])


def test_workflow_runs_trusted_target_event():
    text = _workflow_text()
    assert "\n  pull_request_target:\n" in text
    # No PR-context trigger may remain: any of them reopens the self-bypass.
    assert "\n  pull_request:\n" not in text
    assert "pull_request_review" not in text
    assert "base.sha" in text


def test_workflow_grants_contents_read_for_checkout():
    block = _permissions_block()
    assert "contents: read" in block
    assert "pull-requests: read" in block


def test_backlog_thread_does_not_block():
    mod = _load()
    thread_with_backlog = {
        "id": "t",
        "path": "a.py",
        "isResolved": False,
        "isOutdated": False,
        "comments": {
            "nodes": [
                {"author": {"login": "chatgpt-codex-connector[bot]"}, "body": "[![P1 Badge](x)] theoretical flaw"},
                {"author": {"login": "developer"}, "body": "Tracked in backlog: https://github.com/allinai0506/HAFlow/issues/999"},
            ]
        },
    }
    out = mod.find_blocking_threads(_payload(thread_with_backlog))
    assert out == []


def test_count_bot_reviews():
    mod = _load()
    payload = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviews": {
                        "nodes": [
                            {"author": {"login": "chatgpt-codex-connector[bot]"}},
                            {"author": {"login": "someone-else"}},
                            {"author": {"login": "chatgpt-codex-connector[bot]"}},
                        ]
                    }
                }
            }
        }
    }
    assert mod.count_bot_reviews(payload) == 2


def test_max_rounds_stop_line_passes(monkeypatch, capsys):
    mod = _load()
    payload = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviews": {
                        "nodes": [
                            {"author": {"login": "chatgpt-codex-connector[bot]"}},
                            {"author": {"login": "chatgpt-codex-connector[bot]"}},
                        ]
                    },
                    "reviewThreads": {
                        "nodes": [
                            _thread("chatgpt-codex-connector[bot]", "[![P1 Badge](x)] round 3 edge case")
                        ]
                    }
                }
            }
        }
    }
    monkeypatch.setenv("GITHUB_TOKEN", "fake_tok")
    monkeypatch.setattr(mod, "fetch_threads", lambda o, r, p, t: payload)
    rc = mod.main(["--repo-slug", "o/r", "--pr-number", "1", "--max-rounds", "2"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "PASS (exceeded max review rounds limit 2" in captured.out
    assert "[ADVISORY BACKLOG]" in captured.out


def test_whitespace_body_does_not_raise_index_error():
    mod = _load()
    thread = {
        "id": "t",
        "path": "a.py",
        "isResolved": False,
        "isOutdated": False,
        "comments": {
            "nodes": [
                {"author": {"login": "chatgpt-codex-connector[bot]"}, "body": "   \n\n   "},
                {"author": {"login": "chatgpt-codex-connector[bot]"}, "body": "[![P1 Badge](x)] flaw"},
            ]
        },
    }
    blockers = mod.find_blocking_threads(_payload(thread))
    assert len(blockers) == 1
    assert blockers[0]["path"] == "a.py"
    assert blockers[0]["title"] == ""


