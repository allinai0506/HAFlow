"""herdr/interaction_loop.py — Agent Human Interaction Loop V1.

纯函数核心：检测 Agent 终端交互阻塞、提取选项、生成按键映射、发送响应、审计。

设计约束：
- 本模块无直接外部 I/O（读取终端内容由调用方传入 pane_content str）。
- _send_keys_impl 是唯一的副作用边界，可被测试 mock。
- 不调用 steering、gate、advance 等调度 API。
- 所有选项通过服务端生成的 option_id（UUID）标识，浏览器不能提交原始按键。

已验证的 Agent 交互模式（V1 范围）：
- Claude Code: 数字选项提示（如 "1. Yes  2. No"）
- Codex: 字母选项提示（如 "a) Yes  b) No"）
- Qoder/Qwen: [y]/[n] 提示
- 通用: [y/n]、[Y/n]、[y/N] inline 提示

未验证情况（返回 detected=False，保留终端入口）：
- 未知 Agent 的专有交互协议
- 多轮对话式确认
- 需要用户手动输入文本的自由输入提示
"""

import hashlib
import re
import subprocess
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# ANSI strip helper
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from terminal output."""
    return _ANSI_RE.sub('', text)


# ---------------------------------------------------------------------------
# Known interaction patterns (V1: verified via terminal observation)
# ---------------------------------------------------------------------------

# Pattern: numbered options "1. label  2. label" or "1) label"
_NUMBERED_OPTION_RE = re.compile(
    r'^\s*(\d+)[.)]\s+(.+)$', re.MULTILINE
)

# Pattern: lettered options "a) label  b) label"
_LETTERED_OPTION_RE = re.compile(
    r'^\s*([a-z])[.)]\s+(.+)$', re.MULTILINE
)

# Pattern: bracketed Y/N inline — "[y] Yes  [n] No" or "[y/n]:"
_YN_BRACKET_RE = re.compile(
    r'\[([yYnN])\]\s*(\w+)', re.IGNORECASE
)

# Pattern: trailing [y/n] or [Y/n] or [y/N] prompt
_YN_INLINE_RE = re.compile(
    r'\[([Yy]/[Nn]|[Nn]/[Yy])\]\s*:?\s*$', re.MULTILINE
)

# Pattern: numbered bracket prompt at end of line  "[1 for yes, 2 to cancel]:"
_NUMBERED_BRACKET_PROMPT_RE = re.compile(
    r'\[\d+[^\]]+\]\s*:?\s*$'
)

# Pattern: generic question ending with "?" or ":" before options
_QUESTION_RE = re.compile(
    r'([^\n]{10,}\?)\s*$', re.MULTILINE
)


def _make_option(label: str, key_sequence: str, seed: str = "") -> Dict[str, str]:
    """Build a single option dict with a server-generated opaque, deterministic option_id."""
    raw = f"{seed}:{key_sequence}:{label.strip()}" if seed else f"{key_sequence}:{label.strip()}"
    opt_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return {
        "option_id": f"opt_{opt_hash}",
        "label": label.strip(),
        "key_sequence": key_sequence,
    }


def _extract_numbered_options(text: str, seed: str = "") -> Optional[List[Dict[str, str]]]:
    """Extract numbered options (1. Yes, 2. No) → key_sequence = "1", "2", ..."""
    matches = _NUMBERED_OPTION_RE.findall(text)
    if len(matches) < 2:
        return None
    return [_make_option(label, num, seed) for num, label in matches]


def _extract_lettered_options(text: str, seed: str = "") -> Optional[List[Dict[str, str]]]:
    """Extract lettered options (a) Yes, b) No) → key_sequence = "a", "b", ..."""
    matches = _LETTERED_OPTION_RE.findall(text)
    if len(matches) < 2:
        return None
    return [_make_option(label, letter, seed) for letter, label in matches]


def _extract_yn_bracket_options(text: str, seed: str = "") -> Optional[List[Dict[str, str]]]:
    """Extract [y] Yes [n] No style options."""
    matches = _YN_BRACKET_RE.findall(text)
    if len(matches) < 2:
        return None
    return [_make_option(label or key.upper(), key.lower(), seed) for key, label in matches]


def _extract_yn_inline_options(text: str, seed: str = "") -> Optional[List[Dict[str, str]]]:
    """Extract trailing [y/n] or [Y/n] prompt → Yes (y) / No (n)."""
    if _YN_INLINE_RE.search(text):
        return [
            _make_option("Yes", "y", seed),
            _make_option("No", "n", seed),
        ]
    return None


def _find_question_line(text: str) -> str:
    """Find the most likely question line in the terminal content."""
    # Take last non-empty line before options, or last line with '?'
    m = _QUESTION_RE.findall(text)
    if m:
        return m[-1].strip()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return lines[-1] if lines else ""


def _detect_generic_choice(text: str, seed: str = "") -> Optional[Tuple[str, List[Dict[str, str]]]]:
    """Generic choice detection: numbered > lettered > y/n bracket > y/n inline."""
    # Numbered (most reliable — Claude code, Codex)
    opts = _extract_numbered_options(text, seed)
    if opts:
        return "choice_prompt", opts

    # Lettered (Codex, some Claude responses)
    opts = _extract_lettered_options(text, seed)
    if opts:
        return "choice_prompt", opts

    # [y] Yes [n] No bracket (Qoder, Kimi)
    opts = _extract_yn_bracket_options(text, seed)
    if opts:
        return "yn_prompt", opts

    # Trailing [y/n] inline
    opts = _extract_yn_inline_options(text, seed)
    if opts:
        return "yn_prompt", opts

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_agent_interaction(
    pane_content: str,
    agent_name: str,
) -> Dict[str, Any]:
    """Detect if the agent terminal contains an interactive prompt awaiting user input.

    Args:
        pane_content: Raw terminal output (ANSI sequences accepted, will be stripped).
        agent_name: Agent name string (e.g. "claude", "codex", "qodercli").

    Returns:
        dict with keys:
            detected (bool): True if a supported interactive prompt was found.
            interaction_type (str): "choice_prompt" | "yn_prompt" | ""
            raw_prompt (str): The question text extracted from terminal.
            options (list[dict]): List of {option_id, label, key_sequence}.
            agent_name (str): Echo of input agent_name.
            unrecognized (bool): True if detected=False due to unknown format.
    """
    if not pane_content or not pane_content.strip():
        return _not_detected(agent_name, "empty_content")

    clean = _strip_ansi(pane_content)
    seed = compute_prompt_fingerprint(clean)

    result = _detect_generic_choice(clean, seed)
    if result is None:
        raw_prompt = _find_question_line(clean)
        return _not_detected(agent_name, "no_pattern_match", raw_prompt=raw_prompt)

    interaction_type, options = result
    raw_prompt = _find_question_line(clean)

    return {
        "detected": True,
        "interaction_type": interaction_type,
        "raw_prompt": raw_prompt,
        "options": options,
        "agent_name": agent_name,
        "unrecognized": False,
    }


def _not_detected(agent_name: str, reason: str = "", raw_prompt: str = "") -> Dict[str, Any]:
    return {
        "detected": False,
        "interaction_type": "",
        "raw_prompt": raw_prompt,
        "options": [],
        "agent_name": agent_name,
        "unrecognized": reason == "no_pattern_match",
    }


def compute_prompt_fingerprint(pane_content: str) -> str:
    """Compute a 16-hex-char fingerprint of the prompt content.

    Used to detect if the agent has moved on (prompt changed) before submitting.
    Returns 8 bytes (16 hex chars) of SHA-256 of the raw text.
    """
    digest = hashlib.sha256(pane_content.encode("utf-8", errors="replace")).digest()
    return digest[:8].hex()


def validate_interaction_identity(
    stored: Dict[str, Any],
    current: Dict[str, Any],
) -> Tuple[bool, str]:
    """Validate that the agent, pane, terminal and prompt fingerprint have not changed.

    Args:
        stored: Snapshot taken at interaction creation time.
        current: Fresh snapshot from herdr agent get + pane read.

    Returns:
        (ok: bool, reason: str) — reason is "" on success, description on failure.
    """
    if stored.get("pane_id") != current.get("pane_id"):
        return False, f"pane drift: stored={stored.get('pane_id')!r} current={current.get('pane_id')!r}"

    if stored.get("agent") != current.get("agent"):
        return False, f"agent changed: stored={stored.get('agent')!r} current={current.get('agent')!r}"

    if stored.get("terminal_id") != current.get("terminal_id"):
        return False, f"terminal_id changed: stored={stored.get('terminal_id')!r} current={current.get('terminal_id')!r}"

    stored_fp = stored.get("prompt_fingerprint", "")
    current_fp = current.get("prompt_fingerprint", "")
    if stored_fp and current_fp and stored_fp != current_fp:
        return False, f"prompt fingerprint mismatch: stored={stored_fp!r} current={current_fp!r}"

    return True, ""


def resolve_option_key(
    option_id: str,
    options: List[Dict[str, str]],
) -> Optional[str]:
    """Resolve a server-generated option_id back to its key_sequence.

    Returns None if option_id is not in the provided options list.
    This ensures only server-issued option IDs can trigger key sends.
    """
    if not option_id or not options:
        return None
    for opt in options:
        if opt.get("option_id") == option_id:
            return opt.get("key_sequence")
    return None


def _send_keys_impl(pane_id: str, key: str) -> bool:
    """Send keystroke to target pane/agent.

    Per protocol: prioritizes `herdr agent send-keys` native agent interaction API;
    falls back to `herdr pane send-keys` if needed.
    Timeout: 10 seconds.
    """
    try:
        r = subprocess.run(
            ["herdr", "agent", "send-keys", pane_id, key],
            text=True,
            capture_output=True,
            timeout=10,
        )
        if r.returncode == 0:
            return True
    except Exception:
        pass

    try:
        r = subprocess.run(
            ["herdr", "pane", "send-keys", pane_id, key],
            text=True,
            capture_output=True,
            timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def send_interaction_response(pane_id: str, key_sequence: str) -> bool:
    """Send a resolved key_sequence to the target pane.

    Args:
        pane_id: Target pane identifier.
        key_sequence: The validated key to send (e.g. "1", "y", "enter").

    Returns:
        True if herdr send-keys returned exit code 0, False otherwise.
        False does NOT automatically retry — caller must record uncertain state.
    """
    if not pane_id or not key_sequence:
        return False
    ok = _send_keys_impl(pane_id, key_sequence)
    if ok and key_sequence.lower() not in ("enter", "\r", "\n", "esc", "escape"):
        _send_keys_impl(pane_id, "enter")
    return ok


def record_interaction_event(
    store: Any,
    event_type: str,
    workflow_id: str,
    task_id: str,
    pane_id: str,
    agent: str,
    prompt_fingerprint: str,
    option_id: str,
    result: str,
    detail: str = "",
) -> None:
    """Record an interaction lifecycle event to the StateStore audit log.

    Sensitive data (actual key_sequence, passwords, tokens) must NOT be logged.
    Only the opaque option_id and result status are recorded.
    """
    payload: Dict[str, Any] = {
        "pane_id": pane_id,
        "agent": agent,
        "prompt_fingerprint": prompt_fingerprint,
        "option_id": option_id,
        "result": result,
        "recorded_at": time.time(),
    }
    if detail:
        payload["detail"] = detail

    store.record_event(
        event_type,
        payload,
        workflow_id=workflow_id,
        task_id=task_id,
        source="interaction_loop",
    )


def describe_known_patterns() -> List[Dict[str, str]]:
    """Return a list of known, verified interaction patterns for V1.

    This is the authoritative list of what the system can reliably detect.
    Patterns not listed here fall through to detected=False.
    """
    return [
        {
            "agent": "claude",
            "pattern_type": "numbered_choice",
            "description": "Numbered list options (1. Yes, 2. No) with trailing [N for ...] prompt",
            "example": "  1. Yes, proceed\n  2. No, cancel\n  [1 for yes, 2 to cancel]: ",
            "key_mapping": "digit (1, 2, ...) → key_sequence",
        },
        {
            "agent": "codex",
            "pattern_type": "lettered_choice",
            "description": "Lettered options (a) Yes, b) No, c) Always) with implicit selection",
            "example": "  a) Yes, allow once\n  b) No, deny\n  c) Yes, always allow",
            "key_mapping": "letter (a, b, c) → key_sequence",
        },
        {
            "agent": "qodercli",
            "pattern_type": "yn_bracket",
            "description": "[y] Yes  [n] No bracket-style permission prompt",
            "example": "  Run shell command: ...?\n  [y] Yes  [n] No: ",
            "key_mapping": "y → Yes, n → No",
        },
        {
            "agent": "generic",
            "pattern_type": "yn_inline",
            "description": "Trailing [y/n] or [Y/n] or [y/N] inline confirmation prompt",
            "example": "  Are you sure you want to continue? [y/N]: ",
            "key_mapping": "y → Yes, n → No",
        },
    ]
