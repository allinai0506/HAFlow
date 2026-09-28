"""Selective Reverification v1 (HAFlow PR #108).

候选变化后「哪些 verifier 必须重跑」的纯决策核心。核心原则:

    Reuse must be proven. Rerun is the default.

UNKNOWN → RERUN。复用只在下列四者同时成立时才允许:

1. **Candidate Diff** —— 真实 ``git diff --name-status A B``,不是 Agent 自述;
2. **Explicit Non-impact Policy** —— 显式声明的「明确不会影响」路径;
3. **Previous Verified PASS** —— 来源验证自身 PASS 且带 verified_candidate_sha;
4. **Immutable Reuse Fact** —— 复用是新事实,绝不改写历史 Task。

任一条件无法证明 → RERUN。判据全部是可枚举的机器事实,没有 LLM、
没有 import 关系推理、没有 AST/CodeGraph、没有测试用例选择。

边界与分层(§34):
- 本模块只回答「要不要重新验证」;
- ``herdr/scheduler.py`` 决定「哪些节点现在 Ready / 门禁是否放行」;
- ``herdr/direct_dispatch.py`` + controller 决定「派给谁、真的派不派」。

因此本模块的决策逻辑是纯函数,**不 import scheduler**(scheduler 同样不
import 本模块),避免复用决策与门禁裁决互相污染。唯一的例外是
:func:`_same_revision`:revision 相等性必须与事实层用同一条规则判定,否则
同一个不变量会有两个答案,所以它**惰性**导入 scheduler 复用那一个判定,
而不是在此重写一份。

Git 调用一律有界(``timeout``)、失败一律返回不可证明值(``""`` / ``None``
/ ``False``),绝不猜测。风格对齐 ``herdr/scheduler.py`` 的
``resolve_candidate_sha_for_branch``。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 策略版本进入每一条决策事实:未来策略变化后,仍能解释
# 「为什么当时这个 verifier 被跳过」。
POLICY_VERSION = "selective-reverification-v1"

# 决策。
DECISION_REUSE = "reuse"
DECISION_RERUN = "rerun"

#: 来源验证的性质。``fresh`` 是真的跑过一次。v1 只接受 fresh 作为复用来源
#: (§15:不递归复用链)——复用得来的派生事实不得再次作为来源。
SOURCE_FRESH = "fresh"

# 稳定的原因码。审计、CLI 展示和测试都依赖这些字符串,不做自由文本。
REASON_REUSE_NON_IMPACT = "all_changed_paths_explicitly_non_impacting"
REASON_RERUN_DIFF_UNAVAILABLE = "diff_unavailable"
REASON_RERUN_NON_LINEAR = "non_linear_candidate"
REASON_RERUN_NO_CHANGES = "no_changed_paths"
REASON_RERUN_POLICY_ABSENT = "policy_not_declared"
REASON_RERUN_NO_SCOPE = "no_reusable_scope_declared"
REASON_RERUN_OUTSIDE_SCOPE = "changed_paths_outside_declared_scope"
REASON_RERUN_SOURCE_ABSENT = "source_verification_absent"
REASON_RERUN_SOURCE_NOT_PASS = "source_verdict_not_pass"
REASON_RERUN_SOURCE_UNBOUND = "source_verification_missing_verified_candidate_sha"
REASON_RERUN_SOURCE_FOREIGN = "source_verification_bound_to_other_candidate"
REASON_RERUN_SOURCE_CLAIM_MISMATCH = "source_candidate_claim_mismatch"
REASON_RERUN_SOURCE_NOT_FRESH = "source_verification_is_not_fresh"

# 键名沿用 #107 已有契约,不另造命名空间。
POLICY_KEY = "reverification"
SCOPE_KEY = "reusable_only_if_changes_within"

GIT_TIMEOUT = 10

#: ``git diff --name-status`` 的状态字母。
#: ``R``/``C`` 额外带一条旧路径;``T``(类型变化)与 git 实测一致只带一条。
_TWO_PATH_STATUSES = ("R", "C")
_ONE_PATH_STATUSES = ("M", "T")
#: A status token is a known letter, optionally followed by git's similarity
#: score (git only emits one for R/C). Anything else is a shape this parser
#: does not understand, and guessing it would mean inventing paths.
_STATUS_TOKEN_RE = re.compile(r"\A[MADTRC][0-9]{0,3}\Z")

_IDENTITY_PREFIX_DECISION = "rever_"
_IDENTITY_PREFIX_PLAN = "replan_"


# ============================================================
# 策略
# ============================================================

def default_policy() -> Dict[str, Any]:
    """Fail-closed 默认策略:不声明任何 verifier。"""
    return {"version": POLICY_VERSION, "verifiers": {}}


def policy_from_workflow(workflow_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """从 workflow 配置解析策略;任何无法解析的形态都退回默认(全部 RERUN)。

    两种「没有安全范围」的写法必须等价,它们都表示「不存在安全复用范围」,
    而不是「什么都可以复用」(§10)::

        review: []
        review: {reusable_only_if_changes_within: []}
    """
    fallback = default_policy()
    block = (workflow_cfg or {}).get(POLICY_KEY)
    if not isinstance(block, dict):
        return fallback

    version = block.get("version")
    version = str(version).strip() if isinstance(version, str) else ""
    if not version:
        version = POLICY_VERSION

    verifiers: Dict[str, Dict[str, Any]] = {}
    for name, entry in block.items():
        if name == "version" or not isinstance(name, str) or not name.strip():
            continue
        scope = _parse_scope(entry)
        if scope is None:
            # 声明了但写坏:当作没声明,而不是当作空范围。两者都 RERUN,
            # 但原因码不同,审计时能看出是配置错误。
            continue
        verifiers[name.strip()] = {SCOPE_KEY: scope}
    if not verifiers:
        return {"version": version, "verifiers": {}}
    return {"version": version, "verifiers": verifiers}


def _parse_scope(entry: Any) -> Optional[List[str]]:
    """把一条 verifier 策略解析成路径列表;None 表示无法解析。"""
    if entry is None:
        return []
    if isinstance(entry, list):
        raw = entry
    elif isinstance(entry, dict):
        if SCOPE_KEY not in entry:
            return []
        raw = entry.get(SCOPE_KEY)
        if raw is None:
            return []
        if not isinstance(raw, list):
            return None
    else:
        return None
    patterns: List[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            return None
        patterns.append(item.strip())
    return patterns


def verifier_scope(policy: Optional[Dict[str, Any]], verifier: str) -> Optional[List[str]]:
    """某 verifier 的安全范围。

    返回 ``None`` 表示策略未声明该 verifier(与「已声明但范围为空」区分),
    两者都 RERUN,但审计原因不同。
    """
    verifiers = (policy or {}).get("verifiers")
    if not isinstance(verifiers, dict):
        return None
    entry = verifiers.get(str(verifier or "").strip())
    if not isinstance(entry, dict):
        return None
    scope = entry.get(SCOPE_KEY)
    if not isinstance(scope, list):
        return None
    return [str(p) for p in scope]


def policy_identity(policy: Optional[Dict[str, Any]]) -> str:
    """A fingerprint of the *resolved* policy, not of its declared version.

    A version string is a human label and does not change when the policy does.
    Narrowing ``test`` from ``docs/**/*.md`` to ``docs/adr/**`` — or deleting the
    block entirely — leaves the version untouched, so a version-keyed lookup
    would keep honouring reuse that the current policy no longer permits, and
    narrowing the scope would be purely cosmetic.

    The fingerprint therefore covers what the policy actually resolves to: the
    version plus every verifier's scope. Any change that can alter a reuse
    verdict changes the identity, and reuse recorded under the old policy stops
    being honoured.
    """
    resolved = policy if isinstance(policy, dict) else default_policy()
    verifiers = resolved.get("verifiers")
    verifiers = verifiers if isinstance(verifiers, dict) else {}
    scopes = {
        str(name).strip(): sorted(
            str(p) for p in (verifier_scope(resolved, name) or []))
        for name in verifiers
    }
    return "policy-" + _identity("", "policy", resolved.get("version") or "",
                                 json.dumps(scopes, ensure_ascii=False,
                                            sort_keys=True,
                                            separators=(",", ":"))).lstrip("-")[:32]


# ============================================================
# 路径范围匹配
# ============================================================

_GLOB_CACHE: Dict[str, "re.Pattern[str]"] = {}


def _glob_to_regex(pattern: str) -> "re.Pattern[str]":
    """把 gitignore 风格的 glob 编译成锚定的正则。

    语义(与 gitignore 一致,刻意比 fnmatch 更严):
    - ``**/`` 匹配零个或多个目录段,所以 ``docs/**/*.md`` 命中 ``docs/a.md``;
    - 结尾的 ``/**`` 要求至少一层内容,``docs/**`` 不命中 ``docs`` 自身;
    - 单个 ``*`` 与 ``?`` 不跨 ``/``,``docs/*.md`` 不命中 ``docs/x/a.md``;
    - 路径按 git 的仓库相对原样比较,**不做** ``./`` 归一化——归一化会让
      无法判定的相对路径凭空落进安全范围。
    """
    cached = _GLOB_CACHE.get(pattern)
    if cached is not None:
        return cached
    out: List[str] = []
    i, n = 0, len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.endswith("/**") and i == n - 3:
            out.append("/.+")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    compiled = re.compile("".join(out) + r"\Z")
    _GLOB_CACHE[pattern] = compiled
    return compiled


def scope_matches(path: str, pattern: str) -> bool:
    """路径是否落在显式声明的非影响范围内。"""
    target = str(path or "")
    if not target or not pattern:
        return False
    try:
        return _glob_to_regex(pattern).match(target) is not None
    except re.error:  # pragma: no cover - 编译期已保证,防御性保留
        return False


# ============================================================
# Git 事实层(有界、失败即不可证明)
# ============================================================

def _git(repo_path: str, args: Sequence[str], timeout: int = GIT_TIMEOUT
         ) -> Tuple[Optional[str], bool]:
    """跑一条只读 git 命令。

    返回 ``(stdout, ok)``。任何异常、超时、非零退出都返回 ``(None, False)``,
    调用方一律按「无法证明」处理——绝不用「看起来应该没问题」兜底。
    """
    if not repo_path:
        return None, False
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path)] + list(args),
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except Exception:
        return None, False
    if result.returncode != 0:
        return None, False
    return (result.stdout or ""), True


def canonicalize_sha(repo_path: str, sha: str, timeout: int = GIT_TIMEOUT) -> str:
    """把 revision 解析成完整 commit SHA;不可解析返回 ``""``。

    权威 canonicalisation 走 ``rev-parse <sha>^{commit}``:缩写可解析成全量,
    非 commit 对象与未知 revision 都拿不到 40 位 SHA。
    """
    raw = str(sha or "").strip()
    if not raw or not repo_path:
        return ""
    out, ok = _git(repo_path, ["rev-parse", "--verify", "--quiet",
                               raw + "^{commit}"], timeout)
    if not ok:
        return ""
    return str(out or "").strip().lower()


def is_ancestor(repo_path: str, ancestor: str, descendant: str,
                timeout: int = GIT_TIMEOUT) -> bool:
    """``ancestor`` 是否是 ``descendant`` 的祖先;无法证明返回 ``False``。

    候选必须线性演进(§16)。force push / 切分支 / 回滚到分叉历史都会让这里
    返回 False,于是所有 verifier 一律 RERUN。
    """
    left = str(ancestor or "").strip()
    right = str(descendant or "").strip()
    if not left or not right or not repo_path:
        return False
    _out, ok = _git(repo_path, ["merge-base", "--is-ancestor", left, right], timeout)
    return ok


def compute_candidate_diff(repo_path: str, from_sha: str, to_sha: str,
                           timeout: int = GIT_TIMEOUT
                           ) -> Tuple[Optional[List[Dict[str, str]]], str]:
    """产出 ``A → B`` 的真实变更条目。

    返回 ``(entries, reason)``。``entries is None`` 表示 diff 不可得,
    ``reason`` 是稳定原因码(``diff_unavailable``)。
    """
    left = str(from_sha or "").strip()
    right = str(to_sha or "").strip()
    if not repo_path or not left or not right:
        return None, REASON_RERUN_DIFF_UNAVAILABLE
    out, ok = _git(
        repo_path,
        ["-c", "core.quotePath=false", "diff", "--name-status", "-z", left, right],
        timeout,
    )
    if not ok:
        return None, REASON_RERUN_DIFF_UNAVAILABLE
    entries = parse_name_status(out or "")
    if entries is None:
        return None, REASON_RERUN_DIFF_UNAVAILABLE
    return entries, ""


def parse_name_status(raw: Any) -> Optional[List[Dict[str, str]]]:
    """解析 ``git diff --name-status -z`` 输出。

    用 ``-z`` 是为了不依赖引号转义:带空格、制表符或非 ASCII 的路径会原样
    NUL 分隔,不需要反解 C 风格引号。无法解析一律返回 ``None``,由调用方
    Fail-Closed 到 RERUN——绝不「尽力猜一个路径出来」。
    """
    if not isinstance(raw, str):
        return None
    if raw == "":
        return []
    tokens = raw.split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    entries: List[Dict[str, str]] = []
    i, n = 0, len(tokens)
    while i < n:
        status = tokens[i]
        if not _STATUS_TOKEN_RE.match(status):
            return None
        letter = status[0]
        if letter in _TWO_PATH_STATUSES:
            if i + 2 >= n:
                return None
            old_path, new_path = tokens[i + 1], tokens[i + 2]
            i += 3
        elif letter == "A":
            if i + 1 >= n:
                return None
            old_path, new_path = "", tokens[i + 1]
            i += 2
        elif letter == "D":
            if i + 1 >= n:
                return None
            old_path, new_path = tokens[i + 1], ""
            i += 2
        else:  # M / T
            if i + 1 >= n:
                return None
            old_path = new_path = tokens[i + 1]
            i += 2
        if not old_path and not new_path:
            return None
        entries.append({
            "status": letter,
            "old_path": old_path,
            "new_path": new_path,
        })
    return entries


def changed_paths(entries: Optional[Sequence[Dict[str, str]]]) -> List[str]:
    """变更涉及的全部路径(去重、排序)。

    rename/copy 两侧都算:只看 ``new_path`` 会让
    ``R herdr/a.py docs/a.md`` 被误判成 docs-only(§12)。
    """
    found: set = set()
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        for key in ("old_path", "new_path"):
            value = str(entry.get(key) or "").strip()
            if value:
                found.add(value)
    return sorted(found)


def collect_candidate_changes(repo_path: str, from_sha: str, to_sha: str,
                              timeout: int = GIT_TIMEOUT
                              ) -> Tuple[Optional[List[Dict[str, str]]], str]:
    """从 canonicalise 到 diff 的完整事实链,任一步不可证明即 RERUN。

    顺序有语义:先确认两个 SHA 存在且是 commit,再确认线性演进,最后取 diff。
    线性检查放在 diff 之前,是因为对分叉历史做「变更了什么」的分析没有意义。
    """
    left = canonicalize_sha(repo_path, from_sha, timeout)
    if not left:
        return None, REASON_RERUN_DIFF_UNAVAILABLE
    right = canonicalize_sha(repo_path, to_sha, timeout)
    if not right:
        return None, REASON_RERUN_DIFF_UNAVAILABLE
    if left == right:
        # 同一候选不存在 re-verification 需求,交由调用方保持 #107 幂等语义。
        return None, REASON_RERUN_DIFF_UNAVAILABLE
    if not is_ancestor(repo_path, left, right, timeout):
        return None, REASON_RERUN_NON_LINEAR
    return compute_candidate_diff(repo_path, left, right, timeout)


# ============================================================
# 判定
# ============================================================

def evaluate_verifier_impact(verifier: str, entries: Optional[Sequence[Dict[str, str]]],
                             policy: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """仅凭「变更事实 + 显式策略」判定某个 verifier 是否可以复用。

    不看来源验证证据:那是 :func:`build_reverification_plan` 的第二道关。
    """
    verifier = str(verifier or "").strip()
    scope = verifier_scope(policy, verifier)
    paths = changed_paths(entries)
    base = {
        "verifier": verifier,
        "changed_paths": paths,
        "reusable_scope": list(scope) if scope is not None else None,
        "out_of_scope_paths": [],
    }
    if scope is None:
        return {**base, "decision": DECISION_RERUN,
                "reason": REASON_RERUN_POLICY_ABSENT}
    if not scope:
        return {**base, "decision": DECISION_RERUN, "reason": REASON_RERUN_NO_SCOPE}
    if not paths:
        # 两个不同 commit 却没有路径变化(空提交):没有可证明的非影响事实。
        return {**base, "decision": DECISION_RERUN, "reason": REASON_RERUN_NO_CHANGES}
    outside = [p for p in paths
               if not any(scope_matches(p, pattern) for pattern in scope)]
    if outside:
        return {**base, "out_of_scope_paths": outside, "decision": DECISION_RERUN,
                "reason": REASON_RERUN_OUTSIDE_SCOPE}
    return {**base, "decision": DECISION_REUSE, "reason": REASON_REUSE_NON_IMPACT}


def _select_source(sources: Optional[Sequence[Dict[str, Any]]], verifier: str
                   ) -> Optional[Dict[str, Any]]:
    """Pick the newest admissible source verification for one verifier.

    Newest wins because a later BLOCKED must never be masked by an earlier PASS
    on the same candidate. Ties break on task id so the choice is deterministic
    and replayable rather than dependent on dict or file ordering.
    """
    name = str(verifier or "").strip()
    matched = [
        s for s in (sources or [])
        if isinstance(s, dict) and str(s.get("verifier") or "").strip() == name
    ]
    if not matched:
        return None
    return max(matched, key=lambda s: (
        _as_float(s.get("updated_at")),
        str(s.get("task_id") or ""),
    ))


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _same_revision(left: Any, right: Any) -> bool:
    """Strict revision equality, using the scheduler's own rule.

    The fact store binds a source with ``scheduler.shas_identical``, so the plan
    must judge the same binding the same way. Comparing raw strings here would
    reject an abbreviated-but-valid ``verified_candidate_sha`` here and accept it
    one layer up — two answers to one invariant, and the stricter one is in the
    wrong place (it would report "bound to another candidate" for a source that
    is in fact correctly bound).
    """
    try:
        from .scheduler import shas_identical
    except ImportError:  # pragma: no cover - script-style fallback
        from herdr.scheduler import shas_identical
    return shas_identical(left, right)


def _rerun(base: Dict[str, Any], reason: str,
           source: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a RERUN decision, naming the source evidence it was refused on.

    The source fields are kept even for a RERUN: an operator reading the audit
    trail needs to see *which* verification was rejected and why (not passed,
    not bound to A, already itself a reuse), not just that something reruns.
    """
    return {
        **base,
        "decision": DECISION_RERUN,
        "reason": reason,
        "source_task_id": str((source or {}).get("task_id") or ""),
        "source_verdict": str((source or {}).get("verdict") or ""),
        "source_candidate_sha": str((source or {}).get("candidate_sha") or ""),
        "source_verified_candidate_sha": str(
            (source or {}).get("verified_candidate_sha") or ""),
        "source_freshness": str((source or {}).get("source") or ""),
    }


def build_reverification_plan(workflow_id: str, from_sha: str, to_sha: str,
                              entries: Optional[Sequence[Dict[str, str]]],
                              sources: Optional[Sequence[Dict[str, Any]]],
                              policy: Optional[Dict[str, Any]],
                              *, diff_reason: str = "",
                              verifiers: Optional[Sequence[str]] = None,
                              episode_id: Any = ""
                              ) -> Dict[str, Any]:
    """构建一次可持久化、可重放、可解释的重新验证计划。

    判定顺序即证据强度顺序,原因码因此永远指向**最先失败的那道关**:
    策略 → 变更范围 → 来源存在性 → 来源 PASS → 来源绑定 → 来源新鲜度。

    ``verifiers`` 是本次要判定的 verifier 集合(通常取自工作流的汇聚门禁
    分支)。它与策略声明的键刻意解耦:策略没声明的 verifier 仍会得到一条
    ``policy_not_declared`` 的 RERUN 决策,而不是从计划里消失。审计必须能
    区分「判定为重跑」与「压根没判定」——后者无法回答「为什么没重跑」。
    """
    from_sha = str(from_sha or "").strip()
    to_sha = str(to_sha or "").strip()
    policy = policy if isinstance(policy, dict) else default_policy()
    version = str(policy.get("version") or POLICY_VERSION)
    fingerprint = policy_identity(policy)
    # Carried on every fact so a reader can tell *which freeze* authorised it.
    # An empty episode means "the caller could not name one", which is
    # unprovable provenance and therefore refuses reuse downstream.
    episode = "" if episode_id in (None, "") else str(episode_id)
    verifiers_cfg = policy.get("verifiers")
    verifiers_cfg = verifiers_cfg if isinstance(verifiers_cfg, dict) else {}
    if verifiers is None:
        wanted = list(verifiers_cfg)
    else:
        wanted = [str(v).strip() for v in verifiers if str(v or "").strip()]
    paths = changed_paths(entries)

    plan: Dict[str, Any] = {
        "workflow_id": str(workflow_id or ""),
        "plan_id": plan_identity(workflow_id, from_sha, to_sha, version,
                                 fingerprint, episode),
        "candidate_frozen_event_id": episode,
        "from_candidate_sha": from_sha,
        "to_candidate_sha": to_sha,
        "changed_paths": paths,
        "changed_entries": [dict(e) for e in (entries or []) if isinstance(e, dict)],
        "diff_available": entries is not None,
        "diff_reason": str(diff_reason or ""),
        "policy": {name: dict(cfg) for name, cfg in sorted(verifiers_cfg.items())
                   if isinstance(cfg, dict)},
        "policy_version": version,
        "policy_identity": fingerprint,
        "verifiers": {},
    }

    for name in sorted(set(wanted)):
        impact = evaluate_verifier_impact(name, entries, policy)
        # Every per-verifier decision is self-contained on purpose: each one is
        # persisted as its own immutable fact, so it must name the episode it
        # belongs to and carry the evidence the decision rests on. A decision
        # that only made sense when read next to its parent plan could not be
        # audited, replayed, or explained in isolation.
        base = {
            "verifier": name,
            "decision_identity": decision_identity(
                workflow_id, from_sha, to_sha, name, version, fingerprint,
                episode),
            "policy_version": version,
            "policy_identity": fingerprint,
            "candidate_frozen_event_id": episode,
            "from_candidate_sha": from_sha,
            "to_candidate_sha": to_sha,
            "changed_paths": paths,
            "reusable_scope": impact["reusable_scope"],
            "out_of_scope_paths": impact["out_of_scope_paths"],
        }
        if entries is None:
            plan["verifiers"][name] = _rerun(
                base, diff_reason or REASON_RERUN_DIFF_UNAVAILABLE, None)
            continue

        if impact["decision"] != DECISION_REUSE:
            plan["verifiers"][name] = _rerun(base, impact["reason"], None)
            continue

        source = _select_source(sources, name)
        if source is None:
            plan["verifiers"][name] = _rerun(
                base, REASON_RERUN_SOURCE_ABSENT, None)
            continue
        if str(source.get("verdict") or "").strip().lower() != "pass":
            plan["verifiers"][name] = _rerun(
                base, REASON_RERUN_SOURCE_NOT_PASS, source)
            continue
        verified = str(source.get("verified_candidate_sha") or "").strip()
        if not verified:
            plan["verifiers"][name] = _rerun(
                base, REASON_RERUN_SOURCE_UNBOUND, source)
            continue
        if not _same_revision(verified, from_sha):
            plan["verifiers"][name] = _rerun(
                base, REASON_RERUN_SOURCE_FOREIGN, source)
            continue
        # The claim must bind to the same candidate as the evidence. A task that
        # was told to verify B but whose verdict-time read says A is a
        # claim/evidence mismatch: PR #107's join gate refuses such a task as
        # proof of *either* revision, so it cannot be promoted into proof of A
        # here either. Reuse must never rest on a verification that failed the
        # gate of its own round.
        claimed = str(source.get("candidate_sha") or "").strip()
        if not claimed or not _same_revision(claimed, from_sha):
            plan["verifiers"][name] = _rerun(
                base, REASON_RERUN_SOURCE_CLAIM_MISMATCH, source)
            continue
        if str(source.get("source") or "") != SOURCE_FRESH:
            # Missing freshness is *not* assumed fresh. A source that does not
            # say how it was obtained is an unknown-provenance record, and in a
            # module whose premise is that reuse must be proven, unknown has to
            # mean RERUN.
            plan["verifiers"][name] = _rerun(
                base, REASON_RERUN_SOURCE_NOT_FRESH, source)
            continue
        plan["verifiers"][name] = {
            **base,
            "decision": DECISION_REUSE,
            "reason": REASON_REUSE_NON_IMPACT,
            "source_task_id": str(source.get("task_id") or ""),
            "source_verdict": "pass",
            "source_candidate_sha": str(source.get("candidate_sha") or ""),
            "source_verified_candidate_sha": verified,
            "source_freshness": "fresh",
        }
    return plan


# ============================================================
# 事实身份
# ============================================================

def _identity(prefix: str, *parts: Any) -> str:
    """确定性、无歧义的固定长度身份。

    用 JSON 数组编码而不是 ``"-"`` 拼接:``("a-b","c")`` 与 ``("a","b-c")``
    必须可区分。哈希只用于把身份压成定长主键,不是用来「近似去重」。
    """
    canonical = json.dumps([str(p or "") for p in parts],
                           ensure_ascii=False, separators=(",", ":"))
    return prefix + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def decision_identity(workflow_id: str, from_sha: str, to_sha: str,
                      verifier: str, policy_version: str,
                      policy_fingerprint: str = "",
                      episode_id: Any = "") -> str:
    """一条 reuse/rerun 决策事实的身份。

    身份是**episode** 而不是集合:

    - ``from`` / ``to`` 按位参与编码,所以 ``A → B`` 与 ``B → A`` 必然不同
      (#107 在候选身份上踩过的同一个坑);
    - ``episode_id`` 进一步绑定到**这一次候选冻结**,所以同一对
      (from, to) 在不同轮次出现时也彼此独立;
    - 策略以**解析后的指纹**参与身份,而不只是声明的版本号——版本号是个人类
      标签,收窄范围不会改变它。

    ``episode_id`` 是候选冻结事件的唯一 id,而不是候选 SHA:回滚到曾经冻结过的
    SHA 时,新的冻结是**新的一轮**,旧轮次的 reuse 事实不得复活。
    """
    return _identity(_IDENTITY_PREFIX_DECISION, workflow_id, from_sha, to_sha,
                     verifier, policy_fingerprint or policy_version, episode_id)


def plan_identity(workflow_id: str, from_sha: str, to_sha: str,
                  policy_version: str, policy_fingerprint: str = "",
                  episode_id: Any = "") -> str:
    """一次重新验证 episode 的身份(覆盖该 episode 内的全部 verifier)。"""
    return _identity(_IDENTITY_PREFIX_PLAN, workflow_id, from_sha, to_sha,
                     policy_fingerprint or policy_version, episode_id)


# ============================================================
# 计划切片 / 指标
# ============================================================

def _decisions(plan: Optional[Dict[str, Any]], decision: str) -> List[Dict[str, Any]]:
    verifiers = (plan or {}).get("verifiers")
    if not isinstance(verifiers, dict):
        return []
    return [dict(v) for _name, v in sorted(verifiers.items())
            if isinstance(v, dict) and v.get("decision") == decision]


def select_reuse_decisions(plan: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return _decisions(plan, DECISION_REUSE)


def select_rerun_decisions(plan: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return _decisions(plan, DECISION_RERUN)


def reverification_metrics(decisions: Optional[Sequence[Dict[str, Any]]]
                           ) -> Dict[str, Any]:
    """最小可算指标集。刻意不做 Dashboard。"""
    reuse = sum(1 for d in (decisions or [])
                if isinstance(d, dict) and d.get("decision") == DECISION_REUSE)
    rerun = sum(1 for d in (decisions or [])
                if isinstance(d, dict) and d.get("decision") == DECISION_RERUN)
    total = reuse + rerun
    return {
        "total_verifiers": total,
        "rerun_count": rerun,
        "reuse_count": reuse,
        "reuse_rate": (reuse / total) if total else 0.0,
    }
