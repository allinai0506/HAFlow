"""Prompt Ledger V1: immutable delivered text, transport evidence, and history."""

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from herdr.prompt_ledger import (
    PromptLedgerError,
    PromptTransportError,
    apply_outcome,
    apply_readiness,
    assess_tty_buffer,
    classify_transport_result,
    diff_prompts,
    get_snapshot,
    legacy_terminal_outcome,
    list_snapshots,
    open_snapshot,
    prompt_sha256,
    purge_expired,
    record_generated,
    snapshot_history,
    task_usage,
    upgrade_with_tty_buffer,
    verify_snapshot,
)
from herdr.state_store import SQLiteStateStore, get_state_store
from herdr.task_cli_parser import build_parser


def _task(task_id="t", **extra):
    task = {
        "task_id": task_id,
        "workflow_id": "wf",
        "node": "implementation",
        "run_id": "r",
        "pane_id": "p",
        "status": "rework",
        "agent": "codex",
        "agent_kind": "codex",
        "candidate_sha": "a" * 40,
        "completion_protocol": "receipt-v1",
    }
    task.update(extra)
    return task


@pytest.fixture
def store(tmp_path):
    return get_state_store(tmp_path / "state.db")


def _deliver(store, task, prompt, intervention, send):
    from herdr.supervisor_delivery import deliver
    store.save_task(task)
    return deliver(
        task, store, "RETRY",
        {"intervention_id": intervention, "action": "RETRY"},
        prompt, send,
    )


def _snapshot_for(store, task_id):
    rows = list_snapshots(store, task_id=task_id)
    assert len(rows) == 1
    return get_snapshot(store, rows[0]["prompt_id"], reveal=True)


def test_short_prompt_records_exact_text_and_does_not_claim_model_receipt(store):
    seen = []

    def send(pane, text):
        seen.append(text)
        return None

    _deliver(store, _task(), "fix the parser", "i-short", send)
    row = _snapshot_for(store, "t")
    assert row["prompt_content"] == seen[0]
    assert row["prompt_sha256"] == prompt_sha256(seen[0])
    assert verify_snapshot(store, row["prompt_id"])["ok"] is True
    assert row["dispatch_status"] == "DISPATCHED"
    assert row["delivery_evidence"]["level"] == "transport_command_accepted"
    assert row["delivery_evidence"]["model_receipt"] == "UNOBSERVABLE"
    assert row["composition"]["model_system_prompt"]["status"] == "UNOBSERVABLE"
    assert row["composition"]["agent_global_instructions"]["status"] == "UNOBSERVABLE"
    assert row["composition"]["dynamic_protocol"]["assembler"] == "herdr.supervisor_delivery.deliver"


def test_long_prompt_is_stored_whole_and_hashes_the_same_bytes(store):
    body = "LONG-" + ("x" * 200_000)
    seen = []
    _deliver(store, _task(), body, "i-long", lambda pane, text: seen.append(text))
    row = _snapshot_for(store, "t")
    assert row["prompt_content"] == seen[0]
    assert row["prompt_content"].startswith(body)
    assert len(row["prompt_content"]) > len(body)
    assert row["content_bytes"] == len(seen[0].encode("utf-8"))
    assert verify_snapshot(store, row["prompt_id"])["actual"] == prompt_sha256(seen[0])


def test_rejected_before_input_is_failed_and_timeout_is_unverified(store):
    blocked = classify_transport_result(returncode=1, stderr="error: agent_blocked")

    def send_blocked(pane, text):
        raise PromptTransportError(blocked)

    with pytest.raises(Exception) as blocked_exc:
        _deliver(store, _task(), "blocked", "i-block", send_blocked)
    assert blocked_exc.type.__name__ == "DeliveryUnknown"
    failed = _snapshot_for(store, "t")
    assert failed["dispatch_status"] == "DELIVERY_FAILED"
    assert failed["delivery_evidence"]["level"] == "rejected_before_input"
    assert failed["delivery_evidence"]["channel_bytes"] == "NONE"

    other = _task(task_id="t-timeout", run_id="r2")

    def send_timeout(pane, text):
        raise subprocess.TimeoutExpired(["herdr"], 120)

    with pytest.raises(Exception):
        _deliver(store, other, "wait", "i-timeout", send_timeout)
    timed = _snapshot_for(store, "t-timeout")
    assert timed["dispatch_status"] == "DELIVERY_UNVERIFIED"
    assert timed["delivery_evidence"]["level"] == "transport_timeout"


def test_agent_not_ready_cannot_be_marked_verified():
    accepted = classify_transport_result(returncode=0)
    verified = upgrade_with_tty_buffer(accepted, "hello", "prefix hello suffix")
    assert verified["status"] == "DELIVERY_VERIFIED"
    withheld = apply_readiness(verified, interactive_ready=False, agent_status="launch_pending")
    assert withheld["status"] == "DISPATCHED"
    assert withheld["evidence"]["verification_withheld"] == "agent_not_interactive_ready"
    assert withheld["evidence"]["model_receipt"] == "UNOBSERVABLE"
    truncated = assess_tty_buffer("hello", "prefix hello suffix", truncated=True)
    assert truncated["verified"] is False
    assert truncated["reason"] == "pane_read_truncated"
    argv = classify_transport_result(returncode=1, errno_name="E2BIG")
    assert argv["status"] == "DELIVERY_FAILED"
    assert argv["evidence"]["level"] == "argv_rejected"


def test_same_intervention_does_not_create_a_second_snapshot(store):
    def send(pane, text):
        return None

    first = _deliver(store, _task(), "once", "same", send)
    second = _deliver(store, store.get_task("t"), "once-but-ignored", "same", send)
    assert first["retry_dispatched"] is True
    assert second["already_applied"] is True
    rows = list_snapshots(store, task_id="t")
    assert len(rows) == 1
    revealed = get_snapshot(store, rows[0]["prompt_id"], reveal=True)
    assert revealed["prompt_content"].startswith("once")
    assert "once-but-ignored" not in revealed["prompt_content"]


def test_rework_rounds_keep_both_snapshots_when_text_changes(store):
    sent = []

    def send(pane, text):
        sent.append(text)

    _deliver(store, _task(), "round-one", "round-1", send)
    updated = store.get_task("t")
    _deliver(store, updated, "round-two", "round-2", send)
    rows = list_snapshots(store, task_id="t")
    assert len(rows) == 2
    first = get_snapshot(store, rows[0]["prompt_id"], reveal=True)
    second = get_snapshot(store, rows[1]["prompt_id"], reveal=True)
    assert first["prompt_content"] == sent[0]
    assert second["prompt_content"] == sent[1]
    assert first["prompt_sha256"] != second["prompt_sha256"]
    compared = diff_prompts(store, first["prompt_id"], second["prompt_id"], reveal=True)
    assert compared["equal"] is False
    assert "round-one" in compared["diff"]
    assert "round-two" in compared["diff"]
    hidden = diff_prompts(store, first["prompt_id"], second["prompt_id"], reveal=False)
    assert hidden["diff"] is None
    assert hidden["access"] == "reveal_required"


def test_interrupted_transport_keeps_the_snapshot_and_stays_unverified(store):
    from herdr import supervisor_delivery as api
    task = _task()
    store.save_task(task)

    def boundary(phase):
        if phase == "before_prompt":
            raise BaseException("crash after transport started")

    with pytest.raises(BaseException, match="crash"):
        api.deliver(task, store, "RETRY", {"intervention_id": "crash"}, "keep-me", lambda *_: pytest.fail("sent"), boundary=boundary)
    original = _snapshot_for(store, "t")
    with pytest.raises(api.DeliveryUnknown):
        api.deliver(task, store, "RETRY", {"intervention_id": "crash"}, "replacement", lambda *_: pytest.fail("resent"))
    restored = get_snapshot(store, original["prompt_id"], reveal=True)
    assert restored["prompt_content"] == original["prompt_content"]
    assert restored["prompt_content"].startswith("keep-me")
    assert "replacement" not in restored["prompt_content"]
    assert restored["dispatch_status"] == "DELIVERY_UNVERIFIED"
    assert verify_snapshot(store, restored["prompt_id"])["ok"] is True


def test_queries_usage_history_and_node_filter(store, tmp_path):
    root = tmp_path / "clone"
    root.mkdir()
    (root / "AGENTS.md").write_text("codex rules\n", encoding="utf-8")
    (root / ".agents" / "skills" / "writing-plans").mkdir(parents=True)
    (root / ".agents" / "skills" / "writing-plans" / "SKILL.md").write_text("---\nname: writing-plans\n---\n", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    (other / "CLAUDE.md").write_text("claude rules\n", encoding="utf-8")
    codex = _task(clone_path=str(root), agent="codex", agent_kind="codex", agent_name="codex")
    claude = _task(
        task_id="t-claude", run_id="r-claude", agent="claude", agent_kind="claude",
        agent_name="claude", clone_path=str(other), node="review",
    )
    _deliver(store, codex, "codex job", "codex-1", lambda *_: None)
    _deliver(store, claude, "claude job", "claude-1", lambda *_: None)
    by_node = list_snapshots(store, workflow_id="wf", node_id="review")
    assert [row["task_id"] for row in by_node] == ["t-claude"]
    usage = task_usage(store, "t")
    assert usage["snapshots"][0]["agent_kind"] == "codex"
    assert usage["snapshots"][0]["skills_digest"] not in {"", "UNKNOWN", "UNOBSERVABLE"}
    assert usage["snapshots"][0]["model_system_prompt"] == "UNOBSERVABLE"
    claude_row = _snapshot_for(store, "t-claude")
    codex_row = _snapshot_for(store, "t")
    assert codex_row["rules_digest"] != claude_row["rules_digest"]
    assert "writing-plans" in json.dumps(codex_row["composition"]["skills"])
    history = snapshot_history(store, codex_row["prompt_id"])
    assert [item["payload"]["to_status"] for item in history["transitions"]] == [
        "GENERATED", "DISPATCH_PENDING", "DISPATCHED",
    ]


def test_snapshot_is_immutable_redacted_and_purge_keeps_the_hash(store, monkeypatch):
    import herdr.prompt_ledger as ledger
    monkeypatch.setattr(ledger, "RETENTION_SECONDS", -1)
    _deliver(store, _task(), "token=api_key=supersecretvalue please", "secret", lambda *_: None)
    row = _snapshot_for(store, "t")
    public = get_snapshot(store, row["prompt_id"], reveal=False)
    assert "supersecretvalue" not in json.dumps(public)
    assert public["content_redacted"] is True
    conn = sqlite3.connect(store.db_path)
    with pytest.raises(sqlite3.Error):
        conn.execute("UPDATE prompt_snapshots SET prompt_content=? WHERE prompt_id=?", ("tampered", row["prompt_id"]))
        conn.commit()
    conn.close()
    assert verify_snapshot(store, row["prompt_id"])["ok"] is True
    assert purge_expired(store, now=row["retain_until"] + 1) == 1
    purged = verify_snapshot(store, row["prompt_id"])
    assert purged["recomputable"] is False
    assert purged["prompt_sha256"] == row["prompt_sha256"]
    assert get_snapshot(store, row["prompt_id"], reveal=True)["prompt_content"] == ""


def test_changed_text_for_one_intervention_does_not_overwrite(store):
    task = _task()
    store.save_task(task)
    conn_holder = {}
    from herdr import state_db
    conn = state_db.get_db_connection(Path(store.db_path))
    try:
        first = record_generated(conn, task=task, prompt="alpha", action="RETRY", intervention_id="once", store=store)
        with pytest.raises(PromptLedgerError):
            record_generated(conn, task=task, prompt="beta", action="RETRY", intervention_id="once", store=store)
    finally:
        conn.close()
    kept = get_snapshot(store, first["prompt_id"], reveal=True)
    assert kept["prompt_content"] == "alpha"
    conn_holder["ok"] = True


def test_ledger_outage_does_not_change_delivery(store, monkeypatch):
    import herdr.prompt_ledger as ledger
    monkeypatch.setattr(ledger, "record_generated", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("ledger down")))
    result = _deliver(store, _task(), "still-send", "outage", lambda *_: None)
    assert result["retry_dispatched"] is True
    assert list_snapshots(store, task_id="t") == []


def test_illegal_transition_and_legacy_fallback_classification(store):
    task = _task(task_id="legacy", run_id=None, status="dispatched")
    store.save_task(task)
    opened = open_snapshot(store, task, "plain", action="LEGACY", intervention_id="legacy-1")
    apply_outcome(store, opened["prompt_id"], {
        "status": "DELIVERY_VERIFIED",
        "evidence": {"level": "tty_channel_accepted", "model_receipt": "UNOBSERVABLE"},
    }, task=task)
    with pytest.raises(PromptLedgerError):
        apply_outcome(store, opened["prompt_id"], {
            "status": "DISPATCH_PENDING",
            "evidence": {"level": "none"},
        }, task=task)
    rejected = legacy_terminal_outcome(
        primary_returncode=124, timed_out=True, screen="", prompt="plain", fallback="rejected",
    )
    assert rejected["status"] == "DELIVERY_FAILED"
    accepted = legacy_terminal_outcome(
        primary_returncode=124, timed_out=True, screen="plain", prompt="plain", fallback="accepted",
        screen_truncated=False,
    )
    assert accepted["status"] == "DELIVERY_VERIFIED"
    assert accepted["evidence"]["level"] == "tty_channel_accepted"
    unproven = legacy_terminal_outcome(
        primary_returncode=0, timed_out=False, screen="plain", prompt="plain", fallback=None,
    )
    assert unproven["status"] == "DISPATCHED"
    assert unproven["evidence"]["tty_channel"] == "pane_read_truncated"
    pending = legacy_terminal_outcome(
        primary_returncode=0, timed_out=False, screen="HERDR_ORCH_TASK:legacy", prompt="plain",
        fallback=None, agent_status="idle", screen_truncated=False,
    )
    assert pending["status"] == "DISPATCHED"
    assert pending["evidence"]["tty_channel"] == "tty_echo_not_observed"


def test_parser_exposes_prompt_ledger_commands():
    parser = build_parser()
    args = parser.parse_args(["prompt-ledger", "diff", "--left", "ps_a", "--right", "ps_b", "--reveal"])
    assert args.command == "prompt-ledger"
    assert args.ledger_command == "diff"
    assert args.reveal is True


def test_dispatched_delivery_reconciles_a_snapshot_left_at_pending(store, monkeypatch):
    import herdr.prompt_ledger as ledger
    real = ledger.apply_outcome

    def fail_dispatched(store, prompt_id, outcome, *, task):
        if outcome["status"] == "DISPATCHED":
            raise RuntimeError("status write failed")
        return real(store, prompt_id, outcome, task=task)

    monkeypatch.setattr(ledger, "apply_outcome", fail_dispatched)
    calls = []
    first = _deliver(store, _task(), "kept", "i-recon", lambda *_: calls.append(1))
    assert first["retry_dispatched"] is True
    assert _snapshot_for(store, "t")["dispatch_status"] == "DISPATCH_PENDING"
    monkeypatch.setattr(ledger, "apply_outcome", real)
    second = _deliver(store, store.get_task("t"), "ignored", "i-recon", lambda *_: calls.append(1))
    assert second["already_applied"] is True
    assert calls == [1]
    restored = _snapshot_for(store, "t")
    assert restored["dispatch_status"] == "DISPATCHED"
    assert restored["prompt_content"].startswith("kept")
    assert "ignored" not in restored["prompt_content"]


def test_database_rejects_a_status_jump_to_verified(store):
    task = _task(task_id="jump")
    store.save_task(task)
    opened = open_snapshot(store, task, "alpha", action="INITIAL", intervention_id="jump-1")
    conn = sqlite3.connect(store.db_path)
    with pytest.raises(sqlite3.Error):
        conn.execute(
            "UPDATE prompt_snapshots SET dispatch_status='DELIVERY_VERIFIED' WHERE prompt_id=?",
            (opened["prompt_id"],),
        )
        conn.commit()
    conn.close()
    assert get_snapshot(store, opened["prompt_id"])["dispatch_status"] == "GENERATED"


def test_composition_skips_symlinks_and_names_the_legacy_assembler(store, tmp_path):
    root = tmp_path / "clone"
    outside = tmp_path / "outside.md"
    outside.write_text("do not follow\n", encoding="utf-8")
    root.mkdir()
    (root / "AGENTS.md").symlink_to(outside)
    (root / "RULES.md").write_text("local rules\n", encoding="utf-8")
    skill_link = root / ".agents" / "skills"
    skill_link.parent.mkdir(parents=True)
    skill_link.symlink_to(tmp_path)
    task = _task(clone_path=str(root), run_id=None)
    store.save_task(task)
    opened = open_snapshot(store, task, "legacy text", action="LEGACY", intervention_id="legacy-sym")
    row = get_snapshot(store, opened["prompt_id"], reveal=True)
    rules = row["composition"]["rules"]["files"]
    assert [item["path"] for item in rules] == ["RULES.md"]
    assert row["composition"]["skills"]["files"] == []
    assert row["composition"]["dynamic_protocol"]["assembler"] == "herdr.task_cli_execution.handle_dispatch_task"
    assert "do not follow" not in json.dumps(row["composition"])


def test_parent_symlink_is_not_followed_for_skills(store, tmp_path):
    outside = tmp_path / "outside-agents" / "skills" / "evil"
    outside.mkdir(parents=True)
    (outside / "SKILL.md").write_text("outside skill\n", encoding="utf-8")
    root = tmp_path / "clone"
    root.mkdir()
    (root / ".agents").symlink_to(tmp_path / "outside-agents")
    task = _task(clone_path=str(root), task_id="sym-parent", run_id="sym-run")
    store.save_task(task)
    opened = open_snapshot(store, task, "job", action="INITIAL", intervention_id="sym-parent")
    row = get_snapshot(store, opened["prompt_id"], reveal=True)
    assert row["composition"]["skills"]["files"] == []
    assert "evil" not in json.dumps(row["composition"])
    assert "outside skill" not in json.dumps(row["composition"])


def test_oserror_e2big_is_failed_and_does_not_resend(store):
    import errno

    def send_once(pane, text):
        raise OSError(errno.E2BIG, "Argument list too long")

    with pytest.raises(Exception) as raised:
        _deliver(store, _task(task_id="e2big", run_id="e2"), "too-big", "i-e2big", send_once)
    assert raised.type.__name__ == "DeliveryUnknown"
    row = _snapshot_for(store, "e2big")
    assert row["dispatch_status"] == "DELIVERY_FAILED"
    assert row["delivery_evidence"]["level"] == "argv_rejected"
    assert row["delivery_evidence"]["channel_bytes"] == "NONE"


def test_default_read_hides_transport_detail(store):
    task = _task(task_id="detail", run_id="detail-run")
    store.save_task(task)
    opened = open_snapshot(store, task, "body", action="INITIAL", intervention_id="detail-1")
    apply_outcome(store, opened["prompt_id"], classify_transport_result(
        returncode=1, stderr="hunter2 stayed visible",
    ), task=task)
    public = get_snapshot(store, opened["prompt_id"], reveal=False)
    assert "hunter2" not in json.dumps(public)
    assert "detail" not in public["delivery_evidence"]
    revealed = get_snapshot(store, opened["prompt_id"], reveal=True)
    assert "hunter2" in revealed["delivery_evidence"]["detail"]


def test_trigger_blocks_early_purge_delete_and_replace(store):
    task = _task(task_id="guard", run_id="guard-run")
    store.save_task(task)
    opened = open_snapshot(store, task, "keep", action="INITIAL", intervention_id="guard-1")
    conn = sqlite3.connect(store.db_path)
    try:
        with pytest.raises(sqlite3.Error):
            conn.execute(
                "UPDATE prompt_snapshots SET prompt_content='', content_purged=1 WHERE prompt_id=?",
                (opened["prompt_id"],),
            )
        conn.rollback()
        with pytest.raises(sqlite3.Error):
            conn.execute(
                "UPDATE prompt_snapshots SET delivery_evidence_json=? WHERE prompt_id=?",
                ('{"level":"forged"}', opened["prompt_id"]),
            )
        conn.rollback()
        with pytest.raises(sqlite3.Error):
            conn.execute("DELETE FROM prompt_snapshots WHERE prompt_id=?", (opened["prompt_id"],))
        conn.rollback()
        with pytest.raises(sqlite3.Error):
            conn.execute(
                """INSERT OR REPLACE INTO prompt_snapshots (
                    prompt_id, task_id, prompt_content, prompt_sha256, content_bytes,
                    created_at, dispatch_status
                ) VALUES (?,?,?,?,?,?,?)""",
                (opened["prompt_id"], "guard", "tampered", "0" * 64, 8, 1, "GENERATED"),
            )
        conn.rollback()
    finally:
        conn.close()
    kept = get_snapshot(store, opened["prompt_id"], reveal=True)
    assert kept["prompt_content"] == "keep"
    assert kept["dispatch_status"] == "GENERATED"


def test_purge_keeps_an_old_row_when_a_borderline_row_is_present(store, monkeypatch):
    import herdr.prompt_ledger as ledger
    monkeypatch.setattr(ledger, "RETENTION_SECONDS", -30)
    _deliver(store, _task(task_id="old", run_id="old"), "old-secret", "old", lambda *_: None)
    monkeypatch.setattr(ledger, "RETENTION_SECONDS", 0)
    _deliver(store, _task(task_id="edge", run_id="edge"), "edge-secret", "edge", lambda *_: None)
    assert purge_expired(store) >= 1
    old = _snapshot_for(store, "old")
    assert old["prompt_content"] == ""
    assert verify_snapshot(store, old["prompt_id"])["status"] == "HASH_RETAINED_CONTENT_PURGED"


def test_opening_the_store_replaces_a_weaker_snapshot_trigger(tmp_path):
    path = tmp_path / "state.db"
    first = SQLiteStateStore(path)
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER IF EXISTS prompt_snapshots_immutable")
    conn.execute("""
        CREATE TRIGGER prompt_snapshots_immutable
        BEFORE UPDATE ON prompt_snapshots
        BEGIN
            SELECT 1;
        END;
    """)
    conn.commit()
    conn.close()
    store = SQLiteStateStore(path)
    task = _task(task_id="refresh", run_id="refresh")
    store.save_task(task)
    opened = open_snapshot(store, task, "keep", action="INITIAL", intervention_id="refresh-1")
    conn = sqlite3.connect(path)
    with pytest.raises(sqlite3.Error):
        conn.execute(
            "UPDATE prompt_snapshots SET prompt_content='', content_purged=1 WHERE prompt_id=?",
            (opened["prompt_id"],),
        )
    conn.close()
    assert get_snapshot(store, opened["prompt_id"], reveal=True)["prompt_content"] == "keep"
    assert first.db_path == store.db_path


def test_failed_trigger_rewrite_keeps_the_previous_trigger(tmp_path):
    from herdr.state.schema import _run_exclusive
    path = tmp_path / "state.db"
    store = SQLiteStateStore(path)
    task = _task(task_id="atomic", run_id="atomic")
    store.save_task(task)
    opened = open_snapshot(store, task, "original", action="INITIAL", intervention_id="atomic-1")
    conn = sqlite3.connect(path, isolation_level=None)
    with pytest.raises(sqlite3.Error):
        _run_exclusive(conn, [
            "DROP TRIGGER IF EXISTS prompt_snapshots_immutable",
            """CREATE TRIGGER prompt_snapshots_immutable
               BEFORE UPDATE ON prompt_snapshots
               BEGIN
                   SELECT RAISE(ABORT, 'temporary');
               END;""",
            "SELECT RAISE(ABORT, 'stop before commit');",
        ])
    conn.close()
    conn = sqlite3.connect(path)
    with pytest.raises(sqlite3.Error, match="prompt snapshot is immutable"):
        conn.execute(
            "UPDATE prompt_snapshots SET prompt_content='pwned' WHERE prompt_id=?",
            (opened["prompt_id"],),
        )
    conn.close()
    assert get_snapshot(store, opened["prompt_id"], reveal=True)["prompt_content"] == "original"


def test_direct_sqlite_store_receives_the_table(tmp_path):
    store = SQLiteStateStore(tmp_path / "direct.db")
    task = _task()
    store.save_task(task)
    opened = open_snapshot(store, task, "direct", action="INITIAL", intervention_id="direct-1")
    assert verify_snapshot(store, opened["prompt_id"])["ok"] is True
