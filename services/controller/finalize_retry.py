"""Recover and back off pending Git finalization attempts.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def _check_finalize_retry(ports, task, status, now):
    """Shared finalize-retry driver for completed/commit Retry paths.

    Returns True when a finalize attempt was made or exhaustion was recorded.

    M-2 (AC4-2/AC4-3): deterministic outcomes (rc=3 EMPTY, rc=4 REFUSED,
    rc=5 integrate-blocked, rc=6 conflict) never consume retry budget and
    rc=4 never emits a ``commit_retry`` log. ``finalize_completed_task``
    reports ``retryable``; only retryable attempts log
    ``[REGISTRY WATCHER] ... retry finalize`` and increment the attention
    episode. Already-escalated tasks are settled and skip re-driving
    entirely (revoke via ``clear_finalize_escalation``). M-6:
    ``subprocess.CalledProcessError`` from ``check=True``/``check_output``
    paths is escalated immediately instead of bypassing budget accounting.
    """
    task = task or {}
    task_id = task.get("task_id")
    if not task_id:
        return False
    if task.get("finalize_escalated"):
        return True
    mode = task.get("integration_mode") or "none"
    if mode not in ("git", "none") or ports.workflow_closed(
        task.get("workflow_id")
    ):
        ports.attention_clear(f"{task_id}:finalize")
        ports._finalize_retry_exhausted_logged.discard(task_id)
        return False
    key = f"{task_id}:finalize"
    owner_episode = ports.blocker_queue_episode(task)
    def expire_owned_wait(current):
        detail = current.get("detail") or {}
        if (current.get("reason") == "git_index_lock"
                and isinstance(detail, dict) and detail.get("owner_episode")
                and detail["owner_episode"] != owner_episode):
            return {"__replace__": {}}
        return None
    # Only newly typed waits have a trustworthy owner. Never reset generic
    # failures or legacy escalations; metadata-only saves retain history ID.
    ports._attention_store.mutate(key, expire_owned_wait)
    # P1 recovery coherence: `herdr-task clear-escalation` (or any external
    # recovery) removes the shared attention episode from another process.
    # The in-memory exhausted latch must follow the episode: otherwise a
    # later re-exhaustion would stall silently with no log and no
    # re-escalation because the stale latch suppresses both.
    if ports.attention_get(key) is None:
        ports._finalize_retry_exhausted_logged.discard(task_id)
    try:
        retry, reason, exhausted = ports.should_retry_finalize(
            status, ports.attention_get(key), now
        )
    except (OSError, RuntimeError, ValueError, KeyError,
            ports.subprocess.SubprocessError) as exc:
        print(f"[FINALIZE RETRY ERROR] task={task_id}: {exc}")
        ports._escalate_finalize(ports.get_task(task_id) or task, "retry_driver_error",
                           {"error": f"{type(exc).__name__}: {exc}"})
        return True
    if exhausted:
        if task_id not in ports._finalize_retry_exhausted_logged:
            ports._finalize_retry_exhausted_logged.add(task_id)
            print(
                f"[FINALIZE RETRY EXHAUSTED] task={task_id} "
                f"status={status} attempts>={ports.FINALIZE_RETRY_MAX} "
                "-> manual/coordinator intervention required"
            )
            ports._escalate_finalize(
                ports.get_task(task_id) or task,
                "retry_exhausted",
                {"status": status, "reason": reason},
            )
        return True
    if not retry:
        return False
    wait_episode_before = ports.attention_get(key) or {}
    try:
        outcome = ports.finalize_completed_task(task_id)
    except (OSError, RuntimeError, ValueError, KeyError,
            ports.subprocess.SubprocessError) as exc:
        # M-6: integrate check=True / check_output raises
        # CalledProcessError (SubprocessError, not OSError). Without this
        # the exception escapes to registry_watcher, skips attention
        # budgeting, and retries every ~1s with no backoff.
        print(f"[FINALIZE SUBPROCESS ERROR] task={task_id}: "
              f"{type(exc).__name__}: {exc}")
        ports._escalate_finalize(ports.get_task(task_id) or task,
                           "finalize_subprocess_error",
                           {"error": f"{type(exc).__name__}: {exc}"[:500]})
        return True
    if (isinstance(outcome, dict) and outcome.get("kind") == "wait"
            and outcome.get("reason") == "git_index_lock"
            and outcome.get("budgeted") is False):
        fresh = ports.get_task(task_id)
        if (fresh and fresh.get("status") == status
                and fresh.get("run_id") == task.get("run_id")
                and fresh.get("version") == task.get("version")):
            def persist_wait(current):
                # Cross-process CAS under the existing EpisodeStore lock:
                # do not overwrite an episode another driver changed while
                # the CLI or the authoritative task read was in flight.
                if current != wait_episode_before:
                    return None
                episode = dict(current or {})
                episode.update(task_id=task_id, workflow_id=fresh.get("workflow_id"),
                               event_type="finalize", reason="git_index_lock",
                               attempts=int(episode.get("attempts") or 0),
                               last_attempt_at=ports.time.time(), next_retry_at=now + 60,
                               detail={"run_id": fresh.get("run_id"), "version": fresh.get("version"),
                                       "owner_episode": owner_episode})
                episode.setdefault("first_seen_at", ports.time.time())
                return episode
            ports._attention_store.mutate(key, persist_wait)
        # A late result must not clear or overwrite another run/transition's
        # shared-key episode. Leave its current owner to drive recovery.
        return True
    retryable = True
    if isinstance(outcome, dict) and "retryable" in outcome:
        retryable = bool(outcome.get("retryable"))
    cur_t = ports.get_task(task_id)
    if not retryable:
        # Deterministic failure (EMPTY/REFUSED): settled via escalation or
        # auto-release, no budget consumed, no retry log (AC4-2/AC4-3).
        if cur_t and cur_t.get("status") != status:
            ports.attention_clear(key)
            ports._finalize_retry_exhausted_logged.discard(task_id)
        return True
    print(
        f"[REGISTRY WATCHER] "
        f"task={task_id} "
        f"status={status} -> retry finalize ({reason})"
    )
    if cur_t and cur_t.get("status") == status:
        episode = ports.attention_get(key) or {}
        ports.attention_note(
            key,
            task,
            "finalize",
            reason=reason,
            attempts=int(episode.get("attempts") or 0) + 1,
        )
        ports.attention_throttle(key, now=now)
    else:
        ports.attention_clear(key)
        ports._finalize_retry_exhausted_logged.discard(task_id)
    return True


DEPENDENCIES = {'_check_finalize_retry': ('FINALIZE_RETRY_MAX', '_attention_store', '_escalate_finalize',
                           '_finalize_retry_exhausted_logged', 'attention_clear', 'attention_get',
                           'attention_note', 'attention_throttle', 'blocker_queue_episode',
                           'finalize_completed_task', 'get_task', 'should_retry_finalize',
                           'subprocess', 'time', 'workflow_closed')}

__all__ = tuple(DEPENDENCIES)
