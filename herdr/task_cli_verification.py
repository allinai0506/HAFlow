"""Read-only task verification CLI reporting."""

import json
import os
import re
import time
import sys
import hashlib
from pathlib import Path


def verify_baseline(
    task_id,
    *,
    load_tasks,
    find_task,
    _git_head_sha,
    _git_interval_commits,
    context_workspace_fingerprint,
    current_workspace_fingerprint,
):
    data = load_tasks()
    task = find_task(data, task_id)

    if not task:
        print(f"Task not found: {task_id}")
        sys.exit(1)

    clone = task.get("clone_path")

    if not clone or not os.path.isdir(clone):
        print(f"Clone not found: {clone}")
        sys.exit(2)

    baseline = task.get("baseline_fingerprint")

    if baseline is None:
        print(f"Task has no baseline_fingerprint: " f"{task_id}")
        sys.exit(3)

    if task.get("execution_mode") != "context":
        # Path-level union (plan-arch §2 AR-2, §3.4): verdict is driven by the
        # union of worktree-vs-HEAD paths and anchor..HEAD interval paths, not
        # by commit existence. Pure --allow-empty intervals carry no paths and
        # stay BASELINE_MATCH. An already-registered HEAD (task commit, or
        # the post-rebase integrated_commit) is registered deliverable, not
        # unregistered change, and stays BASELINE_MATCH. Anchor-less tasks
        # keep commit分量 closed (commits_ahead=0, basis="absent", AC3-8).
        anchor = task.get("baseline_commit")
        anchor_head = _git_head_sha(clone) if anchor else None
        committed_changes = []
        commits_ahead = 0
        basis = "absent"
        head_for_payload = anchor_head
        if anchor:
            basis = "baseline_commit"
            if anchor_head and anchor_head != anchor:
                # P2: integrate may rebase the deliverable (commit=A,
                # HEAD=integrated_commit=B). Either registered identity
                # counts; anything else is unregistered change. Status
                # guards the integrated identity so a stray
                # integrated_commit value on a non-integrated task cannot
                # launder unrelated HEADs.
                commit_registered = task.get("commit") == anchor_head and task.get(
                    "commit_result"
                ) in ("adopted", "created")
                integrated_registered = task.get(
                    "integrated_commit"
                ) == anchor_head and task.get("status") in ("committed", "integrated")
                registered = commit_registered or integrated_registered
                interval = _git_interval_commits(clone, anchor, anchor_head)
                if interval is None:
                    # M-4: enumeration failure must fail closed, never
                    # BASELINE_MATCH. Record an unknown committed entry.
                    commits_ahead = 0
                    if not registered:
                        committed_changes.append(
                            {
                                "type": "committed",
                                "path": "__enumeration_failed__",
                                "commit": anchor_head,
                                "baseline": anchor,
                                "current": anchor_head,
                            }
                        )
                else:
                    commits_ahead = len(interval)
                    if not registered:
                        unknown_shas = [
                            item.get("sha")
                            for item in interval
                            if item.get("paths") is None
                        ]
                        if unknown_shas:
                            # F-3: uninspectable interval commits fail
                            # closed, never BASELINE_MATCH.
                            committed_changes.append(
                                {
                                    "type": "committed",
                                    "path": "__commit_paths_unknown__",
                                    "commit": anchor_head,
                                    "baseline": anchor,
                                    "current": anchor_head,
                                }
                            )
                        seen_paths = set()
                        for item in interval:
                            sha = item.get("sha")
                            for path in item.get("paths") or []:
                                if path not in seen_paths:
                                    seen_paths.add(path)
                                    committed_changes.append(
                                        {
                                            "type": "committed",
                                            "path": path,
                                            "commit": sha,
                                            "baseline": anchor,
                                            "current": anchor_head,
                                        }
                                    )
                        # Pure --allow-empty interval: no paths -> no committed
                        # entries; verdict falls through to worktree-only.
            else:
                commits_ahead = 0
        # Stash for the union below (worktree changes computed next).
        _vb_committed = committed_changes
        _vb_commits_ahead = commits_ahead
        _vb_basis = basis
        _vb_anchor = anchor
        _vb_head = head_for_payload
    else:
        _vb_committed = []
        _vb_commits_ahead = 0
        _vb_basis = "absent"
        _vb_anchor = None
        _vb_head = None

    current = (
        context_workspace_fingerprint(clone)
        if task.get("execution_mode") == "context"
        else current_workspace_fingerprint(clone)
    )

    changes = []

    for kind in ("tracked", "untracked"):
        before = baseline.get(kind, {})
        after = current.get(kind, {})

        all_paths = sorted(set(before.keys()) | set(after.keys()))

        for relpath in all_paths:
            old = before.get(relpath, "__NOT_PRESENT__")
            new = after.get(relpath, "__NOT_PRESENT__")

            if old != new:
                changes.append(
                    {"type": kind, "path": relpath, "baseline": old, "current": new}
                )

    worktree_changed = bool(changes)
    if task.get("execution_mode") != "context":
        changes = changes + list(_vb_committed)
    else:
        _vb_committed = []
        _vb_commits_ahead = 0
        _vb_basis = "absent"
        _vb_anchor = None
        _vb_head = None

    payload = {
        "task_id": task_id,
        "baseline_match": not bool(changes),
        "changes": changes,
        "worktree_changed": worktree_changed,
        "commits_ahead": int(_vb_commits_ahead),
        "baseline_commit": _vb_anchor,
        "head": _vb_head,
        "basis": _vb_basis,
    }

    if not changes:
        print("BASELINE_MATCH")
        print("HERDR_BASELINE_RESULT=" + json.dumps(payload, ensure_ascii=False))
        return

    print("TASK_CHANGED:")

    for item in changes:
        print(f"- [{item['type']}] " f"{item['path']}")

    print("HERDR_BASELINE_RESULT=" + json.dumps(payload, ensure_ascii=False))


def verify_metrics(task_id, min_score=99.9, if_present=False, *, load_tasks, find_task):
    """Verify that .herdr-loop/METRICS.json exists and meets required score threshold."""
    data = load_tasks()
    task = find_task(data, task_id)

    if not task:
        print(f"Task not found: {task_id}")
        sys.exit(1)

    clone = task.get("clone_path")
    if not clone or not os.path.isdir(clone):
        print(f"Clone not found: {clone}")
        sys.exit(2)

    loop_dir = Path(clone) / ".herdr-loop"
    metrics_file = loop_dir / "METRICS.json"

    if not metrics_file.exists():
        if if_present:
            print(
                f"TASK_METRICS: skipped (no .herdr-loop configured for task {task_id})"
            )
            sys.exit(0)
        print(f"No metrics found for task {task_id} (.herdr-loop/METRICS.json missing)")
        sys.exit(3)

    try:
        metrics_data = json.loads(metrics_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Invalid metrics file: {e}")
        sys.exit(4)

    score = float(metrics_data.get("composite_score", 0.0))
    passed = score >= min_score

    print(f"TASK_METRICS: score={score:.2f} (target>={min_score:.2f}) passed={passed}")
    print(
        "HERDR_METRICS_RESULT="
        + json.dumps(
            {
                "task_id": task_id,
                "score": score,
                "target": min_score,
                "passed": passed,
                "metrics": metrics_data,
            },
            ensure_ascii=False,
        )
    )

    if not passed:
        sys.exit(5)


def differential_test(args):
    """Evaluate candidate test failures against the selected baseline."""
    from herdr.domain import get_domain_adapter

    candidate_path = getattr(args, "candidate_output", None)
    if not candidate_path:
        print(
            "ERROR: --candidate-output is required for differential-test",
            file=sys.stderr,
        )
        sys.exit(2)
    if not os.path.isfile(candidate_path):
        print(
            "ERROR: candidate output file not found: " + str(candidate_path),
            file=sys.stderr,
        )
        sys.exit(1)

    base_output = None
    base_output_path = getattr(args, "base_output", None)
    if base_output_path:
        if not os.path.isfile(base_output_path):
            print(
                "ERROR: base output file not found: " + str(base_output_path),
                file=sys.stderr,
            )
            sys.exit(1)
        base_output = Path(base_output_path).read_text(
            encoding="utf-8", errors="replace"
        )

    base_snapshot_path = getattr(args, "base_snapshot", None)
    if base_snapshot_path and not os.path.isfile(base_snapshot_path):
        print(
            "ERROR: base snapshot file not found: " + str(base_snapshot_path),
            file=sys.stderr,
        )
        sys.exit(1)

    candidate_output = Path(candidate_path).read_text(
        encoding="utf-8", errors="replace"
    )
    adapter = get_domain_adapter(domain=getattr(args, "domain", "software"))
    result = adapter.evaluate_differential_tests(
        candidate_output=candidate_output,
        base_output=base_output,
        base_snapshot=base_snapshot_path,
        candidate_exit_code=getattr(args, "candidate_exit_code", 0),
        base_exit_code=getattr(args, "base_exit_code", 0),
    )

    verdict = result.get("verdict", "pass")
    delta_count = len(result.get("delta_failures", []))
    baseline_count = len(result.get("pre_existing_failures", []))
    print(
        f"DIFFERENTIAL_TEST: verdict={verdict} delta={delta_count} pre_existing={baseline_count}"
    )
    if result.get("warning"):
        print(f"WARNING: {result['warning']}")
    print("HERDR_DIFFERENTIAL_TEST_RESULT=" + json.dumps(result, ensure_ascii=False))
    if verdict != "pass":
        sys.exit(1)


def _is_gate_task(t):
    if not t:
        return False
    if t.get("gate") is False:
        return False

    workflow_id = t.get("workflow_id")
    node_id = t.get("node") or t.get("stage")

    # Authoritative workflow configuration check FIRST
    wf_cfg = None
    if workflow_id:
        try:
            from herdr.projects import workflow_config_for

            wf_cfg = workflow_config_for(workflow_id) or {}
        except Exception:
            pass
    if not wf_cfg and t.get("workflow_config") and node_id:
        try:
            import yaml
            from pathlib import Path

            p = Path(t["workflow_config"]).expanduser()
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    wf_cfg = yaml.safe_load(f) or {}
        except Exception:
            pass

    node = str(node_id or "").lower()
    if wf_cfg and isinstance(wf_cfg, dict):
        for n in wf_cfg.get("nodes", []):
            if n.get("id") == node_id:
                if n.get("gate") is False:
                    return False
                if n.get("gate") is not None or n.get("node_type") == "gate":
                    return True
                if node in (
                    "test",
                    "review",
                    "wrapup",
                    "gate",
                    "cleanroom",
                    "cleanroom_test",
                    "cleanroom_review",
                    "cleanroom_gate",
                ) or re.search(r"(^|[_-])(gate|cleanroom)($|[_-])", node):
                    return True
                if n.get("node_type") in (
                    "agent",
                    "implementation",
                    "worker",
                    "code",
                ):
                    return False
                break

    if t.get("gate") is True:
        return True

    # Legacy fallback: exact names or word-boundary tokens, NOT arbitrary substring
    if node in (
        "test",
        "review",
        "wrapup",
        "gate",
        "cleanroom",
        "cleanroom_test",
        "cleanroom_review",
        "cleanroom_gate",
    ):
        return True
    if re.search(r"(^|[_-])(gate|cleanroom)($|[_-])", node):
        return True
    return bool(t.get("gate"))


def _gate_verdict_file_candidates(task):
    """PR #250 & Codex review: Return ordered list of candidate gate-verdict file paths."""
    candidates = []
    task_id = task.get("task_id") or task.get("id")
    if task_id:
        try:
            from herdr.direct_dispatch import gate_verdict_path

            candidates.append(gate_verdict_path(task_id))
        except Exception:
            pass
        override = os.environ.get("HERDR_GATE_VERDICT_DIR")
        vdir = (
            Path(override).expanduser()
            if override
            else (Path.home() / ".herdr-controller" / "gate-verdicts")
        )
        candidates.append(str(vdir / f"{task_id}.json"))

    clone_path = task.get("clone_path")
    if clone_path:
        candidates.append(os.path.join(clone_path, ".herdr/gate-verdict.json"))
        candidates.append(os.path.join(clone_path, ".herdr", "gate-verdict.json"))

    seen = set()
    result = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            result.append(c)
    return result


class GateVerdictEvidence(tuple):
    """Authenticated gate verdict evidence container compatible with legacy 4-tuple unpacking."""
    snapshot_hash: str = ""

    def __new__(cls, verdict, note, affected_ids, mtime, snapshot_hash=""):
        obj = super().__new__(cls, (verdict, note, affected_ids, mtime))
        obj.snapshot_hash = snapshot_hash
        return obj


def _authenticate_gate_verdict_evidence(task, expected_verdict=None):
    """Authenticate durable gate verdict evidence from disk for task.

    Verifies that a valid gate verdict file exists for the task, belongs to
    the current execution epoch, has a fresh mtime (not older than rework_started_at,
    and not in the future beyond clock skew allowance), matches expected_verdict,
    and verifies SHA256 against tasks.payload_json.evidence_snapshot_hash if present.

    Returns (True, GateVerdictEvidence(verdict, note, affected_ids, st_mtime, snapshot_hash))
    if valid and matches expected_verdict.
    Returns (False, err_msg) if missing, stale, invalid, or mismatched.
    """
    candidates = _gate_verdict_file_candidates(task)
    rework_started = task.get("rework_started_at")
    expected_token = task.get("execution_token") or task.get("rework_request_id")
    expected_hash = task.get("evidence_snapshot_hash")
    skew_limit = 5.0
    try:
        raw_skew = os.environ.get("HERDR_MAX_CLOCK_SKEW_SECONDS")
        if raw_skew is not None and raw_skew.strip():
            skew_limit = float(raw_skew.strip())
    except Exception:
        skew_limit = 5.0

    now = time.time()
    found_any = False
    last_err = "no durable gate verdict file found"

    for path in candidates:
        try:
            if not os.path.exists(path):
                continue
            found_any = True
            st = os.stat(path)
            if rework_started and st.st_mtime < (rework_started - 1.0):
                last_err = f"gate verdict file {path} is stale (mtime {st.st_mtime} < rework_started {rework_started})"
                continue
            if (st.st_mtime - now) > skew_limit:
                last_err = f"gate verdict file {path} has future timestamp exceeding clock skew allowance ({st.st_mtime - now:.1f}s > {skew_limit}s)"
                continue
            with open(path, "rb") as f_raw:
                raw_bytes = f_raw.read()
            file_hash = hashlib.sha256(raw_bytes).hexdigest()
            if expected_hash and file_hash != expected_hash:
                return (
                    False,
                    f"gate verdict evidence {path} hash mismatch: expected {expected_hash}, got {file_hash} (tampered or corrupted)",
                )
            try:
                payload = json.loads(raw_bytes.decode("utf-8"))
            except Exception:
                last_err = f"gate verdict file {path} is not valid UTF-8 JSON"
                continue
            if not isinstance(payload, dict):
                last_err = f"gate verdict file {path} is not a valid JSON dictionary"
                continue
            if expected_token:
                file_token = (
                    payload.get("execution_token")
                    or payload.get("rework_request_id")
                    or payload.get("rework_token")
                )
                if not file_token or file_token != expected_token:
                    # Codex Review Round 7 P2: Authenticate operator bypass receipts as a trusted path
                    bypass = payload.get("bypass")
                    tid = task.get("task_id") or task.get("id")
                    if (
                        bool(tid)
                        and isinstance(bypass, dict)
                        and (bypass.get("task_id") == tid)
                        and bypass.get("bypass_id")
                    ):
                        pass
                    else:
                        last_err = f"execution token mismatch in {path} ({file_token} != {expected_token})"
                        continue
            file_v = str(payload.get("verdict") or "").strip().lower()
            if file_v not in ("pass", "blocked"):
                last_err = (
                    f"gate verdict file {path} contains invalid verdict: '{file_v}'"
                )
                continue
            if expected_verdict:
                norm_exp = str(expected_verdict).strip().lower()
                if file_v != norm_exp:
                    return (
                        False,
                        f"durable gate verdict on disk changed to '{file_v}' (expected '{norm_exp}')",
                    )
            note = str(payload.get("note") or "").strip()
            affected_ids = []
            if file_v == "blocked":
                raw_aff = payload.get("affected_task_ids")
                if raw_aff is not None:
                    if not isinstance(raw_aff, list):
                        print(
                            f"[ADOPT VERDICT WARN] Malformed affected_task_ids in {path}: "
                            f"expected list, got {type(raw_aff).__name__}; rejecting attribution",
                            file=sys.stderr,
                        )
                        affected_ids = []
                    else:
                        valid = True
                        ids = []
                        for item in raw_aff:
                            text = str(item).strip() if isinstance(item, str) else ""
                            if not text:
                                valid = False
                                break
                            if text not in ids:
                                ids.append(text)
                        if valid:
                            affected_ids = ids
                        else:
                            print(
                                f"[ADOPT VERDICT WARN] Malformed affected_task_ids in {path}: "
                                f"contains non-string or blank item; rejecting attribution",
                                file=sys.stderr,
                            )
                            affected_ids = []
            return True, GateVerdictEvidence(file_v, note, affected_ids, st.st_mtime, snapshot_hash=file_hash)
        except Exception as exc:
            last_err = f"error inspecting gate verdict candidate {path}: {exc}"
            continue

    if not found_any:
        return False, "no durable gate verdict file found"
    return False, last_err
