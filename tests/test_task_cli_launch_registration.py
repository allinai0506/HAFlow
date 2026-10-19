from types import SimpleNamespace

from herdr.task_cli_launch_registration import (
    LaunchRegistration,
    register_launched_task,
)


class Store:
    db_path = ':memory:'

    def __init__(self):
        self.saved = []
        self.events = []

    def save_task(self, task):
        self.saved.append(task)


def test_register_launched_task_persists_context_and_identity_fields(monkeypatch):
    args = SimpleNamespace(
        task_id='task-1', workflow_id='wf-1', integration_mode='context',
        artifact_mode='shared_artifacts', task_type='code', goal='ship it',
        acceptance=['works'], customer=' Acme ', business_task_type='invoice',
        mandatory_experience_rules=True, replay_of='task-old', candidate_sha='sha-1',
        dispatch_role='worker', dispatch_round=1,
    )
    context = LaunchRegistration(
        args=args, now=100.0, node={'node_type': 'agent'},
        node_id='build', run_id='run-1', project={'project_id': 'p', 'execution_mode': 'git'},
        exec_fields={'execution_mode': 'context', 'context': {'ref': 'ctx-1'}},
        source_repo='/source', base_branch='main', stage_config={'tab_id': 'tab'},
        workspace_id='workspace', pane_id='pane', pane_source='dynamic',
        resolved_agent='codex', requested_agent='auto', clone_path='/clone', branch='agent/task',
        baseline_commit='base', onto_branch='feature', baseline_untracked=[],
        baseline_fingerprint={'tracked': {}, 'untracked': {}}, recorded_onto='feature',
        launch_intent={'intent_id': 'intent-1', 'dispatch_operation_id': 'op-1',
                       'supersedes': 'task-old'}, worker_result={'agent_session_id': 'session'},
        predecessor=None, committed_recovery=False,
    )
    store = Store()
    calls = []
    finished = []
    monkeypatch.setattr('herdr.task_resources.finish_launch_intent',
                        lambda store, intent: finished.append(intent))

    task = register_launched_task(
        context, store,
        apply_execution_identity=lambda task, args: task.update(execution_id='exec-1'),
        init_status_history=lambda task, status, now: task.update(status_history=[]),
        is_gate_task=lambda task: False,
        trajectory_ledger_factory=lambda path: object(),
        record_trajectory_event=lambda *a, **k: calls.append((a, k)),
    )

    assert task is store.saved[0]
    assert task['execution_mode'] == 'context'
    assert task['context'] == {'ref': 'ctx-1'}
    assert task['execution_id'] == 'exec-1'
    assert task['dispatch_operation_id'] == 'op-1'
    assert task['supersedes'] == 'task-old'
    assert task['customer'] == 'Acme'
    assert task['status_history'] == []
    assert [call[0][1] for call in calls] == ['run_started', 'task_started', 'agent_started']
    assert finished == [context.launch_intent]
