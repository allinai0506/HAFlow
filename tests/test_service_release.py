import importlib
import plistlib
from pathlib import Path
import pytest


def module():
    assert importlib.util.find_spec('herdr.service_release'), 'atomic release publisher missing'
    return importlib.import_module('herdr.service_release')


def make(path, tail=()):
    path.write_bytes(plistlib.dumps({'ProgramArguments':['/usr/bin/python3','/old/releases/abc/services/herdr-controller.py',*tail]}))


def test_publish_idempotent_and_root(tmp_path):
    m=module(); p=tmp_path/'com.user.herdr-controller.plist'; make(p)
    root=tmp_path/'releases'/'new'; (root/'services').mkdir(parents=True); (root/'services/herdr-controller.py').touch()
    m.publish_service_plists([p],root); first=p.read_bytes(); m.publish_service_plists([p],root)
    assert p.read_bytes()==first
    d=plistlib.loads(first)
    assert d['ProgramArguments']==['/usr/bin/python3',str(root/'services/herdr-controller.py')]
    assert d['EnvironmentVariables']['HERDR_ROOT']==str(root)
    assert p.with_suffix('.plist.bak').exists()


def test_unknown_arguments_reject_all_before_publish(tmp_path):
    m=module(); a=tmp_path/'com.user.herdr-controller.plist'; b=tmp_path/'com.user.herdr-sentinel.plist'; make(a); make(b,['--user-setting'])
    before=a.read_bytes()
    with pytest.raises(ValueError,match='unknown'):
        m.publish_service_plists([a,b],tmp_path/'release')
    assert a.read_bytes()==before


def test_runtime_unknown_is_not_configuration_success(tmp_path):
    m=module(); p=tmp_path/'com.user.herdr-controller.plist'; make(p)
    r=m.service_fingerprint(p)
    assert r['running_sha']=='unknown' and r['running_import_root']=='unknown'


def test_workspace_layout_preserved(tmp_path):
    m=module(); root=tmp_path/'workspace'; (root/'services').mkdir(parents=True); script=root/'services/herdr-notifier.py'; script.touch()
    p=tmp_path/'com.user.herdr-notifier.plist'; p.write_bytes(plistlib.dumps({'ProgramArguments':['python3',str(script)]}))
    m.publish_service_plists([p],tmp_path/'releases/new')
    d=plistlib.loads(p.read_bytes()); assert d['ProgramArguments'][1]==str(script); assert d['EnvironmentVariables']['HERDR_ROOT']==str(root)


def test_invalid_second_script_preserves_first(tmp_path):
    m=module(); a=tmp_path/'com.user.herdr-controller.plist'; b=tmp_path/'com.user.herdr-sentinel.plist'; make(a)
    b.write_bytes(plistlib.dumps({'ProgramArguments':['python3','/unknown.py']})); before=a.read_bytes()
    (tmp_path/'release/services').mkdir(parents=True); (tmp_path/'release/services/herdr-controller.py').touch()
    with pytest.raises(ValueError,match='unknown'):m.publish_service_plists([a,b],tmp_path/'release')
    assert a.read_bytes()==before


def test_installer_no_restart_temp_home(tmp_path):
    import os
    import subprocess
    repo=Path(__file__).resolve().parents[1]
    sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
    root=tmp_path/'.herdr-controller/releases'/sha
    agents=tmp_path/'Library/LaunchAgents'; agents.mkdir(parents=True)
    p=agents/'com.user.herdr-controller.plist'; make(p)
    env=dict(os.environ,HOME=str(tmp_path))
    result=subprocess.run(['bash',str(repo/'scripts/install-herdr-console.sh'),'--sha',sha,'--no-restart'],cwd=repo,env=env,text=True,capture_output=True,timeout=30)
    assert result.returncode==0,result.stderr
    d=plistlib.loads(p.read_bytes())
    assert d['ProgramArguments']==['/usr/bin/python3',str(root/'services/herdr-controller.py')]
    assert d['EnvironmentVariables']['HERDR_ROOT']==str(root)
    assert 'unknown' in result.stdout
    cache=root/'services/__pycache__';cache.mkdir(parents=True)
    (cache/'herdr-controller.cpython-313.pyc').write_bytes(b'unverified cached instructions')
    first=p.read_bytes()
    repeated=subprocess.run(['bash',str(repo/'scripts/install-herdr-console.sh'),'--sha',sha,'--no-restart'],cwd=repo,env=env,text=True,capture_output=True,timeout=30)
    assert repeated.returncode==0,repeated.stderr
    assert not cache.exists() and p.read_bytes()==first
    assert d['EnvironmentVariables']['PYTHONDONTWRITEBYTECODE']=='1'


def test_runtime_directory_name_does_not_attest_release(tmp_path):
    m=module();root=tmp_path/'releases'/('a'*40);root.mkdir(parents=True)
    assert m.runtime_fingerprint(root)['running_sha']=='unknown'


def test_installer_rejects_mutated_existing_snapshot_before_plist(tmp_path):
    import os,subprocess
    repo=Path(__file__).resolve().parents[1];sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
    root=tmp_path/'.herdr-controller/releases'/sha;(root/'console').mkdir(parents=True)
    (root/'console/herdr_factory_console.py').write_text('# locally mutated snapshot')
    (root/'services').mkdir();(root/'services/herdr-controller.py').touch()
    agents=tmp_path/'Library/LaunchAgents';agents.mkdir(parents=True);p=agents/'com.user.herdr-controller.plist';make(p);original=p.read_bytes()
    result=subprocess.run(['bash',str(repo/'scripts/install-herdr-console.sh'),'--sha',sha,'--no-restart'],cwd=repo,env=dict(os.environ,HOME=str(tmp_path)),capture_output=True,text=True,timeout=30)
    assert result.returncode!=0,'mutated SHA-named snapshot was accepted'
    assert p.read_bytes()==original


def test_verified_snapshot_manifest_detects_subsequent_mutation(tmp_path):
    m=module();expected=tmp_path/'archive';expected.mkdir();(expected/'module.py').write_text('original')
    root=tmp_path/'releases'/('b'*40);root.mkdir(parents=True);(root/'module.py').write_text('original')
    m.verify_snapshot(expected,root,'b'*40)
    assert m.runtime_fingerprint(root)['running_sha']=='b'*40
    (root/'module.py').write_text('changed')
    assert m.runtime_fingerprint(root)['running_sha']=='unknown'


def test_timestamp_valid_bytecode_cannot_attest_snapshot(tmp_path):
    import importlib.util,marshal,struct
    m=module();expected=tmp_path/'archive';expected.mkdir();(expected/'module.py').write_text('VALUE=1\n')
    root=tmp_path/'releases'/('c'*40);root.mkdir(parents=True);source=root/'module.py';source.write_text('VALUE=1\n')
    m.verify_snapshot(expected,root,'c'*40)
    bytecode=Path(importlib.util.cache_from_source(str(source)));bytecode.parent.mkdir()
    stat=source.stat();bytecode.write_bytes(importlib.util.MAGIC_NUMBER+struct.pack('<III',0,int(stat.st_mtime),stat.st_size)+marshal.dumps(compile('VALUE=2\n',str(source),'exec')))
    spec=importlib.util.spec_from_file_location('mutated_cached_module',source);loaded=importlib.util.module_from_spec(spec);spec.loader.exec_module(loaded)
    assert loaded.VALUE==2,'fixture must execute stale timestamp-valid bytecode'
    assert m.runtime_fingerprint(root)['running_sha']=='unknown'
    m.verify_snapshot(expected,root,'c'*40)
    assert not bytecode.exists() and not bytecode.parent.exists()
    assert m.runtime_fingerprint(root)['running_sha']=='c'*40


def test_plist_disables_release_bytecode_writes(tmp_path):
    m=module();p=tmp_path/'com.user.herdr-controller.plist';make(p)
    root=tmp_path/'releases/new';(root/'services').mkdir(parents=True);(root/'services/herdr-controller.py').touch()
    m.publish_service_plists([p],root)
    assert plistlib.loads(p.read_bytes())['EnvironmentVariables']['PYTHONDONTWRITEBYTECODE']=='1'


def test_bad_source_preserves_unverified_cache(tmp_path):
    m=module();expected=tmp_path/'archive';expected.mkdir();(expected/'module.py').write_text('original')
    root=tmp_path/'releases/new';root.mkdir(parents=True);(root/'module.py').write_text('mutated')
    cache=root/'__pycache__';cache.mkdir();bytecode=cache/'module.cpython-313.pyc';bytecode.write_bytes(b'cache')
    with pytest.raises(ValueError,match='differs'):
        m.verify_snapshot(expected,root,'new')
    assert bytecode.read_bytes()==b'cache'


def test_snapshot_executable_bit_matches_archive_contract(tmp_path):
    m=module();expected=tmp_path/'archive';expected.mkdir();script=expected/'command';script.write_text('#!/bin/sh\nexit 0\n');script.chmod(0o755)
    root=tmp_path/'releases'/('d'*40);root.mkdir(parents=True);release_script=root/'command';release_script.write_bytes(script.read_bytes());release_script.chmod(0o644)
    with pytest.raises(ValueError,match='differs'):
        m.verify_snapshot(expected,root,'d'*40)
    assert release_script.stat().st_mode & 0o111==0,'invalid existing snapshot must remain unchanged'
    release_script.chmod(0o755);m.verify_snapshot(expected,root,'d'*40)
    assert m.runtime_fingerprint(root)['running_sha']=='d'*40
    release_script.chmod(0o644)
    assert m.runtime_fingerprint(root)['running_sha']=='unknown'


# --- 部署纳管：带参数服务 + 未纳管漂移 ------------------------------------------------


UNPINNED_SHA = 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef'

HUB_ARGS = ['/opt/homebrew/bin/python3', '/old/releases/abc/bin/herdr-experience-hub',
            'start', '--host', '127.0.0.1', '--port', '8780',
            '--db-path', '/host-state/experience_hub.db']
HUB_WORKDIR = '/old/releases/abc'


def hub_plist(path):
    path.write_bytes(plistlib.dumps({
        'ProgramArguments': list(HUB_ARGS),
        'EnvironmentVariables': {'HERDR_ROOT': HUB_WORKDIR, 'PYTHONUNBUFFERED': '1'},
        'WorkingDirectory': HUB_WORKDIR,
    }))


def hub_release(tmp_path):
    root = tmp_path/'releases'/('n'*40)
    (root/'bin').mkdir(parents=True)
    script = root/'bin/herdr-experience-hub'
    script.write_text('#!/usr/bin/env python3\n')
    script.chmod(0o755)
    return root


def test_declared_argv_tail_migrates_only_the_script_path(tmp_path):
    """Argument-bearing services are adoptable: host state stays verbatim, the release path moves."""
    m=module(); root=hub_release(tmp_path); p=tmp_path/'com.user.herdr-experience-hub.plist'; hub_plist(p)
    m.publish_service_plists([p], root)
    args = plistlib.loads(p.read_bytes())['ProgramArguments']
    assert args[1] == str(root/'bin/herdr-experience-hub')
    assert args[2:] == HUB_ARGS[2:], 'declared tail must survive token-for-token'
    first = p.read_bytes()
    m.publish_service_plists([p], root)
    assert p.read_bytes() == first, 're-running the publish must not re-derive the tail'


def test_release_paths_never_split_across_generations(tmp_path):
    """ProgramArguments, HERDR_ROOT and WorkingDirectory must agree on one release root."""
    m=module(); root=hub_release(tmp_path); p=tmp_path/'com.user.herdr-experience-hub.plist'; hub_plist(p)
    m.publish_service_plists([p], root)
    data = plistlib.loads(p.read_bytes())
    assert data['ProgramArguments'][1].startswith(str(root))
    assert data['EnvironmentVariables']['HERDR_ROOT'] == str(root)
    assert data['WorkingDirectory'] == str(root), 'pinned cwd would run new code in the old release tree'
    assert data['EnvironmentVariables']['PYTHONUNBUFFERED'] == '1', 'unrelated environment must survive'


def test_undeclared_flag_still_rejected(tmp_path):
    m=module(); root=hub_release(tmp_path); p=tmp_path/'com.user.herdr-experience-hub.plist'; hub_plist(p)
    args = list(HUB_ARGS) + ['--allow-insecure-http']
    p.write_bytes(plistlib.dumps({'ProgramArguments': args}))
    before = p.read_bytes()
    with pytest.raises(ValueError, match='unknown'):
        m.publish_service_plists([p], root)
    assert p.read_bytes() == before


def test_declared_flag_without_value_rejected(tmp_path):
    m=module(); root=hub_release(tmp_path); p=tmp_path/'com.user.herdr-experience-hub.plist'; hub_plist(p)
    p.write_bytes(plistlib.dumps({'ProgramArguments': HUB_ARGS[:-1]}))
    with pytest.raises(ValueError, match='value'):
        m.publish_service_plists([p], root)


def test_tail_values_are_not_pinned_so_operators_can_retarget(tmp_path):
    """A host-chosen port/db path is still a contract match: only the flag vocabulary is checked."""
    m=module(); root=hub_release(tmp_path); p=tmp_path/'com.user.herdr-experience-hub.plist'
    p.write_bytes(plistlib.dumps({'ProgramArguments': [
        HUB_ARGS[0], HUB_ARGS[1], 'start', '--host', '127.0.0.1', '--port', '9999',
        '--db-path', '/elsewhere/hub.db']}))
    m.publish_service_plists([p], root)
    args = plistlib.loads(p.read_bytes())['ProgramArguments']
    assert args[2:] == ['start','--host','127.0.0.1','--port','9999','--db-path','/elsewhere/hub.db']


def test_workspace_service_keeps_its_own_root_and_cwd(tmp_path):
    m=module(); ws=tmp_path/'workspace'; (ws/'bin').mkdir(parents=True)
    script=ws/'bin/herdr-experience-hub'; script.touch()
    p=tmp_path/'com.user.herdr-experience-hub.plist'
    p.write_bytes(plistlib.dumps({'ProgramArguments':[HUB_ARGS[0], str(script), 'start'],
                                  'WorkingDirectory': str(ws)}))
    m.publish_service_plists([p], tmp_path/'releases'/('n'*40))
    data=plistlib.loads(p.read_bytes())
    assert data['ProgramArguments'][1]==str(script) and data['WorkingDirectory']==str(ws)


def test_absent_working_directory_is_not_invented(tmp_path):
    m=module(); p=tmp_path/'com.user.herdr-controller.plist'; make(p)
    root=tmp_path/'releases'/('n'*40); (root/'services').mkdir(parents=True); (root/'services/herdr-controller.py').touch()
    m.publish_service_plists([p], root)
    assert 'WorkingDirectory' not in plistlib.loads(p.read_bytes())


def test_managed_set_is_derived_from_one_authority(tmp_path):
    """The bash SERVICES array must not be a second copy of the whitelist."""
    import subprocess
    m=module(); repo=Path(__file__).resolve().parents[1]
    labels=subprocess.check_output(['python3','-m','herdr.service_release','--labels'],
                                   cwd=repo,text=True).split()
    assert sorted(labels) == sorted(m.SCRIPTS)
    assert 'com.user.herdr-experience-hub' in labels


def test_unmanaged_release_pinned_plist_is_reported(tmp_path):
    m=module(); agents=tmp_path/'Library/LaunchAgents'; agents.mkdir(parents=True)
    (agents/'com.user.herdr-worker.plist').write_bytes(plistlib.dumps({
        'ProgramArguments':['python3','/old/releases/'+UNPINNED_SHA+'/services/herdr-worker.py']}))
    (agents/'com.user.herdr-sentinel.plist').write_bytes(plistlib.dumps(
        {'ProgramArguments':['/usr/bin/python3','/old/releases/xyz/services/herdr-sentinel.py']}))
    (agents/'com.other.app.plist').write_bytes(plistlib.dumps(
        {'ProgramArguments':['/bin/sh','/releases/not-herdr']}))
    found = {entry['service']: entry['configured_sha'] for entry in m.unmanaged_release_plists(sorted(agents.glob('*.plist')))}
    assert found == {'com.user.herdr-worker': UNPINNED_SHA}, found


def test_drift_discovery_uses_the_python_namespace(tmp_path):
    """installer 只交出目录：命名空间判定不在 shell 里复制第二份，也不误报备份文件。"""
    m=module(); agents=tmp_path/'Library/LaunchAgents'; agents.mkdir(parents=True)
    pinned = {'ProgramArguments':['python3','/old/releases/'+UNPINNED_SHA+'/services/herdr-worker.py']}
    (agents/'com.user.herdr-worker.plist').write_bytes(plistlib.dumps(pinned))
    (agents/'com.user.herdr-worker.plist.bak-20261016').write_bytes(plistlib.dumps(pinned))
    (agents/'com.other.app.plist').write_bytes(plistlib.dumps(pinned))
    found = [e['service'] for e in m.unmanaged_plists_in(agents)]
    assert found == ['com.user.herdr-worker'], 'backups 与非 herdr plist 不得算作漂移'


def test_workspace_pinned_plist_is_not_drift(tmp_path):
    m=module(); agents=tmp_path/'Library/LaunchAgents'; agents.mkdir(parents=True)
    (agents/'com.user.herdr-worker.plist').write_bytes(plistlib.dumps(
        {'ProgramArguments':['python3', str(tmp_path/'workspace/services/herdr-worker.py')]}))
    assert m.unmanaged_release_plists(sorted(agents.glob('*.plist'))) == []


def test_installer_refuses_unmanaged_release_plist_before_touching_anything(tmp_path):
    import os, subprocess
    repo=Path(__file__).resolve().parents[1]
    sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
    agents=tmp_path/'Library/LaunchAgents'; agents.mkdir(parents=True)
    controller=agents/'com.user.herdr-controller.plist'; make(controller)
    worker=agents/'com.user.herdr-worker.plist'; worker.write_bytes(plistlib.dumps({
        'ProgramArguments':['python3','/old/releases/'+UNPINNED_SHA+'/services/herdr-worker.py']}))
    controller_before, worker_before = controller.read_bytes(), worker.read_bytes()
    result=subprocess.run(['bash',str(repo/'scripts/install-herdr-console.sh'),'--sha',sha,'--no-restart'],
                          cwd=repo,env=dict(os.environ,HOME=str(tmp_path)),capture_output=True,text=True,timeout=45)
    assert result.returncode!=0,'unmanaged pinned service must not deploy silently'
    assert 'com.user.herdr-worker' in result.stdout+result.stderr
    assert controller.read_bytes()==controller_before
    assert worker.read_bytes()==worker_before


def test_installer_deploys_when_drift_is_exempted(tmp_path):
    import os, subprocess
    repo=Path(__file__).resolve().parents[1]
    sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
    agents=tmp_path/'Library/LaunchAgents'; agents.mkdir(parents=True)
    controller=agents/'com.user.herdr-controller.plist'; make(controller)
    worker=agents/'com.user.herdr-worker.plist'; worker.write_bytes(plistlib.dumps({
        'ProgramArguments':['python3','/old/releases/'+UNPINNED_SHA+'/services/herdr-worker.py']}))
    result=subprocess.run(['bash',str(repo/'scripts/install-herdr-console.sh'),'--sha',sha,'--no-restart',
                           '--allow-unmanaged','com.user.herdr-worker'],
                          cwd=repo,env=dict(os.environ,HOME=str(tmp_path)),capture_output=True,text=True,timeout=45)
    assert result.returncode==0, result.stdout+result.stderr
    assert 'com.user.herdr-worker' in result.stdout
    assert plistlib.loads(controller.read_bytes())['EnvironmentVariables']['HERDR_ROOT'].endswith(sha)
    assert plistlib.loads(worker.read_bytes())['ProgramArguments'][1].startswith('/old/releases/'+UNPINNED_SHA)


def test_installer_adopts_argument_bearing_service_end_to_end(tmp_path):
    import os, subprocess
    repo=Path(__file__).resolve().parents[1]
    sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
    agents=tmp_path/'Library/LaunchAgents'; agents.mkdir(parents=True)
    p=agents/'com.user.herdr-experience-hub.plist'; hub_plist(p)
    result=subprocess.run(['bash',str(repo/'scripts/install-herdr-console.sh'),'--sha',sha,'--no-restart'],
                          cwd=repo,env=dict(os.environ,HOME=str(tmp_path)),capture_output=True,text=True,timeout=45)
    assert result.returncode==0, result.stdout+result.stderr
    root=tmp_path/'.herdr-controller/releases'/sha
    data=plistlib.loads(p.read_bytes())
    assert data['ProgramArguments'][1]==str(root/'bin/herdr-experience-hub')
    assert data['WorkingDirectory']==str(root)
    assert data['ProgramArguments'][2:]==HUB_ARGS[2:]
