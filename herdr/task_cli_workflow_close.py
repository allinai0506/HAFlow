"""Owned workflow close and physical task teardown orchestration."""

import json
import os
import sys
import tarfile
from pathlib import Path


def _experience_collector_factory(hub_client=None):
    """经验采集器工厂（可注入点，PR #377 测试与实现共用）。

    Collector 自己的 local_collector_records 库保持默认（Sentinel 周期
    drain 的同一个库）；db_path 参数误传 workflow 库会把 close 期记录
    写进 recovery 永远捞不到的地方（Codex 三轮 P1）。
    """
    from herdr.experience.client import ExperienceClient
    from herdr.experience.collector import ExperienceCollector
    hub = hub_client
    if hub is None:
        try:
            client = ExperienceClient()
            if client.token:
                hub = client
        except Exception:
            hub = None
    return ExperienceCollector(hub_client=hub)


def _experience_background_drain(state_db_path=None, hub_client=None):
    """后台单飞 drain：Hub 上传离开 close 同步路径（Hub 不可达不阻塞收尾）。

    透传 close 期已认证的 Hub client——trigger_background_sync 自建的
    collector 只在 HERDR_HUB_URL/TOKEN 环境变量存在时才有 Hub 客户端，
    丢弃 client 会让 token-file 凭据场景静默 no_hub_client（Codex 三轮
    P1）。
    """
    from herdr.experience.collector import trigger_background_sync
    return trigger_background_sync(state_db_path=state_db_path, hub_client=hub_client)


def _post_close_experience_sync(workflow_id, db_path=None):
    """收尾后的经验同步（Codex 二轮三项，PR #377 backlog 修复）：

    1. 尊重 HERDR_EXPERIENCE_COLLECTION_DISABLED（与
       _trigger_experience_collection_best_effort 同一 opt-out 守卫）；
    2. close 只做本地持久化（sync_to_hub=False），Hub 上传交给后台单飞
       drain——Hub 不可达时 close 不被 8s×N 请求超时拖住；
    3. 采集任务事实绑定 close 所用库（db_path=workflow state db）；
       collector 记录库保持默认（与 recovery drain 同库，Codex 三轮 P1）；
    4. 已认证 Hub client 透传给后台 drain。
    """
    import os
    if os.environ.get("HERDR_EXPERIENCE_COLLECTION_DISABLED") == "1":
        print("[EXPERIENCE SYNC] disabled by HERDR_EXPERIENCE_COLLECTION_DISABLED")
        return
    try:
        collector = _experience_collector_factory()
        collector.collect_and_sync_lessons(sync_to_hub=False)
        collector.collect_workflow_tasks(workflow_id, state_db_path=db_path)
        _experience_background_drain(db_path, hub_client=getattr(collector, "hub_client", None))
    except Exception as exc:
        print(f"[EXPERIENCE SYNC NOTICE] post-close sync: {exc}")


def close_workflow(
    workflow_id,
    include_coordinator=False,
    purge_clones=False,
    dry_run=False,
    abandon=False,
    force=False,
    accept_escalated=False,
    confirm_force=None,
    *,
    get_store,
    workflow_close_claim,
    business_gate_blockers,
    validate_delivery_inventory,
    close_workflow_owned,
    build_delivery_report,
):
    """Validate, claim, execute and receipt a workflow close operation."""
    if not abandon:
        store = get_store()
        if store.get_workflow(workflow_id):
            authority, config, owned_tasks = store.read_workflow_snapshot(workflow_id)
            try:
                validate_delivery_inventory(authority, config, owned_tasks)
            except ValueError as exc:
                print(f"[CLOSE ABORT] {exc}")
                sys.exit(2)
            missing_business = business_gate_blockers(
                store, authority, config, owned_tasks
            )
            if missing_business:
                print(
                    "[CLOSE ABORT] business acceptance unavailable: "
                    + ", ".join(missing_business)
                )
                sys.exit(2)
    options = dict(
        include_coordinator=include_coordinator,
        purge_clones=purge_clones,
        abandon=abandon,
        force=force,
        accept_escalated=accept_escalated,
    )
    with workflow_close_claim(
        get_store(), workflow_id, dry_run=dry_run, options=options
    ) as claim:
        if claim.state == "completed":
            store = get_store()
            if hasattr(store, 'get_workflow'):
                current = store.get_workflow(workflow_id)
                if current and current.get('status') != 'completed':
                    raise ValueError('workflow status conflicts with completed close receipt; inspect and reconcile explicitly')
            print(json.dumps(claim.receipt, ensure_ascii=False))
            return claim.receipt
        if claim.state == "in_progress":
            report = {"workflow_id": workflow_id, "close_state": "in_progress"}
            print(json.dumps(report))
            return report
        report = close_workflow_owned(
            workflow_id,
            include_coordinator,
            purge_clones,
            dry_run,
            abandon,
            force,
            accept_escalated,
            confirm_force,
            close_claim=claim if not dry_run else None,
        )
        facts = build_delivery_report(workflow_id, get_store())
        import hashlib

        report["delivery_report"] = facts
        report["delivery_report_sha256"] = hashlib.sha256(
            json.dumps(facts, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        report["execution_mode"] = "dry-run" if dry_run else "execute"
        return report if dry_run else claim.complete(report, require_workflow_completed=True)


def close_workflow_owned(
    workflow_id,
    include_coordinator=False,
    purge_clones=False,
    dry_run=False,
    abandon=False,
    force=False,
    accept_escalated=False,
    confirm_force=None,
    close_claim=None,
    *,
    load_tasks,
    load_workflow_entry,
    get_store,
    finalize_one,
    close_pane,
    stage_reset,
    mark_workflow_completed,
    workflow_stage_tabs,
    live_workspace_panes,
    foreign_panes_in_tab,
    close_owned_pane,
    teardown_output_guard,
    record_output_retention,
    clone_root,
    teardown_blocking_statuses,
):
    data = load_tasks()
    tasks = [t for t in data["tasks"] if t.get("workflow_id") == workflow_id]
    entry, _ = load_workflow_entry(workflow_id)
    if not tasks and entry is None:
        print(f"Unknown workflow (no tasks, no record): {workflow_id}")
        sys.exit(1)

    # 1. Preflight Gate: Validate workflow transition to 'closing' BEFORE any teardown side effects
    if entry:
        from herdr.transitions import validate_workflow_transition

        validate_workflow_transition(
            entry.get("status", "pending"),
            "closing",
            force=force,
        )

    if not tasks:
        # Empty configured deliveries were rejected before acquiring close ownership.
        print(f"[CLOSE REPORT] workflow={workflow_id} (no tasks)")
        if not dry_run:
            if entry.get("status") != "completed":
                mark_workflow_completed(
                    workflow_id,
                    outcome="abandoned" if abandon else "delivered",
                    force=force,
                )
            stage_reset(workflow_id)
            print(f"[WORKFLOW CLOSED] {workflow_id}")
        else:
            print("[DRY RUN] no changes were made")
        return {
            "workflow_id": workflow_id,
            "dry_run": dry_run,
            "outcome": "abandoned" if abandon else "delivered",
            "tasks": [],
            "tabs_closed": [],
            "tabs_would_close": [],
            "tabs_skipped": {},
            "coordinator_closed": False,
            "include_coordinator": include_coordinator,
        }

    base_branch = (entry or {}).get("base_branch") or "main"

    blocked_verdicts = [
        t["task_id"]
        for t in tasks
        if t.get("status") != "superseded" and t.get("stage_verdict") == "blocked"
        # accepted_with_limits 是人工裁决的终态放行:裁决文本按约定保留
        # blocked,但运行时(_blocking)不再阻断;close 不应对同一事实给出
        # 更严的二次裁决(wf-haflow-1022-01 requirements-challenger-r2 曾
        # 因此在全部节点通过后仍无法收口)。
        and not (t.get("accepted_with_limits") and not t.get("finalize_escalated"))
    ]
    if blocked_verdicts and not abandon:
        print(
            "[CLOSE ABORT] workflow has blocked gate verdicts awaiting "
            "fix-loop resolution: " + ", ".join(blocked_verdicts)
        )
        print(
            "Resolve via fix-loop (or supersede stale verdicts); "
            "to abandon the deliverable explicitly:"
        )
        print(f"  herdr-task close-workflow {workflow_id} --abandon")
        sys.exit(2)

    blocking = [
        t["task_id"] for t in tasks if t.get("status") in teardown_blocking_statuses
    ]
    if blocking:
        print("[CLOSE ABORT] workflow has unsettled tasks: " + ", ".join(blocking))
        for t in tasks:
            if t.get("task_id") in blocking:
                print(
                    f'  {t["task_id"]}({t.get("status")} @ {t.get("clone_path") or "no-clone"}): '
                    f"wait for settle, steer, or supersede with --reason"
                )
        sys.exit(2)

    # completed/committed 且 git 集成的任务尚未走完 commit/integrate 管线;
    # 物理收尾会把它们直接推进到 cleaned,在跑的 git 子进程随后撞
    # 'cleaned -> committed' 非法转移,交付分支落不进集成链路
    # (2026-09-17 wf-nexusarchive-0917-01 wrapup 实测)。
    unsettled_git = [
        t["task_id"]
        for t in tasks
        if t.get("status") in ("completed", "committed")
        and (t.get("integration_mode") or "none") == "git"
        and not t.get("finalize_escalated")
    ]
    if unsettled_git:
        print(
            "[CLOSE ABORT] workflow has git tasks awaiting finalize "
            "(commit/integrate not settled): " + ", ".join(unsettled_git)
        )
        for t in tasks:
            if t.get("task_id") in unsettled_git:
                print(
                    f'  {t["task_id"]}(completed @ {t.get("clone_path") or "no-clone"}): '
                    f'herdr-task commit {t["task_id"]} / herdr-task integrate {t["task_id"]}'
                )
        print(
            "  等待 Controller 终化重试收敛;或手工收口: "
            "herdr-task commit <task_id>(completed)/ "
            "herdr-task integrate <task_id>(committed);"
        )
        print("  确要丢弃交付物时: herdr-task supersede <task_id> --reason ...")
        sys.exit(2)

    # H-1: machine-set finalize_escalated no longer implies closable.
    # Escalated tasks stay unclosable until a human explicitly confirms
    # via --accept-escalated/--force/--abandon or supersedes the task.
    # Five choices cost model (FR-5.3, shown in help + abort text):
    # --accept-escalated keeps deliverables, scoped to escalated only;
    # --force directly bypasses the human-confirmation contract;
    # --abandon discards the entire workflow deliverable;
    # supersede permanently discards that branch commits;
    # clear-escalation revokes the flag and re-drives integrate (may re-conflict).
    # The gate is mode-agnostic: the none-mode finalize branch escalates too
    # (preservation commit refused, delivery blocked), and its tasks are
    # typically already cleaned by the time close runs — filtering by git
    # mode or pre-teardown status silently waived the contract
    # (wf-haflow-1022-01 wrapup-auto-r2 closed as delivered, unconfirmed).
    escalated = [
        t["task_id"]
        for t in tasks
        if t.get("finalize_escalated") and t.get("status") != "superseded"
    ]
    # --force is the explicitly confirmed direct escape hatch.  It is the
    # historical command-line contract; no second confirmation flag is needed.
    escalated_via = (
        "accept-escalated"
        if accept_escalated
        else ("force" if force else ("abandon" if abandon else ""))
    )
    close_outcome = (
        "abandoned"
        if abandon
        else "escalated_accepted" if escalated and accept_escalated else "delivered"
    )
    if escalated and not (accept_escalated or force or abandon):
        print(
            "[CLOSE ABORT] workflow has escalated tasks "
            "requiring human confirmation: " + ", ".join(escalated)
        )
        print(
            "  finalize_escalated is machine-set and revocable; "
            "it does not imply the deliverable is safe to close."
        )
        print(
            "  Five choices (costs differ, choose explicitly): "
            f"herdr-task close-workflow {workflow_id} --accept-escalated "
            "(keeps deliverables, scoped to escalated only) | "
            f"herdr-task close-workflow {workflow_id} --force "
            "(directly bypasses the whole human-confirmation contract) | "
            f"herdr-task close-workflow {workflow_id} --abandon "
            "(discards the entire workflow deliverable) | "
            "herdr-task supersede <task_id> --reason ... "
            "(permanently discards that branch commits) | "
            "herdr-task clear-escalation <task_id> after recovery "
            "(revokes flag, re-drives integrate, may re-conflict)."
        )
        sys.exit(2)

    # Preflight is read-only: pause may still win until the atomic closing
    # transition. Escape flags authorize output handling, never archive bypass.
    from herdr.clone_archive import plan_clone, preserve_clone

    reclaim_plans = {}
    output_authorizations = {}
    for task in tasks:
        task_id = task["task_id"]
        output = teardown_output_guard(task)
        explicit = (
            "purge-clones"
            if purge_clones
            else (
                "force"
                if force
                else (
                    "abandon"
                    if abandon
                    else (
                        "accept-escalated"
                        if accept_escalated and task_id in escalated
                        else ""
                    )
                )
            )
        )
        clone = task.get("clone_path")
        exists = bool(clone and os.path.lexists(os.path.expanduser(clone)))
        deleting_archive = task.get("clone_archive") and task.get(
            "clone_reclaim_started"
        ) == task["clone_archive"].get("manifest_sha256")
        document_output = (
            output["reason"]
            in {"unpreserved_worktree_output", "unintegrated_commit_diverged"}
            and (task.get("integration_mode") or "none") == "none"
        )
        safe_path = (
            exists
            and not os.path.islink(clone)
            and os.path.commonpath([clone_root, os.path.realpath(clone)]) == clone_root
            and os.path.realpath(clone) != clone_root
        )
        allowed = (
            not output["reason"]
            or explicit
            or task.get("status") == "failed"
            or safe_path
            and (deleting_archive or document_output)
        )
        if output["reason"] and not allowed:
            if not dry_run:
                record_output_retention(task, output)
                shown = dict(output)
                if shown.get("remediation_cmd") and clone:
                    shown["remediation_cmd"] = shown["remediation_cmd"].replace(
                        "<clone>", str(clone)
                    )
                print(
                    "[CLOSE ABORT] unpreserved task output: "
                    + json.dumps({"task_id": task_id, **shown}, ensure_ascii=False)
                )
                sys.exit(2)
            continue
        if exists:
            try:
                reclaim_plans[task_id] = plan_clone(task, clone_root)
            except (OSError, ValueError, tarfile.TarError) as exc:
                print(
                    f"[CLOSE ABORT] sandbox archive planning failed: {task_id}: {exc}"
                )
                sys.exit(2)
        output_authorizations[task_id] = explicit

    # Own the workflow state before the first archive or archive metadata write.
    # SQLite validates the current state in the same transaction as this change;
    # a concurrent pause either wins before this point or is rejected afterwards.
    if not dry_run and entry and entry.get("status") not in {"closing", "completed"}:
        from herdr import kernel

        kernel.transition_workflow(
            workflow_id=workflow_id,
            to_status="closing",
            reason=f"close_workflow: {'abandoned' if abandon else 'delivered'}",
            source="herdr-task",
            force=force,
            store=get_store(),
            expected_status=entry.get("status"),
        )

    archives = {}
    if not dry_run:
        from herdr import kernel

        for task in tasks:
            task_id = task["task_id"]
            if task_id not in reclaim_plans:
                continue
            try:
                if task.get("clone_archive"):
                    archive = preserve_clone(task, clone_root)
                else:
                    from herdr.deliverables import project_for_tasks

                    catalog = project_for_tasks(
                        workflow_id, [task], include_expectations=False
                    )
                    if catalog.get("error") or catalog.get("truncated"):
                        raise ValueError("deliverable inventory incomplete")
                    archive = preserve_clone(task, clone_root)
                    archive["deliverables"] = catalog["items"]
                # A recovery snapshot must not grant an implicit Git-output
                # override when output appeared during the archive operation.
                recovering = task.get("clone_archive") and task.get(
                    "clone_reclaim_started"
                ) == task["clone_archive"].get("manifest_sha256")
                if (
                    (task.get("integration_mode") or "none") == "git"
                    and task.get("status") != "failed"
                    and not recovering
                    and not output_authorizations.get(task_id)
                ):
                    current_output = teardown_output_guard(task)
                    if current_output["reason"]:
                        record_output_retention(task, current_output)
                        raise ValueError(
                            "Git output changed before archive publication: "
                            + current_output["reason"]
                        )
            except (OSError, ValueError, tarfile.TarError) as exc:
                print(f"[CLOSE ABORT] sandbox archive failed: {task_id}: {exc}")
                sys.exit(2)
            archives[task_id] = archive
            kernel.update_task_metadata(
                task_id, {"clone_archive": archive}, store=get_store()
            )

    if dry_run:
        print(f"[DRY RUN] close-workflow {workflow_id}")

    report = {
        "workflow_id": workflow_id,
        "dry_run": dry_run,
        "outcome": close_outcome,
        "tasks": [],
        "tabs_closed": [],
        "tabs_would_close": [],
        "tabs_skipped": {},
        "coordinator_closed": False,
        "include_coordinator": include_coordinator,
        "escalated_accepted": [
            {"task_id": tid, "via": escalated_via}
            for tid in escalated
            if escalated_via
        ],
    }

    for task in tasks:
        if task.get("status") == "failed" and task["task_id"] not in reclaim_plans:
            report["tasks"].append(
                {
                    "task_id": task.get("task_id"),
                    "status": "failed",
                    "action": "retained-failed",
                    "clone_retained_reason": "failed task kept for debugging",
                }
            )
            continue
        finalize_options = dict(
            purge_clones=purge_clones, dry_run=dry_run, close_claim=close_claim
        )
        if dry_run and task["task_id"] in reclaim_plans:
            finalize_options["reclaim_plan"] = reclaim_plans[task["task_id"]]
        if task["task_id"] in archives:
            finalize_options["reclaim_archive"] = archives[task["task_id"]]
        authorization = output_authorizations.get(task["task_id"])
        if authorization:
            finalize_options["output_override"] = authorization
        report["tasks"].append(finalize_one(task, **finalize_options))

    if (
        any(
            row.get("action") == "retained"
            and (row.get("output_guard") or {}).get("reason")
            for row in report["tasks"]
        )
        and not dry_run
    ):
        print(
            "[CLOSE ABORT] output changed during teardown: "
            + json.dumps(report, ensure_ascii=False)
        )
        sys.exit(2)

    if not dry_run:
        from herdr.clone_archive import (
            _declared_deliverable_rel_paths,
            _root as _clone_root_of,
            is_residual_clone_shell,
            delete_residual_clone_shell,
        )

        remaining = []
        for task in tasks:
            clone = task.get("clone_path")
            if clone and os.path.lexists(os.path.expanduser(clone)):
                path = Path(clone).expanduser()
                try:
                    declared = frozenset(_declared_deliverable_rel_paths(task, _clone_root_of(task, clone_root)))
                except (OSError, ValueError, KeyError):
                    # 声明集合不可计算时禁止回收（fail-closed）：回收可能
                    # 销毁唯一副本 (PR #362 评审遗留)。
                    remaining.append(task["task_id"])
                    continue
                if task.get("clone_archive") and is_residual_clone_shell(path, declared=declared):
                    if delete_residual_clone_shell(path, clone_root=clone_root, declared=declared):
                        if not os.path.lexists(path):
                            continue
                remaining.append(task["task_id"])
        if remaining:
            print(
                "[CLOSE ABORT] sandbox reclamation incomplete: " + ", ".join(remaining)
            )
            sys.exit(2)

    topo = workflow_stage_tabs(workflow_id, tasks)
    live_panes = (
        live_workspace_panes(topo["workspace_id"])
        if topo["workspace_id"] and not dry_run
        else None
    )
    for tab_id in topo["tab_ids"]:
        if not topo["workspace_id"]:
            report["tabs_skipped"][
                tab_id
            ] = "workspace unknown; cannot verify ownership"
            continue
        if live_panes is None:
            report["tabs_skipped"][tab_id] = "pane list unavailable"
            continue
        foreign = foreign_panes_in_tab(live_panes, tab_id, topo["owned_pane_ids"])
        if foreign:
            report["tabs_skipped"][tab_id] = f"shared with foreign panes: {foreign}"
            continue
        if dry_run:
            report["tabs_skipped"][tab_id] = "tab instance ownership unknown; retained"
        else:
            # Tab/anchor IDs lack an immutable instance identity. Closing a
            # reused or empty tab cannot be authorized by persisted IDs alone.
            report["tabs_skipped"][tab_id] = "tab instance ownership unknown; retained"
    if not dry_run:
        latest = get_store().get_workflow(workflow_id)
        if latest and latest.get("status") != "completed":
            mark_workflow_completed(
                workflow_id,
                outcome=close_outcome,
                force=force,
            )
        stage_reset(workflow_id)

    coordinator_pane = topo.get("coordinator_pane") or (entry or {}).get(
        "coordinator_pane_id"
    )
    if include_coordinator and not dry_run:
        coordinator = dict((entry or {}).get("coordinator_runtime") or {})
        identity = {
            "pane_id": coordinator_pane,
            "runtime": coordinator,
            "agent": coordinator.get("agent"),
        }
        action = close_claim.action(
            "coordinator:" + str(coordinator_pane),
            identity,
            close_owned_pane,
            lambda: close_pane(coordinator_pane),
        )
        report["coordinator_closed"] = bool(action.get("result"))

    # ---- 收尾报告 ----
    print(f"[CLOSE REPORT] workflow={workflow_id} dry_run={dry_run}")
    for row in report["tasks"]:
        line = (
            f"  {row.get('task_id')} status={row.get('status')} "
            f"action={row.get('action')} pane_closed={row.get('pane_closed')}"
        )
        if row.get("evidence"):
            line += f" evidence={row['evidence']}"
        if row.get("clone_retained_reason"):
            line += f" clone_retained({row['clone_retained_reason']})"
        if row.get("pane_retained_reason"):
            line += f" pane_retained({row['pane_retained_reason']})"
        print(line)
    if report.get("escalated_accepted"):
        print(
            "  [ESCALATED ACCEPTED] human-claimed escalated tasks (not ordinary cleaned):"
        )
        for item in report["escalated_accepted"]:
            print(f"    {item.get('task_id')} via={item.get('via')}")
        print(
            "  [DELIVERABLE STATUS] workflow close was accepted with escalation; "
            "task commit/integration status and retained Clone are reported per task."
        )
    print(f"  tabs_closed={report['tabs_closed'] or '[]'}")
    if report.get("tabs_skipped"):
        for tab_id, reason in report["tabs_skipped"].items():
            print(f"  tab_skipped={tab_id} ({reason})")
    if include_coordinator:
        print(f"  coordinator_closed={report['coordinator_closed']}")
    else:
        print(
            "  [COORDINATOR KEPT] "
            f"pane={coordinator_pane or 'unknown'} — "
            "完成知识沉淀并合并 PR 后，请按以下步骤收尾：\n"
            "    1. 运行 six-step-finish 技能执行破坏性收尾步骤（步骤 3 最终确认 + 步骤 4-6）：\n"
            f"       bash .agents/skills/six-step-finish/scripts/finish-task.sh "
            f"<任务分支> --base {base_branch} --forge none\n"
            "       （<任务分支> 替换为已合入的任务分支名；存在多个已合入分支时分别对每个分支执行）\n"
            "       （绝对兜底：~/.agents/skills/six-step-finish/scripts/finish-task.sh）\n"
            "    2. 确认收尾完成后，运行：\n"
            f"       herdr-task close-workflow {workflow_id} --include-coordinator"
        )

    if dry_run:
        print("[DRY RUN] no changes were made")
    else:
        # Trigger corporate lessons and task experience sync (100% fail-open)
        if not abandon:
            _post_close_experience_sync(workflow_id, get_store().db_path)
        print(f"[WORKFLOW CLOSED] {workflow_id}")
    return report


def mark_workflow_completed(workflow_id, outcome="delivered", force=False, *, deps):
    get_store = deps["get_store"]
    load_workflow_entry = deps["load_workflow_entry"]
    now = deps["now"]
    sync_workflows_projection = deps["sync_workflows_projection"]
    workflows_file = deps["workflows_file"]
    store = get_store()
    entry, _ = load_workflow_entry(workflow_id)
    if entry is None:
        print(f"[WORKFLOW RECORD] no entry for {workflow_id}; skip marking")
        return

    try:
        from herdr import kernel

        kernel.transition_workflow(
            workflow_id=workflow_id,
            to_status="completed",
            reason=f"workflow_completed: {outcome}",
            source="herdr-task",
            metadata={"outcome": outcome, "completed_at": now()},
            force=force,
            store=store,
        )
        try:
            from herdr import node_dispatch_store

            node_dispatch_store.reconcile_workflow(
                store.db_path, workflow_id, discover=False
            )
        except Exception:
            pass
    except Exception as exc:
        print(f"Error marking workflow {workflow_id} completed: {exc}", file=sys.stderr)
        raise

    wf_file = workflows_file
    sync_workflows_projection(store=store, wf_file=Path(wf_file) if wf_file else None)
