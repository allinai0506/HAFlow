"""Evidence and delivery record commands for herdr-task."""

import json, os, subprocess, sys
from pathlib import Path


def note_add(args, *, deps):
    find_task = deps["find_task"]
    load_tasks = deps["load_tasks"]
    _task_head_sha = deps["task_head_sha"]
    from herdr import workflow_docs as wd

    if str(args.kind or "").strip() == "delivery":
        print(
            "Error: delivery 备注必须用 'herdr-task record-delivery' 写入 (需要 delivery_branch/candidate_sha/review_task/test_gate)；--kind delivery 已禁用，人工留痕请用 --kind note。"
        )
        sys.exit(2)
    task = None
    if args.task:
        task = find_task(load_tasks(), args.task)
        if not task:
            print(f"Task not found: {args.task}")
            sys.exit(1)
    if args.file:
        note_file = Path(args.file).expanduser()
        if not note_file.exists():
            print(f"Note file not found: {note_file}")
            sys.exit(2)
        try:
            with note_file.open(encoding="utf-8") as handle:
                body = handle.read(wd.MAX_BODY + 1)
        except OSError as exc:
            print(f"Error reading note file: {exc}")
            sys.exit(2)
    else:
        body = args.text or ""
    node = args.node or (task.get("node") or task.get("stage") if task else None)
    agent = args.agent or (task.get("agent") if task else None)
    base_sha = args.base_sha or _task_head_sha(task) or None
    extra_fields = {}
    for raw in getattr(args, "field", None) or []:
        key, sep, value = str(raw).partition("=")
        if not sep or not key.strip():
            print(f"Error: --field expects KEY=VALUE, got: {raw}")
            sys.exit(2)
        extra_fields[key.strip()] = value
    try:
        record = wd.append_note(
            args.workflow_id,
            kind=args.kind,
            title=args.title,
            body=body,
            node=node,
            task_id=args.task or "",
            agent=agent,
            source=args.source,
            base_sha=base_sha,
            round=args.round,
            invalidates=args.invalidates,
            fields=extra_fields or None,
        )
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(2)
    print(f"[NOTE_ADDED] {record['note_id']}")
    print(f"[NOTE_DIR] {wd.workflow_docs_dir(args.workflow_id)}")


def note_list(args):
    from herdr import workflow_docs as wd

    try:
        annotated = wd.annotate_notes(
            wd.load_notes(args.workflow_id), current_base_sha=args.current_base_sha
        )
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(2)
    if args.node:
        annotated = [n for n in annotated if (n.get("node") or "") == args.node]
    if args.limit and args.limit > 0:
        annotated = annotated[-args.limit :]
    if args.json:
        print(
            json.dumps(
                {
                    "workflow_id": args.workflow_id,
                    "docs_dir": str(wd.workflow_docs_dir(args.workflow_id)),
                    "notes": annotated,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    print(f"=== 工作流共享文档 [{args.workflow_id}] ===")
    print(f"目录: {wd.workflow_docs_dir(args.workflow_id)}")
    if not annotated:
        print("暂无条目")
        return
    for note in annotated:
        stale = (
            f"[STALE: {note.get('stale_reason') or 'stale'}] "
            if note.get("stale")
            else ""
        )
        print(
            f"{stale}[{note.get('kind')}][{note.get('node') or '-'}] {note.get('title')} ({note.get('note_id')})"
        )


def _capture_verified_candidate(task, *, deps):
    """Persist the clone revision that was live at verdict-write time.

    This is completion evidence, distinct from ``baseline_commit`` (launch
    evidence). Reading HEAD here catches an agent that pulled, checked out, or
    rebased while running: the recorded value then names what the verifier
    actually saw, and the join gate can refuse a verdict that never proved the
    frozen candidate.

    Never raises: the caller (set_status) inspects the returned value and
    refuses the verdict, so an unreadable clone fails closed at the decision
    boundary rather than being swallowed here.
    """
    _get_store = deps["get_store"]
    clone = str((task or {}).get("clone_path") or "").strip()
    if not clone or not os.path.isdir(clone):
        return ""
    try:
        probe = subprocess.run(
            ["git", "-C", clone, "rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return ""
    head = (probe.stdout or "").strip()
    if probe.returncode != 0 or not head:
        return ""
    task_id = str((task or {}).get("task_id") or "")
    if not task_id:
        return head
    try:
        store = _get_store()
        from herdr import kernel

        kernel.update_task_metadata(
            task_id=task_id, updates={"verified_candidate_sha": head}, store=store
        )
        task["verified_candidate_sha"] = head
        try:
            fresh = store.get_task(task_id)
            if fresh and isinstance(fresh, dict) and ("version" in fresh):
                task["version"] = fresh["version"]
            elif task.get("version") is not None:
                task["version"] = int(task.get("version") or 0) + 1
        except Exception:
            if task.get("version") is not None:
                task["version"] = int(task.get("version") or 0) + 1
        print(f"[VERIFIED CANDIDATE] {task_id} head={head}")
    except Exception as exc:
        print(f"[VERIFIED CANDIDATE WARN] {task_id}: {exc}", file=sys.stderr)
    return head


def _record_gate_note(task, verdict, note, *, deps):
    """Best-effort machine evidence: gate verdict -> workflow shared docs."""
    _task_head_sha = deps["task_head_sha"]
    try:
        from herdr import workflow_docs as wd

        workflow_id = task.get("workflow_id")
        if not workflow_id:
            return
        provenance = wd.provenance_from_task(task)
        base_sha = provenance["base_sha"] or _task_head_sha(task)
        title = f"{provenance['node'] or '未知节点'} 门禁结论 {verdict}"
        for existing in wd.load_notes(workflow_id):
            if (
                existing.get("kind") == "gate"
                and existing.get("task_id") == provenance["task_id"]
                and (existing.get("title") == title)
                and ((existing.get("base_sha") or "") == (base_sha or ""))
            ):
                return
        wd.append_note(
            workflow_id,
            kind="gate",
            title=title,
            body=note or "",
            node=provenance["node"],
            task_id=provenance["task_id"],
            agent=provenance["agent"],
            source=wd.SOURCE_CONTROLLER,
            base_sha=base_sha,
        )
    except Exception as exc:
        print(f"[NOTE EVIDENCE WARN] {exc}")


def record_delivery_note(
    workflow_id,
    delivery_branch,
    candidate_sha,
    review_task,
    test_gate,
    base="",
    node="",
    agent="",
    supersedes=None,
):
    """Record one explicit candidate identity, rejecting implicit ambiguity."""
    from herdr import delivery_record as _dr
    from herdr import workflow_docs as wd

    candidate_sha = str(candidate_sha)
    payload = {
        "delivery_id": f"candidate-{candidate_sha}",
        "delivery_branch": delivery_branch,
        "candidate_sha": candidate_sha,
        "review_task": review_task,
        "test_gate": test_gate,
        "base": base,
        "supersedes": supersedes or "",
    }
    ok, missing = _dr.validate_delivery_payload(payload)
    if not ok:
        print(f"delivery payload missing fields: {missing}")
        sys.exit(2)
    identity = _dr.candidate_identity(payload)

    def _find_existing(existing_notes):
        annotated = wd.annotate_notes(existing_notes, current_base_sha=base or None)
        for existing in annotated:
            if existing.get("kind") != "delivery":
                continue
            if _dr.candidate_identity(existing) != identity:
                continue
            if existing.get("stale"):
                print(
                    f"[DELIVERY INVALIDATED] {identity} cannot be replayed; record a new candidate identity"
                )
                sys.exit(2)
            if _dr._delivery_fingerprint(existing) != _dr._delivery_fingerprint(
                payload
            ):
                print(f"[DELIVERY CONFLICT] identity {identity} has different payload")
                sys.exit(2)
            return existing
        return None

    def _can_append(existing_notes):
        if _find_existing(existing_notes) is not None:
            return False
        annotated = wd.annotate_notes(existing_notes, current_base_sha=base or None)
        if supersedes:
            known_aliases = {
                alias
                for existing in annotated
                if existing.get("kind") == "delivery"
                for alias in _dr._candidate_aliases(existing)
            }
            if str(supersedes) not in known_aliases:
                print(
                    f"[DELIVERY SUPERSEDES UNKNOWN] {supersedes}; refusing an unverifiable replacement edge"
                )
                sys.exit(2)
        try:
            active = _dr.select_effective_delivery(annotated)
        except _dr.DeliveryAmbiguityError as exc:
            print(f"[DELIVERY AMBIGUOUS] {exc}")
            sys.exit(2)
        if active is not None and (not supersedes):
            print(
                "[DELIVERY AMBIGUOUS] an active delivery candidate already exists; provide an explicit supersedes relation before replacing it"
            )
            sys.exit(2)
        prior_candidates = [
            item
            for item in annotated
            if item.get("kind") == "delivery" and _dr._is_delivery_claim(item)
        ]
        if prior_candidates and (not supersedes):
            print(
                "[DELIVERY REPLACEMENT REQUIRED] a prior delivery candidate exists; provide --supersedes even when it was invalidated"
            )
            sys.exit(2)
        return True

    initial_notes = wd.load_notes(workflow_id)
    legacy = _dr.unidentified_delivery_notes(initial_notes)
    if legacy:
        shown = ", ".join((str(note.get("note_id") or "?") for note in legacy[:5]))
        print(
            f"[BASELINE DEGRADED] {len(legacy)} identity-less delivery note(s) ignored (human evidence, not delivery claims): {shown}"
        )
    existing = _find_existing(initial_notes)
    if existing is not None:
        print(f"[DELIVERY EXISTS] {existing.get('note_id')}")
        return existing
    _can_append(initial_notes)
    title = _dr.delivery_note_title(delivery_branch, candidate_sha)
    body_text = _dr.delivery_note_body(payload)
    fields = {
        "delivery_id": payload["delivery_id"],
        "delivery_branch": delivery_branch,
        "candidate_sha": candidate_sha,
        "review_task": review_task,
        "test_gate": test_gate,
    }
    if supersedes:
        fields["supersedes"] = supersedes
    try:
        record = wd.append_note(
            workflow_id,
            kind="delivery",
            title=title,
            body=body_text,
            node=node or "wrapup",
            task_id=review_task,
            agent=agent,
            source=wd.SOURCE_CONTROLLER,
            base_sha=base or None,
            fields=fields,
            precondition=lambda existing_notes, _record: _can_append(existing_notes),
        )
    except ValueError:
        existing = _find_existing(wd.load_notes(workflow_id))
        if existing is None:
            raise
        print(f"[DELIVERY EXISTS] {existing.get('note_id')}")
        return existing
    print(f"[DELIVERY RECORDED] {record['note_id']} {delivery_branch}@{candidate_sha}")
    return record


def check_delivery_for_pr(
    workflow_id,
    head_sha,
    head_ref_name="",
    merged_prs=None,
    cherry_lines=None,
    head_tree=None,
    candidate_tree=None,
    repo_path=None,
    *,
    deps,
):
    subprocess = deps["subprocess"]
    "Thin PR gate (FR-4.2/4.3/4.4): equivalence + template + same-branch warn."
    from herdr import delivery_record as _dr
    from herdr import workflow_docs as wd

    notes = wd.load_notes(workflow_id)
    try:
        raw_effective = _dr.select_effective_delivery(notes)
    except _dr.DeliveryAmbiguityError as exc:
        print(f"[DELIVERY AMBIGUOUS] {exc}; refusing PR")
        sys.exit(2)
    if raw_effective is None:
        print("[DELIVERY CHECK] no effective delivery note; refusing ambiguous PR")
        sys.exit(2)
    current_base = str(
        raw_effective.get("base")
        or _dr._body_value(raw_effective, "base")
        or raw_effective.get("base_sha")
        or ""
    )
    annotated = wd.annotate_notes(notes, current_base_sha=current_base or None)
    try:
        effective = _dr.select_effective_delivery(annotated)
    except _dr.DeliveryAmbiguityError as exc:
        print(f"[DELIVERY AMBIGUOUS] {exc}; refusing PR")
        sys.exit(2)
    if effective is None:
        print("[DELIVERY CHECK] delivery candidate was invalidated; refusing PR")
        sys.exit(2)
    body = effective.get("body") or ""
    candidate_sha = str(effective.get("candidate_sha") or "")
    if not candidate_sha:
        for line in body.splitlines():
            if line.startswith("candidate_sha:"):
                candidate_sha = line.split(":", 1)[1].strip()
                break
    if not candidate_sha:
        candidate_sha = str(effective.get("base_sha") or "")
    repo_path = os.path.realpath(repo_path or os.getcwd())
    if merged_prs is None and head_ref_name:
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "merged",
                "--head",
                head_ref_name,
                "--json",
                "number,headRefName,headRefOid,state",
            ],
            cwd=repo_path,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            try:
                merged_prs = json.loads(proc.stdout or "[]")
            except (TypeError, ValueError):
                merged_prs = []
                print("[DELIVERY INPUT WARN] gh returned invalid merged-PR JSON")
        else:
            merged_prs = []
            print(
                f"[DELIVERY INPUT WARN] merged PR history unavailable; gh exit={proc.returncode}: {(proc.stderr or '').strip()[:300]}"
            )
    if head_tree is None or candidate_tree is None:

        def tree_for(sha):
            if not sha:
                return None
            proc = subprocess.run(
                ["git", "-C", repo_path, "rev-parse", "--verify", f"{sha}^{{tree}}"],
                text=True,
                capture_output=True,
                check=False,
            )
            return proc.stdout.strip() if proc.returncode == 0 else None

        head_tree = head_tree if head_tree is not None else tree_for(head_sha)
        candidate_tree = (
            candidate_tree if candidate_tree is not None else tree_for(candidate_sha)
        )
    equivalent, eq_reason = _dr.candidates_equivalent(
        candidate_sha=candidate_sha,
        head_sha=head_sha,
        cherry_lines=cherry_lines,
        head_tree=head_tree,
        candidate_tree=candidate_tree,
    )
    warn = _dr.same_branch_warning(
        head_ref_name=head_ref_name, merged_prs=merged_prs, candidate_sha=candidate_sha
    )
    fields = _dr.pr_template_fields(
        branch=head_ref_name,
        head_sha=head_sha,
        base=current_base or "main",
        review_ref=effective.get("task_id") or "",
        delivery_id=_dr.candidate_identity(effective),
    )
    print(_dr.format_pr_body(fields))
    print(f"equivalent={equivalent} reason={eq_reason}")
    print(f"same_branch_warn={warn['warn']} detail={warn['detail']}")
    if not equivalent:
        print("[DELIVERY CHECK] head not equivalent to candidate; warn (not block)")
    if warn["warn"]:
        print(f"[DELIVERY WARN] {warn['detail']}")
    return {"equivalent": equivalent, "warn": warn, "fields": fields}


def task_head_sha(task):
    """Best-effort: task branch head sha from its CoW clone (gate evidence)."""
    task = task or {}
    task_id = task.get("task_id") or ""
    branch = task.get("branch") or ""
    if not task_id or not branch:
        return ""
    clone_root = Path(
        os.environ.get("HERDR_CLONES_DIR")
        or (Path.home() / ".herdr-controller" / "clones")
    )
    clone = clone_root / task_id
    if not clone.exists():
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "--short", branch],
            text=True,
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""
