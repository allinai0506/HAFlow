"""Resolve and freeze the scheduler revision passed to every dispatch path.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def _scheduler_current_frozen_candidate_sha(ports, workflow_id):
    """最新一次冻结候选 SHA(从 freeze 台账读,不重算);无 → ""。"""
    if ports.scheduler_facts_store is None:
        return ""
    try:
        return ports.scheduler_facts_store.latest_frozen_candidate_sha(workflow_id)
    except (ValueError, OSError):
        return ''

def _scheduler_current_frozen_branch(ports, workflow_id):
    """最新一次冻结候选的可证明分支(从 freeze 台账读);无 → ""。"""
    if ports.scheduler_facts_store is None:
        return ""
    try:
        events = ports.scheduler_facts_store.list_candidate_frozen_events(workflow_id)
    except Exception:
        return ""
    if not events:
        return ""
    return str((events[-1].get("payload") or {}).get("delivery_branch") or "")

def _dispatch_candidate_ready(ports, project_root, base_branch, specs, workflow_id=None):
    """候选非空预检:onto 分支相对基线无提交时拒绝派发(转总指挥)。

    r6 曾直派测试 main 空候选并恒 blocked，白烧内环。已冻结完整SHA
    同时匹配spec pin与实际onto时，等于基线不代表空候选。未知情况
    （缺 refs、git 失败）一律 fail-open 照常派发。
    """
    ontos = sorted(
        {s.get("onto_branch") or s.get("candidate_branch") for s in (specs or [])
         if s.get("onto_branch") or s.get("candidate_branch")}
    )
    if not ontos:
        return True
    base = (base_branch or "dev").strip() or "dev"
    checked = 0
    empty = 0
    for onto in ontos:
        try:
            result = ports.subprocess.run(
                ["git", "-C", project_root, "rev-list", "--count",
                 f"{base}..{onto}", "--"],
                text=True,
                capture_output=True,
                timeout=10,
            )
        except Exception:
            return True
        if result.returncode != 0:
            return True
        try:
            is_empty = int(result.stdout.strip()) == 0
        except (TypeError, ValueError):
            return True
        checked += 1
        if is_empty:
            empty += 1
    if checked and empty == checked:
        frozen = ports._scheduler_current_frozen_candidate_sha(workflow_id) if workflow_id else ""
        if ports.re.fullmatch(r"[0-9a-f]{40}", frozen) and all(
            (spec.get("onto_branch") or spec.get("candidate_branch")) and spec.get("candidate_sha") == frozen
            for spec in specs
        ):
            proven = True
            for onto in ontos:
                try:
                    resolved = ports.subprocess.run(
                        ["git", "-C", project_root, "rev-parse", "--verify",
                         f"{onto}^{{commit}}"],
                        text=True, capture_output=True, timeout=10,
                    )
                except Exception:
                    return True  # Preserve the existing unknown-Git policy.
                if resolved.returncode != 0:
                    return True
                if resolved.stdout.strip() != frozen:
                    proven = False
                    break
            if proven:
                return True
        print(
            f"[DIRECT DISPATCH CANDIDATE EMPTY] "
            f"onto={','.join(ontos)} has no commits beyond {base} "
            "-> fallback to coordinator for adjudication"
        )
        return False
    return True


# Critical-Path Scheduler v1:候选冻结 + 汇聚门禁 (HAFlow PR #107)
#
# - _scheduler_expected_candidate_sha:已发布episode优先，未发布基线沿用旧选择；
#   失败一律返回 ""(fail-open 原语义,门禁侧 fail-closed)。
# - _scheduler_freeze_candidate:implementation 完成后冻结候选(幂等)。
# - _scheduler_join_gate_allows:join 语义节点的确定性放行判定。
def _scheduler_expected_candidate_sha(ports, workflow_id, project_root, dep_ids, candidate_branch):
    """Use the published candidate; legacy baseline selection never publishes it."""
    frozen = ports._scheduler_current_frozen_candidate_sha(workflow_id)
    if ports.re.fullmatch(r'[0-9a-f]{40}', frozen or ''):
        if ports.scheduler_core is None:
            return ''
        resolved = ports.scheduler_core.resolve_candidate_sha_for_branch(project_root, f'{frozen}^{{commit}}')
        return frozen if resolved == frozen else ''
    if (
        ports.scheduler_core is None
        or ports.delivery_record_mod is None
        or ports.workflow_docs_mod is None
    ):
        return ""
    try:
        notes = ports.workflow_docs_mod.load_notes(workflow_id)
        effective = ports.delivery_record_mod.select_effective_delivery(
            notes, workflow_id=workflow_id
        )
    except Exception:
        effective = None
    if effective is not None:
        try:
            sha = ports.delivery_record_mod._body_value(effective, "candidate_sha")
        except Exception:
            sha = ""
        sha = str(sha or effective.get("candidate_sha") or "").strip()
        if sha:
            return sha
    if candidate_branch is None:
        # Integration may publish a task ref without changing source HEAD.
        # A verifier can create its own branch at the proven frozen commit;
        # it must not borrow the implementation task's owned branch.
        frozen = ports._scheduler_current_frozen_candidate_sha(workflow_id)
        if ports.re.fullmatch(r"[0-9a-f]{40}", frozen):
            commit = ports.scheduler_core.resolve_candidate_sha_for_branch(
                project_root, f"{frozen}^{{commit}}"
            )
            if commit == frozen:
                return frozen
        return ""
    try:
        return ports.scheduler_core.resolve_candidate_sha_for_branch(
            project_root, candidate_branch
        )
    except Exception:
        return ""

def _scheduler_freeze_candidate(ports, workflow_id, project_root, source_node, dep_ids=None):
    """Freeze the candidate SHA and ensure a delivery record exists (Plan A).

    Two distinct facts, two distinct lifecycles (do not merge them):
    - candidate_frozen (this function): "which revision this verification
      round targets". Owned by the scheduler; carries no verifier identity.
    - delivery record (written later, by review-pass/wrapup with the REAL
      verifier task ids): "which real tasks verified and formed the".

    Freezing therefore records only the frozen-candidate fact. It never
    invents review_task/test_gate ids: delivery_record treats those as part
    of an immutable fingerprint, so a placeholder would both conflict with
    the real ids later and misrepresent delivery auditability.

    Returns the frozen SHA, or "" when unprovable (join side fail-closed).
    """
    if ports.scheduler_core is None or ports.scheduler_facts_store is None:
        return ""
    # Activity and delivery-note edits are not candidate publication events.
    from herdr import recovery_store, state_db
    store = ports._get_store()
    workflow, _, delivery_tasks = recovery_store.read_snapshot(store.db_path, workflow_id)
    epoch = max(float(workflow.get('created_at') or 0), float(workflow.get('reopened_at') or 0))
    conn = state_db.get_readonly_db_connection(store.db_path)
    try:
        publication = conn.execute("SELECT * FROM events WHERE workflow_id=? AND event_type='candidate_published' AND source='herdr-task' AND json_extract(payload_json,'$.execution_id')=? AND timestamp>=? ORDER BY id DESC LIMIT 1",
                                   (workflow_id, workflow.get('execution_id'), epoch)).fetchone()
        episode = conn.execute("SELECT id FROM events WHERE workflow_id=? AND event_type='candidate_frozen' AND source='critical-path-scheduler' AND timestamp>=? ORDER BY id DESC LIMIT 1", (workflow_id, epoch)).fetchone()
    finally:
        conn.close()
    if publication:
        published_task = next((t for t in delivery_tasks if t['task_id'] == publication['task_id']), {})
        from herdr.wrapup_delivery import integrated
        if integrated(published_task, workflow):
            source_node = 'wrapup'
    git_deliveries = [t for t in delivery_tasks
        if (t.get('node') or t.get('stage')) == source_node
        and (not t.get('execution_id') or t['execution_id'] == workflow.get('execution_id'))
        and t.get('integration_mode') == 'git' and t.get('status') != 'superseded'
        and not t.get('superseded_by')]
    if len(git_deliveries) > 1:
        from herdr.candidate_assembly import delivery_inventory, assemble_candidate
        try:
            inventory = delivery_inventory(workflow, delivery_tasks, source_node)
            publication_current = bool(publication and (not episode or publication['id'] > episode['id'])
                and ports.json.loads(publication['payload_json']).get('expected_episode_id') == (workflow.get('candidate_episode_id') or 0))
            sha = assemble_candidate(project_root, inventory, preferred=workflow.get('candidate_sha') or '',
                                     allow_merge=publication_current)
            if sha == workflow.get('candidate_sha'):
                return sha
            res = ports.scheduler_facts_store.record_candidate_frozen(workflow_id, sha, source_node=source_node,
                db_path=store.db_path, expected_episode_id=workflow.get('candidate_episode_id') or 0,
                delivery_inventory=inventory, publication_event_id=publication['id'],
                repo_path=project_root, base_branch=workflow.get('base_branch'))
            if isinstance(res, dict) and res.get('status') == 'invalidated':
                return ''
            return sha
        except (ValueError, OSError, ports.subprocess.TimeoutExpired) as exc:
            print(f"[SCHEDULER ASSEMBLY REFUSED] workflow={workflow_id}: {type(exc).__name__}")
            return ''
    if publication is None or (episode and publication['id'] <= episode['id']):
        frozen = workflow.get('candidate_sha') or ''
        if not frozen:
            return ''
        resolved = ports.scheduler_core.resolve_candidate_sha_for_branch(project_root, f'{frozen}^{{commit}}')
        return frozen if resolved == frozen else ''
    sha = ports.json.loads(publication['payload_json']).get('candidate_sha')
    if ports.scheduler_core.resolve_candidate_sha_for_branch(project_root, f'{sha}^{{commit}}') != sha:
        return ''
    try:
        res = ports.scheduler_facts_store.record_candidate_frozen(workflow_id, sha, source_node=source_node,
            db_path=store.db_path, expected_episode_id=ports.json.loads(publication['payload_json']).get('expected_episode_id'),
            publication_event_id=publication['id'],
            repo_path=project_root, base_branch=workflow.get('base_branch'))
        if isinstance(res, dict) and res.get('status') == 'invalidated':
            return ''
    except ValueError as exc:
        print(f"[SCHEDULER FREEZE REJECTED] workflow={workflow_id}: {exc}")
        return ''
    return recovery_store.read_snapshot(store.db_path, workflow_id)[0].get('candidate_sha') or ''

def _scheduler_previous_candidate_sha(ports, workflow_id):
    """The candidate frozen immediately before the newest one ("" when none).

    Read from the freeze ledger rather than recomputed: the ledger is the
    authoritative record of which revisions were verified in which order, so it
    is what makes A -> B -> A three distinct episodes instead of two.
    """
    if ports.scheduler_facts_store is None:
        return ""
    try:
        events = ports.scheduler_facts_store.list_candidate_frozen_events(workflow_id)
    except Exception as exc:
        print(f"[SCHEDULER FROZEN HISTORY WARN] workflow={workflow_id}: {exc}")
        return ""
    if len(events) < 2:
        return ""
    return str((events[-2].get("payload") or {}).get("candidate_sha") or "")

def _scheduler_frozen_candidate_identity(ports, workflow_id, project_ctx, dep_ids=None):
    """The frozen candidate identity a fallback dispatch must carry verbatim.

    Direct dispatch binds the candidate through ``--candidate-sha`` and
    ``--onto``. When it falls back to the coordinator, that binding would
    otherwise be lost and the coordinator would re-derive a revision on its
    own, giving one scheduler decision two execution semantics.

    This returns the frozen fact only — never a freshly resolved SHA — so the
    fallback path cannot silently substitute a different candidate. Returns
    ("", "") when the scheduler never froze one (legacy workflows, and any
    resolution error), which leaves the legacy coordinator prompt unchanged.
    """
    if ports.scheduler_facts_store is None:
        return "", ""
    try:
        events = ports.scheduler_facts_store.list_candidate_frozen_events(workflow_id)
    except Exception as exc:
        print(f"[SCHEDULER FROZEN IDENTITY WARN] workflow={workflow_id}: {exc}")
        return "", ""
    if not events:
        return "", ""
    payload = (events[-1].get("payload") or {})
    return (
        str(payload.get("candidate_sha") or "").strip(),
        str(payload.get("delivery_branch") or "").strip(),
    )


DEPENDENCIES = {'_scheduler_current_frozen_candidate_sha': ('scheduler_facts_store',),
 '_scheduler_current_frozen_branch': ('scheduler_facts_store',),
 '_dispatch_candidate_ready': ('_scheduler_current_frozen_candidate_sha', 're', 'subprocess'),
 '_scheduler_expected_candidate_sha': ('_scheduler_current_frozen_candidate_sha',
                                       'delivery_record_mod', 're', 'scheduler_core',
                                       'workflow_docs_mod'),
 '_scheduler_freeze_candidate': ('_get_store', 'json', 'scheduler_core', 'scheduler_facts_store',
                                 'subprocess'),
 '_scheduler_previous_candidate_sha': ('scheduler_facts_store',),
 '_scheduler_frozen_candidate_identity': ('scheduler_facts_store',)}

__all__ = tuple(DEPENDENCIES)
