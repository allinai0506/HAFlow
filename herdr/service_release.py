"""LaunchAgent configuration publication; runtime evidence is separate."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import tempfile
import shutil

MANIFEST = '.herdr-release-manifest.json'


def _snapshot_files(root):
    entries = {}
    for path in sorted(Path(root).rglob('*')):
        relative = path.relative_to(root)
        if path.name == MANIFEST or '__pycache__' in relative.parts or path.suffix == '.pyc':
            continue
        if path.is_symlink():
            raise ValueError('snapshot symlink content is not attested')
        if path.is_file():
            entries[str(relative)] = {
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'executable_bits': path.stat().st_mode & 0o111,
            }
    return entries


def verify_snapshot(expected_root, release_root, sha):
    """Compare every source file with a fresh git archive before publication."""
    root = Path(release_root)
    if root.is_symlink():
        raise ValueError('snapshot root must not be a symlink')
    expected = _snapshot_files(Path(expected_root))
    if not expected or _snapshot_files(root) != expected:
        raise ValueError('snapshot content differs from requested git archive; preserve it and use a clean release directory')
    # Bytecode can execute different instructions while source hashes match.
    # Remove caches only after verifying all source; unlink cache symlinks
    # without following them outside this verified release directory.
    for cache in sorted(root.rglob('__pycache__'), key=lambda p: len(p.parts), reverse=True):
        if cache.is_symlink():
            cache.unlink()
        elif cache.is_dir():
            shutil.rmtree(cache)
    for cache in root.rglob('*.pyc'):
        if cache.is_file() or cache.is_symlink():
            cache.unlink()
    encoded = json.dumps({'sha': sha, 'files': expected}, sort_keys=True).encode()
    fd, name = tempfile.mkstemp(prefix='.manifest-', dir=root)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(encoded); handle.flush(); os.fsync(handle.fileno())
        os.chmod(name, 0o444)
        os.replace(name, root / MANIFEST)
    finally:
        if os.path.exists(name): os.unlink(name)


def _attested_snapshot_sha(root):
    try:
        manifest = json.loads((root / MANIFEST).read_text())
        candidate = _release_sha(root)
        if (manifest.get('sha') == candidate and len(candidate) == 40
                and all(c in '0123456789abcdef' for c in candidate)
                and not any(root.rglob('__pycache__')) and not any(root.rglob('*.pyc'))
                and manifest.get('files') and _snapshot_files(root) == manifest['files']):
            return candidate
    except (OSError, ValueError, TypeError):
        pass
    return 'unknown'

SCRIPTS = {
    'com.user.herdr-factory-console': ('console/herdr_factory_console.py', ()),
    'com.user.herdr-controller': ('services/herdr-controller.py', ()),
    'com.user.herdr-sentinel': ('services/herdr-sentinel.py', ()),
    'com.user.herdr-notifier': ('services/herdr-notifier.py', ()),
    'com.user.herdr-experience-hub': ('bin/herdr-experience-hub',
                                      ('start', '--host', '--port', '--db-path')),
}
SERVICE_NAMESPACE = 'com.user.herdr-'


def _argument_problem(label, tail, declared):
    """Return why this argv tail is not the service's declared contract, or None.

    Flags are validated by vocabulary, values by nothing: a host may retarget a
    port or db path without the release migration refusing to run.
    """
    index = 0
    while index < len(tail):
        token = tail[index]
        if token.startswith('-'):
            if token not in declared:
                return f'unknown argument {token} for {label}; migrate custom arguments explicitly'
            if index + 1 >= len(tail):
                return f'unknown argument value for {token} in {label}'
            index += 2
            continue
        if token not in declared:
            return f'unknown argument {token} for {label}; migrate custom arguments explicitly'
        index += 1
    return None


def _pinned_sha(data):
    """Release sha a plist is frozen onto, from any field that names a release."""
    values = [a for a in (data.get('ProgramArguments') or []) if isinstance(a, str)]
    values.append((data.get('EnvironmentVariables') or {}).get('HERDR_ROOT'))
    values.append(data.get('WorkingDirectory'))
    for value in values:
        parts = Path(str(value or '')).parts
        if 'releases' in parts:
            following = parts[parts.index('releases') + 1:]
            if following:
                return following[0]
    return None


def unmanaged_release_plists(paths, managed=None, namespace=SERVICE_NAMESPACE):
    """Installed services frozen onto a release snapshot that nothing publishes to.

    These keep running an old generation forever, and no deploy reports it.
    """
    managed = SCRIPTS if managed is None else managed
    drift = []
    for path in map(Path, paths):
        label = path.stem
        if not label.startswith(namespace) or label in managed:
            continue
        try:
            sha = _pinned_sha(plistlib.loads(path.read_bytes()))
        except Exception:
            sha = None
        if sha:
            drift.append({'service': label, 'configured_sha': sha,
                          'reason': 'release-pinned but not published by the installer'})
    return drift


def unmanaged_plists_in(directory, **kwargs):
    return unmanaged_release_plists(
        sorted(Path(directory).glob(SERVICE_NAMESPACE + '*.plist')), **kwargs)


def _atomic(path, data):
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.chmod(name, path.stat().st_mode & 0o777)
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def publish_service_plists(paths, release_root):
    """Validate the entire batch before any replacement. Each file is atomic.

    Supported contract: interpreter + one known script, optionally followed by
    that service's declared argument vocabulary. Only release-scoped fields
    migrate, and they migrate together: a split between script path,
    HERDR_ROOT and working directory would run new code inside an old snapshot.
    """
    prepared = []
    for path in map(Path, paths):
        original = path.read_bytes(); data = plistlib.loads(original)
        spec = SCRIPTS.get(path.stem)
        args = data.get('ProgramArguments')
        if not spec or not isinstance(args, list) or len(args) < 2:
            raise ValueError(f'unknown ProgramArguments in {path}; migrate custom arguments explicitly')
        if not all(isinstance(a, str) and a and '\x00' not in a for a in args):
            raise ValueError(f'unknown service script in {path}')
        relative, declared = spec
        if not args[1].endswith('/' + relative):
            raise ValueError(f'unknown service script in {path}')
        problem = _argument_problem(path.stem, args[2:], declared)
        if problem:
            raise ValueError(problem)
        old_root = Path(args[1]).parents[len(Path(relative).parts)-1]
        root = Path(release_root).resolve() if 'releases' in old_root.parts else old_root
        script = root / relative
        if not script.is_file(): raise ValueError(f'service script missing: {script}')
        data['ProgramArguments'] = [args[0], str(script), *args[2:]]
        working = data.get('WorkingDirectory')
        if isinstance(working, str) and 'releases' in Path(working).parts:
            data['WorkingDirectory'] = str(root)
        env = data.setdefault('EnvironmentVariables', {})
        if not isinstance(env, dict): raise ValueError(f'invalid environment in {path}')
        env['HERDR_ROOT'] = str(root)
        if 'releases' in root.parts:
            env['PYTHONDONTWRITEBYTECODE'] = '1'
        encoded = plistlib.dumps(data, sort_keys=True)
        plistlib.loads(encoded)
        prepared.append((path, original, encoded))
    for path, original, encoded in prepared:
        if original != encoded:
            _atomic(path.with_suffix('.plist.bak'), original) if path.with_suffix('.plist.bak').exists() else _backup(path, original)
            _atomic(path, encoded)
    return [service_fingerprint(path) for path, _, _ in prepared]


def _backup(path, data):
    backup = path.with_suffix('.plist.bak')
    backup.touch(mode=path.stat().st_mode & 0o777)
    _atomic(backup, data)


def _release_sha(root):
    path = Path(root)
    return path.name if path.parent.name == 'releases' else 'unknown'


def runtime_fingerprint(import_root=None, component_paths=()):
    root = Path(import_root or Path(__file__).resolve().parent.parent).resolve()
    return {'running_sha': _attested_snapshot_sha(root), 'running_import_root': str(root),
            'component_versions': {str(p): hashlib.sha256(Path(p).read_bytes()).hexdigest() if Path(p).is_file() else 'unknown' for p in component_paths}}


def service_fingerprint(path, runtime=None):
    data = plistlib.loads(Path(path).read_bytes())
    root = data.get('EnvironmentVariables', {}).get('HERDR_ROOT', 'unknown')
    return {'service': Path(path).stem, 'configured_sha': _release_sha(root),
            'configured_import_root': root, 'configuration_hash': hashlib.sha256(plistlib.dumps(data, sort_keys=True)).hexdigest(),
            'running_sha': (runtime or {}).get('running_sha', 'unknown'),
            'running_import_root': (runtime or {}).get('running_import_root', 'unknown'),
            'component_versions': (runtime or {}).get('component_versions', {})}


if __name__ == '__main__':
    import sys
    if sys.argv[1] == '--verify-snapshot':
        verify_snapshot(sys.argv[2], sys.argv[3], sys.argv[4])
    elif sys.argv[1] == '--labels':
        print(' '.join(SCRIPTS))
    elif sys.argv[1] == '--drift':
        for entry in unmanaged_plists_in(sys.argv[2]):
            print(entry['service'], entry['configured_sha'])
    else:
        print(json.dumps(publish_service_plists(sys.argv[2:], sys.argv[1]), ensure_ascii=False))
