#!/usr/bin/env python3
"""Shared agent CLI binary resolution.

LaunchAgent 服务（console / controller）运行在精简 PATH 下
（/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin），
装在 volta、~/.local/bin、~/.qoder-cn/entry 等位置的 CLI 会被
shutil.which 误判为"未安装"。本模块是 agent id -> CLI 二进制映射
与解析的唯一事实来源，console / preflight / deep_preflight 一律从这里取。
"""

import os
import shutil
import subprocess
from pathlib import Path

HOME = Path.home()

# Internal Factory agent id -> actual local CLI binary.
AGENT_BINARIES = {
    "opencode": "opencode",
    "codex": "codex",
    "claude": "claude",
    "qodercli": "qodercn",
    "agy": "agy",
    "pi": "pi",
    "grok": "grok",
    "kimi": "kimi",
}

# 用户主目录下的常见 Agent CLI 安装目录（按优先级排列）。
# 优先于系统全局目录，确保用户更新的最新 CLI 优先于系统陈旧版本。
USER_BIN_DIRS = [
    HOME / ".opencode" / "bin",
    HOME / ".local" / "bin",
    HOME / ".volta" / "bin",
    HOME / ".cargo" / "bin",
    HOME / ".bun" / "bin",
    HOME / ".grok" / "bin",
    HOME / ".kimi-code" / "bin",
    HOME / ".qoder-cn" / "entry",
    HOME / ".qoder-cn" / "bin",
    HOME / ".qoder" / "bin",
    HOME / ".qodersec" / "bin",
]

# LaunchAgent 精简 PATH 之外或顺序靠后的常见 CLI 安装目录。
EXTRA_BIN_DIRS = USER_BIN_DIRS + [
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
]


def ensure_user_bin_dirs():
    """将用户主目录下的常见 CLI 路径前置注入到 os.environ['PATH']，
    消除 LaunchAgent 精简 PATH 与用户交互式终端之间的环境差异，
    并保证用户级 CLI（如 ~/.opencode/bin）优先于系统级/Homebrew 残留。
    """
    current_path = os.environ.get("PATH", "")
    current_parts = current_path.split(os.pathsep) if current_path else []
    parts_to_add = []
    for d in USER_BIN_DIRS:
        sd = str(d)
        if d.exists() and sd not in current_parts:
            parts_to_add.append(sd)
    if parts_to_add:
        os.environ["PATH"] = os.pathsep.join(parts_to_add + current_parts)


ensure_user_bin_dirs()


def _find_in_dirs(binary_name, dirs):
    for d in dirs:
        p = Path(d) / binary_name
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    return None


def _find_via_login_shell(binary_name):
    # 最后兜底：登录 shell 加载 rc 后的 PATH，覆盖其余版本管理器场景。
    # 解析 rc 可能有 banner 输出，只取最后一行并校验文件存在。
    shell = os.environ.get("SHELL", "/bin/zsh")
    try:
        r = subprocess.run(
            [shell, "-lic", f"command -v {binary_name}"],
            text=True,
            capture_output=True,
            timeout=8,
        )
        lines = (r.stdout or "").strip().splitlines()
        if r.returncode == 0 and lines:
            candidate = lines[-1].strip()
            if candidate and Path(candidate).exists():
                return candidate
    except Exception:
        pass
    return None


def resolve_binary(binary_name):
    """按 PATH -> 常见安装目录 -> 登录 shell 的顺序解析 CLI 绝对路径。"""
    if not binary_name:
        return None
    direct = shutil.which(binary_name)
    if direct:
        return direct
    in_dirs = _find_in_dirs(binary_name, EXTRA_BIN_DIRS)
    if in_dirs:
        return in_dirs
    return _find_via_login_shell(binary_name)


def resolve_agent_binary(agent):
    """按 Factory agent id 解析本地 CLI；未识别的 agent 按同名 CLI 处理。"""
    return resolve_binary(AGENT_BINARIES.get(agent, agent))
