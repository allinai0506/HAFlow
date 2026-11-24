"""派发前基线新鲜度 fail-fast（方案 A：把失败从 t4 移到 t0）。

背景见 `docs/handoffs/herdr-dev-baseline-drift-handoff.md`：
workflow 启动快照在 t0 正确、dev 在执行期间前进后失效，而校验发生在 t4
（wrapup 提交），此时双门禁成本已付出。本模块在 `herdr-task launch`
创建任何 clone / Pane 之前做一次廉价预检：候选基线落后 dev 即拒。

分层（纯核心与装配解耦）：
- `decide_baseline_failfast`：纯决策，无 I/O，stdlib only，可单测。
- `probe_claim_freshness`：薄 git 探测，只读 + 一次 `fetch origin <base>`
  （仅更新远端跟踪分支，不碰工作区/index）。
  fetch 失败则用缓存 ref 继续（只可能漏报、不会误杀，方向安全）；
  ref 不可解析 / git 执行失败一律 `unknown`（fail-open 照常派发）。

未知策略：ref 不可解析 / git 失败一律 `unknown`（fail-open 照常派发）。
派发前门禁只是 fail-fast 优化，正确性后栏仍由 FR-6.2 系列校验
fail-closed 兜底——与 `_dispatch_candidate_ready` 的未知策略一致。
"""

import subprocess

FETCH_TIMEOUT = 30
GIT_TIMEOUT = 10


def decide_baseline_failfast(*, dev_tip, claim_sha, behind_by,
                             claim_label="candidate"):
    """纯判定：候选落后 dev（behind_by > 0）即拒。

    `behind_by=None` 表示 git 事实未知 → fail-open 放行（code=baseline_unknown），
    下游 FR-6.2 保持 fail-closed。
    """
    dev_tip = str(dev_tip or "").strip().lower()
    claim_sha = str(claim_sha or "").strip().lower()
    label = str(claim_label or "candidate").strip() or "candidate"
    if behind_by is None or not dev_tip or not claim_sha:
        return {
            "refused": False,
            "code": "baseline_unknown",
            "behind_by": None,
            "dev_tip": dev_tip,
            "claim_sha": claim_sha,
            "message": (
                f"{label} 基线新鲜度未知（dev/候选不可解析），"
                "fail-open 照常派发，下游 FR-6.2 保持 fail-closed。"
            ),
            "remediation": "",
        }
    try:
        behind = int(behind_by)
    except (TypeError, ValueError):
        behind = None
    if behind is None or behind < 0:
        return {
            "refused": False,
            "code": "baseline_unknown",
            "behind_by": None,
            "dev_tip": dev_tip,
            "claim_sha": claim_sha,
            "message": (
                f"{label} 落后提交数不可判定，fail-open 照常派发。"
            ),
            "remediation": "",
        }
    if behind == 0:
        return {
            "refused": False,
            "code": "baseline_fresh",
            "behind_by": 0,
            "dev_tip": dev_tip,
            "claim_sha": claim_sha,
            "message": f"{label} 基线新鲜（含当前 dev），允许派发。",
            "remediation": "",
        }
    short_claim = claim_sha[:8]
    short_dev = dev_tip[:8]
    return {
        "refused": True,
        "code": "baseline_stale",
        "behind_by": behind,
        "dev_tip": dev_tip,
        "claim_sha": claim_sha,
        "message": (
            f"[BASELINE STALE] {label} {short_claim} 落后 dev {behind} 个提交 "
            f"（dev={short_dev}）；派发前拒绝，避免全部门禁变绿后在 t4 失败。"
        ),
        "remediation": (
            "将候选 rebase 到最新 dev 后重派 "
            "（git fetch origin dev && git rebase origin/dev）；"
            "或用新基线重新 launch（--candidate-sha <新SHA> / --onto <新分支>）。"
        ),
    }


def _git(source_repo, *args, timeout=GIT_TIMEOUT):
    try:
        probe = subprocess.run(
            ["git", "-C", str(source_repo), *args],
            text=True, capture_output=True, check=False, timeout=timeout,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return None
    if probe.returncode != 0:
        return None
    return (probe.stdout or "").strip()


def _rev_parse_commit(source_repo, revision):
    out = _git(source_repo, "rev-parse", "--verify", "--quiet",
               f"{revision}^{{commit}}")
    if not out:
        return ""
    return out.strip().lower()


def probe_claim_freshness(source_repo, *, base_branch="dev", claim_sha="",
                          onto_branch=None, timeout=GIT_TIMEOUT):
    """探测候选相对当前 dev 是否新鲜。

    Returns dict(status, dev_tip, claim_sha, behind_by, dev_ref, fetched, detail)，
    status ∈ {fresh, stale, skipped, unknown}。永不抛 git 异常：未知即 unknown。
    """
    base = str(base_branch or "dev").strip() or "dev"
    claim = str(claim_sha or "").strip()
    onto = str(onto_branch or "").strip()

    def unknown(detail, dev_tip="", resolved="", fetched=False):
        return {
            "status": "unknown",
            "dev_tip": dev_tip,
            "claim_sha": resolved,
            "behind_by": None,
            "dev_ref": "",
            "fetched": fetched,
            "detail": detail,
        }

    if not claim and not onto:
        return {
            "status": "skipped",
            "dev_tip": "",
            "claim_sha": "",
            "behind_by": None,
            "dev_ref": "",
            "fetched": False,
            "detail": "无候选声明（新鲜分支跟踪实时 base 分支），无需预检。",
        }

    fetched = _git(source_repo, "fetch", "origin", base,
                   timeout=max(timeout, FETCH_TIMEOUT)) is not None
    dev_tip = _rev_parse_commit(source_repo, f"origin/{base}")
    dev_ref = f"origin/{base}" if dev_tip else ""
    if not dev_tip:
        dev_tip = _rev_parse_commit(source_repo, base)
        dev_ref = base if dev_tip else ""
    if not dev_tip:
        return unknown(f"dev 基线不可解析（origin/{base} 与本地 {base} 均无）",
                        fetched=fetched)

    claims = []
    if claim:
        resolved = _rev_parse_commit(source_repo, claim)
        if resolved:
            claims.append(resolved)
    if onto:
        onto_head = _rev_parse_commit(source_repo, onto)
        if onto_head:
            claims.append(onto_head)
    if not claims:
        return unknown("候选不可解析（candidate-sha/onto 均无有效提交）",
                       dev_tip=dev_tip, fetched=fetched)

    worst_behind = 0
    stale_claims = []
    for resolved in claims:
        is_ancestor = _git_is_ancestor(source_repo, dev_tip, resolved)
        if is_ancestor is None:
            return unknown("merge-base 探测失败", dev_tip=dev_tip,
                           resolved=resolved, fetched=fetched)
        if is_ancestor:
            continue
        count_out = _git(source_repo, "rev-list", "--count",
                         f"{resolved}..{dev_tip}", "--")
        try:
            behind = int((count_out or "").strip())
        except (TypeError, ValueError):
            return unknown("落后提交数不可判定", dev_tip=dev_tip,
                           resolved=resolved, fetched=fetched)
        worst_behind = max(worst_behind, behind)
        stale_claims.append(resolved)

    if stale_claims:
        return {
            "status": "stale",
            "dev_tip": dev_tip,
            "claim_sha": stale_claims[0],
            "behind_by": worst_behind,
            "dev_ref": dev_ref,
            "fetched": fetched,
            "detail": f"{len(stale_claims)} 个候选基线落后 dev",
        }
    return {
        "status": "fresh",
        "dev_tip": dev_tip,
        "claim_sha": claims[0],
        "behind_by": 0,
        "dev_ref": dev_ref,
        "fetched": fetched,
        "detail": "候选含当前 dev，无落后提交。",
    }


def _git_is_ancestor(source_repo, ancestor, descendant):
    """`git merge-base --is-ancestor` 的布尔封装（rc==0 即真）。

    `_git` 把 rc!=0 与执行失败都折成 None，此处需要区分，
    故单独执行一次保留 returncode 语义。
    """
    try:
        probe = subprocess.run(
            ["git", "-C", str(source_repo), "merge-base", "--is-ancestor",
             ancestor, descendant],
            text=True, capture_output=True, check=False, timeout=GIT_TIMEOUT,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return None
    return probe.returncode == 0


def refresh_remote_base(source_repo, base, timeout=10):
    """Best-effort 刷新 origin/<base> 远程跟踪引用，供冻结前的新鲜度判定使用。

    与上方 freshness probe 的 fetch 同源：离线、无 origin 或 fetch 失败
    返回 False，调用方按既有 `origin/<base>` -> `<base>` 解析回退，
    不视为致命错误。
    """
    if not base:
        return False
    return _git(source_repo, "fetch", "origin", base, timeout=timeout) is not None


def try_auto_rebase_candidate(source_repo, candidate_sha, *, base_branch="dev", timeout=60):
    """Attempt a clean, conflict-free rebase of candidate_sha onto origin/<base_branch> inside a temporary worktree."""
    import tempfile
    from pathlib import Path

    if not source_repo or not candidate_sha:
        return {"ok": False, "reason": "invalid_arguments", "detail": "missing source_repo or candidate_sha"}
    refresh_remote_base(source_repo, base_branch, timeout=min(timeout, 30))
    base_ref = f"origin/{base_branch}"
    base_tip = _rev_parse_commit(source_repo, base_ref) or _rev_parse_commit(source_repo, base_branch)
    if not base_tip:
        return {"ok": False, "reason": "base_tip_unknown", "detail": f"cannot resolve {base_ref}"}
    target_ref = base_ref if _rev_parse_commit(source_repo, base_ref) else base_branch
    with tempfile.TemporaryDirectory(prefix="herdr-auto-rebase-") as tmpdir:
        wt_path = str(Path(tmpdir) / "wt")
        add_res = subprocess.run(
            ["git", "-C", str(source_repo), "worktree", "add", "--detach", wt_path, candidate_sha],
            capture_output=True, text=True, timeout=timeout,
        )
        if add_res.returncode != 0:
            return {"ok": False, "reason": "worktree_add_failed", "detail": (add_res.stderr or add_res.stdout or "")[:300]}
        try:
            rb_res = subprocess.run(
                ["git", "-C", wt_path, "rebase", target_ref],
                capture_output=True, text=True, timeout=timeout,
            )
            if rb_res.returncode != 0:
                subprocess.run(["git", "-C", wt_path, "rebase", "--abort"], capture_output=True, text=True, timeout=15)
                return {
                    "ok": False,
                    "reason": "rebase_conflict",
                    "base_tip": base_tip,
                    "detail": (rb_res.stderr or rb_res.stdout or "rebase conflict")[:300],
                }
            new_sha = _rev_parse_commit(wt_path, "HEAD")
            if not new_sha:
                return {"ok": False, "reason": "head_unresolvable", "base_tip": base_tip, "detail": "HEAD unreadable after rebase"}
            return {"ok": True, "old_sha": candidate_sha, "new_sha": new_sha, "base_tip": base_tip}
        finally:
            subprocess.run(
                ["git", "-C", str(source_repo), "worktree", "remove", "--force", wt_path],
                capture_output=True, text=True, timeout=30,
            )


def auto_rebase_stale_workflow(store, workflow_id, project_root, candidate_sha, base_branch, node_id):
    """Attempt conflict-free rebase for a stale workflow candidate and atomically re-freeze only if candidate generation/watermark did not advance."""
    from herdr import recovery_store, state_db

    try:
        wf_before = store.get_workflow(workflow_id) or {}
        if wf_before.get("status") != "running":
            return None
        stale_sha = candidate_sha or wf_before.get("candidate_sha") or ""
        pre_exec_id = wf_before.get("execution_id")
        pre_episode_id = wf_before.get("candidate_episode_id")

        ro = state_db.get_readonly_db_connection(store.db_path)
        try:
            row = ro.execute(
                "SELECT COALESCE(MAX(id), 0) AS max_id FROM events "
                "WHERE workflow_id=? AND event_type IN ('candidate_frozen', 'candidate_published', 'candidate_integrated')",
                (workflow_id,),
            ).fetchone()
            pre_watermark = int(row["max_id"]) if row else 0
        finally:
            ro.close()

        rb = try_auto_rebase_candidate(project_root, stale_sha, base_branch=base_branch or "dev")
        if not (rb.get("ok") and rb.get("new_sha")):
            return None

        with recovery_store._transaction(store.db_path) as conn:
            fresh_wf, _, _ = recovery_store._snapshot(conn, workflow_id)
            if (
                fresh_wf.get("status") != "running"
                or fresh_wf.get("execution_id") != pre_exec_id
                or fresh_wf.get("candidate_episode_id") != pre_episode_id
                or (fresh_wf.get("candidate_sha") or "") != stale_sha
            ):
                print(f"[BASELINE AUTO-REBASE STALE] workflow={workflow_id} candidate changed concurrently; aborting freeze")
                return None
            newer = conn.execute(
                "SELECT id FROM events WHERE workflow_id=? AND id > ? "
                "AND event_type IN ('candidate_frozen', 'candidate_published', 'candidate_integrated') LIMIT 1",
                (workflow_id, pre_watermark),
            ).fetchone()
            if newer is not None:
                print(f"[BASELINE AUTO-REBASE STALE] workflow={workflow_id} newer publication event={newer['id']}; aborting freeze")
                return None
            state_db.record_event(
                {
                    "event_type": "candidate_frozen",
                    "workflow_id": workflow_id,
                    "source": "critical-path-scheduler",
                    "payload": {"candidate_sha": rb["new_sha"], "source_node": "auto_rebase", "previous_candidate_sha": stale_sha},
                },
                conn=conn,
            )
            state_db.record_event(
                {
                    "event_type": "candidate_auto_rebased",
                    "workflow_id": workflow_id,
                    "node_id": node_id,
                    "source": "herdr-controller",
                    "payload": {"old_sha": stale_sha, "new_sha": rb["new_sha"], "base_tip": rb.get("base_tip")},
                },
                conn=conn,
            )
            state_db.update_workflow_metadata(workflow_id, {"candidate_sha": rb["new_sha"]}, conn=conn)
        print(f"[BASELINE AUTO-REBASED] workflow={workflow_id} {stale_sha[:8]} -> {rb['new_sha'][:8]}")
        return rb["new_sha"]
    except Exception as exc:
        print(f"[BASELINE AUTO-REBASE WARN] workflow={workflow_id}: {exc}")
    return None
