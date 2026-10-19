"""CLI orchestration for retrying work in an existing task workspace."""

import os
import subprocess
import subprocess
import sys
import time
import uuid


def handle_rework(
    args,
    *,
    get_store,
    herdr,
    is_gate_task,
    recover_unconsumed_gate_cleanup_id,
    cleanup_gate_for_rework,
    now=time.time,
):
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
        if task["status"] not in {
            "blocked",
            "agent_done",
            "working",
            "rework",
            "paused",
            "interrupted",
        }:
            print(
                f"Cannot rework task from {task['status']}; terminal results remain historical"
            )
            sys.exit(2)
        request_id = getattr(args, "request_id", None)
        if (
            request_id
            and task.get("rework_request_id") == request_id
            and (task.get("rework_delivery") == "delivered")
        ):
            print(f"[REWORK NOOP] {args.task_id} request already delivered")
            return
        owned, reason = owned_live_pane(task)
        if not owned:
            print(f"Rework refused: pane ownership {reason}")
            sys.exit(2)
        execution_token = (
            getattr(args, "execution_token", None)
            or getattr(args, "request_id", None)
            or (
                task.get("rework_request_id")
                if task.get("rework_delivery") == "pending"
                else None
            )
            or recover_unconsumed_gate_cleanup_id(task, store)
            or f"tok-{uuid.uuid4().hex[:12]}"
        )
        prompt = (
            args.prompt
            or task.get("rework_prompt")
            or "继续当前任务，就地修复评审问题并重新自测，不新建 Task。"
        )
        if is_gate_task(task):
            prompt += f'\n\n【门禁执行令牌契约】\n本次返工执行令牌为：{execution_token}\n写入门禁结论文件时，JSON 必须包含 "execution_token": "{execution_token}" 字段，例如：{{"verdict": "pass", "note": "...", "execution_token": "{execution_token}"}}。'
        if task.get("completion_protocol") == "receipt-v1":
            from herdr.supervisor_delivery import deliver
            from herdr import state_db

            delivery_id = execution_token
            cleanup_gate_for_rework(task, delivery_id, store)

            def prepare_task(conn):
                current = state_db._decode_task_row(
                    conn.execute(
                        "SELECT * FROM tasks WHERE task_id=?", (args.task_id,)
                    ).fetchone()
                )
                result = state_db.transition_task(
                    args.task_id,
                    "rework",
                    reason=args.reason,
                    source="herdr-task",
                    conn=conn,
                    expected_status=current["status"],
                    expected_version=current["version"],
                    metadata={
                        "stage_verdict": None,
                        "stage_verdict_note": None,
                        "rework_prompt": prompt,
                        "rework_delivery": "pending",
                        "rework_request_id": delivery_id,
                        "execution_token": delivery_id,
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
                task,
                store,
                "REWORK",
                {"intervention_id": delivery_id, "action": "REWORK"},
                prompt,
                send,
                prepare_task=prepare_task,
            )
            store.update_task_metadata(
                args.task_id,
                {"rework_delivery": "delivered", "execution_token": delivery_id},
            )
            print(f"[REWORK] {args.task_id} pane={pane_reference(task)} reused")
            return result
        if not (
            task.get("status") == "rework" and task.get("rework_delivery") == "pending"
        ):
            cleanup_gate_for_rework(task, execution_token, store)
        from herdr.task_delivery import instruction_block

        prompt += instruction_block(task)
        started_at = now()
        result = kernel.transition_task(
            args.task_id,
            "rework",
            reason=args.reason,
            source="herdr-task",
            store=store,
            expected_status=task["status"],
            expected_version=task.get("version"),
            metadata={
                "rework_prompt": prompt,
                "rework_delivery": "pending",
                "rework_request_id": request_id,
                "execution_token": execution_token,
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
            if (
                not current
                or current.get("rework_request_id") != request_id
                or current.get("rework_started_at") != started_at
            ):
                print(
                    "Rework delivered; request changed before receipt, retaining new result"
                )
                return
            metadata = {"rework_delivery": "delivered"}
            expected_delivery_version = (
                contract_version
                if task.get("completion_protocol") == "receipt-v1"
                else result.get("current_version")
            )
            if current.get("version") == expected_delivery_version:
                metadata.update(stage_verdict=None, stage_verdict_note=None)
            receipt = kernel.transition_task(
                args.task_id,
                current["status"],
                reason="rework_delivered",
                source="herdr-task",
                store=store,
                expected_status=current["status"],
                expected_version=current.get("version"),
                metadata=metadata,
            )
            if receipt.get("accepted", True):
                break
        else:
            print("Rework delivered; receipt pending due to concurrent changes")
            sys.exit(2)
        print(f"[REWORK] {args.task_id} pane={pane_reference(task)} reused")


def _is_gate_cleanup_completed(task, delivery_id, store, *, deps):
    _is_gate_task = deps["is_gate_task"]
    "Check if a durable gate cleanup completion phase was already persisted for this delivery."
    if not delivery_id or not store:
        return False
    from herdr import state_db

    conn = state_db.get_db_connection(store.db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM events WHERE task_id=? AND event_type='gate_cleanup_completed' AND json_extract(payload_json, '$.delivery_id')=? LIMIT 1",
            (task["task_id"], delivery_id),
        ).fetchone()
        return bool(row)
    finally:
        conn.close()


def _recover_unconsumed_gate_cleanup_id(task, store, *, deps):
    _is_gate_task = deps["is_gate_task"]
    "Recover existing gate cleanup operation ID when retrying a rework before delivery committed."
    if not task or not store or (not _is_gate_task(task)):
        return None
    from herdr import state_db

    conn = state_db.get_db_connection(store.db_path)
    try:
        meta_id = task.get("rework_cleanup_id")
        if meta_id:
            dispatched = conn.execute(
                "\n                SELECT 1 FROM events\n                WHERE task_id=? AND json_extract(payload_json, '$.intervention_id')=? AND event_type IN ('rework_dispatched', 'rework_dispatch_prepared')\n                LIMIT 1\n                ",
                (task["task_id"], meta_id),
            ).fetchone()
            if not dispatched and (
                not (
                    task.get("rework_delivery") == "delivered"
                    and task.get("execution_token") == meta_id
                )
            ):
                return meta_id
        row = conn.execute(
            "\n            SELECT json_extract(payload_json, '$.delivery_id') AS delivery_id\n            FROM events\n            WHERE task_id=? AND (run_id=? OR ? IS NULL) AND event_type='gate_cleanup_completed'\n            ORDER BY id DESC LIMIT 1\n            ",
            (task["task_id"], task.get("run_id"), task.get("run_id")),
        ).fetchone()
        if not row or not row["delivery_id"]:
            return None
        candidate_id = row["delivery_id"]
        dispatched = conn.execute(
            "\n            SELECT 1 FROM events\n            WHERE task_id=? AND json_extract(payload_json, '$.intervention_id')=? AND event_type IN ('rework_dispatched', 'rework_dispatch_prepared')\n            LIMIT 1\n            ",
            (task["task_id"], candidate_id),
        ).fetchone()
        if dispatched:
            return None
        if task.get("rework_delivery") == "delivered" and (
            task.get("rework_request_id") == candidate_id
            or task.get("execution_token") == candidate_id
        ):
            return None
        return candidate_id
    finally:
        conn.close()


def _cleanup_gate_for_rework(task, delivery_id=None, store=None, *, deps):
    _is_gate_task = deps["is_gate_task"]
    _herdr = deps["herdr"]
    _gate_verdict_file_candidates = deps["gate_verdict_file_candidates"]
    os = deps["os"]
    gate_cleanup_completed = deps["gate_cleanup_completed"]
    "PR #250 & Codex review: Stop old writer and purge prior verdicts for gate tasks in a two-phase recoverable cleanup."
    if not _is_gate_task(task):
        return
    if (
        task.get("status") == "rework"
        and task.get("rework_delivery") == "pending"
        and (not task.get("_force_cleanup"))
    ):
        return
    if (
        not task.get("_force_cleanup")
        and store
        and delivery_id
        and gate_cleanup_completed(task, delivery_id, store)
    ):
        return
    if store and delivery_id:
        store.record_event(
            "gate_cleanup_intent",
            {
                "delivery_id": delivery_id,
                "task_id": task["task_id"],
                "run_id": task.get("run_id"),
            },
            task_id=task["task_id"],
            workflow_id=task.get("workflow_id"),
            run_id=task.get("run_id"),
            source="herdr-task",
        )
    from herdr.node_capacity import pane_reference

    pane_ref = pane_reference(task)
    if pane_ref:
        res = _herdr("pane", "send-keys", str(pane_ref), "C-c")
        if res.returncode != 0:
            raise RuntimeError(
                f"Failed to interrupt gate pane {pane_ref} (exit {res.returncode}): {res.stderr or res.stdout}"
            )
    candidates = _gate_verdict_file_candidates(task)
    for cand in candidates:
        if cand and os.path.exists(cand):
            try:
                os.remove(cand)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise RuntimeError(
                    f"Failed to purge gate verdict file {cand}: {exc}"
                ) from exc
            if os.path.exists(cand):
                raise RuntimeError(
                    f"Gate verdict file still exists after removal attempt: {cand}"
                )
    if store and delivery_id:
        store.record_event(
            "gate_cleanup_completed",
            {
                "delivery_id": delivery_id,
                "task_id": task["task_id"],
                "run_id": task.get("run_id"),
            },
            task_id=task["task_id"],
            workflow_id=task.get("workflow_id"),
            run_id=task.get("run_id"),
            source="herdr-task",
        )


def autosave_clone_wip(task, *, run_command, internal_untracked_exact):
    INTERNAL_UNTRACKED_EXACT = internal_untracked_exact
    "Supersede 前把 clone 工作树变更存成 WIP commit(best-effort)。\n\n    fix1/fix4 的实现曾以未提交形态 stranded 在 retained clone 里。\n    只含工作树文件变更,不含 .herdr-loop 等内部装配目录;失败只告警，\n    永不阻断作废本身。不 push（分支归属与网络留给集成流程）。\n    "
    task_id = (task or {}).get("task_id") or "unknown-task"
    clone = (task or {}).get("clone_path") or ""
    branch = (task or {}).get("branch") or ""
    if not clone or not branch:
        return False
    if not os.path.isdir(os.path.join(clone, ".git")):
        return False

    def _git(*args, timeout=15):
        return run_command(
            ["git", "-C", clone, *args], text=True, capture_output=True, timeout=timeout
        )

    try:
        status = _git("status", "--porcelain")
        if status.returncode != 0 or not (status.stdout or "").strip():
            return False
        add = _git("add", "-A")
        if add.returncode != 0:
            print(
                f"[WIP AUTOSAVE WARN] {task_id}: git add failed: {(add.stderr or '').strip()[:200]}"
            )
            return False
        _git("reset", "-q", "--", *sorted(INTERNAL_UNTRACKED_EXACT))
        staged = _git("diff", "--cached", "--quiet")
        if staged.returncode == 0:
            return False
        commit = _git("commit", "-m", f"wip: auto-save {task_id} on supersede")
        if commit.returncode != 0:
            print(f"[WIP AUTOSAVE WARN] {task_id}: git commit failed")
            return False
    except Exception as exc:
        print(f"[WIP AUTOSAVE WARN] {task_id}: {exc}")
        return False
    print(f"[WIP AUTOSAVE] {task_id}: work saved on {branch}")
    return True
