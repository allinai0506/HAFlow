"""Measure native actions lost between the loader and the assembled trace page.

Real sessions only, read-only. A Codex session id can span several rollout
files; source_ref is prefixed by session id + per-file ordinal, so refs
collide and merge_trace_events drops the later file's actions.
"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from herdr.execution_trace import assemble_trace  # noqa: E402
from herdr.native_session_trace import (  # noqa: E402
    MAX_BYTES,
    _codex_meta,
    _read_text,
    load_native_actions,
    parse_codex_jsonl,
)

ROOT = Path.home() / ".codex" / "sessions"

by_id = {}
for path in sorted(ROOT.rglob("*.jsonl")):
    if path.name.endswith(".jsonl.zst"):
        continue
    cwd, sid = _codex_meta(path)
    if sid and cwd:
        by_id.setdefault(sid, []).append((cwd, path))

multi = {k: v for k, v in by_id.items() if len({p for _, p in v}) > 1}
print(f"sessions with >1 rollout file: {len(multi)}")

worst = []
for sid, items in multi.items():
    files = sorted({p for _, p in items})[:8]
    parsed = 0
    for path in files:
        text, _ = _read_text(path, MAX_BYTES)
        acts, _n = parse_codex_jsonl(text, session_id=sid)
        parsed += len(acts)
    if parsed == 0:
        continue
    refs = Counter()
    for path in files:
        text, _ = _read_text(path, MAX_BYTES)
        acts, _n = parse_codex_jsonl(text, session_id=sid)
        for a in acts:
            refs[a["source_ref"]] += 1
    colliding = sum(c - 1 for c in refs.values() if c > 1)
    worst.append((colliding, parsed, sid, len(files)))

worst.sort(reverse=True)
print("top collisions (lost, parsed, session, files):")
for row in worst[:5]:
    print("  ", row)

# End-to-end through the loader + assembler, using the real clone as cwd.
if worst:
    _colliding, _parsed, sid, _files = worst[0]
    paths = [p for _c, p in by_id[sid]]
    cwd = None
    for c, p in by_id[sid]:
        cwd = c
        break
    task = {"task_id": "probe-task", "agent": "codex", "clone_path": cwd, "created_at": 10, "status_history": []}
    actions, notes = load_native_actions(task, [task], roots={"codex_sessions": ROOT}, budget_s=20.0)
    refs = {a["source_ref"] for a in actions}
    page = assemble_trace(task, [], actions, limit=200)
    kinds = Counter(e["kind"] for e in page["events"])
    print(f"e2e session={sid[:8]} loaded_actions={len(actions)} unique_refs={len(refs)} page_events={len(page['events'])} notes={notes}")
    print("  dropped_by_ref_collision =", len(actions) - len(refs), " kinds:", dict(kinds))
