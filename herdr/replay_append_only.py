"""Fail-closed three-way export of append-only document resolutions."""
import difflib
import hashlib
import json
import re
import subprocess
from pathlib import Path

DOCUMENTS = ('wiki/log.md', 'docs/lessons/lessons-learned.md', 'wiki/task-lifecycle.md')
_HEADING_LINE = re.compile(rb'^## (\d+)\.')
_FENCE_LINE = re.compile(rb'^ {0,3}(`{3,}|~{3,})')


def _contains_append_block(history, block):
    """Match an exact block only at complete-line append boundaries."""
    offset = 0
    while block:
        position = history.find(block, offset)
        if position < 0:
            return False
        end = position + len(block)
        starts_on_boundary = position == 0 or history[position - 1:position] == b'\n'
        ends_on_boundary = end == len(history) or history[end:end + 1] == b'\n'
        if starts_on_boundary and ends_on_boundary:
            return True
        offset = position + 1
    return True


def _numbered_entries(document):
    """Return real lesson headings, excluding heading-shaped fenced examples."""
    entries = []
    offset = 0
    fence = None
    for line in document.splitlines(keepends=True):
        fence_match = _FENCE_LINE.match(line)
        if fence is None and fence_match:
            marker = fence_match[1]
            fence = (marker[:1], len(marker))
        elif fence is not None and fence_match:
            marker = fence_match[1]
            remainder = line[fence_match.end():].strip(b'\r\n')
            if (marker[:1] == fence[0] and len(marker) >= fence[1]
                    and not remainder.strip(b' \t')):
                fence = None
        elif fence is None:
            heading = _HEADING_LINE.match(line)
            if heading:
                entries.append((int(heading[1]), offset, offset + heading.start(1),
                                offset + heading.end(1)))
        offset += len(line)
    return entries


def _entry_block(document, entries, index):
    start = entries[index][1]
    end = entries[index + 1][1] if index + 1 < len(entries) else len(document)
    return document[start:end]


def _entry_identity(document, entries, index):
    block = _entry_block(document, entries, index).rstrip(b'\r\n')
    return _HEADING_LINE.sub(b'## N.', block, count=1)


def _unmatched_entry_tail(candidate_block, main_block):
    candidate_tail = candidate_block[len(candidate_block.rstrip(b'\r\n')):]
    main_tail = main_block[len(main_block.rstrip(b'\r\n')):]
    if main_tail and candidate_tail.endswith(main_tail):
        return candidate_tail[:-len(main_tail)]
    return candidate_tail if candidate_tail != main_tail else b''


def _applied_prefix(main, candidate, candidate_entries):
    """Find the longest applied candidate prefix and its unmatched separators."""
    main_entries = _numbered_entries(main)
    main_identities = [_entry_identity(main, main_entries, index)
                       for index in range(len(main_entries))]
    candidate_identities = [_entry_identity(candidate, candidate_entries, index)
                            for index in range(len(candidate_entries))]
    best = (0, b'')
    for start in range(len(main_identities)):
        matched = 0
        pending = []
        for main_index in range(start, len(main_identities)):
            identity = main_identities[main_index]
            if (matched < len(candidate_identities)
                    and candidate_identities[matched] == identity):
                pending.append(_unmatched_entry_tail(
                    _entry_block(candidate, candidate_entries, matched),
                    _entry_block(main, main_entries, main_index),
                ))
                matched += 1
        if matched > best[0]:
            best = (matched, b''.join(pending))
    return best


def _renumber_entries(document, entries, start_number):
    mapping = {}
    replacements = []
    cursor = start_number
    for number, _, number_start, number_end in entries:
        assigned = max(number, cursor + 1)
        if assigned != number:
            mapping[number] = assigned
            replacements.append((number_start, number_end, str(assigned).encode()))
        cursor = assigned
    for start, end, replacement in reversed(replacements):
        document = document[:start] + replacement + document[end:]
    return document, mapping


def merge_append_only(base, main, incoming, *, numbered=False):
    """Keep main byte-for-byte; only incoming appended headings may change."""
    if not main.startswith(base) or not incoming.startswith(base):
        raise ValueError('append-only replay refuses deletions or historical edits')
    if incoming != base and any(data and not data.endswith(b'\n') for data in (base, main, incoming)):
        raise ValueError('append-only replay requires complete EOF lines')
    suffix = incoming[len(base):]
    main_suffix = main[len(base):]
    already_applied = _contains_append_block(main_suffix, suffix)
    if already_applied:
        return main, {}
    mapping = {}
    if numbered and suffix:
        candidate_entries = _numbered_entries(suffix)
        added = [entry[0] for entry in candidate_entries]
        if len(added) != len(set(added)):
            raise ValueError('duplicate new lesson numbers within incoming history')
        prefix = suffix[:candidate_entries[0][1]] if candidate_entries else b''
        prefix_pending = (prefix if prefix.strip()
                          and not _contains_append_block(main_suffix, prefix) else b'')
        applied, unmatched_separators = _applied_prefix(
            main_suffix, suffix, candidate_entries)
        if applied == len(candidate_entries) and candidate_entries:
            return main + prefix_pending + unmatched_separators, {}
        if applied or prefix_pending != prefix:
            separator = prefix if not prefix.strip() else prefix_pending
            suffix = (separator + unmatched_separators
                      + suffix[candidate_entries[applied][1]:])
            candidate_entries = _numbered_entries(suffix)
        existing = [entry[0] for entry in _numbered_entries(main)]
        suffix, mapping = _renumber_entries(suffix, candidate_entries,
                                            max(existing, default=0))
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
