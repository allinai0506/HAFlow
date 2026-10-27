"""Deliver an admitted fix-loop notification to its workflow coordinator.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def _handle_fix_loop_item(ports, item):
    """门禁 blocked 的回流通知:作废已由 handle_fix_loop 原子完成,
    这里只负责把 blocker 清单与修复派发指引送到总指挥。"""
    if "rework_requests" not in item:
        affected = set(item.get("invalidated") or [])
        item["rework_requests"] = {t["task_id"]: t["rework_request_id"] for t in ports.load_tasks()
                                  if t.get("workflow_id") == item["workflow_id"]
                                  and t.get("task_id") in affected
                                  and t.get("rework_request_id")}
    workflow_id = item["workflow_id"]
    try:
        from herdr.stage_skills import is_staged_dev_flow_workflow
        wf_cfg = ports.workflow_config_for(workflow_id) or {}
        is_staged = is_staged_dev_flow_workflow(wf_cfg)
    except Exception:
        is_staged = False

    if is_staged:
        gate_stage = item.get("gate_stage")
        retry_node = item.get("retry_node")
        ports.attention_clear(f"{workflow_id}:fix_loop:{gate_stage}")
        print(
            f"[FIX LOOP DIRECT] "
            f"workflow={workflow_id} "
            f"gate={gate_stage} "
            f"retry={retry_node} "
            f"-> handled by controller directly without coordinator"
        )
        return

    coord_pane = ports.coordinator_pane_for_workflow(workflow_id)

    if not coord_pane:
        print(
            f"[FIX LOOP SKIP] "
            f"no coordinator pane for workflow={workflow_id}"
        )
        return

    gate_stage = item["gate_stage"]
    retry_node = item["retry_node"]
    project_ctx = ports.project_for_workflow(workflow_id) or {}

    message = ports.build_fix_loop_message(
        item,
        project_ctx.get("project_name", "unknown"),
    )

    waited = 0

    while ports.coordinator_status(workflow_id) not in ("idle", "done"):
        if waited >= 120:
            import json as _json

            from herdr import fix_loop as fix_loop_core

            summary = fix_loop_core.summarize_fix_loop_item(item)
            episode_key = f"{workflow_id}:fix_loop:{gate_stage}"
            episode = ports.attention_get(episode_key) or {}
            try:
                attempts = int(episode.get("attempts") or 0) + 1
            except (TypeError, ValueError):
                attempts = 1
            ports.attention_note(
                episode_key,
                {"task_id": f"fix_loop:{gate_stage}:{retry_node}",
                 "workflow_id": workflow_id},
                "fix_loop",
                reason="coordinator_busy",
                attempts=attempts,
                next_retry_at=(
                    ports.time.time() + ports.liveness.attention_retry_interval()
                ),
                detail=_json.dumps(summary, ensure_ascii=False),
            )
            print(
                f"[FIX LOOP WAIT TIMEOUT] "
                f"workflow={workflow_id} "
                f"-> persisted for redelivery "
                f"(attempts={attempts})"
            )
            return

        ports.time.sleep(2)
        waited += 2

    result = ports.subprocess.run(
        [
            "herdr",
            "agent",
            "prompt",
            coord_pane,
            message,
            "--wait",
            "--timeout",
            "600000"
        ],
        text=True,
        capture_output=True
    )

    if result.returncode == 0:
        ports.attention_clear(f"{workflow_id}:fix_loop:{gate_stage}")
        print(
            f"[FIX LOOP NOTIFIED] "
            f"workflow={workflow_id} "
            f"gate={gate_stage} "
            f"retry={retry_node}"
        )

        ports.maybe_compact_coordinator(
            workflow_id,
            reason=f"fix_loop:{gate_stage}->{retry_node}",
        )
    else:
        print(
            f"[FIX LOOP ERROR] "
            f"workflow={workflow_id}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


DEPENDENCIES = {'_handle_fix_loop_item': ('attention_clear', 'attention_get', 'attention_note',
                           'build_fix_loop_message', 'coordinator_pane_for_workflow',
                           'coordinator_status', 'liveness', 'load_tasks',
                           'maybe_compact_coordinator', 'project_for_workflow', 'subprocess',
                           'time', 'workflow_config_for')}

__all__ = tuple(DEPENDENCIES)
