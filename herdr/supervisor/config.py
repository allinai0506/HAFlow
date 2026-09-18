#!/usr/bin/env python3
"""Semantic Supervisor configuration (herdr/supervisor/config.py).

Precedence: built-in defaults < ~/.herdr-controller/supervisor.json (env
``HERDR_SUPERVISOR_CONFIG`` overrides the path) < process environment.
Secrets (API keys) are resolved from environment variables only and are
never stored in the returned config mapping.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.herdr-controller/supervisor.json")

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "provider": "jev",
    "interval": 300,           # minimum seconds between evaluations per task
    "cooldown": 120,           # quiet window after one evaluation (aggregation)
    "max_context_size": 8000,  # serialized SupervisorState budget (chars)
    "max_calls_per_task": 12,  # hard budget against runaway supervision
    "recent_events_limit": 15,
    "enforce": False,          # V1 default: observe + decide, do not intervene
    "jev": {
        "enabled": True,
        "model": "jev-latest",
        "timeout": 20,
        "base_url": "https://api.typesafe.ai",
    },
    "signals": {},             # per-signal enable flags; {} = all built-ins on
    "thresholds": {
        "worker_stuck": 0.70,
        "work_off_track": 0.65,
        "meaningful_progress": 0.50,
        "requirements_satisfied": 0.70,
        "implementation_complete": 0.70,
        "tests_sufficient": 0.60,
        "needs_verification": 0.60,
        "needs_human": 0.60,
        "ready_to_finish": 0.80,
    },
    "policy": {
        # High-risk actions require trigger signals to clear their threshold
        # by this margin (binary judgments give no provider confidence, so
        # "how far above threshold" is the confidence proxy).
        "min_margin": 0.05,
        "max_attempts": 2,
        "max_verifications": 2,
        "allow_auto_execute": True,
        "allow_reroute": False,   # V1: decision may appear, never enforced
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
    """Load the ``supervisor`` section as a plain dict (defaults-filled)."""
    environ = env if env is not None else os.environ
    config_path = path or environ.get("HERDR_SUPERVISOR_CONFIG") or DEFAULT_CONFIG_PATH
    config = json.loads(json.dumps(DEFAULTS))  # deep copy of literals
    try:
        with open(config_path, encoding="utf-8") as handle:
            raw = json.load(handle)
        if isinstance(raw, dict):
            section = raw.get("supervisor") if isinstance(raw.get("supervisor"), dict) else raw
            config = _merge(config, section if isinstance(section, dict) else {})
    except (OSError, ValueError):
        pass

    # Normalize shorthand ``"jev": false`` into the dict form so every
    # downstream reader sees one shape (and false really means off).
    if config.get("jev") is False:
        config["jev"] = {"enabled": False}
    elif not isinstance(config.get("jev"), dict):
        config["jev"] = {}

    enabled = _flag(environ, "HERDR_SUPERVISOR_ENABLED")
    if enabled is not None:
        config["enabled"] = enabled
    enforce = _flag(environ, "HERDR_SUPERVISOR_ENFORCE")
    if enforce is not None:
        config["enforce"] = enforce
    provider = str(environ.get("HERDR_SUPERVISOR_PROVIDER", "")).strip()
    if provider:
        config["provider"] = provider
    _apply_numbers(config, environ)
    return config


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


_NUMBER_ENV = {
    "interval": "HERDR_SUPERVISOR_INTERVAL",
    "cooldown": "HERDR_SUPERVISOR_COOLDOWN",
    "max_context_size": "HERDR_SUPERVISOR_MAX_CONTEXT_SIZE",
    "max_calls_per_task": "HERDR_SUPERVISOR_MAX_CALLS_PER_TASK",
}


def _apply_numbers(config: Dict[str, Any], environ: dict) -> None:
    for key, name in _NUMBER_ENV.items():
        value = _env_num(environ, name)
        if value is not None:
            config[key] = value
    jev_flag = _flag(environ, "HERDR_SUPERVISOR_JEV_ENABLED")
    if jev_flag is not None:
        config["jev"]["enabled"] = jev_flag
    model = str(environ.get("HERDR_JEV_MODEL", "")).strip()
    if model:
        config["jev"]["model"] = model
    timeout = _env_num(environ, "HERDR_JEV_TIMEOUT")
    if timeout is not None:
        config["jev"]["timeout"] = timeout
    base_url = str(environ.get("HERDR_JEV_BASE_URL", "")).strip()
    if base_url:
        config["jev"]["base_url"] = base_url
    thresholds = environ.get("HERDR_SUPERVISOR_THRESHOLDS")
    if thresholds:
        # JSON overlay, e.g. HERDR_SUPERVISOR_THRESHOLDS='{"worker_stuck":0.8}'
        try:
            overlay = json.loads(thresholds)
            if isinstance(overlay, dict):
                config["thresholds"] = _merge(config["thresholds"], overlay)
        except ValueError:
            pass
    policy_overlay = environ.get("HERDR_SUPERVISOR_POLICY")
    if policy_overlay:
        try:
            overlay = json.loads(policy_overlay)
            if isinstance(overlay, dict):
                config["policy"] = _merge(config["policy"], overlay)
        except ValueError:
            pass


def _jev_section(config: Dict[str, Any]) -> Dict[str, Any]:
    """The jev sub-config in one shape (``"jev": false`` means disabled)."""
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


def supervisor_enabled(config: Dict[str, Any]) -> bool:
    if not config.get("enabled", False):
        return False
    if not provider_enabled(config):
        return False
    provider = config.get("provider")
    if provider == "jev":
        from ..decision.providers.jev import resolve_api_key
        return resolve_api_key(_jev_section(config)) is not None
    return bool(provider)
