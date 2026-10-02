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
