"""CLI handlers for bounded task tools and task checkpoints."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

PROMPT_DELIVERY_TIMEOUT = 120


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


def compile_working_context_ref(task, *, get_store):
    """Compile a launch-boundary context without changing task state."""
    try:
        from herdr.context_compiler import compile_working_context, infer_agent_role
        context = compile_working_context(
            workflow_id=task.get("workflow_id"),
            task_id=task.get("task_id"),
            agent_role=infer_agent_role(task),
            store=get_store(),
        )
        return context.context_id
    except Exception as exc:
        print(f"[WORKING_CONTEXT SKIPPED] task={task.get('task_id')}: {type(exc).__name__}")
        return None


def fetch_experience_rules_block(task, *, get_store):
    """Fetch active published enterprise business rules for this task."""
    try:
        try:
            from herdr.experience.collector import trigger_background_sync
            trigger_background_sync()
        except Exception:
            pass
        from herdr.experience.client import fetch_experience_rules_block as fetch
        return fetch(task, store=get_store())
    except Exception as exc:
        if task.get("mandatory_experience_rules"):
            raise RuntimeError(f"Experience Hub is unreachable but mandatory rules required: {exc}")
        return ""


def prefetch_mandatory_rules(task, *, get_store):
    """Resolve mandatory rules before dispatch state or prompt side effects."""
    return fetch_experience_rules_block(task, get_store=get_store)


def mandatory_experience_gate(args, node_id):
    """Resolve required experience rules before allocating launch resources."""
    if not getattr(args, "mandatory_experience_rules", False):
        return None, []
    context = {
        "task_id": getattr(args, "task_id", None),
        "customer": getattr(args, "customer", None),
        "business_task_type": getattr(args, "business_task_type", None),
        "task_type": getattr(args, "task_type", None),
        "stage": node_id,
        "node": node_id,
        "mandatory_experience_rules": True,
    }
    try:
        from herdr.experience.client import fetch_experience_rules_block
        references = []
        block = fetch_experience_rules_block(context, references=references)
        return block, references
    except Exception as exc:
        print(
            "[EXPERIENCE GATE] mandatory experience rules could not be resolved "
            f"before allocating launch resources: {exc}",
            file=sys.stderr,
        )
        raise


def handle_dispatch_task(task_id, prompt, prefetched_experience_suffix=None,
                         prefetched_experience_refs=None, *, get_store, set_status,
                         compile_context_ref=None, fetch_rules_block=None,
                         prefetch_rules=None, timeout=PROMPT_DELIVERY_TIMEOUT):
    """Build and deliver a task prompt through the bounded dispatch protocol."""
    store = get_store()
    task = store.get_task(task_id)
    if not task:
        print(f"Task not found: {task_id}")
        sys.exit(1)

    receipt_delivery = bool(task.get("run_id"))
    if task["status"] != "pending" and not (receipt_delivery and task["status"] == "dispatched"):
        print(
            f"Task cannot dispatch from status: {task['status']}; use herdr-task rework "
            f"{task_id} --prompt '<repair instructions>' for the same owned execution"
        )
        sys.exit(2)

    if prefetched_experience_suffix is not None:
        experience_suffix = prefetched_experience_suffix
        has_prefetched_experience = True
        if prefetched_experience_refs:
            try:
                metadata = dict(task.get("metadata") or {})
                metadata["applied_experience_rules"] = prefetched_experience_refs
                store.update_task_metadata(task_id, metadata)
            except (OSError, ValueError, RuntimeError, AttributeError, KeyError):
                pass
    else:
        experience_suffix = None
        has_prefetched_experience = False
        if task.get("mandatory_experience_rules"):
            resolve_rules = prefetch_rules or (
                lambda current: prefetch_mandatory_rules(current, get_store=get_store)
            )
            experience_suffix = resolve_rules(task)
            has_prefetched_experience = True

    if not receipt_delivery:
        set_status(task_id, "dispatched")
    task = get_store().get_task(task_id)
    if not task:
        print(f"Task not found after dispatch transition: {task_id}")
        sys.exit(1)

    import shlex
    from herdr.workflow_docs import cli_path
    artifact_task = shlex.quote(str(cli_path()))
    artifact_loop = shlex.quote(str(cli_path().with_name("herdr-loop")))
    loop_dir = Path(task.get("clone_path", "")) / ".herdr-loop"
    loop_suffix = ""
    if loop_dir.exists():
        loop_suffix = (
            "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + "【工位内循环铁律 - Inner Loop Protocol】\n"
            + "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + "工位已装配量化评估环境，请严格遵循以下工作法则：\n\n"
            + "【正常完成流程】\n"
            + "1. 阅读 .herdr-loop/GOAL.md 了解目标契约与验收标准；\n"
            + "2. 实现或修改代码；\n"
            + f"3. 运行自检评估：{artifact_loop} eval\n"
            + "   → 查看 .herdr-loop/EVALUATION.md 了解失败原因；\n"
            + "4. 若自检未通过（得分 < 100.0），继续就地修改，回到步骤 2；\n"
            + "5. 自检 100% 通过后，方可输出完成标记。\n\n"
            + "【铁律禁止项】\n"
            + "❌ 严禁在自检未通过前输出完成标记（HERDR_TASK_DONE）；\n"
            + "❌ 严禁在自检未通过时放弃工位或声称无法继续；\n"
            + "❌ 严禁向上层汇报局部问题（如单个语法错误）—— 工位负责就地解决；\n\n"
            + "【熔断求助流程（连续自愈失败≥3次）】\n"
            + "若遇到外部依赖或环境障碍导致连续 3 次自愈仍未通过，执行以下步骤：\n"
            + f"1. 运行 {artifact_loop} eval 更新最终评分；\n"
            + "2. 检查 .herdr-loop/STATE.md，若 status=exhausted，说明已达上限；\n"
            + "3. 在终端输出求助总结（包含复现路径与阻碍点）；\n"
            + "4. 在终端单独输出一行：HERDR_TASK_BLOCKER:" + task_id + "\n"
            + "   （表示工位主动请求总指挥仲裁，等待外部指示）；\n"
            + "5. 不要输出 HERDR_TASK_DONE，静默等待。\n"
        )

    context_suffix = ""
    if task.get("execution_mode") == "context":
        from herdr.workflow import render_context_reference_block
        block = render_context_reference_block(task.get("context") or {})
        if block:
            context_suffix = "\n\n" + block + "\n"

    compile_ref = compile_context_ref or (
        lambda current: compile_working_context_ref(current, get_store=get_store)
    )
    working_context_ref = compile_ref(task)
    working_context_suffix = ""
    if working_context_ref:
        working_context_suffix = (
            "\n\nWORKING_CONTEXT REF:\n"
            f"WORKING_CONTEXT_REF:{working_context_ref}\n"
            "Load this immutable context by reference before continuing:\n"
            f"{artifact_task} working-context get --context-id {working_context_ref}\n"
        )

    fetch_rules = fetch_rules_block or (
        lambda current: fetch_experience_rules_block(current, get_store=get_store)
    )
    rules_suffix = experience_suffix if has_prefetched_experience else fetch_rules(task)
    checkpoint_suffix = ""
    completion_suffix = "" if receipt_delivery else (
        "When all requested work is finished, output on its own line "
        "the prefix HERDR_TASK_DONE: followed immediately by this task id.\n"
    )
    prompt = (
        (f"WORKER_WRITE_ROOT: {task['clone_path']}\n"
         "业务相对路径以此目录为根；来源项目路径仅供定位，业务产物写入本工位克隆。\n"
         "共享报告使用工作流受管文档接口发布；不要把来源目录文件视为本 Task 的产物。\n\n"
         if task.get("clone_path") and task.get("execution_mode") != "context" else "")
        + prompt + context_suffix + working_context_suffix + rules_suffix + loop_suffix
        + checkpoint_suffix + "\nHERDR orchestration protocol:\n"
        + "HERDR_ORCH_TASK:" + task_id + "\n" + completion_suffix
    )

    try:
        from herdr import completion
        prompt, sanitized_count = completion.sanitize_prompt(prompt, task_id)
        if sanitized_count:
            try:
                get_store().record_event(
                    "prompt_marker_sanitized",
                    {"task_id": task_id, "replaced": sanitized_count},
                    workflow_id=task.get("workflow_id"),
                    node_id=task.get("node") or task.get("stage"),
                    task_id=task_id,
                    source="herdr-task",
                )
            except (OSError, ValueError, RuntimeError, AttributeError):
                pass
            print(f"[PROMPT HYGIENE] task={task_id} sanitized_markers={sanitized_count}")
    except (ImportError, AttributeError):
        pass

    if not receipt_delivery:
        from herdr.task_delivery import instruction_block
        prompt += instruction_block(task)

    if receipt_delivery:
        from herdr.supervisor_delivery import deliver, DeliveryUnknown
        from herdr import state_db
        store = get_store()
        if task["status"] == "dispatched" and not store.list_events(
            task_id=task_id, event_type="initial_dispatch_prepared", source="herdr-task", limit=1
        ):
            raise DeliveryUnknown("INITIAL", "initial:" + task["run_id"])

        def prepare_task(conn):
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            current = state_db._decode_task_row(row)
            result = state_db.transition_task(
                task_id, "dispatched", source="herdr-task", reason="initial_delivery_prepared",
                expected_status=current["status"], expected_version=current["version"], conn=conn,
            )
            if not result.get("accepted"):
                raise ValueError("Task changed before prepared delivery")

        def send(pane, text):
            from herdr.supervisor_delivery import current_delivery_task
            pane = current_delivery_task(task, store, native=True)["pane_id"]
            result = subprocess.run(
                ["herdr", "agent", "prompt", str(pane), text], text=True,
                capture_output=True, timeout=timeout,
            )
            if result.returncode != 0:
                raise RuntimeError("Initial native prompt transport failed")

        return deliver(
            task, store, "INITIAL", {"intervention_id": "initial:" + task["run_id"], "action": "INITIAL"},
            prompt, send, prepare_task=prepare_task,
        )

    pane_id = task["pane_id"]
    marker = "HERDR_ORCH_TASK:" + task_id

    def run_command(command, command_timeout=None):
        try:
            return subprocess.run(
                command, text=True, capture_output=True, timeout=command_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            return subprocess.CompletedProcess(
                command, 124, "", f"command timed out after {command_timeout}s: {exc}"
            )

    def status():
        result = run_command(["herdr", "agent", "get", pane_id])
        if result.returncode != 0:
            return None
        try:
            return json.loads(result.stdout)["result"]["agent"].get("agent_status")
        except Exception:
            return None

    def visible():
        result = run_command(["herdr", "pane", "read", pane_id, "--source", "visible"])
        return (result.stdout or "") + "\n" + (result.stderr or "")

    def focus():
        run_command(["herdr", "agent", "focus", pane_id])
        time.sleep(0.25)

    def press_enter():
        return run_command(["herdr", "pane", "send-keys", pane_id, "enter"])

    def send_full_prompt():
        focus()
        sent = run_command(["herdr", "pane", "send-text", pane_id, prompt])
        time.sleep(0.35)
        entered = press_enter()
        return sent.returncode == 0 and entered.returncode == 0

    run_command(["herdr", "agent", "focus", pane_id])
    time.sleep(0.25)
    run_command(["herdr", "pane", "send-text", pane_id, "/clear"])
    run_command(["herdr", "pane", "send-keys", pane_id, "enter"])
    print(f"[CONTEXT CLEAR] task={task_id} pane={pane_id}")

    clear_deadline = time.time() + 8
    while time.time() < clear_deadline:
        if status() in {"idle", None}:
            break
        time.sleep(0.4)
    time.sleep(1.5)

    primary = run_command(
        ["herdr", "agent", "prompt", pane_id, prompt],
        command_timeout=timeout,
    )
    if primary.returncode == 124:
        print(f"[PROMPT TIMEOUT] task={task_id} pane={pane_id} after={timeout}s")

    deadline = time.time() + 6
    while time.time() < deadline:
        current_status = status()
        screen = visible()
        if current_status in {"working", "blocked", "done"}:
            print(f"[PROMPT DELIVERED] task={task_id} via=agent-prompt state={current_status}")
            print(f"Task dispatched: {task_id} -> {pane_id}")
            return
        if marker in screen and current_status in {"idle", "unknown", None}:
            focus()
            press_enter()
            time.sleep(1.0)
            current_status = status()
            if current_status in {"working", "blocked", "done"}:
                print(f"[PROMPT DELIVERED] task={task_id} via=enter-fallback state={current_status}")
                print(f"Task dispatched: {task_id} -> {pane_id}")
                return
        time.sleep(0.5)

    if marker not in visible():
        if not send_full_prompt():
            set_status(task_id, "failed")
            print(f"Prompt delivery failed for {task_id}: {primary.stderr or primary.stdout}")
            sys.exit(3)
        print(f"[PROMPT FALLBACK] task={task_id} via=send-text+enter")
    else:
        print(f"[PROMPT PENDING] task={task_id} marker-visible; sentinel will monitor")
    print(f"Task dispatched: {task_id} -> {pane_id}")
