"""Append-only replay must preserve both histories and fail on history edits."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from herdr.replay_append_only import merge_append_only


def test_both_eof_appends_preserve_main_prefix():
    base = b'# log\nold\n'
    main = base + b'\nmain entry\n'
    merged, mapping = merge_append_only(base, main, base + b'\nincoming entry\n')
    assert merged == main + b'\nincoming entry\n'
    assert mapping == {}


def test_colliding_lesson_headings_are_renumbered():
    base = b'## 177. old\nold body\n'
    main = base + b'\n## 178. main\nmain body\n'
    incoming = base + b'\n## 178. incoming\nincoming body\n\n## 179. another\nlast body\n'
    merged, mapping = merge_append_only(base, main, incoming, numbered=True)
    assert merged.startswith(main)
    assert b'## 179. incoming\nincoming body' in merged
    assert b'## 180. another\nlast body' in merged
    assert mapping == {178: 179, 179: 180}


@pytest.mark.parametrize('main,incoming', [(b'changed\n', b'old\nnew\n'), (b'old\nmain\n', b'new\n')])
def test_deletion_or_history_edit_refused(main, incoming):
    with pytest.raises(ValueError, match='append-only'):
        merge_append_only(b'old\n', main, incoming)


def test_real_git_export_patch_applies_and_does_not_change_source(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True, text=True, capture_output=True).stdout.strip()
    git('init', '-b', 'main')
    git('config', 'user.name', 'T')
    git('config', 'user.email', 't@t')
    git('config', 'commit.gpgsign', 'false')
    path = repo / 'docs/lessons/lessons-learned.md'
    path.parent.mkdir(parents=True)
    path.write_text('## 1. base\nbase body\n')
    git('add', '.')
    git('commit', '-m', 'base')
    base = git('rev-parse', 'HEAD')
    path.write_text(path.read_text() + '\n## 2. main\nmain body\n')
    git('commit', '-am', 'main append')
    onto = git('rev-parse', 'HEAD')
    git('checkout', '-b', 'candidate', base)
    path.write_text(path.read_text() + '\n## 2. incoming\nincoming body\n')
    git('commit', '-am', 'incoming append')
    candidate = git('rev-parse', 'HEAD')
    before = git('status', '--porcelain')
    out = tmp_path / 'resolved'
    cli = Path(__file__).resolve().parents[1] / 'scripts/replay-append-only.py'
    result = subprocess.run([sys.executable, str(cli), '--repo', str(repo), '--base', base,
        '--onto', onto, '--candidate', candidate, '--output-dir', str(out)], capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert git('rev-parse', 'HEAD') == candidate
    assert git('status', '--porcelain') == before
    manifest = json.loads((out / 'manifest.json').read_text())
    assert manifest['files'][0]['main_prefix_preserved'] is True
    git('checkout', 'main')
    git('apply', '--check', str(out / 'append-only.patch'))
    git('apply', str(out / 'append-only.patch'))
    assert path.read_text() == '## 1. base\nbase body\n\n## 2. main\nmain body\n\n## 3. incoming\nincoming body\n'


def test_historical_duplicate_numbers_do_not_require_rewriting_main():
    base = b'## 1. old\nfirst\n## 1. historical duplicate\nsecond\n'
    main = base + b'## 2. main\nmain body\n'
    merged, mapping = merge_append_only(base, main, base + b'## 2. incoming\nnew body\n', numbered=True)
    assert merged.startswith(main)
    assert merged.endswith(b'## 3. incoming\nnew body\n')
    assert mapping == {2: 3}


@pytest.mark.parametrize('base,main,incoming,refused', [
    ('old\n', 'old\nmain', 'old\nincoming\n', True),
    ('old\n', 'old\nmain\n', 'old\nincoming', True),
    ('', '', 'incoming\n', False),
    (None, None, 'incoming\n', False),
])
def test_real_git_patch_boundaries(tmp_path, base, main, incoming, refused):
    from herdr.replay_append_only import export_resolutions
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True, text=True, capture_output=True).stdout.strip()
    git('init', '-b', 'main')
    git('config', 'user.name', 'T')
    git('config', 'user.email', 't@t')
    git('config', 'commit.gpgsign', 'false')
    path = repo / 'wiki/log.md'
    path.parent.mkdir()
    def commit(text):
        if text is not None:
            path.write_text(text)
        git('add', '.')
        git('commit', '--allow-empty', '-m', 'fixture')
        return git('rev-parse', 'HEAD')
    ancestor = commit(base)
    onto = commit(main)
    git('checkout', '-b', 'incoming', ancestor)
    candidate = commit(incoming)
    out = tmp_path / 'resolved'
    if refused:
        with pytest.raises(ValueError, match='complete.*line'):
            export_resolutions(repo, ancestor, onto, candidate, out)
        assert not out.exists()
    else:
        export_resolutions(repo, ancestor, onto, candidate, out)
        git('checkout', 'main')
        git('apply', '--check', str(out / 'append-only.patch'))
        git('apply', str(out / 'append-only.patch'))
        assert path.read_text() == incoming


@pytest.mark.parametrize('numbered', [False, True])
def test_identical_append_already_on_main_is_not_duplicated(numbered):
    base = b'# old\n'
    appended = b'\n## 2. shared\nsame body\n'
    result, mapping = merge_append_only(base, base + appended + b'\n## 3. main only\nbody\n',
                                        base + appended, numbered=numbered)
    assert result.count(b'shared') == 1
    assert mapping == {}


def test_identical_numbered_append_between_other_main_appends_is_not_duplicated():
    base = b'## 1. old\nold body\n'
    shared = b'\n## 2. shared\nsame body\n'
    main = base + b'\n## 2. earlier main\nfirst body\n' + shared + b'\n## 4. later main\nlast body\n'

    result, mapping = merge_append_only(base, main, base + shared, numbered=True)

    assert result == main
    assert result.count(b'## 2. shared\n') == 1
    assert mapping == {}


def test_retry_deduplicates_a_block_renumbered_by_the_first_replay():
    base = b'## 1. old\nold body\n'
    main = base + b'\n## 2. main\nmain body\n'
    incoming = base + b'\n## 2. incoming\nincoming body\n'
    first, first_mapping = merge_append_only(base, main, incoming, numbered=True)

    second, second_mapping = merge_append_only(base, first, incoming, numbered=True)

    assert first_mapping == {2: 3}
    assert second == first
    assert second.count(b'incoming body\n') == 1
    assert second_mapping == {}


def test_retry_appends_only_entries_after_an_already_applied_prefix():
    base = b'## 1. old\nold body\n'
    main = base + b'\n## 2. main\nmain body\n'
    incoming_b = base + b'\n## 2. incoming B\nB body\n'
    first, _ = merge_append_only(base, main, incoming_b, numbered=True)
    incoming_bc = base + (b'\n## 2. incoming B\nB body\n'
                          b'\n## 3. incoming C\nC body\n')

    second, mapping = merge_append_only(base, first, incoming_bc, numbered=True)

    assert second.count(b'B body\n') == 1
    assert second.count(b'C body\n') == 1
    assert b'## 4. incoming C\n' in second
    assert mapping == {3: 4}


def test_retry_deduplicates_remainder_after_interleaved_main_append():
    base = b'## 1. old\nold body\n'
    incoming_bc = base + (b'\n## 2. incoming B\nB body\n'
                          b'\n## 3. incoming C\nC body\n')
    main = base + (b'\n## 2. incoming B\nB body\n'
                   b'\n## 3. main D\nD body\n')
    first, _ = merge_append_only(base, main, incoming_bc, numbered=True)

    second, mapping = merge_append_only(base, first, incoming_bc, numbered=True)

    assert second == first
    assert second.count(b'B body\n') == 1
    assert second.count(b'C body\n') == 1
    assert mapping == {}


def test_numbered_replay_ignores_headings_inside_fenced_code_blocks():
    base = b'## 100. old\nold body\n'
    incoming = base + (b'\n## 101. incoming\nbefore\n'
                       b'```md\n## 999. sample\n```\n'
                       b'\n## 102. next\nbody\n')

    merged, mapping = merge_append_only(base, base, incoming, numbered=True)

    assert b'```md\n## 999. sample\n```\n' in merged
    assert b'## 101. incoming\n' in merged
    assert b'## 102. next\n' in merged
    assert mapping == {}
