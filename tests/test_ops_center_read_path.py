"""Read-only ops requests must not initialize state or repeat per-card reads."""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from herdr import state_db


def seed(path, suffix="a"):
    state_db.init_db(path)
    for i in range(8):
        wid = f"wf-{suffix}-{i}"
        state_db.save_workflow({"workflow_id": wid, "title": wid, "created_at": 1,
            "config": {"name": wid, "nodes": [{"id": "plan"}]}}, db_path=path)
        state_db.save_task({"task_id": f"t-{suffix}-{i}", "workflow_id": wid,
            "node": "plan", "agent": "codex", "status": "working",
            "pane_id": "shared", "created_at": 1}, db_path=path)


def test_ops_reads_single_snapshot_and_deduplicates_probes(tmp_path):
    from herdr.ops_center import build_ops_center_payload
    db = tmp_path / "state.db"
    seed(db)
    statements = []
    original = state_db.get_readonly_db_connection
    def connect(path=None):
        conn = original(path)
        conn.set_trace_callback(statements.append)
        return conn
    with patch.object(state_db, "get_readonly_db_connection", side_effect=connect), \
         patch.object(state_db, "init_db", side_effect=AssertionError("DDL on read")), \
         patch("herdr.state_store.get_state_store", side_effect=AssertionError("store on read")), \
         patch("herdr.ops_center._default_runtime_probe", return_value="working") as probe:
        result = build_ops_center_payload(db_path=db, now=100)
    assert len(result["workflow_cards"]) == 8
    assert result["count"] == 8
    assert probe.call_count == 1
    assert sum(s.startswith("SELECT * FROM tasks") for s in statements) == 1
    assert sum(s.startswith("SELECT * FROM workflows") for s in statements) == 1
    assert not any("WHERE workflow_id" in s for s in statements)


def test_ops_concurrent_scopes_do_not_share_snapshot(tmp_path):
    from herdr.ops_center import build_ops_center_payload
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    seed(a, "a")
    seed(b, "b")
    def read(pair):
        db, scope = pair
        return build_ops_center_payload(scope, db_path=db, runtime_probe=lambda _: "working")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(read, [(a, "wf-a-0"), (b, "wf-b-0")]))
    assert [r["workflow_cards"][0]["workflow_id"] for r in results] == ["wf-a-0", "wf-b-0"]


def test_ops_missing_db_does_not_create_it(tmp_path):
    from herdr.ops_center import build_ops_center_payload
    path = tmp_path / "missing" / "state.db"
    with pytest.raises(FileNotFoundError):
        build_ops_center_payload(db_path=path)
    assert not path.parent.exists()


def test_ops_runtime_budget_and_unknown_are_visible(tmp_path):
    from herdr import ops_center
    db = tmp_path / "state.db"
    seed(db)
    for i in range(40):
        state_db.save_task({"task_id": f"extra-{i}", "workflow_id": "wf-a-0",
            "node": "plan", "agent": "codex", "status": "working",
            "pane_id": f"pane-{i}", "created_at": 1}, db_path=db)
    calls = []
    def probe(pane):
        calls.append(pane)
        return "working"
    result = ops_center.build_ops_center_payload(db_path=db, now=100, runtime_probe=probe)
    assert len(calls) == ops_center.MAX_RUNTIME_PROBES
    assert len(set(calls)) == len(calls)
    # An omitted pane is unknown and still retains recovery visibility.
    assert any(a["kind"] == "CONTROLLER_RECOVERY" for a in result["anomalies"])


def test_ops_required_task_out_of_scope_remains_blocked(tmp_path):
    from herdr.ops_center import build_ops_center_payload
    db = tmp_path / "state.db"
    seed(db)
    state_db.save_workflow({"workflow_id": "wf-a-0", "config": {"nodes": [
        {"id": "plan", "required_task_ids": ["t-a-1"]}]}}, db_path=db)
    result = build_ops_center_payload("wf-a-0", db_path=db, runtime_probe=lambda _: "working")
    node = result["workflow_cards"][0]["nodes"][0]
    assert node["status"] != "completed"
    assert node["completion_issues"] == [{"task_id": None, "reason": "workflow_config_invalid"}]


def test_ops_cli_handler_uses_same_readonly_api(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from herdr.ops_center import _helpers
    db = tmp_path / "state.db"
    seed(db)
    monkeypatch.setenv("HERDR_STATE_DB", str(db))
    with patch.object(state_db, "init_db", side_effect=AssertionError("DDL on read")), \
         patch("herdr.ops_center._default_runtime_probe", return_value="working"):
        _helpers().ops_center(SimpleNamespace(workflow_id="wf-a-0", include_tasks=True))
    import json
    result = json.loads(capsys.readouterr().out)
    assert result["scope"] == "wf-a-0"
    assert result["count"] == 1
    assert result["task_summaries"][0]["task_id"] == "t-a-0"


def test_ops_runtime_admission_is_shared_across_requests(tmp_path):
    import threading
    from herdr import ops_center
    db = tmp_path / "state.db"
    seed(db)
    for i in range(8):
        state_db.save_task({"task_id": f"active-{i}", "workflow_id": "wf-a-0",
            "node": "plan", "agent": "codex", "status": "working",
            "pane_id": f"p-{i}", "created_at": 1}, db_path=db)
    entered = threading.Barrier(9)
    release = threading.Event()
    def blocking_probe(pane):
        entered.wait(timeout=5)
        assert release.wait(timeout=5)
        return "working"
    with ThreadPoolExecutor(max_workers=1) as callers:
        first = callers.submit(ops_center.build_ops_center_payload, db_path=db,
            runtime_probe=blocking_probe)
        entered.wait(timeout=5)
        try:
            with patch("herdr.ops_center._default_runtime_probe", side_effect=AssertionError("unbounded probe")):
                second = ops_center.build_ops_center_payload(db_path=db)
            assert second["runtime_sampling"] == {"sampled": 0, "omitted": 9}
        finally:
            release.set()
        assert first.result(timeout=5)["runtime_sampling"] == {"sampled": 8, "omitted": 1}


def test_inventory_candidates_match_current_epoch_and_workflow_identity(tmp_path):
    from herdr.console_queries import read_inventory
    from herdr.recovery_store import read_snapshot
    db = tmp_path / "state.db"
    seed(db)
    state_db.save_workflow({"workflow_id": "wf-a-0", "created_at": 10,
        "reopened_at": 100, "candidate_sha": "stale-metadata"}, db_path=db)
    state_db.save_workflow({"workflow_id": "wf-a-1", "created_at": 10,
        "reopened_at": 500, "candidate_sha": "stale-other"}, db_path=db)
    for wid, timestamp, source, sha in [
        ("wf-a-0", 90, "critical-path-scheduler", "previous-epoch"),
        ("wf-a-0", 110, "critical-path-scheduler", "current"),
        ("wf-a-0", 120, "foreign-source", "wrong-source"),
        ("wf-a-1", 200, "critical-path-scheduler", "other-workflow-old")]:
        state_db.record_event({"workflow_id": wid, "timestamp": timestamp,
            "source": source, "event_type": "candidate_frozen", "payload": {"candidate_sha": sha}}, db_path=db)
    tasks, workflows = read_inventory(db)
    assert len(tasks) == 8
    assert workflows["wf-a-0"]["candidate_sha"] == "current"
    assert workflows["wf-a-1"]["candidate_sha"] is None
    for wid in ["wf-a-0", "wf-a-1"]:
        authoritative = read_snapshot(db, wid)[0]
        assert workflows[wid]["candidate_sha"] == authoritative["candidate_sha"]
        assert workflows[wid]["candidate_episode_id"] == authoritative["candidate_episode_id"]


@pytest.mark.parametrize("metadata,expected", [
    ({"title": "explicit", "requirement_subject": "subject", "requirement": "request"}, "explicit"),
    ({"requirement_subject": "subject", "requirement": "request"}, "subject"),
    ({"requirement": "## 需求\nrequest"}, "request"),
    ({}, "config-name"),
])
def test_ops_preserves_console_subject_label(tmp_path, metadata, expected):
    from herdr.ops_center import build_ops_center_payload
    db = tmp_path / "state.db"
    seed(db)
    state_db.save_workflow({"workflow_id": "wf-a-0", "config": {
        "name": "config-name", "nodes": [{"id": "plan"}]}, **metadata}, db_path=db)
    result = build_ops_center_payload("wf-a-0", db_path=db, runtime_probe=lambda _: "working")
    assert result["workflow_cards"][0]["workflow_label"] == expected


def test_cli_ops_preserves_explicit_task_database_precedence(tmp_path, monkeypatch, capsys):
    import json
    from types import SimpleNamespace
    from herdr.ops_center import _helpers
    cli = _helpers()
    selected = tmp_path / 'selected'
    fallback = tmp_path / 'fallback'
    selected.mkdir()
    fallback.mkdir()
    seed(selected / 'state.db', 'selected')
    seed(fallback / 'state.db', 'fallback')
    monkeypatch.delenv('HERDR_STATE_DB', raising=False)
    monkeypatch.setenv('CHECKPOINTS_DIR', str(fallback / 'checkpoints'))
    monkeypatch.setattr(cli, 'TASKS_FILE', str(selected / 'tasks.json'))
    with patch('herdr.ops_center._default_runtime_probe', return_value='working'):
        cli.ops_center(SimpleNamespace(workflow_id=None, include_tasks=False))
    payload = json.loads(capsys.readouterr().out)
    assert {c['workflow_id'] for c in payload['workflow_cards']} == {f'wf-selected-{i}' for i in range(8)}


def test_invalid_external_workflow_is_isolated_from_valid_cards(tmp_path):
    from herdr.ops_center import build_ops_center_payload
    db = tmp_path / 'state.db'
    seed(db)
    external = tmp_path / 'invalid.json'
    external.write_text('{"nodes":["bad"]}')
    state_db.save_workflow({'workflow_id': 'wf-a-0', 'workflow_file': str(external)}, db_path=db)
    result = build_ops_center_payload(db_path=db, runtime_probe=lambda _: 'working')
    assert len(result['workflow_cards']) == 8
    assert any(c['workflow_id'] == 'wf-a-1' for c in result['workflow_cards'])
    invalid = next(c for c in result['workflow_cards'] if c['workflow_id'] == 'wf-a-0')
    assert invalid['nodes'][0]['completion_issues'] == [{'task_id': None, 'reason': 'workflow_config_invalid'}]
