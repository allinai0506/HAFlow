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
