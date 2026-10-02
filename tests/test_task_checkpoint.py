from pathlib import Path
import importlib
import pytest
from herdr.state_store import get_state_store
from herdr.observation import ObservationStore


def api():
    from importlib.util import find_spec
    assert find_spec('herdr.task_checkpoint'), 'checkpoint publication API missing'
    return importlib.import_module('herdr.task_checkpoint')


@pytest.fixture
def scene(tmp_path):
    store = get_state_store(tmp_path / 'state.db')
    store.save_task({'task_id': 't', 'workflow_id': 'wf', 'node': 'n', 'run_id': 'r', 'completion_epoch': 'e', 'status': 'working'})
    return store


def publish(store, text='report password=secret123', step=1, **extra):
    return api().publish_task_checkpoint('t', 'r', 'e', text, step, store=store, completed_steps=['inspect'], next_step='test', summary='progress', **extra)


def test_publish_redacts_and_retries_one_immutable_receipt(scene):
    receipt = publish(scene)
    repeated = publish(scene)
    assert repeated == receipt
    obs = ObservationStore(scene.db_path).get(receipt['observation_id'])
    assert 'secret123' not in Path(obs.content_ref).read_text()
    assert ObservationStore(scene.db_path).verify(obs.observation_id)['valid']
    assert len(scene.list_events(event_type='task_artifact_checkpoint')) == 1
    assert api().read_task_checkpoints('t', 'r', 'e', store=scene)['segments'] == [receipt]


@pytest.mark.parametrize('run,epoch', [('foreign', 'e'), ('r', 'old')])
def test_wrong_identity_rejected_before_files_or_events(scene, run, epoch):
    with pytest.raises(ValueError, match='identity'):
        api().publish_task_checkpoint('t', run, epoch, 'secret', 1, store=scene)
    assert ObservationStore(scene.db_path).list(task_id='t') == []
    assert scene.list_events(event_type='task_artifact_checkpoint') == []


def test_modified_segment_and_conflicting_step_rejected(scene):
    receipt = publish(scene, 'one')
    with pytest.raises(ValueError, match='step'):
        publish(scene, 'different')
    obs = ObservationStore(scene.db_path).get(receipt['observation_id'])
    Path(obs.content_ref).chmod(0o600)
    Path(obs.content_ref).write_text('tampered')
    with pytest.raises(ValueError, match='integrity'):
        api().read_task_checkpoints('t', 'r', 'e', store=scene)


def test_crash_after_observation_before_receipt_retry_recovers(scene, monkeypatch):
    module = api()
    original = module.state_db.record_event
    monkeypatch.setattr(module.state_db, 'record_event', lambda *a, **k: (_ for _ in ()).throw(OSError('interruption')))
    with pytest.raises(OSError): publish(scene, 'durable')
    assert len(ObservationStore(scene.db_path).list(task_id='t')) == 1
    monkeypatch.setattr(module.state_db, 'record_event', original)
    assert api().read_task_checkpoints('t', 'r', 'e', store=scene)['segments'] == []
    receipt = publish(scene, 'durable')
    assert len(ObservationStore(scene.db_path).list(task_id='t')) == 1
    assert api().read_task_checkpoints('t', 'r', 'e', store=scene)['segments'] == [receipt]


def test_aggregate_creates_new_artifact_preserving_segments(scene):
    one, two = publish(scene, 'one', 1), publish(scene, 'two', 2)
    result = api().aggregate_task_checkpoints('t', 'r', 'e', store=scene)
    assert result['observation_id'] not in {one['observation_id'], two['observation_id']}
    obs = ObservationStore(scene.db_path).get(result['observation_id'])
    assert Path(obs.content_ref).read_text() == 'one\n\ntwo'
    assert api().read_task_checkpoints('t', 'r', 'e', store=scene)['segments'] == [one, two]


def test_cli_publish_read_aggregate_real_database(scene, tmp_path):
    import os, subprocess, sys, json
    root = Path(__file__).resolve().parents[1]
    segment = tmp_path / 'part.txt'
    segment.write_text('one password=secret123')
    env = {**os.environ, 'HOME': str(tmp_path), 'HERDR_STATE_DB': str(scene.db_path), 'TASKS_FILE': str(tmp_path / 'tasks.json'), 'WORKFLOWS_FILE': str(tmp_path / 'workflows.json')}
    common = ['--task-id', 't', '--run-id', 'r', '--epoch', 'e']
    def cli(command, extra=()):
        result = subprocess.run([sys.executable, str(root / 'bin/herdr-task'), command, *common, *extra], env=env, text=True, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)
    receipt = cli('checkpoint-publish', ['--segment-file', str(segment), '--step', '1', '--next-step', 'review'])
    assert receipt['sha256']
    assert cli('checkpoint-read')['segments'] == [receipt]
    assert cli('checkpoint-aggregate')['observation_id'] != receipt['observation_id']
    assert api().validate_checkpoint_artifact(scene.get_task('t'), receipt, store=scene)['sha256'] == receipt['sha256']


def test_partial_file_is_not_recovery_and_missing_file_rejects(scene):
    module = api()
    directory = ObservationStore(scene.db_path).content_dir
    directory.mkdir()
    (directory / '.segment-interrupted').write_text('partial')
    assert module.read_task_checkpoints('t', 'r', 'e', store=scene)['segments'] == []
    receipt = publish(scene, 'complete')
    Path(ObservationStore(scene.db_path).get(receipt['observation_id']).content_ref).unlink()
    with pytest.raises(ValueError, match='integrity'):
        module.read_task_checkpoints('t', 'r', 'e', store=scene)


def _checkpoint_racer(db, barrier, queue):
    store = get_state_store(db)
    barrier.wait(timeout=10)
    queue.put(publish(store, 'same segment'))


def test_independent_process_same_segment_one_receipt(scene):
    import multiprocessing as mp
    ctx = mp.get_context('spawn')
    barrier, queue = ctx.Barrier(2), ctx.Queue()
    processes = [ctx.Process(target=_checkpoint_racer, args=(scene.db_path, barrier, queue)) for _ in range(2)]
    for process in processes: process.start()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    assert queue.get(timeout=2) == queue.get(timeout=2)
    assert len(scene.list_events(event_type='task_artifact_checkpoint')) == 1
    assert len(ObservationStore(scene.db_path).list(task_id='t')) == 1


def test_epoch_changes_during_publication_cannot_register_old_receipt(scene, monkeypatch):
    module = api()
    original = module._AtomicObservationStore.create
    def drift(observations, **kwargs):
        observation = original(observations, **kwargs)
        scene.update_task_metadata('t', {'completion_epoch': 'new'})
        return observation
    monkeypatch.setattr(module._AtomicObservationStore, 'create', drift)
    with pytest.raises(ValueError, match='identity'):
        publish(scene, 'old epoch report')
    assert scene.list_events(event_type='task_artifact_checkpoint') == []


def test_interruption_before_atomic_publish_no_receipt_and_clean_retry(scene, monkeypatch):
    module = api()
    original = module.os.link
    monkeypatch.setattr(module.os, 'link', lambda *a: (_ for _ in ()).throw(OSError('interruption')))
    with pytest.raises(OSError): publish(scene, 'complete segment')
    directory = ObservationStore(scene.db_path).content_dir
    assert list(directory.iterdir()) == []
    assert scene.list_events(event_type='task_artifact_checkpoint') == []
    monkeypatch.setattr(module.os, 'link', original)
    assert publish(scene, 'complete segment')['sha256']


def test_completion_artifact_ref_rejects_cross_run_and_tampering(scene):
    module = api()
    receipt = publish(scene, 'verified')
    task = scene.get_task('t')
    with pytest.raises(ValueError, match='identity'):
        module.validate_checkpoint_artifact({**task, 'run_id': 'foreign'}, receipt, store=scene)
    with pytest.raises(ValueError, match='integrity'):
        module.validate_checkpoint_artifact(task, {**receipt, 'sha256': 'wrong'}, store=scene)
    aggregate = module.aggregate_task_checkpoints('t', 'r', 'e', store=scene)
    assert module.validate_checkpoint_artifact(task, aggregate, store=scene)['sha256'] == aggregate['sha256']


def test_checkpoint_prompt_contract_binds_identity_and_preserves_tool_limits(scene):
    module = api()
    assert callable(getattr(module, 'checkpoint_instruction_block', None)), 'Worker checkpoint contract missing'
    block = module.checkpoint_instruction_block(scene.get_task('t'), 'e')
    assert 'checkpoint-publish --task-id t --run-id r --epoch e' in block
    assert 'checkpoint-read --task-id t --run-id r --epoch e' in block
    assert 'tool-run --task-id t --run-id r --epoch e' in block
    assert 'published segments' in block and 'external Agent' in block


def test_aggregate_reference_must_match_its_segment_chain(scene):
    module = api()
    receipt = publish(scene, 'original')
    obs = ObservationStore(scene.db_path).create(run_id='r', task_id='t', workflow_id='wf', source_type='artifact', source_ref='checkpoint-aggregate:forged', content='invented aggregate', media_type='text/plain', metadata={'epoch': 'e', 'segments': [receipt['observation_id']]})
    with pytest.raises(ValueError, match='aggregate'):
        module.validate_checkpoint_artifact(scene.get_task('t'), {'observation_id': obs.observation_id, 'sha256': obs.sha256}, store=scene)


def test_prompt_artifact_instruction_matches_real_completion_cli(scene, tmp_path):
    import subprocess, sys, os, json
    from herdr.completion_receipt import issue_completion_contract
    module = api()
    contract = issue_completion_contract('t', scene)
    task = scene.get_task('t')
    block = module.checkpoint_instruction_block(task, contract['epoch'])
    assert '--artifact <observation_id>:<sha256>' in block
    assert '--artifacts-json' not in block
    receipt = module.publish_task_checkpoint('t', 'r', contract['epoch'], 'final segment', 1, store=scene)
    env = {**os.environ, 'HOME': str(tmp_path), 'HERDR_STATE_DB': str(scene.db_path), 'TASKS_FILE': str(tmp_path / 'tasks.json'), 'WORKFLOWS_FILE': str(tmp_path / 'workflows.json')}
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / 'bin/herdr-task'), 'report-completion', 't', '--identity-file', contract['path'], '--artifact', receipt['observation_id'] + ':' + receipt['sha256']], env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['artifacts'][0]['observation_id'] == receipt['observation_id']


def _read_current_epoch_process(db, queue):
    store = get_state_store(db)
    try:
        queue.put(api().read_task_checkpoints('t', 'r', 'e', store=store))
    except Exception as exc:
        queue.put({'error': str(exc)})


def test_old_epoch_history_does_not_consume_current_budget(scene):
    import multiprocessing as mp
    for step in range(1, 1001):
        scene.record_event('task_artifact_checkpoint', {'task_id': 't', 'run_id': 'old-run', 'epoch': 'old-epoch', 'step': step}, task_id='t', run_id='old-run', source='checkpoint')
    receipt = publish(scene, 'current segment')
    ctx = mp.get_context('spawn')
    queue = ctx.Queue()
    process = ctx.Process(target=_read_current_epoch_process, args=(scene.db_path, queue))
    process.start()
    process.join(15)
    assert process.exitcode == 0
    result = queue.get(timeout=2)
    assert result.get('segments') == [receipt], result
