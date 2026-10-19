"""Task resource teardown operations shared by finalize and workflow close."""

import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path


def finalize_one(
    task,
    purge_clones=False,
    dry_run=False,
    close_claim=None,
    output_override="",
    reclaim_archive=None,
    reclaim_plan=None,
    *,
    deps,
):
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
    "对单个已收敛任务执行物理收尾,返回报告行。幂等。"
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
    archive_required = (
        close_claim is not None and (not dry_run) and (not reclaim_archive)
    )

    def retain_unarchived_clone():
        clone = task.get("clone_path")
        if archive_required and clone and os.path.lexists(os.path.expanduser(clone)):
            diagnosis = {"reason": "recovery_archive_required"}
            row.update(
                action="retained",
                output_guard=diagnosis,
                clone_retained_reason=diagnosis["reason"],
                pane_retained_reason=diagnosis["reason"],
            )
            _record_output_retention(task, diagnosis)
            return True
        return False

    if retain_unarchived_clone():
        return row
    output = _teardown_output_guard(task)
    authorization = output_override or (
        "verified-archive"
        if reclaim_archive
        else (
            "planned-archive"
            if dry_run and reclaim_plan
            else "purge-clones" if purge_clones else ""
        )
    )
    if output["reason"]:
        row["output_guard"] = output
        row["clone_retained_reason"] = output["reason"]
        if not dry_run:
            _record_output_retention(task, output, authorized_via=authorization)
        if not authorization:
            return {
                **row,
                "action": "retained",
                "pane_retained_reason": output["reason"],
                "pane_would_close": False,
                "clone_would_delete": False,
            }
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
        elif not deletable and (not purge_clones):
            row["clone_retained_reason"] = reason
        return row

    def retain_new_output():
        if (
            reclaim_archive
            and task.get("clone_path")
            and os.path.exists(task["clone_path"])
        ):
            from herdr.clone_archive import verify_source

            try:
                verify_source(task, CLONE_ROOT, reclaim_archive)
            except (OSError, ValueError, tarfile.TarError) as exc:
                diagnosis = {"reason": "archive_source_changed", "detail": str(exc)}
                row.update(
                    action="retained",
                    output_guard=diagnosis,
                    clone_retained_reason=diagnosis["reason"],
                    pane_retained_reason=diagnosis["reason"],
                )
                _record_output_retention(task, diagnosis)
                return True
        current_output = _teardown_output_guard(task)
        if current_output["reason"] and (not authorization):
            row.update(
                action="retained",
                output_guard=current_output,
                clone_retained_reason=current_output["reason"],
                pane_retained_reason=current_output["reason"],
            )
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
            return {
                "closed": (
                    close_pane(pane_id)
                    if _close_owned_pane(identity) and (not retain_new_output())
                    else False
                ),
                "evidence": evidence,
            }

        action = close_claim.action(
            "pane:"
            + str(pane_id)
            + ":"
            + __import__("hashlib")
            .sha256(json.dumps(identity, sort_keys=True).encode())
            .hexdigest(),
            identity,
            lambda value: not retain_new_output() and _close_owned_pane(value),
            close_owned,
        )
        result = action.get("result") or {}
        row["evidence"] = result.get("evidence")
        row["pane_closed"] = bool(result.get("closed"))
        if not row["pane_closed"]:
            row["pane_retained_reason"] = (
                "runtime ownership unknown, changed, absent, or close failed"
            )
    if row.get("action") == "retained":
        return row
    if retain_new_output():
        return row
    deletable, reason = clone_deletable(task)
    if task.get("clone_path"):
        if archive_required:
            retain_unarchived_clone()
        elif deletable or purge_clones or reclaim_archive:
            if close_claim is None:
                row["clone_deleted"] = delete_clone_safely(task["clone_path"])
            else:
                clone = os.path.realpath(os.path.expanduser(task["clone_path"]))
                stat = os.stat(clone) if os.path.isdir(clone) else None
                identity = {
                    "path": clone,
                    "device": stat.st_dev if stat else None,
                    "inode": stat.st_ino if stat else None,
                }

                def owns_clone(original):
                    try:
                        current = os.stat(original["path"])
                        if (current.st_dev, current.st_ino) == (
                            original["device"],
                            original["inode"],
                        ):
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

                        kernel.update_task_metadata(
                            task_id,
                            {
                                "clone_reclaim_started": reclaim_archive[
                                    "manifest_sha256"
                                ]
                            },
                            store=_get_store(),
                        )
                    path_obj = Path(clone)
                    from herdr.clone_archive import (
                        is_residual_clone_shell,
                        delete_residual_clone_shell,
                    )

                    if is_residual_clone_shell(path_obj):
                        return delete_residual_clone_shell(
                            path_obj, clone_root=CLONE_ROOT
                        )
                    return delete_clone_safely(clone)

                action = close_claim.action(
                    "clone:" + task_id,
                    identity,
                    owns_clone,
                    delete_owned_clone,
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
            "pane_retained": bool(close_claim and (not row["pane_closed"]) and pane_id),
            "resource_retention": (
                "retained"
                if close_claim
                and (not row["pane_closed"])
                and pane_id
                or (
                    task.get("clone_path")
                    and (not row["clone_deleted"])
                    and os.path.exists(task["clone_path"])
                )
                else "purged"
            ),
            "clone_retained": bool(
                task.get("clone_path")
                and (not row["clone_deleted"])
                and os.path.exists(task["clone_path"])
            ),
        }
        if row["evidence"]:
            meta_updates["evidence"] = row["evidence"]
        kernel.update_task_metadata(task_id, meta_updates, store=store)
    return row


def herdr_command(*args, deps):
    resolve_binary = deps["resolve_binary"]
    run_command = deps["run_command"]
    "调用 herdr CLI;显式解析绝对路径以兼容 LaunchAgent 精简 PATH。"
    binary = resolve_binary("herdr") or "herdr"
    return subprocess.run([binary, *args], text=True, capture_output=True, timeout=20)


def delete_clone_safely(clone_path, *, deps):
    CLONE_ROOT = deps["clone_root"]
    "删除 CoW clone;仅允许 ~/.herdr-controller/clones 下的路径。"
    if not clone_path:
        return False
    clone_real = os.path.realpath(os.path.expanduser(clone_path))
    try:
        common = os.path.commonpath([CLONE_ROOT, clone_real])
    except ValueError:
        return False
    if common != CLONE_ROOT or clone_real == CLONE_ROOT:
        return False
    if not os.path.exists(clone_real):
        return False
    shutil.rmtree(clone_real)
    return True


def _teardown_output_guard(task):
    """Bounded read-only output gate before closing a Pane or deleting its clone."""
    from herdr.repo_hygiene import teardown_output_diagnosis
    from herdr.bounded_tools import run_bounded

    clone = task.get("clone_path")
    if clone and os.path.lexists(os.path.expanduser(clone)):
        path = Path(clone).expanduser()
        if path.is_symlink():
            return {"reason": "clone_identity_unknown"}
        from herdr.clone_archive import is_residual_clone_shell

        if task.get("clone_archive") and is_residual_clone_shell(path):
            return {"reason": ""}
        tag = path / ".herdr-launch-identity.json"
        if tag.exists():
            try:
                identity = (
                    json.loads(tag.read_text())
                    if not tag.is_symlink() and tag.stat().st_size <= 8192
                    else {}
                )
            except (OSError, ValueError):
                identity = {}
            if identity.get("task_id") != task.get("task_id") or (
                task.get("run_id") and identity.get("run_id") != task.get("run_id")
            ):
                return {"reason": "clone_identity_changed"}
        if (
            task.get("execution_mode") == "context"
            and (task.get("integration_mode") or "none") == "none"
            and path.is_dir()
            and (not os.path.lexists(path / ".git"))
        ):
            return {"reason": ""}
        try:
            root = run_bounded(
                ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                timeout=5,
                output_limit=8192,
            )
            if (
                root["status"] != "completed"
                or root["exit_code"]
                or Path(root["stdout"].strip()).resolve() != path.resolve()
            ):
                return {"reason": "clone_git_inventory_unknown"}
            result = run_bounded(
                [
                    "git",
                    "-C",
                    str(path),
                    "status",
                    "--porcelain",
                    "--untracked-files=normal",
                ],
                timeout=5,
                output_limit=65536,
            )
            expected_commit = task.get("integrated_commit") or task.get("commit")
            head_sha = ""
            head_out = run_bounded(
                ["git", "-C", str(path), "rev-parse", "HEAD"],
                timeout=5,
                output_limit=8192,
            )
            if head_out["status"] != "completed":
                return {"reason": "clone_git_inventory_unknown"}
            if head_out["exit_code"] == 0:
                head_sha = head_out["stdout"].strip()
            elif expected_commit:
                return {"reason": "clone_git_inventory_unknown"}
        except (OSError, ValueError):
            return {"reason": "clone_git_inventory_unknown"}
        diagnosis = teardown_output_diagnosis(
            result["stdout"], task_id=task.get("task_id", "")
        )
        if diagnosis["reason"]:
            if (task.get("integration_mode") or "none") == "none":
                diagnosis["remediation_cmd"] = (
                    f"git -C <clone> status; herdr-task commit {task.get('task_id', '')}"
                )
            return diagnosis
        if expected_commit and head_sha and (head_sha != expected_commit):
            cmd = (
                f"git -C <clone> status; herdr-task commit {task.get('task_id', '')}"
                if (task.get("integration_mode") or "none") == "none"
                else f"git -C <clone> log; herdr-task integrate {task.get('task_id', '')}"
            )
            return {"reason": "unintegrated_commit_diverged", "remediation_cmd": cmd}
    if task.get("integration_mode") == "git" and task.get("status") in {
        "completed",
        "committed",
    }:
        return {
            "reason": "git_finalize_pending",
            "remediation_cmd": "herdr-task commit "
            + str(task.get("task_id"))
            + "; herdr-task integrate "
            + str(task.get("task_id")),
        }
    return {"reason": ""}


def _record_output_retention(task, diagnosis, *, authorized_via="", deps):
    _get_store = deps["get_store"]
    store = _get_store()
    if store.get_task(task.get("task_id")):
        from herdr import kernel

        kernel.update_task_metadata(
            task["task_id"],
            {"resource_retention": "retained", "output_retention": diagnosis},
            store=store,
        )
        store.record_event(
            (
                "teardown_output_retained"
                if not authorized_via
                else "teardown_output_override"
            ),
            {**diagnosis, "authorized_via": authorized_via},
            task_id=task["task_id"],
            workflow_id=task.get("workflow_id"),
            source="herdr-task",
        )


def clone_deletable(task, *, deps):
    _teardown_output_guard = deps["teardown_output_guard"]
    "clone 承载的成果是否已安全沉淀。\n\n    integration_ref/branch 存在说明 Controller 已完成 integrate;\n    superseded 任务从未交付,由替代任务负责成果。其余情况\n    (如 mode=none 的 docs 任务)成果只存在于 clone 与终端转写中,\n    默认保留 clone,由 --purge-clones 显式授权删除。\n    "
    output = _teardown_output_guard(task)
    if output["reason"]:
        return (False, output["reason"])
    if (
        task.get("execution_mode") == "context"
        and (task.get("integration_mode") or "none") == "none"
    ):
        return (False, "context artifacts retained; explicit purge required")
    if task.get("integration_ref") or task.get("integration_branch"):
        return (True, "integrated")
    if task.get("status") == "superseded":
        return (True, "superseded")
    if task.get("status") == "committed":
        return (False, "committed but not integrated — run herdr-task integrate first")
    return (False, "no integration evidence")


def dump_transcript(task, *, deps):
    _herdr = deps["herdr"]
    _now = deps["now"]
    EVIDENCE_ROOT = deps["evidence_root"]
    "把 pane 终端转写落盘为验收证据;尽力而为,失败不阻塞。"
    pane_id = task.get("pane_id")
    if not pane_id:
        return None
    read = _herdr(
        "pane", "read", pane_id, "--source", "recent-unwrapped", "--lines", "20000"
    )
    if read.returncode != 0 or not read.stdout.strip():
        return None
    task_dir = os.path.join(EVIDENCE_ROOT, task.get("task_id", "unknown"))
    os.makedirs(task_dir, exist_ok=True)
    path = os.path.join(task_dir, "terminal.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write(read.stdout)
    meta = {
        "task_id": task.get("task_id"),
        "pane_id": pane_id,
        "agent": task.get("agent"),
        "dumped_at": _now(),
        "agent_session": None,
    }
    info = _herdr("pane", "get", pane_id)
    try:
        pane = json.loads(info.stdout)["result"]["pane"]
        meta["agent_session"] = pane.get("agent_session")
    except Exception:
        pass
    with open(os.path.join(task_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return path


def close_pane(pane_id, *, deps):
    _get_store = deps["get_store"]
    _herdr = deps["herdr"]
    if not pane_id:
        return False
    from herdr.task_resources import workflow_launch_lock

    with workflow_launch_lock(_get_store().db_path, "managed-dynamic-pane-lifecycle"):
        return _herdr("pane", "close", pane_id).returncode == 0


def _advance_to_cleaned(task_id, status, *, deps):
    FINALIZE_ADVANCE = deps["finalize_advance"]
    set_status = deps["set_status"]
    "按 FINALIZE_ADVANCE 链把已验收任务推进到 cleaned。"
    while status in FINALIZE_ADVANCE:
        nxt = FINALIZE_ADVANCE[status]
        try:
            set_status(task_id, nxt)
        except SystemExit:
            break
        status = nxt
    return status


def _close_task_identity(task):
    return {
        key: task.get(key)
        for key in (
            "task_id",
            "workflow_id",
            "run_id",
            "pane_id",
            "agent",
            "agent_session_id",
            "agent_name",
            "runtime",
        )
    }


def _close_owned_pane(identity, *, deps):
    _get_store = deps["get_store"]
    _herdr = deps["herdr"]
    from herdr.task_resources import owned_live_pane
    from herdr.observer.live import probe_live_runtime

    def runner(argv, timeout):
        result = _herdr(*argv[1:])
        return (result.returncode, result.stdout, result.stderr)

    current = (
        _get_store().get_task(identity.get("task_id"))
        if identity.get("task_id")
        else None
    )
    if identity.get("task_id") and current is None:
        return False
    if current and _close_task_identity(current) != identity:
        return False
    proven, _ = owned_live_pane(
        identity, probe=lambda task: probe_live_runtime(task, runner=runner)
    )
    return proven
