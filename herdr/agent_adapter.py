#!/usr/bin/env python3
"""Agent Adapter Matrix & Intervention Primitives (herdr/agent_adapter.py).

Establishes a transport-independent contract (AgentAdapter) between high-level
orchestration / steering and heterogeneous Agent runtimes.

Architecture:

    AgentAdapter          ← Transport-independent contract (zero TTY knowledge)
        │
        ├── UnknownAgentAdapter   ← Fail-closed fallback for unregistered agents
        │
        └── TTYAgentAdapter       ← TTY-level implementation (ctrl-c / pane send)
                ├── ClaudeAdapter
                ├── CodexAdapter
                ├── OpenCodeAdapter
                ├── QoderAdapter
                ├── AgyAdapter
                ├── PiAdapter
                ├── GrokAdapter
                └── KimiAdapter

The current concrete steering implementation (TTYAgentAdapter and subclasses) is
explicitly labelled "TTY-level steering prototype" — not a universal agent
steering protocol — because different agents diverge significantly in their
handling of:
  - Ctrl-C interrupts
  - Prompt injection & stdin parsing
  - Multi-turn session state retention
  - Post-interrupt session resume

A future NativeRpcAdapter or ApiAdapter would extend AgentAdapter directly,
providing its own implementations of steer_urgent/steer_soft/interrupt/resume
without any TTY knowledge.
"""

from dataclasses import dataclass
from datetime import datetime
import subprocess
import time
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class AgentCapability:
    """Declared runtime capabilities of an Agent Adapter.

    Defaults are all False (fail-closed). Every concrete adapter must
    explicitly declare what it supports.
    """

    supports_interrupt: bool = False        # Can handle SIGINT / ctrl-c soft halt
    supports_soft_steer: bool = False       # Can receive prompt injection during idle gap without interrupt
    supports_resume: bool = False           # Supports resuming execution context after interrupt
    supports_prompt_injection: bool = False # Can accept text prompts via stdin / pane
    protocol_level: str = "unknown"         # "tty_prototype" | "native_rpc" | "api" | "unknown"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "supports_interrupt": self.supports_interrupt,
            "supports_soft_steer": self.supports_soft_steer,
            "supports_resume": self.supports_resume,
            "supports_prompt_injection": self.supports_prompt_injection,
            "protocol_level": self.protocol_level,
        }


# ---------------------------------------------------------------------------
# Abstract contract — NO TTY / pane / subprocess knowledge
# ---------------------------------------------------------------------------

class AgentAdapter:
    """Transport-independent agent intervention contract.

    Subclasses MUST implement: interrupt(), steer_urgent(), steer_soft(), resume().
    This base class has NO knowledge of TTY, panes, ctrl-c, send-keys, or
    send-text. All transport-specific logic lives in TTYAgentAdapter.
    """

    name: str = "base"
    capabilities: AgentCapability = AgentCapability()  # all-False default

    @property
    def supports_interrupt(self) -> bool:
        return self.capabilities.supports_interrupt

    @property
    def supports_soft_steer(self) -> bool:
        return self.capabilities.supports_soft_steer

    @property
    def supports_resume(self) -> bool:
        return self.capabilities.supports_resume

    @property
    def supports_prompt_injection(self) -> bool:
        return self.capabilities.supports_prompt_injection

    @property
    def protocol_level(self) -> str:
        return self.capabilities.protocol_level

    def format_steer_prompt(self, instruction: str, operator: str = "human") -> str:
        """Format structured high-priority intervention prompt. Transport-agnostic."""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return (
            "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "【总指挥实时插话纠偏指令 - STEERING INSTRUCTION】\n"
            f"发起人：{operator} | 时间：{now_str}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "总指挥向你发送了高优先级干预指令，请立即优先吸收并按此调整后续动作：\n"
            f"> {instruction}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        )

    # ---- Methods that subclasses MUST implement --------------------------------

    def interrupt(self, target: str, reason: str = "") -> bool:
        """Send interrupt signal to the agent. Returns True on success."""
        raise NotImplementedError(f"{self.__class__.__name__} must implement interrupt()")

    def steer_urgent(
        self,
        target: str,
        instruction: str,
        operator: str = "human",
        wait_after_interrupt: float = 0.1,
    ) -> Dict[str, Any]:
        """Interrupt agent then inject high-priority instruction."""
        raise NotImplementedError(f"{self.__class__.__name__} must implement steer_urgent()")

    def steer_soft(
        self,
        target: str,
        instruction: str,
        operator: str = "human",
    ) -> Dict[str, Any]:
        """Inject instruction without interrupting (idle-gap soft steer)."""
        raise NotImplementedError(f"{self.__class__.__name__} must implement steer_soft()")

    def resume(self, target: str, **kwargs) -> bool:
        """Resume execution after interrupt, if supported."""
        raise NotImplementedError(f"{self.__class__.__name__} must implement resume()")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "capabilities": self.capabilities.to_dict(),
            "protocol_level": self.protocol_level,
        }


# ---------------------------------------------------------------------------
# Fail-closed fallback for unregistered agents
# ---------------------------------------------------------------------------

class UnknownAgentAdapter(AgentAdapter):
    """Fail-closed adapter returned for any unregistered or unknown agent.

    All capabilities are False and all operations return failure.
    Steering is disabled until an explicit adapter is registered.
    """

    name = "unknown"
    capabilities = AgentCapability(
        supports_interrupt=False,
        supports_soft_steer=False,
        supports_resume=False,
        supports_prompt_injection=False,
        protocol_level="unknown",
    )

    def _refuse(self, operation: str) -> Dict[str, Any]:
        return {
            "ok": False,
            "interrupted": False,
            "injected": False,
            "reason": "unknown_agent_no_adapter_registered",
            "detail": f"{operation} refused: agent not registered in AgentAdapter registry",
            "adapter": self.name,
            "protocol_level": self.protocol_level,
        }

    def interrupt(self, target: str, reason: str = "") -> bool:
        return False

    def steer_urgent(
        self,
        target: str,
        instruction: str,
        operator: str = "human",
        wait_after_interrupt: float = 0.1,
    ) -> Dict[str, Any]:
        return self._refuse("steer_urgent")

    def steer_soft(
        self,
        target: str,
        instruction: str,
        operator: str = "human",
    ) -> Dict[str, Any]:
        return self._refuse("steer_soft")

    def resume(self, target: str, **kwargs) -> bool:
        return False


# ---------------------------------------------------------------------------
# TTY-level implementation — all TTY/pane logic lives here
# ---------------------------------------------------------------------------

def _send_keys(pane_id: str, key: str) -> bool:
    """Send keystroke (e.g. enter, ctrl-c) to Herdr pane via subprocess."""
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


def _send_text(pane_id: str, text: str) -> bool:
    """Send text prompt to Herdr pane via subprocess."""
    try:
        r = subprocess.run(
            ["herdr", "pane", "send-text", pane_id, text],
            text=True,
            capture_output=True,
            timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


class TTYAgentAdapter(AgentAdapter):
    """TTY-level adapter implementing the current ctrl-c / pane steering prototype.

    All concrete TTY-based adapters (Claude, Codex, etc.) extend this class.
    TTY-specific primitives (send_keys, send_text) are ONLY in this class,
    keeping the AgentAdapter contract transport-independent.

    IMPORTANT: This is a TTY-level steering prototype, not a universal protocol.
    """

    name = "tty_prototype"
    capabilities = AgentCapability(
        supports_interrupt=True,
        supports_soft_steer=True,
        supports_resume=False,
        supports_prompt_injection=True,
        protocol_level="tty_prototype",
    )

    # ---- TTY primitives ------------------------------------------------------

    def send_keys(self, pane_id: str, key: str) -> bool:
        """Send keystroke to Herdr pane via _send_keys."""
        return _send_keys(pane_id, key)

    def send_text(self, pane_id: str, text: str) -> bool:
        """Send text prompt to Herdr pane via _send_text."""
        return _send_text(pane_id, text)

    def inject_prompt(self, pane_id: str, prompt: str) -> bool:
        """Inject prompt into agent via TTY: send-text + enter."""
        if not self.supports_prompt_injection:
            return False
        ok = self.send_text(pane_id, prompt)
        if ok:
            return self.send_keys(pane_id, "enter")
        return False

    # ---- Contract implementations using TTY primitives -----------------------

    def interrupt(self, target: str, reason: str = "") -> bool:
        """Send ctrl-c to interrupt the running agent in target pane."""
        if not self.supports_interrupt:
            return False
        return self.send_keys(target, "ctrl-c")

    def steer_urgent(
        self,
        target: str,
        instruction: str,
        operator: str = "human",
        wait_after_interrupt: float = 0.1,
    ) -> Dict[str, Any]:
        """Interrupt then inject high-priority instruction via TTY."""
        interrupted = False
        if self.supports_interrupt:
            interrupted = self.interrupt(target, reason="urgent_steer")
            if not interrupted:
                return {
                    "ok": False,
                    "interrupted": False,
                    "injected": False,
                    "reason": "interrupt_failed",
                    "detail": f"Failed to send interrupt signal to pane '{target}'",
                    "adapter": self.name,
                    "protocol_level": self.protocol_level,
                }
            if wait_after_interrupt > 0:
                time.sleep(wait_after_interrupt)

        prompt = self.format_steer_prompt(instruction, operator=operator)
        injected = self.inject_prompt(target, prompt)
        return {
            "ok": injected,
            "interrupted": interrupted,
            "injected": injected,
            "reason": None if injected else "inject_prompt_failed",
            "adapter": self.name,
            "protocol_level": self.protocol_level,
            "warning": (
                None if self.supports_interrupt
                else f"Agent '{self.name}' does not support interrupt signal; injected without interrupt"
            ),
        }

    def steer_soft(
        self,
        target: str,
        instruction: str,
        operator: str = "human",
    ) -> Dict[str, Any]:
        """Inject instruction without interrupting.

        Enforces capability: if supports_soft_steer is False, refuses to inject
        and returns ok=False. Does NOT fall through to TTY injection.
        """
        if not self.supports_soft_steer:
            return {
                "ok": False,
                "interrupted": False,
                "injected": False,
                "reason": "soft_steer_not_supported",
                "detail": (
                    f"Agent '{self.name}' does not support soft steer "
                    "(supports_soft_steer=False). Use urgent steer (with interrupt) instead."
                ),
                "adapter": self.name,
                "protocol_level": self.protocol_level,
            }

        prompt = self.format_steer_prompt(instruction, operator=operator)
        injected = self.inject_prompt(target, prompt)
        return {
            "ok": injected,
            "interrupted": False,
            "injected": injected,
            "reason": None if injected else "inject_prompt_failed",
            "adapter": self.name,
            "protocol_level": self.protocol_level,
        }

    def resume(self, target: str, **kwargs) -> bool:
        """Resume via TTY: send enter. Only if supports_resume."""
        if not self.supports_resume:
            return False
        return self.send_keys(target, "enter")


# Backwards compatibility alias
TTYSteeringPrototypeAdapter = TTYAgentAdapter


# ---------------------------------------------------------------------------
# Concrete TTY adapters — declare capabilities, inherit TTY implementation
# ---------------------------------------------------------------------------

class ClaudeAdapter(TTYAgentAdapter):
    """Adapter for Anthropic Claude Code CLI."""

    name = "claude"
    capabilities = AgentCapability(
        supports_interrupt=True,
        supports_soft_steer=True,
        supports_resume=True,
        supports_prompt_injection=True,
        protocol_level="tty_prototype",
    )


class CodexAdapter(TTYAgentAdapter):
    """Adapter for OpenAI Codex CLI."""

    name = "codex"
    capabilities = AgentCapability(
        supports_interrupt=True,
        supports_soft_steer=True,
        supports_resume=True,
        supports_prompt_injection=True,
        protocol_level="tty_prototype",
    )


class OpenCodeAdapter(TTYAgentAdapter):
    """Adapter for OpenCode CLI.

    OpenCode in auto-mode runs tool loops; soft-steer without interrupt
    gets ignored or swallowed by active tool executions.
    supports_soft_steer=False enforces this: soft steer is refused, not silently
    attempted.
    """

    name = "opencode"
    capabilities = AgentCapability(
        supports_interrupt=True,
        supports_soft_steer=False,
        supports_resume=False,
        supports_prompt_injection=True,
        protocol_level="tty_prototype",
    )


class QoderAdapter(TTYAgentAdapter):
    """Adapter for Qoder CLI."""

    name = "qodercli"
    capabilities = AgentCapability(
        supports_interrupt=True,
        supports_soft_steer=False,
        supports_resume=False,
        supports_prompt_injection=True,
        protocol_level="tty_prototype",
    )


class AgyAdapter(TTYAgentAdapter):
    """Adapter for Google Antigravity (Agy) CLI."""

    name = "agy"
    capabilities = AgentCapability(
        supports_interrupt=True,
        supports_soft_steer=True,
        supports_resume=True,
        supports_prompt_injection=True,
        protocol_level="tty_prototype",
    )


class PiAdapter(TTYAgentAdapter):
    """Adapter for Pi CLI."""

    name = "pi"
    capabilities = AgentCapability(
        supports_interrupt=True,
        supports_soft_steer=True,
        supports_resume=False,
        supports_prompt_injection=True,
        protocol_level="tty_prototype",
    )


class GrokAdapter(TTYAgentAdapter):
    """Adapter for Grok Build CLI."""

    name = "grok"
    capabilities = AgentCapability(
        supports_interrupt=True,
        supports_soft_steer=True,
        supports_resume=True,
        supports_prompt_injection=True,
        protocol_level="tty_prototype",
    )


class KimiAdapter(TTYAgentAdapter):
    """Adapter for Kimi Code CLI."""

    name = "kimi"
    capabilities = AgentCapability(
        supports_interrupt=True,
        supports_soft_steer=True,
        supports_resume=True,
        supports_prompt_injection=True,
        protocol_level="tty_prototype",
    )


# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------

_ADAPTER_REGISTRY: Dict[str, AgentAdapter] = {
    "tty_prototype": TTYAgentAdapter(),
    "claude": ClaudeAdapter(),
    "codex": CodexAdapter(),
    "opencode": OpenCodeAdapter(),
    "qodercli": QoderAdapter(),
    "agy": AgyAdapter(),
    "pi": PiAdapter(),
    "grok": GrokAdapter(),
    "kimi": KimiAdapter(),
}

_ALIASES: Dict[str, str] = {
    "qoder": "qodercli",
    "qodercn": "qodercli",
    "grokcli": "grok",
    "kimi-code": "kimi",
    "kimicli": "kimi",
}

_UNKNOWN_ADAPTER = UnknownAgentAdapter()


def register_agent_adapter(adapter: AgentAdapter) -> None:
    """Register or override an AgentAdapter in the global registry."""
    _ADAPTER_REGISTRY[adapter.name] = adapter


def get_agent_adapter(agent_name: Optional[str] = None) -> AgentAdapter:
    """Get the AgentAdapter for a given agent name.

    Resolves aliases. Returns UnknownAgentAdapter (fail-closed) if the agent
    name is None, empty, or not registered — steering is disabled for unknown
    agents until an explicit adapter is registered.
    """
    if not agent_name:
        return _UNKNOWN_ADAPTER

    clean_name = str(agent_name).strip().lower()
    resolved_name = _ALIASES.get(clean_name, clean_name)

    return _ADAPTER_REGISTRY.get(resolved_name, _UNKNOWN_ADAPTER)


def list_agent_adapters() -> Dict[str, Dict[str, Any]]:
    """List all registered adapters and their capabilities."""
    return {name: adapter.to_dict() for name, adapter in _ADAPTER_REGISTRY.items()}
