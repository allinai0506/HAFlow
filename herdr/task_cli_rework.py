"""CLI orchestration for retrying work in an existing task workspace."""

import subprocess
import sys
import time
import uuid


def handle_rework(args, *, get_store, herdr, is_gate_task,
                  recover_unconsumed_gate_cleanup_id,
                  cleanup_gate_for_rework, now=time.time):
    from herdr.task_resources import owned_live_pane, workflow_launch_lock
    from herdr.node_capacity import pane_reference
    from herdr import kernel

    store = get_store()
    task = store.get_task(args.task_id)
    if not task:
        print(f"Task not found: {args.task_id}")
        sys.exit(2)
    with workflow_launch_lock(store.db_path, task["workflow_id"]):
        task = store.get_task(args.task_id)
        if task["status"] not in {"blocked", "agent_done", "working", "rework", "paused", "interrupted"}:
            print(f"Cannot rework task from {task['status']}; terminal results remain historical")
            sys.exit(2)
        request_id = getattr(args, "request_id", None)
        if request_id and task.get("rework_request_id") == request_id and task.get("rework_delivery") == "delivered":
            print(f"[REWORK NOOP] {args.task_id} request already delivered")
            return
        owned, reason = owned_live_pane(task)
        if not owned:
            print(f"Rework refused: pane ownership {reason}")
            sys.exit(2)
        execution_token = (
            getattr(args, "execution_token", None)
            or getattr(args, "request_id", None)
            or (task.get("rework_request_id") if task.get("rework_delivery") == "pending" else None)
            or recover_unconsumed_gate_cleanup_id(task, store)
            or f"tok-{uuid.uuid4().hex[:12]}"
        )
        prompt = args.prompt or task.get("rework_prompt") or "继续当前任务，就地修复评审问题并重新自测，不新建 Task。"
        if is_gate_task(task):
            prompt += (
                f"\n\n【门禁执行令牌契约】\n"
                f"本次返工执行令牌为：{execution_token}\n"
                f"写入门禁结论文件时，JSON 必须包含 \"execution_token\": \"{execution_token}\" 字段，"
                f"例如：{{\"verdict\": \"pass\", \"note\": \"...\", \"execution_token\": \"{execution_token}\"}}。"
            )
        if task.get("completion_protocol") == "receipt-v1":
            from herdr.supervisor_delivery import deliver
            from herdr import state_db
            delivery_id = execution_token
            cleanup_gate_for_rework(task, delivery_id, store)

            def prepare_task(conn):
                current = state_db._decode_task_row(conn.execute(
                    "SELECT * FROM tasks WHERE task_id=?", (args.task_id,)
                ).fetchone())
                result = state_db.transition_task(
                    args.task_id, "rework", reason=args.reason, source="herdr-task",
                    conn=conn, expected_status=current["status"],
                    expected_version=current["version"],
                    metadata={
                        "stage_verdict": None, "stage_verdict_note": None,
                        "rework_prompt": prompt, "rework_delivery": "pending",
                        "rework_request_id": delivery_id, "execution_token": delivery_id,
                        "rework_started_at": now(),
                    },
                )
                if not result.get("accepted"):
                    raise ValueError("Task changed before prepared rework")

            def send(pane, text):
                from herdr.supervisor_delivery import current_delivery_task
                pane = current_delivery_task(task, store, native=True)["pane_id"]
                sent = herdr("agent", "prompt", str(pane), text)
                if sent.returncode != 0:
                    raise RuntimeError("Rework native prompt transport failed")

            result = deliver(
                task, store, "REWORK",
                {"intervention_id": delivery_id, "action": "REWORK"},
                prompt, send, prepare_task=prepare_task,
            )
            store.update_task_metadata(args.task_id, {
                "rework_delivery": "delivered", "execution_token": delivery_id,
            })
            print(f"[REWORK] {args.task_id} pane={pane_reference(task)} reused")
            return result

        if not (task.get("status") == "rework" and task.get("rework_delivery") == "pending"):
            cleanup_gate_for_rework(task, execution_token, store)
        from herdr.task_delivery import instruction_block
        prompt += instruction_block(task)
        started_at = now()
        result = kernel.transition_task(
            args.task_id, "rework", reason=args.reason, source="herdr-task", store=store,
            expected_status=task["status"], expected_version=task.get("version"),
            metadata={
                "rework_prompt": prompt, "rework_delivery": "pending",
                "rework_request_id": request_id, "execution_token": execution_token,
                "rework_started_at": started_at,
            },
        )
        if not result.get("accepted", True):
            print("Rework refused: task changed")
            sys.exit(2)
        if task.get("completion_protocol") == "receipt-v1":
            from herdr.completion_receipt import completion_instruction
            prompt += completion_instruction(args.task_id, store)
            contract_version = store.get_task(args.task_id).get("version")
        try:
            sent = herdr("agent", "prompt", str(pane_reference(task)), prompt)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"Rework delivery pending: {exc}")
            sys.exit(2)
        if sent.returncode != 0:
            print("Rework delivery pending; retry herdr-task rework on the same task")
            sys.exit(2)
        for _ in range(3):
            current = store.get_task(args.task_id)
            if (not current or current.get("rework_request_id") != request_id
                    or current.get("rework_started_at") != started_at):
                print("Rework delivered; request changed before receipt, retaining new result")
                return
            metadata = {"rework_delivery": "delivered"}
            expected_delivery_version = (contract_version if task.get("completion_protocol") == "receipt-v1"
                                         else result.get("current_version"))
            if current.get("version") == expected_delivery_version:
                metadata.update(stage_verdict=None, stage_verdict_note=None)
            receipt = kernel.transition_task(
                args.task_id, current["status"], reason="rework_delivered",
                source="herdr-task", store=store,
                expected_status=current["status"], expected_version=current.get("version"),
                metadata=metadata,
            )
            if receipt.get("accepted", True):
                break
        else:
            print("Rework delivered; receipt pending due to concurrent changes")
            sys.exit(2)
        print(f"[REWORK] {args.task_id} pane={pane_reference(task)} reused")
