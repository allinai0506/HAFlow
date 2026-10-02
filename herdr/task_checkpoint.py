"""Run-bound report segments using existing Observation and event authorities."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import shlex

from .workflow_docs import cli_path
from . import state_db
from .observation import ObservationStore
from .supervisor.state import redact_text
from .task_resources import workflow_launch_lock
from .trajectory import TrajectoryLedger, record_trajectory_event_best_effort

MAX_SEGMENT_BYTES = 1024 * 1024
MAX_SEGMENTS = 1000


def checkpoint_instruction_block(task, epoch):
    command = shlex.quote(str(cli_path()))
    identity = ' '.join(("--task-id", shlex.quote(task['task_id']), "--run-id",
                         shlex.quote(task['run_id']), "--epoch", shlex.quote(epoch)))
    return ("\nLong-task report/tool contract:\n"
            "Publish a bounded report segment after each completed phase, before lengthy next work:\n"
            f"{command} checkpoint-publish {identity} --segment-file <part.txt> --step <number> "
            "--completed-step <done> --next-step <next> --summary <bounded-summary>\n"
            f"Resume by reading: {command} checkpoint-read {identity}\n"
            f"Aggregate final published segments: {command} checkpoint-aggregate {identity}\n"
            "Retain observation_id and sha256 receipts; append --artifact <observation_id>:<sha256> "
            "to the report-completion command below for each output.\n"
            f"For HAFlow-owned bounded commands: {command} tool-run {identity} "
            "--timeout 30 --output-limit 65536 -- <program> <args...>\n"
            "NUL/oversized input is rejected; timeout/output cap means side effects unknown.\n"
            "Resume only from published segments; unpublished memory cannot be recovered. "
            "HAFlow cannot intercept all external Agent MCP/shell tools.\n")


class _AtomicObservationStore(ObservationStore):
    def write_content(self, content_ref, content):
        path = Path(content_ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix='.segment-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp, 0o444)
            os.link(temp, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(temp).unlink(missing_ok=True)


def _identity(task, task_id, run_id, epoch):
    if (not task or not run_id or not epoch or task.get('task_id') != task_id
            or task.get('run_id') != run_id or task.get('completion_epoch') != epoch):
        raise ValueError('checkpoint identity does not match current task/run/epoch')
    return task


def _receipts(store, task_id, run_id, epoch):
    # The budget is per execution epoch, not cumulative task history. Keep
    # filtering in the existing event authority before its bounded LIMIT.
    conn = state_db.get_db_connection(store.db_path)
    try:
        events = conn.execute('''SELECT payload_json FROM events
            WHERE task_id=? AND run_id=? AND event_type='task_artifact_checkpoint'
              AND source='checkpoint' AND json_extract(payload_json, '$.run_id')=?
              AND json_extract(payload_json, '$.epoch')=?
            ORDER BY timestamp DESC, id DESC LIMIT ?''',
            (task_id, run_id, run_id, epoch, MAX_SEGMENTS + 1)).fetchall()
    finally:
        conn.close()
    if len(events) > MAX_SEGMENTS:
        raise ValueError('checkpoint segment budget exhausted')
    rows = [json.loads(event['payload_json']) for event in events]
    return sorted(rows, key=lambda row: row['step'])


def _verify_receipt(receipt, task, observations):
    obs = observations.get(receipt['observation_id'])
    if (not obs or obs.task_id != task['task_id'] or obs.run_id != task['run_id']
            or obs.workflow_id != task.get('workflow_id') or obs.sha256 != receipt['sha256']
            or obs.metadata.get('epoch') != task['completion_epoch']
            or obs.size_bytes > MAX_SEGMENT_BYTES
            or not Path(obs.content_ref).resolve().is_relative_to(observations.content_dir.resolve())
            or not observations.verify(obs.observation_id)['valid']):
        raise ValueError('checkpoint integrity or ownership mismatch')
    return obs


def publish_task_checkpoint(task_id, run_id, epoch, segment, step, *, store,
                            completed_steps=(), next_step='', summary=''):
    if not isinstance(segment, str) or '\x00' in segment:
        raise ValueError('segment must be NUL-free text')
    if len(segment.encode()) > MAX_SEGMENT_BYTES:
        raise ValueError('checkpoint segment exceeds budget')
    if type(step) is not int or not 1 <= step <= MAX_SEGMENTS:
        raise ValueError('step exceeds checkpoint budget')
    if not isinstance(completed_steps, (list, tuple)) or len(completed_steps) > 100:
        raise ValueError('completed_steps exceeds budget')
    if any(not isinstance(item, str) or len(item) > 256 for item in completed_steps):
        raise ValueError('completed step exceeds budget')
    if not isinstance(next_step, str) or len(next_step) > 1024 or not isinstance(summary, str) or len(summary.encode()) > 4096:
        raise ValueError('checkpoint summary/next step exceeds budget')
    text = redact_text(segment)
    digest = hashlib.sha256(text.encode()).hexdigest()
    observations = _AtomicObservationStore(store.db_path)
    with workflow_launch_lock(store.db_path, f'checkpoint:{task_id}'):
        task = _identity(store.get_task(task_id), task_id, run_id, epoch)
        rows = _receipts(store, task_id, run_id, epoch)
        progress = {'completed_steps': [redact_text(item) for item in completed_steps],
                    'next_step': redact_text(next_step), 'summary': redact_text(summary)}
        existing = next((row for row in rows if row['step'] == step), None)
        if existing:
            if existing['sha256'] != digest or any(existing[key] != value for key, value in progress.items()):
                raise ValueError('checkpoint step already published with different content/progress')
            _verify_receipt(existing, task, observations)
            return existing
        if len(rows) >= MAX_SEGMENTS:
            raise ValueError('checkpoint segment budget exhausted')
        scope = hashlib.sha256(json.dumps([task_id, run_id, epoch, step]).encode()).hexdigest()
        obs = observations.create(run_id=run_id, task_id=task_id, workflow_id=task.get('workflow_id'),
                                  source_type='artifact', source_ref=f'checkpoint:{scope}',
                                  content=text, media_type='text/plain', metadata={'epoch': epoch, 'step': step})
        receipt = {'task_id': task_id, 'run_id': run_id, 'epoch': epoch, 'step': step,
                   'observation_id': obs.observation_id, 'sha256': obs.sha256,
                   'size_bytes': obs.size_bytes, **progress}
        _verify_receipt(receipt, task, observations)
        # Publication is authoritative only after a same-epoch database append.
        conn = state_db.get_db_connection(store.db_path)
        try:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            _identity(state_db._decode_task_row(row) if row else None, task_id, run_id, epoch)
            state_db.record_event({'event_type': 'task_artifact_checkpoint', 'task_id': task_id,
                                   'workflow_id': task.get('workflow_id'), 'run_id': run_id,
                                   'payload': receipt, 'source': 'checkpoint'}, conn=conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        record_trajectory_event_best_effort(task, 'task_checkpoint_published', ledger=TrajectoryLedger(store.db_path),
                                artifact={'observation_id': obs.observation_id, 'sha256': obs.sha256},
                                metadata={'epoch': epoch, 'step': step})
        return receipt


def read_task_checkpoints(task_id, run_id, epoch, *, store):
    task = _identity(store.get_task(task_id), task_id, run_id, epoch)
    observations = ObservationStore(store.db_path)
    rows = _receipts(store, task_id, run_id, epoch)
    for row in rows:
        _verify_receipt(row, task, observations)
    return {'task_id': task_id, 'run_id': run_id, 'epoch': epoch, 'segments': rows,
            'next_step': rows[-1]['next_step'] if rows else None,
            'resume_scope': 'published_segments_only'}


def aggregate_task_checkpoints(task_id, run_id, epoch, *, store):
    with workflow_launch_lock(store.db_path, f'checkpoint:{task_id}'):
        report = read_task_checkpoints(task_id, run_id, epoch, store=store)
        if not report['segments']:
            raise ValueError('no verified segments to aggregate')
        observations = _AtomicObservationStore(store.db_path)
        chunks, size = [], 0
        for receipt in report['segments']:
            obs = observations.get(receipt['observation_id'])
            content_bytes = Path(obs.content_ref).read_bytes()
            if len(content_bytes) != obs.size_bytes or hashlib.sha256(content_bytes).hexdigest() != obs.sha256:
                raise ValueError('checkpoint integrity changed during aggregation')
            content = content_bytes.decode('utf-8')
            size += len(content.encode()) + 2
            if size > 8 * MAX_SEGMENT_BYTES:
                raise ValueError('aggregate exceeds budget')
            chunks.append(content)
        scope = hashlib.sha256(json.dumps([task_id, run_id, epoch, [r['sha256'] for r in report['segments']]]).encode()).hexdigest()
        obs = observations.create(run_id=run_id, task_id=task_id,
                                  workflow_id=store.get_task(task_id).get('workflow_id'),
                                  source_type='artifact', source_ref=f'checkpoint-aggregate:{scope}',
                                  content='\n\n'.join(chunks), media_type='text/plain',
                                  metadata={'epoch': epoch, 'segments': [r['observation_id'] for r in report['segments']]})
        return {'task_id': task_id, 'run_id': run_id, 'epoch': epoch,
                'observation_id': obs.observation_id, 'sha256': obs.sha256, 'size_bytes': obs.size_bytes}


def validate_checkpoint_artifact(task, reference, *, store, observations=None):
    """Validate completion references against registered segment/aggregate facts."""
    observations = observations or ObservationStore(store.db_path)
    if not isinstance(reference, dict) or not reference.get('observation_id') or not reference.get('sha256'):
        raise ValueError('artifact requires observation_id and sha256')
    obs = observations.get(reference['observation_id'])
    if (not obs or obs.sha256 != reference['sha256'] or obs.task_id != task['task_id']
            or obs.run_id != task.get('run_id') or obs.metadata.get('epoch') != task.get('completion_epoch')
            or obs.workflow_id != task.get('workflow_id')
            or not Path(obs.content_ref).resolve().is_relative_to(observations.content_dir.resolve())
            or not observations.verify(obs.observation_id)['valid']):
        raise ValueError('checkpoint artifact integrity/identity mismatch')
    rows = _receipts(store, task['task_id'], task['run_id'], task['completion_epoch'])
    registered = {row['observation_id']: row for row in rows}
    if obs.observation_id in registered:
        _verify_receipt(registered[obs.observation_id], task, observations)
    else:
        segments = obs.metadata.get('segments')
        if not obs.source_ref.startswith('checkpoint-aggregate:') or not segments or len(segments) > MAX_SEGMENTS:
            raise ValueError('checkpoint artifact is not a registered segment or aggregate')
        digest = hashlib.sha256()
        size = 0
        hashes = []
        for segment_id in segments:
            if segment_id not in registered:
                raise ValueError('aggregate contains unregistered checkpoint segment')
            segment = _verify_receipt(registered[segment_id], task, observations)
            if hashes:
                digest.update(b'\n\n')
                size += 2
            size += segment.size_bytes
            if size > 8 * MAX_SEGMENT_BYTES:
                raise ValueError('aggregate exceeds budget')
            with Path(segment.content_ref).open('rb') as stream:
                remaining = segment.size_bytes + 1
                while remaining and (chunk := stream.read(min(65536, remaining))):
                    digest.update(chunk)
                    remaining -= len(chunk)
                if remaining == 0:
                    raise ValueError('aggregate segment changed size during read')
            hashes.append(segment.sha256)
        scope = hashlib.sha256(json.dumps([task['task_id'], task['run_id'], task['completion_epoch'], hashes]).encode()).hexdigest()
        if obs.source_ref != f'checkpoint-aggregate:{scope}' or digest.hexdigest() != obs.sha256 or size != obs.size_bytes:
            raise ValueError('aggregate does not match its registered segment chain')
    return {'observation_id': obs.observation_id, 'sha256': obs.sha256, 'size_bytes': obs.size_bytes}
