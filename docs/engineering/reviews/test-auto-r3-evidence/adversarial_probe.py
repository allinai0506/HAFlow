"""Tester-side adversarial probes against the frozen candidate (read-only).

Each probe asserts the behaviour the acceptance criteria require. A probe that
fails prints FAIL with the observed value; nothing here mutates business code.
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from herdr import native_session_trace as nst  # noqa: E402
from herdr.execution_trace import (  # noqa: E402
    _classify_action,
    assemble_trace,
    project_native_actions,
    project_task_history,
)

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)[:220]))


def rows(items):
    return "\n".join(json.dumps({"type": "response_item", "payload": p}, ensure_ascii=False) for p in items)


def codex_exec(call_id, input_text, ts=None):
    payload = {"type": "custom_tool_call", "name": "exec", "call_id": call_id, "input": input_text}
    if ts is not None:
        payload["timestamp"] = ts
    return payload


def codex_out(call_id, text):
    return {"type": "custom_tool_call_output", "call_id": call_id, "output": [{"type": "input_text", "text": text}]}


def acts(text):
    a, _n = nst.parse_codex_jsonl(text, session_id="s1")
    return a


TASK = {"task_id": "t1", "clone_path": "/clone", "agent": "codex", "run_id": "r1"}

# ---- F1: conservative Codex identification -------------------------------
# 1a: secret-bearing read must not carry the path outward.
a = acts(rows([codex_exec("s", 'text(await tools.exec_command({cmd:"cat .env"}));', ts=1),
               codex_out("s", '{"exit_code":0}')]))
ev = project_native_actions(a, TASK)[0]
check("F1.secret_read_no_path_leak", ev["kind"] == "file_read" and ".env" not in json.dumps(ev, ensure_ascii=False), ev)

# 1b: multi-file cat must stay unclaimed.
a = acts(rows([codex_exec("m", 'text(await tools.exec_command({cmd:"cat a.txt b.txt"}));', ts=1),
               codex_out("m", '{"exit_code":0}')]))
check("F1.multi_file_cat_unclaimed", not [x for x in a if x.get("kind") == "file_read"], a)

# 1c: glob/redirect/heredoc forms unclaimed.
bad = ["cat *.py", "cat file > /tmp/x", "cat $HOME/f", "git diff<<EOF\nx\nEOF", "sed -n 1,10p *.py"]
leaks = []
for cmd in bad:
    raw = 'text(await tools.exec_command({cmd:"' + cmd + '"}));'
    a = acts(rows([codex_exec("g", raw, ts=1), codex_out("g", '{"exit_code":0}')]))
    for item in a:
        if item.get("kind") == "file_read":
            leaks.append((cmd, item.get("path")))
check("F1.shell_meta_never_read_claim", not leaks, leaks)

# || chain head must not be interpreted: overall exit 0 does not prove the head ran
# successfully. Requirement "保守识别 / 无证据不生成事件". -> DEFECT-1 if this fails.
a = acts(rows([codex_exec("o", 'text(await tools.exec_command({cmd:"cat missing.txt || cat real.txt"}));', ts=1),
               codex_out("o", '{"exit_code":0}')]))
check("F1.or_chain_head_unclaimed[DEFECT-1]", not [x for x in a if x.get("kind") == "file_read"], a)
a = acts(rows([codex_exec("o2", 'text(await tools.exec_command({cmd:"cat a.py && rm x || cp y z"}));', ts=1),
               codex_out("o2", '{"exit_code":0}')]))
check("F1.mixed_chain_head_unclaimed[DEFECT-1]", not [x for x in a if x.get("kind") == "file_read"], a)

# 1e: patch with an over-long path is not claimed.
long_path = "x" * 600
lit = f'*** Begin Patch\\n*** Update File: {long_path}\\n@@\\n-a\\n+b\\n*** End Patch'
a = acts(rows([codex_exec("L", f'const patch = "{lit}";\\ntext(await tools.apply_patch(patch));', ts=1),
               codex_out("L", '{"exit_code":0,"stdout":"Success."}')]))
check("F1.overlong_patch_path_unclaimed", not [x for x in a if x.get("kind") == "file_edit"], a)

# 1f: Begin Patch marker inside an *output* blob must not fabricate an edit.
a = acts(rows([codex_exec("Q", 'text(await tools.exec_command({cmd:"ls"}));', ts=1),
               codex_out("Q", '*** Begin Patch\\n*** Update File: src/ghost.py\\n*** End Patch')]))
check("F1.output_marker_not_edit", not [x for x in a if x.get("kind") == "file_edit"], a)

# 1g: exit_code 0 for an unrelated nested JSON must not vouch for a rejected patch.
a = acts(rows([codex_exec("V", f'const patch = "*** Begin Patch\\n*** Update File: src/a.py\\n@@\\n-a\\n+b\\n*** End Patch";\\ntext(await tools.apply_patch(patch));', ts=1),
               codex_out("V", 'patch rejected {"exit_code":0} ok')]))
kinds = [x.get("kind") for x in a]
check("F1.rejected_patch_text_not_claimed", "file_edit" not in kinds or all(x.get("exit_code") == 0 for x in a if x.get("kind") == "file_edit"), (kinds, a))

# 1h: malformed escape anywhere must never raise out of the parser.
try:
    a = acts(rows([codex_exec("Z", 'const patch = "*** Begin Patch\\q\\n*** End Patch";', ts=1)]))
    check("F1.malformed_no_escape", len(a) == 1 and a[0].get("kind") is None, a)
except Exception as exc:  # noqa: BLE001
    check("F1.malformed_no_escape", False, f"{type(exc).__name__}: {exc}")

# 1i: no exception escapes load_native_actions for a hostile tree.
tmp = ROOT / ".herdr-test-evidence" / "fake_codex"
sess = tmp / "2026" / "01"
sess.mkdir(parents=True, exist_ok=True)
(sess / "rollout-x-s1.jsonl").write_text(rows([
    codex_exec("h", 'text(await tools.exec_command({cmd:"cat \\u0000bad"}));'),
    codex_out("h", "\x00\x00binary"),
]), encoding="utf-8")
task = {"task_id": "t1", "clone_path": str(tmp), "agent": "codex", "runtime": {"native_session_id": "s1"}}
try:
    out, notes = nst.load_native_actions(task, [task], roots={"codex_sessions": tmp}, budget_s=2.0)
    check("F1.loader_no_exception", isinstance(out, list), notes)
except Exception as exc:  # noqa: BLE001
    check("F1.loader_no_exception", False, f"{type(exc).__name__}: {exc}")

# ---- F2: retry semantics ---------------------------------------------------
def retry_pair(prev_kind, cur_cmd, cur_exit=0):
    prev_input = 'text(await tools.exec_command({cmd:"pytest -q",max_output_tokens:10}));' if prev_kind == "test" else 'text(await tools.exec_command({cmd:"cat a.py"}));'
    prev_out = '{"exit_code":1}' if prev_kind == "test" else '{"exit_code":0}'
    payload_r = codex_exec("c2", f'text(await tools.exec_command({{cmd:"{cur_cmd}"}})); retry_marker', ts=5)
    payload_r["retry_of"] = "c1"
    items = [codex_exec("c1", prev_input, ts=1), codex_out("c1", prev_out), payload_r, codex_out("c2", json.dumps({"exit_code": cur_exit}))]
    a = acts(rows(items))
    evs = project_native_actions(a, TASK)
    return [e for e in evs if e["kind"] == "retry"]


r = retry_pair("test", "pytest -q")
check("F2.test_to_test_title", r and r[0]["title"] == "修复并再次测试", r)
r = retry_pair("other", "pytest -q")
check("F2.tool_to_test_title", r and r[0]["title"] == "再次测试", r)
r = retry_pair("test", "cat a.py")
check("F2.test_to_tool_title", r and r[0]["title"] == "重试工具调用", r)

# retry pointing at a later/unknown/self ref must not emit a retry event.
payload = codex_exec("c9", 'text(await tools.exec_command({cmd:"cat a.py"}));', ts=9)
payload["retry_of"] = "c9"
a = acts(rows([payload, codex_out("c9", '{"exit_code":0}')]))
check("F2.self_retry_ignored", not [e for e in project_native_actions(a, TASK) if e["kind"] == "retry"], a)

payload = codex_exec("c10", 'text(await tools.exec_command({cmd:"cat a.py"}));', ts=9)
payload["retry_of"] = "does-not-exist"
a = acts(rows([codex_exec("c1", 'text(await tools.exec_command({cmd:"cat b.py"}));', ts=1), codex_out("c1", '{"exit_code":0}'), payload, codex_out("c10", '{"exit_code":0}')]))
check("F2.unknown_retry_target_ignored", not [e for e in project_native_actions(a, TASK) if e["kind"] == "retry"], a)

# ---- F3: commit time-SHA correlation --------------------------------------
hist = task_history = {
    "task_id": "t1", "run_id": "r1", "agent": "codex", "created_at": 100.0, "started_at": 101.0,
    "commit": "a" * 40,
    "status_history": [
        {"to": "committed", "timestamp": 110.0},                       # no own SHA
        {"to": "committed", "timestamp": 120.0, "commit": "b" * 40},   # with own SHA
    ],
}
events = project_task_history(hist)
commits = [e for e in events if e["kind"] == "commit"]
titles = [e["title"] for e in events if e["kind"] == "task_completed"]
check("F3.shaless_committed_never_carries_task_sha", all("aaaaaaa" not in (e.get("detail") or "") for e in commits if e.get("timestamp") == 120.0), commits)
check("F3.commit_events_use_own_sha_only", sorted(e.get("detail") or "" for e in commits) == sorted(["Commit · bbbbbbb", "Commit · aaaaaaa"]), commits)
check("F3.shaless_becomes_submitted", "已提交" in titles, titles)
check("F3.unbacked_project_never_stamps_time", all(e.get("timestamp") is None for e in commits if e.get("source_ref") == "task:t1:commit"), commits)

# evidence-backed transition must win the correlation over the untimed projection
hist2 = dict(hist, status_history=[{"to": "committed", "timestamp": 110.0, "commit": "a" * 40}])
merged = assemble_trace(hist2, [], [])
commit_events = [e for e in merged["events"] if e["kind"] == "commit"]
check("F3.evidence_commit_wins_single_event", len(commit_events) == 1 and commit_events[0]["timestamp"] == 110.0, commit_events)

# unknown time stays unknown in the assembled page (no fabrication)
hist3 = dict(hist, status_history=[])
merged = assemble_trace(hist3, [], [])
ce = [e for e in merged["events"] if e["kind"] == "commit"]
check("F3.time_unknown_preserved", len(ce) == 1 and ce[0]["timestamp"] is None, ce)

# commit_result status strings must never masquerade as a SHA
hist4 = {"task_id": "t1", "run_id": "r1", "agent": "codex", "created_at": 100.0, "started_at": 101.0,
         "commit": None, "commit_result": "adopted",
         "status_history": [{"to": "committed", "timestamp": 110.0}]}
check("F3.adopted_not_a_commit", not [e for e in project_task_history(hist4) if e["kind"] == "commit"], project_task_history(hist4))

# ---- F6: exact session must not be starved by the scan budget -------------
deep = ROOT / ".herdr-test-evidence" / "f6"
sessions = deep / "sessions"
sessions.mkdir(parents=True, exist_ok=True)
clone = deep / "clone"
clone.mkdir(parents=True, exist_ok=True)
# Fill the newest-name-first scan window with unrelated sessions.
for i in range(nst.MAX_SCAN + 20):
    d = sessions / "2026" / "09" / f"{i:03d}"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"rollout-noise-{i:08d}.jsonl").write_text(
        json.dumps({"type": "response_item", "payload": {"type": "session_meta", "cwd": "/elsewhere", "session_id": f"noise-{i}"}}) + "\n",
        encoding="utf-8",
    )
old = sessions / "2025" / "01" / "01"
old.mkdir(parents=True, exist_ok=True)
target_id = "<redacted-uuid>"
(old / f"rollout-2025-01-01-{target_id}.jsonl").write_text(rows([
    {"type": "session_meta", "cwd": str(clone), "session_id": target_id},
    codex_exec("1", 'text(await tools.exec_command({cmd:"cat GOAL.md"}));', ts=10),
    codex_out("1", '{"exit_code":0}'),
]) + "\n", encoding="utf-8")
task6 = {"task_id": "t6", "clone_path": str(clone), "agent": "codex", "runtime": {"native_session_id": target_id}}
out6, notes6 = nst.load_native_actions(task6, [task6], roots={"codex_sessions": sessions}, budget_s=8.0)
check("F6.pinned_exact_session_found", any(x.get("kind") == "file_read" for x in out6), (len(out6), notes6))

# recorded id that cannot be proven must not bind another session
task7 = dict(task6, runtime={"native_session_id": "../../etc/passwd"})
out7, notes7 = nst.load_native_actions(task7, [task7], roots={"codex_sessions": sessions}, budget_s=8.0)
check("F6.traversal_recorded_id_binds_nothing", not out7, (len(out7), notes7))

# a tiny deadline must degrade with a note, never hang or crash
start = time.monotonic()
try:
    out8, notes8 = nst.load_native_actions(dict(task6, runtime={"native_session_id": "missing-id"}), [task6], roots={"codex_sessions": sessions}, budget_s=0.2)
    check("F6.deadline_bounded", time.monotonic() - start < 8.0 and isinstance(out8, list), (len(out8), notes8, round(time.monotonic() - start, 3)))
except Exception as exc:  # noqa: BLE001
    check("F6.deadline_bounded", False, f"{type(exc).__name__}: {exc}")

# ---- truthfulness: failure never reported as success ----------------------
a = acts(rows([codex_exec("f1", 'text(await tools.exec_command({cmd:"pytest -q"}));', ts=1), codex_out("f1", '{"exit_code":1,"output":"1 failed"}')]))
ev = project_native_actions(a, TASK)
check("T.failure_not_success", ev[0]["kind"] == "test_failed" and ev[0]["status"] == "error", ev)
a = acts(rows([codex_exec("f2", 'text(await tools.exec_command({cmd:"pytest -q"}));', ts=1)]))
ev = project_native_actions(a, TASK)
check("T.no_evidence_no_test_event", ev[0]["kind"] == "tool_call", ev)
classified = _classify_action({"tool": "exec", "command": "cat " + "y" * 4000, "exit_code": 0, "kind": "file_read", "source_ref": "codex:s:1"})
check("T.detail_bounded", len(classified["detail"]) <= 160, len(classified["detail"]))

# ---- DEFECT-2: same session id across rollout files collides on source_ref --
d2 = ROOT / ".herdr-test-evidence" / "f2dir" / "sessions" / "2026" / "01"
d2clone = ROOT / ".herdr-test-evidence" / "f2dir" / "clone"
d2.mkdir(parents=True, exist_ok=True)
d2clone.mkdir(parents=True, exist_ok=True)
sid2 = "<redacted-uuid>"
for n, cmd in (("A", "cat one.md"), ("B", "cat two.md")):
    body = rows([
        {"type": "session_meta", "cwd": str(d2clone), "session_id": sid2},
        codex_exec("1", f'text(await tools.exec_command({{cmd:"{cmd}"}}));', ts=10),
        codex_out("1", '{"exit_code":0}'),
    ])
    (d2 / f"rollout-2026-01-0{n}-{sid2}.jsonl").write_text(body + "\n", encoding="utf-8")
task_d2 = {"task_id": "t-d2", "clone_path": str(d2clone), "agent": "codex", "created_at": 1.0, "status_history": [], "runtime": {"native_session_id": sid2}}
loaded, notes_d2 = nst.load_native_actions(task_d2, [task_d2], roots={"codex_sessions": ROOT / ".herdr-test-evidence" / "f2dir" / "sessions"}, budget_s=5.0)
page_d2 = assemble_trace(task_d2, [], loaded, limit=100)
read_paths = [e.get("details", {}).get("path") for e in page_d2["events"] if e["kind"] == "file_read"]
check("F1.multi_file_session_keeps_every_action[DEFECT-2]",
      len(loaded) == 2 and set(read_paths) == {"one.md", "two.md"}, (len(loaded), read_paths, notes_d2))

# ---- reporting ------------------------------------------------------------
bad = [r for r in results if not r[1]]
for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail if not ok else ''}")
print(json.dumps({"total": len(results), "failed": len(bad)}, ensure_ascii=False))
sys.exit(1 if bad else 0)
