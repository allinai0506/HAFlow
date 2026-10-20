"""Read and validate fresh gate verdict evidence from files and terminal output.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def auto_verdict_enabled(ports):
    value = ports.os.environ.get("HERDR_AUTO_VERDICT", "1")
    return value.strip().lower() not in ("0", "false", "off", "no")

def _normalize_gate_verdict(ports, value):
    return ports._GATE_VERDICT_ALIASES.get(str(value or "").strip().lower())

def _gate_verdict_file_candidates(ports, task):
    """结论文件候选路径：状态目录优先，clone 内旧契约路径兜底。"""
    candidates = []
    task_id = task.get("task_id")
    if task_id and ports.direct_dispatch_planner is not None:
        try:
            candidates.append(ports.direct_dispatch_planner.gate_verdict_path(task_id))
        except Exception:
            pass
    clone_path = task.get("clone_path")
    if clone_path:
        candidates.append(ports.os.path.join(clone_path, ports.GATE_VERDICT_FILE))
    return candidates

def _gate_verdict_file_is_fresh(ports, path, task):
    """PR #250 & Codex P1: Check if candidate verdict file belongs to current execution epoch."""
    expected_token = task.get("execution_token") or task.get("rework_request_id")
    if expected_token:
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = ports.json.load(f)
            if not isinstance(payload, dict):
                return False
            file_token = (
                payload.get("execution_token")
                or payload.get("rework_request_id")
                or payload.get("rework_token")
            )
            if not file_token or file_token != expected_token:
                # Codex Review Round 7: Authenticate operator bypass receipts as a trusted path
                bypass = payload.get("bypass")
                tid = task.get("task_id") or task.get("id")
                if bool(tid) and isinstance(bypass, dict) and (bypass.get("task_id") == tid) and bypass.get("bypass_id"):
                    pass
                else:
                    return False
        except Exception:
            return False

    rework_started = task.get("rework_started_at")
    if not rework_started:
        return True
    try:
        st = ports.os.stat(path)
        return st.st_mtime >= (rework_started - 1.0)
    except OSError:
        return False

def _gate_verdict_is_fresh(ports, task):
    """PR #250 & Codex P1: Return True if at least one candidate file is valid and fresh."""
    for path in ports._gate_verdict_file_candidates(task):
        try:
            if not ports.os.path.exists(path):
                continue
            if not ports._gate_verdict_file_is_fresh(path, task):
                continue
            with open(path, encoding="utf-8") as handle:
                payload = ports.json.load(handle)
            if isinstance(payload, dict) and ports._normalize_gate_verdict(payload.get("verdict")):
                return True
        except Exception:
            continue
    return False

def _gate_verdict_file_mtime(ports, task):
    """PR #250 & Codex review: Return mtime of first fresh, valid candidate verdict file, or None."""
    for path in ports._gate_verdict_file_candidates(task):
        try:
            if not ports.os.path.exists(path):
                continue
            if not ports._gate_verdict_file_is_fresh(path, task):
                continue
            with open(path, encoding="utf-8") as handle:
                payload = ports.json.load(handle)
            if isinstance(payload, dict) and ports._normalize_gate_verdict(payload.get("verdict")):
                return ports.os.stat(path).st_mtime
        except Exception:
            continue
    return None

def _verdict_from_file(ports, task):
    for path in ports._gate_verdict_file_candidates(task):
        try:
            if not ports.os.path.exists(path):
                continue
            if not ports._gate_verdict_file_is_fresh(path, task):
                continue
            with open(path, encoding="utf-8") as handle:
                payload = ports.json.load(handle)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        verdict = ports._normalize_gate_verdict(payload.get("verdict"))
        if verdict:
            return verdict, str(payload.get("note") or "").strip()
    return None, ""

def _verdict_affected_task_ids(ports, task):
    """PR #110:仅从门禁结论文件读取 affected_task_ids(结构化字段)。

    屏幕输出是自由文本,绝不作为归因来源。取值与 _verdict_from_file
    选用同一文件(第一个给出合法 verdict 的候选)。字段缺失/非列表/
    含非字符串成员 → None(调用方不传 --affected-task-id,等价 legacy);
    显式空列表 → [](Verifier 表示无法归因,决策层据此 fallback)。
    """
    for path in ports._gate_verdict_file_candidates(task):
        try:
            if not ports.os.path.exists(path):
                continue
            if not ports._gate_verdict_file_is_fresh(path, task):
                continue
            with open(path, encoding="utf-8") as handle:
                payload = ports.json.load(handle)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if not ports._normalize_gate_verdict(payload.get("verdict")):
            continue
        raw = payload.get("affected_task_ids")
        if not isinstance(raw, list):
            return None
        ids = []
        for item in raw:
            text = str(item).strip() if isinstance(item, str) else ""
            if not text:
                return None
            if text not in ids:
                ids.append(text)
        return ids
    return None

def _is_instructional_or_ambiguous_verdict_line(ports, line):
    """过滤指令模板、Prompt回显或歧义讨论行，防止误判为有效门禁结论。"""
    if not line:
        return True
    # 1. 单行出现多次标记(如 'HERDR_GATE_VERDICT: pass 或 HERDR_GATE_VERDICT: blocked')
    if line.count(ports.GATE_VERDICT_MARKER) > 1:
        return True

    # 2. 含有二选一、条件词或模板占位符
    line_lower = line.lower()
    disjunctive_keywords = (
        "或", " or ", " / ", "二选一", "示例", "example", "格式", "template",
        "<pass", "[pass", "<blocked", "[blocked", "二者选一"
    )
    if any(k in line_lower for k in disjunctive_keywords):
        return True

    return False

def _verdict_from_screen(ports, task):
    pane_id = task.get("pane_id")
    if not pane_id:
        return None, ""
    try:
        result = ports.subprocess.run(
            ["herdr", "pane", "read", pane_id, "--source", "visible"],
            text=True,
            capture_output=True,
            timeout=10,
        )
    except Exception:
        return None, ""

    screen = (result.stdout or "") + "\n" + (result.stderr or "")
    verdicts = set()
    note = ""
    for line in screen.splitlines():
        if ports.GATE_VERDICT_MARKER in line:
            if ports._is_instructional_or_ambiguous_verdict_line(line):
                continue
            raw = line.split(ports.GATE_VERDICT_MARKER, 1)[1].strip()
            tokens = raw.split()
            if not tokens:
                continue
            first_token = tokens[0].strip().rstrip(".,;:")
            normalized = ports._normalize_gate_verdict(first_token)
            if normalized:
                # 检查第一词后续 token 中是否混有对立裁决词(如 'pass blocked' 等歧义讨论)
                if len(tokens) > 1:
                    trailing = {t.strip(".,;:()/[]{}").lower() for t in tokens[1:]}
                    conflicting = "blocked" if normalized == "pass" else "pass"
                    if conflicting in trailing:
                        continue
                verdicts.add(normalized)
        if ports.GATE_NOTE_MARKER in line and not note:
            candidate_note = line.split(ports.GATE_NOTE_MARKER, 1)[1].strip()
            # 过滤模板占位符说明如 'HERDR_GATE_NOTE: <原因>' 或 'HERDR_GATE_NOTE: 一句话结论'
            if candidate_note and not any(k in candidate_note for k in ("<原因>", "一句话结论", "阻塞原因清单", "note...")):
                note = candidate_note

    if len(verdicts) != 1:
        return None, ""
    return verdicts.pop(), note

def read_gate_verdict(ports, task):
    """(verdict, note, source)：文件与终端两路信号一致才返回 verdict。"""
    file_verdict, file_note = ports._verdict_from_file(task)
    screen_verdict, screen_note = ports._verdict_from_screen(task)

    signals = {v for v in (file_verdict, screen_verdict) if v}
    if len(signals) != 1:
        return None, "", ""

    verdict = signals.pop()
    note = file_note or screen_note
    source = "file+screen" if file_verdict and screen_verdict else (
        "file" if file_verdict else "screen"
    )
    return verdict, note, source


DEPENDENCIES = {'auto_verdict_enabled': ('os',),
 '_normalize_gate_verdict': ('_GATE_VERDICT_ALIASES',),
 '_gate_verdict_file_candidates': ('GATE_VERDICT_FILE', 'direct_dispatch_planner', 'os'),
 '_gate_verdict_file_is_fresh': ('json', 'os'),
 '_gate_verdict_is_fresh': ('_gate_verdict_file_candidates', '_gate_verdict_file_is_fresh',
                            '_normalize_gate_verdict', 'json', 'os'),
 '_gate_verdict_file_mtime': ('_gate_verdict_file_candidates', '_gate_verdict_file_is_fresh',
                              '_normalize_gate_verdict', 'json', 'os'),
 '_verdict_from_file': ('_gate_verdict_file_candidates', '_gate_verdict_file_is_fresh',
                        '_normalize_gate_verdict', 'json', 'os'),
 '_verdict_affected_task_ids': ('_gate_verdict_file_candidates', '_gate_verdict_file_is_fresh',
                                '_normalize_gate_verdict', 'json', 'os'),
 '_is_instructional_or_ambiguous_verdict_line': ('GATE_VERDICT_MARKER',),
 '_verdict_from_screen': ('GATE_NOTE_MARKER', 'GATE_VERDICT_MARKER',
                          '_is_instructional_or_ambiguous_verdict_line', '_normalize_gate_verdict',
                          'subprocess'),
 'read_gate_verdict': ('_verdict_from_file', '_verdict_from_screen')}

__all__ = tuple(DEPENDENCIES)
