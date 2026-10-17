"""Read-only independent recount of real Codex corpus against candidate parser.

Not a repo test: tester-side evidence only. Never writes state or session files.
"""
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from herdr.execution_trace import _classify_action  # noqa: E402
from herdr.native_session_trace import (  # noqa: E402
    MAX_BYTES,
    _read_text,
    parse_codex_jsonl,
)

ROOT = Path.home() / ".codex" / "sessions"

# False-positive re-check predicates, deliberately stricter than the parser.
READ_BINS = {"cat", "head", "tail", "sed"}
TEST_BINS = {"pytest", "py.test", "tox"}


def main():
    kinds = Counter()
    files = 0
    actions_total = 0
    suspicious = []
    for path in sorted(ROOT.rglob("*.jsonl")):
        if path.name.endswith(".jsonl.zst"):
            continue
        try:
            text, _ = _read_text(path, MAX_BYTES)
        except OSError:
            continue
        files += 1
        acts, _notes = parse_codex_jsonl(text, session_id=path.stem)
        actions_total += len(acts)
        for action in acts:
            view = _classify_action(action)
            kind = view.get("kind")
            kinds[kind] += 1
            command = action.get("command") or ""
            if kind == "file_read":
                if action.get("exit_code") is None:
                    kinds["file_read_exit_unknown"] += 1
                head = command.split("&&")[0].split("||")[0].strip()
                argv = head.split()
                base = os.path.basename(argv[0]) if argv else ""
                if base not in READ_BINS or not action.get("path"):
                    suspicious.append(("file_read", path.name, command[:100], action.get("path")))
                if any(c in command for c in "><|*$\\'\"") and "&&" not in command:
                    suspicious.append(("file_read-shellmeta", path.name, command[:100], action.get("path")))
            elif kind == "file_edit":
                if action.get("tool") != "apply_patch":
                    suspicious.append(("file_edit", path.name, str(action.get("tool"))[:60], None))
            elif kind in {"test_passed", "test_failed"}:
                argv = command.split()
                base = os.path.basename(argv[0]) if argv else ""
                if base not in TEST_BINS and not (base in {"python", "python3"} and argv[1:2] == ["-m"]):
                    suspicious.append(("test_bin", path.name, command[:100], kind))
                if "&&" in command or "||" in command or ";" in command:
                    suspicious.append(("test_compound", path.name, command[:100], kind))
                if action.get("exit_code") is None:
                    suspicious.append(("test_no_exit", path.name, command[:100], kind))

    report = {
        "jsonl_files": files,
        "native_actions": actions_total,
        "kinds": dict(kinds),
        "suspicious_count": len(suspicious),
        "suspicious_sample": suspicious[:20],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
