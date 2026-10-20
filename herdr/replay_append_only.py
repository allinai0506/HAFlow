"""Fail-closed three-way export of append-only document resolutions."""
import difflib
import hashlib
import json
import re
import subprocess
from pathlib import Path

DOCUMENTS = ('wiki/log.md', 'docs/lessons/lessons-learned.md', 'wiki/task-lifecycle.md')
_HEADING = re.compile(rb'^## (\d+)\.', re.MULTILINE)


def merge_append_only(base, main, incoming, *, numbered=False):
    """Keep main byte-for-byte; only incoming appended headings may change."""
    if not main.startswith(base) or not incoming.startswith(base):
        raise ValueError('append-only replay refuses deletions or historical edits')
    if incoming != base and any(data and not data.endswith(b'\n') for data in (base, main, incoming)):
        raise ValueError('append-only replay requires complete EOF lines')
    suffix = incoming[len(base):]
    if main[len(base):].startswith(suffix):
        return main, {}
    mapping = {}
    if numbered and suffix:
        existing = [int(match[1]) for match in _HEADING.finditer(main)]
        added = [int(match[1]) for match in _HEADING.finditer(suffix)]
        if len(added) != len(set(added)):
            raise ValueError('duplicate new lesson numbers within incoming history')
        cursor = max(existing, default=0)
        for number in added:
            assigned = max(number, cursor + 1)
            if assigned != number:
                mapping[number] = assigned
            cursor = assigned
        suffix = _HEADING.sub(lambda match: b'## ' + str(mapping.get(int(match[1]), int(match[1]))).encode() + b'.', suffix)
    return main + suffix, mapping


def export_resolutions(repo, base, onto, candidate, output_dir):
    """Export document files and an onto-relative patch; never mutate Git state."""
    repo, output_dir = Path(repo).resolve(), Path(output_dir).resolve()
    if output_dir.exists():
        raise ValueError('output directory must not exist')
    if output_dir == repo or repo in output_dir.parents:
        raise ValueError('output directory must be outside the source repository')
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True,
                              capture_output=True, timeout=30).stdout
    revisions = [git('rev-parse', '--verify', value + '^{commit}').decode().strip()
                 for value in (base, onto, candidate)]
    base, onto, candidate = revisions
    for revision in (onto, candidate):
        git('merge-base', '--is-ancestor', base, revision)
    prepared, patches, receipts = {}, [], []
    def content(revision, name):
        # Missing documents are allowed only when absent from the explicit tree.
        if not git('ls-tree', revision, '--', name):
            return b''
        data = git('show', revision + ':' + name)
        if len(data) > 8 * 1024 * 1024 or b'\x00' in data:
            raise ValueError('document exceeds text/size budget: ' + name)
        return data
    for name in DOCUMENTS:
        old, main, incoming = (content(revision, name) for revision in revisions)
        main_exists = bool(git('ls-tree', onto, '--', name))
        if old == incoming:
            continue
        try:
            merged, mapping = merge_append_only(old, main, incoming,
                numbered=name == 'docs/lessons/lessons-learned.md')
        except ValueError as exc:
            raise ValueError(name + ': ' + str(exc)) from exc
        if merged == main:
            continue
        prepared[name] = merged
        # UTF-8 Markdown; newline-preserving diff is applied with ordinary git apply.
        before, after = main.decode('utf-8').splitlines(keepends=True), merged.decode('utf-8').splitlines(keepends=True)
        patches.append(''.join(difflib.unified_diff(before, after, fromfile='a/' + name if main_exists else '/dev/null', tofile='b/' + name)))
        receipts.append({'path': name, 'main_prefix_preserved': merged.startswith(main),
                         'main_sha256': hashlib.sha256(main).hexdigest(),
                         'merged_sha256': hashlib.sha256(merged).hexdigest(), 'renumbered': mapping})
    # All inputs pass before any export is created.
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, data in prepared.items():
        path = output_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (output_dir / 'append-only.patch').write_text(''.join(patches), encoding='utf-8')
    manifest = {'base': base, 'onto': onto, 'candidate': candidate, 'files': receipts,
                'scope': 'document resolutions only; source branch and index unchanged'}
    (output_dir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest
