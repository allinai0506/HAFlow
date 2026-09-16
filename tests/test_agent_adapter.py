"""Tests for AgentAdapter and Agent Capabilities Matrix (herdr/agent_adapter.py)."""

from unittest.mock import MagicMock, patch
import pytest

from herdr.agent_adapter import (
    AgentAdapter,
    AgentCapability,
    TTYAgentAdapter,
    TTYSteeringPrototypeAdapter,
    UnknownAgentAdapter,
    ClaudeAdapter,
    CodexAdapter,
    OpenCodeAdapter,
    QoderAdapter,
    AgyAdapter,
    PiAdapter,
    GrokAdapter,
    get_agent_adapter,
    register_agent_adapter,
    list_agent_adapters,
)


def test_agent_capability_defaults_fail_closed():
    """AgentCapability defaults must be all False (fail-closed security)."""
    cap = AgentCapability()
    assert cap.supports_interrupt is False
    assert cap.supports_soft_steer is False
    assert cap.supports_resume is False
    assert cap.supports_prompt_injection is False
    assert cap.protocol_level == "unknown"

    d = cap.to_dict()
    assert d["supports_interrupt"] is False
    assert d["protocol_level"] == "unknown"


def test_known_agent_adapters_capability_matrix():
    # Claude
    claude = get_agent_adapter("claude")
    assert isinstance(claude, ClaudeAdapter)
    assert isinstance(claude, TTYAgentAdapter)
    assert claude.name == "claude"
    assert claude.supports_interrupt is True
    assert claude.supports_soft_steer is True
    assert claude.supports_resume is True
    assert claude.supports_prompt_injection is True
    assert claude.protocol_level == "tty_prototype"

    # Codex
    codex = get_agent_adapter("codex")
    assert isinstance(codex, CodexAdapter)
    assert codex.name == "codex"
    assert codex.supports_interrupt is True
    assert codex.supports_soft_steer is True
    assert codex.supports_resume is True
    assert codex.supports_prompt_injection is True

    # OpenCode: auto-mode runs tool loops; soft steer without interrupt gets swallowed
    opencode = get_agent_adapter("opencode")
    assert isinstance(opencode, OpenCodeAdapter)
    assert opencode.name == "opencode"
    assert opencode.supports_interrupt is True
    assert opencode.supports_soft_steer is False
    assert opencode.supports_resume is False
    assert opencode.supports_prompt_injection is True

    # Qoder / qodercli: soft steer not supported
    qoder = get_agent_adapter("qodercli")
    assert isinstance(qoder, QoderAdapter)
    assert qoder.name == "qodercli"
    assert qoder.supports_interrupt is True
    assert qoder.supports_soft_steer is False
    assert qoder.supports_resume is False

    # Agy
    agy = get_agent_adapter("agy")
    assert isinstance(agy, AgyAdapter)
    assert agy.name == "agy"
    assert agy.supports_interrupt is True
    assert agy.supports_soft_steer is True
    assert agy.supports_resume is True

    # Pi
    pi = get_agent_adapter("pi")
    assert isinstance(pi, PiAdapter)
    assert pi.name == "pi"
    assert pi.supports_interrupt is True
    assert pi.supports_soft_steer is True
    assert pi.supports_resume is False

    # Grok
    grok = get_agent_adapter("grok")
    assert isinstance(grok, GrokAdapter)
    assert grok.name == "grok"
    assert grok.supports_interrupt is True
    assert grok.supports_soft_steer is True
    assert grok.supports_resume is True
    assert grok.supports_prompt_injection is True


def test_adapter_registry_aliases_and_fallback():
    # Alias qoder -> qodercli
    q1 = get_agent_adapter("qoder")
    q2 = get_agent_adapter("qodercn")
    assert isinstance(q1, QoderAdapter)
    assert isinstance(q2, QoderAdapter)

    # Alias grokcli -> grok
    g1 = get_agent_adapter("grokcli")
    assert isinstance(g1, GrokAdapter)

    # Unknown agent falls back to UnknownAgentAdapter (fail closed)
    fallback = get_agent_adapter("unknown-llm-bot")
    assert isinstance(fallback, UnknownAgentAdapter)
    assert fallback.name == "unknown"
    assert fallback.protocol_level == "unknown"
    assert fallback.supports_interrupt is False
    assert fallback.supports_soft_steer is False

    # None falls back to UnknownAgentAdapter (fail closed)
    none_adapter = get_agent_adapter(None)
    assert isinstance(none_adapter, UnknownAgentAdapter)


def test_unknown_agent_refuses_all_steering():
    """Test 1: Unknown agent must fail-closed and refuse all steering operations."""
    adapter = get_agent_adapter("unregistered-agent-xyz")
    assert isinstance(adapter, UnknownAgentAdapter)

    # Urgent steer refused
    urgent_res = adapter.steer_urgent("pane-1", "Emergency stop")
    assert urgent_res["ok"] is False
    assert urgent_res["reason"] == "unknown_agent_no_adapter_registered"
    assert urgent_res["interrupted"] is False
    assert urgent_res["injected"] is False

    # Soft steer refused
    soft_res = adapter.steer_soft("pane-1", "Gentle correction")
    assert soft_res["ok"] is False
    assert soft_res["reason"] == "unknown_agent_no_adapter_registered"

    # Interrupt and resume refused
    assert adapter.interrupt("pane-1") is False
    assert adapter.resume("pane-1") is False


def test_soft_steer_not_supported_refuses_tty_injection():
    """Test 2: When supports_soft_steer=False, steer_soft must refuse and NOT inject into TTY."""
    opencode = get_agent_adapter("opencode")
    qoder = get_agent_adapter("qodercli")

    assert opencode.supports_soft_steer is False
    assert qoder.supports_soft_steer is False

    mock_run = MagicMock()
    with patch("subprocess.run", mock_run):
        res_opencode = opencode.steer_soft("pane-opencode", "Please adjust algorithm")
        res_qoder = qoder.steer_soft("pane-qoder", "Please adjust algorithm")

    # Both must refuse execution
    assert res_opencode["ok"] is False
    assert res_opencode["reason"] == "soft_steer_not_supported"
    assert res_qoder["ok"] is False
    assert res_qoder["reason"] == "soft_steer_not_supported"

    # CRITICAL: zero TTY subprocess calls must have been made!
    assert mock_run.call_count == 0


def test_agent_adapter_contract_independent_of_tty():
    """Base AgentAdapter has no TTY knowledge; transport-agnostic adapters can be cleanly implemented."""
    class MockRpcAdapter(AgentAdapter):
        name = "mock_rpc"
        capabilities = AgentCapability(
            supports_interrupt=True,
            supports_soft_steer=True,
            supports_resume=True,
            supports_prompt_injection=True,
            protocol_level="native_rpc",
        )

        def __init__(self):
            self.calls = []

        def interrupt(self, target: str, reason: str = "") -> bool:
            self.calls.append(("interrupt", target, reason))
            return True

        def steer_urgent(self, target: str, instruction: str, operator: str = "human", wait_after_interrupt: float = 0.1):
            self.calls.append(("steer_urgent", target, instruction))
            return {"ok": True, "interrupted": True, "injected": True, "adapter": self.name, "protocol_level": self.protocol_level}

        def steer_soft(self, target: str, instruction: str, operator: str = "human"):
            self.calls.append(("steer_soft", target, instruction))
            return {"ok": True, "interrupted": False, "injected": True, "adapter": self.name, "protocol_level": self.protocol_level}

        def resume(self, target: str, **kwargs) -> bool:
            self.calls.append(("resume", target))
            return True

    rpc_adapter = MockRpcAdapter()
    register_agent_adapter(rpc_adapter)

    retrieved = get_agent_adapter("mock_rpc")
    assert retrieved.name == "mock_rpc"
    assert retrieved.protocol_level == "native_rpc"
    assert retrieved.supports_interrupt is True

    res = retrieved.steer_urgent("node-endpoint-1", "Switch to fallback")
    assert res["ok"] is True
    assert res["protocol_level"] == "native_rpc"
    assert ("steer_urgent", "node-endpoint-1", "Switch to fallback") in rpc_adapter.calls


def test_list_agent_adapters():
    adapters = list_agent_adapters()
    assert "claude" in adapters
    assert "codex" in adapters
    assert "opencode" in adapters
    assert "qodercli" in adapters
    assert "agy" in adapters
    assert "pi" in adapters
    assert "grok" in adapters
    assert "tty_prototype" in adapters

    claude_info = adapters["claude"]
    assert claude_info["capabilities"]["supports_interrupt"] is True
    assert claude_info["protocol_level"] == "tty_prototype"


def test_adapter_steer_urgent_success():
    adapter = CodexAdapter()
    mock_run = MagicMock()
    mock_run.return_value.returncode = 0

    with patch("subprocess.run", mock_run):
        res = adapter.steer_urgent("pane-123", "Stop and refactor", operator="tester", wait_after_interrupt=0.01)

    assert res["ok"] is True
    assert res["interrupted"] is True
    assert res["injected"] is True
    assert res["adapter"] == "codex"
    assert res["protocol_level"] == "tty_prototype"

    # Verify subprocess calls: send-keys ctrl-c, send-text prompt, send-keys enter
    calls = mock_run.call_args_list
    assert len(calls) == 3
    assert "ctrl-c" in calls[0][0][0]
    assert "send-text" in calls[1][0][0]
    assert "Stop and refactor" in calls[1][0][0][4]
    assert "enter" in calls[2][0][0]


def test_adapter_steer_urgent_interrupt_failure():
    """If interrupt fails during steer_urgent, the operation must abort with ok=False."""
    adapter = ClaudeAdapter()
    mock_run = MagicMock()
    mock_run.return_value.returncode = 1  # simulate failure

    with patch("subprocess.run", mock_run):
        res = adapter.steer_urgent("pane-fail", "Stop", operator="tester", wait_after_interrupt=0.0)

    assert res["ok"] is False
    assert res["interrupted"] is False
    assert res["reason"] == "interrupt_failed"


def test_adapter_steer_soft_success():
    adapter = ClaudeAdapter()
    mock_run = MagicMock()
    mock_run.return_value.returncode = 0

    with patch("subprocess.run", mock_run):
        res = adapter.steer_soft("pane-456", "Keep standard library only", operator="alice")

    assert res["ok"] is True
    assert res["interrupted"] is False
    assert res["injected"] is True
    assert res["adapter"] == "claude"

    calls = mock_run.call_args_list
    # Soft steer does NOT call ctrl-c
    assert not any("ctrl-c" in c[0][0] for c in calls)
    assert any("send-text" in c[0][0] for c in calls)
    assert any("enter" in c[0][0] for c in calls)


def test_adapter_resume():
    codex = CodexAdapter()
    opencode = OpenCodeAdapter()

    mock_run = MagicMock()
    mock_run.return_value.returncode = 0

    with patch("subprocess.run", mock_run):
        assert codex.resume("pane-111") is True
        assert opencode.resume("pane-222") is False
