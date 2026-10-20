"""Discover and dispatch pending workflow continuations with scan throttling.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def check_workflow_continuation(ports, workflow_id, now=None):
    """Persist unresolved obligations; notification delivery never resolves them."""
    from herdr.projects import inspect_continuation
    now = ports.time.time() if now is None else now
    key = f"{workflow_id}:continuation"
    record = ports.project_for_workflow(workflow_id) or {}
    pending = inspect_continuation(record, ports.workflow_config_for(workflow_id) or {}, ports.load_tasks())
    if pending is None and not ports.attention_get(key):
        return
    queued = None
    notify = False
    interval = ports.liveness.attention_retry_interval()
    with ports._attention_store.transaction() as episodes:
        if pending is None:
            episodes.pop(key, None)
            return
        episode = episodes.get(key) or {}
        if episode.get("fingerprint") != pending["fingerprint"]:
            episode = dict(pending, first_seen_at=now, attempts=0, claims=0,
                           next_retry_at=max(now, pending["last_progress_at"] + ports.liveness.stage_advance_sla()))
        if now >= episode["next_retry_at"] and not episode.get("escalated"):
            if episode["attempts"] >= 2 or now - episode["first_seen_at"] >= 2 * interval:
                episode.update(escalated=True, notification_pending=True, notify_after=now)
            else:
                episode["claims"] += 1
                episode["next_retry_at"] = now + interval
                queued = {"kind": "workflow_continuation", "workflow_id": workflow_id,
                          "key": key, "fingerprint": pending["fingerprint"],
                          "claim": episode["claims"]}
        if episode.get("notification_pending") and now >= episode.get("notify_after", 0):
            episode["notify_after"] = now + interval
            notify = True
        episode.update(reason="workflow_continuation", event_type="workflow_continuation")
        episodes[key] = episode
    if queued:
        ports.coordinator_queue.put(queued)
    if notify:
        delivered = ports.notify_attention("Herdr Factory · 工作流等待人工推进", record,
                                     pending["message"], "workflow_continuation")
        with ports._attention_store.transaction() as episodes:
            current = episodes.get(key)
            if current and current.get("fingerprint") == pending["fingerprint"]:
                current["notification_pending"] = not delivered

def continuation_coordinator(ports, record):
    """Resolve the canonical named coordinator; a cached Pane ID is not ownership."""
    from herdr.projects import coordinator_agent_name
    if not record.get("project_id"):
        return None
    name = coordinator_agent_name(record["project_id"])
    try:
        result = ports.subprocess.run(["herdr", "agent", "get", name], text=True,
                                capture_output=True, timeout=5)
        if result.returncode != 0:
            return None
        agent = ports.json.loads(result.stdout).get("result", {}).get("agent", {})
        if agent.get("name") == name and agent.get("agent_status") in ("idle", "done"):
            return name
    except (OSError, ports.subprocess.TimeoutExpired, ValueError, AttributeError):
        pass
    return None

def handle_workflow_continuation(ports, item):
    """Revalidate under the existing per-workflow executor before any prompt."""
    from herdr.projects import inspect_continuation
    wid = item["workflow_id"]
    record = ports.project_for_workflow(wid) or {}
    pending = inspect_continuation(record, ports.workflow_config_for(wid) or {}, ports.load_tasks())
    if pending is None or pending["fingerprint"] != item["fingerprint"]:
        return
    coordinator = ports.continuation_coordinator(record)
    if coordinator is None:
        return
    # Claim a send only after a live, owned coordinator is ready. A duplicate
    # queue item or a lost process cannot send the same claim twice.
    with ports._attention_store.transaction() as episodes:
        episode = episodes.get(item["key"]) or {}
        if (episode.get("fingerprint") != item["fingerprint"] or episode.get("escalated")
                or episode.get("claims") != item["claim"]
                or episode.get("last_sent_claim", 0) >= item["claim"] or episode.get("attempts", 0) >= 2):
            return
        episode["last_sent_claim"] = item["claim"]
        episode["attempts"] += 1
    message = ("HERDR_WORKFLOW_CONTINUATION_EVENT\n" +
               ports.json.dumps(pending, ensure_ascii=False) +
               "\n请核对计划和持久集成引用，完成授权范围内的合流与缺失 Task 续派。"
               "先检查是否已有同 ID 或替代 Task，遵守串行屏障和候选版本门禁。"
               "不得 force-pass、删除计划义务、扩大范围或重复创建任务。"
               "若需人工裁决请落盘明确阻塞；通知收到不代表义务完成。\n" + ports.COORDINATOR_DISCIPLINE)
    try:
        ports.subprocess.run(["herdr", "agent", "prompt", coordinator, message, "--wait", "--timeout", "30000"],
                       text=True, capture_output=True, timeout=35)
    except (OSError, ports.subprocess.TimeoutExpired) as exc:
        print(f"[CONTINUATION DELIVERY ERROR] workflow={wid}: {type(exc).__name__}")

def schedule_workflow_continuations(ports, workflow_ids, now):
    """One bounded background scan, without blocking the fast stage sweep."""
    if not ports._continuation_scan_lock.acquire(blocking=False):
        return
    def scan():
        try:
            for wid in workflow_ids:
                try:
                    ports.check_workflow_recovery(wid)
                except Exception as exc:
                    print(f"[RECOVERY CHECK ERROR] workflow={wid}: {type(exc).__name__}")
                try:
                    ports.check_workflow_continuation(wid, now=now)
                except Exception as exc:
                    print(f"[CONTINUATION CHECK ERROR] workflow={wid}: {type(exc).__name__}")
        finally:
            ports._continuation_scan_lock.release()
    try:
        ports._CONTINUATION_EXECUTOR.submit(scan)
    except RuntimeError:
        ports._continuation_scan_lock.release()


DEPENDENCIES = {'check_workflow_continuation': ('_attention_store', 'attention_get', 'coordinator_queue',
                                 'liveness', 'load_tasks', 'notify_attention',
                                 'project_for_workflow', 'time', 'workflow_config_for'),
 'continuation_coordinator': ('json', 'subprocess'),
 'handle_workflow_continuation': ('COORDINATOR_DISCIPLINE', '_attention_store',
                                  'continuation_coordinator', 'json', 'load_tasks',
                                  'project_for_workflow', 'subprocess', 'workflow_config_for'),
 'schedule_workflow_continuations': ('_CONTINUATION_EXECUTOR', '_continuation_scan_lock',
                                     'check_workflow_continuation', 'check_workflow_recovery')}

__all__ = tuple(DEPENDENCIES)
