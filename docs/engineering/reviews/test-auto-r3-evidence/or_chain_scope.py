"""Quantify how many claimed file_read events come from an OR-chain head.

`cat a || cat b` overall exit 0 does NOT prove `cat a` succeeded, so a read
claim from the head of an OR-chain is unproven evidence.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from herdr.native_session_trace import MAX_BYTES, _read_text, parse_codex_jsonl  # noqa: E402

ROOT = Path.home() / ".codex" / "sessions"

or_reads = []
and_reads = 0
plain_reads = 0
for path in sorted(ROOT.rglob("*.jsonl")):
    if path.name.endswith(".jsonl.zst"):
        continue
    try:
        text, _ = _read_text(path, MAX_BYTES)
    except OSError:
        continue
    actions, _ = parse_codex_jsonl(text, session_id=path.stem)
    for action in actions:
        if action.get("kind") != "file_read":
            continue
        command = action.get("command") or ""
        if "||" in command:
            or_reads.append((path.name, command[:120], action.get("path"), action.get("exit_code")))
        elif "&&" in command:
            and_reads += 1
        else:
            plain_reads += 1

print(f"file_read claims: total={len(or_reads) + and_reads + plain_reads} plain={plain_reads} and_chain={and_reads} or_chain={len(or_reads)}")
for item in or_reads[:10]:
    print("OR-CHAIN:", item)
