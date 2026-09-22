from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from herdr import state_db
from herdr.trajectory import TrajectoryLedger


ROOT = Path(__file__).resolve().parent.parent


def test_metrics_cli_json_reads_one_run(tmp_path: Path):
    db_path = tmp_path / "state.db"
    state_db.save_task({
        "task_id": "task-cli",
        "run_id": "run-cli",
        "workflow_id": "wf-cli",
        "status": "working",
        "created_at": 10.0,
    }, db_path=db_path)
    TrajectoryLedger(db_path).append_event({
        "run_id": "run-cli",
        "task_id": "task-cli",
        "workflow_id": "wf-cli",
        "event_type": "run_started",
        "timestamp": 10.0,
    })

    env = os.environ.copy()
    env["HERDR_STATE_DB"] = str(db_path)
    result = subprocess.run(
        [sys.executable, "bin/herdr-task", "metrics", "--run-id", "run-cli", "--json"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["run_id"] == "run-cli"
    assert payload["task_completed"] is False
    assert payload["trajectory_events"] == 1
    assert payload["metadata"]["model_usage"] == "unsupported"


def test_metrics_cli_rejects_missing_run_id():
    result = subprocess.run(
        [sys.executable, "bin/herdr-task", "metrics", "--run-id", ""],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "run_id" in result.stderr
