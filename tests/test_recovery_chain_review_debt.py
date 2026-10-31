"""恢复链评审遗留守卫（批次②）：#336×2 + #339×2 + #360 fetch 瞬时。

1. waiting_human 通知：投递失败必须可重试（op_version 仅在成功投递后锁定），
   同一版本的重试有界退避。
2. watchdog 存活宽限：pane 探针必须证明任务标记（所有权），仅有
   agent_status=working 不得豁免超时。
3. 义务收敛：PASS 证据必须是 verified_candidate_sha（claim-only 不算），
   且证据与义务都必须绑定当前 execution。
4. integrate base 引用：网络类 fetch 失败必须走可重试路径，仅确认远端
   缺失才判 base_ref_missing（永久）。
"""
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from herdr.state_store import SQLiteStateStore

ROOT = Path(__file__).resolve().parents[1]

FROZEN = 'a' * 40


# ---------------------------------------------------------------- notify

class _EpisodeStore:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def upsert(self, key, value):
        self.data[key] = dict(value)

    def clear(self, key):
        self.data.pop(key, None)


def _notify_ports(notify_results, clock):
    calls = []
    store = _EpisodeStore()

    def notify(title, task, message, reason):
        calls.append(reason)
        result = notify_results[len(calls) - 1]
        return result

    return (SimpleNamespace(_attention_store=store, notify_attention=notify,
                            time=SimpleNamespace(time=lambda: clock[0])),
            store, calls)


def test_waiting_human_notification_is_retryable_with_backoff(monkeypatch):
    from services.controller import workflow_recovery
    monkeypatch.setattr(workflow_recovery, 'NOTIFY_RETRY_INTERVAL_SECONDS', 300.0)
    clock = [1000.0]
    ports, store, calls = _notify_ports([False, True], clock)
    op = {'id': 'op-1', 'status': 'waiting_human', 'version': 3,
          'payload': {'kind': 'fix_loop'}, 'detail': {'reason': 'r'}}

    workflow_recovery._notify_waiting_human(ports, 'wf', [op])
    assert len(calls) == 1                       # 第一次尝试已发出
    key = 'wf:recovery_waiting:op-1'
    assert store.get(key).get('op_version') != 3  # 失败不得锁定版本（RED：现状直接锁定）

    workflow_recovery._notify_waiting_human(ports, 'wf', [op])
    assert len(calls) == 1                       # 退避窗口内不重复投递

    clock[0] += 301.0
    workflow_recovery._notify_waiting_human(ports, 'wf', [op])
    assert len(calls) == 2                       # 退避后重试
    workflow_recovery._notify_waiting_human(ports, 'wf', [op])
    assert len(calls) == 2                       # 第二次成功 → 版本锁定，不再投递
    assert store.get(key).get('op_version') == 3


# ---------------------------------------------------------------- sentinel

def test_watchdog_grace_requires_pane_task_marker(monkeypatch, tmp_path):
    """Codex PR #336 P2：复用/陈旧 pane 的 working 状态不得豁免看门狗超时。"""
    from tests.test_sentinel_review_debt import (
        _make_clone, _persist, _state, _task)
    import importlib as _importlib
    sentinel = _importlib.import_module("services.herdr-sentinel")
    from herdr.state_store import SQLiteStateStore as _Store
    monkeypatch.setenv('HERDR_TASK_HARD_TIMEOUT', '10')
    now = time.time()
    clone, shas = _make_clone(tmp_path, [(10_000.0, 'baseline')])
    store = _Store(tmp_path / 'state.db')
    stored = _persist(store, _task(clone, baseline_commit=shas[0]), idle=100.0, now=now)
    monkeypatch.setattr(sentinel, '_get_store', lambda: store)

    with __import__('unittest.mock', fromlist=['patch']).patch.object(
            sentinel, '_pane_delivery_evidence',
            return_value={'has_marker': False, 'agent_status': 'working'}):
        sentinel.check_task_stalls([stored], _state(),
                                   notify_fn=lambda *a: None, now=now)
    # 无任务标记（所有权未证明）→ 不得豁免：任务必须进入失败处置
    assert store.get_task('t-sentinel')['status'] == 'failed'


# ---------------------------------------------------------------- converge

def test_obligation_convergence_requires_verified_evidence_and_current_execution(tmp_path):
    """Codex PR #339 ×2：claim-only PASS 不清账；跨 execution 的证据不清账。"""
    from services.controller.infra_recovery import converge_dangling_replacement_obligations
    store = SQLiteStateStore(tmp_path / 'state.db')
    store.save_workflow({'workflow_id': 'wf', 'status': 'running', 'execution_id': 'exec-new'})

    def task(tid, **extra):
        base = {'task_id': tid, 'workflow_id': 'wf', 'node': 'impl', 'stage': 'impl'}
        base.update(extra)
        return base

    tasks = [
        task('t-sup', status='superseded', replacement_pending=True,
             execution_id='exec-new'),
        # claim-only PASS：只有 candidate_sha（派发声明），无 verified 证据
        task('t-claim', status='cleaned', stage_verdict='pass',
             candidate_sha=FROZEN, execution_id='exec-new'),
        # 历史 execution 的 verified PASS：不得替当前义务清账
        task('t-old', status='cleaned', stage_verdict='pass',
             verified_candidate_sha=FROZEN, execution_id='exec-old'),
    ]
    for item in tasks:
        store.save_task(dict(item))
    ports = SimpleNamespace(
        workflow_closed=lambda w: False,
        load_tasks=lambda: tasks,
        _scheduler_current_frozen_candidate_sha=lambda w: FROZEN,
        _get_store=lambda: store,
    )
    assert converge_dangling_replacement_obligations(ports, 'wf', tasks=tasks) is False
    assert store.get_task('t-sup')['replacement_pending'] is True

    # 正向控制：当前 execution 的 verified PASS 才允许收敛
    tasks.append(task('t-good', status='cleaned', stage_verdict='pass',
                      verified_candidate_sha=FROZEN, execution_id='exec-new'))
    assert converge_dangling_replacement_obligations(ports, 'wf', tasks=tasks) is True
    assert store.get_task('t-sup')['replacement_pending'] is False


# ---------------------------------------------------------------- base ref

def _git(cwd, *args, check=True):
    return subprocess.run(['git', '-C', str(cwd), *args], capture_output=True,
                          text=True, check=check)


def test_transient_fetch_failure_is_not_base_ref_missing(tmp_path):
    """Codex PR #360 P2：只有 ls-remote 确认远端缺失才判永久 base_ref_missing。"""
    from herdr.task_cli_integration import resolve_integration_base_ref
    origin = tmp_path / 'origin.git'
    main_repo = tmp_path / 'main'
    clone = tmp_path / 'clone'
    subprocess.run(['git', 'init', '--bare', str(origin)], check=True, capture_output=True)
    main_repo.mkdir()
    _git(main_repo, 'init', '-b', 'main')
    (main_repo / 'f.txt').write_text('x\n')
    _git(main_repo, 'add', '.')
    _git(main_repo, 'commit', '-m', 'init')
    _git(main_repo, 'remote', 'add', 'origin', str(origin))
    _git(main_repo, 'push', 'origin', 'main')
    subprocess.run(['git', 'clone', str(origin), str(clone)], check=True, capture_output=True)
    task_id = 't-int'

    # 1) fetch 成功
    ref, err = resolve_integration_base_ref(str(clone), str(main_repo), 'main', task_id)
    assert (ref, err) == ('origin/main', None)

    # 2) fetch 失败 + 本地回退存在 → 走本地回退
    _git(main_repo, 'branch', 'feature-x')
    broken = tmp_path / 'broken-clone'
    subprocess.run(['git', 'clone', str(origin), str(broken)], check=True, capture_output=True)
    # origin fetch 必然失败（URL 失效），但本地回退可用 → 仍走回退成功
    _git(broken, 'remote', 'set-url', 'origin', str(tmp_path / 'vanished-origin.git'))
    ref, err = resolve_integration_base_ref(str(broken), str(main_repo), 'main', task_id)
    assert err is None and ref.startswith('refs/herdr/bases/')

    # 3) 远端确认缺失（ls-remote 成功且无该 ref）→ 永久 base_ref_missing
    ref, err = resolve_integration_base_ref(str(clone), str(main_repo), 'no-such-branch', task_id)
    assert (ref, err) == (None, 'base_ref_missing')

    # 4) origin 不可达（网络类失败）且无本地回退 → 可重试，而非永久
    main_repo_no_base = tmp_path / 'main-no-base'
    main_repo_no_base.mkdir()
    _git(main_repo_no_base, 'init', '-b', 'trunk')  # 无 refs/heads/main，本地回退不可用
    gone = tmp_path / 'gone'
    subprocess.run(['git', 'clone', str(origin), str(gone)], check=True, capture_output=True)
    _git(gone, 'remote', 'set-url', 'origin', str(tmp_path / 'vanished.git'))
    ref, err = resolve_integration_base_ref(str(gone), str(main_repo_no_base), 'main', task_id)
    assert (ref, err) == (None, 'base_fetch_transient')
