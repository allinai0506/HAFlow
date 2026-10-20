"""Schedule bounded background context compaction without delaying task execution.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def _schedule_context_compact(ports, task, wait_for=None, supervisor_done=None):
    """Schedule working-memory creation without joining or affecting done flow."""
    if not task:
        return False
    try:
        from herdr.context_compact import compact_run_best_effort
        from herdr.trajectory import run_id_for_task
        store = ports._get_store()
        target_task_id = str(task.get("task_id") or "")
        target_run_id = str(run_id_for_task(task))
        if not target_task_id or not target_run_id:
            return False

        def worker():
            try:
                if wait_for is not None:
                    wait_for.wait()
                if supervisor_done is not None:
                    supervisor_done.wait()
                from herdr import state_db
                fresh_task = state_db.get_task(
                    target_task_id, db_path=getattr(store, "db_path", None),
                )
                if not fresh_task or str(run_id_for_task(fresh_task)) != target_run_id:
                    print(f"[CONTEXT COMPACT SKIPPED] task={target_task_id}: task/run identity changed")
                    return
                provider = None
                try:
                    from herdr.observer.harness import get_provider
                    provider = get_provider()
                except Exception as provider_exc:
                    print(f"[CONTEXT COMPACT PROVIDER FALLBACK] task={task.get('task_id')}: {type(provider_exc).__name__}: {provider_exc}")
                compact_run_best_effort(
                    target_run_id, task=fresh_task, store=store, provider=provider,
                )
            except Exception as exc:  # defensive boundary isolation
                print(f"[CONTEXT COMPACT WORKER SKIPPED] task={task.get('task_id')}: {type(exc).__name__}: {exc}")

        thread = ports.threading.Thread(
            target=worker,
            name=f"context-compact-{task.get('task_id', 'run')}",
            daemon=True,
        )
        thread.start()
        return True
    except Exception as exc:
        print(f"[CONTEXT COMPACT SKIPPED] task={task.get('task_id')}: {type(exc).__name__}: {exc}")
        return False


DEPENDENCIES = {'_schedule_context_compact': ('_get_store', 'threading')}

__all__ = tuple(DEPENDENCIES)
