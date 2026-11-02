"""Schedule bounded blocked-SLA work and deliver recovery prompts.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def schedule_blocked_sla_task(ports, task, now=None):
    """Schedule one bounded blocked-SLA step without blocking the watcher."""
    task_id = str((task or {}).get("task_id") or "")
    if not task_id:
        return False
    with ports._blocked_sla_schedule_lock:
        if task_id in ports._blocked_sla_scheduled:
            return False
        ports._blocked_sla_scheduled.add(task_id)

    snapshot = dict(task or {})

    def _run():
        try:
            ports.process_blocked_sla_task(snapshot, now=now)
        except (OSError, RuntimeError, ValueError, AttributeError) as exc:
            print(f"[BLOCKED SLA ASYNC ERROR] task={task_id}: {exc}")
        finally:
            with ports._blocked_sla_schedule_lock:
                ports._blocked_sla_scheduled.discard(task_id)

    try:
        ports._BLOCKED_SLA_EXECUTOR.submit(_run)
    except (RuntimeError, OSError):
        with ports._blocked_sla_schedule_lock:
            ports._blocked_sla_scheduled.discard(task_id)
        return False
    return True

def _send_blocked_repush(ports, task, decision):
    """Send the one permitted automatic prompt re-push."""
    pane_id = task.get("pane_id")
    if not pane_id:
        return False, "pane_id_missing"
    number = int(decision.get("repush_number") or 1)
    message = (
        f"第 {number} 次自动重推（blocked episode "
        f"{decision.get('episode_id', '')}）。\n"
        "这是本 episode 唯一一次自动重推；请先阅读 BLOCKER.md，"
        "若仍无法恢复，等待第二 SLA 的人类升级，不要创建新 Task。"
    )
    try:
        result = ports.subprocess.run(
            [
                "herdr", "agent", "prompt", pane_id, message,
                "--wait", "--timeout", "120000",
            ],
            text=True,
            capture_output=True,
            timeout=130,
        )
    except (OSError, ValueError, RuntimeError, ports.subprocess.SubprocessError) as exc:
        return False, str(exc)[:500]
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip() or
                  f"exit={result.returncode}")[:500]
        return False, detail
    return True, (result.stdout.strip() or "delivered")[:500]

def _blocked_sla_step(ports, task, now, poll_seconds=3.0):
    """Compatibility read/advance helper; production claims use the atomic path."""
    from herdr import blocked_sla as _sla

    task = task or {}
    key = ports._blocked_sla_key(str(task.get("task_id") or ""))
    with ports._attention_store.transaction() as episodes:
        episode = dict(episodes.get(key) or {})
        if not ports._blocked_episode_matches_task(episode, task):
            episode = _sla.new_episode(task, now)
        else:
            episode["run_id"] = task.get("run_id")
            episode["workflow_id"] = task.get("workflow_id")
            try:
                last_tick = float(episode.get("last_tick_at") or now)
                active = float(episode.get("active_seconds") or 0)
            except (TypeError, ValueError):
                last_tick = now
                active = 0.0
            episode["active_seconds"] = active + _sla.active_tick_increment(
                max(0.0, now - last_tick), poll_seconds=poll_seconds
            )
            episode["last_tick_at"] = now
        decision = _sla.decide_blocked_action(
            task=task, episode=episode, now=now
        )
        decision["claimed"] = False
        episodes[key] = episode
        return decision

def process_blocked_sla_task(ports, task, now=None, send_prompt=None):
    """Run one Controller-owned blocked SLA step, including prompt delivery."""
    now = ports.time.time() if now is None else float(now)
    if hasattr(ports, "_completion_marker_snapshot"):
        try:
            from herdr.task_resources import owned_live_pane
            try:
                owned, _ = owned_live_pane(task)
            except Exception:
                owned = False
            if owned:
                _, screen = ports._completion_marker_snapshot(task)
                from herdr.deep_preflight import classify_runtime_token_exhausted
                if screen and classify_runtime_token_exhausted(screen, task.get("agent") or "") == "TOKEN_EXHAUSTED":
                    agent = task.get("agent")
                    workflow_id = task.get("workflow_id")
                    if agent and workflow_id and hasattr(ports, "_get_store"):
                        try:
                            store = ports._get_store()
                            if hasattr(store, "record_agent_unhealthy"):
                                store.record_agent_unhealthy(workflow_id, agent, status="TOKEN_EXHAUSTED")
                            else:
                                workflow = store.get_workflow(workflow_id) or {}
                                from herdr.workspace_trust import merge_trust_failure_unhealthy
                                unhealthy, healthy = merge_trust_failure_unhealthy(workflow, agent, status="TOKEN_EXHAUSTED")
                                store.update_workflow_metadata(workflow_id, {
                                    "healthy_agents": healthy, "unhealthy_agents": unhealthy,
                                })
                                from herdr.state_store import sync_workflows_projection
                                sync_workflows_projection(store=store)
                        except Exception:
                            pass
                    if hasattr(ports, "_set_observed_status"):
                        ports._set_observed_status(task, "failed", "token_exhausted")
                    return {"action": "token_exhausted", "claimed": False}
        except Exception:
            pass

    _episode, decision = ports._claim_blocked_sla_action(task, now, poll_seconds=3.0)
    if decision.get("claimed"):
        current, reason = ports._blocked_task_claim_is_current(task, decision)
        if not current:
            return ports._release_stale_blocked_claim(task, decision, reason)
    if decision.get("claimed") and decision.get("action") in {"repush", "repush_recover"}:
        sender = send_prompt or ports._send_blocked_repush
        try:
            success, detail = sender(task, decision)
        except Exception as exc:  # delivery boundary is observable, not fatal
            success, detail = False, str(exc)[:500]
        current, reason = ports._blocked_task_claim_is_current(task, decision)
        if not current:
            return ports._release_stale_blocked_claim(task, decision, reason)
        ports._blocked_sla_record_repush(
            task, decision, bool(success), detail, now,
        )
        if success:
            # The existing coordinator card remains useful, but it is not the
            # SLA decision and does not change blocked status.
            try:
                ports.enqueue_coordinator_event(task, "inner_loop_exhausted")
            except (OSError, RuntimeError, ValueError, AttributeError):
                pass
    elif decision.get("claimed") and decision.get("action") == "escalate":
        try:
            active = float(_episode.get("active_seconds") or 0)
        except (TypeError, ValueError):
            active = 0.0
        delivered = ports._notify_blocked_human_upgrade(
            task, decision.get("episode_id", ""), active
        )
        current, reason = ports._blocked_task_claim_is_current(task, decision)
        if not current:
            return ports._release_stale_blocked_claim(task, decision, reason)
        ports._blocked_sla_record_escalation(task, decision, delivered, now)
    elif decision.get("action") in {"notice", "suppressed_human"}:
        ports._blocked_sla_record_action(task, decision, now)
    return decision


DEPENDENCIES = {'schedule_blocked_sla_task': ('_BLOCKED_SLA_EXECUTOR', '_blocked_sla_schedule_lock',
                                           '_blocked_sla_scheduled', 'process_blocked_sla_task'),
 '_send_blocked_repush': ('subprocess',),
 '_blocked_sla_step': ('_attention_store', '_blocked_episode_matches_task', '_blocked_sla_key'),
 'process_blocked_sla_task': ('_blocked_sla_record_action', '_blocked_sla_record_escalation',
                              '_blocked_sla_record_repush', '_blocked_task_claim_is_current',
                              '_claim_blocked_sla_action', '_completion_marker_snapshot',
                              '_get_store', '_notify_blocked_human_upgrade',
                              '_release_stale_blocked_claim', '_send_blocked_repush',
                              '_set_observed_status', 'enqueue_coordinator_event', 'time')}

__all__ = tuple(DEPENDENCIES)
