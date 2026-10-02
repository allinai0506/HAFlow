"""Provider transport replaced; smoke parsing and native startup policy remain real."""
import json
from unittest.mock import patch
import pytest
from herdr import deep_preflight as dp, agent_adapter as aa, agent_router as ar

@pytest.mark.parametrize('stdout,stderr', [('', ''), ('Usage: cli --help', ''), ('Reply with exactly HERDR_PREFLIGHT_OK and nothing else.', ''), ('', 'HERDR_PREFLIGHT_OK')])
def test_exit_zero_without_response_is_unknown(stdout, stderr):
    with patch.object(dp, 'choose_smoke_command', return_value=(['fake'], 'test')), patch.object(dp, 'run', return_value={'returncode':0,'stdout':stdout,'stderr':stderr}):
        assert dp.smoke_probe('codex', 'fake', '.')['status'] == 'UNKNOWN'

@pytest.mark.parametrize('agent,output', [('claude', '{"type":"result","subtype":"success","is_error":false,"result":"HERDR_PREFLIGHT_OK"}'), ('codex', '{"type":"item.completed","item":{"type":"agent_message","text":"HERDR_PREFLIGHT_OK"}}'), ('pi', 'HERDR_PREFLIGHT_OK\n')])
def test_supported_response_evidence(agent, output):
    with patch.object(dp, 'choose_smoke_command', return_value=(['fake'], 'test')), patch.object(dp, 'run', return_value={'returncode':0,'stdout':output,'stderr':''}):
        assert dp.smoke_probe(agent, 'fake', '.')['status'] == 'READY'

@pytest.mark.parametrize('message,status', [('401 Unauthorized','AUTH_REQUIRED'),('quota exhausted','TOKEN_EXHAUSTED'),('model_not_found','PROVIDER_ERROR')])
def test_clear_failure_wins_over_marker(message,status):
    with patch.object(dp, 'choose_smoke_command', return_value=(['fake'], 'test')), patch.object(dp, 'run', return_value={'returncode':0,'stdout':'HERDR_PREFLIGHT_OK','stderr':message}), patch.object(dp.time, 'sleep'):
        assert dp.smoke_probe('codex','fake','.')['status'] == status


def test_fingerprint_changes_for_binary_config_and_mode(tmp_path):
    binary = tmp_path/'cli'; binary.write_text('v1')
    config = tmp_path/'config.toml'; config.write_text('model="one"\napi_key="fake-secret"')
    kwargs = dict(agent='codex',binary=str(binary),cwd=str(tmp_path),config_paths=[config])
    one = dp.preflight_identity(**kwargs)
    assert 'fake-secret' not in json.dumps(one)
    config.write_text('model="two"\napi_key="fake-secret"')
    two = dp.preflight_identity(**kwargs)
    assert one['fingerprint'] != two['fingerprint']
    binary.write_text('v2')
    assert two['fingerprint'] != dp.preflight_identity(**kwargs)['fingerprint']
    assert dp.preflight_identity(**kwargs,launch_mode='interactive')['fingerprint'] != dp.preflight_identity(**kwargs)['fingerprint']
    record={'preflight_checked_at':100,'preflight_identity':one}
    assert ar.preflight_snapshot_fresh(record,now=101,current_identity=one)
    assert not ar.preflight_snapshot_fresh(record,now=101,current_identity=two)
    assert not ar.preflight_snapshot_fresh(record,now=10000,current_identity=one)

@pytest.mark.parametrize('runtime,text,status', [({'agent_session':{'value':'s'},'agent_status':'idle'},'Do you trust this folder?','TRUST_REQUIRED'), ({'agent_session':'other','agent_status':'idle'},'','IDENTITY_MISMATCH'), ({'agent_session':'s','agent_status':'starting'},'','UNKNOWN'), ({'agent_session':'s','agent_status':'idle'},'','READY')])
def test_startup_requires_native_identity_and_interactive_ready(runtime,text,status):
    result=aa.startup_readiness('codex',{'agent_session_id':'s'},runtime,text)
    assert result['status']==status
    assert result['interactive_ready'] is (status=='READY')


def test_type_or_pane_alone_cannot_prove_startup_identity():
    assert aa.startup_readiness('codex',{}, {'agent_status':'idle'},'')['status']=='UNKNOWN'


def test_real_safe_cli_transport_and_inspect_facts(tmp_path, monkeypatch):
    binary = tmp_path / "codex"
    binary.write_text("#!/usr/bin/env python3\nimport sys\nif '--help' in sys.argv: print('exec noninteractive')\nelif '--version' in sys.argv: print('fake-cli 1')\nelse: print('HERDR_PREFLIGHT_OK')\n")
    binary.chmod(0o755)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(dp,'AUTH_HINTS',{})
    with patch.object(dp,'resolve_binary',return_value=str(binary)), patch.object(dp,'project_pool',return_value={'allowed_agents':['codex']}):
        row=dp.inspect({'project_id':'p','project_root':str(tmp_path)},deep=True)[0]
    assert row['binary_present'] is True
    assert row['request_verified'] is True
    assert row['interactive_ready'] is None
    assert row['preflight_identity']['verifiable'] is True


def test_warning_with_real_plain_response_keeps_legacy_support():
    assert aa.smoke_response_verified('qodercli', 'Skill conflict: duplicate skill warning\nHERDR_PREFLIGHT_OK\n')
    assert not aa.smoke_response_verified('codex', 'Reply with exactly HERDR_PREFLIGHT_OK and nothing else.\nHERDR_PREFLIGHT_OK')


def test_smoke_response_verified_with_ansi_and_trailing_newlines():
    assert aa.smoke_response_verified('qodercli', '\x1b[32mHERDR_PREFLIGHT_OK\x1b[0m\n\n')
    assert aa.smoke_response_verified('kimi', '\x1b[1mHERDR_PREFLIGHT_OK\x1b[0m\n')


def test_smoke_response_verified_with_banner_and_info_lines():
    out = '[INFO] Plugin loaded\nTip: session auto-saved\nHERDR_PREFLIGHT_OK\n'
    assert aa.smoke_response_verified('qodercli', out)
    out2 = 'Warning: model deprecation notice\nHERDR_PREFLIGHT_OK\nDone in 0.3s\n'
    assert aa.smoke_response_verified('kimi', out2)
    out3 = '2026-10-02 19:08:10.123 [INFO] connected\nHERDR_PREFLIGHT_OK\nFinished in 0.4s\nCost: $0.0001\n'
    assert aa.smoke_response_verified('qodercli', out3)


def test_smoke_response_verified_with_markdown_and_period():
    assert aa.smoke_response_verified('qodercli', '`HERDR_PREFLIGHT_OK`')
    assert aa.smoke_response_verified('kimi', 'HERDR_PREFLIGHT_OK.')
    assert aa.smoke_response_verified('kimi', '"HERDR_PREFLIGHT_OK".')
    assert aa.smoke_response_verified('kimi', '“HERDR_PREFLIGHT_OK”。')
    assert aa.smoke_response_verified('agy', '**HERDR_PREFLIGHT_OK**')


def test_smoke_response_verified_structured_assistant_envelope():
    assert aa.smoke_response_verified('qodercli', '{"type":"assistant","content":"HERDR_PREFLIGHT_OK"}')
    assert aa.smoke_response_verified('kimi', '{"role":"assistant","content":"HERDR_PREFLIGHT_OK"}')


def test_per_agent_identity_map_drift_is_stale(tmp_path):
    binary=tmp_path/'codex'; binary.write_text('v1')
    identity=dp.preflight_identity('codex',str(binary),str(tmp_path),config_paths=[])
    record={'preflight_checked_at':100,'preflight_identities':{'codex':identity}}
    assert ar.preflight_snapshot_fresh(record,now=101,current_identity={'codex':identity})
    changed=dict(identity,fingerprint='different')
    assert not ar.preflight_snapshot_fresh(record,now=101,current_identity={'codex':changed})
    assert not ar.preflight_snapshot_fresh(record,now=101,current_identity={})


def test_probe_evidence_is_redacted_before_truncation():
    secret='sk-test-fake-credential-123456'
    output='401 Unauthorized api_key='+secret
    with patch.object(dp,'choose_smoke_command',return_value=(['fake'],'test')), patch.object(dp,'run',return_value={'returncode':1,'stdout':'','stderr':output}):
        response=dp.smoke_probe('codex','fake','.')
    assert secret not in response['output']
    assert response['status']=='AUTH_REQUIRED'


def test_structured_user_echo_or_error_never_verifies():
    assert not aa.smoke_response_verified('codex', '{"type":"item.completed","item":{"type":"user_message","text":"HERDR_PREFLIGHT_OK"}}')
    assert not aa.smoke_response_verified('claude', '{"type":"result","subtype":"success","is_error":true,"result":"HERDR_PREFLIGHT_OK"}')


def test_native_identity_precedes_unowned_transcript_classification():
    response=aa.startup_readiness('codex', {'agent_session_id':'owned'}, {'agent_session':'foreign','agent_status':'idle'}, '401 Unauthorized')
    assert response['status']=='IDENTITY_MISMATCH'
    assert aa.startup_readiness('codex', {'agent_name':'instance'}, {'name':'instance','agent_status':'idle'}, None)['status']=='UNKNOWN'
    assert aa.startup_readiness('codex', {'agent_name':'instance'}, {'name':'instance','agent_status':'idle'}, '')['status']=='READY'


def test_installed_large_agent_binary_can_be_fingerprinted(tmp_path):
    from herdr.deep_preflight import preflight_identity
    binary=tmp_path/'large-agent'
    with binary.open('wb') as stream:
        stream.seek(70*1024*1024);stream.write(b'end')
    result=preflight_identity('opencode',binary,tmp_path,config_paths=[])
    assert result['verifiable'] is True
    assert len(result['binary']['sha256'])==64


def test_preflight_concurrency_bounded(monkeypatch, tmp_path):
    created_workers = []
    real_executor = dp.ThreadPoolExecutor

    def fake_executor(*args, **kwargs):
        workers = kwargs.get("max_workers")
        if workers is None and args:
            workers = args[0]
        created_workers.append(workers)
        return real_executor(*args, **kwargs)

    monkeypatch.setattr(dp, "ThreadPoolExecutor", fake_executor)
    monkeypatch.setenv("HERDR_PREFLIGHT_CONCURRENCY", "3")
    with patch.object(dp, "resolve_binary", return_value=None), \
         patch.object(dp, "project_pool", return_value={"allowed_agents": ["a", "b", "c", "d", "e", "f", "g", "h"]}):
        dp.inspect({"project_id": "p", "project_root": str(tmp_path)}, deep=False)

    assert created_workers == [3]
