"""Task resource teardown operations shared by finalize and workflow close."""
import json
import os
import tarfile
from pathlib import Path


def finalize_one(task, purge_clones=False, dry_run=False, close_claim=None, output_override="", reclaim_archive=None, reclaim_plan=None, *, deps):
    CLONE_ROOT = deps["clone_root"]
    FINALIZE_ADVANCE = deps["finalize_advance"]
    _advance_to_cleaned = deps["advance_to_cleaned"]
    _close_owned_pane = deps["close_owned_pane"]
    _close_task_identity = deps["close_task_identity"]
    _get_store = deps["get_store"]
    _record_output_retention = deps["record_output_retention"]
    _teardown_output_guard = deps["teardown_output_guard"]
    clone_deletable = deps["clone_deletable"]
    close_pane = deps["close_pane"]
    delete_clone_safely = deps["delete_clone_safely"]
    dump_transcript = deps["dump_transcript"]
    set_status = deps["set_status"]
    """对单个已收敛任务执行物理收尾,返回报告行。幂等。"""
    task_id = task.get("task_id")
    status = task.get("status")
    pane_id = task.get("pane_id")

    row = {
        "task_id": task_id,
        "agent": task.get("agent"),
        "stage": task.get("stage"),
        "status": status,
        "pane_id": pane_id,
        "clone_path": task.get("clone_path"),
        "pane_closed": False,
        "evidence": None,
        "clone_deleted": False,
        "clone_retained_reason": None,
        "action": "finalized",
    }

    archive_required = close_claim is not None and not dry_run and not reclaim_archive

    def retain_unarchived_clone():
        clone = task.get("clone_path")
        if archive_required and clone and os.path.lexists(os.path.expanduser(clone)):
            diagnosis = {"reason": "recovery_archive_required"}
            row.update(action="retained", output_guard=diagnosis,
                       clone_retained_reason=diagnosis["reason"], pane_retained_reason=diagnosis["reason"])
            _record_output_retention(task, diagnosis)
            return True
        return False

    if retain_unarchived_clone():
        return row
    output = _teardown_output_guard(task)
    authorization = output_override or ("verified-archive" if reclaim_archive else
                                         "planned-archive" if dry_run and reclaim_plan else
                                         "purge-clones" if purge_clones else "")
    if output["reason"]:
        row["output_guard"] = output
        row["clone_retained_reason"] = output["reason"]
        if not dry_run:
            _record_output_retention(task, output, authorized_via=authorization)
        if not authorization:
            return {**row, "action": "retained", "pane_retained_reason": output["reason"], "pane_would_close": False, "clone_would_delete": False}
        row["output_override"] = authorization
    deletable, reason = clone_deletable(task)

    if dry_run:
        row["action"] = "dry-run"
        row["pane_would_close"] = False
        row["pane_retained_reason"] = "runtime ownership not probed in dry-run"
        row["clone_would_delete"] = bool(
            task.get("clone_path") and (deletable or purge_clones)
        )
        if reclaim_plan:
            row.update(reclaim_plan)
            row["clone_retained_reason"] = None
        elif not deletable and not purge_clones:
            row["clone_retained_reason"] = reason
        return row

    def retain_new_output():
        if reclaim_archive and task.get("clone_path") and os.path.exists(task["clone_path"]):
            from herdr.clone_archive import verify_source
            try:
                verify_source(task, CLONE_ROOT, reclaim_archive)
            except (OSError, ValueError, tarfile.TarError) as exc:
                diagnosis = {"reason": "archive_source_changed", "detail": str(exc)}
                row.update(action="retained", output_guard=diagnosis,
                           clone_retained_reason=diagnosis["reason"], pane_retained_reason=diagnosis["reason"])
                _record_output_retention(task, diagnosis)
                return True
        current_output = _teardown_output_guard(task)
        if current_output["reason"] and not authorization:
            row.update(action="retained", output_guard=current_output,
                       clone_retained_reason=current_output["reason"], pane_retained_reason=current_output["reason"])
            _record_output_retention(task, current_output)
            return True
        return False

    if close_claim is None:
        row["evidence"] = dump_transcript(task)
        if retain_new_output():
            return row
        row["pane_closed"] = close_pane(pane_id)
    else:
        identity = _close_task_identity(task)
        def close_owned():
            evidence = dump_transcript(task)
            # Transcript collection may race with runtime replacement.
            return {"closed": close_pane(pane_id) if _close_owned_pane(identity) and not retain_new_output() else False,
                    "evidence": evidence}
        action = close_claim.action(
            "pane:" + str(pane_id) + ':' + __import__('hashlib').sha256(
                json.dumps(identity, sort_keys=True).encode()).hexdigest(),
            identity, lambda value: not retain_new_output() and _close_owned_pane(value), close_owned,
        )
        result = action.get("result") or {}
        row["evidence"] = result.get("evidence")
        row["pane_closed"] = bool(result.get("closed"))
        if not row["pane_closed"]:
            row["pane_retained_reason"] = "runtime ownership unknown, changed, absent, or close failed"

    if row.get("action") == "retained":
        return row
    if retain_new_output():
        return row
    deletable, reason = clone_deletable(task)
    if task.get("clone_path"):
        if archive_required:
            # Never invoke delete without a workflow recovery point, even if
            # the clone appeared after preflight or after the initial check.
            retain_unarchived_clone()
        elif deletable or purge_clones or reclaim_archive:
            if close_claim is None:
                row["clone_deleted"] = delete_clone_safely(task["clone_path"])
            else:
                clone = os.path.realpath(os.path.expanduser(task["clone_path"]))
                stat = os.stat(clone) if os.path.isdir(clone) else None
                identity = {"path": clone, "device": stat.st_dev if stat else None,
                            "inode": stat.st_ino if stat else None}
                def owns_clone(original):
                    try:
                        current = os.stat(original["path"])
                        if (current.st_dev, current.st_ino) == (original["device"], original["inode"]):
                            return not retain_new_output()
                        from herdr.clone_archive import is_residual_clone_shell
                        if is_residual_clone_shell(Path(original["path"])):
                            return not retain_new_output()
                        return False
                    except OSError:
                        return False
                def delete_owned_clone():
                    if retain_new_output():
                        return False
                    if reclaim_archive:
                        from herdr import kernel
                        kernel.update_task_metadata(task_id, {
                            "clone_reclaim_started": reclaim_archive["manifest_sha256"],
                        }, store=_get_store())
                    path_obj = Path(clone)
                    from herdr.clone_archive import is_residual_clone_shell, delete_residual_clone_shell
                    if is_residual_clone_shell(path_obj):
                        return delete_residual_clone_shell(path_obj, clone_root=CLONE_ROOT)
                    return delete_clone_safely(clone)
                action = close_claim.action(
                    "clone:" + task_id, identity, owns_clone, delete_owned_clone,
                    retry_incomplete=bool(reclaim_archive),
                )
                row["clone_deleted"] = bool(action.get("result"))
            if not row["clone_deleted"]:
                row["clone_retained_reason"] = "clone path absent or unsafe"
        else:
            row["clone_retained_reason"] = reason

    if row.get("action") == "retained":
        return row

    if status in FINALIZE_ADVANCE:
        row["status"] = _advance_to_cleaned(task_id, status)

    store = _get_store()
    record = store.get_task(task_id)
    if record:
        from herdr import kernel
        meta_updates = {
            "pane_retained": bool(close_claim and not row["pane_closed"] and pane_id),
            "resource_retention": "retained" if (close_claim and not row["pane_closed"] and pane_id) or (task.get("clone_path") and not row["clone_deleted"] and os.path.exists(task["clone_path"])) else "purged",
            "clone_retained": bool(task.get("clone_path") and not row["clone_deleted"] and os.path.exists(task["clone_path"])),
        }
        if row["evidence"]:
            meta_updates["evidence"] = row["evidence"]
        kernel.update_task_metadata(task_id, meta_updates, store=store)
    return row