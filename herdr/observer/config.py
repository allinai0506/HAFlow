#!/usr/bin/env python3
"""Trajectory Observer configuration (herdr/observer/config.py).

Precedence: built-in defaults < ~/.herdr-controller/observer.json (env
``HERDR_OBSERVER_CONFIG`` overrides the path) < process environment.

The observer is an optional, best-effort观察层: the kill switch is either the
``enabled`` flag or the provider enablement. Secrets stay in the environment
(JEV_API_KEY via the shared DecisionProvider) and are never stored here.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.herdr-controller/observer.json")

# Product minimum for max_context_size: load_config clamps it explicitly so a
# configured 100 can never silently become 500 inside _fit_budget.
MIN_MAX_CONTEXT_SIZE = 500

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "provider": "jev",
    "interval": 300,              # minimum seconds between observations per run
    "max_calls_per_run": 24,      # process-local per-run observation budget (resets on controller restart)
    "recent_events": 50,          # trajectory window sent to the provider
    "verification_events": 5,     # verification rows kept in the context
    "max_findings": 10,           # cap per observation
    "max_context_size": 8000,     # serialized ObservationContext budget (min 500, clamped on load)
    "confidence_threshold": 0.6,  # provider probability required to confirm
    "stall_after_seconds": 1800,  # time-only stall suspect (never critical alone)
    "repeated_failure_min": 2,    # consecutive failed verifications
    "repeated_action_min": 3,     # identical failing action signature
    "no_progress_min_reworks": 3, # rework loops without a later passing check
    "log_repeat_min": 3,          # repeated identical error signature in tail
    "log_tail_lines": 200,
    "log_tail_bytes": 16384,
    "log_tail_chars": 4000,
    "live_probe": True,           # read-only pane/agent liveness probe per observation
    "live_probe_timeout": 2.0,    # seconds per herdr subprocess (bounded)
    "live_transcript_timeout": 3.0,
    "jev": {
        "enabled": True,
        "model": "jev-latest",
        "timeout": 20,
        "base_url": "https://api.typesafe.ai",
    },
}


def _merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: Optional[str] = None, env: Optional[dict] = None) -> Dict[str, Any]:
    """Load the observer config as a plain dict (defaults-filled).

    ``path=""`` explicitly disables file loading (tests); ``path=None`` uses
    the environment/default location.
    """
    environ = env if env is not None else os.environ
    if path is None:
        config_path: Optional[str] = environ.get("HERDR_OBSERVER_CONFIG") or DEFAULT_CONFIG_PATH
    else:
        config_path = path or None
    config = json.loads(json.dumps(DEFAULTS))  # deep copy of literals

    if config_path:
        try:
            with open(config_path, encoding="utf-8") as handle:
                raw = json.load(handle)
            if isinstance(raw, dict):
                section = raw.get("observer") if isinstance(raw.get("observer"), dict) else raw
                config = _merge(config, section if isinstance(section, dict) else {})
        except (OSError, ValueError):
            pass

    if config.get("jev") is False:
        config["jev"] = {"enabled": False}
    elif not isinstance(config.get("jev"), dict):
        config["jev"] = {}

    enabled = _flag(environ, "HERDR_OBSERVER_ENABLED")
    if enabled is not None:
        config["enabled"] = enabled
    live_probe = _flag(environ, "HERDR_OBSERVER_LIVE_PROBE")
    if live_probe is not None:
        config["live_probe"] = live_probe
    provider = str(environ.get("HERDR_OBSERVER_PROVIDER", "")).strip()
    if provider:
        config["provider"] = provider
    _apply_numbers(config, environ)
    jev_flag = _flag(environ, "HERDR_OBSERVER_JEV_ENABLED")
    if jev_flag is not None:
        config["jev"]["enabled"] = jev_flag
    for env_name, key in (
        ("HERDR_OBSERVER_JEV_MODEL", "model"),
        ("HERDR_OBSERVER_JEV_BASE_URL", "base_url"),
    ):
        value = str(environ.get(env_name, "")).strip()
        if value:
            config["jev"][key] = value
    timeout = _env_num(environ, "HERDR_OBSERVER_JEV_TIMEOUT")
    if timeout is not None:
        config["jev"]["timeout"] = timeout
    try:
        configured = int(config.get("max_context_size", 8000))
    except (TypeError, ValueError):
        configured = 8000
    config["max_context_size"] = max(MIN_MAX_CONTEXT_SIZE, configured)
    return config


_NUMBER_ENV = {
    "interval": "HERDR_OBSERVER_INTERVAL",
    "max_calls_per_run": "HERDR_OBSERVER_MAX_CALLS_PER_RUN",
    "recent_events": "HERDR_OBSERVER_RECENT_EVENTS",
    "max_context_size": "HERDR_OBSERVER_MAX_CONTEXT_SIZE",
    "confidence_threshold": "HERDR_OBSERVER_THRESHOLD",
    "stall_after_seconds": "HERDR_OBSERVER_STALL_SECONDS",
    "log_tail_lines": "HERDR_OBSERVER_LOG_LINES",
    "log_tail_bytes": "HERDR_OBSERVER_LOG_BYTES",
    "log_tail_chars": "HERDR_OBSERVER_LOG_CHARS",
    "live_probe_timeout": "HERDR_OBSERVER_LIVE_PROBE_TIMEOUT",
    "live_transcript_timeout": "HERDR_OBSERVER_LIVE_TRANSCRIPT_TIMEOUT",
}


def _flag(environ: dict, name: str) -> Optional[bool]:
    raw = str(environ.get(name, "")).strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return None


def _env_num(environ: dict, name: str) -> Optional[float]:
    raw = str(environ.get(name, "")).strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _apply_numbers(config: Dict[str, Any], environ: dict) -> None:
    for key, name in _NUMBER_ENV.items():
        value = _env_num(environ, name)
        if value is None:
            continue
        if key == "confidence_threshold" or key.endswith("_timeout"):
            config[key] = value
        else:
            config[key] = int(value)


def _jev_section(config: Dict[str, Any]) -> Dict[str, Any]:
    jev = config.get("jev")
    if jev is False:
        return {"enabled": False}
    return jev if isinstance(jev, dict) else {}


def jev_provider_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Provider kwargs for the jev DecisionProvider (no secret values)."""
    jev = _jev_section(config)
    return {
        "enabled": bool(jev.get("enabled", True)),
        "model": jev.get("model") or "jev-latest",
        "timeout": jev.get("timeout") or 20,
        "base_url": jev.get("base_url"),
        "api_key_env": jev.get("api_key_env"),
    }


def provider_enabled(config: Dict[str, Any]) -> bool:
    """Provider-specific enablement only (no credential handling)."""
    provider = config.get("provider")
    if not provider:
        return False
    if provider == "jev":
        if not _jev_section(config).get("enabled", True):
            return False
    return True
