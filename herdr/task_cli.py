import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from herdr.task_cli_parser import build_parser


def main(argv=None, *, handlers):
    _get_store = handlers["_get_store"]
    add_task = handlers["add_task"]
    adopt_verdict_cli = handlers["adopt_verdict_cli"]
    bypass_gate_cli = handlers["bypass_gate_cli"]
    check_delivery_for_pr = handlers["check_delivery_for_pr"]
    check_zombie_obligations = handlers["check_zombie_obligations"]
    cleanup_task = handlers["cleanup_task"]
    clear_finalize_escalation = handlers["clear_finalize_escalation"]
    close_workflow = handlers["close_workflow"]
    cmd_canary_eval = handlers["cmd_canary_eval"]
    cmd_compact = handlers["cmd_compact"]
    cmd_eval = handlers["cmd_eval"]
    cmd_eval_compare = handlers["cmd_eval_compare"]
    cmd_launch_reconcile = handlers["cmd_launch_reconcile"]
    cmd_metrics = handlers["cmd_metrics"]
    cmd_observe = handlers["cmd_observe"]
    cmd_panes = handlers["cmd_panes"]
    cmd_reap = handlers["cmd_reap"]
    cmd_renew_completion = handlers["cmd_renew_completion"]
    cmd_replay = handlers["cmd_replay"]
    cmd_reverification = handlers["cmd_reverification"]
    cmd_rework = handlers["cmd_rework"]
    cmd_rollout = handlers["cmd_rollout"]
    cmd_route_shadow = handlers["cmd_route_shadow"]
    cmd_shadow_eval = handlers["cmd_shadow_eval"]
    cmd_task_checkpoint = handlers["cmd_task_checkpoint"]
    cmd_tool_run = handlers["cmd_tool_run"]
    cmd_workflow_migration = handlers["cmd_workflow_migration"]
    cmd_workflow_recovery = handlers["cmd_workflow_recovery"]
    cmd_working_context = handlers["cmd_working_context"]
    commit_task = handlers["commit_task"]
    create_pull_request = handlers["create_pull_request"]
    differential_test = handlers["differential_test"]
    dispatch_task = handlers["dispatch_task"]
    ensure_node_runtime = handlers["ensure_node_runtime"]
    finalize_task = handlers["finalize_task"]
    force_advance = handlers["force_advance"]
    get_task = handlers["get_task"]
    integrate_task = handlers["integrate_task"]
    launch_task = handlers["launch_task"]
    list_tasks = handlers["list_tasks"]
    merge_pull_request = handlers["merge_pull_request"]
    node_status = handlers["node_status"]
    note_add = handlers["note_add"]
    note_list = handlers["note_list"]
    ops_center = handlers["ops_center"]
    pause_task = handlers["pause_task"]
    purge_task = handlers["purge_task"]
    record_delivery_note = handlers["record_delivery_note"]
    record_review_closure = handlers["record_review_closure"]
    reopen_workflow = handlers["reopen_workflow"]
    resolve_workflow_id_for_close = handlers["resolve_workflow_id_for_close"]
    resume_task = handlers["resume_task"]
    set_status = handlers["set_status"]
    stage_reset = handlers["stage_reset"]
    stage_status = handlers.get("stage_status", node_status)
    supersede_task = handlers["supersede_task"]
    verify_baseline = handlers["verify_baseline"]
    verify_metrics = handlers["verify_metrics"]
    from herdr.task_cli_parser import build_parser

    args = build_parser().parse_args(argv)
    if args.command == "sync-experience":
        from herdr.experience.collector import ExperienceCollector

        explicit_token = getattr(args, "token", None)
        hub_client = None
        if explicit_token:
            from herdr.experience.client import ExperienceClient

            hub_client = ExperienceClient(token=explicit_token)
        collector = ExperienceCollector(hub_client=hub_client)
        if getattr(args, "workflow_id", None):
            collector.collect_workflow_tasks(args.workflow_id)
        if getattr(args, "include_lessons", False):
            les_path = getattr(args, "lessons_path", None)
            les_res = collector.collect_and_sync_lessons(lessons_path=les_path)
            if not les_res.get("ok"):
                print(
                    f"[EXPERIENCE SYNC WARNING] lessons sync notice: {les_res.get('reason')}",
                    file=sys.stderr,
                )
        res = collector.recover_and_sync(
            force=getattr(args, "force", False)
            or bool(
                getattr(args, "workflow_id", None)
                or getattr(args, "include_lessons", False)
            )
        )
        if getattr(args, "json", False):
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(
                f"[EXPERIENCE SYNC] collected={res.get('pending_collected', 0)} retried={res.get('retried', 0)} succeeded={res.get('succeeded', 0)} failed={res.get('failed', 0)}"
            )
        if res.get("pending_upload_remaining", 0) > 0 and (
            res.get("reason") == "no_hub_client"
            or res.get("failed", 0) > 0
            or getattr(args, "force", False)
        ):
            print(
                "[EXPERIENCE SYNC FAILED] pending uploads remain; recovery did not complete",
                file=sys.stderr,
            )
            sys.exit(2)
        if res.get("pending_collection_failed", 0) > 0:
            print(
                "[EXPERIENCE SYNC FAILED] pending collections failed; recovery did not complete",
                file=sys.stderr,
            )
            sys.exit(2)
        if res.get("pending_collection_error", 0) > 0:
            print(
                "[EXPERIENCE SYNC FAILED] quarantined collections remain; a settlement was dropped",
                file=sys.stderr,
            )
            sys.exit(2)
        sys.exit(0)
    elif args.command == "acceptance-record":
        from herdr.task_checkpoint import record_business_acceptance

        try:
            references = []
            for item in args.artifact:
                obs, digest = item.split(":", 1)
                references.append({"observation_id": obs, "sha256": digest})
            print(
                json.dumps(
                    record_business_acceptance(
                        _get_store(),
                        args.task_id,
                        args.run_id,
                        args.epoch,
                        args.candidate_sha,
                        args.verdict,
                        references,
                        args.criterion,
                    ),
                    ensure_ascii=False,
                )
            )
        except (ValueError, OSError) as exc:
            print(f"[ACCEPTANCE REJECTED] {exc}", file=sys.stderr)
            sys.exit(2)
    elif args.command == "node-budget-extend":
        from herdr.node_config import extend_budget

        try:
            print(
                json.dumps(
                    extend_budget(
                        _get_store(),
                        args.workflow_id,
                        args.node,
                        expected_sha=args.expected_sha,
                        additional=args.additional,
                        operator=args.operator,
                        reason=args.reason,
                    ),
                    ensure_ascii=False,
                )
            )
        except ValueError as exc:
            print(f"[BUDGET REJECTED] {exc}", file=sys.stderr)
            sys.exit(2)
    elif args.command == "workflow-migrate":
        cmd_workflow_migration(args)
    elif args.command == "partial-launch-preview":
        import hashlib

        try:
            probe = subprocess.run(
                ["herdr", "pane", "read", args.pane_id],
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            if probe.returncode != 0 or len(probe.stdout.encode()) > 65536:
                raise ValueError("Pane transcript 不可读或超过 64 KiB")
            print(
                json.dumps(
                    {
                        "pane_id": args.pane_id,
                        "transcript": probe.stdout,
                        "sha256": hashlib.sha256(probe.stdout.encode()).hexdigest(),
                    },
                    ensure_ascii=False,
                )
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print(f"[PARTIAL LAUNCH PREVIEW REJECTED] {exc}", file=sys.stderr)
            sys.exit(2)
    elif args.command == "partial-launch-retire":
        from herdr import dispatch_recovery, state_db

        try:
            report = dispatch_recovery.abandon_partial_launch(
                state_db.resolve_state_db_path(),
                args.workflow_id,
                args.operation_id,
                args.expected_version,
                args.prior_operation_id,
                args.expected_prior_version,
                operator=args.operator,
                reason=args.reason,
                candidate_sha=args.candidate_sha,
                intent_id=args.intent_id,
                task_id=args.task_id,
                pane_id=args.pane_id,
                terminal_id=args.terminal_id,
                transcript_sha256=args.transcript_sha256,
                confirmed_startup_only=args.confirm_startup_only,
            )
            print(json.dumps(report, ensure_ascii=False))
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(f"[PARTIAL LAUNCH RETIREMENT REJECTED] {exc}", file=sys.stderr)
            sys.exit(2)
    elif args.command in ("recovery-status", "recovery-decide"):
        cmd_workflow_recovery(args)
    elif args.command == "add":
        add_task(args)
    elif args.command == "launch":
        launch_task(args)
    elif args.command in (
        "checkpoint-publish",
        "checkpoint-read",
        "checkpoint-aggregate",
    ):
        cmd_task_checkpoint(args)
    elif args.command == "tool-run":
        cmd_tool_run(args)
    elif args.command == "launch-reconcile":
        cmd_launch_reconcile(args)
    elif args.command == "rework":
        cmd_rework(args)
    elif args.command == "reap":
        cmd_reap(args)
    elif args.command == "panes":
        cmd_panes(args)
    elif args.command == "dispatch":
        dispatch_task(args.task_id, args.prompt)
    elif args.command == "verify-baseline":
        verify_baseline(args.task_id)
    elif args.command == "verify-metrics":
        verify_metrics(
            args.task_id, min_score=args.min_score, if_present=args.if_present
        )
    elif args.command == "differential-test":
        differential_test(args)
    elif args.command == "commit":
        commit_task(
            args.task_id,
            args.message,
            allow_rebase=getattr(args, "allow_rebase", False),
        )
    elif args.command == "integrate":
        integrate_task(args.task_id)
    elif args.command == "cleanup":
        cleanup_task(args.task_id)
    elif args.command == "purge":
        purge_task(args.task_id)
    elif args.command == "finalize":
        finalize_task(args.task_id, force=args.force, purge_clones=args.purge_clones)
    elif args.command == "close-workflow":
        target_wid = resolve_workflow_id_for_close(args.workflow_id)
        try:
            close_workflow(
                target_wid,
                include_coordinator=args.include_coordinator,
                purge_clones=args.purge_clones,
                dry_run=args.dry_run,
                abandon=args.abandon,
                force=args.force,
                accept_escalated=getattr(args, "accept_escalated", False),
            )
        except Exception as exc:
            print(f"Error closing workflow {target_wid}: {exc}", file=sys.stderr)
            sys.exit(2)
    elif args.command == "reopen-workflow":
        reopen_workflow(args.workflow_id)
    elif args.command == "get":
        get_task(args.task_id)
    elif args.command == "set":
        set_status(
            args.task_id,
            args.status,
            args.verdict,
            args.note,
            affected_task_ids=args.affected_task_ids,
            skip_review_closure=args.skip_review_closure,
            skip_operator=args.operator,
            operator_token=getattr(args, "operator_token", None),
            operator_credentials=getattr(args, "operator_credentials", None),
        )
    elif args.command == "adopt-verdict":
        adopt_verdict_cli(
            args.task_id,
            verdict=args.verdict,
            note=args.note,
            operator=args.operator,
            as_json=getattr(args, "json", False),
        )
    elif args.command == "record-review-closure":
        record_review_closure(
            args.task_id,
            pr_url=args.pr_url,
            ci_status=args.ci_status,
            findings=args.findings,
            deferred=args.deferred,
            reason=args.reason,
            head_sha=args.head_sha,
            unresolved_p1=args.unresolved_p1,
            unresolved_p2=args.unresolved_p2,
        )
    elif args.command == "bypass-gate":
        bypass_gate_cli(
            args.task_id,
            reason=args.reason,
            operator=args.operator,
            note=getattr(args, "note", None),
            as_json=getattr(args, "json", False),
        )
    elif args.command == "stage-status":
        stage_status(args.workflow_id, args.stage)
    elif args.command == "node-status":
        node_status(args.workflow_id, args.node)
    elif args.command == "ops-center":
        ops_center(args)
    elif args.command == "ensure-runtime":
        res = ensure_node_runtime(args.workflow_id, args.node)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif args.command == "list":
        list_tasks(args.workflow_id)
    elif args.command == "supersede":
        supersede_task(
            args.task_id,
            new_task_id=args.new_task_id,
            reason=args.reason,
            abandon=args.abandon,
            allow_pending=getattr(args, "allow_pending", False),
        )
    elif args.command == "clear-escalation":
        clear_finalize_escalation(args.task_id)
    elif args.command == "stage-reset":
        stage_reset(args.workflow_id, args.stage)
    elif args.command == "advance":
        force_advance(args.workflow_id, args.stage)
    elif args.command == "pause":
        pause_task(args.task_id, reason=args.reason)
    elif args.command == "resume":
        resume_task(args.task_id)
    elif args.command == "halt":
        import herdr.steering as herdr_steering

        try:
            res = herdr_steering.halt_task(
                args.task_id, reason=args.reason, operator=args.operator
            )
            if not res.get("ok"):
                print(
                    f"[HALT FAILED] {res.get('task_id', args.task_id)}: {res.get('error', 'unknown_error')} (status unchanged: {res.get('status', 'unknown')})"
                )
                sys.exit(1)
            print(f"[HALTED] {res['task_id']}: {res['reason']}")
        except Exception as e:
            print(f"Error halting task: {e}")
            sys.exit(1)
    elif args.command == "steer":
        import herdr.steering as herdr_steering

        try:
            res = herdr_steering.queue_steer(
                args.task_id,
                instruction=args.instruction,
                operator=args.operator,
                urgent=args.urgent,
            )
            mode = (
                ("IMMEDIATE" if args.urgent else "QUEUED")
                if res.get("ok")
                else "FAILED"
            )
            print(f"[STEER_{mode}] {res['steer_id']} for {res['task_id']}")
            if not res.get("ok"):
                print(
                    f"Steering not delivered: {res.get('reason') or 'delivery_failed'}"
                )
                sys.exit(1)
        except Exception as e:
            print(f"Error steering task: {e}")
            sys.exit(1)
    elif args.command == "steer-queue":
        import herdr.steering as herdr_steering

        steers = herdr_steering.list_task_steers(args.task_id)
        if not steers:
            print(f"No steers for task {args.task_id}")
        else:
            for s in steers:
                print(
                    f"- [{s['status']}] {s['steer_id']} (urgent={s['urgent']}): {s['instruction']}"
                )
    elif args.command == "adapters":
        from herdr.agent_adapter import list_agent_adapters

        adapters = list_agent_adapters()
        if getattr(args, "json", False):
            print(json.dumps(adapters, ensure_ascii=False, indent=2))
        else:
            print("=== Herdr Agent Adapter Capability Matrix (TTY Prototype) ===")
            for name, item in adapters.items():
                caps = item.get("capabilities", {})
                print(f"- [{name}] (protocol: {item.get('protocol_level')}):")
                print(
                    f"    supports_interrupt:        {caps.get('supports_interrupt')}"
                )
                print(
                    f"    supports_soft_steer:       {caps.get('supports_soft_steer')}"
                )
                print(f"    supports_resume:           {caps.get('supports_resume')}")
                print(
                    f"    supports_prompt_injection: {caps.get('supports_prompt_injection')}"
                )
    elif args.command == "project":
        import herdr.projection as herdr_projection

        try:
            res = herdr_projection.project_task(args.task_id)
            if getattr(args, "json", False):
                print(json.dumps(res, ensure_ascii=False, indent=2))
            else:
                print(f"=== 任务白盒简报 [{res['task_id']}] ===")
                print(f"状态: {res['status']} | 执行者: {res.get('agent', '-')}")
                print(f"当前意图: {res.get('intent', '-')}")
                if res.get("blockers"):
                    print(f"⚠️ 卡点告警: {', '.join(res['blockers'])}")
                print("\n[动态路标]")
                for m in res.get("milestones", []):
                    st = (
                        "✓"
                        if m["status"] == "completed"
                        else ">" if m["status"] == "in_progress" else " "
                    )
                    print(f"  [{st}] {m['label']}")
                print("\n[产物列表]")
                for a in res.get("artifacts", []):
                    print(f"  - [{a.get('kind')}] {a.get('name')}: {a.get('summary')}")
                print("\n[近期动态]")
                for act in res.get("recent_activity", []):
                    print(f"  • {act}")
        except Exception as e:
            print(f"Error projecting task: {e}")
            sys.exit(1)
    elif args.command == "artifacts":
        import herdr.projection as herdr_projection

        try:
            res = herdr_projection.project_task(args.task_id)
            artifacts = res.get("artifacts", [])
            if getattr(args, "json", False):
                print(json.dumps(artifacts, ensure_ascii=False, indent=2))
            else:
                print(f"=== 任务产物清单 [{res['task_id']}] ===")
                if not artifacts:
                    print("暂无产物生成")
                for a in artifacts:
                    print(f"[{a.get('kind')}] {a.get('name')}")
                    print(f"  摘要: {a.get('summary')}")
                    if a.get("path"):
                        print(f"  路径: {a.get('path')}")
        except Exception as e:
            print(f"Error collecting artifacts: {e}")
            sys.exit(1)
    elif args.command == "delivery-report":
        from herdr.delivery_report import build_delivery_report

        print(
            json.dumps(
                build_delivery_report(args.workflow_id, _get_store()),
                ensure_ascii=False,
            )
        )
    elif args.command == "renew-completion":
        cmd_renew_completion(args)
    elif args.command == "delivery-check":
        from herdr.task_delivery import check_delivery

        try:
            result = check_delivery(args.task_id, store=_get_store())
            print(json.dumps(result, ensure_ascii=False))
            sys.exit(0 if result["status"] == "ready" else 3)
        except (OSError, ValueError, KeyError) as exc:
            from herdr.supervisor.state import redact_text

            print(
                json.dumps(
                    {"status": "unknown", "error": redact_text(str(exc))},
                    ensure_ascii=False,
                )
            )
            sys.exit(2)
    elif args.command == "report-completion":
        from herdr.completion_receipt import report_completion

        identity = json.loads(Path(args.identity_file).read_text())
        references = []
        for reference in args.artifact:
            observation_id, digest = reference.split(":", 1)
            references.append({"observation_id": observation_id, "sha256": digest})
        print(
            json.dumps(
                report_completion(args.task_id, identity, references, _get_store()),
                ensure_ascii=False,
            )
        )
    elif args.command == "checkpoint-create":
        import herdr.kernel as herdr_kernel

        try:
            res = herdr_kernel.create_checkpoint(
                args.workflow_id, tag=args.tag, parent_checkpoint_id=args.parent
            )
            print(
                f"[CHECKPOINT_CREATED] {res['checkpoint_id']} (tag={res['tag']}, tasks={res['task_count']})"
            )
        except Exception as e:
            print(f"Error creating checkpoint: {e}")
            sys.exit(1)
    elif args.command == "checkpoint-list":
        import herdr.kernel as herdr_kernel

        try:
            cps = herdr_kernel.list_checkpoints(args.workflow_id)
            if getattr(args, "json", False):
                print(json.dumps(cps, ensure_ascii=False, indent=2))
            else:
                print(f"=== 工作流检查点列表 [{args.workflow_id}] ===")
                if not cps:
                    print("暂无快照记录")
                for cp in cps:
                    parent_str = (
                        f" (parent={cp.get('parent_checkpoint_id')})"
                        if cp.get("parent_checkpoint_id")
                        else ""
                    )
                    print(
                        f"- {cp['checkpoint_id']} [tag={cp.get('tag') or '-'}] tasks={cp['task_count']} status={cp.get('workflow_status')}{parent_str}"
                    )
        except Exception as e:
            print(f"Error listing checkpoints: {e}")
            sys.exit(1)
    elif args.command == "checkpoint-restore":
        import herdr.kernel as herdr_kernel

        try:
            res = herdr_kernel.restore_checkpoint(args.workflow_id, args.checkpoint_id)
            print(
                f"[CHECKPOINT_RESTORED] {res['checkpoint_id']} -> workflow={res['workflow_id']}, tasks={res['restored_tasks']}, status={res['status']}"
            )
        except Exception as e:
            print(f"Error restoring checkpoint: {e}")
            sys.exit(1)
    elif args.command == "checkpoint-fork":
        import herdr.kernel as herdr_kernel

        try:
            res = herdr_kernel.fork_workflow_from_checkpoint(
                args.checkpoint_id, args.new_workflow_id, new_title=args.title
            )
            print(
                f"[WORKFLOW_FORKED] {res['new_workflow_id']} from {res['source_checkpoint_id']} (tasks={res['cloned_tasks']})"
            )
        except Exception as e:
            print(f"Error forking workflow: {e}")
            sys.exit(1)
    elif args.command == "note-add":
        note_add(args)
    elif args.command == "note-list":
        note_list(args)
    elif args.command == "record-delivery":
        record_delivery_note(
            args.workflow_id,
            args.delivery_branch,
            args.candidate_sha,
            args.review_task,
            args.test_gate,
            base=getattr(args, "base", ""),
            node=getattr(args, "node", "wrapup"),
            supersedes=getattr(args, "supersedes", None),
        )
    elif args.command == "freeze-candidate":
        from herdr.scheduler_facts import record_candidate_frozen

        res = record_candidate_frozen(
            args.workflow_id,
            args.candidate_sha,
            source_node=getattr(args, "source_node", ""),
            delivery_branch=getattr(args, "delivery_branch", ""),
        )
        print(
            f"[CANDIDATE FROZEN] workflow={args.workflow_id} candidate_sha={args.candidate_sha} status={res.get('status', 'recorded')}"
        )
    elif args.command in {"node-config", "node-config-set", "authorize-completion"}:
        try:
            if args.command == "authorize-completion":
                from herdr.completion_receipt import authorize_legacy_completion

                result = authorize_legacy_completion(
                    args.task_id,
                    args.run_id,
                    args.expected_version,
                    args.reason,
                    _get_store(),
                )
                import shlex

                result["report_command"] = shlex.join(
                    [
                        "herdr-task",
                        "report-completion",
                        args.task_id,
                        "--identity-file",
                        result["path"],
                    ]
                )
            elif args.command == "node-config-set":
                from herdr.node_config import update_required_tasks

                result = update_required_tasks(
                    _get_store(),
                    args.workflow_id,
                    args.node,
                    args.required_task_id or [],
                    expected_sha=args.expected_sha,
                    reason=args.reason,
                )
            else:
                from herdr.node_config import read_configuration

                _, config, sha = read_configuration(_get_store(), args.workflow_id)
                matches = [
                    n
                    for n in config.get("nodes", [])
                    if (n.get("id") or n.get("key")) == args.node
                ]
                if len(matches) != 1:
                    raise ValueError("node missing or ambiguous")
                result = {
                    "workflow_id": args.workflow_id,
                    "node": matches[0],
                    "config_sha": sha,
                }
            print(json.dumps(result, ensure_ascii=False))
        except (ValueError, OSError, RuntimeError) as exc:
            print(f"[CONFIGURATION/RECOVERY REFUSED] {exc}", file=sys.stderr)
            sys.exit(2)
    elif args.command == "create-pr":
        try:
            create_pull_request(args)
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            print(f"[PR PUBLICATION REFUSED] {exc}", file=sys.stderr)
            sys.exit(2)
    elif args.command == "merge-pr":
        try:
            merge_pull_request(args)
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            print(f"[PR MERGE REFUSED] {exc}", file=sys.stderr)
            sys.exit(2)
    elif args.command == "check-obligations":
        zombies = check_zombie_obligations()
        if args.workflow_id:
            zombies = [
                z
                for z in zombies
                if str(z.get("workflow_id") or "") == str(args.workflow_id)
            ]
        if args.json:
            print(json.dumps(zombies, ensure_ascii=False))
        elif not zombies:
            print("[OBLIGATIONS CLEAN] No zombie obligations found")
        else:
            for z in zombies:
                print(
                    f"[ZOMBIE OBLIGATION] task_id={z.get('task_id')} workflow_id={z.get('workflow_id')} node={z.get('node') or z.get('stage')}"
                )
        sys.exit(1 if zombies else 0)
    elif args.command == "check-delivery":
        check_delivery_for_pr(
            args.workflow_id,
            args.head_sha,
            head_ref_name=getattr(args, "head_ref", ""),
            repo_path=getattr(args, "repo_path", None),
        )
    elif args.command == "observe":
        cmd_observe(args)
    elif args.command == "compact":
        cmd_compact(args)
    elif args.command == "metrics":
        cmd_metrics(args)
    elif args.command == "working-context":
        cmd_working_context(args)
    elif args.command == "eval":
        cmd_eval(args)
    elif args.command == "replay":
        cmd_replay(args)
    elif args.command == "eval-compare":
        cmd_eval_compare(args)
    elif args.command == "route-shadow":
        cmd_route_shadow(args)
    elif args.command == "shadow-eval":
        cmd_shadow_eval(args)
    elif args.command == "canary-eval":
        cmd_canary_eval(args)
    elif args.command == "rollout":
        cmd_rollout(args)
    elif args.command == "reverification":
        cmd_reverification(args)
