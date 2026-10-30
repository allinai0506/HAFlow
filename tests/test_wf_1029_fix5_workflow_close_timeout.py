"""Regression test for wf-haflow-1029-02 Fix 5:
workflow_close scales timeout dynamically with the number of tasks in the workflow,
ensuring multi-task workflows (e.g. 28 clones) don't get terminated by a static 900s timeout,
while preserving full Git history in archive snapshots.
"""

from unittest.mock import MagicMock
from services.controller import workflow_close


def test_workflow_close_scales_timeout_for_large_workflows():
    mock_ports = MagicMock()
    # 28 tasks in workflow
    tasks = [{"task_id": f"t-{i}", "workflow_id": "wf-large"} for i in range(28)]
    mock_ports.load_tasks.return_value = tasks
    mock_ports.git_escalated_tasks.return_value = []
    mock_ports._workflow_close_inflight = set()
    mock_ports.TASK_MANAGER = "herdr-task"

    # Capture subprocess.run calls
    executed_timeouts = []

    def fake_run(cmd, text=True, capture_output=True, timeout=None):
        executed_timeouts.append(timeout)
        res = MagicMock()
        res.returncode = 0
        res.stdout = "OK"
        res.stderr = ""
        return res

    mock_ports.subprocess.run.side_effect = fake_run

    # Execute close_workflow_if_all_delivered
    # Mock threading.Thread to run target synchronously
    class SyncThread:
        def __init__(self, target, **kwargs):
            self.target = target
        def start(self):
            self.target()

    mock_ports.attention_blocks_retry.return_value = False
    mock_ports._workflow_entry.return_value = {"status": "running"}
    mock_ports.liveness.workflow_is_foreign.return_value = False
    mock_ports.git_finalize_pending_tasks.return_value = []
    mock_ports.threading.Thread = SyncThread

    workflow_close.maybe_close_completed_workflow(mock_ports, "wf-large")

    assert executed_timeouts
    # 28 tasks * 120s = 3360s, which is > 900s
    assert executed_timeouts[0] == 28 * 120
    assert executed_timeouts[0] == 3360
