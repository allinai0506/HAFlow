"""CLI handlers for bounded task tools and task checkpoints."""

import hashlib
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path


def handle_tool_run(args, *, get_store):
    from herdr.bounded_tools import run_bounded, validate_tool_input
    from herdr.task_checkpoint import _AtomicObservationStore, _identity
    try:
        store = get_store()
        task = _identity(store.get_task(args.task_id), args.task_id, args.run_id, args.epoch)
        argv = args.tool_argv[1:] if args.tool_argv[:1] == ['--'] else args.tool_argv
        data = b''
        if args.input_file:
            with Path(args.input_file).expanduser().open('rb') as stream:
                data = stream.read(1024 * 1024 + 1)
        validate_tool_input(argv, data, timeout=args.timeout, output_limit=args.output_limit)
        operation_id = uuid.uuid4().hex
        store.record_event('tool_execution_started', {
            'operation_id': operation_id, 'epoch': args.epoch,
            'argv_sha256': hashlib.sha256(json.dumps(argv).encode()).hexdigest(),
            'side_effects': 'unknown'}, task_id=args.task_id, workflow_id=task.get('workflow_id'),
            run_id=args.run_id, source='tool-run')
        execution_env = dict(os.environ)
        for name in ('HERDR_STATE_DB', 'TASKS_FILE', 'WORKFLOWS_FILE', 'STAGE_STATE_FILE', 'CHECKPOINTS_DIR', 'WORKFLOW_FILE'):
            execution_env.pop(name, None)
        execution_env.update(HERDR_TASK_ID=args.task_id, HERDR_RUN_ID=args.run_id,
                             HERDR_COMPLETION_EPOCH=args.epoch,
                             HERDR_EVALUATION_ROOT=str(Path(task.get('clone_path') or '').resolve()))
        with tempfile.TemporaryDirectory(prefix='herdr-tool-home-') as tool_home:
            execution_env['HOME'] = tool_home
            result = run_bounded(argv, data, timeout=args.timeout,
                                 output_limit=args.output_limit, env=execution_env)
        observations = _AtomicObservationStore(store.db_path)
        obs = observations.create(run_id=args.run_id, task_id=args.task_id,
                                  workflow_id=task.get('workflow_id'), source_type='tool_output',
                                  source_ref=f'tool:{operation_id}', content=result,
                                  media_type='application/json', metadata={'epoch': args.epoch, 'operation_id': operation_id})
        receipt = {key: result[key] for key in ('status', 'exit_code', 'side_effects', 'owned_process_group', 'elapsed_seconds')}
        receipt.update(operation_id=operation_id, epoch=args.epoch, observation_id=obs.observation_id,
                       sha256=obs.sha256, size_bytes=obs.size_bytes)
        store.record_event('tool_execution', receipt, task_id=args.task_id,
                           workflow_id=task.get('workflow_id'), run_id=args.run_id, source='tool-run')
        print(json.dumps({**receipt, 'stdout': result['stdout'], 'stderr': result['stderr']}, ensure_ascii=False))
        if result['status'] != 'completed':
            sys.exit(124 if result['status'] == 'timeout' else 125)
        if result['exit_code']:
            sys.exit(result['exit_code'] if result['exit_code'] > 0 else 128 - result['exit_code'])
    except (OSError, ValueError, KeyError) as exc:
        from herdr.supervisor.state import redact_text
        print(f'[TOOL REJECTED] {redact_text(str(exc))}', file=sys.stderr)
        sys.exit(2)


def handle_task_checkpoint(args, *, get_store):
    from herdr import task_checkpoint as checkpoints
    try:
        store = get_store()
        common = (args.task_id, args.run_id, args.epoch)
        if args.command == 'checkpoint-publish':
            with Path(args.segment_file).expanduser().open('rb') as stream:
                segment = stream.read(checkpoints.MAX_SEGMENT_BYTES + 1)
            if len(segment) > checkpoints.MAX_SEGMENT_BYTES:
                raise ValueError('checkpoint segment exceeds budget')
            result = checkpoints.publish_task_checkpoint(
                *common, segment.decode('utf-8'), args.step, store=store,
                completed_steps=args.completed_step, next_step=args.next_step,
                summary=args.summary)
        elif args.command == 'checkpoint-read':
            result = checkpoints.read_task_checkpoints(*common, store=store)
        else:
            result = checkpoints.aggregate_task_checkpoints(*common, store=store)
        print(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, KeyError) as exc:
        print(f'[CHECKPOINT REJECTED] {exc}', file=sys.stderr)
        sys.exit(2)
