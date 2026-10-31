"""PR #343 评审遗留守卫：cockpit 预算闭环与跨脚本作用域接线。

五条缺陷（2026-10-31 审计批次①）：
1. ``_bounded_copy`` 字段级截断后 sizes 归零，行预算二段无法继续收缩，
   三个 100KB 字段可产出 >8192 字节的行。
2. ``read_cockpit`` 在预算检查之后追加 blockers/receipts 且 receipts 不限量，
   2MB+ 响应仍 read_state=confirmed。
3. ``execute_action`` 不检查 read_state，超预算投影的动作仍可自动执行。
4. ``cockpitTargetLookup`` 未从 IIFE 解构导出，深链打开任务拿不到定向 actions。
5. Console 的 Controller 版本读的是 Console 进程指纹，而非 Controller
   自发布的运行证据。
"""
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from herdr.state_store import SQLiteStateStore

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path):
    result = SQLiteStateStore(tmp_path / 'state.db')
    result.save_workflow({'workflow_id': 'wf-1', 'status': 'paused', 'execution_id': 'exec-1',
                          'config': {'nodes': [{'id': 'build', 'node_type': 'agent'}]}})
    return result


def deps_for(store, **kw):
    return SimpleNamespace(store=store, **kw)


def test_bounded_copy_row_budget_can_shrink_earlier_truncated_fields():
    """Codex PR #343 P2：字段级截断后剩余大小必须可被行预算二段继续收缩。"""
    from herdr.controller_cockpit import TASK_IDENTITY_FIELDS, _bounded_copy, _utf8_len
    big = 'x' * 100_000
    record = {'task_id': 't1', 'goal': big, 'prompt': big, 'summary': big,
              'note': 'y' * 200, 'status': 'working'}
    row = _bounded_copy(record, TASK_IDENTITY_FIELDS, 4096, 8192)
    size = _utf8_len(json.dumps(row, ensure_ascii=False, default=str))
    assert size <= 8192, f'row budget exceeded after both passes: {size}'
    # 截断必须如实记录（字段仍可辨识），身份字段完整。
    assert row['task_id'] == 't1'
    fields = {item['field'] for item in row.get('_truncated_fields') or []}
    assert {'goal', 'prompt', 'summary'} <= fields


def _insert_receipt(store, workflow_id, key, payload):
    from services.controller.cockpit_automation import persist_receipt
    receipt = dict(payload)
    receipt.setdefault('workflow_id', workflow_id)
    receipt.setdefault('action_id', key.split('#')[0])
    persist_receipt(store, receipt, key=key)


def test_read_cockpit_enforces_response_budget_after_shell_enrichment(store):
    """Codex PR #343 P2：blockers/receipts 在投影预算检查之后追加，必须同样受限。"""
    from services.controller.cockpit_automation import read_cockpit
    huge = 'y' * (2 * 1024 * 1024)
    _insert_receipt(store, 'wf-1', 'wf-1:resume_workflow#1',
                    {'identity_key': 'k1', 'status': 'applied', 'echo': huge})
    result = read_cockpit(store, 'wf-1', controller={'status': 'running'})
    size = len(json.dumps(result, ensure_ascii=False, default=str))
    if result.get('read_state') == 'confirmed':
        assert size <= 2 * 1024 * 1024, f'confirmed read exceeds advertised budget: {size}'
    else:
        assert result.get('errors', {}).get('response_budget') == 'exceeded'
    assert 'read_state' in result


def test_automatic_action_refused_when_projection_read_state_unknown(store):
    """Codex PR #343 P2：预算失败的投影必须阻断动作自动化（fail-closed）。"""
    from services.controller.cockpit_automation import execute_action, read_cockpit
    action = read_cockpit(store, 'wf-1', controller={'status': 'running'})['actions'][0]

    def unknown_project(workflow_id):
        result = read_cockpit(store, workflow_id, controller={'status': 'running'})
        result['read_state'] = 'unknown'
        result['errors'] = {**result.get('errors', {}), 'response_budget': 'exceeded'}
        return result

    result = execute_action(
        {'workflow_id': 'wf-1', 'action_id': action['action_id'],
         'expected_identity': action['expected_identity']},
        deps=deps_for(store, project=unknown_project), automatic=True)
    assert result['receipt']['status'] == 'rejected'
    assert result['receipt']['reason_code'] == 'read_state_unknown'
    assert store.get_workflow('wf-1')['status'] == 'paused'


def test_cockpit_target_lookup_exported_to_shared_script_scope():
    """Codex PR #343 P2：cockpitTargetLookup 必须进入顶层解构，深链才触发定向读取。"""
    from console import page
    from tests.test_console_cockpit_runtime import PRELUDE
    script = re.findall(r'<script>(.*?)</script>', page.HTML_TEMPLATE, re.S)[0]
    script = script.replace('\ninitFromUrlOrState();', '\n')
    probe = PRELUDE + '\n' + script + '''
if (typeof cockpitTargetLookup !== 'function') {
  console.error('cockpitTargetLookup is not exported to shared scope');
  process.exit(1);
}
'''
    # Linux 单参数上限：365KB 的探针经 stdin 传入（node -），不走 argv
    result = subprocess.run(['node', '-'], input=probe, text=True,
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr or 'shared scope probe failed'


def test_controller_version_cache_invalidates_on_controller_restart(monkeypatch, tmp_path):
    """Codex 二轮：Controller 重启追加新指纹后缓存必须失效；缺失不永久缓存。"""
    from console import runtime
    log = tmp_path / 'controller.out.log'
    log.write_text('HERDR_RUNTIME_FINGERPRINT={"running_sha": "sha-v1"}\n', encoding='utf-8')
    monkeypatch.setenv('HERDR_CONTROLLER_LOG', str(log))
    runtime._controller_version = None
    runtime._controller_version_source = None
    try:
        assert runtime.controller_version() == 'sha-v1'
        log.write_text('HERDR_RUNTIME_FINGERPRINT={"running_sha": "sha-v2"}\n', encoding='utf-8')
        assert runtime.controller_version() == 'sha-v2'
        log.unlink()
        assert runtime.controller_version() in (None, '')
    finally:
        runtime._controller_version = None
        runtime._controller_version_source = None


def test_controller_version_reads_controller_published_fingerprint(monkeypatch, tmp_path):
    """Codex PR #343 P2：Controller 版本必须来自 Controller 自发布证据，缺失即 unknown。"""
    from console import runtime
    log = tmp_path / 'controller.out.log'
    log.write_text(
        '[CONTROLLER V12] starting\n'
        'HERDR_RUNTIME_FINGERPRINT={"pid": 42, "running_import_root": "/x",'
        ' "running_sha": "abc123def", "service": "com.user.herdr-controller"}\n',
        encoding='utf-8')
    monkeypatch.setenv('HERDR_CONTROLLER_LOG', str(log))
    runtime._controller_version = None
    runtime._controller_version_source = None
    try:
        assert runtime.controller_version() == 'abc123def'
    finally:
        runtime._controller_version = None
    # 证据缺失 → unknown（不得回退为 Console 自身指纹）。
    monkeypatch.delenv('HERDR_CONTROLLER_LOG', raising=False)
    monkeypatch.setenv('HOME', str(tmp_path / 'empty-home'))
    (tmp_path / 'empty-home').mkdir()
    runtime._controller_version = None
    runtime._controller_version_source = None
    try:
        assert runtime.controller_version() in (None, '')
    finally:
        runtime._controller_version = None
